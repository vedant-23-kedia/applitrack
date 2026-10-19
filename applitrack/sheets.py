"""Write the tracker to a Google Sheet.

The sheet is rewritten in full on each sync rather than diffed, because the
source of truth is the mailbox -- anything typed into the generated columns
would be overwritten anyway. The "My Notes" column at the far right is left
untouched so there is somewhere safe to write by hand.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import COLUMNS, Application, Status

STATUS_COLORS = {
    "Offer":           {"red": 0.78, "green": 0.93, "blue": 0.78},
    "Interview":       {"red": 0.80, "green": 0.88, "blue": 0.97},
    "Assessment":      {"red": 0.99, "green": 0.91, "blue": 0.75},
    "Action Required": {"red": 1.00, "green": 0.90, "blue": 0.70},
    "Acknowledged":    {"red": 0.95, "green": 0.95, "blue": 0.95},
    "Applied":         {"red": 0.95, "green": 0.95, "blue": 0.95},
    "Rejected":        {"red": 0.98, "green": 0.82, "blue": 0.82},
    "Ghosted":         {"red": 0.90, "green": 0.90, "blue": 0.90},
    "Withdrawn":       {"red": 0.90, "green": 0.90, "blue": 0.90},
}

NOTES_COLUMN = "My Notes"
SHEET_TITLE = "Applications"
SUMMARY_TITLE = "Summary"


class SheetsWriter:
    def __init__(self, service=None, spreadsheet_id: str | None = None):
        if service is None:
            from .auth import build_service

            service = build_service("sheets", "v4")
        self.service = service
        self.spreadsheet_id = spreadsheet_id

    # -- creation ----------------------------------------------------------

    def create(self, title: str = "Job Application Tracker") -> str:
        body = {
            "properties": {"title": title},
            "sheets": [
                {"properties": {"title": SHEET_TITLE, "gridProperties": {"frozenRowCount": 1}}},
                {"properties": {"title": SUMMARY_TITLE}},
            ],
        }
        result = self.service.spreadsheets().create(body=body, fields="spreadsheetId").execute()
        self.spreadsheet_id = result["spreadsheetId"]
        return self.spreadsheet_id

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"

    # -- writing -----------------------------------------------------------

    def write(self, apps: list[Application], summary: dict | None = None) -> str:
        """Replace the tracker contents, preserving the hand-written notes."""
        if not self.spreadsheet_id:
            self.create()

        existing_notes = self._read_notes()
        self._ensure_sheets()

        header = COLUMNS + [NOTES_COLUMN]
        rows = [header]
        for app in apps:
            row = [str(app.to_row()[c]) for c in COLUMNS]
            row.append(existing_notes.get(_note_key(app), ""))
            rows.append(row)

        sheet_id = self._sheet_id(SHEET_TITLE)

        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id, range=f"{SHEET_TITLE}!A:Z"
        ).execute()

        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{SHEET_TITLE}!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()

        self._format(sheet_id, apps, len(header))

        if summary is not None:
            self._write_summary(summary, len(apps))

        return self.url

    def _read_notes(self) -> dict[str, str]:
        """Keep whatever the user typed in the notes column across syncs."""
        try:
            resp = (
                self.service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=f"{SHEET_TITLE}!A:Z")
                .execute()
            )
        except Exception:
            return {}

        values = resp.get("values", [])
        if len(values) < 2:
            return {}

        header = values[0]
        if NOTES_COLUMN not in header:
            return {}
        note_idx = header.index(NOTES_COLUMN)
        try:
            company_idx = header.index("Company")
            role_idx = header.index("Role")
        except ValueError:
            return {}

        notes: dict[str, str] = {}
        for row in values[1:]:
            if len(row) > note_idx and row[note_idx].strip():
                company = row[company_idx] if len(row) > company_idx else ""
                role = row[role_idx] if len(row) > role_idx else ""
                notes[f"{company}|{role}".lower()] = row[note_idx]
        return notes

    def _ensure_sheets(self):
        meta = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        have = {s["properties"]["title"] for s in meta["sheets"]}
        requests = [
            {"addSheet": {"properties": {"title": title}}}
            for title in (SHEET_TITLE, SUMMARY_TITLE)
            if title not in have
        ]
        if requests:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}
            ).execute()

    def _sheet_id(self, title: str) -> int:
        meta = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        for sheet in meta["sheets"]:
            if sheet["properties"]["title"] == title:
                return sheet["properties"]["sheetId"]
        raise KeyError(title)

    def _format(self, sheet_id: int, apps: list[Application], width: int):
        requests = [
            {  # bold header
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {
                        "userEnteredFormat": {
                            # Text colour belongs inside textFormat. At the
                            # CellFormat level the API rejects the whole
                            # request with "Unknown name foregroundColorStyle".
                            "textFormat": {
                                "bold": True,
                                "foregroundColorStyle": {
                                    "rgbColor": {"red": 1, "green": 1, "blue": 1}
                                },
                            },
                            "backgroundColor": {"red": 0.2, "green": 0.25, "blue": 0.32},
                        }
                    },
                    "fields": "userEnteredFormat(textFormat,backgroundColor)",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": width,
                    }
                }
            },
        ]

        status_col = COLUMNS.index("Status")
        for i, app in enumerate(apps, start=1):
            color = STATUS_COLORS.get(app.status.value)
            if not color:
                continue
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": status_col,
                        "endColumnIndex": status_col + 1,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        ).execute()

    def _write_summary(self, summary: dict, total: int):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rows = [
            ["Job Application Tracker"],
            ["Last synced", now],
            [],
            ["Total applications", total],
            ["Still in progress", summary.get("active", 0)],
            ["Response rate", f"{summary.get('response_rate', 0):.0%}"],
            [],
            ["Status", "Count"],
        ]
        for status in Status:
            count = summary.get("by_status", {}).get(status.value)
            if count:
                rows.append([status.value, count])

        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id, range=f"{SUMMARY_TITLE}!A:Z"
        ).execute()
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{SUMMARY_TITLE}!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()


def _note_key(app: Application) -> str:
    return f"{app.company}|{app.role}".lower()

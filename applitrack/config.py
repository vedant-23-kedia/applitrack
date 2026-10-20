"""Configuration: YAML file, overridden by environment variables.

Secrets (SMTP password, API keys) should come from the environment, not the
YAML file, so the config can be committed without leaking anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import DEFAULT_LOOKBACK_DAYS

DEFAULT_PATH = "config.yaml"

DEFAULTS = {
    "lookback_days": DEFAULT_LOOKBACK_DAYS,
    "max_emails": 1500,
    "ghost_after_days": 45,
    "min_score": None,            # None = use rules.MIN_SCORE
    "db_path": "data/applitrack.db",
    "xlsx_path": "data/applications.xlsx",
    "csv_path": "data/applications.csv",
    "spreadsheet_id": "",
    "spreadsheet_title": "Job Application Tracker",
    "use_llm": False,
    "llm_model": "claude-sonnet-5-5",
    "notify": {"desktop": True, "email_to": ""},
    "smtp": {"host": "smtp.gmail.com", "port": 587, "user": "", "password": ""},
    "extra_query": "",
    "ignore_senders": [],
    "ignore_companies": [],
}


@dataclass
class Config:
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    max_emails: int = 1500
    ghost_after_days: int = 45
    min_score: int | None = None
    db_path: str = "data/applitrack.db"
    xlsx_path: str = "data/applications.xlsx"
    csv_path: str = "data/applications.csv"
    spreadsheet_id: str = ""
    spreadsheet_title: str = "Job Application Tracker"
    use_llm: bool = False
    llm_model: str = "claude-sonnet-5-5"
    notify: dict = field(default_factory=lambda: dict(DEFAULTS["notify"]))
    smtp: dict = field(default_factory=lambda: dict(DEFAULTS["smtp"]))
    extra_query: str = ""
    ignore_senders: list = field(default_factory=list)
    ignore_companies: list = field(default_factory=list)
    path: str = DEFAULT_PATH

    @classmethod
    def load(cls, path: str = DEFAULT_PATH) -> "Config":
        data = dict(DEFAULTS)
        file = Path(path)
        if file.exists():
            try:
                import yaml

                loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            except ImportError:
                loaded = {}
            for key, value in loaded.items():
                if isinstance(value, dict) and isinstance(data.get(key), dict):
                    data[key] = {**data[key], **value}
                else:
                    data[key] = value

        # Environment wins, so secrets never have to be written to disk.
        env_map = {
            "APPLITRACK_SPREADSHEET_ID": "spreadsheet_id",
            "APPLITRACK_DB": "db_path",
            "APPLITRACK_LOOKBACK_DAYS": "lookback_days",
            "APPLITRACK_EMAIL_TO": None,
            "APPLITRACK_SMTP_USER": None,
            "APPLITRACK_SMTP_PASSWORD": None,
            "APPLITRACK_SMTP_HOST": None,
        }
        for env_key, field_name in env_map.items():
            value = os.environ.get(env_key)
            if not value:
                continue
            if field_name:
                data[field_name] = int(value) if field_name.endswith("_days") else value
            elif env_key == "APPLITRACK_EMAIL_TO":
                data["notify"]["email_to"] = value
            elif env_key == "APPLITRACK_SMTP_USER":
                data["smtp"]["user"] = value
            elif env_key == "APPLITRACK_SMTP_PASSWORD":
                data["smtp"]["password"] = value
            elif env_key == "APPLITRACK_SMTP_HOST":
                data["smtp"]["host"] = value

        data["path"] = path
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        import yaml

        payload = {
            k: v for k, v in self.__dict__.items()
            if k != "path" and not (k == "smtp" and not v.get("user"))
        }
        Path(self.path).write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )

    @property
    def can_email(self) -> bool:
        return bool(self.notify.get("email_to") and self.smtp.get("user") and self.smtp.get("password"))

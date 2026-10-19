@echo off
REM AppliTrack - sync Gmail, refresh the Excel file and the Google Sheet.
REM Double-click this, or point Windows Task Scheduler at it.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m applitrack.cli sync --sheet --notify %*
) else (
  python -m applitrack.cli sync --sheet --notify %*
)
if "%1"=="" pause

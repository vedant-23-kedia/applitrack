@echo off
REM Show the digest without touching Gmail.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m applitrack.cli digest
) else (
  python -m applitrack.cli digest
)
pause

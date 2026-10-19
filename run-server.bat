@echo off
REM Start the AppliTrack web service (multi-user).
cd /d "%~dp0"
if "%APPLITRACK_SECRET_KEY%"=="" (
  echo APPLITRACK_SECRET_KEY is not set.
  echo Generate one with:  python -m applitrack.web.cli keygen
  pause
  exit /b 1
)
if exist .venv\Scripts\python.exe (
  set PY=.venv\Scripts\python.exe
) else (
  set PY=python
)
%PY% -m applitrack.web.cli check
if errorlevel 1 ( pause & exit /b 1 )
%PY% -m applitrack.web.cli serve --host 0.0.0.0 --port 5000
pause

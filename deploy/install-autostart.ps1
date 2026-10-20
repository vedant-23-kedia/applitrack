<#
    Register AppliTrack to start automatically when you log in.

    Creates a Scheduled Task that launches the server with pythonw.exe, so
    there is no console window. The task runs as you, only when you are
    logged in -- it does not need admin rights and cannot see anything you
    could not already see.

    Install:    powershell -ExecutionPolicy Bypass -File deploy\install-autostart.ps1
    Remove:     powershell -ExecutionPolicy Bypass -File deploy\install-autostart.ps1 -Uninstall
#>

param(
    [switch]$Uninstall,
    [string]$TaskName = "AppliTrack"
)

$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $PSScriptRoot
$script  = Join-Path $project "deploy\serve_windows.py"

function Resolve-Pythonw {
    # Prefer the project's venv, fall back to whatever python is on PATH.
    $venv = Join-Path $project ".venv\Scripts\pythonw.exe"
    if (Test-Path $venv) { return $venv }

    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw "python not found on PATH." }

    $pythonw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
    if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $py" }
    return $pythonw
}

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the '$TaskName' startup task." -ForegroundColor Green
    } else {
        Write-Host "No '$TaskName' task was registered." -ForegroundColor Yellow
    }
    Write-Host "AppliTrack will no longer start automatically."
    exit 0
}

# ------------------------------------------------------------------ install
Write-Host "Installing AppliTrack autostart" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $script)) { throw "Cannot find $script" }

$pythonw = Resolve-Pythonw
Write-Host "  project : $project"
Write-Host "  python  : $pythonw"

# Fail early on the things that silently break a background start.
foreach ($required in @(".env", "credentials.json")) {
    $path = Join-Path $project $required
    if (-not (Test-Path $path)) {
        Write-Host ""
        Write-Host "  MISSING: $required" -ForegroundColor Red
        Write-Host "  The server cannot start without it. See README.md."
        exit 1
    }
}
Write-Host "  config  : .env and credentials.json present"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "  (replacing the existing task)" -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $pythonw `
                                  -Argument "`"$script`"" `
                                  -WorkingDirectory $project

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Restart if it dies; do not stop it just because the laptop is on battery.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Starts the AppliTrack job application tracker at logon." `
    | Out-Null

Write-Host ""
Write-Host "Registered. AppliTrack will start every time you log in." -ForegroundColor Green
Write-Host ""
Write-Host "  Start it now      : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Open it           : http://localhost:5000"
Write-Host "  Logs              : $project\data\server.log"
Write-Host "  Stop it           : Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Remove autostart  : powershell -ExecutionPolicy Bypass -File deploy\install-autostart.ps1 -Uninstall"

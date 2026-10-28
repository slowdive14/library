# Registers a Task Scheduler task that keeps the bot running on this Windows PC.
# - Runs .venv\Scripts\pythonw.exe bot.py with no console window while you are signed in.
# - Fires every 5 minutes. If the bot is already running the new start is ignored, so it
#   comes back within 5 minutes after sign-in, a crash or a startup without network.
# - The log goes to bot.log in the project folder (rotated at 5 MB).
#
# Usage (in the project folder, no admin needed):
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1
# Stop for good:  Disable-ScheduledTask -TaskName LibraryBot; Stop-ScheduledTask -TaskName LibraryBot
# Remove:         Unregister-ScheduledTask -TaskName LibraryBot -Confirm:$false

$ErrorActionPreference = 'Stop'
$TaskName = 'LibraryBot'
$AppDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$PythonW = Join-Path $AppDir '.venv\Scripts\pythonw.exe'

if (-not (Test-Path (Join-Path $AppDir '.env'))) {
    throw "Missing $AppDir\.env. Copy .env.example to .env and fill it in first."
}
if (-not (Test-Path $PythonW)) {
    Write-Host 'Creating .venv and installing requirements...'
    python -m venv (Join-Path $AppDir '.venv')
    & (Join-Path $AppDir '.venv\Scripts\python.exe') -m pip install -q -r (Join-Path $AppDir 'requirements.txt')
}

$action = New-ScheduledTaskAction -Execute $PythonW -Argument 'bot.py' -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Bucheon library Telegram bot (restarted within 5 minutes if it stops)' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Registered and started '$TaskName'."
Write-Host "Log: $AppDir\bot.log"

# Register the JARVIS dispatcher service: starts at logon and now, runs hidden as the current user.
# Stop it with JarvisWorkspace\.jarvis-stop, or set "enabled": false in config\dispatcher.json.
param([switch]$StartNow)

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$workspace = Join-Path $root 'JarvisWorkspace'
$command = "`$env:JARVIS_WORKSPACE='$workspace'; `$env:JARVIS_DOWNLOADS_READONLY='1'; Set-Location '$root'; & '$python' -m jarvis.cli.dispatch serve"

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$command`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'JARVIS Dispatcher' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'JARVIS W5 dispatcher: wakes AI employees with board work. Never merges to main.' -Force | Out-Null
if ($StartNow) { Start-ScheduledTask -TaskName 'JARVIS Dispatcher' }
Get-ScheduledTask -TaskName 'JARVIS Dispatcher' | Select-Object TaskName, State

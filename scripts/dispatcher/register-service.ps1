# Register the JARVIS dispatcher service: starts at logon and now, runs hidden as the current user.
# Stop it with JarvisWorkspace\.jarvis-stop, or set "enabled": false in config\dispatcher.json.
param([switch]$StartNow)

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
# Run python directly (no PowerShell wrapper) so ending the task ends the service, and the service's
# kill-on-close job ends any employee it started. The default workspace is <root>\JarvisWorkspace.
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $python)) { $python = Join-Path $root '.venv\Scripts\python.exe' }

$action = New-ScheduledTaskAction -Execute $python -Argument '-m jarvis.cli.dispatch serve' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'JARVIS Dispatcher' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'JARVIS W5 dispatcher: wakes AI employees with board work. Never merges to main.' -Force | Out-Null
if ($StartNow) { Start-ScheduledTask -TaskName 'JARVIS Dispatcher' }
Get-ScheduledTask -TaskName 'JARVIS Dispatcher' | Select-Object TaskName, State

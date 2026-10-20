# Register a daily Windows scheduled task that runs backup.ps1 as the current user.
# Missed runs (PC off) start at the next opportunity.
param([string]$At = '23:00')

$script = Join-Path $PSScriptRoot 'backup.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'JARVIS Backup' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Encrypted restic backup of JarvisWorkspace and .jarvis-auth to Cloudflare R2' -Force | Out-Null
Get-ScheduledTask -TaskName 'JARVIS Backup' | Select-Object TaskName, State

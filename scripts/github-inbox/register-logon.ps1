# Opt-in GitHub inbox sync on user logon. Does not start AI agents.
param([switch]$StartNow)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$workspace = Join-Path $root 'JarvisWorkspace'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python venv missing: $python" }
if (-not (Test-Path -LiteralPath $workspace -PathType Container)) { throw "Workspace missing: $workspace" }
if (-not [Environment]::GetEnvironmentVariable('GITHUB_TOKEN','User')) {
    throw 'Set a read-only GITHUB_TOKEN in the user environment before registering.'
}
$arguments = '-m jarvis.cli.github_inbox --workspace "' + $workspace + '" --apply'
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName 'JARVIS GitHub Inbox' -Action $action -Trigger $trigger -Settings $settings -Description 'Import approved GitHub Issues into local board at logon; never run tasks' -Force | Out-Null
if ($StartNow) { Start-ScheduledTask -TaskName 'JARVIS GitHub Inbox' }
Get-ScheduledTask -TaskName 'JARVIS GitHub Inbox' | Select-Object TaskName,State

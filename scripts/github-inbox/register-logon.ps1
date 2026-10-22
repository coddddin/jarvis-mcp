# Opt-in GitHub inbox sync on user logon. Does not start AI agents.
param([switch]$StartNow)
$ErrorActionPreference = 'Stop'
$taskName = 'JARVIS GitHub Inbox'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'Inbox task already exists; registration refused. Review it locally before making changes.'
}
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
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Import approved GitHub Issues into local board at logon; never run tasks' | Out-Null
if ($StartNow) { Start-ScheduledTask -TaskName 'JARVIS GitHub Inbox' }
Get-ScheduledTask -TaskName 'JARVIS GitHub Inbox' | Select-Object TaskName,State


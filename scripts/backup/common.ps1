# Shared settings for JARVIS encrypted backup (restic -> Cloudflare R2).
# Secrets live outside the project, encrypted with Windows DPAPI for the current user.

$ErrorActionPreference = 'Stop'
$BackupHome   = Join-Path $env:LOCALAPPDATA 'JarvisBackup'
$SecretsPath  = Join-Path $BackupHome 'secrets.clixml'
$StagingPath  = Join-Path $BackupHome 'staging'
$LogPath      = Join-Path $BackupHome 'backup.log'
$ProjectRoot  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$PythonExe    = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

function Get-ResticExe {
    $cmd = Get-Command restic -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $found = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') -Recurse -Filter 'restic*.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $found) { throw 'restic not found. Install: winget install --id restic.restic -e' }
    return $found
}

function ConvertTo-Plain([securestring]$Secure) {
    return [System.Net.NetworkCredential]::new('', $Secure).Password
}

function Use-BackupSecrets {
    if (-not (Test-Path $SecretsPath)) { throw "Secrets not set up. Run setup-secrets.ps1 first ($SecretsPath)." }
    $s = Import-Clixml $SecretsPath
    $env:AWS_ACCESS_KEY_ID     = ConvertTo-Plain $s.AccessKeyId
    $env:AWS_SECRET_ACCESS_KEY = ConvertTo-Plain $s.SecretAccessKey
    $env:RESTIC_PASSWORD       = ConvertTo-Plain $s.ResticPassword
    $env:AWS_DEFAULT_REGION    = 'auto'
    $env:RESTIC_REPOSITORY     = "s3:https://$($s.AccountId).r2.cloudflarestorage.com/$($s.Bucket)"
}

function Clear-BackupSecrets {
    foreach ($name in 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'RESTIC_PASSWORD', 'AWS_DEFAULT_REGION', 'RESTIC_REPOSITORY') {
        Remove-Item "env:$name" -ErrorAction SilentlyContinue
    }
}

function Write-BackupLog([string]$Message) {
    New-Item -ItemType Directory -Force $BackupHome | Out-Null
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" | Add-Content -Path $LogPath -Encoding utf8
}

# One-time setup: run in your own PowerShell window. Values are typed here, never shown to an AI session.
. (Join-Path $PSScriptRoot 'common.ps1')

Write-Host ''
Write-Host '=== JARVIS 백업 설정 (Cloudflare R2) ===' -ForegroundColor Cyan
Write-Host '입력값은 이 PC의 Windows 계정 암호화(DPAPI)로만 저장됩니다.'
Write-Host ''
$accountId = Read-Host 'Cloudflare Account ID'
$bucket    = Read-Host 'R2 버킷 이름 (예: jarvis-backup)'
$accessKey = Read-Host 'R2 Access Key ID' -AsSecureString
$secretKey = Read-Host 'R2 Secret Access Key' -AsSecureString

if ($accountId -notmatch '^[0-9a-f]{32}$') { throw 'Account ID는 32자리 16진수여야 합니다.' }
if ($bucket -notmatch '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$') { throw '버킷 이름 형식이 올바르지 않습니다.' }

$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$resticPassword = [Convert]::ToBase64String($bytes)

New-Item -ItemType Directory -Force $BackupHome | Out-Null
[pscustomobject]@{
    AccountId       = $accountId
    Bucket          = $bucket
    AccessKeyId     = $accessKey
    SecretAccessKey = $secretKey
    ResticPassword  = (ConvertTo-SecureString $resticPassword -AsPlainText -Force)
} | Export-Clixml -Path $SecretsPath

Write-Host ''
Write-Host '=== 백업 암호 (지금 한 번만 표시) ===' -ForegroundColor Yellow
Write-Host $resticPassword -ForegroundColor Yellow
Write-Host '이 암호를 비밀번호 관리자나 종이에 꼭 따로 보관하세요.'
Write-Host 'PC가 고장 나면 이 암호와 R2 키 없이는 백업을 복원할 수 없습니다.'
Write-Host ''
Read-Host '보관했으면 Enter를 누르세요 (저장소를 초기화합니다)' | Out-Null
Clear-Host

Use-BackupSecrets
try {
    & (Get-ResticExe) init
    if ($LASTEXITCODE -ne 0) { throw "restic init failed ($LASTEXITCODE)" }
    Write-BackupLog 'repository initialized'
    Write-Host '설정 완료. 이 창은 닫아도 됩니다.' -ForegroundColor Green
} finally {
    Clear-BackupSecrets
}
Read-Host 'Enter를 누르면 닫힙니다' | Out-Null

# One-time setup for PA4-T1 (Telegram status). Run in your own PowerShell window; the token is typed here and
# never shown to an AI session. It is stored encrypted with Windows DPAPI for the current user only.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security

$Folder = Join-Path $env:LOCALAPPDATA 'JarvisTelegram'
$TokenPath = Join-Path $Folder 'bot-token.bin'
# Must match ENTROPY in jarvis/cli/telegram_poll.py.
$Entropy = [System.Text.Encoding]::ASCII.GetBytes('jarvis-telegram-v1')

Write-Host ''
Write-Host '=== JARVIS Telegram 조회 봇 토큰 설정 ===' -ForegroundColor Cyan
Write-Host 'BotFather가 준 토큰을 붙여 넣으세요. 화면에 표시되지 않습니다.'
Write-Host '토큰은 이 PC의 Windows 계정 암호화(DPAPI)로만 저장됩니다.'
Write-Host ''
if (Test-Path $TokenPath) {
    $answer = Read-Host '이미 저장된 토큰이 있습니다. 바꿀까요? (y/N)'
    if ($answer -ne 'y') { Write-Host '바꾸지 않았습니다.'; return }
}
$secure = Read-Host 'Bot token' -AsSecureString
$plain = [System.Net.NetworkCredential]::new('', $secure).Password
try {
    if ($plain -notmatch '^[0-9]{5,16}:[A-Za-z0-9_-]{30,64}$') { throw '토큰 형식이 올바르지 않습니다 (숫자:문자열).' }
    $bytes = [System.Text.Encoding]::ASCII.GetBytes($plain)
    $blob = [System.Security.Cryptography.ProtectedData]::Protect(
        $bytes, $Entropy, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    New-Item -ItemType Directory -Force $Folder | Out-Null
    [System.IO.File]::WriteAllBytes($TokenPath, $blob)
    [Array]::Clear($bytes, 0, $bytes.Length)
} finally {
    $plain = $null
}
Write-Host ''
Write-Host "저장했습니다: $TokenPath" -ForegroundColor Green
Write-Host '다음: python -m jarvis.cli.telegram_poll check, 그다음 pair.'

# Daily encrypted backup of JarvisWorkspace and .jarvis-auth to Cloudflare R2.
. (Join-Path $PSScriptRoot 'common.ps1')

$restic = Get-ResticExe
Use-BackupSecrets
try {
    $out = & $PythonExe -I (Join-Path $PSScriptRoot 'stage_data.py') $ProjectRoot $StagingPath
    if ($LASTEXITCODE -ne 0) { throw "staging failed ($LASTEXITCODE)" }
    Write-BackupLog $out

    Push-Location $StagingPath
    try {
        & $restic backup . --tag jarvis --host jarvis-pc --quiet
        if ($LASTEXITCODE -ne 0) { throw "restic backup failed ($LASTEXITCODE)" }
    } finally { Pop-Location }

    & $restic forget --tag jarvis --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune --quiet
    if ($LASTEXITCODE -ne 0) { throw "restic forget failed ($LASTEXITCODE)" }
    Write-BackupLog 'backup ok'
} catch {
    Write-BackupLog "backup FAILED: $($_.Exception.Message)"
    throw
} finally {
    Clear-BackupSecrets
    if (Test-Path $StagingPath) { Remove-Item -Recurse -Force $StagingPath }
}

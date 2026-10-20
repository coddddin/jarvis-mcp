# Restore rehearsal: restore the latest snapshot into an empty temp folder and verify it.
# Never touches the live project data.
. (Join-Path $PSScriptRoot 'common.ps1')

$restic = Get-ResticExe
$target = Join-Path $BackupHome ('restore-check-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
Use-BackupSecrets
try {
    & $restic snapshots --tag jarvis --latest 1
    & $restic restore latest --tag jarvis --target $target --quiet
    if ($LASTEXITCODE -ne 0) { throw "restic restore failed ($LASTEXITCODE)" }
    & $restic check --quiet
    if ($LASTEXITCODE -ne 0) { throw "restic check failed ($LASTEXITCODE)" }
} finally {
    Clear-BackupSecrets
}

$bad = 0
$dbs = Get-ChildItem $target -Recurse -File | Where-Object {
    $fs = [IO.File]::OpenRead($_.FullName); $buf = New-Object byte[] 16; $null = $fs.Read($buf, 0, 16); $fs.Close()
    [Text.Encoding]::ASCII.GetString($buf, 0, 15) -eq 'SQLite format 3'
}
foreach ($db in $dbs) {
    $r = & $PythonExe -I -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" $db.FullName
    if ($r -ne 'ok') { $bad++; Write-Host "BAD  $($db.FullName): $r" }
}
$files = (Get-ChildItem $target -Recurse -File).Count
$msg = "restore-check files=$files sqlite=$($dbs.Count) bad=$bad"
# The restored copy holds decrypted credentials; never leave it on disk.
Remove-Item -Recurse -Force $target
Write-BackupLog $msg
Write-Host $msg
if ($bad -gt 0) { exit 1 }

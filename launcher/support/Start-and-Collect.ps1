$ErrorActionPreference = 'Stop'
$r = Join-Path ([Environment]::GetFolderPath('Desktop')) 'JCS2-Windows-Personal'
if (Test-Path (Join-Path $env:JCS2_USB_DIR 'JCS2Launcher.exe')) { $r = $env:JCS2_USB_DIR }
$d = $null
try {
    if (!(Test-Path "$r\JCS2Launcher.exe")) { throw 'Put this CMD beside JCS2Launcher.exe, then run it again.' }
    Set-Location $r
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $d = Join-Path $r "diagnostics\$stamp"
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    $ini = [IO.File]::ReadAllText("$r\launcher.ini")
    $m = [regex]::Match($ini, '(?m)^adb_port=(\d+)\s*$')
    if (!$m.Success) { throw 'Missing adb_port in launcher.ini' }
    $ap = [int]$m.Groups[1].Value
    if ($ap -lt 1024 -or $ap -gt 65535 -or $ap -eq 5037) { throw 'Refusing to touch a shared/default ADB port.' }
    $adb = "$r\sdk\platform-tools\adb.exe"
    $owners = @(Get-NetTCPConnection -LocalPort $ap -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($owner in $owners) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$owner"
        if (!$p.ExecutablePath -or $p.ExecutablePath -ine $adb) { throw "Port $ap belongs to another program; it was NOT stopped." }
    }
    if ($owners.Count) {
        Write-Host 'Clearing this launcher''s leftover ADB server...'
        & $adb -P $ap kill-server
        Start-Sleep -Seconds 2
    }
    Copy-Item "$r\launcher.ini" "$d\launcher-before.ini"
    $ini = [regex]::Replace($ini, '(?m)^emulator_port=\d+', 'emulator_port=5584')
    [IO.File]::WriteAllText("$r\launcher.ini", $ini)
    Write-Host 'Starting JCS2. Do NOT separately click the EXE. Please wait up to two minutes.'
    $ErrorActionPreference = 'Continue'
    & "$r\JCS2Launcher.exe" 2>&1 | Tee-Object -FilePath "$d\startup.txt"
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    Write-Host "Launcher exited with code $code."
    "Exit code: $code" | Out-File "$d\exit.txt"
} catch {
    Write-Host "STOPPED: $($_.Exception.Message)" -ForegroundColor Yellow
    if ($d) { $_ | Out-String | Out-File "$d\support-error.txt" }
} finally {
    if ($d) {
        Copy-Item "$r\*.log", "$r\launcher.ini" $d -ErrorAction SilentlyContinue
        Copy-Item "$r\state\avd\.jcs2-sessions\*.log" $d -ErrorAction SilentlyContinue
        $z = Join-Path $env:JCS2_USB_DIR "JCS2-diagnostics-$stamp.zip"
        try {
            Compress-Archive -Path "$d\*" -DestinationPath $z -Force
            Write-Host "Diagnostics saved: $z"
        } catch { Write-Host "Logs remain in $d" }
    }
}
Read-Host 'Press Enter to close'

param([string]$Root = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$cfg = Join-Path $Root 'launcher.ini'
if (!(Test-Path $cfg)) { throw "Missing launcher.ini; copy launcher.ini.example and edit user-local paths." }
$required = @('sdk\platform-tools\adb.exe','sdk\emulator\emulator.exe', 'sdk\system-images\android-28\google_apis\x86')
foreach ($rel in $required) { $p=Join-Path $Root $rel; if (!(Test-Path $p -PathType Leaf)) { throw "Missing staged official file: $rel" } }
$ini = Get-Content $cfg -Raw
$matches = [regex]::Matches($ini,'(?im)^\s*apk\d+\s*=\s*(.+)$')
if ($matches.Count -ne 5) { throw "launcher.ini must name exactly five APK splits." }
foreach($m in $matches){$rel=$m.Groups[1].Value.Trim();$p=Join-Path $Root $rel;if(!(Test-Path $p -PathType Leaf)){throw "Missing staged APK: $rel"}}
$controllerKeys = @('controller_exe','controller_helper_jar','controller_mapping')
$controllerValues = @{}
foreach($k in $controllerKeys){$m=[regex]::Match($ini,"(?im)^\s*"+$k+"\s*=\s*(.+)$");if($m.Success){$controllerValues[$k]=$m.Groups[1].Value.Trim()}}
if ($controllerValues.Count -ne 0 -and $controllerValues.Count -ne 3) { throw 'launcher.ini controller_exe/controller_helper_jar/controller_mapping must be configured together.' }
foreach($k in $controllerKeys){if($controllerValues.ContainsKey($k)){if(!(Test-Path (Join-Path $Root $controllerValues[$k]) -PathType Leaf)){throw "Missing controller file: $($controllerValues[$k])"}}}
$sdkRoot = (Resolve-Path (Join-Path $Root 'sdk')).Path
$avdMatch = [regex]::Match($ini,'(?im)^\s*avd_dir\s*=\s*(.+)$')
if (!$avdMatch.Success) { throw 'launcher.ini must set avd_dir.' }
$avdHome = $avdMatch.Groups[1].Value.Trim()
$avdHome = Join-Path $Root $avdHome
$imageDir = Join-Path $sdkRoot 'system-images\android-28\google_apis\x86'
if (!(Test-Path $imageDir -PathType Container)) { throw "Missing pre-staged system image: $imageDir (no downloads are performed)." }
if (!(Test-Path $avdHome)) { New-Item -ItemType Directory -Path $avdHome | Out-Null }
$guest = Join-Path $avdHome 'JCS2-personal.avd'
$pointer = Join-Path $avdHome 'JCS2-personal.ini'
$owned = Join-Path $guest '.jcs2-owned'
if (Test-Path $owned) {
  if ((Get-Content $owned -Raw).Trim() -ne 'JCS2-personal') { throw 'Refusing an AVD owned by another name.' }
} elseif ((Get-ChildItem $avdHome -Force | Measure-Object).Count -ne 0) {
  throw 'Refusing to claim a non-empty unowned AVD state directory.'
} else {
  New-Item -ItemType Directory -Path $guest | Out-Null
  $cfg = @"
AvdId = JCS2-personal
PlayStore.enabled = false
abi.type = x86
avd.ini.displayname = JCS2 API28 Google APIs x86
avd.ini.encoding = UTF-8
fastboot.forceColdBoot = yes
fastboot.forceFastBoot = no
hw.cpu.arch = x86
hw.cpu.ncore = 2
hw.gpu.enabled = yes
hw.gpu.mode = swiftshader_indirect
hw.ramSize = 1536
image.sysdir.1 = $([IO.Path]::GetFullPath($imageDir))
image.sysdir.2 =
network.latency = none
network.speed = full
runtime.network.latency = none
runtime.network.speed = full
tag.display = Google APIs
tag.id = google_apis
vm.heapSize = 256
disk.dataPartition.size = 6442450944
"@
  Set-Content -Path (Join-Path $guest 'config.ini') -Value $cfg -Encoding utf8NoBOM
  Set-Content -Path $pointer -Value "path=$([IO.Path]::GetFullPath($guest))" -Encoding utf8NoBOM
  Set-Content -Path $owned -Value "JCS2-personal" -Encoding utf8NoBOM
}
$iniProps = Join-Path $guest 'config.ini'
$config = Get-Content $iniProps -Raw
if ($config -notmatch '(?m)^image.sysdir.1\s*=') { throw 'Fresh AVD config is missing image.sysdir.1.' }
$hashes = foreach($m in $matches){Get-FileHash (Join-Path $Root $m.Groups[1].Value.Trim()) -Algorithm SHA256}
$hashes | Format-Table -AutoSize
Write-Host 'Validated official local tools, created/verified a fresh personal AVD, and verified five APK hashes.'
Write-Host 'No downloads, account setup, restore of private AVDs, emulator start, or device actions were performed.'

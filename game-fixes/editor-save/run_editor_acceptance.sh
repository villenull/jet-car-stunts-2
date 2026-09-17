#!/usr/bin/env bash
# Editor save+reopen isolated acceptance harness (GATED, editor-owned).
#
# Default (no args): DRY RUN ONLY — verifies every path/port/lock/hash/cert,
# prints the exact commands --run would execute, spawns NOTHING, kills NOTHING.
#   --run : execute the bounded cycle (only by root, only in the assigned
#           isolated slot AFTER the controls owner releases cleanup).
#
# Bounds: fresh scratch guest ONLY (editor-test in scratch avdhome). The
# personal guest (hardened_api28) can never match: the script aborts if any
# path contains 'hardened'. No wipe of anything except the scratch guest's
# own first-boot state. No uninstall, no pm clear, no buy/restore taps, no
# payment network. UI driving is by the owner; this harness only boots,
# installs, captures read-only evidence (lists + sha256), and cleans up.
# Candidate status stays UNVERIFIED until steps 3-6 below all PASS.
#
# Acceptance (all required): save `tasktest` -> listed (H1) -> tap opens,
# edit-or-play usable -> restart -> listed (H2==H1) -> tap opens again.
set -u
MODE="${1:---dry-run}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGE="$ROOT/staging/editor-save-20260915T010000Z"
APKDIR="$STAGE/apks-signed"
SDK="$ROOT/analysis/arm-runtime-20260910T010130Z/runtime/sdk"
EMULATOR="$SDK/emulator/emulator"
QEMU_IMG="$SDK/emulator/qemu-img"
ADB="$ROOT/tools/platform-tools-20260910T004712Z/extracted/platform-tools/adb"
SYSIMG="$SDK/system-images/android-28/google_apis/x86"
AVDHOME="$ROOT/staging/editor-avd-scratch"
AVD="editor-test"
PKG="com.trueaxis.jetcarstunts2"
EXPECTED_LIB="cb2bd45b85cd6bf161199e57c460870d295b73d4ad2101bb7d97d0d413f0ffb1"
EXPECTED_CERT="DB:86:A5:6E:99:37:04:51:84:A3:B5:B8:E3:64:E3:0F:53:33:A3:0A:FD:A1:27:39:E1:1E:71:78:B4:AD:33:ED"
ADB_SERVER_PORT=5042
CONSOLE_PORT=5672
ADB_PORT=5673
SERIAL="127.0.0.1:5673"
LOCKDIR="/tmp/jcs2-launcher-$(id -u)"
PORT_LOCK="$LOCKDIR/ports-5042-5672-5673.lock"
AVD_LOCK="$AVDHOME/editor-test.avd/.jcs2-test.lock"
EVIDENCE="$ROOT/staging/editor-acceptance-$(date -u +%Y%m%dT%H%M%SZ)"
SPLITS="base.apk split_config.armeabi_v7a.apk split_config.en.apk split_config.es.apk split_config.xhdpi.apk"

fail() { echo "ABORT: $*" >&2; exit 1; }
log() { echo "[editor-accept] $*"; }

write_avd() {
  log "creating fresh test AVD (never copies personal qcow2)..."
  log "geometry: proven 1280x800-landscape keys (hw.lcd 800x1280 + game-forced"
  log "landscape; keys copied from prior isolated runs, no invented flags)..."
  mkdir -p "$AVDHOME/editor-test.avd"
  printf 'avd.ini.encoding=UTF-8\npath=%s/editor-test.avd\npath.rel=avd/editor-test.avd\ntarget=android-28\n' \
    "$AVDHOME" > "$AVDHOME/editor-test.ini"
  cat > "$AVDHOME/editor-test.avd/config.ini" <<EOF
avd.ini.encoding=UTF-8
PlayStore.enabled = false
hw.accelerometer = yes
hw.audioInput = no
hw.audioOutput = yes
hw.camera.back = none
hw.camera.front = none
hw.cpu.arch = x86
hw.dPad = no
hw.gps = no
hw.gpu.enabled = yes
hw.gpu.mode = swiftshader_indirect
hw.initialOrientation = portrait
hw.keyboard = yes
hw.lcd.density = 420
hw.lcd.height = 1280
hw.lcd.width = 800
hw.mainKeys = no
hw.ramSize = 1536
hw.sdCard = no
image.sysdir.1=$SYSIMG/
showDeviceFrame = no
tag.display = Google APIs
tag.id = google_apis
disk.dataPartition.size = 6442450944
EOF
  grep -r 'hardened' "$AVDHOME" && fail "contamination in $AVDHOME"
  log "fresh userdata (created by emulator at boot)"
}

# Full-touch preflight: proves the whole game surface accepts touches
# BEFORE any UI verdict can depend on taps. Fails closed on any drop.
touch_preflight() {
  log "full-touch preflight (4 corners + center, 1280x800 game space)..."
  timeout "$ADB_T" "$ADB" -s "$SERIAL" shell logcat -c
  for pt in "20 20" "1260 20" "20 780" "1260 780" "640 400"; do
    # shellcheck disable=SC2086
    timeout "$ADB_T" "$ADB" -s "$SERIAL" shell input tap $pt
    sleep 2
  done
  if timeout "$ADB_T" "$ADB" -s "$SERIAL" logcat -d 2>/dev/null \
      | grep -q "Dropping event"; then
    fail "touch preflight: dropped tap(s) -- geometry not full-touch"
  fi
  log "touch preflight: 5/5 delivered, no drops"
}

case "$AVDHOME $AVD $SDK $SYSIMG $STAGE" in
  *hardened*) fail "personal-guest string in paths" ;;
esac

verify_paths() {
  log "verifying paths (no process spawn)..."
  [ -d "$STAGE" ] || fail "missing stage: $STAGE"
  [ -f "$STAGE/editor-save-provenance.json" ] || fail "missing provenance"
  [ -x "$EMULATOR" ] || fail "missing emulator: $EMULATOR"
  [ -x "$QEMU_IMG" ] || fail "missing qemu-img: $QEMU_IMG"
  [ -x "$ADB" ] || fail "missing adb: $ADB"
  [ -f "$SYSIMG/system.img" ] || fail "missing system.img: $SYSIMG"
  for s in $SPLITS; do
    [ -f "$APKDIR/$s" ] || fail "missing split: $APKDIR/$s"
  done
  python3 -c "
import json,hashlib,zipfile,sys
p=json.load(open('$STAGE/editor-save-provenance.json'))
assert p['variant_lib_sha256']=='$EXPECTED_LIB', 'provenance lib hash drift'
assert p['derivative_cert_sha256_fingerprint']=='$EXPECTED_CERT', 'cert drift'
assert p['variant_lib_changed_bytes']==45, 'footprint drift'
d=zipfile.ZipFile('$APKDIR/split_config.armeabi_v7a.apk').read('lib/armeabi-v7a/libtrueaxis.so')
assert hashlib.sha256(d).hexdigest()=='$EXPECTED_LIB', 'signed lib drift'
print('gates: provenance + signed lib == $EXPECTED_LIB (45B), cert DB86')
" || fail "hash/cert gate failed"
  log "paths + hash/cert gates OK"
}

print_plan() {
  log "DRY RUN -- exact commands --run would execute (nothing spawned):"
  echo "  mkdir -p $LOCKDIR $AVDHOME $EVIDENCE"
  echo "  lock $PORT_LOCK + $AVD_LOCK (abort if held: controls probes own their slots)"
  echo "  AVD geometry: proven keys (hw.lcd 800x1280/density 420, swiftshader, 1536MB, showDeviceFrame=no) -> 1280x800 landscape game"
  echo "  emulator -avd $AVD -ports $CONSOLE_PORT,$ADB_PORT -adb-path ... -memory 1536 -qemu -net none (offline; scratch avdhome $AVDHOME)"
  echo "  adb root FIRST (isolated guest only); full-touch preflight 5/5 (corners+center, zero drops) BEFORE any UI verdict"
  echo "  install-multiple (ONE session, all 5 splits) $APKDIR/{base,4 splits} (fresh guest, no -r over personal)"
  echo "  UI (uncheck-tolerant): uncheck True-Axis box if checked, PLAY (local-editor use must not require login) -> Create -> minimal track -> Save as 'tasktest'"
  echo "  file evidence: adb root pull preferred (isolated guest only); fallback adb backup unpack (NOT run-as, NOT personal route):"
  echo "  pull/unpack -> expect userLevels/tasktest.bin (else FAIL); sha256sum (record H1)"
  echo "  OWNER UI: tap tasktest -> must OPEN (no store prompt) -> edit-or-play briefly -> back out"
  echo "  adb reboot $SERIAL; wait-for-device; re-list; sha256sum (record H2); PASS iff H1==H2"
  echo "  OWNER UI: tap tasktest again -> opens+usable (PASS = persisted usable track)"
  echo "  cleanup: stop emulator, release locks, keep $EVIDENCE (hashes/screenshots/logs)"
  echo "  owned cleanup (exact): rm -rf $AVDHOME $PORT_LOCK $AVD_LOCK (evidence KEPT)"
}

cleanup_owned() {
  log "owned cleanup: removing scratch guest + locks (evidence kept)..."
  rm -rf "$AVDHOME" "$PORT_LOCK" "$AVD_LOCK"
  log "cleanup done"
}

if [ "$MODE" = "---dry-run" ] || [ "$MODE" = "--dry-run" ]; then
  verify_paths
  print_plan
  log "DRY RUN COMPLETE -- no live action taken; candidate remains UNVERIFIED"
  exit 0
fi

if [ "$MODE" != "--run" ]; then
  fail "usage: $0 [--dry-run|--run]"
fi

verify_paths
# Bound: total run budget 90 min from here (boot+install+UI+restart);
# every adb call individually timed out. No unbounded waits.
START_TS="$SECONDS"
TOTAL_BUDGET=5400
BOOT_TIMEOUT=600
ADB_T=60
deadline_left() { echo $((TOTAL_BUDGET - (SECONDS - START_TS))); }
require_time() { [ "$(deadline_left)" -gt "$1" ] || fail "total budget exceeded"; }
mkdir -p "$LOCKDIR" "$AVDHOME" "$EVIDENCE"
[ -e "$PORT_LOCK" ] && fail "port lock held: $PORT_LOCK"
[ -e "$AVD_LOCK" ] && fail "avd lock held: $AVD_LOCK"
touch "$PORT_LOCK" "$AVD_LOCK"
trap 'log "teardown..."; ADB_SERVER_SOCKET=tcp:localhost:$ADB_SERVER_PORT timeout 30 "$ADB" -s "$SERIAL" emu kill 2>/dev/null || true; rm -f "$PORT_LOCK" "$AVD_LOCK"; log "locks released; evidence kept at $EVIDENCE"' EXIT
write_avd

log "booting scratch guest headless (see plan above for bounded UI steps)..."
log "THIS MODE RUNS ONLY IN THE ASSIGNED SLOT -- slot owner drives all UI"
# --- bounded live body: boot, install, evidence capture, teardown via trap ---
# Launch recipe mirrors the proven isolated runs (controls/audio): console
# -ports pair, -adb-path to the project adb, swiftshader_indirect, 1536MB,
# -no-snapshot-save, -qemu -net none (offline isolation),
# ANDROID_ADB_SERVER_PORT for the server endpoint (never personal ports).
export ANDROID_AVD_HOME="$AVDHOME" ANDROID_SDK_ROOT="$SDK"
export ANDROID_ADB_SERVER_PORT="$ADB_SERVER_PORT"
# Server startup MUST NOT inherit a remote socket spec: start explicitly.
log "starting local adb server on $ADB_SERVER_PORT..."
timeout "$ADB_T" env -u ADB_SERVER_SOCKET -u ANDROID_ADB_SERVER_PORT \
  "$ADB" -P "$ADB_SERVER_PORT" start-server \
  || fail "adb start-server failed"
export ADB_SERVER_SOCKET="tcp:localhost:$ADB_SERVER_PORT"
timeout 15 "$ADB" devices | tee "$EVIDENCE/adb-devices-boot.txt"
"$EMULATOR" -avd "$AVD" -no-window -gpu swiftshader_indirect -memory 1536 -no-snapshot -no-snapshot-save -wipe-data -ports "$CONSOLE_PORT,$ADB_PORT" -adb-path "$SDK/platform-tools/adb" -no-boot-anim -qemu -net none &
EMU_PID=$!
log "waiting for device (bounded ${BOOT_TIMEOUT}s)..."
timeout "$BOOT_TIMEOUT" "$ADB" -s "$SERIAL" wait-for-device \
  || fail "boot timeout (${BOOT_TIMEOUT}s), no device"
timeout "$ADB_T" "$ADB" -s "$SERIAL" shell getprop sys.boot_completed \
  | tee "$EVIDENCE/boot-completed.txt"
# Root FIRST (proven stable on isolated guests; never restart adbd after).
log "requesting root adbd (isolated guest only)..."
timeout "$ADB_T" "$ADB" -s "$SERIAL" root | tee "$EVIDENCE/adb-root.txt"
sleep 3
timeout "$ADB_T" "$ADB" -s "$SERIAL" shell id | tee "$EVIDENCE/adb-id.txt"
if grep -q "uid=0" "$EVIDENCE/adb-id.txt"; then
  log "root adbd confirmed (uid=0) -- private-file pull available"
  echo "root" > "$EVIDENCE/priv-pull-mode.txt"
else
  log "no root adbd -- file evidence falls back to adb backup unpacking"
  echo "backup" > "$EVIDENCE/priv-pull-mode.txt"
fi
# Installs: ALL FIVE splits in ONE install-multiple session (a config split
# cannot install alone); must exit 0 AND print Success; else abort.
require_time 900
timeout 240 "$ADB" -s "$SERIAL" install-multiple \
  "$APKDIR/base.apk" \
  "$APKDIR/split_config.armeabi_v7a.apk" \
  "$APKDIR/split_config.en.apk" \
  "$APKDIR/split_config.es.apk" \
  "$APKDIR/split_config.xhdpi.apk" \
  > "$EVIDENCE/install-splits.log" 2>&1 \
  || fail "install-multiple nonzero (see $EVIDENCE/install-splits.log)"
grep -q "Success" "$EVIDENCE/install-splits.log" \
  || fail "install-multiple missing Success (see $EVIDENCE/install-splits.log)"
log "install verified Success: 5-split session"
timeout "$ADB_T" "$ADB" -s "$SERIAL" shell pm path "$PKG" \
  | tee "$EVIDENCE/pm-path.txt"
grep -q "$PKG" "$EVIDENCE/pm-path.txt" || fail "pm path missing package"
timeout "$ADB_T" "$ADB" -s "$SERIAL" shell dumpsys package "$PKG" \
  | grep -E "versionCode|signatures" | tee "$EVIDENCE/pkg-info.txt"
log "INSTALLED+VERIFIED (5/5 Success, pm path, pkg info); retaining guest for UI"
# Launch the game FIRST: preflight taps are only meaningful in the game's
# forced-landscape 1280x800 space (pre-launch portrait clips them by design).
log "launching game via monkey (bounded)..."
timeout 120 "$ADB" -s "$SERIAL" shell monkey -p "$PKG" 1 \
  | tee "$EVIDENCE/monkey-launch.log"
sleep 25
timeout "$ADB_T" "$ADB" -s "$SERIAL" shell screencap -p /sdcard/shot00.png
timeout "$ADB_T" "$ADB" -s "$SERIAL" pull /sdcard/shot00.png \
  "$EVIDENCE/shot00-launch.png"
touch_preflight
log "driver: run bounded UI steps 3-6 now (see --dry-run plan), then:"
log "  touch $EVIDENCE/UI_DONE  (PASS) or touch $EVIDENCE/UI_FAIL (fail)"
log "login flow (uncheck-tolerant): with full touch, tap the True-Axis box"
log "  to UNCHECK server login if checked, then PLAY. Local-editor use must"
log "  never require a login -- record whether PLAY proceeds unchecked."
log "evidence commands (mode per $EVIDENCE/priv-pull-mode.txt):"
echo "  root mode:   $ADB -s $SERIAL pull /data/data/$PKG/files/userLevels/tasktest.bin $EVIDENCE/tasktest-H1.bin"
echo "  backup mode: $ADB -s $SERIAL backup -f $EVIDENCE/app1.ab $PKG && unpack -> tasktest.bin"
echo "  sha256sum tasktest*.bin | tee $EVIDENCE/H1.txt"
echo "  (restart) $ADB -s $SERIAL reboot; $ADB wait-for-device (bounded)"
echo "  re-pull same way; sha256sum | tee $EVIDENCE/H2.txt"
echo "  test \"\$(cut -d' ' -f1 $EVIDENCE/H1.txt)\" = \"\$(cut -d' ' -f1 $EVIDENCE/H2.txt)\" && echo REOPEN-PERSISTED"
# Retain the guest (bounded by TOTAL_BUDGET) until the driver records verdict.
# Verdict files are tested FIRST so a recorded verdict never loses to expiry.
while true; do
  [ -e "$EVIDENCE/UI_DONE" ] && { log "UI verdict recorded: DONE"; break; }
  [ -e "$EVIDENCE/UI_FAIL" ] && fail "UI verdict recorded: FAIL (see $EVIDENCE/FAIL-REASON.txt)"
  require_time 120
  sleep 15
done

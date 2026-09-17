#!/usr/bin/env bash
# Maps isolated fresh-guest live run (GATED).
#
# Default (no args): DRY RUN ONLY — verifies every path/port/lock/hash,
# prints the exact commands --run would execute, spawns NOTHING.
#   --run : execute the full cycle in THIS shell (locks + env + trap
#           cleanup stay in one shell; teardown always runs).
#
# Safety: fresh test AVD only (maps-test in scratch avdhome). The personal
# guest (hardened_api28) can never match: the script aborts if any path
# contains 'hardened'. No wipe of anything except the scratch test AVD's
# own first-boot state. No uninstall, no pm clear, no buy/unlock taps.
set -u
MODE="${1:---dry-run}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SDK="$ROOT/analysis/arm-runtime-20260910T010130Z/runtime/sdk"
EMULATOR="$SDK/emulator/emulator"
QEMU_IMG="$SDK/emulator/qemu-img"
ADB="$ROOT/tools/platform-tools-20260910T004712Z/extracted/platform-tools/adb"
SYSIMG="$SDK/system-images/android-28/google_apis/x86"
APKDIR="${MAPS_APKS:-$ROOT/staging/maps-derivative-v2frozen/apks-signed}"
# A/B arms: MAPS_APKS=<set> selects the 5-split set:
#   pristine: staging/maps-pristine-v2frozen/apks-signed
#   maps:     staging/maps-derivative-v2frozen/apks-signed
if [ ! -d "$APKDIR" ]; then echo "ABORT: APK set missing: $APKDIR" >&2; exit 1; fi
# Scratch test AVD home: project disk (362G free). /tmp is too small
# (emulator wants ~7.4GB userdata; /tmp has ~4.8GB). Disposable;
# personal qcow2 never copied here. Overridable for review.
AVDHOME="${MAPS_AVDHOME:-$ROOT/staging/maps-avd-scratch}"
AVD="maps-test"
PKG="com.trueaxis.jetcarstunts2"
ADB_SERVER_PORT=5040
CONSOLE_PORT=5668
ADB_PORT=5669
SERIAL="127.0.0.1:5669"  # explicit IP form: with a custom
  # ADB_SERVER_SOCKET + explicit -ports, the emulator-XXXX alias never
  # registers; connect + use the IP serial (proven live 2026-09-13).
LOCKDIR="/tmp/jcs2-launcher-$(id -u)"
PORT_LOCK="$LOCKDIR/ports-5040-5668-5669.lock"
AVD_LOCK="$AVDHOME/maps-test.avd/.jcs2-test.lock"
EVIDENCE="$ROOT/staging/maps-live-$(date -u +%Y%m%dT%H%M%SZ)"
SPLITS="base.apk split_config.armeabi_v7a.apk split_config.en.apk split_config.es.apk split_config.xhdpi.apk"

fail() { echo "ABORT: $*" >&2; exit 1; }
log() { echo "[maps-live] $*"; }

# --- Hard guard: personal guest must be unreferenceable ---
case "$AVDHOME $AVD $SDK $SYSIMG" in
  *hardened*) fail "personal-guest string in paths" ;;
esac

verify_paths() {
  log "verifying paths (no process spawn)..."
  [ -x "$EMULATOR" ] || fail "missing emulator: $EMULATOR"
  [ -x "$QEMU_IMG" ] || fail "missing qemu-img: $QEMU_IMG"
  [ -x "$ADB" ] || fail "missing adb: $ADB"
  [ -f "$SYSIMG/system.img" ] || fail "missing system.img: $SYSIMG"
  for s in $SPLITS; do
    [ -f "$APKDIR/$s" ] || fail "missing split: $APKDIR/$s"
  done
  # Set hashes must match that set's frozen provenance exactly.
  python3 - "$APKDIR" <<'EOF'
import hashlib, json, sys, pathlib, glob
apkdir = pathlib.Path(sys.argv[1])
provfiles = glob.glob(str(apkdir.parent / 'maps-*-provenance.json'))
assert len(provfiles) == 1, provfiles
expected = json.load(open(provfiles[0]))["derivative_apk_sha256"]
for name, digest in expected.items():
    actual = hashlib.sha256(pathlib.Path(apkdir, name).read_bytes()).hexdigest()
    assert actual == digest, f"{name} hash drift: {actual} != {digest}"
print("set hashes match frozen provenance")
EOF
  [ -d "$AVDHOME" ] || mkdir -p "$AVDHOME" || fail "cannot create $AVDHOME"
  log "paths OK"
}

verify_ports_procs() {
  log "verifying ports free and no emulator procs..."
  if ss -ltn 2>/dev/null | grep -qE ":(5040|5668|5669)[[:space:]]"; then
    fail "one of ports 5040/5668/5669 is already listening"
  fi
  if pgrep -af 'qemu-system|/emulator |run-jcs2|runner\.py' >/dev/null; then
    pgrep -af 'qemu-system|/emulator |run-jcs2|runner\.py' >&2
    fail "emulator/runner processes already running"
  fi
  df --output=avail -BG /home | tail -n 1
  # AVD scratch must hold ~8GB userdata: check ITS filesystem.
  mkdir -p "$AVDHOME" || fail "cannot create $AVDHOME"
  local avail_gb
  avail_gb=$(df --output=avail -BG "$AVDHOME" | tail -n 1 | tr -dc '0-9')
  [ "${avail_gb:-0}" -ge 12 ] || fail "AVD scratch only ${avail_gb}G (<12G)"
  log "avd scratch ${avail_gb}G free"
  log "ports/procs OK"
}

write_avd() {
  log "creating fresh test AVD (never copies personal qcow2)..."
  mkdir -p "$AVDHOME/maps-test.avd"
  printf 'avd.ini.encoding=UTF-8\npath=%s/maps-test.avd\npath.rel=avd/maps-test.avd\ntarget=android-28\n' \
    "$AVDHOME" > "$AVDHOME/maps-test.ini"
  cat > "$AVDHOME/maps-test.avd/config.ini" <<EOF
avd.ini.encoding=UTF-8
hw.cpu.arch=x86
target=android-28
image.sysdir.1=$SYSIMG/
hw.audioOutput=yes
hw.audioInput=no
hw.accelerometer=yes
EOF
  grep -r 'hardened' "$AVDHOME" && fail "contamination in $AVDHOME"
  if [ -e "$AVDHOME/maps-test.avd/userdata-qcow2.img" ]; then
    "$QEMU_IMG" info --backing-chain "$AVDHOME/maps-test.avd/userdata-qcow2.img"
  else
    log "fresh userdata (created by emulator at boot)"
  fi
}

print_plan() {
  cat <<EOF
DRY RUN — would execute (in this order, one shell, trap cleanup):
  export ANDROID_AVD_HOME=$AVDHOME ANDROID_SDK_ROOT=$SDK ADB_SERVER_SOCKET=tcp:localhost:5040
  flock -n $PORT_LOCK + $AVD_LOCK (0700 dir)
  $ADB start-server
  $EMULATOR -avd maps-test -no-window -gpu swiftshader_indirect -no-snapshot -wipe-data -ports 5668,5669 -no-boot-anim &
  (offline isolation post-boot: svc wifi/data disable + airplane; emulator 37 dropped -net)
  $ADB -s $SERIAL wait-for-device
  $ADB -s $SERIAL install-multiple --no-streaming $APKDIR/{5 splits}
  $ADB -s $SERIAL shell pm path $PKG ; dumpsys package $PKG (versionCode+signatures)
  $ADB -s $SERIAL shell monkey -p $PKG 1
  wait UI_DONE_1 sentinel (UI checks window 1) -> emu kill -> reboot (no wipe) -> wait UI_DONE_2 -> teardown
EOF
}

cleanup() {
  # Same-shell teardown: owned emulator group only, owned adb server only.
  log "teardown: stopping owned emulator (if any)..."
  export ADB_SERVER_SOCKET="tcp:localhost:$ADB_SERVER_PORT"
  "$ADB" -s "$SERIAL" emu kill >/dev/null 2>&1 || true
  if [ -n "${EMUPID:-}" ] && kill -0 "$EMUPID" 2>/dev/null; then
    kill -TERM -- "-$EMUPID" 2>/dev/null || kill -TERM "$EMUPID" 2>/dev/null || true
    sleep 5
    kill -KILL -- "-$EMUPID" 2>/dev/null || kill -KILL "$EMUPID" 2>/dev/null || true
  fi
  "$ADB" kill-server >/dev/null 2>&1 || true
  if ss -ltn 2>/dev/null | grep -qE ":(5040|5668|5669)[[:space:]]"; then
    echo "WARNING: ports still listening after teardown" >&2
  else
    log "ports free; locks release on shell exit"
  fi
}

boot_and_install() {
  local wipemode="$1"  # -wipe-data on first boot only
  log "starting emulator $wipemode ..."
  # NOTE: emulator 37 dropped '-net none' ("unknown option"). Offline
  # isolation is done post-boot via svc/airplane (billing then fails
  # closed; no accounts/sync). See OVERNIGHT-20260910 radio0 caveat.
  # shellcheck disable=SC2086
  setsid "$EMULATOR" -avd "$AVD" -no-window -gpu swiftshader_indirect \
    -no-snapshot $wipemode -ports "$CONSOLE_PORT,$ADB_PORT" \
    -no-boot-anim >>"$EVIDENCE/emulator.log" 2>&1 &
  EMUPID=$!
  log "emulator pid $EMUPID (owned group)"
  # adbd is not up at launch: a single-shot connect races it (Connection
  # refused is normal for the first ~60 s). Retry bounded: 60x5s = 300 s cap.
  # (Arm A 2026-09-13 died exactly here: one connect at t~2 s refused, never
  # retried, wait-for-device blocked on the unconnected serial until interrupt
  # while the guest itself had booted in 33 s.)
  connected=""
  for i in $(seq 1 60); do
    if "$ADB" connect 127.0.0.1:5669 2>&1 | grep -qE 'connected|already connected'; then
      connected=1; break
    fi
    sleep 5
  done
  [ -n "$connected" ] || fail "adb connect failed after 300 s"
  timeout 600 "$ADB" -s "$SERIAL" wait-for-device || fail "device never came up"
  log "isolating guest network (offline test)..."
  # svc needs the Wi-Fi service up: right after boot it answers
  # "Wi-Fi service is not ready". Retry bounded: 12x10s = 120 s cap.
  # (Arm A 2026-09-13: device up, first svc attempt hit not-ready and the
  # bare || fail aborted a healthy run.)
  isolated=""
  for i in $(seq 1 12); do
    out=$("$ADB" -s "$SERIAL" shell "svc wifi disable; svc data disable" 2>&1) && \
    ! printf '%s' "$out" | grep -qiE 'not ready|error|failed|not found' && { isolated=1; break; }
    sleep 10
  done
  [ -n "$isolated" ] || fail "network isolation failed after 120 s"
  "$ADB" -s "$SERIAL" shell settings put global airplane_mode_on 1 \
    >/dev/null || fail "airplane mode failed"
  "$ADB" -s "$SERIAL" shell dumpsys connectivity \
    | grep -i 'active network' | tee "$EVIDENCE/net-isolation.log" || true
  log "device up; installing derivative splits..."
  # shellcheck disable=SC2086
  "$ADB" -s "$SERIAL" install-multiple --no-streaming $(
    for s in $SPLITS; do printf '%s ' "$APKDIR/$s"; done
  ) 2>&1 | tee "$EVIDENCE/install-multiple.log" || fail "install failed"
  "$ADB" -s "$SERIAL" shell pm path "$PKG" | tee "$EVIDENCE/pm-path.log"
  "$ADB" -s "$SERIAL" shell dumpsys package "$PKG" \
    | grep -i -A2 'versionCode\|signatures' | tee "$EVIDENCE/dumpsys-package.log"
}

wait_ui() {
  local sentinel="$1" window_min="$2"
  log "UI window open: create $sentinel when checks done (timeout ${window_min}m)"
  local deadline=$((SECONDS + window_min * 60))
  while [ ! -e "$sentinel" ] && [ $SECONDS -lt $deadline ]; do sleep 10; done
  [ -e "$sentinel" ] || fail "UI window timed out ($sentinel missing)"
  log "UI window closed by sentinel"
}

if [ "$MODE" = "--dry-run" ] || [ "$MODE" = "" ]; then
  verify_paths
  verify_ports_procs
  print_plan
  log "DRY RUN COMPLETE — nothing spawned"
  exit 0
fi

if [ "$MODE" != "--run" ]; then
  echo "usage: $0 [--dry-run|--run]" >&2; exit 2
fi

# ------------------------------- LIVE -------------------------------
mkdir -p -m 0700 "$LOCKDIR"
mkdir -p "$EVIDENCE"
verify_paths
verify_ports_procs
write_avd
export ANDROID_AVD_HOME="$AVDHOME" ANDROID_SDK_ROOT="$SDK" \
  ADB_SERVER_SOCKET="tcp:localhost:$ADB_SERVER_PORT"
exec 9>"$PORT_LOCK"; flock -n 9 || fail "port lock busy: $PORT_LOCK"
exec 8>"$AVD_LOCK"; flock -n 8 || fail "avd lock busy: $AVD_LOCK"
log "locks held; evidence: $EVIDENCE"
trap cleanup EXIT
"$ADB" start-server
boot_and_install "-wipe-data"
"$ADB" -s "$SERIAL" shell monkey -p "$PKG" 1 | tee "$EVIDENCE/monkey-launch-1.log"
wait_ui "$AVDHOME/UI_DONE_1" 30
log "cold restart (no wipe) for persistence check..."
# emu kill can flake on transient adb; fall back to owned-group TERM.
# Never fail the run here: trap owns final cleanup either way.
# (Arm A pristine 2026-09-13: bare || fail aborted a healthy run post-window-1.)
"$ADB" -s "$SERIAL" emu kill >/dev/null 2>&1 || true
sleep 5
if [ -n "${EMUPID:-}" ] && kill -0 "$EMUPID" 2>/dev/null; then
  kill -TERM "$EMUPID" >/dev/null 2>&1 || true
  sleep 5
fi
EMUPID=""
boot_and_install ""
wait_ui "$AVDHOME/UI_DONE_2" 30
log "LIVE CYCLE COMPLETE"

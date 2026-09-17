#!/bin/bash
# BOUNDED sound-fix retest. Disarmed by default; runs ONLY when ALL hold:
#   SOUND_RETEST_SLOT=granted ./retest-plan.sh --copy-ok \
#     --base B.apk --armeabi A.apk --en E.apk --es S.apk --xhdpi X.apk
# Five EXPLICIT split paths (no globs). Operates ONLY on the isolated copy
# at /tmp/opencode/sound-repro (personal guest never referenced).
# Aborts on: slot missing, --copy-ok missing, any emulator process alive
# (including a stale owned one: clean state required), any runner-port
# listener (user session), my-port listeners, original-profile hash drift.
set -u
R=/home/deck/Projects/JCS2
SDK=$R/analysis/arm-runtime-20260910T010130Z/runtime/sdk
ADB=$SDK/platform-tools/adb
STAGE=/tmp/opencode/sound-repro
SERIAL=127.0.0.1:5667
export ANDROID_ADB_SERVER_PORT=5039
ORIG_INI_HASH=e60bacfa581284579cbeeb9762e847f65541b97c839b1db2a7f7804799826ab8
ORIG_CFG_HASH=7237ba2d061f7e87581233bae7c0cf2c025e3719d7fe158492d8265b2d3f32f0

[ "${SOUND_RETEST_SLOT:-}" = "granted" ] || { echo "ABORT: slot not granted"; exit 10; }
[ "${1:-}" = "--copy-ok" ] || { echo "ABORT: --copy-ok required (copy-guest mutation acknowledged)"; exit 10; }
shift
BASE=""; ARMEABI=""; EN=""; ES=""; XHDPI=""
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE=$2; shift 2;; --armeabi) ARMEABI=$2; shift 2;;
    --en) EN=$2; shift 2;; --es) ES=$2; shift 2;;
    --xhdpi) XHDPI=$2; shift 2;; *) echo "ABORT: unknown arg $1"; exit 10;;
  esac
done
for f in "$BASE" "$ARMEABI" "$EN" "$ES" "$XHDPI"; do
  [ -n "$f" ] && [ -f "$f" ] || { echo "ABORT: all five explicit split paths required"; exit 10; }
done
log() { echo "$(date -u +%FT%TZ) $*" | tee -a $STAGE/retest-steps.log; }
# Any emulator alive (foreign OR stale owned) -> clean state required, abort.
if pgrep -af "qemu-system|emulator/emulator" 2>/dev/null | grep -v grep | grep -q .; then
  echo "ABORT: emulator process present (wait for slot release / clean up first)"
  pgrep -af "qemu-system|emulator/emulator" 2>/dev/null | grep -v grep | head -n 3
  exit 11
fi
(ss -ltn 2>/dev/null | grep -qE ':(5038|5594|5595) ') && { echo "ABORT: user session ports busy"; exit 11; }
(ss -ltn 2>/dev/null | grep -qE ':(5039|5666|5667) ') && { echo "ABORT: my ports busy"; exit 11; }
# Executed (not commented) original-profile verification.
[ "$(sha256sum "$R/analysis/hardened-runtime-20260910T150000Z/avdhome/hardened_api28.ini" | cut -d' ' -f1)" = "$ORIG_INI_HASH" ] || { echo "ABORT: original ini drifted"; exit 12; }
[ "$(sha256sum "$R/analysis/hardened-runtime-20260910T150000Z/avdhome/hardened_api28.avd/config.ini" | cut -d' ' -f1)" = "$ORIG_CFG_HASH" ] || { echo "ABORT: original config drifted"; exit 12; }
grep -q "path=$STAGE/avdhome/hardened_api28.avd" $STAGE/avdhome/hardened_api28.ini || { echo "ABORT: staged copy altered"; exit 12; }
for q in cache.img encryptionkey.img userdata-qemu.img; do
  [ -f "$STAGE/avdhome/hardened_api28.avd/$q" ] || { echo "ABORT: backing $q missing"; exit 12; }
done
printf 'owner=630cd122-sound-retest ports=5039,5666,5667 started_utc=' > $STAGE/live-slot.lock
date -u +%FT%TZ >> $STAGE/live-slot.lock

owned_emulator_pid() { # prints pid only if cmdline+env prove owned identity
  [ -f $STAGE/emulator.pid ] || return 1
  P=$(cat $STAGE/emulator.pid)
  cmd=$(tr '\0' ' ' < /proc/$P/cmdline 2>/dev/null) || return 1
  env=$(tr '\0' '\n' < /proc/$P/environ 2>/dev/null) || return 1
  echo "$cmd" | grep -q "hardened_api28" || return 1
  echo "$cmd" | grep -q "5666" || return 1
  echo "$env" | grep -qx "ANDROID_AVD_HOME=$STAGE/avdhome" || return 1
  echo "$P"
}
owned_logcat_pid() { # prints pid only if cmdline proves own capture
  [ -f $STAGE/logcat.pid ] || return 1
  P=$(cat $STAGE/logcat.pid)
  tr '\0' ' ' < /proc/$P/cmdline 2>/dev/null | grep -q "127.0.0.1:5667.*logcat" || return 1
  echo "$P"
}
cleanup() {
  P=$(owned_emulator_pid) && { kill -TERM $P 2>/dev/null; sleep 8; kill -0 $P 2>/dev/null && kill -KILL $P; log "owned emulator $P stopped"; }
  L=$(owned_logcat_pid) && { kill $L 2>/dev/null; log "own logcat $L stopped"; }
  ANDROID_ADB_SERVER_PORT=5039 "$ADB" kill-server 2>/dev/null
  rm -f $STAGE/live-slot.lock
}
trap cleanup EXIT

# 1. boot (same flags as crash repro; -net none like normal launch)
log "boot"
ANDROID_AVD_HOME=$STAGE/avdhome ANDROID_SDK_ROOT=$SDK ANDROID_ADB_SERVER_PORT=5039 \
  nohup "$SDK/emulator/emulator" @hardened_api28 -port 5666 \
  -no-snapshot -no-snapshot-save -no-boot-anim -adb-path "$ADB" \
  -gpu swiftshader_indirect -memory 1536 -no-window -qemu -net none \
  > $STAGE/emulator-retest.log 2>&1 &
echo $! > $STAGE/emulator.pid
[ -n "$(owned_emulator_pid)" ] || { log "FAIL owned identity check"; exit 20; }
deadline=$(( $(date +%s) + 180 ))
while [ $(date +%s) -lt $deadline ]; do
  kill -0 $(cat $STAGE/emulator.pid) 2>/dev/null || { log "FAIL emulator exited"; exit 20; }
  if (ss -ltn 2>/dev/null | grep -q ':5667 '); then
    "$ADB" connect $SERIAL >/dev/null 2>&1
    state=$("$ADB" -s $SERIAL get-state 2>/dev/null | tr -d '\r')
    boot=$("$ADB" -s $SERIAL shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    if [ "$state" = "device" ] && [ "$boot" = "1" ]; then log "BOOT_COMPLETE"; break; fi
  fi
  sleep 3
done
tail -n 1 $STAGE/retest-steps.log | grep -q BOOT_COMPLETE || { log "FAIL boot timeout"; exit 21; }

# 2. fresh install of the EXPLICIT composed splits in the COPY ONLY
#    (cert differs -> copy save wiped; originals never at risk)
log "reinstall composed candidate (copy guest only)"
"$ADB" -s $SERIAL logcat -c
setsid "$ADB" -s $SERIAL logcat -b main,system,crash -v threadtime > $STAGE/logcat-retest-raw.txt 2>&1 < /dev/null &
echo $! > $STAGE/logcat.pid
sleep 1
[ -n "$(owned_logcat_pid)" ] || { log "FAIL logcat identity check"; exit 22; }
"$ADB" -s $SERIAL uninstall com.trueaxis.jetcarstunts2
"$ADB" -s $SERIAL install-multiple "$BASE" "$ARMEABI" "$EN" "$ES" "$XHDPI" || { log "FAIL install"; exit 22; }
"$ADB" -s $SERIAL shell dumpsys package com.trueaxis.jetcarstunts2 | grep -E "versionCode" | head -n 2 | tee -a $STAGE/retest-steps.log

check_alive() { # $1 = label
  sleep 6
  "$ADB" -s $SERIAL shell pidof com.trueaxis.jetcarstunts2 | grep -q . || { log "FAIL $1: game dead"; return 1; }
  "$ADB" -s $SERIAL shell dumpsys activity activities | grep -q "mResumedActivity.*jetcarstunts2" || { log "FAIL $1: not resumed"; return 1; }
  "$ADB" -s $SERIAL logcat -d -b crash -v threadtime 2>/dev/null | grep -q "Fatal signal" && { log "FAIL $1: SIGSEGV"; return 1; }
  log "OK $1: alive, resumed, no SIGSEGV"
}
shot() { # $1 = name; captures AND validates 1280x800 PNG (evidence integrity)
  "$ADB" -s $SERIAL shell screencap -p /sdcard/$1.png
  "$ADB" -s $SERIAL pull /sdcard/$1.png $STAGE/$1.png >/dev/null
  python3 -c "
import struct, sys
d = open('$STAGE/$1.png','rb').read()
assert d[:8] == bytes.fromhex('89504e470d0a1a0a'), 'not PNG'
w, h = struct.unpack('>II', d[16:24])
assert (w, h) == (1280, 800), f'unexpected size {w}x{h}'
" || { log "FAIL $1: bad screenshot"; return 1; }
  log "shot $1 ok"
}

# 3. launch, navigate (pre+post shot per tap; coordinates are estimates
#    confirmed by the shot sequence reviewed at report time)
"$ADB" -s $SERIAL shell monkey -p com.trueaxis.jetcarstunts2 1 >/dev/null 2>&1
sleep 8; check_alive "launch" || exit 23
shot nav0
"$ADB" -s $SERIAL shell input tap 700 470; sleep 2; shot nav1    # HELP AND OPTIONS
"$ADB" -s $SERIAL shell input tap 620 328; sleep 2; shot nav2    # SETTINGS
"$ADB" -s $SERIAL shell input swipe 640 600 640 200 300; sleep 2; shot soundrow0
# 4. toggle both directions with pre/post shots + asserts
"$ADB" -s $SERIAL shell input tap 600 190
check_alive "toggle-1" || exit 24; shot soundrow1
"$ADB" -s $SERIAL shell input tap 600 190
check_alive "toggle-2" || exit 25; shot soundrow2
"$ADB" -s $SERIAL logcat -d -b main,system -v threadtime 2>/dev/null | grep -E "AudioFlinger|AudioTrack|libOpenSLES|PlayerBase" | tail -n 15 | tee -a $STAGE/retest-steps.log
# 5. BACK to menu, QUIT cleanly (record clean vs unclean: persistence interpretation)
for i in 1 2 3 4; do "$ADB" -s $SERIAL shell input keyevent KEYCODE_BACK; sleep 1; done
shot backmenu
if "$ADB" -s $SERIAL shell pidof com.trueaxis.jetcarstunts2 | grep -q .; then
  "$ADB" -s $SERIAL shell input tap 200 670; sleep 4
  shot quitcheck
fi
"$ADB" -s $SERIAL shell pidof com.trueaxis.jetcarstunts2 | grep -q . \
  && log "WARN still alive; persistence inconclusive (no clean exit)" \
  || log "OK clean game exit"
# 6. cold reboot + persisted-label + audio-alive check
EPID=$(owned_emulator_pid) && kill -TERM $EPID; sleep 10
log "COLD_REBOOT"
ANDROID_AVD_HOME=$STAGE/avdhome ANDROID_SDK_ROOT=$SDK ANDROID_ADB_SERVER_PORT=5039 \
  nohup "$SDK/emulator/emulator" @hardened_api28 -port 5666 \
  -no-snapshot -no-snapshot-save -no-boot-anim -adb-path "$ADB" \
  -gpu swiftshader_indirect -memory 1536 -no-window -qemu -net none \
  > $STAGE/emulator-retest2.log 2>&1 &
echo $! > $STAGE/emulator.pid
[ -n "$(owned_emulator_pid)" ] || { log "FAIL reboot identity"; exit 26; }
deadline=$(( $(date +%s) + 180 ))
while [ $(date +%s) -lt $deadline ]; do
  kill -0 $(cat $STAGE/emulator.pid) 2>/dev/null || { log "FAIL reboot exited"; exit 26; }
  if (ss -ltn 2>/dev/null | grep -q ':5667 '); then
    "$ADB" connect $SERIAL >/dev/null 2>&1
    state=$("$ADB" -s $SERIAL get-state 2>/dev/null | tr -d '\r')
    boot=$("$ADB" -s $SERIAL shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    if [ "$state" = "device" ] && [ "$boot" = "1" ]; then log "REBOOT_COMPLETE"; break; fi
  fi
  sleep 3
done
"$ADB" -s $SERIAL shell monkey -p com.trueaxis.jetcarstunts2 1 >/dev/null 2>&1
sleep 8; check_alive "cold-relaunch" || exit 27
"$ADB" -s $SERIAL shell input tap 700 470; sleep 2
"$ADB" -s $SERIAL shell input tap 620 328; sleep 2
"$ADB" -s $SERIAL shell input swipe 640 600 640 200 300; sleep 2
shot soundrow3
"$ADB" -s $SERIAL logcat -d -b main,system -v threadtime 2>/dev/null | grep -E "AudioFlinger|AudioTrack|libOpenSLES" | tail -n 8 | tee -a $STAGE/retest-steps.log

# 7. scrubbed excerpt for reports (raw stays local-only; never publish raw)
grep -aE "Fatal signal|FAST denied|createTrack_l|Force finishing activity com.trueaxis|PlayerBase|ActivityManager: Displayed com.trueaxis" \
  $STAGE/logcat-retest-raw.txt | grep -avE "orderId|purchaseToken|GPA\.|System\.out" \
  > $STAGE/logcat-retest-scrubbed.txt
wc -l $STAGE/logcat-retest-raw.txt $STAGE/logcat-retest-scrubbed.txt | tee -a $STAGE/retest-steps.log
log "PLAN_END (trap performs owned cleanup on exit)"

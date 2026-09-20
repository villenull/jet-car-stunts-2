#!/usr/bin/env bash
# JetCarStunts 2 (Android port) — Steam non-Steam shortcut target.
# Point the shortcut at this script. Menus use the emulator's native
# pointer/touch input. The Python bridge handles driving controls only;
# it does not synthesize menu taps or start a menu-highlight overlay.
# See controller-notes.txt for the current input scope and validation limits.
#
# Stop any Desktop game session first: only ONE session can own ports
# 5038/5594/5595. Launch failures are recorded in the lane's log directory
# (state/logs for the portable layout; JCS2_LOGDIR overrides it).
set -Eeuo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."

# Share the runner's explicit legacy/portable selection and root-relative overrides.
# Capture status before eval so configuration errors cannot silently fall through.
#
# Gaming Mode runs its own session and lets linux-launcher/gamescope_window.py
# present the game window, so no desktop-only variable (DISPLAY, WAYLAND_DISPLAY,
# XDG_RUNTIME_DIR, HYPRLAND_INSTANCE_SIGNATURE) is baked in here. Supply the lane
# spec the desktop supervisor uses: the portable layout, the profile prepared for
# it, and the prepared legacy32 SDK under state/. Explicit JCS2_* values from the
# environment still win, and an installed copy (the jcs2-layout.json marker that
# runtime_paths.py reads) keeps its own runtime/sdk, state/avd and profile name.
if [[ ! -f "$PWD/jcs2-layout.json" ]]; then
  export JCS2_LAYOUT="${JCS2_LAYOUT:-portable}"
  export JCS2_AVD="${JCS2_AVD:-jcs2-fresh}"
  export JCS2_SDK="${JCS2_SDK:-$PWD/state/emulator-ab-10696886}"
fi
# Qt must not probe XI2 under the Gamescope session; the runner owns the window.
export QT_XCB_NO_XI2="${QT_XCB_NO_XI2:-1}"

# Select JCS2_PYTHON, then its runtime_paths: an installed jcs2-layout.json
# chooses the portable layout without relying on Steam launch options.
source "$PWD/linux-launcher/python-select.sh"
jcs2_select_python "$PWD" || exit $?
if ! runtime_env="$("$JCS2_PYTHON" "$PWD/linux-launcher/runtime_paths.py" --shell)"; then
  echo "Invalid JCS2 runtime path configuration" >&2
  exit 10
fi
eval "$runtime_env"
LOG_ROOT="$JCS2_LOGDIR"
mkdir -p "$LOG_ROOT"
STARTUP_LOG="$LOG_ROOT/steam-launch-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
echo "JCS2 Steam launch log: $STARTUP_LOG"
exec >>"$STARTUP_LOG" 2>&1
trap 'status=$?; echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Steam launcher exit status=$status"' EXIT
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Steam launcher start pid=$$ python=$JCS2_PYTHON"

# Steam injects both overlay architectures into every subprocess, including
# adb. Exclude it so loader diagnostics do not pollute tool discovery output.
# Keep unrelated preload entries; only exclude Steam's overlay renderer.
preload_keep=()
preload_value="${LD_PRELOAD:-}"
read -r -a preload_entries <<< "${preload_value//:/ }"
for entry in "${preload_entries[@]}"; do
  case "$entry" in
    */gameoverlayrenderer.so) ;;
    *) preload_keep+=("$entry") ;;
  esac
done
if ((${#preload_keep[@]})); then
  export LD_PRELOAD="${preload_keep[*]}"
else
  unset LD_PRELOAD
fi

export ADB_SERVER_PORT=5038

# Check the selected SDK itself; a system adb cannot substitute for this runtime.
if [[ ! -x "$ANDROID_SDK_ROOT/platform-tools/adb" ]]; then
  echo "adb not found in selected SDK: $ANDROID_SDK_ROOT/platform-tools/adb" >&2
  exit 10
fi

# Only one session can own the emulator/adb ports. Fail fast instead of
# booting a black, unresponsive window over a live Desktop session.
if (exec 3<>/dev/tcp/127.0.0.1/5038) 2>/dev/null; then
  echo "JCS2 is already running (port 5038 busy). Stop the other session first." >&2
  exit 11
fi
for p in 5594 5595; do
  if (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null; then
    echo "JCS2 port $p busy. Stop the other session first." >&2
    exit 11
  fi
done

# The runner itself needs no sudo authorization. Keep this small supervisor
# alive so early failures and the final status remain available after Steam
# returns to Play. Forward stop signals and wait for the runner's owned-process
# cleanup before exiting; a signal interrupts Bash's wait, so retry if alive.
runner_pid=""
forward_signal() {
  if [[ -n "$runner_pid" ]]; then
    kill -s "$1" "$runner_pid" 2>/dev/null || true
  fi
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT
# Always launch the physical joystick lane the desktop supervisor uses; any
# earlier --input (a manual QA override) is superseded because the runner's
# parser takes the last occurrence.
./run-jcs2 "$@" --input joystick &
runner_pid=$!
while true; do
  if wait "$runner_pid"; then
    status=0
  else
    status=$?
  fi
  if ! kill -0 "$runner_pid" 2>/dev/null; then
    break
  fi
done
exit "$status"

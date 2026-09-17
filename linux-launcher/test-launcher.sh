#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
L="$ROOT/linux-launcher/jcs2-launcher.sh"
R="$ROOT/linux-launcher/runner.py"
grep -q 'ADB_PORT = 5038' "$R"
grep -q 'CONSOLE_PORT = 5594' "$R"
grep -q 'SERIAL = "127.0.0.1:5595"' "$R"
grep -q -- '"-no-snapshot"' "$R"
grep -q -- '"-net", "none"' "$R"
grep -q 'start_new_session=True' "$ROOT/linux-launcher/runner.py"
grep -q 'mResumedActivity' "$R"
grep -q 'device.*connected.*false' "$R"
grep -q '"link", "show", "up"' "$R"
grep -q 'test.*-w.*/dev/uhid' "$R"
grep -q 'killpg' "$R"
! grep -qE '(^|[[:space:]])(install(-multiple)?|uninstall|restore|wipe-data)' "$ROOT/linux-launcher/runner.py"
! grep -qE 'adb(-| )+kill-server|killall|pkill' "$R"
! grep -qE '(^|[[:space:]])(wipe-data|uninstall|restore)' "$R"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
if JCS2_SDK="$tmp/missing" "$L" >"$tmp/out" 2>&1; then exit 1; fi
grep -q 'missing SDK tools' "$tmp/out"
echo 'linux-launcher regression checks: PASS'

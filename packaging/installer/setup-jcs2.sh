#!/usr/bin/env bash
# Jet Car Stunts 2 — sticky single-file setup entry point for SteamOS.
#
# Run this from the extracted jet-car-stunts-2-setup archive:
#
#   ./setup-jcs2.sh                     # dry-run: report exactly what it would do
#   ./setup-jcs2.sh --execute --accept-licenses \
#       --archives-dir /path/to/pinned-archives --payload-dir /path/to/payload
#
# It only chooses an interpreter and forwards every argument to
# packaging/installer/install_jcs2.py, which owns the whole stage list.
# The runtime python is preferred once it exists, so a re-run is not tied to
# the host interpreter version.
set -Eeuo pipefail

here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
installer="$here/packaging/installer/install_jcs2.py"

if [ ! -f "$installer" ]; then
  echo "setup-jcs2: missing $installer (extract the whole archive, not just this script)" >&2
  exit 2
fi

python=""
if [ -x "$here/runtime/python/bin/python3" ]; then
  python="$here/runtime/python/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  python="$(command -v python3)"
fi
if [ -z "$python" ]; then
  echo "setup-jcs2: no python3 found (install python3 or run $installer with an interpreter)" >&2
  exit 2
fi

exec "$python" "$installer" --root "$here" "$@"

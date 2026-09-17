#!/usr/bin/env bash
set -Eeuo pipefail
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
source "$here/python-select.sh"
jcs2_select_python "$(dirname "$here")" || exit $?
exec "$JCS2_PYTHON" "$here/runner.py" "$@"

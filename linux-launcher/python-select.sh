# Sourced by the launcher scripts. Sets JCS2_PYTHON for the given install root:
# an explicit JCS2_PYTHON must be executable; otherwise prefer the installed
# runtime/python interpreter, then the host python3 (legacy checkouts).
jcs2_select_python() {
  local root="$1"
  if [[ -n "${JCS2_PYTHON:-}" ]]; then
    if [[ "$JCS2_PYTHON" != */* ]]; then
      JCS2_PYTHON="$(command -v -- "$JCS2_PYTHON" || true)"
    fi
    if [[ -z "$JCS2_PYTHON" || ! -f "$JCS2_PYTHON" || ! -x "$JCS2_PYTHON" ]]; then
      echo "JCS2_PYTHON is not an executable interpreter: ${JCS2_PYTHON:-unset}" >&2
      return 10
    fi
  elif [[ -x "$root/runtime/python/bin/python3" ]]; then
    JCS2_PYTHON="$root/runtime/python/bin/python3"
  elif command -v python3 >/dev/null 2>&1; then
    JCS2_PYTHON="$(command -v python3)"
  else
    echo "python3 not found; install the bundled runtime or set JCS2_PYTHON" >&2
    return 10
  fi
  export JCS2_PYTHON
}

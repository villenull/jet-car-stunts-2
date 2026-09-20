#!/usr/bin/env python3
"""Run the release allowlist of offline JCS2 test suites.

Every suite here uses fakes, temporary files, pipes, and temporary local test
sockets only: no emulator, ADB, X11 display changes, motion sensor, Steam, or
session action. Passing proves offline behavior, not physical Gaming Mode,
touch, tilt, or audio behavior. Usage: python3 run-offline-tests.py [-v] [NAME...]
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT / "linux-launcher"

# name: (argv relative to ROOT, working directory, extra environment)
SUITES = {
    "live-controls": (["linux-launcher/test-live-controls.py"], LAUNCHER, {}),
    "window-policy": (["linux-launcher/test-gamescope-window.py"], LAUNCHER, {}),
    "runner": (["linux-launcher/test-runner.py"], LAUNCHER, {}),
    "lifecycle": (["linux-launcher/test-lifecycle.py"], LAUNCHER, {}),
    "tilt": (["-m", "unittest", "tilt_control.test_tilt"], LAUNCHER, {"PYTHONPATH": str(LAUNCHER)}),
    "audio-config": (["linux-launcher/test-audio-config.py"], LAUNCHER, {}),
    "runtime-paths": (["linux-launcher/test-runtime-paths.py"], LAUNCHER, {}),
    "personal-controls": (["linux-launcher/test-personal-controls.py"], LAUNCHER, {}),
    "bridge-map": (["linux-launcher/test-bridge.py"], LAUNCHER, {}),
    "bridge-side-channel": (["linux-launcher/test-bridge-side-channel.py"], LAUNCHER, {}),
    "hud-layout": (["linux-launcher/test-hud-layout.py"], LAUNCHER, {}),
    "ebadf": (["linux-launcher/test_ebadf_fix.py"], LAUNCHER, {}),
    "steam-wrapper": (["steam/test-steam-launch.py"], ROOT, {}),
    "packaging": (["-m", "unittest", "discover", "-s", "packaging", "-p", "test_*.py"], ROOT, {}),
}
# Peer-owned suites join automatically once their files exist.
OPTIONAL = {
    "progression-choice": (["linux-launcher/test-progression-choice.py"], LAUNCHER, {}),
    "steam-shortcut": (["steam/test_steam_shortcut.py"], ROOT, {}),
    "installer": (["-m", "unittest", "discover", "-s", "packaging/installer/tests", "-t", "packaging/installer"], ROOT, {}),
}


def selected(names: list[str]):
    table = dict(SUITES)
    for name, spec in OPTIONAL.items():
        target = ROOT / (spec[0][0] if not spec[0][0].startswith("-") else spec[0][4])
        if target.exists():
            table[name] = spec
    unknown = [name for name in names if name not in table]
    if unknown:
        raise SystemExit(f"unknown suite(s): {', '.join(unknown)}; choose from {', '.join(table)}")
    return {name: table[name] for name in (names or table)}


def main() -> int:
    args = sys.argv[1:]
    verbose = "-v" in args
    names = [arg for arg in args if arg != "-v"]
    failures, total = [], 0
    for name, (argv, cwd, extra) in selected(names).items():
        env = {k: v for k, v in os.environ.items() if not k.startswith("JCS2_")}
        env.update(extra, PYTHONDONTWRITEBYTECODE="1")
        started = time.monotonic()
        # Relative script paths are resolved against ROOT regardless of cwd.
        command = [sys.executable, *([str(ROOT / argv[0]), *argv[1:]] if argv[0].endswith(".py") else argv)]
        try:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
            output, code = result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired as error:
            output, code = f"{error.stdout or ''}{error.stderr or ''}\nTIMEOUT", 124
        ran = re.findall(r"^Ran (\d+) tests?", output, re.MULTILINE)
        count = int(ran[-1]) if ran else 0
        skipped = sum(int(n) for n in re.findall(r"skipped=(\d+)", output))
        total += count
        status = "PASS" if code == 0 else "FAIL"
        detail = f"{count} tests" if ran else "script checks"
        if skipped:
            detail += f", {skipped} skipped"
        print(f"{status} {name:22} {detail} ({time.monotonic() - started:.1f}s)")
        if code != 0:
            failures.append(name)
        if verbose or code != 0:
            print(output.rstrip())
    print(f"{'FAILED' if failures else 'OK'}: {total} unittest cases across {len(selected(names))} suites"
          + (f"; failing: {', '.join(failures)}" if failures else ""))
    print("Offline evidence only; physical Gaming Mode, touch, tilt, and audio remain live tests.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

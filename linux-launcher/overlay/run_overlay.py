#!/usr/bin/env python3
"""Host overlay poll loop: follows the runner's cursor.json, hides in gameplay.

Usage: run_overlay.py <cursor.json path> [poll seconds]
Runs until killed. Never touches the guest.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from overlay.host_overlay import HostOverlay


def main() -> int:
    # gtk4-layer-shell must precede libwayland in link order under PyGObject;
    # otherwise layer surfaces silently never materialize (window falls back
    # to a tiled normal window). Re-exec with preload when missing.
    import os
    if "LD_PRELOAD" not in os.environ or "libgtk4-layer-shell" not in os.environ["LD_PRELOAD"]:
        os.environ["LD_PRELOAD"] = "/usr/lib/libgtk4-layer-shell.so" + (
            ":" + os.environ["LD_PRELOAD"] if os.environ.get("LD_PRELOAD") else "")
        os.execv(sys.executable, [sys.executable] + sys.argv)
    if len(sys.argv) < 2:
        print("usage: run_overlay.py <cursor.json> [poll_s]", flush=True)
        return 2
    path = sys.argv[1]
    poll = float(sys.argv[2]) if len(sys.argv) > 2 else 0.25
    ov = HostOverlay()
    ov.start()
    try:
        while True:
            try:
                ov.update_from_cursor_file(path)
            except Exception as exc:  # never die on a bad read
                print(f"overlay poll: {exc}", flush=True)
            time.sleep(poll)
    except KeyboardInterrupt:
        pass
    finally:
        ov.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

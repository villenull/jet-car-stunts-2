#!/usr/bin/env python3
"""D-pad menu navigation for JCS2 native OpenGL menus.

The game's True Axis engine renders menus via OpenGL without native Android
D-pad/focus support.  This module translates physical D-pad events into
``input tap`` commands at known button coordinates, with screencap-based
screen detection to separate menu from gameplay contexts.

Architecture:
  - Reads NDJSON controller events from stdin (same format as joystick_bridge)
  - Uses ADB screencap to detect which menu is active
  - Maintains a virtual cursor (highlighted button index)
  - Sends ``input tap`` via ADB when A is pressed on a highlighted button
  - Passes non-menu events through to stdout for downstream consumption
  - D-pad events are CONSUMED in menu mode (never reach gameplay)
  - D-pad events are DROPPED in gameplay mode (same as DeckControls)

Standalone test:  echo '{"type":"button","key":"DPAD_DOWN","action":"down","t_ms":0}' | \\
                  python3 menu-navigator.py --adb-path ... --serial ...

Integration: the runner would pipe bridge stdout through this before the
controller helper stdin.  This module does NOT modify joystick_bridge.py,
runner.py, or the input helper.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from menu_detect import Screen, detect_screen, REF_WIDTH, REF_HEIGHT
from menu_maps import MENUS, MenuLayout, FOOTER_QUIT, FOOTER_QUIT_LEVEL

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADB = str(ROOT / "analysis/arm-runtime-20260910T010130Z/runtime/sdk/platform-tools/adb")
DEFAULT_SERIAL = "127.0.0.1:5595"
DEFAULT_PORT = 5038

DPAD_KEYS = {"DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"}
MENU_INTERCEPT_KEYS = DPAD_KEYS | {"A"}
SCREEN_CACHE_MS = 2000
MIN_TAP_INTERVAL_MS = 300


def _get_display_size(adb_path: str, serial: str, port: int) -> tuple[int, int]:
    """Query the Android display size (landscape w, h)."""
    env = {"ANDROID_ADB_SERVER_PORT": str(port)}
    try:
        result = subprocess.run(
            [adb_path, "-s", serial, "shell", "wm", "size"],
            capture_output=True, timeout=5, env=env, text=True, check=False,
        )
        for line in result.stdout.strip().splitlines():
            if ":" in line:
                parts = line.split(":")[-1].strip().split("x")
                if len(parts) == 2:
                    w, h = int(parts[0]), int(parts[1])
                    if h > w:
                        w, h = h, w
                    return w, h
    except Exception:
        pass
    return REF_WIDTH, REF_HEIGHT


class MenuNavigator:
    """Stateful menu navigation controller."""

    def __init__(self, adb_path: str, serial: str, port: int,
                 log_file=None, passthrough: bool = True):
        self.adb_path = adb_path
        self.serial = serial
        self.port = port
        self.passthrough = passthrough
        self._log_file = log_file

        self._screen: Screen = Screen.UNKNOWN
        self._screen_time: float = 0.0
        self._layout: MenuLayout | None = None
        self._cursor: int = 0
        self._last_tap_time: float = 0.0
        self._detect_failures: int = 0

        dw, dh = _get_display_size(adb_path, serial, port)
        self._display_w = dw
        self._display_h = dh
        self._scale_x = dw / REF_WIDTH
        self._scale_y = dh / REF_HEIGHT

    def _log(self, msg: str, **kw) -> None:
        row = {"time": time.strftime("%H:%M:%S"), "msg": msg}
        row.update(kw)
        line = json.dumps(row, separators=(",", ":"))
        if self._log_file:
            self._log_file.write(line + "\n")
            self._log_file.flush()
        print(f"menu-nav: {msg}", file=sys.stderr, flush=True)

    def _adb_tap(self, x: int, y: int) -> bool:
        """Send input tap via ADB, scaling from 1920x1080 ref coords."""
        now = time.monotonic() * 1000
        if now - self._last_tap_time < MIN_TAP_INTERVAL_MS:
            return False
        tx = int(x * self._scale_x)
        ty = int(y * self._scale_y)
        env = {"ANDROID_ADB_SERVER_PORT": str(self.port)}
        try:
            subprocess.run(
                [self.adb_path, "-s", self.serial, "shell",
                 "input", "tap", str(tx), str(ty)],
                timeout=5, capture_output=True, env=env, check=False,
            )
            self._last_tap_time = now
            self._log("tap", x=x, y=y, tx=tx, ty=ty,
                       scale=f"{self._display_w}x{self._display_h}")
            return True
        except (subprocess.TimeoutExpired, OSError) as e:
            self._log("tap-failed", error=str(e))
            return False

    def _refresh_screen(self, force: bool = False) -> Screen:
        """Detect current screen, caching for SCREEN_CACHE_MS."""
        now = time.monotonic() * 1000
        if not force and now - self._screen_time < SCREEN_CACHE_MS:
            return self._screen
        try:
            screen, diag = detect_screen(self.adb_path, self.serial, self.port)
            self._screen = screen
            self._screen_time = now
            self._detect_failures = 0
            layout_name = screen.value
            new_layout = MENUS.get(layout_name)
            if new_layout is not None and (self._layout is None or self._layout.name != layout_name):
                self._layout = new_layout
                self._cursor = new_layout.default_index
                self._log("screen-change", screen=layout_name,
                          cursor=self._cursor, label=new_layout.buttons[self._cursor].label)
            elif new_layout is None:
                if self._layout is not None:
                    self._log("left-menu", screen=screen.value)
                self._layout = None
            return screen
        except Exception as e:
            self._detect_failures += 1
            self._log("detect-error", error=str(e), failures=self._detect_failures)
            if self._detect_failures > 3:
                self._screen = Screen.UNKNOWN
                self._layout = None
            return self._screen

    def _move_cursor(self, direction: str) -> None:
        """Move virtual cursor within current menu layout."""
        if self._layout is None:
            return
        old = self._cursor
        if direction == "DPAD_DOWN":
            if self._layout.nav_vertical:
                self._cursor = (self._cursor + 1) % self._layout.count
        elif direction == "DPAD_UP":
            if self._layout.nav_vertical:
                self._cursor = (self._cursor - 1) % self._layout.count
        elif direction == "DPAD_RIGHT":
            if self._layout.nav_horizontal:
                self._cursor = min(self._cursor + 1, self._layout.count - 1)
        elif direction == "DPAD_LEFT":
            if self._layout.nav_horizontal:
                self._cursor = max(self._cursor - 1, 0)
        if self._cursor != old:
            btn = self._layout.buttons[self._cursor]
            self._log("cursor", index=self._cursor, label=btn.label)

    def _select_current(self) -> None:
        """Tap the currently highlighted button."""
        if self._layout is None:
            return
        btn = self._layout.buttons[self._cursor]
        self._log("select", label=btn.label, verified=btn.verified)
        self._adb_tap(btn.x, btn.y)
        self._screen_time = 0.0

    def handle_event(self, event: dict) -> list[dict]:
        """Process one NDJSON event. Returns events to pass downstream.

        Menu-intercepted events return []; non-menu events return [event].
        """
        etype = event.get("type")
        if etype != "button":
            return [event]

        key = event.get("key", "")
        action = event.get("action", "")

        if key not in MENU_INTERCEPT_KEYS:
            return [event]

        if key in DPAD_KEYS:
            screen = self._refresh_screen()
            if screen == Screen.GAMEPLAY or screen == Screen.UNKNOWN:
                return []
            if action == "down":
                self._move_cursor(key)
            return []

        if key == "A":
            screen = self._refresh_screen()
            if self._layout is not None and action == "down":
                self._select_current()
                return []
            return [event]

        return [event]

    @property
    def state_summary(self) -> dict:
        """Current state for external inspection."""
        return {
            "screen": self._screen.value,
            "layout": self._layout.name if self._layout else None,
            "cursor": self._cursor,
            "label": (self._layout.buttons[self._cursor].label
                      if self._layout and self._cursor < self._layout.count
                      else None),
        }


def main():
    parser = argparse.ArgumentParser(description="JCS2 menu D-pad navigator")
    parser.add_argument("--adb-path", default=DEFAULT_ADB)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", default="", help="Log file path")
    parser.add_argument("--no-passthrough", action="store_true",
                        help="Don't pass non-menu events to stdout")
    parser.add_argument("--test-detect", action="store_true",
                        help="Just detect current screen and exit")
    args = parser.parse_args()

    if args.test_detect:
        screen, diag = detect_screen(args.adb_path, args.serial, args.port)
        print(json.dumps({"screen": screen.value, "diagnostics": diag}, indent=2))
        return 0

    log_file = open(args.log, "a", encoding="utf-8") if args.log else None
    nav = MenuNavigator(args.adb_path, args.serial, args.port,
                        log_file=log_file, passthrough=not args.no_passthrough)

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            downstream = nav.handle_event(event)
            if not args.no_passthrough:
                for evt in downstream:
                    print(json.dumps(evt, separators=(",", ":")), flush=True)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if log_file:
            log_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

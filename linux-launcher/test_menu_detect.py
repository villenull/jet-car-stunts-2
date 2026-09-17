#!/usr/bin/env python3
"""Tests for menu screen detection against known screenshots.

Uses PIL to convert PNG screenshots to raw RGBA for the detector,
validating classification without needing a live ADB connection.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from menu_detect import Screen, detect_screen_from_raw

try:
    from PIL import Image
except ImportError:
    print("SKIP: PIL not available")
    sys.exit(0)

ANALYSIS = ROOT / "analysis/linux-launcher"
CONTROLLER_FIX = ANALYSIS / "controller-fix"

PASS = 0
FAIL = 0


def _load_raw(path: Path) -> tuple[bytes, int, int]:
    """Load a PNG screenshot and convert to raw RGBA bytes."""
    img = Image.open(path).convert("RGBA")
    return img.tobytes(), img.width, img.height


def check(name: str, path: Path, expected: Screen):
    global PASS, FAIL
    if not path.exists():
        print(f"  SKIP {name}: {path.name} not found")
        return
    raw, w, h = _load_raw(path)
    screen, diag = detect_screen_from_raw(raw, w, h)
    status = "PASS" if screen == expected else "FAIL"
    if screen != expected:
        FAIL += 1
        print(f"  {status} {name}: expected {expected.value}, got {screen.value}")
        print(f"        diag: {json.dumps(diag)}")
    else:
        PASS += 1
        print(f"  {status} {name}: {screen.value}")


print("=== Menu Screen Detection Tests ===\n")

print("Main menu screenshots:")
check("menu-before",
      CONTROLLER_FIX / "menu-test-before.png", Screen.MAIN_MENU)
check("menu-down-hat",
      CONTROLLER_FIX / "menu-down-hat.png", Screen.MAIN_MENU)
check("menu-down-key",
      CONTROLLER_FIX / "menu-down-key.png", Screen.MAIN_MENU)

print("\nPause menu screenshots:")
check("pause-current",
      CONTROLLER_FIX / "pause-current.png", Screen.PAUSE)

print("\nGameplay screenshots:")
check("gameplay-current",
      ANALYSIS / "menu-navigation/current-state.png", Screen.GAMEPLAY)
check("menu-a-test-gameplay",
      CONTROLLER_FIX / "menu-a-test.png", Screen.GAMEPLAY)

print("\nSettings screenshots:")
check("settings-current",
      CONTROLLER_FIX / "settings-current.png", Screen.OTHER_MENU)
check("settings-lower",
      CONTROLLER_FIX / "settings-lower.png", Screen.OTHER_MENU)

# Additional screenshots from other analysis directories
OTHER_MENUS = [
    ("runtime-login-screen", ROOT / "analysis/arm-runtime-20260910T010130Z/runtime/game-menu.png", Screen.OTHER_MENU),
    ("user-current", ANALYSIS / "user-current.png", Screen.MAIN_MENU),
]
print("\nAdditional menu screenshots:")
for name, path, expected in OTHER_MENUS:
    check(name, path, expected)

# Gameplay from various sources
OTHER_GAMEPLAY = [
    ("hat-before", CONTROLLER_FIX / "before-hat-test.png", Screen.GAMEPLAY),
    ("hat-after", CONTROLLER_FIX / "after-hat-test.png", Screen.GAMEPLAY),
]
print("\nAdditional gameplay screenshots:")
for name, path, expected in OTHER_GAMEPLAY:
    check(name, path, expected)

print(f"\n=== Results: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)

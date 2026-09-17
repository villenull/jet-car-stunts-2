#!/usr/bin/env python3
"""Unit tests for the menu navigator logic (no ADB required)."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from menu_detect import Screen
from menu_maps import MAIN_MENU, PAUSE_MENU, LEVEL_SELECT
from menu_navigator import MenuNavigator

PASS = 0
FAIL = 0


def check(name: str, actual, expected):
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: expected {expected!r}, got {actual!r}")


def make_event(key: str, action: str = "down") -> dict:
    return {"type": "button", "key": key, "action": action, "t_ms": 0}


def make_axis(axis: str, value: float) -> dict:
    return {"type": "axis", "axis": axis, "value": value, "t_ms": 0}


def make_nav(screen, layout=None, cursor=0, adb_path="", serial="", port=5038):
    nav = MenuNavigator.__new__(MenuNavigator)
    nav._screen = screen
    nav._screen_time = 1e15
    nav._layout = layout
    nav._cursor = cursor
    nav._last_tap_time = 0
    nav._detect_failures = 0
    nav._log_file = None
    nav.adb_path = adb_path
    nav.serial = serial
    nav.port = port
    nav._display_w = 1920
    nav._display_h = 1080
    nav._scale_x = 1.0
    nav._scale_y = 1.0
    return nav


print("=== Menu Navigator Unit Tests ===\n")

# --- Test 1: Gameplay passthrough ---
print("Gameplay passthrough:")
nav = make_nav(Screen.GAMEPLAY)

result = nav.handle_event(make_axis("LX", 0.5))
check("axis passes through", len(result), 1)

result = nav.handle_event(make_event("DPAD_DOWN"))
check("dpad dropped in gameplay", len(result), 0)

result = nav.handle_event(make_event("RB"))
check("RB passes through", len(result), 1)

result = nav.handle_event(make_event("A"))
check("A passes through in gameplay", len(result), 1)

# --- Test 2: Main menu cursor movement ---
print("\nMain menu cursor movement:")
nav2 = make_nav(Screen.MAIN_MENU, MAIN_MENU)

check("initial cursor at 0 (PLAY)", nav2.state_summary["label"], "PLAY")

result = nav2.handle_event(make_event("DPAD_DOWN"))
check("dpad_down consumed", len(result), 0)
check("cursor moved to 1 (CREATE)", nav2.state_summary["label"], "CREATE")

result = nav2.handle_event(make_event("DPAD_DOWN"))
check("cursor at 2 (USER CHALLENGES)", nav2.state_summary["label"], "USER CHALLENGES")

result = nav2.handle_event(make_event("DPAD_UP"))
check("dpad_up back to CREATE", nav2.state_summary["label"], "CREATE")

# Wrap around
nav2._cursor = 5
result = nav2.handle_event(make_event("DPAD_DOWN"))
check("wrap to PLAY", nav2.state_summary["label"], "PLAY")

nav2._cursor = 0
result = nav2.handle_event(make_event("DPAD_UP"))
check("wrap to STORE", nav2.state_summary["label"], "STORE")

# Horizontal ignored in vertical menu
nav2._cursor = 0
result = nav2.handle_event(make_event("DPAD_RIGHT"))
check("horizontal ignored in main menu", nav2.state_summary["label"], "PLAY")

# Up release does not move cursor
nav2._cursor = 2
result = nav2.handle_event(make_event("DPAD_DOWN", "up"))
check("release does not move", nav2._cursor, 2)

# --- Test 3: Level select horizontal navigation ---
print("\nLevel select horizontal navigation:")
nav3 = make_nav(Screen.LEVEL_SELECT, LEVEL_SELECT)

check("initial at Easy", nav3.state_summary["label"], "Easy")

result = nav3.handle_event(make_event("DPAD_RIGHT"))
check("moved to Medium", nav3.state_summary["label"], "Medium")

result = nav3.handle_event(make_event("DPAD_RIGHT"))
check("moved to Hard", nav3.state_summary["label"], "Hard")

result = nav3.handle_event(make_event("DPAD_RIGHT"))
check("stays at Hard (no wrap)", nav3.state_summary["label"], "Hard")

result = nav3.handle_event(make_event("DPAD_LEFT"))
check("back to Medium", nav3.state_summary["label"], "Medium")

# Vertical ignored
result = nav3.handle_event(make_event("DPAD_DOWN"))
check("vertical ignored in level select", nav3.state_summary["label"], "Medium")

# --- Test 4: Pause menu ---
print("\nPause menu:")
nav4 = make_nav(Screen.PAUSE, PAUSE_MENU, cursor=1)

check("default at RESUME", nav4.state_summary["label"], "RESUME")

result = nav4.handle_event(make_event("DPAD_UP"))
check("moved to RESTART", nav4.state_summary["label"], "RESTART")

result = nav4.handle_event(make_event("DPAD_DOWN"))
check("back to RESUME", nav4.state_summary["label"], "RESUME")

result = nav4.handle_event(make_event("DPAD_DOWN"))
check("to HELP AND OPTIONS", nav4.state_summary["label"], "HELP AND OPTIONS")

# --- Test 5: A-press calls tap ---
print("\nA-press tap:")
nav5 = make_nav(Screen.MAIN_MENU, MAIN_MENU, adb_path="/fake/adb", serial="127.0.0.1:5595")

with patch("subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(returncode=0)
    result = nav5.handle_event(make_event("A"))
    check("A consumed in menu", len(result), 0)
    check("tap called", mock_run.called, True)
    args = mock_run.call_args
    cmd = args[0][0]
    check("tap coordinates correct", cmd[-2:], ["850", "300"])

# --- Test 5b: A-press with scaled resolution ---
print("\nA-press tap (scaled 1280x800):")
nav5b = make_nav(Screen.MAIN_MENU, MAIN_MENU, adb_path="/fake/adb", serial="127.0.0.1:5595")
nav5b._display_w = 1280
nav5b._display_h = 800
nav5b._scale_x = 1280 / 1920
nav5b._scale_y = 800 / 1080

with patch("subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(returncode=0)
    result = nav5b.handle_event(make_event("A"))
    check("A consumed in menu (scaled)", len(result), 0)
    args = mock_run.call_args
    cmd = args[0][0]
    sx, sy = int(850 * 1280 / 1920), int(300 * 800 / 1080)
    check("scaled tap X", cmd[-2], str(sx))
    check("scaled tap Y", cmd[-1], str(sy))

# --- Test 6: B passes through (already BACK) ---
print("\nB passthrough:")
nav6 = make_nav(Screen.MAIN_MENU, MAIN_MENU)

result = nav6.handle_event(make_event("B"))
check("B passes through", len(result), 1)
check("B key preserved", result[0]["key"], "B")

# --- Test 7: Unknown screen drops dpad ---
print("\nUnknown screen:")
nav7 = make_nav(Screen.UNKNOWN)

result = nav7.handle_event(make_event("DPAD_DOWN"))
check("dpad dropped in unknown", len(result), 0)

print(f"\n=== Results: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)

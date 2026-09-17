#!/usr/bin/env python3
"""Detect which JCS2 screen is active via ADB screencap pixel sampling.

Uses raw screencap format (not PNG) for minimal latency (~80ms vs ~300ms).
Samples small pixel regions at diagnostic positions to classify the screen.
All coordinates are in the 1920x1080 landscape display space.

Detection strategy:
  - Footer bar (white, y=1000, x=700-1100): present in all menus, absent in gameplay
  - Dark panel gaps (y=250, y=390): dark gray between button rows in main menu
  - Pause header bar: lighter gray (R~66) at (480, 85) vs darker (R~41) elsewhere
  - Fuel bar (orange, y=15, x=960): present during gameplay
  - Title text scanning: white text at y=90 differs between PAUSE/SETTINGS/main
"""

from __future__ import annotations

import struct
import subprocess
from enum import Enum
from typing import NamedTuple


class Screen(Enum):
    UNKNOWN = "unknown"
    MAIN_MENU = "main_menu"
    PAUSE = "pause"
    LEVEL_SELECT = "level_select"
    SETTINGS = "settings"
    GAMEPLAY = "gameplay"
    OTHER_MENU = "other_menu"


class Color(NamedTuple):
    r: int
    g: int
    b: int


REF_WIDTH = 1920
REF_HEIGHT = 1080


def _adb_screencap_raw(adb_path: str, serial: str, port: int) -> tuple[int, int, bytes]:
    """Capture raw RGBA screencap. Returns (width, height, pixel_data)."""
    env = {"ANDROID_ADB_SERVER_PORT": str(port)}
    result = subprocess.run(
        [adb_path, "-s", serial, "shell", "screencap"],
        capture_output=True, timeout=10, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"screencap failed: {result.stderr.decode(errors='replace')}")
    data = result.stdout
    if len(data) < 12:
        raise RuntimeError(f"screencap too short: {len(data)} bytes")
    width, height, pixel_format = struct.unpack_from("<III", data, 0)
    pixel_data = data[12:]
    return width, height, pixel_data


def _scale_coords(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """Scale reference coordinates (1920x1080) to actual resolution."""
    if width == REF_WIDTH and height == REF_HEIGHT:
        return x, y
    return int(x * width / REF_WIDTH), int(y * height / REF_HEIGHT)


def _sample_pixel(pixel_data: bytes, width: int, x: int, y: int) -> Color:
    """Read one RGBA pixel from raw screencap data."""
    offset = (y * width + x) * 4
    if offset + 3 >= len(pixel_data):
        return Color(0, 0, 0)
    return Color(pixel_data[offset], pixel_data[offset + 1], pixel_data[offset + 2])


def _avg_region(pixel_data: bytes, width: int, cx: int, cy: int, radius: int = 5) -> Color:
    """Average a small square region around (cx, cy)."""
    total_r = total_g = total_b = 0
    count = 0
    for dy in range(-radius, radius + 1, 2):
        for dx in range(-radius, radius + 1, 2):
            c = _sample_pixel(pixel_data, width, cx + dx, cy + dy)
            total_r += c.r
            total_g += c.g
            total_b += c.b
            count += 1
    return Color(total_r // count, total_g // count, total_b // count)


def _is_white(c: Color, threshold: int = 210) -> bool:
    return c.r > threshold and c.g > threshold and c.b > threshold


def _is_dark(c: Color, threshold: int = 90) -> bool:
    return c.r < threshold and c.g < threshold and c.b < threshold


def _has_footer_bar(pixel_data: bytes, width: int, height: int) -> bool:
    """Check for the white horizontal footer bar at y=1000 (ref coords)."""
    probes = [
        _avg_region(pixel_data, width, *_scale_coords(700, 1000, width, height), radius=max(4, int(6 * width / REF_WIDTH))),
        _avg_region(pixel_data, width, *_scale_coords(900, 1000, width, height), radius=max(4, int(6 * width / REF_WIDTH))),
        _avg_region(pixel_data, width, *_scale_coords(1100, 1000, width, height), radius=max(4, int(6 * width / REF_WIDTH))),
    ]
    whites = sum(1 for c in probes if _is_white(c, 220))
    return whites >= 2


def _has_pause_header(pixel_data: bytes, width: int, height: int) -> bool:
    """Check for the PAUSE screen's lighter gray title header bar.

    PAUSE has a distinctive gray bar at (480, 85) with R~66,G~77,B~82.
    Main menu and settings/other screens have darker values (R~41,G~53).
    """
    sx, sy = _scale_coords(480, 90, width, height)
    bar = _avg_region(pixel_data, width, sx, sy, radius=max(6, int(10 * width / REF_WIDTH)))
    return bar.r > 55 and bar.g > 65 and bar.b > 70 and bar.r < 100


def _has_dark_panel(pixel_data: bytes, width: int, height: int) -> bool:
    """Check for the dark overlay panel behind main menu buttons.

    Probe at y=200 (dark area above the red logo stripe) and y=250
    (gap between stripe and PLAY text).  Both are dark (<70) only
    in the main menu; pause and settings have lighter backgrounds.
    """
    samples = [
        _avg_region(pixel_data, width, *_scale_coords(750, 200, width, height), radius=max(3, int(4 * width / REF_WIDTH))),
        _avg_region(pixel_data, width, *_scale_coords(750, 250, width, height), radius=max(3, int(4 * width / REF_WIDTH))),
    ]
    return all(_is_dark(c, 70) for c in samples)


def _has_level_tabs(pixel_data: bytes, width: int, height: int) -> bool:
    """Check for difficulty tabs (Easy/Medium/Hard) at y~170-200."""
    tab_region = _avg_region(pixel_data, width, *_scale_coords(1300, 185, width, height), radius=max(6, int(10 * width / REF_WIDTH)))
    bg_region = _avg_region(pixel_data, width, *_scale_coords(1300, 250, width, height), radius=max(6, int(10 * width / REF_WIDTH)))
    return _is_dark(bg_region) and not _is_dark(tab_region)


def _has_fuel_bar(pixel_data: bytes, width: int, height: int) -> bool:
    """Check for the orange/red fuel bar at top center during gameplay."""
    bar = _avg_region(pixel_data, width, *_scale_coords(960, 15, width, height), radius=max(4, int(6 * width / REF_WIDTH)))
    return bar.r > 150 and bar.g > 80 and bar.b < 80


def _has_main_menu_title(pixel_data: bytes, width: int, height: int) -> bool:
    """Check for the main menu's dark top area (no title text at y=90).

    Main menu has dark gray at (640,95) with R<50.  PAUSE and SETTINGS
    have title bar text there (white or lighter gray).
    """
    probe = _avg_region(pixel_data, width, *_scale_coords(640, 95, width, height), radius=max(5, int(8 * width / REF_WIDTH)))
    return probe.r < 55 and probe.g < 65


def _classify(pixel_data: bytes, width: int, height: int) -> tuple[Screen, dict]:
    """Core classification logic."""
    footer = _has_footer_bar(pixel_data, width, height)
    pause_header = _has_pause_header(pixel_data, width, height)
    dark_panel = _has_dark_panel(pixel_data, width, height)
    level_tabs = _has_level_tabs(pixel_data, width, height)
    fuel_bar = _has_fuel_bar(pixel_data, width, height)
    main_title = _has_main_menu_title(pixel_data, width, height)

    diag = dict(footer=footer, pause_header=pause_header,
                dark_panel=dark_panel, level_tabs=level_tabs,
                fuel_bar=fuel_bar, main_title=main_title)

    if footer and pause_header:
        return Screen.PAUSE, diag
    if footer and dark_panel and main_title:
        return Screen.MAIN_MENU, diag
    if footer and level_tabs:
        return Screen.LEVEL_SELECT, diag
    if footer and main_title and not dark_panel:
        return Screen.OTHER_MENU, diag
    if footer:
        return Screen.OTHER_MENU, diag
    if fuel_bar and not footer:
        return Screen.GAMEPLAY, diag
    if not footer and not pause_header:
        return Screen.GAMEPLAY, diag

    return Screen.UNKNOWN, diag


def _is_landscape(width: int, height: int) -> bool:
    """Check if resolution is a valid landscape orientation."""
    return width > height and width >= 800


def detect_screen(adb_path: str, serial: str, port: int = 5038) -> tuple[Screen, dict]:
    """Classify the current JCS2 screen via live ADB screencap."""
    width, height, pixel_data = _adb_screencap_raw(adb_path, serial, port)
    diag: dict = {"width": width, "height": height, "raw_bytes": len(pixel_data)}

    if not _is_landscape(width, height):
        if _is_landscape(height, width):
            diag["note"] = "portrait orientation detected"
        else:
            diag["note"] = f"unexpected resolution {width}x{height}"
        return Screen.UNKNOWN, diag

    screen, class_diag = _classify(pixel_data, width, height)
    diag.update(class_diag)
    return screen, diag


def detect_screen_from_raw(pixel_data: bytes, width: int, height: int) -> tuple[Screen, dict]:
    """Classify from pre-captured raw pixel data (for testing)."""
    diag: dict = {"width": width, "height": height, "raw_bytes": len(pixel_data)}
    if not _is_landscape(width, height):
        return Screen.UNKNOWN, diag

    screen, class_diag = _classify(pixel_data, width, height)
    diag.update(class_diag)
    return screen, diag

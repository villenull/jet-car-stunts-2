"""Screen detection for JCS2 menus — resolution-adaptive, fail-closed.

Classifies game screens by sampling diagnostic pixel regions at relative
positions. All probes are expressed as fractional coordinates (0..1) of the
actual captured resolution — no hardcoded 1920x1080 reference.

Detection fails closed: ambiguous, stale, or portrait frames → UNKNOWN.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import NamedTuple

from .adb_adapter import AdbAdapter, Color, ScreenCapture


class Screen(Enum):
    UNKNOWN = "unknown"
    MAIN_MENU = "main_menu"
    PAUSE = "pause"
    LEVEL_SELECT = "level_select"
    RESULTS = "results"
    SETTINGS = "settings"
    GAMEPLAY = "gameplay"
    OTHER_MENU = "other_menu"


class DetectionResult(NamedTuple):
    screen: Screen
    diagnostics: dict
    capture_ms: float
    resolution: tuple[int, int]
    stale: bool


MAX_FRAME_AGE_MS = 3000
MIN_LANDSCAPE_WIDTH = 800


def _sample_pixel(data: bytes, width: int, x: int, y: int) -> Color:
    offset = (y * width + x) * 4
    if offset + 3 >= len(data):
        return Color(0, 0, 0)
    return Color(data[offset], data[offset + 1], data[offset + 2])


def _avg_region(data: bytes, width: int, cx: int, cy: int, radius: int) -> Color:
    r = g = b = 0
    n = 0
    for dy in range(-radius, radius + 1, 2):
        for dx in range(-radius, radius + 1, 2):
            px, py = cx + dx, cy + dy
            if 0 <= px < width and py >= 0:
                c = _sample_pixel(data, width, px, py)
                r += c.r
                g += c.g
                b += c.b
                n += 1
    if n == 0:
        return Color(0, 0, 0)
    return Color(r // n, g // n, b // n)


def _probe(data: bytes, w: int, h: int, fx: float, fy: float, r: int = 4) -> Color:
    """Sample at fractional coordinates with adaptive radius."""
    x = int(fx * w)
    y = int(fy * h)
    radius = max(3, int(r * w / 1280))
    return _avg_region(data, w, x, y, radius)


def _is_white(c: Color, thr: int = 210) -> bool:
    return c.r > thr and c.g > thr and c.b > thr


def _is_dark(c: Color, thr: int = 90) -> bool:
    return c.r < thr and c.g < thr and c.b < thr


def _check_footer(data: bytes, w: int, h: int) -> bool:
    """White horizontal footer bar at ~y=92.6% of display height.

    Probes target known-white regions of the footer bar.  The center
    probe may land on text overlays; at least 2 of 3 must be white.
    """
    fy = 0.926
    probes = [
        _probe(data, w, h, 0.25, fy),
        _probe(data, w, h, 0.50, fy),
        _probe(data, w, h, 0.85, fy),
    ]
    return sum(1 for c in probes if _is_white(c, 220)) >= 2


def _check_pause_header(data: bytes, w: int, h: int) -> bool:
    """PAUSE screen lighter gray title bar at ~(25%, 8.3%)."""
    bar = _probe(data, w, h, 0.25, 0.083, r=6)
    return 55 < bar.r < 100 and bar.g > 65 and bar.b > 70


def _check_dark_panel(data: bytes, w: int, h: int) -> bool:
    """Dark overlay behind main menu buttons at ~y=18.5% and y=23%."""
    s1 = _probe(data, w, h, 0.39, 0.185)
    s2 = _probe(data, w, h, 0.39, 0.231)
    return _is_dark(s1, 70) and _is_dark(s2, 70)


def _check_level_tabs(data: bytes, w: int, h: int) -> bool:
    """Difficulty tabs at ~y=17%, x=68%."""
    tab = _probe(data, w, h, 0.677, 0.171, r=6)
    bg = _probe(data, w, h, 0.677, 0.231, r=6)
    return _is_dark(bg) and not _is_dark(tab)


def _check_results_panel(data: bytes, w: int, h: int) -> bool:
    """RESULTS screen: large red score panel in the top-right quadrant.

    Calibrated against results-continue-red-1280x800.png (1271x800), where
    the panel spans ~(0.55..1.0, 0.04..0.34) in strong red (~206,0,0).  Both
    probes must be strong red so other screens (main-menu's dim reddish edge,
    level-select white top-right) never match.
    """
    p1 = _probe(data, w, h, 0.90, 0.20)
    p2 = _probe(data, w, h, 0.85, 0.28)
    return (p1.r > 150 and p1.g < 80 and p1.b < 80
            and p2.r > 150 and p2.g < 80 and p2.b < 80)


def _check_fuel_bar(data: bytes, w: int, h: int) -> bool:
    """Orange fuel bar at top center during gameplay."""
    bar = _probe(data, w, h, 0.5, 0.014)
    return bar.r > 150 and bar.g > 80 and bar.b < 80


def _check_main_title_area(data: bytes, w: int, h: int) -> bool:
    """Main menu has dark area at ~(33%, 8.8%) — no title text."""
    p = _probe(data, w, h, 0.333, 0.088)
    return p.r < 55 and p.g < 65


def classify_capture(cap: ScreenCapture) -> DetectionResult:
    """Classify a screen capture. Fail-closed on non-landscape or tiny frames."""
    w, h, data = cap.width, cap.height, cap.pixel_data
    diag: dict = {"width": w, "height": h, "capture_ms": cap.capture_time_ms}

    if h >= w or w < MIN_LANDSCAPE_WIDTH:
        diag["reason"] = "not_landscape"
        return DetectionResult(Screen.UNKNOWN, diag, cap.capture_time_ms, (w, h), False)

    footer = _check_footer(data, w, h)
    pause_hdr = _check_pause_header(data, w, h)
    dark_panel = _check_dark_panel(data, w, h)
    level_tabs = _check_level_tabs(data, w, h)
    fuel_bar = _check_fuel_bar(data, w, h)
    main_title = _check_main_title_area(data, w, h)
    results_panel = _check_results_panel(data, w, h)

    diag.update(footer=footer, pause_header=pause_hdr, dark_panel=dark_panel,
                level_tabs=level_tabs, fuel_bar=fuel_bar, main_title=main_title,
                results_panel=results_panel)

    # Fail-closed: all-black frames with no features → UNKNOWN (not GAMEPLAY).
    # Sample center pixel to detect loading/transition screens.
    center = _probe(data, w, h, 0.5, 0.5, r=8)
    all_features_off = not footer and not pause_hdr and not fuel_bar
    too_dark = center.r < 20 and center.g < 20 and center.b < 20

    screen = Screen.UNKNOWN
    if footer and pause_hdr:
        screen = Screen.PAUSE
    elif footer and dark_panel and main_title:
        screen = Screen.MAIN_MENU
    elif footer and level_tabs:
        screen = Screen.LEVEL_SELECT
    elif footer and not dark_panel and not pause_hdr:
        screen = Screen.OTHER_MENU
    elif results_panel:
        screen = Screen.RESULTS
    elif fuel_bar and not footer:
        screen = Screen.GAMEPLAY
    elif all_features_off and too_dark:
        # All-black with no features → UNKNOWN (loading/transition)
        screen = Screen.UNKNOWN
    elif not footer and not pause_hdr and not fuel_bar:
        screen = Screen.GAMEPLAY

    return DetectionResult(screen, diag, cap.capture_time_ms, (w, h), False)


class AsyncScreenDetector:
    """Bounded async screen detector with staleness tracking.

    Does NOT block the input/driving path — detection runs on demand with
    a cache window. Stale results beyond MAX_FRAME_AGE_MS are reported
    as UNKNOWN (fail-closed).
    """

    def __init__(self, adapter: AdbAdapter, cache_ms: float = 2000):
        self._adapter = adapter
        self._cache_ms = cache_ms
        self._last_result: DetectionResult | None = None
        self._last_time: float = 0.0
        self._consecutive_failures = 0
        self._max_failures = 3

    def detect(self, force: bool = False) -> DetectionResult:
        now = time.monotonic() * 1000
        if not force and self._last_result is not None:
            age = now - self._last_time
            if age < self._cache_ms:
                return self._last_result
            if age > MAX_FRAME_AGE_MS:
                stale = DetectionResult(
                    Screen.UNKNOWN,
                    {"reason": "stale", "age_ms": age},
                    0, (0, 0), True,
                )
                self._last_result = stale
                return stale

        try:
            cap = self._adapter.screencap_raw()
            result = classify_capture(cap)
            self._last_result = result
            self._last_time = now
            self._consecutive_failures = 0
            return result
        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_failures:
                self._last_result = DetectionResult(
                    Screen.UNKNOWN,
                    {"reason": "detection_failures", "error": str(e)[:200]},
                    0, (0, 0), False,
                )
            if self._last_result is not None:
                return self._last_result
            return DetectionResult(
                Screen.UNKNOWN, {"reason": "no_data"}, 0, (0, 0), False,
            )

    @property
    def last_resolution(self) -> tuple[int, int] | None:
        if self._last_result is not None and self._last_result.resolution != (0, 0):
            return self._last_result.resolution
        return None

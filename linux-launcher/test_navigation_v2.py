#!/usr/bin/env python3
"""Unit tests for navigation_v2 — no ADB, no guest, no live calls.

Tests cover:
  - event_types: raw physical event definitions
  - adb_adapter: fail-closed returncode checks, env merge, DryRunAdapter
  - screen_detect: resolution-adaptive classification, staleness
  - menu_layout: fractional coordinate resolution independence, neighbor tables
  - navigator: directional tap gating (menu vs gameplay), neighbor taps
  - overlay: DummyOverlay coordinate mapping
  - settings_ui: launcher settings persistence
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from navigation_v2.event_types import (
    DPAD_BUTTONS, MENU_NAV_BUTTONS, ButtonAction, PhysicalButton,
    RawAxisEvent, RawButtonEvent,
)
from navigation_v2.adb_adapter import (
    DryRunAdapter, RealAdbAdapter, ScreenCapture,
)
from navigation_v2.screen_detect import (
    AsyncScreenDetector, DetectionResult, Screen, classify_capture,
    MIN_LANDSCAPE_WIDTH,
)
from navigation_v2.menu_layout import (
    MAIN_MENU, LEVEL_SELECT, PAUSE_MENU, RESULTS_MENU, SETTINGS_MENU,
    MenuDef, MenuState, NavDirection, get_menu_for_screen,
)
from navigation_v2.navigator import MenuNavigator, NavigationTrace
from navigation_v2.overlay import DummyOverlay
from navigation_v2.settings_ui import (
    LauncherSettings, PitchMode, SettingsController, SteeringMode,
)


def _make_capture(w: int, h: int, fill: bytes = b"\x00") -> ScreenCapture:
    """Create a synthetic ScreenCapture with uniform pixel fill."""
    data = fill * (w * h)
    return ScreenCapture(w, h, data, 10.0)


def _make_rgba_capture(w: int, h: int, r: int, g: int, b: int) -> ScreenCapture:
    """Create a synthetic capture filled with a single RGBA color."""
    pixel = bytes([r, g, b, 255])
    data = pixel * (w * h)
    return ScreenCapture(w, h, data, 10.0)


def _make_capture_with_red_blocks(w: int = 1280, h: int = 800,
                                  both: bool = True) -> ScreenCapture:
    """Synthetic capture with strong-red blocks at the RESULTS panel probes.

    Stamps red squares at (0.90, 0.20) and, when `both`, (0.85, 0.28) — the
    two probe locations used by the RESULTS screen detector.
    """
    data = bytearray(b"\x00" * (w * h * 4))

    def stamp(fx: float, fy: float, color: tuple[int, int, int] = (200, 8, 8)):
        x, y = int(fx * w), int(fy * h)
        for dy in range(-10, 11):
            for dx in range(-10, 11):
                px, py = x + dx, y + dy
                if 0 <= px < w and 0 <= py < h:
                    off = (py * w + px) * 4
                    data[off] = color[0]
                    data[off + 1] = color[1]
                    data[off + 2] = color[2]
                    data[off + 3] = 255

    stamp(0.90, 0.20)
    if both:
        stamp(0.85, 0.28)
    return ScreenCapture(w, h, bytes(data), 10.0)


# ─── event_types ──────────────────────────────────────────────────────

class TestEventTypes(unittest.TestCase):
    def test_dpad_buttons_subset_of_nav(self):
        self.assertTrue(DPAD_BUTTONS.issubset(MENU_NAV_BUTTONS))

    def test_nav_buttons_include_a_b(self):
        self.assertIn(PhysicalButton.A, MENU_NAV_BUTTONS)
        self.assertIn(PhysicalButton.B, MENU_NAV_BUTTONS)

    def test_raw_button_event_fields(self):
        e = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 1000)
        self.assertEqual(e.button, PhysicalButton.A)
        self.assertEqual(e.action, ButtonAction.DOWN)
        self.assertEqual(e.timestamp_ms, 1000)

    def test_raw_axis_event_fields(self):
        e = RawAxisEvent("LX", 0.5, 2000)
        self.assertEqual(e.axis, "LX")
        self.assertAlmostEqual(e.value, 0.5)
        self.assertEqual(e.timestamp_ms, 2000)

    def test_all_physical_buttons_exist(self):
        for name in ["DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
                      "A", "B", "START", "Y"]:
            self.assertIsNotNone(PhysicalButton(name))


# ─── adb_adapter ──────────────────────────────────────────────────────

class TestAdbAdapter(unittest.TestCase):
    def test_real_adapter_env_merges_with_os_environ(self):
        """RealAdbAdapter env must include PATH and other system vars."""
        with patch.dict(os.environ, {"MY_TEST_VAR": "hello"}):
            adapter = RealAdbAdapter("/usr/bin/adb", "127.0.0.1:5595", 5038)
            self.assertIn("PATH", adapter._env)
            self.assertEqual(adapter._env["MY_TEST_VAR"], "hello")
            self.assertEqual(adapter._env["ANDROID_ADB_SERVER_PORT"], "5038")

    def test_real_adapter_fail_closed_on_nonzero_returncode(self):
        """screencap must raise on ADB failure."""
        adapter = RealAdbAdapter("/usr/bin/adb", "127.0.0.1:5595", 5038)
        mock_result = MagicMock(returncode=1, stderr=b"device not found")
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                adapter.screencap_raw()
            self.assertIn("rc=1", str(ctx.exception))

    def test_real_adapter_fail_closed_on_short_data(self):
        """screencap must raise on truncated data."""
        adapter = RealAdbAdapter("/usr/bin/adb", "127.0.0.1:5595", 5038)
        mock_result = MagicMock(returncode=0, stdout=b"\x00" * 4)
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                adapter.screencap_raw()
            self.assertIn("too short", str(ctx.exception))

    def test_real_adapter_fail_closed_on_invalid_dims(self):
        """screencap must raise on zero/negative/oversized dimensions."""
        adapter = RealAdbAdapter("/usr/bin/adb", "127.0.0.1:5595", 5038)
        header = struct.pack("<III", 0, 0, 1)  # 0x0
        mock_result = MagicMock(returncode=0, stdout=header + b"\x00" * 100)
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                adapter.screencap_raw()
            self.assertIn("invalid dimensions", str(ctx.exception))

    def test_real_adapter_fail_closed_on_pixel_truncation(self):
        """screencap must raise when pixel data is too short."""
        adapter = RealAdbAdapter("/usr/bin/adb", "127.0.0.1:5595", 5038)
        header = struct.pack("<III", 10, 10, 1)  # 10x10 = 400 bytes expected
        mock_result = MagicMock(returncode=0, stdout=header + b"\x00" * 10)
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError) as ctx:
                adapter.screencap_raw()
            self.assertIn("truncated", str(ctx.exception))

    def test_real_adapter_screencap_success(self):
        """Valid screencap returns correct dimensions."""
        adapter = RealAdbAdapter("/usr/bin/adb", "127.0.0.1:5595", 5038)
        w, h = 1280, 800
        header = struct.pack("<III", w, h, 1)
        pixels = b"\x80" * (w * h * 4)
        mock_result = MagicMock(returncode=0, stdout=header + pixels)
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=mock_result):
            cap = adapter.screencap_raw()
            self.assertEqual(cap.width, w)
            self.assertEqual(cap.height, h)
            self.assertEqual(len(cap.pixel_data), w * h * 4)

    def test_real_adapter_input_tap_checks_returncode(self):
        adapter = RealAdbAdapter("/usr/bin/adb", "127.0.0.1:5595", 5038)
        ok = MagicMock(returncode=0)
        fail = MagicMock(returncode=1)
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=ok):
            self.assertTrue(adapter.input_tap(100, 200))
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=fail):
            self.assertFalse(adapter.input_tap(100, 200))

    def test_real_adapter_input_key_checks_returncode(self):
        adapter = RealAdbAdapter("/usr/bin/adb", "127.0.0.1:5595", 5038)
        ok = MagicMock(returncode=0)
        fail = MagicMock(returncode=1)
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=ok):
            self.assertTrue(adapter.input_key(4))
        with patch("navigation_v2.adb_adapter.subprocess.run", return_value=fail):
            self.assertFalse(adapter.input_key(4))

    def test_dry_run_adapter_records_taps(self):
        cap = _make_capture(100, 100)
        adapter = DryRunAdapter(fixture_capture=cap)
        self.assertTrue(adapter.input_tap(10, 20))
        self.assertEqual(len(adapter.tap_log), 1)
        self.assertEqual(adapter.tap_log[0][:2], (10, 20))

    def test_dry_run_adapter_records_keys(self):
        cap = _make_capture(100, 100)
        adapter = DryRunAdapter(fixture_capture=cap)
        self.assertTrue(adapter.input_key(4))
        self.assertEqual(len(adapter.key_log), 1)
        self.assertEqual(adapter.key_log[0][0], 4)

    def test_dry_run_adapter_screencap_returns_fixture(self):
        cap = _make_capture(640, 480)
        adapter = DryRunAdapter(fixture_capture=cap)
        result = adapter.screencap_raw()
        self.assertEqual(result.width, 640)
        self.assertEqual(result.height, 480)
        self.assertEqual(adapter.screencap_count, 1)

    def test_dry_run_adapter_no_fixture_raises(self):
        adapter = DryRunAdapter()
        with self.assertRaises(RuntimeError):
            adapter.screencap_raw()


# ─── screen_detect ────────────────────────────────────────────────────

class TestScreenDetect(unittest.TestCase):
    def test_portrait_rejected(self):
        """Portrait captures must return UNKNOWN (fail-closed)."""
        cap = _make_capture(800, 1280)
        result = classify_capture(cap)
        self.assertEqual(result.screen, Screen.UNKNOWN)
        self.assertEqual(result.diagnostics["reason"], "not_landscape")

    def test_narrow_rejected(self):
        """Captures narrower than MIN_LANDSCAPE_WIDTH rejected."""
        cap = _make_capture(700, 500)
        result = classify_capture(cap)
        self.assertEqual(result.screen, Screen.UNKNOWN)

    def test_empty_black_unknown(self):
        """All-black landscape capture → UNKNOWN (no distinguishing features)."""
        cap = _make_rgba_capture(1280, 800, 0, 0, 0)
        result = classify_capture(cap)
        self.assertEqual(result.screen, Screen.UNKNOWN)

    def test_resolution_passed_through(self):
        """Detection result reports actual capture resolution."""
        cap = _make_capture(1920, 1080)
        result = classify_capture(cap)
        self.assertEqual(result.resolution, (1920, 1080))

    def test_resolution_1280x800_accepted(self):
        """1280x800 (Deck native) should be accepted as landscape."""
        cap = _make_capture(1280, 800)
        result = classify_capture(cap)
        # Should not be rejected for resolution reasons
        self.assertNotEqual(result.diagnostics.get("reason"), "not_landscape")

    def test_async_detector_caching(self):
        """Cached results returned within cache window."""
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=5000)
        r1 = detector.detect(force=True)
        r2 = detector.detect()  # should use cache
        self.assertEqual(r1.screen, r2.screen)
        self.assertEqual(adapter.screencap_count, 1)

    def test_async_detector_force_refresh(self):
        """force=True bypasses cache."""
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=5000)
        detector.detect(force=True)
        detector.detect(force=True)
        self.assertEqual(adapter.screencap_count, 2)

    def test_async_detector_stale_returns_unknown(self):
        """Results older than MAX_FRAME_AGE_MS reported as stale UNKNOWN."""
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=0)
        detector.detect(force=True)
        # Manually age the result
        import time
        detector._last_time = time.monotonic() * 1000 - 5000
        result = detector.detect()
        self.assertEqual(result.screen, Screen.UNKNOWN)
        self.assertTrue(result.stale)

    def test_async_detector_failure_count(self):
        """Consecutive failures tracked and eventually produce UNKNOWN."""
        adapter = DryRunAdapter()  # no fixture → raises
        detector = AsyncScreenDetector(adapter, cache_ms=0)
        for _ in range(3):
            detector.detect(force=True)
        result = detector._last_result
        self.assertEqual(result.screen, Screen.UNKNOWN)
        self.assertIn("detection_failures", result.diagnostics["reason"])

    def test_async_detector_last_resolution(self):
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=5000)
        detector.detect(force=True)
        self.assertEqual(detector.last_resolution, (1280, 800))

    def test_results_panel_classified_as_results(self):
        """Strong-red top-right panel (both probes) → RESULTS."""
        cap = _make_capture_with_red_blocks(1280, 800)
        result = classify_capture(cap)
        self.assertEqual(result.screen, Screen.RESULTS)

    def test_results_panel_requires_both_red_probes(self):
        """One red probe alone must not classify as RESULTS (fail-closed)."""
        cap = _make_capture_with_red_blocks(1280, 800, both=False)
        result = classify_capture(cap)
        self.assertNotEqual(result.screen, Screen.RESULTS)

    def test_results_panel_dim_red_rejected(self):
        """Dim reddish edge (main-menu style) must not classify as RESULTS."""
        cap = _make_capture_with_red_blocks(1280, 800)
        # Overwrite both probes with dim/dark pixels (main-menu edge color)
        data = bytearray(cap.pixel_data)
        for fx, fy in [(0.90, 0.20), (0.85, 0.28)]:
            x, y = int(fx * cap.width), int(fy * cap.height)
            for dy in range(-10, 11):
                for dx in range(-10, 11):
                    px, py = x + dx, y + dy
                    if 0 <= px < cap.width and 0 <= py < cap.height:
                        off = (py * cap.width + px) * 4
                        data[off], data[off + 1], data[off + 2] = 104, 38, 40
        dim = ScreenCapture(1280, 800, bytes(data), 10.0)
        result = classify_capture(dim)
        self.assertNotEqual(result.screen, Screen.RESULTS)


# ─── menu_layout ──────────────────────────────────────────────────────

class TestMenuLayout(unittest.TestCase):
    def test_fractional_pixel_center(self):
        """Button center computed from fractional coords × resolution."""
        btn = MAIN_MENU.buttons[0]  # PLAY
        cx, cy = btn.pixel_center(1280, 800)
        expected_x = int(btn.cx * 1280)
        expected_y = int(btn.cy * 800)
        self.assertEqual(cx, expected_x)
        self.assertEqual(cy, expected_y)

    def test_fractional_pixel_bbox(self):
        """Bounding box adapts to resolution."""
        btn = MAIN_MENU.buttons[0]
        x1, y1, x2, y2 = btn.pixel_bbox(1280, 800)
        self.assertLess(x1, x2)
        self.assertLess(y1, y2)
        # Center should be inside bbox
        cx, cy = btn.pixel_center(1280, 800)
        self.assertGreaterEqual(cx, x1)
        self.assertLessEqual(cx, x2)
        self.assertGreaterEqual(cy, y1)
        self.assertLessEqual(cy, y2)

    def test_resolution_independence(self):
        """Same fractional coords produce proportionally different pixels."""
        btn = MAIN_MENU.buttons[0]
        cx1, cy1 = btn.pixel_center(1280, 800)
        cx2, cy2 = btn.pixel_center(1920, 1080)
        # Ratios should be the same
        self.assertAlmostEqual(cx1 / 1280, cx2 / 1920, places=2)
        self.assertAlmostEqual(cy1 / 800, cy2 / 1080, places=2)

    def test_main_menu_button_count(self):
        self.assertEqual(MAIN_MENU.count, 6)
        self.assertEqual(MAIN_MENU.direction, NavDirection.VERTICAL)
        self.assertTrue(MAIN_MENU.wraps)

    def test_pause_menu_default_resume(self):
        self.assertEqual(PAUSE_MENU.default_index, 1)
        self.assertEqual(PAUSE_MENU.buttons[1].label, "RESUME")

    def test_level_select_horizontal(self):
        self.assertEqual(LEVEL_SELECT.direction, NavDirection.HORIZONTAL)
        self.assertFalse(LEVEL_SELECT.wraps)
        self.assertEqual(LEVEL_SELECT.count, 4)

    def test_settings_menu_unsupported(self):
        self.assertEqual(SETTINGS_MENU.state, MenuState.UNSUPPORTED)
        self.assertEqual(SETTINGS_MENU.count, 0)

    def test_get_menu_for_screen(self):
        self.assertEqual(get_menu_for_screen("main_menu"), MAIN_MENU)
        self.assertEqual(get_menu_for_screen("pause"), PAUSE_MENU)
        self.assertEqual(get_menu_for_screen("level_select"), LEVEL_SELECT)
        self.assertEqual(get_menu_for_screen("results"), RESULTS_MENU)
        self.assertIsNone(get_menu_for_screen("nonexistent"))

    def test_results_menu_bottom_row(self):
        """RESULTS screen navigates its bottom row: RETRY/VIEW/REPLAY/CONTINUE."""
        self.assertEqual(RESULTS_MENU.count, 4)
        labels = [b.label for b in RESULTS_MENU.buttons]
        self.assertEqual(labels, ["RETRY", "VIEW", "REPLAY", "CONTINUE"])
        self.assertEqual(RESULTS_MENU.direction, NavDirection.HORIZONTAL)
        self.assertFalse(RESULTS_MENU.wraps)
        self.assertEqual(RESULTS_MENU.state, MenuState.SUPPORTED)

    def test_results_bottom_row_x_increasing(self):
        """Bottom-row centers increase left to right."""
        for i in range(1, len(RESULTS_MENU.buttons)):
            prev = RESULTS_MENU.buttons[i - 1]
            curr = RESULTS_MENU.buttons[i]
            self.assertGreater(curr.cx, prev.cx,
                               f"{curr.label} cx should be > {prev.label} cx")

    def test_main_menu_neighbor_wrap(self):
        """Main menu vertical neighbor table wraps top↔bottom."""
        self.assertEqual(MAIN_MENU.neighbor(PhysicalButton.DPAD_UP, 0), 5)
        self.assertEqual(MAIN_MENU.neighbor(PhysicalButton.DPAD_DOWN, 0), 1)
        self.assertEqual(MAIN_MENU.neighbor(PhysicalButton.DPAD_UP, 5), 4)
        self.assertEqual(MAIN_MENU.neighbor(PhysicalButton.DPAD_DOWN, 5), 0)

    def test_pause_menu_neighbor_wrap(self):
        self.assertEqual(PAUSE_MENU.neighbor(PhysicalButton.DPAD_UP, 1), 0)
        self.assertEqual(PAUSE_MENU.neighbor(PhysicalButton.DPAD_DOWN, 2), 0)

    def test_level_select_neighbor_no_wrap(self):
        """Level select tab row: edge-locked at EASY and ALL."""
        self.assertEqual(LEVEL_SELECT.neighbor(PhysicalButton.DPAD_LEFT, 0), 0)
        self.assertEqual(LEVEL_SELECT.neighbor(PhysicalButton.DPAD_LEFT, 1), 0)
        self.assertEqual(LEVEL_SELECT.neighbor(PhysicalButton.DPAD_RIGHT, 1), 2)
        self.assertEqual(LEVEL_SELECT.neighbor(PhysicalButton.DPAD_RIGHT, 3), 3)

    def test_results_neighbor_table(self):
        """Results bottom row: edge-locked at RETRY and CONTINUE."""
        self.assertEqual(RESULTS_MENU.neighbor(PhysicalButton.DPAD_LEFT, 0), 0)
        self.assertEqual(RESULTS_MENU.neighbor(PhysicalButton.DPAD_RIGHT, 0), 1)
        self.assertEqual(RESULTS_MENU.neighbor(PhysicalButton.DPAD_RIGHT, 2), 3)
        self.assertEqual(RESULTS_MENU.neighbor(PhysicalButton.DPAD_RIGHT, 3), 3)
        self.assertEqual(RESULTS_MENU.neighbor(PhysicalButton.DPAD_LEFT, 3), 2)

    def test_neighbor_opposite_axis_edge_locks(self):
        """Vertical menus have no LEFT/RIGHT neighbors (stay put)."""
        for menu in (MAIN_MENU, PAUSE_MENU):
            for i in range(menu.count):
                self.assertEqual(menu.neighbor(PhysicalButton.DPAD_LEFT, i), i)
                self.assertEqual(menu.neighbor(PhysicalButton.DPAD_RIGHT, i), i)

    def test_neighbor_tables_stay_in_range(self):
        """Every neighbor table entry maps within the menu's button range."""
        for menu in (MAIN_MENU, PAUSE_MENU, LEVEL_SELECT, RESULTS_MENU):
            for direction, table in menu.neighbors.items():
                for src, dst in table.items():
                    self.assertIn(direction, (PhysicalButton.DPAD_UP,
                                              PhysicalButton.DPAD_DOWN,
                                              PhysicalButton.DPAD_LEFT,
                                              PhysicalButton.DPAD_RIGHT),
                                  f"{menu.name}: bad direction {direction}")
                    self.assertIn(src, range(menu.count),
                                  f"{menu.name}: source {src} out of range")
                    self.assertIn(dst, range(menu.count),
                                  f"{menu.name}: {direction}->{dst} out of range")

    def test_neighbor_tables_cover_all_entries(self):
        """Every supported menu entry is present in each direction table."""
        for menu in (MAIN_MENU, PAUSE_MENU, LEVEL_SELECT, RESULTS_MENU):
            for direction, table in menu.neighbors.items():
                self.assertEqual(
                    sorted(table.keys()), list(range(menu.count)),
                    f"{menu.name}: {direction} table must cover every entry")

    def test_unsupported_menus_have_no_neighbors(self):
        self.assertEqual(SETTINGS_MENU.neighbor(PhysicalButton.DPAD_DOWN, 0), 0)

    def test_all_buttons_have_positive_half_widths(self):
        """All fractional half-widths/half-heights must be positive."""
        for menu in [MAIN_MENU, PAUSE_MENU, LEVEL_SELECT]:
            for btn in menu.buttons:
                self.assertGreater(btn.hw, 0, f"{btn.label} hw")
                self.assertGreater(btn.hh, 0, f"{btn.label} hh")


# ─── navigator ────────────────────────────────────────────────────────

class TestNavigator(unittest.TestCase):
    def _make_nav(self, screen: Screen, menu: MenuDef | None = None,
                  cursor: int = 0, trace: bool = True) -> MenuNavigator:
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        # Force detection result
        detector._last_result = DetectionResult(
            screen, {}, 10.0, (1280, 800), False
        )
        detector._last_time = __import__("time").monotonic() * 1000

        nav_trace = NavigationTrace() if trace else None
        nav = MenuNavigator(
            detector=detector,
            adapter=adapter,
            trace=nav_trace,
        )
        # Manually set state for testing
        nav._current_screen = screen
        if menu and menu.count > 0:
            nav._current_menu = menu
            nav._cursor = cursor
            nav._active = True
        return nav

    # --- Context gating: D-pad taps adjacent in menu, passes through in gameplay ---
    def test_dpad_taps_adjacent_in_menu(self):
        """D-pad in a supported menu taps the immediately-adjacent entry."""
        nav = self._make_nav(Screen.MAIN_MENU, MAIN_MENU)
        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)  # consumed — never reaches the game
        # Cursor moved PLAY(0) → CREATE(1) and CREATE was tapped (select flash)
        self.assertEqual(nav._cursor, 1)
        self.assertEqual(len(nav._adapter.tap_log), 1)

    def test_dpad_tap_lands_on_neighbor_center(self):
        """DPAD_DOWN from PLAY taps CREATE at its fractional pixel center."""
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.MAIN_MENU, {}, 10.0, (1280, 800), False
        )
        detector._last_time = __import__("time").monotonic() * 1000
        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.MAIN_MENU
        nav._current_menu = MAIN_MENU
        nav._cursor = 0
        nav._active = True

        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        self.assertEqual(nav._cursor, 1)
        self.assertEqual(len(adapter.tap_log), 1)
        tx, ty = adapter.tap_log[0][:2]
        btn = MAIN_MENU.buttons[1]  # CREATE
        self.assertEqual((tx, ty), btn.pixel_center(1280, 800))

    def test_dpad_passthrough_in_gameplay(self):
        """D-pad passes through during gameplay (car control)."""
        nav = self._make_nav(Screen.GAMEPLAY)
        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertFalse(consumed)  # pass through to game

    def test_dpad_consumed_in_unknown(self):
        nav = self._make_nav(Screen.UNKNOWN)
        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)

    def test_dpad_dropped_in_unsupported_menu(self):
        """Unsupported menus (settings) drop D-pad — no tap map, no tap."""
        nav = self._make_nav(Screen.SETTINGS, SETTINGS_MENU)
        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)  # consumed (safe drop — fail-closed)
        self.assertEqual(len(nav._adapter.tap_log), 0)

    # --- A button: confirm tap on cursor in menu, passes through in gameplay ---
    def test_a_consumed_in_menu(self):
        nav = self._make_nav(Screen.MAIN_MENU, MAIN_MENU)
        event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)

    def test_a_passes_through_in_gameplay(self):
        nav = self._make_nav(Screen.GAMEPLAY)
        event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertFalse(consumed)

    def test_a_passes_through_when_no_menu(self):
        """A passes through when screen has no active menu."""
        nav = self._make_nav(Screen.UNKNOWN)
        event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertFalse(consumed)

    # --- B button: consumed in menu, passes through in gameplay ---
    def test_b_consumed_in_menu(self):
        nav = self._make_nav(Screen.MAIN_MENU, MAIN_MENU)
        event = RawButtonEvent(PhysicalButton.B, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        # B sends KEYCODE_BACK (4)
        self.assertEqual(len(nav._adapter.key_log), 1)
        self.assertEqual(nav._adapter.key_log[0][0], 4)

    def test_b_passes_through_in_gameplay(self):
        nav = self._make_nav(Screen.GAMEPLAY)
        event = RawButtonEvent(PhysicalButton.B, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertFalse(consumed)

    # --- Axis always passes through ---
    def test_axis_always_passes_through(self):
        nav = self._make_nav(Screen.MAIN_MENU, MAIN_MENU)
        event = RawAxisEvent("LX", 0.5, 0)
        consumed = nav.handle_axis(event)
        self.assertFalse(consumed)

    # --- Directional tap: neighbor movement (wrap + edge lock) ---
    def test_dpad_wraps_vertically_and_taps(self):
        """Main menu wraps: DPAD_DOWN from STORE (5) taps PLAY (0)."""
        nav = self._make_nav(Screen.MAIN_MENU, MAIN_MENU, cursor=5)
        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        self.assertEqual(nav._cursor, 0)
        self.assertEqual(len(nav._adapter.tap_log), 1)
        tx, ty = nav._adapter.tap_log[0][:2]
        self.assertEqual((tx, ty), MAIN_MENU.buttons[0].pixel_center(1280, 800))

    def test_dpad_forwarded_in_menu_horizontal_taps_next_tab(self):
        """Level select tab row: DPAD_RIGHT from EASY taps MEDIUM."""
        nav = self._make_nav(Screen.LEVEL_SELECT, LEVEL_SELECT, cursor=0)
        event = RawButtonEvent(PhysicalButton.DPAD_RIGHT, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        self.assertEqual(nav._cursor, 1)
        self.assertEqual(len(nav._adapter.tap_log), 1)
        tx, ty = nav._adapter.tap_log[0][:2]
        self.assertEqual((tx, ty), LEVEL_SELECT.buttons[1].pixel_center(1280, 800))

    def test_dpad_edge_locks_no_tap(self):
        """Edge with no adjacent entry: cursor stays, nothing tapped."""
        nav = self._make_nav(Screen.LEVEL_SELECT, LEVEL_SELECT, cursor=0)
        event = RawButtonEvent(PhysicalButton.DPAD_LEFT, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)  # still consumed in menu
        self.assertEqual(nav._cursor, 0)  # stays
        self.assertEqual(len(nav._adapter.tap_log), 0)  # no tap

    def test_dpad_release_consumed_no_tap(self):
        """D-pad release in menu is consumed but does not tap."""
        nav = self._make_nav(Screen.MAIN_MENU, MAIN_MENU, cursor=0)
        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.UP, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        self.assertEqual(nav._cursor, 0)
        self.assertEqual(len(nav._adapter.tap_log), 0)

    def test_results_bottom_row_directional_tap(self):
        """Results bottom row: DPAD_RIGHT from RETRY taps VIEW."""
        nav = self._make_nav(Screen.RESULTS, RESULTS_MENU, cursor=0)
        event = RawButtonEvent(PhysicalButton.DPAD_RIGHT, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        self.assertEqual(nav._cursor, 1)
        self.assertEqual(len(nav._adapter.tap_log), 1)
        tx, ty = nav._adapter.tap_log[0][:2]
        self.assertEqual((tx, ty), RESULTS_MENU.buttons[1].pixel_center(1280, 800))

    def test_a_confirm_taps_cursor_again(self):
        """A-press after a D-pad move re-taps the cursor (confirm)."""
        nav = self._make_nav(Screen.LEVEL_SELECT, LEVEL_SELECT, cursor=0)
        # DPAD_RIGHT taps MEDIUM
        nav.handle_button(
            RawButtonEvent(PhysicalButton.DPAD_RIGHT, ButtonAction.DOWN, 0))
        self.assertEqual(len(nav._adapter.tap_log), 1)
        # A re-taps MEDIUM to confirm
        nav.handle_button(
            RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0))
        self.assertEqual(nav._cursor, 1)
        self.assertEqual(len(nav._adapter.tap_log), 2)
        tx, ty = nav._adapter.tap_log[1][:2]
        self.assertEqual((tx, ty), LEVEL_SELECT.buttons[1].pixel_center(1280, 800))

    # --- Visible highlight: no longer emitted by navigator (tap flash owns it) ---
    def test_no_highlight_on_dpad_tap(self):
        """D-pad taps do not emit overlay highlight (game flashes on tap)."""
        highlights = []
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.MAIN_MENU, {}, 10.0, (1280, 800), False
        )
        detector._last_time = __import__("time").monotonic() * 1000

        def on_hl(menu_name, cursor, btn, res):
            highlights.append((menu_name, cursor, btn.label, res))

        nav = MenuNavigator(
            detector=detector,
            adapter=adapter,
            on_highlight_change=on_hl,
        )
        nav._current_screen = Screen.MAIN_MENU
        nav._current_menu = MAIN_MENU
        nav._cursor = 0
        nav._active = True

        # D-pad taps the neighbor — no overlay highlight emitted
        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.DOWN, 0)
        nav.handle_button(event)

        self.assertEqual(len(highlights), 0)  # tap flash is the game's own
        self.assertEqual(len(adapter.tap_log), 1)  # but the tap happened

    def test_highlight_not_emitted_in_unsupported_menu(self):
        """No highlight for UNSUPPORTED menus."""
        highlights = []
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.SETTINGS, {}, 10.0, (1280, 800), False
        )
        detector._last_time = __import__("time").monotonic() * 1000

        def on_hl(menu_name, cursor, btn, res):
            highlights.append((menu_name, cursor, btn.label, res))

        nav = MenuNavigator(
            detector=detector,
            adapter=adapter,
            on_highlight_change=on_hl,
        )
        nav._current_screen = Screen.SETTINGS
        nav._current_menu = SETTINGS_MENU
        nav._cursor = 0
        nav._active = False  # unsupported → not active

        # D-pad should not emit highlight
        event = RawButtonEvent(PhysicalButton.DPAD_DOWN, ButtonAction.DOWN, 0)
        nav.handle_button(event)
        self.assertEqual(len(highlights), 0)

    # --- State property ---
    def test_state_property(self):
        nav = self._make_nav(Screen.MAIN_MENU, MAIN_MENU, cursor=0)
        state = nav.state
        self.assertEqual(state["screen"], "main_menu")
        self.assertTrue(state["active"])
        self.assertEqual(state["menu"], "main_menu")
        self.assertEqual(state["cursor"], 0)
        self.assertEqual(state["label"], "PLAY")


# ─── overlay ──────────────────────────────────────────────────────────

class TestOverlay(unittest.TestCase):
    def test_dummy_overlay_records_highlight(self):
        overlay = DummyOverlay()
        self.assertFalse(overlay.available)
        overlay.set_highlight((100, 200, 300, 400), "PLAY")
        self.assertEqual(overlay.last_bbox, (100, 200, 300, 400))
        self.assertEqual(overlay.last_label, "PLAY")
        self.assertTrue(overlay.visible)

    def test_dummy_overlay_clear_highlight(self):
        overlay = DummyOverlay()
        overlay.set_highlight((100, 200, 300, 400), "PLAY")
        overlay.set_highlight(None)
        self.assertIsNone(overlay.last_bbox)
        self.assertFalse(overlay.visible)

    def test_dummy_overlay_update_display_size(self):
        overlay = DummyOverlay()
        # Should not raise
        overlay.update_display_size(1920, 1080)

    def test_dummy_overlay_noop_start_stop(self):
        overlay = DummyOverlay()
        overlay.start()  # should not raise
        overlay.stop()   # should not raise


class TestOverlayRendering(unittest.TestCase):
    """Test real overlay rendering via Cairo image surface (no GTK/Wayland)."""

    def test_render_to_surface_produces_pixels(self):
        """OverlayHighlight.render_to_surface returns RGBA data with green pixels."""
        try:
            import cairo
        except ImportError:
            self.skipTest("cairo Python bindings not available")

        from navigation_v2.overlay import OverlayHighlight
        overlay = OverlayHighlight(display_width=1280, display_height=800)

        # Set a highlight for the PLAY button region (fractional coords from
        # the committed layout; derived so this stays valid if re-measured).
        btn = MAIN_MENU.buttons[0]  # PLAY
        bbox = btn.pixel_bbox(1280, 800)
        overlay.set_highlight(bbox, "PLAY")

        # Render to a 1280x800 surface
        pixels = overlay.render_to_surface(1280, 800)
        self.assertEqual(len(pixels), 1280 * 800 * 4)

        # Verify green pixels exist inside the highlight bbox
        # Cairo ARGB32 format: bytes are [B, G, R, A] per pixel
        # Fill uses 0.15 alpha (G=38), stroke uses 0.85 alpha (G=217)
        found_green = False
        for y in range(bbox[1] + 5, bbox[3] - 5):
            for x in range(bbox[0] + 5, bbox[2] - 5):
                offset = (y * 1280 + x) * 4
                b, g, r, a = pixels[offset], pixels[offset+1], pixels[offset+2], pixels[offset+3]
                # Green channel should be dominant (0.2, 1.0, 0.2)
                # Fill area: G=38 (0.15 alpha), Stroke area: G=217 (0.85 alpha)
                if g > 30 and r < 100 and b < 100 and a > 30:
                    found_green = True
                    break
            if found_green:
                break
        self.assertTrue(found_green, "Highlight should contain green pixels")

    def test_render_empty_when_no_highlight(self):
        """render_to_surface returns all-transparent when no highlight set."""
        try:
            import cairo
        except ImportError:
            self.skipTest("cairo Python bindings not available")

        from navigation_v2.overlay import OverlayHighlight
        overlay = OverlayHighlight(display_width=1280, display_height=800)

        pixels = overlay.render_to_surface(1280, 800)
        # All pixels should be transparent (alpha=0)
        all_transparent = True
        for i in range(3, len(pixels), 4):  # check every 4th byte (alpha channel)
            if pixels[i] != 0:
                all_transparent = False
                break
        self.assertTrue(all_transparent, "No highlight should produce transparent output")

    def test_render_scaling_matches_display_size(self):
        """Highlight coordinates scale correctly with display size."""
        try:
            import cairo
        except ImportError:
            self.skipTest("cairo Python bindings not available")

        from navigation_v2.overlay import OverlayHighlight

        # Test with different display sizes
        for dw, dh, sw, sh in [(1280, 800, 1280, 800), (1920, 1080, 1920, 1080)]:
            overlay = OverlayHighlight(display_width=dw, display_height=dh)
            bbox = (100, 100, 200, 200)
            overlay.set_highlight(bbox, "TEST")

            pixels = overlay.render_to_surface(sw, sh)
            self.assertEqual(len(pixels), sw * sh * 4,
                           f"Pixel count mismatch for {dw}x{dh} -> {sw}x{sh}")


# ─── settings_ui ──────────────────────────────────────────────────────

class TestSettingsUI(unittest.TestCase):
    def test_default_settings(self):
        s = LauncherSettings()
        self.assertEqual(s.steering_mode, SteeringMode.GAMEPAD)
        self.assertEqual(s.pitch_mode, PitchMode.GAMEPAD)
        self.assertAlmostEqual(s.tilt_sensitivity, 1.0)
        self.assertFalse(s.progression_unlock_enabled)

    def test_settings_roundtrip_json(self):
        s = LauncherSettings(
            steering_mode=SteeringMode.TILT,
            pitch_mode=PitchMode.TILT,
            tilt_sensitivity=1.5,
            progression_unlock_enabled=True,
        )
        d = s.to_dict()
        s2 = LauncherSettings.from_dict(d)
        self.assertEqual(s2.steering_mode, SteeringMode.TILT)
        self.assertEqual(s2.pitch_mode, PitchMode.TILT)
        self.assertAlmostEqual(s2.tilt_sensitivity, 1.5)
        self.assertTrue(s2.progression_unlock_enabled)

    def test_settings_save_load(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            s = LauncherSettings(steering_mode=SteeringMode.TILT)
            s.save(path)
            loaded = LauncherSettings.load(path)
            self.assertEqual(loaded.steering_mode, SteeringMode.TILT)

    def test_settings_load_missing_file(self):
        s = LauncherSettings.load(Path("/nonexistent/settings.json"))
        self.assertEqual(s, LauncherSettings())

    def test_settings_controller_navigation(self):
        s = LauncherSettings()
        ctrl = SettingsController(s)
        ctrl.activate()
        self.assertTrue(ctrl.active)
        self.assertEqual(ctrl.cursor, 0)
        ctrl.move_down()
        self.assertEqual(ctrl.cursor, 1)
        ctrl.move_up()
        self.assertEqual(ctrl.cursor, 0)

    def test_settings_controller_adjust(self):
        s = LauncherSettings()
        changes = []
        ctrl = SettingsController(s, on_settings_change=lambda s: changes.append(s))
        ctrl.activate()
        ctrl.adjust_right()  # steering_mode → TILT
        self.assertEqual(s.steering_mode, SteeringMode.TILT)
        self.assertEqual(len(changes), 1)

    def test_settings_controller_select_cycles(self):
        s = LauncherSettings()
        ctrl = SettingsController(s)
        ctrl.activate()
        ctrl.select()  # cycles steering_mode
        self.assertEqual(s.steering_mode, SteeringMode.TILT)

    def test_settings_controller_calibrate(self):
        s = LauncherSettings()
        calibrated = []
        ctrl = SettingsController(s, on_calibrate=lambda: calibrated.append(True))
        ctrl.activate()
        ctrl._cursor = 5  # calibrate item (index 5 after deadzone+smoothing)
        ctrl.select()
        self.assertEqual(len(calibrated), 1)

    def test_settings_controller_inactive_noop(self):
        s = LauncherSettings()
        ctrl = SettingsController(s)
        ctrl.move_down()  # should be no-op
        self.assertEqual(ctrl.cursor, 0)

    def test_settings_controller_deactivate(self):
        s = LauncherSettings()
        ctrl = SettingsController(s)
        ctrl.activate()
        ctrl.deactivate()
        self.assertFalse(ctrl.active)

    def test_settings_controller_render_state(self):
        s = LauncherSettings()
        ctrl = SettingsController(s)
        ctrl.activate()
        state = ctrl.render_state()
        self.assertEqual(len(state), 7)  # 7 items (added deadzone, smoothing)
        self.assertTrue(state[0]["selected"])
        self.assertFalse(state[1]["selected"])

    def test_sensitivity_levels(self):
        s = LauncherSettings()
        ctrl = SettingsController(s)
        ctrl.activate()
        ctrl._cursor = 2  # sensitivity item
        ctrl.adjust_right()  # → 1.25x
        self.assertAlmostEqual(s.tilt_sensitivity, 1.25)
        ctrl.adjust_right()  # → 1.5x
        self.assertAlmostEqual(s.tilt_sensitivity, 1.5)


# ─── Navigator integration: A-tap dry-run ────────────────────────────

class TestNavigatorIntegration(unittest.TestCase):
    def test_a_tap_triggers_adapter(self):
        """A-press in menu calls adapter.input_tap with pixel coords."""
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.MAIN_MENU, {}, 10.0, (1280, 800), False
        )
        detector._last_time = __import__("time").monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.MAIN_MENU
        nav._current_menu = MAIN_MENU
        nav._cursor = 0
        nav._active = True

        event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)

        self.assertTrue(consumed)
        self.assertEqual(len(adapter.tap_log), 1)
        tx, ty = adapter.tap_log[0][:2]
        # PLAY button center, derived from the committed layout
        # (measured solid-interior center, see tap-hardening/MEASUREMENT.md)
        expected_x, expected_y = MAIN_MENU.buttons[0].pixel_center(1280, 800)
        self.assertEqual(tx, expected_x)
        self.assertEqual(ty, expected_y)

    def test_b_triggers_keycode_back(self):
        """B-press in menu sends KEYCODE_BACK (4)."""
        cap = _make_capture(1280, 800)
        adapter = DryRunAdapter(fixture_capture=cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.MAIN_MENU, {}, 10.0, (1280, 800), False
        )
        detector._last_time = __import__("time").monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.MAIN_MENU
        nav._current_menu = MAIN_MENU
        nav._cursor = 0
        nav._active = True

        event = RawButtonEvent(PhysicalButton.B, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)

        self.assertTrue(consumed)
        self.assertEqual(len(adapter.key_log), 1)
        self.assertEqual(adapter.key_log[0][0], 4)  # KEYCODE_BACK

    def test_resolution_independent_tap(self):
        """Tap coordinates scale with detected resolution."""
        for w, h in [(1280, 800), (1920, 1080), (800, 600)]:
            cap = _make_capture(w, h)
            adapter = DryRunAdapter(fixture_capture=cap)
            detector = AsyncScreenDetector(adapter, cache_ms=999999)
            detector._last_result = DetectionResult(
                Screen.MAIN_MENU, {}, 10.0, (w, h), False
            )
            detector._last_time = __import__("time").monotonic() * 1000

            nav = MenuNavigator(detector=detector, adapter=adapter)
            nav._current_screen = Screen.MAIN_MENU
            nav._current_menu = MAIN_MENU
            nav._cursor = 0
            nav._active = True

            event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
            consumed = nav.handle_button(event)
            self.assertTrue(consumed)
            tx, ty = adapter.tap_log[0][:2]
            ex, ey = MAIN_MENU.buttons[0].pixel_center(w, h)
            self.assertEqual(tx, ex)
            self.assertEqual(ty, ey)
            adapter.tap_log.clear()


# ─── Fixture interior-point assertions (menu tap hardening) ─────────────

class FixtureInteriorPointTests(unittest.TestCase):
    """Assert every supported menu button center lands ON its button in the
    actual game fixtures under analysis/linux-launcher/menu-fixtures/.

    Background: directional taps used stale centers (~30-50px high, ~50px
    right on 1280x800) so taps landed on bar edges / dark gaps and the menus
    felt dead.  Every center in menu_layout.py was re-measured against these
    fixtures with PIL, and this class re-runs that measurement so a regressed
    fixture or coordinate can never go back to an edge/gap silently.

    Measurement method (recorded, reproducible via
    analysis/linux-launcher/tap-hardening/measure.py):

      * button-red mask  : r>=140, g<=20, b<=20 (buttons are red trapezoids
        with white labels on a dark teal background).
      * solid rows       : rows whose WIDEST contiguous red run inside the
        button x-band is >= max(60px, 85% of the band's widest run).  This
        deliberately excludes label text (white glyphs split the runs) and
        the slanted edges (narrow taper rows).
      * center           : cx = median of solid-run midpoints, cy = median of
        solid rows  -> a solid-interior point, not text, not an edge.
    """

    FIXTURES = (Path(__file__).resolve().parent.parent
                / "analysis" / "linux-launcher" / "menu-fixtures")

    # (button, y-band) for MAIN_MENU's six red trapezoids (1280x800).
    MAIN_MENU_BANDS = [
        ("PLAY", 204, 283), ("CREATE", 288, 369),
        ("USER CHALLENGES", 371, 451), ("HELP AND OPTIONS", 454, 531),
        ("USER LEVELS", 537, 614), ("STORE", 621, 697),
    ]

    def _numpy(self):
        try:
            import numpy as np
        except ImportError:  # pragma: no cover
            self.skipTest("numpy not available")
        return np

    def _load_rgb(self, name):
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("PIL not available")
        path = self.FIXTURES / name
        if not path.exists():
            self.skipTest(f"fixture missing: {path}")
        return Image.open(path).convert("RGB")

    @staticmethod
    def _red_mask(a):
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        return (r >= 140) & (g <= 20) & (b <= 20)

    def _solid_center(self, mask, y0, y1, xlo=250, xhi=900,
                      min_run=60, keep=0.85):
        """Solid-interior center of a red trapezoid (method above)."""
        np = self._numpy()
        best = {}
        for y in range(y0, y1 + 1):
            xs = np.where(mask[y])[0]
            runs = []
            if len(xs):
                s = int(xs[0]); p = int(xs[0])
                for x in xs[1:]:
                    if int(x) > p + 1:
                        runs.append((s, p)); s = int(x)
                    p = int(x)
                runs.append((s, p))
            rs = [(s, p) for (s, p) in runs if s >= xlo and p <= xhi]
            if rs:
                best[y] = max(rs, key=lambda t: t[1] - t[0])
        if not best:
            self.fail("no red runs in band")
        wmax = max(p - s for s, p in best.values())
        solid = {y: (s, p) for y, (s, p) in best.items()
                 if p - s >= max(min_run, int(wmax * keep))}
        if not solid:
            solid = best
        cx = int(round(float(np.median([(s + p) // 2 for s, p in solid.values()]))))
        cy = int(round(float(np.median(sorted(solid)))))
        return cx, cy

    def _assert_close(self, got, expected, tol=4, what="center"):
        gx, gy = got
        ex, ey = expected
        self.assertLessEqual(abs(gx - ex), tol,
                             f"{what} x off: got {gx}, expected {ex}")
        self.assertLessEqual(abs(gy - ey), tol,
                             f"{what} y off: got {gy}, expected {ey}")

    def test_main_menu_centers_are_solid_red_in_fixtures(self):
        """Every MAIN_MENU center == measured solid interior AND solid red in
        both main-menu fixtures (method asserts 'interior point')."""
        from PIL import Image
        np = self._numpy()
        im1 = self._load_rgb("main-menu-1280x800.png")
        im2 = self._load_rgb("main-menu-after-back-1280x800.png")
        a1 = np.asarray(im1); m1 = self._red_mask(a1)
        a2 = np.asarray(im2)
        self.assertEqual(im1.size, (1280, 800))
        for idx, btn in enumerate(MAIN_MENU.buttons):
            _, y0, y1 = self.MAIN_MENU_BANDS[idx]
            cx, cy = self._solid_center(m1, y0, y1)
            px, py = btn.pixel_center(1280, 800)
            self._assert_close((px, py), (cx, cy),
                               what=f"{btn.label} center vs measured")
            for name, aa in (("main-menu", a1), ("after-back", a2)):
                r, g, b = aa[py, px]
                self.assertTrue(r > 120 and g < 60 and b < 60,
                                f"{btn.label} ({px},{py}) not on solid red "
                                f"in {name} fixture: {(r, g, b)}")

    def test_level_select_centers_are_solid_red(self):
        """LEVEL_SELECT difficulty tabs land on solid red tab-bar interior."""
        np = self._numpy()
        im = self._load_rgb("level-select-1280x800.png")
        a = np.asarray(im); m = self._red_mask(a)
        self.assertEqual(im.size, (1280, 800))
        for btn in LEVEL_SELECT.buttons:
            px, py = btn.pixel_center(1280, 800)
            r, g, b = a[py, px]
            self.assertTrue(r > 120 and g < 60 and b < 60,
                            f"{btn.label} tab ({px},{py}) not on solid red "
                            f"tab bar: {(r, g, b)}")
            # center is inside its own bounding box
            x1, y1, x2, y2 = btn.pixel_bbox(1280, 800)
            self.assertTrue(x1 <= px <= x2 and y1 <= py <= y2,
                            f"{btn.label} center outside bbox")

    def test_results_centers_are_on_button(self):
        """RESULTS row centers land on the dark/red bottom-row buttons.
        RETRY/VIEW/REPLAY are white labels on dark trapezoids, CONTINUE is
        the red trapezoid; 'on-button' = dark or red (not bright ground)."""
        np = self._numpy()
        im = self._load_rgb("results-continue-red-1280x800.png")
        a = np.asarray(im)
        w, h = im.size
        self.assertEqual((w, h), (1271, 800))
        for btn in RESULTS_MENU.buttons:
            px, py = btn.pixel_center(w, h)
            r, g, b = a[py, px]
            on_button = (r < 90 and g < 100 and b < 110) or \
                        (r > 140 and g < 50 and b < 50)
            self.assertTrue(on_button,
                            f"{btn.label} ({px},{py}) not on a button "
                            f"(dark trapezoid or red): {(r, g, b)}")
            x1, y1, x2, y2 = btn.pixel_bbox(w, h)
            self.assertTrue(x1 <= px <= x2 and y1 <= py <= y2,
                            f"{btn.label} center outside bbox")

    def test_measurement_method_reproduces_committed_centers(self):
        """The recorded PIL method re-derives the committed MAIN_MENU centers
        from the fixture (the exact method documented in
        analysis/linux-launcher/tap-hardening/MEASUREMENT.md)."""
        np = self._numpy()
        im = self._load_rgb("main-menu-1280x800.png")
        a = np.asarray(im)
        m = self._red_mask(a)
        for idx, btn in enumerate(MAIN_MENU.buttons):
            _, y0, y1 = self.MAIN_MENU_BANDS[idx]
            cx, cy = self._solid_center(m, y0, y1)
            px, py = btn.pixel_center(1280, 800)
            self._assert_close((px, py), (cx, cy), tol=4,
                               what=f"{btn.label} method-vs-committed")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Fixture-based tests using real game screenshots.

Loads PNG fixtures from analysis/linux-launcher/menu-fixtures/ and verifies:
  - Screen detection correctly classifies each fixture
  - Button fractional coordinates produce reasonable tap positions
  - Overlay highlight bbox covers the expected button regions
  - MenuContext integration works end-to-end with fixture data

No ADB, no guest, no live calls — pure offline verification.
"""

from __future__ import annotations

import struct
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from navigation_v2.adb_adapter import DryRunAdapter, ScreenCapture
from navigation_v2.event_types import (
    ButtonAction, PhysicalButton, RawButtonEvent,
)
from navigation_v2.menu_layout import (
    MAIN_MENU, LEVEL_SELECT, PAUSE_MENU, RESULTS_MENU,
    ButtonRegion, MenuDef, MenuState, NavDirection,
)
from navigation_v2.navigator import MenuNavigator, NavigationTrace
from navigation_v2.screen_detect import (
    AsyncScreenDetector, DetectionResult, Screen, classify_capture,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "analysis" / "linux-launcher" / "menu-fixtures"


def _load_png_as_capture(png_path: Path) -> ScreenCapture:
    """Load a PNG file and convert to ScreenCapture (RGBA raw pixel data)."""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow required for fixture loading: pip install Pillow")
    img = Image.open(png_path).convert("RGBA")
    w, h = img.size
    pixel_data = img.tobytes()
    return ScreenCapture(w, h, pixel_data, 10.0)


def _capture_has_color_at(
    cap: ScreenCapture, fx: float, fy: float,
    r_min: int, r_max: int, g_min: int, g_max: int, b_min: int, b_max: int,
    radius: int = 4,
) -> bool:
    """Check if a region around fractional coords has pixel color in range."""
    w, h, data = cap.width, cap.height, cap.pixel_data
    cx, cy = int(fx * w), int(fy * h)
    for dy in range(-radius, radius + 1, 2):
        for dx in range(-radius, radius + 1, 2):
            px, py = cx + dx, cy + dy
            if 0 <= px < w and 0 <= py < h:
                offset = (py * w + px) * 4
                r, g, b = data[offset], data[offset + 1], data[offset + 2]
                if (r_min <= r <= r_max and g_min <= g <= g_max
                        and b_min <= b <= b_max):
                    return True
    return False


class TestMainMenuFixture(unittest.TestCase):
    """Tests against the main-menu-1280x800.png fixture."""

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = FIXTURE_DIR / "main-menu-1280x800.png"
        if not cls.fixture_path.exists():
            raise unittest.SkipTest(f"Fixture not found: {cls.fixture_path}")
        cls.cap = _load_png_as_capture(cls.fixture_path)

    def test_fixture_is_landscape_1280x800(self):
        self.assertEqual(self.cap.width, 1280)
        self.assertEqual(self.cap.height, 800)

    def test_detection_classifies_as_main_menu(self):
        result = classify_capture(self.cap)
        self.assertEqual(result.screen, Screen.MAIN_MENU,
                         f"Expected MAIN_MENU, got {result.screen}. "
                         f"Diagnostics: {result.diagnostics}")

    def test_detection_has_footer(self):
        result = classify_capture(self.cap)
        self.assertTrue(result.diagnostics.get("footer"),
                        "Main menu should have white footer bar")

    def test_detection_has_dark_panel(self):
        result = classify_capture(self.cap)
        self.assertTrue(result.diagnostics.get("dark_panel"),
                        "Main menu should have dark panel behind buttons")

    def test_detection_has_main_title(self):
        result = classify_capture(self.cap)
        self.assertTrue(result.diagnostics.get("main_title"),
                        "Main menu should have dark title area")

    def test_play_button_center_has_content(self):
        """PLAY button center (measured layout) has visible content (not black)."""
        btn = MAIN_MENU.buttons[0]
        self.assertTrue(
            _capture_has_color_at(
                self.cap, btn.cx, btn.cy,
                r_min=50, r_max=255, g_min=0, g_max=255, b_min=0, b_max=255,
            ),
            "PLAY button center should have visible content",
        )

    def test_play_button_area_has_red(self):
        """PLAY button left-of-center area (inside the bar) has red background."""
        btn = MAIN_MENU.buttons[0]
        self.assertTrue(
            _capture_has_color_at(
                self.cap, btn.cx - btn.hw * 0.5, btn.cy,
                r_min=80, r_max=255, g_min=0, g_max=40, b_min=0, b_max=40,
                radius=6,
            ),
            "PLAY button area should have red background",
        )

    def test_play_button_bbox_contains_center(self):
        """PLAY button bounding box should contain its center."""
        btn = MAIN_MENU.buttons[0]
        cx, cy = btn.pixel_center(1280, 800)
        x1, y1, x2, y2 = btn.pixel_bbox(1280, 800)
        self.assertGreaterEqual(cx, x1)
        self.assertLessEqual(cx, x2)
        self.assertGreaterEqual(cy, y1)
        self.assertLessEqual(cy, y2)

    def test_all_button_centers_have_content(self):
        """All main menu button centers should have visible content (not black)."""
        for btn in MAIN_MENU.buttons:
            self.assertTrue(
                _capture_has_color_at(
                    self.cap, btn.cx, btn.cy,
                    r_min=50, r_max=255, g_min=0, g_max=255, b_min=0, b_max=255,
                ),
                f"{btn.label} center ({btn.cx}, {btn.cy}) should have content",
            )

    def test_all_button_areas_have_red(self):
        """All main menu button areas should have red backgrounds nearby."""
        for btn in MAIN_MENU.buttons:
            # Check left-of-center, still INSIDE the button bar (offset scaled
            # to the measured half-width so it never lands on the dark gap).
            self.assertTrue(
                _capture_has_color_at(
                    self.cap, btn.cx - btn.hw * 0.5, btn.cy,
                    r_min=80, r_max=255, g_min=0, g_max=40, b_min=0, b_max=40,
                    radius=6,
                ),
                f"{btn.label} area should have red background",
            )

    def test_tap_coordinates_are_valid(self):
        """A-tap should produce pixel coordinates within the capture bounds."""
        adapter = DryRunAdapter(fixture_capture=self.cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.MAIN_MENU, {}, 10.0, (1280, 800), False
        )
        detector._last_time = time.monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.MAIN_MENU
        nav._current_menu = MAIN_MENU
        nav._cursor = 0
        nav._active = True

        event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
        nav.handle_button(event)

        self.assertEqual(len(adapter.tap_log), 1)
        tx, ty = adapter.tap_log[0][:2]
        self.assertGreater(tx, 0)
        self.assertLess(tx, 1280)
        self.assertGreater(ty, 0)
        self.assertLess(ty, 800)

    def test_all_buttons_produce_valid_tap_coords(self):
        """Every button should produce valid tap coordinates within bounds."""
        for i, btn in enumerate(MAIN_MENU.buttons):
            adapter = DryRunAdapter(fixture_capture=self.cap)
            detector = AsyncScreenDetector(adapter, cache_ms=999999)
            detector._last_result = DetectionResult(
                Screen.MAIN_MENU, {}, 10.0, (1280, 800), False
            )
            detector._last_time = time.monotonic() * 1000

            nav = MenuNavigator(detector=detector, adapter=adapter)
            nav._current_screen = Screen.MAIN_MENU
            nav._current_menu = MAIN_MENU
            nav._cursor = i
            nav._active = True

            event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
            nav.handle_button(event)

            self.assertEqual(len(adapter.tap_log), 1,
                             f"Button {i} ({btn.label}) should produce a tap")
            tx, ty = adapter.tap_log[0][:2]
            self.assertGreater(tx, 0, f"{btn.label}: tap x should be > 0")
            self.assertLess(tx, 1280, f"{btn.label}: tap x should be < 1280")
            self.assertGreater(ty, 0, f"{btn.label}: tap y should be > 0")
            self.assertLess(ty, 800, f"{btn.label}: tap y should be < 800")

    def test_fixture_reloadable(self):
        """Loading the same fixture twice should produce identical captures."""
        cap2 = _load_png_as_capture(self.fixture_path)
        self.assertEqual(self.cap.width, cap2.width)
        self.assertEqual(self.cap.height, cap2.height)
        self.assertEqual(self.cap.pixel_data, cap2.pixel_data)


class TestLevelSelectFixture(unittest.TestCase):
    """Tests against the level-select-1280x800.png fixture."""

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = FIXTURE_DIR / "level-select-1280x800.png"
        if not cls.fixture_path.exists():
            raise unittest.SkipTest(f"Fixture not found: {cls.fixture_path}")
        cls.cap = _load_png_as_capture(cls.fixture_path)

    def test_fixture_is_landscape_1280x800(self):
        self.assertEqual(self.cap.width, 1280)
        self.assertEqual(self.cap.height, 800)

    def test_detection_classifies_as_level_select(self):
        result = classify_capture(self.cap)
        self.assertEqual(result.screen, Screen.LEVEL_SELECT,
                         f"Expected LEVEL_SELECT, got {result.screen}. "
                         f"Diagnostics: {result.diagnostics}")

    def test_detection_has_footer(self):
        result = classify_capture(self.cap)
        self.assertTrue(result.diagnostics.get("footer"),
                        "Level select should have white footer bar")

    def test_easy_tab_is_red(self):
        """EASY tab (selected) should have red background."""
        self.assertTrue(
            _capture_has_color_at(
                self.cap, 0.53, 0.10,
                r_min=140, r_max=255, g_min=0, g_max=30, b_min=0, b_max=30,
                radius=6,
            ),
            "EASY tab should be red (selected)",
        )

    def test_all_tabs_are_dark_gray(self):
        """ALL tab (unselected) should be dark gray."""
        self.assertTrue(
            _capture_has_color_at(
                self.cap, 0.83, 0.10,
                r_min=30, r_max=80, g_min=40, g_max=90, b_min=50, b_max=100,
                radius=6,
            ),
            "ALL tab should be dark gray (unselected)",
        )

    def test_level_select_has_four_tabs(self):
        """The level select screen has 4 difficulty tabs."""
        tabs = ["EASY", "MEDIUM", "HARD", "ALL"]
        labels = [b.label for b in LEVEL_SELECT.buttons]
        for tab in tabs:
            self.assertIn(tab, labels, f"Missing tab: {tab}")

    def test_horizontal_navigation(self):
        """Level select should use horizontal navigation."""
        self.assertEqual(LEVEL_SELECT.direction, NavDirection.HORIZONTAL)

    def test_no_wrapping(self):
        """Level select tabs should not wrap."""
        self.assertFalse(LEVEL_SELECT.wraps)

    def test_tap_coordinates_are_valid(self):
        """A-tap on each tab should produce valid pixel coordinates."""
        for i, btn in enumerate(LEVEL_SELECT.buttons):
            adapter = DryRunAdapter(fixture_capture=self.cap)
            detector = AsyncScreenDetector(adapter, cache_ms=999999)
            detector._last_result = DetectionResult(
                Screen.LEVEL_SELECT, {}, 10.0, (1280, 800), False
            )
            detector._last_time = time.monotonic() * 1000

            nav = MenuNavigator(detector=detector, adapter=adapter)
            nav._current_screen = Screen.LEVEL_SELECT
            nav._current_menu = LEVEL_SELECT
            nav._cursor = i
            nav._active = True

            event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
            nav.handle_button(event)

            self.assertEqual(len(adapter.tap_log), 1,
                             f"Tab {i} ({btn.label}) should produce a tap")
            tx, ty = adapter.tap_log[0][:2]
            self.assertGreater(tx, 0, f"{btn.label}: tap x > 0")
            self.assertLess(tx, 1280, f"{btn.label}: tap x < 1280")
            self.assertGreater(ty, 0, f"{btn.label}: tap y > 0")
            self.assertLess(ty, 800, f"{btn.label}: tap y < 800")

    def test_tab_centers_are_increasing_x(self):
        """Tab centers should have increasing x (left to right)."""
        for i in range(1, len(LEVEL_SELECT.buttons)):
            prev = LEVEL_SELECT.buttons[i - 1]
            curr = LEVEL_SELECT.buttons[i]
            self.assertGreater(curr.cx, prev.cx,
                               f"{curr.label} cx should be > {prev.label} cx")


class TestMainMenuAfterBackFixture(unittest.TestCase):
    """Tests against the main-menu-after-back-1280x800.png fixture."""

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = FIXTURE_DIR / "main-menu-after-back-1280x800.png"
        if not cls.fixture_path.exists():
            raise unittest.SkipTest(f"Fixture not found: {cls.fixture_path}")
        cls.cap = _load_png_as_capture(cls.fixture_path)

    def test_fixture_is_landscape_1280x800(self):
        self.assertEqual(self.cap.width, 1280)
        self.assertEqual(self.cap.height, 800)

    def test_detection_classifies_as_main_menu(self):
        """After pressing BACK from level select, should return to main menu."""
        result = classify_capture(self.cap)
        self.assertEqual(result.screen, Screen.MAIN_MENU,
                         f"Expected MAIN_MENU after BACK, got {result.screen}. "
                         f"Diagnostics: {result.diagnostics}")

    def test_play_button_has_content(self):
        """PLAY button should still have content after returning from level select."""
        btn = MAIN_MENU.buttons[0]
        self.assertTrue(
            _capture_has_color_at(
                self.cap, btn.cx, btn.cy,
                r_min=50, r_max=255, g_min=0, g_max=255, b_min=0, b_max=255,
            ),
            "PLAY button should have content after BACK",
        )

    def test_back_returns_to_main_menu_via_navigator(self):
        """Navigator B-press (KEYCODE_BACK) should return to main menu."""
        # Simulate: main_menu → B-press → detect main_menu again
        adapter = DryRunAdapter(fixture_capture=self.cap)
        detector = AsyncScreenDetector(adapter, cache_ms=0)
        # First detect main menu
        detector._last_result = DetectionResult(
            Screen.MAIN_MENU, {}, 10.0, (1280, 800), False
        )
        detector._last_time = time.monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.MAIN_MENU
        nav._current_menu = MAIN_MENU
        nav._cursor = 0
        nav._active = True

        # Press B
        event = RawButtonEvent(PhysicalButton.B, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed, "B should be consumed in menu mode")
        # B sends KEYCODE_BACK
        self.assertEqual(len(adapter.key_log), 1)
        self.assertEqual(adapter.key_log[0][0], 4)  # KEYCODE_BACK


class TestResultsFixture(unittest.TestCase):
    """Tests against the results-continue-red-1280x800.png fixture.

    The RESULTS screen (score panel + leaderboard) carries a horizontal
    bottom row: RETRY / VIEW / REPLAY / CONTINUE.  CONTINUE is the red
    button in the bottom-right corner.
    """

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = FIXTURE_DIR / "results-continue-red-1280x800.png"
        if not cls.fixture_path.exists():
            raise unittest.SkipTest(f"Fixture not found: {cls.fixture_path}")
        cls.cap = _load_png_as_capture(cls.fixture_path)

    def test_fixture_is_landscape(self):
        """Results fixture is landscape (PIL reports 1271x800)."""
        self.assertGreater(self.cap.width, 0)
        self.assertGreater(self.cap.height, 0)
        self.assertLess(self.cap.height, self.cap.width)

    def test_detection_classifies_as_results(self):
        """The results screen must classify as Screen.RESULTS."""
        result = classify_capture(self.cap)
        self.assertEqual(result.screen, Screen.RESULTS,
                         f"Expected RESULTS, got {result.screen}. "
                         f"Diagnostics: {result.diagnostics}")

    def test_detection_has_results_panel(self):
        """Detection diagnostic exposes the results red panel probe."""
        result = classify_capture(self.cap)
        self.assertTrue(result.diagnostics.get("results_panel"),
                        "Results screen should show the red panel diagnostic")

    def test_bottom_row_has_four_entries(self):
        """The bottom row navigates RETRY/VIEW/REPLAY/CONTINUE."""
        labels = [b.label for b in RESULTS_MENU.buttons]
        for label in ("RETRY", "VIEW", "REPLAY", "CONTINUE"):
            self.assertIn(label, labels, f"Missing bottom-row entry: {label}")
        self.assertEqual(len(RESULTS_MENU.buttons), 4)

    def test_bottom_row_horizontal_no_wrap(self):
        self.assertEqual(RESULTS_MENU.direction, NavDirection.HORIZONTAL)
        self.assertFalse(RESULTS_MENU.wraps)

    def test_continue_button_is_red(self):
        """CONTINUE (bottom-right corner) has its red background."""
        self.assertTrue(
            _capture_has_color_at(
                self.cap, 0.861, 0.930,
                r_min=120, r_max=255, g_min=0, g_max=90, b_min=0, b_max=90,
                radius=12,
            ),
            "CONTINUE button area should be red",
        )

    def test_all_bottom_row_entries_have_content(self):
        """Every bottom-row entry has bright text content nearby."""
        for btn in RESULTS_MENU.buttons:
            self.assertTrue(
                _capture_has_color_at(
                    self.cap, btn.cx, btn.cy,
                    r_min=140, r_max=255, g_min=140, g_max=255,
                    b_min=140, b_max=255, radius=12,
                ),
                f"{btn.label} should have bright text content nearby",
            )

    def test_dpad_right_taps_adjacent_bottom_row(self):
        """DPAD_RIGHT from RETRY consumes and taps VIEW (adjacent entry)."""
        adapter = DryRunAdapter(fixture_capture=self.cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.RESULTS, {}, 10.0, (self.cap.width, self.cap.height), False
        )
        detector._last_time = time.monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.RESULTS
        nav._current_menu = RESULTS_MENU
        nav._cursor = 0
        nav._active = True

        event = RawButtonEvent(PhysicalButton.DPAD_RIGHT, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed, "DPAD_RIGHT should be consumed in results menu")
        self.assertEqual(nav._cursor, 1)
        self.assertEqual(len(adapter.tap_log), 1)
        tx, ty = adapter.tap_log[0][:2]
        self.assertGreater(tx, 0)
        self.assertLess(tx, self.cap.width)
        self.assertGreater(ty, 0)
        self.assertLess(ty, self.cap.height)

    def test_dpad_walk_bottom_row_to_continue(self):
        """Walking DPAD_RIGHT from RETRY reaches CONTINUE, then edge-locks."""
        adapter = DryRunAdapter(fixture_capture=self.cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.RESULTS, {}, 10.0, (self.cap.width, self.cap.height), False
        )
        detector._last_time = time.monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.RESULTS
        nav._current_menu = RESULTS_MENU
        nav._cursor = 0
        nav._active = True

        for _ in range(4):
            nav.handle_button(
                RawButtonEvent(PhysicalButton.DPAD_RIGHT, ButtonAction.DOWN, 0))
        self.assertEqual(nav._cursor, 3)  # edge-locked at CONTINUE
        taps = len(adapter.tap_log)
        self.assertEqual(taps, 3)  # RETRY→VIEW→REPLAY→CONTINUE = 3 taps
        # Edge: DPAD_RIGHT at CONTINUE — nothing new tapped
        nav.handle_button(
            RawButtonEvent(PhysicalButton.DPAD_RIGHT, ButtonAction.DOWN, 0))
        self.assertEqual(len(adapter.tap_log), taps)

    def test_dpad_left_at_retry_edge_locks(self):
        """DPAD_LEFT at RETRY (leftmost) stays — no tap, still consumed."""
        adapter = DryRunAdapter(fixture_capture=self.cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.RESULTS, {}, 10.0, (self.cap.width, self.cap.height), False
        )
        detector._last_time = time.monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.RESULTS
        nav._current_menu = RESULTS_MENU
        nav._cursor = 0
        nav._active = True

        event = RawButtonEvent(PhysicalButton.DPAD_LEFT, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        self.assertEqual(nav._cursor, 0)
        self.assertEqual(len(adapter.tap_log), 0)

    def test_a_confirm_tap_on_cursor_within_bounds(self):
        """A on CONTINUE performs a confirm tap within the capture bounds."""
        adapter = DryRunAdapter(fixture_capture=self.cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.RESULTS, {}, 10.0, (self.cap.width, self.cap.height), False
        )
        detector._last_time = time.monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.RESULTS
        nav._current_menu = RESULTS_MENU
        nav._cursor = 3
        nav._active = True

        event = RawButtonEvent(PhysicalButton.A, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        self.assertEqual(len(adapter.tap_log), 1)
        tx, ty = adapter.tap_log[0][:2]
        self.assertGreater(tx, 0)
        self.assertLess(tx, self.cap.width)
        self.assertGreater(ty, 0)
        self.assertLess(ty, self.cap.height)

    def test_b_back_from_results(self):
        """B in the results menu issues KEYCODE_BACK."""
        adapter = DryRunAdapter(fixture_capture=self.cap)
        detector = AsyncScreenDetector(adapter, cache_ms=999999)
        detector._last_result = DetectionResult(
            Screen.RESULTS, {}, 10.0, (self.cap.width, self.cap.height), False
        )
        detector._last_time = time.monotonic() * 1000

        nav = MenuNavigator(detector=detector, adapter=adapter)
        nav._current_screen = Screen.RESULTS
        nav._current_menu = RESULTS_MENU
        nav._cursor = 0
        nav._active = True

        event = RawButtonEvent(PhysicalButton.B, ButtonAction.DOWN, 0)
        consumed = nav.handle_button(event)
        self.assertTrue(consumed)
        self.assertEqual(len(adapter.key_log), 1)
        self.assertEqual(adapter.key_log[0][0], 4)


class TestFixtureDetectionRobustness(unittest.TestCase):
    """Verify detection works across all fixtures with various conditions."""

    def setUp(self):
        self.fixtures = {}
        for name in ["main-menu-1280x800", "level-select-1280x800",
                      "main-menu-after-back-1280x800",
                      "results-continue-red-1280x800"]:
            path = FIXTURE_DIR / f"{name}.png"
            if path.exists():
                self.fixtures[name] = _load_png_as_capture(path)

    def test_all_fixtures_are_valid_captures(self):
        """All loaded fixtures should have valid dimensions and pixel data."""
        for name, cap in self.fixtures.items():
            self.assertGreater(cap.width, 0, f"{name}: width > 0")
            self.assertGreater(cap.height, 0, f"{name}: height > 0")
            self.assertLess(cap.height, cap.width, f"{name}: landscape")
            expected_len = cap.width * cap.height * 4
            self.assertEqual(len(cap.pixel_data), expected_len,
                             f"{name}: pixel data length == {expected_len}")

    def test_all_fixtures_classify_to_known_screens(self):
        """Every fixture should classify to a known screen type."""
        known_screens = {Screen.MAIN_MENU, Screen.LEVEL_SELECT, Screen.PAUSE,
                         Screen.RESULTS}
        for name, cap in self.fixtures.items():
            result = classify_capture(cap)
            self.assertIn(result.screen, known_screens,
                          f"{name}: classified as {result.screen}")

    def test_dry_run_adapter_with_each_fixture(self):
        """DryRunAdapter should work with each fixture for tap recording."""
        for name, cap in self.fixtures.items():
            adapter = DryRunAdapter(fixture_capture=cap)
            result = adapter.screencap_raw()
            self.assertEqual(result.width, cap.width, f"{name}: width match")
            self.assertEqual(result.height, cap.height, f"{name}: height match")

            # Tap should be recorded
            self.assertTrue(adapter.input_tap(100, 100), f"{name}: tap ok")
            self.assertEqual(len(adapter.tap_log), 1, f"{name}: tap logged")


if __name__ == "__main__":
    unittest.main(verbosity=2)

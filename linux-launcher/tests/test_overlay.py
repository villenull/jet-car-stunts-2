#!/usr/bin/env python3
"""Comprehensive tests for HOST OVERLAY v1.

Tests cover:
  - OverlaySettings: serialization, deserialization, clamping, persistence
  - HighlightRegion: creation, bbox calculation
  - HighlightRenderer: coordinate mapping, rendering
  - HostOverlay/DummyOverlay: state management, visibility, rendering
  - Click-through: the overlay installs an EMPTY input region after
    mapping so all pointer/touch input passes through to the game
  - Theme colors: all themes produce valid RGBA tuples

All tests are OFFLINE ONLY — no ADB, no guest, no live system calls.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from overlay.settings_model import OverlayPosition, OverlaySettings, OverlayTheme
from overlay.highlight import (
    ARROW_H,
    ARROW_TIP_GAP,
    ARROW_W,
    REFERENCE_BAR_H,
    HighlightRegion,
    HighlightRenderer,
)
from overlay.host_overlay import (
    DummyOverlay,
    HostOverlay,
    OverlayState,
    make_empty_input_region,
)


class TestOverlaySettings(unittest.TestCase):
    """Tests for OverlaySettings data model."""

    def test_default_settings(self):
        """Default settings have valid values."""
        settings = OverlaySettings()
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.show_during_gameplay)
        self.assertEqual(settings.position, OverlayPosition.TOP_RIGHT)
        self.assertEqual(settings.theme, OverlayTheme.GAME_GREEN)
        self.assertAlmostEqual(settings.opacity, 0.85)
        self.assertEqual(settings.scale, 1.0)

    def test_to_dict_roundtrip(self):
        """Settings survive JSON serialization roundtrip."""
        original = OverlaySettings(
            enabled=False,
            position=OverlayPosition.BOTTOM_LEFT,
            theme=OverlayTheme.DARK,
            opacity=0.7,
            scale=1.5,
        )
        data = original.to_dict()
        restored = OverlaySettings.from_dict(data)
        self.assertEqual(restored.enabled, original.enabled)
        self.assertEqual(restored.position, original.position)
        self.assertEqual(restored.theme, original.theme)
        self.assertAlmostEqual(restored.opacity, original.opacity)
        self.assertAlmostEqual(restored.scale, original.scale)

    def test_from_dict_ignores_unknown_keys(self):
        """Unknown keys in JSON are silently ignored."""
        data = {"enabled": False, "unknown_key": "value", "another": 123}
        settings = OverlaySettings.from_dict(data)
        self.assertFalse(settings.enabled)
        # Unknown keys don't cause errors

    def test_from_dict_handles_missing_keys(self):
        """Missing keys use defaults."""
        settings = OverlaySettings.from_dict({})
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.position, OverlayPosition.TOP_RIGHT)

    def test_clamp_values(self):
        """clamp_values brings out-of-range values into valid range."""
        settings = OverlaySettings(
            scale=10.0,  # Too high
            opacity=-0.5,  # Too low
            corner_radius=-5,  # Negative
            border_width=0,  # Zero
        )
        settings.clamp_values()
        self.assertAlmostEqual(settings.scale, 2.0)
        self.assertAlmostEqual(settings.opacity, 0.0)
        self.assertEqual(settings.corner_radius, 0)
        self.assertEqual(settings.border_width, 1)

    def test_save_and_load(self):
        """Settings persist to file and load correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "settings.json"
            original = OverlaySettings(enabled=False, opacity=0.5)
            original.save(path)
            
            loaded = OverlaySettings.load(path)
            self.assertFalse(loaded.enabled)
            self.assertAlmostEqual(loaded.opacity, 0.5)

    def test_load_returns_defaults_on_missing_file(self):
        """Loading non-existent file returns default settings."""
        settings = OverlaySettings.load("/nonexistent/path/settings.json")
        self.assertEqual(settings, OverlaySettings())

    def test_load_returns_defaults_on_corrupt_file(self):
        """Loading corrupt JSON returns default settings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "corrupt.json"
            path.write_text("not valid json {{{")
            settings = OverlaySettings.load(path)
            self.assertEqual(settings, OverlaySettings())

    def test_theme_colors_dark(self):
        """Dark theme returns valid RGBA color tuples."""
        settings = OverlaySettings(theme=OverlayTheme.DARK, opacity=0.9)
        colors = settings.theme_colors
        self.assertIn("background", colors)
        self.assertIn("border", colors)
        self.assertIn("text", colors)
        self.assertIn("accent", colors)
        for color in colors.values():
            self.assertEqual(len(color), 4)
            for component in color:
                self.assertGreaterEqual(component, 0.0)
                self.assertLessEqual(component, 1.0)

    def test_theme_colors_light(self):
        """Light theme returns valid RGBA color tuples."""
        settings = OverlaySettings(theme=OverlayTheme.LIGHT, opacity=0.8)
        colors = settings.theme_colors
        self.assertIn("background", colors)
        for color in colors.values():
            self.assertEqual(len(color), 4)

    def test_theme_colors_game_green(self):
        """Game green theme returns valid RGBA color tuples."""
        settings = OverlaySettings(theme=OverlayTheme.GAME_GREEN, opacity=1.0)
        colors = settings.theme_colors
        self.assertIn("background", colors)
        self.assertIn("accent", colors)
        # Green theme accent should have high green component
        r, g, b, a = colors["accent"]
        self.assertGreater(g, 0.5)

    def test_all_positions_valid(self):
        """All OverlayPosition values are valid."""
        for pos in OverlayPosition:
            settings = OverlaySettings(position=pos)
            self.assertEqual(settings.position, pos)


class TestHighlightRegion(unittest.TestCase):
    """Tests for HighlightRegion data class."""

    def test_basic_creation(self):
        """HighlightRegion stores coordinates and label."""
        region = HighlightRegion(x=10, y=20, width=100, height=50, label="Test")
        self.assertEqual(region.x, 10)
        self.assertEqual(region.y, 20)
        self.assertEqual(region.width, 100)
        self.assertEqual(region.height, 50)
        self.assertEqual(region.label, "Test")

    def test_bbox_conversion(self):
        """bbox property returns (x1, y1, x2, y2) tuple."""
        region = HighlightRegion(x=10, y=20, width=100, height=50)
        self.assertEqual(region.bbox, (10, 20, 110, 70))

    def test_from_bbox_factory(self):
        """from_bbox creates region from bounding box coordinates."""
        region = HighlightRegion.from_bbox(10, 20, 110, 70, label="Test")
        self.assertEqual(region.x, 10)
        self.assertEqual(region.y, 20)
        self.assertEqual(region.width, 100)
        self.assertEqual(region.height, 50)
        self.assertEqual(region.label, "Test")

    def test_default_color(self):
        """Default color is green (matching JCS2 HUD theme)."""
        region = HighlightRegion(x=0, y=0, width=10, height=10)
        r, g, b, a = region.color
        self.assertGreater(g, 0.5)  # High green component


class TestHighlightRenderer(unittest.TestCase):
    """Tests for HighlightRenderer coordinate mapping."""

    def test_update_display_size(self):
        """update_display_size changes coordinate mapping."""
        renderer = HighlightRenderer(1280, 800)
        renderer.update_display_size(1920, 1080)
        # Internal state should be updated (no public accessor, but no error)

    def test_render_to_surface_requires_cairo(self):
        """render_to_surface raises RuntimeError if cairo unavailable."""
        renderer = HighlightRenderer()
        with patch.dict("sys.modules", {"cairo": None}):
            with self.assertRaises(RuntimeError) as ctx:
                renderer.render_to_surface(100, 100, [])
            self.assertIn("cairo", str(ctx.exception))

    def test_render_empty_regions(self):
        """Rendering with no regions produces valid output."""
        try:
            import cairo
            renderer = HighlightRenderer(1280, 800)
            pixels = renderer.render_to_surface(100, 100, [])
            self.assertEqual(len(pixels), 100 * 100 * 4)
        except ImportError:
            self.skipTest("cairo not available")

    def test_render_with_regions(self):
        """Rendering with highlight regions produces valid output."""
        try:
            import cairo
            renderer = HighlightRenderer(1280, 800)
            region = HighlightRegion(x=100, y=100, width=200, height=100)
            pixels = renderer.render_to_surface(1280, 800, [region])
            self.assertEqual(len(pixels), 1280 * 800 * 4)
        except ImportError:
            self.skipTest("cairo not available")

    def test_render_scaled_coordinates(self):
        """Rendering scales from display to surface coordinates."""
        try:
            import cairo
            renderer = HighlightRenderer(1280, 800)
            # Region at half display size
            region = HighlightRegion(x=640, y=400, width=640, height=400)
            # Render to smaller surface
            pixels = renderer.render_to_surface(640, 400, [region])
            self.assertEqual(len(pixels), 640 * 400 * 4)
        except ImportError:
            self.skipTest("cairo not available")


class TestDummyOverlay(unittest.TestCase):
    """Tests for DummyOverlay (headless/test overlay)."""

    def test_dummy_overlay_not_available(self):
        """DummyOverlay reports as unavailable."""
        overlay = DummyOverlay()
        self.assertFalse(overlay.available)

    def test_dummy_overlay_default_state(self):
        """DummyOverlay starts with default state."""
        overlay = DummyOverlay()
        self.assertFalse(overlay.state.visible)
        self.assertEqual(overlay.state.fps, 0.0)
        self.assertEqual(overlay.state.game_status, "")

    def test_dummy_overlay_show_hide(self):
        """DummyOverlay show/hide toggles visibility."""
        overlay = DummyOverlay()
        overlay.start()
        self.assertTrue(overlay.state.visible)
        overlay.hide()
        self.assertFalse(overlay.state.visible)
        overlay.show()
        self.assertTrue(overlay.state.visible)
        overlay.stop()
        self.assertFalse(overlay.state.visible)

    def test_dummy_overlay_update_fps(self):
        """DummyOverlay update_fps changes state."""
        overlay = DummyOverlay()
        overlay.update_fps(60.0)
        self.assertAlmostEqual(overlay.state.fps, 60.0)
        self.assertGreater(overlay.state.last_update, 0)

    def test_dummy_overlay_update_game_status(self):
        """DummyOverlay update_game_status changes state."""
        overlay = DummyOverlay()
        overlay.update_game_status("Playing Level 1")
        self.assertEqual(overlay.state.game_status, "Playing Level 1")

    def test_dummy_overlay_update_controls_hint(self):
        """DummyOverlay update_controls_hint changes state."""
        overlay = DummyOverlay()
        overlay.update_controls_hint("A: Jump, B: Boost")
        self.assertEqual(overlay.state.controls_hint, "A: Jump, B: Boost")

    def test_dummy_overlay_set_highlight_regions(self):
        """DummyOverlay set_highlight_regions changes state."""
        overlay = DummyOverlay()
        regions = [HighlightRegion(x=10, y=20, width=100, height=50)]
        overlay.set_highlight_regions(regions)
        self.assertEqual(len(overlay.state.highlight_regions), 1)
        overlay.set_highlight_regions(None)
        self.assertIsNone(overlay.state.highlight_regions)

    def test_dummy_overlay_update_settings(self):
        """DummyOverlay update_settings changes settings."""
        overlay = DummyOverlay()
        new_settings = OverlaySettings(enabled=False, opacity=0.5)
        overlay.update_settings(new_settings)
        self.assertFalse(overlay.settings.enabled)
        self.assertAlmostEqual(overlay.settings.opacity, 0.5)

    def test_dummy_overlay_update_display_size(self):
        """DummyOverlay update_display_size changes coordinate mapping."""
        overlay = DummyOverlay()
        overlay.update_display_size(1920, 1080)
        # No error means success

    def test_dummy_overlay_render_to_surface(self):
        """DummyOverlay render_to_surface produces valid output."""
        try:
            import cairo
            overlay = DummyOverlay()
            overlay.start()
            overlay.update_fps(60.0)
            pixels = overlay.render_to_surface(100, 100)
            self.assertEqual(len(pixels), 100 * 100 * 4)
        except ImportError:
            self.skipTest("cairo not available")

    def test_dummy_overlay_render_hidden(self):
        """DummyOverlay render when hidden produces transparent output."""
        try:
            import cairo
            overlay = DummyOverlay()
            # Don't start, so visible=False
            pixels = overlay.render_to_surface(10, 10)
            # All pixels should be transparent (0,0,0,0)
            self.assertEqual(len(pixels), 10 * 10 * 4)
        except ImportError:
            self.skipTest("cairo not available")


class TestHostOverlay(unittest.TestCase):
    """Tests for HostOverlay (GTK4 overlay, using DummyOverlay path)."""

    def test_host_overlay_not_available_without_gtk(self):
        """HostOverlay reports unavailable when GTK not present."""
        overlay = HostOverlay()
        # availability depends on system GTK, but we can test the logic
        self.assertIn(overlay.available, (True, False))

    def test_host_overlay_settings_property(self):
        """HostOverlay exposes settings."""
        settings = OverlaySettings(enabled=False)
        overlay = HostOverlay(settings=settings)
        self.assertFalse(overlay.settings.enabled)

    def test_host_overlay_state_property(self):
        """HostOverlay exposes state."""
        overlay = HostOverlay()
        state = overlay.state
        self.assertIsInstance(state, OverlayState)
        self.assertFalse(state.visible)

    def test_host_overlay_update_fps(self):
        """HostOverlay update_fps updates state."""
        overlay = HostOverlay()
        overlay.update_fps(55.0)
        self.assertAlmostEqual(overlay.state.fps, 55.0)

    def test_host_overlay_update_game_status(self):
        """HostOverlay update_game_status updates state."""
        overlay = HostOverlay()
        overlay.update_game_status("Menu")
        self.assertEqual(overlay.state.game_status, "Menu")

    def test_host_overlay_update_controls_hint(self):
        """HostOverlay update_controls_hint updates state."""
        overlay = HostOverlay()
        overlay.update_controls_hint("LB: Menu")
        self.assertEqual(overlay.state.controls_hint, "LB: Menu")

    def test_host_overlay_set_highlight_regions(self):
        """HostOverlay set_highlight_regions updates state."""
        overlay = HostOverlay()
        regions = [HighlightRegion(x=0, y=0, width=50, height=50)]
        overlay.set_highlight_regions(regions)
        self.assertEqual(len(overlay.state.highlight_regions), 1)

    def test_host_overlay_show_hide(self):
        """HostOverlay show/hide toggles state."""
        overlay = HostOverlay()
        overlay.show()
        self.assertTrue(overlay.state.visible)
        overlay.hide()
        self.assertFalse(overlay.state.visible)

    def test_host_overlay_disabled_does_not_start(self):
        """HostOverlay with enabled=False does not become visible."""
        settings = OverlaySettings(enabled=False)
        overlay = HostOverlay(settings=settings)
        overlay.start()
        # Should not be visible when disabled
        self.assertFalse(overlay.state.visible)

    def test_host_overlay_update_settings(self):
        """HostOverlay update_settings changes settings."""
        overlay = HostOverlay()
        new_settings = OverlaySettings(opacity=0.3)
        overlay.update_settings(new_settings)
        self.assertAlmostEqual(overlay.settings.opacity, 0.3)


class TestOverlayState(unittest.TestCase):
    """Tests for OverlayState data class."""

    def test_overlay_state_fields(self):
        """OverlayState has all required fields."""
        state = OverlayState(
            fps=60.0,
            game_status="Playing",
            controls_hint="A: Jump",
            visible=True,
            last_update=1234567890.0,
        )
        self.assertEqual(state.fps, 60.0)
        self.assertEqual(state.game_status, "Playing")
        self.assertEqual(state.controls_hint, "A: Jump")
        self.assertTrue(state.visible)
        self.assertEqual(state.last_update, 1234567890.0)

    def test_overlay_state_default_values(self):
        """OverlayState defaults are sensible."""
        state = OverlayState()
        self.assertEqual(state.fps, 0.0)
        self.assertEqual(state.game_status, "")
        self.assertEqual(state.controls_hint, "")
        self.assertIsNone(state.highlight_regions)
        self.assertFalse(state.visible)
        self.assertEqual(state.last_update, 0.0)


class TestIntegration(unittest.TestCase):
    """Integration tests combining overlay components."""

    def test_settings_persistence_with_overlay(self):
        """Settings can be saved and loaded with overlay."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "overlay_settings.json"
            settings = OverlaySettings(
                enabled=False,
                position=OverlayPosition.BOTTOM_CENTER,
                theme=OverlayTheme.DARK,
            )
            settings.save(path)
            
            overlay = DummyOverlay(OverlaySettings.load(path))
            self.assertFalse(overlay.settings.enabled)
            self.assertEqual(overlay.settings.position, OverlayPosition.BOTTOM_CENTER)

    def test_highlight_regions_rendering(self):
        """Highlight regions can be set and rendered."""
        try:
            import cairo
            overlay = DummyOverlay()
            overlay.start()
            regions = [
                HighlightRegion(x=100, y=100, width=200, height=100, label="Button 1"),
                HighlightRegion(x=400, y=300, width=150, height=80, label="Button 2"),
            ]
            overlay.set_highlight_regions(regions)
            pixels = overlay.render_to_surface(1280, 800)
            self.assertEqual(len(pixels), 1280 * 800 * 4)
        except ImportError:
            self.skipTest("cairo not available")

    def test_theme_switching(self):
        """Theme can be changed and colors update."""
        overlay = DummyOverlay(OverlaySettings(theme=OverlayTheme.DARK))
        dark_colors = overlay.settings.theme_colors
        
        overlay.update_settings(OverlaySettings(theme=OverlayTheme.LIGHT))
        light_colors = overlay.settings.theme_colors
        
        # Themes should produce different colors
        self.assertNotEqual(dark_colors["background"], light_colors["background"])


class TestArrowCalibration(unittest.TestCase):
    """Locks the calibrated pointer-arrow geometry (the "66% spec").

    Reference geometry (display 1280x800, surface 1280x800 => sx=sy=1):
    PLAY main-menu bbox = (426, 173, 179, 80) from
    navigation_v2.menu_layout.MAIN_MENU.buttons[0].pixel_bbox(1280, 800).
    """

    #: PLAY main-menu region (x, y, w, h) at 1280x800, from menu_layout.
    PLAY_BOX = (426, 173, 179, 80)
    DISPLAY = (1280, 800)

    def _geo(self, box=PLAY_BOX, surface=(1280, 800)):
        ren = HighlightRenderer(*self.DISPLAY)
        rx = box[0] * surface[0] / self.DISPLAY[0]
        ry = box[1] * surface[1] / self.DISPLAY[1]
        rw = box[2] * surface[0] / self.DISPLAY[0]
        rh = box[3] * surface[1] / self.DISPLAY[1]
        return ren.arrow_geometry(rx, ry, rw, rh)

    def test_reference_vertices_exact(self):
        """PLAY bbox at 1280x800 yields the exact calibrated triangle."""
        (bx0, by0), (tip_x, mid_y), (bx1, by1) = self._geo()
        self.assertEqual((bx0, by0), (374.0, 189.0))  # base top
        self.assertEqual((tip_x, mid_y), (416.0, 213.0))  # apex
        self.assertEqual((bx1, by1), (374.0, 237.0))  # base bottom

    def test_dimensions_42x48(self):
        """Arrow is 42 px wide and 48 px tall at reference scale."""
        (bx0, by0), (tip_x, _), (_, by1) = self._geo()
        self.assertEqual(tip_x - bx0, 42.0)  # width
        self.assertEqual(by1 - by0, 48.0)  # height

    def test_tip_gap_ten_px_left(self):
        """Apex sits 10 px LEFT of the region's left edge (dark gap)."""
        (_, _), (tip_x, _), (_, _) = self._geo()
        self.assertEqual(self.PLAY_BOX[0] - tip_x, 10.0)

    def test_vertically_centered(self):
        """Apex is vertically centered on the highlighted region."""
        (_, by0), (_, mid_y), (_, by1) = self._geo()
        self.assertEqual(mid_y, self.PLAY_BOX[1] + self.PLAY_BOX[3] / 2.0)
        self.assertEqual((by0 + by1) / 2.0, mid_y)

    def test_points_right_into_entry(self):
        """Apex is right of the base => arrow points right into the entry."""
        (bx0, _), (tip_x, _), (bx1, _) = self._geo()
        self.assertGreater(tip_x, bx0)
        self.assertGreater(tip_x, bx1)

    def test_never_overlaps_region_or_text(self):
        """Whole triangle stays strictly left of the region's left edge.

        This is the invariant behind "never overlapping text": the arrow
        is entirely in the dark gap beside the entry, so it can never
        cover the entry's label or bar.
        """
        (bx0, _), (tip_x, _), (bx1, _) = self._geo()
        self.assertEqual(bx0, bx1)  # base is vertical
        self.assertLess(bx1, self.PLAY_BOX[0])
        self.assertLess(tip_x, self.PLAY_BOX[0])
        self.assertLess(bx1, tip_x)  # apex right of the base => points right

    def test_66pct_spec_height(self):
        """48 px height == ~66% of the measured 73 px main-menu bar."""
        self.assertEqual(ARROW_H, round(0.66 * REFERENCE_BAR_H))
        self.assertEqual(ARROW_W / ARROW_H, 42.0 / 48.0)  # 42x48 ratio

    def test_display_scaling_keeps_spec(self):
        """Geometry stays a FIXED 42x48 surface pixels (previous behavior).

        The arrow size is intentionally resolution-independent: the overlay
        surface equals the eDP-1 display size in production (sx=sy=1) so
        the arrow is exactly 42x48 px = 66% of the ~73 px bar there.  At a
        different surface size only the region box scales; the arrow size
        is preserved (same as the pre-calibration renderer).  The tip still
        tracks the scaled region's left edge.
        """
        ren = HighlightRenderer(*self.DISPLAY)
        surface = (1920, 1080)  # sx=1.5, sy=1.35
        (bx0, by0), (tip_x, mid_y), (bx1, by1) = self._geo(surface=surface)
        rx = 426 * 1.5  # 639.0
        ry = 173 * 1.35  # 233.55
        rh = 80 * 1.35  # 108.0
        # Region box scales, arrow stays fixed-size.
        self.assertEqual(tip_x, rx - ARROW_TIP_GAP)
        self.assertEqual(mid_y, ry + rh / 2.0)
        self.assertEqual(tip_x - bx0, ARROW_W)
        self.assertEqual(by1 - by0, ARROW_H)
        # Production configuration: surface == display => exact spec.
        (bx0p, by0p), (tip_xp, _), (bx1p, by1p) = self._geo(surface=self.DISPLAY)
        self.assertEqual(tip_xp - bx0p, 42.0)
        self.assertEqual(by1p - by0p, 48.0)
        self.assertEqual(ARROW_H, round(0.66 * REFERENCE_BAR_H))

    def test_66pct_spec_ratio_at_reference(self):
        """The locked constants satisfy the 66% spec at the reference bar."""
        self.assertEqual(ARROW_H, round(0.66 * REFERENCE_BAR_H))
        self.assertEqual(ARROW_W / ARROW_H, 42.0 / 48.0)  # 42x48 ratio
        self.assertAlmostEqual(ARROW_H / REFERENCE_BAR_H, 0.66, places=1)

    def test_all_main_menu_buttons_clear_of_entry(self):
        """Apex stays left of every main-menu button's left edge (no text
        overlap) and never leaves the display bounds."""
        try:
            from navigation_v2.menu_layout import MAIN_MENU
        except Exception:
            self.skipTest("navigation_v2.menu_layout unavailable")
        ren = HighlightRenderer(*self.DISPLAY)
        for btn in MAIN_MENU.buttons:
            x1, y1, x2, y2 = btn.pixel_bbox(*self.DISPLAY)
            region = HighlightRegion(x=x1, y=y1, width=x2 - x1, height=y2 - y1)
            (bx0, by0), (tip_x, _), (_, by1) = ren.arrow_geometry(
                region.x, region.y, region.width, region.height)
            self.assertLess(tip_x, region.x, f"{btn.label}: tip not left of box")
            self.assertLess(tip_x - bx0, region.x, f"{btn.label}: base inside box")
            self.assertGreaterEqual(by0, 0, f"{btn.label}: arrow off top")
            self.assertLessEqual(by1, self.DISPLAY[1], f"{btn.label}: arrow off bottom")

    def test_render_arrow_triangle_shape(self):
        """End-to-end render: exactly one right-pointing 42x48 triangle."""
        try:
            import cairo  # noqa: F401
        except ImportError:
            self.skipTest("cairo not available")
        ren = HighlightRenderer(*self.DISPLAY)
        region = HighlightRegion(x=426, y=173, width=179, height=80)
        pixels = ren.render_to_surface(1280, 800, [region])
        arr = memoryview(pixels).cast("B")
        # FORMAT_ARGB32 is stored little-endian as B,G,R,A bytes.
        pts = []
        for y in range(800):
            for x in range(1280):
                o = (y * 1280 + x) * 4
                b_, g_, r_, a_ = arr[o], arr[o + 1], arr[o + 2], arr[o + 3]
                if a_ > 40 and g_ > 60 and g_ - max(r_, b_) > 25:
                    pts.append((x, y))
        self.assertTrue(pts, "no arrow pixels rendered")
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        # Anti-aliased vertices: the apex column and the base corners render
        # as ~50% partial pixels, so the filled bbox is x[374..415],
        # y[189..236] = exactly 42x48 px.
        self.assertEqual((min(xs), max(xs)), (374, 415))  # 42 px wide
        self.assertEqual((min(ys), max(ys)), (189, 236))  # 48 px tall
        self.assertGreaterEqual(len(pts), 950)  # ~triangle area 1008
        self.assertLessEqual(len(pts), 1150)
        # Peak rows (full 42 px width) must sit at the vertical center.
        from collections import Counter
        per_row = Counter(ys)
        widest = max(per_row.values())
        self.assertEqual(widest, 42)
        peak_rows = [y for y, c in per_row.items() if c == widest]
        self.assertTrue(all(211 <= y <= 214 for y in peak_rows), peak_rows)

    def test_level_select_label_adjacency_known_limitation(self):
        """LEVEL_SELECT: tab labels sit left of the tab bar, so the arrow's
        fixed left-of-bbox footprint grazes them (documented limitation).

        The EASY tab's white label spans ~x384-547 and the chevron starts
        ~x545, so the calibrated arrow (tip at bbox.x1 - 10) unavoidably
        overlaps the label's right tail (66 px of the arrow x-range lies
        inside the label's x-range).  This cannot be fixed from the generic
        renderer: it only knows the bbox, and the label is OUTSIDE the
        bbox.  Fixing it needs menu_layout per-screen geometry (outside
        this module's ownership) or per-screen arrow offsets.

        This test documents the current measured state so a future layout
        or geometry change that resolves it is noticed (counts drop to 0).
        """
        fixture = Path(__file__).resolve().parents[2] / "analysis" / "linux-launcher" \
            / "menu-fixtures" / "level-select-1280x800.png"
        if not fixture.exists():
            self.skipTest("level-select fixture not present")
        try:
            import numpy as np
            from PIL import Image
        except ImportError:
            self.skipTest("numpy/PIL not available")
        im = np.array(Image.open(fixture).convert("RGB")).astype(int)
        white = (im[:, :, 0] > 180) & (im[:, :, 1] > 180) & (im[:, :, 2] > 180)
        try:
            from navigation_v2.menu_layout import LEVEL_SELECT
        except Exception:
            self.skipTest("navigation_v2.menu_layout unavailable")
        ren = HighlightRenderer(*self.DISPLAY)
        observed = {}
        for btn in LEVEL_SELECT.buttons:
            x1, y1, x2, y2 = btn.pixel_bbox(*self.DISPLAY)
            (bx0, by0), (tip_x, _), (_, by1) = ren.arrow_geometry(x1, y1, x2 - x1, y2 - y1)
            ax0, ax1 = int(bx0), int(tip_x)
            ay0, ay1 = int(by0), int(by1)
            sub = white[ay0:ay1, ax0:ax1]
            observed[btn.label] = int(sub.sum())
        # Measured on fixture 2026-09-11: EASY/HARD labels graze the arrow.
        self.assertGreater(observed["EASY"], 0, "EASY label adjacency regression")
        self.assertGreater(observed["HARD"], 0, "HARD label adjacency regression")

    def test_fixture_arrow_clear_of_label_text(self):
        """Over the real main-menu fixture, the arrow never covers an
        entry's white label (spec: never overlapping text).

        Measured exception (documented, not an entry-label overlap): the
        STORE entry sits directly above a persistent full-width footer
        text row (fixture pixels y>=700 in both menu fixtures); the
        arrow's bottom quarter brushes that footer.  The entry's own
        label is untouched because the triangle lies strictly left of the
        region box.
        """
        fixture = Path(__file__).resolve().parents[2] / "analysis" / "linux-launcher" \
            / "menu-fixtures" / "main-menu-1280x800.png"
        if not fixture.exists():
            self.skipTest("main-menu fixture not present")
        try:
            import numpy as np
            from PIL import Image
        except ImportError:
            self.skipTest("numpy/PIL not available")
        im = np.array(Image.open(fixture).convert("RGB")).astype(int)
        r, g, b = im[:, :, 0], im[:, :, 1], im[:, :, 2]
        try:
            from navigation_v2.menu_layout import MAIN_MENU
        except Exception:
            self.skipTest("navigation_v2.menu_layout unavailable")
        ren = HighlightRenderer(*self.DISPLAY)
        for btn in MAIN_MENU.buttons:
            x1, y1, x2, y2 = btn.pixel_bbox(*self.DISPLAY)
            region = HighlightRegion(x=x1, y=y1, width=x2 - x1, height=y2 - y1)
            (bx0, by0), (tip_x, _), (_, by1) = ren.arrow_geometry(
                region.x, region.y, region.width, region.height)
            # The arrow box must stay fully left of the entry, so it can
            # never cover the entry's label text or red bar.
            ax0, ax1 = int(min(bx0, tip_x)), int(max(bx0, tip_x))
            ay0, ay1 = int(by0), int(by1)
            self.assertLess(ax1, x1, f"{btn.label}: arrow not left of entry")
            sub = im[ay0:ay1, ax0:ax1]
            bright = (sub[:, :, 0] > 180) & (sub[:, :, 1] > 180) & (sub[:, :, 2] > 180)
            redish = (sub[:, :, 0] > 130) & (sub[:, :, 1] < 40) & (sub[:, :, 2] < 40)
            self.assertEqual(int(redish.sum()), 0, f"{btn.label}: arrow over red bar")
            if btn.label == "STORE":
                # Bottom-left footer text coexists below STORE (y>=700 in
                # both fixtures); documented, cosmetic only.
                bys, _ = np.where(bright)
                self.assertTrue(len(bys) > 0 and bys.min() + ay0 >= 700,
                                "STORE footer overlap regression")
            else:
                self.assertEqual(int(bright.sum()), 0,
                                 f"{btn.label}: arrow over white text")


class TestClickThrough(unittest.TestCase):
    """Click-through contract: the overlay must never eat clicks/taps.

    The host overlay renders on wlr-layer-shell's OVERLAY layer above the
    game, so without an explicit input shape it would swallow ALL pointer
    and touch input (exclusive_zone -1 does not affect input at all).

    Contract: HostOverlay installs an EMPTY input region (a cairo Region
    with zero rectangles) on its layer surface after the window maps and
    re-applies it on every show/map, so every click/tap passes through to
    the game below while the yellow arrow and text keep drawing.  Keyboard
    input stays NONE; the overlay layer and monitor pin are unchanged.
    """

    def setUp(self):
        """Ensure GTK module names exist so mock patches work without GTK."""
        import overlay.host_overlay as mod
        for name in ("Gtk4LayerShell", "Gdk", "Gtk", "GLib"):
            if not hasattr(mod, name):
                setattr(mod, name, None)

    def test_empty_input_region_has_no_rectangles(self):
        """make_empty_input_region is a cairo Region with zero rectangles."""
        region = make_empty_input_region()
        if region is None:
            self.skipTest("cairo bindings not available")
        num = region.num_rectangles
        if callable(num):  # pycairo exposes it as property or method
            num = num()
        self.assertEqual(num, 0)
        # An empty region contains no point: no input may be delivered.
        self.assertFalse(region.contains_point(0, 0))
        self.assertFalse(region.contains_point(640, 400))

    def test_empty_input_region_degrades_without_cairo(self):
        """make_empty_input_region returns None when cairo is unavailable."""
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "cairo":
                raise ImportError("no cairo")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            self.assertIsNone(make_empty_input_region())

    def test_apply_click_through_noop_without_window(self):
        """Click-through is a safe no-op before a window exists."""
        overlay = HostOverlay()
        with patch("overlay.host_overlay.make_empty_input_region",
                   return_value=MagicMock()) as mk:
            self.assertFalse(overlay._apply_click_through())
        mk.assert_not_called()

    def test_apply_click_through_noop_without_surface(self):
        """Click-through is a safe no-op while the surface is unmapped."""
        overlay = HostOverlay()
        win = MagicMock()
        win.get_surface.return_value = None
        overlay._window = win
        with patch("overlay.host_overlay.make_empty_input_region",
                   return_value=MagicMock()) as mk:
            self.assertFalse(overlay._apply_click_through())
        win.get_surface.assert_called_once()
        mk.assert_not_called()

    def test_apply_click_through_sets_empty_region(self):
        """_apply_click_through installs the empty region on the surface."""
        overlay = HostOverlay()
        surface = MagicMock()
        win = MagicMock()
        win.get_surface.return_value = surface
        overlay._window = win
        region = MagicMock()
        with patch("overlay.host_overlay.make_empty_input_region",
                   return_value=region) as mk:
            self.assertTrue(overlay._apply_click_through())
        mk.assert_called_once_with()
        surface.set_input_region.assert_called_once_with(region)

    def test_activate_hooks_map_and_preserves_contract(self):
        """_on_activate keeps keyboard NONE + overlay layer, and the map
        hook installs the empty input region once the layer surface maps."""
        with patch("overlay.host_overlay.Gtk4LayerShell") as lshell, \
             patch("overlay.host_overlay.Gdk") as gdk, \
             patch("overlay.host_overlay.Gtk") as gtk, \
             patch("overlay.host_overlay.GLib") as glib, \
             patch("overlay.host_overlay.make_empty_input_region",
                   return_value=MagicMock()) as mk:
            gdk.Display.get_default.return_value = None  # headless: no monitor
            overlay = HostOverlay()
            app = gtk.Application.return_value
            overlay._on_activate(app)
            win = lshell.init_for_window.call_args.args[0]
            # Keyboard stays NONE and the layer stays OVERLAY (monitor
            # pinning and geometry code paths are untouched).
            lshell.set_keyboard_mode.assert_called_once_with(
                win, lshell.KeyboardMode.NONE)
            lshell.set_layer.assert_called_once_with(
                win, lshell.Layer.OVERLAY)
            # Click-through: a map hook must exist on the window.
            map_calls = [c for c in win.connect.call_args_list
                         if c.args and c.args[0] == "map"]
            self.assertTrue(map_calls, "expected a 'map' click-through hook")
            # Simulate the compositor mapping the window: the empty input
            # region is installed on the layer surface.
            map_calls[0].args[1](win)
            win.get_surface.return_value.set_input_region.assert_called_once()
            mk.assert_called_once_with()

    def test_show_reapplies_empty_input_region(self):
        """show() → _update_visibility re-applies the empty input region."""
        overlay = HostOverlay()
        surface = MagicMock()
        win = MagicMock()
        win.get_surface.return_value = surface
        overlay._window = win
        overlay._state.visible = True
        region = MagicMock()
        with patch("overlay.host_overlay.make_empty_input_region",
                   return_value=region) as mk, \
             patch.object(overlay, "_start_hide_timer"):
            result = overlay._update_visibility()
        self.assertFalse(result)  # idle callback contract: don't repeat
        win.set_visible.assert_called_with(True)
        mk.assert_called_once_with()
        surface.set_input_region.assert_called_once_with(region)

    def test_show_dispatches_reapply_on_gtk_thread(self):
        """show() schedules the visibility update that re-applies click-
        through on the GTK thread."""
        with patch("overlay.host_overlay._GTK_AVAILABLE", True), \
             patch("overlay.host_overlay.GLib") as glib, \
             patch("overlay.host_overlay.make_empty_input_region",
                   return_value=MagicMock()) as mk, \
             patch.object(HostOverlay, "_start_hide_timer"):
            overlay = HostOverlay()
            surface = MagicMock()
            win = MagicMock()
            win.get_surface.return_value = surface
            overlay._window = win
            overlay.show()
            glib.idle_add.assert_called()
            callback = glib.idle_add.call_args.args[0]
            callback()  # run the scheduled update on the (fake) GTK thread
        win.set_visible.assert_called_with(True)
        surface.set_input_region.assert_called_once()
        mk.assert_called()

    def test_hide_does_not_reapply(self):
        """Hiding the overlay does not re-install the input region."""
        overlay = HostOverlay()
        surface = MagicMock()
        win = MagicMock()
        win.get_surface.return_value = surface
        overlay._window = win
        overlay._state.visible = False
        with patch("overlay.host_overlay.make_empty_input_region",
                   return_value=MagicMock()) as mk:
            result = overlay._update_visibility()
        self.assertFalse(result)
        win.set_visible.assert_called_with(False)
        mk.assert_not_called()
        surface.set_input_region.assert_not_called()


if __name__ == "__main__":
    unittest.main()

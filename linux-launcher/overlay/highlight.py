"""Highlight rendering for HOST OVERLAY v1.

Provides Cairo-based rendering of highlight rectangles and UI elements.
This module is offline-only and never interacts with the guest system.

Arrow calibration (the "66% spec")
-----------------------------------
The menu cursor is drawn as a pointer arrow, not a box: a filled right
pointing triangle whose APEX sits in the dark gap immediately LEFT of the
highlighted entry's left edge, pointing right into the entry.  It never
overlaps the entry's label text because the entire triangle lies strictly
left of the region's left edge.

Locked geometry (display-space, reference 1280x800):

  * ARROW_W = 42 px wide, ARROW_H = 48 px tall.
  * 48 px tall is ~66% of the measured main-menu button bar height
    (~73 px @1280x800; see analysis/linux-launcher/tap-hardening/
    MEASUREMENT.md) -- hence "66% spec".  The overlay surface always
    equals the target eDP-1 display size (sx=sy=1) in production, so the
    arrow is exactly 42x48 px on-screen and the 66% ratio holds.
  * ARROW_TIP_GAP = 10 px: the apex is drawn 10 px left of the region's
    left edge (x1 - 10), vertically centered on the region.

The pure geometry is exposed as HighlightRenderer.arrow_geometry() so the
calibration is unit-testable offline (no cairo/GTK required); the draw
path uses only those vertices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Arrow calibration constants (locked; see module docstring "66% spec").
# ---------------------------------------------------------------------------
ARROW_W = 42          # arrow width in px (apex -> base) at reference scale
ARROW_H = 48          # arrow height in px (2 * half-height) at reference scale
ARROW_TIP_GAP = 10    # px the apex sits LEFT of the region's left edge
# Measured main-menu button bar height @1280x800 (tap-hardening MEASUREMENT.md
# reports ~74 px; fixture red-run measurement gives 72-73 px).  ARROW_H is
# ~66% of this value, which is the origin of the "66% spec".
REFERENCE_BAR_H = 73


@dataclass
class HighlightRegion:
    """A rectangular region to highlight on the overlay."""
    x: int
    y: int
    width: int
    height: int
    label: str = ""
    color: tuple[float, float, float, float] = (0.2, 1.0, 0.2, 0.85)

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        """Return (x1, y1, x2, y2) bounding box."""
        return (self.x, self.y, self.x + self.width, self.y + self.height)

    @classmethod
    def from_bbox(cls, x1: int, y1: int, x2: int, y2: int,
                  label: str = "", color: tuple[float, float, float, float] = (0.2, 1.0, 0.2, 0.85)) -> HighlightRegion:
        """Create from bounding box coordinates."""
        return cls(x=x1, y=y1, width=x2 - x1, height=y2 - y1, label=label, color=color)


class HighlightRenderer:
    """Renders highlight regions using Cairo.

    This is a synchronous, testable rendering path that doesn't require
    GTK or Wayland. Used for offline verification of highlight geometry.
    """

    def __init__(self, display_width: int = 1280, display_height: int = 800):
        self._display_w = display_width
        self._display_h = display_height

    def update_display_size(self, width: int, height: int) -> None:
        """Update the display dimensions for coordinate mapping."""
        self._display_w = width
        self._display_h = height

    def render_to_surface(self, width: int, height: int,
                          regions: list[HighlightRegion]) -> bytes:
        """Render highlight regions to a Cairo image surface.

        Args:
            width: Output surface width in pixels
            height: Output surface height in pixels
            regions: List of highlight regions to render

        Returns:
            Raw RGBA pixel data (width * height * 4 bytes)
        """
        try:
            import cairo
        except ImportError:
            raise RuntimeError("cairo Python bindings required for render_to_surface")

        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)

        # Clear to transparent
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        for region in regions:
            self._render_region(cr, region, width, height)

        surface.flush()
        return surface.get_data().tobytes()

    def arrow_geometry(self, rx: float, ry: float,
                       rw: float, rh: float) -> tuple[tuple[float, float],
                                                      tuple[float, float],
                                                      tuple[float, float]]:
        """Return the calibrated pointer-arrow triangle vertices.

        Args:
            rx, ry, rw, rh: the highlighted region box in SURFACE
                coordinates (already scaled from display space by sx/sy).

        Returns:
            Three ``(x, y)`` vertices ``(base_top, tip, base_bottom)`` of the
            filled right-pointing triangle.  The apex (tip) is at
            ``(rx - ARROW_TIP_GAP, ry + rh/2)``; the vertical base is
            ``ARROW_W`` px to its left, spanning ``ARROW_H`` px vertically.

        Spec (see module docstring): 42 wide x 48 tall per the 66% spec,
        tip 10 px left of the entry's left edge in the dark gap, pointing
        right into the entry, never overlapping the entry's text.
        """
        tip_x = rx - ARROW_TIP_GAP
        mid_y = ry + rh / 2.0
        return (
            (tip_x - ARROW_W, mid_y - ARROW_H / 2.0),
            (tip_x, mid_y),
            (tip_x - ARROW_W, mid_y + ARROW_H / 2.0),
        )

    def _render_region(self, cr: "cairo.Context", region: HighlightRegion,
                       surface_width: int, surface_height: int) -> None:
        """Render a single highlight region."""
        # Scale from display coordinates to surface dimensions
        sx = surface_width / self._display_w if self._display_w > 0 else 1
        sy = surface_height / self._display_h if self._display_h > 0 else 1

        rx = region.x * sx
        ry = region.y * sy
        rw = region.width * sx
        rh = region.height * sy

        r, g, b, a = region.color

        # Calibrated pointer arrow (see module docstring): apex in the dark
        # gap immediately LEFT of the entry's left edge, pointing right into
        # the entry, 42 wide x 48 tall per the 66% spec.  The triangle lies
        # entirely left of the region, so it never overlaps the label text.
        (bx0, by0), (tip_x, mid_y), (bx1, by1) = self.arrow_geometry(rx, ry, rw, rh)
        cr.set_source_rgba(r, g, b, min(1.0, a * 1.1))
        cr.move_to(bx0, by0)
        cr.line_to(tip_x, mid_y)
        cr.line_to(bx1, by1)
        cr.close_path()
        cr.fill()

    def render_text(self, cr: "cairo.Context", text: str,
                    x: float, y: float, font_size: float = 14,
                    color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)) -> None:
        """Render text at the given position.

        Args:
            cr: Cairo context
            text: Text to render
            x: X position
            y: Y position
            font_size: Font size in pixels
            color: RGBA color tuple
        """
        try:
            cr.select_font_face("Sans", 0, 0)  # CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL
            cr.set_font_size(font_size)
            cr.set_source_rgba(*color)
            cr.move_to(x, y)
            cr.show_text(text)
        except Exception:
            # Text rendering is best-effort; don't fail the overlay
            pass

    def render_panel(self, cr: "cairo.Context",
                     x: float, y: float, width: float, height: float,
                     bg_color: tuple[float, float, float, float],
                     border_color: tuple[float, float, float, float],
                     corner_radius: float = 8.0,
                     border_width: float = 2.0) -> None:
        """Render a rounded rectangle panel.

        Args:
            cr: Cairo context
            x, y: Top-left corner
            width, height: Panel dimensions
            bg_color: Background RGBA color
            border_color: Border RGBA color
            corner_radius: Radius for rounded corners
            border_width: Width of the border stroke
        """
        # Background
        cr.set_source_rgba(*bg_color)
        self._rounded_rect(cr, x, y, width, height, corner_radius)
        cr.fill_preserve()

        # Border
        cr.set_source_rgba(*border_color)
        cr.set_line_width(border_width)
        cr.stroke()

    def _rounded_rect(self, cr: "cairo.Context",
                      x: float, y: float, width: float, height: float,
                      radius: float) -> None:
        """Draw a rounded rectangle path."""
        radius = min(radius, width / 2, height / 2)
        cr.new_sub_path()
        cr.arc(x + width - radius, y + radius, radius, -1.5708, 0)  # -PI/2
        cr.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)  # PI/2
        cr.arc(x + radius, y + height - radius, radius, 1.5708, 3.14159)  # PI
        cr.arc(x + radius, y + radius, radius, 3.14159, 4.71239)  # 3PI/2
        cr.close_path()

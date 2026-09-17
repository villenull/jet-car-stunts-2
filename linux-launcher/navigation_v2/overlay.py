"""GTK4 Layer Shell overlay for visible menu highlight on Steam Deck eDP-1.

Renders a transparent click-through overlay showing the currently selected
menu button with a bright highlight rectangle. Only appears on the internal
Deck display, never on external monitors. Hides during gameplay/unknown.

Requirements: GTK4, Gtk4LayerShell (both available on Omarchy).
The overlay:
  - Uses wlr-layer-shell overlay layer (above game, below nothing)
  - Has no keyboard interactivity (click-through, no stolen focus)
  - Targets eDP-1 monitor only via GdkMonitor matching
  - Draws with Cairo on a transparent surface
  - Auto-hides when no active menu highlight
  - Cleans up on process exit
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .menu_layout import ButtonRegion

_GTK_AVAILABLE = False
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell
    _GTK_AVAILABLE = True
except (ValueError, ImportError):
    pass


class OverlayHighlight:
    """Manages the GTK4 layer-shell highlight overlay.

    Thread-safe: highlight updates can come from any thread;
    actual GTK operations are dispatched to the GLib main loop.
    """

    def __init__(self, target_monitor: str = "eDP-1",
                 display_width: int = 1280, display_height: int = 800):
        self._target_monitor_name = target_monitor
        self._display_w = display_width
        self._display_h = display_height
        self._highlight_bbox: tuple[int, int, int, int] | None = None
        self._highlight_label: str = ""
        self._visible = False
        self._window: Gtk.Window | None = None
        self._drawing_area: Gtk.DrawingArea | None = None
        self._app: Gtk.Application | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return _GTK_AVAILABLE

    def start(self):
        """Start the overlay in a background thread."""
        if not _GTK_AVAILABLE:
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_gtk, daemon=True,
                                        name="nav-overlay")
        self._thread.start()

    def stop(self):
        """Stop the overlay and clean up."""
        self._running = False
        if _GTK_AVAILABLE and self._app:
            GLib.idle_add(self._shutdown_gtk)

    def _shutdown_gtk(self):
        if self._window:
            self._window.close()
        if self._app:
            self._app.quit()
        return False

    def _run_gtk(self):
        self._app = Gtk.Application(application_id="com.jcs2.nav.overlay")
        self._app.connect("activate", self._on_activate)
        self._app.run([])

    def _find_monitor(self) -> Gdk.Monitor | None:
        """Find the eDP-1 monitor by connector name."""
        display = Gdk.Display.get_default()
        if not display:
            return None
        monitors = display.get_monitors()
        for i in range(monitors.get_n_items()):
            mon = monitors.get_item(i)
            connector = mon.get_connector()
            if connector == self._target_monitor_name:
                return mon
        return None

    def _on_activate(self, app: Gtk.Application):
        self._window = Gtk.Window(application=app)
        self._window.set_decorated(False)
        self._window.set_default_size(self._display_w, self._display_h)

        Gtk4LayerShell.init_for_window(self._window)
        Gtk4LayerShell.set_layer(self._window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(
            self._window, Gtk4LayerShell.KeyboardMode.NONE
        )
        # Anchor to all edges to fill the screen
        for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM,
                     Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT):
            Gtk4LayerShell.set_anchor(self._window, edge, True)
        Gtk4LayerShell.set_exclusive_zone(self._window, -1)
        Gtk4LayerShell.set_namespace(self._window, "jcs2-nav-highlight")

        monitor = self._find_monitor()
        if monitor:
            Gtk4LayerShell.set_monitor(self._window, monitor)
            # Detect actual monitor resolution for coordinate scaling
            geo = monitor.get_geometry()
            if geo.width > 0 and geo.height > 0:
                with self._lock:
                    self._display_w = geo.width
                    self._display_h = geo.height

        # Transparent CSS
        css = Gtk.CssProvider()
        css.load_from_string(
            "window { background: transparent; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._drawing_area = Gtk.DrawingArea()
        self._drawing_area.set_draw_func(self._draw)
        self._window.set_child(self._drawing_area)

        # Start hidden
        self._window.set_visible(False)

    def _draw(self, area: Gtk.DrawingArea, cr, width: int, height: int):
        """Cairo draw function for the highlight rectangle."""
        cr.set_operator(2)  # CAIRO_OPERATOR_CLEAR
        cr.paint()
        cr.set_operator(0)  # CAIRO_OPERATOR_OVER

        with self._lock:
            bbox = self._highlight_bbox
            label = self._highlight_label

        if bbox is None:
            return

        x1, y1, x2, y2 = bbox

        # Scale from game resolution to overlay dimensions
        sx = width / self._display_w if self._display_w > 0 else 1
        sy = height / self._display_h if self._display_h > 0 else 1
        rx1 = x1 * sx
        ry1 = y1 * sy
        rw = (x2 - x1) * sx
        rh = (y2 - y1) * sy

        # Bright green highlight border (matches game's green HUD theme)
        cr.set_source_rgba(0.2, 1.0, 0.2, 0.85)
        cr.set_line_width(3)
        cr.rectangle(rx1, ry1, rw, rh)
        cr.stroke()

        # Semi-transparent fill
        cr.set_source_rgba(0.2, 1.0, 0.2, 0.15)
        cr.rectangle(rx1, ry1, rw, rh)
        cr.fill()

        # Small indicator arrows on left side
        mid_y = ry1 + rh / 2
        cr.set_source_rgba(0.2, 1.0, 0.2, 0.9)
        cr.move_to(rx1 - 15, mid_y - 8)
        cr.line_to(rx1 - 5, mid_y)
        cr.line_to(rx1 - 15, mid_y + 8)
        cr.close_path()
        cr.fill()

    def set_highlight(self, bbox: tuple[int, int, int, int] | None,
                      label: str = ""):
        """Update the highlight rectangle (thread-safe).

        bbox is in game pixel coordinates (e.g. 1280x800).
        Pass None to hide.
        """
        with self._lock:
            self._highlight_bbox = bbox
            self._highlight_label = label

        if _GTK_AVAILABLE:
            GLib.idle_add(self._update_visibility)

    def _update_visibility(self):
        if not self._window:
            return False
        with self._lock:
            should_show = self._highlight_bbox is not None
        if should_show != self._visible:
            self._visible = should_show
            self._window.set_visible(should_show)
        if self._drawing_area:
            self._drawing_area.queue_draw()
        return False

    def update_display_size(self, w: int, h: int):
        """Update the game display resolution for coordinate mapping."""
        self._display_w = w
        self._display_h = h

    def render_to_surface(self, width: int, height: int) -> bytes:
        """Render the current highlight to a Cairo image surface and return RGBA pixels.

        This is a synchronous, testable rendering path that doesn't require
        GTK or Wayland. Used for offline verification of highlight geometry.
        Returns raw RGBA pixel data (width * height * 4 bytes).
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

        with self._lock:
            bbox = self._highlight_bbox
            label = self._highlight_label

        if bbox is None:
            return surface.get_data().tobytes()

        x1, y1, x2, y2 = bbox

        # Scale from game resolution to surface dimensions
        sx = width / self._display_w if self._display_w > 0 else 1
        sy = height / self._display_h if self._display_h > 0 else 1
        rx1 = x1 * sx
        ry1 = y1 * sy
        rw = (x2 - x1) * sx
        rh = (y2 - y1) * sy

        # Bright green highlight border
        cr.set_source_rgba(0.2, 1.0, 0.2, 0.85)
        cr.set_line_width(3)
        cr.rectangle(rx1, ry1, rw, rh)
        cr.stroke()

        # Semi-transparent fill
        cr.set_source_rgba(0.2, 1.0, 0.2, 0.15)
        cr.rectangle(rx1, ry1, rw, rh)
        cr.fill()

        # Small indicator arrows on left side
        mid_y = ry1 + rh / 2
        cr.set_source_rgba(0.2, 1.0, 0.2, 0.9)
        cr.move_to(rx1 - 15, mid_y - 8)
        cr.line_to(rx1 - 5, mid_y)
        cr.line_to(rx1 - 15, mid_y + 8)
        cr.close_path()
        cr.fill()

        surface.flush()
        return surface.get_data().tobytes()


class DummyOverlay:
    """No-op overlay for headless/test environments."""

    def __init__(self):
        self.last_bbox: tuple[int, int, int, int] | None = None
        self.last_label: str = ""
        self.visible = False

    @property
    def available(self) -> bool:
        return False

    def start(self):
        pass

    def stop(self):
        pass

    def set_highlight(self, bbox: tuple[int, int, int, int] | None,
                      label: str = ""):
        self.last_bbox = bbox
        self.last_label = label
        self.visible = bbox is not None

    def update_display_size(self, w: int, h: int):
        pass

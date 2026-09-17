"""HOST OVERLAY v1 — GTK4 Layer Shell overlay for game-running state.

Renders a transparent click-through overlay on the Steam Deck's internal
display (eDP-1) showing game status, performance metrics, and control hints.

KEY CONSTRAINT: This overlay is OFFLINE ONLY and never touches the guest.
- No ADB communication
- No interaction with the Android system
- Purely host-side rendering
- Game state is provided via in-memory callbacks, not guest queries

Requirements: GTK4, Gtk4LayerShell (both available on Omarchy).

The overlay:
  - Uses wlr-layer-shell overlay layer (above game, below nothing)
  - Has no keyboard interactivity (click-through, no stolen focus)
  - Is input-transparent: after mapping, an EMPTY input region is installed
    on the layer surface so all pointer/touch clicks pass through to the
    game below while the overlay still draws (exclusive_zone -1 alone would
    NOT stop the surface from swallowing clicks)
  - Targets eDP-1 monitor only via GdkMonitor matching
  - Draws with Cairo on a transparent surface
  - Auto-hides when no active content
  - Cleans up on process exit
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .highlight import HighlightRegion, HighlightRenderer
from .settings_model import OverlayPosition, OverlaySettings, OverlayTheme

if TYPE_CHECKING:
    pass

_GTK_AVAILABLE = False
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell
    _GTK_AVAILABLE = True
except (ValueError, ImportError):
    pass


@dataclass
class OverlayState:
    """Current state of the overlay content."""
    fps: float = 0.0
    game_status: str = ""
    controls_hint: str = ""
    highlight_regions: list[HighlightRegion] | None = None
    visible: bool = False
    last_update: float = 0.0


def make_empty_input_region():
    """Create an EMPTY cairo Region: the overlay's click-through input shape.

    A region containing zero rectangles tells the Wayland compositor to
    deliver NO pointer/touch events to the overlay surface, so every
    click/tap falls through to the game window below while the overlay
    keeps drawing (yellow navigation arrow, FPS, hint text).  Keyboard
    input is already blocked via KeyboardMode.NONE.

    Returns a pycairo ``cairo.Region`` with no rectangles, or None when
    cairo bindings are unavailable (GTK-less/test environments) so
    callers degrade gracefully.
    """
    try:
        import cairo
    except ImportError:
        return None
    try:
        return cairo.Region()
    except Exception:
        return None


class HostOverlay:
    """Manages the GTK4 layer-shell host overlay.

    Thread-safe: state updates can come from any thread;
    actual GTK operations are dispatched to the GLib main loop.

    This overlay is OFFLINE ONLY — it never communicates with the guest
    Android system or uses ADB. All game state is provided via callbacks
    or in-memory state updates.
    """

    def __init__(self, settings: OverlaySettings | None = None,
                 target_monitor: str = "eDP-1",
                 display_width: int = 1280, display_height: int = 800):
        self._settings = settings or OverlaySettings()
        self._target_monitor_name = target_monitor
        self._display_w = display_width
        self._display_h = display_height
        self._state = OverlayState()
        self._window: Gtk.Window | None = None
        self._drawing_area: Gtk.DrawingArea | None = None
        self._app: Gtk.Application | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._renderer = HighlightRenderer(display_width, display_height)
        self._hide_timer_id: int | None = None

    @property
    def available(self) -> bool:
        """True if GTK4 and Gtk4LayerShell are available."""
        return _GTK_AVAILABLE

    @property
    def settings(self) -> OverlaySettings:
        return self._settings

    @property
    def state(self) -> OverlayState:
        with self._lock:
            return OverlayState(
                fps=self._state.fps,
                game_status=self._state.game_status,
                controls_hint=self._state.controls_hint,
                highlight_regions=self._state.highlight_regions,
                visible=self._state.visible,
                last_update=self._state.last_update,
            )

    def start(self) -> None:
        """Start the overlay in a background thread."""
        if not _GTK_AVAILABLE:
            return
        if self._running:
            return
        if not self._settings.enabled:
            return
        self._running = True
        self._state.visible = True
        self._thread = threading.Thread(target=self._run_gtk, daemon=True,
                                        name="host-overlay")
        self._thread.start()

    def stop(self) -> None:
        """Stop the overlay and clean up."""
        self._running = False
        self._state.visible = False
        if _GTK_AVAILABLE and self._app:
            GLib.idle_add(self._shutdown_gtk)

    def _shutdown_gtk(self) -> bool:
        if self._hide_timer_id is not None:
            try:
                GLib.source_remove(self._hide_timer_id)
            except Exception:
                pass
            self._hide_timer_id = None
        if self._window:
            self._window.close()
        if self._app:
            self._app.quit()
        return False

    def _run_gtk(self) -> None:
        self._app = Gtk.Application(application_id="com.jcs2.host.overlay")
        self._app.connect("activate", self._on_activate)
        self._app.run([])

    def _find_monitor(self) -> Gdk.Monitor | None:
        """Find the target monitor by connector name."""
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

    def _apply_click_through(self) -> bool:
        """Set an EMPTY input region on the layer surface (click-through).

        Must be called on the GTK main thread once the window is mapped.
        An empty (zero-rectangle) region makes the compositor deliver no
        pointer/touch events to this overlay surface, so all clicks and
        taps pass through to the game below while the overlay keeps
        drawing the yellow arrow and text.  Keyboard input stays blocked
        via KeyboardMode.NONE.

        Returns True when the empty region was applied, False when there
        is no window/surface yet or cairo is unavailable (safe no-op;
        the map hook re-applies it as soon as the surface exists).
        """
        surface = None
        if self._window is not None:
            surface = self._window.get_surface()
        if surface is None:
            return False
        region = make_empty_input_region()
        if region is None:
            return False
        surface.set_input_region(region)
        return True

    def _on_map_click_through(self, _widget) -> None:
        """Re-apply the empty input region on every window map/show."""
        self._apply_click_through()

    def _on_activate(self, app: Gtk.Application) -> None:
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
        Gtk4LayerShell.set_namespace(self._window, "jcs2-host-overlay")

        monitor = self._find_monitor()
        if monitor:
            Gtk4LayerShell.set_monitor(self._window, monitor)
            # Detect actual monitor resolution for coordinate scaling
            geo = monitor.get_geometry()
            if geo.width > 0 and geo.height > 0:
                with self._lock:
                    self._display_w = geo.width
                    self._display_h = geo.height
                self._renderer.update_display_size(geo.width, geo.height)

        # Transparent CSS
        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._drawing_area = Gtk.DrawingArea()
        self._drawing_area.set_draw_func(self._draw)
        self._window.set_child(self._drawing_area)

        # Click-through contract: this overlay must NEVER eat clicks/taps.
        # exclusive_zone -1 only removes layout influence — without an empty
        # input region the layer surface would still swallow ALL pointer and
        # touch input above the game.  Hook the window's map signal so the
        # empty input region is applied after every map (initial show and
        # every re-show), letting clicks pass through to the game below
        # while the yellow arrow and text keep rendering.
        self._window.connect("map", self._on_map_click_through)

        # Apply initial visibility based on settings
        self._window.set_visible(self._settings.enabled and self._state.visible)

    def _draw(self, area: Gtk.DrawingArea, cr: object, width: int, height: int) -> None:
        """Cairo draw function for the overlay."""
        try:
            import cairo
            # Clear to transparent
            cr.set_operator(cairo.OPERATOR_CLEAR)  # type: ignore
            cr.paint()  # type: ignore
            cr.set_operator(cairo.OPERATOR_OVER)  # type: ignore
        except (ImportError, AttributeError):
            return

        with self._lock:
            state = self._state
            settings = self._settings
            display_w = self._display_w
            display_h = self._display_h

        if not state.visible:
            return

        colors = settings.theme_colors
        sx = width / display_w if display_w > 0 else 1
        sy = height / display_h if display_h > 0 else 1

        # Calculate position based on settings
        panel_x, panel_y = self._calculate_position(
            width, height, settings.position, settings.margin_x, settings.margin_y
        )

        # Draw FPS counter
        if settings.show_fps and state.fps > 0:
            fps_text = f"FPS: {state.fps:.0f}"
            try:
                cr.select_font_face("Sans", 0, 0)  # type: ignore
                cr.set_font_size(16)  # type: ignore
                cr.set_source_rgba(*colors["accent"])  # type: ignore
                cr.move_to(panel_x, panel_y + 20)  # type: ignore
                cr.show_text(fps_text)  # type: ignore
            except Exception:
                pass

        # Draw game status
        if settings.show_game_status and state.game_status:
            try:
                cr.select_font_face("Sans", 0, 0)  # type: ignore
                cr.set_font_size(14)  # type: ignore
                cr.set_source_rgba(*colors["text"])  # type: ignore
                cr.move_to(panel_x, panel_y + 45)  # type: ignore
                cr.show_text(state.game_status)  # type: ignore
            except Exception:
                pass

        # Draw controls hint
        if settings.show_controls_hint and state.controls_hint:
            try:
                cr.select_font_face("Sans", 0, 0)  # type: ignore
                cr.set_font_size(12)  # type: ignore
                cr.set_source_rgba(*colors["text"])  # type: ignore
                cr.move_to(panel_x, panel_y + 65)  # type: ignore
                cr.show_text(state.controls_hint)  # type: ignore
            except Exception:
                pass

        # Draw highlight regions
        if state.highlight_regions:
            for region in state.highlight_regions:
                self._renderer._render_region(cr, region, width, height)

    def _calculate_position(self, surface_width: int, surface_height: int,
                            position: OverlayPosition,
                            margin_x: int, margin_y: int) -> tuple[float, float]:
        """Calculate panel position based on settings."""
        panel_width = 200  # Estimated panel width
        panel_height = 80  # Estimated panel height

        if position == OverlayPosition.TOP_LEFT:
            return (margin_x, margin_y)
        elif position == OverlayPosition.TOP_RIGHT:
            return (surface_width - panel_width - margin_x, margin_y)
        elif position == OverlayPosition.BOTTOM_LEFT:
            return (margin_x, surface_height - panel_height - margin_y)
        elif position == OverlayPosition.BOTTOM_RIGHT:
            return (surface_width - panel_width - margin_x,
                    surface_height - panel_height - margin_y)
        elif position == OverlayPosition.TOP_CENTER:
            return ((surface_width - panel_width) / 2, margin_y)
        elif position == OverlayPosition.BOTTOM_CENTER:
            return ((surface_width - panel_width) / 2,
                    surface_height - panel_height - margin_y)
        return (margin_x, margin_y)

    #: Screens in which the overlay stays hidden (gameplay + unknown).
    _HIDDEN_SCREENS = frozenset({"gameplay", "unknown", "other_menu"})

    def update_from_cursor_file(self, path) -> bool:
        """Apply a runner-published cursor.json file to the overlay.

        Expected keys: screen, active, menu, cursor, label, bbox [x1,y1,x2,y2]
        or None. Returns True if a highlight is shown. Hidden in gameplay /
        unknown / inactive states. Yellow outline per user spec.
        """
        import json
        try:
            raw = open(path, encoding="utf-8").read()
        except OSError:
            return False
        try:
            st = json.loads(raw.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return False
        if (not st.get("active") or st.get("screen") in self._HIDDEN_SCREENS
                or not st.get("bbox")):
            self.hide()
            return False
        try:
            x1, y1, x2, y2 = (int(v) for v in st["bbox"])
            region = HighlightRegion.from_bbox(
                x1, y1, x2, y2, label=st.get("label") or "",
                color=(1.0, 0.9, 0.1, 0.9))
        except (TypeError, ValueError, KeyError):
            self.hide()
            return False
        self.set_highlight_regions([region])
        self.show()
        return True

    def update_fps(self, fps: float) -> None:
        """Update the displayed FPS value (thread-safe)."""
        with self._lock:
            self._state.fps = fps
            self._state.last_update = time.monotonic()
        self._schedule_redraw()

    def update_game_status(self, status: str) -> None:
        """Update the game status text (thread-safe)."""
        with self._lock:
            self._state.game_status = status
            self._state.last_update = time.monotonic()
        self._schedule_redraw()

    def update_controls_hint(self, hint: str) -> None:
        """Update the controls hint text (thread-safe)."""
        with self._lock:
            self._state.controls_hint = hint
            self._state.last_update = time.monotonic()
        self._schedule_redraw()

    def set_highlight_regions(self, regions: list[HighlightRegion] | None) -> None:
        """Set the highlight regions to display (thread-safe).

        Args:
            regions: List of regions to highlight, or None to clear
        """
        with self._lock:
            self._state.highlight_regions = regions
            self._state.last_update = time.monotonic()
        self._schedule_redraw()

    def show(self) -> None:
        """Show the overlay (thread-safe)."""
        with self._lock:
            self._state.visible = True
        if _GTK_AVAILABLE:
            GLib.idle_add(self._update_visibility)

    def hide(self) -> None:
        """Hide the overlay (thread-safe)."""
        with self._lock:
            self._state.visible = False
        if _GTK_AVAILABLE:
            GLib.idle_add(self._update_visibility)

    def _schedule_redraw(self) -> None:
        """Schedule a redraw on the GTK thread."""
        if _GTK_AVAILABLE:
            GLib.idle_add(self._do_redraw)

    def _do_redraw(self) -> bool:
        """Perform the actual redraw (must be called on GTK thread)."""
        if self._drawing_area:
            self._drawing_area.queue_draw()
        return False

    def _update_visibility(self) -> bool:
        """Update window visibility (must be called on GTK thread)."""
        if not self._window:
            return False
        with self._lock:
            should_show = self._settings.enabled and self._state.visible
        self._window.set_visible(should_show)
        if should_show:
            # Re-apply the click-through input region on every show: some
            # compositors/GTK frames may reset the surface input region, and
            # show cycles remap the window, so re-install the empty region.
            self._apply_click_through()

        # Auto-hide timer
        if should_show and self._settings.auto_hide_after_seconds > 0:
            self._start_hide_timer()

        return False

    def _start_hide_timer(self) -> None:
        """Start or restart the auto-hide timer."""
        if self._hide_timer_id is not None:
            try:
                GLib.source_remove(self._hide_timer_id)
            except Exception:
                pass

        interval_ms = int(self._settings.auto_hide_after_seconds * 1000)
        self._hide_timer_id = GLib.timeout_add(interval_ms, self._auto_hide_callback)

    def _auto_hide_callback(self) -> bool:
        """Callback for auto-hide timer."""
        with self._lock:
            last_update = self._state.last_update
            now = time.monotonic()

        if now - last_update >= self._settings.auto_hide_after_seconds:
            self.hide()
            self._hide_timer_id = None
            return False  # Don't repeat
        return True  # Repeat

    def update_settings(self, settings: OverlaySettings) -> None:
        """Update overlay settings (thread-safe)."""
        with self._lock:
            self._settings = settings
        self._renderer.update_display_size(self._display_w, self._display_h)
        self._schedule_redraw()

    def update_display_size(self, width: int, height: int) -> None:
        """Update the game display resolution for coordinate mapping."""
        with self._lock:
            self._display_w = width
            self._display_h = height
        self._renderer.update_display_size(width, height)

    def render_to_surface(self, width: int, height: int) -> bytes:
        """Render the current overlay state to a Cairo image surface.

        This is a synchronous, testable rendering path that doesn't require
        GTK or Wayland. Used for offline verification of overlay rendering.
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
            state = self._state
            settings = self._settings
            display_w = self._display_w
            display_h = self._display_h

        if not state.visible:
            return surface.get_data().tobytes()

        colors = settings.theme_colors
        sx = width / display_w if display_w > 0 else 1
        sy = height / display_h if display_h > 0 else 1

        # Calculate position
        panel_x, panel_y = self._calculate_position(
            width, height, settings.position, settings.margin_x, settings.margin_y
        )

        # Draw FPS counter
        if settings.show_fps and state.fps > 0:
            fps_text = f"FPS: {state.fps:.0f}"
            try:
                cr.select_font_face("Sans", 0, 0)
                cr.set_font_size(16)
                cr.set_source_rgba(*colors["accent"])
                cr.move_to(panel_x, panel_y + 20)
                cr.show_text(fps_text)
            except Exception:
                pass

        # Draw game status
        if settings.show_game_status and state.game_status:
            try:
                cr.select_font_face("Sans", 0, 0)
                cr.set_font_size(14)
                cr.set_source_rgba(*colors["text"])
                cr.move_to(panel_x, panel_y + 45)
                cr.show_text(state.game_status)
            except Exception:
                pass

        # Draw controls hint
        if settings.show_controls_hint and state.controls_hint:
            try:
                cr.select_font_face("Sans", 0, 0)
                cr.set_font_size(12)
                cr.set_source_rgba(*colors["text"])
                cr.move_to(panel_x, panel_y + 65)
                cr.show_text(state.controls_hint)
            except Exception:
                pass

        # Draw highlight regions
        if state.highlight_regions:
            for region in state.highlight_regions:
                self._renderer._render_region(cr, region, width, height)

        surface.flush()
        return surface.get_data().tobytes()


class DummyOverlay:
    """No-op overlay for headless/test environments.

    Provides the same interface as HostOverlay but without GTK/Wayland.
    Used for unit testing and headless operation.
    """

    def __init__(self, settings: OverlaySettings | None = None):
        self._settings = settings or OverlaySettings()
        self._state = OverlayState()
        self._display_w = 1280
        self._display_h = 800
        self._renderer = HighlightRenderer(self._display_w, self._display_h)

    @property
    def available(self) -> bool:
        return False

    @property
    def settings(self) -> OverlaySettings:
        return self._settings

    @property
    def state(self) -> OverlayState:
        return self._state

    def start(self) -> None:
        self._state.visible = True

    def stop(self) -> None:
        self._state.visible = False

    def update_fps(self, fps: float) -> None:
        self._state.fps = fps
        self._state.last_update = time.monotonic()

    def update_game_status(self, status: str) -> None:
        self._state.game_status = status
        self._state.last_update = time.monotonic()

    def update_controls_hint(self, hint: str) -> None:
        self._state.controls_hint = hint
        self._state.last_update = time.monotonic()

    def set_highlight_regions(self, regions: list[HighlightRegion] | None) -> None:
        self._state.highlight_regions = regions
        self._state.last_update = time.monotonic()

    def show(self) -> None:
        self._state.visible = True

    def hide(self) -> None:
        self._state.visible = False

    def update_settings(self, settings: OverlaySettings) -> None:
        self._settings = settings
        self._renderer.update_display_size(self._display_w, self._display_h)

    def update_display_size(self, width: int, height: int) -> None:
        self._display_w = width
        self._display_h = height
        self._renderer.update_display_size(width, height)

    def render_to_surface(self, width: int, height: int) -> bytes:
        """Render the current overlay state to a Cairo image surface.

        This is a synchronous, testable rendering path that doesn't require
        GTK or Wayland. Used for offline verification of overlay rendering.
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

        if not self._state.visible:
            return surface.get_data().tobytes()

        colors = self._settings.theme_colors
        sx = width / self._display_w if self._display_w > 0 else 1
        sy = height / self._display_h if self._display_h > 0 else 1

        # Calculate position
        panel_x, panel_y = self._calculate_position(
            width, height, self._settings.position,
            self._settings.margin_x, self._settings.margin_y
        )

        # Draw FPS counter
        if self._settings.show_fps and self._state.fps > 0:
            fps_text = f"FPS: {self._state.fps:.0f}"
            try:
                cr.select_font_face("Sans", 0, 0)
                cr.set_font_size(16)
                cr.set_source_rgba(*colors["accent"])
                cr.move_to(panel_x, panel_y + 20)
                cr.show_text(fps_text)
            except Exception:
                pass

        # Draw game status
        if self._settings.show_game_status and self._state.game_status:
            try:
                cr.select_font_face("Sans", 0, 0)
                cr.set_font_size(14)
                cr.set_source_rgba(*colors["text"])
                cr.move_to(panel_x, panel_y + 45)
                cr.show_text(self._state.game_status)
            except Exception:
                pass

        # Draw controls hint
        if self._settings.show_controls_hint and self._state.controls_hint:
            try:
                cr.select_font_face("Sans", 0, 0)
                cr.set_font_size(12)
                cr.set_source_rgba(*colors["text"])
                cr.move_to(panel_x, panel_y + 65)
                cr.show_text(self._state.controls_hint)
            except Exception:
                pass

        # Draw highlight regions
        if self._state.highlight_regions:
            for region in self._state.highlight_regions:
                self._renderer._render_region(cr, region, width, height)

        surface.flush()
        return surface.get_data().tobytes()

    def _calculate_position(self, surface_width: int, surface_height: int,
                            position: OverlayPosition,
                            margin_x: int, margin_y: int) -> tuple[float, float]:
        """Calculate panel position based on settings."""
        panel_width = 200
        panel_height = 80

        if position == OverlayPosition.TOP_LEFT:
            return (margin_x, margin_y)
        elif position == OverlayPosition.TOP_RIGHT:
            return (surface_width - panel_width - margin_x, margin_y)
        elif position == OverlayPosition.BOTTOM_LEFT:
            return (margin_x, surface_height - panel_height - margin_y)
        elif position == OverlayPosition.BOTTOM_RIGHT:
            return (surface_width - panel_width - margin_x,
                    surface_height - panel_height - margin_y)
        elif position == OverlayPosition.TOP_CENTER:
            return ((surface_width - panel_width) / 2, margin_y)
        elif position == OverlayPosition.BOTTOM_CENTER:
            return ((surface_width - panel_width) / 2,
                    surface_height - panel_height - margin_y)
        return (margin_x, margin_y)

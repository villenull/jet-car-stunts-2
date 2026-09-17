"""HOST OVERLAY v1 — Launcher-owned overlay for game-running state display.

This package provides a GTK4 layer-shell overlay that renders on the HOST
display (Steam Deck eDP-1) during gameplay. It is:
  - OFFLINE ONLY: no ADB communication, no guest interaction
  - Never touches the guest Android system
  - Configurable via settings model
  - Testable offline via render_to_surface()

The overlay shows game status, performance metrics, and control hints
directly on the host display without affecting the game or guest system.
"""

from .host_overlay import HostOverlay, DummyOverlay
from .settings_model import OverlaySettings, OverlayPosition, OverlayTheme
from .highlight import HighlightRenderer

__all__ = [
    "HostOverlay",
    "DummyOverlay",
    "OverlaySettings",
    "OverlayPosition",
    "OverlayTheme",
    "HighlightRenderer",
]

"""Settings model for HOST OVERLAY v1.

Provides configurable overlay appearance and behavior without any
guest/ADB interaction. Settings are persisted to JSON and can be
modified via the launcher settings UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class OverlayPosition(Enum):
    """Screen position for the overlay."""
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    TOP_CENTER = "top_center"
    BOTTOM_CENTER = "bottom_center"


class OverlayTheme(Enum):
    """Visual theme for the overlay."""
    DARK = "dark"
    LIGHT = "light"
    GAME_GREEN = "game_green"  # Matches JCS2 green HUD theme


@dataclass
class OverlaySettings:
    """Configuration for the host overlay.

    All settings are host-side only and never affect the guest.
    """
    # Visibility
    enabled: bool = True
    show_during_gameplay: bool = True
    auto_hide_after_seconds: float = 5.0  # 0 = never auto-hide

    # Position and size
    position: OverlayPosition = OverlayPosition.TOP_RIGHT
    scale: float = 1.0  # 0.5 to 2.0
    margin_x: int = 20
    margin_y: int = 20

    # Appearance
    theme: OverlayTheme = OverlayTheme.GAME_GREEN
    opacity: float = 0.85  # 0.0 to 1.0
    corner_radius: int = 8
    border_width: int = 2

    # Content
    show_fps: bool = True
    show_controls_hint: bool = True
    show_game_status: bool = True

    # Performance
    target_fps: int = 60
    update_interval_ms: int = 100

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "enabled": self.enabled,
            "show_during_gameplay": self.show_during_gameplay,
            "auto_hide_after_seconds": self.auto_hide_after_seconds,
            "position": self.position.value,
            "scale": self.scale,
            "margin_x": self.margin_x,
            "margin_y": self.margin_y,
            "theme": self.theme.value,
            "opacity": self.opacity,
            "corner_radius": self.corner_radius,
            "border_width": self.border_width,
            "show_fps": self.show_fps,
            "show_controls_hint": self.show_controls_hint,
            "show_game_status": self.show_game_status,
            "target_fps": self.target_fps,
            "update_interval_ms": self.update_interval_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OverlaySettings:
        """Deserialize from dict, ignoring unknown keys."""
        kwargs: dict[str, Any] = {}
        if "enabled" in data:
            kwargs["enabled"] = bool(data["enabled"])
        if "show_during_gameplay" in data:
            kwargs["show_during_gameplay"] = bool(data["show_during_gameplay"])
        if "auto_hide_after_seconds" in data:
            kwargs["auto_hide_after_seconds"] = float(data["auto_hide_after_seconds"])
        if "position" in data:
            try:
                kwargs["position"] = OverlayPosition(data["position"])
            except ValueError:
                pass
        if "scale" in data:
            kwargs["scale"] = max(0.5, min(2.0, float(data["scale"])))
        if "margin_x" in data:
            kwargs["margin_x"] = int(data["margin_x"])
        if "margin_y" in data:
            kwargs["margin_y"] = int(data["margin_y"])
        if "theme" in data:
            try:
                kwargs["theme"] = OverlayTheme(data["theme"])
            except ValueError:
                pass
        if "opacity" in data:
            kwargs["opacity"] = max(0.0, min(1.0, float(data["opacity"])))
        if "corner_radius" in data:
            kwargs["corner_radius"] = max(0, int(data["corner_radius"]))
        if "border_width" in data:
            kwargs["border_width"] = max(1, int(data["border_width"]))
        if "show_fps" in data:
            kwargs["show_fps"] = bool(data["show_fps"])
        if "show_controls_hint" in data:
            kwargs["show_controls_hint"] = bool(data["show_controls_hint"])
        if "show_game_status" in data:
            kwargs["show_game_status"] = bool(data["show_game_status"])
        if "target_fps" in data:
            kwargs["target_fps"] = max(1, int(data["target_fps"]))
        if "update_interval_ms" in data:
            kwargs["update_interval_ms"] = max(16, int(data["update_interval_ms"]))
        return cls(**kwargs)

    def save(self, path: Path | str) -> None:
        """Save settings to JSON file atomically."""
        path = Path(path)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except OSError:
            # Clean up temp file on failure
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path | str) -> OverlaySettings:
        """Load settings from JSON file, returning defaults on error."""
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (OSError, json.JSONDecodeError):
            return cls()

    @property
    def theme_colors(self) -> dict[str, tuple[float, float, float, float]]:
        """Get RGBA colors for the current theme.

        Returns dict with keys: background, border, text, accent
        Each value is (r, g, b, a) with values in 0.0-1.0 range.
        """
        if self.theme == OverlayTheme.DARK:
            return {
                "background": (0.1, 0.1, 0.1, self.opacity),
                "border": (0.3, 0.3, 0.3, self.opacity),
                "text": (0.9, 0.9, 0.9, self.opacity),
                "accent": (0.4, 0.8, 1.0, self.opacity),
            }
        elif self.theme == OverlayTheme.LIGHT:
            return {
                "background": (0.9, 0.9, 0.9, self.opacity),
                "border": (0.6, 0.6, 0.6, self.opacity),
                "text": (0.1, 0.1, 0.1, self.opacity),
                "accent": (0.0, 0.5, 0.8, self.opacity),
            }
        else:  # GAME_GREEN
            return {
                "background": (0.05, 0.15, 0.05, self.opacity * 0.9),
                "border": (0.2, 1.0, 0.2, self.opacity),
                "text": (0.8, 1.0, 0.8, self.opacity),
                "accent": (0.2, 1.0, 0.2, self.opacity),
            }

    def clamp_values(self) -> None:
        """Clamp all numeric values to valid ranges."""
        self.scale = max(0.5, min(2.0, self.scale))
        self.opacity = max(0.0, min(1.0, self.opacity))
        self.corner_radius = max(0, self.corner_radius)
        self.border_width = max(1, self.border_width)
        self.target_fps = max(1, self.target_fps)
        self.update_interval_ms = max(16, self.update_interval_ms)
        self.margin_x = max(0, self.margin_x)
        self.margin_y = max(0, self.margin_y)

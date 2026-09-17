"""Launcher-owned Settings UI for steering/pitch mode selection.

This is a LAUNCHER overlay for configuring the input pipeline — NOT a
modification of native game settings. It controls:
  - Steering mode: Gamepad (left stick) vs Tilt (accelerometer)
  - Pitch mode: Gamepad (left stick Y) vs Tilt (gyroscope)
  - Sensitivity: 0.5x .. 2.0x (multiplier for tilt steering/pitch)
  - Deadzone: 0.0 .. 0.5 (tilt input deadzone)
  - Smoothing: 0.0 .. 1.0 (gyro trust weight / smoothing alpha)
  - Calibrate: trigger tilt center recalibration
  - Progression unlock: optional setting (requires restart; actual apply
    hook is supplied by the coordinator, not implemented here)

The UI is rendered as a GTK4 layer-shell overlay, navigable with D-pad/A/B.
It is explicitly labeled as "JCS2 Launcher Settings" to avoid confusion
with native game settings.

Changes take effect via a callback to the runner/bridge pipeline — this
module does NOT modify the Android guest or game files.

Tilt settings bridge: sync_to_tilt_settings() writes LauncherSettings
into tilt_control.settings.TiltSettings for runtime consumption.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

# Optional tilt_control bridge (fail-closed if unavailable)
try:
    from tilt_control.settings import TiltSettings, ControlMode as TiltControlMode
except ImportError:
    TiltSettings = None
    TiltControlMode = None


class SteeringMode(Enum):
    GAMEPAD = "gamepad"
    TILT = "tilt"


class PitchMode(Enum):
    GAMEPAD = "gamepad"
    TILT = "tilt"


@dataclass
class LauncherSettings:
    """Current launcher input settings. Serializable to JSON."""
    steering_mode: SteeringMode = SteeringMode.GAMEPAD
    pitch_mode: PitchMode = PitchMode.GAMEPAD
    tilt_sensitivity: float = 1.0
    tilt_deadzone: float = 0.10
    tilt_smoothing: float = 0.92
    # progression_unlock requires restart; actual apply hook from coordinator
    progression_unlock_enabled: bool = False

    def to_dict(self) -> dict:
        return {
            "steering_mode": self.steering_mode.value,
            "pitch_mode": self.pitch_mode.value,
            "tilt_sensitivity": self.tilt_sensitivity,
            "tilt_deadzone": self.tilt_deadzone,
            "tilt_smoothing": self.tilt_smoothing,
            "progression_unlock_enabled": self.progression_unlock_enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> LauncherSettings:
        return cls(
            steering_mode=SteeringMode(d.get("steering_mode", "gamepad")),
            pitch_mode=PitchMode(d.get("pitch_mode", "gamepad")),
            tilt_sensitivity=float(d.get("tilt_sensitivity", 1.0)),
            tilt_deadzone=float(d.get("tilt_deadzone", 0.10)),
            tilt_smoothing=float(d.get("tilt_smoothing", 0.92)),
            progression_unlock_enabled=bool(d.get("progression_unlock_enabled", False)),
        )

    def save(self, path: Path):
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> LauncherSettings:
        if path.exists():
            return cls.from_dict(json.loads(path.read_text()))
        return cls()

    def sync_to_tilt_settings(self, tilt_settings_path: Path | str | None = None) -> bool:
        """Write this LauncherSettings into tilt_control.settings.TiltSettings.

        Returns True if the sync succeeded, False if tilt_control is unavailable.
        This bridges the launcher UI settings to the tilt adapter runtime.
        """
        if TiltSettings is None:
            return False
        try:
            ts = TiltSettings(tilt_settings_path) if tilt_settings_path else TiltSettings()
            # Map steering mode to tilt control mode
            tilt_mode = TiltControlMode.TILT if self.steering_mode == SteeringMode.TILT else TiltControlMode.GAMEPAD
            ts.update(
                mode=tilt_mode,
                steering_mode=self.steering_mode.value,
                pitch_mode=self.pitch_mode.value,
                tilt_gain_lx=self.tilt_sensitivity,
                tilt_gain_ly=self.tilt_sensitivity,
                deadzone=self.tilt_deadzone,
                smoothing_alpha=self.tilt_smoothing,
            )
            return True
        except Exception:
            return False

    @classmethod
    def from_tilt_settings(cls, tilt_settings_path: Path | str | None = None) -> LauncherSettings:
        """Load LauncherSettings from an existing TiltSettings file.

        Returns default LauncherSettings if tilt_control is unavailable.
        """
        if TiltSettings is None:
            return cls()
        try:
            ts = TiltSettings(tilt_settings_path) if tilt_settings_path else TiltSettings()
            steering = SteeringMode.TILT if ts.axis_mode("LX") == TiltControlMode.TILT else SteeringMode.GAMEPAD
            return cls(
                steering_mode=steering,
                pitch_mode=PitchMode.TILT if ts.axis_mode("LY") == TiltControlMode.TILT else PitchMode.GAMEPAD,
                tilt_sensitivity=ts.tilt_gain_lx,
                tilt_deadzone=ts.deadzone,
                tilt_smoothing=ts.smoothing_alpha,
            )
        except Exception:
            return cls()


class SettingsItem:
    """One item in the settings menu."""
    def __init__(self, key: str, label: str, values: list[str],
                 current_index: int = 0, requires_restart: bool = False):
        self.key = key
        self.label = label
        self.values = values
        self._index = current_index
        self.requires_restart = requires_restart

    @property
    def current_value(self) -> str:
        return self.values[self._index]

    @property
    def index(self) -> int:
        return self._index

    def next_value(self):
        self._index = (self._index + 1) % len(self.values)

    def prev_value(self):
        self._index = (self._index - 1) % len(self.values)

    def set_index(self, idx: int):
        self._index = max(0, min(idx, len(self.values) - 1))


class SettingsController:
    """Controller for the launcher settings menu.

    Manages navigation within settings items and applies changes
    through callbacks. The actual overlay rendering is handled
    separately by the overlay module.

    Settings are explicitly labeled as launcher settings (not game settings).
    """

    def __init__(
        self,
        settings: LauncherSettings,
        on_settings_change: Callable[[LauncherSettings], None] | None = None,
        on_calibrate: Callable[[], None] | None = None,
        apply_progression_hook: Callable[[bool], None] | None = None,
    ):
        self._settings = settings
        self._on_change = on_settings_change
        self._on_calibrate = on_calibrate
        self._apply_progression = apply_progression_hook
        self._cursor = 0
        self._active = False

        self._items = self._build_items()

    def _build_items(self) -> list[SettingsItem]:
        items = [
            SettingsItem(
                "steering_mode", "Steering",
                ["Gamepad (Left Stick)", "Tilt (Accelerometer)"],
                0 if self._settings.steering_mode == SteeringMode.GAMEPAD else 1,
            ),
            SettingsItem(
                "pitch_mode", "Pitch",
                ["Gamepad (Left Stick Y)", "Tilt (Gyroscope)"],
                0 if self._settings.pitch_mode == PitchMode.GAMEPAD else 1,
            ),
            SettingsItem(
                "sensitivity", "Tilt Sensitivity",
                ["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"],
                self._sensitivity_to_index(self._settings.tilt_sensitivity),
            ),
            SettingsItem(
                "deadzone", "Tilt Deadzone",
                ["0.00", "0.05", "0.10", "0.15", "0.20", "0.30", "0.50"],
                self._deadzone_to_index(self._settings.tilt_deadzone),
            ),
            SettingsItem(
                "smoothing", "Tilt Smoothing",
                ["0.0 (none)", "0.50", "0.80", "0.92", "0.95", "1.0 (max)"],
                self._smoothing_to_index(self._settings.tilt_smoothing),
            ),
            SettingsItem(
                "calibrate", "Calibrate Tilt Center",
                ["[Press A to calibrate]"],
            ),
            SettingsItem(
                "progression_unlock", "Unlock Progression",
                ["Off", "On (requires restart)"],
                1 if self._settings.progression_unlock_enabled else 0,
                requires_restart=True,
            ),
        ]
        return items

    @staticmethod
    def _sensitivity_to_index(val: float) -> int:
        levels = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        closest = min(range(len(levels)), key=lambda i: abs(levels[i] - val))
        return closest

    @staticmethod
    def _index_to_sensitivity(idx: int) -> float:
        levels = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        return levels[min(idx, len(levels) - 1)]

    @staticmethod
    def _deadzone_to_index(val: float) -> int:
        levels = [0.00, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]
        closest = min(range(len(levels)), key=lambda i: abs(levels[i] - val))
        return closest

    @staticmethod
    def _index_to_deadzone(idx: int) -> float:
        levels = [0.00, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]
        return levels[min(idx, len(levels) - 1)]

    @staticmethod
    def _smoothing_to_index(val: float) -> int:
        levels = [0.0, 0.50, 0.80, 0.92, 0.95, 1.0]
        closest = min(range(len(levels)), key=lambda i: abs(levels[i] - val))
        return closest

    @staticmethod
    def _index_to_smoothing(idx: int) -> float:
        levels = [0.0, 0.50, 0.80, 0.92, 0.95, 1.0]
        return levels[min(idx, len(levels) - 1)]

    @property
    def items(self) -> list[SettingsItem]:
        return self._items

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def active(self) -> bool:
        return self._active

    @property
    def settings(self) -> LauncherSettings:
        return self._settings

    def activate(self):
        self._active = True
        self._cursor = 0

    def deactivate(self):
        self._active = False

    def move_up(self):
        if not self._active:
            return
        self._cursor = (self._cursor - 1) % len(self._items)

    def move_down(self):
        if not self._active:
            return
        self._cursor = (self._cursor + 1) % len(self._items)

    def adjust_left(self):
        if not self._active:
            return
        item = self._items[self._cursor]
        if item.key == "calibrate":
            return
        item.prev_value()
        self._apply_item(item)

    def adjust_right(self):
        if not self._active:
            return
        item = self._items[self._cursor]
        if item.key == "calibrate":
            return
        item.next_value()
        self._apply_item(item)

    def select(self):
        """A button on current item."""
        if not self._active:
            return
        item = self._items[self._cursor]
        if item.key == "calibrate":
            if self._on_calibrate:
                self._on_calibrate()
            return
        # For toggle items, A cycles to next value
        item.next_value()
        self._apply_item(item)

    def _apply_item(self, item: SettingsItem):
        if item.key == "steering_mode":
            self._settings.steering_mode = (
                SteeringMode.GAMEPAD if item.index == 0 else SteeringMode.TILT
            )
        elif item.key == "pitch_mode":
            self._settings.pitch_mode = (
                PitchMode.GAMEPAD if item.index == 0 else PitchMode.TILT
            )
        elif item.key == "sensitivity":
            self._settings.tilt_sensitivity = self._index_to_sensitivity(item.index)
        elif item.key == "deadzone":
            self._settings.tilt_deadzone = self._index_to_deadzone(item.index)
        elif item.key == "smoothing":
            self._settings.tilt_smoothing = self._index_to_smoothing(item.index)
        elif item.key == "progression_unlock":
            self._settings.progression_unlock_enabled = item.index == 1
            if self._apply_progression:
                self._apply_progression(self._settings.progression_unlock_enabled)

        if self._on_change:
            self._on_change(self._settings)

    def render_state(self) -> list[dict]:
        """Snapshot of all items for overlay rendering."""
        return [
            {
                "key": item.key,
                "label": item.label,
                "value": item.current_value,
                "selected": i == self._cursor,
                "requires_restart": item.requires_restart,
            }
            for i, item in enumerate(self._items)
        ]

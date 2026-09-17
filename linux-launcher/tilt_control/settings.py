"""Persistent project-local settings for tilt control mode and calibration."""

import enum
import json
import os
import time
import tempfile
from pathlib import Path

def default_settings_path() -> Path:
    # Match the runner for both explicit portable and legacy layouts.
    from runtime_paths import resolve_paths
    return resolve_paths().logdir / "tilt-settings.json"


class ControlMode(enum.Enum):
    GAMEPAD = "gamepad"
    TILT = "tilt"


_DEFAULTS = {
    "mode": ControlMode.GAMEPAD.value,
    "calibration": None,
    "tilt_gain_lx": 1.0,
    "tilt_gain_ly": 1.0,
    "deadzone": 0.10,
    "smoothing_alpha": 0.92,
}


_ALLOWED_KEYS = frozenset({
    "mode",
    "steering_mode",
    "pitch_mode",
    "calibration",
    "tilt_gain_lx",
    "tilt_gain_ly",
    "deadzone",
    "smoothing_alpha",
})


class TiltSettings:
    """Read/write project-local persistent JSON state for tilt control.

    All public methods are safe to call from any thread; file I/O is
    atomic via rename.
    """

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else default_settings_path()
        self._cache: dict | None = None

    def _load(self) -> dict:
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp = tempfile.mkstemp(prefix=self._path.name + ".", dir=self._path.parent)
        try:
            with os.fdopen(descriptor, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def axis_mode(self, axis: str) -> ControlMode:
        """Compatibility accessor: both axes always share one driving mode."""
        if axis not in ("LX", "LY"):
            raise KeyError(axis)
        return self.mode

    @property
    def mode(self) -> ControlMode:
        data = self._load()
        legacy = data.get("mode", _DEFAULTS["mode"])
        # Older files may carry per-axis choices. Agreeing choices survive;
        # ambiguous/malformed mixed choices safely return to the joystick.
        try:
            steering = ControlMode(data.get("steering_mode", legacy))
            pitch = ControlMode(data.get("pitch_mode", legacy))
        except (ValueError, TypeError):
            return ControlMode.GAMEPAD
        return steering if steering == pitch else ControlMode.GAMEPAD

    @mode.setter
    def mode(self, value: ControlMode) -> None:
        data = self._load()
        data["mode"] = value.value
        data["steering_mode"] = value.value
        data["pitch_mode"] = value.value
        self._save(data)

    @property
    def calibration(self) -> dict | None:
        """Return stored calibration or None."""
        data = self._load()
        return data.get("calibration", None)

    @calibration.setter
    def calibration(self, value: dict | None) -> None:
        data = self._load()
        if value is not None:
            value["timestamp"] = time.time()
        data["calibration"] = value
        self._save(data)

    @property
    def deadzone(self) -> float:
        """Deadzone clamped to [0, 0.99]; adapter math stays well-defined."""
        data = self._load()
        return max(0.0, min(0.99, float(data.get("deadzone", _DEFAULTS["deadzone"]))))

    @property
    def smoothing_alpha(self) -> float:
        """Gyro trust weight clamped to [0, 1]."""
        data = self._load()
        return max(0.0, min(1.0, float(data.get("smoothing_alpha", _DEFAULTS["smoothing_alpha"]))))

    @property
    def tilt_gain_lx(self) -> float:
        data = self._load()
        return float(data.get("tilt_gain_lx", _DEFAULTS["tilt_gain_lx"]))

    @property
    def tilt_gain_ly(self) -> float:
        data = self._load()
        return float(data.get("tilt_gain_ly", _DEFAULTS["tilt_gain_ly"]))

    def update(self, **kwargs) -> None:
        """Merge known keys into settings; unknown keys raise ValueError.

        Rejects typos instead of silently persisting them, so a bad key can
        never poison the settings file.
        """
        unknown = set(kwargs) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(
                f"unknown settings key(s): {sorted(unknown)}; "
                f"allowed: {sorted(_ALLOWED_KEYS)}")
        data = self._load()
        for k, v in kwargs.items():
            if isinstance(v, ControlMode):
                v = v.value
            data[k] = v
        self._save(data)

    def as_dict(self) -> dict:
        merged = dict(_DEFAULTS)
        merged.update(self._load())
        return merged

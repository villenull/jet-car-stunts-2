"""Tilt control adapter: coordinator-facing interface.

Provides select_mode, calibrate, consume_motion, axes, and health, plus a
small CLI (``python -m tilt_control.adapter``) for the Gamepad/Tilt
selector and calibration.

Corrected-C: the runner's live path uses ONLY ``sensor_acceleration``
(native Android vectors). ``consume_motion`` virtual LX/LY events are
retained for the CLI/tests but are never forwarded to the game.
Stick suppression authority lives SOLELY in ``tilt_control/stick_gate.py``
as applied by the runner: persisted Tilt Drive drops physical LX/LY
(fail-closed, sensor-independent); Gamepad forwards them. This adapter
never forwards axes itself, so ``controlled_axes`` below is legacy-only
and is not consulted on the live path.

Live contract: the runner MUST call ``poll_native()`` before
``sensor_acceleration()`` on every native push. ``sensor_acceleration``
reads the estimator angles, and nothing else feeds the estimator —
without ``poll_native`` the vector is frozen at neutral and physical
rotation can never reach the guest (Sept-14 root cause #2).

Selector scope (legacy): the Gamepad/Tilt choice governed ONLY steering
(LX) and pitch (LY) axes. Physical button mappings (R2 accel, L2
reverse, LB airbrake, START pause, Y reset) live in the joystick bridge
and are unchanged in either mode.
"""

import argparse
import json
import math
import sys
import time

from .estimator import TiltEstimator
from .motion_reader import MotionReader, MotionSample, SYSFS_INPUT_ROOT
from .settings import TiltSettings, ControlMode

MAX_TILT_ANGLE_RAD = math.radians(35.0)
CALIBRATION_SAMPLES = 50


class TiltAdapter:
    """Coordinator-facing interface for tilt steering and pitch.

    Parameters
    ----------
    settings : TiltSettings or None
        Persistent settings store; creates default if None.
    device_path : str or None
        Override motion sensor evdev path; auto-discovers if None.
    """

    def __init__(
        self,
        settings: TiltSettings | None = None,
        device_path: str | None = None,
        sysfs_root: str | None = None,
    ):
        self.settings = settings or TiltSettings()
        self._reader = MotionReader(device_path,
                                    sysfs_root=sysfs_root or SYSFS_INPUT_ROOT)
        self._estimator = TiltEstimator(alpha=self.settings.smoothing_alpha)
        self._mode = self.settings.mode
        self._lx = 0.0
        self._ly = 0.0
        self._last_update: float | None = None
        self._open = False

        cal = self.settings.calibration
        if cal:
            # load_calibration validates and rejects partial/legacy dicts.
            self._estimator.load_calibration(cal)

    def open(self) -> bool:
        """Open the motion sensor. Safe to call multiple times."""
        if self._open:
            return True
        self._open = self._reader.open()
        return self._open

    def await_motion(self, timeout: float = 0.5) -> bool:
        """Return True once the opened sensor delivers a complete sample.

        An openable node is not proof of motion data: another owner of the
        controller can leave the evdev node silent. Samples read here only
        prove readiness; select_mode() resets the estimator afterward.
        """
        if not self._open:
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._reader.read_samples():
                return True
            if not self._reader.is_open or time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self) -> None:
        self._reader.close()
        self._open = False
        self._lx = 0.0
        self._ly = 0.0
        self._estimator.reset_to_neutral()

    def select_mode(self, mode: ControlMode) -> None:
        """Switch between GAMEPAD and TILT mode. Persists to disk."""
        self.settings.mode = mode
        self._mode = mode
        self._lx = 0.0
        self._ly = 0.0
        self._estimator.reset_to_neutral()

    @property
    def mode(self) -> ControlMode:
        return self._mode

    def calibrate(self, samples: list[MotionSample] | None = None) -> dict | None:
        """Capture neutral position from accumulated samples or live read.

        If samples is None, reads CALIBRATION_SAMPLES from the device.
        Returns calibration dict or None on failure. Persists to settings.
        """
        if samples is None:
            if not self._open and not self.open():
                return None
            collected: list[MotionSample] = []
            deadline = time.monotonic() + 2.0
            while len(collected) < CALIBRATION_SAMPLES and time.monotonic() < deadline:
                batch = self._reader.read_samples()
                collected.extend(batch)
                if not batch:
                    time.sleep(0.008)
            samples = collected

        if not samples:
            return None

        avg_x = sum(s.acc_x for s in samples) / len(samples)
        avg_y = sum(s.acc_y for s in samples) / len(samples)
        avg_z = sum(s.acc_z for s in samples) / len(samples)

        cal = self._estimator.calibrate(avg_x, avg_y, avg_z)
        cal["sample_count"] = len(samples)
        self.settings.calibration = cal
        return cal

    @property
    def controlled_axes(self) -> frozenset[str]:
        """Legacy synthetic-path axis record (not consulted on the live path).

        Sole suppression authority is ``tilt_control/stick_gate.py`` as
        applied by the runner (persisted Tilt Drive drops LX/LY fail-closed).
        Retained for API/test compatibility and as a record of which axes
        the old synthetic path used to own.
        """
        if not self._open or self._mode != ControlMode.TILT:
            return frozenset()
        return frozenset(("LX", "LY"))

    def consume_motion(self) -> list[dict]:
        return self._consume_motion()

    def poll_native(self) -> bool:
        """Feed pending IMU samples into the estimator for the native feed.

        Called by the runner's always-on native path before
        ``sensor_acceleration()``. Unlike ``consume_motion`` this never
        produces virtual axis events and runs in EVERY mode whenever the
        IMU is owned (the game's own Gamepad toggle arbitrates
        guest-side). Returns True when at least one fresh sample updated
        the estimator; False when closed or no data is pending (in which
        case the estimator — and the stale check — are untouched).
        """
        if not self._open:
            return False
        # Honor persisted smoothing alpha even if it changed on disk since
        # construction (settings are read fresh from disk on each access).
        self._estimator.alpha = self.settings.smoothing_alpha
        samples = self._reader.read_samples()
        if not samples:
            return False
        for sample in samples:
            self._estimator.update(
                sample.acc_x, sample.acc_y, sample.acc_z,
                sample.gyro_x, sample.gyro_y, sample.gyro_z,
                sample.timestamp,
            )
        return True

    @property
    def device_error(self) -> str | None:
        """Last IMU open/read failure reason, if any (never raises)."""
        try:
            return self._reader.last_error
        except AttributeError:
            return None

    @property
    def match_tier(self) -> str | None:
        """Discovery tier that owned the IMU (tier1/tier2/tier3), if any."""
        try:
            return self._reader.match_tier
        except AttributeError:
            return None

    @property
    def discovery_snapshot(self) -> str | None:
        """Sysfs inventory from the last discovery (miss or match)."""
        try:
            return self._reader.last_snapshot
        except AttributeError:
            return None

    def _consume_motion(self) -> list[dict]:
        """Read pending motion samples and produce virtual axis events.

        Returns a list of dicts in the same format as joystick_bridge:
            {"type": "axis", "axis": "LX"|"LY", "value": float, "t_ms": int}

        In GAMEPAD mode, returns empty (physical stick is authoritative).
        In TILT mode, returns LX/LY events derived from IMU tilt.
        Stale sensor data or disconnect forces neutral.
        """
        if self._mode != ControlMode.TILT:
            return []

        if not self._open:
            return []

        # Honor persisted smoothing alpha even if it changed on disk since
        # construction (settings are read fresh from disk on each access).
        self._estimator.alpha = self.settings.smoothing_alpha

        samples = self._reader.read_samples()
        events: list[dict] = []
        now_mono = time.monotonic()

        if not samples and self._estimator.is_stale(now_mono):
            if self._lx != 0.0 or self._ly != 0.0:
                self._lx = 0.0
                self._ly = 0.0
                t_ms = int(now_mono * 1000)
                events.append({"type": "axis", "axis": "LX", "value": 0.0, "t_ms": t_ms})
                events.append({"type": "axis", "axis": "LY", "value": 0.0, "t_ms": t_ms})
            return events

        for sample in samples:
            roll, pitch = self._estimator.update(
                sample.acc_x, sample.acc_y, sample.acc_z,
                sample.gyro_x, sample.gyro_y, sample.gyro_z,
                sample.timestamp,
            )
            self._last_update = sample.timestamp

        if not samples:
            return events

        gain_lx = self.settings.tilt_gain_lx
        gain_ly = self.settings.tilt_gain_ly
        deadzone = self.settings.deadzone

        raw_lx = self._estimator.roll / MAX_TILT_ANGLE_RAD * gain_lx
        raw_ly = self._estimator.pitch / MAX_TILT_ANGLE_RAD * gain_ly

        new_lx = self._apply_deadzone_and_clamp(raw_lx, deadzone)
        new_ly = self._apply_deadzone_and_clamp(raw_ly, deadzone)

        t_ms = int(time.monotonic() * 1000)

        if abs(new_lx - self._lx) > 0.005:
            self._lx = new_lx
            events.append({"type": "axis", "axis": "LX", "value": round(new_lx, 4), "t_ms": t_ms})

        if abs(new_ly - self._ly) > 0.005:
            self._ly = new_ly
            events.append({"type": "axis", "axis": "LY", "value": round(new_ly, 4), "t_ms": t_ms})

        return events

    @staticmethod
    def _apply_deadzone_and_clamp(value: float, deadzone: float) -> float:
        if deadzone <= 0.0:
            return max(-1.0, min(1.0, value))
        if deadzone >= 1.0:
            return 0.0
        mag = abs(value)
        if mag <= deadzone:
            return 0.0
        sign = 1.0 if value >= 0 else -1.0
        scaled = (mag - deadzone) / (1.0 - deadzone)
        return sign * min(1.0, scaled)

    @property
    def axes(self) -> dict[str, float]:
        """Current tilt-derived axis values."""
        return {"LX": self._lx, "LY": self._ly}

    def sensor_acceleration(self) -> tuple[float, float, float]:
        """Current roll/pitch as an Android accelerometer vector (m/s^2).

        Uses the live estimator angles with the persisted per-axis gains,
        so host calibration and the game's own Tilt calibration compose.
        Neutral (no tilt) yields (0, 0, g). Imported lazily to keep the
        adapter importable without the console transport.
        """
        from .sensor_transport import acceleration_from_tilt

        return acceleration_from_tilt(
            self._estimator.roll,
            self._estimator.pitch,
            self.settings.tilt_gain_lx,
            self.settings.tilt_gain_ly,
        )

    def health(self) -> dict:
        """Status snapshot for diagnostics."""
        now = time.monotonic()
        return {
            "mode": self._mode.value,
            "device_open": self._open,
            "device_path": self._reader.device_path,
            "device_error": self.device_error,
            "match_tier": self.match_tier,
            "calibrated": self._estimator.is_calibrated,
            "stale": self._estimator.is_stale(now),
            "roll_rad": self._estimator.roll,
            "pitch_rad": self._estimator.pitch,
            "lx": self._lx,
            "ly": self._ly,
        }


def run_cli(argv: list[str] | None = None, settings_path: str | None = None) -> int:
    """Selector / calibration command line interface.

    Subcommands
    -----------
    status      Print current mode and calibration (no device access).
    select      gamepad|tilt — switch the steering/pitch input source and
                persist the choice to the settings file.
    calibrate   Capture the current flat hold as neutral and persist it
                (opens the IMU read-only; non-blocking, no grab).

    The selector governs only steering (LX) and pitch (LY); physical
    mappings (R2/L2/LB/A/START/Y) are owned by the joystick bridge and are
    unaffected. Returns 0 on success, non-zero on recoverable errors.
    """
    parser = argparse.ArgumentParser(
        prog="tilt-control",
        description="Tilt control mode selector and calibration (steering + pitch only).")
    parser.add_argument(
        "--settings", default=settings_path,
        help="settings JSON path (default: selected launcher log directory/tilt-settings.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show persisted mode and calibration state")

    p_select = sub.add_parser(
        "select", help="choose input source for LX steering and LY pitch")
    p_select.add_argument(
        "mode", choices=[ControlMode.GAMEPAD.value, ControlMode.TILT.value],
        help="gamepad (physical stick) or tilt (IMU)")

    p_cal = sub.add_parser(
        "calibrate", help="capture the current flat hold as neutral tilt")
    p_cal.add_argument(
        "--device", default=None,
        help="motion sensor evdev path (default: auto-discover)")

    args = parser.parse_args(argv)
    settings = TiltSettings(args.settings) if args.settings else TiltSettings()

    if args.command == "status":
        cal = settings.calibration or {}
        info = {
            "mode": settings.mode.value,
            "calibrated": bool(
                "roll_offset" in cal and "pitch_offset" in cal),
            "calibration_sample_count": cal.get("sample_count"),
            "calibration_timestamp": cal.get("timestamp"),
            "deadzone": settings.deadzone,
            "smoothing_alpha": settings.smoothing_alpha,
            "tilt_gain_lx": settings.tilt_gain_lx,
            "tilt_gain_ly": settings.tilt_gain_ly,
            "scope": "LX steering + LY pitch only (buttons: joystick bridge)",
            "settings_path": str(settings._path),
        }
        print(json.dumps(info, indent=2))
        return 0

    if args.command == "select":
        mode = ControlMode(args.mode)
        settings.mode = mode
        print(json.dumps({
            "mode": mode.value,
            "persisted": True,
            "scope": "LX steering + LY pitch only; R2/L2/LB/A/START/Y mapped by bridge",
        }))
        return 0

    if args.command == "calibrate":
        adapter = TiltAdapter(settings=settings, device_path=args.device)
        try:
            if not adapter.open():
                print("ERROR: could not open motion sensor", file=sys.stderr)
                return 1
            cal = adapter.calibrate()
        finally:
            adapter.close()
        if cal is None:
            print("ERROR: calibration failed (no samples within timeout)",
                  file=sys.stderr)
            return 2
        print(json.dumps(cal, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())

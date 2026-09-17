"""Complementary filter tilt estimator from accelerometer + gyroscope.

Hardware evidence (Steam Deck Motion Sensors, event9/js1):
  - Vendor 28de:1205, node input2, Handlers: event9 js1
  - ABS_X/Y/Z  = accelerometer, range +-32768, resolution 16384 (units/g)
  - ABS_RX/RY/RZ = gyroscope, range +-32768, resolution 16 (units per deg/s)
  - Sample rate ~250 Hz (4ms between events)

Coordinate system (verified with Deck at rest in landscape game position):
  AccX ~510 (~0.03g)  = left-right axis
  AccY ~8680 (~0.53g) = screen-normal / tilt-back component
  AccZ ~-13985 (-0.85g) = gravity downward

Roll  = atan2(AccX, -AccZ)  → 0 when upright, + when tilted right
Pitch = atan2(AccY, -AccZ)  → neutral at user's calibrated hold angle
"""

import math
import time

ACCEL_RESOLUTION = 16384.0
GYRO_RESOLUTION = 16.0

STALE_TIMEOUT_S = 0.25
MAX_DT = 0.05
MIN_DT = 0.001


class TiltEstimator:
    """Bounded complementary filter producing roll/pitch from raw IMU data.

    Parameters
    ----------
    alpha : float
        Gyro trust weight in [0, 1]. Higher = smoother but slower response.
        Default 0.92 balances ~50ms latency with good jitter rejection.
    """

    def __init__(self, alpha: float = 0.92):
        self._alpha = 0.0
        self.alpha = alpha
        self._calibration: dict | None = None
        self._roll = 0.0
        self._pitch = 0.0
        self._last_ts: float | None = None
        self._initialized = False

    @property
    def alpha(self) -> float:
        """Gyro trust weight, clamped to [0, 1].

        Settable at runtime so persisted settings (smoothing_alpha) can be
        honored without recreating the estimator.
        """
        return self._alpha

    @alpha.setter
    def alpha(self, value: float) -> None:
        self._alpha = max(0.0, min(1.0, float(value)))

    def calibrate(self, acc_x: float, acc_y: float, acc_z: float) -> dict:
        """Capture current orientation as neutral. Returns calibration dict."""
        roll_offset = math.atan2(acc_x, -acc_z)
        pitch_offset = math.atan2(acc_y, -acc_z)
        self._calibration = {
            "roll_offset": roll_offset,
            "pitch_offset": pitch_offset,
            "acc_x": acc_x,
            "acc_y": acc_y,
            "acc_z": acc_z,
        }
        self._roll = 0.0
        self._pitch = 0.0
        self._initialized = True
        self._last_ts = None
        return dict(self._calibration)

    def load_calibration(self, cal: dict) -> bool:
        """Restore a previously saved calibration.

        Returns True if the calibration was applied. Dicts missing either
        required offset (partial/legacy/corrupt persisted state) are
        rejected without mutating anything, so ``update()`` can never hit a
        KeyError from a half-valid calibration.
        """
        if not isinstance(cal, dict) or "roll_offset" not in cal or "pitch_offset" not in cal:
            return False
        self._calibration = dict(cal)
        self._roll = 0.0
        self._pitch = 0.0
        self._initialized = True
        self._last_ts = None
        return True

    @property
    def is_calibrated(self) -> bool:
        return self._calibration is not None

    def reset_to_neutral(self) -> None:
        """Force outputs to zero (mode switch / disconnect)."""
        self._roll = 0.0
        self._pitch = 0.0
        self._last_ts = None

    def update(
        self,
        acc_x: float, acc_y: float, acc_z: float,
        gyro_x: float, gyro_y: float, gyro_z: float,
        timestamp: float,
    ) -> tuple[float, float]:
        """Process one IMU sample. Returns (roll_rad, pitch_rad) relative to calibration.

        All accelerometer values in raw device units (+-32768, res 16384/g).
        All gyro values in raw device units (+-32768, res 16/deg_s).
        timestamp in seconds (monotonic).
        """
        accel_roll = math.atan2(acc_x, -acc_z)
        accel_pitch = math.atan2(acc_y, -acc_z)

        if self._calibration:
            accel_roll -= self._calibration["roll_offset"]
            accel_pitch -= self._calibration["pitch_offset"]

        if not self._initialized or self._last_ts is None:
            self._roll = accel_roll
            self._pitch = accel_pitch
            self._last_ts = timestamp
            self._initialized = True
            return (self._roll, self._pitch)

        dt = timestamp - self._last_ts
        self._last_ts = timestamp

        if dt <= 0 or dt > MAX_DT:
            self._roll = accel_roll
            self._pitch = accel_pitch
            return (self._roll, self._pitch)

        gyro_roll_dps = gyro_x / GYRO_RESOLUTION
        gyro_pitch_dps = gyro_y / GYRO_RESOLUTION
        gyro_roll_rad = math.radians(gyro_roll_dps) * dt
        gyro_pitch_rad = math.radians(gyro_pitch_dps) * dt

        self._roll = self.alpha * (self._roll + gyro_roll_rad) + (1.0 - self.alpha) * accel_roll
        self._pitch = self.alpha * (self._pitch + gyro_pitch_rad) + (1.0 - self.alpha) * accel_pitch

        self._roll = max(-math.pi / 2, min(math.pi / 2, self._roll))
        self._pitch = max(-math.pi / 2, min(math.pi / 2, self._pitch))

        return (self._roll, self._pitch)

    def is_stale(self, now: float | None = None) -> bool:
        """True if no update received within STALE_TIMEOUT_S."""
        if self._last_ts is None:
            return True
        if now is None:
            now = time.monotonic()
        return (now - self._last_ts) > STALE_TIMEOUT_S

    @property
    def roll(self) -> float:
        return self._roll

    @property
    def pitch(self) -> float:
        return self._pitch

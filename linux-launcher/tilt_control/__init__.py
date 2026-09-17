"""Deck tilt steering: IMU-based complementary filter for LX/LY control."""

from .estimator import TiltEstimator
from .motion_reader import MotionReader
from .adapter import TiltAdapter
from .settings import TiltSettings, ControlMode
from .sensor_transport import (
    ConsoleSensorTransport,
    acceleration_from_tilt,
    format_sensor_set,
    NEUTRAL_ACCELERATION,
    GRAVITY,
)

__all__ = [
    "TiltEstimator",
    "MotionReader",
    "TiltAdapter",
    "TiltSettings",
    "ControlMode",
    "ConsoleSensorTransport",
    "acceleration_from_tilt",
    "format_sensor_set",
    "NEUTRAL_ACCELERATION",
    "GRAVITY",
]

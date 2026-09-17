"""Injectable ADB adapter for screencap and input tap.

Provides a real implementation and a dry-run tracer for testing without
guest mutation. All commands use explicit -P port and -s serial.
"""

from __future__ import annotations

import os
import struct
import subprocess
import time
from abc import ABC, abstractmethod
from typing import NamedTuple


class Color(NamedTuple):
    r: int
    g: int
    b: int


class ScreenCapture(NamedTuple):
    width: int
    height: int
    pixel_data: bytes
    capture_time_ms: float


class AdbAdapter(ABC):
    @abstractmethod
    def screencap_raw(self) -> ScreenCapture:
        """Capture raw RGBA screencap. Raises on failure."""

    @abstractmethod
    def input_tap(self, x: int, y: int) -> bool:
        """Send input tap at guest coordinates. Returns success."""

    @abstractmethod
    def input_key(self, keycode: int) -> bool:
        """Send input keyevent. Returns success."""


class RealAdbAdapter(AdbAdapter):
    def __init__(self, adb_path: str, serial: str, port: int = 5038):
        self._adb = adb_path
        self._serial = serial
        self._port = port
        self._env = {**os.environ, "ANDROID_ADB_SERVER_PORT": str(port)}

    def _run(self, args: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._adb, "-P", str(self._port), "-s", self._serial] + args,
            capture_output=True, timeout=timeout, env=self._env, check=False,
        )

    def screencap_raw(self) -> ScreenCapture:
        t0 = time.monotonic()
        result = self._run(["shell", "screencap"], timeout=10)
        elapsed = (time.monotonic() - t0) * 1000
        if result.returncode != 0:
            raise RuntimeError(
                f"screencap failed rc={result.returncode}: "
                f"{result.stderr.decode(errors='replace')[:200]}"
            )
        data = result.stdout
        if len(data) < 12:
            raise RuntimeError(f"screencap too short: {len(data)} bytes")
        width, height, _fmt = struct.unpack_from("<III", data, 0)
        if width <= 0 or height <= 0 or width > 8192 or height > 8192:
            raise RuntimeError(f"screencap invalid dimensions: {width}x{height}")
        pixel_data = data[12:]
        expected = width * height * 4
        if len(pixel_data) < expected:
            raise RuntimeError(
                f"screencap pixel data truncated: got {len(pixel_data)}, "
                f"expected {expected}"
            )
        return ScreenCapture(width, height, pixel_data, elapsed)

    def input_tap(self, x: int, y: int) -> bool:
        result = self._run(["shell", "input", "tap", str(x), str(y)], timeout=5)
        return result.returncode == 0

    def input_key(self, keycode: int) -> bool:
        result = self._run(["shell", "input", "keyevent", str(keycode)], timeout=5)
        return result.returncode == 0


class DryRunAdapter(AdbAdapter):
    """Records all operations without touching the guest."""

    def __init__(self, fixture_capture: ScreenCapture | None = None):
        self._fixture = fixture_capture
        self.tap_log: list[tuple[int, int, float]] = []
        self.key_log: list[tuple[int, float]] = []
        self.screencap_count = 0

    def screencap_raw(self) -> ScreenCapture:
        self.screencap_count += 1
        if self._fixture is None:
            raise RuntimeError("DryRunAdapter: no fixture loaded")
        return self._fixture

    def input_tap(self, x: int, y: int) -> bool:
        self.tap_log.append((x, y, time.monotonic()))
        return True

    def input_key(self, keycode: int) -> bool:
        self.key_log.append((keycode, time.monotonic()))
        return True

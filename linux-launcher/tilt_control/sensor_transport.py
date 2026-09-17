"""Deck IMU -> Android accelerometer injection over the emulator console.

Why this module exists
----------------------
The game ships two independent driving inputs (verified statically from the
original v1.0.23 base.apk + armeabi-v7a libtrueaxis.so, hashes in
backups/usb-20260910T005449Z/SHA256SUMS):

* Gamepad path: True Axis InputManagerCompat joystick (AXIS_X/AXIS_Y, ...,
  JaypadIsSupported). Our persistent UHID "JCS2 Virtual Xbox Controller"
  serves this path. The game's Settings toggle ``m_bAllowJoysticks``
  enables/disables it (persisted in options.bin, binary layout unknown).
* Native tilt path: NDK ``ASensorManager_getDefaultSensor`` /
  ``ASensorEventQueue_enableSensor`` G-sensor (``Game::UpdateGSensor``,
  ``GSensor::Update``, ``UiFormTiltCalibration`` with Accept/Cancel/Default/
  Calibrate buttons). The emulator provides this sensor
  (``hw.accelerometer = yes``, ``hw.sensors.orientation = yes``).

The previous host tilt implementation only emitted synthetic virtual
LX/LY through the UHID gamepad. With the game's Gamepad switch OFF the
guest ignores every UHID axis, so the in-game Tilt calibration screen saw
no sensor and physical Deck tilt did nothing. This module feeds the
*native* accelerometer instead, and the runner keeps that feed live in
EVERY View mode whenever the Deck IMU is owned: the game's own Gamepad
toggle alone selects the driving source (OFF = native tilt, ON = sticks),
so the user never needs a double switch. View Tilt additionally produces
virtual LX/LY (authoritative when the gamepad is ON); View GAMEPAD leaves
the physical stick authoritative. When the host IMU is not owned the guest
sensor is left strictly alone at its emulator default.

Transport
---------
The emulator console (same ``127.0.0.1:5594`` + ``~/.emulator_console_auth_token``
the runner already uses for ``rotate``) accepts::

    sensor set acceleration <x>:<y>:<z>

with values in m/s^2. One persistent authenticated TCP connection is kept
open for the whole Tilt session: no ``adb`` process is spawned per frame,
and the existing short-lived ``rotate`` use is untouched.

Mapping
-------
``acceleration_from_tilt(roll, pitch)`` converts the estimator's
calibration-relative roll/pitch (radians) to a synthetic gravity vector::

    ax = sin(roll  * gain_lx) * g
    ay = sin(pitch * gain_ly) * g
    az = cos(roll) * cos(pitch) * g

Neutral (roll = pitch = 0) injects ``(0, 0, g)``. The game's own Tilt
calibration stores whatever is injected at calibration time as its
neutral, so host calibration (Deck hold) and game calibration (injected
vector) compose: each zeroes its own stage. Sign note: roll > 0 (Deck
tilted right) yields ax > 0 and pitch > 0 (Deck tilted forward) yields
ay > 0. If the live car steers/pitches backwards, the physical fix is a
sign flip here (or negative gain), to be confirmed by the user's physical
test -- direction signs are NOT proven offline.

Only ``linux-launcher/runner.py`` (InputRouter) and ``tilt_control``
own this transport. No APK, guest image, driver, or global config is
touched. Device access is read-only Deck IMU + a guest-local console TCP
socket.
"""

from __future__ import annotations

import math
import socket
import time
from pathlib import Path

GRAVITY = 9.81
NEUTRAL_ACCELERATION: tuple[float, float, float] = (0.0, 0.0, GRAVITY)

CONSOLE_HOST = "127.0.0.1"
CONSOLE_PORT = 5594
CONSOLE_TIMEOUT_S = 2.0
MIN_SEND_INTERVAL_S = 1.0 / 60.0  # at most ~60 sensor updates/sec
MIN_VECTOR_DELTA = 0.02  # m/s^2; below this the guest already has our value


def acceleration_from_tilt(
    roll_rad: float,
    pitch_rad: float,
    gain_lx: float = 1.0,
    gain_ly: float = 1.0,
    g: float = GRAVITY,
) -> tuple[float, float, float]:
    """Map calibration-relative tilt angles to an Android gravity vector.

    Gains scale the angle (not the output), so ``gain < 1`` desensitizes
    without clipping the neutral region and a negative gain flips that
    axis if the live test proves a sign is backwards.
    """
    roll = roll_rad * gain_lx
    pitch = pitch_rad * gain_ly
    ax = math.sin(roll) * g
    ay = math.sin(pitch) * g
    az = math.cos(roll_rad) * math.cos(pitch_rad) * g
    return (ax, ay, az)


def format_sensor_set(vector: tuple[float, float, float]) -> bytes:
    """Render one emulator-console ``sensor set`` command line."""
    ax, ay, az = (float(v) for v in vector)
    for v in (ax, ay, az):
        if not math.isfinite(v) or abs(v) > 100.0:
            raise ValueError(f"refusing implausible acceleration value: {vector!r}")
    if ax == 0.0 and ay == 0.0 and az == 0.0:
        # Free-fall is never a legitimate Deck hold; emitting it would
        # corrupt the game's Tilt calibration neutral. Every mapping output
        # has magnitude ~g, so this only fires on a programming error.
        raise ValueError("refusing free-fall zero vector: neutral must carry +g")
    return f"sensor set acceleration {ax:.3f}:{ay:.3f}:{az:.3f}\n".encode()


def console_token_path() -> Path:
    """Local emulator console credential; read, never logged or stored."""
    return Path.home() / ".emulator_console_auth_token"


class ConsoleSensorTransport:
    """One persistent authenticated emulator-console sensor channel.

    ``connect_fn`` injects a fake ``(host, port, timeout) -> socket`` in
    tests; production uses ``socket.create_connection``. The socket object
    only needs ``sendall``, ``recv`` and ``close``.
    """

    def __init__(
        self,
        host: str = CONSOLE_HOST,
        port: int = CONSOLE_PORT,
        token_path: Path | None = None,
        connect_fn=None,
        time_fn=time.monotonic,
    ):
        self.host = host
        self.port = port
        self.token_path = token_path or console_token_path()
        self._connect_fn = connect_fn or (lambda h, p, t: socket.create_connection((h, p), timeout=t))
        self._time = time_fn
        self._sock = None
        self._last_sent: tuple[float, float, float] | None = None
        self._last_at: float = 0.0
        self.last_error: str | None = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        """Open + authenticate the console session. Bounded, fail-closed."""
        if self._sock is not None:
            return True
        try:
            sock = self._connect_fn(self.host, self.port, CONSOLE_TIMEOUT_S)
        except OSError as error:
            self.last_error = f"console connect failed: {error}"
            return False
        try:
            greeting = self._read_response(sock)
            if b"Authentication required" in greeting:
                try:
                    token = self.token_path.read_text(encoding="utf-8").strip()
                except OSError as error:
                    self.last_error = f"console token unreadable: {error}"
                    self._close(sock)
                    return False
                if not token or any(ch.isspace() for ch in token):
                    self.last_error = "console token malformed"
                    self._close(sock)
                    return False
                sock.sendall(("auth " + token + "\n").encode())
                reply = self._read_response(sock)
                if b"OK" not in reply:
                    self.last_error = "console auth rejected"
                    self._close(sock)
                    return False
            self._sock = sock
            self._last_sent = None
            self._last_at = 0.0
            self.last_error = None
            return True
        except OSError as error:
            self.last_error = f"console handshake failed: {error}"
            self._close(sock)
            return False

    def set_acceleration(
        self,
        vector: tuple[float, float, float],
        *,
        force: bool = False,
    ) -> bool:
        """Push one accelerometer vector; throttled unless ``force``.

        Returns True when the guest acknowledged (or when throttling
        skipped a redundant update). Any transport failure closes the
        channel so the next push reconnects with backoff owned by the
        caller; callers must never spin this per frame on failure.
        """
        if self._sock is None and not self.connect():
            return False
        now = self._time()
        if not force and self._last_sent is not None:
            if now - self._last_at < MIN_SEND_INTERVAL_S:
                return True
            dist = math.dist(vector, self._last_sent)
            if dist < MIN_VECTOR_DELTA:
                return True
        try:
            command = format_sensor_set(vector)
        except ValueError as error:
            self.last_error = str(error)
            return False
        try:
            assert self._sock is not None
            self._sock.sendall(command)
            reply = self._read_response(self._sock)
        except OSError as error:
            self.last_error = f"sensor push failed: {error}"
            self.close()
            return False
        if b"OK" not in reply:
            self.last_error = f"console rejected sensor set: {reply[:120]!r}"
            return False
        self._last_sent = tuple(float(v) for v in vector)
        self._last_at = now
        self.last_error = None
        return True

    def sensor_status(self) -> str | None:
        """Best-effort ``sensor status`` dump for diagnostics (bounded)."""
        if self._sock is None and not self.connect():
            return None
        try:
            assert self._sock is not None
            self._sock.sendall(b"sensor status\n")
            return self._read_response(self._sock).decode(errors="replace")
        except OSError as error:
            self.last_error = f"sensor status failed: {error}"
            self.close()
            return None

    def close(self) -> None:
        """Drop the console session; values persist in the guest."""
        sock, self._sock = self._sock, None
        self._close(sock)

    @staticmethod
    def _close(sock) -> None:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _read_response(sock, limit: int = 65536) -> bytes:
        data = bytearray()
        while len(data) < limit:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if data.endswith(b"OK\r\n") or data.endswith(b"OK\n") or b"OK" in data.splitlines()[-1:]:
                break
            if any(line.startswith(b"KO") for line in data.splitlines()):
                break
        return bytes(data)

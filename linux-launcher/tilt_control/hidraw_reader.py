"""Steam-active IMU source: raw HID reader + Deck-report parser (no decode claims).

Authoritative layout: Linux ``drivers/hid/hid-steam.c`` (Deck IMU support;
``ID_CONTROLLER_DECK_STATE = 9``) + SDL ``controller_structs.h``
(``SteamDeckStatePacket_t``) + SDL triton sensor scaling.

While the Steam client runs, hid-steam tears down the IMU evdev node
(``steam_client_ll_open`` → ``steam_sensors_unregister``; re-registered on
``steam_client_ll_close`` — exactly the observed vanish/restore), but the
HID function ``0003:28DE:1205.0004`` (PHYS ``usb-0000:04:00.4-3/input2``,
same physical function as the idle-time evdev node) keeps streaming
64-byte reports at ~250 Hz with ``user:deck:rw-`` access.

VALIDATED on-device (Sept-14, 1250-frame Steam-active capture):
report = 64-byte ValveInReport, version=1, type=9, length=64; u32 LE
sequence at bytes 4-7 incrementing once per frame (~250 Hz); buttons /
pads / triggers / sticks / pressures plausible at rest.

NOT validated: the MOTION fields. Payload accel (bytes 28-33) and gyro
(34-39) on this interface report rest-impossible values (gyro ≈ -11878 /
-4224 LSB at rest; accel norm inconsistent with the idle evdev gravity
vector from the same stationary Deck), and the report carries NO sensor
timestamp (the driver synthesizes +4000 us per report). So this module
parses and validates FORMAT (invariants below) but performs NO accel/gyro
decode: feeding the estimator from unvalidated channels would be a fake
fix. Rotation validation (user tilts while capturing) is still required.

Documented scales (for USE once validated): accel ±2 g @ 16384 LSB/g;
gyro ±2000 dps @ 16 LSB/dps (``STEAM_DECK_ACCEL_RES_PER_G``,
``STEAM_DECK_GYRO_RES_PER_DPS``). Kernel axis remap for the evdev path:
ABS_X=data+24, ABS_Z=-data+26, ABS_Y=data+28, ABS_RX=data+30,
ABS_RZ=-data+32, ABS_RY=data+34.
"""

import json
import os
import time

HIDRAW_DEFAULT_PATH = "/dev/hidraw2"
REPORT_LEN = 64

# Identity of the Steam-active IMU source (stable across Steam states and
# evdev re-enumeration; NEVER hardcode /dev/hidrawN — numbers shift).
HID_BUS_ROOT = "/sys/bus/hid/devices"
DEV_ROOT = "/dev"
IMU_VID = "28de"
IMU_PID = "1205"
IMU_PHYS_SUFFIX = "/input2"  # same physical function as the evdev node

# Deck-state report identity (frame offsets include the 4-byte header).
DECK_REPORT_VERSION = 1
DECK_REPORT_TYPE = 9  # ID_CONTROLLER_DECK_STATE
DECK_REPORT_LEN = 64
DECK_VID = "28de"  # documented context: HID function 0003:28DE:1205.0004
DECK_PID = "1205"

# Documented scales (APPLY ONLY after rotation validation).
ACCEL_LSB_PER_G = 16384  # +/- 2 g
GYRO_LSB_PER_DPS = 16  # +/- 2000 dps

# Field offsets (s16 LE unless noted) per hid-steam.c deck table.
OFF_SEQ = 4
OFF_ACCEL = (28, 30, 32)
OFF_GYRO = (34, 36, 38)
OFF_STICKS = (52, 54, 56, 58)


def parse_deck_report(frame: bytes) -> dict:
    """Parse one 64-byte Deck-state report into raw field dict (no scaling).

    Raises ValueError on length/version/type mismatch. Returned accel/gyro
    are RAW s16 device values — NOT validated motion (see module docstring).
    """
    if len(frame) != REPORT_LEN:
        raise ValueError(f"deck report must be {REPORT_LEN} bytes, got {len(frame)}")
    import struct as _struct
    version = _struct.unpack_from("<H", frame, 0)[0]
    rtype = frame[2]
    rlen = frame[3]
    if version != DECK_REPORT_VERSION or rtype != DECK_REPORT_TYPE or rlen != DECK_REPORT_LEN:
        raise ValueError(
            f"not a deck-state report: ver={version} type={rtype} len={rlen}")
    seq = _struct.unpack_from("<I", frame, OFF_SEQ)[0]
    return {
        "seq": seq,
        "buttons": frame[8:16].hex(),
        "accel_raw": [_struct.unpack_from("<h", frame, off)[0] for off in OFF_ACCEL],
        "gyro_raw": [_struct.unpack_from("<h", frame, off)[0] for off in OFF_GYRO],
        "sticks_raw": [_struct.unpack_from("<h", frame, off)[0] for off in OFF_STICKS],
    }


def seq_is_monotonic(parsed_reports: list[dict]) -> bool:
    """True when report seq increments by exactly 1 per frame (no drops)."""
    seqs = [report["seq"] for report in parsed_reports]
    return all(b - a == 1 for a, b in zip(seqs, seqs[1:]))


def _hid_uevent_value(device_dir: str, key: str) -> str:
    """Read one KEY=value line from a HID device uevent file (never raises)."""
    try:
        with open(os.path.join(device_dir, "uevent"), "r",
                  encoding="utf-8", errors="replace") as stream:
            for line in stream.read(2048).splitlines():
                if line.startswith(key + "="):
                    return line[len(key) + 1:].strip()
    except OSError:
        pass
    return ""


def resolve_imu_hidraw(hid_root: str = HID_BUS_ROOT,
                       dev_root: str = DEV_ROOT) -> tuple[str | None, str]:
    """Resolve the IMU hidraw /dev node BY IDENTITY (never raises).

    Selects the HID device with VID 28DE / PID 1205 whose PHYS ends in
    ``/input2`` (the IMU function, same physical function as the idle
    evdev node) AND that currently exposes a ``hidraw`` child. Returns
    (devnode path, detail). Miss detail names what was found instead
    (e.g. input2 function without hidraw = Steam idle state).
    """
    import glob as _glob
    try:
        entries = sorted(_glob.glob(os.path.join(hid_root, "*")))
    except OSError:
        return None, "hid bus unreadable"
    seen: list[str] = []
    for entry in entries:
        base = os.path.basename(entry)
        parts = base.split(":")
        if len(parts) != 3:
            continue
        _bus, vidpid = parts[0], parts[1:]
        vidpid_join = ":".join(vidpid).lower()
        if IMU_VID not in vidpid_join or IMU_PID not in vidpid_join:
            continue
        phys = _hid_uevent_value(entry, "HID_PHYS")
        try:
            children = sorted(os.listdir(entry))
        except OSError:
            continue
        hidraw_kids = [c for c in children
                       if c.startswith("hidraw") and len(c) > len("hidraw")]
        # Direct child names appear as hidraw/hidrawN links; list via subdir.
        try:
            hidraw_dir = os.path.join(entry, "hidraw")
            hidraw_kids = sorted(os.listdir(hidraw_dir))
        except OSError:
            hidraw_kids = []
        seen.append(f"{base} phys={phys or '?'} "
                    f"hidraw={','.join(hidraw_kids) or '-'} "
                    f"input={'yes' if 'input' in children else 'no'}")
        if phys.endswith(IMU_PHYS_SUFFIX) and hidraw_kids:
            node = os.path.join(dev_root, hidraw_kids[0])
            detail = (f"{base} {phys} -> {node} "
                      f"({'; '.join(seen)})")
            return node, detail
    return None, "no input2 hidraw function: " + ("; ".join(seen) or "no 28de:1205 HID devices")


def check_stream_ready(device_path: str, timeout_s: float = 30.0,
                       need_valid: int = 5) -> tuple[bool, str]:
    """Open read-only/non-blocking and require valid framed reports.

    Passive only: O_RDONLY|O_NONBLOCK, sequential reads, zero feature
    writes, zero exclusive grabs (hidraw offers no grab ioctl path used
    here). Requires ``need_valid`` consecutive parseable Deck reports
    with advancing seq. Returns (ok, detail).
    """
    reader = HidrawFrameReader(device_path)
    if not reader.open():
        return False, reader.last_error or "open failed"
    try:
        deadline = time.monotonic() + max(1.0, timeout_s)
        parsed: list[dict] = []
        while time.monotonic() < deadline and len(parsed) < need_valid:
            for _mono_ts, frame in reader.read_frames():
                try:
                    parsed.append(parse_deck_report(frame))
                except ValueError:
                    return False, "framing rejected: not ver=1/type=9/len=64"
            time.sleep(0.01)
        if len(parsed) < need_valid:
            return False, f"only {len(parsed)}/{need_valid} valid frames in {timeout_s}s"
        seqs = [p["seq"] for p in parsed]
        if seqs[-1] <= seqs[0]:
            return False, "seq not advancing (stale source)"
        return True, (f"{len(parsed)} valid frames, "
                      f"seq {seqs[0]}->{seqs[-1]} @ {device_path}")
    finally:
        reader.close()


class HidrawFrameReader:
    """Non-blocking raw HID frame reader (never raises on stream errors)."""

    def __init__(self, device_path: str = HIDRAW_DEFAULT_PATH):
        self._device_path = device_path
        self._fd: int | None = None
        self.last_error: str | None = None
        self.frames_read = 0
        self.short_frames = 0

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def open(self) -> bool:
        if self._fd is not None:
            return True
        try:
            self._fd = os.open(self._device_path, os.O_RDONLY | os.O_NONBLOCK)
            self.last_error = None
            return True
        except OSError as error:
            self.last_error = (
                f"hidraw open failed: {self._device_path}: "
                f"{error.strerror or error} (errno {error.errno})")
            return False

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def read_frames(self) -> list[tuple[float, bytes]]:
        """Drain all pending complete frames; empty list when none/closed."""
        if self._fd is None:
            return []
        frames: list[tuple[float, bytes]] = []
        try:
            while True:
                try:
                    data = os.read(self._fd, REPORT_LEN * 32)
                except BlockingIOError:
                    break
                if not data:
                    break
                offset = 0
                while offset + REPORT_LEN <= len(data):
                    chunk = data[offset:offset + REPORT_LEN]
                    offset += REPORT_LEN
                    if len(chunk) != REPORT_LEN:
                        self.short_frames += 1
                        continue
                    self.frames_read += 1
                    frames.append((time.monotonic(), chunk))
                if len(data) % REPORT_LEN:
                    self.short_frames += 1
        except OSError as error:
            self.last_error = (
                f"hidraw read failed: {self._device_path}: "
                f"{error.strerror or error} (errno {error.errno})")
            self.close()
        return frames


def capture_to_ndjson(reader: HidrawFrameReader, duration_s: float,
                      stream) -> int:
    """Capture frames for ``duration_s`` writing {"t_mono":..., "hex":...} rows."""
    deadline = time.monotonic() + max(0.0, duration_s)
    count = 0
    while time.monotonic() < deadline:
        for mono_ts, frame in reader.read_frames():
            stream.write(json.dumps({"t_mono": mono_ts, "hex": frame.hex()})
                         + "\n")
            count += 1
        time.sleep(0.005)
    return count


def analyze_variance(hex_rows: list[str]) -> dict[int, int]:
    """Per-byte-offset distinct-value counts (finds motion-tracking fields).

    Pure function over captured hex rows: returns {offset: distinct_count}.
    A physical-rotation capture must show exactly the motion fields varying
    fast (gyro) vs slow 1 g-tracking (accel); static fields stay at 1.
    """
    frames = [bytes.fromhex(row) for row in hex_rows]
    result: dict[int, int] = {}
    width = max((len(frame) for frame in frames), default=0)
    for offset in range(width):
        values = {frame[offset] for frame in frames if len(frame) > offset}
        result[offset] = len(values)
    return result

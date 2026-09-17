"""Non-blocking evdev reader for Steam Deck motion sensor.

Opens /dev/input/event9 (or discovered node) read-only without any
exclusive grab, mode change, or ioctl mutation. Uses O_NONBLOCK for
non-blocking reads suitable for polling loops.

Device identity verified:
  Name: "Steam Deck Motion Sensors"
  Vendor: 28de, Product: 1205
  Sysfs: /devices/pci0000:00/0000:00:08.1/0000:04:00.4/usb3/3-3/3-3:1.2
  Handlers: event9 js1
  ABS capabilities: 0x3f (axes 0-5)
"""

import glob
import os
import struct
import time

EV_ABS = 3
EV_SYN = 0
SYN_REPORT = 0
SYN_DROPPED = 3
REQUIRED_AXES = (0, 1, 2, 3, 4, 5)  # acc xyz + gyro xyz: no sample before all seen
INPUT_EVENT_STRUCT = struct.Struct("llHHi")  # timeval(sec,usec), type, code, value

MOTION_SENSOR_NAME = "Steam Deck Motion Sensors"
STEAM_DECK_VID = "28de"
STEAM_DECK_PID = "1205"

SYSFS_INPUT_ROOT = "/sys/class/input"
DEV_INPUT_ROOT = "/dev/input"

# Capability evidence (Sept-14, host event8): ev=0x19 (SYN|ABS|MSC, no KEY),
# abs=0x3f (axes 0-5: acc xyz + gyro xyz). Tier-3 fallback requires the 6
# ABS axes and NO EV_KEY bit so keyboards/mice/sticks can never match.
EV_KEY_BIT = 0x02
ABS_AXES_0_5_MASK = 0x3F

# Bounded miss diagnostics: snapshot text is capped so a 5 s reprobe miss
# cannot grow the log (only sysfs names/ids/caps + devnode access — never
# environment, cmdlines, or anything sensitive).
SNAPSHOT_MAX_CHARS = 2048


def _sysfs_text(path: str, limit: int = 256) -> str:
    """Bounded sysfs read; empty string on any failure (never raises)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            return stream.read(limit).strip()
    except OSError:
        return ""


def _iter_input_nodes(sysfs_root: str = SYSFS_INPUT_ROOT) -> list[dict]:
    """Inventory every /sys/class/input/event* node (never raises).

    Each entry: node (eventN), name, vendor, product, abs/ev cap hex,
    dev_read/dev_write (os.access on /dev/input/eventN). Pure sysfs +
    access probe — no environment or process data.
    """
    nodes: list[dict] = []
    try:
        entries = sorted(glob.glob(os.path.join(sysfs_root, "event*")))
    except OSError:
        return nodes
    for entry in entries:
        node = os.path.basename(entry)
        device_dir = os.path.join(entry, "device")
        name = _sysfs_text(os.path.join(device_dir, "name"))
        vendor = _sysfs_text(os.path.join(device_dir, "id", "vendor")).lower()
        product = _sysfs_text(os.path.join(device_dir, "id", "product")).lower()
        abs_caps = _sysfs_text(os.path.join(device_dir, "capabilities", "abs"))
        ev_caps = _sysfs_text(os.path.join(device_dir, "capabilities", "ev"))
        devnode = os.path.join(DEV_INPUT_ROOT, node)
        try:
            dev_read = os.access(devnode, os.R_OK)
            dev_write = os.access(devnode, os.W_OK)
        except OSError:
            dev_read = dev_write = False
        nodes.append({
            "node": node,
            "name": name,
            "vendor": vendor,
            "product": product,
            "abs": abs_caps,
            "ev": ev_caps,
            "dev_read": dev_read,
            "dev_write": dev_write,
        })
    return nodes


def _norm_id(value: str) -> str:
    return value.strip().lower().removeprefix("0x").zfill(4)


def _caps_int(text: str) -> int | None:
    try:
        return int(text.strip(), 16)
    except (ValueError, TypeError):
        return None


def discovery_snapshot(sysfs_root: str = SYSFS_INPUT_ROOT,
                       max_chars: int = SNAPSHOT_MAX_CHARS) -> str:
    """One-line-per-node sysfs inventory for discovery-miss diagnostics."""
    parts: list[str] = []
    nodes = _iter_input_nodes(sysfs_root)
    for entry in nodes:
        vid = _norm_id(entry["vendor"]) if entry["vendor"] else "?"
        pid = _norm_id(entry["product"]) if entry["product"] else "?"
        rw = ("r" if entry["dev_read"] else "-") + ("w" if entry["dev_write"] else "-")
        parts.append(
            f"{entry['node']}:{entry['name'] or '?'}|{vid}:{pid}"
            f"|abs={entry['abs'] or '?'}|ev={entry['ev'] or '?'}|dev={rw}")
    text = f"nodes={len(nodes)} " + ";".join(parts)
    return text[:max_chars]


def _discover_motion_node(sysfs_root: str = SYSFS_INPUT_ROOT
                          ) -> tuple[str | None, str, str]:
    """Tiered IMU discovery. Returns (devnode path|None, tier, snapshot).

    Tiers (first match wins; evidence before broad matching):
      tier1  exact name + 28de:1205 (proven host identity).
      tier2  exact name, any VID:PID (re-enumeration / PID change).
      tier3  VID 28de + ABS axes 0-5 present + NO EV_KEY bit
             (rename-proof; keyboards/mice/sticks carry EV_KEY).
    Miss returns tier "miss" with the sysfs snapshot for logging.
    """
    nodes = _iter_input_nodes(sysfs_root)
    snapshot = discovery_snapshot(sysfs_root)
    tier1 = tier2 = tier3 = None
    for entry in nodes:
        vid = _norm_id(entry["vendor"]) if entry["vendor"] else ""
        pid = _norm_id(entry["product"]) if entry["product"] else ""
        is_deck_ids = (vid == STEAM_DECK_VID and pid == STEAM_DECK_PID)
        if entry["name"] == MOTION_SENSOR_NAME and is_deck_ids and tier1 is None:
            tier1 = f"{DEV_INPUT_ROOT}/{entry['node']}"
        if entry["name"] == MOTION_SENSOR_NAME and tier2 is None:
            tier2 = f"{DEV_INPUT_ROOT}/{entry['node']}"
        abs_caps = _caps_int(entry["abs"] or "")
        ev_caps = _caps_int(entry["ev"] or "")
        if (vid == STEAM_DECK_VID and abs_caps is not None and ev_caps is not None
                and (abs_caps & ABS_AXES_0_5_MASK) == ABS_AXES_0_5_MASK
                and not (ev_caps & EV_KEY_BIT) and tier3 is None):
            tier3 = f"{DEV_INPUT_ROOT}/{entry['node']}"
    if tier1 is not None:
        return tier1, "tier1", snapshot
    if tier2 is not None:
        return tier2, "tier2", snapshot
    if tier3 is not None:
        return tier3, "tier3", snapshot
    return None, "miss", snapshot


def _find_motion_event_node(sysfs_root: str = SYSFS_INPUT_ROOT) -> str | None:
    """Discover the evdev node for the Deck's motion sensor by sysfs identity."""
    path, _tier, _snapshot = _discover_motion_node(sysfs_root)
    return path


class MotionSample:
    """One complete IMU frame (all 6 axes from one SYN_REPORT group)."""
    __slots__ = ("acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z", "timestamp")

    def __init__(self):
        self.acc_x: int = 0
        self.acc_y: int = 0
        self.acc_z: int = 0
        self.gyro_x: int = 0
        self.gyro_y: int = 0
        self.gyro_z: int = 0
        self.timestamp: float = 0.0


class MotionReader:
    """Non-blocking, non-exclusive evdev reader for the Deck IMU.

    Usage::

        reader = MotionReader()
        if reader.open():
            samples = reader.read_samples()  # returns list of MotionSample
            reader.close()
    """

    def __init__(self, device_path: str | None = None,
                 sysfs_root: str = SYSFS_INPUT_ROOT):
        self._device_path = device_path
        self._sysfs_root = sysfs_root
        self._fd: int | None = None
        self._partial = MotionSample()
        self._has_partial = False
        # Last-known value per ABS code + codes seen in the open group.
        # evdev suppresses unchanged axes inside a SYN group, so a group
        # carrying only X must NOT emit Y/Z/gyro as 0 (Sept-14 capture:
        # 73% of stillness frames carried literal zeros). Missing axes
        # hold their last value; only a never-seen axis stays 0.
        self._last_axes: dict[int, int] = {}
        self._seen_axes: set[int] = set()
        self._seen_ever: set[int] = set()
        self.last_error: str | None = None
        self.match_tier: str | None = None
        self.last_snapshot: str | None = None

    @property
    def device_path(self) -> str | None:
        return self._device_path

    @property
    def fd(self) -> int | None:
        return self._fd

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def _reset_stream_state(self) -> None:
        """Drop all framing/held axis state (disconnect/reopen/drop).

        After close, reopen, or SYN_DROPPED the kernel-side axis values
        are uncertain, so held values must not survive: the next emitted
        sample waits until every required axis has been seen fresh
        (see read_samples). Never raises.
        """
        self._partial = MotionSample()
        self._has_partial = False
        self._last_axes = {}
        self._seen_axes = set()
        self._seen_ever = set()

    def open(self) -> bool:
        """Open the motion sensor node. Returns True on success.

        Failures record a human-readable reason in ``last_error``
        (discovery miss vs OS errno) so callers can log WHY the IMU is
        not owned instead of a bare False. A discovery miss also stores
        the sysfs inventory in ``last_snapshot`` (names/ids/caps +
        devnode access only). ``match_tier`` names the discovery tier
        (tier1/tier2/tier3) on success, "miss" on discovery failure.
        Error and snapshot are cleared on success.
        """
        if self._fd is not None:
            return True
        if self._device_path is not None:
            path, tier, snapshot = self._device_path, "explicit", ""
        else:
            path, tier, snapshot = _discover_motion_node(self._sysfs_root)
        if path is None:
            self.match_tier = "miss"
            self.last_snapshot = snapshot
            self.last_error = (
                "motion sensor node not found: no evdev device named "
                f"'{MOTION_SENSOR_NAME}' ({STEAM_DECK_VID}:{STEAM_DECK_PID})"
                f" | {snapshot}"
            )
            return False
        try:
            self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            self._device_path = path
            self.last_error = None
            self.last_snapshot = snapshot or None
            self.match_tier = tier
            self._reset_stream_state()
            return True
        except OSError as error:
            self.last_error = f"motion sensor open failed: {path}: {error.strerror or error} (errno {error.errno})"
            self.match_tier = tier if tier != "explicit" else None
            return False

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._reset_stream_state()

    def read_samples(self) -> list[MotionSample]:
        """Read all available complete IMU frames without blocking.

        Returns a list of MotionSample. Empty list if no data or device
        closed. Coalesces: only returns samples delimited by SYN_REPORT,
        and only after every required axis (acc xyz + gyro xyz) has been
        seen at least once — no sample is emitted with uninitialized
        axes. SYN_DROPPED or close/reopen fully resets framing state.
        No timestamps are synthesized: kernel stamps are only re-based
        onto ``time.monotonic()`` (see below), and silence yields [].

        Timestamps are re-based onto ``time.monotonic()``: evdev reports
        kernel wall-clock times, and mixing those with ``time.monotonic()``
        in staleness checks would make every sample look instantly stale.
        Re-basing each batch against the newest event keeps both the filter
        dt and stale detection on a single monotonic clock (error is bounded
        by the read/batch latency, milliseconds at 250 Hz).
        """
        if self._fd is None:
            return []
        samples: list[MotionSample] = []
        event_size = INPUT_EVENT_STRUCT.size
        try:
            data = os.read(self._fd, event_size * 200)
        except BlockingIOError:
            return []
        except OSError as error:
            self.last_error = f"motion sensor read failed: {self._device_path}: {error.strerror or error} (errno {error.errno})"
            self.close()
            return []

        newest_kernel_ts: float | None = None
        offset = 0
        while offset + event_size <= len(data):
            sec, usec, ev_type, code, value = INPUT_EVENT_STRUCT.unpack_from(data, offset)
            offset += event_size

            if ev_type == EV_ABS:
                self._has_partial = True
                ts = sec + usec / 1_000_000.0
                newest_kernel_ts = ts
                self._partial.timestamp = ts
                self._last_axes[code] = value
                self._seen_axes.add(code)
                self._seen_ever.add(code)
                if code == 0:
                    self._partial.acc_x = value
                elif code == 1:
                    self._partial.acc_y = value
                elif code == 2:
                    self._partial.acc_z = value
                elif code == 3:
                    self._partial.gyro_x = value
                elif code == 4:
                    self._partial.gyro_y = value
                elif code == 5:
                    self._partial.gyro_z = value
            elif ev_type == EV_SYN and code == SYN_DROPPED:
                # Kernel dropped events: framing state is uncertain.
                # Full reset; emission resumes only after all required
                # axes are re-seen fresh (no stale holds, no zeros).
                self.last_error = (
                    f"motion sensor stream dropped events on {self._device_path}; "
                    "resynchronizing")
                self._reset_stream_state()
            elif ev_type == EV_SYN and code == SYN_REPORT and self._has_partial:
                for missing, attr in ((0, "acc_x"), (1, "acc_y"),
                                      (2, "acc_z"), (3, "gyro_x"),
                                      (4, "gyro_y"), (5, "gyro_z")):
                    if missing not in self._seen_axes and missing in self._last_axes:
                        setattr(self._partial, attr, self._last_axes[missing])
                if all(axis in self._seen_ever for axis in REQUIRED_AXES):
                    samples.append(self._partial)
                self._partial = MotionSample()
                self._has_partial = False
                self._seen_axes = set()

        if samples and newest_kernel_ts is not None:
            kernel_to_mono = newest_kernel_ts - time.monotonic()
            for s in samples:
                s.timestamp -= kernel_to_mono

        return samples

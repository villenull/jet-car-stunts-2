#!/usr/bin/env python3
"""Owned Linux launcher for the preserved API28 JCS2 Android guest.

The runner owns every process it starts, keeps ADB on its dedicated port, and
does not install, restore, wipe, or create an AVD. The controller receives
input through its normal NDJSON stdin. A Unix socket is provided for
deterministic QA; the physical joystick bridge is another producer of the
same stream.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import selectors
import signal
import stat
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterable

from runtime_paths import resolve_paths
from audio_config import audio_config_status, seed_media_volume
from deck_pad import DeckPadError, claim_pad, release_pad
from qt_settings import (
    QtSettingsError,
    seed_compatibility_warning_suppression,
)

# --- Bridge side-channel (raw-event Unix socket) ---
try:
    from bridge_side_channel import BridgeSideChannel
except ImportError:
    BridgeSideChannel = None

# --- Tilt control integration (read-only API) ---
try:
    from tilt_control.adapter import TiltAdapter
    from tilt_control.settings import ControlMode
    from tilt_control.stick_gate import stick_gate_allows
except ImportError:
    TiltAdapter = None
    ControlMode = None

    def stick_gate_allows(event, tilt_steering_active):  # type: ignore[misc]
        return True

# --- Native accelerometer transport (emulator console, no adb per frame) ---
try:
    from tilt_control.sensor_transport import (
        ConsoleSensorTransport,
        NEUTRAL_ACCELERATION,
        GRAVITY,
        CONSOLE_PORT,
        format_sensor_set,
    )
except ImportError:
    ConsoleSensorTransport = None
    NEUTRAL_ACCELERATION = (-0.34, 0.0, 9.80)  # deterministic 2 degree roll park
    GRAVITY = 9.81
    CONSOLE_PORT = 5594

    def format_sensor_set(vector):  # type: ignore[misc]
        ax, ay, az = (float(value) for value in vector)
        return f"sensor set acceleration {ax:.3f}:{ay:.3f}:{az:.3f}\n".encode()

# --- DeckControls (moved from bridge to runner for side-channel mode) ---
try:
    from joystick_bridge import DeckControls as _DeckControls
except ImportError:
    _DeckControls = None

ROOT = Path(__file__).resolve().parents[1]
PATHS = resolve_paths(ROOT)
SDK = PATHS.sdk
DIST = PATHS.dist
AVD_HOME = PATHS.avd_home
AVD = PATHS.avd
CONTROLLER = PATHS.controller
HELPER_JAR = PATHS.helper_jar
GAME = "com.trueaxis.jetcarstunts2"
PACKAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
ADB_PORT = 5038
CONSOLE_PORT = 5594
SERIAL = "127.0.0.1:5595"
# Buttons with no guest action. A and B are menu-free (native touch does
# menus); DPAD is dropped by DeckControls. VIEW (physical SELECT) had the
# host panel as its only action; with the panel gone it is dropped here so
# it can never reach the controller, which aborts its live replay on an
# unknown name and takes the lane down with it (2026-09-18: the controller
# exited with "invalid live button VIEW" ~40 s after start).
UNASSIGNED_BUTTONS = frozenset(("A", "B", "VIEW", "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"))
MOTION_READY_TIMEOUT = 0.5  # Deck IMU reports at ~250 Hz once readable.
SENSOR_UNAVAILABLE = "Motion sensor unavailable; left joystick remains active."
SENSOR_SILENT = "Motion sensor sent no data; left joystick remains active."
GAMEPLAY_TIMEOUT = 12 * 60 * 60  # 12h sessions (user-approved; was 6h)
# Host-window rotation bookkeeping (see Launcher.align_visible_emulator).
# The emulator renders the guest framebuffer upright only while its own window
# rotation offset and the guest display rotation sum to 0 (mod 4), and each
# emulator-console `rotate` advances that offset by exactly one. There is no
# launch flag, AVD setting, or readable getter for the offset in 32.1.15, so the
# launcher tracks it relative to the state it aligned.
GUEST_ROTATION_SETTLE_TIMEOUT = 40.0
# Render verification after alignment. The rotate arithmetic alone is not enough:
# measured 2026-09-18 the console `rotate` also moves the guest display quarter,
# so the window offset model can be off by a quarter without anything reporting
# it. The launcher therefore compares its OWN grim of the host screen against the
# guest's screencap, rotated through the four quarters, and corrects the offset
# it can actually see. Frames are downsampled to this grid before comparing.
RENDER_VERIFY_SCALE = "0.1"
RENDER_VERIFY_COLUMNS = 40
RENDER_VERIFY_ROWS = 25
RENDER_VERIFY_MAX_ATTEMPTS = 4
# A column "has picture" when its luminance spans more than this; a band of
# content-free edge columns at least this wide is a 90-degree window error.
RENDER_CONTENT_VARIANCE = 0.06
RENDER_BLANK_BAND_MAX = 0.25
# Recovery plan for a render the measurement says is NOT upright. Chosen by
# measurement, not arithmetic: a console ``rotate`` advances the host window
# offset AND the guest display quarter together, so a 90-degree strip is a fixed
# point of rotating (measured 2026-09-18: six single rotates, each followed by a
# fresh guest/host pair, left the strip unchanged, as did four ``user_rotation``
# values). The channel that does move it is the accelerometer, and only while
# the guest display is UNPINNED - the pin this stage applies afterwards is
# exactly what makes a wrong render permanent for the rest of the lane. Each
# action below is followed by a fresh measurement, and the loop stops at the
# first upright one.
RENDER_RECOVERY_PLAN = ("probe", "rotate", "probe")
RENDER_VERIFY_SETTLE_SECONDS = 1.0
# A window that has not painted yet measures as a blank frame (the content-free
# band covers the whole window), and acting on that is how the stage was driven
# to "recover" a picture that was already correct: measured 2026-09-18, the
# align stage read band 1.0 then 0.6 within four seconds of the game resuming and
# the picture was upright moments later. A measurement therefore only counts as
# a verdict once two consecutive reads agree on a frame that has content.
RENDER_PRESENT_ATTEMPTS = 3
# Pose sent by the "probe" recovery action: the emulator re-derives its window
# offset from it and lands upright for a guest on display quarter 1 (measured).
RENDER_RECOVERY_ACCELERATION = (GRAVITY, 0.0, 0.0)
# The display pin the last VERIFIED align enforced, persisted beside the logs.
# A fresh lane can then hold the proven orientation instead of re-deriving it:
# the provocation cycle exists only to make the emulator re-lay-out its window,
# and a lane whose last render measured upright already has that layout. The file
# is advisory - a hold that does not measure upright is discarded, the display is
# unpinned again, and the full align runs.
DISPLAY_PIN_FILE = "display-pin.json"
# A hold is honoured only while the picture is upright on every settled read.
# The emulator is still rebuilding its window layout for the first seconds after
# the game resumes (measured 2026-09-18: a hold read 180 degrees one second after
# resume), so one read would either honour a wrong picture or throw away a good
# pin by accident.
RENDER_HOLD_READS = 3
# Guard thrash rules (user directive 2026-09-18): the polling guard corrects a
# guest-rotation flip at most once per burst, never two bursts inside this
# window, and re-parks the accelerometer after every burst. The emulator only
# re-derives its window layout from a guest rotation *change*, so a live feed
# left behind after the correction is what made the guard fight itself; the
# parked pose also has exactly one guest rotation, so seeing that rotation is
# never a reason to rotate.
# Tilt Drive is UNSELECTABLE on this AVD (user verdict 2026-09-18, after three
# live attempts): the game's window requests SCREEN_ORIENTATION_SENSOR_LANDSCAPE
# (`dumpsys window tokens` shows mOrientation=6), so the framework picks the
# display quarter from the very accelerometer the tilt mirror feeds, and every
# tilt re-derives the emulator window as a sideways strip; this image exposes no
# rotation-hold lever (`wm user-rotation` and `cmd window user-rotation` are both
# "Unknown command"). The engine below is kept intact - only the user-facing
# ingress is closed - so the feature can return on an image that can hold the
# display. Gamepad is the only selectable mode.
TILT_DRIVE_SELECTABLE = False
TILT_DRIVE_DISABLED_REASON = (
    "Tilt Drive is disabled on this device: the game's sensor-landscape window "
    "lets tilt re-derive the display and the picture flips. Gamepad stays.")
GUARD_ROTATE_COOLDOWN = 300.0
PARKED_GUEST_ROTATION = 1
# One quarter-turn probe per guest landscape quarter: gravity along an X half
# axis reads as the matching landscape rotation, so the launcher can move the
# guest display exactly 90 degrees (Launcher.provoke_guest_display_rotation).
ROTATION_PROBE_ACCELERATION = {
    3: (GRAVITY, 0.0, 0.0),
    1: (-GRAVITY, 0.0, 0.0),
}
# Native-Quit lifecycle: once launch_game has confirmed the game resumed, a
# sustained HOME/launcher foreground PLUS an ended game process means the user
# accepted the game's own Quit rather than a boot/loading state. Arbitrary
# non-game foreground (permission dialogs, Settings, other apps) is never a
# quit signal on its own. Require consecutive confirmations in a bounded
# window: any unknown sample (ADB glitch, missing line, pidof failure) or any
# non-qualifying sample (game resumed, unrelated app, game still alive)
# resets the streak, so sparse polls can never accumulate into a quit.
GAME_QUIT_POLL_INTERVAL = 2.0
GAME_QUIT_CONFIRM_POLLS = 3
# Observed home/launcher foregrounds only. NexusLauncher is the guest's
# actual launcher (prior dumpsys evidence); AOSP Launcher3 is the fallback.
# Anything else resumed (Settings, dialogs, other apps) resets the window.
HOME_PACKAGES = frozenset({
    "com.google.android.apps.nexuslauncher",
    "com.android.launcher3",
})


ISOLATION_SCRIPT = (
    "svc wifi disable; svc data disable; "
    "for i in $(ip -o link | awk -F': ' '$2 != \"lo\" {print $2}' | cut -d@ -f1); "
    "do ip link set \"$i\" down || exit 41; ip addr flush dev \"$i\" || exit 42; done; "
    "ip route flush table all || exit 43; ip -6 route flush table all || exit 44; "
    "test -e /dev/uhid || exit 45; chmod 660 /dev/uhid || exit 46"
)


class LauncherError(RuntimeError):
    """A fail-closed launcher error."""


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def compact(value: str, limit: int = 4096) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…"


def command_text(argv: Iterable[object]) -> str:
    return " ".join(str(arg) for arg in argv)


def exact_uid(output: str, wanted: int) -> bool:
    match = re.search(r"(?:^|\s)uid=(\d+)(?:\(|\s|$)", output)
    return bool(match and int(match.group(1)) == wanted)


def resumed_package(dumpsys: str) -> str | None:
    """Return only the package on the exact mResumedActivity line."""
    for line in dumpsys.splitlines():
        if "mResumedActivity:" not in line:
            continue
        match = re.search(r"\bu\d+\s+([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/", line)
        return match.group(1) if match else None
    return None


def game_task_state(dumpsys: str) -> bool | None:
    """Tri-state game-task presence in a dumpsys activity snapshot.

    True = a game TaskRecord/Activity is still listed (backgrounded, the
    Activity has NOT finished — e.g. Home button with the game alive).
    False = TaskRecord markers are present but no game task is listed
    (the Activity finished; a still-alive process is only cached).
    None = no TaskRecord marker at all (minimal/transition output) —
    unknown, never evidence; callers must fall back to the pidof signal.
    """
    if "TaskRecord" not in dumpsys:
        return None
    if f"A={GAME}" in dumpsys or f"{GAME}/" in dumpsys:
        return True
    return False


def input_device_matches(dumpsys: str, device_id: int) -> bool:
    """Match one Android input device by both exact id and JCS2 name."""
    lines = dumpsys.splitlines()
    for index, line in enumerate(lines):
        if re.search(rf"(?:^|\s){re.escape(str(device_id))}: JCS2 Virtual Xbox Controller\s*$", line):
            return True
        if re.search(rf"\bDevice {re.escape(str(device_id))}: JCS2 Virtual Xbox Controller\s*$", line):
            return True
        context = "\n".join(lines[max(0, index - 3):index + 4])
        if f"deviceId={device_id}" in line and "JCS2" in context:
            return True
    return False


def isolated_network(link_output: str, ipv4_routes: str, ipv6_routes: str) -> tuple[bool, str]:
    """Require loopback as the only interface in ``ip link show up`` output."""
    up_names: list[str] = []
    for line in link_output.splitlines():
        # The caller uses ``ip -o link show up``.  That command is the source
        # of truth for operational state; do not reject loopback merely
        # because its kernel state is UNKNOWN rather than UP.
        match = re.match(r"\d+: ([^: @]+)(?:@[^: ]+)?\s*:", line)
        if match:
            up_names.append(match.group(1))
    if set(up_names) != {"lo"}:
        return False, f"unexpected UP interfaces: {up_names!r}"
    if ipv4_routes.strip() or ipv6_routes.strip():
        return False, "unexpected IPv4/IPv6 routes remain"
    return True, "only lo is UP; IPv4/IPv6 route tables are empty"


def wifi_disabled(settings_output: str, dumpsys_output: str) -> bool:
    """Accept the API28 settings value or an explicit dumpsys disabled flag."""
    if settings_output.strip() == "0":
        return True
    return bool(re.search(r"(?:Wi-Fi is disabled|mWifiEnabled\s*[:=]\s*false|Wi-Fi enabled:\s*false)", dumpsys_output, re.IGNORECASE))


@dataclass
class OwnedProcess:
    name: str
    process: subprocess.Popen
    log_handles: tuple[IO[str], ...] = ()

    pgid: int = field(init=False)

    def __post_init__(self) -> None:
        # Every owned process is created with start_new_session=True. Capture
        # its group now: looking up an exited leader can fail during cleanup.
        self.pgid = self.process.pid


class CommandLog:
    """Persist every direct subprocess invocation and its result."""

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _text(value) -> str:
        """Binary output is summarised, never decoded into the log."""
        if value is None:
            return ""
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        return value

    def record(self, argv: list[str], result: subprocess.CompletedProcess | None, error: str = "") -> None:
        row = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "argv": argv,
            "returncode": None if result is None else result.returncode,
            "stdout": "" if result is None else self._text(result.stdout),
            "stderr": "" if result is None else self._text(result.stderr),
            "error": error,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            json.dump(row, stream, sort_keys=True)
            stream.write("\n")


def downsample_ppm(raw: bytes, columns: int, rows: int, region=None):
    """PPM (P6) bytes -> coarse RGB grid, or None when the header is unexpected."""
    if not raw.startswith(b"P6"):
        return None
    fields: list[int] = []
    position = 2
    while len(fields) < 3 and position < len(raw):
        while position < len(raw) and raw[position:position + 1].isspace():
            position += 1
        if raw[position:position + 1] == b"#":
            while position < len(raw) and raw[position:position + 1] != b"\n":
                position += 1
            continue
        start = position
        while position < len(raw) and not raw[position:position + 1].isspace():
            position += 1
        if position == start:
            return None
        fields.append(int(raw[start:position]))
    position += 1
    width, height = fields[0], fields[1]
    pixels = raw[position:]
    if width <= 0 or height <= 0 or len(pixels) < width * height * 3:
        return None
    return sample_grid(pixels, width, height, 3, columns, rows, region)


def downsample_rgba(raw: bytes, columns: int, rows: int, region=None):
    """Android raw screencap (w, h, format + RGBA pixels) -> coarse RGB grid."""
    if len(raw) < 12:
        return None
    width = int.from_bytes(raw[0:4], "little")
    height = int.from_bytes(raw[4:8], "little")
    pixels = raw[12:]
    if width <= 0 or height <= 0 or len(pixels) < width * height * 4:
        return None
    return sample_grid(pixels, width, height, 4, columns, rows, region)


def sample_grid(pixels: bytes, width: int, height: int, channels: int,
                columns: int, rows: int, region=None):
    """Average blocks of a packed image into a small RGB grid (0..1 floats).

    ``region`` (x, y, w, h) restricts sampling, which is how the host screen is
    reduced to just the emulator window: the deck shows a desktop around it, and
    comparing that against the guest frame produced a 0.6 mismatch for a
    perfectly upright picture (measured 2026-09-18).
    """
    if region:
        rx, ry, rw, rh = region
        rx, ry = max(0, min(rx, width - 1)), max(0, min(ry, height - 1))
        rw = max(1, min(rw, width - rx))
        rh = max(1, min(rh, height - ry))
    else:
        rx, ry, rw, rh = 0, 0, width, height
    grid = []
    for row in range(rows):
        y0 = ry + row * rh // rows
        y1 = max(y0 + 1, ry + (row + 1) * rh // rows)
        line = []
        for column in range(columns):
            x0 = rx + column * rw // columns
            x1 = max(x0 + 1, rx + (column + 1) * rw // columns)
            total = [0, 0, 0]
            count = 0
            for y in range(y0, y1, max(1, (y1 - y0) // 3)):
                base = y * width
                for x in range(x0, x1, max(1, (x1 - x0) // 3)):
                    offset = (base + x) * channels
                    total[0] += pixels[offset]
                    total[1] += pixels[offset + 1]
                    total[2] += pixels[offset + 2]
                    count += 1
            line.append(tuple(component / (count * 255.0) for component in total))
        grid.append(line)
    return grid


def rotate_grid(grid, quarters: int):
    """Rotate a small grid by 90-degree quarters (clockwise)."""
    result = grid
    for _ in range(quarters % 4):
        result = [list(row) for row in zip(*result[::-1])]
    return result


def _luminance(pixel) -> float:
    red, green, blue = pixel
    return 0.299 * red + 0.587 * green + 0.114 * blue


def content_columns(grid) -> list[bool]:
    """Per column: does it carry picture, or is it uniform letterbox fill?"""
    flags = []
    for column in range(len(grid[0])):
        values = [_luminance(grid[row][column]) for row in range(len(grid))]
        flags.append((max(values) - min(values)) > RENDER_CONTENT_VARIANCE)
    return flags


def blank_band_ratio(grid) -> float:
    """Widest content-free band touching a left or right edge, as a ratio.

    A 90-degree window error renders the guest scaled into a portrait strip with
    a wide uniform band beside it (measured 2026-09-18: content in the left ~55
    percent, blank fill to the right), which no rotation of the guest frame can
    match - so this, not a pixel comparison, is what detects a quarter error.
    """
    if not grid or not grid[0]:
        return 1.0
    flags = content_columns(grid)
    leading = 0
    while leading < len(flags) and not flags[leading]:
        leading += 1
    trailing = 0
    while trailing < len(flags) - 1 and not flags[len(flags) - 1 - trailing]:
        trailing += 1
    return max(leading, trailing) / len(flags)


def render_offset(host, guest) -> tuple[int, float]:
    """Quarter offset that best maps the guest frame onto the host frame.

    Returns (offset, error): offset 0 means the host renders the guest upright.
    Only the 0- and 180-degree quarters are compared pixel-wise, because a
    90-degree error puts the guest into a scaled portrait strip; that case is
    reported by a wide blank band instead (offset 1). Pure and
    display-independent, so the offline suite pins it with synthetic frames.
    """
    if not host or not guest or len(host) != len(guest) or len(host[0]) != len(guest[0]):
        return 1, 1.0
    band = blank_band_ratio(host)
    if band >= RENDER_BLANK_BAND_MAX:
        return 1, band
    best, best_error = 0, None
    for quarters in (0, 2):
        candidate = rotate_grid(guest, quarters)
        total = 0.0
        for host_row, guest_row in zip(host, candidate):
            for (hr, hg, hb), (gr, gg, gb) in zip(host_row, guest_row):
                total += abs(hr - gr) + abs(hg - gg) + abs(hb - gb)
        error = total / (3.0 * len(host) * len(host[0]))
        if best_error is None or error < best_error:
            best, best_error = quarters, error
    return best, (best_error if best_error is not None else 1.0)


def emulator_window_region(clients) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the emulator client in a parsed ``hyprctl clients`` list.

    Pure and display-independent, so the offline suite pins it without a
    compositor.
    """
    for client in clients if isinstance(clients, list) else []:
        if not isinstance(client, dict) or str(client.get("class", "")).lower() != "emulator":
            continue
        at, size = client.get("at"), client.get("size")
        if (isinstance(at, list) and isinstance(size, list) and len(at) == 2
                and len(size) == 2 and size[0] > 0 and size[1] > 0):
            return int(at[0]), int(at[1]), int(size[0]), int(size[1])
    return None


def hyprland_signature_candidates(env: dict | None = None) -> list[str]:
    """Hyprland instance signatures to try, the configured one first.

    A supervisor's pinned ``HYPRLAND_INSTANCE_SIGNATURE`` goes stale across a
    reboot: the compositor runtime dir keeps the previous instance beside the
    new one, every ``hyprctl`` call under the old name fails, and the window
    region then went unknown (measured 2026-09-18). The live instance is found
    by trying the runtime dirs newest-first, because the running compositor is
    the one whose directory was touched last.
    """
    environ = os.environ if env is None else env
    candidates: list[str] = []
    configured = environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if configured:
        candidates.append(configured)
    runtime = Path(environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "hypr"
    try:
        entries = [path for path in runtime.iterdir() if path.is_dir()]
    except OSError:
        return candidates
    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    for path in sorted(entries, key=mtime, reverse=True):
        if path.name not in candidates:
            candidates.append(path.name)
    return candidates


class Launcher:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.children: list[OwnedProcess] = []
        self.stop_requested = False
        log_root = resolve_paths(ROOT).logdir
        base = log_root / f"run-{utc_stamp()}"
        self.run_dir = base
        suffix = 1
        while True:
            try:
                self.run_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                self.run_dir = log_root / f"{base.name}-{suffix}"
                suffix += 1
        self.commands = CommandLog(self.run_dir / "commands.ndjson")
        self.controller: OwnedProcess | None = None
        self.controller_input: IO[bytes] | None = None
        self.router: InputRouter | None = None
        self.server: OwnedProcess | None = None
        self.emulator: OwnedProcess | None = None
        self.window_guard: OwnedProcess | None = None
        self._profile_lock: IO[bytes] | None = None
        self._endpoint_lock: IO[bytes] | None = None
        # Guest display rotation the host window was last aligned to (None until
        # align_visible_emulator has run once).
        self._aligned_guest_rotation: int | None = None
        # Emulator-window rectangle the last host frame was sampled through
        # (None when the host screen could not be cropped); reported in the
        # render-measurement log so an offset of 1 can be told apart from a
        # whole-screen capture that simply has no window in it.
        self._host_window_region: tuple[int, int, int, int] | None = None
        # Quarter offset the last guest/host pair measured (None when no pair
        # could be captured): the align stage pins the display only on a render
        # whose measurement says upright.
        self._last_measured_offset: int | None = None
        # Content-free band ratio of the same frame (1.0 = nothing painted yet).
        self._last_host_band: float = 1.0
        # Guard thrash bookkeeping: the flip already handled and when the last
        # correction burst was sent (see guard_display_orientation).
        self._guard_last_rotate_at: float | None = None
        self._guard_rotated_for: int | None = None
        self._guard_read_failed = False
        self._guard_quiet_until = 0.0
        # True once the guest display quarter is pinned (tilt cannot flip it).
        self._guest_rotation_locked = False
        # What claiming the built-in pad changed, so cleanup() can reverse it.
        self._deck_pad_state: dict | None = None

    def log(self, message: str, **fields: object) -> None:
        row = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "message": message}
        row.update(fields)
        with (self.run_dir / "launcher.ndjson").open("a", encoding="utf-8") as stream:
            json.dump(row, stream, sort_keys=True)
            stream.write("\n")
        suffix = f" {json.dumps(fields, sort_keys=True)}" if fields else ""
        print(f"[{row['time']}] {message}{suffix}", flush=True)

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({"ANDROID_HOME": str(SDK), "ANDROID_SDK_ROOT": str(SDK),
                    "ANDROID_AVD_HOME": str(AVD_HOME), "ADB_SERVER_PORT": str(ADB_PORT)})
        # Steam sets ENABLE_GAMESCOPE_WSI for its own games. The emulator is an
        # Xwayland client (gamescope composites its window there), and that layer
        # kills its boot when launched from Steam in Gaming Mode: emulator.log
        # ends with "cannot add library .../lib64/vulkan/libvulkan.so: failed",
        # "[Gamescope WSI] Executable name: qemu-system-x86_64", then the process
        # exits ~2 s into boot ("emulator exited during boot"). It is not needed
        # for the X11/GL path we use, so it is switched off for every child.
        env["ENABLE_GAMESCOPE_WSI"] = "0"
        return env

    def ensure_server_alive_if_needed(self, argv: list[str]) -> None:
        if len(argv) < 2 or Path(argv[0]).name != "adb" or argv[1] != "-P":
            return
        if self.server is not None:
            if self.server.process.poll() is not None or not tcp_open("127.0.0.1", ADB_PORT):
                raise LauncherError("owned ADB server is no longer alive; refusing client auto-replacement")

    def run(self, argv: list[str], timeout: float = 15, check: bool = True) -> subprocess.CompletedProcess:
        self.ensure_server_alive_if_needed(argv)
        started = time.monotonic()
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self.environment())
        except (OSError, subprocess.TimeoutExpired) as error:
            self.commands.record(argv, None, repr(error))
            raise LauncherError(f"command failed: {command_text(argv)}: {error}") from error
        self.commands.record(argv, result)
        self.log("command", argv=argv, returncode=result.returncode,
                 duration_ms=round((time.monotonic() - started) * 1000),
                 stdout=compact(result.stdout), stderr=compact(result.stderr))
        if check and result.returncode:
            raise LauncherError(f"command failed ({result.returncode}): {command_text(argv)}: {compact(result.stderr or result.stdout)}")
        return result

    def run_bytes(self, argv: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
        """Run a command whose stdout is BINARY (raw screencap, PPM frames).

        ``run`` decodes stdout as text, which is wrong for frames: a raw guest
        screencap is pixel data, and decoding it raised UnicodeDecodeError out of
        the align stage (measured 2026-09-18: the lane stopped the moment the
        render verification captured a guest frame). Binary output is summarised
        in the command log instead of stored.
        """
        started = time.monotonic()
        try:
            result = subprocess.run(argv, capture_output=True, timeout=timeout, env=self.environment())
        except (OSError, subprocess.TimeoutExpired) as error:
            self.commands.record(argv, None, repr(error))
            raise LauncherError(f"command failed: {command_text(argv)}: {error}") from error
        self.commands.record(argv, result)
        self.log("command", argv=argv, returncode=result.returncode,
                 duration_ms=round((time.monotonic() - started) * 1000),
                 stdout=f"<{len(result.stdout or b'')} bytes>",
                 stderr=compact(self.commands._text(result.stderr)))
        return result

    def spawn(self, name: str, argv: list[str], stdout_name: str, stderr_name: str | None = None) -> OwnedProcess:
        stdout_path = self.run_dir / stdout_name
        stderr_path = stdout_path if stderr_name is None else self.run_dir / stderr_name
        handles = []
        try:
            stdout = stdout_path.open("w", encoding="utf-8")
            handles.append(stdout)
            stderr = stdout if stderr_path == stdout_path else stderr_path.open("w", encoding="utf-8")
            if stderr is not stdout:
                handles.append(stderr)
            process = subprocess.Popen(argv, stdin=subprocess.PIPE if name == "controller" else subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True, env=self.environment())
        except BaseException:
            for handle in handles:
                try:
                    handle.close()
                except OSError:
                    pass
            raise
        owned = OwnedProcess(name, process, (stdout,) if stderr is stdout else (stdout, stderr))
        self.children.append(owned)
        self.log("spawn", name=name, pid=process.pid, pgid=owned.pgid, argv=argv)
        return owned

    def preflight(self) -> None:
        adb = SDK / "platform-tools/adb"
        emulator = SDK / "emulator/emulator"
        if not (adb.is_file() and os.access(adb, os.X_OK) and emulator.is_file() and os.access(emulator, os.X_OK) and CONTROLLER.is_file() and os.access(CONTROLLER, os.X_OK)):
            raise LauncherError("missing SDK tools, emulator, or Linux controller")
        if not HELPER_JAR.is_file() or not (DIST / "controller/mapping.json").is_file():
            raise LauncherError("missing shipping helper JAR or mapping asset")
        if not (AVD_HOME / f"{AVD}.avd").is_dir():
            raise LauncherError(f"missing authorized AVD profile: {AVD}")
        audio = audio_config_status(AVD_HOME / f"{AVD}.avd/config.ini")
        self.log("stage=audio-config", **audio)
        if audio["output_disabled"]:
            self.log("warning", detail="AVD audio output disabled; hw.audioOutput must be yes for sound")
        for port in (ADB_PORT, CONSOLE_PORT, 5595):
            if tcp_open("127.0.0.1", port):
                raise LauncherError(f"port {port} is already busy")
        self.log("stage=preflight", sdk=str(SDK), avd_home=str(AVD_HOME), avd=AVD,
                 adb_port=ADB_PORT, console_port=CONSOLE_PORT, serial=SERIAL,
                 input_mode=self.args.input_mode)
        try:
            settings_path, warning_key, changed = (
                seed_compatibility_warning_suppression(AVD))
        except QtSettingsError as error:
            raise LauncherError(str(error)) from error
        self.log("stage=compatibility-warning-seeded", path=str(settings_path),
                 key=warning_key, changed=changed)

    def start_server(self) -> None:
        adb = str(SDK / "platform-tools/adb")
        self.server = self.spawn("adb-server", [adb, "-P", str(ADB_PORT), "nodaemon", "server"], "adb-server.log")
        wait_until(lambda: self.server is not None and self.server.process.poll() is None and tcp_open("127.0.0.1", ADB_PORT), 10, "ADB server")
        self.log("stage=adb-server-ready")

    def adb(self, *args: str, timeout: float = 15, check: bool = True) -> subprocess.CompletedProcess:
        return self.run([str(SDK / "platform-tools/adb"), "-P", str(ADB_PORT), "-s", SERIAL, *args], timeout, check)

    def wait_for_adb_device(self, label: str, timeout: float = 30) -> None:
        """Wait for adbd to finish a root/unroot transport restart."""
        def ready() -> bool:
            result = self.adb("get-state", timeout=5, check=False)
            return result.returncode == 0 and result.stdout.strip() == "device"

        wait_until(ready, timeout, f"{label} ADB reconnect")

    def isolation_argv(self) -> list[str]:
        """Build isolation argv with the script kept as one ADB shell arg."""
        return [str(SDK / "platform-tools/adb"), "-P", str(ADB_PORT), "-s", SERIAL, "shell", ISOLATION_SCRIPT]

    def gpu_mode(self) -> str:
        """Emulator GPU mode for this session.

        `-gpu host` is the desktop default and the proven 60 fps path, but it
        needs a desktop GL context: inside a gamescope session its color buffers
        fail ("ColorBuffer::create get gl error 0x502", "bad color buffer
        handle" in emulator.log) and the guest's surface dies with the game a few
        seconds after launch. The AVD is provisioned for swiftshader_indirect,
        which gamescope does not fight. JCS2_GPU overrides either choice.
        """
        override = os.environ.get("JCS2_GPU", "").strip()
        if override:
            return override
        return "host" if self.desktop_compositor_available() else "swiftshader_indirect"

    def start_emulator(self) -> None:
        argv = [str(SDK / "emulator/emulator"), "-avd", AVD, "-port", str(CONSOLE_PORT), "-no-snapshot", "-no-boot-anim",
                "-adb-path", str(SDK / "platform-tools/adb"),
                "-gpu", self.gpu_mode(), "-memory", "1536", "-qemu", "-net", "none"]
        if self.args.headless:
            argv.insert(argv.index("-qemu"), "-no-window")
        else:
            argv.insert(argv.index("-qemu"), "-fixed-scale")
        self.emulator = self.spawn("emulator", argv, "emulator.log")
        self.start_gamescope_window_guard()
        self.log("stage=emulator-start", network="none", snapshots="disabled", headless=self.args.headless)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not self.stop_requested:
            if self.emulator.process.poll() is not None:
                raise LauncherError("emulator exited during boot")
            if tcp_open("127.0.0.1", 5595):
                self.adb("connect", SERIAL, timeout=10, check=False)
                state = self.adb("get-state", timeout=10, check=False).stdout.strip()
                boot = self.adb("shell", "getprop", "sys.boot_completed", timeout=10, check=False).stdout.strip()
                self.log("boot-poll", state=state, boot_completed=boot)
                if state == "device" and boot == "1":
                    self.log("stage=emulator-ready")
                    return
            time.sleep(2)
        raise LauncherError("bounded emulator boot/reconnect timeout")

    def start_gamescope_window_guard(self) -> None:
        # The helper is an X11/XWayland input and fullscreen boundary, not a
        # Gamescope-only feature. Desktop sessions also need the owned toolbar
        # suppression and explicit active-window request for real touch input.
        if self.args.headless or not os.environ.get("DISPLAY"):
            return
        assert self.emulator is not None
        self.window_guard = self.spawn("gamescope-window", [
            sys.executable, str(ROOT / "linux-launcher/gamescope_window.py"),
            "--pid", str(self.emulator.process.pid),
            "--ready-file", str(self.run_dir / "gamescope-window-ready.json"),
            "--reveal-file", str(self.run_dir / "game-window-reveal.json"),
        ], "gamescope-window.log")

    def verify_gamescope_window(self) -> None:
        if self.window_guard is None:
            return
        marker = self.run_dir / "gamescope-window-ready.json"
        deadline = time.monotonic() + 30
        while not self.stop_requested and time.monotonic() < deadline:
            if self.window_guard.process.poll() is not None:
                raise LauncherError("Gaming Mode window setup failed; see gamescope-window.log")
            if marker.exists():
                self.log("stage=gamescope-window-ready", marker=str(marker))
                return
            time.sleep(0.1)
        raise LauncherError("Gaming Mode main window was not ready within the startup deadline")

    def console_send(self, command: str) -> None:
        """Send one authenticated emulator-console command (bounded)."""
        with socket.create_connection(("127.0.0.1", CONSOLE_PORT), timeout=5) as console:
            def response() -> bytes:
                data = bytearray()
                while len(data) < 65536:
                    chunk = console.recv(4096)
                    if not chunk:
                        raise LauncherError("emulator console closed during command")
                    data.extend(chunk)
                    if any(line.startswith(b"KO") for line in data.splitlines()):
                        raise LauncherError("emulator console rejected command")
                    if data.endswith(b"OK\r\n") or data.endswith(b"OK\n"):
                        return bytes(data)
                raise LauncherError("oversized emulator console response")

            greeting = response()
            if b"Authentication required" in greeting:
                token = (Path.home() / ".emulator_console_auth_token").read_text().strip()
                # Never put this local console credential into command/event logs.
                console.sendall(("auth " + token + "\n").encode())
                response()
            console.sendall((command + "\n").encode())
            response()

    def orient_visible_emulator(self) -> None:
        """Rotate the virtual phone, not the host monitor or game framebuffer."""
        if self.args.headless:
            return
        self.console_send("rotate")
        self.log("stage=virtual-display-rotated", clockwise_degrees=90)

    def guest_display_rotation(self) -> int | None:
        """Guest display rotation (0..3), or None when it cannot be read."""
        try:
            result = self.adb("shell", "dumpsys", "display", timeout=10, check=False)
        except (LauncherError, OSError):
            return None
        if result.returncode:
            return None
        match = re.search(r"mCurrentOrientation=(\d)", result.stdout)
        return int(match.group(1)) if match else None

    def wait_for_guest_display_rotation(self, exclude: int | None = None) -> int | None:
        """Guest display rotation once it has settled on a landscape quarter.

        The game requests SCREEN_ORIENTATION_SENSOR_LANDSCAPE, so the rotation is
        decided again while its activity resumes. Two consecutive equal landscape
        samples mean it settled; anything else returns None so the caller skips
        alignment rather than guessing an orientation. ``exclude`` waits for a
        rotation *other* than that value (used to observe a provoked change).
        """
        deadline = time.monotonic() + GUEST_ROTATION_SETTLE_TIMEOUT
        previous = None
        while not self.stop_requested and time.monotonic() < deadline:
            rotation = self.guest_display_rotation()
            if rotation in (1, 3) and rotation == previous and rotation != exclude:
                return rotation
            previous = rotation
            time.sleep(0.5)
        return None

    def provoke_guest_display_rotation(self, rotation: int) -> int | None:
        """Force one guest display-rotation change, then park the sensor flat.

        The emulator only rebuilds its window layout to match the guest once it
        observes a guest display-rotation change *while its window is up*; the
        change at boot usually lands before that, leaving the portrait layout —
        the game then renders as a sideways or narrow portrait strip. The
        launcher therefore provokes exactly one deliberate quarter-turn through
        the accelerometer channel it already owns, waits for the guest to settle
        on the other landscape quarter, and parks the sensor flat again: neutral
        for the game's own tilt mode and proposal-free for the framework, so
        nothing moves afterwards. Returns the new rotation, or None when the
        guest did not move (caller then leaves the rotation alone).
        """
        probe = ROTATION_PROBE_ACCELERATION.get(rotation)
        if probe is None:
            return None
        self.console_send(format_sensor_set(probe).decode().strip())
        moved = self.wait_for_guest_display_rotation(exclude=rotation)
        self.console_send(format_sensor_set(NEUTRAL_ACCELERATION).decode().strip())
        return moved

    @staticmethod
    def host_rotation_steps(guest_rotation: int, reference_rotation: int) -> int:
        """Console ``rotate`` steps that keep the host window rendering upright.

        The emulator draws the guest framebuffer upright only while its own
        rotation offset and the guest display rotation sum to 0 (mod 4), and
        every ``rotate`` advances that offset by one. A guest rotation change of
        one quarter-turn therefore needs ``(4 - delta) % 4`` steps. Measured on
        the proven portrait AVD (800x1280 native) with guest rotation 1: offset 3
        renders upright, offsets 0 and 2 render a sideways portrait strip, and
        offset 1 renders upside down.
        """
        return (4 - (guest_rotation - reference_rotation)) % 4

    def desktop_compositor_available(self) -> bool:
        """True when a desktop compositor we can measure through owns the display.

        Gaming Mode presents the emulator window through gamescope's own
        Xwayland with no Hyprland to query, so the host render cannot be
        measured there and provoking a guest rotation to re-derive it would
        restart the game's SENSOR_LANDSCAPE activity (measured 2026-09-19: the
        game left the foreground ~12 s after the align stage ran). Cached: a
        session cannot gain or lose its compositor mid-lane.
        """
        if getattr(self, "_desktop_compositor", None) is None:
            signatures = hyprland_signature_candidates()
            self._desktop_compositor = any(
                self.hyprctl_clients(signature) is not None for signature in signatures)
            if not self._desktop_compositor:
                self.log("display-compositor-unavailable",
                         reason="no Hyprland session; gamescope owns the output")
        return self._desktop_compositor

    def hyprctl_clients(self, signature: str):
        """Parsed ``hyprctl clients -j`` under one signature, or None on failure."""
        env = self.environment()
        env["HYPRLAND_INSTANCE_SIGNATURE"] = signature
        argv = ["hyprctl", "clients", "-j"]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None
        self.commands.record(argv, result)
        self.log("command", argv=argv, returncode=result.returncode,
                 hyprland_signature=signature, stdout=compact(result.stdout),
                 stderr=compact(result.stderr))
        if result.returncode:
            return None
        try:
            clients = json.loads(result.stdout or "[]")
        except ValueError:
            return None
        return clients if isinstance(clients, list) else None

    def hyprland_window_region(self):
        """(x, y, w, h) of the emulator window on the host screen, or None.

        The launcher's own grim captures the whole deck; the picture lives in
        the emulator window, so the comparison samples that rectangle only. The
        configured compositor signature is tried first and the live one is
        healed in behind it (see hyprland_signature_candidates); None means the
        window could not be resolved at all, which callers must treat as "cannot
        measure" and never as licence to compare the desktop against the guest
        frame - that mismatch is what made an upright picture read as 0.6 and
        drove recovery actions against a correct render (measured 2026-09-18).
        """
        configured = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        for signature in hyprland_signature_candidates():
            clients = self.hyprctl_clients(signature)
            if clients is None:
                continue
            region = emulator_window_region(clients)
            if signature != configured:
                self.log("stage=hyprland-signature-healed", signature=signature,
                         configured=configured, window_region_known=bool(region))
            if region is not None:
                return region
        return None

    def host_render_grid(self):
        """Downsampled host screen (grim -> PPM), or None when unavailable.

        The window region used for the crop is cached on the launcher so the
        measurement log can report it without a second hyprctl call per attempt.
        """
        self._host_window_region = None
        ppm = self.run_dir / "render-verify.ppm"
        try:
            result = self.run(["grim", "-s", RENDER_VERIFY_SCALE, "-t", "ppm", str(ppm)],
                              timeout=10, check=False)
        except (LauncherError, OSError):
            return None
        if result.returncode or not ppm.is_file():
            return None
        try:
            raw = ppm.read_bytes()
        except OSError:
            return None
        self._host_window_region = self.hyprland_window_region()
        if self._host_window_region is None:
            # No crop, no measurement: sampling the whole deck against the guest
            # frame reports a mismatch for an upright picture, so an unresolved
            # window is reported as "cannot see" instead.
            return None
        return downsample_ppm(raw, RENDER_VERIFY_COLUMNS, RENDER_VERIFY_ROWS, self._host_window_region)

    def guest_render_grid(self):
        """Downsampled guest screen (raw screencap), or None when unavailable."""
        try:
            result = self.run_bytes([str(SDK / "platform-tools/adb"), "-P", str(ADB_PORT),
                                     "-s", SERIAL, "exec-out", "screencap"], timeout=15)
        except (LauncherError, OSError):
            return None
        if result.returncode:
            return None
        return downsample_rgba(result.stdout, RENDER_VERIFY_COLUMNS, RENDER_VERIFY_ROWS)

    def measure_render_offset(self, attempt: int):
        """(offset, error) of the host window against a paired guest frame.

        ``offset`` is what the host actually renders: 0 upright, 2 upside down,
        1 a 90/270 strip (the two strip quarters are not separable from the host
        frame alone). None when either frame could not be captured, so a caller
        can never mistake "no picture" for "upright". Every measurement is
        logged: the offset is the only evidence that the window is upright.
        """
        host = self.host_render_grid()
        guest = self.guest_render_grid()
        if host is None or guest is None:
            self.log("stage=display-render-unmeasured", error="frame capture unavailable",
                     attempt=attempt, host_frame=host is not None, guest_frame=guest is not None)
            return None
        offset, error = render_offset(host, guest)
        self._last_measured_offset = offset
        self._last_host_band = blank_band_ratio(host)
        self.log("stage=display-render-measured", offset=offset, error=round(error, 4),
                 attempt=attempt, host_band_ratio=round(self._last_host_band, 3),
                 window_region=str(bool(getattr(self, "_host_window_region", None))))
        return offset, error

    def measure_settled_frame(self, attempt: int):
        """(offset, error) of a frame that has actually presented.

        Two consecutive measurements that agree on a frame with content are a
        verdict; a blank frame (the content-free band covering the whole window)
        is a window that has not painted yet, so it is re-read instead of acted
        on. Bounded: at most RENDER_PRESENT_ATTEMPTS reads, and None when the
        window never presented or no pair could be captured - which the caller
        reports as unverified rather than guessing.
        """
        previous, measured = None, None
        for _ in range(RENDER_PRESENT_ATTEMPTS):
            measured = self.measure_render_offset(attempt)
            if measured is None:
                return None
            offset = measured[0]
            if offset == previous and self._last_host_band < 1.0:
                return measured
            previous = offset
            time.sleep(RENDER_VERIFY_SETTLE_SECONDS)
        if self._last_host_band >= 1.0:
            return None  # nothing painted: no verdict to act on
        return measured

    def display_pin_path(self) -> Path:
        """Where the last verified display pin is persisted (beside the run dirs)."""
        return self.run_dir.parent / DISPLAY_PIN_FILE

    def load_display_pin(self) -> int | None:
        """Guest display quarter the last verified align enforced, or None.

        Absent, unreadable, or out-of-range state is not an error: it just means
        this lane has no proven orientation to hold and must run the full align.
        """
        try:
            data = json.loads(self.display_pin_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        rotation = data.get("user_rotation") if isinstance(data, dict) else None
        return rotation if isinstance(rotation, int) and 0 <= rotation <= 3 else None

    def save_display_pin(self, rotation: int) -> None:
        """Record the quarter a verified render was aligned against."""
        row = {"schema": 1, "user_rotation": int(rotation),
               "verified_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            self.display_pin_path().write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as error:
            self.log("display-pin-persist-failed", error=str(error)[:200])

    def hold_guest_display_pin(self, rotation: int) -> bool:
        """Apply the persisted pin and measure it - no probe, no rotate.

        The picture is the only check: the pin is enforced, the deterministic
        roll is parked, and RENDER_HOLD_READS settled guest/host pairs must all
        read upright before the hold is honoured - the window is still settling
        right after the game resumes, so a single read is not a verdict. A hold
        that does not hold is unpinned and handed to the full align, so the fast
        path can never keep a wrong orientation.
        """
        self.lock_guest_display_rotation(rotation)
        self.console_send(format_sensor_set(NEUTRAL_ACCELERATION).decode().strip())
        time.sleep(RENDER_VERIFY_SETTLE_SECONDS)
        offsets: list[int | None] = []
        for read in range(1, RENDER_HOLD_READS + 1):
            measured = self.measure_settled_frame(read)
            offsets.append(measured[0] if measured else None)
            if measured is None or measured[0] != 0:
                break
            if read < RENDER_HOLD_READS:
                time.sleep(RENDER_VERIFY_SETTLE_SECONDS)
        if offsets == [0] * RENDER_HOLD_READS:
            self._aligned_guest_rotation = rotation
            self.save_display_pin(rotation)
            self.log("stage=display-pin-honoured", user_rotation=rotation,
                     reads=len(offsets))
            return True
        self._aligned_guest_rotation = None
        self.log("stage=display-pin-missed", user_rotation=rotation,
                 measured_offsets=offsets)
        self.unpin_guest_display_rotation()
        return False

    def unpin_guest_display_rotation(self) -> None:
        """Release the orientation pin so the sensor channel can move the display.

        The pin (``accelerometer_rotation 0``) is what makes a wrong render
        unrecoverable: with it set neither the emulator's ``rotate`` nor a sensor
        probe moves the guest display, so a strip measured after the pin stays
        for the life of the lane (measured 2026-09-18).
        """
        try:
            self.adb("shell", "settings", "put", "system", "accelerometer_rotation", "1", timeout=10)
        except (LauncherError, OSError) as error:
            self.log("guest-display-rotation-unpin-failed", error=str(error)[:200])
            return
        self._guest_rotation_locked = False

    def recover_render_offset(self, action: str) -> None:
        """Send one bounded, measured recovery action for a non-upright window.

        ``probe`` releases the pin and drives the accelerometer to the pose the
        emulator re-derives the upright window offset from (see
        RENDER_RECOVERY_PLAN); ``rotate`` sends one console rotate. Both re-park
        at the deterministic pose afterwards: the emulator only re-lays out its
        window from a guest rotation *change*, and the parked pose is the state
        that never proposes one.
        """
        if action == "probe":
            self.unpin_guest_display_rotation()
            self.console_send(format_sensor_set(RENDER_RECOVERY_ACCELERATION).decode().strip())
            time.sleep(RENDER_VERIFY_SETTLE_SECONDS)
        else:
            self.console_send("rotate")
        self.console_send(format_sensor_set(NEUTRAL_ACCELERATION).decode().strip())
        self.log("stage=display-render-recovery", action=action)

    def verify_visible_render(self, rotation: int = 0):
        """Measure the render against the guest and correct the window offset.

        Returns the measured quarter offset (0 = upright) or None when the frames
        could not be compared. Bounded: at most RENDER_VERIFY_MAX_ATTEMPTS
        measurements, each recovery action followed by a fresh guest/host pair
        and the deterministic re-park, and every outcome is logged - a
        verification that cannot see the screen says so instead of claiming
        success.
        """
        if getattr(self.args, "headless", False):
            return None
        offset, error = 1, None
        for attempt in range(1, RENDER_VERIFY_MAX_ATTEMPTS + 1):
            measured = self.measure_settled_frame(attempt)
            if measured is None:
                self._last_measured_offset = None
                self.log("stage=display-render-unverified",
                         error="no settled guest/host pair (window not presented)", attempt=attempt)
                return None
            offset, error = measured
            if offset == 0:
                self.log("stage=display-render-verified", offset=0, error=round(error, 4),
                         attempts=attempt)
                return 0
            if attempt == RENDER_VERIFY_MAX_ATTEMPTS:
                break
            self.recover_render_offset(RENDER_RECOVERY_PLAN[(attempt - 1) % len(RENDER_RECOVERY_PLAN)])
            time.sleep(RENDER_VERIFY_SETTLE_SECONDS)  # let the window rebuild
        self.log("stage=display-render-unverified", error="not upright after the bounded recovery plan",
                 last_offset=offset, error_value=round(error, 4),
                 window_region=str(bool(getattr(self, "_host_window_region", None))))
        return None

    def lock_guest_display_rotation(self, rotation: int) -> bool:
        """Pin the guest display quarter so tilt cannot re-derive the window.

        The emulator re-derives its window rotation whenever the guest display
        rotation changes and lands a quarter turn off, so in Tilt Drive - where
        the guest picks its quarter from the live accelerometer - the host render
        flips between upright, sideways strip and upside-down *between two poll
        ticks* (measured 2026-09-18: ten 2 s rotation samples stayed on rotation
        1 while the render went 180 degrees off, which no compensating guard can
        see). Pinning the quarter removes the cause: the accelerometer *data*
        stays live for tilt steering, only the display orientation stops moving.
        One read-back confirms the write; a guest that refuses it leaves the lane
        running and says so in the log.
        """
        try:
            self.adb("shell", "settings", "put", "system", "accelerometer_rotation", "0", timeout=10)
            self.adb("shell", "settings", "put", "system", "user_rotation", str(int(rotation)), timeout=10)
            readback = self.adb("shell", "settings", "get", "system", "user_rotation", timeout=10).stdout.strip()
        except (LauncherError, OSError) as error:
            self.log("guest-display-rotation-lock-skipped", error=str(error)[:200])
            return False
        self._guest_rotation_locked = readback == str(int(rotation))
        if self._guest_rotation_locked:
            self.log("stage=guest-display-rotation-locked", user_rotation=int(rotation))
        else:
            self.log("guest-display-rotation-lock-unconfirmed", readback=readback[:40])
        return self._guest_rotation_locked

    def align_visible_emulator(self) -> None:
        """Rotate the host window until it renders the guest upright.

        Runs once the game window has resumed, because the guest's
        SENSOR_LANDSCAPE display rotation is only settled by then. A lane whose
        last align was verified holds that orientation instead of re-deriving it:
        the persisted pin is applied, the deterministic roll is parked, and one
        settled guest/host pair decides (see hold_guest_display_pin). Only a hold
        that does not measure upright falls through to the full align, where the
        emulator rebuilds its window layout only when it observes a guest
        rotation change while its window is up, so one deliberate quarter-turn is
        provoked first (guest-rotation change) and the offset is then corrected
        with console rotates - the only supported lever, since emulator 32.1.15
        exposes no launch flag, AVD setting, or readable getter for the host
        window rotation.

        The guest-rotation delta supplies the *candidate* step count only, and is
        never trusted on its own: measured 2026-09-18 the console `rotate` also
        advances the guest display quarter, so the offset model can be off by a
        quarter while every row of its arithmetic still looks right (the lane
        ended in a 90-degree strip with `rotate_steps=2` logged). The picture is
        the authority: verify_visible_render compares the launcher's own grim of
        the emulator window with a paired guest screencap and drives the bounded
        recovery plan (see RENDER_RECOVERY_PLAN) until the frames match - or logs
        that it could not see. The display is pinned only for a render whose
        measurement says upright, because the pin is what stops the accelerometer
        from moving it.
        """
        if getattr(self.args, "headless", False):
            return
        if not self.desktop_compositor_available():
            # No desktop compositor: gamescope presents the emulator window on a
            # fixed output, so there is no window rotation to measure or correct,
            # and provoking a guest rotation would restart the game's
            # SENSOR_LANDSCAPE activity. Locking the parked landscape quarter is
            # what keeps the picture upright; the host is left alone.
            self.log("display-alignment-skipped",
                     reason="no desktop compositor (gamescope session)")
            self.lock_guest_display_rotation(PARKED_GUEST_ROTATION)
            return
        hint = self.load_display_pin()
        if hint is not None and self.hold_guest_display_pin(hint):
            # The proven orientation held: no provoke cycle and no rotate at
            # startup, which is the whole point of persisting it.
            return
        rotation = self.wait_for_guest_display_rotation()
        if rotation is None:
            self.log("display-alignment-skipped", reason="guest display rotation never settled landscape")
            return
        moved = self.provoke_guest_display_rotation(rotation)
        if moved is None:
            self.log("display-alignment-skipped", reason="guest display rotation did not move",
                     guest_rotation=rotation)
            # The window could not be re-derived, but the quarter the guest did
            # settle on can still be pinned: that is what stops Tilt Drive from
            # flipping the render under the user later (measured 2026-09-18).
            self.lock_guest_display_rotation(rotation)
            return
        steps = self.host_rotation_steps(moved, rotation)
        for _ in range(steps):
            self.console_send("rotate")
        self.log("stage=display-orientation-candidate", guest_rotation=moved,
                 provoked_from=rotation, rotate_steps=steps, source="guest-rotation-delta")
        # The picture decides. Every correction below moves the display and
        # re-parks, so the pin (and the rotation the guard maintains) comes after
        # the corrections settle.
        verified = self.verify_visible_render(moved)
        settled = self.guest_display_rotation()
        self._aligned_guest_rotation = settled if settled is not None else moved
        self.log("stage=display-orientation-aligned", guest_rotation=self._aligned_guest_rotation,
                 provoked_from=rotation, rotate_steps=steps,
                 measured_offset=getattr(self, "_last_measured_offset", None))
        if verified != 0 and getattr(self, "_last_measured_offset", None) is not None:
            # A render that was measured and is NOT upright must not be pinned:
            # the pin is what freezes the display against the accelerometer, so
            # pinning here is what made a wrong orientation permanent for the
            # rest of the lane (measured 2026-09-18). Leaving it unpinned keeps
            # the recovery channel open for the guard and the next attempt.
            self.unpin_guest_display_rotation()
            self.log("stage=display-orientation-unpinned",
                     reason="render measured not upright; not freezing it with the pin",
                     last_offset=getattr(self, "_last_measured_offset", None))
            return
        self.save_display_pin(self._aligned_guest_rotation)
        self.lock_guest_display_rotation(self._aligned_guest_rotation)

    def guard_display_orientation(self) -> None:
        """Keep the host window upright after the guest display rotation moves.

        The emulator re-derives its window rotation whenever the guest display
        rotation changes, and lands 90 degrees off — the game then renders as a
        sideways portrait strip. The guest requests SENSOR_LANDSCAPE, so this can
        only happen if something feeds its accelerometer while the Deck is
        handled (Gamepad mode parks the feed; Tilt Drive streams it by design).
        One short ADB read per polling tick, and only when the launcher has
        aligned at least once: a stable session issues no rotates at all.

        Thrash rules (user directive 2026-09-18): one correction burst per flip,
        never a second burst inside GUARD_ROTATE_COOLDOWN, a full stabilization
        window after each burst, the sensor re-parked flat afterwards, and no
        correction at all while the guest sits on the parked rotation.        """
        if getattr(self.args, "headless", False):
            return
        if self._aligned_guest_rotation is None:
            # Nothing has been aligned, so there is no relation to maintain: the
            # align stage owns that state and logs its own skip reason. Reading
            # the rotation here would cost an ADB call on every tick of a lane
            # that never aligned, and could only produce a guess.
            return
        now = time.monotonic()
        if now < self._guard_quiet_until:
            return  # still inside the stabilization window after a burst
        try:
            rotation = self.guest_display_rotation()
        except Exception as error:  # noqa: BLE001 - the guard must never abort the poll
            # guest_display_rotation already reports its own failures; this is the
            # last line of defence for a caller (the poll) that cannot take one.
            if not self._guard_read_failed:
                self._guard_read_failed = True
                self.log("display-orientation-read-failed", error=str(error)[:120])
            return
        if rotation is None:
            return
        previous = self._aligned_guest_rotation
        if rotation == previous:
            return
        if self._guard_rotated_for == rotation:
            return  # this quarter has already been corrected once
        if getattr(self, "_guest_rotation_locked", False):
            # A pinned quarter cannot flip outside Tilt Drive, so there is
            # nothing to repair: this state means the pin did not hold.
            return
        if (self._guard_last_rotate_at is not None
                and now - self._guard_last_rotate_at < GUARD_ROTATE_COOLDOWN):
            return  # never two bursts inside the cooldown, however the guest flaps
        steps = self.host_rotation_steps(rotation, previous)
        for _ in range(steps):
            self.console_send("rotate")
        # Re-park: the guest is only re-derived from a change, and leaving the
        # feed live is what makes a corrected rotation move again.
        self.console_send(format_sensor_set(NEUTRAL_ACCELERATION).decode().strip())
        self._guard_last_rotate_at = now
        self._guard_rotated_for = rotation
        self._guard_quiet_until = now + GUEST_ROTATION_SETTLE_TIMEOUT
        self.log("display-orientation-repaired", guest_rotation=rotation,
                 previous_rotation=previous, rotate_steps=steps)
        self._aligned_guest_rotation = rotation

    def isolate_guest(self) -> None:
        self.log("stage=trusted-root-isolation")
        root = self.run([str(SDK / "platform-tools/adb"), "-P", str(ADB_PORT), "-s", SERIAL, "root"], timeout=25, check=False)
        (self.run_dir / "adb-root.txt").write_text(root.stdout + root.stderr, encoding="utf-8")
        if root.returncode:
            raise LauncherError("adb root failed; refusing unverified guest isolation")
        self.wait_for_adb_device("root", 30)
        identity = self.adb("shell", "id").stdout
        (self.run_dir / "uid-root.txt").write_text(identity, encoding="utf-8")
        if not exact_uid(identity, 0):
            raise LauncherError(f"root identity is not uid=0: {identity.strip()}")
        result = self.run(self.isolation_argv(), timeout=20, check=False)
        (self.run_dir / "isolation-command.txt").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise LauncherError("guest network/UHID isolation failed")
        link = self.adb("shell", "ip", "-o", "link", "show", "up").stdout
        ipv4 = self.adb("shell", "ip", "-o", "route", "show").stdout
        ipv6 = self.adb("shell", "ip", "-6", "-o", "route", "show").stdout
        ok, detail = isolated_network(link, ipv4, ipv6)
        (self.run_dir / "network-check.txt").write_text(f"{detail}\n\nLINK UP:\n{link}\nIPv4:\n{ipv4}\nIPv6:\n{ipv6}", encoding="utf-8")
        if not ok:
            raise LauncherError(detail)
        self.log("network-check", detail=detail)
        wifi_settings = self.adb("shell", "settings", "get", "global", "wifi_on", timeout=15, check=False)
        wifi_dump = self.adb("shell", "dumpsys", "wifi", timeout=15, check=False)
        wifi_evidence = wifi_settings.stdout + "\n" + wifi_settings.stderr + "\n\n" + wifi_dump.stdout + wifi_dump.stderr
        (self.run_dir / "wifi-check.txt").write_text(wifi_evidence, encoding="utf-8")
        if not wifi_disabled(wifi_settings.stdout, wifi_dump.stdout):
            raise LauncherError("guest Wi-Fi is not verified disabled")
        self.log("wifi-check", detail="wifi disabled", settings=wifi_settings.stdout.strip())
        unroot = self.run([str(SDK / "platform-tools/adb"), "-P", str(ADB_PORT), "-s", SERIAL, "unroot"], timeout=25, check=False)
        (self.run_dir / "adb-unroot.txt").write_text(unroot.stdout + unroot.stderr, encoding="utf-8")
        self.wait_for_adb_device("unroot", 30)
        shell_id = self.adb("shell", "id").stdout
        (self.run_dir / "uid-verified.txt").write_text(shell_id, encoding="utf-8")
        if unroot.returncode or not exact_uid(shell_id, 2000):
            raise LauncherError(f"unroot did not produce exact shell uid=2000: {shell_id.strip()}")
        access = self.adb("shell", "sh", "-c", "id; test \"$(id -u)\" = 2000 && test -r /dev/uhid && test -w /dev/uhid && ls -l /dev/uhid", timeout=15, check=False)
        (self.run_dir / "uhid-shell-permissions.txt").write_text(access.stdout + access.stderr, encoding="utf-8")
        if access.returncode or not exact_uid(access.stdout, 2000):
            raise LauncherError("shell UID 2000 cannot read/write /dev/uhid")
        self.log("stage=guest-unrooted", uid=2000, uhid="shell-readable-writable")

    def seed_guest_media_volume(self) -> None:
        """Raise the guest STREAM_MUSIC index so the race mix is audible.

        One guest setting write through the emulator's ``media`` tool plus an
        independent read-back, recorded as ``stage=media-volume-seeded`` with
        the guest's actual index. A guest that refuses the write is logged as
        skipped rather than failing the launch: the mix is otherwise carried by
        the persisted guest volume and the host sink.
        """
        try:
            volume = seed_media_volume(self.adb)
        except (LauncherError, OSError) as error:
            self.log("stage=media-volume-seed-skipped", error=str(error)[:200])
            return
        self.log("stage=media-volume-seeded", **volume)

    def verify_packages(self) -> None:
        required = required_packages()
        installed = set(re.findall(r"^package:(\S+)\s*$", self.adb("shell", "pm", "list", "packages").stdout, re.MULTILINE))
        missing = [name for name in required if name not in installed]
        if missing:
            raise LauncherError(f"required guest packages are absent: {', '.join(missing)}; refusing install or restore")
        self.log("stage=packages-present", game=GAME, required=required)

    def start_controller(self) -> None:
        ready = self.run_dir / "controller-ready.json"
        argv = [str(CONTROLLER), "--helper-jar", str(HELPER_JAR), "--adb-path", str(SDK / "platform-tools/adb"),
                "--adb-port", str(ADB_PORT), "--adb-serial", SERIAL, "--mapping", str(DIST / "controller/mapping.json"),
                "--ready-file", str(ready), "--replay", "-", "--trace", "--live"]
        self.controller = self.spawn("controller", argv, "controller.stdout", "controller.stderr")
        self.controller_input = self.controller.process.stdin
        if self.controller_input is None:
            raise LauncherError("controller stdin pipe was not created")
        self.log("stage=controller-start", helper="persistent-app_process", input_socket=str(self.run_dir / "input.sock"))
        wait_until(lambda: self.controller is not None and self.controller.process.poll() is None and ready.exists(), 30, "controller READY")
        try:
            marker = json.loads(ready.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LauncherError(f"controller READY is invalid: {error}") from error
        device_id = marker.get("device_id")
        if marker.get("serial") != SERIAL or marker.get("transport") != "uhid" or not isinstance(device_id, int) or device_id < 0:
            raise LauncherError(f"controller READY has unexpected fields: {marker!r}")
        input_dump = self.adb("shell", "dumpsys", "input", timeout=20).stdout
        (self.run_dir / "input-dumpsys-ready.txt").write_text(input_dump, encoding="utf-8")
        if not input_device_matches(input_dump, device_id):
            raise LauncherError(f"READY device {device_id} is not the actual Android JCS2 InputDevice")
        self.log("stage=controller-ready", serial=SERIAL, transport="uhid", device_id=device_id, input_device="matched")

    def launch_game(self) -> None:
        self.log("stage=game-launch")
        self.adb("shell", "monkey", "-p", GAME, "1", timeout=25)
        wait_until(lambda: resumed_package(self.adb("shell", "dumpsys", "activity", "activities", timeout=20, check=False).stdout) == GAME,
                   30, "game resumed activity")
        activity = self.adb("shell", "dumpsys", "activity", "activities", timeout=20).stdout
        (self.run_dir / "activities-resumed.txt").write_text(activity, encoding="utf-8")
        if self.window_guard is not None:
            (self.run_dir / "game-window-reveal.json").write_text(json.dumps({"resumed_package": GAME}) + "\n")
        self.log("stage=running", resumed_package=GAME)

    def claim_deck_pad(self) -> None:
        """Take the built-in pad from the Desktop-Mode mapper before the bridge.

        In Desktop Mode ``deck-input-mapper`` holds an exclusive EVIOCGRAB on
        the pad's evdev node, and the kernel then delivers that device's events
        to the grabbing handle only: the bridge's ``/dev/input/js0`` receives
        nothing but its open-time state snapshot, so every physical press is
        lost without a single error (measured on this Deck 2026-09-18: zero live
        js0 events in seven hours of capture while the user was pressing
        controls).  The pair in deck_pad is lane-scoped, reversed in cleanup(),
        and idempotent; a lane that cannot claim the pad still launches, with
        the reason logged here and named again by the bridge on its own stderr.
        """
        mode = getattr(self.args, "input_mode", "")
        if mode != "joystick":
            # Only the joystick lane reads the pad; a QA-socket lane leaves the
            # Desktop-Mode mapper holding it, so the operator keeps the pointer.
            self.log("stage=deck-pad-claim-skipped",
                     reason=f"input mode {mode or 'unknown'} does not read the pad")
            return
        try:
            state = claim_pad()
        except (DeckPadError, OSError, subprocess.SubprocessError) as error:
            self.log("stage=deck-pad-claim-skipped", error=str(error)[:200])
            return
        self._deck_pad_state = state
        (self.run_dir / "deck-pad-ownership.json").write_text(
            json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        self.log("stage=deck-pad-claimed", **state)

    def start_input(self) -> None:
        self.router = InputRouter(self, self.controller_input)
        self.router.start_socket(self.run_dir / "input.sock")
        if self.args.input_mode == "joystick":
            self.router.start_tilt()
            bridge = self.router.start_bridge(ROOT / "linux-launcher/joystick_bridge.py")
            wait_until(lambda: bridge.process.poll() is None, 5, "joystick bridge")
            self.log("stage=input-ready", mode="joystick", socket=str(self.run_dir / "input.sock"))
        else:
            self.log("stage=input-ready", mode="qa-socket", socket=str(self.run_dir / "input.sock"))

    def read_resumed_package(self) -> tuple[str | None, bool | None, bool, str]:
        """Poll the guest foreground; return (package, game_task, confirmed, detail).

        ``game_task`` is the tri-state :func:`game_task_state` of the same
        dumpsys snapshot (True = game task still listed, False = finished,
        None = unknown shape). ``confirmed`` is True only when dumpsys
        succeeded AND a resumed activity line was found. Anything else (ADB
        transport error, nonzero return code, missing resumed line during
        an activity transition) is unknown and must never count as a quit.
        """
        try:
            result = self.adb("shell", "dumpsys", "activity", "activities", timeout=20, check=False)
        except (LauncherError, OSError) as error:
            return None, None, False, f"adb-error: {error}"
        if result.returncode != 0:
            return None, None, False, f"adb-rc={result.returncode}: {compact(result.stderr or result.stdout)}"
        package = resumed_package(result.stdout)
        if package is None:
            return None, None, False, "no-resumed-activity-line"
        return package, game_task_state(result.stdout), True, ""

    def is_game_process_alive(self) -> tuple[bool | None, str]:
        """Check the guest game process; (alive, detail).

        ``alive`` is None when the query itself failed (transport error or
        unexpected return code) — unknown, never evidence. toybox pidof
        returns 1 with empty output when no process matches; aliveness is
        decided by non-empty output, not by the return code alone.
        """
        try:
            result = self.adb("shell", "pidof", GAME, timeout=10, check=False)
        except (LauncherError, OSError) as error:
            return None, f"pidof-error: {error}"
        if result.returncode not in (0, 1):
            return None, f"pidof-rc={result.returncode}: {compact(result.stderr or result.stdout)}"
        return bool(result.stdout.strip()), ""

    def read_owned_window_identity(self) -> dict | None:
        """Read the recorded window-guard ready file (owned pid + main xid).

        Written once by the owned helper when the game window became ready;
        this is the launch's own window identity, not a fresh discovery.
        Returns None when the file is missing or malformed (helper never
        became ready) — callers must fall back to a validated inventory
        match, never to an unvalidated one. Never raises.
        """
        try:
            identity = json.loads((self.run_dir / "gamescope-window-ready.json").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(identity, dict):
            return None
        if not isinstance(identity.get("pid"), int) or not isinstance(identity.get("main_xid"), int):
            return None
        return identity

    def hide_owned_emulator_window(self, opener=None) -> None:
        """Best-effort unmap of the owned emulator main window on quit.

        GamingMode keeps the mapped emulator (Android home) on screen until
        the emulator process exits during cleanup (~11-17 s later). Unmapping
        hides it promptly so Steam returns to the gaming UI. Owned-only:
        the recorded ready-file identity (owned pid + main xid) is
        re-validated against a fresh inventory (same pid AND Emulator
        class); only then is that xid unmapped. Fallback is an inventory
        match on owned pid + Emulator class + main-window title. A confirmed
        Quit ends the session, so any OTHER mapped window owned by the same
        emulator pid (shutdown popups/transients — Sept-14 GamingMode left
        owned xid 10485785 mapped while only the main was hidden) is hidden
        too. Foreign processes are never touched. The ready file
        is only ever READ here — a hide (or its headless skip) never marks
        any window ready. Never raises: headless QA has no DISPLAY, and a
        hide failure must never block the quit.
        """
        try:
            emulator = self.emulator
            pid = emulator.process.pid if emulator is not None else None
            if pid is None:
                self.log("game-hide-skip", reason="no-owned-emulator")
                return
            if not os.environ.get("DISPLAY"):
                self.log("game-hide-skip", reason="no-display")
                return
            if opener is None:
                from gamescope_window import X11
                opener = X11
            adapter = opener()
            try:
                owned = [window for window in adapter.inventory()
                         if window.pid == pid and "Emulator" in window.classes]
                if not owned:
                    self.log("game-hide-skip", reason="no-owned-windows")
                    return
                targets: list = []
                via = "inventory-fallback"
                identity = self.read_owned_window_identity()
                if identity is not None and identity.get("pid") == pid:
                    recorded = [window for window in owned
                                if window.xid == identity.get("main_xid") and window.mapped]
                    if recorded:
                        targets, via = recorded, "recorded-identity"
                if not targets:
                    targets = [window for window in owned
                               if window.title.startswith("Android Emulator - ")
                               and window.mapped]
                seen = {window.xid for window in targets}
                extras = [window for window in owned
                          if window.mapped and window.xid not in seen]
                if extras and not targets:
                    via = "owned-transients-only"
                targets += extras
                if not targets:
                    self.log("game-hide-skip", reason="no-mapped-owned-main")
                    return
                for window in targets:
                    adapter.unmap(window.xid)
                self.log("stage=game-hide", pid=pid,
                         xids=[window.xid for window in targets], via=via)
            finally:
                adapter.close()
        except Exception as error:
            try:
                self.log("game-hide-skip",
                         reason=f"{type(error).__name__}: {error}"[:200])
            except Exception:
                pass

    def begin_quit_teardown(self) -> None:
        """Start the owned shutdown the moment a Quit is confirmed.

        Hide first (prompt GamingMode return), then SIGTERM the owned
        emulator group immediately instead of letting cleanup's sequential
        drain (5 s voluntary wait per child: bridge → controller →
        window-guard → emulator → adb-server, ~22 s observed) leave
        Android home up for ~17 s. Owned pgid only — never a broad kill;
        Paseo/Steam/the session live in other groups. The window helper
        exits on its own via its emulator pidfd; ``cleanup()`` still reaps
        every child and logs each ``process-exit`` row. Never raises.
        """
        try:
            self.hide_owned_emulator_window()
        except Exception as error:
            try:
                self.log("game-hide-skip",
                         reason=f"{type(error).__name__}: {error}"[:200])
            except Exception:
                pass
        emulator = self.emulator
        if emulator is None:
            return
        try:
            if emulator.process.poll() is None:
                os.killpg(emulator.pgid, signal.SIGTERM)
                self.log("stage=game-teardown", name="emulator",
                         pid=emulator.process.pid, signal="TERM")
        except ProcessLookupError:
            pass
        except Exception as error:
            try:
                self.log("game-teardown-skip",
                         reason=f"{type(error).__name__}: {error}"[:200])
            except Exception:
                pass

    def run_until_stop(self) -> int:
        started = time.monotonic()
        quit_streak = 0
        next_game_poll = started + GAME_QUIT_POLL_INTERVAL
        while not self.stop_requested:
            if self.emulator and self.emulator.process.poll() is not None:
                raise LauncherError("emulator exited while game was running")
            if self.window_guard and self.window_guard.process.poll() is not None:
                raise LauncherError("Gaming Mode window helper exited; see gamescope-window.log")
            if self.controller and self.controller.process.poll() is not None:
                raise LauncherError("controller/helper exited while game was running")
            if self.router and self.router.bridge and self.router.bridge.process.poll() is not None:
                raise LauncherError("joystick bridge exited while game was running")
            if self.router:
                self.router.pump(0.25)
            if time.monotonic() - started > GAMEPLAY_TIMEOUT:
                raise LauncherError("gameplay monitoring timeout")
            now = time.monotonic()
            if now < next_game_poll:
                continue
            next_game_poll = now + GAME_QUIT_POLL_INTERVAL
            self.guard_display_orientation()
            resumed, task_present, confirmed, detail = self.read_resumed_package()
            if not confirmed:
                # Unknown sample breaks consecutiveness: reset the window so
                # sparse polls across glitches can never accumulate into a
                # quit. A genuine quit simply re-confirms on later polls.
                if quit_streak:
                    self.log("game-poll-reset", reason=detail, streak=quit_streak)
                else:
                    self.log("game-poll-unknown", detail=detail)
                quit_streak = 0
                continue
            if resumed == GAME:
                if quit_streak:
                    self.log("game-poll-recovered", resumed_package=resumed, streak=quit_streak)
                quit_streak = 0
                continue
            if resumed not in HOME_PACKAGES:
                # Permission dialogs, Settings, other apps: never quit
                # evidence, even when sustained.
                if quit_streak:
                    self.log("game-poll-reset", reason=f"unrelated-foreground: {resumed}",
                             streak=quit_streak)
                else:
                    self.log("game-poll-unrelated", resumed_package=resumed)
                quit_streak = 0
                continue
            # Home/launcher foreground: only half the evidence. A live game
            # process WITH its task still listed means temporary backgrounding
            # (user pressed Home, transient focus switch), not an accepted
            # Quit. A live process whose task is GONE finished its Activity:
            # the process is only cached, and that is a genuine Quit.
            alive, alive_detail = self.is_game_process_alive()
            if alive is None:
                if quit_streak:
                    self.log("game-poll-reset", reason=alive_detail, streak=quit_streak)
                else:
                    self.log("game-poll-unknown", detail=alive_detail)
                quit_streak = 0
                continue
            task_label = ("present" if task_present is True
                          else "finished" if task_present is False else "unknown")
            if alive:
                if task_present is False:
                    # Cached-process Quit: fall through to the quit candidate
                    # below with the process state recorded exactly.
                    pass
                else:
                    if quit_streak:
                        self.log("game-poll-reset", reason="game-process-still-alive",
                                 streak=quit_streak)
                    else:
                        self.log("game-poll-backgrounded", resumed_package=resumed,
                                 game_task=task_label)
                    quit_streak = 0
                    continue
            quit_streak += 1
            self.log("game-poll-quit-candidate", resumed_package=resumed,
                     game_process=("ended" if not alive else "cached-task-finished"),
                     game_task=task_label, streak=quit_streak,
                     required=GAME_QUIT_CONFIRM_POLLS)
            if quit_streak >= GAME_QUIT_CONFIRM_POLLS:
                # Confirmed Quit: hide the owned emulator window promptly
                # (GamingMode would otherwise show Android home until the
                # emulator exits ~11-17 s later) and start the owned
                # emulator exiting now so cleanup's sequential drain does
                # not hold the shutdown open. Best-effort only; a teardown
                # failure must never block the quit.
                try:
                    self.begin_quit_teardown()
                except Exception as error:
                    try:
                        self.log("game-teardown-skip",
                                 reason=f"{type(error).__name__}: {error}"[:200])
                    except Exception:
                        pass
                self.log("stage=game-quit", resumed_package=resumed,
                         game_process=("ended" if not alive else "cached-task-finished"),
                         game_task=task_label,
                         confirm_polls=GAME_QUIT_CONFIRM_POLLS,
                         detail="native Quit accepted; cleaning owned processes")
                return 0
        return 130

    @staticmethod
    def endpoint_lock_path() -> Path:
        """Use a private per-user directory shared by every project copy."""
        uid = os.getuid()
        # Independent of XDG_RUNTIME_DIR: Desktop, Steam, and terminal
        # launches must contend on the same inode even with different envs.
        directory = Path("/tmp") / f"jcs2-launcher-{uid}"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
            raise LauncherError(f"launcher lock directory is not private: {directory}")
        return directory / "ports-5038-5594-5595.lock"

    @staticmethod
    def open_lock(path: Path, label: str) -> IO[bytes]:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        handle = os.fdopen(fd, "a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise LauncherError(f"{label} already owned by another launcher") from error
        except BaseException:
            handle.close()
            raise
        return handle

    def acquire_launch_locks(self) -> None:
        if self._endpoint_lock is not None:
            return
        self._endpoint_lock = self.open_lock(self.endpoint_lock_path(), "ADB/emulator ports")
        try:
            self.acquire_profile_lock()
        except BaseException:
            self._endpoint_lock.close()
            self._endpoint_lock = None
            raise

    def acquire_profile_lock(self) -> None:
        """Serialize launchers sharing an AVD, including their preflight checks."""
        if self._profile_lock is not None:
            return
        path = AVD_HOME / f"{AVD}.avd" / ".jcs2-launcher.lock"
        self._profile_lock = self.open_lock(path, f"AVD {AVD}")

    def release_profile_lock(self) -> None:
        handle, self._profile_lock = self._profile_lock, None
        if handle is not None:
            handle.close()  # Closing the non-inherited descriptor releases flock.

    def cleanup(self) -> None:
        errors = []
        def attempt(label, action):
            try:
                action()
            except Exception as error:
                errors.append(f"{label}: {error}")
        try:
            attempt("cleanup log", lambda: self.log("stage=cleanup-start"))
            if self.router:
                attempt("input router", self.router.close)
            if self.controller_input:
                def disconnect():
                    if not self.controller or self.controller.process.poll() is not None:
                        return
                    self.controller_input.write(b'{"type":"device","connected":false,"t_ms":0}\n')
                    self.controller_input.flush()
                attempt("controller disconnect", disconnect)
                attempt("controller stdin", self.controller_input.close)
            for owned in reversed(self.children):
                attempt(owned.name, lambda owned=owned: self.stop_owned(owned))
            if self._deck_pad_state is not None:
                pad_state, self._deck_pad_state = self._deck_pad_state, None
                attempt("deck pad release", lambda: self.log(
                    "stage=deck-pad-released", **release_pad(pad_state)))
            attempt("cleanup log", lambda: self.log("stage=cleanup-complete", errors=errors))
        finally:
            try:
                self.release_profile_lock()
            finally:
                handle, self._endpoint_lock = self._endpoint_lock, None
                if handle is not None:
                    handle.close()
        if errors:
            raise LauncherError("cleanup incomplete: " + "; ".join(errors))

    def stop_owned(self, owned: OwnedProcess) -> None:
        process = owned.process
        try:
            if process.poll() is None:
                try:
                    process.wait(timeout=10 if owned.name == "controller" else 5)
                except subprocess.TimeoutExpired:
                    pass
            # The leader may already be gone while descendants keep its group
            # alive. Always address the captured group, never discover by name.
            try:
                os.killpg(owned.pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while True:
                process.poll()  # Reap the leader so its zombie cannot retain the group.
                try:
                    os.killpg(owned.pgid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    try:
                        os.killpg(owned.pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                time.sleep(0.05)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired as error:
                raise LauncherError(f"owned process did not exit: {owned.name}") from error
        finally:
            errors = []
            handles = (*owned.log_handles, process.stdin, process.stdout, process.stderr)
            for handle in {id(h): h for h in handles if h is not None}.values():
                try:
                    handle.close()
                except Exception as error:
                    errors.append(str(error))
            if errors:
                raise LauncherError(f"cannot close {owned.name} resources: " + "; ".join(errors))
        self.log("process-exit", name=owned.name, pid=process.pid, returncode=process.returncode)

    def owned_progression_session(self):
        """Owned install target for an explicit progression-variant switch.

        Identity comes from live owned state: the runner-held lock files,
        this launch's AVD/ports/serial, and game-not-started (called before
        launch_game). Nothing is invented: serial/adb paths match the
        runner's own adb() transport exactly.
        """
        from progression_choice import OwnedSession
        return OwnedSession(
            serial=SERIAL,
            adb_prefix=(str(SDK / "platform-tools/adb"), "-P", str(ADB_PORT)),
            avd_name=AVD,
            console_port=CONSOLE_PORT,
            endpoint_lock=str(self.endpoint_lock_path()),
            profile_lock=str(AVD_HOME / f"{AVD}.avd" / ".jcs2-launcher.lock"),
            game_started=False)

    def maybe_apply_progression_switch(self) -> None:
        """Prelaunch consumption of an explicit progression choice.

        Acts ONLY on a fresh explicit intent (the progression panel arms
        one per explicit Save). No intent -> launch proceeds untouched;
        nothing here ever auto-switches. Outcomes from
        apply_pending_progression_switch: proceed normally, or abort the
        launch (LauncherError) when a switch attempt left the device
        uncertain — never launch a game on an unknown install.
        """
        try:
            from progression_choice import apply_pending_progression_switch
        except ImportError as error:
            self.log("warning", detail=f"progression module unavailable; launching current install ({error})")
            return
        try:
            action, message = apply_pending_progression_switch(
                self.owned_progression_session())
        except Exception as error:
            raise LauncherError(f"progression switch infrastructure failure; refusing to launch: {error}") from error
        self.log("stage=progression-choice", action=action, detail=message[:300])
        if action == "aborted":
            raise LauncherError(f"progression switch uncertain; launch aborted: {message[:300]}")

    def main(self) -> int:
        stages = (self.acquire_launch_locks, self.preflight, self.start_server,
                  self.start_emulator, self.orient_visible_emulator,
                  self.isolate_guest, self.seed_guest_media_volume,
                  self.maybe_apply_progression_switch,
                  self.verify_packages, self.start_controller, self.claim_deck_pad,
                  self.start_input,
                  self.launch_game, self.verify_gamescope_window,
                  self.align_visible_emulator)
        for stage in stages:
            if self.stop_requested:
                return 130
            stage()
        if self.stop_requested:
            return 130
        return self.run_until_stop()


class InputRouter:
    """Route controller events, apply driving controls, and inject tilt axes.

    Menus use the emulator's native touch/mouse path. D-pad and A/B are
    unassigned; routing never detects screens or synthesizes menu taps.
    """

    def __init__(self, launcher: Launcher, destination: IO[bytes] | None):
        self.launcher = launcher
        self.destination = destination
        self.selector = selectors.DefaultSelector()
        self.listener: socket.socket | None = None
        self.clients: dict[socket.socket, bytearray] = {}
        # Keep one byte buffer per producer.  Reading a BufferedReader with
        # readline() can prefetch a whole burst from the joystick pipe; the
        # selector then sees no readiness for the prefetched lines.  Producers
        # are read directly with nonblocking os.read() chunks instead.
        self.producer_buffers: dict[object, bytearray] = {}
        # Compatibility buffer for direct callers/tests of forward(); live
        # producers use producer_buffers so each stream is isolated.
        self.forward_buffer = bytearray()
        self.bridge: OwnedProcess | None = None
        self._tilt_adapter = None  # TiltAdapter for IMU steering/pitch
        self._sensor_transport = None  # ConsoleSensorTransport for native accel
        self._sensor_retry_at = 0.0  # backoff: don't reconnect every frame
        self._native_state = "unknown"  # live | unavailable | degraded
        self._desktop_compositor: bool | None = None  # cached Hyprland probe
        self._pose_logged = False  # one stage=guest-pose-deterministic row per lane
        self._native_error: str | None = None
        self._native_ever_live = False
        self._native_settled = False  # guest already holds our level value
        self._ctl_context: dict = {}  # last requested/available/error for status
        self._stick_axes = {"LX": 0.0, "LY": 0.0}
        self._stick_gate_held = False  # tilt owns LX/LY (neutral sent on engage)
        # Side-channel: raw-event Unix socket for driving control mapping
        self._side_channel: BridgeSideChannel | None = None
        # DeckControls: runner-owned instance for side-channel mode
        self._deck_controls = _DeckControls() if _DeckControls is not None else None
        self._side_channel_active = False

    def start_socket(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ, "listener")
        path.chmod(0o600)

    def start_bridge(self, bridge_path: Path) -> OwnedProcess:
        """Start the joystick bridge with optional raw-event side-channel.

        When BridgeSideChannel is available, creates a Unix socket pair and
        passes the write-end FD to the bridge.  The bridge sends raw events
        over this socket; the runner applies DeckControls before forwarding
        to the controller.

        The bridge's stdout still outputs NDJSON (raw events when side-channel
        is active, DeckControls-mapped when standalone) for backward compat.
        """
        stderr = (self.launcher.run_dir / "joystick.stderr").open("w", encoding="utf-8")

        try:
            bridge_argv = [sys.executable, str(bridge_path)]
            # Create side-channel if available
            bridge_fd = None
            if BridgeSideChannel is not None:
                self._side_channel = BridgeSideChannel()
                bridge_fd = self._side_channel.bridge_fd
                assert bridge_fd > 2, f"side-channel fd must be >2 (got {bridge_fd})"
                os.set_inheritable(bridge_fd, True)
                bridge_argv.extend(["--side-channel", str(bridge_fd)])
                self._side_channel_active = True

            bridge = subprocess.Popen(bridge_argv, stdout=subprocess.PIPE, stderr=stderr,
                                      start_new_session=True, env=self.launcher.environment(),
                                      pass_fds=[bridge_fd] if bridge_fd is not None else ())
        except BaseException:
            stderr.close()
            if self._side_channel is not None:
                self._side_channel.close()
                self._side_channel = None
                self._side_channel_active = False
            raise
        owned = OwnedProcess("joystick-bridge", bridge, (stderr,))
        self.launcher.children.append(owned)
        self.bridge = owned
        if bridge.stdout is None:
            raise LauncherError("joystick bridge stdout unavailable")
        os.set_blocking(bridge.stdout.fileno(), False)
        self.producer_buffers[bridge.stdout] = bytearray()
        self.selector.register(bridge.stdout, selectors.EVENT_READ, "bridge")
        self.bridge = owned

        # Register side-channel fd with selector for reading raw events
        if self._side_channel_active and self._side_channel is not None:
            sc_fd = self._side_channel._server_sock.fileno()
            os.set_blocking(sc_fd, False)
            self.producer_buffers["side_channel"] = bytearray()
            self.selector.register(self._side_channel._server_sock,
                                   selectors.EVENT_READ, "side_channel")
            self.launcher.log("bridge-side-channel", fd=sc_fd)

        self.launcher.log("spawn", name=owned.name, pid=bridge.pid, pgid=owned.pgid,
                          argv=bridge_argv)
        return owned

    def _tilt_steering_active(self) -> bool:
        """True when Tilt Drive owns analog steering (analog gate armed).

        FAIL-CLOSED on the persisted launcher mode ALONE: armed iff the
        persisted mode is TILT ("Tilt Drive"). Live IMU ownership is
        deliberately NOT consulted — a dropped/unowned sensor must never
        silently re-enable the physical sticks (that would double-drive
        against a recovering sensor feed and yank the car). Tilt Drive
        with a dead sensor therefore steers nothing; the dead feed is
        surfaced loudly via control-mode.json ``native_error``, telling the
        user to recover the sensor.
        Buttons and triggers are never gated — Tilt Drive disables LX/LY
        only. Reconnect-safe: no per-device state, mode is re-read from
        the persisted setting, so a re-attached pad resumes gated.
        """
        adapter = self._tilt_adapter
        if adapter is None or ControlMode is None:
            return False
        try:
            tilted = adapter.mode == ControlMode.TILT
        except AttributeError:
            return False
        return bool(tilted)

    def _handle_side_channel_event(self, event: dict) -> bool:
        """Map raw driving events; drop unassigned buttons and filtered axes.

        Analog-only gate (Tilt Drive): while persisted Tilt Drive mode is
        selected, LX/LY are dropped so stick and native tilt cannot
        double-drive — regardless of live sensor ownership (fail-closed;
        a dead sensor never silently re-enables sticks). Triggers, bumpers,
        and all other physical buttons always flow — Tilt Drive disables
        analog turning/pitch only. In Gamepad mode the physical stick is
        always forwarded; the game's own Gamepad toggle (kept ON)
        arbitrates guest-side.
        """
        self._cache_stick(event, raw=True)
        if not stick_gate_allows(event, self._tilt_steering_active()):
            return False
        if event.get("type") == "button" and event.get("key") in UNASSIGNED_BUTTONS:
            return False

        # DeckControls: transform raw Deck controls to game virtual keys
        if self._deck_controls is not None:
            translated = self._deck_controls.translate(event)
            if not translated:
                return False  # DeckControls filtered (DPAD, RX/RY, duplicate RT)
            # Forward each translated event
            for evt in translated:
                self._forward_event(evt)
        else:
            # No DeckControls available; forward raw event
            self._forward_event(event)

        return True

    def _forward_event(self, event: dict) -> None:
        """Forward a single event dict to the controller stdin."""
        if self.destination is None:
            return
        try:
            line = json.dumps(event, separators=(',', ':')) + '\n'
            self.destination.write(line.encode())
            self.destination.flush()
            with (self.launcher.run_dir / "input-events.ndjson").open("ab") as stream:
                stream.write(line.encode())
        except (BrokenPipeError, OSError) as error:
            raise LauncherError(f"controller stdin closed: {error}") from error

    def _forward_line(self, payload: bytes) -> None:
        """Forward a raw NDJSON line to the controller (legacy/stdout path).

        Analog-only gate applies here too: LX/LY drop while persisted Tilt
        Drive mode is selected (fail-closed, sensor-independent);
        buttons/triggers always flow (see _handle_side_channel_event).
        """
        if self.destination is None:
            return
        if not payload.strip():
            return
        try:
            event = json.loads(payload)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("event must be a JSON object with type")
                return
            if event["type"] not in ("axis", "button"):
                # Clients speak axis/button only; anything else (e.g. the
                # retired panel control_mode request) must never reach the
                # guest as a bogus event.
                raise ValueError(f"unsupported event type: {event['type']!r}")
            self._cache_stick(event)
            if not stick_gate_allows(event, self._tilt_steering_active()):
                return
            if event.get("type") == "button" and event.get("key") in UNASSIGNED_BUTTONS:
                return
            clean = (json.dumps(event, separators=(",", ":")) + "\n").encode()
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.launcher.log("input-rejected", error=str(error), payload=compact(payload.decode(errors="replace")))
            return
        try:
            self.destination.write(clean)
            self.destination.flush()
            with (self.launcher.run_dir / "input-events.ndjson").open("ab") as stream:
                stream.write(clean)
        except (BrokenPipeError, OSError) as error:
            raise LauncherError(f"controller stdin closed: {error}") from error

    def _read_producer(self, producer: object) -> bool:
        """Read all currently available bytes and forward every full line."""
        try:
            data = os.read(producer.fileno(), 65536)  # type: ignore[attr-defined]
        except (BlockingIOError, OSError):
            # EBADF / closed fd: drop the producer to prevent busy-loop.
            # The selector keeps reporting the fd as readable after close;
            # without this guard each pump cycle retries the same failed read.
            return False
        if not data:
            return False
        buffer = self.producer_buffers.setdefault(producer, bytearray())
        buffer.extend(data)
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            buffer[:] = remainder
            self._forward_line(line)
        return True

    def forward(self, payload: bytes) -> None:
        """Normalize complete lines for legacy/direct callers."""
        self.forward_buffer.extend(payload)
        while b"\n" in self.forward_buffer:
            line, _, remainder = self.forward_buffer.partition(b"\n")
            self.forward_buffer[:] = remainder
            self._forward_line(line)

    def _drop_producer(self, producer: object) -> None:
        try:
            self.selector.unregister(producer)
        except (KeyError, ValueError):
            pass
        self.producer_buffers.pop(producer, None)

    def _drop_client(self, client: socket.socket) -> None:
        self._drop_producer(client)
        client.close()
        self.clients.pop(client, None)

    def _read_side_channel(self) -> bool:
        """Read raw side-channel events and apply driving controls.

        Returns True if the side-channel is still alive.
        """
        if self._side_channel is None:
            return True
        events = self._side_channel.read_events()
        if not events and self._side_channel._closed:
            return False
        for event in events:
            self._handle_side_channel_event(event)
        return True

    def start_tilt(self, settings_path: str | None = None) -> bool:
        """Initialize the tilt adapter for IMU steering/pitch.

        The Deck IMU is owned whenever present: the native accelerometer
        mirror runs independently of the stick path. Driving source is the
        persisted launcher mode — Gamepad (physical sticks, always
        forwarded; the in-game Gamepad toggle stays ON) or Tilt Drive
        (LX/LY gated host-side, steering from the sensor mirror). The
        game's own OFF setting is NOT used: proven live (E-tilt v2) the
        game ignores sensor input with its Gamepad OFF, so Tilt Drive
        keeps the game ON. No synthetic IMU axes are ever produced: the
        router never calls ``consume_motion``. FAIL-CLOSED: a
        missing/silent sensor never changes the persisted mode and never
        re-enables sticks; the dead feed is reported loudly and the user
        selects Gamepad explicitly (reversible at any time).
        """
        if TiltAdapter is None:
            self.launcher.log("tilt-unavailable", reason="tilt_control not importable")
            return False

        tilt_settings_path = settings_path or str(
            self.launcher.run_dir.parent / "tilt-settings.json")
        try:
            from tilt_control.settings import TiltSettings
            settings = TiltSettings(tilt_settings_path)
            self._tilt_adapter = TiltAdapter(settings=settings)
            requested = settings.mode
            if requested == ControlMode.TILT and not TILT_DRIVE_SELECTABLE:
                # One-way migration (2026-09-18): the previous lane could persist
                # Tilt Drive, and honouring it here would arm a mode whose display
                # flips. Rewrite it once, visibly, before anything reads it.
                self.launcher.log("stage=tilt-drive-disabled", persisted_mode="tilt",
                                  reason="sensor-landscape display flip; no rotation-hold lever")
                self._tilt_adapter.select_mode(ControlMode.GAMEPAD)  # persists + updates the adapter
                requested = ControlMode.GAMEPAD
            available, error = self._open_motion(self._tilt_adapter)
            if requested == ControlMode.TILT and not available:
                # FAIL-CLOSED startup: keep persisted Tilt Drive (gate stays
                # armed); NEVER rewrite it to Gamepad silently. The error is
                # reported loudly below (not swallowed) so the user knows
                # sticks are disabled until they select Gamepad or the sensor
                # recovers.
                error = (
                    "Tilt Drive selected but the Deck motion sensor is "
                    f"unavailable ({error}). Physical sticks stay disabled; "
                    "the car will not steer until the sensor recovers or you "
                    "select Gamepad. The in-game Gamepad toggle stays ON.")
                self.launcher.log("tilt-native-unavailable", error=error,
                                  device_error=self._tilt_adapter.device_error,
                                  match_tier=self._tilt_adapter.match_tier,
                                  discovery=(self._tilt_adapter.discovery_snapshot or "")[:1200],
                                  persisted_mode="tilt")
            if requested != ControlMode.TILT and not available:
                # Sticks need no sensor; keep the failure out of the
                # panel error line and let the native status carry it.
                # device_error names the underlying IMU open/read reason
                # (Sept-14: user logs showed opened=False with no cause).
                # match_tier/discovery narrow in-session visibility gaps:
                # tier1 exact, tier2 name-only, tier3 capability fallback,
                # miss + sysfs inventory (names/ids/caps, never env).
                self.launcher.log("tilt-native-unavailable", error=error,
                                  device_error=self._tilt_adapter.device_error,
                                  match_tier=self._tilt_adapter.match_tier,
                                  discovery=(self._tilt_adapter.discovery_snapshot or "")[:1200])
                error = None
            self._write_control_status(requested.value, available, error)
            self.launcher.log("tilt-init", settings_path=tilt_settings_path,
                              opened=available, mode=self._tilt_adapter.mode.value,
                              device_error=self._tilt_adapter.device_error,
                              match_tier=self._tilt_adapter.match_tier,
                              discovery=(self._tilt_adapter.discovery_snapshot or "")[:1200])
            return True
        except Exception as error:
            self.launcher.log("tilt-error", error=str(error))
            self._tilt_adapter = None
            return False

    def _open_motion(self, adapter) -> tuple[bool, str | None]:
        """Accept Tilt only after the sensor opens and actually reports data."""
        if not adapter.open():
            return False, SENSOR_UNAVAILABLE
        if adapter.await_motion(MOTION_READY_TIMEOUT):
            return True, None
        adapter.close()
        return False, SENSOR_SILENT

    def _maybe_reprobe_imu(self, adapter) -> None:
        """Re-attempt IMU ownership when the startup probe failed (bounded).

        Retried at most every 5 s: ``open()`` is non-blocking and
        ``await_motion`` is capped at 0.2 s, so a starved IMU costs one
        short stall per window, never a per-frame spin. On recovery the
        estimator/poll path picks up on the next push and ``live`` is
        reported through the normal status update; continued failure
        stays silent here (the startup ``tilt-init`` row already carries
        the ``device_error`` reason). Never raises.
        """
        now = time.monotonic()
        if now < getattr(self, "_imu_reprobe_at", 0.0):
            return
        self._imu_reprobe_at = now + 5.0
        try:
            if adapter.open() and adapter.await_motion(0.2):
                self.launcher.log("tilt-imu-recovered",
                                  device_path=getattr(getattr(adapter, "_reader", None),
                                                      "device_path", None),
                                  match_tier=getattr(adapter, "match_tier", None))
        except Exception as error:  # noqa: BLE001 -- diagnostics only
            self.launcher.log("tilt-reprobe-error", error=str(error))

    @property
    def tilt_mode(self) -> bool:
        """True if tilt adapter is in TILT mode (steering from IMU)."""
        if self._tilt_adapter is None or ControlMode is None:
            return False
        return self._tilt_adapter.mode == ControlMode.TILT

    def _open_sensor_transport(self):
        """Lazily open the native accelerometer channel (bounded, backoff).

        Returns the transport or None. Failures are logged once per
        backoff window; the caller must not spin reconnects per frame.
        """
        if ConsoleSensorTransport is None:
            return None
        if self._sensor_transport is not None and self._sensor_transport.is_open:
            return self._sensor_transport
        now = time.monotonic()
        if now < self._sensor_retry_at:
            # Backoff window: report no transport so callers skip socket
            # work entirely instead of retrying a dead session per frame.
            return None
        self._sensor_retry_at = now + 2.0
        transport = self._sensor_transport or ConsoleSensorTransport(port=CONSOLE_PORT)
        self._sensor_transport = transport
        if transport.connect():
            return transport
        self.launcher.log("sensor-console-error", error=transport.last_error)
        return None

    def _close_sensor_transport(self) -> None:
        transport, self._sensor_transport = self._sensor_transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception as error:  # noqa: BLE001 -- diagnostics only
                self.launcher.log("sensor-console-error", error=str(error))

    def _neutral_sensor(self) -> None:
        """Level the guest accelerometer once.

        Mode switches need no explicit neutral: the always-on push below
        settles the guest on stale data by itself.
        """
        if not getattr(self._tilt_adapter, "_open", False):
            return
        transport = self._open_sensor_transport()
        if transport is None:
            self._set_native_state("unavailable", self._native_sink_error(None))
            return
        if transport.set_acceleration(NEUTRAL_ACCELERATION, force=True):
            self._native_ever_live = True
            self._native_settled = True
            self._set_native_state("live", None)
        else:
            self._set_native_state("degraded" if self._native_ever_live else "unavailable",
                                   self._native_sink_error(transport.last_error))

    def _push_tilt_to_guest(self) -> None:
        """Feed the guest accelerometer: live tilt in Tilt Drive, flat otherwise.

        Tilt Drive streams the live Deck IMU — the estimator receives samples
        ONLY here, so ``poll_native()`` MUST run before
        ``sensor_acceleration()``, and ``consume_motion`` is never called (no
        synthetic axes). Every other mode holds the guest at the parked flat
        pose instead (see ``_park_guest_sensor``). That is deliberate: the game
        window requests SCREEN_ORIENTATION_SENSOR_LANDSCAPE, so a live sensor
        feed re-decides the *guest display rotation* whenever the Deck moves
        (Android 9 consults the sensor for SENSOR_LANDSCAPE regardless of the
        rotation lock), and the emulator rotates its host window 90 degrees off
        on every such change — the game then renders as a sideways portrait
        strip. Gamepad driving consumes the controller, not the accelerometer,
        so parking costs nothing there. When the host IMU is not owned the guest
        sensor is left strictly alone at its emulator default. Stale IMU levels
        the guest exactly once; the transport throttle suppresses redundant
        frames and a 2 s reconnect backoff prevents pump spins.
        """
        adapter = self._tilt_adapter
        if adapter is None:
            return
        if not getattr(adapter, "_open", False):
            # Startup probe is single-shot; a transient open failure
            # (device re-enumeration, Steam Input attach order) would
            # otherwise keep the native feed dead all session. Re-probe
            # bounded (see helper); guest sensor untouched until owned.
            self._maybe_reprobe_imu(adapter)
            return
        try:
            adapter.poll_native()
        except AttributeError:
            pass  # foreign/legacy adapter without the Sept-14 feed method
        transport = self._open_sensor_transport()
        if transport is None:
            self._set_native_state("unavailable", self._native_sink_error(None))
            self._native_settled = False
            return
        if not self._tilt_steering_active():
            self._park_guest_sensor(transport)
            return
        try:
            stale = adapter._estimator.is_stale(time.monotonic())
        except AttributeError:
            stale = False
        if stale:
            if self._native_settled:
                return
            vector, force = NEUTRAL_ACCELERATION, True
        else:
            try:
                vector = adapter.sensor_acceleration()
            except (AttributeError, ValueError) as error:
                self.launcher.log("sensor-console-error", error=str(error))
                return
            force = False
        if transport.set_acceleration(vector, force=force):
            self._native_ever_live = True
            self._native_settled = stale
            self._set_native_state("live", None)
        else:
            self._native_settled = False
            self._set_native_state("degraded" if self._native_ever_live else "unavailable",
                                   self._native_sink_error(transport.last_error))

    def _park_guest_sensor(self, transport) -> None:
        """Hold the guest accelerometer at the deterministic parked pose.

        NEUTRAL_ACCELERATION is the device lying flat with a deliberate ~2
        degree roll: neutral for the game's own tilt mode (the roll is inside any
        dead zone) and DETERMINISTIC for the framework, whose landscape quarter is
        ambiguous when gravity sits on +Z alone - measured 2026-09-18, a single
        emulator console `rotate` flipped the guest display 1 -> 3 with the
        display pin in place. Pushed once per transport session or ownership
        change (``_native_settled``); the transport throttle keeps the channel
        quiet afterwards.
        """
        if transport.set_acceleration(NEUTRAL_ACCELERATION, force=not self._native_settled):
            self._native_ever_live = True
            self._native_settled = True
            self._set_native_state("live", None)
            if not self._pose_logged:
                self._pose_logged = True
                self.launcher.log("stage=guest-pose-deterministic",
                                  acceleration=list(NEUTRAL_ACCELERATION),
                                  reason="flat pose is roll-ambiguous for the framework quarter")
        else:
            self._native_settled = False
            self._set_native_state("degraded" if self._native_ever_live else "unavailable",
                                   self._native_sink_error(transport.last_error))

    def _write_control_status(self, requested, available, error=None, request_id=None):
        mode = self._tilt_adapter.mode.value if self._tilt_adapter else "gamepad"
        self._ctl_context = dict(requested_mode=requested, sensor_available=bool(available),
                                 error=error, request_id=request_id)
        self._refresh_control_status()

    def _refresh_control_status(self) -> None:
        """Atomically rewrite control-mode.json from stored context + native state."""
        mode = self._tilt_adapter.mode.value if self._tilt_adapter else "gamepad"
        status = dict(mode=mode, **self._ctl_context,
                      native_sensor=self._native_state, native_error=self._native_error)
        path = self.launcher.run_dir / "control-mode.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(status) + "\n")
        temporary.replace(path)
        self.launcher.log("control-mode", **status)

    def _set_native_state(self, state: str, error: str | None) -> None:
        """Record guest-sink truth; rewrite the status file only on change.

        ``live`` means guest accelerometer pushes are acknowledged.
        ``unavailable``/``degraded`` never blocks the acknowledged View
        request: physical sticks still drive whenever the GAME's gamepad is
        ON, so the error text must say exactly that instead of claiming
        tilt is ready.
        """
        if state == self._native_state and error == self._native_error:
            return
        self._native_state = state
        self._native_error = error
        try:
            self._refresh_control_status()
        except OSError as failure:
            self.launcher.log("control-mode-error", error=str(failure))

    @staticmethod
    def _native_sink_error(detail: str | None) -> str:
        cause = detail or "emulator console unreachable"
        return (f"Game tilt feed unavailable ({cause}). "
                "In Tilt Drive the physical sticks stay disabled by design — "
                "select Gamepad in the launcher to restore stick driving, "
                "or tilt resumes automatically when the feed reconnects. "
                "Keep the in-game Gamepad toggle ON.")

    def _cache_stick(self, event, raw=False) -> None:
        """Remember the last cooked LX/LY so a gate release re-drives it.

        The cache is what ``_restore_stick`` replays after a mode change:
        it must hold the value the guest actually received, so raw bridge
        events are cached through the same DeckControls curve as the live
        forwarding path.
        """
        if event.get("type") != "axis" or event.get("axis") not in self._stick_axes:
            return
        value = event.get("value")
        if not isinstance(value, (int, float)) or not -1 <= value <= 1:
            return
        if raw and self._deck_controls is not None:
            translated = self._deck_controls.translate(event)
            if not translated:
                return
            event = translated[0]
        self._stick_axes[event["axis"]] = event["value"]

    def _neutral_axes(self):
        for axis in ("LX", "LY"):
            self._forward_event({"type": "axis", "axis": axis, "value": 0.0})

    def _restore_stick(self):
        # Physical sticks are authoritative host-side whenever the analog
        # gate is open. While tilt steering is armed the cached LX/LY must
        # NOT be re-driven (that would double-drive with native tilt).
        if self._tilt_steering_active():
            return
        for axis, value in self._stick_axes.items():
            self._forward_event({"type": "axis", "axis": axis, "value": value})

    def _apply_control_mode(self, requested, request_id=None):
        """Apply a driving mode (Gamepad or the retained Tilt Drive engine)."""
        if requested not in ("gamepad", "tilt") or not isinstance(request_id, (str, type(None))):
            self._write_control_status(str(requested), False, "Invalid driving mode request.")
            return
        if self._tilt_adapter is None:
            self.start_tilt()
        adapter = self._tilt_adapter
        if adapter is None:
            self._write_control_status(requested, False, "Motion controls unavailable.", request_id)
            return
        available = False
        error = None
        effective = ControlMode(requested)
        try:
            if effective == ControlMode.TILT:
                available, error = self._open_motion(adapter)
                if not available:
                    # FAIL-CLOSED: persist the requested Tilt Drive anyway so the
                    # stick gate stays armed; NEVER silently fall back to Gamepad
                    # (that would re-enable sticks without telling the user).
                    # The dead feed is reported loudly; the user selects Gamepad
                    # explicitly to restore sticks, or tilt resumes on recovery.
                    error = (
                        "Tilt Drive selected but the Deck motion sensor is "
                        f"unavailable ({error or 'no device'}). Physical sticks "
                        "stay disabled; the car will not steer until the sensor "
                        "recovers or you select Gamepad. The in-game Gamepad "
                        "toggle stays ON.")
            # Persist before mutating the adapter; failed persistence leaves
            # the current mode authoritative. This router is the sole writer.
            adapter.select_mode(effective)
        except Exception as failure:
            self._write_control_status(requested, bool(adapter.controlled_axes),
                                       str(failure), request_id)
            return
        self._neutral_axes()
        # Explicit mode changes settle the stick here (neutral always sent
        # above; restore skipped by _restore_stick while the gate is armed),
        # so sync the transition flag: _mirror_native_sensor then only
        # settles ownership-driven changes (sensor drop/recovery).
        self._stick_gate_held = self._tilt_steering_active()
        self._restore_stick()
        # The native feed is always-on by design (the game's own Gamepad
        # toggle arbitrates), so leaving Tilt never closes the console
        # session: the next pump keeps mirroring live tilt either way.
        self._write_control_status(requested, available, error, request_id)

    def _native_live(self) -> bool:
        """True when the host IMU is owned and the native feed may run."""
        return self._tilt_adapter is not None and bool(getattr(self._tilt_adapter, "_open", False))

    def _mirror_native_sensor(self) -> None:
        """Push the always-on native feed.

        Corrected-C: NO synthetic IMU axes are ever forwarded. The router
        never calls ``consume_motion``; Deck tilt reaches the game ONLY as
        Android accelerometer vectors via ``_push_tilt_to_guest``.

        Analog-gate transitions are settled here: engaging tilt steering
        levels any held stick (neutral), releasing it restores the cached
        stick so neither transition can leave a stuck or dead axis.
        """
        armed = self._tilt_steering_active()
        if armed != self._stick_gate_held:
            self._stick_gate_held = armed
            if armed:
                self._neutral_axes()
            else:
                self._restore_stick()
        self._push_tilt_to_guest()

    def pump(self, duration: float = 0.25) -> None:
        # Mirror the native sensor before processing bridge/client events
        # so they appear on the same timeline in temporal order.
        self._mirror_native_sensor()
        # Sensor input is sampled by the adapter rather than registered in
        # this selector. Poll fast whenever the IMU is owned so the native
        # feed stays live; View mode no longer affects cadence.
        if self._native_live():
            duration = min(duration, 0.01)
        for key, _ in self.selector.select(duration):
            if key.data == "listener":
                client, _ = self.listener.accept()  # type: ignore[union-attr]
                client.setblocking(False)
                self.clients[client] = bytearray()
                self.producer_buffers[client] = bytearray()
                self.selector.register(client, selectors.EVENT_READ, "client")
                self.launcher.log("input-client-connected")
            elif key.data == "side_channel":
                # Raw events from bridge: apply driving control mappings
                alive = self._read_side_channel()
                if not alive:
                    self.launcher.log("bridge-side-channel-closed")
                    self._drop_producer(key.fileobj)
                    self._side_channel_active = False
            else:
                producer = key.fileobj
                # When side-channel is active, bridge stdout events are
                # already processed by the side-channel path.  Suppress
                # stdout forwarding to avoid double-processing.
                if self._side_channel_active and key.data == "bridge":
                    # Drain bridge stdout but don't forward (side-channel owns it).
                    # EBADF / closed fd: fall through to alive-check which will
                    # drop the producer via _drop_producer.
                    try:
                        os.read(producer.fileno(), 65536)
                    except (BlockingIOError, OSError):
                        alive = False
                    continue
                alive = self._read_producer(producer)
                if not alive:
                    if key.data == "client":
                        self._drop_client(producer)
                    else:
                        self._drop_producer(producer)

    def close(self) -> None:
        errors = []
        def attempt(action):
            try:
                action()
            except Exception as error:
                errors.append(str(error))
        channel, self._side_channel = self._side_channel, None
        self._side_channel_active = False
        if channel is not None:
            attempt(channel.close)
        adapter, self._tilt_adapter = self._tilt_adapter, None
        if adapter is not None:
            attempt(adapter.close)
        sensor, self._sensor_transport = self._sensor_transport, None
        if sensor is not None:
            attempt(sensor.close)
        for client in list(self.clients):
            attempt(client.close)
        self.clients.clear()
        self.producer_buffers.clear()
        listener, self.listener = self.listener, None
        if listener is not None:
            try:
                path = Path(listener.getsockname())
                attempt(lambda: path.unlink(missing_ok=True))
            except (OSError, TypeError, ValueError) as error:
                errors.append(str(error))
            attempt(listener.close)
        attempt(self.selector.close)
        if errors:
            raise LauncherError("input resources could not close: " + "; ".join(errors))


def required_packages(env=None) -> list[str]:
    """The game is always required; extra guest packages are explicit opt-ins."""
    value = (os.environ if env is None else env).get("JCS2_REQUIRE_PACKAGES", "")
    extra = [name for name in re.split(r"[\s,]+", value) if name]
    invalid = [name for name in extra if not PACKAGE_NAME.fullmatch(name)]
    if invalid:
        raise LauncherError(f"JCS2_REQUIRE_PACKAGES has invalid package names: {invalid!r}")
    return list(dict.fromkeys([GAME, *extra]))


def tcp_open(host: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((host, port)) == 0


def wait_until(predicate, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise LauncherError(f"{label} timeout")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headless", action="store_true", help="hide the emulator window for QA")
    parser.add_argument("--input", dest="input_mode", choices=("joystick", "qa"),
                        default=os.environ.get("JCS2_INPUT", "joystick"),
                        help="default physical joystick or controlled QA Unix socket")
    return parser.parse_args()


def main() -> int:
    launcher = Launcher(parse_args())
    signal.signal(signal.SIGINT, lambda _signum, _frame: setattr(launcher, "stop_requested", True))
    signal.signal(signal.SIGTERM, lambda _signum, _frame: setattr(launcher, "stop_requested", True))
    def report(message, error):
        try:
            launcher.log(message, error=str(error))
        except Exception:
            print(f"{message}: {error}", file=sys.stderr)
    result = 1
    try:
        result = launcher.main()
    except (LauncherError, OSError) as error:
        report("error", error)
    finally:
        try:
            launcher.cleanup()
        except Exception as error:
            report("cleanup-error", error)
            # A cleanup failure must not mask a prior failure/cancellation.
            if result == 0:
                result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())

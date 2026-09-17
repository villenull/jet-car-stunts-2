#!/usr/bin/env python3
"""Translate Linux joystick(4) events to the controller's JSON replay stream.

This uses the kernel's stable, read-only /dev/input/js* ABI; it never creates a
device or changes groups/configuration.  The first readable supported joystick
is used; known motion-sensor nodes are ignored.
Linux input-event-codes.h is the source of the numeric constants below.  The
semantic names describe the conventional Xbox/Steam Deck layout, while the
ioctl map is always logged because a node may expose only a subset of axes.

Side-channel mode (JCS2_BRIDGE_SIDECHANNEL=<fd>):
  When set, raw events are sent over a Unix socket to the runner.  The runner
  applies DeckControls before forwarding to the
  controller.  DeckControls is NOT applied in the bridge when the side-channel
  is active (the runner owns all gameplay remapping in live mode).
  Stdout still outputs DeckControls-mapped NDJSON for backward compatibility.
"""

import array
import fcntl
import glob
import json
import os
import select
import socket
import struct
import sys
import time
from pathlib import Path

FMT = struct.Struct("IhBB")

# Linux ABS_* constants from /usr/include/linux/input-event-codes.h.  Do not
# infer these from a particular device's axis ordering.
ABS_X = 0
ABS_Y = 1
ABS_Z = 2
ABS_RX = 3
ABS_RY = 4
ABS_RZ = 5
ABS_HAT0X = 16
ABS_HAT0Y = 17
ABS_HAT1X = 18
ABS_HAT1Y = 19
ABS_HAT2X = 20
ABS_HAT2Y = 21

AXIS_NAMES = {
    ABS_X: "ABS_X",
    ABS_Y: "ABS_Y",
    ABS_Z: "ABS_Z",
    ABS_RX: "ABS_RX",
    ABS_RY: "ABS_RY",
    ABS_RZ: "ABS_RZ",
    ABS_HAT0X: "ABS_HAT0X",
    ABS_HAT0Y: "ABS_HAT0Y",
    ABS_HAT1X: "ABS_HAT1X",
    ABS_HAT1Y: "ABS_HAT1Y",
    ABS_HAT2X: "ABS_HAT2X",
    ABS_HAT2Y: "ABS_HAT2Y",
}
AXIS_CODES = {
    ABS_X: "LX",
    ABS_Y: "LY",
    ABS_Z: "LT",
    ABS_RX: "RX",
    ABS_RY: "RY",
    ABS_RZ: "RT",
}

# Linux BTN_* constants: BTN_TL2/TR2 are the separate shoulder-2 controls,
# not LB/RB, so they are intentionally unsupported here.  Trigger axes use
# ABS_Z/ABS_RZ above instead.
BTN_A = 304
BTN_B = 305
BTN_X = 307
BTN_Y = 308
BTN_TL = 310
BTN_TR = 311
BTN_TL2 = 312
BTN_TR2 = 313
BTN_SELECT = 314
BTN_START = 315
BTN_DPAD_UP = 544
BTN_DPAD_DOWN = 545
BTN_DPAD_LEFT = 546
BTN_DPAD_RIGHT = 547
BUTTON_NAMES = {
    BTN_A: "BTN_A",
    BTN_B: "BTN_B",
    BTN_X: "BTN_X",
    BTN_Y: "BTN_Y",
    BTN_TL: "BTN_TL",
    BTN_TR: "BTN_TR",
    BTN_TL2: "BTN_TL2",
    BTN_TR2: "BTN_TR2",
    BTN_SELECT: "BTN_SELECT",
    BTN_START: "BTN_START",
    BTN_DPAD_UP: "BTN_DPAD_UP",
    BTN_DPAD_DOWN: "BTN_DPAD_DOWN",
    BTN_DPAD_LEFT: "BTN_DPAD_LEFT",
    BTN_DPAD_RIGHT: "BTN_DPAD_RIGHT",
}
BUTTON_CODES = {
    BTN_A: "A",
    BTN_B: "B",
    BTN_X: "X",
    BTN_Y: "Y",
    BTN_TL: "LB",
    BTN_TR: "RB",
    BTN_SELECT: "VIEW",  # Host settings opener, distinct from mapped START pause.
    BTN_START: "START",
    BTN_DPAD_UP: "DPAD_UP",
    BTN_DPAD_DOWN: "DPAD_DOWN",
    BTN_DPAD_LEFT: "DPAD_LEFT",
    BTN_DPAD_RIGHT: "DPAD_RIGHT",
}

JSIOCGAXES = 0x80016A11
JSIOCGBUTTONS = 0x80016A12
JSIOCGAXMAP = 0x80406A32
JSIOCGBTNMAP = 0x88006A34
JSIOCGNAME_BASE = 0x80006A13
SYSFS_INPUT_ROOT = Path("/sys/class/input")
STEAM_DECK_VENDOR = "28de"
STEAM_DECK_PRODUCT = "1205"

DECK_AXIS_CODES = {
    ABS_X: "LX", ABS_Y: "LY",
    ABS_RX: "RX", ABS_RY: "RY",
    # This Deck exposes right trigger on HAT2X and left on HAT2Y.
    ABS_HAT2X: "RT", ABS_HAT2Y: "LT",
}


def _normalise_identity(value: object) -> str:
    return str(value).strip().lower().removeprefix("0x").zfill(4)


def device_metadata(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read non-mutating sysfs identity metadata for one js node."""
    node = Path(path).name
    root = SYSFS_INPUT_ROOT / node / "device"
    metadata: dict[str, str] = {}
    for key in ("name", "id/vendor", "id/product", "id/bustype"):
        try:
            metadata[key.replace("/", "_")] = (root / key).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return metadata


def joystick_name(fd: int, length: int = 256) -> str:
    """Read the kernel joystick name without consuming any input events."""
    # _IOC(_IOC_READ, 'j', JSIOCGNAME, length), as defined by joystick.h.
    request = JSIOCGNAME_BASE | (length << 16)
    result = bytearray(length)
    fcntl.ioctl(fd, request, result, True)
    return bytes(result).split(b"\0", 1)[0].decode(errors="replace").strip()


def device_layout(name: str, metadata: dict[str, object] | None = None) -> str:
    """Select an explicit Deck map, generic Xbox map, or ignored sensor node."""
    folded = name.strip().casefold()
    if "motion sensor" in folded:
        return "ignore-motion-sensors"
    metadata = metadata or {}
    vendor = metadata.get("id_vendor", metadata.get("vendor", ""))
    product = metadata.get("id_product", metadata.get("product", ""))
    if folded == "steam deck" and _normalise_identity(vendor) == STEAM_DECK_VENDOR and _normalise_identity(product) == STEAM_DECK_PRODUCT:
        return "steam-deck"
    return "xbox"


def mapped_controls(amap, bmap, layout: str = "xbox"):
    """Return only controls with an explicit semantic mapping.

    This Deck's ABS_X/Y and ABS_RX/RY are sticks; HAT0/HAT1 are trackpads.
    The trackpads are intentionally omitted. HAT2 carries the triggers.
    """
    axis_codes = DECK_AXIS_CODES if layout == "steam-deck" else AXIS_CODES
    axes = {index: axis_codes[code] for index, code in enumerate(amap) if code in axis_codes}
    buttons = {index: BUTTON_CODES[code] for index, code in enumerate(bmap) if code in BUTTON_CODES}
    return axes, buttons


def describe_mapping(amap, bmap, *, name: str = "", metadata: dict[str, object] | None = None):
    """Return raw ioctl codes, kernel names, and translated controls."""
    metadata = metadata or {}
    layout = device_layout(name, metadata) if name else "xbox"
    axes, buttons = mapped_controls(amap, bmap, layout)
    return {
        "device_name": name,
        "device_identity": metadata,
        "layout": layout,
        "axis_count": len(amap),
        "button_count": len(bmap),
        "axes": [
            {"index": index, "code": code, "name": AXIS_NAMES.get(code, "UNKNOWN"),
             "control": axes.get(index)}
            for index, code in enumerate(amap)
        ],
        "buttons": [
            {"index": index, "code": code, "name": BUTTON_NAMES.get(code, "UNKNOWN"),
             "control": buttons.get(index)}
            for index, code in enumerate(bmap)
        ],
    }


def normalise_axis_value(value: int, control: str) -> float:
    """Convert the signed JS range to the controller's normalized range."""
    value = max(-32767, min(32767, int(value)))
    normalized = value / 32767.0
    if control in ("LT", "RT"):
        # Linux js axes use -32767 at rest and +32767 when fully pressed.
        normalized = (normalized + 1.0) / 2.0
    return max(-1.0, min(1.0, normalized))


def steering_curve(value: float) -> float:
    """Gentle center response with full lock still available at full travel."""
    magnitude = min(1.0, abs(value))
    deadzone = 0.12
    if magnitude <= deadzone:
        return 0.0
    travel = (magnitude - deadzone) / (1.0 - deadzone)
    curved = deadzone + (1.0 - deadzone) * (0.33 * travel + 0.67 * travel ** 3)
    return -curved if value < 0 else curved


class DeckControls:
    """Personal layout: R2 go, L2 reverse, left-stick steering.

    A, B, and D-pad are unassigned; menus use native touch/mouse input.
    RB passes through as virtual LB
    (game L1 boost).  LB emits LT-axis airbrake (value 1.0 down / 0.0 up).
    DPAD dropped; R2/L2/sticks/START as-is.
    """
    def __init__(self):
        self.accelerating = None

    def translate(self, event):
        event = dict(event)
        if event['type'] == 'button':
            key = event['key']
            # A and B are unassigned; menus use native touch/mouse input.
            if key in ('A', 'B'):
                return []
            if key.startswith('DPAD_'):
                return []
            if key == 'RB':
                # RB passes through as virtual LB (game L1 boost).
                event['key'] = 'LB'
                return [event]
            if key == 'LB':
                # LB emits LT-axis airbrake.
                return [dict(type='axis', axis='LT', value=1.0 if event['action'] == 'down' else 0.0, t_ms=event['t_ms'])]
            if key == 'START':
                event['key'] = 'BACK'  # Game's pause/menu action.
        else:
            axis = event['axis']
            if axis in ('RX', 'RY'):
                return []
            if axis == 'LX':
                event['value'] = steering_curve(event['value'])
            if axis == 'RT':
                # The game's R1 accelerator is digital; avoid trigger chatter.
                pressed = event['value'] > (0.12 if self.accelerating else 0.20)
                if pressed == self.accelerating:
                    return []
                self.accelerating = pressed
                return [dict(type='button', key='RB', action='down' if pressed else 'up', t_ms=event['t_ms'])]
            if axis == 'LT':
                event['axis'] = 'RT'
        return [event]


def ioctl_count(fd, request):
    result = bytearray(1)
    fcntl.ioctl(fd, request, result, True)
    return result[0]


def _parse_bridge_args():
    """Parse bridge-specific CLI args. Returns side_channel_fd or None."""
    side_channel_fd = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--side-channel" and i + 1 < len(args):
            side_channel_fd = int(args[i + 1])
            i += 2
        else:
            i += 1
    return side_channel_fd


def main():
    side_channel_fd = _parse_bridge_args()
    candidates = os.environ.get("JCS2_JOYSTICK", "").split() or sorted(glob.glob("/dev/input/js*"))
    fd = None
    chosen = None
    chosen_layout = "xbox"
    chosen_description = None
    for path in candidates:
        try:
            fd = open(path, "rb", buffering=0); chosen = path; break
        except OSError:
            continue
    if fd is None:
        print("joystick: no readable /dev/input/js* (set JCS2_JOYSTICK)", file=sys.stderr)
        return 2
    # Query each readable node's metadata and maps before selecting it.  This
    # keeps the motion-sensor js node from becoming a generic stick producer.
    while fd is not None:
        try:
            name = joystick_name(fd.fileno())
            metadata = device_metadata(chosen)
            axis_count = ioctl_count(fd, JSIOCGAXES)
            button_count = ioctl_count(fd, JSIOCGBUTTONS)
            amap = array.array("B", [0] * 64)
            bmap = array.array("H", [0] * 512)
            fcntl.ioctl(fd, JSIOCGAXMAP, amap, True)
            fcntl.ioctl(fd, JSIOCGBTNMAP, bmap, True)
            amap = amap[:axis_count]
            bmap = bmap[:button_count]
            chosen_layout = device_layout(name, metadata)
            chosen_description = describe_mapping(amap, bmap, name=name, metadata=metadata)
            print(f"joystick: kernel-map {json.dumps(chosen_description, sort_keys=True)}", file=sys.stderr, flush=True)
            if chosen_layout == "ignore-motion-sensors":
                print(f"joystick: ignoring {chosen} ({name})", file=sys.stderr, flush=True)
                fd.close()
                fd = None
                chosen = None
                # A caller that explicitly selected js1 should get a clean
                # no-device result; automatic discovery can continue to js2.
                remaining = candidates[candidates.index(path) + 1:]
                for next_path in remaining:
                    try:
                        fd = open(next_path, "rb", buffering=0); chosen = next_path; break
                    except OSError:
                        continue
                if fd is None:
                    break
                path = chosen
                continue
            break
        except OSError as e:
            print(f"joystick: cannot query kernel metadata/maps: {e}", file=sys.stderr)
            fd.close()
            return 3
    if fd is None or chosen is None or chosen_description is None:
        print("joystick: no supported readable joystick", file=sys.stderr)
        return 2
    print(f"joystick: using {chosen} layout={chosen_layout}", file=sys.stderr, flush=True)
    axis_names, button_names = mapped_controls(amap, bmap, chosen_layout)
    if not axis_names and not button_names:
        print("joystick: maps contain no supported Xbox-style controls", file=sys.stderr); return 4
    axes = {}
    personal = DeckControls() if chosen_layout == 'steam-deck' else None
    # Side-channel socket: sends raw events to the runner for
    # DeckControls processing.  When active, DeckControls is
    # NOT applied in the bridge — the runner owns all gameplay remapping.
    side_channel_sock = None
    if side_channel_fd is not None:
        try:
            side_channel_sock = socket.fromfd(side_channel_fd, socket.AF_UNIX, socket.SOCK_STREAM)
            side_channel_sock.setblocking(False)
            print(f"joystick: side-channel active (fd={side_channel_fd})", file=sys.stderr, flush=True)
        except OSError as e:
            print(f"joystick: side-channel failed: {e}", file=sys.stderr, flush=True)
            side_channel_sock = None
    def emit(event):
        for translated in personal.translate(event) if personal else [event]:
            print(json.dumps(translated), flush=True)
    on_raw_event = None  # Legacy in-process callback (unused in subprocess mode)
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            raw = fd.read(FMT.size)
            if len(raw) != FMT.size:
                return 0
            _, value, kind, number = FMT.unpack(raw)
            kind &= 0x7f  # JS_EVENT_INIT is advisory
            now = int(time.monotonic() * 1000)
            if kind == 1 and number in button_names and button_names[number]:
                raw_event = {"type":"button", "key":button_names[number], "action":"down" if value else "up", "t_ms":now}
                # Send raw event over side-channel for runner-side driving control mapping.
                if side_channel_sock is not None:
                    try:
                        side_channel_sock.sendall((json.dumps(raw_event, separators=(",",":")) + "\n").encode())
                    except (BrokenPipeError, OSError):
                        pass  # runner closed; keep running for stdout compat
                # Legacy in-process callback (for testing)
                if on_raw_event is not None and not on_raw_event(raw_event):
                    continue  # Consumed by in-process callback
                # When side-channel is active, skip DeckControls here — the
                # runner applies it to filtered events.  Stdout still gets
                # DeckControls for backward compat / standalone debugging.
                if side_channel_sock is not None and personal is not None:
                    print(json.dumps(raw_event, separators=(",",":")), flush=True)
                else:
                    emit(raw_event)
            elif kind == 2 and number in axis_names and axis_names[number]:
                key = axis_names[number]
                v = normalise_axis_value(value, key)
                # Deck sticks report small resting jitter continuously. Avoid
                # flooding the synchronous controller pipe with neutral input.
                if key in ('LX', 'LY', 'RX', 'RY') and abs(v) < 0.12:
                    v = 0.0
                if key in axes and axes[key] == v:
                    continue
                axes[key] = v
                raw_event = {"type":"axis", "axis":key, "value":v, "t_ms":now}
                # Send raw event over side-channel
                if side_channel_sock is not None:
                    try:
                        side_channel_sock.sendall((json.dumps(raw_event, separators=(",",":")) + "\n").encode())
                    except (BrokenPipeError, OSError):
                        pass
                # Legacy in-process callback
                if on_raw_event is not None and not on_raw_event(raw_event):
                    continue
                # When side-channel active, stdout gets raw event (runner owns DeckControls)
                if side_channel_sock is not None and personal is not None:
                    print(json.dumps(raw_event, separators=(",",":")), flush=True)
                else:
                    emit(raw_event)
    except BrokenPipeError:
        return 0
    finally:
        fd.close()
        if side_channel_sock is not None:
            try:
                side_channel_sock.close()
            except OSError:
                pass

if __name__ == "__main__":
    raise SystemExit(main())

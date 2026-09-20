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
import errno
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

# _IOW('E', 0x90, int): the evdev exclusive-grab ioctl (linux/input.h).
EVIOCGRAB = 0x40044590

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
    BTN_SELECT: "VIEW",  # Physical SELECT: no guest action; the runner drops it.
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


def evdev_sibling(path: str | os.PathLike[str], root: Path | None = None) -> str | None:
    """Return the evdev node of the SAME device as a js node, or None.

    js and event handlers of one device sit side by side in the device's sysfs
    directory (js0 + event7 for the built-in Steam Deck pad), so the evdev node
    is read from the js node's own entry instead of guessed.
    """
    directory = (root or SYSFS_INPUT_ROOT) / Path(path).name / "device"
    try:
        names = [entry.name for entry in directory.iterdir()]
    except OSError:
        return None
    for name in sorted(names):
        if name.startswith("event") and name[len("event"):].isdigit():
            return f"/dev/input/{name}"
    return None


def grab_probe(path: str | os.PathLike[str]) -> None:
    """Take and immediately release an exclusive grab on one evdev node.

    Raises OSError (EBUSY) when another process already holds the device.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, EVIOCGRAB, 1)
        fcntl.ioctl(fd, EVIOCGRAB, 0)
    finally:
        os.close(fd)


def exclusive_grab_reason(path: str | os.PathLike[str] | None, probe=grab_probe) -> str:
    """Explain why a js node delivers nothing, or '' when it is free.

    A js node has no grab of its own: EVIOCGRAB is taken on the evdev node of
    the same device, and the kernel then delivers that device's events to the
    grabbing handle only -- the js node sees none of them, not even a partial
    stream.  Probing with a grab of our own is the only userspace way to see
    that, and a held device answers EBUSY.

    Anything other than EBUSY (no such node, no permission) means "cannot
    tell" and reports nothing rather than a guess.
    """
    if not path:
        return ""
    try:
        probe(path)
    except OSError as error:
        if error.errno == errno.EBUSY:
            return f"{path} is exclusively grabbed by another process (EVIOCGRAB)"
        return ""
    return ""


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
    # Tuned 2026-09-18 (user: "slightly decrease" the steering): the cubic
    # weight is an ~8% reduction mid-travel with the deadzone and the full-lock
    # endpoint unchanged, so nothing becomes unreachable.
    curved = deadzone + (1.0 - deadzone) * (0.25 * travel + 0.75 * travel ** 3)
    return -curved if value < 0 else curved


class DeckControls:
    """Personal layout: R2 go, L2 reverse, left-stick steering.

    A, B, and D-pad are unassigned; menus use native touch/mouse input.
    R2 (analog RT) emits the digital RB key — the game's accelerator is
    digital — while L2 (analog LT) drives the guest's BRAKE trigger axis
    (the game reads brake/reverse on its own R2).  RB passes through as
    virtual LB (game L1 boost); LB (physical L1) drives the guest's
    HANDBRAKE trigger axis (the game's own L2 air brake).  The two shoulder
    pairs therefore stay on four distinct guest controls; sharing one axis
    between them made L1 and L2 the same action (user report 2026-09-18,
    "every button works except L2 and L1 — both apply handbrake").
    START becomes BACK (pause).  DPAD dropped; sticks pass through (LX curved).
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
                # Physical L1 is the game's AIR BRAKE / HANDBRAKE, which its
                # own L2 trigger drives: the guest's handbrake axis, kept
                # distinct from the brake axis physical L2 drives below.
                return [dict(type='axis', axis='HANDBRAKE', value=1.0 if event['action'] == 'down' else 0.0, t_ms=event['t_ms'])]
            if key == 'START':
                event['key'] = 'BACK'  # Game's pause/menu action.
        else:
            axis = event['axis']
            if axis in ('RX', 'RY'):
                return []
            if axis == 'LT':
                # Physical L2 is BRAKE / REVERSE (the game reads that on its R2
                # trigger axis), not the handbrake L1 owns.
                event['axis'] = 'BRAKE'
            if axis == 'LX':
                event['value'] = steering_curve(event['value'])
            if axis == 'RT':
                # The game's R1 accelerator is digital; avoid trigger chatter.
                pressed = event['value'] > (0.12 if self.accelerating else 0.20)
                if pressed == self.accelerating:
                    return []
                self.accelerating = pressed
                return [dict(type='button', key='RB', action='down' if pressed else 'up', t_ms=event['t_ms'])]
        return [event]


def ioctl_count(fd, request):
    result = bytearray(1)
    fcntl.ioctl(fd, request, result, True)
    return result[0]


# Steam's virtual pad (Gaming Mode) is torn down and recreated while a game
# runs: the open fd then fails reads with ENODEV.  Reopen the SAME node for a
# bounded window instead of exiting, because the runner treats a bridge exit as
# fatal and tears the whole lane down.
REOPEN_ATTEMPTS = 40
REOPEN_DELAY = 0.5


class JoystickQueryError(OSError):
    """The node opened but its kernel identity/maps could not be queried."""


def joystick_maps(path):
    """Open one js node and derive its identity, layout and control maps.

    Raises OSError if the node cannot be opened or queried.  Kept as the one
    place that turns a device path into maps: a reopen after a device
    re-enumeration must derive them exactly the same way as startup.
    """
    fd = open(path, "rb", buffering=0)
    try:
        name = joystick_name(fd.fileno())
        metadata = device_metadata(path)
        axis_count = ioctl_count(fd, JSIOCGAXES)
        button_count = ioctl_count(fd, JSIOCGBUTTONS)
        amap = array.array("B", [0] * 64)
        bmap = array.array("H", [0] * 512)
        fcntl.ioctl(fd, JSIOCGAXMAP, amap, True)
        fcntl.ioctl(fd, JSIOCGBTNMAP, bmap, True)
        amap = amap[:axis_count]
        bmap = bmap[:button_count]
    except OSError as error:
        fd.close()
        raise JoystickQueryError(str(error)) from error
    layout = device_layout(name, metadata)
    description = describe_mapping(amap, bmap, name=name, metadata=metadata)
    return fd, name, layout, description, amap, bmap


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
    amap = bmap = None
    # Query each readable node's metadata and maps before selecting it.  This
    # keeps the motion-sensor js node from becoming a generic stick producer.
    for path in candidates:
        try:
            fd, name, chosen_layout, chosen_description, amap, bmap = joystick_maps(path)
        except JoystickQueryError as error:
            print(f"joystick: cannot query kernel metadata/maps: {error}", file=sys.stderr)
            return 3
        except OSError:
            continue  # unreadable node; try the next candidate
        chosen = path
        print(f"joystick: kernel-map {json.dumps(chosen_description, sort_keys=True)}", file=sys.stderr, flush=True)
        if chosen_layout == "ignore-motion-sensors":
            print(f"joystick: ignoring {chosen} ({name})", file=sys.stderr, flush=True)
            fd.close()
            fd = None
            chosen = None
            # A caller that explicitly selected js1 should get a clean
            # no-device result; automatic discovery can continue to js2.
            continue
        break
    if fd is None:
        print("joystick: no readable /dev/input/js* (set JCS2_JOYSTICK)", file=sys.stderr)
        return 2
    if chosen is None or chosen_description is None:
        print("joystick: no supported readable joystick", file=sys.stderr)
        return 2
    chosen_name = name
    chosen_identity = device_metadata(chosen)
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
    pressed = set()

    def publish(raw_event) -> bool:
        """Send one raw event to the runner and/or stdout. False = consumed."""
        if side_channel_sock is not None:
            try:
                side_channel_sock.sendall((json.dumps(raw_event, separators=(",",":")) + "\n").encode())
            except (BrokenPipeError, OSError):
                pass  # runner closed; keep running for stdout compat
        if on_raw_event is not None and not on_raw_event(raw_event):
            return False  # Consumed by in-process callback
        if side_channel_sock is not None and personal is not None:
            print(json.dumps(raw_event, separators=(",",":")), flush=True)
        else:
            emit(raw_event)
        return True

    def neutralise():
        """Release every control we still hold.

        A device that disappears mid-race leaves the guest holding whatever was
        down when it vanished; without this the car keeps accelerating (or
        steering) until something else changes that control.
        """
        now = int(time.monotonic() * 1000)
        for key, value in list(axes.items()):
            if value:
                publish({"type": "axis", "axis": key, "value": 0.0, "t_ms": now})
        for key in sorted(pressed):
            publish({"type": "button", "key": key, "action": "up", "t_ms": now})
        axes.clear()
        pressed.clear()
    # A js node cannot be grabbed itself: events are lost to another process's
    # EVIOCGRAB on the same device's evdev node, and the only symptom is the
    # silence below.  Report that state instead of reading a dead node for
    # hours, and noisily mark the JS_EVENT_INIT burst so a state snapshot can
    # never be mistaken for presses in the trace.
    evdev_node = evdev_sibling(chosen)
    grab_reason = exclusive_grab_reason(evdev_node)
    if grab_reason:
        print(f"joystick: WARNING input is blocked: {grab_reason}; {chosen} will deliver "
              f"nothing but its open-time state snapshot until it is released "
              f"(Desktop Mode: deck-input-mapper --grab)", file=sys.stderr, flush=True)
    last_live = time.monotonic()
    next_ownership_check = last_live + 30.0
    saw_init = False
    saw_live = False
    try:
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except OSError:
                ready = [fd]  # dead fd still reports readable; the read decides
            if not ready:
                # Probe only while the node is quiet: taking the grab candidate
                # starves this very node for the duration of the probe, so it
                # must never race a press that is already flowing.
                now = time.monotonic()
                if evdev_node and now >= next_ownership_check and now - last_live >= 30.0:
                    next_ownership_check = now + 30.0
                    reason = exclusive_grab_reason(evdev_node)
                    if reason != grab_reason:
                        grab_reason = reason
                        if reason:
                            print(f"joystick: WARNING input is blocked: {reason}", file=sys.stderr, flush=True)
                        else:
                            print(f"joystick: {evdev_node} released; {chosen} is live", file=sys.stderr, flush=True)
                continue
            try:
                raw = fd.read(FMT.size)
            except OSError as error:
                # The device is gone: release everything it was holding before
                # anything else, so a vanished pad cannot leave the guest on a
                # stuck throttle or steering angle.
                neutralise()
                print(f"joystick: {chosen} read failed ({error}); released held controls, "
                      f"reopening the same node", file=sys.stderr, flush=True)
                reopened = None
                for attempt in range(REOPEN_ATTEMPTS):
                    time.sleep(REOPEN_DELAY)
                    try:
                        candidate = joystick_maps(chosen)
                    except OSError:
                        continue
                    name, layout = candidate[1], candidate[2]
                    if layout == "ignore-motion-sensors":
                        # Never adopt the motion-sensor node: keep waiting for
                        # the pad to come back on this path.
                        candidate[0].close()
                        continue
                    identity = device_metadata(chosen)
                    if (name, identity) != (chosen_name, chosen_identity):
                        print(f"joystick: {chosen} came back as a different device "
                              f"({chosen_name!r} {chosen_identity} -> {name!r} {identity}); "
                              f"adopting it because it is still a supported pad",
                              file=sys.stderr, flush=True)
                    reopened = candidate
                    break
                if reopened is None:
                    print(f"joystick: {chosen} did not come back within "
                          f"{REOPEN_ATTEMPTS * REOPEN_DELAY:.0f}s ({error}); exiting",
                          file=sys.stderr, flush=True)
                    return 5
                fd.close()
                fd, name, chosen_layout, chosen_description, amap, bmap = reopened
                axis_names, button_names = mapped_controls(amap, bmap, chosen_layout)
                if not axis_names and not button_names:
                    print(f"joystick: {chosen} came back with no supported controls",
                          file=sys.stderr, flush=True)
                    return 4
                personal = DeckControls() if chosen_layout == 'steam-deck' else None
                evdev_node = evdev_sibling(chosen)
                grab_reason = exclusive_grab_reason(evdev_node)
                axes.clear()  # fresh device: re-send its state instead of diffing
                saw_init = saw_live = False
                last_live = time.monotonic()
                next_ownership_check = last_live + 30.0
                print(f"joystick: {chosen} re-enumerated; reopened after "
                      f"{(attempt + 1) * REOPEN_DELAY:.1f}s layout={chosen_layout}",
                      file=sys.stderr, flush=True)
                continue
            if len(raw) != FMT.size:
                return 0
            _, value, kind, number = FMT.unpack(raw)
            is_init = bool(kind & 0x80)
            kind &= 0x7f  # JS_EVENT_INIT is advisory
            if is_init:
                if not saw_init:
                    saw_init = True
                    print(f"joystick: state snapshot on {chosen} (JS_EVENT_INIT: device state at "
                          f"open, not user input)", file=sys.stderr, flush=True)
            else:
                last_live = time.monotonic()
                if not saw_live:
                    saw_live = True
                    print(f"joystick: first live event from {chosen}", file=sys.stderr, flush=True)
            now = int(time.monotonic() * 1000)
            if kind == 1 and number in button_names and button_names[number]:
                raw_event = {"type":"button", "key":button_names[number], "action":"down" if value else "up", "t_ms":now}
                if value:
                    pressed.add(raw_event["key"])
                else:
                    pressed.discard(raw_event["key"])
                # Side-channel carries the raw event for runner-side driving
                # mapping; when it is active DeckControls is NOT applied here
                # (the runner owns it), while stdout keeps it for standalone use.
                if not publish(raw_event):
                    continue  # consumed by in-process callback
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
                if not publish(raw_event):
                    continue
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

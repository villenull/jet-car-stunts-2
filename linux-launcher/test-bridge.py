#!/usr/bin/env python3
import sys
sys.path.insert(0, __import__('os').path.dirname(__file__))
from joystick_bridge import (
    ABS_HAT0X, ABS_HAT0Y, ABS_HAT1X, ABS_HAT1Y, ABS_HAT2X, ABS_HAT2Y,
    ABS_RX, ABS_RY, ABS_RZ, ABS_X, ABS_Y, ABS_Z, BTN_DPAD_DOWN,
    BTN_DPAD_LEFT, BTN_DPAD_RIGHT, BTN_DPAD_UP, BTN_TL, BTN_TL2,
    BTN_TR, BTN_TR2, describe_mapping, device_layout, mapped_controls,
    normalise_axis_value,
)

# Verify the actual Linux input-event-codes.h constants, then test the
# conventional Xbox/Steam Deck semantic map independently of device order.
assert (ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ) == (0, 1, 2, 3, 4, 5)
assert (BTN_TL, BTN_TR, BTN_TL2, BTN_TR2) == (310, 311, 312, 313)
a, b = mapped_controls([ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ], [304, 305, 307, 308, 310, 311, 312, 313, 314, 315, 544, 545, 546, 547])
assert a == {0: 'LX', 1: 'LY', 2: 'LT', 3: 'RX', 4: 'RY', 5: 'RT'}
assert b == {0: 'A', 1: 'B', 2: 'X', 3: 'Y', 4: 'LB', 5: 'RB', 8: 'VIEW', 9: 'START', 10: 'DPAD_UP', 11: 'DPAD_DOWN', 12: 'DPAD_LEFT', 13: 'DPAD_RIGHT'}
a, b = mapped_controls([1, 3, 4, 16], [312, 313])
assert a == {0: 'LY', 1: 'RX', 2: 'RY'}
assert b == {}

# Evidence captured from this Deck's js0: name Steam Deck, USB 28de:1205,
# ten axes (sticks first, HAT0/HAT1 trackpads, HAT2 triggers), 24 buttons.
# Physical identities corroborated by /usr/local/bin/deck-input-mapper.
deck_name = "Steam Deck"
deck_identity = {"id_vendor": "28de", "id_product": "1205", "id_bustype": "0003"}
assert device_layout(deck_name, deck_identity) == "steam-deck"
assert device_layout(deck_name, {"id_vendor": "045e", "id_product": "028e"}) == "xbox"
deck_axes = [ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_HAT0X, ABS_HAT0Y, ABS_HAT1X, ABS_HAT1Y, ABS_HAT2X, ABS_HAT2Y]
deck_buttons = [289, 290, 294, 304, 305, 307, 308, 310, 311, 312, 313, 314, 315, 316, 317, 318, BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT, 704, 705, 706, 707]
description = describe_mapping(deck_axes, deck_buttons, name=deck_name, metadata=deck_identity)
assert description["axis_count"] == 10 and description["button_count"] == 24
a, b = mapped_controls(deck_axes, deck_buttons, "steam-deck")
assert a == {0: 'LX', 1: 'LY', 2: 'RX', 3: 'RY', 8: 'RT', 9: 'LT'}
assert all(i not in a for i in (4, 5, 6, 7))  # Trackpads never steer.
assert b[11] == 'VIEW' and b[12] == 'START'
assert b[7] == 'LB'  # BTN_TL/BTN_TR remain LB/RB
assert b[8] == 'RB' and b[16] == 'DPAD_UP' and b[19] == 'DPAD_RIGHT'
assert 9 not in b and 10 not in b  # BTN_TL2/TR2 are not digital bumpers
assert normalise_axis_value(-32767, "LT") == 0.0
assert normalise_axis_value(32767, "RT") == 1.0
assert normalise_axis_value(0, "LT") == 0.5

# js1 is a separate sensor node and must never become a generic stick source.
assert device_layout("Steam Deck Motion Sensors", deck_identity) == "ignore-motion-sensors"
assert mapped_controls([ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ], [], "xbox")[0] == {0: 'LX', 1: 'LY', 2: 'LT', 3: 'RX', 4: 'RY', 5: 'RT'}

# A js node shares its device directory with the evdev node that CAN be
# grabbed, so that is where the evdev sibling is read from. (js0 -> event7 is
# this Deck's real layout; the temp tree mirrors it.)
import errno
import os
import tempfile
from pathlib import Path

from joystick_bridge import EVIOCGRAB, evdev_sibling, exclusive_grab_reason

# _IOW('E', 0x90, int) as spelled by linux/input.h.
assert EVIOCGRAB == 0x40044590
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    device = root / "js0" / "device"
    device.mkdir(parents=True)
    (device / "event7").mkdir()
    (device / "js0").mkdir()
    (device / "name").write_text("Steam Deck\n")
    assert evdev_sibling("/dev/input/js0", root) == "/dev/input/event7"
    assert evdev_sibling("/dev/input/js9", root) is None  # absent node
    (root / "js1" / "device").mkdir(parents=True)
    assert evdev_sibling("/dev/input/js1", root) is None  # device with no evdev node

# A device held by another process answers EBUSY; every other failure means
# "cannot tell" and must stay silent rather than send the reader hunting.
def probe_busy(path):
    raise OSError(errno.EBUSY, "Device or resource busy")

def probe_denied(path):
    raise OSError(errno.EACCES, "Permission denied")

def probe_missing(path):
    raise OSError(errno.ENOENT, "No such file or directory")

reason = exclusive_grab_reason("/dev/input/event7", probe_busy)
assert "event7" in reason and "EVIOCGRAB" in reason
assert exclusive_grab_reason("/dev/input/event7", probe_denied) == ""
assert exclusive_grab_reason("/dev/input/event7", probe_missing) == ""
assert exclusive_grab_reason(None, probe_busy) == ""   # unresolved sibling
assert exclusive_grab_reason("", probe_busy) == ""
probed = []
assert exclusive_grab_reason("/dev/input/event7", probed.append) == ""
assert probed == ["/dev/input/event7"]                 # the resolved node is probed
print('bridge map-layout tests: PASS')

#!/usr/bin/env python3
"""Read and restore the in-race HUD button layout of a live guest.

Jet Car Stunts 2 keeps its draggable HUD button positions in
``m_fHudPositions`` and the layout it would compute for the current screen in
``m_fHudPositionsDefault`` (both in ``libtrueaxis.so`` .bss).  The game
persists ``m_fHudPositions``, so a session whose layout pass ran while the
screen was narrower leaves the right-hand buttons parked left of the defaults:
on the Deck lane every affected button sits 664 logical units left of its
default, which draws reset/forward/backwards near the middle of the screen
instead of the right edge.

The game's own HUD-layout form fixes this by copying ``m_fHudPositionsDefault``
over ``m_fHudPositions`` (its DEFAULT button).  This module performs the same
copy in the live guest over ``/proc/<pid>/mem`` so a lane can repair a guest
without driving the in-race UI.  It only ever writes that one array, never a
game asset, and never touches the guest unless ``--apply`` is given.

Fail-closed guards: exactly one game process, a readable ``libtrueaxis.so``
mapping, a plausible default array, and ``m_bHudMirrored`` clear.  A mirrored
HUD needs the mirror of the defaults, which this module deliberately refuses
to guess.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
from typing import Iterable, Sequence

PACKAGE = "com.trueaxis.jetcarstunts2"
LIB_NAME = "libtrueaxis.so"
BUTTON_COUNT = 12
PAGE = 4096

# .bss offsets and sizes in libtrueaxis.so for the pinned builds (pristine
# v1.0.23 and the installed monolith patch both keep these addresses).
SYMBOLS: dict[str, tuple[int, int]] = {
    "m_fHudPositions": (0x43C29C, BUTTON_COUNT * 2 * 4),
    "m_fHudPositionsDefault": (0x43C2FC, BUTTON_COUNT * 2 * 4),
    "m_bHudMirrored": (0x475430, 1),
    "g_ppHudButtons": (0x43D790, BUTTON_COUNT * 4),
}

# sha256 of libtrueaxis.so for the builds these offsets were measured on.
KNOWN_LIB_SHA256 = (
    "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d",  # pristine v1.0.23
    "cb2bd45b85cd6bf161199e57c460870d295b73d4ad2101bb7d97d0d413f0ffb1",  # installed monolith
)

DRIFT_HINT = (
    "restore the HUD layout with --apply (the game's own HUD LAYOUT form "
    "DEFAULT button does the same copy)"
)


class HudLayoutError(RuntimeError):
    """The guest state could not be read or safely repaired."""


@dataclass
class Guest:
    """Minimal ADB handle: an argv prefix that reaches one running guest."""

    adb: Sequence[str]
    timeout: float = 20.0
    su: str = "su 0"
    _root: bool | None = None

    def run(self, args: Iterable[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(list(self.adb) + list(args), capture_output=True, text=True,
                              timeout=timeout or self.timeout)

    def shell(self, script: str, timeout: float | None = None, check: bool = True) -> str:
        result = self.run(["shell", script], timeout=timeout)
        if check and result.returncode != 0:
            raise HudLayoutError(f"guest command failed ({result.returncode}): {script}: "
                                 f"{result.stderr.strip()}")
        return result.stdout

    def is_root(self) -> bool:
        if self._root is None:
            self._root = self.shell("id -u", check=False).strip() == "0"
        return self._root

    def privileged(self, script: str, timeout: float | None = None) -> str:
        """Run script as guest root: directly when the ADB shell is root, else via su.

        The lane deliberately keeps adbd unrooted (uid 2000), so the reader uses
        the image's own su instead of `adb root`, which would restart the ADB
        transport under the launcher.
        """
        if self.is_root():
            return self.shell(script, timeout=timeout)
        result = self.run(["shell", f"{self.su} sh -c {shlex.quote(script)}"], timeout=timeout)
        if result.returncode != 0:
            raise HudLayoutError(f"guest root command failed ({result.returncode}): {result.stderr.strip()}"
                                 f"{' (is su available?)' if 'su' in result.stderr else ''}")
        return result.stdout


def guest_from_runtime(adb_port: int | None = None, serial: str | None = None) -> Guest:
    """Build a Guest from the launcher's resolved paths (honours JCS2_SDK)."""
    import runtime_paths  # local imports keep the module importable on its own
    import runner

    paths = runtime_paths.resolve_paths()
    return Guest([str(paths.sdk / "platform-tools/adb"), "-P", str(adb_port or runner.ADB_PORT),
                  "-s", serial or runner.SERIAL])


def guest_pids(guest: Guest) -> list[int]:
    out = guest.shell(f"pidof {PACKAGE}", check=False).strip()
    pids = []
    for token in out.split():
        if token.isdigit():
            pids.append(int(token))
    return pids


def require_single_pid(guest: Guest) -> int:
    pids = guest_pids(guest)
    if len(pids) != 1:
        raise HudLayoutError(f"expected exactly one {PACKAGE} process, found {pids}")
    return pids[0]


def lib_base(guest: Guest, pid: int, lib_name: str = LIB_NAME) -> int:
    """Address of the library image: the mapping whose file offset is zero."""
    maps = guest.privileged(f"cat /proc/{pid}/maps")
    for line in maps.splitlines():
        if lib_name not in line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        span, _perms, offset = parts[0], parts[1], parts[2]
        if int(offset, 16) != 0:
            continue
        return int(span.split("-")[0], 16)
    raise HudLayoutError(f"{lib_name} is not mapped in pid {pid}")


def read_mem(guest: Guest, pid: int, address: int, size: int) -> bytes:
    """Read guest memory, one page-aligned dd block per page spanned."""
    data = bytearray()
    cursor = address
    while len(data) < size:
        page = cursor - (cursor % PAGE)
        chunk = guest.privileged(f"dd if=/proc/{pid}/mem bs={PAGE} skip={page // PAGE} count=1 "
                                 f"2>/dev/null | base64", timeout=guest.timeout)
        block = base64.b64decode(re.sub(r"\s+", "", chunk) or b"")
        if len(block) != PAGE:
            raise HudLayoutError(f"short read at {hex(page)}: {len(block)} bytes")
        start = cursor - page
        take = min(size - len(data), PAGE - start)
        data += block[start:start + take]
        cursor += take
    return bytes(data)


def write_mem_page(guest: Guest, pid: int, address: int, payload: bytes) -> None:
    """Write payload at address, rewriting only the pages it overlaps."""
    cursor = address
    remaining = payload
    while remaining:
        page = cursor - (cursor % PAGE)
        offset = cursor - page
        take = min(len(remaining), PAGE - offset)
        page_bytes = bytearray(read_mem(guest, pid, page, PAGE))
        page_bytes[offset:offset + take] = remaining[:take]
        encoded = base64.b64encode(bytes(page_bytes)).decode()
        guest.privileged(f"printf %s {encoded} | base64 -d | "
                         f"dd of=/proc/{pid}/mem bs={PAGE} seek={page // PAGE} count=1")
        remaining = remaining[take:]
        cursor += take


def floats(payload: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(payload) // 4}f", payload))


def read_layout(guest: Guest, pid: int, base: int) -> dict:
    """Read the current/default button positions and the mirror flag."""
    current_addr = base + SYMBOLS["m_fHudPositions"][0]
    default_addr = base + SYMBOLS["m_fHudPositionsDefault"][0]
    mirror_addr = base + SYMBOLS["m_bHudMirrored"][0]
    current = floats(read_mem(guest, pid, current_addr, SYMBOLS["m_fHudPositions"][1]))
    default = floats(read_mem(guest, pid, default_addr, SYMBOLS["m_fHudPositionsDefault"][1]))
    mirrored = read_mem(guest, pid, mirror_addr, 1)[0] != 0
    return layout_report(current, default, mirrored)


def layout_report(current: Sequence[float], default: Sequence[float], mirrored: bool) -> dict:
    pairs_current = _pairs(current)
    pairs_default = _pairs(default)
    drift = []
    for index, ((cx, cy), (dx, dy)) in enumerate(zip(pairs_current, pairs_default)):
        if (cx, cy) != (dx, dy):
            drift.append({"index": index, "current": [cx, cy], "default": [dx, dy],
                          "delta": [round(cx - dx, 3), round(cy - dy, 3)]})
    return {
        "buttons": len(pairs_current),
        "mirrored": mirrored,
        "current": [list(pair) for pair in pairs_current],
        "default": [list(pair) for pair in pairs_default],
        "drift": drift,
        "matches_default": not drift,
        "ok": not drift and not mirrored,
    }


def _pairs(values: Sequence[float]) -> list[tuple[float, float]]:
    if len(values) % 2:
        raise HudLayoutError(f"expected x/y float pairs, got {len(values)} floats")
    return [(values[i], values[i + 1]) for i in range(0, len(values), 2)]


BUTTON_CENTER_OFFSET = 0x20  # Hud::Button: +0x20 x, +0x24 y (drawn centre, pixels)
BUTTON_STRUCT = 0x40
# g_ppHudButtons index -> m_fHudPositions pair, measured from the live guest
# (each button's stored centre matched exactly one array entry; sprite atlas
# rects confirmed the pairing: 143-223/110-190 = pause, 42-122/291-371 = the
# green accelerate arrow, 42-122/371-451 = the red reverse arrow).
BUTTON_ORDER = (1, 0, 2, 3, 5, 7, 4, 8, 11, 10, 6, 9)


def read_buttons(guest: Guest, pid: int, base: int) -> list[dict]:
    """Read the live Hud::Button objects reachable from g_ppHudButtons."""
    pointers = struct.unpack(f"<{BUTTON_COUNT}I",
                             read_mem(guest, pid, base + SYMBOLS["g_ppHudButtons"][0],
                                      SYMBOLS["g_ppHudButtons"][1]))
    buttons = []
    for index, pointer in enumerate(pointers):
        raw = read_mem(guest, pid, pointer, BUTTON_STRUCT)
        rect = struct.unpack_from("<4f", raw, 0x10)
        centre = struct.unpack_from("<2f", raw, BUTTON_CENTER_OFFSET)
        buttons.append({"index": index, "address": hex(pointer), "atlas_rect": list(rect),
                        "centre": list(centre)})
    return buttons


def restore_button_centres(guest: Guest, pid: int, base: int, drift: Sequence[dict],
                           default_pairs: Sequence[Sequence[float]] | None = None) -> dict:
    """Point the live buttons at the default positions.

    The game copies m_fHudPositions into Hud::Button when the HUD is built, so
    the drawn rects only follow the array after that copy runs (a level restart
    does not re-copy; a fresh app start does).  Writing the buttons reproduces
    what the game's own HUD-LAYOUT form does on ACCEPT, so the corrected
    placement shows immediately.

    Targets come from the array drift when there is one; otherwise each button
    is matched to its own array entry by the measured BUTTON_ORDER.
    """
    wanted = {(row["current"][0], row["current"][1]): (row["default"][0], row["default"][1])
              for row in drift}
    moved = []
    for button in read_buttons(guest, pid, base):
        key = (button["centre"][0], button["centre"][1])
        target = wanted.get(key)
        if target is None and default_pairs is not None:
            candidate = tuple(default_pairs[BUTTON_ORDER[button["index"]]])
            if candidate != key:
                target = candidate
        if target is None:
            continue
        write_mem_page(guest, pid, int(button["address"], 16) + BUTTON_CENTER_OFFSET,
                       struct.pack("<2f", *target))
        moved.append({"index": button["index"], "from": list(key), "to": list(target)})
    return {"action": "restored-buttons" if moved else "none", "moved": moved}


def plausible_defaults(pairs: Sequence[Sequence[float]]) -> bool:
    """Defaults must be a full, finite, on-screen-ish layout."""
    if len(pairs) != BUTTON_COUNT:
        return False
    for pair in pairs:
        if len(pair) != 2:
            return False
        for value in pair:
            if value != value or abs(value) > 8192:  # NaN or absurd
                return False
    return True


def restore_defaults(guest: Guest, apply: bool = False, buttons: bool = True) -> dict:
    """Report the layout, and with apply=True copy defaults over the live array.

    With buttons=True the live Hud::Button centres are pointed at the same
    defaults, so the corrected placement shows without waiting for the HUD to be
    rebuilt from the array.
    """
    state: dict = {"package": PACKAGE}
    try:
        pid = require_single_pid(guest)
        base = lib_base(guest, pid)
        state.update({"pid": pid, "lib_base": hex(base)})
        layout = read_layout(guest, pid, base)
    except (HudLayoutError, struct.error, subprocess.SubprocessError, OSError) as error:
        state.update({"ok": False, "error": str(error)})
        return state
    state.update(layout)
    state["symbols"] = {name: hex(base + vaddr) for name, (vaddr, _size) in SYMBOLS.items()}
    if not plausible_defaults(layout["default"]):
        state.update({"ok": False, "error": "default layout is not plausible for the pinned build"})
        return state
    if layout["mirrored"]:
        state.update({"ok": False, "error": "m_bHudMirrored is set; mirrored defaults are not derived"})
        return state
    if layout["matches_default"]:
        state["action"] = "none"
        state["hint"] = "HUD positions already match the defaults"
        if apply and buttons:
            try:
                rewrite = restore_button_centres(guest, pid, base, layout["drift"], layout["default"])
            except (HudLayoutError, struct.error, subprocess.SubprocessError, OSError) as error:
                state.update({"ok": False, "error": f"button centres not rewritten: {error}"})
                return state
            state["buttons"] = rewrite
            if rewrite["moved"]:
                state["action"] = "restored-buttons"
                state["hint"] = "array matched; live button centres re-pointed at the defaults"
        return state
    if not apply:
        state["action"] = "dry-run"
        state["hint"] = DRIFT_HINT
        return state
    payload = struct.pack(f"<{len(layout['default']) * 2}f",
                          *[value for pair in layout["default"] for value in pair])
    state["action"] = "restore-attempted"
    try:
        write_mem_page(guest, pid, base + SYMBOLS["m_fHudPositions"][0], payload)
        after = read_layout(guest, pid, base)
    except (HudLayoutError, struct.error, subprocess.SubprocessError, OSError) as error:
        state.update({"ok": False, "error": str(error)})
        return state
    state["action"] = "restored"
    state["after"] = after
    state["ok"] = after["matches_default"] and not after["mirrored"]
    if not state["ok"]:
        state["error"] = "verification read did not match the defaults"
    if state["ok"] and buttons:
        try:
            state["buttons"] = restore_button_centres(guest, pid, base, layout["drift"],
                                                       layout["default"])
        except (HudLayoutError, struct.error, subprocess.SubprocessError, OSError) as error:
            state.update({"ok": False, "error": f"button centres not rewritten: {error}"})
    return state


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the default positions into the live guest (default: read-only)")
    parser.add_argument("--no-buttons", action="store_true",
                        help="with --apply, leave the live Hud::Button centres alone")
    parser.add_argument("--adb-port", type=int, default=None)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        guest = guest_from_runtime(args.adb_port, args.serial)
        report = restore_defaults(guest, apply=args.apply, buttons=not args.no_buttons)
    except (HudLayoutError, subprocess.SubprocessError, OSError) as error:
        report = {"ok": False, "error": str(error)}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"ok={report.get('ok')} action={report.get('action', 'read')}")
        for row in report.get("drift", []):
            print(f"  button {row['index']:>2}: current {row['current']} -> default {row['default']}"
                  f" (delta {row['delta']})")
        for row in report.get("buttons", {}).get("moved", []):
            print(f"  button #{row['index']} centre {row['from']} -> {row['to']}")
        for key in ("hint", "error"):
            if report.get(key):
                print(f"  {key}: {report[key]}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

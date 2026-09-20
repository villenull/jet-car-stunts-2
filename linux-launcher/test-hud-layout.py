#!/usr/bin/env python3
"""Tests for hud_layout: reading and restoring the live in-race HUD positions.

The fake guest implements just the command shapes hud_layout shells out to
(pidof, /proc maps, id -u, page-aligned dd reads/writes over /proc/pid/mem), so
these tests prove the drift maths, the page-preserving write, and every
fail-closed guard without an emulator.
"""
from __future__ import annotations

import base64
import re
import shlex
import struct
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hud_layout
from hud_layout import (BUTTON_COUNT, PAGE, Guest, HudLayoutError, layout_report,
                        read_mem, restore_defaults)

LIB_BASE = 0xD0782000
PID = 3357


def packed(pairs):
    return struct.pack(f"<{len(pairs) * 2}f", *[value for pair in pairs for value in pair])


# The measured Deck drift: the six right-hand buttons sit 664 logical units
# left of the game's defaults; the left-hand buttons were never touched.
DEFAULTS = [(994, 450), (994, 610), (30, 610), (30, 450), (994, 260), (1004, 20),
            (1004, 20), (20, 20), (30, 260), (30, 260), (784, 260), (240, 260)]
DRIFTED = [(994 - 664, 450), (994 - 664, 610), (30, 610), (30, 450), (994 - 664, 260),
           (1004 - 664, 20), (1004 - 664, 20), (20, 20), (30, 260), (30, 260),
           (784 - 664, 260), (240, 260)]


class FakeGuest(Guest):
    """Serves /proc/pid/mem style commands from a dict of pages."""

    def __init__(self, memory, mapped=True, pid_output=str(PID), uid="0", base=LIB_BASE, has_su=True):
        super().__init__(adb=["fake-adb"])
        self.memory = bytearray(memory)
        self.mapped = mapped
        self.pid_output = pid_output
        self.uid = uid
        self.has_su = has_su
        self.base = base
        self.writes = []
        self.privileged_calls = 0

    def _address(self, addr):
        return addr - self.base

    def privileged(self, script, timeout=None):
        """Emulate the real privileged() wrapper exactly as it is produced."""
        if self.uid == "0":
            return self.shell(script, timeout=timeout)
        return self.shell(f"su 0 sh -c {shlex.quote(script)}", timeout=timeout)

    def shell(self, script, timeout=None, check=True):
        if script.startswith("pidof "):
            return self.pid_output
        if script == "id -u":
            return self.uid
        # hud_layout routes root-only work through `su 0 sh -c '<script>'`
        match = re.match(r"^su 0 sh -c '(.*)'$", script, re.S)
        if match:
            self.privileged_calls += 1
            if not self.has_su:
                raise hud_layout.HudLayoutError("guest root command failed (1): su: not found")
            if self.uid != "0":
                # su escalates: the wrapped script now sees uid 0
                self.uid = "0"
            return self.shell(match.group(1), timeout=timeout, check=check)
        if script.startswith("cat /proc/"):
            if not self.mapped:
                return "1000-2000 r-xp 00000000 00:00 0\n"
            return (f"{hex(self.base)}-{hex(self.base + 0x100000)} r--p 00000000 fc:01 1  {hud_layout.LIB_NAME}\n"
                    f"{hex(self.base + 0x200000)}-{hex(self.base + 0x201000)} rw-p 00200000 fc:01 1  "
                    f"{hud_layout.LIB_NAME}\n")
        match = re.match(r"dd if=/proc/\d+/mem bs=(\d+) skip=(\d+) count=(\d+).*\| base64$", script)
        if match:
            if self.uid != "0":
                raise hud_layout.HudLayoutError("permission denied reading /proc/pid/mem")
            size, page, count = (int(match.group(i)) for i in (1, 2, 3))
            start = page * size - self.base
            return base64.b64encode(bytes(self.memory[start:start + size * count])).decode()
        match = re.match(r"printf %s (\S+) \| base64 -d \| dd of=/proc/\d+/mem bs=(\d+) seek=(\d+) "
                         r"count=(\d+)$", script)
        if match:
            if self.uid != "0":
                raise hud_layout.HudLayoutError("permission denied writing /proc/pid/mem")
            payload = base64.b64decode(match.group(1))
            size, page, count = (int(match.group(i)) for i in (2, 3, 4))
            start = page * size - self.base
            self.writes.append((start, len(payload)))
            self.memory[start:start + len(payload)] = payload[:size * count]
            return ""
        raise AssertionError(f"unexpected guest command: {script}")


BUTTON_SPARE = 0x100000  # fake heap area holding the button structs
# g_ppHudButtons order measured on the live guest: each index maps to one
# m_fHudPositions pair.
BUTTON_ORDER = hud_layout.BUTTON_ORDER


def build_guest(current=DRIFTED, default=DEFAULTS, mirrored=False, **kwargs):
    memory = bytearray(0x480000)
    memory[0x200000:0x200000 + 0x1000] = bytes([0xAB]) * 0x1000  # sentinel neighbourhood
    memory[0x43C000:0x43D000] = bytes([0x5A]) * 0x1000  # sentinel page holding m_fHudPositions
    memory[hud_layout.SYMBOLS["m_fHudPositions"][0]:hud_layout.SYMBOLS["m_fHudPositions"][0] + 96] = packed(current)
    memory[hud_layout.SYMBOLS["m_fHudPositionsDefault"][0]:
           hud_layout.SYMBOLS["m_fHudPositionsDefault"][0] + 96] = packed(default)
    memory[hud_layout.SYMBOLS["m_bHudMirrored"][0]] = 1 if mirrored else 0
    ptrs = []
    for i, source in enumerate(BUTTON_ORDER):
        address = BUTTON_SPARE + i * 0x40
        ptrs.append(LIB_BASE + address)
        struct.pack_into("<4f", memory, address + 0x10, 42.0, 291.0, 122.0, 371.0)
        struct.pack_into("<2f", memory, address + 0x20, *current[source])
    struct.pack_into("<12I", memory, hud_layout.SYMBOLS["g_ppHudButtons"][0], *ptrs)
    return FakeGuest(memory, **kwargs)


def button_centres(guest):
    base = LIB_BASE
    out = []
    for i in range(12):
        address = BUTTON_SPARE + i * 0x40 + 0x20
        out.append(struct.unpack_from("<2f", guest.memory, address))
    return out


class TestLayoutReport(unittest.TestCase):
    def test_drift_pairs_are_named_individually(self):
        report = layout_report([v for pair in DRIFTED for v in pair],
                               [v for pair in DEFAULTS for v in pair], False)
        self.assertFalse(report["matches_default"])
        self.assertEqual([row["index"] for row in report["drift"]], [0, 1, 4, 5, 6, 10])
        self.assertTrue(all(row["delta"] == [-664.0, 0.0] for row in report["drift"]))

    def test_matching_layout_is_ok(self):
        report = layout_report([v for pair in DEFAULTS for v in pair],
                               [v for pair in DEFAULTS for v in pair], False)
        self.assertTrue(report["ok"])
        self.assertEqual(report["drift"], [])

    def test_mirrored_layout_is_never_ok(self):
        report = layout_report([v for pair in DEFAULTS for v in pair],
                               [v for pair in DEFAULTS for v in pair], True)
        self.assertFalse(report["ok"])

    def test_odd_float_count_is_rejected(self):
        with self.assertRaises(HudLayoutError):
            layout_report([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], False)


class TestReadMem(unittest.TestCase):
    def test_reads_across_a_page_boundary(self):
        guest = build_guest()
        address = LIB_BASE + 0x43C3FE  # 2 bytes before the page end
        got = read_mem(guest, PID, address, 8)
        self.assertEqual(got, bytes(guest.memory[0x43C3FE:0x43C406]))

    def test_short_read_is_an_error(self):
        guest = build_guest()
        guest.memory = bytearray(16)  # pages now read back short
        with self.assertRaises(HudLayoutError):
            read_mem(guest, PID, LIB_BASE + 0x1000, 4)


class TestRestoreDefaults(unittest.TestCase):
    def test_dry_run_reports_drift_and_writes_nothing(self):
        guest = build_guest()
        report = restore_defaults(guest, apply=False)
        self.assertEqual(report["action"], "dry-run")
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["drift"]), 6)
        self.assertEqual(guest.writes, [])
        self.assertEqual(bytes(guest.memory[hud_layout.SYMBOLS["m_fHudPositions"][0]:
                                           hud_layout.SYMBOLS["m_fHudPositions"][0] + 96]),
                         packed(DRIFTED))

    def test_apply_copies_defaults_and_verifies(self):
        guest = build_guest()
        report = restore_defaults(guest, apply=True)
        self.assertEqual(report["action"], "restored")
        self.assertTrue(report["ok"])
        self.assertTrue(report["after"]["matches_default"])
        self.assertEqual(bytes(guest.memory[hud_layout.SYMBOLS["m_fHudPositions"][0]:
                                           hud_layout.SYMBOLS["m_fHudPositions"][0] + 96]),
                         packed(DEFAULTS))
        # the default array itself and neighbouring bytes stay untouched
        self.assertEqual(bytes(guest.memory[hud_layout.SYMBOLS["m_fHudPositionsDefault"][0]:
                                           hud_layout.SYMBOLS["m_fHudPositionsDefault"][0] + 96]),
                         packed(DEFAULTS))
        self.assertEqual(guest.memory[0x200000], 0xAB)
        # the rest of the page holding the array is preserved byte for byte
        self.assertEqual(guest.memory[0x43C000:0x43C29C], bytes([0x5A]) * 0x29C)
        self.assertEqual(guest.memory[0x43C35C:0x43C35C + 64], bytes([0x5A]) * 64)
        # the live Hud::Button centres move to the defaults too, so the drawn
        # rects follow without waiting for a HUD rebuild
        report_buttons = report["buttons"]
        self.assertEqual(report_buttons["action"], "restored-buttons")
        self.assertEqual(len(report_buttons["moved"]), 6)
        centres = button_centres(guest)
        for i, source in enumerate(BUTTON_ORDER):
            self.assertEqual(centres[i], DEFAULTS[source])

    def test_apply_is_idempotent(self):
        guest = build_guest(current=DEFAULTS)
        report = restore_defaults(guest, apply=True)
        self.assertEqual(report["action"], "none")
        self.assertTrue(report["ok"])
        self.assertEqual(guest.writes, [])

    def test_missing_process_fails_closed(self):
        guest = build_guest(pid_output="")
        report = restore_defaults(guest, apply=True)
        self.assertFalse(report["ok"])
        self.assertIn("expected exactly one", report["error"])
        self.assertEqual(guest.writes, [])

    def test_two_processes_fail_closed(self):
        guest = build_guest(pid_output="3357 3358")
        report = restore_defaults(guest, apply=True)
        self.assertFalse(report["ok"])
        self.assertEqual(guest.writes, [])

    def test_unmapped_library_fails_closed(self):
        guest = build_guest(mapped=False)
        report = restore_defaults(guest, apply=True)
        self.assertFalse(report["ok"])
        self.assertIn("not mapped", report["error"])
        self.assertEqual(guest.writes, [])

    def test_mirrored_guest_refuses_to_write(self):
        guest = build_guest(mirrored=True)
        report = restore_defaults(guest, apply=True)
        self.assertFalse(report["ok"])
        self.assertIn("mirrored", report["error"])
        self.assertEqual(guest.writes, [])

    def test_implausible_defaults_refuse_to_write(self):
        guest = build_guest(default=[(float("nan"), 0)] * BUTTON_COUNT)
        report = restore_defaults(guest, apply=True)
        self.assertFalse(report["ok"])
        self.assertIn("plausible", report["error"])
        self.assertEqual(guest.writes, [])

    def test_unreadable_layout_fails_closed(self):
        guest = build_guest()
        guest.memory = bytearray(16)
        report = restore_defaults(guest, apply=True)
        self.assertFalse(report["ok"])
        self.assertIn("short read", report["error"])

    def test_unprivileged_shell_escalates_through_su(self):
        guest = build_guest(uid="2000")
        report = restore_defaults(guest, apply=True)
        self.assertTrue(report["ok"], report.get("error"))
        self.assertEqual(report["action"], "restored")
        self.assertGreater(guest.privileged_calls, 0)  # went through su, not `adb root`

    def test_missing_su_fails_closed(self):
        guest = build_guest(uid="2000", has_su=False)
        report = restore_defaults(guest, apply=True)
        self.assertFalse(report["ok"])
        self.assertIn("su", report["error"])
        self.assertEqual(guest.writes, [])

    def test_report_exposes_symbol_addresses_for_the_log(self):
        guest = build_guest()
        report = restore_defaults(guest)
        self.assertEqual(report["lib_base"], hex(LIB_BASE))
        self.assertEqual(report["symbols"]["m_fHudPositions"], hex(LIB_BASE + 0x43C29C))
        self.assertEqual(report["buttons"], BUTTON_COUNT)
        self.assertEqual(report["pid"], PID)


class TestPrivilegedWrapper(unittest.TestCase):
    """The lane keeps adbd unrooted, so guest root work must go through su."""

    def _guest(self, uid):
        guest = Guest(adb=["adb"])
        calls = []

        def fake_run(args, timeout=None):
            calls.append(list(args))
            out = f"{uid}\n" if args[-1] == "id -u" else ""
            return subprocess.CompletedProcess(list(args), 0, out, "")

        guest.run = fake_run
        return guest, calls

    def test_unrooted_shell_uses_su_sh_c(self):
        guest, calls = self._guest(2000)
        guest.privileged("cat /proc/1/maps")
        self.assertEqual(calls[0][-1], "id -u")
        self.assertEqual(calls[1], ["shell", "su 0 sh -c 'cat /proc/1/maps'"])

    def test_root_shell_runs_directly(self):
        guest, calls = self._guest(0)
        guest.privileged("cat /proc/1/maps")
        self.assertEqual(calls[-1][-1], "cat /proc/1/maps")


if __name__ == "__main__":
    unittest.main(verbosity=2)

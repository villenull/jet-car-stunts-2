#!/usr/bin/env python3
"""Cycling-feasibility budget proof for the SOUND toggle (offline, read-only).

Question: can a guarded HIGH-attempt with LOW fallback + truthful label fit
in the available executable space of the pristine libtrueaxis.so?

Method: hash-gate the pristine lib, scan .text for executable zero-caves,
size the MINIMAL honest op sequence (Thumb-2 encodings documented inline),
and assert needed > available. This test passing means "not fittable as a
minimal hash-gated patch" on the current binary; if the binary changes, the
hash gate forces re-evaluation instead of silent reuse.

No device, no emulator, no repo mutation. Lane: game-fixes/audio only.
"""
import hashlib
import unittest
import zipfile
from pathlib import Path

ORIG_APK = (Path(__file__).resolve().parents[2] / "backups"
            / "usb-20260910T005449Z" / "split_config.armeabi_v7a.apk")
ORIG_LIB_SHA256 = ("bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d")
MEMBER = "lib/armeabi-v7a/libtrueaxis.so"

TEXT_OFF, TEXT_VA, TEXT_SIZE = 0xAB038, 0xAB038, 0x11D028
CAVE_VA, CAVE_SIZE = 0x1C8020, 64

# Hijack sites (blx UpdateDoubleBuffer) that a cycling design would retarget.
HIJACK_SITES = [0x154A7C, 0x11E318]

# Minimal HONEST op sequence for guarded attempt + fallback + truthful label.
# Sizes are Thumb-2 encoding realities (pools counted: they must live in the
# cave itself; nothing else executable-adjacent is free). Dropping any row
# either crashes (preserve/verify), or lies (label), or mutes (fallback).
MIN_OPS = [
    # Preserve caller state: callback uses r4,r5,r6,r8 after the blx
    # (r6 = canary base, r8 = spill). push{r4-r6,lr}(2) + str r8,[sp,#-4]!(4)
    # + sub sp,#0x10 scratch(2).
    ("preserve", 8),
    # Direction check on new flag in r0: cmp + bne.
    ("direction", 4),
    # Three external calls via PLT (stop, start/HIGH, start/LOW fallback):
    # ldr rX,[pc,#pool](2) + blx rX(2) + pool word(4) each.
    ("calls_stop_start_fallback", 24),
    # Build struct base once: ldr-literal(2) + pool word(4).
    ("struct_base", 6),
    # Verify player + queue words non-NULL: ldr(2)+cbz(2)+ldr.w(4)+cbz(2).
    ("verify_interfaces", 10),
    # Restore flag byte on fallback: strb (2).
    ("flag_restore", 2),
    # Truthful label on fallback: WString build + SetText + dtor, 5 calls
    # with pools (conservative low figure; reuses no existing tail).
    ("label_fixup", 40),
    # Epilogue: add sp(2) + ldr r8(4) + pop{r4-r6,pc}(2).
    ("epilogue", 8),
]
MIN_NEEDED = sum(b for _, b in MIN_OPS)  # 102

# Headless-verifiable backend discriminators for a future isolated slot.
# Audible output remains a user gate; these prove backend MODE, not audibility.
HEADLESS_DISCRIMINATORS = {
    "low": {"frameCount": 1066, "notificationFrames": 355,
            "fast_denied_survivable": True},
    "high_predicted": {"channels": 2, "note": "stereo/44100-class counts "
                       "iff HIGH ever initializes; prediction, not evidence"},
    "crash_signature": {"signal": "SIGSEGV SEGV_MAPERR addr 0x0",
                        "after": "FAST denied + TrackPlayerBase create"},
}


def load_orig_lib() -> bytes:
    raw = ORIG_APK.read_bytes()
    with zipfile.ZipFile(ORIG_APK) as source:
        data = source.read(MEMBER)
    assert hashlib.sha256(data).hexdigest() == ORIG_LIB_SHA256, \
        "pristine lib drifted; re-evaluate, do not reuse this proof"
    return data


def zero_runs(text: bytes):
    runs = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == 0:
            j = i
            while j < n and text[j] == 0:
                j += 1
            if j - i >= 16:
                runs.append((TEXT_VA + i, j - i))
            i = j
        else:
            i += 1
    return runs


class CyclingBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_orig_lib()
        cls.text = cls.data[TEXT_OFF:TEXT_OFF + TEXT_SIZE]

    def test_pristine_hash_gated(self):
        self.assertEqual(hashlib.sha256(self.data).hexdigest(),
                         ORIG_LIB_SHA256)

    def test_single_64b_cave(self):
        runs = zero_runs(self.text)
        self.assertEqual(runs, [(CAVE_VA, CAVE_SIZE)])

    def test_hijack_sites_reach_cave(self):
        # Retargeting itself is encodable (24-bit blx range); space, not
        # range, is the blocker.
        for site in HIJACK_SITES:
            self.assertLess(abs(CAVE_VA - site), 0x1000000)

    def test_minimal_honest_design_exceeds_cave(self):
        self.assertEqual(MIN_NEEDED, 102)
        self.assertGreater(MIN_NEEDED, CAVE_SIZE)

    def test_stripped_design_still_exceeds_or_lies(self):
        # Without label_fixup (40): 62B fits by 2B, but then a failed HIGH
        # leaves flag/label claiming HIGH on a LOW/silent engine (fake),
        # with no room left for pools, re-verify, or failure surfacing.
        # Honest minimum therefore always exceeds the cave.
        stripped = MIN_NEEDED - 40
        self.assertLessEqual(stripped, CAVE_SIZE)  # fits physically...
        # ...yet is forbidden: it cannot keep flag==label==engine. The
        # feasibility claim is that NO fittable subset is honest.
        honest_minimum = MIN_NEEDED
        self.assertGreater(honest_minimum, CAVE_SIZE)

    def test_headless_discriminators_wellformed(self):
        low = HEADLESS_DISCRIMINATORS["low"]
        self.assertEqual(low["frameCount"], 1066)
        self.assertIn("SIGSEGV", HEADLESS_DISCRIMINATORS["crash_signature"]["signal"])


if __name__ == "__main__":
    unittest.main()

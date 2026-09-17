#!/usr/bin/env python3
"""Editor save+reopen gate verifier (offline, read-only).

Asserts on a libtrueaxis.so copy:
  1. Library identity: size + SHA-256 (+ BuildID presence).
  2. Gate sites: file 0x159a9a holds EITHER stock 48b3 (save cbz) or
     patched 00bf (nop); file 0x15a9a8 holds EITHER stock b8b3 (reopen
     cbz) or patched 00bf -- nothing else.
  3. Neighbour gate calls intact: file 0x159a96 = 4cf706ef and file
     0x15a9a4 = 4bf77eef (blx IsLevelEditorUnlocked, both consumers).
  4. Predicate + sibling sites undisturbed: IsLevelEditorUnlocked body
     head (push b5d0 @0x10fc18), OnSave/OnCreate/OnSelect heads, audio v3,
     maps 12B head, six sentinels ORIGINAL (c06b) in a standalone lib.
  5. Save/reopen-chain symbols present: OnSaveLevelClicked,
     OnSelectUserLevelClicked, OnCreateButtonClicked,
     IsLevelEditorUnlocked, InGameLevelEditor::Save, LoadUserLevelFiles,
     SelectLevel, IsItemPurchasedPc, Store_IsItemPurchased.

Fails closed (nonzero exit) on identity/gate/chain mismatch. Never modifies
its input. Independent decode bar: run
  llvm-objdump --arch-name=thumb --mcpu=cortex-a9 -d \
    --start-address=0x159a80 --stop-address=0x159ab0 <lib>
  llvm-objdump --arch-name=thumb --mcpu=cortex-a9 -d \
    --start-address=0x15a98c --stop-address=0x15a9b0 <lib>
and confirm `blx ...IsLevelEditorUnlocked@plt` + `cbz` (stock) or `nop`
(patched) at both sites.

Usage:
    python3 verify_editor_save.py <libtrueaxis.so>
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

EXPECTED_LIB_SIZE = 2227488
ORIGINAL_LIB_SHA256 = (
    "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d"
)
KNOWN_BUILDID = "39f1603fac960e176dfbfd39f4a00bc2c3c43234"

GATE_SITES = [
    (0x159A9A, bytes.fromhex("48b3"), "save cbz r0,0x159af0"),
    (0x15A9A8, bytes.fromhex("b8b3"), "reopen cbz r0,0x15aa1a"),
]
GATE_PATCHED = bytes.fromhex("00bf")
BLX_SITES = [
    (0x159A96, bytes.fromhex("4cf706ef"), "save blx IsLevelEditorUnlocked"),
    (0x15A9A4, bytes.fromhex("4bf77eef"),
     "reopen blx IsLevelEditorUnlocked"),
]

BYTE_CHECKS = [
    (0x10FC18, bytes.fromhex("d0b5"),
     "IsLevelEditorUnlocked head (push {r4,r6,r7,lr})"),
    (0x159A80, bytes.fromhex("f0b5"),
     "OnSaveLevelClicked head (push {r4-r7,lr})"),
    (0x15A98C, bytes.fromhex("f0b5"),
     "OnSelectUserLevelClicked head (push {r4-r7,lr})"),
    (0x15B86C, bytes.fromhex("f0b5"),
     "OnCreateButtonClicked head (push {r4-r7,lr})"),
    (0x154A7C, bytes.fromhex("4ff7aeea"),
     "audio v3 site 1 must be ORIGINAL here (standalone lib)"),
    (0x133014, bytes.fromhex("80b56f46"),
     "maps predicate head must be ORIGINAL here (standalone lib)"),
    (0x133046, bytes.fromhex("c06b"), "sentinel 1 ORIGINAL"),
    (0x13D846, bytes.fromhex("c06b"), "sentinel 2 ORIGINAL"),
    (0x13D944, bytes.fromhex("c06b"), "sentinel 3 ORIGINAL"),
    (0x14BFBC, bytes.fromhex("c06b"), "sentinel 4 ORIGINAL"),
    (0x14C10A, bytes.fromhex("c06b"), "sentinel 5 ORIGINAL"),
    (0x14C11C, bytes.fromhex("c06b"), "sentinel 6 ORIGINAL"),
]

REQUIRED_SYMBOLS = [
    "_ZN34UiFormUserLevelPlaythroughComplete18OnSaveLevelClickedEP15UiControlButton",
    "_ZN16UiFormUserLevels24OnSelectUserLevelClickedEP15UiControlButton",
    "_ZN16UiFormUserLevels21OnCreateButtonClickedEP15UiControlButton",
    "_Z32StoreItems_IsLevelEditorUnlockedv",
    "_ZN17InGameLevelEditor4SaveEPc",
    "_Z25Levels_LoadUserLevelFilesv",
    "_ZN9UiManager11SelectLevelEj",
    "_Z15IsItemPurchasedPc",
    "_Z21Store_IsItemPurchasedPKc",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("library", type=Path)
    args = ap.parse_args()
    if not args.library.exists():
        print(f"ERROR: {args.library} not found", file=sys.stderr)
        return 2
    data = args.library.read_bytes()
    failures = []

    if len(data) != EXPECTED_LIB_SIZE:
        failures.append(f"size {len(data)} != {EXPECTED_LIB_SIZE}")
    digest = hashlib.sha256(data).hexdigest()
    if digest not in (ORIGINAL_LIB_SHA256,):
        failures.append(
            f"sha256 {digest} != standalone original "
            f"{ORIGINAL_LIB_SHA256} (if this is a composed candidate, "
            "verify it through check_combined.py, not this standalone check)")
    if bytes.fromhex(KNOWN_BUILDID) not in data[:0x200]:
        failures.append("BuildID absent (wrong library build)")

    for off, orig, label in GATE_SITES:
        gate = data[off:off + 2]
        if gate == orig:
            print(f"gate @{off:#x} ({label}): ORIGINAL (stock, "
                  f"{orig.hex()})")
        elif gate == GATE_PATCHED:
            print(f"gate @{off:#x} ({label}): PATCHED (nop, 00bf)")
        else:
            failures.append(
                f"gate @{off:#x} ({label}): got {gate.hex()} "
                f"expected {orig.hex()} (original) or 00bf (patched)")

    for off, exp, label in BLX_SITES:
        act = data[off:off + 4]
        if act != exp:
            failures.append(
                f"gate call @{off:#x} ({label}): got {act.hex()} "
                f"expected {exp.hex()}")

    for off, exp, desc in BYTE_CHECKS:
        act = data[off:off + len(exp)]
        if act != exp:
            failures.append(
                f"@{off:#x} {desc}: got {act.hex()} "
                f"expected {exp.hex()}")

    out = subprocess.run(["readelf", "-Ws", str(args.library)],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        failures.append("readelf failed: " + out.stderr.strip())
    else:
        missing = [s for s in REQUIRED_SYMBOLS if s not in out.stdout]
        if missing:
            failures.append("missing symbols: " + ", ".join(missing))

    if failures:
        print(f"FAIL: {len(failures)} violation(s) in {args.library}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS: editor save+reopen invariants hold for {args.library}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

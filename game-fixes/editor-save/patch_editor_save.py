#!/usr/bin/env python3
"""Editor-save+reopen gate patcher for libtrueaxis.so (offline, reversible).

Two ENFORCING gates on the SAME predicate (independent llvm decode,
`--arch-name=thumb --mcpu=cortex-a9`, not a custom decoder):

 1. SAVE: UiFormUserLevelPlaythroughComplete::OnSaveLevelClicked
      file 0x159a81 region (symbol 0x159a81 Thumb, 232 bytes)
      +0x15 (file 0x159a96, 4B): blx StoreItems_IsLevelEditorUnlocked@plt
      +0x19 (file 0x159a9a, 2B): cbz r0, 0x159af0 (bytes 48 b3)
    Taken (locked): warning popup via DoPopupMessage (the photographed
    Note). Fall-through (unlocked): name entry via
    UiFormPopupTextInput_Create, then
    UiFormPopupLevelSaveAreYouSure::OnSaveClicked ->
    InGameLevelEditor::Save -> userLevels/%s.bin -> LoadUserLevelFiles.

 2. REOPEN: UiFormUserLevels::OnSelectUserLevelClicked
      file 0x15a98c (symbol 0x15a98d Thumb)
      +0x18 (file 0x15a9a4, 4B): blx StoreItems_IsLevelEditorUnlocked@plt
      +0x1c (file 0x15a9a8, 2B): cbz r0, 0x15aa1a (bytes b8 b3)
    Taken (locked): Yes/No store prompt via DoPopupYesOrNo (Yes leg heads
    to the store via callback 0x15db75) -- a saved track can NOT be
    opened/played. Fall-through (unlocked): UserLevelExists ->
    CloseActiveForm -> SelectLevel -> FadeOut (open/play).

Saving without reopening is not a fix ("files existing while user cannot
use them" is rejected): this patch NOPs BOTH consumers (4 bytes total):

    0x159a9a: 48b3 (cbz r0, 0x159af0) -> 00bf (nop = halfword 0xBF00)
    0x15a9a8: b8b3 (cbz r0, 0x15aa1a) -> 00bf (nop)

Effect: save flow AND open/play flow always run. Untouched: the
StoreItems_IsLevelEditorUnlocked predicate itself, Store_IsItemPurchased
shop predicate, Levels 12B gate, six progression sentinels, g_bUnLockAll /
Difficulty::Unlock, AreAdsDisabled, sound v3, controls button-gate, and the
dismiss-once Create/main-menu notes (cosmetic). No store state is forced,
no purchase/restore network, no saves cleared.

Byte-order warning (controls B1 lesson): Thumb NOP is halfword 0xBF00,
i.e. file bytes 00 BF. The swapped form BF 00 decodes as
`lsls r7, r7, #2` and corrupts r7 -- this patcher writes 00bf only.

ABI note: both neighbouring calls are BLX (Thumb->ARM PLT interworking).
This patch touches no branch -- no Thumb-BL-to-ARM-PLT is introduced.

Usage:
    python3 patch_editor_save.py [--apply|--revert|--check] <libtrueaxis.so>
"""

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

# (offset, original 2B, patched 2B, description)
SITES = [
    (0x159A9A, bytes.fromhex("48b3"), bytes.fromhex("00bf"),
     "save cbz r0,0x159af0 -> nop"),
    (0x15A9A8, bytes.fromhex("b8b3"), bytes.fromhex("00bf"),
     "reopen cbz r0,0x15aa1a -> nop"),
]

EXPECTED_LIB_SHA256 = (
    "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d"
)
EXPECTED_LIB_SIZE = 2227488
KNOWN_BUILDID = "39f1603fac960e176dfbfd39f4a00bc2c3c43234"

# Sites owned by sibling workers -- this patch must never overlap them.
# (Read-only disjointness guard; sibling files are never edited.)
RESERVED_RANGES = [
    (0x154A7C, 4, "audio v3 site 1"),
    (0x11E318, 6, "audio v3 site 2"),
    (0x154A1C, 4, "audio v3 site 3"),
    (0x133014, 16, "maps Levels_IsPurchased 12B (changed tail 12)"),
    (0x133046, 2, "sentinel 1"),
    (0x13D846, 2, "sentinel 2"),
    (0x13D944, 2, "sentinel 3"),
    (0x14BFBC, 2, "sentinel 4"),
    (0x14C10A, 2, "sentinel 5"),
    (0x14C11C, 2, "sentinel 6"),
    (0x10BC62, 4, "controls button-gate NOP"),
]


def state(data: bytes) -> str:
    states = []
    for off, orig, patched, _ in SITES:
        cur = data[off:off + 2]
        if cur == orig:
            states.append("original")
        elif cur == patched:
            states.append("patched")
        else:
            states.append("unknown")
    if all(s == "original" for s in states):
        return "original"
    if all(s == "patched" for s in states):
        return "patched"
    return "mixed:" + ",".join(states)


def main():
    parser = argparse.ArgumentParser(
        description="Editor save+reopen gate patcher (2x CBZ -> NOP).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true")
    group.add_argument("--revert", action="store_true")
    group.add_argument("--check", action="store_true")
    parser.add_argument("library", type=Path)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    if not args.library.exists():
        print(f"ERROR: {args.library} not found")
        sys.exit(1)
    data = args.library.read_bytes()
    print(f"File: {args.library}")
    print(f"Size: {len(data)} bytes")
    print(f"SHA-256: {hashlib.sha256(data).hexdigest()}")
    if bytes.fromhex(KNOWN_BUILDID) not in data[:0x200]:
        print("WARNING: BuildID mismatch; refusing (wrong library build).")
        sys.exit(1)

    reserved = {r for r, ln, _ in RESERVED_RANGES
                for r in range(r, r + ln)}
    for off, _, _, desc in SITES:
        if off in reserved or off + 1 in reserved:
            print(f"ERROR: site {off:#x} ({desc}) overlaps a reserved "
                  "sibling range; refusing.")
            sys.exit(1)

    current = state(data)
    if args.check:
        print(f"Editor save+reopen patch state: {current.upper()}")
        for (off, orig, patched, desc), s in zip(
                SITES, current.replace("mixed:", "").split(",")
                if current.startswith("mixed:") else [current] * len(SITES)):
            print(f"  @{off:#x} {desc}: {s}")
        print("  original = stock blocks; patched = save+reopen always run")
        sys.exit(0 if current in ("original", "patched") else 1)

    if args.apply:
        if current == "patched":
            print("Already patched. Nothing to do.")
            return
        if current != "original":
            print(f"ERROR: refusing to apply on {current} state "
                  "(revert to original first).")
            sys.exit(1)
        if len(data) != EXPECTED_LIB_SIZE or \
                hashlib.sha256(data).hexdigest() != EXPECTED_LIB_SHA256:
            print("ERROR: refusing to patch: not the original v1.0.23 "
                  "library (size/hash mismatch). Patch applies ONLY on top "
                  "of the proven original bytes.")
            sys.exit(1)
        if not args.no_backup:
            bak = args.library.with_suffix(".so.original")
            if not bak.exists():
                shutil.copy2(args.library, bak)
                print(f"Backup saved to {bak}")
        out = bytearray(data)
        for off, _, patched, desc in SITES:
            out[off:off + 2] = patched
        args.library.write_bytes(bytes(out))
        print(f"Applied editor save+reopen patch "
              f"({len(SITES)} sites, 4 bytes: save 48b3->00bf @0x159a9a, "
              "reopen b8b3->00bf @0x15a9a8).")
    else:  # revert
        if current == "original":
            print("Already original. Nothing to do.")
            return
        if current.startswith("mixed:"):
            print(f"Reverting from {current} state.")
        out = bytearray(data)
        for off, orig, _, _ in SITES:
            out[off:off + 2] = orig
        args.library.write_bytes(bytes(out))
        print("Reverted editor save+reopen patch (both sites to stock).")


if __name__ == "__main__":
    main()

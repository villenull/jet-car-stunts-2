#!/usr/bin/env python3
"""Maps ownership patcher for libtrueaxis.so (offline, reversible).

Target: `Levels_IsPurchased(int)` at file offset 0x133014 (16 bytes).
The patch makes it unconditionally return 1 (owned):

    original: 80b5 6f46 70f7 9ceb bde8 8040 93f0 debd
              (push; mov; BL Levels_GetLevelPack via PLT; epilogue)
    patched:  80b5 6f46 0120 80bd bf00 bf00 bf00 bf00
              (push; mov; movs r0,#1; pop {r7,pc}; nop x4)

Why this site (independent audit ruling, tilt worker round-10):
  `Levels_IsPurchased` has EXACTLY 3 callers via its true PLT slot
  0xa592c (GOT 0x21e2fc, reloc-bound): 0x13d2c8 LevelRow ctor (stores
  byte to row+0xc41), 0x14bf4c LevelSelect population, 0x158ece
  UserChallenges click (beq skip). Return-1 is consumed at these 3
  verified UI gates; effect is BOUNDED (non-inert): shop (6 predicate
  sites), progression (IsLocked + sentinels), unlock-all all
  re-verified untouched. APPROVED per auditor contract.
  [Superseded history: the "exactly 2" claim and the round-7 "1 DLC-zip
  caller / 0xa781c slot" claim are both FALSE — see CENSUS-CORRECTION.md.
  My custom decoder and stride scans are out of the chain.]
  * Game::LoadLevel enforces NO purchase/lock check (verified by call scan),
    so owning the UI gate is sufficient for gameplay.
  * Untouched: Store_IsItemPurchased predicate (shop screen keeps stock
    behavior), the six progression sentinel sites (progression stays
    gated), g_bUnLockAll + Stats::Level::Difficulty::Unlock (the optional
    store unlock-all path stays a distinct user action), ads/editor
    predicates (separate functions).
  * 16-byte in-place replacement: no address shifts, no new sections.

This patch changes APK bytes, so a patched build must be re-signed with
one project key and can only ever be a FRESH install (cert differs from
the original True Axis cert). It must NEVER in-place update the personal
guest. See compose_maps_candidate.py.

Usage:
    python3 patch_maps_ownership.py [--apply|--revert|--check] <libtrueaxis.so>
"""

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

OFFSET = 0x133014
ORIGINAL_BYTES = bytes.fromhex("80b56f4670f79cebbde8804093f0debd")
PATCHED_BYTES = bytes.fromhex("80b56f46012080bdbf00bf00bf00bf00")

EXPECTED_LIB_SHA256 = (
    "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d"
)
EXPECTED_LIB_SIZE = 2227488
KNOWN_BUILDID = "39f1603fac960e176dfbfd39f4a00bc2c3c43234"


def state(data: bytes) -> str:
    current = data[OFFSET:OFFSET + 16]
    if current == ORIGINAL_BYTES:
        return "original"
    if current == PATCHED_BYTES:
        return "patched"
    return "unknown"


def main():
    parser = argparse.ArgumentParser(
        description="Maps ownership patcher (Levels_IsPurchased -> owned).")
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

    current = state(data)
    if args.check:
        print(f"Maps ownership patch state: {current.upper()}")
        print("  original = stock purchase gates; "
              "patched = all map packs owned, progression still gated")
        sys.exit(0 if current in ("original", "patched") else 1)

    if current == "unknown":
        print(f"ERROR: unexpected bytes at {OFFSET:#x}: "
              f"{data[OFFSET:OFFSET+16].hex()}; refusing.")
        sys.exit(1)

    if args.apply:
        if current == "patched":
            print("Already patched. Nothing to do.")
            return
        if hashlib.sha256(data).hexdigest() != EXPECTED_LIB_SHA256:
            print("ERROR: refusing to patch: not the original v1.0.23 "
                  "library (hash mismatch). Patch applies ONLY on top of "
                  "the proven original bytes.")
            sys.exit(1)
        if not args.no_backup:
            bak = args.library.with_suffix(".so.original")
            if not bak.exists():
                shutil.copy2(args.library, bak)
                print(f"Backup saved to {bak}")
        out = bytearray(data)
        out[OFFSET:OFFSET + 16] = PATCHED_BYTES
        args.library.write_bytes(bytes(out))
        print(f"Applied maps ownership patch at {OFFSET:#x} "
              f"({len(PATCHED_BYTES)} bytes).")
    else:  # revert
        if current == "original":
            print("Already original. Nothing to do.")
            return
        out = bytearray(data)
        out[OFFSET:OFFSET + 16] = ORIGINAL_BYTES
        args.library.write_bytes(bytes(out))
        print(f"Reverted maps ownership patch at {OFFSET:#x}.")


if __name__ == "__main__":
    main()

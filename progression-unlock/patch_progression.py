#!/usr/bin/env python3
"""
JCS2 Progression Unlock Patcher

Applies or reverts a targeted 12-byte binary patch to libtrueaxis.so that
bypasses medal-based progression locks in the level select UI. Does NOT
affect IAP/purchase gating, existing medal values, scores, or times.

The patch changes 6 instances of the Thumb instruction:
    ldr r0, [r0, #0x3c]   (bytes: c0 6b)
to:
    movs r0, #0            (bytes: 00 20)

at specific addresses that perform the -1 sentinel check for
"progression locked." Medal display code reads the same field but
without the sentinel check, so it is unaffected.

Usage:
    python3 patch_progression.py [--apply|--revert|--check] <libtrueaxis.so>

The tool operates on a file in-place. Use --check to inspect without modifying.
"""

import argparse
import hashlib
import shutil
import struct
import sys
from pathlib import Path

PATCH_SITES = [
    {
        "offset": 0x133046,
        "function": "Levels_IsLocked",
        "purpose": "standalone lock predicate",
    },
    {
        "offset": 0x13D846,
        "function": "UiControlButtonLevelRow::ctor",
        "purpose": "overall button lock state (Easy check)",
    },
    {
        "offset": 0x13D944,
        "function": "UiControlButtonLevelRow::ctor",
        "purpose": "per-difficulty lock state",
    },
    {
        "offset": 0x14BFBC,
        "function": "UpdateLevelPanelPopulation",
        "purpose": "filter pass 0: show unlocked levels",
    },
    {
        "offset": 0x14C10A,
        "function": "UpdateLevelPanelPopulation",
        "purpose": "purchased level lock check",
    },
    {
        "offset": 0x14C11C,
        "function": "UpdateLevelPanelPopulation",
        "purpose": "filter pass 1: show locked levels",
    },
]

ORIGINAL_BYTES = bytes([0xC0, 0x6B])  # ldr r0, [r0, #0x3c]
PATCHED_BYTES  = bytes([0x00, 0x20])  # movs r0, #0

CONTEXT_AFTER  = bytes([0x01, 0x30])  # adds r0, #1 (sentinel check)

KNOWN_BUILDID = "39f1603fac960e176dfbfd39f4a00bc2c3c43234"


def read_bytes(data: bytes, offset: int, length: int) -> bytes:
    return data[offset:offset + length]


def check_buildid(data: bytes) -> bool:
    marker = bytes.fromhex(KNOWN_BUILDID)
    return marker in data[:0x200]


def check_state(data: bytes):
    """Returns ('original', 'patched', or 'unknown') for each site."""
    results = []
    for site in PATCH_SITES:
        off = site["offset"]
        current = read_bytes(data, off, 2)
        context = read_bytes(data, off + 2, 2)

        if context != CONTEXT_AFTER:
            results.append(("context_mismatch", site))
            continue

        if current == ORIGINAL_BYTES:
            results.append(("original", site))
        elif current == PATCHED_BYTES:
            results.append(("patched", site))
        else:
            results.append(("unknown", site))
    return results


def print_status(results):
    for state, site in results:
        off = site["offset"]
        fn = site["function"]
        tag = {"original": "LOCKED", "patched": "UNLOCKED",
               "context_mismatch": "MISMATCH", "unknown": "UNKNOWN"}[state]
        print(f"  0x{off:08X}  {tag:10s}  {fn}: {site['purpose']}")


def apply_patch(filepath: Path, data: bytes) -> bytes:
    results = check_state(data)
    already = sum(1 for s, _ in results if s == "patched")
    original = sum(1 for s, _ in results if s == "original")
    other = len(results) - already - original

    if other > 0:
        print("ERROR: some sites have unexpected bytes; refusing to patch.")
        print_status(results)
        return data

    if already == len(results):
        print("All sites already patched. Nothing to do.")
        return data

    if already > 0 and original > 0:
        print(f"WARNING: mixed state ({already} patched, {original} original).")
        print("Patching remaining original sites.")

    out = bytearray(data)
    patched_count = 0
    for state, site in results:
        if state == "original":
            off = site["offset"]
            out[off:off + 2] = PATCHED_BYTES
            patched_count += 1
            print(f"  Patched 0x{off:08X} ({site['function']})")

    filepath.write_bytes(bytes(out))
    print(f"\nApplied {patched_count} patches to {filepath}")
    return bytes(out)


def revert_patch(filepath: Path, data: bytes) -> bytes:
    results = check_state(data)
    already = sum(1 for s, _ in results if s == "original")
    patched = sum(1 for s, _ in results if s == "patched")
    other = len(results) - already - patched

    if other > 0:
        print("ERROR: some sites have unexpected bytes; refusing to revert.")
        print_status(results)
        return data

    if already == len(results):
        print("All sites already original. Nothing to do.")
        return data

    out = bytearray(data)
    reverted_count = 0
    for state, site in results:
        if state == "patched":
            off = site["offset"]
            out[off:off + 2] = ORIGINAL_BYTES
            reverted_count += 1
            print(f"  Reverted 0x{off:08X} ({site['function']})")

    filepath.write_bytes(bytes(out))
    print(f"\nReverted {reverted_count} patches in {filepath}")
    return bytes(out)


def main():
    parser = argparse.ArgumentParser(
        description="JCS2 progression-lock patcher for libtrueaxis.so")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true",
                       help="Apply the progression unlock patch")
    group.add_argument("--revert", action="store_true",
                       help="Revert to original locked progression")
    group.add_argument("--check", action="store_true",
                       help="Check current patch state without modifying")
    parser.add_argument("library", type=Path,
                        help="Path to libtrueaxis.so")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip creating a .bak backup before patching")
    args = parser.parse_args()

    if not args.library.exists():
        print(f"ERROR: {args.library} not found")
        sys.exit(1)

    data = args.library.read_bytes()

    if not check_buildid(data):
        print("WARNING: BuildID does not match expected JCS2 1.0.23 library.")
        print("This may be a different version. Proceeding with caution.\n")

    sha = hashlib.sha256(data).hexdigest()
    print(f"File: {args.library}")
    print(f"Size: {len(data)} bytes")
    print(f"SHA-256: {sha}")
    print()

    results = check_state(data)

    if args.check:
        print("Patch site status:")
        print_status(results)
        original = sum(1 for s, _ in results if s == "original")
        patched = sum(1 for s, _ in results if s == "patched")
        if original == len(results):
            print("\nState: ORIGINAL (progression locks active)")
        elif patched == len(results):
            print("\nState: PATCHED (progression locks bypassed)")
        else:
            print("\nState: MIXED or UNKNOWN")
        return

    if args.apply:
        if not args.no_backup:
            bak = args.library.with_suffix(".so.original")
            if not bak.exists():
                shutil.copy2(args.library, bak)
                print(f"Backup saved to {bak}\n")
            else:
                print(f"Backup already exists at {bak}\n")
        apply_patch(args.library, data)

    elif args.revert:
        revert_patch(args.library, data)


if __name__ == "__main__":
    main()

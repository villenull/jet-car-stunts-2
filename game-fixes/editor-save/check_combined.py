#!/usr/bin/env python3
"""Combined compatibility check: editor save+reopen 4B gates on top of the
corrected controls candidate (offline, read-only except a /tmp scratch copy).

Input (never modified): staging/controls-fix-20260915T000000Z/
  libtrueaxis-signed-check.so  (variant lib c5eadf97: sound v3 14B + maps
  12B + six 0020 sentinels + controls NOP 00bf00bf @0x10bc62).

Check:
  1. Input hash/size == c5eadf97 / 2227488 (refuse otherwise).
  2. Both gate sites stock in the input:
       0x159a96 = 4cf706ef (save blx), 0x159a9a = 48b3 (save cbz);
       0x15a9a4 = 4bf77eef (reopen blx), 0x15a9a8 = b8b3 (reopen cbz).
  3. Apply 48b3 -> 00bf @0x159a9a and b8b3 -> 00bf @0x15a9a8 to an
     in-memory copy; assert the diff vs the controls input is EXACTLY 4
     bytes (0x159a9a, 0x159a9b, 0x15a9a8, 0x15a9a9).
  4. Assert all sibling sites intact in the patched copy (sound v3, maps
     12B, six sentinels, controls NOP, Create blx/cbz stock).
  5. Independent decode bar: run
       llvm-objdump --arch-name=thumb --mcpu=cortex-a9 -d \
         --start-address=0x159a80 --stop-address=0x159ab0 <scratch>
       llvm-objdump --arch-name=thumb --mcpu=cortex-a9 -d \
         --start-address=0x15a98c --stop-address=0x15a9b0 <scratch>
     and assert `blx ...IsLevelEditorUnlocked@plt` + `nop` with the next
     instruction in sync at BOTH sites (no desync).
  6. Write the scratch copy (default /tmp/opencode/editor-combined-scratch.so
     or --out) and print its SHA-256. Expected: <see EXPECTED below, printed
     on first proven run and then pinned> (45 changed bytes vs pristine
     bc7fdf9d = 41 controls + 4 editor).

Composition contract (for root, not performed here): rebuild a NEW stage
from originals in sound-first order (v3 -> 12B -> sentinels -> controls NOP
-> this 4B editor NOP pair), faithful repack, ONE project key (DB86
lineage). Final lib hash MUST equal the scratch hash. Same-key reinstall
preserves saves; no clear/uninstall; no purchase/restore network.

Usage:
    python3 check_combined.py [--controls <lib>] [--out <scratch.so>]
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.resolve().parents[1]

DEFAULT_CONTROLS = (ROOT / "staging/controls-fix-20260915T000000Z"
                    / "libtrueaxis-signed-check.so")
CONTROLS_SHA256 = (
    "c5eadf9759a3475c098b7b8054f8b383bf70c8c0ad047b4528f271abfa3bfd42"
)
EXPECTED_SCRATCH_SHA256 = (
    "cb2bd45b85cd6bf161199e57c460870d295b73d4ad2101bb7d97d0d413f0ffb1"
)
LIB_SIZE = 2227488

STOCK_SITES = [
    (0x159A96, bytes.fromhex("4cf706ef"), "save blx"),
    (0x159A9A, bytes.fromhex("48b3"), "save cbz"),
    (0x15A9A4, bytes.fromhex("4bf77eef"), "reopen blx"),
    (0x15A9A8, bytes.fromhex("b8b3"), "reopen cbz"),
]
PATCH_SITES = [
    (0x159A9A, bytes.fromhex("00bf")),
    (0x15A9A8, bytes.fromhex("00bf")),
]

SIBLING_CHECKS = [
    (0x10BC62, bytes.fromhex("00bf00bf"), "controls NOP"),
    (0x154A7C, bytes.fromhex("00bf00bf"), "audio v3 site 1"),
    (0x11E318, bytes.fromhex("00bf00bf00bf"), "audio v3 site 2"),
    (0x154A1C, bytes.fromhex("704700bf"), "audio v3 site 3"),
    (0x133014, bytes.fromhex("80b56f46"), "maps predicate head"),
    (0x133018, bytes.fromhex("012080bdbf00bf00bf00bf00"),
     "maps 12B tail"),
    (0x15B886, bytes.fromhex("4bf70ee8"), "create blx (stock)"),
    (0x15B88C, bytes.fromhex("c0b1"), "create cbz (stock)"),
]
SENTINELS = (0x133046, 0x13D846, 0x13D944, 0x14BFBC, 0x14C10A, 0x14C11C)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--controls", type=Path, default=DEFAULT_CONTROLS)
    ap.add_argument("--out", type=Path,
                    default=Path("/tmp/opencode/editor-combined-scratch.so"))
    args = ap.parse_args()

    failures = []
    if not args.controls.exists():
        print(f"ERROR: controls input not found: {args.controls}")
        return 2
    base = args.controls.read_bytes()
    print(f"controls input: {args.controls}")
    print(f"  size {len(base)}, sha256 {hashlib.sha256(base).hexdigest()}")
    if len(base) != LIB_SIZE or \
            hashlib.sha256(base).hexdigest() != CONTROLS_SHA256:
        print("ERROR: not the corrected controls lib c5eadf97; refusing.")
        return 1

    for off, exp, label in STOCK_SITES:
        act = base[off:off + len(exp)]
        if act != exp:
            failures.append(f"input {label} @{off:#x}: {act.hex()} != "
                            f"{exp.hex()} (already patched or disturbed?)")
        else:
            print(f"input {label} @{off:#x}: stock OK ({exp.hex()})")

    patched = bytearray(base)
    for off, new in PATCH_SITES:
        patched[off:off + 2] = new
    patched = bytes(patched)
    diff = [i for i, (a, b) in enumerate(zip(base, patched)) if a != b]
    want = [0x159A9A, 0x159A9B, 0x15A9A8, 0x15A9A9]
    if diff != want:
        failures.append(f"footprint drift vs controls: {len(diff)} bytes "
                        f"{[hex(x) for x in diff[:10]]} want "
                        f"{[hex(x) for x in want]}")
    else:
        print(f"footprint vs controls: exactly 4 bytes "
              f"{[hex(x) for x in diff]} "
              "(save 48b3->00bf, reopen b8b3->00bf)")

    for off, exp, label in SIBLING_CHECKS:
        act = patched[off:off + len(exp)]
        if act != exp:
            failures.append(f"sibling {label} @{off:#x}: {act.hex()} != "
                            f"{exp.hex()}")
    for off in SENTINELS:
        if patched[off:off + 2] != bytes.fromhex("0020"):
            failures.append(f"sentinel @{off:#x} disturbed: "
                            f"{patched[off:off+2].hex()}")
    if not failures:
        print("siblings: all intact (sound v3, maps 12B, 6 sentinels, "
              "controls NOP, create stock)")

    args.out.write_bytes(patched)
    digest = hashlib.sha256(patched).hexdigest()
    print(f"scratch: {args.out}")
    print(f"  sha256 {digest}")
    print("  changed vs pristine bc7fdf9d: 41 (controls) + 4 (editor) = 45")
    if EXPECTED_SCRATCH_SHA256 != "REPLACE_ME_AFTER_FIRST_PROVEN_RUN" \
            and digest != EXPECTED_SCRATCH_SHA256:
        failures.append(f"scratch hash {digest} != expected "
                        f"{EXPECTED_SCRATCH_SHA256}")

    # Independent decode bar (llvm, not self-golden), BOTH sites.
    jobs = [
        (0x159a80, 0x159ab0, "159a96", "blx", "159a9a", "save"),
        (0x15a98c, 0x15a9b0, "15a9a4", "blx", "15a9a8", "reopen"),
    ]
    for start, stop, blx_addr, _, nop_addr, label in jobs:
        r = subprocess.run(
            ["llvm-objdump", "--arch-name=thumb", "--mcpu=cortex-a9", "-d",
             f"--start-address={start:#x}", f"--stop-address={stop:#x}",
             str(args.out)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            failures.append(f"llvm-objdump failed ({label}): "
                            + r.stderr.strip()[:200])
            continue
        txt = r.stdout
        ok_blx = (f"{blx_addr}:" in txt and "blx" in txt
                  and "IsLevelEditorUnlocked" in txt)
        ok_nop = f"{nop_addr}:" in txt and "nop" in txt
        print(f"llvm {label}: blx gate "
              + ("OK" if ok_blx else "MISSING")
              + ", nop fallthrough "
              + ("OK" if ok_nop else "MISSING"))
        if not (ok_blx and ok_nop):
            failures.append(f"llvm {label} gate decode mismatch")
            print(txt[:1500])

    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: combined compatibility holds "
          f"(controls c5eadf97 + 4B editor NOPs = {digest[:16]}...)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

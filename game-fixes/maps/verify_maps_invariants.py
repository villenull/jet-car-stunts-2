#!/usr/bin/env python3
"""Maps entitlement invariants verifier (offline, read-only).

Checks a libtrueaxis.so copy against the ORIGINAL v1.0.23 bytes and asserts
the maps-fix safety invariants:

  1. Library identity: size + SHA-256 match the original split APK payload.
  2. Ownership predicate intact: Store_IsItemPurchased prologue + purchased
     byte load (ldrb.w r0,[r0,#0x50]) present and UNPATCHED (no blanket true).
  3. Legitimate setter intact: Store_SetTCPurchasedItem purchased byte store
     (strb.w r1,[r0,#0x50]) present (this is the sanctioned own-one-SKU seam).
  4. Progression NOT blanket-unlocked: all six -1 sentinel sites hold ORIGINAL
     bytes (c0 6b). The old six-site sentinel patch is NOT the maps default.
  5. Unlock-all path preserved: g_bUnLockAll symbol + Difficulty::Unlock
     function present; no statement made about their runtime value.
  6. SKU literals present byte-for-byte (including the sic spelling).

Fails closed (nonzero exit) on any mismatch. Never modifies its input.

Usage:
    python3 verify_maps_invariants.py <libtrueaxis.so>
    python3 verify_maps_invariants.py --lib <path>
"""

import argparse
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWLIST_PATH = HERE / "OWNERSHIP-ALLOWLIST.json"

EXPECTED_LIB_SIZE = 2227488
EXPECTED_LIB_SHA256 = (
    "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d"
)

# (file offset, expected file-order bytes, description)
BYTE_CHECKS = [
    (0x107268, bytes.fromhex("80b56f46"),
     "Store_IsItemPurchased prologue (push {r7,lr}; mov r7,sp)"),
    (0x10727E, bytes.fromhex("90f85000"),
     "Store_IsItemPurchased purchased load (ldrb.w r0,[r0,#0x50])"),
    (0x107244, bytes.fromhex("80b56f46"),
     "Store_SetTCPurchasedItem prologue (push {r7,lr}; mov r7,sp)"),
    (0x10725C, bytes.fromhex("80f85010"),
     "Store_SetTCPurchasedItem purchased store (strb.w r1,[r0,#0x50])"),
    (0x133046, bytes.fromhex("c06b"),
     "Levels_IsLocked progression sentinel (ORIGINAL, must NOT be patched)"),
    (0x133048, bytes.fromhex("0130"),
     "Levels_IsLocked sentinel context (adds r0,#1)"),
    (0x13D846, bytes.fromhex("c06b"),
     "UiControlButtonLevelRow::ctor site 2 (ORIGINAL, must NOT be patched)"),
    (0x13D944, bytes.fromhex("c06b"),
     "UiControlButtonLevelRow::ctor site 3 (ORIGINAL, must NOT be patched)"),
    (0x14BFBC, bytes.fromhex("c06b"),
     "UpdateLevelPanelPopulation site 4 (ORIGINAL, must NOT be patched)"),
    (0x14C10A, bytes.fromhex("c06b"),
     "UpdateLevelPanelPopulation site 5 (ORIGINAL, must NOT be patched)"),
    (0x14C11C, bytes.fromhex("c06b"),
     "UpdateLevelPanelPopulation site 6 (ORIGINAL, must NOT be patched)"),
]

PATCHED_SENTINEL = bytes.fromhex("0020")  # movs r0,#0 (six-site patch marker)


def load_allowlist():
    with open(ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)["content_skus"]


def check_symbols(lib: Path):
    """Confirm presence of key dynamic symbols via readelf."""
    out = subprocess.run(
        ["readelf", "-Ws", str(lib)], capture_output=True, text=True, timeout=30
    )
    if out.returncode != 0:
        return False, ["readelf failed: " + out.stderr.strip()]
    text = out.stdout
    required = [
        "_Z21Store_IsItemPurchasedPKc",
        "_Z24Store_SetTCPurchasedItemPKc",
        "_Z15Levels_IsLockedi",
        "_Z18Levels_IsPurchasedi",
        "_Z19Levels_GetLevelPacki",
        "_Z10IAPUnlocksN5Level4PackE",
        "g_bUnLockAll",
        "_ZN5Stats5Level10Difficulty6UnlockEv",
        "_Z35Store_RestoreExistingLocalPurchasesv",
    ]
    missing = [s for s in required if s not in text]
    return (len(missing) == 0, missing)


def verify(lib_path: Path):
    failures = []
    data = lib_path.read_bytes()

    if len(data) != EXPECTED_LIB_SIZE:
        failures.append(
            f"size {len(data)} != expected {EXPECTED_LIB_SIZE}"
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_LIB_SHA256:
        failures.append(
            f"sha256 {digest} != expected {EXPECTED_LIB_SHA256} "
            "(refusing: not the original v1.0.23 library)"
        )

    for offset, expected, desc in BYTE_CHECKS:
        actual = data[offset:offset + len(expected)]
        if actual != expected:
            failures.append(
                f"@{offset:#x} {desc}: got {actual.hex()} "
                f"expected {expected.hex()}"
            )

    # Explicit guard: reject the old six-site sentinel patch as maps default.
    sentinel_offsets = (0x133046, 0x13D846, 0x13D944,
                        0x14BFBC, 0x14C10A, 0x14C11C)
    patched = [o for o in sentinel_offsets
               if data[o:o + 2] == PATCHED_SENTINEL]
    if patched:
        failures.append(
            "six-site progression patch detected at "
            + ", ".join(f"{o:#x}" for o in patched)
            + " — NOT the correct maps default; progression must stay gated"
        )

    try:
        skus = load_allowlist()
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        failures.append(f"allowlist unreadable: {exc}")
        skus = []
    for sku in skus:
        if sku.encode("ascii") + b"\x00" not in data:
            failures.append(f"SKU literal missing from library: {sku}")
    # The sic spelling must be present literally; a 'corrected' spelling must
    # NOT be present (guards against silent SKU mismatch).
    if b"jcs2_platforming_1_75\x00" in data:
        failures.append(
            "unexpected 'corrected' jcs2_platforming_1_75 literal present; "
            "native string is jcs2_patforming_1_75 (sic)"
        )

    ok, missing = check_symbols(lib_path)
    if not ok:
        failures.append("missing required symbols: " + ", ".join(missing))

    return failures


def main():
    parser = argparse.ArgumentParser(
        description="Verify maps entitlement safety invariants (read-only).")
    parser.add_argument("library", nargs="?", type=Path,
                        help="Path to libtrueaxis.so copy")
    parser.add_argument("--lib", dest="library_opt", type=Path, default=None)
    args = parser.parse_args()
    lib = args.library_opt or args.library
    if lib is None:
        print("ERROR: provide a path to libtrueaxis.so", file=sys.stderr)
        return 2
    if not lib.exists():
        print(f"ERROR: {lib} not found", file=sys.stderr)
        return 2
    failures = verify(lib)
    if failures:
        print(f"FAIL: {len(failures)} invariant violation(s) in {lib}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"PASS: maps invariants hold for {lib}")
    print("  ownership predicate intact, setter seam present, "
          "progression gated, unlock-all path preserved, "
          f"{len(load_allowlist())} allowlisted SKU literals present")
    return 0


if __name__ == "__main__":
    sys.exit(main())

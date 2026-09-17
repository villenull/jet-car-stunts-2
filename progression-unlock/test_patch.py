#!/usr/bin/env python3
"""
Offline static tests for the JCS2 progression unlock patch.

Tests verify the patch tool against the actual libtrueaxis.so binary
without modifying the original file.
"""

import hashlib
import shutil
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from patch_progression import (
    CONTEXT_AFTER,
    KNOWN_BUILDID,
    ORIGINAL_BYTES,
    PATCH_SITES,
    PATCHED_BYTES,
    apply_patch,
    check_buildid,
    check_state,
    revert_patch,
)

LIB_PATH = Path(__file__).parent.parent / "analysis/private-state-inspection-20260910/libtrueaxis.so"

EXPECTED_SIZE = 2_227_488
EXPECTED_BUILDID = KNOWN_BUILDID

pass_count = 0
fail_count = 0


def check(name, condition, detail=""):
    global pass_count, fail_count
    if condition:
        print(f"  PASS: {name}")
        pass_count += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        fail_count += 1


def test_library_identity():
    print("\n=== Library Identity ===")
    check("library exists", LIB_PATH.exists(), f"not found at {LIB_PATH}")
    if not LIB_PATH.exists():
        return False

    data = LIB_PATH.read_bytes()
    check("file size", len(data) == EXPECTED_SIZE,
          f"got {len(data)}, expected {EXPECTED_SIZE}")
    check("ELF magic", data[:4] == b"\x7fELF", "not an ELF file")
    check("32-bit ARM", data[4] == 1 and data[18] == 0x28,
          "not 32-bit ARM ELF")
    check("BuildID present", check_buildid(data),
          f"expected BuildID {EXPECTED_BUILDID}")
    return True


def test_original_state():
    print("\n=== Original State Verification ===")
    data = LIB_PATH.read_bytes()
    results = check_state(data)

    for state, site in results:
        off = site["offset"]
        fn = site["function"]
        check(f"0x{off:08X} ({fn}) is original",
              state == "original",
              f"state={state}")

    check("all sites in original state",
          all(s == "original" for s, _ in results))


def test_context_bytes():
    print("\n=== Context Byte Validation ===")
    data = LIB_PATH.read_bytes()

    for site in PATCH_SITES:
        off = site["offset"]
        actual = data[off + 2:off + 4]
        check(f"0x{off:08X} context (adds r0, #1)",
              actual == CONTEXT_AFTER,
              f"got {actual.hex()}, expected {CONTEXT_AFTER.hex()}")


def test_function_boundaries():
    """Verify patch sites are within expected function boundaries."""
    print("\n=== Function Boundary Checks ===")

    data = LIB_PATH.read_bytes()

    # Levels_IsLocked: push {r7, lr} at 0x133024
    check("Levels_IsLocked prologue",
          data[0x133024:0x133026] == bytes([0x80, 0xB5]),
          "expected push {r7, lr}")
    # pop {r7, pc} at 0x133050
    check("Levels_IsLocked epilogue",
          data[0x133050:0x133052] == bytes([0x80, 0xBD]),
          "expected pop {r7, pc}")
    # Patch at 0x133046 is within [0x133024, 0x133052)
    check("patch site 1 within Levels_IsLocked",
          0x133024 <= 0x133046 < 0x133052)

    # UiControlButtonLevelRow constructor: starts at 0x13ceb4
    check("UiControlButtonLevelRow::ctor prologue",
          data[0x13CEB4:0x13CEB6] == bytes([0xF0, 0xB5]),
          "expected push {r4-r7, lr}")
    check("patch site 2 within UiControlButtonLevelRow::ctor",
          0x13CEB4 <= 0x13D846 < 0x13CEB4 + 3776)
    check("patch site 3 within UiControlButtonLevelRow::ctor",
          0x13CEB4 <= 0x13D944 < 0x13CEB4 + 3776)

    # UpdateLevelPanelPopulation: starts at 0x14bef4
    check("UpdateLevelPanelPopulation prologue",
          data[0x14BEF4:0x14BEF6] == bytes([0xF0, 0xB5]),
          "expected push {r4-r7, lr}")
    check("patch site 4 within UpdateLevelPanelPopulation",
          0x14BEF4 <= 0x14BFBC < 0x14BEF4 + 940)
    check("patch site 5 within UpdateLevelPanelPopulation",
          0x14BEF4 <= 0x14C10A < 0x14BEF4 + 940)
    check("patch site 6 within UpdateLevelPanelPopulation",
          0x14BEF4 <= 0x14C11C < 0x14BEF4 + 940)


def test_no_other_lock_patterns():
    """Verify we found ALL lock-check patterns in the binary."""
    print("\n=== Exhaustive Pattern Search ===")
    data = LIB_PATH.read_bytes()

    pattern = ORIGINAL_BYTES + CONTEXT_AFTER  # c0 6b 01 30
    found = []
    pos = 0
    while True:
        idx = data.find(pattern, pos)
        if idx == -1:
            break
        found.append(idx)
        pos = idx + 1

    known = {site["offset"] for site in PATCH_SITES}
    extra = set(found) - known
    missing = known - set(found)

    check(f"found exactly {len(PATCH_SITES)} lock-check patterns",
          len(found) == len(PATCH_SITES),
          f"found {len(found)}: {[f'0x{x:08X}' for x in found]}")
    check("no unexpected pattern locations",
          len(extra) == 0,
          f"extra: {[f'0x{x:08X}' for x in extra]}")
    check("all known sites found",
          len(missing) == 0,
          f"missing: {[f'0x{x:08X}' for x in missing]}")


def test_medal_display_unaffected():
    """Verify the medal icon lookup at 0x13d914 does NOT match the patch pattern."""
    print("\n=== Medal Display Isolation ===")
    data = LIB_PATH.read_bytes()

    # Medal icon: ldr r0, [r0, #0x3c] at 0x13d914 followed by blx (not adds #1)
    medal_bytes = data[0x13D914:0x13D916]
    check("medal icon reads same field (c0 6b)",
          medal_bytes == ORIGINAL_BYTES,
          f"got {medal_bytes.hex()}")

    after_medal = data[0x13D916:0x13D918]
    check("medal icon NOT followed by adds r0, #1",
          after_medal != CONTEXT_AFTER,
          "medal site WOULD be caught by patch pattern!")

    # GetTextureFromRank is called after the medal load
    check("medal icon followed by blx (GetTextureFromRank call)",
          after_medal[1] & 0xF0 == 0xE0 or after_medal[1] & 0xF8 == 0xF0,
          f"got {after_medal.hex()}")


def test_purchase_check_separate():
    """Verify Levels_IsPurchased doesn't contain the lock pattern."""
    print("\n=== Purchase Check Isolation ===")
    data = LIB_PATH.read_bytes()

    # Levels_IsPurchased at 0x133015, size 16
    purchase_fn = data[0x133014:0x133024]
    check("Levels_IsPurchased has no lock pattern",
          ORIGINAL_BYTES + CONTEXT_AFTER not in purchase_fn)

    # Store_IsItemPurchased at 0x107269, size 32
    store_fn = data[0x107268:0x107288]
    check("Store_IsItemPurchased has no lock pattern",
          ORIGINAL_BYTES + CONTEXT_AFTER not in store_fn)


def test_game_loadlevel_no_lock():
    """Verify Game::LoadLevel doesn't check progression locks."""
    print("\n=== Game::LoadLevel Isolation ===")
    data = LIB_PATH.read_bytes()

    # Game::LoadLevel at 0x11f251, size 668
    loadlevel = data[0x11F250:0x11F250 + 668]
    check("Game::LoadLevel has no lock pattern",
          ORIGINAL_BYTES + CONTEXT_AFTER not in loadlevel)


def test_apply_revert_cycle():
    """Test apply and revert on a temporary copy."""
    print("\n=== Apply/Revert Cycle ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / "libtrueaxis.so"
        shutil.copy2(LIB_PATH, tmp)
        original_sha = hashlib.sha256(tmp.read_bytes()).hexdigest()

        # Apply
        data = tmp.read_bytes()
        data = apply_patch(tmp, data)
        results = check_state(data)
        check("all sites patched after apply",
              all(s == "patched" for s, _ in results))

        patched_sha = hashlib.sha256(data).hexdigest()
        check("SHA changed after apply", patched_sha != original_sha)

        # Verify exactly 12 bytes changed
        orig_data = LIB_PATH.read_bytes()
        diff_count = sum(1 for a, b in zip(orig_data, data) if a != b)
        check("exactly 12 bytes changed", diff_count == 12,
              f"changed {diff_count} bytes")

        # Revert
        data = revert_patch(tmp, data)
        results = check_state(data)
        check("all sites original after revert",
              all(s == "original" for s, _ in results))

        reverted_sha = hashlib.sha256(data).hexdigest()
        check("SHA matches original after revert",
              reverted_sha == original_sha)


def test_idempotent():
    """Test that double-apply and double-revert are safe."""
    print("\n=== Idempotency ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / "libtrueaxis.so"
        shutil.copy2(LIB_PATH, tmp)

        data = tmp.read_bytes()
        data = apply_patch(tmp, data)
        sha1 = hashlib.sha256(tmp.read_bytes()).hexdigest()

        data = tmp.read_bytes()
        data = apply_patch(tmp, data)
        sha2 = hashlib.sha256(tmp.read_bytes()).hexdigest()
        check("double apply is idempotent", sha1 == sha2)

        data = tmp.read_bytes()
        data = revert_patch(tmp, data)
        data = tmp.read_bytes()
        data = revert_patch(tmp, data)
        sha3 = hashlib.sha256(tmp.read_bytes()).hexdigest()
        orig_sha = hashlib.sha256(LIB_PATH.read_bytes()).hexdigest()
        check("double revert restores original", sha3 == orig_sha)


def test_progression_logic():
    """Verify understanding of Levels_DoProgression via disassembly constants."""
    print("\n=== Progression Logic Constants ===")
    data = LIB_PATH.read_bytes()

    # Levels_DoProgression loops 0x78 (120) times
    # Check for the cmp r4, #0x78 instruction (2c78 → LE bytes 78 2c)
    fn_data = data[0x132D38:0x132D38 + 176]
    check("Levels_DoProgression loops over 120 levels",
          bytes([0x78, 0x2C]) in fn_data,
          "cmp r4, #0x78 not found")

    # Levels_Exists checks against 0x78 (2878 → LE bytes 78 28)
    exists_data = data[0x132DE8:0x132DE8 + 48]
    check("Levels_Exists boundary at 120",
          bytes([0x78, 0x28]) in exists_data,
          "cmp r0, #0x78 not found")

    # The stats difficulty stride is 72 bytes (9*8)
    # Check add.w r1, r8, r8, lsl #3 in DoProgression (r8*9)
    check("difficulty stride uses *9 multiplier",
          bytes([0x08, 0xEB, 0xC8, 0x01]) in fn_data,
          "add.w r1, r8, r8, lsl #3 not found")

    # Check the 0x3c offset read
    check("stats reads at offset 0x3c",
          bytes([0xC0, 0x6B]) in fn_data,
          "ldr r0, [r0, #0x3c] not found")


def main():
    print("JCS2 Progression Unlock — Static Patch Tests")
    print("=" * 60)

    if not test_library_identity():
        print("\nCannot continue without the library file.")
        sys.exit(1)

    test_original_state()
    test_context_bytes()
    test_function_boundaries()
    test_no_other_lock_patterns()
    test_medal_display_unaffected()
    test_purchase_check_separate()
    test_game_loadlevel_no_lock()
    test_apply_revert_cycle()
    test_idempotent()
    test_progression_logic()

    print("\n" + "=" * 60)
    print(f"Results: {pass_count} passed, {fail_count} failed")

    if fail_count > 0:
        sys.exit(1)
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()

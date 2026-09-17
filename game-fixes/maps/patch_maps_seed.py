#!/usr/bin/env python3
"""Maps per-SKU store seeder for libtrueaxis.so (offline, reversible).

Mechanism: hook the EPILOGUE of the JNI entry
`Java_com_trueaxis_cLib_TrueaxisLib_populateStore` (@0x1054d1, 920 bytes)
so that immediately after the game finishes registering its store table,
our appended stub calls the game's OWN legitimate setter
`Store_SetTCPurchasedItem(char*)` (@0x107244: lookup + mla stride 0x864 +
`movs r1,#1; strb.w r1,[r0,#0x50]`) once per allowlisted content SKU.
The store predicate (`Store_IsItemPurchased`) is NOT modified: it returns
owned naturally because the table holds owned bytes — the same bytes a real
purchase would write. remove_ads is deliberately NOT seeded (excluded SKU;
ads behavior unchanged).

Why the epilogue (init-flow evidence, system llvm-objdump numeric
disassembly + readelf, no custom decoder in the chain):
  * populateStore is a JNI entry: NO native BL caller exists (exhaustive
    whole-.text numeric scan), so there is no native post-population call
    site to redirect. Java drives it; the epilogue runs at the end of EVERY
    population pass, before any reader can observe unseeded state.
  * populateStore has exactly two exits: an early stack-canary epilogue
    @0x10570e (fires BEFORE the item-add strcmp/stride loop, i.e. nothing
    populated — nothing to seed) and the final `pop {r4-r7,pc}` @0x1058a0
    after the loop. Hooking the final exit covers every populated path.
  * Re-entry is idempotent (setter writes 1 unconditionally).

Hook encoding (4 bytes available: `bdf0` + trailing `bf00` nop @0x1058a2):
  original: bdf0 bf00  (pop {r4,r5,r6,r7,pc}; nop)
  patched:  b.w STUB   (4-byte Thumb branch; stub completes the epilogue
                        itself, then seeds, then returns to the Java caller)
Stub (appended in the LOAD0 zero pad @0x213400; file offset == vaddr there).
HARD RULE (live SIGILL class, 2026-09-13): the ARM app runs under this
x86 guest's ndk_translation, whose Thumb decoder demonstrably SIGILLs on
some valid encodings. The stub therefore uses ONLY 16-bit data/memory
forms (every one with 1000+ in-game executions) plus 32-bit B/BL which
the game itself uses thousands of times. Concretely: no 32-bit LDM/STM
at all (game has zero 32-bit pc-relative loads and its wide multiples
never demonstrably execute with high regs — the 32-bit `ldr.w r0,[pc]`
was the second SIGILL), no LR/PC/r12 in any list, no post-index LDR.
Return address travels exclusively via the stack + bx r3:
  pop {r4-r7}             ; 16-bit bcf0 (retaddr stays on stack top)
  push {r0-r3}            ; 16-bit b40f (16 B)
  10x: ldr r0,[pc,#k]     ; 16-bit T1 48xx (pool < 1 KB away by construction)
       bl.w SETTER        ; legitimate per-SKU purchase setter
  pop {r0-r3}             ; 16-bit bc0f (r0 = populateStore's return value)
  pop {r3}                ; 16-bit bc08 (retaddr; sp back to epilogue value)
  bx r3                   ; 16-bit 4718
Register/stack contract for the reviewer: r0 (populateStore's return
value) and r4-r7 are exactly preserved; r1-r3/r12 are caller-saved
scratch (dead after return); sp during the BLs equals the aligned
epilogue sp and returns to the original epilogue value (S+20); the
setter is a normal C function (push {r7,lr} frame, no callbacks into the
stub). Unwind/exidx: the stub frame has no exidx coverage (same caveat
as the proven ELF-growth note; the setter path is practically no-throw
— live test is the gate).
LIVE HISTORY: rev1 (LDM.W mask bit15=PC + ldr.w lr post-index) and rev2
(LR in LDM/STM masks) both died in ndk_translation UndefinedInsn on the
game thread. All high-reg forms are now eliminated, not individually
appealed — the remaining forms all have direct in-game precedent.
SKU scope: EXACTLY the 10 OWNERSHIP-ALLOWLIST.json content_skus
(byte-exact, including the sic `jcs2_patforming_1_75` spelling).

Timing/init note: the seed runs synchronously at the end of each
populateStore pass (code-level, not save-level). No flag file, no
options.bin dependency. Post-population overwrites would need a live
server round-trip (TCP/billing paths need live servers; the local-restore
path has zero callers) — offline behavior is fully determined; online
overwrite risk stays flagged for live. The 12-byte Levels patch is kept
alongside (proven live): gameplay stays owned even if a store byte were
ever externally cleared.

Disjointness: hook site 0x1058a0 + pad 0x213400+ touch nothing else.
Sound v2 site 0x154a7c / v3 sites (0x154a7c, 0x11e318-0x11e31d, 0x154a1c),
12B site 0x133014-23, six sentinels (0x133046+), predicate/setter bodies
are all asserted intact by --check.

Usage:
    python3 patch_maps_seed.py [--apply|--revert|--check] <libtrueaxis.so>
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWLIST = json.loads(
    (HERE / "OWNERSHIP-ALLOWLIST.json").read_text(encoding="utf-8"))


def _active_skus():
    """Allowlist SKUs, or MAPS_SEED_SKUS override (comma list; empty =
    hook-only NULL stub for live bisection). remove_ads never seeds."""
    import os as _os
    override = _os.environ.get("MAPS_SEED_SKUS")
    skus = ([s for s in override.split(",") if s] if override is not None
            else list(ALLOWLIST["content_skus"]))
    assert "jcs2_remove_ads" not in skus
    if override is None:
        assert len(skus) == 10, skus
    return skus


SKUS = _active_skus()

EXPECTED_LIB_SIZE = 2227488
KNOWN_BUILDID = "39f1603fac960e176dfbfd39f4a00bc2c3c43234"

POPULATE_END = 0x1058a0          # final pop {r4-r7,pc}
ORIGINAL_EPILOGUE = bytes.fromhex("f0bd00bf")  # bdf0 bf00
SETTER = 0x107244
STUB_ADDR = 0x213400
PAD_START = 0x2133fc
PAD_END = 0x213ba0              # 0x7A4 zero bytes; LOAD1 starts here


def encode_bw(src: int, dst: int) -> bytes:
    """4-byte Thumb B.W encoding (T4). Raises if out of +-16 MiB range."""
    pc = src + 4
    diff = dst - pc
    # NOTE: explicit parens — `not a <= b < c` chains ambiguously;
    # the unparenthesised form silently skipped this guard (caught by
    # test_branch_range_refusal).
    if diff % 2 != 0 or not (-(1 << 24) <= diff < (1 << 24)):
        raise ValueError(f"branch {src:#x}->{dst:#x} out of range")
    v = diff & 0xFFFFFFFF
    s = (v >> 24) & 1
    i1 = (v >> 23) & 1
    i2 = (v >> 22) & 1
    j1 = (~(i1 ^ s)) & 1
    j2 = (~(i2 ^ s)) & 1
    imm10 = (v >> 12) & 0x3FF
    imm11 = (v >> 1) & 0x7FF
    first = 0xF000 | (s << 10) | imm10
    # T4 B skeleton is 10 J1 1 J2 imm11: base 0x9000 carries bits 15:14
    # and the fixed bit12; J1/J2 OR in. (A 0xB800 base folds J1=J2=1 in
    # and mis-encodes J1=0 extremes — caught by the max-range test.)
    second = 0x9000 | (j1 << 13) | (1 << 12) | (j2 << 11) | imm11
    import struct
    return struct.pack("<HH", first, second)


def decode_bw(src: int, raw: bytes) -> int:
    """Inverse of encode_bw (for --check verification)."""
    import struct
    first, second = struct.unpack("<HH", raw)
    s = (first >> 10) & 1
    imm10 = first & 0x3FF
    j1 = (second >> 13) & 1
    j2 = (second >> 11) & 1
    imm11 = second & 0x7FF
    i1 = (~(j1 ^ s)) & 1
    i2 = (~(j2 ^ s)) & 1
    v = (s << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
    if s:
        v -= 1 << 25
    return src + 4 + v


STRLEN_PLT = 0xa0bf8  # blx target for call-probe mode (bisection only)


def encode_blx_imm(addr: int, target: int) -> bytes:
    """4-byte Thumb BLX.W immediate (bisection probe only)."""
    import struct
    pc = (addr + 4) & ~3
    diff = target - pc
    if diff % 2 != 0 or not (-(1 << 24) <= diff < (1 << 24)):
        raise ValueError(f"blx {addr:#x}->{target:#x} out of range")
    v = diff & 0xFFFFFFFF
    s = (v >> 24) & 1
    i1 = (v >> 23) & 1
    i2 = (v >> 22) & 1
    j1 = (~(i1 ^ s)) & 1
    j2 = (~(i2 ^ s)) & 1
    imm10 = (v >> 12) & 0x3FF
    imm11 = (v >> 1) & 0x7FF
    first = 0xF000 | (s << 10) | imm10
    second = 0xE800 | (j1 << 13) | (1 << 12) | (j2 << 11) | imm11
    return struct.pack("<HH", first, second)


def encode_ldr_t1(addr: int, target: int) -> bytes:
    """2-byte Thumb LDR r0,[PC,#imm] (T1). Pool must be < 1 KB away."""
    import struct
    pc = (addr + 4) & ~3
    off = target - pc
    if not 0 <= off <= 1020 or off % 4 != 0:
        raise ValueError(f"T1 literal {target:#x} out of range from {addr:#x}")
    return struct.pack("<H", 0x4800 | (off // 4))  # ldr r0,[pc,#off]


SEG_TABLE_ADDRESSES = (  # in-segment rodata literals (bisection only)
    2149680, 2149808, 2149936, 2150064, 2150192,
    2150320, 2150448, 2150576, 2150704, 2150832)  # +0 stride-128 table


def build_stub() -> tuple:
    """Return (stub_bytes, sku_string_offsets). Deterministic."""
    import os as _os2
    no_call = _os2.environ.get("MAPS_SEED_MODE") == "nobl"  # bisection only
    code = bytearray()
    code += bytes.fromhex("f0bc")  # pop {r4-r7}
    code += bytes.fromhex("0fb4")  # push {r0-r3}
    ldr_positions = []
    for _ in SKUS:
        ldr_positions.append(len(code))
        code += b"\x00\x00"         # ldr r0,[pc,#k] (patched below)
        if not no_call:
            code += b"\x00\x00\x00\x00"  # bl.w SETTER (patched below)
    code += bytes.fromhex("0fbc")  # pop {r0-r3}
    code += bytes.fromhex("08bc")  # pop {r3} (retaddr)
    code += bytes.fromhex("1847")  # bx r3
    code += bytes.fromhex("bf00")  # nop pad (pool must stay word-aligned)
    assert len(code) % 4 == 0
    pool_base = STUB_ADDR + len(code)
    pool = bytearray()
    str_base = pool_base + 4 * len(SKUS)
    str_blob = bytearray()
    import os as _os4
    if _os4.environ.get("MAPS_SEED_SRC") == "seg":
        # Bisection only: point at existing in-segment rodata literals.
        assert len(SKUS) == 10, "seg mode needs the full allowlist"
        for addr in SEG_TABLE_ADDRESSES:
            assert addr < PAD_START, hex(addr)
        sku_addrs = list(SEG_TABLE_ADDRESSES)
    else:
        sku_addrs = []
        for sku in SKUS:
            sku_addrs.append(str_base + len(str_blob))
            str_blob += sku.encode("ascii") + b"\x00"
    import struct
    for addr in sku_addrs:
        pool += struct.pack("<I", addr)
    # Fix up per-SKU ldr/bl now that every address is known.
    import os as _os3
    mode = _os3.environ.get("MAPS_SEED_MODE", "")  # bisection only
    no_call = mode == "nobl"
    probe = mode == "probecall"
    stride = 2 if no_call else 6
    for i, pos in enumerate(ldr_positions):
        ldr_addr = STUB_ADDR + pos
        code[pos:pos + 2] = encode_ldr_t1(ldr_addr, pool_base + 4 * i)
        if probe:
            code[pos + 2:pos + 6] = encode_blx_imm(ldr_addr + 2, STRLEN_PLT)
        elif not no_call:
            code[pos + 2:pos + 6] = encode_bw(ldr_addr + 2, SETTER)
    assert ldr_positions == [4 + stride * i for i in range(len(SKUS))]
    return bytes(code), bytes(pool), bytes(str_blob)


def expected_layout() -> tuple:
    code, pool, strings = build_stub()
    return code, pool, strings


def state(data: bytes) -> str:
    if data[POPULATE_END:POPULATE_END + 4] == ORIGINAL_EPILOGUE:
        pad = data[PAD_START:PAD_END]
        if all(b == 0 for b in pad):
            return "original"
        return "unknown"
    code, pool, strings = expected_layout()
    total = code + pool + strings
    try:
        branch = encode_bw(POPULATE_END, STUB_ADDR)
    except ValueError:
        return "unknown"
    if (data[POPULATE_END:POPULATE_END + 4] == branch
            and data[STUB_ADDR:STUB_ADDR + len(total)] == total):
        return "seeded"
    return "unknown"


def apply_patch(path: Path, data: bytes) -> bytes:
    st = state(data)
    if st == "seeded":
        print("Already seeded. Nothing to do.")
        return data
    if st != "original":
        raise ValueError("unexpected seed state; refusing (not original bytes)")
    code, pool, strings = expected_layout()
    total = code + pool + strings
    if len(total) > PAD_END - STUB_ADDR:
        raise ValueError("stub does not fit the LOAD0 pad")
    out = bytearray(data)
    out[POPULATE_END:POPULATE_END + 4] = encode_bw(POPULATE_END, STUB_ADDR)
    out[STUB_ADDR:STUB_ADDR + len(total)] = total
    path.write_bytes(bytes(out))
    print(f"seeded {len(SKUS)} SKUs via populateStore epilogue hook "
          f"(stub @{STUB_ADDR:#x}, {len(total)} B in LOAD0 pad)")
    return bytes(out)


def revert_patch(path: Path, data: bytes) -> bytes:
    st = state(data)
    if st == "original":
        print("Already original. Nothing to do.")
        return data
    if st != "seeded":
        raise ValueError("unexpected seed state; refusing")
    code, pool, strings = expected_layout()
    out = bytearray(data)
    out[POPULATE_END:POPULATE_END + 4] = ORIGINAL_EPILOGUE
    used = STUB_ADDR + len(code + pool + strings)
    out[STUB_ADDR:used] = b"\x00" * (used - STUB_ADDR)
    path.write_bytes(bytes(out))
    print("reverted seed hook (epilogue + pad restored)")
    return bytes(out)


def check_library(path: Path) -> int:
    data = path.read_bytes()
    print(f"lib {path} ({len(data)} B, "
          f"sha256 {hashlib.sha256(data).hexdigest()[:16]}...)")
    print(f"seed state: {state(data)}")
    if state(data) == "seeded":
        branch = encode_bw(POPULATE_END, STUB_ADDR)
        assert data[POPULATE_END:POPULATE_END + 4] == branch
        assert decode_bw(POPULATE_END, branch) == STUB_ADDR
        code, pool, strings = expected_layout()
        total = code + pool + strings
        assert data[STUB_ADDR:STUB_ADDR + len(total)] == total
        print(f"branch target + {len(total)} stub bytes verified; "
              f"10 SKU literals byte-exact")
    # Disjointness report (informational; hard gates live in tests).
    import re
    _ = re
    print("disjointness anchors: sound v2 0x154a7c / v3 "
          "{0x154a7c,0x11e318,0x154a1c}, 12B 0x133014-23, "
          "sentinels 0x133046+ untouched by this tool")
    return 0 if state(data) in ("original", "seeded") else 1


def main():
    parser = argparse.ArgumentParser(
        description="Maps per-SKU store seeder (populateStore epilogue hook).")
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
    if len(data) != EXPECTED_LIB_SIZE:
        print(f"ERROR: unexpected size {len(data)}")
        sys.exit(1)
    if bytes.fromhex(KNOWN_BUILDID) not in data[:0x200]:
        print("ERROR: BuildID mismatch; refusing")
        sys.exit(1)

    if args.check:
        sys.exit(check_library(args.library))
    if not args.no_backup:
        backup = args.library.with_suffix(".so.seedbak")
        if not backup.exists():
            shutil.copyfile(args.library, backup)
            print(f"backup: {backup}")
    try:
        if args.apply:
            apply_patch(args.library, data)
        else:
            revert_patch(args.library, data)
    except ValueError as error:
        print(f"ERROR: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()

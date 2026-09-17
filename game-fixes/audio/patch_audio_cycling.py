#!/usr/bin/env python3
"""Hash-gated guarded-cycling patch for the SOUND toggle (offline only).

v2 (post independent review sound-cycling-review-v1.md; candidate ac225508
REJECTED, preserved as evidence in candidate-cycling/REJECTED.json):

- B1: shim internal calls are BLX.W-imm to ARM PLT (exactly the original
  pattern: toggle/update call PLT via blx; local encoder reproduces
  original bytes f79eef20/f797eba4/4ff7aeea byte-exact). Hook stays bl.w
  (Thumb->Thumb). Second-half top-nibble 0xE asserted (0xF = BL.W lie).
- B2: explicit LOAD0 FileSiz/MemSiz extend 0x2133fc -> 0x213700 (file size
  unchanged; stays clear of LOAD1 file 0x213ba0). Verified via readelf.
- B3: zero absolute VAs. Every shim address = runtime-pc + link-time delta
  replicated from ORIGINAL computations (send1i base, cb1 base, toggle
  string) or live r5 (flag). One pool word PER USE (deltas differ per
  ldr position). ASLR-proof by construction; simulated nonzero-bias tests.
- B4: no new concurrency shape (same routines/thread/order as pause/resume
  + toggle). Synchrony beyond that is live-gated, stated candidly.
- B5: FAIL path re-verifies with one unrolled LOW-rebuild retry, then
  restore+return (documented residual only if double-fail).
- B6: FAIL path rewrites the label to LOW via the game's own SetText
  (truthful at tap time); LoadOptions keeps strb + calls the shim, so a
  saved-HIGH boot attempts HIGH truthfully (engine proven live there by
  the 3/3 identical-signature boot crashes) with LOW fallback.

Shim machine rules: 16-bit Thumb with in-game precedent + 32-bit B/BL/BLX
(BLX.W-imm to PLT allowlist only). No 32-bit LDM/STM/ldr.w, no LR/PC/r12
in any list, no post-index. Every emitted halfword is oracle-anchored
(llvm-mc assembler + objdump roundtrips, game-byte precedents); the
llvm-mc DISASSEMBLY display is NOT trusted for 0xB1xx (overlap quirk).
Register contract: r4-r6,r8,lr preserved (caller uses them after the
call); r0(new flag)/r5(&flag) consumed.

Layout (SHIM=0x213600; seed zone 0x213400-0x213600 reserved zeros):
  preserve | blx stop | blx start | verify(player,Q1,Q2)+verdict
  -> SUCCESS: restore + return (flag/label/engine HIGH)
  -> FAIL: blx stop, flag=LOW, blx start(LOW), re-verify (+1 retry),
     SetText(LOW) label fixup, restore + return (flag+engine LOW)

Usage:
  python3 patch_audio_cycling.py --check <libtrueaxis.so>
  python3 patch_audio_cycling.py --apply --lib-in <orig> --lib-out <new>
  python3 patch_audio_cycling.py --stage <new-empty-dir>   # APK->lib+provenance
"""
import argparse
import datetime
import hashlib
import json
import struct
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "maps"))
from patch_maps_seed import encode_bw, decode_bw  # noqa: E402 (import only)
# NOTE: patch_maps_seed.encode_blx_imm is NOT used: it forces bit12=1 in
# the second half (0xE800|(1<<12)|... = 0xFxxx), emitting BL.W (no state
# switch). Silicon-executed originals (toggle/update/pause paths) all show
# top-nibble 0xE (e.g. f79e ef20 -> stop PLT). Local fixed encoder below
# reproduces those bytes exactly (tested). Maps file NOT edited (owned
# lane only); reported to root as a maps-lane finding.

SPEC_PATH = HERE / "audio_cycling_patch.json"

SHIM_ADDR = 0x213600
SEED_ZONE_END = 0x213600      # seed stub lives below; assert zeros
SHIM_ZONE_END = 0x213700      # PHDR extend target; assert shim fits
PAD_END = 0x213BA0
LOAD0_FILESZ_NEW = 0x213700

PLT_STOP = 0xA2DA0
PLT_START = 0xA2DAC
PLT_WSTR = 0xA0D0C
PLT_SETTEXT = 0xA5950
PLT_D1 = 0xA0CF4
PLT_ALLOW = {PLT_STOP, PLT_START, PLT_WSTR, PLT_SETTEXT, PLT_D1}

# Replication link targets from ORIGINAL file bytes.
# SEND add@0x103be0 is %4==0 (rule-independent). CB1/CB2 add sites are
# %4==2 and use the display-proven unaligned rule (same T2 form as the
# toggle strings). CB1_T == CB2_T under EITHER rule (site distance 0x10
# == cell distance -0x10): ONE queue base shared by both callbacks
# (double-buffer = 2 callbacks, 1 queue). No adjacency guess.
# LOWSTR is display-proven unaligned (game renders full rows).
SEND_SITE_PC = 0x103BE4
SEND_POOL_OFF = 0x103C08
CB1_SITE_PC = 0x103C42
CB1_POOL_OFF = 0x103C48
CB2_SITE_PC = 0x103C52
CB2_POOL_OFF = 0x103C58
LOWSTR_SITE_PC = 0x154A42 + 4
LOWSTR_POOL_OFF = 0x154AAC

HOOK_SITE = 0x154A7C
LOADOPT_SITE = 0x11E318
LOADOPT_BL_OFF = 0x11E31A
ENTRY_OFF = 0x154A1C


class PatchError(Exception):
    pass


def encode_blx_imm(addr: int, target: int) -> bytes:
    """Thumb BLX.W immediate (Thumb->ARM PLT, the original pattern).

    Correct form validated byte-exact against silicon-executed originals:
    encode_blx_imm(0x103f5c, 0xa2da0) == f79eef20 (stop),
    encode_blx_imm(0x10b662, 0xa2dac) == f797eba4 (start),
    encode_blx_imm(0x154a7c, 0xa3fdc) == 4ff7aeea (update).
    Second-half top nibble is 0xE (true BLX); 0xF... would be BL.W
    (no state switch -> SIGILL class at ARM PLT).
    """
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
    second = 0xE800 | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack("<HH", first, second)


def h16(op: int) -> bytes:
    assert 0 <= op <= 0xFFFF
    return struct.pack("<H", op)


def read_orig_word(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def replication_targets(orig: bytes):
    send_base = SEND_SITE_PC + read_orig_word(orig, SEND_POOL_OFF)
    cb1_base = CB1_SITE_PC + read_orig_word(orig, CB1_POOL_OFF)
    cb2_base = CB2_SITE_PC + read_orig_word(orig, CB2_POOL_OFF)
    # Structural anchor (rule-free): both callbacks resolve the SAME base.
    assert cb2_base == cb1_base, (hex(cb1_base), hex(cb2_base))
    lowstr = LOWSTR_SITE_PC + read_orig_word(orig, LOWSTR_POOL_OFF)
    assert orig[lowstr:lowstr + 18] == b"SOUND: LOW LATENCY", hex(lowstr)
    return {
        "struct_base": send_base,    # sound state block (flag = [base])
        "player_loc": send_base + 8,  # send-path iface word (crash root)
        "play_loc": send_base + 0x28,  # SetPlayState iface (stop/start obj)
        "q_loc": cb1_base + 8,        # the ONE queue iface word
        "lowstr": lowstr,
    }


def enc_push() -> bytes:
    return h16(0xB5F0)


def enc_pop_ret() -> bytes:
    return h16(0xBDF0)


def enc_mov_rr(dst: int, src: int) -> bytes:
    # Oracle: mov r3,r8=0x4643, mov r8,r7=0x46B8, mov r3,r0=0x4603.
    assert 0 <= dst <= 8 and 0 <= src <= 8
    if dst > 7 or src > 7:
        d = 1 if dst > 7 else 0
        return h16(0x4600 | (d << 7) | ((src & 0xF) << 3) | (dst & 0x7))
    return h16(0x4600 | (src << 3) | dst)


def enc_ldr_imm(rt: int, rn: int, off: int) -> bytes:
    # Oracle: ldr r2,[r2]=0x6812, ldr r2,[r2,#8]=0x6892.
    assert 0 <= rt <= 7 and 0 <= rn <= 7 and off % 4 == 0
    assert 0 <= off // 4 < 32
    return h16(0x6800 | ((off // 4) << 6) | (rn << 3) | rt)


def enc_mov_imm(reg: int, val: int) -> bytes:
    assert 0 <= reg <= 7 and 0 <= val < 256
    return h16(0x2000 | (reg << 8) | val)


def enc_strb_imm(rt: int, rn: int) -> bytes:
    # Oracle: strb r1,[r5]=0x7029.
    assert 0 <= rt <= 7 and 0 <= rn <= 7
    return h16(0x7000 | (rn << 3) | rt)


def enc_add_sp_imm(reg: int, off: int) -> bytes:
    # Oracle: add r0,sp,#0=0xA800 (game A804/A920 family).
    assert 0 <= reg <= 7 and off % 4 == 0 and off // 4 < 256
    return h16(0xA800 | (reg << 8) | (off // 4))


def enc_sub_sp(n: int) -> bytes:
    # Oracle: sub sp,#24=0xB186; game precedent 0xB084.
    assert n % 4 == 0 and n // 4 < 128
    return h16(0xB180 | (n // 4))


def enc_add_sp(n: int) -> bytes:
    # mc-display + pattern: add sp,#0x18=0xB006.
    assert n % 4 == 0 and n // 4 < 128
    return h16(0xB000 | (n // 4))


def enc_adds_imm(reg: int, val: int) -> bytes:
    # Oracle: adds r2,#6=0x3206 (T2: 00110 Rd imm8).
    assert 0 <= reg <= 7 and 0 <= val < 256
    return h16(0x3200 | (reg << 8) | val)


def enc_adds_imm(reg: int, val: int) -> bytes:
    # Oracle: adds r2,#6=0x3206 (T2: 00110 Rd imm8).
    assert 0 <= reg <= 7 and 0 <= val < 256
    return h16(0x3200 | (reg << 8) | val)


def enc_subs_imm(reg: int, val: int) -> bytes:
    # Oracle: subs r2,#2=0x3A02.
    assert 0 <= reg <= 7 and 0 <= val < 256
    return h16(0x3800 | (reg << 8) | val)


def enc_cmp_imm(reg: int, val: int) -> bytes:
    # Oracle: cmp r3,#0=0x2B00.
    assert 0 <= reg <= 7 and 0 <= val < 256
    return h16(0x2800 | (reg << 8) | val)


def build_shim(orig: bytes):
    """Return (code, pools, blx_targets, labels, uses).

    pools: one word PER ldr-literal use: (target_link - ldr_pc_link).
    uses: list of (code_offset, target_name) for ASLR simulation.
    """
    tgts = replication_targets(orig)
    # Segment membership (link-time; preserved under ASLR since segments
    # move together — every shim read lands in a mapped segment, so a
    # wrong word can only mistrigger fallback, never fault the shim).
    assert 0x220630 <= tgts["player_loc"] < 0x478268, hex(tgts["player_loc"])
    assert 0x220630 <= tgts["play_loc"] < 0x478268, hex(tgts["play_loc"])
    assert 0x220000 <= tgts["q_loc"] < 0x220624, hex(tgts["q_loc"])
    assert 0x1EB2F0 <= tgts["lowstr"] < 0x2133FC, hex(tgts["lowstr"])
    code = bytearray()
    blx_targets = {}
    lits = []      # (code_offset, reg, target_name)
    branches = []  # (code_offset, kind, reg, label)

    def emit(b: bytes) -> int:
        off = len(code)
        code.extend(b)
        return off

    def emit_blx(plt: int):
        assert plt in PLT_ALLOW
        off = emit(b"\x00\x00\x00\x00")
        blx_targets[off] = plt

    def emit_ldr_rep(reg: int, target: str):
        # Align ldr to %4==0 so its own pc is rule-independent; the
        # following add-pc uses the display-proven unaligned rule.
        while len(code) % 4:
            code.extend(h16(0xBF00))  # nop (game-precedent padding)
        off = emit(b"\x00\x00")
        lits.append((off, reg, target))

    def emit_cbz(reg: int, label: str):
        off = emit(b"\x00\x00")
        branches.append((off, "cbz", reg, label))

    def emit_verify(to_label: str):
        # Per check: ldr pool-delta; add-pc (= runtime target); deref; cbz.
        # Pools replicate to the exact WORD locations (player = send+8,
        # play = send+0x28, queue = cb+8). ONE queue: CB1_T == CB2_T proven.
        for kind in ("player_loc", "play_loc", "q_loc"):
            emit_ldr_rep(2, kind)
            emit(enc_add_r2_pc())
            emit(enc_ldr_imm(2, 2, 0))
            emit_cbz(2, to_label)

    def emit_tail():
        emit(enc_mov_rr(8, 7))
        emit(enc_pop_ret())

    # preserve: r4-r7 pushed; r8 parks in pushed r7.
    emit(enc_push())
    emit(enc_mov_rr(7, 8))
    # main attempt with current flag.
    emit_blx(PLT_STOP)
    emit_blx(PLT_START)
    emit(enc_mov_rr(3, 0))        # verdict -> r3 (caller-dead)
    emit_verify("FAIL")
    emit(enc_cmp_imm(3, 0))
    branches.append((len(code), "bne", 0, "FAIL"))
    emit(b"\x00\x00")
    # SUCCESS tail.
    emit_tail()
    # FAIL block.
    fail = len(code)
    emit_blx(PLT_STOP)            # halt HIGH callbacks first
    emit(enc_mov_imm(1, 0))       # flag=LOW (POLARITY, §9: 0=LOW-latency
    emit(enc_strb_imm(1, 5))      #  single-buffer, 1=HIGH double-buffered)
    emit_blx(PLT_START)           # rebuild LOW (resume-class)
    emit_verify("RETRY")
    branches.append((len(code), "bn", 0, "LABELFIX"))
    emit(b"\x00\x00")
    # RETRY block (one unrolled LOW-rebuild retry for transient failure).
    # Terminal double-fail falls through to LABELFIX (truthful LOW label)
    # then TAIL_B: flag LOW + label LOW, engine possibly dead. That terminal
    # state is DOCUMENTED (not fail-safe): settings stays usable, but the
    # next audio op hits the original NULL-deref class. Live matrix item 9
    # (dead-LOW injection) is the gate; no mute machinery exists in scope.
    retry = len(code)
    emit_blx(PLT_STOP)
    emit_blx(PLT_START)
    # V2 verify targets TRAMP for its last check (a cbz whose target is
    # the immediately following block would be degenerate/fallthrough-
    # equivalent and untestable). TRAMP bounces back into LABELFIX, so
    # EVERY failed path fixes the label (N3); V2-pass falls through to
    # LABELFIX directly.
    emit_verify("TRAMP")
    # LABELFIX: SetText(LOW) via the game's own setters (B6).
    labelfix = len(code)
    emit(enc_sub_sp(0x18))        # own WString frame
    emit(enc_add_sp_imm(0, 0))    # r0 = sp (buf)
    emit_ldr_rep(1, "lowstr")
    emit(enc_add_r1_pc())
    emit_blx(PLT_WSTR)            # WStringC1EPKc(buf, LOWSTR)
    emit(enc_mov_rr(0, 4))        # r0 = control (r4 preserved)
    emit(enc_add_sp_imm(1, 0))    # r1 = buf
    emit_blx(PLT_SETTEXT)
    emit(enc_add_sp_imm(0, 0))    # r0 = buf
    emit_blx(PLT_D1)
    emit(enc_add_sp(0x18))
    # TAIL_B.
    tail_b = len(code)
    emit_tail()
    # TRAMP: backward bounce for V2's last cbz (a forward cbz here would
    # be degenerate: its target == fallthrough == LABELFIX). B.n range
    # covers it; forward-only rule applies to cbz alone.
    tramp = len(code)
    branches.append((len(code), "bn", 0, "LABELFIX"))
    emit(b"\x00\x00")
    labels = {"FAIL": fail, "RETRY": retry, "LABELFIX": labelfix,
              "TAIL_B": tail_b, "TRAMP": tramp}

    # fix up blx.W-imm (Thumb->ARM PLT, exactly the original pattern).
    for off, plt in blx_targets.items():
        code[off:off + 4] = encode_blx_imm(SHIM_ADDR + off, plt)
    # pools: one word per use; then ldr-literal fixups.
    while len(code) % 4:
        code.extend(h16(0xBF00))
    uses = [(off, kind) for off, _, kind in lits]
    pool_pos = {}
    pool_bytes = bytearray()
    for off, reg, kind in lits:
        pool_pos[off] = len(pool_bytes)
        pool_bytes.extend(b"\x00\x00\x00\x00")
    pool_base = SHIM_ADDR + len(code)
    for off, reg, kind in lits:
        target = tgts[kind]
        # add-pc (the instruction at off+2) uses addr+4 UNALIGNED, proven
        # by game display (toggle string pools resolve to exact full
        # strings only unaligned; the game renders full rows and v2 tap
        # screenshots show HIGH QUALITY).
        pc = SHIM_ADDR + off + 2 + 4
        delta = target - pc
        struct.pack_into("<i", pool_bytes, pool_pos[off], delta)
        # ldr-literal pc is rule-independent (positions aligned %4==0).
        lpc = SHIM_ADDR + off + 4
        assert lpc % 4 == 0, hex(off)
        want = pool_base + pool_pos[off]
        rng = want - lpc
        assert 0 <= rng <= 1020 and rng % 4 == 0, (off, kind)
        code[off:off + 2] = struct.pack("<H", 0x4800 | (reg << 8)
                                        | (rng // 4))
    # fix up branches: cbz forward-only; bn/bne cover small backwards.
    for off, kind, reg, label in branches:
        dst = SHIM_ADDR + labels[label]
        src = SHIM_ADDR + off
        if kind == "cbz":
            delta = dst - (src + 4)
            # CBZ T1 range 0..254 step 2 (game 0xB17A = +30; obj 0xB15A).
            assert 0 <= delta < 256 and delta % 2 == 0, (off, label)
            word = delta // 2
            # Oracle: game 0xB17A == cbz r2,+30; obj 0xB15A == cbz r2,+22.
            code[off:off + 2] = struct.pack(
                "<H", 0xB100 | ((word >> 5) << 9) | ((word & 0x1F) << 3)
                | reg)
        elif kind in ("bne", "bn"):
            delta = dst - (src + 4)
            assert -256 <= delta < 256 and delta % 2 == 0, (off, label)
            base = 0xD100 if kind == "bne" else 0xE000
            code[off:off + 2] = struct.pack("<H", base | ((delta // 2)
                                                           & 0xFF))
        else:
            raise AssertionError(kind)
    return bytes(code), bytes(pool_bytes), blx_targets, labels, uses, branches


def enc_add_r2_pc() -> bytes:
    return h16(0x447A)          # oracle + game 447x pattern


def enc_add_r1_pc() -> bytes:
    return h16(0x4479)          # game 447x pattern


def seed_compliance_or_raise(code: bytes, blx_targets: dict) -> None:
    """B1+B2 machine rule: 16-bit forms only, except 32-bit B/BL/BLX with
    BLX.W-imm restricted to the ARM-PLT allowlist (interworking-correct:
    Thumb->ARM like every original external call)."""
    assert len(code) % 2 == 0
    blx_offs = set(blx_targets)
    bw_offs = set()
    pos = 0
    units = struct.unpack(f"<{len(code) // 2}H", code)
    idx = 0
    while pos < len(code):
        w = units[idx]
        if pos in blx_offs:
            assert 0xF000 <= w <= 0xF7FF, hex(w)
            raw = code[pos:pos + 4]
            dst = decode_blx_imm(SHIM_ADDR + pos, raw)
            assert dst == blx_targets[pos], (hex(pos), hex(dst))
            assert dst in PLT_ALLOW, hex(dst)
            # True BLX.W-imm form: second-half top nibble 0xE (originals
            # f79eef20 / f797eba4 / 4ff7aeea all show it; 0xF... = BL.W,
            # no ARM switch -> SIGILL class). This kills the BL.W
            # masquerade even if encoder and decoder shared a wrong map.
            second = struct.unpack("<H", raw[2:4])[0]
            assert (second >> 12) == 0xE, hex(second)
            pos += 4
            idx += 2
            continue
        if 0xF000 <= w <= 0xF7FF:
            # 32-bit B/BL (hook-adjacent forms only in builder-known spots
            # are blx; anything else here must be bl.w — none emitted).
            raise PatchError(f"unexpected 32-bit prefix @{pos:#x}: {w:#x}")
        assert w < 0xE800, f"wide form @{pos:#x}: {w:#x}"
        assert not (0xF800 <= w <= 0xF9FF), f"wide @{pos:#x}: {w:#x}"
        pos += 2
        idx += 1


def decode_blx_imm(addr: int, raw: bytes) -> int:
    # BLX.W-imm decode (seed encode_blx_imm inverse; Thumb->ARM PLT).
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
    pc = (addr + 4) & ~3
    return pc + v


def spec_data():
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert spec["schema"] == 1
    return spec["target"], spec["sites"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_all(orig: bytes):
    code, pools, blx, labels, uses, branches = build_shim(orig)
    seed_compliance_or_raise(code, blx)
    total = code + pools
    if SHIM_ADDR + len(total) > SHIM_ZONE_END:
        raise PatchError("shim exceeds reserved zone")
    return code, pools, blx, labels, uses, branches


def phdr_patch(orig: bytes):
    """LOAD0 FileSiz/MemSiz extend (B2). Returns (offset, orig8, new8)."""
    e_phoff = struct.unpack_from("<I", orig, 28)[0]
    base = e_phoff + 32  # LOAD0 entry (PHDR table: [0]=PHDR,[1]=LOAD0)
    assert struct.unpack_from("<I", orig, base)[0] == 1, "not LOAD0"
    assert struct.unpack_from("<I", orig, base + 4)[0] == 0, "offset"
    assert struct.unpack_from("<I", orig, base + 8)[0] == 0, "vaddr"
    assert struct.unpack_from("<I", orig, base + 24)[0] == 0x5, "flags R+E"
    old = struct.unpack_from("<I", orig, base + 16)[0]
    assert old == 0x2133FC, hex(old)
    new = struct.pack("<II", LOAD0_FILESZ_NEW, LOAD0_FILESZ_NEW)
    assert LOAD0_FILESZ_NEW < 0x213BA0, "must avoid LOAD1"
    return base + 16, orig[base + 16:base + 24], new


def site_states(data: bytes, target, sites, shim_total: bytes):
    states = []
    for entry in sites:
        off = int(entry["offset"], 16)
        kind = entry.get("patched")
        if kind == "shim":
            pre = data[off - 8:off]
            cur = data[off:off + len(shim_total)]
            post = data[off + len(shim_total):off + len(shim_total) + 8]
            if (cur == b"\x00" * len(shim_total) and pre == bytes(8)
                    and post == bytes(8)):
                states.append("original")
            elif cur == shim_total:
                states.append("patched")
            else:
                states.append("unknown")
            continue
        orig = bytes.fromhex(entry["orig"])
        patched = bytes.fromhex(entry["patched"])
        pre = bytes.fromhex(entry["context_before"])
        post = bytes.fromhex(entry["context_after"])
        if (data[off - len(pre):off] == pre
                and data[off:off + len(orig)] == orig
                and data[off + len(orig):off + len(orig) + len(post)] == post):
            states.append("original")
        elif (data[off - len(pre):off] == pre
                and data[off:off + len(orig)] == patched
                and data[off + len(orig):off + len(orig) + len(post)] == post):
            states.append("patched")
        else:
            states.append("unknown")
    return states


def lib_state(data: bytes, target, sites, shim_total: bytes) -> str:
    states = set(site_states(data, target, sites, shim_total))
    if states == {"original"}:
        return "original"
    if states == {"patched"}:
        return "patched"
    if states <= {"original", "patched"}:
        return "mixed"
    return "unknown"


def check_seed_zone(data: bytes):
    if not all(b == 0 for b in data[0x213400:SEED_ZONE_END]):
        raise PatchError("seed zone disturbed; refusing (maps owns pad)")


def apply_patch(data: bytes, target, sites, shim_total: bytes) -> bytes:
    if sha256(data) != target["lib_sha256"]:
        raise PatchError("refusing unexpected library hash")
    if len(data) != target["lib_size"]:
        raise PatchError("refusing unexpected library size")
    state = lib_state(data, target, sites, shim_total)
    if state == "patched":
        raise PatchError("already patched; refusing to re-apply")
    if state != "original":
        raise PatchError(f"refusing unexpected patch state: {state}")
    check_seed_zone(data)
    out = bytearray(data)
    for entry in sites:
        off = int(entry["offset"], 16)
        kind = entry.get("patched")
        if kind == "shim":
            out[off:off + len(shim_total)] = shim_total
        else:
            orig = bytes.fromhex(entry["orig"])
            out[off:off + len(orig)] = bytes.fromhex(entry["patched"])
    result = bytes(out)
    if lib_state(result, target, sites, shim_total) != "patched":
        raise PatchError("internal error: patch did not apply cleanly")
    if len(result) != len(data):
        raise PatchError("length changed; refusing")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--check", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--lib-in", type=Path)
    parser.add_argument("--lib-out", type=Path)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--apk", type=Path, default=None)
    args = parser.parse_args(argv)
    target, sites = spec_data()
    if args.check is not None:
        data = args.check.read_bytes()
        code, pools, _, _, _, _ = build_all(data)
        shim_total = code + pools
        print(json.dumps({"file": str(args.check), "sha256": sha256(data),
                          "state": lib_state(data, target, sites,
                                             shim_total)}))
        return 0
    if args.apply:
        if args.lib_in is None or args.lib_out is None:
            parser.error("--apply needs --lib-in and --lib-out")
        if args.lib_out.exists():
            raise PatchError(f"refusing to overwrite {args.lib_out}")
        data = args.lib_in.read_bytes()
        code, pools, _, _, _, _ = build_all(data)
        args.lib_out.write_bytes(apply_patch(data, target, sites,
                                             code + pools))
        print(json.dumps({"out": str(args.lib_out),
                          "sha256": sha256(args.lib_out.read_bytes()),
                          "state": "patched"}))
        return 0
    if args.stage is not None:
        if args.stage.exists():
            raise PatchError(f"refusing existing stage path {args.stage}")
        apk = args.apk or (HERE.parents[1] / "backups"
                           / "usb-20260910T005449Z"
                           / "split_config.armeabi_v7a.apk")
        raw = apk.read_bytes()
        if sha256(raw) != target["apk_sha256"]:
            raise PatchError("refusing unexpected source APK hash")
        with zipfile.ZipFile(apk) as source:
            original = source.read(target["member"])
        code, pools, blx, labels, uses, branches = build_all(original)
        patched = apply_patch(original, target, sites, code + pools)
        try:
            sys.path.insert(0, str(HERE.parent / "maps"))
            import compose_maps_candidate as maps
            for off in maps.SENTINEL_SITES:
                if patched[off:off + 2] != bytes.fromhex("c06b"):
                    raise PatchError("sentinel disturbed")
            if patched[maps.MAPS_SITE:maps.MAPS_SITE
                       + len(maps.MAPS_ORIG)] != maps.MAPS_ORIG:
                raise PatchError("maps site disturbed")
        except ImportError:
            pass
        args.stage.mkdir(parents=True)
        (args.stage / "libtrueaxis.so.patched").write_bytes(patched)
        provenance = {
            "tool": "game-fixes/audio/patch_audio_cycling.py --stage (v2)",
            "utc": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "source_apk": str(apk),
            "source_apk_sha256": sha256(raw),
            "member": target["member"],
            "orig_lib_sha256": sha256(original),
            "patched_lib_sha256": sha256(patched),
            "variant": ("cycling-guarded v2: blx-PLT shim (B1), PHDR extend "
                        "(B2), PIC replication (B3), LOW retry (B5), "
                        "SetText fixup + boot-apply (B6)"),
            "supersedes": ("candidate-cycling/ac225508 REJECTED "
                           "(see REJECTED.json); evidence preserved"),
            "sites": [{"offset": e["offset"],
                       "orig": e["orig"], "patched": e["patched"]}
                      for e in sites],
            "shim": {"addr": hex(SHIM_ADDR),
                     "bytes": len(code + pools),
                     "blx_targets": {hex(k): hex(v)
                                     for k, v in blx.items()},
                     "labels": labels,
                     "uses": uses},
            "state": lib_state(patched, target, sites, code + pools),
            "guest_modified": False,
            "length_preserved": len(patched) == len(original),
            "note": ("staged patched LIB only; APK assembly + combined "
                     "verification owned by maps composer; isolated live "
                     "slot only after tilt, root-granted; no personal "
                     "deploy without acceptance + user approval"),
        }
        (args.stage / "provenance.json").write_text(
            json.dumps(provenance, indent=2) + "\n")
        print(json.dumps(provenance, indent=2))
        return 0
    parser.error("need --check, --apply, or --stage")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)

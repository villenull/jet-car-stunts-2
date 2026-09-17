#!/usr/bin/env python3
"""Literal-pool resolver for the JCS2 libtrueaxis.so (project scratch).

Read-only. Parses an llvm-objdump Thumb disassembly (which resolves every
`ldr rX,[pc,#imm]` target in `@ 0x...` comments), reads the pool dword from
the library, and follows the `add rX,pc` user to the referenced C string.
Confirms which UI strings a static callback references.

Usage:
  llvm-objdump --triple=thumbv7-linux-androideabi -d libtrueaxis.so > dis.txt
  python3 symbolicate.py libtrueaxis.so dis.txt --func 0x154a1c --size 0x90
"""
import re
import struct
import sys


def u32(data, off):
    return struct.unpack_from('<I', data, off)[0]


def cstr_at(data, va, maxlen=80):
    out = bytearray()
    while len(out) < maxlen and 0 <= va < len(data) and data[va] != 0:
        out.append(data[va])
        va += 1
    return out.decode('ascii', errors='replace')


LDR = re.compile(r'^\s*([0-9a-f]+):\s+[0-9a-f ]+\s+ldr(\.w)?\s+(r\d+),\s*\[pc[^\]]*\]\s+@\s*(0x[0-9a-f]+)')
ADDPC = re.compile(r'^\s*([0-9a-f]+):\s+[0-9a-f ]+\s+add\s+(r\d+),\s*pc\s*$')
ADDSYM = re.compile(r'^\s*([0-9a-f]+):')


def main():
    lib, dis = sys.argv[1], sys.argv[2]
    data = open(lib, 'rb').read()
    lines = open(dis, errors='replace').read().splitlines()
    args = sys.argv[3:]
    i = args.index('--func')
    fva, size = int(args[i + 1], 16), int(args[i + 3], 16)
    # index add-pc users by (register) -> list of vas in range
    adds = {}
    ldrs = []
    for l in lines:
        m = ADDPC.match(l)
        if m:
            va, reg = int(m.group(1), 16), m.group(2)
            if fva <= va < fva + size:
                adds.setdefault(reg, []).append(va)
            continue
        m = LDR.match(l)
        if m:
            va, reg, tgt = int(m.group(1), 16), m.group(3), int(m.group(4), 16)
            if fva <= va < fva + size:
                ldrs.append((va, reg, tgt))
    for va, reg, tgt in ldrs:
        try:
            cell = u32(data, tgt)
        except struct.error:
            cell = None
        users = [u for u in adds.get(reg, []) if u > va]
        tag = f'ldr {va:#x} {reg} -> pool {tgt:#x}={cell:#x} add-users {[hex(u) for u in users]}' if cell is not None else \
              f'ldr {va:#x} {reg} -> pool {tgt:#x}=(unreadable)'
        print(tag)
        if cell is not None and users:
            u = users[0]
            s = ((u + 4) & ~3) + cell
            if 0 <= s < len(data):
                print(f'    => {s:#x} = {cstr_at(data, s)[:64]!r}')


if __name__ == '__main__':
    main()

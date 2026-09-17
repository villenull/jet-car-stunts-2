#!/usr/bin/env python3
"""Root-adb memory dump/diff probe for the native gamepad-setting flag.

Isolated guest only. With the game running: resolves the game pid,
locates libtrueaxis.so mappings, SIGSTOPs for consistency, dumps the
writable segments via /proc/<pid>/mem, SIGCONTs. Two dumps (e.g. Gamepad
ON vs OFF) diffed byte-wise to find the persistent setting flag vs
transient state. No debugger, no writes to the guest process, no APK
changes. Requires root adbd (our guests run `adb root`).
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

GAME = "com.trueaxis.jetcarstunts2"


def adb(base, *args, timeout=30):
    proc = subprocess.run(
        [*base, *args], capture_output=True, text=True, timeout=timeout)
    return proc


def shell(base, serial, script, timeout=30):
    return adb(base, "-s", serial, "shell", script, timeout=timeout)


def game_pid(base, serial):
    proc = shell(base, serial, f"pidof {GAME}")
    out = proc.stdout.strip().split()
    if not out:
        raise RuntimeError("game not running")
    return out[0]


def lib_maps(base, serial, pid):
    """Return [(start, end, perms, path)] for libtrueaxis mappings."""
    proc = shell(base, serial, f"cat /proc/{pid}/maps")
    regions = []
    for line in proc.stdout.splitlines():
        match = re.match(
            r"([0-9a-f]+)-([0-9a-f]+) (\S+) [0-9a-f]+ \S+ \d+\s*(.*)",
            line.strip())
        if not match:
            continue
        start, end, perms, path = match.groups()
        if "libtrueaxis" in path and "w" in perms[:2]:
            regions.append((int(start, 16), int(end, 16), perms, path))
    return regions


def dump_regions(base, serial, pid, regions, out_dir, label):
    shell(base, serial, f"kill -STOP {pid}")
    blobs = []
    try:
        for start, end, perms, path in regions:
            length = end - start
            if length <= 0 or length > 64 * 1024 * 1024:
                continue
            pages = (length + 4095) // 4096
            if pages > 1024:
                blobs.append({"start": hex(start), "error": "region too big"})
                continue
            shell(base, serial, "rm -f /data/local/tmp/jcs2dump")
            ok_count = 0
            for page in range(pages):
                proc = shell(
                    base, serial,
                    f"dd if=/proc/{pid}/mem "
                    f"of=/data/local/tmp/jcs2p{page} bs=4096 "
                    f"skip={start // 4096 + page} count=1 "
                    f"2>/dev/null; echo done",
                    timeout=60)
                if "done" in proc.stdout:
                    ok_count += 1
            shell(base, serial,
                  "cat /data/local/tmp/jcs2p* > /data/local/tmp/jcs2dump "
                  "2>/dev/null; rm -f /data/local/tmp/jcs2p*")
            pull = adb(base, "-s", serial, "pull", "/data/local/tmp/jcs2dump",
                       str(out_dir / f"{label}-{start:016x}.bin"),
                       timeout=120)
            size = ((out_dir / f"{label}-{start:016x}.bin").stat().st_size
                    if pull.returncode == 0 else -1)
            blobs.append({"start": hex(start), "length": length,
                          "perms": perms, "pulled": pull.returncode == 0,
                          "size": size})
    finally:
        shell(base, serial, f"kill -CONT {pid}")
    shell(base, serial, "rm -f /data/local/tmp/jcs2dump")
    return blobs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    base = [args.adb, "-P", str(args.port)]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pid = game_pid(base, args.serial)
    regions = lib_maps(base, args.serial, pid)
    if not regions:
        print("ERROR: no writable libtrueaxis mappings", file=sys.stderr)
        return 2
    blobs = dump_regions(base, args.serial, pid, regions, out_dir, args.label)
    manifest = {"label": args.label, "pid": pid,
                "utc": time.time(), "regions": blobs}
    (out_dir / f"{args.label}-manifest.json").write_text(
        json.dumps(manifest, indent=2))
    print(json.dumps({"label": args.label, "pid": pid,
                      "regions": len(blobs)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

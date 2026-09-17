#!/usr/bin/env python3
"""Stage an isolated patched-lib candidate for maps-owner APK assembly.

Reads ONLY the precise original APK (hash-gated), writes a NEW directory
with the patched libtrueaxis.so + provenance.json. Refuses existing paths.
Never modifies originals, guests, saves, or installer state.

Usage:
  python3 compose_audio_candidate.py --stage <new-empty-dir> [--apk <orig-apk>]
"""
import argparse
import datetime
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from patch_audio import apply_patch, lib_state, load_spec, sha256, PatchError

HERE = Path(__file__).resolve().parent
DEFAULT_APK = (HERE.parents[1] / "backups" / "usb-20260910T005449Z"
               / "split_config.armeabi_v7a.apk")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--apk", type=Path, default=DEFAULT_APK)
    args = parser.parse_args(argv)
    if args.stage.exists():
        raise PatchError(f"refusing existing stage path {args.stage}")
    target, sites = load_spec()
    raw = args.apk.read_bytes()
    if sha256(raw) != target["apk_sha256"]:
        raise PatchError("refusing unexpected source APK hash")
    with zipfile.ZipFile(args.apk) as source:
        original = source.read(target["member"])
    if sha256(original) != target["lib_sha256"] or len(original) != target["lib_size"]:
        raise PatchError("refusing unexpected embedded lib")
    patched = apply_patch(original, expected_lib_sha256=target["lib_sha256"], sites=sites)
    args.stage.mkdir(parents=True)
    (args.stage / "libtrueaxis.so.patched").write_bytes(patched)
    spec = json.loads((HERE / "audio_patch.json").read_text())
    provenance = {
        "tool": "game-fixes/audio/compose_audio_candidate.py",
        "utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_apk": str(args.apk),
        "source_apk_sha256": sha256(raw),
        "member": target["member"],
        "orig_lib_sha256": sha256(original),
        "patched_lib_sha256": sha256(patched),
        "sites": [{"offset": e["offset"], "orig": e["orig"], "patched": e["patched"]}
                  for e in spec["sites"]],
        "state": lib_state(patched, sites),
        "guest_modified": False,
        "note": "staged patched LIB only; APK assembly + combined verification owned by maps worker",
    }
    (args.stage / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)

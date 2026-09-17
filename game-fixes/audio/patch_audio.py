#!/usr/bin/env python3
"""Hash-gated sound-crash patch: apply + read-only verify (never touches originals).

Spec: audio_patch.json (beside this file). v3 force-LOW compatibility mode,
3 sites (14 bytes total):
  0x154a7c (4B): NOP toggle-row `blx SoundEngine_UpdateDoubleBuffer`
  0x11e318 (6B atomic): NOP LoadOptions `strb` flag-store + `blx UpdateDoubleBuffer`
  0x154a1c (4B): callback entry `push/add` -> `bx lr; nop` (safe no-op tap)
Settings-ctor inline Update @0x153490 intentionally untouched (observed safe).

Usage:
  python3 patch_audio.py --check <libtrueaxis.so>
  python3 patch_audio.py --apply --lib-in <orig-lib> --lib-out <new-file>
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent / "audio_patch.json"


class PatchError(Exception):
    pass


def load_spec(path=SPEC_PATH):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    assert spec["schema"] == 1
    sites = []
    for entry in spec["sites"]:
        sites.append({
            "offset": int(entry["offset"], 16),
            "orig": bytes.fromhex(entry["orig"]),
            "patched": bytes.fromhex(entry["patched"]),
            "context_before": bytes.fromhex(entry["context_before"]) if entry.get("context_before") else b"",
            "context_after": bytes.fromhex(entry["context_after"]) if entry.get("context_after") else b"",
            "desc": entry.get("desc", ""),
        })
    target = spec["target"]
    return target, sites


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def site_states(data: bytes, sites):
    """Per-site state without modifying anything.

    A site counts as original/patched only if its 8-byte context on both
    sides also matches, so a shifted or wrong-version binary can never be
    patched (or misreported) on the strength of 4 coincident bytes.
    """
    states = []
    for site in sites:
        off = site["offset"]
        pre = data[off - len(site["context_before"]):off]
        current = data[off:off + len(site["orig"])]
        post = data[off + len(site["orig"]):off + len(site["orig"]) + len(site["context_after"])]
        if current == site["orig"] and pre == site["context_before"] and post == site["context_after"]:
            states.append("original")
        elif (current == site["patched"] and pre == site["context_before"]
                and post == site["context_after"]):
            states.append("patched")
        else:
            states.append("unknown")
    return states


def lib_state(data: bytes, sites) -> str:
    states = set(site_states(data, sites))
    if states == {"original"}:
        return "original"
    if states == {"patched"}:
        return "patched"
    if states <= {"original", "patched"}:
        return "mixed"
    return "unknown"


def apply_patch(data: bytes, *, expected_lib_sha256, sites) -> bytes:
    """Return patched bytes. Fail-closed on hash/context/state mismatch."""
    if sha256(data) != expected_lib_sha256:
        raise PatchError("refusing unexpected library hash (not the original v1.0.23 lib)")
    state = lib_state(data, sites)
    if state == "patched":
        raise PatchError("already patched; refusing to re-apply")
    if state != "original":
        raise PatchError(f"refusing unexpected patch state: {state}")
    out = bytearray(data)
    for site in sites:
        out[site["offset"]:site["offset"] + len(site["orig"])] = site["patched"]
    result = bytes(out)
    if lib_state(result, sites) != "patched":
        raise PatchError("internal error: patch did not apply cleanly")
    changed = {i for i, (a, b) in enumerate(zip(data, result)) if a != b}
    expected = {site["offset"] + i for site in sites for i in range(len(site["orig"]))}
    if changed != expected or len(result) != len(data):
        raise PatchError("internal error: patch touched unexpected bytes")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--check", type=Path, help="read-only: report patch state of a lib copy")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--lib-in", type=Path)
    parser.add_argument("--lib-out", type=Path)
    args = parser.parse_args(argv)
    target, sites = load_spec(args.spec)
    if args.check is not None:
        data = args.check.read_bytes()
        print(json.dumps({"file": str(args.check), "sha256": sha256(data),
                          "state": lib_state(data, sites)}))
        return 0
    if args.apply:
        if args.lib_in is None or args.lib_out is None:
            parser.error("--apply needs --lib-in and --lib-out")
        if args.lib_out.exists():
            raise PatchError(f"refusing to overwrite existing {args.lib_out}")
        data = args.lib_in.read_bytes()
        args.lib_out.write_bytes(apply_patch(data, expected_lib_sha256=target["lib_sha256"], sites=sites))
        print(json.dumps({"out": str(args.lib_out), "sha256": sha256(args.lib_out.read_bytes()),
                          "state": "patched"}))
        return 0
    parser.error("need --check or --apply")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)

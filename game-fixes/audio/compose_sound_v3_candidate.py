#!/usr/bin/env python3
"""Assemble the immutable sound-v3 five-APK candidate (offline, sound-owned).

v3 = pristine originals + 14-byte force-LOW audio patch (3 sites), NO maps
patch. Reuses the maps composer's faithful repack + one-key signing helpers
by IMPORT ONLY (no maps-owned file is edited). Refuses existing stage paths.
Never touches originals, guests, saves, installer state, or v2frozen.

Usage:
  python3 compose_sound_v3_candidate.py --stage <new-empty-dir>
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maps"))
from patch_audio import (  # noqa: E402
    apply_patch, lib_state, load_spec, PatchError,
)
import compose_maps_candidate as maps  # noqa: E402 (import only, no edits)

V3_LIB_SHA256 = "85020c6475fed1fcdfc3f8274b448660d25bad0c31f45cf95e4e906c0bf71303"
SENTINEL_ORIG = bytes.fromhex("c06b")


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.stage.exists():
        raise PatchError(f"refusing existing stage path {args.stage}")

    target, sites = load_spec()
    apk_dir, staged, skus = maps.stage_originals(args.stage)
    lib_path, lib_digest = maps.extract_lib(apk_dir, args.stage)
    if lib_digest != target["lib_sha256"]:
        raise PatchError("staged lib != v3 spec target; refusing")

    # Apply v3 to the STAGED copy only.
    original = lib_path.read_bytes()
    if lib_state(original, sites) != "original":
        raise PatchError("staged lib not in original state; refusing")
    # Sound input must carry no maps patch (sound-only variant).
    if original[maps.MAPS_SITE:maps.MAPS_SITE + len(maps.MAPS_ORIG)] != maps.MAPS_ORIG:
        raise PatchError("maps site disturbed in sound input; refusing")
    for off in maps.SENTINEL_SITES:
        if original[off:off + 2] != SENTINEL_ORIG:
            raise PatchError(f"sentinel {hex(off)} disturbed; refusing")
    v3lib = apply_patch(original, expected_lib_sha256=target["lib_sha256"], sites=sites)
    if hashlib.sha256(v3lib).hexdigest() != V3_LIB_SHA256:
        raise PatchError("v3 footprint drift; refusing")
    if lib_state(v3lib, sites) != "patched":
        raise PatchError("v3 lib not in patched state; refusing")
    lib_path.write_bytes(v3lib)
    print(f"v3 lib sha256 {V3_LIB_SHA256} (14 bytes, 3 sites, verified)")

    signed_dir, deriv_hashes, alignment = maps._finish_signed_set(
        args.stage, apk_dir, v3lib, "sound-v3 lib")

    spec = json.loads((HERE / "audio_patch.json").read_text(encoding="utf-8"))
    provenance = {
        "package": maps.PACKAGE,
        "version": maps.VERSION,
        "source_dir": str(maps.SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": lib_digest,
        "original_cert_sha256_fingerprint": maps.ORIGINAL_CERT_SHA256_FPR,
        "variant_lib_sha256": V3_LIB_SHA256,
        "derivative_apk_sha256": deriv_hashes,
        "derivative_cert_sha256_fingerprint": maps.DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "signature": ("all five splits re-signed with ONE project key "
                      "(offline-local-developer); FRESH INSTALL ONLY"),
        "variant": ("sound-v3 force-LOW (toggle blx NOP @0x154a7c; "
                    "LoadOptions strb+blx NOP @0x11e318 atomic 6B; "
                    "callback entry bx-lr @0x154a1c; NO maps patch)"),
        "audio_sites": [{"offset": e["offset"], "orig": e["orig"],
                         "patched": e["patched"]} for e in spec["sites"]],
        "maps_site": "ORIGINAL (sound-only; A/B + ownership remain maps-owned)",
        "progression": "gated normally (six sentinel sites ORIGINAL)",
        "truthful_label": ("flag locked LOW at both writers; labels always "
                           "read LOW LATENCY; Sound-row taps are safe no-ops; "
                           "saved-HIGH normalizes at boot with no data clear"),
        "panel_note": ("controls_settings.py:84 reword owned by tilt/reviewer "
                       "d853d417; applies ONLY with actual personal deploy, "
                       "never prematurely (note is accurate for unpatched builds)"),
        "assembler": ("game-fixes/audio/compose_sound_v3_candidate.py reusing "
                      "maps faithful repack + one-key sign by import only"),
        "live_gate": ("isolated slot only, root-granted after maps release; "
                      "never the personal guest without explicit user approval"),
    }
    (args.stage / "maps-sound-v3-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n")
    (args.stage / "FROZEN.json").write_text(json.dumps({
        "frozen": True,
        "builder": "compose_sound_v3_candidate.py faithful+aligned repack",
        "cert": maps.DERIV_CERT_SHA256_FPR,
        "alignment": alignment,
        "record": {"variant": "sound-v3", "apk_sha256": deriv_hashes},
    }, indent=2) + "\n")
    print(f"DONE: sound-v3 candidate at {args.stage}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PatchError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)

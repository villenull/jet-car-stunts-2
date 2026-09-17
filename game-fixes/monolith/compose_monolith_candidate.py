#!/usr/bin/env python3
"""Compose the installable JCS2 monolith variant (offline, import-only).

Variant = pristine originals + sound v3 (14B, 3 sites) + maps 12B +
six unlock sentinels (c06b->0020) + controls button-gate NOP (3 changed
bytes, 1 site) + editor save+reopen NOPs (4B, 2 sites). Total 45 changed
bytes at 13 sites, all verified original+disjoint before apply.

Reuses the family composers by IMPORT ONLY (no maps/controls/editor/audio
owned file is edited): patch_audio.load_spec/apply_patch/lib_state,
maps.MAPS_SITE/MAPS_ORIG/MAPS_PATCHED/SENTINEL_SITES/_manifest_version_code/
_repack_unsigned/PACKAGE, controls_mod.SENTINEL_ORIG/SENTINEL_UNLOCK/
SENTINEL_CONTEXT/CTRL_SITE/CTRL_ORIG/CTRL_PATCHED, and EDITOR_SITES/
EDITOR_BLX/EXPECTED_FINAL_SHA256 from compose_editor_candidate.

Two modes (stdlib only, no network, no signing, no key material):
  python3 compose_monolith_candidate.py --check --source <monolith.apk>
      Dry run: verify versionCode==29, lib member sha256==pristine,
      all 13 ranges pristine with contexts + disjoint; print the 45-byte
      footprint table; exit 0; write nothing.
  python3 compose_monolith_candidate.py --source <monolith.apk> \\
      --out-dir <new-empty-dir>
      Refuse an existing out dir; extract the lib; apply audio v3 then
      maps 12B + six sentinels + controls NOP + 2 editor NOPs with the
      same per-site guards the family composers use; assert final lib
      sha256 == cb2bd45b85cd6bf161199e57c460870d295b73d4ad2101bb7d97d0d413f0ffb1;
      faithfully repack the WHOLE monolith with the final lib swapped in
      (same per-entry compression, META-INF stripped for later signing);
      write unsigned-monolith.apk + variant-descriptor.json.
Fail closed (PatchError, nonzero exit) on any mismatch.
"""

import hashlib
import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "audio"))
sys.path.insert(0, str(HERE.parent / "maps"))
sys.path.insert(0, str(HERE.parent / "controls"))
sys.path.insert(0, str(HERE.parent / "editor-save"))
from patch_audio import (  # noqa: E402
    PatchError,
    apply_patch,
    lib_state,
    load_spec,
)
import compose_maps_candidate as maps  # noqa: E402 (import only, no edits)
import compose_controls_candidate as controls_mod  # noqa: E402 (import only)
import compose_editor_candidate as editor_mod  # noqa: E402 (import only)

ORIG_LIB_SHA256 = "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d"
EXPECTED_FINAL_SHA256 = editor_mod.EXPECTED_FINAL_SHA256
assert EXPECTED_FINAL_SHA256 == (
    "cb2bd45b85cd6bf161199e57c460870d295b73d4ad2101bb7d97d0d413f0ffb1"
), "editor composer final hash drifted; refusing to compose blind"

LIB_MEMBER = "lib/armeabi-v7a/libtrueaxis.so"
assert maps.LIB_IN_APK == LIB_MEMBER, "maps lib member drifted; refusing"

EXPECTED_VERSION_CODE = 29


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_lib(source: Path) -> bytes:
    with zipfile.ZipFile(source) as zin:
        try:
            return zin.read(LIB_MEMBER)
        except KeyError:
            raise PatchError(f"{source}: member {LIB_MEMBER} absent; refusing")


def _footprint_rows(sites):
    """Per-site table rows: (label, offset, orig_hex, patched_hex, changed)."""
    rows = []
    for entry in sites:
        off = entry["offset"]
        rows.append(("audio-v3", off, entry["orig"].hex(),
                     entry["patched"].hex(), len(entry["patched"])))
    rows.append(("maps-12B", maps.MAPS_SITE, maps.MAPS_ORIG.hex(),
                 maps.MAPS_PATCHED.hex(), 12))
    for off in maps.SENTINEL_SITES:
        rows.append(("sentinel", off, controls_mod.SENTINEL_ORIG.hex(),
                     controls_mod.SENTINEL_UNLOCK.hex(), 2))
    ctrl_changed = sum(
        1 for a, b in zip(controls_mod.CTRL_ORIG, controls_mod.CTRL_PATCHED)
        if a != b)
    rows.append(("controls-NOP", controls_mod.CTRL_SITE,
                 controls_mod.CTRL_ORIG.hex(), controls_mod.CTRL_PATCHED.hex(),
                 ctrl_changed))
    for site in editor_mod.EDITOR_SITES:
        rows.append((site["label"], site["offset"], site["orig"].hex(),
                     site["patched"].hex(), len(site["patched"])))
    return rows


def _check_ranges(data: bytes, sites) -> list:
    """Verify all 13 ranges pristine (context-gated) and disjoint.

    Returns the footprint rows. Raises PatchError on any mismatch.
    """
    problems = []

    # Audio sites: context-gated state check (same rule as patch_audio).
    states = lib_state(data, sites)
    if states != "original":
        problems.append(f"audio v3 state is {states!r}, want 'original'")

    # Maps 16B window pristine.
    if data[maps.MAPS_SITE:maps.MAPS_SITE + len(maps.MAPS_ORIG)] != maps.MAPS_ORIG:
        problems.append(f"maps window {maps.MAPS_SITE:#x} not pristine")

    # Six sentinels: orig bytes + guard context pristine.
    for off in maps.SENTINEL_SITES:
        if data[off:off + 2] != controls_mod.SENTINEL_ORIG:
            problems.append(f"sentinel {off:#x} not pristine")
        if data[off + 2:off + 4] != controls_mod.SENTINEL_CONTEXT:
            problems.append(f"sentinel context {off:#x} drifted")

    # Controls site pristine.
    ctrl = controls_mod
    if data[ctrl.CTRL_SITE:ctrl.CTRL_SITE + 4] != ctrl.CTRL_ORIG:
        problems.append(f"controls site {ctrl.CTRL_SITE:#x} not pristine")

    # Editor sites pristine + neighbour BLX intact.
    for site in editor_mod.EDITOR_SITES:
        off = site["offset"]
        if data[off:off + 2] != site["orig"]:
            problems.append(f"editor site {off:#x} "
                            f"({data[off:off+2].hex()} != "
                            f"{site['orig'].hex()}) not pristine")
    for off, exp in editor_mod.EDITOR_BLX:
        if data[off:off + 4] != exp:
            problems.append(f"editor BLX @{off:#x} disturbed")

    if problems:
        raise PatchError("pristine check failed: " + "; ".join(problems))

    # Disjointness over full windows (maps counts its 16B window).
    windows = ([(s["offset"], s["offset"] + len(s["orig"]),
                 f"audio {s['offset']:#x}") for s in sites]
               + [(maps.MAPS_SITE, maps.MAPS_SITE + len(maps.MAPS_ORIG),
                   "maps-12B window")]
               + [(o, o + 2, f"sentinel {o:#x}") for o in maps.SENTINEL_SITES]
               + [(ctrl.CTRL_SITE, ctrl.CTRL_SITE + 4, "controls-NOP")]
               + [(s["offset"], s["offset"] + 2, s["label"])
                  for s in editor_mod.EDITOR_SITES])
    assert len(windows) == 13, f"want 13 sites, have {len(windows)}"
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            a0, a1, al = windows[i]
            b0, b1, bl = windows[j]
            if a0 < b1 and b0 < a1:
                raise PatchError(f"range overlap: {al} [{a0:#x},{a1:#x}) "
                                 f"vs {bl} [{b0:#x},{b1:#x}); refusing")

    rows = _footprint_rows(sites)
    total = sum(r[4] for r in rows)
    if total != 45:
        raise PatchError(f"footprint {total} != 45; refusing")
    return rows


def _print_footprint(rows) -> None:
    print("45-byte footprint table (13 sites, all verified disjoint):")
    print(f"  {'site':<16} {'offset':<10} {'orig':<34}-> patched")
    for label, off, orig, patched, changed in rows:
        print(f"  {label:<16} {off:#08x}   {orig:<34}-> {patched} "
              f"({changed} B)")
    print(f"  TOTAL: {sum(r[4] for r in rows)} changed bytes "
          f"at {len(rows)} sites")


def do_check(source: Path) -> int:
    if not source.is_file():
        raise PatchError(f"source {source} missing; refusing")
    version = maps._manifest_version_code(source)
    if version != EXPECTED_VERSION_CODE:
        raise PatchError(f"versionCode {version} != "
                         f"{EXPECTED_VERSION_CODE}; refusing")
    print(f"manifest versionCode: {version} (OK)")
    lib = _read_lib(source)
    digest = _sha256_bytes(lib)
    if digest != ORIG_LIB_SHA256:
        raise PatchError(f"lib {digest} != pristine {ORIG_LIB_SHA256}; "
                         "refusing")
    print(f"lib {LIB_MEMBER}: sha256 {digest} (pristine OK, "
          f"{len(lib)} bytes)")
    target, sites = load_spec()
    if target["lib_sha256"] != ORIG_LIB_SHA256:
        raise PatchError("audio spec target != pristine lib; refusing")
    rows = _check_ranges(lib, sites)
    print("all 13 ranges pristine (context-gated) + disjoint (OK)")
    _print_footprint(rows)
    print(f"package: {maps.PACKAGE}")
    print("CHECK OK: source is the pristine monolith; wrote nothing")
    return 0


def do_compose(source: Path, out_dir: Path) -> int:
    if out_dir.exists():
        raise PatchError(f"refusing existing out dir {out_dir}")
    if not source.is_file():
        raise PatchError(f"source {source} missing; refusing")

    version = maps._manifest_version_code(source)
    if version != EXPECTED_VERSION_CODE:
        raise PatchError(f"versionCode {version} != "
                         f"{EXPECTED_VERSION_CODE}; refusing")

    target, sites = load_spec()
    if target["lib_sha256"] != ORIG_LIB_SHA256:
        raise PatchError("audio spec target != pristine lib; refusing")

    original = _read_lib(source)
    if _sha256_bytes(original) != ORIG_LIB_SHA256:
        raise PatchError("source lib != pristine; refusing")

    # 1. Sound v3 on an isolated copy (hash/context/state gated inside).
    if lib_state(original, sites) != "original":
        raise PatchError("source lib not in original audio state; refusing")
    v3lib = apply_patch(original,
                        expected_lib_sha256=target["lib_sha256"], sites=sites)
    if lib_state(v3lib, sites) != "patched":
        raise PatchError("v3 lib not in patched state; refusing")

    # 2. Same per-site disjointness guards the family composers use.
    if v3lib[maps.MAPS_SITE:maps.MAPS_SITE + len(maps.MAPS_ORIG)] != maps.MAPS_ORIG:
        raise PatchError("maps site disturbed in sound input; refusing")
    for off in maps.SENTINEL_SITES:
        if v3lib[off:off + 2] != controls_mod.SENTINEL_ORIG:
            raise PatchError(f"sentinel {off:#x} disturbed; refusing")
        if v3lib[off + 2:off + 4] != controls_mod.SENTINEL_CONTEXT:
            raise PatchError(f"sentinel context {off:#x} drifted; refusing")
    if v3lib[controls_mod.CTRL_SITE:controls_mod.CTRL_SITE + 4] != \
            controls_mod.CTRL_ORIG:
        raise PatchError("controls site disturbed; refusing")
    for site in editor_mod.EDITOR_SITES:
        off = site["offset"]
        if v3lib[off:off + 2] != site["orig"]:
            raise PatchError(f"editor site {off:#x} disturbed; refusing")
    rows = _check_ranges(original, sites)  # disjointness proof on pristine

    # 3. Apply maps 12B + six sentinels + controls NOP + 2 editor NOPs.
    final = bytearray(v3lib)
    final[maps.MAPS_SITE:maps.MAPS_SITE + len(maps.MAPS_PATCHED)] = \
        maps.MAPS_PATCHED
    for off in maps.SENTINEL_SITES:
        final[off:off + 2] = controls_mod.SENTINEL_UNLOCK
    final[controls_mod.CTRL_SITE:controls_mod.CTRL_SITE + 4] = \
        controls_mod.CTRL_PATCHED
    for site in editor_mod.EDITOR_SITES:
        off = site["offset"]
        final[off:off + 2] = site["patched"]
    final = bytes(final)
    final_digest = _sha256_bytes(final)

    # 4. Exact 45-byte footprint proof.
    diff = [i for i, (a, b) in enumerate(zip(original, final)) if a != b]
    expect = set()
    for entry in sites:
        expect |= set(range(entry["offset"],
                            entry["offset"] + len(entry["patched"])))
    expect |= set(range(maps.MAPS_SITE + 4, maps.MAPS_SITE + 16))
    for off in maps.SENTINEL_SITES:
        expect |= {off, off + 1}
    expect |= {controls_mod.CTRL_SITE + k for k in range(4)
               if controls_mod.CTRL_ORIG[k] != controls_mod.CTRL_PATCHED[k]}
    for site in editor_mod.EDITOR_SITES:
        expect |= {site["offset"], site["offset"] + 1}
    if set(diff) != expect:
        raise PatchError(
            f"footprint drift: got {len(diff)} bytes, want {len(expect)}; "
            f"extra={sorted(set(diff) - expect)[:8]} "
            f"missing={sorted(expect - set(diff))[:8]}")
    if len(diff) != 45:
        raise PatchError(f"footprint {len(diff)} != 45; refusing")

    # Per-site state re-verification on the final bytes.
    if lib_state(final, sites) != "patched":
        raise PatchError("sound v3 not intact in final; refusing")
    if final[maps.MAPS_SITE:maps.MAPS_SITE + 16] != maps.MAPS_PATCHED:
        raise PatchError("maps 12B not intact in final; refusing")
    for off in maps.SENTINEL_SITES:
        if final[off:off + 2] != controls_mod.SENTINEL_UNLOCK:
            raise PatchError(f"sentinel {off:#x} not unlock; refusing")
    if final[controls_mod.CTRL_SITE:controls_mod.CTRL_SITE + 4] != \
            controls_mod.CTRL_PATCHED:
        raise PatchError("controls NOP not intact in final; refusing")
    for off, exp in editor_mod.EDITOR_BLX:
        if final[off:off + 4] != exp:
            raise PatchError(f"editor BLX @{off:#x} disturbed; refusing")
    for site in editor_mod.EDITOR_SITES:
        off = site["offset"]
        if final[off:off + 2] != site["patched"]:
            raise PatchError(f"editor site {off:#x} not NOP; refusing")
    if final_digest != EXPECTED_FINAL_SHA256:
        raise PatchError(f"final lib {final_digest} != expected "
                         f"{EXPECTED_FINAL_SHA256}; refusing")
    print(f"final lib sha256 {final_digest} "
          f"({len(diff)} bytes at 13 sites, verified)")
    _print_footprint(rows)

    # 5. Faithful repack of the WHOLE monolith, final lib swapped in,
    #    same per-entry compression, META-INF stripped for later signing.
    out_dir.mkdir(parents=False, exist_ok=False)
    unsigned = out_dir / "unsigned-monolith.apk"
    maps._repack_unsigned(source, unsigned,
                          replacements={LIB_MEMBER: final}, keep_meta=False)

    # 6. Verify the unsigned output before describing it.
    with zipfile.ZipFile(source) as zin:
        src_names = [i.filename for i in zin.infolist()
                     if not i.is_dir()]
        src_methods = {i.filename: i.compress_type for i in zin.infolist()
                       if not i.is_dir()}
    with zipfile.ZipFile(unsigned) as zout:
        out_names = [i.filename for i in zout.infolist()
                     if not i.is_dir()]
        out_methods = {i.filename: i.compress_type for i in zout.infolist()
                       if not i.is_dir()}
        out_lib = zout.read(LIB_MEMBER)
    want_names = [n for n in src_names if not n.startswith("META-INF/")]
    if out_names != want_names:
        raise PatchError("unsigned entry list != source minus META-INF; "
                         "refusing")
    if any(n.startswith("META-INF/") for n in out_names):
        raise PatchError("unsigned APK still carries META-INF; refusing")
    for name in out_names:
        if out_methods[name] != src_methods[name]:
            raise PatchError(f"compression drift on {name}; refusing")
    if _sha256_bytes(out_lib) != EXPECTED_FINAL_SHA256:
        raise PatchError("unsigned lib != final lib; refusing")
    if maps._manifest_version_code(unsigned) != EXPECTED_VERSION_CODE:
        raise PatchError("unsigned manifest versionCode drifted; refusing")

    descriptor = {
        "package": maps.PACKAGE,
        "versionCode": EXPECTED_VERSION_CODE,
        "source_apk_sha256": _sha256_file(source),
        "final_lib_sha256": final_digest,
        "sites": [
            {"label": label, "offset": hex(off), "orig": orig,
             "patched": patched, "changed_bytes": changed}
            for label, off, orig, patched, changed in rows
        ],
        "cert": "TBD-at-sign-time",
    }
    (out_dir / "variant-descriptor.json").write_text(
        json.dumps(descriptor, indent=2), encoding="utf-8")
    print(f"DONE: unsigned monolith + descriptor in {out_dir} "
          f"({len(out_names)} entries, versionCode {EXPECTED_VERSION_CODE}, "
          "no META-INF, no signing)")
    return 0


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="dry run: verify source, print footprint, "
                             "write nothing")
    parser.add_argument("--source", type=Path, required=True,
                        help="source monolithic APK")
    parser.add_argument("--out-dir", type=Path,
                        help="new empty dir for compose outputs")
    args = parser.parse_args(argv)
    try:
        if args.check:
            if args.out_dir is not None:
                raise PatchError("--check writes nothing; "
                                 "drop --out-dir")
            return do_check(args.source)
        if args.out_dir is None:
            parser.error("compose mode needs --source and --out-dir")
        return do_compose(args.source, args.out_dir)
    except PatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

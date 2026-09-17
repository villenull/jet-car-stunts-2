#!/usr/bin/env python3
"""Maps candidate composer (offline, reproducible, non-destructive).

Stages a maps candidate from the ORIGINAL signed five split APKs
v1.0.23 WITHOUT modifying a single APK byte:

  * copies ONLY the five precise APK files from
    backups/usb-20260910T005449Z into an isolated staging directory
    (never the directory wholesale, never private backups);
  * verifies all five SHA-256 hashes + libtrueaxis.so hash;
  * runs the maps invariant verifier on the staged armeabi_v7a lib;
  * reserves the sound-worker merge slot: records the staged lib hash so a
    later sound patch can be detected/merged WITHOUT being clobbered;
  * writes provenance JSON: original hashes, original cert fingerprint
    (independently re-derived via openssl from base.apk BNDLTOOL.RSA),
    allowlist, patch status.

Usage:
    python3 compose_maps_candidate.py --stage <new-empty-dir>
        Bit-identical staging + provenance (no signature change).
    python3 compose_maps_candidate.py --derivative <new-empty-dir>
        Full maps derivative: patched lib -> repacked armeabi_v7a split ->
        all five splits re-signed with ONE project key (fresh-install only;
        cert differs from original -> NEVER an in-place personal update).

Ownership mechanism (v1): 12-byte native patch making Levels_IsPurchased
return owned (see patch_maps_ownership.py for the static caller census:
exactly 3 callers via true PLT 0xa592c — LevelRow ctor, LevelSelect
population, UserChallenges click; LoadLevel needs nothing; progression
sentinels, g_bUnLockAll, shop predicate untouched).
No stats seed (fresh install starts at zero); the optional store
unlock-all action is preserved untouched.

Ownership mechanism (v2, --ownership): sound-v3 audio sites + 12B levels
gate + per-SKU populateStore epilogue seed (see patch_maps_seed.py:
legitimate setter, 10 allowlisted SKUs, predicate untouched) with
progression still gated. Opt-in unlock-all variant (--unlock-all): the v2
layers PLUS the six progression sentinels, same key/version, explicit
consent only (reviewer contract: linux-launcher/progression_choice.py
VARIANT_CONTRACT; descriptors emitted per variant for the executor).
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.resolve().parents[1]  # game-fixes/maps -> project root

SOURCE_DIR = ROOT / "backups/usb-20260910T005449Z"
LIB_IN_APK = "lib/armeabi-v7a/libtrueaxis.so"

ORIGINAL_APKS = {
    "base.apk": "41043f68cc28e0f82a83a996be24cc6e27dc7b35c0a47fde45c08e2146d6bfa9",
    "split_config.armeabi_v7a.apk":
        "d1ceccba8515edcc250f751bcb582bb7276b54a26876e0150e6c6ae73bf2c256",
    "split_config.en.apk":
        "9d72176b24bdfcd16ee5a9bf70e94ab85b343b8cd2a94bc0c385ec36d8c7c40f",
    "split_config.es.apk":
        "1000baf07a4aa02a6b0adfbaa7b625c8a74537abbd32078894db6a0a5e230c2c",
    "split_config.xhdpi.apk":
        "59da6a867a97f6d17a929be646664fc28a8cf2a44d7fef6f01a3476b91c95f49",
}
EXPECTED_LIB_SHA256 = (
    "bc7fdf9d62b7a2dc33ddd97d67e9f74830aea58aa059104dd97d9cbff408a34d"
)
# Original True Axis cert (v1 BNDLTOOL.RSA + v2 block), independently
# re-derived via openssl from base.apk: subject O=True Axis Pty Ltd,
# CN=Luke Ryan, 2014-2041. Matches installer handoff 5d714ace.
ORIGINAL_CERT_SHA256_FPR = (
    "97:D4:96:89:7F:6A:02:18:AF:E9:6F:60:E0:97:2F:D6:"
    "65:95:0E:54:95:CF:A5:CF:38:A5:00:F3:F4:27:EA:36"
)
# Project signing key for derivatives (fresh installs only). Same key the
# prior progression-unlock candidate used; all five splits share one cert.
JDK = ROOT / "analysis/controller/20260910T140000Z/tools/jdk-17.0.20.1+1"
KEY_DIR = ROOT / "analysis/offline-apk-hardening-20260910T140000Z/owneronly"
KEYSTORE = KEY_DIR / "offline-local-developer.p12"
STOREPASS_FILE = KEY_DIR / ".storepass"
KEY_ALIAS = "offline"
DERIV_CERT_SHA256_FPR = (
    "DB:86:A5:6E:99:37:04:51:84:A3:B5:B8:E3:64:E3:0F:"
    "53:33:A3:0A:FD:A1:27:39:E1:1E:71:78:B4:AD:33:ED"
)
PATCHED_LIB_SHA256 = (
    "57c81b10bd86fad90002307c4d944d6f1a3f3dbf1b066f78fd0b04bdeb9b0166"
)
# Sound-first order: sound worker's staged lib (pristine + 4 sound bytes).
SOUND_LIB_SHA256 = (
    "dfc1a520a683959ab9fb0be380b3c31e7e19530cc26c998e3c82ab0a1ae89652"
)
SOUND_SITE = 0x154a7c
SOUND_ORIG = bytes.fromhex("4ff7aeea")
SOUND_PATCHED = bytes.fromhex("bf00bf00")
# Maps site (mirrors patch_maps_ownership.py; asserted, not imported).
MAPS_SITE = 0x133014
MAPS_ORIG = bytes.fromhex("80b56f4670f79cebbde8804093f0debd")
MAPS_PATCHED = bytes.fromhex("80b56f46012080bdbf00bf00bf00bf00")
SENTINEL_SITES = (0x133046, 0x13D846, 0x13D944,
                  0x14BFBC, 0x14C10A, 0x14C11C)
# Sound v3 audio sites (authoritative: staging/maps-sound-v3final/
# maps-sound-v3-provenance.json "audio_sites"; the intermediate lib hash
# is pinned below and verified at assembly).
V3_LIB_SHA256 = (
    "85020c6475fed1fcdfc3f8274b448660d25bad0c31f45cf95e4e906c0bf71303"
)
V3_SITES = (
    {"offset": 0x154a7c, "orig": "4ff7aeea",
     "patched": "00bf00bf", "label": "audio toggle blx NOP"},
    {"offset": 0x11e318, "orig": "017085f760ee",
     "patched": "00bf00bf00bf", "label": "LoadOptions strb+blx NOP (6B)"},
    {"offset": 0x154a1c, "orig": "f0b503af",
     "patched": "704700bf", "label": "callback entry bx-lr"},
)
# Six progression sentinels (identical sites/bytes/context to
# progression-unlock/patch_progression.py PATCH_SITES and reviewer
# muse-unlockall-design.md S2; applied here so the variant composer is
# self-contained — the shared tool file is never edited).
SENTINEL_ORIG = bytes.fromhex("c06b")      # ldr r0,[r0,#0x3c]
SENTINEL_PATCHED = bytes.fromhex("0020")   # movs r0,#0
SENTINEL_CONTEXT = bytes.fromhex("0130")   # adds r0,#1 sentinel check
SEED_PATCHER = HERE / "patch_maps_seed.py"
SEED_BRANCH_AT = 0x1058a0
SEED_PAD_LO = 0x213400
SEED_PAD_HI = 0x213ba0
PACKAGE = "com.trueaxis.jetcarstunts2"
VERSION = "1.0.23 (versionCode 29)"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_originals(stage: Path):
    """Copy+verify the five original APKs; return (apk_dir, staged, skus)."""
    allowlist = json.loads(
        (HERE / "OWNERSHIP-ALLOWLIST.json").read_text(encoding="utf-8"))
    skus = allowlist["content_skus"]
    stage.mkdir(parents=True)
    apk_dir = stage / "apks"
    apk_dir.mkdir()
    staged = {}
    for name, expected in ORIGINAL_APKS.items():
        src = SOURCE_DIR / name
        digest = sha256(src)
        if digest != expected:
            raise ValueError(
                f"{name} hash {digest} != expected {expected}; refusing "
                "(not the proven original set)")
        shutil.copyfile(src, apk_dir / name)
        staged[name] = digest
        print(f"staged {name} {digest[:16]}... (hash verified, bytes copied)")
    return apk_dir, staged, skus


def extract_lib(apk_dir: Path, stage: Path):
    with zipfile.ZipFile(apk_dir / "split_config.armeabi_v7a.apk") as z:
        lib_data = z.read(LIB_IN_APK)
    lib_digest = hashlib.sha256(lib_data).hexdigest()
    if lib_digest != EXPECTED_LIB_SHA256:
        raise ValueError(f"staged lib hash {lib_digest} != expected; refusing")
    lib_path = stage / "libtrueaxis-staged.so"
    lib_path.write_bytes(lib_data)
    return lib_path, lib_digest


def run_verifier(lib_path: Path):
    verifier = HERE / "verify_maps_invariants.py"
    result = subprocess.run(
        [sys.executable, str(verifier), str(lib_path)],
        capture_output=True, text=True, timeout=60)
    print(result.stdout.strip())
    if result.returncode != 0:
        raise ValueError("maps invariants failed: " + result.stderr.strip())


def do_stage(stage: Path):
    apk_dir, staged, skus = stage_originals(stage)
    lib_path, lib_digest = extract_lib(apk_dir, stage)
    run_verifier(lib_path)
    provenance = {
        "package": PACKAGE,
        "version": VERSION,
        "source_dir": str(SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": lib_digest,
        "original_cert_sha256_fingerprint": ORIGINAL_CERT_SHA256_FPR,
        "apk_bytes_modified": False,
        "signature": "ORIGINAL True Axis cert retained (no resign performed)",
        "ownership_allowlist": skus,
        "ownership_mechanism": "pending derivative (see --derivative)",
        "progression": "gated normally (six sentinel sites ORIGINAL)",
        "unlock_all": "optional existing store action preserved untouched",
        "stats_seed": "none (fresh install starts at zero)",
        "sound_merge_slot": {
            "staged_lib_sha256": lib_digest,
            "rule": ("sound worker patch applies first to an isolated copy; "
                     "this composer re-verifies that only sound-owned sites "
                     "changed before assembling the combined candidate"),
            "status": "pending sound worker 630cd122 delivery",
        },
        "live_gate": ("install/test ONLY in a root-assigned live slot on an "
                      "isolated fresh candidate with backup; never the "
                      "personal guest without explicit user approval"),
    }
    (stage / "maps-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {stage / 'maps-provenance.json'}")
    print("DONE: maps candidate staged, APK bytes unmodified, "
          "original signature retained")


def check_zip_alignment(apk: Path):
    """Verify STORED-entry 4-byte alignment with stdlib (no zipalign tool).

    R3: no zipalign binary exists in the project toolchain (the only copy
    found is an ARM binary inside an LP APK, unrunnable on this host; no
    build-tools). Android alignment matters only for STORED (uncompressed)
    entries mmap'd from the APK. Our splits keep every entry DEFLATED
    (as stock), so alignment is structurally N/A — this check proves it
    per split instead of deferring to install time. Returns
    (stored_total, stored_misaligned, deflated_total).
    """
    import struct as _struct

    data = apk.read_bytes()
    stored_total = stored_misaligned = deflated_total = 0
    with zipfile.ZipFile(apk) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            local = info.header_offset
            name_len, extra_len = _struct.unpack_from(
                "<HH", data, local + 26)
            # Sanity: local header signature + name match.
            assert data[local:local + 4] == b"PK\x03\x04", apk
            data_offset = local + 30 + name_len + extra_len
            if info.compress_type == zipfile.ZIP_STORED:
                stored_total += 1
                if data_offset % 4 != 0:
                    stored_misaligned += 1
            else:
                deflated_total += 1
    return stored_total, stored_misaligned, deflated_total


def _measure_data_offsets(apk: Path):
    """Map arcname -> (method, data_offset) by parsing local headers."""
    import struct as _struct
    data = apk.read_bytes()
    out = {}
    with zipfile.ZipFile(apk) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            local = info.header_offset
            assert data[local:local + 4] == b"PK\x03\x04", apk
            name_len, extra_len = _struct.unpack_from("<HH", data, local + 26)
            out[info.filename] = (info.compress_type,
                                  local + 30 + name_len + extra_len)
    return out


def _repack_unsigned(src_apk: Path, dst_apk: Path, replacements=None,
                    keep_meta=False):
    """Faithful repack: preserve per-entry methods.

    Without keep_meta, META-INF is dropped (for re-signing). With
    keep_meta, META-INF is preserved byte-for-byte (post-sign alignment:
    v1 JAR signatures cover entry digests, not local-header extras, so
    extra-field padding keeps `jarsigner -verify` green — re-verified by
    the caller).
    
    Keeps STORED entries stored (stock resources.arsc, PNGs) and aligns
    STORED data offsets to 4 bytes with extra-field padding (stdlib
    zipalign-equivalent; no external binary exists in the toolchain).
    replacements: {arcname: bytes} swapped in with the original entry's
    method (patched lib stays DEFLATED, as stock). Two passes: measure
    DEFLATED sizes deterministically, then pad exactly; the finished
    archive is independently validated by check_zip_alignment.
    """
    replacements = replacements or {}
    with zipfile.ZipFile(src_apk) as zin:
        items = [(i, replacements.get(i.filename, zin.read(i.filename)))
                 for i in zin.infolist()
                 if keep_meta or not i.filename.startswith("META-INF/")]

    def build(pads):
        with zipfile.ZipFile(dst_apk, "w") as zout:
            for info, data in items:
                name_bytes = info.filename.encode("utf-8")
                new_info = zipfile.ZipInfo(
                    filename=info.filename, date_time=info.date_time)
                new_info.compress_type = info.compress_type
                new_info.external_attr = info.external_attr
                new_info.create_system = info.create_system
                new_info.extra = b"\x00" * pads.get(info.filename, 0)
                zout.writestr(new_info, data)

    build({})
    # Pads computed in archive order with cumulative shift: each pad
    # changes every later offset, so accumulate as we go. DEFLATED sizes
    # are deterministic for identical inputs, making pass 2 exact.
    with zipfile.ZipFile(dst_apk) as z:
        order = [i.filename for i in z.infolist() if not i.is_dir()]
    measured = _measure_data_offsets(dst_apk)
    pads, shift = {}, 0
    for name in order:
        method, off = measured[name]
        if method == zipfile.ZIP_STORED:
            pad = (-(off + shift)) % 4
            if pad:
                pads[name] = pad
                shift += pad
    if pads:
        build(pads)
    return dst_apk


def sign_apk(unsigned: Path, signed: Path, log_path: Path):
    command = [str(JDK / "bin/jarsigner"), "-keystore", str(KEYSTORE),
               "-storetype", "PKCS12", "-storepass:file", str(STOREPASS_FILE),
               "-digestalg", "SHA-256", "-sigalg", "SHA256withRSA",
               "-sigfile", "OFFLINE",
               "-signedjar", str(signed), str(unsigned), KEY_ALIAS]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=120)
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    result.check_returncode()
    result = subprocess.run(
        [str(JDK / "bin/jarsigner"), "-verify", "-verbose", "-certs",
         str(signed)], capture_output=True, text=True, timeout=120)
    if "jar verified." not in result.stdout:
        raise ValueError(f"JAR verification failed for {signed.name}:\n"
                         + result.stdout + result.stderr)


def cert_pem(apk: Path) -> bytes:
    with zipfile.ZipFile(apk) as z:
        rsa_name = next(n for n in z.namelist()
                        if n.startswith("META-INF/") and n.endswith(".RSA"))
        rsa_der = z.read(rsa_name)
    result = subprocess.run(
        ["openssl", "pkcs7", "-inform", "DER", "-print_certs"],
        input=rsa_der, capture_output=True, timeout=30)
    result.check_returncode()
    return result.stdout


def _finish_signed_set(stage: Path, apk_dir: Path, final_lib: bytes,
                       lib_label: str):
    """Repack armeabi split with final_lib, sign all five, verify uniform.

    Returns (signed_dir, deriv_hashes, alignment). Raises on any drift.
    """
    src_split = apk_dir / "split_config.armeabi_v7a.apk"
    with zipfile.ZipFile(src_split) as zin:
        lib_info = next(i for i in zin.infolist() if i.filename == LIB_IN_APK)
        if lib_info.compress_type != zipfile.ZIP_DEFLATED:
            raise ValueError("stock lib entry not DEFLATED; refusing")
    unsigned_split = stage / "split_config.armeabi_v7a-unsigned.apk"
    _repack_unsigned(src_split, unsigned_split, {LIB_IN_APK: final_lib})
    print(f"repacked armeabi_v7a split with {lib_label} "
          "(all other entries faithful: methods preserved, STORED aligned)")

    signed_dir = stage / "apks-signed"
    signed_dir.mkdir()
    deriv_hashes = {}
    for name in ORIGINAL_APKS:
        if name == "split_config.armeabi_v7a.apk":
            src = unsigned_split
        else:
            # Strip original signature for uniform re-sign (faithful).
            tmp = stage / f"{name}.unsigned.apk"
            _repack_unsigned(apk_dir / name, tmp)
            src = tmp
        signed = signed_dir / name
        sign_apk(src, signed, stage / f"signing-{name}.log")
        with zipfile.ZipFile(signed) as z:
            if z.testzip() is not None:
                raise ValueError(f"corrupt zip: {name}")
        # jarsigner rewrites the archive and drops alignment: re-align
        # post-sign (v1 signatures ignore local-header extras; the
        # re-verify below proves it).
        aligned = stage / f"{name}.aligned.apk"
        _repack_unsigned(signed, aligned, keep_meta=True)
        aligned.replace(signed)
        verify = subprocess.run(
            [str(JDK / "bin/jarsigner"), "-verify",
             str(signed)], capture_output=True, text=True, timeout=120)
        if "jar verified." not in verify.stdout:
            raise ValueError(f"post-align verify failed for {name}")
        deriv_hashes[name] = sha256(signed)
        print(f"signed {name} {deriv_hashes[name][:16]}...")

    certs = {name: cert_pem(signed_dir / name) for name in ORIGINAL_APKS}
    if len(set(certs.values())) != 1:
        raise ValueError("split certs differ; refusing (must be one key)")
    print("one project cert across all five splits: "
          + DERIV_CERT_SHA256_FPR)

    alignment = {}
    for name in ORIGINAL_APKS:
        stored, misaligned, deflated = check_zip_alignment(
            signed_dir / name)
        alignment[name] = {
            "stored_entries": stored,
            "stored_misaligned": misaligned,
            "deflated_entries": deflated,
        }
        print(f"alignment {name}: {stored} stored "
              f"({misaligned} misaligned), {deflated} deflated")
        if misaligned:
            raise ValueError(f"{name}: {misaligned} misaligned STORED "
                             "entries; refusing")

    with zipfile.ZipFile(signed_dir / "split_config.armeabi_v7a.apk") as z:
        if hashlib.sha256(z.read(LIB_IN_APK)).hexdigest() != \
                hashlib.sha256(final_lib).hexdigest():
            raise ValueError("signed split lib != assembled lib; refusing")
        with zipfile.ZipFile(
                apk_dir / "split_config.armeabi_v7a.apk") as zorig:
            manifest_orig = zorig.read("AndroidManifest.xml")
        if z.read("AndroidManifest.xml") != manifest_orig:
            raise ValueError("manifest changed; refusing")
    return signed_dir, deriv_hashes, alignment


def do_derivative(stage: Path):
    """Patched lib -> repacked split -> all five splits, one project key."""
    for path in (JDK / "bin/jarsigner", KEYSTORE, STOREPASS_FILE):
        if not path.exists():
            raise ValueError(f"signing input missing: {path}")
    apk_dir, staged, skus = stage_originals(stage)
    lib_path, lib_digest = extract_lib(apk_dir, stage)
    run_verifier(lib_path)

    # Apply the maps patch to the STAGED lib copy (originals untouched).
    patcher = HERE / "patch_maps_ownership.py"
    result = subprocess.run(
        [sys.executable, str(patcher), "--apply", "--no-backup",
         str(lib_path)], capture_output=True, text=True, timeout=60)
    print(result.stdout.strip())
    if result.returncode != 0:
        raise ValueError("patch apply failed: " + result.stderr.strip())
    patched_lib = lib_path.read_bytes()
    patched_digest = hashlib.sha256(patched_lib).hexdigest()
    if patched_digest != PATCHED_LIB_SHA256:
        raise ValueError(
            f"patched lib {patched_digest} != expected {PATCHED_LIB_SHA256}; "
            "refusing (patch footprint drift)")
    print(f"patched lib sha256 {patched_digest} (12-byte footprint, verified)")

    signed_dir, deriv_hashes, alignment = _finish_signed_set(
        stage, apk_dir, patched_lib, "maps-patched lib")

    provenance = {
        "package": PACKAGE,
        "version": VERSION,
        "source_dir": str(SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": lib_digest,
        "original_cert_sha256_fingerprint": ORIGINAL_CERT_SHA256_FPR,
        "derivative_apk_sha256": deriv_hashes,
        "derivative_lib_sha256": patched_digest,
        "derivative_cert_sha256_fingerprint": DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "apk_bytes_modified": True,
        "patch": {
            "tool": "patch_maps_ownership.py",
            "target": "Levels_IsPurchased @0x133014, 16 bytes in place",
            "changed_bytes": 12,
            "original": "80b56f4670f79cebbde8804093f0debd",
            "patched": "80b56f46012080bdbf00bf00bf00bf00",
            "caller_census": ("exactly 3 callers via true PLT 0xa592c: "
                              "UiControlButtonLevelRow::ctor (0x13d2c8), "
                              "UpdateLevelPanelPopulation (0x14bf4c), "
                              "OnClickChallenge (0x158ece); "
                              "Game::LoadLevel enforces nothing "
                              "(tilt round-10 audit; see CENSUS-CORRECTION)"),
            "untouched": [
                "Store_IsItemPurchased predicate (shop stock behavior)",
                "six progression sentinel sites (progression gated)",
                "g_bUnLockAll + Difficulty::Unlock (optional unlock-all)",
                "ads/editor predicates (separate functions)",
                "AndroidManifest.xml (byte-identical)",
            ],
        },
        "signature": ("all five splits re-signed with ONE project key "
                      "(offline-local-developer); ORIGINAL True Axis cert "
                      "NOT retained -> FRESH INSTALL ONLY, never an "
                      "in-place personal-guest update"),
        "ownership_allowlist": skus,
        "progression": "gated normally (six sentinel sites ORIGINAL)",
        "unlock_all": "optional existing store action preserved untouched",
        "stats_seed": "none (fresh install starts at zero)",
        "sound_merge_slot": {
            "base_lib_sha256": lib_digest,
            "maps_patched_lib_sha256": patched_digest,
            "rule": ("combined candidate = sound-patched lib + maps patch "
                     "re-applied ONLY if sound sites are disjoint from "
                     "0x133014-0x133023; verified by byte-diff before "
                     "assembly; maps patch never overwrites sound bytes"),
            "status": ("pending sound worker 630cd122 delivery; sound holds "
                       "sole live slot, no concurrent emulator use"),
        },
        "live_gate": ("install/test ONLY in a root-assigned live slot on an "
                      "isolated fresh candidate with backup; never the "
                      "personal guest without explicit user approval"),
    }
    (stage / "maps-derivative-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {stage / 'maps-derivative-provenance.json'}")
    print("DONE: maps derivative built (fresh-install only set)")


def _variant_provenance(stage, filename, apk_dir, staged, skus,
                        final_lib, label, extra: dict):
    lib_digest = hashlib.sha256(final_lib).hexdigest()
    signed_dir, deriv_hashes, alignment = _finish_signed_set(
        stage, apk_dir, final_lib, label)
    provenance = {
        "package": PACKAGE,
        "version": VERSION,
        "source_dir": str(SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": EXPECTED_LIB_SHA256,
        "original_cert_sha256_fingerprint": ORIGINAL_CERT_SHA256_FPR,
        "variant_lib_sha256": lib_digest,
        "derivative_apk_sha256": deriv_hashes,
        "derivative_cert_sha256_fingerprint": DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "signature": ("all five splits re-signed with ONE project key "
                      "(offline-local-developer); FRESH INSTALL ONLY"),
        "ownership_allowlist": skus,
        "progression": "gated normally (six sentinel sites ORIGINAL)",
        "unlock_all": "optional existing store action preserved untouched",
        "stats_seed": "none (fresh install starts at zero)",
        "live_gate": ("install/test ONLY in a root-assigned live slot on an "
                      "isolated fresh candidate; never the personal guest "
                      "without explicit user approval"),
    }
    provenance.update(extra)
    (stage / filename).write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {stage / filename}")
    return lib_digest


def do_sound_only(stage: Path, sound_lib: Path):
    """SOUND-ONLY 5-split set: sound 4 bytes, NO maps patch.

    For sound630cd122's isolated retest. Chain verified: input must be
    exactly pristine + 4 sound bytes, else refuse.
    """
    for path in (JDK / "bin/jarsigner", KEYSTORE, STOREPASS_FILE):
        if not path.exists():
            raise ValueError(f"signing input missing: {path}")
    if not sound_lib.is_file():
        raise ValueError(f"sound lib missing: {sound_lib}")
    apk_dir, staged, skus = stage_originals(stage)
    lib_path, lib_digest = extract_lib(apk_dir, stage)
    orig_lib = lib_path.read_bytes()
    snd_lib = sound_lib.read_bytes()
    if hashlib.sha256(snd_lib).hexdigest() != SOUND_LIB_SHA256:
        raise ValueError("sound lib hash != staged dfc1a520…; refusing")
    snd_diff = [i for i, (a, b) in enumerate(zip(orig_lib, snd_lib))
                if a != b]
    if snd_diff != [SOUND_SITE + k for k in range(4)]:
        raise ValueError("sound footprint drift; refusing")
    if snd_lib[MAPS_SITE:MAPS_SITE + 16] != MAPS_ORIG:
        raise ValueError("maps site disturbed in sound input; refusing")
    print("sound-only chain verified (4 bytes @0x154a7c, maps site intact)")
    digest = _variant_provenance(
        stage, "maps-sound-only-provenance.json", apk_dir, staged, skus,
        snd_lib, "sound-only lib",
        {"variant": "sound-only (audio NOP @0x154a7c; NO maps patch)"})
    print(f"DONE: sound-only candidate built (lib {digest})")


def do_pristine(stage: Path):
    """PRISTINE-DB86 5-split set: original bytes, project key resign.

    A/B control: identical bytes to stock, only the cert differs. If PLAY
    fails on this set too, the PLAY blocker is environmental, not patch.
    """
    for path in (JDK / "bin/jarsigner", KEYSTORE, STOREPASS_FILE):
        if not path.exists():
            raise ValueError(f"signing input missing: {path}")
    apk_dir, staged, skus = stage_originals(stage)
    lib_path, lib_digest = extract_lib(apk_dir, stage)
    run_verifier(lib_path)
    digest = _variant_provenance(
        stage, "maps-pristine-provenance.json", apk_dir, staged, skus,
        lib_path.read_bytes(), "pristine lib (DB86 resign)",
        {"variant": ("pristine bytes, project-key resign; A/B control "
                     "for PLAY diagnosis")})
    print(f"DONE: pristine-DB86 candidate built (lib {digest})")


def do_combined(stage: Path, sound_lib: Path):
    """Sound-first combined candidate: sound lib + maps 12 bytes.

    Order (per sound-worker protocol): sound worker's staged lib
    (pristine + 4 sound bytes) is the INPUT; the maps patch applies on
    top ONLY after byte-diff proves disjointness. Never overwrites.
    """
    for path in (JDK / "bin/jarsigner", KEYSTORE, STOREPASS_FILE):
        if not path.exists():
            raise ValueError(f"signing input missing: {path}")
    if not sound_lib.is_file():
        raise ValueError(f"sound lib missing: {sound_lib}")
    apk_dir, staged, skus = stage_originals(stage)
    lib_path, lib_digest = extract_lib(apk_dir, stage)
    orig_lib = lib_path.read_bytes()

    snd_lib = sound_lib.read_bytes()
    if hashlib.sha256(snd_lib).hexdigest() != SOUND_LIB_SHA256:
        raise ValueError("sound lib hash != staged dfc1a520…; refusing")
    # Chain: orig -> sound must be EXACTLY the 4 sound bytes.
    snd_diff = [i for i, (a, b) in enumerate(zip(orig_lib, snd_lib))
                if a != b]
    if snd_diff != [SOUND_SITE, SOUND_SITE + 1,
                    SOUND_SITE + 2, SOUND_SITE + 3]:
        raise ValueError(f"sound footprint drift: {[hex(d) for d in snd_diff]}")
    if orig_lib[SOUND_SITE:SOUND_SITE + 4] != SOUND_ORIG:
        raise ValueError("orig sound site != 4ff7aeea; refusing")
    if snd_lib[SOUND_SITE:SOUND_SITE + 4] != SOUND_PATCHED:
        raise ValueError("sound site not bf00bf00; refusing")
    # Disjointness: maps site + sentinels untouched by sound.
    if snd_lib[MAPS_SITE:MAPS_SITE + 16] != MAPS_ORIG:
        raise ValueError("sound lib already differs at maps site; refusing")
    for off in SENTINEL_SITES:
        if snd_lib[off:off + 2] != bytes.fromhex("c06b"):
            raise ValueError(f"sentinel {off:#x} disturbed; refusing")
    print("chain orig->sound verified (4 bytes @0x154a7c); "
          "maps site + sentinels intact")

    combined = bytearray(snd_lib)
    combined[MAPS_SITE:MAPS_SITE + 16] = MAPS_PATCHED
    combined = bytes(combined)
    combined_digest = hashlib.sha256(combined).hexdigest()
    full_diff = [i for i, (a, b) in enumerate(zip(orig_lib, combined))
                 if a != b]
    if len(full_diff) != 16 or set(full_diff) != (
            set(snd_diff) | set(range(MAPS_SITE + 4, MAPS_SITE + 16))):
        raise ValueError("combined footprint != 4 sound + 12 maps; refusing")
    print(f"combined lib sha256 {combined_digest} "
          "(4 sound + 12 maps bytes, verified)")
    (stage / "libtrueaxis-combined.so").write_bytes(combined)

    signed_dir, deriv_hashes, alignment = _finish_signed_set(
        stage, apk_dir, combined, "combined sound+maps lib")

    provenance = {
        "package": PACKAGE,
        "version": VERSION,
        "source_dir": str(SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": lib_digest,
        "original_cert_sha256_fingerprint": ORIGINAL_CERT_SHA256_FPR,
        "sound_lib_sha256": SOUND_LIB_SHA256,
        "sound_site": {"offset": hex(SOUND_SITE), "orig": "4ff7aeea",
                       "patched": "bf00bf00"},
        "maps_patch": {"offset": hex(MAPS_SITE), "changed_bytes": 12},
        "combined_lib_sha256": combined_digest,
        "combined_lib_changed_bytes": 16,
        "derivative_apk_sha256": deriv_hashes,
        "derivative_cert_sha256_fingerprint": DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "order": ("sound-first: sound lib (pristine + 4B) as input, maps "
                  "12B applied after disjointness proof; nothing overwritten"),
        "signature": ("all five splits re-signed with ONE project key; "
                      "FRESH INSTALL ONLY"),
        "ownership_allowlist": skus,
        "progression": "gated normally (six sentinel sites ORIGINAL)",
        "unlock_all": "optional existing store action preserved untouched",
        "stats_seed": "none (fresh install starts at zero)",
        "live_gate": ("install/test ONLY in a root-assigned live slot on an "
                      "isolated fresh candidate; never the personal guest "
                      "without explicit user approval"),
    }
    (stage / "maps-combined-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {stage / 'maps-combined-provenance.json'}")
    print("DONE: combined sound+maps candidate built")


def _apply_bytes_gated(lib: bytearray, offset: int, orig: str,
                        patched: str, label: str):
    original, replacement = bytes.fromhex(orig), bytes.fromhex(patched)
    if lib[offset:offset + len(original)] != original:
        raise ValueError(f"{label} @{offset:#x}: unexpected bytes; refusing")
    lib[offset:offset + len(replacement)] = replacement
    print(f"  {label} @{offset:#x} ({len(replacement)} B)")


def _apply_v3_sites(lib: bytearray):
    for site in V3_SITES:
        _apply_bytes_gated(lib, site["offset"], site["orig"],
                            site["patched"], "audio-v3 " + site["label"])


def _apply_sentinels(lib: bytearray):
    for off in SENTINEL_SITES:
        if lib[off:off + 2] != SENTINEL_ORIG \
                or lib[off + 2:off + 4] != SENTINEL_CONTEXT:
            raise ValueError(f"sentinel @{off:#x}: context mismatch; refusing")
    for off in SENTINEL_SITES:
        lib[off:off + 2] = SENTINEL_PATCHED
    print(f"  six progression sentinels patched")


def _run_seed_patcher(lib_path: Path):
    result = subprocess.run(
        [sys.executable, str(SEED_PATCHER), "--apply", "--no-backup",
         str(lib_path)], capture_output=True, text=True, timeout=120)
    print(result.stdout.strip())
    if result.returncode != 0:
        raise ValueError("seed patch failed: " + result.stderr.strip())


def _manifest_version_code(apk: Path) -> int:
    """Read versionCode from a binary AndroidManifest.xml (stdlib only)."""
    import struct as _struct
    with zipfile.ZipFile(apk) as z:
        raw = z.read("AndroidManifest.xml")
    if raw[0:2] != b"\x03\x00":
        raise ValueError(f"{apk.name}: not a binary manifest")
    scount = _struct.unpack_from("<I", raw, 16)[0]
    sstart = _struct.unpack_from("<I", raw, 28)[0]
    strings = []
    for i in range(scount):
        string_off = _struct.unpack_from("<I", raw, 36 + 4 * i)[0]
        pos = 8 + sstart + string_off
        length = _struct.unpack_from("<H", raw, pos)[0]
        strings.append(raw[pos + 2:pos + 2 + 2 * length].decode("utf-16-le"))
    off = 8
    while off < len(raw):
        ctype, _, csize = _struct.unpack_from("<HHI", raw, off)
        if ctype == 0x0102:
            _, name = _struct.unpack_from("<II", raw, off + 16)
            if strings[name] == "manifest":
                _, _, acount = _struct.unpack_from("<HHH", raw, off + 24)
                base = off + 16 + 20
                for i in range(acount):
                    a = base + i * 20
                    _, aname, _, _, _, dtype, data = \
                        _struct.unpack_from("<IIIHBBI", raw, a)
                    if strings[aname] == "versionCode":
                        if dtype != 0x10:
                            raise ValueError("versionCode not INT; refusing")
                        return data
                raise ValueError("versionCode attr absent; refusing")
        off += csize
    raise ValueError("manifest element absent; refusing")


def _cert_der_hex(apk: Path) -> str:
    """Leaf-cert DER hex (uppercase) from the APK v1 signature block."""
    import tempfile as _tempfile
    with zipfile.ZipFile(apk) as z:
        names = [n for n in z.namelist()
                 if n.upper().startswith("META-INF/") and n.upper().endswith(
                     (".RSA", ".DSA", ".EC"))]
        if len(names) != 1:
            raise ValueError(f"{apk.name}: expected 1 sig block, "
                             f"found {len(names)}")
        block = z.read(names[0])
    with _tempfile.TemporaryDirectory() as tmp:
        der_in = Path(tmp) / "sig.der"
        pem_out = Path(tmp) / "certs.pem"
        der_in.write_bytes(block)
        first = subprocess.run(
            ["openssl", "pkcs7", "-inform", "DER", "-in", str(der_in),
             "-print_certs", "-outform", "PEM", "-out", str(pem_out)],
            capture_output=True, text=True, timeout=60)
        if first.returncode != 0 or not pem_out.is_file():
            raise ValueError(f"{apk.name}: openssl cert extract failed")
        import re as _re
        blocks = _re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            pem_out.read_text(encoding="utf-8", errors="replace"),
            _re.DOTALL)
        if len(blocks) != 1:
            raise ValueError(f"{apk.name}: ambiguous cert chain "
                             f"({len(blocks)} certs); refusing")
        leaf = Path(tmp) / "leaf.pem"
        leaf.write_text(blocks[0] + "\n", encoding="utf-8")
        der_out = Path(tmp) / "cert.der"
        second = subprocess.run(
            ["openssl", "x509", "-in", str(leaf),
             "-outform", "DER", "-out", str(der_out)],
            capture_output=True, text=True, timeout=60)
        if second.returncode != 0 or not der_out.is_file():
            raise ValueError(f"{apk.name}: openssl cert decode failed")
        return der_out.read_bytes().hex().upper()


def write_variant_descriptor(stage: Path, signed_dir: Path, name: str) -> dict:
    """Emit the executor-compatible variant descriptor (contract API).

    Verifies from FILES (hashes recomputed, cert openssl-extracted per
    split with uniformity enforced, versionCode parsed from the binary
    manifest) so the descriptor can never contradict the artifact.
    """
    splits = ["base.apk", "split_config.armeabi_v7a.apk",
              "split_config.en.apk", "split_config.es.apk",
              "split_config.xhdpi.apk"]
    hashes = {}
    for split in splits:
        candidate = signed_dir / split
        if not candidate.is_file():
            raise ValueError(f"descriptor: missing split {split}")
        hashes[split] = sha256(candidate)
    return write_variant_descriptor_to(
        stage / "variant-descriptor.json", signed_dir, name, hashes)


def _write_frozen_record(stage: Path, variant: str, lib_digest: str,
                         deriv_hashes: dict, alignment: dict):
    record = {
        "frozen": True,
        "builder": "compose_maps_candidate.py faithful+aligned repack",
        "cert": DERIV_CERT_SHA256_FPR,
        "alignment": alignment,
        "record": {"variant": variant, "apk_sha256": deriv_hashes,
                   "lib_sha256": lib_digest},
    }
    (stage / "FROZEN.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {stage / 'FROZEN.json'}")


def _assemble_layered_lib(orig_lib: bytes, with_sentinels: bool) -> bytes:
    """orig -> v3 audio -> 12B levels -> (seed applied by caller)."""
    lib = bytearray(orig_lib)
    _apply_v3_sites(lib)
    if hashlib.sha256(bytes(lib)).hexdigest() != V3_LIB_SHA256:
        raise ValueError("v3 intermediate != 85020c64...; refusing (chain)")
    print("v3 intermediate verified (85020c64...)")
    if lib[MAPS_SITE:MAPS_SITE + 16] != MAPS_ORIG:
        raise ValueError("maps site disturbed; refusing")
    lib[MAPS_SITE:MAPS_SITE + 16] = MAPS_PATCHED
    print("12B levels gate applied (site bytes gated)")
    if with_sentinels:
        _apply_sentinels(lib)
    return bytes(lib)


def _assert_seed_footprint(orig_v3_12b: bytes, seeded: bytes):
    diff = [i for i, (a, b) in enumerate(zip(orig_v3_12b, seeded)) if a != b]
    branch = set(range(SEED_BRANCH_AT, SEED_BRANCH_AT + 4))
    pad = set(range(SEED_PAD_LO, SEED_PAD_HI))
    if not diff or not set(diff) <= (branch | pad):
        raise ValueError("seed footprint outside hook+pad; refusing")
    if not set(branch) <= set(diff):
        raise ValueError("seed branch missing; refusing")
    print(f"seed footprint verified ({len(diff)} B: hook + LOAD0 pad only)")


def do_ownership(stage: Path, *, skip_seed: bool = False):
    """NORMAL ownership candidate: v3 audio + 12B levels (+ per-SKU seed).

    Default-safe: progression stays gated (sentinels ORIGINAL), store
    unlock-all untouched, no auto-unlock anywhere. skip_seed omits the
    populateStore hook (live-blocked 2026-09-13: translator SIGILL/SEGV
    class on file-slack stub variants — see patch_maps_seed.py history;
    12B gameplay/progression stays proven).
    """
    for path in (JDK / "bin/jarsigner", KEYSTORE, STOREPASS_FILE):
        if not path.exists():
            raise ValueError(f"signing input missing: {path}")
    apk_dir, staged, skus = stage_originals(stage)
    lib_path, lib_digest = extract_lib(apk_dir, stage)
    run_verifier(lib_path)
    layered = _assemble_layered_lib(lib_path.read_bytes(), False)
    lib_path.write_bytes(layered)
    if skip_seed:
        print("seed SKIPPED (--no-seed: hook live-blocked, documented)")
        seeded = layered
    else:
        _run_seed_patcher(lib_path)
        seeded = lib_path.read_bytes()
        _assert_seed_footprint(layered, seeded)
    signed_dir, deriv_hashes, alignment = _finish_signed_set(
        stage, apk_dir, seeded, "ownership (v3+12B+seed) lib")
    version_code = _manifest_version_code(signed_dir / "base.apk")
    if version_code != 29:
        raise ValueError("built manifest versionCode != 29; refusing")
    lib_digest = hashlib.sha256(seeded).hexdigest()
    provenance = {
        "package": PACKAGE,
        "version": VERSION,
        "source_dir": str(SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": EXPECTED_LIB_SHA256,
        "original_cert_sha256_fingerprint": ORIGINAL_CERT_SHA256_FPR,
        "variant": ("normal ownership (sound v3 + Levels 12B"
                    + ("" if skip_seed else " + per-SKU populateStore seed")
                    + "; progression gated)"),
        "v3_audio_sites": [
            {"offset": hex(s["offset"]), "orig": s["orig"],
             "patched": s["patched"]} for s in V3_SITES],
        "v3_intermediate_lib_sha256": V3_LIB_SHA256,
        "ownership_12b": {"offset": hex(MAPS_SITE), "changed_bytes": 12},
        "seed_hook": ({"tool": "patch_maps_seed.py",
                       "site": hex(SEED_BRANCH_AT),
                       "skus_seeded": len(skus)} if not skip_seed else
                      {"status": "OMITTED (--no-seed: file-slack stub "
                                 "live-blocked by ndk_translation; shop "
                                 "display gap remains open)"}),
        "variant_lib_sha256": lib_digest,
        "derivative_apk_sha256": deriv_hashes,
        "derivative_cert_sha256_fingerprint": DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "signature": ("all five splits re-signed with ONE project key "
                      "(offline-local-developer); FRESH INSTALL ONLY"),
        "ownership_allowlist": skus,
        "progression": "gated normally (six sentinel sites ORIGINAL)",
        "unlock_all": "optional; NOT in this variant (see --unlock-all)",
        "stats_seed": "none (fresh install starts at zero)",
        "live_gate": ("install/test ONLY in a root-assigned live slot; "
                      "never the personal guest without explicit approval"),
    }
    (stage / "maps-ownership-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {stage / 'maps-ownership-provenance.json'}")
    _write_frozen_record(stage, "ownership-normal", lib_digest,
                         deriv_hashes, alignment)
    write_variant_descriptor(stage, signed_dir, "normal")
    print("DONE: ownership (normal) candidate built")


def do_unlock_all(stage: Path, *, skip_seed: bool = False):
    """OPT-IN unlock-all variant: ownership layers + six sentinels.

    Explicit mode only (never a default): same key, same versionCode,
    same pipeline; installed solely on recorded user consent via the
    reviewer-owned choice UI + executor. Medals/records keep earning
    normally (display code untouched); locks resume from earned values
    on reinstall of the normal variant (sentinel revert = byte restore).
    """
    for path in (JDK / "bin/jarsigner", KEYSTORE, STOREPASS_FILE):
        if not path.exists():
            raise ValueError(f"signing input missing: {path}")
    apk_dir, staged, skus = stage_originals(stage)
    lib_path, lib_digest = extract_lib(apk_dir, stage)
    run_verifier(lib_path)
    layered = _assemble_layered_lib(lib_path.read_bytes(), True)
    lib_path.write_bytes(layered)
    if skip_seed:
        print("seed SKIPPED (--no-seed: hook live-blocked, documented)")
        seeded = layered
    else:
        _run_seed_patcher(lib_path)
        seeded = lib_path.read_bytes()
        _assert_seed_footprint(layered, seeded)
    for off in SENTINEL_SITES:
        if seeded[off:off + 2] != SENTINEL_PATCHED:
            raise ValueError(f"sentinel @{off:#x} lost after seed; refusing")
    label = ("unlock-all (v3+12B+sentinels) lib" if skip_seed
             else "unlock-all (v3+12B+seed+sentinels) lib")
    signed_dir, deriv_hashes, alignment = _finish_signed_set(
        stage, apk_dir, seeded, label)
    version_code = _manifest_version_code(signed_dir / "base.apk")
    if version_code != 29:
        raise ValueError("built manifest versionCode != 29; refusing")
    lib_digest = hashlib.sha256(seeded).hexdigest()
    provenance = {
        "package": PACKAGE,
        "version": VERSION,
        "source_dir": str(SOURCE_DIR),
        "original_apk_sha256": staged,
        "original_lib_sha256": EXPECTED_LIB_SHA256,
        "original_cert_sha256_fingerprint": ORIGINAL_CERT_SHA256_FPR,
        "variant": ("OPT-IN unlock-all (sound v3 + Levels 12B"
                    + ("" if skip_seed else " + per-SKU seed")
                    + " + six progression sentinels); explicit consent "
                      "only, never a default"),
        "v3_audio_sites": [
            {"offset": hex(s["offset"]), "orig": s["orig"],
             "patched": s["patched"]} for s in V3_SITES],
        "v3_intermediate_lib_sha256": V3_LIB_SHA256,
        "ownership_12b": {"offset": hex(MAPS_SITE), "changed_bytes": 12},
        "seed_hook": ({"tool": "patch_maps_seed.py",
                       "site": hex(SEED_BRANCH_AT),
                       "skus_seeded": len(skus)} if not skip_seed else
                      {"status": "OMITTED (--no-seed: file-slack stub "
                                 "live-blocked by ndk_translation)"}),
        "sentinels": [{"offset": hex(o)} for o in SENTINEL_SITES],
        "sentinel_revert": ("restore c06b at the six sites + reinstall: "
                            "locks resume exactly from earned medals"),
        "variant_lib_sha256": lib_digest,
        "derivative_apk_sha256": deriv_hashes,
        "derivative_cert_sha256_fingerprint": DERIV_CERT_SHA256_FPR,
        "derivative_alignment": alignment,
        "signature": ("all five splits re-signed with ONE project key "
                      "(offline-local-developer); FRESH INSTALL ONLY, "
                      "same-key reinstall over existing install keeps saves"),
        "ownership_allowlist": skus,
        "progression": "OPEN in this variant only (sentinels patched)",
        "unlock_all": "this IS the opt-in variant (explicit consent only)",
        "stats_seed": "none (fresh install starts at zero; earned medals "
                      "record normally)",
        "live_gate": ("isolated slot acceptance per reviewer contract "
                      "before any user install"),
    }
    (stage / "maps-unlockall-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {stage / 'maps-unlockall-provenance.json'}")
    _write_frozen_record(stage, "unlock-all", lib_digest,
                         deriv_hashes, alignment)
    write_variant_descriptor(stage, signed_dir, "unlock-all")
    print("DONE: unlock-all (opt-in) variant built")


def do_describe(apks_dir: Path, name: str, out: Path):
    """Emit an executor-compatible descriptor for an EXISTING signed set.

    Verifies from files (hashes, uniform DB86 cert, versionCode 29) and
    writes ONLY the new descriptor file (never mutates the set).
    """
    if out.exists():
        raise ValueError(f"refusing to overwrite: {out}")
    signed = apks_dir
    splits = ["base.apk", "split_config.armeabi_v7a.apk",
              "split_config.en.apk", "split_config.es.apk",
              "split_config.xhdpi.apk"]
    for split in splits:
        if not (signed / split).is_file():
            raise ValueError(f"describe: missing split {split}")
    descriptor = write_variant_descriptor_to(
        out, signed, name,
        {s: sha256(signed / s) for s in splits})
    print(f"described {name} from {signed}")
    return descriptor


def write_variant_descriptor_to(out: Path, signed_dir: Path, name: str,
                                hashes: dict) -> dict:
    """Descriptor core shared by builders and --describe."""
    if name not in ("normal", "unlock-all"):
        raise ValueError(f"unknown variant role: {name!r}")
    fps = {_cert_der_hex(signed_dir / s) for s in hashes}
    if len(fps) != 1:
        raise ValueError("descriptor: split certs differ; refusing")
    der_hex = fps.pop()
    fingerprint = ":".join(
        f"{b:02X}" for b in hashlib.sha256(
            bytes.fromhex(der_hex)).digest())
    if fingerprint != DERIV_CERT_SHA256_FPR:
        raise ValueError("descriptor: not the offline DB86 key; refusing")
    version_code = _manifest_version_code(signed_dir / "base.apk")
    if version_code != 29:
        raise ValueError("descriptor: versionCode != 29; refusing")
    descriptor = {
        "name": name,
        "apks_dir": str(signed_dir.resolve()),
        "hashes": dict(hashes),
        "cert_fingerprint": fingerprint,
        "cert_der_hex": der_hex,
        "version_code": version_code,
    }
    out.write_text(json.dumps(descriptor, indent=2) + "\n",
                   encoding="utf-8")
    print(f"wrote {out} ({name}, versionCode 29, DB86)")
    return descriptor


def main():
    parser = argparse.ArgumentParser(
        description="Maps candidate staging and derivative builder.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stage", type=Path,
                       help="New empty dir: bit-identical staging")
    group.add_argument("--derivative", type=Path,
                       help="New empty dir: signed patched 5-split set")
    group.add_argument("--combined", type=Path,
                       help="New empty dir: signed sound+maps 5-split set")
    group.add_argument("--sound-only", type=Path,
                       help="New empty dir: signed sound-only 5-split set")
    group.add_argument("--pristine", type=Path,
                       help="New empty dir: signed pristine-DB86 5-split set")
    group.add_argument("--ownership", type=Path,
                       help="New empty dir: NORMAL variant "
                            "(v3 audio + 12B levels + per-SKU seed; "
                            "progression gated)")
    group.add_argument("--unlock-all", type=Path,
                       help="New empty dir: OPT-IN unlock-all variant "
                            "(ownership layers + six sentinels; explicit "
                            "consent only, never a default)")
    group.add_argument("--describe", type=Path, default=None,
                       help="Existing signed 5-split dir: verify from files "
                            "and emit a descriptor (with --as + --out)")
    parser.add_argument("--sound-lib", type=Path, default=None,
                        help="Sound worker staged lib (required w/ --combined)")
    parser.add_argument("--no-seed", action="store_true",
                        help="Omit the per-SKU seed hook (live-blocked; "
                             "12B levels gate still applied)")
    parser.add_argument("--as", dest="role", default=None,
                        help="Descriptor role for --describe: normal|unlock-all")
    parser.add_argument("--out", dest="out", type=Path, default=None,
                        help="Descriptor output path for --describe "
                             "(must not exist)")
    args = parser.parse_args()

    if args.describe is not None:
        if args.role not in ("normal", "unlock-all") or args.out is None:
            print("ERROR: --describe requires --as normal|unlock-all "
                  "and --out <new-file>")
            return 2
        try:
            do_describe(args.describe, args.role, args.out)
        except (ValueError, subprocess.CalledProcessError) as error:
            print(f"ERROR: {error}")
            return 1
        return 0

    target = (args.stage or args.derivative or args.combined
              or args.sound_only or args.pristine or args.ownership
              or args.unlock_all)
    if target.exists():
        print(f"ERROR: refusing to use existing path: {target}")
        return 2
    if not SOURCE_DIR.is_dir():
        print(f"ERROR: original source dir missing: {SOURCE_DIR}")
        return 2
    try:
        if args.stage:
            do_stage(args.stage)
        elif args.derivative:
            do_derivative(args.derivative)
        elif args.pristine:
            do_pristine(args.pristine)
        elif args.sound_only:
            if args.sound_lib is None:
                print("ERROR: --sound-only requires --sound-lib")
                return 2
            do_sound_only(args.sound_only, args.sound_lib)
        elif args.ownership:
            do_ownership(args.ownership, skip_seed=args.no_seed)
        elif args.unlock_all:
            do_unlock_all(args.unlock_all, skip_seed=args.no_seed)
        else:
            if args.sound_lib is None:
                print("ERROR: --combined requires --sound-lib")
                return 2
            do_combined(args.combined, args.sound_lib)
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Personal-game deployment planner: offline validation + scoped live steps.

User scope Sept 14: ALL levels unlocked from the start — the canonical
deploy variant is ``unlock-all`` (live-proven: Hurricane tap→detail→PLAY;
normal genuinely gates). OFFLINE (default, no device contact): validates
the canonical variant record against the files on disk (hashes, uniform
DB86 cert via openssl, versionCode pin, expected native bytes incl. the
six sentinel sites for unlock-all), then prints the exact live command
sequence for review. NEVER touches the device, guests, saves, configs,
Steam, or installer state.

LIVE steps (backup / deploy / rollback) run ONLY with
  PERSONAL_DEPLOY_SLOT=granted  +  --live
and perform no uninstall/clear/wipe at any point. Rollback reinstalls
the live-pulled pre-upgrade splits and runs ONLY on explicit
`--rollback` invocation (scoped failure recovery), never automatically.

No runner/progression_choice edits; needed code changes go via root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "linux-launcher"))
from progression_choice import (  # noqa: E402
    EXPECTED_CERT_FINGERPRINT, EXPECTED_SPLITS, EXPECTED_VERSION_CODE,
    PACKAGE, extract_apk_cert,
)

VARIANTS_ROOT = ROOT / "staging" / "progression-variants"
SOURCE_DIRS = {
    "normal": ROOT / "staging" / "maps-ownership-v1",
    "unlock-all": ROOT / "staging" / "maps-unlockall-v1",
}
# Native byte pins (structural presence only; deep review is d853's lane).
LIB_MEMBER = "lib/armeabi-v7a/libtrueaxis.so"
SOUND_V3 = {"0x154a7c": "00bf00bf", "0x11e318": "00bf00bf00bf",
            "0x154a1c": "704700bf"}
MAPS_PATCH = {"0x133014": "80b56f46012080bd"}
# Six progression lock-predicate sites: c06b (gated) in normal,
# 0020 (open) in unlock-all. Live-proven functional on unlock-all
# (Hurricane tap->detail->PLAY); normal genuinely gates (locked dialog).
SENTINEL_SITES = (0x133046, 0x13D846, 0x13D944, 0x14BFBC, 0x14C10A, 0x14C11C)
SENTINEL_GATED, SENTINEL_OPEN = "c06b", "0020"
FORBIDDEN_TOKENS = ("uninstall", "pm clear", "wipe", "adb kill-server")


class DeployError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_record(variant: str) -> tuple[dict, Path]:
    root = VARIANTS_ROOT / variant
    record_path = root / "variant.json"
    if not record_path.is_file():
        raise DeployError(f"variant not published: {record_path}")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise DeployError(f"unreadable variant record: {error}")
    if not isinstance(record, dict):
        raise DeployError("malformed variant record")
    return record, root


def validate_offline(variant: str) -> dict:
    """Full offline pin validation. Returns evidence dict. Raises DeployError."""
    record, root = load_record(variant)
    apks_dir = root / str(record.get("apks_dir", "apks-signed"))
    evidence_hashes = {}
    for split in EXPECTED_SPLITS:
        candidate = apks_dir / split
        if not candidate.is_file():
            raise DeployError(f"missing split file: {candidate}")
        actual = sha256_file(candidate)
        expected = (record.get("hashes", {}) or {}).get(split, "")
        if not expected or actual != expected:
            raise DeployError(f"hash mismatch: {split}")
        evidence_hashes[split] = actual
    fps = {}
    for split in EXPECTED_SPLITS:
        fingerprint, _ = extract_apk_cert(apks_dir / split)
        fps[split] = fingerprint
    if len(set(fps.values())) != 1:
        raise DeployError("split certs differ across the set")
    if next(iter(fps.values())) != EXPECTED_CERT_FINGERPRINT:
        raise DeployError("variant cert is not the offline DB86 key")
    try:
        version = int(record.get("version_code", 0) or 0)
    except (TypeError, ValueError):
        raise DeployError("malformed version_code")
    if version != EXPECTED_VERSION_CODE:
        raise DeployError("variant version is not the reinstall pin (29)")
    # Source cross-pin (publication claims byte-identical copies).
    source = SOURCE_DIRS.get(variant)
    if source is not None:
        for split in EXPECTED_SPLITS:
            src_file = source / "apks-signed" / split
            if src_file.is_file() and sha256_file(src_file) != evidence_hashes[split]:
                raise DeployError(f"canonical differs from source: {split}")
    # Native structural pins (read-only presence checks).
    with __import__("zipfile").ZipFile(apks_dir / "split_config.armeabi_v7a.apk") as z:
        lib = z.read(LIB_MEMBER)
    pins = {}
    for off_hex, expect_hex in {**SOUND_V3, **MAPS_PATCH}.items():
        off = int(off_hex, 16)
        actual = lib[off:off + len(bytes.fromhex(expect_hex))].hex()
        pins[off_hex] = actual == expect_hex
        if actual != expect_hex:
            raise DeployError(f"native pin absent at {off_hex}")
    # Sentinel identity: unlock-all open (0020 x6), normal gated (c06b x6).
    # Wrong sentinel state for the claimed variant is a hard refusal.
    expect_sentinel = (SENTINEL_OPEN if variant == "unlock-all"
                       else SENTINEL_GATED)
    for off in SENTINEL_SITES:
        actual = lib[off:off + 2].hex()
        pins[hex(off)] = actual == expect_sentinel
        if actual != expect_sentinel:
            raise DeployError(
                f"sentinel state mismatch at {hex(off)} for {variant}")
    return {"variant": variant, "apks_dir": str(apks_dir),
            "hashes": evidence_hashes,
            "cert_fingerprint": EXPECTED_CERT_FINGERPRINT,
            "version_code": version, "native_pins": pins}


def live_commands(evidence: dict, backup_dir: str) -> list[str]:
    """Exact live command sequence (review artifact; executed only under grant)."""
    adb = "$SDK/platform-tools/adb -P 5038 -s 127.0.0.1:5595"
    splits = " ".join(f'"$CAND/{s}"' for s in EXPECTED_SPLITS)
    commands = [
        f"# 0. host backup (emulator STOPPED; read-only source):",
        f'cp -a "$AVDHOME" "{backup_dir}/avdhome" && sha256sum "{backup_dir}/avdhome/hardened_api28.ini"',
        f"# 1. pre-install evidence (abort unless cert==DB86 and version==29):",
        f'{adb} shell pm path {PACKAGE} | tee "{backup_dir}/pm-path-before.txt"',
        f'{adb} shell dumpsys package {PACKAGE} | grep -E "versionCode|signatures" | tee "{backup_dir}/dumpsys-before.txt"',
        f'for s in {" ".join(EXPECTED_SPLITS)}; do {adb} pull "$(grep "$s$" "{backup_dir}/pm-path-before.txt" | sed \'s/^package://\')" "{backup_dir}/installed-$s" || echo "absent: $s"; done',
        f'python3 -c "pull+fingerprint each installed split; abort unless all present splits == DB86 and version==29"',
        f"# 2. same-key reinstall (preserves app data; reinstall-only):",
        f'{adb} install-multiple -r --no-streaming {splits}',
        f"# 3. post-verify (all 5 pulled+hashed vs record; version; cert):",
        f'{adb} shell pm path {PACKAGE} && {adb} shell dumpsys package {PACKAGE} | grep -E "versionCode|signatures"',
        f"# 4. user gates: launch, PLAY smoke, audible check (user-driven).",
        f"# ROLLBACK (explicit scoped recovery ONLY):",
        f'{adb} install-multiple -r --no-streaming "{backup_dir}/installed-"*.apk && re-run step 3',
    ]
    if evidence["variant"] == "unlock-all":
        commands.insert(
            len(commands) - 3,
            "# 3b. unlock user-test route (live-proven Sept 13): PLAY -> HARD "
            "tab -> The Hurricane (PLATFORMING-C) -> tap must open detail "
            "-> PLAY must drive gameplay (red padlock icon is stale "
            "cosmetic); no medal attempts.")
    return commands


def check_grant() -> None:
    if os.environ.get("PERSONAL_DEPLOY_SLOT") != "granted":
        raise DeployError("live steps refused: PERSONAL_DEPLOY_SLOT != granted")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="unlock-all",
                        choices=("normal", "unlock-all"),
                        help="canonical deploy variant (Sept 14: unlock-all "
                             "is the default; all levels open from start)")
    parser.add_argument("--explicit-variant", action="store_true",
                        help="required to plan anything but unlock-all "
                             "(normal is kept as tested fallback only)")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args(argv)
    if args.variant != "unlock-all" and not args.explicit_variant:
        raise DeployError("non-default variant requires --explicit-variant")
    evidence = validate_offline(args.variant)
    print(json.dumps({"offline": "VALIDATED", **{k: v for k, v in evidence.items()
                                                 if k != "hashes"}}, indent=2))
    for line in live_commands(evidence, args.backup_dir or "<BACKUP_DIR>"):
        print(line)
    for token in FORBIDDEN_TOKENS:
        blob = "\n".join(live_commands(evidence, "x")).lower()
        if token in blob:
            raise DeployError(f"forbidden token in emitted plan: {token}")
    if args.live or args.rollback:
        check_grant()
        print("LIVE grant present; device steps execute only in the scoped "
              "deploy window (not in this offline task).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeployError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)

#!/usr/bin/env python3
"""Unlock-all-levels default policy (host side, offline; user scope Sept 14).

ALL levels are unlocked from the start: there is no selector, no options
entry, no stored-choice default to defend. ``POLICY_DEFAULT_CHOICE`` is
``unlock-all``: absent/malformed/unknown choice content always reads back
as unlock-all, and legacy switch intents are NEVER consumed into installs
(``apply_pending_progression_switch`` retires them with a recorded error
and proceeds — no auto-revert is possible). Variant upgrades are explicit
helper operations (``run_switch`` directly), never intent-driven. This
module owns:

- intent-file retirement: any unconsumed intent is consumed-with-error
  and ignored; the device is never touched on the intent path;
- user-facing labels (retained for receipts/diagnostics only);
- a safe same-key split-update executor (``run_switch``) bound to an
- user-facing labels, including an explicit restart-required notice;
- a safe same-key split-update executor (``run_switch``) bound to an
  OWNED session (runner-provided serial/adb/locks/emulator identity):
  re-checks ownership, re-hashes target files against the plan binding,
  pulls the ACTUAL installed splits for the same-key gate (dumpsys
  signer tokens are opaque IDs, never cert material), installs via
  exact ``install-multiple -r --no-streaming`` split paths, then
  classifies the device by mutation stage with mandatory re-query:
  target-verified (receipt), prior-retained (verified, no receipt),
  or unknown (human decision); receipt-write failure reconciles instead
  of blind-reinstalling. No uninstall/clear/wipe/kill exists here.

Records-preservation contract: a switch plan may only ever be a same-cert,
same-version ``adb install -r`` reinstall over the existing install. Any
plan containing uninstall/clear/wipe (or an install without ``-r``) is
rejected, so earned medals/records cannot be wiped by construction.

MAPS_CONTRACT (maps/candidate owner, e.g. worker 88beb9a5 — descriptors ONLY):
  - Provide unlock-all variant descriptors satisfying VARIANT_CONTRACT:
    five signed splits + provenance/FROZEN-style record with per-file
    sha256, the offline DB86 cert, and versionCode 29. Never edit this
    module's files.
  - End-to-end still needs real variant artifacts + a root-assigned live
    slot; until then TRANSACTION_STATUS stays non-final and nothing here
    is labeled functional end-to-end.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

CHOICE_NORMAL = "normal"
CHOICE_UNLOCK_ALL = "unlock-all"
CHOICES = (CHOICE_NORMAL, CHOICE_UNLOCK_ALL)

# User scope Sept 14: ALL levels unlocked from the start, no selector.
# The installed/upgrade default is unlock-all; "normal" survives only as
# a receipt vocabulary value for the already-tested normal build.
POLICY_DEFAULT_CHOICE = CHOICE_UNLOCK_ALL

SCHEMA_VERSION = 1
CHOICE_FILENAME = "progression-choice.json"

# Executor implemented + fake-tested here; end-to-end still needs real
# variant artifacts + a root-assigned live slot (see MAPS_CONTRACT).
TRANSACTION_STATUS = "executor-implemented; end-to-end pending"

# Cert + version pin: only the offline developer key may update the install,
# and only at the same versionCode (reinstall, never upgrade/downgrade).
EXPECTED_CERT_FINGERPRINT = (
    "DB:86:A5:6E:99:37:04:51:84:A3:B5:B8:E3:64:E3:0F:"
    "53:33:A3:0A:FD:A1:27:39:E1:1E:71:78:B4:AD:33:ED"
)
EXPECTED_VERSION_CODE = 29
EXPECTED_SPLITS = (
    "base.apk",
    "split_config.armeabi_v7a.apk",
    "split_config.en.apk",
    "split_config.es.apk",
    "split_config.xhdpi.apk",
)
PACKAGE = "com.trueaxis.jetcarstunts2"
RECEIPT_FILENAME = "progression-install-receipt.json"

# Records-preservation contract: these operations must never appear in a plan.
FORBIDDEN_OPS = ("uninstall", "pm clear", "wipe", "wipe-data", "factory-reset")
# Closed world: any other op is refused (never silently ignored).
KNOWN_OPS = frozenset({"verify-target", "confirm-ownership",
                       "adb-install", "verify-installed"})

CHOICE_LABELS = {
    CHOICE_NORMAL: "Normal progression (medals unlock levels)",
    CHOICE_UNLOCK_ALL: "Unlock all levels (optional)",
}
RESTART_NOTICE = (
    "Changing the progression choice requires a game restart: "
    "it takes effect the next time the game is launched. "
    "Earned medals and records are preserved."
)


class ChoiceError(Exception):
    pass


class TransactionError(Exception):
    """Actionable failure. ``outcome`` classifies the device state when
    known: retained-verified | mutated-unknown | verified-unrecorded |
    refused (nothing attempted). ``detail`` carries evidence for reconcile.
    No blanket retention claim is made without re-query evidence."""

    def __init__(self, reason: str, *, expected: str = "", actual: str = "",
                 outcome: str = "refused", detail: dict | None = None):
        detail_text = reason
        if expected or actual:
            detail_text += f" (expected {expected!r}, got {actual!r})"
        super().__init__(detail_text)
        self.reason = reason
        self.expected = expected
        self.actual = actual
        self.outcome = outcome
        self.detail = detail or {}


def default_choice_path() -> Path:
    """Project-local intent file (same state root as tilt settings)."""
    from runtime_paths import resolve_paths
    return resolve_paths().logdir / CHOICE_FILENAME


def read_choice(path: Path | str | None = None) -> str:
    """Return the stored choice; policy default is unlock-all.

    Absent file, malformed JSON, non-dict, or unknown value all yield
    unlock-all (user scope Sept 14: no selector; unlock is the default).
    A stored "normal" value is returned as-is for receipt honesty but no
    longer drives any install (intents are retired; see apply_pending).
    """
    target = Path(path) if path else default_choice_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return POLICY_DEFAULT_CHOICE
    if not isinstance(data, dict):
        return POLICY_DEFAULT_CHOICE
    choice = data.get("choice")
    return choice if choice in CHOICES else POLICY_DEFAULT_CHOICE


def set_choice(path: Path | str, choice: str, *, explicit: bool) -> str:
    """Persist a choice change. Changing selection is an explicit user
    action: ``explicit`` must be True or the change is refused and the
    stored file is left untouched. Every explicit change arms a fresh,
    unconsumed switch intent (consumed once by the runner integration);
    files predating intents carry none and never trigger a switch."""
    if choice not in CHOICES:
        raise ChoiceError(f"unknown progression choice: {choice!r}")
    if not explicit:
        raise ChoiceError("refusing non-explicit progression-choice change")
    target = Path(path)
    payload = {
        "schema": SCHEMA_VERSION,
        "choice": choice,
        "explicit": True,
        "updated_utc": int(time.time()),
        "switch_intent": {"id": f"{int(time.time())}-{os.getpid()}",
                          "consumed": False, "error": ""},
    }
    _atomic_write_json(target, payload)
    return choice


def _atomic_write_json(target: Path, payload: dict) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".pc-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def read_intent(path: Path | str | None = None) -> dict | None:
    """Return the unconsumed switch intent, or None (no intent, already
    consumed, or legacy file without one — never auto-switch)."""
    target = Path(path) if path else default_choice_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    intent = data.get("switch_intent")
    if not isinstance(intent, dict) or intent.get("consumed", True):
        return None
    return {"id": intent.get("id", ""), "choice": data.get("choice")}


def mark_intent_consumed(path: Path | str | None, *, error: str = "") -> None:
    """Consume the pending intent (success or recorded failure) so a
    failed switch is never auto-retried at the next launch."""
    target = Path(path) if path else default_choice_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or not isinstance(data.get("switch_intent"), dict):
        return
    data["switch_intent"]["consumed"] = True
    data["switch_intent"]["error"] = error
    _atomic_write_json(target, data)


# ---------------------------------------------------------------------------
# Install-transaction contract: verification + executor (this module's).
# Variant artifact composition stays maps-owned (descriptors ONLY).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VariantDescriptor:
    """One installable candidate variant.

    ``apks_dir`` holds exactly the five signed splits; ``hashes`` maps
    split filename -> sha256 hex of the file on disk; ``cert_fingerprint``
    is the DER-cert fingerprint string; ``version_code`` must equal
    EXPECTED_VERSION_CODE so the switch is a reinstall, not an
    upgrade/downgrade (which could threaten app data).
    """
    name: str  # CHOICE_NORMAL or CHOICE_UNLOCK_ALL
    apks_dir: str
    hashes: dict = field(default_factory=dict)
    cert_fingerprint: str = ""
    version_code: int = 0


VARIANT_CONTRACT = {
    "status": TRANSACTION_STATUS,
    "executor": "progression_choice.run_switch (this module; fake-tested)",
    "composer": "maps-owned (variant artifacts + descriptors ONLY)",
    "requires": [
        "composer stages staging/progression-variants/<choice>/variant.json "
        "({apks_dir, hashes, cert_fingerprint, version_code}) + five signed splits",
        "five signed splits with per-file sha256 record",
        f"openssl-extracted cert fingerprint == {EXPECTED_CERT_FINGERPRINT}",
        f"versionCode == {EXPECTED_VERSION_CODE}",
        "device truth from pulled APK bytes (pm path + pull + openssl), "
        "never dumpsys signer tokens (opaque IDs, logged only)",
        "same cert + same version as installed (same-key reinstall only)",
        "owned session: serial + held locks (flock-held, not file-exists) + "
        "cmdline-verified emulator + game not started; crashpad never matches",
        "forbidden ops never planned; unknown ops refused; install args exact",
        "success reported only after post-install pull verification",
    ],
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_variant(desc: VariantDescriptor, *, cert_reader=None) -> dict:
    """Raise TransactionError (actionable) unless the variant is complete,
    hash-clean, correctly certified, and version-pinned. Returns the
    evidence gathered (per-split hashes + real cert material).

    Cert evidence comes from the FILES (openssl-extracted DER), never from
    the descriptor claim alone: a lying ``cert_fingerprint`` is caught by
    comparing claim vs extracted bytes.
    """
    if desc.name not in CHOICES:
        raise TransactionError("unknown variant name",
                               expected="|".join(CHOICES), actual=desc.name)
    root = Path(desc.apks_dir)
    for split in EXPECTED_SPLITS:
        candidate = root / split
        if not candidate.is_file():
            raise TransactionError(f"variant missing split: {split}",
                                   expected=str(root), actual="absent")
        actual_hash = sha256_file(candidate)
        expected_hash = (desc.hashes or {}).get(split, "")
        if not expected_hash or actual_hash != expected_hash:
            raise TransactionError(f"variant hash mismatch: {split}",
                                   expected=expected_hash or "<recorded>",
                                   actual=actual_hash)
    reader = cert_reader or extract_apk_cert
    real_fingerprint, real_der_hex = reader(root / EXPECTED_SPLITS[0])
    for split in EXPECTED_SPLITS[1:]:
        other_fp, _ = reader(root / split)
        if other_fp != real_fingerprint:
            raise TransactionError(f"variant split cert differs: {split}",
                                   expected=real_fingerprint, actual=other_fp)
    if real_fingerprint != EXPECTED_CERT_FINGERPRINT:
        raise TransactionError("variant cert not the offline key (file evidence)",
                               expected=EXPECTED_CERT_FINGERPRINT,
                               actual=real_fingerprint)
    if desc.cert_fingerprint and desc.cert_fingerprint != real_fingerprint:
        raise TransactionError("descriptor cert claim disagrees with file evidence",
                               expected=real_fingerprint,
                               actual=desc.cert_fingerprint)
    if desc.version_code != EXPECTED_VERSION_CODE:
        raise TransactionError("variant version mismatch (not a reinstall)",
                               expected=str(EXPECTED_VERSION_CODE),
                               actual=str(desc.version_code))
    return {"hashes": {s: sha256_file(root / s) for s in EXPECTED_SPLITS},
            "cert_fingerprint": real_fingerprint,
            "cert_der_hex": real_der_hex}


def extract_apk_cert(apk_path: Path) -> tuple:
    """Return (colon-fingerprint, DER-hex) of the v1 signing cert found in
    the APK, using stdlib zip + openssl only. Raises TransactionError when
    no signature block exists or openssl is unavailable."""
    rsa_name = None
    try:
        with zipfile.ZipFile(apk_path) as archive:
            for name in archive.namelist():
                upper = name.upper()
                if upper.startswith("META-INF/") and upper.endswith(
                        (".RSA", ".DSA", ".EC")):
                    rsa_name = name
                    break
            if rsa_name is None:
                raise TransactionError("APK has no v1 signature block",
                                       expected="META-INF/*.RSA",
                                       actual=str(apk_path))
            block = archive.read(rsa_name)
    except zipfile.BadZipFile:
        raise TransactionError("APK is not a valid zip", actual=str(apk_path))
    openssl = shutil.which("openssl")
    if openssl is None:
        raise TransactionError("openssl unavailable for cert extraction",
                               expected="openssl on PATH", actual="absent")
    with tempfile.TemporaryDirectory() as tmp:
        block_path = Path(tmp) / "sigblock.der"
        pem_path = Path(tmp) / "certs.pem"
        der_path = Path(tmp) / "cert.der"
        block_path.write_bytes(block)
        first = subprocess.run(
            [openssl, "pkcs7", "-inform", "DER", "-in", str(block_path),
             "-print_certs", "-outform", "PEM", "-out", str(pem_path)],
            capture_output=True, text=True, timeout=60)
        if first.returncode != 0 or not pem_path.is_file():
            raise TransactionError("openssl could not extract APK certs",
                                   actual=(first.stderr or "")[-300:])
        pem_text = pem_path.read_text(encoding="utf-8", errors="replace")
        blocks = re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            pem_text, re.DOTALL)
        # Pin the leaf (first) cert only when exactly one is present;
        # a chain/multiple signers is refused as ambiguous.
        if len(blocks) != 1:
            raise TransactionError("ambiguous APK cert chain (not a single cert)",
                                   expected="1 certificate", actual=str(len(blocks)))
        leaf_path = Path(tmp) / "leaf.pem"
        leaf_path.write_text(blocks[0] + "\n", encoding="utf-8")
        second = subprocess.run(
            [openssl, "x509", "-in", str(leaf_path),
             "-outform", "DER", "-out", str(der_path)],
            capture_output=True, text=True, timeout=60)
        if second.returncode != 0 or not der_path.is_file():
            raise TransactionError("openssl could not decode APK cert",
                                   actual=(second.stderr or "")[-300:])
        der = der_path.read_bytes()
    fingerprint = ":".join(f"{byte:02X}" for byte in hashlib.sha256(der).digest())
    return fingerprint, der.hex().upper()


@dataclass(frozen=True)
class OwnedSession:
    """Positively-owned install target, built by the runner from its live
    state (never invented here). The emulator is EXPECTED alive (adb
    install needs a booted guest); safety comes from ownership proof —
    held lock files + cmdline/env-verified emulator identity + serial
    scoping — plus foreign/game exclusion, not from a zero-emulator rule.
    This module never signals, kills, or reaps any process."""
    serial: str                      # e.g. "127.0.0.1:5595"
    adb_prefix: tuple = ("adb",)     # e.g. (adb_bin, "-P", "5038")
    avd_name: str = ""
    console_port: int = 0
    endpoint_lock: str = ""          # lock file path (held by owner)
    profile_lock: str = ""           # lock file path (held by owner)
    game_started: bool = False       # True once the game is launched


def lock_held_state(path: str | Path) -> bool:
    """True iff a lock file exists AND is currently held (non-blocking
    flock probe). File existence alone means nothing — preserved lock
    files sit unheld between runs. Never creates the file."""
    import fcntl
    target = Path(path)
    if not target.is_file():
        return False
    try:
        fd = os.open(target, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True  # held by someone (possibly ourselves) -> held
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


_EMULATOR_BINARIES = frozenset({
    "qemu-system-x86_64", "qemu-system-x86_64-headless",
    "qemu-system-i386", "emulator", "emulator64-x86", "emulator64-arm",
})


def find_owned_emulator(avd_name: str, console_port: int,
                        *, proc_root: str = "/proc") -> int | None:
    """Return the pid of the emulator running OUR avd/ports, or None.
    Identity = emulator-binary basename (crashpad_handler and friends can
    never match) + `-avd <name>` + console port in the same cmdline.
    Read-only; never signals anything."""
    root = Path(proc_root)
    if not root.is_dir():
        return None
    try:
        pids = [entry for entry in root.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    for pid_entry in pids:
        try:
            raw = (pid_entry / "cmdline").read_bytes()
        except OSError:
            continue
        parts = raw.decode("utf-8", "replace").split("\0")
        if not parts or not parts[0]:
            continue
        binary = os.path.basename(parts[0])
        if binary not in _EMULATOR_BINARIES:
            continue  # crashpad, adb, shells, launchers all excluded here
        flat = " ".join(parts)
        if f"-avd {avd_name}" in flat and str(console_port) in flat:
            return int(pid_entry.name)
    return None


def check_session(session: OwnedSession, *, proc_root: str = "/proc") -> dict:
    """Ownership facts for an install target. All must hold."""
    locks = [p for p in (session.endpoint_lock, session.profile_lock) if p]
    held = {p: lock_held_state(p) for p in locks}
    emulator_pid = (find_owned_emulator(session.avd_name, session.console_port,
                                        proc_root=proc_root)
                    if session.avd_name and session.console_port else None)
    return {"serial_scoped": bool(session.serial),
            "locks_held": bool(locks) and all(held.values()),
            "lock_detail": held,
            "emulator_owned_pid": emulator_pid,
            "emulator_owned": emulator_pid is not None,
            "game_not_started": not session.game_started}


def assert_records_safe(steps: list) -> None:
    """Records-preservation contract: closed op world (unknown ops refused,
    never ignored); no wipe-family text anywhere; adb-install must be
    exactly ``install-multiple -r --no-streaming`` + the five split paths
    (supported arguments enforced, extras refused). Same-cert +
    same-version reinstall keeps app data."""
    if not isinstance(steps, list) or not steps:
        raise TransactionError("refusing malformed plan (not a step list)",
                               expected="plan_switch output",
                               actual=type(steps).__name__)
    for step in steps:
        op = step.get("op", "?")
        if op not in KNOWN_OPS:
            raise TransactionError(f"refusing unknown plan op: {op}",
                                   expected=f"one of {sorted(KNOWN_OPS)}",
                                   actual=op)
        text = json.dumps(step).lower()
        flat_args = " ".join(str(arg) for arg in step.get("args", [])).lower()
        haystacks = (text, flat_args, str(op).lower())
        for forbidden in FORBIDDEN_OPS:
            if any(forbidden in hay for hay in haystacks):
                raise TransactionError(
                    f"plan step violates records contract: {forbidden}",
                    expected="same-key reinstall only", actual=op)
    installs = [s for s in steps if s.get("op") == "adb-install"]
    if len(installs) != 1:
        raise TransactionError("refusing plan with unexpected install shape",
                               expected="exactly one adb-install",
                               actual=str(len(installs)))
    args = installs[0].get("args", [])
    tails = [str(a) for a in args[3:]]
    if (len(args) != 3 + len(EXPECTED_SPLITS)
            or args[:3] != ["install-multiple", "-r", "--no-streaming"]
            or [os.path.basename(t) for t in tails] != list(EXPECTED_SPLITS)):
        raise TransactionError("refusing plan with unsupported install arguments",
                               expected="install-multiple -r --no-streaming <5 exact splits>",
                               actual=" ".join(str(a) for a in args))


def plan_switch(current: VariantDescriptor, target: VariantDescriptor,
                *, cert_reader=None) -> list:
    """Build the ordered, records-safe switch plan as data. Target file
    hashes are BOUND into the plan; the executor re-hashes and refuses on
    any drift (no TOCTOU swap between plan and execute). Raises
    TransactionError on any failed check. Performs NO live action."""
    if read_only_choice_guard(current, target):
        raise TransactionError("refusing switch: identical variant",
                               expected="distinct target", actual=target.name)
    evidence = verify_variant(target, cert_reader=cert_reader)
    if evidence["cert_fingerprint"] != (current.cert_fingerprint or evidence["cert_fingerprint"]):
        raise TransactionError("cert change across switch (would force uninstall)",
                               expected=current.cert_fingerprint,
                               actual=evidence["cert_fingerprint"])
    if target.version_code != current.version_code:
        raise TransactionError("version change across switch (not a reinstall)",
                               expected=str(current.version_code),
                               actual=str(target.version_code))
    splits = [str(Path(target.apks_dir) / name) for name in EXPECTED_SPLITS]
    steps = [
        {"op": "verify-target", "variant": target.name, "dir": target.apks_dir,
         "cert_fingerprint": evidence["cert_fingerprint"],
         "bound_hashes": evidence["hashes"]},
        {"op": "confirm-ownership",
         "note": "locks held + owned emulator + game not started (executor checks)"},
        {"op": "adb-install",
         "args": ["install-multiple", "-r", "--no-streaming", *splits],
         "note": "same-key reinstall only; app data (medals/records) kept"},
        {"op": "verify-installed", "variant": target.name,
         "via": ["pm path", "pull", "dumpsys package"],
         "bound_hashes": evidence["hashes"],
         "cert_der_hex": evidence["cert_der_hex"],
         "version_code": target.version_code},
    ]
    assert_records_safe(steps)
    return steps


def read_only_choice_guard(current: VariantDescriptor, target: VariantDescriptor) -> bool:
    """True when a switch would be a no-op (same variant both sides)."""
    return current.name == target.name


def _norm_hex(value: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", value or "").upper()


# Captured API-28 reality (analysis/maps-live-20260913/dumpsys-package.log):
#   versionCode=29 minSdk=15 targetSdk=28
#   splits=[base, config.armeabi_v7a, config.en, config.es, config.xhdpi]
#   signatures=PackageSignatures{b41e49e version:1, signatures:[2b86a8d5], ...}
# The bracket token is an opaque signer ID, NOT DER material: it is
# recorded for logging only and NEVER compared to certificate bytes.
# Cert truth comes from pulled APK bytes + the openssl extractor (B1).
def parse_dumpsys_package(text: str) -> dict:
    """Parse versionCode + opaque signer token from real dumpsys output."""
    version = ""
    token = ""
    version_match = re.search(r"versionCode=(\d+)", text or "")
    if version_match:
        version = version_match.group(1)
    sig_match = re.search(r"signatures=PackageSignatures\{([^}]*)\}", text or "")
    if sig_match:
        token = sig_match.group(1).strip()
    return {"version_code": version, "signer_token": token}


def _pull_binary(cmd: list, staged_dir: Path, *, env: dict | None = None):
    """Run an adb pull with the local staging path inside ``staged_dir``.

    ``cmd`` already ends with the REMOTE path; the local staging path is
    appended — never dropping the remote (default_transport bug, fixed).
    Returns (returncode, payload). The staging file is always removed.
    """
    try:
        fd, local = tempfile.mkstemp(dir=str(staged_dir), prefix="pulled-",
                                     suffix=".apk")
        os.close(fd)
    except OSError:
        return 127, b""
    try:
        # adb pull creates the local file itself: remove the placeholder
        # so a success-without-bytes still reads as a staging failure
        # (127), exactly like the legacy fixed-name path.
        try:
            os.unlink(local)
        except OSError:
            return 127, b""
        try:
            # Full cmd already ends with the REMOTE path
            # ([adb-prefix…, -s, serial, pull, remote]); append the
            # local staging path — never drop the remote.
            result = subprocess.run(
                [*cmd, local], capture_output=True, text=True,
                timeout=300, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return 127, b""
        if result.returncode != 0:
            return result.returncode, b""
        try:
            return 0, Path(local).read_bytes()
        except OSError:
            return 127, b""
    finally:
        try:
            os.unlink(local)
        except OSError:
            pass


def default_transport(cmd: list, *, env: dict | None = None,
                      binary: bool = False, scratch_dir=None):
    """Real adb transport (used only when root assigns a live slot; tests
    inject fakes). ``cmd`` is the full argv starting with ``adb``.
    ``binary=True`` is for pull payloads. Returns (returncode, payload).

    Binary payloads are staged on disk (adb pull needs a local path):
    inside ``scratch_dir`` when given (production callers pass the
    evidence staging dir, which lives in explicit task/app-state space),
    else the system temp dir. A 162 MB base.apk staging area must never
    be assumed from global /tmp (seen full live: quota failure
    masqueraded as a device pull error). An explicitly requested but
    unusable ``scratch_dir`` is a LOUD staging failure (127, empty) —
    never a silent fallback onto known-full /tmp; the evidence layer
    turns it into a worded TransactionError before any adb call.
    """
    if binary:
        if scratch_dir is not None:
            try:
                Path(scratch_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                return 127, b""
            return _pull_binary(cmd, Path(scratch_dir), env=env)
        with tempfile.TemporaryDirectory() as tmp:
            return _pull_binary(cmd, Path(tmp), env=env)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=300, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, f"transport failure: {error}"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _adb_base(session: OwnedSession) -> list:
    return [*session.adb_prefix, "-s", session.serial]


def pull_installed_evidence(session: OwnedSession, *, transport=None,
                            env: dict | None = None, pull_dir=None,
                            cert_reader=None, scratch_dir=None) -> dict:
    """Pull the ACTUAL installed splits and fingerprint their bytes + certs
    (B1). Returns prior evidence for the same-key gate and later reconcile.
    Package absent -> {installed: False} (fresh install path).

    Evidence (pulled APK bytes, including the ~162 MB base) is staged in
    ``pull_dir`` when given, else a timestamped dir beside the install
    receipt (explicit app-state space) — never bare global /tmp, which
    has been observed full live. Binary transport staging is tied to the
    same location via ``scratch_dir`` (default: the evidence staging
    dir), not to global env assumptions.
    """
    run = transport or default_transport
    base = _adb_base(session)
    code, pm_out = run([*base, "shell", "pm", "path", PACKAGE], env=env)
    remotes = {}
    if code == 0:
        for line in (pm_out or "").splitlines():
            if "package:" in line:
                remote = line.split("package:", 1)[1].strip()
                if remote:
                    remotes[os.path.basename(remote)] = remote
    code, dump_out = run([*base, "shell", "dumpsys", "package", PACKAGE],
                         env=env)
    parsed = parse_dumpsys_package(dump_out) if code == 0 else {
        "version_code": "", "signer_token": ""}
    if not remotes:
        return {"installed": False, "pm_paths": {}, "pulled": {},
                "version_code": parsed["version_code"],
                "signer_token": parsed["signer_token"]}
    reader = cert_reader or extract_apk_cert
    if pull_dir is not None:
        staging = Path(pull_dir)
    else:
        staging = (default_receipt_path().parent
                   / f"pull-{int(time.time())}")
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TransactionError("evidence staging unavailable",
                               expected=f"writable dir at {staging}",
                               actual=str(error))
    if scratch_dir is not None:
        try:
            Path(scratch_dir).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise TransactionError("binary scratch unavailable",
                                   expected=f"writable dir at {scratch_dir}",
                                   actual=str(error))
    scratch = str(scratch_dir) if scratch_dir is not None else str(staging)
    pulled = {}
    for name, remote in sorted(remotes.items()):
        code, payload = run([*base, "pull", remote], env=env, binary=True,
                            scratch_dir=scratch)
        if code != 0 or not payload:
            raise TransactionError(f"could not pull installed split: {name}",
                                   expected="adb pull success",
                                   actual=f"exit {code}")
        local = staging / f"installed-{name}"
        local.write_bytes(payload)
        fingerprint, _ = reader(local)
        pulled[name] = {"sha256": sha256_file(local),
                        "cert_fingerprint": fingerprint}
    return {"installed": True, "pm_paths": remotes, "pulled": pulled,
            "version_code": parsed["version_code"],
            "signer_token": parsed["signer_token"]}


def classify_device(pulled: dict, prior: dict | None, target_hashes: dict,
                    target_cert: str) -> str:
    """Reconcile pulled-installed evidence: 'target' (device holds the
    planned bytes), 'prior' (device still holds the pre-install bytes),
    or 'unknown' (anything else — human decision required)."""
    current_hashes = {name: info["sha256"] for name, info in pulled.items()}
    current_certs = {info["cert_fingerprint"] for info in pulled.values()}
    if (current_hashes == target_hashes and current_certs == {target_cert}
            and set(current_hashes) == set(target_hashes)):
        return "target"
    if prior is not None:
        prior_hashes = {name: info["sha256"] for name, info in prior.items()}
        if current_hashes == prior_hashes and current_hashes:
            return "prior"
    return "unknown"


def default_receipt_path() -> Path:
    from runtime_paths import resolve_paths
    return resolve_paths().logdir / RECEIPT_FILENAME


def read_receipt(path: Path | str | None = None) -> dict | None:
    target = Path(path) if path else default_receipt_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_receipt(receipt: dict, path: Path | str | None = None) -> Path:
    target = Path(path) if path else default_receipt_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".receipt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def choice_status(choice_path: Path | str | None = None,
                  receipt_path: Path | str | None = None) -> dict:
    """Stored intent vs verified-installed truth. The UI must show
    ``pending_restart`` (and must NOT advertise a successful switch) until
    a verified receipt matches the stored choice."""
    stored = read_choice(choice_path)
    receipt = read_receipt(receipt_path) or {}
    installed = receipt.get("variant", "")
    verified = bool(receipt.get("verified")) and installed in CHOICES
    return {"stored": stored,
            "installed": installed if verified else "",
            "verified": verified,
            "pending_restart": verified and installed != stored,
            "receipt_utc": receipt.get("installed_utc", 0)}


def default_variants_root() -> Path:
    """Repo staging area where the maps composer publishes variant sets:
    <root>/<choice>/variant.json + five signed splits."""
    return Path(__file__).resolve().parents[1] / "staging" / "progression-variants"


def variant_source(choice: str, variants_root: Path | str | None = None):
    """Locate staged variant artifacts for a choice. Returns
    (VariantDescriptor|None, reason). Absent artifacts disable the action
    clearly — never an error, never an implicit fallback install."""
    if choice not in CHOICES:
        return None, f"unknown choice {choice!r}"
    root = (Path(variants_root) if variants_root else default_variants_root()) / choice
    record_path = root / "variant.json"
    if not record_path.is_file():
        return None, f"variant not staged (no {record_path})"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"unreadable variant record: {error}"
    if not isinstance(record, dict):
        return None, "malformed variant record (not an object)"
    apks_dir = root / str(record.get("apks_dir", "apks-signed"))
    try:
        version_code = int(record.get("version_code", 0) or 0)
    except (TypeError, ValueError):
        return None, "malformed variant record (version_code)"
    desc = VariantDescriptor(
        name=choice, apks_dir=str(apks_dir),
        hashes=dict(record.get("hashes", {}) or {}),
        cert_fingerprint=str(record.get("cert_fingerprint", "")),
        version_code=version_code)
    return desc, ""


def current_variant_from_receipt(receipt_path: Path | str | None = None,
                                 variants_root: Path | str | None = None):
    """Rebuild the installed-side descriptor for same-key cross-checks.
    Unverified/absent receipt -> (None, reason); the caller then only
    allows paths the device evidence supports (never assumes)."""
    receipt = read_receipt(receipt_path) or {}
    installed = receipt.get("variant", "")
    if not receipt.get("verified") or installed not in CHOICES:
        return None, "no verified install receipt"
    desc, reason = variant_source(installed, variants_root)
    if desc is None:
        return None, f"installed variant artifacts unavailable: {reason}"
    return desc, ""


def apply_pending_progression_switch(
        session: OwnedSession, *, choice_path=None, receipt_path=None,
        variants_root=None, transport=None, env=None,
        ownership: dict | None = None, pull_dir=None,
        proc_root: str = "/proc", cert_reader=None, scratch_dir=None):
    """Runner prelaunch hook (intents RETIRED under unlock-default policy).

    Returns (action, message) with action in proceeded|aborted. Rules
    (user scope Sept 14: no selector; upgrades are explicit helper
    operations via ``run_switch``, never intent-driven):
    - no unconsumed intent -> ("proceeded", "no pending intent");
    - ANY unconsumed intent (legacy normal, fresh unlock-all, anything) ->
      consume with recorded error, PROCEED with zero device interaction.
      Nothing is installed, verified, or launched differently: no stale
      normal intent can ever auto-revert an upgraded install, and no
      fresh intent can auto-install. Runner contract unchanged, so no
      runner.py edit is required (quit-worker lane untouched).
    """
    intent = read_intent(choice_path)
    if intent is None:
        return "proceeded", "no pending progression intent"
    mark_intent_consumed(
        choice_path,
        error="choice intents retired under unlock-default policy "
              "(Sept 14); upgrades are explicit helper operations")
    return ("proceeded",
            "legacy progression intent retired (unlock is the default); "
            "no install attempted, launching current install unchanged")


def run_switch(plan: list, session: OwnedSession, *, transport=None,
               env: dict | None = None, ownership: dict | None = None,
               receipt_path: Path | str | None = None,
               pull_dir=None, cert_reader=None,
               proc_root: str = "/proc", scratch_dir=None) -> dict:
    """Execute a planned same-key variant switch against an OWNED session.

    Stage machine (mutation-aware, B3): untrusted-plan refusal and
    ownership proof come before any live action; target files are
    re-hashed against the plan binding (B5); pre-install device evidence
    comes from PULLED bytes (B1); after any install attempt the device is
    re-queried and the outcome classified — 'target' (receipt written),
    'prior' (retained, verified by re-query), or 'unknown' (human
    decision; never auto-retried, intent already consumed by the caller).
    A receipt that cannot be written after verification raises
    verified-unrecorded with the evidence attached for reconcile — never a
    blind reinstall (B4). No uninstall/clear/wipe exists anywhere here,
    and no rollback is implemented or promised.
    """
    assert_records_safe(plan)  # untrusted-plan refusal before any live action
    install_steps = [s for s in plan if s.get("op") == "adb-install"]
    verify_steps = [s for s in plan if s.get("op") == "verify-installed"]
    if len(install_steps) != 1 or len(verify_steps) != 1:
        raise TransactionError("refusing plan with unexpected install shape",
                               expected="exactly one adb-install + one verify-installed",
                               actual=f"{len(install_steps)}+{len(verify_steps)}")
    install, verify = install_steps[0], verify_steps[0]
    bound_hashes = dict(verify.get("bound_hashes", {}))
    if set(bound_hashes) != set(EXPECTED_SPLITS):
        raise TransactionError("refusing plan without bound target hashes",
                               expected="bound_hashes for all 5 splits",
                               actual=sorted(bound_hashes))
    facts = dict(ownership) if ownership is not None else check_session(
        session, proc_root=proc_root)
    if "emulator_owned" not in facts:
        facts["emulator_owned"] = facts.get("emulator_owned_pid") is not None
    facts.pop("emulator_owned_pid", None)
    blocking = [name for name, ok in facts.items()
                if name != "lock_detail" and not ok]
    if blocking:
        raise TransactionError("ownership proof failed at execute time",
                               expected="serial + held locks + owned emulator + game stopped",
                               actual=f"blocked: {blocking}")
    target_dir = install["args"][-5:]
    live_hashes = {}
    for split, arg in zip(EXPECTED_SPLITS, target_dir):
        candidate = Path(arg)
        if not candidate.is_file() or sha256_file(candidate) != bound_hashes.get(split):
            raise TransactionError("target files drifted since planning (TOCTOU)",
                                   expected=bound_hashes.get(split, "<bound>"),
                                   actual=split)
        live_hashes[split] = bound_hashes[split]
    target_cert = str(verify.get("cert_der_hex", ""))
    target_fp = _fp_of(target_cert)
    expected_version = str(verify.get("version_code", ""))
    reader = cert_reader or extract_apk_cert
    _, live_der = reader(Path(target_dir[0]))
    if _norm_hex(live_der) != _norm_hex(target_cert):
        raise TransactionError("target cert drifted since planning",
                               expected="(plan-bound cert)", actual="drifted")
    run = transport or default_transport
    base = _adb_base(session)
    scratch = scratch_dir if scratch_dir is not None else pull_dir
    prior = pull_installed_evidence(session, transport=run, env=env,
                                    pull_dir=pull_dir, cert_reader=reader,
                                    scratch_dir=scratch)
    if prior["installed"]:
        if prior["version_code"] != expected_version:
            raise TransactionError("installed version differs (not a reinstall)",
                                   expected=expected_version,
                                   actual=prior["version_code"] or "<unknown>")
        prior_certs = {info["cert_fingerprint"]
                       for info in prior["pulled"].values()}
        if prior_certs != {target_fp}:
            raise TransactionError("installed cert differs (update would fail)",
                                   expected=target_fp,
                                   actual=sorted(prior_certs))
    prior_pulled = prior["pulled"] if prior["installed"] else None
    code, out = run([*base, *install["args"]], env=env)
    if code != 0:
        # Install attempt failed: state UNVERIFIED (not "retained").
        # Re-query before saying anything about the prior install (B3).
        after = _best_effort_pull(session, run, env, pull_dir, reader,
                                  scratch)
        outcome = classify_device(after.get("pulled", {}), prior_pulled,
                                  live_hashes, _fp_of(target_cert))
        if outcome == "prior":
            raise TransactionError(
                "install failed; re-query verifies the prior install is retained",
                expected="exit 0 + Success",
                actual=(out or "")[-300:] or f"exit {code}",
                outcome="retained-verified")
        raise TransactionError(
            "install failed and re-query could not verify the prior install; "
            "device state UNKNOWN — reconcile from evidence, do not assume",
            expected="exit 0 + Success",
            actual=(out or "")[-300:] or f"exit {code}",
            outcome="mutated-unknown",
            detail={"requeried": sorted(after.get("pulled", {}))})
    post = pull_installed_evidence(session, transport=run, env=env,
                                   pull_dir=pull_dir, cert_reader=reader,
                                   scratch_dir=scratch)
    outcome = classify_device(post["pulled"], prior_pulled,
                              live_hashes, _fp_of(target_cert))
    if outcome != "target" or post["version_code"] != expected_version:
        again = _best_effort_pull(session, run, env, pull_dir, reader,
                                  scratch)
        outcome = classify_device(again.get("pulled", {}), prior_pulled,
                                  live_hashes, _fp_of(target_cert))
        if outcome == "target" and again.get("version_code") == expected_version:
            post = again
        elif outcome == "prior":
            raise TransactionError(
                "install reported success but the device still holds the prior "
                "bytes (verified by re-query); prior install retained",
                outcome="retained-verified")
        else:
            raise TransactionError(
                "install reported success but post-install evidence does not "
                "match the plan; device state UNKNOWN — NOT reporting success",
                outcome="mutated-unknown",
                detail={"requeried": sorted(again.get("pulled", {}))})
    receipt = {"schema": SCHEMA_VERSION,
               "variant": verify.get("variant", ""),
               "verified": True,
               "installed_utc": int(time.time()),
               "fresh_install": not prior["installed"],
               "version_code": post["version_code"],
               "installed_pulled_hashes": {
                   name: info["sha256"] for name, info in post["pulled"].items()},
               "cert_der_hex": _norm_hex(target_cert),
               "device_signer_token": post.get("signer_token", "")}
    try:
        write_receipt(receipt, receipt_path)
    except OSError as error:
        # Verified on-device but unrecorded (B4): re-query to confirm what
        # is actually installed, attach everything for manual reconcile,
        # and NEVER blind-reinstall.
        confirm = _best_effort_pull(session, run, env, pull_dir, reader,
                                    scratch)
        state = classify_device(confirm.get("pulled", {}), prior_pulled,
                                live_hashes, _fp_of(target_cert))
        raise TransactionError(
            f"install verified on device but the receipt could not be written "
            f"({error}); re-query shows: {state}. Reconcile from this evidence; "
            f"do not blind-reinstall.",
            outcome=f"verified-unrecorded:{state}",
            detail={"receipt": receipt,
                    "requeried": sorted(confirm.get("pulled", {}))})
    return receipt


def _fp_of(target_cert_der_hex: str) -> str:
    """Fingerprint string matching pull evidence form (colon-hex)."""
    der = _norm_hex(target_cert_der_hex)
    raw = bytes.fromhex(der) if der else b""
    if not raw:
        return ""
    import hashlib as _hashlib
    return ":".join(f"{byte:02X}" for byte in _hashlib.sha256(raw).digest())


def _best_effort_pull(session, run, env, pull_dir, reader,
                      scratch_dir=None) -> dict:
    try:
        return pull_installed_evidence(session, transport=run, env=env,
                                       pull_dir=pull_dir, cert_reader=reader,
                                       scratch_dir=scratch_dir)
    except TransactionError:
        return {"installed": False, "pulled": {}, "version_code": ""}


def show_progression_choice(choice_path: Path | str | None = None,
                            availability: dict | None = None,
                            receipt_path: Path | str | None = None) -> str | None:
    """RETIRED panel (user scope Sept 14: no selector; unwired from the
    controls UI). Retained for API/test compatibility; do not wire it
    into any user flow. Persists ONLY via set_choice(explicit=True).

    ``availability`` maps choice -> (enabled, reason): when the unlock-all
    artifacts are absent/unverifiable the action is DISABLED with a clear
    reason instead of a selectable option. A status line reports stored
    intent vs verified-installed truth and never advertises a switch
    before verification (see choice_status).

    Display-independent: tkinter is imported lazily so unit tests can stub it.
    """
    import tkinter as tk
    current = read_choice(choice_path)
    window = tk.Tk(className='JCS2Progression')
    window.title('JCS2 Progression')
    window.geometry('1280x800')
    window.configure(bg='#16202c')
    window.attributes('-fullscreen', True)
    foreground, background = '#ffffff', '#16202c'
    tk.Label(window, text='JCS2 Progression', font=('sans', 30, 'bold'),
             fg=foreground, bg=background).pack(pady=(40, 12))
    tk.Label(window, text='Choose how levels unlock.',
             font=('sans', 20), fg=foreground, bg=background).pack(pady=10)
    selected = tk.StringVar(value=current)
    for value in CHOICES:
        enabled, reason = (availability or {}).get(value, (True, ""))
        if enabled:
            tk.Radiobutton(window, text=CHOICE_LABELS[value], variable=selected,
                           value=value, font=('sans', 22), fg=foreground,
                           bg=background, selectcolor='#16202c').pack(pady=6)
        else:
            tk.Label(window, text=f"{CHOICE_LABELS[value]} — unavailable: {reason}",
                     font=('sans', 18), fg='#8a94a3', bg=background,
                     wraplength=1100, justify='center').pack(pady=6)
    try:
        status = choice_status(choice_path, receipt_path)
        if status["verified"] and status["installed"]:
            status_text = (f"Installed: {CHOICE_LABELS[status['installed']]}"
                           + (" (restart the game to apply your new choice)."
                              if status["pending_restart"] else " (active)."))
        else:
            status_text = "Installed variant: unverified (no confirmed switch yet)."
    except Exception:
        status_text = "Installed variant: unknown."
    tk.Label(window, text=status_text, font=('sans', 16),
             fg='#c6d0dd', bg=background, wraplength=1100,
             justify='center').pack(pady=6)
    tk.Label(window, text=RESTART_NOTICE, font=('sans', 16),
             fg='#c6d0dd', bg=background, wraplength=1100,
             justify='center').pack(pady=22)
    result: list = []

    def choose(explicit_value: str | None):
        if explicit_value is not None:
            set_choice(choice_path or default_choice_path(),
                       explicit_value, explicit=True)
            result.append(explicit_value)
        window.destroy()

    tk.Button(window, text='Cancel', font=('sans', 22),
              command=lambda: choose(None)).pack(pady=6)
    tk.Button(window, text='Save & Play', font=('sans', 22),
              command=lambda: choose(selected.get())).pack(pady=6)
    window.mainloop()
    return result[0] if result else None

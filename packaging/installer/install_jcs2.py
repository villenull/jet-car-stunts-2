#!/usr/bin/env python3
"""Jet Car Stunts 2 — single-file SteamOS setup (stdlib only; dry-run default).

This is the entry point behind `jet-car-stunts-2-setup.tar.gz`. It turns an
extracted setup tree into a runnable SteamOS install: pinned Android runtime
fetched and hash-verified, fresh owned guest claimed, launcher payload staged,
the two install markers written, the guest bootstrapped with the existing
fresh-guest bootstrap, one lane smoke run, and the Steam shortcut handed to
steam_shortcut.py.

Stages, in order (each one reports what it WOULD do before it does it):

  preflight      host/root/space checks and the shipped-commit lock
  runtime        verify pinned archives (size + SHA-256) and extract the four
                 runtime components; a component whose dest already carries its
                 required_files is skipped, so a staged install never re-downloads
  payload        game APK set, controller binary, helper JAR, mapping and
                 artwork from the payload directory (separate release assets)
  avd            claim or keep the owned guest (bootstrap_linux_guest.claim_avd)
  layout         write jcs2-layout.json (portable layout marker)
  jcs2-config    persist the launcher driving-mode state (Gamepad only)
  guest          run the existing fresh-guest bootstrap: boot, isolate, install
                 the APKs, push the helper JAR, launch the game
  smoke          adb device + emulator process + lane ports, then one real
                 `run-jcs2 --input joystick` lane run (`--lane-seconds`)
  finalize       install-state.json {status: complete} + md5 runtime manifests
  steam          plan/register the non-Steam shortcut through steam_shortcut.py

Dry-run is the default and touches nothing: no directory creation, no network,
no process spawn, no marker writes. Exit codes:

  0  ok (dry-run plan complete, or execute finished)
  1  dry-run plan is incomplete: blocking pieces are missing (listed in the report)
  2  usage/config (bad root, refused AVD name, licenses not accepted, commit lock)
  3  lane ports busy
  4  AVD claim refused or failed
  5  runtime archive fetch/verify/extract failed
  6  payload staged file missing or unreadable
  7  marker write refused (existing conflicting state, never overwritten)
  8  guest bootstrap failed (see the bootstrap exit taxonomy)
  9  smoke test failed
  10 Steam shortcut registration failed
  130 interrupted (Ctrl+C)

Only `--execute` mutates. Never deletes: an incomplete runtime destination is
moved aside (`<dest>.partial-<utc>`) instead of being removed, and a conflicting
marker is refused rather than rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parents[1]
# A dry-run must leave the extracted setup tree byte-identical, so never drop
# __pycache__ beside the launcher/installer modules this tool imports.
sys.dont_write_bytecode = True
LOCK_PATH = HERE / "runtime-lock.json"
SETUP_MANIFEST = HERE.parent / "plan.py"

LAYOUT_FILE = "jcs2-layout.json"
INSTALL_STATE = "install-state.json"
STATE_DIR = "state"
LOG_DIR = "state/logs"
MANIFEST_JSON = "state/runtime-manifest.json"
MANIFEST_MD5 = "state/runtime-manifest.md5"
TILT_SETTINGS = "tilt-settings.json"
PAYLOAD_COMMIT_FILE = "payload-commit.txt"
PAYLOAD_DIR_REL = "staging/payload"
APKS_DIR_REL = "staging/apks"
CONTROL_MODE = "gamepad"
OWNED_MARKER = ".jcs2-owned"
BOOT_ARTIFACTS = ("data", "hardware-qemu.ini", "multiinstance.lock", "bootcompleted.ini",
                  "version_num.cache")
EXECUTABLE_NAMES = frozenset({"adb", "emulator", "qemu-img"})
ARTWORK_SLOTS = ("cover.png", "hero.png", "logo.png", "icon.png", "landscape.png")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_ARCHIVE_DIRS = (HERE / "archives", Path("/tmp/jcs2-installer-cache"))

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_CONFIG = 2
EXIT_PORTS = 3
EXIT_CLAIM = 4
EXIT_RUNTIME = 5
EXIT_PAYLOAD = 6
EXIT_MARKER = 7
EXIT_GUEST = 8
EXIT_SMOKE = 9
EXIT_STEAM = 10
EXIT_INTERRUPTED = 130


class InstallerError(Exception):
    """Fail-closed installer failure carrying the process exit code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Module loading (same convention as packaging/bootstrap_linux_guest.py).
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise InstallerError(EXIT_CONFIG, f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_module("jcs2_installer_bootstrap", HERE.parent / "bootstrap_linux_guest.py")
_sm = _load_module("jcs2_installer_setup_manifest", SETUP_MANIFEST)


# ---------------------------------------------------------------------------
# Small filesystem helpers.
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json_object(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Write through a sibling temporary file; never truncate in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_runtime_lock() -> dict:
    lock = read_json_object(LOCK_PATH)
    if lock.get("schema") != 1 or not isinstance(lock.get("components"), list):
        raise InstallerError(EXIT_CONFIG, f"unreadable runtime lock: {LOCK_PATH}")
    return lock


def select_components(lock: dict, include_alternates: bool) -> tuple[list, list]:
    """Split the lock into (fetched, skipped) components."""
    fetched, skipped = [], []
    for component in lock["components"]:
        if component.get("role") == "alternate" and not include_alternates:
            skipped.append(component)
        else:
            fetched.append(component)
    return fetched, skipped


def component_dest(cfg: "InstallerConfig", component: dict) -> Path:
    """Lock dest, relocated when --sdk moves the SDK root."""
    relative = PurePosixPath(component["dest"])
    default_sdk = (cfg.root / "runtime" / "sdk").resolve()
    parts = relative.parts
    if parts[:2] == ("runtime", "sdk") and cfg.sdk.resolve() != default_sdk:
        return cfg.sdk.joinpath(*parts[2:])
    return cfg.root.joinpath(*parts)


def component_missing_files(cfg: "InstallerConfig", component: dict) -> list:
    dest = component_dest(cfg, component)
    return [name for name in component["required_files"] if not (dest / name).is_file()]


def find_archive(component: dict, directories) -> "Path | None":
    for directory in directories:
        candidate = Path(directory) / component["filename"]
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Archive verification and extraction (never trusts member paths).
# ---------------------------------------------------------------------------

def verify_archive(path: Path, component: dict) -> dict:
    """Fail closed on size or SHA-256 mismatch; returns the verified identity."""
    size = path.stat().st_size
    if size != component["size"]:
        raise InstallerError(
            EXIT_RUNTIME,
            f"{path.name}: size {size} != pinned {component['size']} (refusing to extract)")
    digest = sha256_file(path)
    if digest != component["sha256"]:
        raise InstallerError(
            EXIT_RUNTIME,
            f"{path.name}: sha256 {digest} != pinned {component['sha256']} (refusing to extract)")
    return {"path": str(path), "size": size, "sha256": digest}


def _safe_member(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return False
    pure = PurePosixPath(name)
    return not pure.is_absolute() and ".." not in pure.parts


def _strip_top(name: str, top):
    parts = PurePosixPath(name).parts
    if top is None:
        return PurePosixPath(*parts) if parts else None
    if not parts or parts[0] != top:
        return None
    rest = parts[1:]
    return PurePosixPath(*rest) if rest else None


def extract_zip(archive: Path, dest: Path, top) -> int:
    count = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if not _safe_member(info.filename):
                raise InstallerError(EXIT_RUNTIME, f"{archive.name}: unsafe member {info.filename!r}")
            relative = _strip_top(info.filename, top)
            if relative is None:
                continue
            mode = (info.external_attr >> 16) & 0o7777
            if stat.S_ISLNK(mode):
                raise InstallerError(EXIT_RUNTIME, f"{archive.name}: symlink member {info.filename!r}")
            target = dest / relative
            if info.is_dir() or info.filename.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            os.chmod(target, mode or 0o644)
            count += 1
    return count


def extract_tar(archive: Path, dest: Path, top) -> int:
    count = 0
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle:
            if not _safe_member(member.name):
                raise InstallerError(EXIT_RUNTIME, f"{archive.name}: unsafe member {member.name!r}")
            relative = _strip_top(member.name, top)
            if relative is None:
                continue
            target = dest / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym():
                link = PurePosixPath(os.path.normpath(str(PurePosixPath(relative).parent / member.linkname)))
                if link.is_absolute() or ".." in link.parts:
                    raise InstallerError(
                        EXIT_RUNTIME, f"{archive.name}: symlink escapes the tree: {member.name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(member.linkname, target)
                count += 1
                continue
            if member.islnk():
                linked = _strip_top(member.linkname, top)
                if linked is None:
                    raise InstallerError(
                        EXIT_RUNTIME, f"{archive.name}: hardlink target outside the tree: {member.linkname!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(dest / linked, target)
                count += 1
                continue
            if not member.isfile():
                raise InstallerError(EXIT_RUNTIME, f"{archive.name}: unsupported member {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            with open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            os.chmod(target, member.mode & 0o7777 or 0o644)
            count += 1
    return count


def install_component(cfg: "InstallerConfig", component: dict, archive: Path, repair: bool) -> dict:
    """Extract into a sibling staging dir, verify, then move into dest."""
    dest = component_dest(cfg, component)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.extract-", dir=str(dest.parent)))
    kept = None
    try:
        top = component.get("top")
        if component["format"] == "zip":
            members = extract_zip(archive, staging, top)
        else:
            members = extract_tar(archive, staging, top)
        if members == 0:
            raise InstallerError(
                EXIT_RUNTIME, f"{archive.name}: no archive members under top {top!r}")
        source = staging
        missing = [name for name in component["required_files"] if not (source / name).is_file()]
        if missing:
            raise InstallerError(EXIT_RUNTIME, f"{archive.name}: extracted tree lacks {missing}")
        not_executable = [name for name in component["required_files"]
                          if Path(name).name in EXECUTABLE_NAMES
                          and not os.access(source / name, os.X_OK)]
        if not_executable:
            raise InstallerError(
                EXIT_RUNTIME,
                f"{archive.name}: extracted {not_executable} without the executable bit")
        if dest.exists() or dest.is_symlink():
            if not repair:
                raise InstallerError(
                    EXIT_MARKER,
                    f"{dest} already exists; refusing to overwrite it "
                    "(pass --repair to move it aside; the existing tree is kept)")
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            kept = dest.with_name(f"{dest.name}.partial-{stamp}")
            os.replace(dest, kept)
        os.replace(source, dest)
        return {"members": members, "dest": str(dest), "kept": str(kept) if kept else None}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def download_archive(url: str, target: Path, component: dict) -> dict:
    """Stream a pinned URL to target, verifying size and SHA-256 before rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": "jcs2-setup/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as sink:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                digest.update(block)
                total += len(block)
                sink.write(block)
    except OSError as error:
        if partial.exists():
            partial.unlink()
        raise InstallerError(EXIT_RUNTIME, f"download failed for {url}: {error}") from error
    if total != component["size"] or digest.hexdigest() != component["sha256"]:
        partial.unlink()
        raise InstallerError(
            EXIT_RUNTIME,
            f"downloaded {url}: size/sha256 mismatch (got {total} / {digest.hexdigest()})")
    os.replace(partial, target)
    return {"path": str(target), "size": total, "sha256": digest.hexdigest()}


# ---------------------------------------------------------------------------
# Host/lane probes.
# ---------------------------------------------------------------------------

def scan_processes(proc_root: str = "/proc"):
    """Yield (pid, argv) for every readable process; no privilege required."""
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            raw = Path(proc_root, entry, "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]
        if argv:
            yield int(entry), argv


def tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    import socket
    try:
        with socket.socket() as probe:
            probe.settimeout(timeout)
            return probe.connect_ex((host, port)) == 0
    except OSError:
        return False


def adb_connect(adb: Path, adb_port: int, serial: str, timeout: float = 20) -> str:
    """Make a host:port serial known to the adb server. Idempotent.

    A bootstrap pass owns its own adb server and tears it down on the way out;
    the next server starts empty, so the emulator that is already running would
    otherwise look like a missing device. Connecting first is harmless when the
    serial is already known ("already connected to ...").
    """
    if ":" not in serial:
        return ""
    try:
        result = subprocess.run([str(adb), "-P", str(adb_port), "connect", serial],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"connect failed: {error}"
    return (result.stdout + result.stderr).strip() or "connected"


def adb_state(adb: Path, adb_port: int, serial: str, timeout: float = 10) -> str:
    try:
        result = subprocess.run([str(adb), "-P", str(adb_port), "-s", serial, "get-state"],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return "unreachable"
    return result.stdout.strip() or result.stderr.strip() or "unknown"


def owned_processes(markers, avd_name: str, pattern: str, proc_root: str = "/proc"):
    """Processes started from this install whose argv matches pattern."""
    markers = [str(marker) for marker in markers]
    matches = []
    for pid, argv in scan_processes(proc_root):
        if not any(part == marker or part.startswith(marker + os.sep)
                   for part in argv for marker in markers):
            continue
        if pattern == "emulator":
            if "-avd" not in argv or avd_name not in argv:
                continue
            if not any(Path(part).name == "emulator" for part in argv):
                continue
        elif pattern == "adb-server":
            if "server" not in argv or "nodaemon" not in argv:
                continue
            if not any(Path(part).name == "adb" for part in argv):
                continue
        matches.append((pid, argv))
    return matches


def stop_processes(pids, timeout: float = 10.0) -> list:
    failures = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            failures.append(f"pid {pid}: {error}")
    deadline = time.monotonic() + timeout
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in sorted(remaining):
            if not Path("/proc", str(pid)).exists():
                remaining.discard(pid)
        time.sleep(0.2)
    for pid in sorted(remaining):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as error:
            failures.append(f"pid {pid}: {error}")
    return failures


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------

@dataclass
class InstallerConfig:
    root: Path
    sdk: Path
    avd_home: Path
    avd_name: str
    adb_port: int
    console_port: int
    archive_dirs: list
    payload_dirs: list
    payload_archive: object
    payload_sha256: object
    artwork_dir: Path
    allow_network: bool
    include_alternates: bool
    accept_licenses: bool
    repair: bool
    allow_commit_mismatch: bool
    execute: bool
    headless: bool
    keep_running: bool
    lane_seconds: int
    steam_account: object
    steam_root: object
    confirm_steam_closed: bool
    json_output: bool
    lock: dict = field(default_factory=dict)

    @property
    def serial(self) -> str:
        return bootstrap.serial_for(self.console_port)

    @property
    def owned_markers(self) -> list:
        """Prefixes whose processes belong to this install (root and SDK)."""
        return [str(self.root), str(self.sdk)]


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="install root (the extracted setup tree); default is this script's repo")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="perform the install; default is dry-run")
    mode.add_argument("--dry-run", action="store_true", help="print the plan and touch nothing (default)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--avd-name", default=bootstrap.DEFAULT_AVD_NAME)
    parser.add_argument("--sdk", type=Path, default=None, help="default: <root>/runtime/sdk")
    parser.add_argument("--avd-home", type=Path, default=None, help="default: <root>/state/avd")
    parser.add_argument("--adb-port", type=int, default=bootstrap.DEFAULT_ADB_PORT)
    parser.add_argument("--console-port", type=int, default=bootstrap.DEFAULT_CONSOLE_PORT)
    parser.add_argument("--archives-dir", type=Path, action="append", default=None,
                        help="repeatable; where the pinned archives live (offline install)")
    parser.add_argument("--payload-dir", type=Path, action="append", default=None,
                        help="repeatable; directory with the game APKs, controller binary and helper JAR")
    parser.add_argument("--payload-archive", type=Path, default=None,
                        help="tar.gz/zip holding the payload; extracted under staging/payload")
    parser.add_argument("--payload-sha256", default=None, help="expected SHA-256 of --payload-archive")
    parser.add_argument("--artwork-dir", type=Path, default=None,
                        help="PNG artwork for the Steam shortcut; default <root>/steam/artwork")
    parser.add_argument("--allow-network", action="store_true",
                        help="download missing pinned archives from their official URLs")
    parser.add_argument("--include-alternates", action="store_true",
                        help="also fetch components marked role=alternate in the lock")
    parser.add_argument("--accept-licenses", action="store_true",
                        help="accept the component licenses listed by the plan (required for --execute)")
    parser.add_argument("--repair", action="store_true",
                        help="move an incomplete runtime destination aside instead of refusing")
    parser.add_argument("--allow-commit-mismatch", action="store_true",
                        help="ignore a payload-commit.txt that differs from this checkout's HEAD")
    parser.add_argument("--headless", action="store_true",
                        help="pass -no-window to the emulator (sessions without a display)")
    parser.add_argument("--lane-seconds", type=int, default=None,
                        help="seconds for the real `run-jcs2 --input joystick` smoke run "
                             "(default 120 with --execute, 0 in dry-run)")
    parser.add_argument("--keep-running", action="store_true",
                        help="leave the smoke lane running instead of stopping it")
    parser.add_argument("--steam-account", default=None,
                        help="Steam account id to register the shortcut for (default: plan only)")
    parser.add_argument("--steam-root", type=Path, default=None)
    parser.add_argument("--confirm-steam-closed", action="store_true",
                        help="required with --steam-account: Steam is fully exited")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> InstallerConfig:
    root = Path(args.root).expanduser().resolve()

    def resolve(value, default_rel):
        if value is None:
            return (root / default_rel).resolve()
        candidate = Path(value).expanduser()
        return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()

    archives = [Path(p).expanduser().resolve() for p in (args.archives_dir or [])]
    if not archives:
        archives = [d for d in DEFAULT_ARCHIVE_DIRS]
        archives.insert(1, root / "staging" / "archives")
        env_dir = os.environ.get("JCS2_ARCHIVES_DIR")
        if env_dir:
            archives.insert(0, Path(env_dir).expanduser().resolve())
    payload_dirs = [Path(p).expanduser().resolve() for p in (args.payload_dir or [])]
    if not payload_dirs:
        env_dir = os.environ.get("JCS2_PAYLOAD_DIR")
        if env_dir:
            payload_dirs.append(Path(env_dir).expanduser().resolve())
        payload_dirs.append((root / PAYLOAD_DIR_REL).resolve())
    avd_name = bootstrap.validate_avd_name(args.avd_name)
    adb_port, console_port = bootstrap.validate_ports(args.adb_port, args.console_port)
    execute = bool(args.execute and not args.dry_run)
    lane_default = 120 if execute else 0
    return InstallerConfig(
        root=root,
        sdk=resolve(args.sdk, "runtime/sdk"),
        avd_home=resolve(args.avd_home, "state/avd"),
        avd_name=avd_name, adb_port=adb_port, console_port=console_port,
        archive_dirs=archives, payload_dirs=payload_dirs,
        payload_archive=Path(args.payload_archive).expanduser().resolve() if args.payload_archive else None,
        payload_sha256=args.payload_sha256,
        artwork_dir=resolve(args.artwork_dir, "steam/artwork"),
        allow_network=bool(args.allow_network),
        include_alternates=bool(args.include_alternates),
        accept_licenses=bool(args.accept_licenses),
        repair=bool(args.repair),
        allow_commit_mismatch=bool(args.allow_commit_mismatch),
        execute=execute,
        headless=bool(args.headless),
        keep_running=bool(args.keep_running),
        lane_seconds=int(args.lane_seconds) if args.lane_seconds is not None else lane_default,
        steam_account=args.steam_account,
        steam_root=Path(args.steam_root).expanduser().resolve() if args.steam_root else None,
        confirm_steam_closed=bool(args.confirm_steam_closed),
        json_output=bool(args.json),
        lock=load_runtime_lock())


# ---------------------------------------------------------------------------
# Report.
# ---------------------------------------------------------------------------

class Report:
    """Ordered stage rows plus the blocking/non-blocking missing-piece list."""

    def __init__(self, mode: str, cfg: InstallerConfig):
        self.mode = mode
        self.root = str(cfg.root)
        self.avd = cfg.avd_name
        self.layout = "portable"
        self.rows = []
        self.missing = []

    def stage(self, stage: str, status: str, action: str, **detail) -> dict:
        row = {"stage": stage, "status": status, "action": action}
        if detail:
            row["detail"] = detail
        self.rows.append(row)
        return row

    def need(self, item: str, reason: str, source=None, blocking: bool = True) -> None:
        entry = {"item": item, "reason": reason, "blocking": blocking}
        if source:
            entry["source"] = source
        self.missing.append(entry)

    @property
    def blocking(self) -> list:
        return [entry for entry in self.missing if entry["blocking"]]

    def document(self) -> dict:
        return {
            "schema": 1,
            "installer": "packaging/installer/install_jcs2.py",
            "mode": self.mode,
            "root": self.root,
            "layout": self.layout,
            "avd": self.avd,
            "stages": self.rows,
            "missing": self.missing,
            "blocking_missing": len(self.blocking),
            "ok": not self.blocking,
        }

    def render(self) -> str:
        lines = [f"jcs2 setup plan ({self.mode}; nothing was touched)"
                 if self.mode == "dry-run" else "jcs2 setup run (execute)",
                 f"root={self.root} layout={self.layout} avd={self.avd}"]
        for row in self.rows:
            lines.append(f"stage={row['stage']} status={row['status']} action={row['action']}")
            for key, value in sorted((row.get("detail") or {}).items()):
                if isinstance(value, (list, tuple)):
                    value = " ".join(str(item) for item in value)
                elif isinstance(value, dict):
                    value = " ".join(f"{k}={v}" for k, v in sorted(value.items()))
                lines.append(f"  {key}={value}")
        if self.missing:
            lines.append(f"missing ({len(self.blocking)} blocking):")
            for entry in self.missing:
                flag = "blocking" if entry["blocking"] else "optional"
                source = f" source={entry['source']}" if entry.get("source") else ""
                lines.append(f"  [{flag}] {entry['item']}: {entry['reason']}{source}")
        else:
            lines.append("missing: none")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Stage: preflight.
# ---------------------------------------------------------------------------

def payload_commit(root: Path) -> str:
    """The commit recorded in the archive, or 'unknown' for a plain checkout."""
    recorded = root / PAYLOAD_COMMIT_FILE
    if recorded.is_file():
        try:
            return recorded.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            return "unknown"
    return "unknown"


def checkout_commit(root: Path):
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def stage_preflight(cfg: InstallerConfig, report: Report) -> None:
    if not cfg.root.is_dir():
        raise InstallerError(EXIT_CONFIG, f"install root is not a directory: {cfg.root}")
    if sys.version_info < (3, 9):
        raise InstallerError(EXIT_CONFIG, f"python {sys.version_info[:3]} is too old (need 3.9+)")
    free = shutil.disk_usage(cfg.root).free
    components, _ = select_components(cfg.lock, cfg.include_alternates)
    absent = [component for component in components
              if component_missing_files(cfg, component)]
    download_bytes = sum(component["size"] for component in absent)
    # Shipped-commit lock: a payload-commit.txt that disagrees with the checkout
    # means the tree was patched after assembly; fail closed before mutating.
    recorded, current = payload_commit(cfg.root), checkout_commit(cfg.root)
    detail = {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "machine": os.uname().machine,
        "free_bytes": free,
        "download_bytes_if_fetched": download_bytes,
        "payload_commit": recorded,
        "checkout_commit": current or "unknown",
    }
    status = "ok"
    if absent and free < download_bytes:
        status = "blocked"
        report.need("disk space",
                    f"free {free} < pinned download {download_bytes} bytes for {len(absent)} "
                    "components (extraction needs more than the archive size)",
                    source=str(cfg.root))
        if cfg.execute:
            raise InstallerError(EXIT_CONFIG,
                                 f"insufficient free space at {cfg.root}: {free} < {download_bytes}")
    if recorded != "unknown" and current and recorded != current:
        detail["mismatch"] = True
        if not cfg.allow_commit_mismatch:
            status = "refused" if status == "ok" else status
            report.need("payload commit",
                        f"shipped payload-commit.txt {recorded[:12]} != checkout HEAD {current[:12]}",
                        source=PAYLOAD_COMMIT_FILE)
            if cfg.execute:
                raise InstallerError(EXIT_CONFIG,
                                     "payload commit mismatch; pass --allow-commit-mismatch to override")
    report.stage("preflight", status, "host, install-root, space and shipped-commit checks", **detail)
    return


# ---------------------------------------------------------------------------
# Stage: runtime.
# ---------------------------------------------------------------------------

def stage_runtime(cfg: InstallerConfig, report: Report) -> None:
    components, skipped = select_components(cfg.lock, cfg.include_alternates)
    rows = {}
    fetched_now = []
    for component in components:
        missing = component_missing_files(cfg, component)
        entry = {
            "dest": component["dest"],
            "url": component["url"],
            "size": component["size"],
            "sha256": component["sha256"],
        }
        if not missing:
            rows[component["id"]] = ("present", "keep the verified tree already at " + component["dest"], entry)
            continue
        archive = find_archive(component, cfg.archive_dirs)
        if archive is not None:
            try:
                verified = verify_archive(archive, component)
            except InstallerError as error:
                report.need(f"runtime/{component['id']}", error.message,
                            source=str(archive))
                rows[component["id"]] = ("mismatch", "refused: " + error.message, entry)
                if cfg.execute:
                    raise
                continue
            entry.update(archive=verified["path"], verified=True)
            action = f"extract {archive.name} -> {component['dest']} (sha256 verified)"
            rows[component["id"]] = ("would-extract", action, entry)
            fetched_now.append((component, archive))
        elif cfg.allow_network:
            target = cfg.archive_dirs[0] / component["filename"]
            entry.update(download_to=str(target))
            rows[component["id"]] = (
                "would-download", f"download {component['url']} -> {target}, verify, extract", entry)
            fetched_now.append((component, None))
        else:
            rows[component["id"]] = (
                "missing", "no local archive and no --allow-network", entry)
            report.need(f"runtime/{component['id']}",
                        f"no {component['filename']} in {[str(d) for d in cfg.archive_dirs]}; "
                        f"stage it there or pass --allow-network ({component['size']} bytes)",
                        source="--archives-dir or --allow-network")
    states = [rows[component["id"]][0] for component in components]
    if "missing" in states or "mismatch" in states:
        status = "missing"
    elif "would-extract" in states or "would-download" in states:
        status = "pending"
    else:
        status = "ok"
    licenses = sorted({component["license"]["text"] for component in components
                       if component.get("license", {}).get("requires_acceptance")})
    needed_licenses = sorted({component["license"]["text"] for component, _ in fetched_now
                              if component.get("license", {}).get("requires_acceptance")})
    report.stage("runtime", status,
                 f"verify and extract {len(components)} pinned components",
                 components={key: value[0] for key, value in rows.items()},
                 licenses_to_accept=needed_licenses or licenses)
    for component in components:
        status_text, action, entry = rows[component["id"]]
        report.stage(f"runtime/{component['id']}", status_text, action, **entry)
    if skipped:
        report.stage("runtime/alternates", "skipped",
                     "lock components marked role=alternate are not fetched",
                     ids=[component["id"] for component in skipped])
    if cfg.execute and not cfg.accept_licenses:
        raise InstallerError(
            EXIT_CONFIG,
            "refusing without --accept-licenses; component licenses live under "
            f"{HERE / 'licenses'} ({', '.join(licenses) or 'none'})")
    unverified = [component["id"] for component in components
                  if rows[component["id"]][0] in ("missing", "mismatch")]
    if cfg.execute and unverified:
        raise InstallerError(
            EXIT_RUNTIME,
            f"no verified archive for {unverified}; nothing was extracted")
    for component, archive in fetched_now:
        if archive is None:
            target = cfg.archive_dirs[0] / component["filename"]
            if not cfg.execute:
                continue
            download_archive(component["url"], target, component)
            archive = target
        if not cfg.execute:
            continue
        result = install_component(cfg, component, archive, cfg.repair)
        if result["kept"]:
            report.stage(f"runtime/{component['id']}", "repaired",
                         f"kept the previous tree at {result['kept']}")
    return


# ---------------------------------------------------------------------------
# Stage: payload (game APKs, controller, helper, mapping, artwork).
# ---------------------------------------------------------------------------

def _first_existing(candidates):
    for candidate in candidates:
        if Path(candidate).is_file():
            return Path(candidate)
    return None


def stage_payload_archive(cfg: InstallerConfig, report: Report) -> bool:
    """Extract --payload-archive under staging/payload before staging files."""
    archive = cfg.payload_archive
    target = cfg.root / PAYLOAD_DIR_REL
    detail = {"archive": str(archive), "dest": str(target)}
    if not archive.is_file():
        report.stage("payload/archive", "missing", f"payload archive not found: {archive}")
        report.need("payload/archive", f"{archive} does not exist", source="--payload-archive")
        if cfg.execute:
            raise InstallerError(EXIT_PAYLOAD, f"payload archive not found: {archive}")
        return False
    if cfg.payload_sha256:
        digest = sha256_file(archive)
        detail["sha256"] = digest
        if digest != cfg.payload_sha256:
            report.stage("payload/archive", "mismatch",
                         f"sha256 {digest} != {cfg.payload_sha256}", **detail)
            report.need("payload/archive",
                        f"{archive.name}: sha256 {digest} != {cfg.payload_sha256}",
                        source="--payload-sha256")
            if cfg.execute:
                raise InstallerError(
                    EXIT_PAYLOAD, f"{archive.name}: sha256 mismatch, nothing extracted")
            return False
    if target.is_dir() and any(target.iterdir()):
        report.stage("payload/archive", "present",
                     f"payload already extracted at {target}", **detail)
        return True
    report.stage("payload/archive", "would-extract", f"extract {archive.name} -> {target}", **detail)
    if not cfg.execute:
        return True
    target.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        extract_zip(archive, target, None)
    else:
        extract_tar(archive, target, None)
    report.stage("payload/archive", "extracted", f"extracted payload into {target}", **detail)
    return True


def stage_payload(cfg: InstallerConfig, report: Report) -> None:
    payload_dirs = list(cfg.payload_dirs)
    if cfg.payload_archive is not None:
        stage_payload_archive(cfg, report)
        payload_dirs.insert(0, cfg.root / PAYLOAD_DIR_REL)

    # Game APK set: exactly one monolith .apk or exactly the five legacy splits.
    apks_dir = cfg.root / APKS_DIR_REL
    try:
        staged = bootstrap.collect_split_paths(apks_dir)
    except bootstrap.BootstrapError:
        staged = None
    source_apks = None
    source_names = []
    for directory in payload_dirs:
        if not directory.is_dir():
            continue
        apks = sorted(path for path in directory.iterdir() if path.suffix == ".apk")
        if not apks:
            continue
        try:
            bootstrap.collect_split_paths(directory)
        except bootstrap.BootstrapError:
            continue
        source_apks, source_names = directory, [path.name for path in apks]
        break
    apk_detail = {
        "dest": str(apks_dir),
        "expected": "one *.apk or " + ", ".join(bootstrap.EXPECTED_SPLITS),
    }
    if staged is not None and (source_apks is None or sorted(p.name for p in staged) == sorted(source_names)):
        report.stage("payload/apks", "present",
                     "keep the staged APK set at " + str(apks_dir),
                     **apk_detail)
        apks_ready = True
    elif source_apks is not None:
        report.stage("payload/apks", "would-stage",
                     f"copy {len(source_names)} APK file(s) {source_apks} -> {apks_dir}",
                     sources=source_names)
        apks_ready = True
        if cfg.execute:
            apks_dir.mkdir(parents=True, exist_ok=True)
            for name in source_names:
                shutil.copy2(source_apks / name, apks_dir / name)
    else:
        searched = [str(directory) for directory in payload_dirs]
        report.stage("payload/apks", "missing", "no game APK set found", searched=searched)
        report.need("payload/game-apks",
                    f"no APK set in {searched}; expected one *.apk or {list(bootstrap.EXPECTED_SPLITS)}",
                    source="--payload-dir (release payload asset)")
        apks_ready = False

    def stage_file(label: str, names, dest_rel: str, executable: bool, required: bool) -> bool:
        dest = cfg.root / dest_rel
        if dest.is_file() and (not executable or os.access(dest, os.X_OK)):
            report.stage(label, "present", f"keep {dest_rel}", dest=str(dest))
            return True
        candidates = [directory / name for directory in payload_dirs for name in names]
        candidates += [cfg.root / "linux-launcher" / names[0],
                       cfg.root / "controller" / names[0]]
        found = _first_existing(candidates)
        if found is None:
            report.stage(label, "missing", f"no {names[0]} staged",
                         searched=[str(directory) for directory in payload_dirs])
            if required:
                report.need(label, f"{names[0]} not found; looked in "
                                   f"{[str(directory) for directory in payload_dirs]}",
                            source="--payload-dir (release payload asset)",
                            blocking=required)
            return False
        report.stage(label, "would-stage", f"copy {found} -> {dest_rel}",
                     source=str(found), dest=str(dest))
        if cfg.execute:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found, dest)
            if executable:
                os.chmod(dest, 0o755)
        return True

    controller_ready = stage_file("payload/controller",
                                  ("jcs2-controller-linux", "controller-linux"),
                                  "linux-launcher/jcs2-controller-linux", True, True)
    helper_ready = stage_file("payload/helper-jar",
                              ("jcs2-input-helper.jar", "helper.jar"),
                              "linux-launcher/jcs2-input-helper.jar", False, True)
    mapping_ready = stage_file("payload/mapping", ("mapping.json",),
                               "assets/controller/mapping.json", False, True)
    ready = {"apks": apks_ready, "controller": controller_ready,
             "helper-jar": helper_ready, "mapping": mapping_ready}
    report.stage("payload", "ok" if all(ready.values()) else "incomplete",
                 "payload asset staging (game APKs, controller, helper JAR, mapping)",
                 items={key: ("staged" if value else "missing") for key, value in ready.items()})

    artwork = {}
    for name in ARTWORK_SLOTS:
        path = cfg.artwork_dir / name
        valid = path.is_file()
        if valid:
            try:
                with open(path, "rb") as handle:
                    valid = handle.read(8) == PNG_MAGIC
            except OSError:
                valid = False
        artwork[name] = "present" if valid else "missing"
    missing_art = [name for name, state in artwork.items() if state == "missing"]
    report.stage("payload/artwork", "ok" if not missing_art else "incomplete",
                 "PNG artwork consumed by steam_shortcut.py",
                 dir=str(cfg.artwork_dir), slots=artwork)
    if missing_art:
        report.need("payload/artwork",
                    f"no {missing_art} under {cfg.artwork_dir}; the shortcut would be "
                    "registered without those library images",
                    source="--artwork-dir (release artwork asset)", blocking=False)


# ---------------------------------------------------------------------------
# Stage: AVD claim.
# ---------------------------------------------------------------------------

def avd_decision(cfg: InstallerConfig) -> dict:
    """Read-only mirror of bootstrap.claim_avd; claim_avd stays authoritative."""
    guest = cfg.avd_home / f"{cfg.avd_name}.avd"
    pointer = cfg.avd_home / f"{cfg.avd_name}.ini"
    marker = guest / OWNED_MARKER
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8")
        except OSError as error:
            return {"decision": "refused", "detail": f"owned marker unreadable: {error}"}
        if text != cfg.avd_name + "\n":
            return {"decision": "refused", "detail": f"owned marker names another guest: {marker}"}
        if not (guest / "config.ini").is_file() or not pointer.is_file():
            return {"decision": "refused", "detail": "owned guest is missing config.ini or pointer"}
        return {"decision": "keep", "detail": f"owned guest already claimed: {guest}"}
    if guest.is_dir() and (guest / "config.ini").is_file() and pointer.is_file():
        booted = sorted(path.name for path in guest.iterdir()
                        if path.suffix == ".qcow2" or path.name in BOOT_ARTIFACTS)
        if booted:
            return {"decision": "refused",
                    "detail": f"booted foreign guest {guest} (state: {', '.join(booted)})"}
        return {"decision": "adopt", "detail": f"never-booted guest without owned marker: {guest}"}
    if cfg.avd_home.exists() and any(cfg.avd_home.iterdir()):
        return {"decision": "refused",
                "detail": f"non-empty avd_home without owned marker: {cfg.avd_home}"}
    return {"decision": "claim", "detail": f"fresh owned guest under {cfg.avd_home}"}


def stage_avd(cfg: InstallerConfig, report: Report) -> None:
    decision = avd_decision(cfg)
    image_dir = cfg.sdk / bootstrap.IMAGE_REL
    detail = {
        "avd_home": str(cfg.avd_home),
        "decision": decision["decision"],
        "note": decision["detail"],
    }
    if decision["decision"] == "refused":
        report.stage("avd", "refused", "refuse to touch an unowned guest", **detail)
        report.need("avd", decision["detail"], source=str(cfg.avd_home))
        if cfg.execute:
            raise InstallerError(EXIT_CLAIM, decision["detail"])
        return
    if not image_dir.is_dir():
        report.stage("avd", "blocked", "system image must be extracted before the guest is claimed",
                     image=str(image_dir), **detail)
        report.need("avd/system-image", f"missing system image tree: {image_dir}",
                    source="runtime component system-image")
        if cfg.execute:
            raise InstallerError(EXIT_CLAIM, f"missing system image dir: {image_dir}")
        return
    actions = {"keep": "keep the claimed guest (no writes)",
               "adopt": "mark the never-booted guest as owned and keep it",
               "claim": "create config.ini + pointer + owned marker"}
    report.stage("avd", "would-" + decision["decision"], actions[decision["decision"]], **detail)
    if not cfg.execute:
        return
    config = bootstrap.BootstrapConfig(
        root=cfg.root, sdk=cfg.sdk, adb=cfg.sdk / "platform-tools" / "adb",
        emulator=cfg.sdk / "emulator" / "emulator", image_dir=image_dir,
        avd_home=cfg.avd_home, avd_name=cfg.avd_name, apks_dir=cfg.root / APKS_DIR_REL,
        split_paths=[], controller=None, mapping=None, helper_jar=None,
        adb_port=cfg.adb_port, console_port=cfg.console_port, serial=cfg.serial,
        accept_licenses=cfg.accept_licenses, execute=True, headless=cfg.headless)
    try:
        guest, pointer, fresh = bootstrap.claim_avd(config)
    except bootstrap.BootstrapError as error:
        raise InstallerError(EXIT_CLAIM, error.message) from error
    report.stage("avd", "claimed",
                 "guest claimed" if fresh else "existing owned guest kept",
                 guest=str(guest), pointer=str(pointer))
    return


# ---------------------------------------------------------------------------
# Stage: layout marker.
# ---------------------------------------------------------------------------

def layout_document(avd_name: str) -> dict:
    return {"schema": 1, "layout": "portable", "avd": avd_name}


def stage_layout(cfg: InstallerConfig, report: Report) -> None:
    path = cfg.root / LAYOUT_FILE
    wanted = layout_document(cfg.avd_name)
    if not path.exists():
        report.stage("layout", "would-write", f"write {LAYOUT_FILE}",
                     path=str(path), content=wanted)
        if cfg.execute:
            atomic_write_text(path, json.dumps(wanted, indent=2) + "\n")
            report.stage("layout", "written", f"wrote {LAYOUT_FILE}", path=str(path))
        return
    existing = read_json_object(path)
    if existing == wanted:
        report.stage("layout", "present", f"{LAYOUT_FILE} already selects this layout",
                     path=str(path))
        return
    report.stage("layout", "refused",
                 f"{LAYOUT_FILE} differs from the requested layout; never overwritten",
                 path=str(path), existing=existing, wanted=wanted)
    report.need("layout", f"{path} selects {existing} but this install needs {wanted}; "
                          "resolve it by hand (the installer never rewrites it)",
                source=LAYOUT_FILE)
    if cfg.execute:
        raise InstallerError(EXIT_MARKER, f"refusing to overwrite {path}")
    return


# ---------------------------------------------------------------------------
# Stage: launcher driving-mode state (Gamepad only).
# ---------------------------------------------------------------------------

def stage_jcs2_config(cfg: InstallerConfig, report: Report) -> None:
    path = cfg.root / LOG_DIR / TILT_SETTINGS
    existing = read_json_object(path)
    detail = {"path": str(path), "mode": CONTROL_MODE}
    if not path.is_file():
        report.stage("jcs2-config", "would-write",
                     f"persist the launcher driving mode {CONTROL_MODE}", **detail)
        if not cfg.execute:
            return
    elif existing.get("mode") == CONTROL_MODE:
        report.stage("jcs2-config", "present",
                     f"driving mode already persisted as {CONTROL_MODE}", **detail)
        return
    else:
        report.stage("jcs2-config", "would-migrate",
                     f"persist driving mode {CONTROL_MODE} (was {existing.get('mode')!r}); "
                     "calibration and gains are kept", **detail)
        if not cfg.execute:
            return
    launcher_dir = str(cfg.root / "linux-launcher")
    if launcher_dir not in sys.path:
        sys.path.insert(0, launcher_dir)
    try:
        settings_module = importlib.import_module("tilt_control.settings")
    except ImportError as error:
        raise InstallerError(EXIT_CONFIG, f"launcher settings modules unavailable: {error}") from error
    # The mode is written through the engine's own settings object: the touch
    # selector that used to do it is gone (Gamepad is the only mode).
    settings = settings_module.TiltSettings(path)
    settings.mode = settings_module.ControlMode(CONTROL_MODE)
    persisted = settings_module.TiltSettings(path).mode.value
    if persisted != CONTROL_MODE:
        raise InstallerError(EXIT_CONFIG, f"driving mode did not persist: {persisted}")
    report.stage("jcs2-config", "written", f"driving mode persisted as {CONTROL_MODE}",
                 path=str(path), mode=persisted)
    return


# ---------------------------------------------------------------------------
# Stage: guest bootstrap (existing packaging/bootstrap_linux_guest.py).
# ---------------------------------------------------------------------------

def guest_config(cfg: InstallerConfig) -> "bootstrap.BootstrapConfig":
    apks_dir = cfg.root / APKS_DIR_REL
    split_paths = bootstrap.collect_split_paths(apks_dir)
    controller = cfg.root / "linux-launcher" / "jcs2-controller-linux"
    helper = cfg.root / "linux-launcher" / "jcs2-input-helper.jar"
    mapping = cfg.root / "assets" / "controller" / "mapping.json"
    bootstrap.validate_controller_trio(controller, mapping, helper)
    identity, apk_hashes, helper_hash = bootstrap.compute_identity(
        bootstrap.PACKAGE, split_paths, helper)
    return bootstrap.BootstrapConfig(
        root=cfg.root, sdk=cfg.sdk, adb=cfg.sdk / "platform-tools" / "adb",
        emulator=cfg.sdk / "emulator" / "emulator", image_dir=cfg.sdk / bootstrap.IMAGE_REL,
        avd_home=cfg.avd_home, avd_name=cfg.avd_name, apks_dir=apks_dir,
        split_paths=split_paths, controller=controller, mapping=mapping, helper_jar=helper,
        adb_port=cfg.adb_port, console_port=cfg.console_port, serial=cfg.serial,
        accept_licenses=cfg.accept_licenses, execute=True, headless=cfg.headless,
        identity=identity, apk_hashes=apk_hashes, helper_hash=helper_hash)


def stage_guest(cfg: InstallerConfig, report: Report) -> None:
    try:
        config = guest_config(cfg)
    except bootstrap.BootstrapError as error:
        report.stage("guest", "missing", "guest bootstrap inputs are incomplete",
                     detail_error=error.message)
        report.need("guest", error.message, source="payload + runtime stages")
        if cfg.execute:
            raise InstallerError(EXIT_PAYLOAD, error.message) from error
        return
    busy = [port for port in (cfg.adb_port, cfg.console_port, cfg.console_port + 1)
            if not bootstrap.port_bind_free(port)]
    detail = {
        "apks": [path.name for path in config.split_paths],
        "identity": config.identity.splitlines()[0] if config.identity else "pending",
        "serial": cfg.serial,
        "busy_ports": busy or "none",
        "headless": cfg.headless,
        "bootstrap": str(HERE.parent / "bootstrap_linux_guest.py"),
    }
    if busy:
        report.stage("guest", "blocked", "lane ports must be free before the guest is booted", **detail)
        report.need("ports", f"busy: {busy}; stop the other session first", source="bind probe")
        if cfg.execute:
            raise InstallerError(EXIT_PORTS, f"ports busy, no mutation performed: {busy}")
        return
    report.stage("guest", "would-run",
                 "boot the guest, isolate it, install the APKs, push the helper JAR and launch the game",
                 **detail)
    if not cfg.execute:
        return
    try:
        code = bootstrap.execute_bootstrap(config)
    except bootstrap.BootstrapError as error:
        raise InstallerError(
            EXIT_GUEST, f"guest bootstrap failed (bootstrap exit {error.code}): {error.message}") from error
    if code != bootstrap.EXIT_OK:
        raise InstallerError(EXIT_GUEST, f"bootstrap exit code {code}")
    report.stage("guest", "bootstrapped",
                 "guest booted, isolated and the game was installed and launched",
                 serial=cfg.serial)
    return


# ---------------------------------------------------------------------------
# Stage: smoke test (adb device + emulator + lane).
# ---------------------------------------------------------------------------

def probe_lane(cfg: InstallerConfig) -> dict:
    adb = cfg.sdk / "platform-tools" / "adb"
    emulators = owned_processes(cfg.owned_markers, cfg.avd_name, "emulator")
    ports = {port: tcp_open(port) for port in (cfg.adb_port, cfg.console_port, cfg.console_port + 1)}
    connect, state = "skipped", "no-adb"
    if adb.is_file():
        connect = adb_connect(adb, cfg.adb_port, cfg.serial)
        state = adb_state(adb, cfg.adb_port, cfg.serial)
    return {
        "adb_connect": connect,
        "adb_device": state,
        "emulator_pids": [pid for pid, _ in emulators],
        "lane_ports": {str(port): ("open" if is_open else "closed") for port, is_open in ports.items()},
        "lane_available": state == "device" and all(ports.values()),
    }


def lane_argv(cfg: "InstallerConfig") -> list:
    argv = [str(cfg.root / "run-jcs2"), "--input", "joystick"]
    if cfg.headless:
        argv.append("--headless")
    return argv


def start_lane(cfg: InstallerConfig, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sink = log_path.open("wb")
    return subprocess.Popen(lane_argv(cfg), stdout=sink, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, cwd=str(cfg.root),
                            start_new_session=True)


def stop_lane(process: subprocess.Popen, timeout: float = 20.0) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def stage_smoke(cfg: InstallerConfig, report: Report) -> None:
    adb = cfg.sdk / "platform-tools" / "adb"
    plan_detail = {
        "checks": ["adb device", "emulator process for the AVD", "lane ports"],
        "serial": cfg.serial,
        "ports": [cfg.adb_port, cfg.console_port, cfg.console_port + 1],
        "adb": str(adb),
        "lane_command": "run-jcs2 --input joystick",
        "lane_seconds": cfg.lane_seconds,
    }
    if not cfg.execute:
        report.stage("smoke", "would-verify",
                     "connect adb, confirm the emulator is up, then run one lane pass",
                     **plan_detail)
        return
    probe = probe_lane(cfg)
    if not probe["lane_available"]:
        report.stage("smoke", "failed", "guest is not reachable after the bootstrap", **probe)
        raise InstallerError(EXIT_SMOKE, f"lane checks failed: {probe}")
    report.stage("smoke", "ok", "adb device, emulator process and lane ports verified", **probe)
    if cfg.lane_seconds <= 0:
        report.stage("smoke/lane", "skipped",
                     "no lane run requested (pass --lane-seconds N)", lane_seconds=cfg.lane_seconds)
        return
    # The lane owns the emulator and both ports (runner.py spawns its own adb
    # server), so the bootstrap-owned guest is stopped first.
    owned = [pid for pid, _ in owned_processes(cfg.owned_markers, cfg.avd_name, "emulator")]
    owned += [pid for pid, _ in owned_processes(cfg.owned_markers, cfg.avd_name, "adb-server")]
    if owned:
        failures = stop_processes(owned)
        report.stage("smoke/lane", "handover",
                     "stopped the bootstrap-owned guest so the lane can own the ports",
                     pids=owned, failures=failures or "none")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not bootstrap.port_bind_free(cfg.adb_port):
            time.sleep(0.5)
        if not bootstrap.port_bind_free(cfg.adb_port):
            raise InstallerError(EXIT_SMOKE, f"port {cfg.adb_port} still busy after guest handover")
    log_path = cfg.root / LOG_DIR / "installer-lane.log"
    process = start_lane(cfg, log_path)
    deadline = time.monotonic() + cfg.lane_seconds
    last = {}
    while time.monotonic() < deadline:
        last = probe_lane(cfg)
        if last["lane_available"]:
            break
        if process.poll() is not None:
            raise InstallerError(
                EXIT_SMOKE,
                f"lane exited with {process.returncode} before it became available; log={log_path}")
        time.sleep(2)
    if not last.get("lane_available"):
        if not cfg.keep_running:
            stop_lane(process)
        raise InstallerError(
            EXIT_SMOKE,
            f"lane did not become available within {cfg.lane_seconds}s: {last}; log={log_path}")
    report.stage("smoke/lane", "ok",
                 f"lane available after {cfg.lane_seconds}s budget (pid {process.pid})",
                 log=str(log_path), **last)
    if cfg.keep_running:
        report.stage("smoke/lane", "left-running", "leaving the lane up (--keep-running)",
                     pid=process.pid)
    else:
        stop_lane(process)
        report.stage("smoke/lane", "stopped", "smoke lane stopped", pid=process.pid)
    return


# ---------------------------------------------------------------------------
# Stage: finalize (install state + md5 runtime manifests).
# ---------------------------------------------------------------------------

def shipped_paths(root: Path) -> list:
    """Files the setup archive itself must have delivered into the install root."""
    return sorted(dict.fromkeys(entry["destination"] for entry in _sm.setup_entries()))


def payload_paths(root: Path) -> list:
    """Files that arrive from the separate payload asset, not from the archive."""
    return ["linux-launcher/jcs2-controller-linux", "linux-launcher/jcs2-input-helper.jar"]


def installed_paths(root: Path) -> list:
    """Everything the md5 runtime manifest covers once the install is complete."""
    return sorted(dict.fromkeys(shipped_paths(root) + payload_paths(root)))


def stage_finalize(cfg: InstallerConfig, report: Report) -> None:
    absent_shipped = [rel for rel in shipped_paths(cfg.root) if not (cfg.root / rel).is_file()]
    absent_payload = [rel for rel in payload_paths(cfg.root) if not (cfg.root / rel).is_file()]
    if absent_shipped:
        report.need("setup tree",
                    f"{len(absent_shipped)} archive file(s) absent from the install root: "
                    f"{absent_shipped[:6]}{' ...' if len(absent_shipped) > 6 else ''}",
                    source="setup archive extraction")
        if cfg.execute:
            raise InstallerError(
                EXIT_MARKER,
                f"install tree is incomplete ({len(absent_shipped)} missing, e.g. "
                f"{absent_shipped[:4]}); re-extract the setup archive before writing install-state.json")
    if absent_payload:
        report.need("payload binaries",
                    f"{absent_payload} not staged; supply the payload asset through --payload-dir",
                    source="payload stage")
        if cfg.execute:
            raise InstallerError(
                EXIT_PAYLOAD,
                f"payload binaries are not staged: {absent_payload}")
    state_path = cfg.root / INSTALL_STATE
    existing = {}
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InstallerError(
                EXIT_MARKER,
                f"{state_path} exists but is not readable JSON ({error}); inspect or remove it "
                "(the installer never overwrites unknown state)") from error
        if not isinstance(existing, dict):
            raise InstallerError(
                EXIT_MARKER,
                f"{state_path} is not a JSON object; inspect or remove it "
                "(the installer never overwrites unknown state)")
    state = dict(existing)
    state.update({
        "schema": 1,
        "status": "complete",
        "title": "Jet Car Stunts 2",
        "layout": "portable",
        "avd": cfg.avd_name,
        "serial": cfg.serial,
        "payload_commit": payload_commit(cfg.root),
        "path": str(cfg.root),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    report.stage("finalize/install-state", "would-write", f"write {INSTALL_STATE} status=complete",
                 path=str(state_path), preserved=sorted(existing) or "none")
    manifest = {
        "schema": 1,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": str(cfg.root),
        "layout": "portable",
        "avd": cfg.avd_name,
        "payload_commit": payload_commit(cfg.root),
        "lane_ports": {"adb": cfg.adb_port, "console": cfg.console_port,
                       "serial": cfg.console_port + 1},
        "components": [],
        "files": {},
    }
    digest_lines = []
    for component in cfg.lock["components"]:
        dest = component_dest(cfg, component)
        files = {}
        for name in component["required_files"]:
            path = dest / name
            files[name] = md5_file(path) if path.is_file() else None
        manifest["components"].append({
            "id": component["id"], "dest": component["dest"],
            "used": component.get("role") != "alternate",
            "source": {"url": component["url"], "size": component["size"],
                       "sha256": component["sha256"]},
            "required_files_md5": files,
        })
    for rel in installed_paths(cfg.root):
        path = cfg.root / rel
        if not path.is_file():
            continue
        digest = md5_file(path)
        manifest["files"][rel] = {"md5": digest, "size": path.stat().st_size}
        digest_lines.append(f"{digest}  {rel}")
    report.stage("finalize/manifest", "would-write",
                 f"write {MANIFEST_MD5} ({len(digest_lines)} files) and {MANIFEST_JSON}",
                 components=len(manifest["components"]))
    if not cfg.execute:
        return
    atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    atomic_write_text(cfg.root / MANIFEST_JSON, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    atomic_write_text(cfg.root / MANIFEST_MD5, "\n".join(digest_lines) + "\n")
    report.stage("finalize", "written",
                 "install-state.json, runtime-manifest.json and runtime-manifest.md5 written",
                 state=str(state_path), md5=str(cfg.root / MANIFEST_MD5))
    return


# ---------------------------------------------------------------------------
# Stage: Steam shortcut (existing steam/steam_shortcut.py).
# ---------------------------------------------------------------------------

def steam_command(cfg: InstallerConfig, verb: str) -> list:
    target = "steam/jcs2-steam-launch.sh"
    argv = [sys.executable, str(cfg.root / "steam" / "steam_shortcut.py"), verb,
            "--install-root", str(cfg.root), "--launch-target", target]
    if verb != "roots":
        argv += ["--account", str(cfg.steam_account or "<account-id>")]
    if cfg.steam_root:
        argv += ["--steam-root", str(cfg.steam_root)]
    if verb == "register":
        argv.append("--confirm-steam-closed")
    return argv


def run_steam(cfg: InstallerConfig, argv: list) -> dict:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": str(error)}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"ok": False, "error": (result.stdout or result.stderr).strip()[:400]}
    payload.setdefault("returncode", result.returncode)
    return payload


def stage_steam(cfg: InstallerConfig, report: Report) -> None:
    script = cfg.root / "steam" / "steam_shortcut.py"
    if not script.is_file():
        report.stage("steam", "missing", f"shortcut helper absent: {script}")
        report.need("steam/steam_shortcut.py", "the setup tree is incomplete",
                    source=str(script))
        if cfg.execute:
            raise InstallerError(EXIT_STEAM, f"missing {script}")
        return
    if cfg.steam_account is None:
        command = " ".join(steam_command(cfg, "register"))
        report.stage("steam", "skipped",
                     "no --steam-account given; register with the command below",
                     command=command)
        report.need("steam registration",
                    f"not requested; run: {command}",
                    source="--steam-account", blocking=False)
        return
    verb = "register" if cfg.execute else "plan"
    if verb == "register" and not cfg.confirm_steam_closed:
        raise InstallerError(EXIT_CONFIG, "refusing to register without --confirm-steam-closed")
    argv = steam_command(cfg, verb)
    if not cfg.execute:
        report.stage("steam", "would-register", "plan the non-Steam shortcut", command=" ".join(argv))
    result = run_steam(cfg, argv)
    detail = {"command": " ".join(argv), "ok": result.get("ok"),
              "action": result.get("action"), "appid": result.get("appid"),
              "warnings": result.get("warnings") or []}
    if not result.get("ok"):
        report.stage("steam", "failed", str(result.get("error"))[:400], **detail)
        if cfg.execute:
            raise InstallerError(EXIT_STEAM, f"steam_shortcut {verb} failed: {result.get('error')}")
        return
    report.stage("steam", verb + "ed" if cfg.execute else "planned",
                 "shortcut registration through steam_shortcut.py", **detail)
    return


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

STAGES = (stage_preflight, stage_runtime, stage_payload, stage_avd, stage_layout,
          stage_jcs2_config, stage_guest, stage_smoke, stage_finalize, stage_steam)


def run(cfg: InstallerConfig, report: Report) -> None:
    for stage in STAGES:
        stage(cfg, report)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = build_config(args)
    except InstallerError as error:
        print(json.dumps({"ok": False, "error": error.message}, indent=2), file=sys.stderr)
        return error.code
    except bootstrap.BootstrapError as error:
        print(json.dumps({"ok": False, "error": error.message}, indent=2), file=sys.stderr)
        return error.code if error.code else EXIT_CONFIG
    report = Report("execute" if cfg.execute else "dry-run", cfg)
    failure = None
    try:
        run(cfg, report)
    except InstallerError as error:
        failure = error
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    if cfg.json_output:
        document = report.document()
        if failure is not None:
            document["error"] = {"code": failure.code, "message": failure.message}
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        sys.stdout.write(report.render())
        if failure is not None:
            print(f"error: {failure.message}", file=sys.stderr)
    if failure is not None:
        return failure.code
    if report.blocking:
        return EXIT_INCOMPLETE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

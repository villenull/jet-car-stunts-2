#!/usr/bin/env python3
"""Linux fresh-guest bootstrap for JCS2 (offline; dry-run by default).

The repo explicitly lacks a Linux fresh-guest bootstrap (packaging/README.md:
"a fresh Android guest bootstrap is ... unresolved"). This script is that
deliverable, modeled stage-for-stage on the Windows launcher
(launcher/launcher.cpp wmain + launcher/logic.hpp) with Linux values taken
from the preserved-guest runner (linux-launcher/runner.py):

  stage 0  config gate + identity (pkg + 5 split sha256 + helper sha256)
  stage 1  space + license gate (plan.py inventory + disk_usage; refuse
           without --accept-licenses)
  stage 2  port gate (bind-probe adb/console/console+1, fail closed)
  stage 3  AVD claim (owned marker; claim only an empty avd_home; Linux
           config.ini with hw.gpu.mode=host; pending bootstrap marker)
  execute  owned adb nodaemon server, emulator argv mirroring runner.py,
           exact-`1` boot poll with early-exit detection, ISOLATION_SCRIPT,
           uid==2000 + /dev/uhid gate, single install-multiple with
           .jcs2-install-state skip marker, helper JAR push, game launch.

Default is dry-run: print the plan, exit 0, touch nothing. Only --execute
mutates. Stdlib only, no network, no live action unless --execute is passed.

Exit-code taxonomy (mirrors the Windows launcher):
  0 ok   2 config/staging/identity   3 ports   4 job/server/AVD-claim
  5 emulator early-exit   6 boot timeout   7 isolation   8 uid/uhid
  10 install   11 install marker   12 helper/launch   13 bootstrap marker
  130 interrupted (Ctrl+C kills only owned pgids)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = "com.trueaxis.jetcarstunts2"
DEFAULT_AVD_NAME = "jcs2-fresh"
REFUSED_AVD_NAMES = frozenset({"hardened_api28"})
EXPECTED_SPLITS = (
    "base.apk",
    "split_config.armeabi_v7a.apk",
    "split_config.en.apk",
    "split_config.es.apk",
    "split_config.xhdpi.apk",
)
# Monolithic single-APK installs (the new pristine
# com.trueaxis.jetcarstunts2.apk) carry every density/ABI in one file.
# The collector below accepts EXACTLY the 5 legacy splits OR exactly one
# .apk (any name); install_argv picks install-multiple vs install.
HELPER_REMOTE_PATH = "/data/local/tmp/jcs2-input-helper.jar"  # controller/helper.go
IMAGE_REL = Path("system-images/android-28/google_apis/x86")
DEFAULT_ADB_PORT = 5038  # linux-launcher/runner.py ADB_PORT
DEFAULT_CONSOLE_PORT = 5594  # linux-launcher/runner.py CONSOLE_PORT
OWNED_MARKER = ".jcs2-owned"
INSTALL_STATE = ".jcs2-install-state"
BOOTSTRAP_STATE = ".jcs2-bootstrap-state"
LICENSE_REL = Path("packaging/installer/licenses")

# linux-launcher/runner.py ISOLATION_SCRIPT, verbatim semantics.
ISOLATION_SCRIPT = (
    "svc wifi disable; svc data disable; "
    "for i in $(ip -o link | awk -F': ' '$2 != \"lo\" {print $2}' | cut -d@ -f1); "
    "do ip link set \"$i\" down || exit 41; ip addr flush dev \"$i\" || exit 42; done; "
    "ip route flush table all || exit 43; ip -6 route flush table all || exit 44; "
    "test -e /dev/uhid || exit 45; chmod 660 /dev/uhid || exit 46"
)

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_PORTS = 3
EXIT_CLAIM = 4
EXIT_EMULATOR = 5
EXIT_BOOT = 6
EXIT_ISOLATION = 7
EXIT_UID = 8
EXIT_INSTALL = 10
EXIT_INSTALL_MARKER = 11
EXIT_LAUNCH = 12
EXIT_BOOTSTRAP_MARKER = 13
EXIT_INTERRUPTED = 130

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent


def _load_plan_module():
    """Reuse packaging/plan.py inventory() for the destination size estimate."""
    spec = importlib.util.spec_from_file_location(
        "jcs2_bootstrap_plan", _HERE / "plan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_plan = _load_plan_module()


class BootstrapError(Exception):
    """Fail-closed bootstrap failure carrying the process exit code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _load_qt_settings_module():
    """Load the shared emulator Qt settings helper from the repo root."""
    spec = importlib.util.spec_from_file_location(
        "jcs2_qt_settings", _REPO_ROOT / "linux-launcher" / "qt_settings.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load linux-launcher/qt_settings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_qt_settings = _load_qt_settings_module()


# ---------------------------------------------------------------------------
# Pure helpers (no filesystem mutation; safe to unit-test with fakes).
# ---------------------------------------------------------------------------

def validate_avd_name(name: str) -> str:
    """Refuse the personal hardened guest and any path-escaping name."""
    if not name or name in (".", "..") or name in REFUSED_AVD_NAMES:
        raise BootstrapError(EXIT_CONFIG, f"refusing AVD name: {name!r}")
    if any(c in name for c in "/\\\x00\n\r"):
        raise BootstrapError(EXIT_CONFIG, f"refusing AVD name with path chars: {name!r}")
    return name


def validate_ports(adb_port: int, console_port: int) -> tuple[int, int]:
    """Mirror logic.hpp valid_port_pair: adb 1024-65535, console even in emulator range."""
    if not 1024 <= adb_port <= 65535:
        raise BootstrapError(EXIT_CONFIG, f"adb port out of range: {adb_port}")
    if not 5554 <= console_port <= 65532 or console_port & 1:
        raise BootstrapError(
            EXIT_CONFIG, f"console port must be even and in 5554-65532: {console_port}")
    if adb_port == console_port or adb_port == console_port + 1:
        raise BootstrapError(
            EXIT_CONFIG, f"adb port {adb_port} collides with console pair "
            f"{console_port}/{console_port + 1}")
    return adb_port, console_port


def serial_for(console_port: int) -> str:
    return f"127.0.0.1:{console_port + 1}"


def collect_split_paths(apks_dir: Path) -> list[Path]:
    """Require EXACTLY the 5 known splits OR exactly one monolith .apk.

    Monolith mode: a single .apk of any name (e.g. signed-monolith.apk);
    extras or gaps are refused in both modes.
    """
    if not apks_dir.is_dir():
        raise BootstrapError(EXIT_CONFIG, f"apks dir is not a directory: {apks_dir}")
    found = sorted(p.name for p in apks_dir.iterdir()
                   if p.is_file() and p.suffix == ".apk")
    if len(found) == 1:
        single = apks_dir / found[0]
        if not single.is_file():
            raise BootstrapError(EXIT_CONFIG, f"apk is not a regular file: {single}")
        return [single]
    if found != sorted(EXPECTED_SPLITS):
        raise BootstrapError(
            EXIT_CONFIG,
            f"apks dir must contain exactly the 5 splits {list(EXPECTED_SPLITS)} "
            f"or exactly one monolith .apk; found {found}")
    paths = [apks_dir / name for name in EXPECTED_SPLITS]
    for path in paths:
        if not path.is_file():
            raise BootstrapError(EXIT_CONFIG, f"split is not a regular file: {path}")
    return paths


def validate_controller_trio(controller: Path | None, mapping: Path | None,
                             helper_jar: Path | None) -> None:
    """Controller inputs are all-or-none; when present all must be files."""
    given = [p for p in (controller, mapping, helper_jar) if p is not None]
    if given and len(given) != 3:
        raise BootstrapError(
            EXIT_CONFIG,
            "controller trio is all-or-none: pass --controller, --mapping and "
            "--helper-jar together, or none of them")
    for path in given:
        if not path.is_file():
            raise BootstrapError(EXIT_CONFIG, f"controller file missing: {path}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_identity(package: str, split_paths: list[Path],
                     helper_jar: Path | None = None) -> tuple[str, list[str], str | None]:
    """Identity = package + apk hashes (+ helper hash), like launcher.cpp state.

    Computed BEFORE any mutation; returned as (identity, apk_hashes, helper_hash).
    Accepts 1 (monolith) or 5 (legacy splits) apk paths.
    """
    if len(split_paths) not in (1, 5):
        raise BootstrapError(EXIT_CONFIG,
                             f"apks must be 1 monolith or 5 splits; got {len(split_paths)}")
    apk_hashes = [sha256_file(p) for p in split_paths]
    helper_hash = sha256_file(helper_jar) if helper_jar is not None else None
    lines = [package, *apk_hashes]
    if helper_hash is not None:
        lines.append(helper_hash)
    return "\n".join(lines) + "\n", apk_hashes, helper_hash


def avd_config_text(image_abs: Path, avd_name: str) -> str:
    """Linux config.ini from the logic.hpp avd_config template.

    hw.gpu.mode=swiftshader_indirect: the personal hardened_api28 guest
    carries this value and the Deck trial proved `-gpu host` cannot start
    headless over SSH (Qt xcb abort SIGABRT; no DISPLAY/Wayland in the SSH
    env), while swiftshader_indirect boots to `Boot completed` headless.
    image.sysdir.1 stays ABSOLUTE because the emulator resolves it relative
    to ANDROID_SDK_ROOT, not ANDROID_AVD_HOME.
    """
    return (
        f"AvdId = {avd_name}\n"
        "PlayStore.enabled = false\n"
        "abi.type = x86\n"
        "avd.ini.displayname = JCS2 API28 Google APIs x86\n"
        "avd.ini.encoding = UTF-8\n"
        "fastboot.forceColdBoot = yes\n"
        "fastboot.forceFastBoot = no\n"
        "hw.accelerometer = yes\n"
        "hw.audioInput = no\n"
        # The proven guest has audio output enabled and the lane warns (and
        # loses sound) without it; audio input stays off because nothing needs
        # the microphone. Found by comparing this template against the working
        # guest on 2026-09-20.
        "hw.audioOutput = yes\n"
        "hw.camera.back = none\n"
        "hw.camera.front = none\n"
        "hw.cpu.arch = x86\n"
        "hw.cpu.ncore = 2\n"
        "hw.dPad = no\n"
        "hw.gps = no\n"
        "hw.gpu.enabled = yes\n"
        "hw.gpu.mode = swiftshader_indirect\n"
        "hw.initialOrientation = portrait\n"
        "hw.keyboard = yes\n"
        "hw.lcd.density = 420\n"
        "hw.lcd.height = 1280\n"
        "hw.lcd.width = 800\n"
        "hw.mainKeys = no\n"
        "hw.ramSize = 1536\n"
        "hw.sdCard = no\n"
        "hw.sensors.orientation = no\n"
        "hw.sensors.proximity = no\n"
        f"image.sysdir.1 = {image_abs}\n"
        "image.sysdir.2 = \n"
        "network.latency = none\n"
        "network.speed = full\n"
        "runtime.network.latency = none\n"
        "runtime.network.speed = full\n"
        "showDeviceFrame = no\n"
        "tag.display = Google APIs\n"
        "tag.id = google_apis\n"
        "vm.heapSize = 256\n"
        "disk.dataPartition.size = 6442450944\n"
    )


def avd_pointer_text(guest_abs: Path) -> str:
    return f"path={guest_abs}\n"


def bootstrap_marker_compatible(prior: str, identity: str, fresh_owned: bool) -> bool:
    """Port of logic.hpp bootstrap_marker_compatible."""
    return (prior == identity or prior == "pending\n" + identity
            or (not prior and fresh_owned))


def bootstrap_marker_completed(prior: str, identity: str) -> bool:
    """Port of logic.hpp bootstrap_marker_completed."""
    return prior == identity


def exact_boot_completed(output: str) -> bool:
    """True when `getprop sys.boot_completed` reports exactly `1` (logic.hpp)."""
    return any(line.strip() == "1" for line in output.splitlines())


def exact_uid(output: str, wanted: int) -> bool:
    """Port of runner.py exact_uid."""
    import re
    match = re.search(r"(?:^|\s)uid=(\d+)(?:\(|\s|$)", output)
    return bool(match and int(match.group(1)) == wanted)


def isolated_network(link_up: str, ipv4_routes: str, ipv6_routes: str) -> tuple[bool, str]:
    """Require loopback as the only UP interface and empty route tables."""
    import re
    up = []
    for line in link_up.splitlines():
        match = re.match(r"\s*\d+:\s*([^ :@]+)", line)
        if match:
            up.append(match.group(1))
    external = [name for name in up if name and name != "lo"]
    if external:
        return False, f"non-loopback interfaces are UP: {external}"
    if ipv4_routes.strip() or ipv6_routes.strip():
        return False, "route tables are not empty"
    return True, "only lo is UP; IPv4/IPv6 route tables are empty"


def adb_server_argv(adb: Path, adb_port: int) -> list[str]:
    return [str(adb), "-P", str(adb_port), "nodaemon", "server"]


def adb_client_base(adb: Path, adb_port: int, serial: str) -> list[str]:
    return [str(adb), "-P", str(adb_port), "-s", serial]


def emulator_argv(emulator: Path, avd_name: str, console_port: int,
                  adb_path: Path, headless: bool = False) -> list[str]:
    """Owned ports, snapshots off, net none; swiftshader_indirect GPU.

    runner.py start_emulator uses `-gpu host` for visible Desktop launches.
    The bootstrap uses swiftshader_indirect unconditionally: headless-over-SSH
    `-gpu host` aborts in Qt xcb (SIGABRT, no display in env), while
    swiftshader_indirect boots to `Boot completed` headless (Deck trial).
    """
    argv = [str(emulator), "-avd", avd_name, "-port", str(console_port),
            "-no-snapshot", "-no-boot-anim",
            "-adb-path", str(adb_path),
            "-gpu", "swiftshader_indirect", "-memory", "1536", "-qemu", "-net", "none"]
    argv.insert(argv.index("-qemu"), "-no-window" if headless else "-fixed-scale")
    return argv


def install_argv(adb: Path, adb_port: int, serial: str,
                 split_paths: list[Path]) -> list[str]:
    """Install form: single `install -r --no-streaming` for a monolith APK,
    else the exact progression_choice.py form for legacy splits:
    install-multiple -r --no-streaming + 5 splits."""
    if len(split_paths) == 1:
        return [*adb_client_base(adb, adb_port, serial),
                "install", "-r", "--no-streaming", str(split_paths[0])]
    return [*adb_client_base(adb, adb_port, serial),
            "install-multiple", "-r", "--no-streaming",
            *(str(p) for p in split_paths)]


def read_marker(path: Path) -> tuple[bool, str]:
    try:
        return True, path.read_text(encoding="utf-8")
    except OSError:
        return False, ""


def port_bind_free(port: int, host: str = "127.0.0.1") -> bool:
    """Bind-probe: True when no LISTENER holds the port.

    SO_REUSEADDR=1 so TIME_WAIT residue from our own past runs does not
    read as busy; only an actual listener refuses the bind. Mirrors the
    runner's tcp_open (connect) semantics from the bind side.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        return True
    except OSError:
        return False


def tcp_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.socket() as probe:
            probe.settimeout(timeout)
            return probe.connect_ex((host, port)) == 0
    except OSError:
        return False


def estimate_destination_bytes(paths: list[Path]) -> dict:
    """Destination size estimate reusing plan.py inventory() (read-only)."""
    total = {"logical_bytes": 0, "allocated_bytes": 0, "files": 0}
    for path in paths:
        if path.exists():
            inv = _plan.inventory(path)
            total["logical_bytes"] += inv["logical_bytes"]
            total["allocated_bytes"] += inv["allocated_bytes"]
            total["files"] += inv["files"]
    return total


def nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return candidate
        candidate = parent
    return candidate


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------

@dataclass
class BootstrapConfig:
    root: Path
    sdk: Path
    adb: Path
    emulator: Path
    image_dir: Path
    avd_home: Path
    avd_name: str
    apks_dir: Path
    split_paths: list[Path]
    controller: Path | None
    mapping: Path | None
    helper_jar: Path | None
    adb_port: int
    console_port: int
    serial: str
    accept_licenses: bool
    execute: bool
    headless: bool
    identity: str = ""
    apk_hashes: list[str] = field(default_factory=list)
    helper_hash: str | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=_REPO_ROOT,
                        help="repo root; relative paths resolve against it")
    parser.add_argument("--sdk", type=Path, default=None)
    parser.add_argument("--avd-home", type=Path, default=None)
    parser.add_argument("--avd-name", default=DEFAULT_AVD_NAME,
                        help=f"fresh guest name (refuses {sorted(REFUSED_AVD_NAMES)})")
    parser.add_argument("--apks-dir", type=Path, default=None)
    parser.add_argument("--helper-jar", type=Path, default=None)
    parser.add_argument("--controller", type=Path, default=None)
    parser.add_argument("--mapping", type=Path, default=None)
    parser.add_argument("--adb-port", type=int, default=DEFAULT_ADB_PORT)
    parser.add_argument("--console-port", type=int, default=DEFAULT_CONSOLE_PORT)
    parser.add_argument("--accept-licenses", action="store_true",
                        help="accept SDK/third-party licenses (required for --execute)")
    parser.add_argument("--execute", action="store_true",
                        help="perform the bootstrap; default is dry-run")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and exit 0 without touching anything")
    parser.add_argument("--headless", action="store_true",
                        help="pass -no-window to the emulator")
    return parser.parse_args(argv)


def _resolve(root: Path, value: Path | None, default_rel: str) -> Path:
    candidate = Path(default_rel) if value is None else value
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate


def load_config(args: argparse.Namespace) -> BootstrapConfig:
    """Validate stage 0 (config/staging/identity). Read-only; raises 2 on failure."""
    root = Path(args.root).resolve()
    sdk = _resolve(root, args.sdk, "runtime/sdk")
    avd_home = _resolve(root, args.avd_home, "state/avd")
    apks_dir = _resolve(root, args.apks_dir, "staging/apks")

    def opt(value: Path | None) -> Path | None:
        if value is None:
            return None
        return value if value.is_absolute() else root / value

    controller, mapping, helper_jar = (opt(args.controller), opt(args.mapping),
                                       opt(args.helper_jar))

    avd_name = validate_avd_name(args.avd_name)
    adb_port, console_port = validate_ports(args.adb_port, args.console_port)
    validate_controller_trio(controller, mapping, helper_jar)

    adb = sdk / "platform-tools" / "adb"
    emulator = sdk / "emulator" / "emulator"
    if not adb.is_file():
        raise BootstrapError(EXIT_CONFIG, f"missing adb executable: {adb}")
    if not emulator.is_file():
        raise BootstrapError(EXIT_CONFIG, f"missing emulator executable: {emulator}")
    image_dir = sdk / IMAGE_REL
    if not image_dir.is_dir():
        raise BootstrapError(EXIT_CONFIG, f"missing system image dir: {image_dir}")
    if not (image_dir / "source.properties").is_file():
        raise BootstrapError(EXIT_CONFIG,
                             f"missing image source.properties: {image_dir}")

    split_paths = collect_split_paths(apks_dir)
    # Identity is computed BEFORE any mutation.
    identity, apk_hashes, helper_hash = compute_identity(
        PACKAGE, split_paths, helper_jar)

    execute = bool(args.execute and not args.dry_run)
    return BootstrapConfig(
        root=root, sdk=sdk, adb=adb, emulator=emulator, image_dir=image_dir,
        avd_home=avd_home, avd_name=avd_name, apks_dir=apks_dir,
        split_paths=split_paths, controller=controller, mapping=mapping,
        helper_jar=helper_jar, adb_port=adb_port, console_port=console_port,
        serial=serial_for(console_port), accept_licenses=bool(args.accept_licenses),
        execute=execute, headless=bool(args.headless),
        identity=identity, apk_hashes=apk_hashes, helper_hash=helper_hash)


def license_files(root: Path) -> list[Path]:
    lic_dir = root / LICENSE_REL
    if not lic_dir.is_dir():
        return []
    return sorted(p for p in lic_dir.iterdir() if p.is_file())


def check_space_and_licenses(cfg: BootstrapConfig) -> dict:
    """Stage 1 gate (read-only probe). Raises 2 when --execute cannot proceed."""
    estimate = estimate_destination_bytes([cfg.sdk, cfg.apks_dir])
    anchor = nearest_existing(cfg.avd_home)
    free = shutil.disk_usage(anchor).free
    licenses = license_files(cfg.root)
    report = {"logical_bytes": estimate["logical_bytes"],
              "allocated_bytes": estimate["allocated_bytes"],
              "files": estimate["files"],
              "free_bytes": free,
              "licenses": [str(p) for p in licenses]}
    if cfg.execute:
        if not cfg.accept_licenses:
            raise BootstrapError(
                EXIT_CONFIG,
                "refusing without --accept-licenses; license texts live under "
                f"{LICENSE_REL} ({len(licenses)} files found)")
        if not licenses:
            raise BootstrapError(EXIT_CONFIG, "no license texts found to accept")
        if free < estimate["logical_bytes"]:
            raise BootstrapError(
                EXIT_CONFIG,
                f"insufficient free space at {anchor}: free={free} < "
                f"logical={estimate['logical_bytes']}; copy sparsely "
                "(README storage lesson: ~11GiB logical / ~5GiB allocated)")
    return report


def check_ports(cfg: BootstrapConfig) -> list[int]:
    """Stage 2 gate: bind-probe adb/console/console+1 before any mutation."""
    ports = [cfg.adb_port, cfg.console_port, cfg.console_port + 1]
    busy = [p for p in ports if not port_bind_free(p)]
    if busy and cfg.execute:
        raise BootstrapError(EXIT_PORTS,
                             f"ports busy, no mutation performed: {busy}")
    return busy


def render_plan(cfg: BootstrapConfig, space: dict, busy: list[int]) -> str:
    lines = [
        "jcs2 linux fresh-guest bootstrap plan (dry-run; nothing was touched)",
        f"root={cfg.root}",
        f"sdk={cfg.sdk}",
        f"adb={cfg.adb}",
        f"emulator={cfg.emulator}",
        f"image={cfg.image_dir}",
        f"avd_home={cfg.avd_home}",
        f"avd_name={cfg.avd_name}",
        f"serial={cfg.serial} adb_port={cfg.adb_port} console_port={cfg.console_port}",
        f"apks_dir={cfg.apks_dir}",
        f"helper_jar={cfg.helper_jar}",
        f"controller={cfg.controller}",
        f"mapping={cfg.mapping}",
        f"accept_licenses={cfg.accept_licenses}",
        f"execute={cfg.execute}",
        f"package={PACKAGE}",
    ]
    for path, digest in zip(cfg.split_paths, cfg.apk_hashes):
        lines.append(f"sha256 {digest}  {path.name}")
    if cfg.helper_hash is not None:
        lines.append(f"sha256 {cfg.helper_hash}  helper")
    lines.append(f"estimate_logical_bytes={space['logical_bytes']}")
    lines.append(f"estimate_allocated_bytes={space['allocated_bytes']}")
    lines.append(f"free_bytes={space['free_bytes']}")
    lines.append("note=copy sparsely; logical size exceeds allocated "
                 "(~11GiB logical / ~5GiB allocated per README storage lesson)")
    lines.append(f"licenses={len(space['licenses'])} files under {LICENSE_REL}")
    lines.append(f"busy_ports={busy if busy else 'none'}")
    lines.append(f"emulator_argv={' '.join(emulator_argv(cfg.emulator, cfg.avd_name, cfg.console_port, cfg.adb, cfg.headless))}")
    install = install_argv(cfg.adb, cfg.adb_port, cfg.serial, cfg.split_paths)
    lines.append(f"install_argv={' '.join(install)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Execute path (--execute only). Everything below mutates or launches.
# ---------------------------------------------------------------------------

class OwnedChildren:
    """Owned subprocesses, each in its own pgid; Ctrl+C kills only these."""

    def __init__(self):
        self._pgids: list[tuple[str, int]] = []

    def spawn(self, name: str, argv: list[str], log_path: Path,
              env: dict[str, str]) -> subprocess.Popen:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")
        try:
            proc = subprocess.Popen(argv, stdout=log_file, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True, env=env)
        except Exception:
            log_file.close()
            raise
        self._pgids.append((name, proc.pid))
        return proc

    def kill_all(self, timeout: float = 20.0) -> None:
        for _, pgid in reversed(self._pgids):
            self._stop_pgid(pgid, timeout)

    def stop(self, name: str, timeout: float = 20.0) -> None:
        """Stop one owned child by name, keep the rest."""
        for child, pgid in self._pgids:
            if child == name:
                self._stop_pgid(pgid, timeout)

    @staticmethod
    def _stop_pgid(pgid: int, timeout: float) -> None:
        """Terminate a process group gracefully, then forcefully.

        This used to go straight to SIGKILL. An emulator killed that way can
        leave guest writes unflushed, which is what a fresh guest showed on
        2026-09-20 (an extracted native library that came back with 'bad ELF
        magic' after the post-install failure path SIGKILLed it). SIGTERM plus
        a bounded wait first, SIGKILL only if the group outlives the deadline;
        ownership and port cleanup are unchanged because the whole group is
        still signalled.
        """
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (OSError, ProcessLookupError):
                return
            time.sleep(0.2)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

def _base_env(cfg: BootstrapConfig) -> dict[str, str]:
    env = os.environ.copy()
    env.update({"ANDROID_HOME": str(cfg.sdk),
                "ANDROID_SDK_ROOT": str(cfg.sdk),
                "ANDROID_AVD_HOME": str(cfg.avd_home),
                "ANDROID_ADB_SERVER_PORT": str(cfg.adb_port)})
    return env


def claim_avd(cfg: BootstrapConfig) -> tuple[Path, Path, bool]:
    """Stage 3: claim or keep the owned guest. Raises 4 on refusal/failure."""
    guest = cfg.avd_home / f"{cfg.avd_name}.avd"
    pointer = cfg.avd_home / f"{cfg.avd_name}.ini"
    owned = guest / OWNED_MARKER
    has_marker, text = read_marker(owned)
    if has_marker:
        if text != cfg.avd_name + "\n":
            raise BootstrapError(EXIT_CLAIM,
                                 f"owned marker mismatch at {owned}; refusing foreign guest")
        if not (guest / "config.ini").is_file() or not pointer.is_file():
            raise BootstrapError(EXIT_CLAIM, "owned guest is missing config.ini or pointer")
        return guest, pointer, False
    # A pre-existing guest WITHOUT a marker is adoptable only when it has
    # never booted: no qcow2 overlays, no hardware-qemu.ini, no data dir,
    # no multiinstance lock. Anything that has already run keeps its
    # foreign state; booting there could resume unknown userdata.
    if guest.is_dir() and (guest / "config.ini").is_file() and pointer.is_file():
        booted = [p.name for p in guest.iterdir()
                  if p.suffix == ".qcow2" or p.name in (
                      "hardware-qemu.ini", "data", "multiinstance.lock",
                      "bootcompleted.ini", "version_num.cache")]
        if booted:
            raise BootstrapError(EXIT_CLAIM,
                                 f"refusing to adopt booted foreign guest {guest} "
                                 f"(state: {', '.join(sorted(booted))}); "
                                 "remove it or use a fresh avd_home")
        try:
            owned.write_text(cfg.avd_name + "\n", encoding="utf-8")
        except OSError as exc:
            raise BootstrapError(EXIT_CLAIM, f"AVD adopt failed: {exc}") from exc
        return guest, pointer, True
    # Only an empty, fresh state root may be claimed.
    if cfg.avd_home.exists() and any(cfg.avd_home.iterdir()):
        raise BootstrapError(EXIT_CLAIM,
                             f"refusing to claim non-empty avd_home without owned marker: {cfg.avd_home}")
    try:
        guest.mkdir(parents=True, exist_ok=True)
        if not cfg.image_dir.is_dir() or not (cfg.image_dir / "source.properties").is_file():
            raise BootstrapError(EXIT_CLAIM, f"system image vanished: {cfg.image_dir}")
        (guest / "config.ini").write_text(
            avd_config_text(cfg.image_dir.resolve(), cfg.avd_name), encoding="utf-8")
        pointer.write_text(avd_pointer_text(guest.resolve()), encoding="utf-8")
        owned.write_text(cfg.avd_name + "\n", encoding="utf-8")
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(EXIT_CLAIM, f"AVD claim failed: {exc}") from exc
    return guest, pointer, True

def check_bootstrap_marker(cfg: BootstrapConfig, fresh_owned: bool) -> bool:
    """Pre-boot marker gate (logic.hpp compatible/completed). Raises 13."""
    state_path = cfg.avd_home / BOOTSTRAP_STATE
    has_prior, prior = read_marker(state_path)
    if has_prior and not prior:
        raise BootstrapError(EXIT_BOOTSTRAP_MARKER,
                             "bootstrap marker is empty; refusing migration")
    # A claimed guest whose .jcs2-owned marker carries THIS run's AVD name
    # is a fresh claim even when the avd_home dir itself pre-existed
    # (e.g. an earlier failed run wrote config + owned marker but exited
    # before the pending-marker write): missing marker means proceed, not
    # a restore. Adopted-but-never-booted guests behave identically.
    if not has_prior and guest_freshly_claimed(cfg):
        return False
    # Adopted-but-never-booted guests (fresh_owned via adopt path) start
    # with no marker exactly like a newly claimed home.
    if not has_prior and fresh_owned:
        return False
    if not bootstrap_marker_compatible(prior if has_prior else "", cfg.identity, fresh_owned):
        if has_prior:
            raise BootstrapError(EXIT_BOOTSTRAP_MARKER,
                                 "bootstrap marker mismatch; refusing migration")
        raise BootstrapError(EXIT_BOOTSTRAP_MARKER,
                             "bootstrap marker missing on existing owned state; refusing restore")
    return bootstrap_marker_completed(prior if has_prior else "", cfg.identity)


def guest_freshly_claimed(cfg: BootstrapConfig) -> bool:
    """True when the owned marker names this run's AVD and the guest never booted."""
    guest = cfg.avd_home / f"{cfg.avd_name}.avd"
    pointer = cfg.avd_home / f"{cfg.avd_name}.ini"
    owned = guest / OWNED_MARKER
    has_marker, text = read_marker(owned)
    if not has_marker or text != cfg.avd_name + "\n":
        return False
    if not (guest / "config.ini").is_file() or not pointer.is_file():
        return False
    booted = [p for p in guest.iterdir()
              if p.suffix == ".qcow2" or p.name in (
                  "hardware-qemu.ini", "data", "multiinstance.lock",
                  "bootcompleted.ini", "version_num.cache")]
    return not booted


def write_pending_marker(cfg: BootstrapConfig) -> None:
    """Write pending identity before first boot (logic.hpp semantics)."""
    state_path = cfg.avd_home / BOOTSTRAP_STATE
    has_prior, _ = read_marker(state_path)
    if not has_prior:
        try:
            state_path.write_text("pending\n" + cfg.identity, encoding="utf-8")
        except OSError as exc:
            raise BootstrapError(EXIT_BOOTSTRAP_MARKER,
                                 f"pending marker write failed: {exc}") from exc

def _run_adb(cfg: BootstrapConfig, env: dict[str, str],
             *args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run([*adb_client_base(cfg.adb, cfg.adb_port, cfg.serial), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def _wait_for_device_state(cfg: BootstrapConfig, env: dict[str, str],
                           timeout: float, code: int, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = _run_adb(cfg, env, "get-state", timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip() == "device":
            return
        time.sleep(1)
    raise BootstrapError(code, f"{label} ADB reconnect timeout")


def _apk_native_libs(split_paths) -> dict:
    """Basename -> sha256 for every lib/<abi>/*.so inside the shipped APK(s)."""
    import zipfile
    libs: dict = {}
    for path in split_paths:
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    parts = member.split("/")
                    if len(parts) == 3 and parts[0] == "lib" and member.endswith(".so"):
                        libs[Path(member).name] = hashlib.sha256(archive.read(member)).hexdigest()
        except (OSError, zipfile.BadZipFile):
            continue
    return libs


def _guest_native_lib_dir(cfg: BootstrapConfig, env) -> str:
    """Ask the guest where it keeps extracted native libraries.

    Deliberately not derived from `pm path` by string surgery: the extracted
    library directory is a different path and the guest reports it itself.
    """
    dump = _run_adb(cfg, env, "shell", "dumpsys", "package", PACKAGE, timeout=30)
    if dump.returncode:
        return ""
    for line in dump.stdout.splitlines():
        for key in ("legacyNativeLibraryDir=", "nativeLibraryDir="):
            if key in line:
                value = line.split(key, 1)[1].strip().split()[0]
                if value and value not in ("null", "[]"):
                    return value
    return ""


def verify_guest_native_libs(cfg: BootstrapConfig, env, timeout: float = 90.0) -> None:
    """Tripwire: the guest's extracted native libraries must match the APK.

    Detector only - it cannot repair a guest, it refuses to call one complete.
    Raises 10 (EXIT_INSTALL) when the guest never converges to the shipped
    bytes, so this class of breakage fails the install loudly instead of
    surfacing later as a lane 'game resumed activity timeout'.
    """
    if not cfg.split_paths:
        return
    expected = _apk_native_libs(cfg.split_paths)
    if not expected:
        print("warning: shipped APK exposes no native libraries to verify", flush=True)
        return
    deadline = time.monotonic() + timeout
    found: dict = {}
    resolved = ""
    while time.monotonic() < deadline:
        directory = _guest_native_lib_dir(cfg, env)
        resolved = directory or resolved
        if directory:
            # The platform reports the parent `lib` directory and puts the
            # architecture libraries one level below it (`lib/arm/`,
            # `lib/armeabi-v7a/`), so a non-recursive `lib/*.so` glob found
            # nothing on a guest whose library was present - the check has to
            # look in the ABI subdirectories too.
            listing = _run_adb(cfg, env, "shell", "sha256sum",
                               f"{directory}/*/*.so", f"{directory}/*.so", timeout=30)
            found = {}
            for line in listing.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    found[Path(parts[1]).name] = parts[0]
            if all(found.get(name) == digest for name, digest in expected.items()):
                return
        time.sleep(3)
    # Capture the guest's own view before anything stops the emulator: the
    # directory listing plus `pm path` is what tells "never extracted" apart
    # from "extracted but wrong" from "wrong directory".
    listing = _run_adb(cfg, env, "shell", "ls", "-l", resolved or "/data/app", timeout=20)
    pm_path = _run_adb(cfg, env, "shell", "pm", "path", PACKAGE, timeout=20)
    print(f"native-lib evidence: dir={resolved!r}\n{listing.stdout.strip()}\n"
          f"pm path: {pm_path.stdout.strip()}", flush=True)
    raise BootstrapError(
        EXIT_INSTALL,
        "guest native libraries do not match the shipped APK; refusing to call this "
        f"install complete (expected {sorted(expected)}, guest has {sorted(found)}); "
        f"resolved native library directory: {resolved!r}; "
        f"guest listing: {listing.stdout.strip()[:400]}")


def execute_bootstrap(cfg: BootstrapConfig) -> int:
    if not cfg.accept_licenses:
        raise BootstrapError(EXIT_CONFIG, "refusing without --accept-licenses")
    space = check_space_and_licenses(cfg)
    if space["free_bytes"] < space["logical_bytes"]:
        raise BootstrapError(EXIT_CONFIG, "insufficient free space (see plan warning)")
    check_ports(cfg)
    guest, _, fresh_owned = claim_avd(cfg)
    _ = guest
    complete = check_bootstrap_marker(cfg, fresh_owned)
    # Pending identity is written while the AVD is genuinely new, before
    # first boot/install; it is the only proof a later retry may continue.
    write_pending_marker(cfg)

    owned = OwnedChildren()
    detached = False
    env = _base_env(cfg)
    log_dir = cfg.avd_home / ".jcs2-bootstrap-logs"
    try:
        try:
            server = owned.spawn("adb-server",
                                 adb_server_argv(cfg.adb, cfg.adb_port),
                                 log_dir / "adb-server.log", env)
        except OSError as exc:
            raise BootstrapError(EXIT_CLAIM, f"adb server spawn failed: {exc}") from exc
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise BootstrapError(EXIT_CLAIM, "owned adb server exited during startup")
            if tcp_open("127.0.0.1", cfg.adb_port):
                break
            time.sleep(0.2)
        else:
            raise BootstrapError(EXIT_CLAIM, "owned adb server did not open its port")

        # Seed the exact emulator-wide QSettings key before any visible
        # emulator process can map and display its compatibility warning.
        try:
            settings_path, warning_key, changed = (
                _qt_settings.seed_compatibility_warning_suppression(
                    cfg.avd_name))
        except _qt_settings.QtSettingsError as error:
            raise BootstrapError(EXIT_EMULATOR, str(error)) from error
        print(f"stage=compatibility-warning-seeded path={settings_path} "
              f"key={warning_key} changed={changed}", flush=True)

        try:
            emulator = owned.spawn(
                "emulator",
                emulator_argv(cfg.emulator, cfg.avd_name, cfg.console_port,
                              cfg.adb, cfg.headless),
                log_dir / "emulator.log", env)
        except OSError as exc:
            raise BootstrapError(EXIT_EMULATOR, f"emulator spawn failed: {exc}") from exc

        # Boot loop: exact `1` with early-exit detection (launcher.cpp stages).
        # `adb connect <serial>` first, mirroring runner.py start_emulator:
        # the owned nodaemon server does not auto-attach the emulator's
        # adbd port, so get-state without connect never reaches `device`.
        booted = False
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if emulator.poll() is not None:
                raise BootstrapError(EXIT_EMULATOR, "emulator exited before boot")
            try:
                _run_adb(cfg, env, "connect", cfg.serial, timeout=10)
                state = _run_adb(cfg, env, "get-state", timeout=10)
                boot = _run_adb(cfg, env, "shell", "getprop",
                                "sys.boot_completed", timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                time.sleep(2)
                continue
            if (state.returncode == 0 and state.stdout.strip() == "device"
                    and boot.returncode == 0 and exact_boot_completed(boot.stdout)):
                booted = True
                break
            time.sleep(2)
        if not booted:
            raise BootstrapError(EXIT_BOOT, "bounded emulator boot timeout")

        # Isolation as root, then drop back to shell uid 2000 (runner.py).
        root = _run_adb(cfg, env, "root", timeout=25)
        if root.returncode:
            raise BootstrapError(EXIT_ISOLATION, "adb root failed; refusing unverified isolation")
        _wait_for_device_state(cfg, env, 30, EXIT_ISOLATION, "root")
        if not exact_uid(_run_adb(cfg, env, "shell", "id", timeout=15).stdout, 0):
            raise BootstrapError(EXIT_ISOLATION, "root identity is not uid=0")
        iso = _run_adb(cfg, env, "shell", ISOLATION_SCRIPT, timeout=20)
        if iso.returncode:
            raise BootstrapError(EXIT_ISOLATION, "guest network/UHID isolation failed")
        link = _run_adb(cfg, env, "shell", "ip", "-o", "link", "show", "up", timeout=15).stdout
        ipv4 = _run_adb(cfg, env, "shell", "ip", "-o", "route", "show", timeout=15).stdout
        ipv6 = _run_adb(cfg, env, "shell", "ip", "-6", "-o", "route",
                        "show", timeout=15).stdout
        ok, detail = isolated_network(link, ipv4, ipv6)
        if not ok:
            raise BootstrapError(EXIT_ISOLATION, detail)
        unroot = _run_adb(cfg, env, "unroot", timeout=25)
        _wait_for_device_state(cfg, env, 30, EXIT_UID, "unroot")
        shell_id = _run_adb(cfg, env, "shell", "id", timeout=15).stdout
        if unroot.returncode or not exact_uid(shell_id, 2000):
            raise BootstrapError(EXIT_UID, f"unroot did not produce shell uid=2000: {shell_id.strip()}")
        access = _run_adb(cfg, env, "shell", "sh", "-c",
                          "id; test \"$(id -u)\" = 2000 && test -r /dev/uhid "
                          "&& test -w /dev/uhid && ls -l /dev/uhid", timeout=15)
        if access.returncode or not exact_uid(access.stdout, 2000):
            raise BootstrapError(EXIT_UID, "shell UID 2000 cannot read/write /dev/uhid")

        # Install with skip marker (pkg+hashes identity + `pm path` reverify).
        state_path = cfg.avd_home / INSTALL_STATE
        has_old, old = read_marker(state_path)
        skip = has_old and old == cfg.identity
        if skip:
            verify = _run_adb(cfg, env, "shell", "pm", "path", PACKAGE, timeout=15)
            skip = verify.returncode == 0 and "package:" in verify.stdout
        if not skip:
            install = subprocess.run(
                install_argv(cfg.adb, cfg.adb_port, cfg.serial, cfg.split_paths),
                capture_output=True, text=True, timeout=180, env=env)
            if install.returncode:
                raise BootstrapError(EXIT_INSTALL,
                                     f"install failed: {(install.stderr or install.stdout).strip()[:500]}")
            try:
                state_path.write_text(cfg.identity, encoding="utf-8")
            except OSError as exc:
                raise BootstrapError(EXIT_INSTALL_MARKER,
                                     f"install-state write failed: {exc}") from exc

        # Helper JAR push, then launch (helper.go remote path; monkey like runner).
        if cfg.helper_jar is not None:
            push = subprocess.run(
                [*adb_client_base(cfg.adb, cfg.adb_port, cfg.serial),
                 "push", str(cfg.helper_jar), HELPER_REMOTE_PATH],
                capture_output=True, text=True, timeout=120, env=env)
            if push.returncode:
                raise BootstrapError(EXIT_LAUNCH, "helper JAR push failed")
        launched = _run_adb(cfg, env, "shell", "monkey", "-p", PACKAGE, "1", timeout=30)
        if launched.returncode:
            raise BootstrapError(EXIT_LAUNCH, "game launch failed")

        # The first launch is also when the guest extracts the APK's native
        # libraries. Verify them before this install may be called complete:
        # the marker used to be written before the launch, so a later failure
        # (or an abrupt stop) could leave a guest that was "complete" on paper
        # with a half-written library, which is what a fresh guest showed on
        # 2026-09-20 ("bad ELF magic" on libtrueaxis.so).
        verify_guest_native_libs(cfg, env)

        if not complete:
            try:
                (cfg.avd_home / BOOTSTRAP_STATE).write_text(cfg.identity, encoding="utf-8")
            except OSError as exc:
                raise BootstrapError(EXIT_BOOTSTRAP_MARKER,
                                     f"bootstrap completion write failed: {exc}")

        print(f"JCS2 bootstrapped on {cfg.serial} with owned ADB port {cfg.adb_port}",
              flush=True)
        # Setup tool, not a launcher: leave the guest running for the
        # caller to verify, stop only the owned ADB server. The runner
        # owns supervision; blocking here held the trial's SSH session
        # open 1500 s past a completed install (Deck evidence).
        owned.stop("adb-server")
        detached = True
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    finally:
        # Ctrl+C (or any failure) kills only owned pgids, never host processes.
        # On success the emulator is deliberately left running and detached.
        if not detached:
            owned.kill_all()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args)
    except BootstrapError as exc:
        print(f"stage=config: {exc.message}", file=sys.stderr)
        return exc.code
    # Read-only probes; in dry-run they only inform the printed plan.
    try:
        space = check_space_and_licenses(cfg)
    except BootstrapError as exc:
        print(f"stage=space-licenses: {exc.message}", file=sys.stderr)
        return exc.code
    try:
        busy = check_ports(cfg)
    except BootstrapError as exc:
        print(f"stage=ports: {exc.message}", file=sys.stderr)
        return exc.code

    if not cfg.execute:
        sys.stdout.write(render_plan(cfg, space, busy))
        return EXIT_OK
    try:
        return execute_bootstrap(cfg)
    except BootstrapError as exc:
        print(f"stage=failed: {exc.message}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())

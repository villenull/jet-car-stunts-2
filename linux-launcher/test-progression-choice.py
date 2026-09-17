#!/usr/bin/env python3
"""Offline tests for the explicit unlock-all choice (fakes + tmp only).

Uses ACTUAL captured device formats (analysis/maps-live-20260913/
dumpsys-package.log, analysis/.../runtime/package-paths.txt):
  versionCode=29 minSdk=15 targetSdk=28
  signatures=PackageSignatures{b41e49e version:1, signatures:[2b86a8d5], ...}
  package:/data/app/<pkg>-<id>==/<split>.apk
The PackageSignatures token is opaque (never cert material): all cert
truth comes from pulled bytes + extractor. FakeTransport models real
platform semantics (failed update keeps old bytes; success replaces).
No emulator, ADB, display, guest action, or deploy.
"""
import fcntl
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import progression_choice as pc

# Captured reality (see module docstring for sources).
DUMPSYS_REAL = (
    "    versionCode=29 minSdk=15 targetSdk=28\n"
    "    versionName=1.0.23\n"
    "    splits=[base, config.armeabi_v7a, config.en, config.es, config.xhdpi]\n"
    "    --\n"
    "    signatures=PackageSignatures{b41e49e version:1, signatures:[2b86a8d5], past signatures:[]}\n"
)
DUMPSYS_V28 = DUMPSYS_REAL.replace("versionCode=29", "versionCode=28")

FP_OTHER = "AA:BB:CC:DD"
DER_TARGET = "AB12" * 512
DER_OTHER = "FFFF" * 512
# Self-consistent test pin: the fake extractor's fp MUST equal
# sha256(fake DER), mirroring the production invariant. The module pin
# is patched to it; production DB86 pinning is proven separately
# against real artifacts (see review notes).
FP_TARGET = pc._fp_of(DER_TARGET)
pc.EXPECTED_CERT_FINGERPRINT = FP_TARGET
BYTES_FP = {}  # sha256hex -> fingerprint (fake extractor registry)


def test_reader(path):
    data = Path(path).read_bytes()
    import hashlib
    fp = BYTES_FP.get(hashlib.sha256(data).hexdigest(), "UNREGISTERED:FP")
    return fp, DER_TARGET if fp == FP_TARGET else DER_OTHER


def register_blob(data: bytes, fp: str):
    import hashlib
    BYTES_FP[hashlib.sha256(data).hexdigest()] = fp


def make_variant(tmp: Path, name: str, *, cert=FP_TARGET,
                 version=pc.EXPECTED_VERSION_CODE, tamper=None,
                 drop=None, split_fp=None, claim=None, tag="") -> pc.VariantDescriptor:
    root = tmp / f"variant-{name}-{tag or len(BYTES_FP)}"
    root.mkdir(parents=True, exist_ok=True)
    for split in pc.EXPECTED_SPLITS:
        if split == drop:
            continue
        data = f"{name}:{tag}:{split}".encode() if split != tamper else b"tampered-bytes"
        (root / split).write_bytes(data)
        register_blob(data, (split_fp or {}).get(split, cert))
    real = {s: pc.sha256_file(root / s) for s in pc.EXPECTED_SPLITS
            if (root / s).is_file()}
    if tamper is not None:
        real[tamper] = "0" * 64
    return pc.VariantDescriptor(name=name, apks_dir=str(root), hashes=real,
                                cert_fingerprint=claim if claim is not None else cert,
                                version_code=version)


def plan(current, target, **kw):
    kw.setdefault("cert_reader", test_reader)
    return pc.plan_switch(current, target, **kw)


OWN_OK = {"serial_scoped": True, "locks_held": True,
          "emulator_owned": True, "game_not_started": True}


def make_session(serial="emulator-5667"):
    return pc.OwnedSession(serial=serial, adb_prefix=("adb",),
                           avd_name="test-avd", console_port=5666,
                           endpoint_lock="/tmp/owned-e.lock",
                           profile_lock="/tmp/owned-p.lock",
                           game_started=False)


def pm_text(*, splits=None, marker="AbC123=="):
    splits = pc.EXPECTED_SPLITS if splits is None else splits
    return "".join(
        f"package:/data/app/{pc.PACKAGE}-{marker}/{s}\n" for s in splits)


class FakeTransport:
    """Models platform install semantics with pulled-byte truth.

    installed: {basename: bytes} served on pull; install-multiple rc=0
    atomically replaces installed with the target files read from the
    install args; rc!=0 (or post_garbage) leaves/twists device bytes.
    dumpsys/pm responses follow the captured format.
    """

    def __init__(self, *, installed=None, dumpsys=DUMPSYS_REAL,
                 install_rc=0, install_out="Success", post_garbage=False):
        self.calls = []
        self.scratch_seen = []
        self.installed = dict(installed or {})
        self.dumpsys = dumpsys
        self.install_rc = install_rc
        self.install_out = install_out
        self.post_garbage = post_garbage

    def _pm(self):
        if not self.installed:
            return ""
        return "".join(
            f"package:/data/app/{pc.PACKAGE}-AbC123==/{name}\n"
            for name in sorted(self.installed))

    def __call__(self, cmd, *, env=None, binary=False, scratch_dir=None):
        self.calls.append(cmd)
        if "dumpsys" in cmd:
            return 0, self.dumpsys
        if "pm" in cmd:
            return 0, self._pm()
        if "pull" in cmd:
            self.scratch_seen.append(scratch_dir)
            remote = cmd[-1]
            name = os.path.basename(remote)
            if name not in self.installed:
                return 1, b""
            return 0, self.installed[name]
        if "install-multiple" in cmd:
            if self.install_rc != 0:
                return self.install_rc, self.install_out
            for arg in cmd:
                if arg.endswith(".apk"):
                    with open(arg, "rb") as handle:
                        self.installed[os.path.basename(arg)] = handle.read()
            if self.post_garbage:
                self.installed["base.apk"] = b"garbage-bytes"
            return 0, self.install_out
        return 1, "unexpected adb call"


def stage_variant(variants_root: Path, choice: str, desc: pc.VariantDescriptor):
    dest = variants_root / choice
    (dest / "apks-signed").mkdir(parents=True, exist_ok=True)
    hashes = {}
    for split in pc.EXPECTED_SPLITS:
        data = Path(desc.apks_dir, split).read_bytes()
        (dest / "apks-signed" / split).write_bytes(data)
        register_blob(data, desc.cert_fingerprint)
        import hashlib
        hashes[split] = hashlib.sha256(data).hexdigest()
    (dest / "variant.json").write_text(json.dumps({
        "apks_dir": "apks-signed", "hashes": hashes,
        "cert_fingerprint": desc.cert_fingerprint,
        "version_code": desc.version_code}), encoding="utf-8")


class DefaultTests(unittest.TestCase):
    """Unlock-default policy (Sept 14): no selector; unlock-all is the
    default for absent/malformed/unknown choice content."""

    def test_absent_file_is_unlock_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pc.read_choice(Path(tmp) / "missing.json"), pc.CHOICE_UNLOCK_ALL)

    def test_malformed_json_is_unlock_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            target.write_text("{not json", encoding="utf-8")
            self.assertEqual(pc.read_choice(target), pc.CHOICE_UNLOCK_ALL)

    def test_unknown_value_is_unlock_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            target.write_text(json.dumps({"choice": "everything-unlocked-please"}), encoding="utf-8")
            self.assertEqual(pc.read_choice(target), pc.CHOICE_UNLOCK_ALL)

    def test_non_dict_is_unlock_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            target.write_text(json.dumps(["unlock-all"]), encoding="utf-8")
            self.assertEqual(pc.read_choice(target), pc.CHOICE_UNLOCK_ALL)

    def test_stored_normal_reads_back_verbatim_but_policy_default_is_unlock(self):
        # A legacy stored "normal" still reads back (receipt honesty) while
        # the policy default for anything else is unlock-all.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            pc.set_choice(target, pc.CHOICE_NORMAL, explicit=True)
            self.assertEqual(pc.read_choice(target), pc.CHOICE_NORMAL)
            self.assertEqual(pc.POLICY_DEFAULT_CHOICE, pc.CHOICE_UNLOCK_ALL)


class ExplicitGateTests(unittest.TestCase):
    def test_change_requires_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            with self.assertRaises(pc.ChoiceError):
                pc.set_choice(target, pc.CHOICE_UNLOCK_ALL, explicit=False)
            self.assertFalse(target.exists())

    def test_non_explicit_leaves_existing_save_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            pc.set_choice(target, pc.CHOICE_NORMAL, explicit=True)
            before = target.read_bytes()
            with self.assertRaises(pc.ChoiceError):
                pc.set_choice(target, pc.CHOICE_UNLOCK_ALL, explicit=False)
            self.assertEqual(target.read_bytes(), before)

    def test_explicit_opt_in_and_opt_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            pc.set_choice(target, pc.CHOICE_UNLOCK_ALL, explicit=True)
            self.assertEqual(pc.read_choice(target), pc.CHOICE_UNLOCK_ALL)
            pc.set_choice(target, pc.CHOICE_NORMAL, explicit=True)
            self.assertEqual(pc.read_choice(target), pc.CHOICE_NORMAL)

    def test_unknown_choice_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pc.ChoiceError):
                pc.set_choice(Path(tmp) / pc.CHOICE_FILENAME, "all", explicit=True)

    def test_fresh_explicit_change_arms_unconsumed_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            pc.set_choice(target, pc.CHOICE_UNLOCK_ALL, explicit=True)
            intent = pc.read_intent(target)
            self.assertIsNotNone(intent)
            self.assertEqual(intent["choice"], pc.CHOICE_UNLOCK_ALL)

    def test_legacy_file_without_intent_never_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            target.write_text(json.dumps({"choice": pc.CHOICE_UNLOCK_ALL}),
                              encoding="utf-8")
            self.assertIsNone(pc.read_intent(target))

    def test_consume_is_single_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            pc.set_choice(target, pc.CHOICE_UNLOCK_ALL, explicit=True)
            pc.mark_intent_consumed(target, error="boom")
            self.assertIsNone(pc.read_intent(target))
            stored = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(stored["switch_intent"]["consumed"])
            self.assertEqual(stored["switch_intent"]["error"], "boom")


class FailedSwitchTests(unittest.TestCase):
    def test_hash_mismatch_retains_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            current = make_variant(tmp, pc.CHOICE_NORMAL)
            target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tamper="base.apk")
            with self.assertRaises(pc.TransactionError) as ctx:
                plan(current, target)
            self.assertIn("base.apk", str(ctx.exception))

    def test_cert_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = make_variant(Path(tmp), pc.CHOICE_NORMAL)
            target = make_variant(Path(tmp), pc.CHOICE_UNLOCK_ALL, cert="AA:BB:CC")
            with self.assertRaises(pc.TransactionError) as ctx:
                plan(current, target)
            self.assertIn("cert", str(ctx.exception).lower())

    def test_descriptor_cert_lie_caught_by_file_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = make_variant(Path(tmp), pc.CHOICE_NORMAL)
            target = make_variant(Path(tmp), pc.CHOICE_UNLOCK_ALL,
                                  claim="00:11:22:33")
            with self.assertRaises(pc.TransactionError) as ctx:
                plan(current, target)
            self.assertIn("claim", str(ctx.exception).lower())

    def test_split_cert_divergence_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = make_variant(Path(tmp), pc.CHOICE_NORMAL)
            target = make_variant(Path(tmp), pc.CHOICE_UNLOCK_ALL,
                                  split_fp={"split_config.en.apk": "DE:AD:BE:EF"})
            with self.assertRaises(pc.TransactionError) as ctx:
                plan(current, target)
            self.assertIn("split_config.en.apk", str(ctx.exception))

    def test_version_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = make_variant(Path(tmp), pc.CHOICE_NORMAL)
            target = make_variant(Path(tmp), pc.CHOICE_UNLOCK_ALL, version=30)
            with self.assertRaises(pc.TransactionError):
                plan(current, target)

    def test_missing_split_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = make_variant(Path(tmp), pc.CHOICE_NORMAL)
            target = make_variant(Path(tmp), pc.CHOICE_UNLOCK_ALL, drop="split_config.en.apk")
            with self.assertRaises(pc.TransactionError) as ctx:
                plan(current, target)
            self.assertIn("split_config.en.apk", str(ctx.exception))

    def test_identical_variant_is_noop_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = make_variant(Path(tmp), pc.CHOICE_NORMAL)
            with self.assertRaises(pc.TransactionError):
                plan(current, current)

    def test_cross_cert_change_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            current = make_variant(tmp, pc.CHOICE_NORMAL, cert="97:D4:OLD")
            target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL)
            with self.assertRaises(pc.TransactionError) as ctx:
                plan(current, target)
            self.assertIn("uninstall", str(ctx.exception).lower())


class RecordsContractTests(unittest.TestCase):
    def good_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            return plan(make_variant(tmp, pc.CHOICE_NORMAL),
                        make_variant(tmp, pc.CHOICE_UNLOCK_ALL))

    def test_valid_plan_shape_and_bound_hashes(self):
        steps = self.good_steps()
        ops = [s["op"] for s in steps]
        self.assertEqual(ops, ["verify-target", "confirm-ownership",
                              "adb-install", "verify-installed"])
        install = next(s for s in steps if s["op"] == "adb-install")
        self.assertEqual(install["args"][:3],
                         ["install-multiple", "-r", "--no-streaming"])
        self.assertEqual(len(install["args"]), 3 + len(pc.EXPECTED_SPLITS))
        verify = next(s for s in steps if s["op"] == "verify-installed")
        self.assertEqual(set(verify["bound_hashes"]), set(pc.EXPECTED_SPLITS))

    def test_unknown_op_refused_never_ignored(self):
        steps = self.good_steps()
        steps.insert(1, {"op": "wipe-cache", "args": []})
        with self.assertRaises(pc.TransactionError) as ctx:
            pc.assert_records_safe(steps)
        self.assertIn("wipe-cache", str(ctx.exception))

    def test_tampered_uninstall_step_rejected(self):
        steps = self.good_steps()
        steps.append({"op": "uninstall", "args": [pc.PACKAGE]})
        with self.assertRaises(pc.TransactionError):
            pc.assert_records_safe(steps)

    def test_tampered_clear_step_rejected(self):
        steps = self.good_steps()
        steps.append({"op": "shell", "args": ["pm", "clear", pc.PACKAGE]})
        self.assertRaises(pc.TransactionError, pc.assert_records_safe, steps)

    def test_extra_install_args_rejected(self):
        steps = self.good_steps()
        install = next(s for s in steps if s["op"] == "adb-install")
        install["args"] = [*install["args"], "--bypass-low-target-sdk-block"]
        with self.assertRaises(pc.TransactionError):
            pc.assert_records_safe(steps)

    def test_install_without_multiple_rejected(self):
        steps = self.good_steps()
        install = next(s for s in steps if s["op"] == "adb-install")
        install["args"] = ["install", "-r", *install["args"][3:]]
        with self.assertRaises(pc.TransactionError):
            pc.assert_records_safe(steps)


class OwnershipTests(unittest.TestCase):
    def held_lock(self, tmp: Path, name: str):
        path = tmp / name
        path.write_bytes(b"x")
        fd = os.open(path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return path, fd

    def test_unheld_lock_file_reports_not_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ports.lock"
            path.write_bytes(b"x")
            self.assertFalse(pc.lock_held_state(path))

    def test_missing_lock_file_reports_not_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(pc.lock_held_state(Path(tmp) / "nope.lock"))

    def test_held_lock_reports_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, fd = self.held_lock(Path(tmp), "ports.lock")
            try:
                self.assertTrue(pc.lock_held_state(path))
            finally:
                os.close(fd)

    def fake_proc(self, tmp: Path, entries: dict):
        root = tmp / "proc"
        for pid, cmdline in entries.items():
            slot = root / str(pid)
            slot.mkdir(parents=True)
            (slot / "cmdline").write_bytes(cmdline)
        return str(root)

    def test_owned_emulator_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fake_proc(Path(tmp), {
                100: b"/sdk/emulator/qemu-system-x86_64-headless\x00-avd\x00test-avd\x00-port\x005666\x00",
                200: b"/usr/bin/crashpad_handler\x00--database=/tmp/emu-stuff\x00",
                300: b"/sdk/platform-tools/adb\x00-L\x00tcp:localhost:5039\x00fork-server\x00",
            })
            self.assertEqual(pc.find_owned_emulator("test-avd", 5666, proc_root=root), 100)

    def test_crashpad_never_matches_despite_emulator_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fake_proc(Path(tmp), {
                200: b"/usr/bin/crashpad_handler\x00--database=/tmp/emulator-crash\x00",
            })
            self.assertIsNone(pc.find_owned_emulator("test-avd", 5666, proc_root=root))

    def test_wrong_avd_or_port_not_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fake_proc(Path(tmp), {
                100: b"/sdk/emulator/emulator\x00-avd\x00other-avd\x00-port\x005666\x00",
            })
            self.assertIsNone(pc.find_owned_emulator("test-avd", 5666, proc_root=root))
            self.assertIsNone(pc.find_owned_emulator("other-avd", 5999, proc_root=root))

    def test_check_session_all_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            endpoint, fd1 = self.held_lock(tmp, "e.lock")
            profile, fd2 = self.held_lock(tmp, "p.lock")
            root = self.fake_proc(tmp, {
                100: b"qemu-system-x86_64\x00-avd\x00test-avd\x00-port\x005666\x00"})
            try:
                session = pc.OwnedSession(
                    serial="s", adb_prefix=("adb",), avd_name="test-avd",
                    console_port=5666, endpoint_lock=str(endpoint),
                    profile_lock=str(profile), game_started=False)
                facts = pc.check_session(session, proc_root=root)
            finally:
                os.close(fd1)
                os.close(fd2)
            self.assertTrue(facts["locks_held"])
            self.assertTrue(facts["emulator_owned"])
            self.assertEqual(facts["emulator_owned_pid"], 100)
            self.assertTrue(facts["game_not_started"])

    def test_game_started_blocks(self):
        session = make_session()
        started = pc.OwnedSession(serial=session.serial, adb_prefix=session.adb_prefix,
                                  avd_name=session.avd_name, console_port=session.console_port,
                                  endpoint_lock=session.endpoint_lock,
                                  profile_lock=session.profile_lock, game_started=True)
        with tempfile.TemporaryDirectory() as tmp:
            facts = pc.check_session(started, proc_root=str(Path(tmp) / "empty"))
            self.assertFalse(facts["game_not_started"])


class ExecutorTests(unittest.TestCase):
    def setup_case(self, tmp: Path, transport: FakeTransport, *, preinstall=True):
        BYTES_FP.clear()
        current = make_variant(tmp, pc.CHOICE_NORMAL, tag="cur")
        target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tag="tgt")
        if preinstall:
            transport.installed = {s: Path(current.apks_dir, s).read_bytes()
                                   for s in pc.EXPECTED_SPLITS}
        steps = plan(current, target)
        return steps, make_session()

    def run_case(self, transport: FakeTransport, *, preinstall=True):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            steps, session = self.setup_case(tmp, transport, preinstall=preinstall)
            receipt_path = tmp / pc.RECEIPT_FILENAME
            pull_dir = tmp / "pulls"
            receipt = pc.run_switch(steps, session, transport=transport,
                                    ownership=dict(OWN_OK),
                                    receipt_path=receipt_path,
                                    pull_dir=pull_dir,
                                    cert_reader=test_reader)
            on_disk = json.loads(receipt_path.read_text(encoding="utf-8"))
            return receipt, transport, on_disk

    def test_success_reports_only_after_pull_verification(self):
        receipt, transport, on_disk = self.run_case(FakeTransport())
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["variant"], pc.CHOICE_UNLOCK_ALL)
        self.assertEqual(receipt["version_code"], "29")
        self.assertEqual(on_disk, receipt)
        self.assertIn("installed_pulled_hashes", receipt)
        install_calls = [c for c in transport.calls if "install-multiple" in c]
        self.assertEqual(len(install_calls), 1)
        cmd = install_calls[0]
        self.assertEqual(cmd[1:4], ["-s", "emulator-5667", "install-multiple"])
        self.assertEqual(cmd[4:6], ["-r", "--no-streaming"])
        pulls = [c for c in transport.calls if "pull" in c]
        self.assertGreaterEqual(len(pulls), 10)  # pre + post, 5 splits each

    def test_install_failure_requeries_and_reports_retained(self):
        transport = FakeTransport(install_rc=1, install_out="Failure [X]")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="cur")
            target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tag="tgt")
            prior = {s: Path(current.apks_dir, s).read_bytes() for s in pc.EXPECTED_SPLITS}
            transport.installed = dict(prior)
            steps = plan(current, target)
            with self.assertRaises(pc.TransactionError) as ctx:
                pc.run_switch(steps, make_session(), transport=transport,
                              ownership=dict(OWN_OK),
                              receipt_path=tmp / pc.RECEIPT_FILENAME,
                              pull_dir=tmp / "p",
                              cert_reader=test_reader)
            self.assertEqual(ctx.exception.outcome, "retained-verified")
            self.assertFalse((tmp / pc.RECEIPT_FILENAME).exists())

    def test_post_verify_mismatch_classifies_unknown(self):
        transport = FakeTransport(post_garbage=True)
        with self.assertRaises(pc.TransactionError) as ctx:
            self.run_case(transport)
        self.assertEqual(ctx.exception.outcome, "mutated-unknown")

    def test_untrusted_plan_never_reaches_adb(self):
        transport = FakeTransport()
        bad = [{"op": "verify-target", "variant": "x"},
               {"op": "adb-install", "args": ["install-multiple", "-r", "a.apk"]},
               {"op": "verify-installed", "variant": "x", "bound_hashes": {}}]
        with self.assertRaises(pc.TransactionError):
            pc.run_switch(bad, make_session(), transport=transport,
                          ownership=dict(OWN_OK))
        self.assertEqual(transport.calls, [])

    def test_malformed_plan_refused(self):
        with self.assertRaises(pc.TransactionError):
            pc.run_switch({"not": "a list"}, make_session(),
                          transport=FakeTransport(), ownership=dict(OWN_OK))

    def test_ownership_unprovable_blocks_before_live(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            steps = plan(make_variant(tmp, pc.CHOICE_NORMAL, tag="c"),
                         make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tag="t"))
            bad = dict(OWN_OK, locks_held=False)
            with self.assertRaises(pc.TransactionError):
                pc.run_switch(steps, make_session(), transport=transport,
                              ownership=bad)
            self.assertEqual(transport.calls, [])

    def test_target_drift_between_plan_and_execute_refused(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="c")
            target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tag="t")
            steps = plan(current, target)
            drifted = Path(target.apks_dir, "base.apk")
            drifted.write_bytes(b"swapped-after-planning")
            register_blob(b"swapped-after-planning", FP_TARGET)
            with self.assertRaises(pc.TransactionError) as ctx:
                pc.run_switch(steps, make_session(), transport=transport,
                              ownership=dict(OWN_OK))
            self.assertIn("drifted", str(ctx.exception).lower())
            self.assertEqual(transport.calls, [])

    def test_installed_version_mismatch_blocks(self):
        transport = FakeTransport(dumpsys=DUMPSYS_V28)
        with self.assertRaises(pc.TransactionError) as ctx:
            self.run_case(transport)
        self.assertIn("version", str(ctx.exception).lower())
        self.assertFalse([c for c in transport.calls if "install-multiple" in c])

    def test_installed_cert_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="c")
            target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tag="t")
            foreign = {s: f"foreign:{s}".encode() for s in pc.EXPECTED_SPLITS}
            for data in foreign.values():
                register_blob(data, FP_OTHER)
            transport = FakeTransport(installed=foreign)
            steps = plan(current, target)
            with self.assertRaises(pc.TransactionError) as ctx:
                pc.run_switch(steps, make_session(), transport=transport,
                              ownership=dict(OWN_OK),
                              receipt_path=tmp / pc.RECEIPT_FILENAME,
                              pull_dir=tmp / "p",
                              cert_reader=test_reader)
            self.assertIn("cert", str(ctx.exception).lower())
            self.assertFalse([c for c in transport.calls if "install-multiple" in c])

    def test_fresh_absent_package_allowed(self):
        transport = FakeTransport(installed={})
        receipt, _, _ = self.run_case(transport, preinstall=False)
        self.assertTrue(receipt["verified"])
        self.assertTrue(receipt["fresh_install"])

    def test_receipt_write_failure_reconciles_not_reinstalls(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="c")
            target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tag="t")
            prior = {s: Path(current.apks_dir, s).read_bytes() for s in pc.EXPECTED_SPLITS}
            transport.installed = dict(prior)
            steps = plan(current, target)
            receipt_dir = tmp / "receipt-dir"
            receipt_dir.mkdir()
            with patch.object(pc, "write_receipt",
                              side_effect=OSError("disk full")):
                with self.assertRaises(pc.TransactionError) as ctx:
                    pc.run_switch(steps, make_session(), transport=transport,
                                  ownership=dict(OWN_OK),
                                  receipt_path=receipt_dir / pc.RECEIPT_FILENAME,
                                  pull_dir=tmp / "p",
                                  cert_reader=test_reader)
            self.assertTrue(ctx.exception.outcome.startswith("verified-unrecorded"))
            self.assertIn("receipt", ctx.exception.detail)
            installs = [c for c in transport.calls if "install-multiple" in c]
            self.assertEqual(len(installs), 1)  # exactly one attempt, no retry


class ApplyTests(unittest.TestCase):
    """Intent retirement under unlock-default policy (Sept 14): ANY
    unconsumed intent is consumed-with-error and ignored with zero device
    interaction — no stale normal intent can auto-revert, no fresh intent
    can auto-install. Upgrades are explicit run_switch operations."""

    def arm(self, tmp: Path, choice: str) -> Path:
        choice_path = tmp / pc.CHOICE_FILENAME
        pc.set_choice(choice_path, choice, explicit=True)
        self.assertIsNotNone(pc.read_intent(choice_path))
        return choice_path

    def test_no_intent_proceeds_silently(self):
        session = make_session()
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            action, _ = pc.apply_pending_progression_switch(
                session, choice_path=Path(tmp) / pc.CHOICE_FILENAME,
                receipt_path=Path(tmp) / pc.RECEIPT_FILENAME,
                variants_root=Path(tmp) / "variants", transport=transport,
                ownership=dict(OWN_OK), cert_reader=test_reader)
            self.assertEqual(action, "proceeded")
            self.assertEqual(transport.calls, [])

    def test_legacy_normal_intent_retired_without_touching_device(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            choice_path = self.arm(tmp, pc.CHOICE_NORMAL)
            receipt_path = tmp / pc.RECEIPT_FILENAME
            action, message = pc.apply_pending_progression_switch(
                make_session(), choice_path=choice_path,
                receipt_path=receipt_path,
                variants_root=tmp / "variants", transport=transport,
                ownership=dict(OWN_OK), cert_reader=test_reader)
            self.assertEqual(action, "proceeded")
            self.assertIn("retired", message)
            self.assertIsNone(pc.read_intent(choice_path))
            stored = json.loads(choice_path.read_text(encoding="utf-8"))
            self.assertTrue(stored["switch_intent"]["consumed"])
            self.assertTrue(stored["switch_intent"]["error"])
            self.assertEqual(transport.calls, [])
            self.assertFalse(receipt_path.exists())

    def test_fresh_unlock_intent_retired_without_touching_device(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            choice_path = self.arm(tmp, pc.CHOICE_UNLOCK_ALL)
            receipt_path = tmp / pc.RECEIPT_FILENAME
            action, message = pc.apply_pending_progression_switch(
                make_session(), choice_path=choice_path,
                receipt_path=receipt_path,
                variants_root=tmp / "variants", transport=transport,
                ownership=dict(OWN_OK), cert_reader=test_reader)
            self.assertEqual(action, "proceeded")
            self.assertIn("retired", message)
            self.assertIsNone(pc.read_intent(choice_path))
            self.assertEqual(transport.calls, [])
            self.assertFalse(receipt_path.exists())

    def test_retirement_needs_no_artifacts_or_ownership(self):
        # Missing variants + unprovable ownership must not matter: the
        # retired path never inspects them (no FileNotFound, no abort).
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            choice_path = self.arm(tmp, pc.CHOICE_NORMAL)
            action, _ = pc.apply_pending_progression_switch(
                make_session(), choice_path=choice_path,
                receipt_path=tmp / pc.RECEIPT_FILENAME,
                variants_root=tmp / "missing-variants", transport=transport,
                ownership={"serial_scoped": False, "locks_held": False,
                           "emulator_owned": False, "game_not_started": True},
                cert_reader=test_reader)
            self.assertEqual(action, "proceeded")
            self.assertEqual(transport.calls, [])


class ReceiptStatusTests(unittest.TestCase):
    def test_no_receipt_means_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = pc.choice_status(Path(tmp) / pc.CHOICE_FILENAME,
                                      Path(tmp) / pc.RECEIPT_FILENAME)
            self.assertEqual(status["stored"], pc.CHOICE_UNLOCK_ALL)
            self.assertFalse(status["verified"])
            self.assertFalse(status["pending_restart"])

    def test_matching_receipt_clears_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            choice = tmp / pc.CHOICE_FILENAME
            pc.set_choice(choice, pc.CHOICE_UNLOCK_ALL, explicit=True)
            receipt = tmp / pc.RECEIPT_FILENAME
            receipt.write_text(json.dumps({"variant": pc.CHOICE_UNLOCK_ALL,
                                           "verified": True, "installed_utc": 1}),
                               encoding="utf-8")
            status = pc.choice_status(choice, receipt)
            self.assertTrue(status["verified"])
            self.assertFalse(status["pending_restart"])

    def test_mismatch_means_pending_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            choice = tmp / pc.CHOICE_FILENAME
            pc.set_choice(choice, pc.CHOICE_UNLOCK_ALL, explicit=True)
            receipt = tmp / pc.RECEIPT_FILENAME
            receipt.write_text(json.dumps({"variant": pc.CHOICE_NORMAL,
                                           "verified": True, "installed_utc": 1}),
                               encoding="utf-8")
            status = pc.choice_status(choice, receipt)
            self.assertTrue(status["pending_restart"])


class ExecutorStatusTests(unittest.TestCase):
    def test_executor_owned_not_maps_pending(self):
        self.assertIn("executor-implemented", pc.TRANSACTION_STATUS)
        self.assertIn("descriptors ONLY", pc.VARIANT_CONTRACT["composer"])

    def test_restart_notice_present(self):
        self.assertIn("restart", pc.RESTART_NOTICE.lower())
        self.assertIn("preserv", pc.RESTART_NOTICE.lower())

    def test_module_contains_no_process_kills(self):
        source = Path(pc.__file__).read_text(encoding="utf-8")
        for token in ("pkill", "kill(", "os.kill", "SIGKILL", "SIGTERM", "terminate("):
            self.assertNotIn(token, source)


class FakeTk:
    """Minimal tkinter stub following test-controls-settings.py conventions."""
    instances = []
    queue = []

    class FakeVar:
        def __init__(self, value=None):
            self._value = value
        def get(self):
            return self._value
        def set(self, value):
            self._value = value

    class FakeWidget:
        def __init__(self, *args, **kwargs):
            FakeTk.instances.append((args, kwargs))
        def pack(self, *args, **kwargs):
            return None

    class FakeButton(FakeWidget):
        def configure(self, *args, **kwargs):
            return None

    class FakeWindow:
        def __init__(self, *args, **kwargs):
            self.commands = {}
            self.destroyed = False
        def title(self, *a):
            return None
        def geometry(self, *a):
            return None
        def configure(self, *a, **k):
            return None
        def attributes(self, *a, **k):
            return None
        def destroy(self):
            self.destroyed = True
            return None
        def protocol(self, *a, **k):
            return None
        def mainloop(self):
            while FakeTk.queue and not self.destroyed:
                self.commands[FakeTk.queue.pop(0)]()

    def __init__(self):
        self._window = None

    def Tk(self, *args, **kwargs):
        self._window = FakeTk.FakeWindow()
        return self._window

    def StringVar(self, value=None):
        return FakeTk.FakeVar(value)

    def Label(self, *args, **kwargs):
        return FakeTk.FakeWidget(*args, **kwargs)

    def Frame(self, *args, **kwargs):
        return FakeTk.FakeWidget(*args, **kwargs)

    def Radiobutton(self, *args, **kwargs):
        return FakeTk.FakeWidget(*args, **kwargs)

    def Button(self, master=None, **kwargs):
        button = FakeTk.FakeButton(master, **kwargs)
        self._window.commands[kwargs["text"]] = kwargs["command"]
        return button


def stub_tk():
    fake = FakeTk()
    module = types.ModuleType("tkinter")
    for name in ("Tk", "StringVar", "Label", "Frame", "Radiobutton", "Button"):
        setattr(module, name, getattr(fake, name))
    return module


class PanelTests(unittest.TestCase):
    def run_panel(self, initial=None, availability=None):
        FakeTk.instances.clear()
        FakeTk.queue.clear()
        FakeTk.queue.extend(["Save & Play"])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / pc.CHOICE_FILENAME
            if initial is not None:
                pc.set_choice(target, initial, explicit=True)
            with patch.dict("sys.modules", {"tkinter": stub_tk()}):
                result = pc.show_progression_choice(target, availability)
            stored = json.loads(target.read_text(encoding="utf-8"))
            widgets = list(FakeTk.instances)
            return result, stored, widgets

    def test_default_preselects_unlock_all_and_saves_explicit(self):
        # Retired panel (unwired since Sept 14) follows the policy default.
        result, stored, _ = self.run_panel()
        self.assertEqual(result, pc.CHOICE_UNLOCK_ALL)
        self.assertEqual(stored["choice"], pc.CHOICE_UNLOCK_ALL)
        self.assertTrue(stored["explicit"])

    def test_existing_unlock_all_preselected(self):
        result, _, _ = self.run_panel(initial=pc.CHOICE_UNLOCK_ALL)
        self.assertEqual(result, pc.CHOICE_UNLOCK_ALL)

    def test_labels_distinct_and_choice_set_complete(self):
        self.assertEqual(set(pc.CHOICE_LABELS), set(pc.CHOICES))
        self.assertNotEqual(pc.CHOICE_LABELS[pc.CHOICE_NORMAL],
                            pc.CHOICE_LABELS[pc.CHOICE_UNLOCK_ALL])

    def test_absent_artifacts_disable_action_clearly(self):
        avail = {pc.CHOICE_NORMAL: (True, ""),
                 pc.CHOICE_UNLOCK_ALL: (False, "unlock-all variant not staged")}
        _, _, widgets = self.run_panel(availability=avail)
        radios = [kw for _, kw in widgets if "value" in kw]
        self.assertEqual([kw["value"] for kw in radios], [pc.CHOICE_NORMAL])
        labels = [kw.get("text", "") for _, kw in widgets if "value" not in kw]
        self.assertTrue(any("unavailable" in text and "not staged" in text
                            for text in labels))


class WiringTests(unittest.TestCase):
    """controls_settings has NO progression UI (Sept 14 scope): no
    selector button, no status line, no choice file created by the panel.
    Play still proceeds (this suite's own stub; tilt-owned suite untouched)."""

    def run_outer(self, queue, choice_dir=None):
        FakeTk.instances.clear()
        FakeTk.queue.clear()
        FakeTk.queue.extend(queue)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            offer = choice_dir or tmp
            with patch.dict("sys.modules", {"tkinter": stub_tk()}):
                with patch.object(pc, "default_choice_path",
                                  return_value=offer / pc.CHOICE_FILENAME):
                    import controls_settings as cs
                    result = cs.show_settings(str(offer / "tilt.json"), None)
            widgets = list(FakeTk.instances)
            stored_path = offer / pc.CHOICE_FILENAME
            stored = (json.loads(stored_path.read_text(encoding="utf-8"))
                      if stored_path.is_file() else None)
            return result, widgets, offer, stored

    def test_no_progression_button_and_no_choice_file(self):
        result, widgets, _, stored = self.run_outer(["Play"])
        self.assertTrue(result)
        self.assertIsNone(stored)
        texts = [kw.get("text", "") for _, kw in widgets]
        self.assertFalse(any("Progression" in t for t in texts))

    def test_no_progression_status_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            offer = Path(tmp)
            pc.set_choice(offer / pc.CHOICE_FILENAME, pc.CHOICE_UNLOCK_ALL,
                          explicit=True)
            result, widgets, _, _ = self.run_outer(["Play"], choice_dir=offer)
            self.assertTrue(result)
            texts = [kw.get("text", "") for _, kw in widgets]
            self.assertFalse(any("Progression:" in t for t in texts))

    def test_driving_panel_needs_no_progression_module(self):
        # show_settings must not import progression_choice at all: even a
        # broken progression module cannot break the driving panel.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("sys.modules", {"tkinter": stub_tk(),
                                            "progression_choice": None}):
                import controls_settings as cs
                FakeTk.instances.clear()
                FakeTk.queue.clear()
                FakeTk.queue.extend(["Play"])
                self.assertTrue(cs.show_settings(str(Path(tmp) / "t.json"), None))


EXPECTED_STAGE_ORDER = [
    "acquire_launch_locks", "preflight", "start_server",
    "start_emulator", "orient_visible_emulator", "isolate_guest",
    "maybe_apply_progression_switch",
    "verify_packages", "start_controller", "start_input",
    "launch_game", "verify_gamescope_window",
]


class RunnerStageTests(unittest.TestCase):
    """The runner calls the switch exactly at the owned prelaunch
    boundary (post-isolate, pre-verify-packages), aborts on uncertain
    outcome, and does nothing without an explicit intent. Uses the real
    Launcher.main stage tuple with stubbed stages (no emulator/ADB)."""

    def make_launcher(self, apply_result=("proceeded", "no pending intent")):
        import shutil
        import types
        import runner as runner_module
        launcher = runner_module.Launcher.__new__(runner_module.Launcher)
        launcher.args = types.SimpleNamespace(control_settings=False)
        launcher.stop_requested = False
        run_dir = Path(tempfile.mkdtemp(prefix="pc-runner-"))
        self.addCleanup(shutil.rmtree, run_dir, True)
        launcher.run_dir = run_dir
        calls = []
        for name in EXPECTED_STAGE_ORDER:
            if name == "maybe_apply_progression_switch":
                continue
            setattr(launcher, name,
                    lambda n=name: calls.append(n))
        real_switch = runner_module.Launcher.maybe_apply_progression_switch
        launcher.maybe_apply_progression_switch = types.MethodType(
            real_switch, launcher)
        launcher.run_until_stop = lambda: calls.append("run_until_stop") or 0
        apply_patch = patch.object(
            pc, "apply_pending_progression_switch",
            side_effect=lambda *a, **k: (calls.append("switch-stage")
                                         or apply_result))
        return launcher, calls, apply_patch, runner_module

    def test_switch_stage_runs_between_isolate_and_verify_packages(self):
        launcher, calls, apply_patch, _ = self.make_launcher()
        with apply_patch:
            self.assertEqual(launcher.main(), 0)
        expected = [n if n != "maybe_apply_progression_switch" else "switch-stage"
                    for n in EXPECTED_STAGE_ORDER] + ["run_until_stop"]
        self.assertEqual(calls, expected)
        self.assertEqual(calls.count("switch-stage"), 1)
        self.assertLess(calls.index("isolate_guest"), calls.index("switch-stage"))
        self.assertLess(calls.index("switch-stage"), calls.index("verify_packages"))

    def test_aborted_switch_aborts_launch(self):
        launcher, calls, apply_patch, runner_module = self.make_launcher(
            ("aborted", "device uncertain"))
        with apply_patch:
            with self.assertRaises(runner_module.LauncherError):
                launcher.main()
        self.assertIn("switch-stage", calls)
        self.assertNotIn("verify_packages", calls)
        self.assertNotIn("launch_game", calls)

    def test_no_intent_has_no_effect(self):
        import runner as runner_module
        seen = {}

        def fake_apply(session, **kwargs):
            seen["serial"] = session.serial
            seen["game_started"] = session.game_started
            return "proceeded", "no pending progression intent"

        launcher = runner_module.Launcher.__new__(runner_module.Launcher)
        launcher.args = __import__("types").SimpleNamespace(control_settings=False)
        launcher.stop_requested = False
        with tempfile.TemporaryDirectory() as tmp:
            launcher.run_dir = Path(tmp)
            with patch.object(pc, "apply_pending_progression_switch",
                              side_effect=fake_apply):
                with patch.object(pc, "default_choice_path",
                                  return_value=Path(tmp) / pc.CHOICE_FILENAME):
                    with patch.object(pc, "default_receipt_path",
                                      return_value=Path(tmp) / pc.RECEIPT_FILENAME):
                        real = runner_module.Launcher.maybe_apply_progression_switch
                        real.__get__(launcher)()
            leftovers = {p.name for p in Path(tmp).iterdir()}
            self.assertNotIn(pc.CHOICE_FILENAME, leftovers)
            self.assertNotIn(pc.RECEIPT_FILENAME, leftovers)

    def test_owned_session_matches_runner_transport(self):
        import runner as runner_module
        launcher = runner_module.Launcher.__new__(runner_module.Launcher)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(runner_module.Launcher, "endpoint_lock_path",
                              return_value=Path(tmp) / "ports.lock"):
                session = launcher.owned_progression_session()
        self.assertEqual(session.serial, runner_module.SERIAL)
        self.assertEqual(session.adb_prefix[1:], ("-P", str(runner_module.ADB_PORT)))
        self.assertTrue(session.adb_prefix[0].endswith("platform-tools/adb"))
        self.assertEqual(session.avd_name, runner_module.AVD)
        self.assertEqual(session.console_port, runner_module.CONSOLE_PORT)
        self.assertFalse(session.game_started)
        self.assertTrue(session.profile_lock.endswith(".jcs2-launcher.lock"))

    def test_applied_role_name_matches_choice(self):
        # Retirement: even a fully-staged fresh unlock-all intent performs
        # no install and writes no receipt (upgrades are explicit).
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            variants = tmp / "variants"
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="cur")
            target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tag="tgt")
            stage_variant(variants, pc.CHOICE_NORMAL, current)
            stage_variant(variants, pc.CHOICE_UNLOCK_ALL, target)
            choice_path = tmp / pc.CHOICE_FILENAME
            receipt_path = tmp / pc.RECEIPT_FILENAME
            pc.set_choice(choice_path, pc.CHOICE_UNLOCK_ALL, explicit=True)
            prior = {s: Path(current.apks_dir, s).read_bytes()
                     for s in pc.EXPECTED_SPLITS}
            transport.installed = dict(prior)
            action, _ = pc.apply_pending_progression_switch(
                make_session(), choice_path=choice_path,
                receipt_path=receipt_path, variants_root=variants,
                transport=transport, ownership=dict(OWN_OK),
                pull_dir=tmp / "p", cert_reader=test_reader)
            self.assertEqual(action, "proceeded")
            self.assertIsNone(pc.read_intent(choice_path))
            self.assertEqual(transport.calls, [])
            self.assertFalse(receipt_path.exists())


class DefaultTransportTests(unittest.TestCase):
    """Regression for the dropped-remote pull bug: the binary branch must
    keep the full production argv ([adb, -P, port, -s, serial, pull,
    remote]) and append the local staging path — never drop the remote.
    subprocess is faked; no ADB is ever invoked."""

    def test_pull_preserves_full_argv_and_returns_bytes(self):
        from unittest import mock
        payload = b"\x50\x4b-binary-apk-bytes"
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["env"] = kwargs.get("env")
            Path(cmd[-1]).write_bytes(payload)  # device wrote staged file
            return mock.Mock(returncode=0, stdout="", stderr="")

        cmd = ["adb", "-P", "5038", "-s", "SER", "pull",
               "/data/app/com.trueaxis.jetcarstunts2-AbC==/base.apk"]
        with mock.patch.object(pc.subprocess, "run", side_effect=fake_run):
            code, out = pc.default_transport(cmd, env={"K": "V"}, binary=True)
        self.assertEqual(code, 0)
        self.assertEqual(out, payload)
        self.assertEqual(seen["cmd"][:7], cmd)  # remote kept
        self.assertTrue(seen["cmd"][7].endswith(".apk"))  # staged appended
        self.assertTrue(Path(seen["cmd"][7]).name.startswith("pulled-"))
        self.assertEqual(seen["env"], {"K": "V"})
        self.assertFalse(Path(seen["cmd"][7]).exists())  # staging cleaned

    def test_pull_failure_returns_code_empty_and_cleans(self):
        from unittest import mock
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["staged"] = cmd[-1]
            return mock.Mock(returncode=1, stdout="", stderr="Failure")

        cmd = ["adb", "-P", "5038", "-s", "SER", "pull", "/remote/base.apk"]
        with mock.patch.object(pc.subprocess, "run", side_effect=fake_run):
            code, out = pc.default_transport(cmd, binary=True)
        self.assertEqual(code, 1)
        self.assertEqual(out, b"")
        self.assertTrue(seen["staged"].endswith(".apk"))
        self.assertFalse(Path(seen["staged"]).exists())

    def test_pull_transport_exception_maps_to_127(self):
        from unittest import mock
        cmd = ["adb", "-s", "SER", "pull", "/remote/base.apk"]
        with mock.patch.object(pc.subprocess, "run", side_effect=OSError("no adb")):
            self.assertEqual(pc.default_transport(cmd, binary=True), (127, b""))

    def test_pull_missing_staged_file_after_success(self):
        from unittest import mock
        cmd = ["adb", "-s", "SER", "pull", "/remote/base.apk"]
        with mock.patch.object(pc.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            self.assertEqual(pc.default_transport(cmd, binary=True), (127, b""))


class ScratchRoutingTests(unittest.TestCase):
    """Production scratch routing (live /tmp-full lesson): binary pull
    payloads (~162 MB base.apk) must stage in explicit task/app-state
    space tied to the evidence pull dir — never an assumed-global /tmp.
    subprocess is faked; no ADB is ever invoked."""

    def test_custom_scratch_dir_used_and_cleaned(self):
        from unittest import mock
        payload = b"\x50\x4b-routed-bytes"
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["staged"] = cmd[-1]
            Path(cmd[-1]).write_bytes(payload)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "custom-scratch"
            cmd = ["adb", "-s", "SER", "pull", "/remote/base.apk"]
            with mock.patch.object(pc.subprocess, "run", side_effect=fake_run):
                code, out = pc.default_transport(cmd, binary=True,
                                                 scratch_dir=str(scratch))
            self.assertEqual((code, out), (0, payload))
            self.assertEqual(Path(seen["staged"]).parent, scratch)
            self.assertFalse(Path(seen["staged"]).exists())
            self.assertEqual(list(scratch.iterdir()), [])

    def test_custom_path_never_touches_system_temp(self):
        from unittest import mock

        def no_system_temp(*args, **kwargs):
            raise AssertionError("system temp must not be used")

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"routed")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            cmd = ["adb", "-s", "SER", "pull", "/remote/base.apk"]
            with mock.patch.object(pc.tempfile, "TemporaryDirectory",
                                   side_effect=no_system_temp):
                with mock.patch.object(pc.subprocess, "run",
                                       side_effect=fake_run):
                    code, out = pc.default_transport(
                        cmd, binary=True, scratch_dir=str(Path(tmp) / "s"))
            self.assertEqual((code, out), (0, b"routed"))

    def test_default_path_still_uses_system_temp(self):
        from unittest import mock
        used = {}

        real_tmpdir = tempfile.TemporaryDirectory

        def recording_tmpdir(*args, **kwargs):
            used["yes"] = True
            return real_tmpdir(*args, **kwargs)

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"legacy")
            return mock.Mock(returncode=0, stdout="", stderr="")

        cmd = ["adb", "-s", "SER", "pull", "/remote/base.apk"]
        with mock.patch.object(pc.tempfile, "TemporaryDirectory",
                               side_effect=recording_tmpdir):
            with mock.patch.object(pc.subprocess, "run",
                                   side_effect=fake_run):
                self.assertEqual(pc.default_transport(cmd, binary=True),
                                 (0, b"legacy"))
        self.assertTrue(used.get("yes"))

    def test_pull_evidence_ties_scratch_to_pull_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="scratch")
            blobs = {s: Path(current.apks_dir, s).read_bytes()
                     for s in pc.EXPECTED_SPLITS}
            fake = FakeTransport(installed=dict(blobs))
            pull_dir = tmp / "evidence-pulls"
            out = pc.pull_installed_evidence(
                make_session(), transport=fake, pull_dir=pull_dir,
                cert_reader=test_reader)
            self.assertTrue(out["installed"])
            self.assertEqual(len(fake.scratch_seen), len(pc.EXPECTED_SPLITS))
            self.assertTrue(all(s == str(pull_dir) for s in fake.scratch_seen))
            self.assertTrue((pull_dir / "installed-base.apk").is_file())

    def test_pull_evidence_fallback_beside_receipt(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="fb")
            blobs = {s: Path(current.apks_dir, s).read_bytes()
                     for s in pc.EXPECTED_SPLITS}
            fake = FakeTransport(installed=dict(blobs))
            with mock.patch.object(pc, "default_receipt_path",
                                   return_value=tmp / "receipt.json"):
                out = pc.pull_installed_evidence(
                    make_session(), transport=fake, cert_reader=test_reader)
            self.assertTrue(out["installed"])
            staging = tmp / [p for p in tmp.iterdir()
                             if p.is_dir() and p.name.startswith("pull-")][0].name
            self.assertTrue((staging / "installed-base.apk").is_file())
            self.assertTrue(all(s == str(staging) for s in fake.scratch_seen))

    def test_run_switch_threads_pull_dir_as_scratch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="cur")
            target = make_variant(tmp, pc.CHOICE_UNLOCK_ALL, tag="tgt")
            fake = FakeTransport(installed={
                s: Path(current.apks_dir, s).read_bytes()
                for s in pc.EXPECTED_SPLITS})
            steps = plan(current, target)
            pull_dir = tmp / "pulls"
            receipt = pc.run_switch(
                steps, make_session(), transport=fake,
                ownership=dict(OWN_OK),
                receipt_path=tmp / pc.RECEIPT_FILENAME,
                pull_dir=pull_dir, cert_reader=test_reader)
            self.assertTrue(receipt["verified"])
            pulls = len(pc.EXPECTED_SPLITS) * 2  # pre + post
            self.assertEqual(len(fake.scratch_seen), pulls)
            self.assertTrue(all(s == str(pull_dir)
                                for s in fake.scratch_seen))

    def test_explicit_scratch_unusable_fails_clear_not_silent_tmp(self):
        # An explicitly requested but unusable scratch dir must raise a
        # worded TransactionError BEFORE any adb call — never silently
        # fall back onto (known-full) system /tmp and masquerade as a
        # device pull failure. ENOTDIR via a file standing in for a dir.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="bad")
            blobs = {s: Path(current.apks_dir, s).read_bytes()
                     for s in pc.EXPECTED_SPLITS}
            fake = FakeTransport(installed=dict(blobs))
            blocker = tmp / "file-not-dir"
            blocker.write_bytes(b"in the way")
            bad = str(blocker / "scratch")
            with self.assertRaises(pc.TransactionError) as ctx:
                pc.pull_installed_evidence(
                    make_session(), transport=fake,
                    pull_dir=tmp / "p", cert_reader=test_reader,
                    scratch_dir=bad)
            self.assertIn("scratch", str(ctx.exception).lower())
            self.assertIn(bad, str(ctx.exception))
            # fail-clear fires after the two read-only queries (pm path +
            # dumpsys); no mutation and no pull is ever attempted.
            self.assertFalse(any("pull" in c for c in fake.calls))

    def test_transport_bad_scratch_is_loud_127(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file-not-dir"
            blocker.write_bytes(b"in the way")
            cmd = ["adb", "-s", "SER", "pull", "/remote/base.apk"]
            with mock.patch.object(pc.tempfile, "TemporaryDirectory",
                                   side_effect=AssertionError(
                                       "must not touch system temp")):
                self.assertEqual(
                    pc.default_transport(
                        cmd, binary=True,
                        scratch_dir=str(blocker / "scratch")),
                    (127, b""))

    def test_evidence_staging_unwritable_fails_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            BYTES_FP.clear()
            current = make_variant(tmp, pc.CHOICE_NORMAL, tag="stage")
            blobs = {s: Path(current.apks_dir, s).read_bytes()
                     for s in pc.EXPECTED_SPLITS}
            fake = FakeTransport(installed=dict(blobs))
            blocker = tmp / "file-not-dir"
            blocker.write_bytes(b"in the way")
            with self.assertRaises(pc.TransactionError) as ctx:
                pc.pull_installed_evidence(
                    make_session(), transport=fake,
                    pull_dir=str(blocker / "pulls"),
                    cert_reader=test_reader)
            self.assertIn("staging", str(ctx.exception).lower())
            self.assertFalse(any("pull" in c for c in fake.calls))


if __name__ == "__main__":
    unittest.main()

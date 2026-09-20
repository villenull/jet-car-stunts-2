"""Unit tests for packaging/installer/install_jcs2.py.

Everything here is offline: temporary trees, synthetic archives and a fake
/proc. Nothing boots an emulator, touches Steam, downloads a pinned archive or
changes a real install.
"""
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

import install_jcs2 as installer

REPO = Path(__file__).resolve().parents[3]


def make_config(root, *extra, execute=False, **overrides):
    argv = ["--root", str(root)] + (["--execute"] if execute else []) + list(extra)
    cfg = installer.build_config(installer.parse_args(argv))
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def make_setup_tree(tmp, binaries=True):
    """Copy the shipped setup file set into tmp, like an extracted archive."""
    root = Path(tmp)
    for entry in installer._sm.setup_entries():
        source = REPO / entry["source"]
        destination = root / entry["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    if binaries:
        launcher = root / "linux-launcher"
        launcher.mkdir(parents=True, exist_ok=True)
        (launcher / "jcs2-controller-linux").write_bytes(b"\x7fELF placeholder")
        (launcher / "jcs2-input-helper.jar").write_bytes(b"PK\x03\x04 placeholder")
    return root


def snapshot(root):
    root = Path(root)
    return {str(path.relative_to(root)): (path.is_dir(), path.stat().st_size if path.is_file() else 0)
            for path in sorted(root.rglob("*"))}


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in members:
            bundle.writestr(name, data)
    return path


def component(**overrides):
    spec = {"id": "test", "format": "zip", "top": "tree", "dest": "runtime/sdk/test",
            "filename": "test.zip", "size": 0, "sha256": "", "required_files": ["tool"]}
    spec.update(overrides)
    return spec


class DryRunTests(unittest.TestCase):
    def test_fresh_root_dry_run_reports_pinned_pieces_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            before = snapshot(root)
            report = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(report):
                code = installer.main(["--root", str(root), "--json"])
            self.assertEqual(code, installer.EXIT_INCOMPLETE)
            self.assertEqual(before, snapshot(root))
            document = json.loads(report.getvalue())
            self.assertEqual(document["mode"], "dry-run")
            stages = {row["stage"]: row for row in document["stages"]}
            self.assertEqual(stages["runtime/emulator"]["detail"]["sha256"],
                             installer.load_runtime_lock()["components"][2]["sha256"])
            self.assertEqual(stages["runtime/emulator"]["status"], "missing")
            self.assertIn("emulator-linux_x64-10696886.zip",
                          stages["runtime/emulator"]["detail"]["url"])
            items = {entry["item"] for entry in document["missing"]}
            self.assertIn("runtime/emulator", items)
            self.assertIn("payload/game-apks", items)
            self.assertIn("runtime/alternates", stages)
            self.assertEqual(stages["runtime/alternates"]["status"], "skipped")

    def test_complete_plan_reports_ok_and_exits_zero(self):
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            # Everything the runtime lock requires, already extracted.
            lock = {"schema": 1, "emulator_select": {"primary": "emulator"}, "components": [
                {"id": "emulator", "url": "https://example.invalid/emulator.zip",
                 "filename": "emulator.zip", "size": 1, "sha256": "0" * 64, "format": "zip",
                 "top": "emulator", "dest": "runtime/sdk/emulator",
                 "required_files": ["emulator", "qemu-img"],
                 "license": {"id": "android-sdk-license", "requires_acceptance": True,
                             "text": "licenses/android-sdk-license.txt"}},
            ]}
            emulator = root / "runtime/sdk/emulator"
            emulator.mkdir(parents=True)
            for name in ("emulator", "qemu-img"):
                (emulator / name).write_bytes(b"binary")
            image = root / "runtime/sdk/system-images/android-28/google_apis/x86"
            image.mkdir(parents=True)
            (image / "source.properties").write_text("Pkg.Revision=28\n")
            apks = root / installer.APKS_DIR_REL
            apks.mkdir(parents=True)
            (apks / "base.apk").write_bytes(b"base")
            mapping = root / "assets/controller/mapping.json"
            mapping.parent.mkdir(parents=True)
            shutil.copy2(REPO / "controller/mapping.json", mapping)
            report = io.StringIO()
            with mock.patch.object(installer, "load_runtime_lock", lambda: lock):
                with contextlib.redirect_stdout(report):
                    code = installer.main(["--root", str(root), "--json"])
            document = json.loads(report.getvalue())
            self.assertEqual(code, installer.EXIT_OK)
            self.assertEqual(document["blocking_missing"], 0)
            self.assertTrue(document["ok"])
            # Only optional extras may remain (artwork asset, Steam registration).
            self.assertTrue(all(not entry["blocking"] for entry in document["missing"]))
            self.assertIn("payload/artwork", [entry["item"] for entry in document["missing"]])

    def test_offline_archive_is_detected_and_verified_without_extracting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            archive_dir = Path(tmp) / "archives"
            archive_dir.mkdir()
            lock = installer.load_runtime_lock()
            emulator = [c for c in lock["components"] if c["id"] == "emulator"][0]
            payload = b"not the real archive"
            (archive_dir / emulator["filename"]).write_bytes(payload)
            spec = dict(emulator, size=len(payload))
            import hashlib
            spec["sha256"] = hashlib.sha256(payload).hexdigest()
            with mock.patch.object(installer, "load_runtime_lock",
                                   lambda: {"schema": 1, "components": [spec]}):
                cfg = make_config(root, "--archives-dir", str(archive_dir))
                report = installer.Report("dry-run", cfg)
                installer.stage_runtime(cfg, report)
            row = [item for item in report.rows if item["stage"] == "runtime/emulator"][0]
            self.assertEqual(row["status"], "would-extract")
            self.assertFalse((root / "runtime/sdk/emulator").exists())
            self.assertEqual(report.blocking, [])

    def test_execute_refuses_without_license_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            cfg = make_config(root, execute=True)
            report = installer.Report("execute", cfg)
            with self.assertRaises(installer.InstallerError) as caught:
                installer.stage_runtime(cfg, report)
            self.assertEqual(caught.exception.code, installer.EXIT_CONFIG)
            self.assertIn("--accept-licenses", caught.exception.message)
            self.assertFalse((root / "runtime").exists())

    def test_execute_fails_closed_when_archives_are_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            cfg = make_config(root, "--accept-licenses", execute=True)
            report = installer.Report("execute", cfg)
            with self.assertRaises(installer.InstallerError) as caught:
                installer.stage_runtime(cfg, report)
            self.assertEqual(caught.exception.code, installer.EXIT_RUNTIME)
            self.assertIn("no verified archive", caught.exception.message)
            self.assertFalse((root / "runtime").exists())


class ArchiveTests(unittest.TestCase):
    def test_verify_archive_is_fail_closed_on_size_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.zip"
            path.write_bytes(b"payload")
            import hashlib
            digest = hashlib.sha256(b"payload").hexdigest()
            good = component(size=7, sha256=digest)
            self.assertEqual(installer.verify_archive(path, good)["sha256"], digest)
            with self.assertRaises(installer.InstallerError):
                installer.verify_archive(path, component(size=8, sha256=digest))
            with self.assertRaises(installer.InstallerError):
                installer.verify_archive(path, component(size=7, sha256="0" * 64))

    def test_extract_rejects_traversal_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for name in ("../escape.txt", "/absolute.txt", "tree/../../escape.txt"):
                archive = make_zip(tmp / "evil.zip", [(name, b"x")])
                with self.assertRaises(installer.InstallerError):
                    installer.extract_zip(archive, tmp / "dest", "tree")
            self.assertFalse((tmp / "escape.txt").exists())
            self.assertFalse((Path(tmp).parent / "escape.txt").exists())

    def test_extract_rejects_escaping_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / "evil.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("tree/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../etc/passwd"
                bundle.addfile(info)
            with self.assertRaises(installer.InstallerError):
                installer.extract_tar(archive, tmp / "dest", "tree")

    def test_install_component_never_deletes_an_existing_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            payload = b"tool-bytes"
            archive = make_zip(tmp / "test.zip", [("tree/tool", payload)])
            import hashlib
            spec = component(size=archive.stat().st_size,
                             sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
            cfg = make_config(tmp)
            dest = installer.component_dest(cfg, spec)
            dest.mkdir(parents=True)
            (dest / "keep-me").write_text("user data")
            with self.assertRaises(installer.InstallerError):
                installer.install_component(cfg, spec, archive, repair=False)
            self.assertEqual((dest / "keep-me").read_text(), "user data")
            result = installer.install_component(cfg, spec, archive, repair=True)
            self.assertEqual((dest / "tool").read_bytes(), payload)
            self.assertTrue(os.path.isfile(Path(result["kept"]) / "keep-me"))

    def test_install_component_skips_when_required_files_are_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = make_zip(tmp / "test.zip", [("tree/tool", b"x")])
            import hashlib
            spec = component(size=archive.stat().st_size,
                             sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
            cfg = make_config(tmp)
            installer.install_component(cfg, spec, archive, repair=False)
            self.assertEqual(installer.component_missing_files(cfg, spec), [])


class MarkerTests(unittest.TestCase):
    def test_layout_marker_conflict_is_refused_and_identical_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            marker = root / installer.LAYOUT_FILE
            marker.write_text(json.dumps({"schema": 1, "layout": "portable", "avd": "someone-else"}))
            cfg = make_config(root, execute=True)
            report = installer.Report("execute", cfg)
            with self.assertRaises(installer.InstallerError) as caught:
                installer.stage_layout(cfg, report)
            self.assertEqual(caught.exception.code, installer.EXIT_MARKER)
            self.assertEqual(json.loads(marker.read_text())["avd"], "someone-else")
            marker.write_text(json.dumps({"schema": 1, "layout": "portable", "avd": cfg.avd_name}))
            report = installer.Report("execute", cfg)
            installer.stage_layout(cfg, report)
            self.assertEqual(report.rows[-1]["status"], "present")

    def test_layout_marker_is_written_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            cfg = make_config(root, execute=True)
            installer.stage_layout(cfg, installer.Report("execute", cfg))
            self.assertEqual(json.loads((root / installer.LAYOUT_FILE).read_text()),
                             {"schema": 1, "layout": "portable", "avd": cfg.avd_name})

    def test_payload_commit_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            (root / installer.PAYLOAD_COMMIT_FILE).write_text("deadbeef" * 5 + "\n")
            cfg = make_config(root, execute=True)
            with mock.patch.object(installer, "checkout_commit", lambda path: "cafebabe" * 5):
                with self.assertRaises(installer.InstallerError) as caught:
                    installer.stage_preflight(cfg, installer.Report("execute", cfg))
                self.assertEqual(caught.exception.code, installer.EXIT_CONFIG)
                allowed = make_config(root, "--allow-commit-mismatch", execute=True)
                report = installer.Report("execute", allowed)
                installer.stage_preflight(allowed, report)
                self.assertEqual([entry["item"] for entry in report.blocking], [])
                self.assertTrue(report.rows[-1]["detail"]["mismatch"])

    def test_install_state_keeps_foreign_keys_and_writes_md5_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            state = root / installer.INSTALL_STATE
            state.write_text(json.dumps({"status": "pending", "owner_note": "keep me"}))
            cfg = make_config(root, execute=True)
            installer.stage_finalize(cfg, installer.Report("execute", cfg))
            written = json.loads(state.read_text())
            self.assertEqual(written["status"], "complete")
            self.assertEqual(written["owner_note"], "keep me")
            manifest = json.loads((root / installer.MANIFEST_JSON).read_text())
            self.assertEqual(manifest["avd"], cfg.avd_name)
            self.assertEqual(manifest["files"]["run-jcs2"]["md5"],
                             installer.md5_file(root / "run-jcs2"))
            self.assertEqual(sorted(manifest["components"][0]["required_files_md5"]),
                             ["bin/python3", "lib/libtcl9.0.so", "lib/libtcl9tk9.0.so"])
            lines = (root / installer.MANIFEST_MD5).read_text().splitlines()
            self.assertIn(f'{installer.md5_file(root / "run-jcs2")}  run-jcs2', lines)

    def test_install_state_unreadable_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            (root / installer.INSTALL_STATE).write_text("{not json")
            cfg = make_config(root, execute=True)
            with self.assertRaises(installer.InstallerError) as caught:
                installer.stage_finalize(cfg, installer.Report("execute", cfg))
            self.assertEqual(caught.exception.code, installer.EXIT_MARKER)

    def test_finalize_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            before = snapshot(root)
            cfg = make_config(root)
            installer.stage_finalize(cfg, installer.Report("dry-run", cfg))
            self.assertEqual(before, snapshot(root))


class PayloadTests(unittest.TestCase):
    def make_payload_zip(self, path):
        make_zip(path, [("com.trueaxis.jetcarstunts2.apk", b"apk-bytes"),
                        ("jcs2-controller-linux", b"\x7fELF controller"),
                        ("jcs2-input-helper.jar", b"PK\x03\x04 helper"),
                        ("mapping.json", b'{"axes": {}}')])
        return path

    def test_payload_archive_is_verified_extracted_and_staged(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp, binaries=False)
            archive = self.make_payload_zip(Path(tmp) / "payload.zip")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            cfg = make_config(root, "--payload-archive", str(archive),
                              "--payload-sha256", digest, execute=True)
            report = installer.Report("execute", cfg)
            installer.stage_payload(cfg, report)
            rows = {row["stage"]: row for row in report.rows}
            self.assertEqual(rows["payload/archive"]["status"], "extracted")
            self.assertEqual(rows["payload"]["status"], "ok")
            self.assertEqual([p.name for p in (root / installer.APKS_DIR_REL).iterdir()],
                             ["com.trueaxis.jetcarstunts2.apk"])
            controller = root / "linux-launcher/jcs2-controller-linux"
            self.assertTrue(os.access(controller, os.X_OK))
            self.assertEqual((root / "assets/controller/mapping.json").read_bytes(),
                             b'{"axes": {}}')
            self.assertEqual(report.blocking, [])

    def test_payload_archive_digest_mismatch_extracts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp, binaries=False)
            archive = self.make_payload_zip(Path(tmp) / "payload.zip")
            cfg = make_config(root, "--payload-archive", str(archive),
                              "--payload-sha256", "0" * 64, execute=True)
            report = installer.Report("execute", cfg)
            with self.assertRaises(installer.InstallerError) as caught:
                installer.stage_payload(cfg, report)
            self.assertEqual(caught.exception.code, installer.EXIT_PAYLOAD)
            self.assertFalse((root / installer.PAYLOAD_DIR_REL).exists())
            self.assertEqual(report.blocking[-1]["item"], "payload/archive")

    def test_guest_stage_plans_identity_without_booting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            apks = root / installer.APKS_DIR_REL
            apks.mkdir(parents=True)
            (apks / "base.apk").write_bytes(b"base")
            mapping = root / "assets/controller/mapping.json"
            mapping.parent.mkdir(parents=True)
            shutil.copy2(REPO / "controller/mapping.json", mapping)
            cfg = make_config(root)
            report = installer.Report("dry-run", cfg)
            installer.stage_guest(cfg, report)
            row = report.rows[-1]
            self.assertEqual(row["status"], "would-run")
            self.assertEqual(row["detail"]["apks"], ["base.apk"])
            self.assertFalse((root / "state").exists())


class LaneTests(unittest.TestCase):
    def test_owned_processes_ignores_foreign_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            proc = tmp / "proc"
            root = tmp / "install"
            other = tmp / "other-install"
            (root / "runtime/sdk/emulator").mkdir(parents=True)
            (other / "runtime/sdk/emulator").mkdir(parents=True)
            entries = {
                101: [str(root / "runtime/sdk/emulator/emulator"), "-avd", "jcs2-fresh"],
                102: [str(other / "runtime/sdk/emulator/emulator"), "-avd", "jcs2-fresh"],
                103: [str(root / "runtime/sdk/platform-tools/adb"), "-P", "5038", "nodaemon", "server"],
                104: ["/usr/bin/emulator", "-avd", "jcs2-fresh"],
            }
            for pid, argv in entries.items():
                directory = proc / str(pid)
                directory.mkdir(parents=True)
                (directory / "cmdline").write_bytes(b"\x00".join(part.encode() for part in argv) + b"\x00")
            (proc / "self").mkdir()
            found = installer.owned_processes([root], "jcs2-fresh", "emulator", proc_root=str(proc))
            self.assertEqual([pid for pid, _ in found], [101])
            servers = installer.owned_processes([root], "jcs2-fresh", "adb-server", proc_root=str(proc))
            self.assertEqual([pid for pid, _ in servers], [103])
            self.assertEqual(installer.owned_processes([other], "jcs2-fresh", "emulator", proc_root=str(proc))[0][0], 102)

    def test_lane_command_and_teardown(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            self.assertEqual(installer.lane_argv(cfg),
                             [str(Path(tmp) / "run-jcs2"), "--input", "joystick"])
            headless = make_config(tmp, "--headless")
            self.assertIn("--headless", installer.lane_argv(headless))
            process = subprocess.Popen(["sleep", "300"], start_new_session=True)
            installer.stop_lane(process, timeout=5)
            self.assertIsNotNone(process.poll())

    def test_smoke_execute_fails_when_no_lane_is_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            cfg = make_config(root, execute=True)
            self.assertEqual(cfg.lane_seconds, 120)
            report = installer.Report("execute", cfg)
            with self.assertRaises(installer.InstallerError) as caught:
                installer.stage_smoke(cfg, report)
            self.assertEqual(caught.exception.code, installer.EXIT_SMOKE)
            self.assertEqual(report.rows[-1]["stage"], "smoke")
            self.assertFalse(report.rows[-1]["detail"]["lane_available"])

    def test_smoke_dry_run_plans_without_probing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_setup_tree(tmp)
            cfg = make_config(root)
            self.assertEqual(cfg.lane_seconds, 0)
            report = installer.Report("dry-run", cfg)
            installer.stage_smoke(cfg, report)
            self.assertEqual(report.rows[-1]["status"], "would-verify")
            self.assertEqual(report.rows[-1]["detail"]["lane_command"], "run-jcs2 --input joystick")


class LockTests(unittest.TestCase):
    def test_emulator_pin_is_the_32_tree_used_by_the_runner(self):
        lock = installer.load_runtime_lock()
        primary = [c for c in lock["components"] if c["id"] == "emulator"][0]
        self.assertEqual(primary["filename"], "emulator-linux_x64-10696886.zip")
        self.assertEqual(primary["dest"], "runtime/sdk/emulator")
        self.assertEqual(primary["size"], 270191252)
        self.assertEqual(len(primary["sha256"]), 64)
        self.assertEqual(primary["license"]["id"], "android-sdk-license")
        self.assertTrue(primary["license"]["requires_acceptance"])
        self.assertNotIn("role", primary)
        fetched, skipped = installer.select_components(lock, include_alternates=False)
        self.assertNotIn("emulator-37.1.11", [c["id"] for c in fetched])
        self.assertEqual([c["id"] for c in skipped], ["emulator-37.1.11"])
        self.assertEqual(lock["emulator_select"]["primary"], "emulator")

    def test_every_fetched_component_pins_a_license_and_hashes(self):
        lock = installer.load_runtime_lock()
        fetched, _ = installer.select_components(lock, include_alternates=False)
        # Pinned on purpose: a guest cannot boot without the android-28 platform,
        # so a runtime that silently loses any of these must fail this test.
        self.assertEqual([spec["id"] for spec in fetched],
                         ["python", "platform-tools", "emulator", "system-image", "platform-28"])
        for spec in fetched:
            self.assertIn("license", spec)
            self.assertEqual(len(spec["sha256"]), 64)
            self.assertGreater(spec["size"], 0)
            self.assertTrue(spec["url"].startswith("https://"))

    def test_runtime_carries_the_platform_the_emulator_validates(self):
        lock = installer.load_runtime_lock()
        platform = [c for c in lock["components"] if c["id"] == "platform-28"][0]
        self.assertEqual(platform["dest"], "runtime/sdk/platforms/android-28")
        self.assertIn("android.jar", platform["required_files"])
        self.assertEqual(platform["top"], "android-9")  # API 28 is Android 9
        self.assertEqual(platform["size"], 75565084)
        self.assertEqual(len(platform["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()

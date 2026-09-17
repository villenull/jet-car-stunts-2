"""Real PE tests under isolated Proton/Wine; use regular logs, never pipes."""
from pathlib import Path
import hashlib, json, os, shutil, socket, subprocess, tarfile, time

ROOT = Path(__file__).parents[2]
HERE = Path(__file__).parent
WORK = HERE / "analysis" / "windows-mock-timestamp"
REUSE_PREFIX = WORK / "prefix-Fresh"  # one bounded Proton/Wine prefix for all fixtures
PROTON = Path("/home/deck/.local/share/Steam/steamapps/common/Proton 11.0/files/bin/wine")
TOOL = ROOT.parent / "tools/llvm-mingw-20260908/llvm-mingw-20260908-msvcrt-ubuntu-22.04-x86_64/bin/x86_64-w64-mingw32-clang++"

def sh(cmd, **kw): return subprocess.run(cmd, check=True, text=True, **kw)

def build():
    sh([str(TOOL), "-std=c++17", "-O2", "-municode", "-static", str(HERE / "mock_tool.cpp"), "-o", str(WORK / "adb.exe")])
    shutil.copy2(WORK / "adb.exe", WORK / "emulator.exe")
    sh([str(TOOL), "-std=c++17", "-O2", "-municode", "-static", str(HERE / "mock_controller.cpp"), "-o", str(WORK / "controller.exe")])

BOOT = ROOT.parent / "analysis/lp-verification-20260910T054718Z/bootstrap"

def fixture(name, bootstrap=False, controller=False):
    d = WORK / name
    if d.exists(): shutil.rmtree(d)
    (d / "sdk/platform-tools").mkdir(parents=True)
    (d / "sdk/emulator").mkdir(parents=True)
    (d / "sdk/system-images/android-28/google_apis/x86").mkdir(parents=True)
    shutil.copy2(WORK / "adb.exe", d / "sdk/platform-tools/adb.exe")
    shutil.copy2(WORK / "emulator.exe", d / "sdk/emulator/emulator.exe")
    if controller: shutil.copy2(WORK / "controller.exe", d / "controller.exe")
    (d / "sdk/system-images/android-28/google_apis/x86/source.properties").write_text("Pkg.Desc=mock\n")
    names = [f"split{i}.apk" for i in range(1, 6)]
    for i, n in enumerate(names, 1):
        p = d / "assets/original" / n; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((f"mock-split-{i}" * 100).encode())
    cfg = "sdk_root=sdk\nemulator=sdk\\emulator\\emulator.exe\nadb=sdk\\platform-tools\\adb.exe\navd_name=JCS2-personal\navd_dir=state\\avd\nadb_port=5040\nemulator_port=5596\npackage=com.trueaxis.jetcarstunts2\n" + "\n".join(f"apk{i}=assets\\original\\{n}" for i, n in enumerate(names, 1)) + "\n"
    if controller:
        cfg += "controller_exe=controller.exe\ncontroller_helper_jar=assets\\controller\\input-helper.jar\ncontroller_mapping=assets\\controller\\mapping.json\n"
        (d / "assets/controller").mkdir(parents=True)
        (d / "assets/controller/input-helper.jar").write_bytes(b"mock-controller-jar")
        (d / "assets/controller/mapping.json").write_text("{}")
    if bootstrap:
        (d / "assets/bootstrap").mkdir(parents=True)
        shutil.copy2(BOOT / "synthetic-data.tar", d / "assets/bootstrap/synthetic-data.tar")
        shutil.copy2(BOOT / "ru.zenootho.mbnleenbe.apk", d / "assets/bootstrap/ru.zenootho.mbnleenbe.apk")
        h = "1b10063f1d7160b1cf98e177c27a5f85bc6167423a09fb31af56443d5350b318"
        cfg += "helper_apk=assets\\bootstrap\\ru.zenootho.mbnleenbe.apk\nhelper_package=ru.zenootho.mbnleenbe\nbootstrap_archive=assets\\bootstrap\\synthetic-data.tar\nbootstrap_sha256=" + h + "\n"
    (d / "launcher.ini").write_text(cfg)
    shutil.copy2(ROOT / "JCS2Launcher.exe", d / "JCS2Launcher.exe")
    return d

def set_hash(d, digest):
    p = d / "launcher.ini"; p.write_text(p.read_text().replace("bootstrap_sha256=" + "1b10063f1d7160b1cf98e177c27a5f85bc6167423a09fb31af56443d5350b318", "bootstrap_sha256=" + digest))

def run(d, timeout=45):
    # Fixture directories provide fresh launcher/AVD state; duplicating a
    # Proton prefix per case needlessly allocates ~624 MiB each time.
    prefix = REUSE_PREFIX
    env = {**os.environ, "WINEPREFIX": str(prefix), "WINEDEBUG": "-all", "WINEESYNC": "0", "WINEDLLOVERRIDES": "winemenubuilder.exe=d"}
    (d / "stop").unlink(missing_ok=True)
    busy = None
    if (d / "port_busy").exists():
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for _ in range(20):
            try:
                busy.bind(("127.0.0.1", 5040)); break
            except OSError:
                time.sleep(.1)
        else:
            busy.close(); raise RuntimeError("port 5040 could not be reserved for collision case")
        busy.listen(1)
    out = open(d / "launcher.stdout.log", "w", encoding="utf-8"); err = open(d / "launcher.stderr.log", "w", encoding="utf-8")
    started = time.monotonic(); p = subprocess.Popen([str(PROTON), str(d / "JCS2Launcher.exe")], cwd=d, env=env, stdout=out, stderr=err, text=True)
    timed_out = False
    try: rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True; p.kill(); rc = p.wait(timeout=10)
    out.close(); err.close()
    if busy: busy.close()
    ps = subprocess.run(["ps", "-eo", "pid=,args="], text=True, capture_output=True, check=False).stdout
    own = [line.strip() for line in ps.splitlines() if str(prefix) in line or str(d) in line]
    return {"rc": rc, "timeout": timed_out, "elapsed": round(time.monotonic() - started, 2), "stdout": (d / "launcher.stdout.log").read_text(errors="replace"), "stderr": (d / "launcher.stderr.log").read_text(errors="replace"), "trace": (d / "trace.txt").read_text(errors="replace") if (d / "trace.txt").exists() else "", "owned_processes_after_wait": own}

def record(results, name, d, result, expected):
    ok = result["rc"] == expected and not result["timeout"] and not result["owned_processes_after_wait"]
    results.append({"case": name, "expected_rc": expected, "pass": ok, **result})

def main():
    WORK.mkdir(parents=True, exist_ok=True); build(); results = []
    d = fixture("Fixture-é-空格"); record(results, "fresh-unicode", d, run(d), 0)
    install = d / "state/avd/.jcs2-install-state"; before = install.read_bytes() if install.exists() else b""
    record(results, "idempotent", d, run(d), 0); results[-1]["progress_preserved"] = install.exists() and install.read_bytes() == before
    d = fixture("Bootstrap-é-空格", bootstrap=True); first = run(d); record(results, "bootstrap-first-start", d, first, 0)
    trace = first["trace"]
    results[-1]["split_count"] = trace.count("split1.apk") + trace.count("split2.apk") + trace.count("split3.apk") + trace.count("split4.apk") + trace.count("split5.apk")
    results[-1]["restore_before_unroot"] = trace.find("bootstrap-op shell | tar") < trace.find("adb -P | 5040 | -s | emulator-5596 | unroot")
    second = run(d); record(results, "bootstrap-relaunch-no-restore", d, second, 0)
    results[-1]["restore_skipped"] = "bootstrap-op shell | tar" not in second["trace"]
    d = fixture("Bootstrap-Restore-Failure-é-空格", bootstrap=True); (d / "bootstrap_fail").write_text("1\n")
    failed = run(d); record(results, "bootstrap-restore-failure", d, failed, 13)
    results[-1]["pending_preserved"] = "pending" in (d / "state/avd/.jcs2-bootstrap-state").read_text()
    (d / "bootstrap_fail").unlink(); recovered = run(d); record(results, "bootstrap-retry-after-pending", d, recovered, 0)
    results[-1]["completed"] = "pending" not in (d / "state/avd/.jcs2-bootstrap-state").read_text()
    d = fixture("Bootstrap-Hash-Failure-é-空格", bootstrap=True); set_hash(d, "0" * 64)
    rejected = run(d); record(results, "bootstrap-hash-rejected", d, rejected, 2)
    results[-1]["mutated"] = (d / "state").exists()
    d = fixture("Bootstrap-Traversal-é-空格", bootstrap=True)
    with tarfile.open(d / "assets/bootstrap/synthetic-data.tar", "w") as t:
        p = d / "bad"; p.write_bytes(b"bad"); t.add(p, arcname="../escape")
    set_hash(d, hashlib.sha256((d / "assets/bootstrap/synthetic-data.tar").read_bytes()).hexdigest())
    rejected = run(d); record(results, "bootstrap-traversal-rejected", d, rejected, 2)
    results[-1]["mutated"] = (d / "state").exists()
    d = fixture("Wrong Marker-é-空格"); (d / "state/avd/JCS2-personal.avd").mkdir(parents=True); (d / "state/avd/JCS2-personal.avd/.jcs2-owned").write_text("wrong\n"); record(results, "wrong-marker", d, run(d), 4)
    d = fixture("Port Busy-é-空格"); (d / "port_busy").write_text("5040\n"); record(results, "port-busy-before-mutation", d, run(d), 3); results[-1]["mutated"] = (d / "state").exists()
    d = fixture("Install Failure-é-空格"); (d / "fail_install").write_text("1\n"); record(results, "install-failure", d, run(d), 10)
    d = fixture("Long Stdout-é-空格"); (d / "long_stdout").write_text("1\n"); record(results, "long-stdout", d, run(d), 0)
    d = fixture("Emulator Failure-é-空格"); (d / "emu_fail").write_text("1\n"); record(results, "emulator-failure", d, run(d), 5)
    # A configured bootstrap must never infer first-start from a missing marker
    # on an already-owned AVD.  Preserve the install marker bytes and assert no
    # install/root/restore operation is appended by the rejected retry.
    d = fixture("Bootstrap-Missing-Marker-é-空格", bootstrap=True); first = run(d); assert first["rc"] == 0
    install_before = (d / "state/avd/.jcs2-install-state").read_bytes()
    (d / "state/avd/.jcs2-bootstrap-state").unlink(); trace_before = (d / "trace.txt").read_text()
    missing = run(d); record(results, "bootstrap-missing-marker-existing-progress", d, missing, 13)
    trace_delta = missing["trace"][len(trace_before):]
    results[-1]["progress_preserved"] = (d / "state/avd/.jcs2-install-state").read_bytes() == install_before
    results[-1]["no_mutation"] = not any(x in trace_delta for x in ("install-multiple", " install |", " root", " shell | tar", "chown", "restorecon"))

    # Every bootstrap identity component is checked before app installation.
    d = fixture("Bootstrap-Changed-Game-é-空格", bootstrap=True); assert run(d)["rc"] == 0
    (d / "assets/original/split1.apk").write_bytes(b"changed-game"); trace_before = (d / "trace.txt").read_text()
    changed = run(d); record(results, "bootstrap-changed-game-before-mutation", d, changed, 13)
    results[-1]["no_install"] = "install-multiple" not in changed["trace"][len(trace_before):]

    d = fixture("Bootstrap-Changed-Helper-é-空格", bootstrap=True); assert run(d)["rc"] == 0
    (d / "assets/bootstrap/ru.zenootho.mbnleenbe.apk").write_bytes(b"changed-helper"); trace_before = (d / "trace.txt").read_text()
    changed = run(d); record(results, "bootstrap-changed-helper-before-mutation", d, changed, 13)
    results[-1]["no_install"] = "install-multiple" not in changed["trace"][len(trace_before):]

    d = fixture("Bootstrap-Changed-Archive-é-空格", bootstrap=True); assert run(d)["rc"] == 0
    archive = d / "assets/bootstrap/synthetic-data.tar"; archive.write_bytes(archive.read_bytes() + b"x"); trace_before = (d / "trace.txt").read_text()
    changed = run(d); record(results, "bootstrap-changed-archive-before-mutation", d, changed, 2)
    results[-1]["no_install"] = "install-multiple" not in changed["trace"][len(trace_before):]

    # A pending plan is immutable: changing any identity component rejects the
    # retry instead of replaying a different archive over existing state.
    d = fixture("Bootstrap-Different-Pending-é-空格", bootstrap=True); (d / "bootstrap_fail").write_text("1\n")
    assert run(d)["rc"] == 13; (d / "bootstrap_fail").unlink()
    (d / "assets/original/split1.apk").write_bytes(b"different-pending"); trace_before = (d / "trace.txt").read_text()
    changed = run(d); record(results, "bootstrap-different-pending-identity", d, changed, 13)
    results[-1]["no_install"] = "install-multiple" not in changed["trace"][len(trace_before):]

    d = fixture("Controller-Unicode-é-空格", controller=True)
    result = run(d); record(results, "controller-ready-before-game", d, result, 0)
    args = (d / "controller.args").read_text(errors="replace")
    results[-1]["args_ordered"] = ("--helper-jar" in args and "--adb-path" in args and
                                    "--adb-port" in args and "--adb-serial" in args and
                                    "--mapping" in args and "--ready-file" in args)
    results[-1]["uhid_ready"] = '"transport":"uhid"' in (d / "controller.ready-proof").read_text()
    d = fixture("Controller-Wrong-Serial", controller=True); (d / "controller_wrong_serial").write_text("1\n")
    record(results, "controller-wrong-serial-fail-closed", d, run(d), 14)
    d = fixture("Controller-Wrong-Transport", controller=True); (d / "controller_wrong_transport").write_text("1\n")
    record(results, "controller-wrong-transport-fail-closed", d, run(d), 14)
    d = fixture("Controller-No-Ready", controller=True); (d / "controller_no_ready").write_text("1\n")
    record(results, "controller-no-ready-timeout", d, run(d, timeout=40), 14)
    d = fixture("Controller-Start-Failure", controller=True); (d / "controller_fail").write_text("1\n")
    record(results, "controller-start-failure", d, run(d), 14)
    report = {"proton": str(PROTON), "launcher_sha256": hashlib.sha256((d / "JCS2Launcher.exe").read_bytes()).hexdigest(), "mock_sha256": hashlib.sha256((WORK / "adb.exe").read_bytes()).hexdigest(), "cases": results,
              "fresh_worker_recipe": "Copy launcher/JCS2Launcher.exe beside launcher.ini; set sdk_root, adb, emulator, avd_dir, apk1..apk5, helper_apk=assets\\bootstrap\\ru.zenootho.mbnleenbe.apk, helper_package=ru.zenootho.mbnleenbe, bootstrap_archive=assets\\bootstrap\\synthetic-data.tar, bootstrap_sha256=1b10063f1d7160b1cf98e177c27a5f85bc6167423a09fb31af56443d5350b318; run the launcher with dedicated adb_port=5040/emulator_port=5596 after checking 5040/5596/5597 are free."
              }
    (WORK / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False)); print(json.dumps(report, indent=2, ensure_ascii=False))
    if not all(x["pass"] for x in results): raise SystemExit(1)

if __name__ == "__main__": main()

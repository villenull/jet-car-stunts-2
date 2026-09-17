#!/usr/bin/env python3
"""Exercise the Steam wrapper in an isolated project with a fake runner."""
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

WRAPPER = Path(__file__).with_name('jcs2-steam-launch.sh')


class SteamLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project with spaces"
        self.root.mkdir()
        (self.root / "linux-launcher").mkdir()
        for name in ("runtime_paths.py", "python-select.sh", "jcs2-launcher.sh"):
            shutil.copy2(WRAPPER.parent.parent / "linux-launcher" / name, self.root / "linux-launcher" / name)
        (self.root / 'steam').mkdir()
        # Redirect fixed production ports only in the temporary script so the
        # tests remain independent of a real Desktop/Gaming Mode session.
        with socket.socket() as a, socket.socket() as b, socket.socket() as c:
            sockets = (a, b, c)
            for probe in sockets:
                probe.bind(('127.0.0.1', 0))
            self.ports = [probe.getsockname()[1] for probe in sockets]
        wrapper = WRAPPER.read_text()
        for old, new in zip((5038, 5594, 5595), self.ports):
            wrapper = wrapper.replace(str(old), str(new))
        self.script(self.root / 'steam/launch.sh', wrapper)
        self.bin = self.root / 'custom sdk/platform-tools'
        self.bin.mkdir(parents=True)
        self.script(self.bin / 'adb', '#!/bin/sh\nexit 0\n')
        self.script(self.bin / 'sudo', '#!/bin/sh\necho CALLED >"$SUDO_MARKER"\nexit 1\n')
        self.env = dict({k: v for k, v in os.environ.items() if not k.startswith("JCS2_")}, JCS2_SDK="custom sdk", PATH=f'{self.bin}:{os.environ["PATH"]}',
                        JCS2_LOGDIR=str(self.root / 'logs'),
                        SUDO_MARKER=str(self.root / 'sudo-called'))

    def script(self, path, body):
        path.write_text(body)
        path.chmod(0o755)

    def launch(self):
        return subprocess.Popen(['bash', str(self.root / 'steam/launch.sh')],
                                env=self.env, cwd="/tmp", stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def log(self):
        logs = list((self.root / 'logs').glob('steam-launch-*.log'))
        self.assertEqual(len(logs), 1)
        return logs[0].read_text()

    def require_free_ports(self):
        # Never contact the real runtime or stop another session to run tests.
        for port in self.ports:
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', port))
                except OSError:
                    self.skipTest(f'port {port} already occupied by another session')

    def test_runner_failure_logged_without_sudo(self):
        self.require_free_ports()
        self.script(self.root / 'run-jcs2', '#!/bin/sh\necho fake-runner-failed >&2\nexit 37\n')
        proc = self.launch()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 37)
        self.assertFalse((self.root / 'sudo-called').exists())
        self.assertIn('fake-runner-failed', self.log())
        self.assertIn('Steam launcher start pid=', self.log())
        self.assertIn('exit status=37', self.log())

    def test_steam_overlay_not_inherited_by_runner(self):
        self.require_free_ports()
        self.env['LD_PRELOAD'] = '/tmp/steam32/gameoverlayrenderer.so:/tmp/steam64/gameoverlayrenderer.so'
        self.script(self.root / 'run-jcs2', '#!/bin/sh\nprintf "runner-preload=%s\\n" "${LD_PRELOAD-unset}"\n')
        proc = self.launch()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertIn('runner-preload=unset', self.log())

    def test_busy_port_logged_before_runner(self):
        with socket.socket() as listener:
            try:
                listener.bind(('127.0.0.1', self.ports[0]))
            except OSError:
                self.skipTest('test port acquired by another process')
            listener.listen()
            self.script(self.root / 'run-jcs2', '#!/bin/sh\necho SHOULD-NOT-RUN\n')
            proc = self.launch()
            proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 11)
        self.assertIn(f'port {self.ports[0]} busy', self.log())
        self.assertIn('exit status=11', self.log())
        self.assertNotIn('SHOULD-NOT-RUN', self.log())

    def test_selected_sdk_and_relative_avd_reach_runner(self):
        self.require_free_ports()
        self.env['JCS2_AVD_HOME'] = 'custom avd'
        self.script(self.root / 'run-jcs2', '#!/bin/sh\nprintf "sdk=%s\\navd=%s\\n" "$ANDROID_HOME" "$ANDROID_AVD_HOME"\n')
        proc = self.launch()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f'sdk={self.root / "custom sdk"}', self.log())
        self.assertIn(f'avd={self.root / "custom avd"}', self.log())

    def test_shell_exports_quote_override_without_evaluation(self):
        self.require_free_ports()
        sdk = self.root / "sdk ' $(touch INJECTED)"
        (sdk / 'platform-tools').mkdir(parents=True)
        self.script(sdk / 'platform-tools/adb', '#!/bin/sh\nexit 0\n')
        self.env['JCS2_SDK'] = sdk.name
        self.script(self.root / 'run-jcs2', '#!/bin/sh\nprintf "sdk=%s\\n" "$ANDROID_HOME"\n')
        proc = self.launch()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f'sdk={sdk}', self.log())
        self.assertFalse((self.root / 'INJECTED').exists())

    def test_missing_selected_adb_does_not_use_system_fallback(self):
        self.env['JCS2_SDK'] = 'missing sdk'
        self.script(self.root / 'run-jcs2', '#!/bin/sh\necho SHOULD-NOT-RUN\n')
        proc = self.launch()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 10)
        self.assertIn('adb not found in selected SDK', self.log())
        self.assertNotIn('SHOULD-NOT-RUN', self.log())

    def fake_python(self, path, marker):
        self.script(path, f'#!/bin/sh\necho "$0" >>"{marker}"\nexec {shutil.which("python3")} "$@"\n')

    def test_bundled_python_preferred_and_reaches_runner(self):
        self.require_free_ports()
        bundled = self.root / 'runtime/python/bin/python3'
        bundled.parent.mkdir(parents=True)
        self.fake_python(bundled, self.root / 'python-used')
        (self.root / 'linux-launcher/runner.py').write_text('import sys\nprint("runner-python=" + sys.argv[0])\n')
        self.script(self.root / 'run-jcs2', '#!/usr/bin/env bash\nexec "$(cd "$(dirname "$0")" && pwd)/linux-launcher/jcs2-launcher.sh" "$@"\n')
        proc = self.launch()
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f'python={bundled}', self.log())
        self.assertIn('runner-python=', self.log())
        # Both the wrapper's path resolver and the runner used the bundled interpreter.
        self.assertEqual((self.root / 'python-used').read_text().count(str(bundled)), 2)

    def test_invalid_explicit_python_fails_before_runner(self):
        self.env['JCS2_PYTHON'] = str(self.root / 'missing python')
        self.script(self.root / 'run-jcs2', '#!/bin/sh\necho SHOULD-NOT-RUN\n')
        proc = self.launch()
        output, _ = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 10)
        self.assertIn('JCS2_PYTHON is not an executable interpreter', output)
        self.assertNotIn('SHOULD-NOT-RUN', output)

    def test_installed_layout_marker_needs_no_launch_options(self):
        self.require_free_ports()
        self.root = self.root.rename(self.root.with_name('Jet Car Stunts 2 é'))
        self.env.pop('JCS2_SDK'); self.env.pop('JCS2_LOGDIR')
        (self.root / 'jcs2-layout.json').write_text('{"schema": 1, "layout": "portable", "avd": "jcs2"}')
        (self.root / 'runtime/sdk/platform-tools').mkdir(parents=True)
        self.script(self.root / 'runtime/sdk/platform-tools/adb', '#!/bin/sh\nexit 0\n')
        self.script(self.root / 'run-jcs2', '#!/bin/sh\nprintf "sdk=%s\\navd=%s\\n" "$ANDROID_HOME" "$ANDROID_AVD_HOME"\n')
        proc = subprocess.Popen(['bash', str(self.root / 'steam/launch.sh')], env=self.env, cwd='/', stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        logs = list((self.root / 'state/logs').glob('steam-launch-*.log'))
        self.assertEqual(len(logs), 1)
        log = logs[0].read_text()
        self.assertIn(f'sdk={self.root / "runtime/sdk"}', log)
        self.assertIn(f'avd={self.root / "state/avd"}', log)

    def test_term_forwarded_and_cleanup_waited(self):
        self.require_free_ports()
        self.script(self.root / 'run-jcs2', '''#!/usr/bin/env python3
import signal
import shutil
import time
from pathlib import Path
stopped = False
def stop(*_):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, stop)
Path('ready').touch()
while not stopped:
    time.sleep(0.01)
time.sleep(0.15)
print('runner-cleanup-complete', flush=True)
raise SystemExit(130)
''')
        proc = self.launch()
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'ready').exists():
                self.assertIsNone(proc.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 130)
            log = self.log()
            self.assertLess(log.index('runner-cleanup-complete'), log.index('exit status=130'))
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()

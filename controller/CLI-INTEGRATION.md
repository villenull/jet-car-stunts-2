# Launcher integration contract

The launcher owns the persistent ADB server and selects the guest explicitly. It
must start one helper process for the whole controller session (never one process
per frame), then write bounded line-oriented commands to its stdin:

```text
key <android-keycode> <down|up>
axis <x> <y> <z> <rz> <lt> <rt>
neutral
quit
```

`x/y/z/rz` are clamped to `[-1,1]`; `lt/rt` are clamped to `[0,1]`. The helper
uses `app_process` as shell UID and reflection for `InputManager.injectInputEvent`:

```text
adb -P <port> -s <serial> shell CLASSPATH=/data/local/tmp/jcs2-input-helper.jar \
  app_process /system/bin Jcs2InputHelper
```

The Go bridge can own this persistent child directly (Windows XInput and Linux
replay use the same state-to-stdin path):

```text
jcs2-controller.exe --helper-jar C:\\session\\jcs2-input-helper.jar \
  --adb-path C:\\Android\\platform-tools\\adb.exe --adb-port 5038 \
  --adb-serial 127.0.0.1:5595 --ready-file C:\\session\\ready.json
```

`--helper-jar` is a host path: the bridge pushes it once to the fixed guest path
`/data/local/tmp/jcs2-input-helper.jar`, confirms shell UID 2000, then launches
the guest helper. The helper emits `READY JCS2_UHID <device-id>` only after
Android enumerates the new joystick device, and every key/axis/neutral command
must receive an `ACK`; startup and command failures are fatal. `--ready-file`
must be an absolute host path; it is atomically written only after readiness and
contains `{serial, transport, device_id}`. Omit `--helper-jar` only for an explicitly selected legacy
emulator-console run; helper startup failure never silently falls back.

Use configurable `adb` path, ADB port, serial, and helper JAR path. On normal
EOF, parse error, timeout, disconnect, or launcher shutdown, close stdin and
terminate the helper; it emits held-key releases and neutral axes in its bounded
teardown path. The Windows XInput poller should map A/B/X/Y/LB/RB/START/BACK,
sticks, and triggers into this exact command stream; Linux replay should feed the
same stream/state provider.

The helper is intentionally not a privileged APK, does not request a hidden
permission, and does not require root. Its input path is real Android input
injection, but a guest must expose an actual joystick InputDevice for game axis
consumers to recognize the MotionEvents.

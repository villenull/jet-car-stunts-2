# JCS2 Windows controller bridge

This directory is the controller-owned handoff for the launcher worker. `jcs2-controller.exe`
is intended to run on Windows, poll one XInput controller through the OS-provided
`xinput1_4.dll` (falling back to `xinput9_1_0.dll`), and write low-latency NDJSON events to
stdout. It never starts an ADB process per frame, opens a network listener, installs a driver,
or injects into the desktop. The launcher owns the persistent, explicitly-selected ADB server
and Android emulator console transport.

The event protocol is deliberately runtime-neutral:

```json
{"type":"axis","axis":"AXIS_X","value":-0.42,"t_ms":1234}
{"type":"button","key":"KEYCODE_BUTTON_A","action":"down","t_ms":1234}
{"type":"device","connected":false,"t_ms":1234}
```

Axis values are normalized to `[-1,1]`; triggers use `[0,1]`. A configurable per-axis deadzone
is applied to stick values, and every button held by a disconnected controller is emitted as `up` before
the `device` disconnect record. Mapping is in `mapping.json`; no game APK or launcher files
are modified by this worker.

## Android path decision

The preferred path is a guest-native UHID gamepad created by the persistent helper. Android's emulator console supports
fake `EV_KEY`/`EV_ABS` gamepad events and documents face buttons, shoulders, triggers, sticks,
and D-pad codes. The bridge should therefore keep one explicitly-selected helper/ADB session
for the guest, not use repeated `adb shell input` calls. The helper creates an Xbox-compatible
temporary device through `/dev/uhid`, giving Android a real joystick device ID for True Axis,
then destroys it on EOF or disconnect.
The APK statically contains True Axis `InputManagerCompat`/`InputManagerV16`, `JaypadIsSupported`,
`AXIS_X`, `AXIS_Y`, `AXIS_RX`, `AXIS_RY`, `AXIS_GAS`, and `AXIS_BRAKE`, so standard joystick
events are the correct target shape.

The helper uses shell UID 2000 reflection for KeyEvent fallback and opens `/dev/uhid` to create
the actual joystick device; it does not grant hidden permissions or require root. A runtime
without usable shell-UID UHID access must be reported as a transport failure, not silently
treated as controller-ready. Validation records the selected device ID, sources, ranges, and
visible JCS2 response.

## Build and test (on Windows with Go installed)

```powershell
$env:CGO_ENABLED="0"
go test ./controller/...
go build -trimpath -ldflags "-s -w" -o jcs2-controller.exe ./controller/cmd/jcs2-controller
```

Runtime contract (the launcher supplies a dedicated token path; the bridge never prints or
searches global emulator token files):

```powershell
.\jcs2-controller.exe --console-port 5596 --token-file .\session\console-auth-token --deadzone 0.12
```

The default Windows console port is 5596; coordinator-assigned guest tests may override it to
5594. The process keeps one authenticated TCP connection open and translates each button or
axis record to `event send EV_KEY:...` / `event send EV_ABS:...` followed by an explicit
`event send EV_SYN:SYN_REPORT:0`, waiting for an exact `OK` response for every command. The
console banner is consumed before `auth`; connect/read/write operations are bounded to two
seconds. `--trace` is diagnostic-only NDJSON to stdout; product input uses the console socket.
The console token file must be explicitly provided by the launcher and is never included in
logs or artifacts.

The same binary also provides deterministic replay mode (available as the Linux utility and
on Windows):

```sh
jcs2-controller-replay --console-port 5594 --token-file ./session/console-auth-token \
  --mapping ./controller/mapping.json --replay ./controller/example.ndjson --trace
```

Replay is one JSON event per line; aliases such as `A` and `LX` are resolved through
`mapping.json`, so changing the mapping changes emitted guest event codes. Use `--replay -`
for stdin. The Windows poller emits held-button releases on disconnect and Ctrl+C.

The Linux host has no Go compiler and no Android emulator binary, so this checkout contains
source and deterministic synthetic tests only; it does not claim a physical-controller or
Windows build result.

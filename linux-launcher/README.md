# Linux launcher

Run the preserved, offline Android game from a normal writable home folder:

```sh
./run-jcs2
```

The launcher owns an API28 `hardened_api28` guest, uses only ADB 5038, console
5594, and serial `127.0.0.1:5595`, and starts the emulator with 1536 MiB,
`-no-snapshot`, and QEMU `-net none`. GPU mode follows the session: `-gpu host`
on a desktop host whose Hyprland session can be queried, and
`-gpu swiftshader_indirect` in Gaming Mode, where gamescope owns the output and
the accelerated path loses the guest's color buffer a few seconds after launch
(emulator.log `ColorBuffer::create ... gl error 0x502`); `JCS2_GPU` overrides
either choice. It performs narrow
root-isolation, verifies shell UID 2000 can read/write `/dev/uhid`, then
launches the already-installed game without reinstall, restore, wipe, or
progress reset. The helper package/service is also required and is kept live by
the persistent controller app_process; `stage=game-launch` foregrounds only the
game and does not claim to open a helper UI. The default is a visible emulator
window and the first readable Linux joystick; joystick axis/button maps are
queried with ioctl, so Valve Steam Controller node indexes are not assumed.

For deterministic local QA, use the same launcher with an explicit headless
and socket input mode:

```sh
./run-jcs2 --headless --input qa
```

The launcher prints and logs a per-run Unix socket at
`analysis/linux-launcher/logs/run-*/input.sock`. Send controlled NDJSON events
to that socket; they feed the same controller stdin protocol as the physical
bridge. The socket starts at the runner, so it exercises only the
runner → controller → guest path: it never reads a js node, so it verifies
neither the joystick bridge nor a physical device. To exercise the bridge
without a physical press, put a virtual pad on a js node and pin the bridge to
it with `JCS2_JOYSTICK=/dev/input/jsN`. For example:

```sh
printf '%s\n' '{"type":"button","key":"RB","action":"down","t_ms":0}' \
  | socat - UNIX-CONNECT:analysis/linux-launcher/logs/run-*/input.sock
```

The controller and its persistent helper app_process must stay live while the
game is played. Closing the emulator window or pressing Ctrl-C releases held
controls, closes controller stdin to deliver EOF, and then terminates only the
runner-owned process groups with bounded TERM/KILL and waits. Every stage,
direct subprocess command,
return code, controller event, READY marker, UID/network check, and foreground
activity check is persisted under the run directory. Existing profile state is
preserved between runs.

The bridge uses the standard Linux constants `ABS_X/LX`, `ABS_Y/LY`,
`ABS_Z/LT`, `ABS_RX/RX`, `ABS_RY/RY`, and `ABS_RZ/RT`. `BTN_TL` and `BTN_TR`
map to LB/RB; `BTN_TL2` and `BTN_TR2` are deliberately ignored because they
are separate shoulder-2 buttons, not LB/RB. HAT and other device-specific
codes are logged but unsupported. The bridge records each selected node's
actual axis/button counts, kernel codes, names, and supported mappings, so a
Steam Deck node exposing a partial map is visible in the run log. A user press
is still required to prove physical response; synthetic socket events are the
fully local QA path.

The Linux launcher has been tested on the current Omarchy development host.
SteamOS Deck compatibility is not yet verified; the intended use is a normal
home-folder/non-Steam-game add-path, with no system modifications or Windows
runtime dependency. Physical button response still requires a user press;
synthetic socket events provide a fully local QA path.

## Runtime paths and offline checks

The normal checkout keeps the existing SDK and saved AVD in their historical
`analysis/` directories. Controller mapping comes from `controller/mapping.json`;
the Linux launcher no longer depends on `dist/JCS2-Windows-Personal`.
Creating a `runtime/` or `state/` directory never changes the selected guest.

An explicitly prepared portable package uses `JCS2_LAYOUT=portable`:

- `runtime/sdk/`: Linux emulator, platform-tools and required system images.
- `state/avd/`: preserved `hardened_api28.ini` and `hardened_api28.avd/`.
- `assets/controller/mapping.json`: controller mapping.
- `linux-launcher/`: launcher, Linux controller executable and helper JAR.
- `state/logs/`: launcher and Steam logs.

`JCS2_SDK`, `JCS2_AVD_HOME`, `JCS2_AVD`, `JCS2_DIST`, `JCS2_CONTROLLER`,
and `JCS2_LOGDIR` override either layout. `JCS2_DIST` means the parent of
`controller/mapping.json`. Relative path overrides resolve against the project
root regardless of the current directory or Steam's “Start In” field.
`runtime_paths.py` is shared by the Python runner and Steam wrapper.

These commands only read local configuration/files; they do not start ADB,
probe ports/devices, launch the emulator, or modify saved progress:

```sh
python3 linux-launcher/runtime_paths.py
python3 linux-launcher/runtime_paths.py --check
JCS2_LAYOUT=portable python3 linux-launcher/runtime_paths.py --check
```

The default command prints resolved paths as JSON. `--check` returns nonzero
for missing required local files. A passing check does not prove that an AVD
boots or that its backing images are self-contained. The current preserved AVD
still contains external absolute image references: package preparation must
rewrite references in a staged copy and preserve all backing images before
portable mode can be used. The resolver never copies, repairs, creates, wipes,
or reinstalls an AVD. Keep the working guest unchanged. Fresh SteamOS launch
and saved-progress persistence remain user-operated live checks.

## Driving mode: gamepad only

There is no host-side driving-mode panel and no `--control-settings` launch
option (both removed 2026-09-18). The lane launches straight into the emulator
and the game: **Gamepad** is the only driving mode. The physical left stick
controls turning and pitching together, every button and trigger keeps its
mapped action, and the game's own Gamepad toggle stays **ON** (with it OFF the
game ignores the sensor feed entirely).

**Tilt Drive is unselectable, not deleted.** The game's window requests
`SCREEN_ORIENTATION_SENSOR_LANDSCAPE`, so the framework derives the display
quarter from the very accelerometer the tilt mirror feeds and the picture flips
whenever the Deck is lifted; this image has no rotation-hold lever. The engine
under `tilt_control/` is kept intact for an image that can hold the display,
`TILT_DRIVE_SELECTABLE = False` in `runner.py` closes the selection, and a lane
that persisted Tilt Drive migrates once to Gamepad at startup with a
`stage=tilt-drive-disabled` log row. While a persisted Tilt mode is in force the
LX/LY gate stays armed fail-closed (no silent re-enable of the sticks); the
retired panel's `control_mode` request is rejected at the runner's input ingress
and never reaches the guest.

The **View / SELECT** button is an ordinary game button again: it is forwarded
to the guest like any other press and opens nothing on the host.

Settings persist in the selected launcher log directory's `tilt-settings.json`
(default `analysis/linux-launcher/logs/tilt-settings.json`, portable
`state/logs/tilt-settings.json`; `JCS2_LOGDIR` overrides it). CLI and runner
share this default; the historical `linux-launcher/tilt-settings.json` path is
not the active default. Reading an old settings file never rewrites it, with one
exception: a persisted Tilt mode is migrated to Gamepad once, visibly, at
startup. Legacy mode-only files remain supported; older per-axis fields are
accepted only when their effective choices agree, and mixed or invalid values
resolve to Gamepad.

Tilt Drive requires the physical **Steam Deck Motion Sensors** device, not
merely Steam's virtual Xbox controller. With Tilt unselectable that device is
only mirrored as the native accelerometer feed; the runner never synthesizes
IMU axes (`consume_motion` is never called), so Deck tilt reaches the game only
as Android accelerometer vectors. The runner's mode is reported in the run
directory's `control-mode.json`.

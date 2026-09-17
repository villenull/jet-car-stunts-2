# Linux launcher

Run the preserved, offline Android game from a normal writable home folder:

```sh
./run-jcs2
```

The launcher owns an API28 `hardened_api28` guest, uses only ADB 5038, console
5594, and serial `127.0.0.1:5595`, and starts the emulator with 1536 MiB,
host GPU rendering, `-no-snapshot`, and QEMU `-net none`. It performs narrow
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
bridge. For example:

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

## Touch driving-mode settings

The optional **JCS2 Controls** panel offers exactly two driving modes:
**Left joystick** controls both turning and pitching, or **Tilt** controls both. The panel opens on request and closes before returning to the game. Physical buttons retain their existing
driving actions. This does not add D-pad menu navigation or digital button
steering; the non-tilt alternative is the left joystick.

In Steam, open the existing JCS2 shortcut's **Properties → Launch Options** and
enter `--control-settings`. On your next launch, tap your driving mode and
**Save & Play**. Cancel exits before launching the emulator and does not save.
Remove the option to launch directly with the saved mode. The optional startup panel is separate from the in-session View-button access
described below.
The panel uses Python Tk/Tcl, available on the current host; fresh SteamOS
availability and its appearance/touch focus in Gamescope remain user test items.

Settings persist in the selected launcher log directory's `tilt-settings.json`
(default `analysis/linux-launcher/logs/tilt-settings.json`, portable
`state/logs/tilt-settings.json`; `JCS2_LOGDIR` overrides it). CLI and runner now
share this default; the historical `linux-launcher/tilt-settings.json` path is
not the active default. No existing settings file was rewritten during development.

Tilt requires the physical **Steam Deck Motion Sensors** device, not merely
Steam's virtual Xbox controller. If the sensor cannot open, physical left-stick
input remains active on both axes. When Tilt is selected and the sensor opens, both joystick axes are suppressed
in favor of motion input. Physical direction, pitch feel, calibration,
and motion-device availability in Gaming Mode still need user testing. The
panel itself does not open motion devices. Agents did not display the panel or
interact with the current game to validate it.

Saving a mode updates turning and pitching together in one atomic file write.
Legacy mode-only files remain supported. Older per-axis fields are accepted
only when their effective choices agree; mixed or invalid values resolve to
Left joystick until explicitly changed. Reading old files never rewrites them.

### Switch during a game session

1. Pause the game normally with **Menu / START**.
2. Press **View** (the small button left of the screen) to open Driving mode.
3. Tap **Left joystick** or **Tilt**, then **Apply & Return**.
4. Resume through the game's normal touch menu. The emulator and game stay open.

View opens the panel; its choices and Apply/Return buttons support touch. There
is no permanent touch overlay or D-pad menu navigation. Opening settings does
not automatically toggle pause, so pause first. Driving input is neutralized
while the panel is open. **Return to game** closes it without requesting a new
mode. Apply waits for the running launcher's acknowledgement; it does not merely
edit a file and assume the controls changed. If the sensor cannot open, the
panel reports the failure and keeps the joystick active. Both axes switch
together, and the effective choice is persisted by the runner.

The panel has its own normal cursor; emulator cursor hiding stays scoped to the
game window. The runner owns the panel lifecycle and restores the game window
when it exits. This prepared in-session flow still needs user validation for
View delivery under Steam Input, touch/focus return, and physical tilt behavior.
It does not modify an already-running launcher process: the updated launcher
code takes effect on the user's next normal game launch.

# Launch JCS2 from Steam

Menus are touch-only. The launcher does not run screen detection or synthesize
D-pad/A/B menu taps, and it does not publish a menu cursor or start a highlight
overlay. D-pad and A/B are unassigned; START retains pause. See
`controller-notes.txt` for driving controls and remaining physical checks.

The existing non-Steam shortcut targets:

`/home/deck/Projects/JCS2/steam/jcs2-steam-launch.sh`

Use the native Linux shortcut with no Proton compatibility override and empty
launch options. The wrapper itself supplies the lane spec the desktop
supervisor runs: `JCS2_LAYOUT=portable`, `JCS2_AVD=jcs2-fresh`,
`JCS2_SDK=<project root>/state/emulator-ab-10696886` (the prepared legacy32 SDK),
`QT_XCB_NO_XI2=1`, and `--input joystick` on the runner command line. It never
bakes in a desktop session (`DISPLAY`, `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`,
`HYPRLAND_INSTANCE_SIGNATURE`): Gaming Mode has its own session and
`linux-launcher/gamescope_window.py` presents the game window. Explicit `JCS2_*`
values in the launch environment still win over these defaults.

Stop any existing JCS2 desktop game before launching from Gaming Mode; both use
the same saved Android profile and ports.

Leave Steam Input off for this shortcut. That is the default for a non-Steam
shortcut, no Steam Input layout is configured for it, and nothing in this repo
changes any Steam setting; the lane reads the pad through the joystick bridge.

Shortcut artwork comes from `steam/artwork/` (`cover.png`, `landscape.png`,
`hero.png`, `logo.png`, `icon.png`) and is installed into the account's grid by:

```sh
cd /home/deck/Projects/JCS2
python3 steam/steam_shortcut.py plan --install-root "$PWD" --account 1486631847 \
  --launch-target steam/jcs2-steam-launch.sh --launch-options ""
python3 steam/steam_shortcut.py register --install-root "$PWD" --account 1486631847 \
  --launch-target steam/jcs2-steam-launch.sh --launch-options "" --confirm-steam-closed
```

Run this with Steam closed. `plan` is a read-only dry run that reports the
derived appid, the would-install artwork and any warning; `register` needs
`install-state.json` holding `{"status": "complete"}` at the install root, which
the installer's finalize stage writes, and otherwise refuses with exit 7 without
writing anything.

A dev checkout has no state file (the installer was never run there), so write it
with the installer's own verified finalize: place the setup entry exactly as the
setup archive ships it, then run only that stage. It checks every shipped file
and the payload binaries before writing `install-state.json`,
`state/runtime-manifest.json` and `state/runtime-manifest.md5`.

```sh
cd /home/deck/Projects/JCS2
cp packaging/installer/setup-jcs2.sh ./setup-jcs2.sh
python3 -c "import sys; sys.path.insert(0, 'packaging/installer'); import install_jcs2 as I; cfg = I.build_config(I.parse_args(['--root', '$PWD', '--execute', '--json'])); I.stage_finalize(cfg, I.Report('execute', cfg))"
```

Never run the full `--execute` install on a live lane: its guest and smoke stages
boot a second emulator on the same ports.

No `sudo -v` preparation is required. The former blanket `sudo -n true` check
caused the confirmed September 12 immediate exits and has been removed.
The runner explicitly supplies the emulator's ADB binary with `-adb-path`;
SDK environment variables alone did not prevent its blocking detection dialog.

Startup output and exit status are saved in the lane's log directory —
`state/logs/steam-launch-*.log` for the portable layout, `analysis/linux-launcher/logs/`
only for the legacy layout or an explicit `JCS2_LOGDIR`. The corresponding `run-*`
directory contains emulator, controller, and detailed launcher logs. An occupied
port produces exit status 11 and an explanation in the Steam launch log; the
script does not terminate an existing session.

In Gaming Mode, the runner starts `linux-launcher/gamescope_window.py` to
hide the separate gray Qt toolbar and present the main game window. It only
touches windows matching the owned emulator PID and class; it never closes the
toolbar, because closing it exits the emulator. It watches for the toolbar
reappearing after rotation, without repeatedly requesting game focus otherwise.
Diagnostics are in the run's `gamescope-window.log`. This window-selection fix
requires the user's next Gaming Mode test; unit tests are not display proof.

The user will perform future session switches, launches, and driving tests.
Do not switch sessions or start live tests automatically.

Gaming Mode controller behavior still needs physical verification. The desktop
controller handover helper assumes an active desktop mapper and must not be
started blindly in Gaming Mode. Do not use desktop gameplay as proof that the
Gaming Mode input path works.

## Path selection

The wrapper and runner share `linux-launcher/runtime_paths.py`. A checkout
without an installer marker launches the portable lane: `state/avd` for the
`jcs2-fresh` profile, `assets` for controller data, `state/logs` for logs, and
the prepared legacy32 SDK in `state/emulator-ab-10696886`. An installed copy
carries `jcs2-layout.json`, and its own `runtime/sdk`, `state/avd` and profile
name stay authoritative — the wrapper leaves marker defaults alone. The portable
package must be prepared before the first launch; `JCS2_LAYOUT=legacy` restores
the current SDK and saved guest.

Path overrides (`JCS2_SDK`, `JCS2_AVD_HOME`, `JCS2_DIST`, `JCS2_CONTROLLER`,
`JCS2_LOGDIR`) work in Steam and resolve relative to the project root. `JCS2_AVD`
selects the profile name. The wrapper checks ADB inside the selected SDK and never
substitutes a system ADB. Missing ADB returns status 10.

Use `python3 linux-launcher/runtime_paths.py --check` for an offline filesystem
check. It never starts the runtime. See `../linux-launcher/README.md` for the
layout and preserved-AVD relocation limitations.

There is no pre-launch controls panel and no `--control-settings` launch option:
the lane starts the game directly on the physical joystick path. Driving comes
from the game's own Gamepad setting; see `controller-notes.txt` for the mapped
buttons and `../docs/SETUP.md` §3 for the gamepad/tilt status. No D-pad menu
navigation is enabled.

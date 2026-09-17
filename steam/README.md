# Launch JCS2 from Steam

Menus are touch-only. The launcher does not run screen detection or synthesize
D-pad/A/B menu taps, and it does not publish a menu cursor or start a highlight
overlay. D-pad and A/B are unassigned; START retains pause. See
`controller-notes.txt` for driving controls and remaining physical checks.

The existing non-Steam shortcut targets:

`/home/deck/Projects/JCS2/steam/jcs2-steam-launch.sh`

Use the native Linux shortcut with no Proton compatibility override and no
additional launch options. Stop any existing JCS2 desktop game before launching
from Gaming Mode; both use the same saved Android profile and ports.

No `sudo -v` preparation is required. The former blanket `sudo -n true` check
caused the confirmed September 12 immediate exits and has been removed.
The runner explicitly supplies the emulator's ADB binary with `-adb-path`;
SDK environment variables alone did not prevent its blocking detection dialog.

Startup output and exit status are saved under
`analysis/linux-launcher/logs/steam-launch-*.log`. The corresponding `run-*`
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

The wrapper and runner share `linux-launcher/runtime_paths.py`. Normal launches
retain the current SDK and saved guest. Optional `JCS2_LAYOUT=portable` selects
a separately prepared `runtime/sdk`, `state/avd`, and `assets` layout; it must
not be enabled before that package is prepared. Path overrides (`JCS2_SDK`,
`JCS2_AVD_HOME`, `JCS2_DIST`, `JCS2_CONTROLLER`, `JCS2_LOGDIR`) work in Steam
and resolve relative to the project root. `JCS2_AVD` selects the profile name.
The wrapper checks ADB inside the selected SDK and never substitutes a system
ADB. Missing ADB returns status 10.

Use `python3 linux-launcher/runtime_paths.py --check` for an offline filesystem
check. It never starts the runtime. See `../linux-launcher/README.md` for the
layout and preserved-AVD relocation limitations.

For the optional touch controls panel, set this shortcut's **Properties → Launch
Options** to `--control-settings`. It appears before the emulator and closes
when you tap **Save & Play**. Choose **Left joystick** for both turning and pitching, or
**Tilt** for both; there are no mixed-axis modes. Cancel returns without starting the game. Removing the
option restores direct launch using the saved settings. This panel is newly
prepared and still needs user testing in Gaming Mode; it is not an in-game
settings overlay. See `../linux-launcher/README.md` for persistence and sensor
fallback details. No D-pad menu navigation is enabled.

During a session, pause with **Menu / START**, then press **View** to open the
touch Driving mode panel. Choose **Left joystick** or **Tilt**, tap **Apply &
Return**, and resume with the game's touch menu. The game remains running;
`--control-settings` is only needed if you also want the panel before launch.
View is the deliberate opener, and START keeps its pause action. This new flow
requires the user's next launch of the updated code and physical verification;
agents have not restarted the current game.

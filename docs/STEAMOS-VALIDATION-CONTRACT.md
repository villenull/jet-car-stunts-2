# SteamOS validation contract for the landed GPU-selection change

Owner: Deck operator agent (GamingModeFinish). Landed source change, no runtime
experiments performed on the Omarchy Deck (parent instruction), no Deck contact.

## 1. What landed

`linux-launcher/runner.py` (sha256 `5e0d886b74acdac933a6070d707abe7ed3f9371e2964531ff3d25d6b6e8bc89d`),
`linux-launcher/test-runner.py` (sha256 `176ee00f593efc046ab8c1c4823d52b66c72926b3cb4b61ec244c7e1d4faabab`);
focused file result: `python3 test-runner.py` -> 87 tests, OK.

- **Renderer choice is now a capability decision, not a session decision.**
  New `host_gpu_capable()`: true when `/dev/dri/renderD*` exists and the lane's
  user can read and write it (cached per session). `gpu_mode()` = `JCS2_GPU`
  override, else `host` when capable, else `swiftshader_indirect`.
  Previously the mode hung on `desktop_compositor_available()`, a Hyprland probe
  that answers "can this lane measure the host window" - which is what the align
  stage needs, not what rendering needs. Consequence: every session without a
  queryable Hyprland (gamescope, and any stock SteamOS desktop) fell back to
  software on machines whose GPU was usable.
- **The decision is now logged**: `stage=gpu-mode {"mode": ..., "reason": ...}`
  immediately before `stage=emulator-start`, so a lane log states which renderer
  ran and why (reasons: `JCS2_GPU override`, `usable DRM render node`,
  `no usable DRM render node`).
- **No silent fallback**: a host lane that cannot boot still fails loudly
  (`emulator exited during boot`); the documented remedy is the override.
- The Hyprland probe still governs the *align* stage only (line ~1265), which is
  correct and unchanged: under gamescope/SteamOS desktops the lane parks the
  guest rotation instead of measuring.

Evidence behind the change (all measured, artifacts preserved): software floor
22.5 fps menus and 29.07-29.59 fps race (`menu1.txt`, `raceA/B/race20.txt` in the
verified backup); one direct host-GPU run rendered the same menus at 59.14 fps
(game pid 295 swaps / 4.971 s; SurfaceFlinger 1:1) with the game's own setting at
60fps - `v1-direct-host-run/` here.

## 2. What fresh SteamOS must verify (the gate)

Run on the reset SteamOS machine, lane started through `steam/jcs2-steam-launch.sh`
(ideally via actual Steam UI Play), once per mode (Desktop Mode and Gaming Mode):

1. **Renderer row.** `state/logs/run-*/launcher.ndjson` contains
   `stage=gpu-mode` with `mode` and `reason`; record both verbatim. `mode=host`
   with `reason="usable DRM render node"` is the success case.
2. **Filesystem prerequisites.**
   - `/dev/dri/renderD*` exists and is writable by the lane user (`ls -l`,
     `test -r/-w`); if not, the lane must log `display-gpu-unavailable` and run
     software - that is a machine finding, not a code failure.
   - The AVD's system image is present under the resolved SDK
     (`platforms/android-28` must also exist for the emulator to accept the SDK
     root; RecoveryArchive pinned `platform-28` for exactly that).
   - Guest library extraction: `lib/armeabi-v7a/libtrueaxis.so` in the payload is
     **deflated**, so it must be extracted; capture
     `dumpsys package com.trueaxis.jetcarstunts2 | grep -E "codePath|nativeLibraryDir|primaryCpuAbi"`
     plus `ls -l` and `sha256sum` of that directory's contents **before** stopping
     the emulator. Expected lib size 2,227,488 B, sha256 `cb2bd45b...f0ffb1`
     (monolith). An empty directory is a real install/commit failure, not a
     resolution quirk.
3. **Session prerequisites.** `DISPLAY` set and pointing at a live X server: the
   pinned emulator's Qt ships **only the xcb plugin**, so an empty `DISPLAY`
   aborts at startup and a transient/stale display kills it with
   `X connection broken`. Gaming Mode must therefore present gamescope's Xwayland
   display (e.g. `DESKTOP_SESSION=gamescope-wayland`, `GAMESCOPE_WAYLAND_DISPLAY`),
   Desktop Mode its own Xwayland. The lane forces `ENABLE_GAMESCOPE_WSI=0` for
   children (Steam's WSI layer otherwise kills the emulator's boot).
4. **Frame measurement (user unchanged priority: smooth play).** With a race
   entered and the game's own `FRAMERATE` at 60fps:
   `atrace -t 20 -b 16384 gfx -a com.trueaxis.jetcarstunts2 -o /data/local/tmp/t.txt`
   then count **game-pid only** `eglSwapBuffers` (SurfaceFlinger counted
   separately - `atrace -a` does not exclude it). Compare against the software
   floor 29.07-29.59 fps. Sustained window >= 20 s, plus median/p95 frame
   intervals and no `DmaMap`/`bad color buffer handle` escalation beyond the
   baseline two lines the software lane emits.
5. **Visuals.** Upright paired host+guest captures during the race (host via the
   session's own capture tool), and the game fullscreen in the session's window.
6. **Lifecycle.** One clean user-facing stop (game quit / Steam Stop) with
   `stage=cleanup-complete errors:[]`, no relaunch, ports 5038/5594/5595 free,
   then a second start to confirm no fresh-boot regression.
7. **Fallback path.** If `mode=host` fails to boot on that machine, set
   `JCS2_GPU=swiftshader_indirect` and confirm the lane runs; record that the
   override works, since that is the documented remedy until the host path is
   proven there.

## 3. Boundaries and claims

- RecoveryArchive owns the installer, wrappers and docs. They must not label the
  frame rate "fixed" until step 4 passes on fresh SteamOS; the honest current
  statement is "the lane now selects the host renderer whenever the machine has a
  usable render node, and the accelerated path is unverified on SteamOS".
- No new env var was introduced and the default changed only in the sense above;
  `JCS2_GPU` remains the override. If the wrapper needs a user-facing way to set
  it on a machine where host fails, that is RecoveryArchive's call.
- The Omarchy Deck is deliberately not a validation target for this gate: its
  Gaming Mode host attempt died with an unexplained colour-buffer failure, and the
  parent ruled further GPU experiments there out.

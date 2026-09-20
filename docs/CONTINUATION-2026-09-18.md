# JCS2 Steam Deck Recovery — Overnight Report (2026-09-18)

> **Read with `docs/CONTINUATION.md` §9.** This is the dated overnight report
> of the Deck lane work; §9 carries the current paused state, the durable
> backup mapping and the known limits that outlived this report. Evidence it
> cites under `/tmp` (`/tmp/jcs2-ev-*`, `/tmp/hudwork/`) is volatile by
> design — the durable copies of the lane logs are in the off-Deck backup
> (`logs/`), and this report is the record for anything no longer on disk.

## Contract (user-confirmed)
Fully working by morning: launch chain, auto-rotate upright, no RAM warning on fresh install,
1:1 touch, R2 accel / L2 brake-reverse / R1 boost / L1 handbrake / Y reset / Start=pause,
left stick steer+pitch (non-tilt), AUDIBLE race audio, pixel-corrected right-column HUD buttons.
No physical user actions except where unavoidable. DeepSeek v4.1 Flash workers only (no Muse, ever).

## Completed (verified with evidence)
1. **Rotation fixed at source.** runner.py: Gamepad mode parks guest accelerometer at flat pose
   (no live IMU mirror), new `align_visible_emulator` stage (provoke one guest quarter-turn,
   bounded console compensates, re-park), `guard_display_orientation` in the 2s poll.
   Log: `stage=display-orientation-aligned guest_rotation=1 provoked_from=3 rotate_steps=2`.
   Stability: 72–90s monitor, 12-13 samples, 0 repairs, held through sleep/wake.
2. **Pad device streaming proved fixed.** After lizard-mode re-send (passwordless
   `/usr/local/sbin/deck-lizard-mode on/off`, NOPASSWD) + USB unbind/rebind (root via SSH,
   user password once) → js0+js1 re-created; armed evdev probe recorded 1127 live events
   incl. real right-trigger press on event5 and live axis data on event7.
3. **L2 brake-reverse fix live.** joystick_bridge.py: physical L2/LT now passes through to the
   guest LT brake axis (previously rewritten to RT gas = "L2 drives forward" bug);
   R2 keeps the digital R1 accelerator. In-race tutorial prompt independently confirms binding.
4. **Race HUD buttons pixel-corrected.** Root cause: game's persisted encrypted options blob
   holds m_fHudPositions six right-hand buttons shifted −664 logical units vs defaults.
   New linux-launcher/hud_layout.py restores array + live Hud::Button centres over /proc/<pid>/mem
   via the image's own `su 0` (guest stays unrooted/uid2000 invariant), page preserving writes,
   verification read-back, fail-closed guards. Deployed + live-applied to pid 3223:
   read-back array == defaults for all 12 pairs. Rendered PROOF (two independent in-race
   captures 2 s apart): /tmp/hudwork/race-fixed-1.png, race-fixed-2.png; vision measure:
   green up-arrow x 1110–1245 y ~460–550; red reverse x 1130–1200 y ~700–770; no button in
   x 300–450; left column unchanged; before-frames show same sprites at x 327–398.
5. **Audible race audio confirmed.** STREAM_MUSIC 5/15 → 15/15 (`media volume --stream 3 --set 15`,
   persisted in userdata settings_system.xml) + Deck Speaker sink 20% → 100% (WirePlumber
   persisted). Measured race audio peaks: before 60fps −scan (guest vol 5/15); after-restart
   15/15; 30fps cadence; final config; in-race sustained −15.3 dBFS over 10s; reference
   DeskClock alarm −10.0 dBFS. No descriptor/binary/APK/global change; config-only, survives
   lane restart. repo: linux-launcher/audio_config.py + test-audio-config.py (10 cases),
   runner stage media-volume-seed 05:35:42Z. NOTE: internal DMIC registers no acoustic energy
   (mis-wired capture path on this Deck); "audible" = measured full-scale digital level at
   sink + open codec path; acoustic truth needs user ears tomorrow.
6. **Lane lifecycle guarded.** deck jcs2-ops scripts (capture chain, lane guard + corrective
   policy, render checker, restart, launch, selftest, console/evprobe/jsprobe helpers) +
   runner.py: seed_media_volume() stage; 5038 orphan-server gating; bridge-loss fatal-on-restart
   fallback; hub service deck-runtime-jcs2-live persist=true restart=always, verified relaunches.
7. **Offline evidence.** 309 + 86 unit cases green; full offline matrix 462 across 16 suites;
   mutants fail (drop range check / skip write / skip parse); vision measures recorded.

## Lane
Service deck-runtime-jcs2-live (renamed from deck-runtime-legacy32-ab, identical spec/env;
restart=on-failure→always, persist). pids run-20260918T053523Z: runner 119402, qemu 119417,
controller 120209, bridge 120229, game 3223, capture chain; game in-race at 60 fps during
AudioPath; currently at MAIN MENU after HudPath's BACK taps hunting the HUD-LAYOUT entry.
Guest adbd unrooted as required. Runtime SDK 37.1.11 untouched; legacy32 override via
JCS2_SDK only.

## Known non-blocking gaps (documented)
- **HUD fix persistence not yet durable**: options.bin still holds drifted layout; restore lives
  in the live process (array + Hud::Button centres). Fresh app start reloads the drift.
  Candidates: the game's own UiFormHudLayout DEFAULT/ACCEPT (entry point unfound in observed
  menus), triggering options.bin's writer, or wiring `hud_layout.py --apply` as a runner stage
  after the HUD build. One mis-write detected+reverted (btn11 [1004,20]→[30,260]) for audit.
- **Play-from-detail no-transition quirk** (taps land, BACK-key known workaround), non-blocking.
- **In-game Gamepad toggle ON** unverified this session (needs user touch).
- **Analog truth**: no acoustic capture possible on this Deck (internal DMIC no 440 Hz energy,
  capture path mis-wired) — verdict rests on full-scale digital + codec path; speaker silence
  to the ear would be analog/hardware.
- Menu/pause screens digitally silent by game behaviour (not a fault; 105 audio entries intact).
- Touch mapping in the aligned (rotation 1) state unverified directly (user did reach a race,
  so touch functions).
- Bedtime rule: no user-described screens; automatic same-moment capture on every report.

## Deliverable stack (repo main + Deck checkout, tests green)
- linux-launcher/runner.py (park + align + guard + console_send/host_rotation_steps)
- linux-launcher/joystick_bridge.py (L2 → guest LT brake axis; R2 digital R1 accelerator)
- linux-launcher/hud_layout.py + test-hud-layout.py (array + live Button restore; idempotent)
- linux-launcher/audio_config.py + test-audio-config.py (media volume seed stage)
- deck jcs2-ops/* (capture chain, lane guard, render check, restart, selftest, helpers)
- Evidence: /tmp/jcs2-ev-* (Deck), /tmp/jcs2-evidence/, /tmp/hudwork/ (workstation);
  full tables /tmp/jcs2-audio-summary.txt, /tmp/jcs2-audio-summary.py.

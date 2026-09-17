# JCS2 grid revision — 2026-09-15 (STAGED, not applied)

Scope: personal Steam grid artwork ONLY for non-Steam shortcut
`Jet Car Stunts 2`, appid `4192595540`. No game, runtime, ADB, emulator,
shortcut rename, appid, Exe/StartDir/launch-option, or icon-field changes.
Portrait cover is PRESERVED by design.

## User request (from 4 Steam Deck photos, 2026-09-15)

1. Wide cover `4192595540.png` (920x430): rebuild as car + road scene like the
   current portrait cover, title TOP-LEFT, cohesive with existing art.
   Current wide is logo-only on dark canvas (giant STUNTS 2 fills the Recent
   Games capsule) — to be replaced.
2. Hero logo `4192595540_logo.png` (1280x720 RGBA): title LARGER, comparable
   to the Fallout 4 hero example (large bottom-left logo above Play).
   Current hero-details logo renders small.
3. Preserve portrait `4192595540p.png` (600x900, yellow jet car + bottom
   title). No change.
4. Hero background `4192595540_hero.png` (1920x620, textless track/sky):
   keep. No raster change planned.

## Inspection findings (read-only, no edits)

- `4192595540.png` (920x430, RGBA, sha256 f0d99fe8…6283b8a9): logo-only on
  dark navy canvas. No car/scene. Confirmed cause of "giant logo" in
  `ef68c4e8… .png` (Recent Games).
- `4192595540_logo.png` (1280x720 RGBA, sha256 7cb3dbaa…be6d445): opaque
  alpha bbox (90,199)-(1253,588); opaque 1163x389 px = 90.9% wide but only
  54.0% tall. Top transparent pad 199 px, bottom pad 132 px, left 90 px,
  right 27 px. Steam composites this full canvas over the hero and scales to
  fit, so the ~46% vertical padding shrinks the visible letterforms — this
  confirms the transparency-padding hypothesis for the small hero logo in
  `8a321256… .png`. Fix = tight-crop re-export of the SAME letterforms
  (raster edit), not a metadata tweak.
- `4192595540_hero.png` (1920x620 RGB, sha256 052881f7…67a952c1): textless
  track/sky, correct slot size. Keep as-is.
- `4192595540p.png` (600x900, sha256 ec637ea0…0dc90): yellow jet car +
  bottom title. Keep as-is (= `revised/library-cover-600x900.png`).
- Reference alternative `library/library-header-alternative.png` (920x430,
  sha256 3d4cd0b6…6cdafee2b4): full track scene with car mid-air, but title
  is small TOP-RIGHT. Closest existing wide scene; the new wide keeps this
  scene language but moves the title TOP-LEFT per request.

## JSON logo position/scale — checked, NO change made

Searched actual formats before touching anything:
- Steam `grid/` has no JSON slot for logo position/scale (only the 4 PNGs).
- `userdata/.../config/librarycache/4192595540.json` is an achievements stub
  (`[["achievements",…]]`), not layout metadata. Left untouched.
- Project `packaging/distribution.json` and
  `analysis/distribution-20260912*/steam-integration.json` artwork blocks
  carry only path/size/transparent/status — no position/scale fields exist in
  the proven format.
- Conclusion: Steam decides hero-logo placement/scale from the PNG canvas
  itself, so no JSON was invented or edited. The enlargement comes from the
  tight-crop logo re-export (see imagegen-prompts.md).

## Status: STAGED ONLY — nothing applied to Steam

- Steam was NOT launched/stopped/restarted. No Steam process found at check
  time (pgrep for steam: only the check command itself; grid mtimes still
  2026-09-14 16:13).
- Backup of current grid: `backups/steam-grid-20260915T050939Z-1486631847/`
  (4 PNGs, hashes match live grid — see manifest below).
- New raster art NOT yet generated: no imagegen tool is exposed in this
  session (only bash/read/edit/write/glob/grep/web tools). No Python image
  editing was used per instructions; no files were written into Steam
  `grid/`. Live grid files are byte-identical before/after this task.
- Previews: none generated (blocked). `imagegen-prompts.md` in this dir is
  the exact ready-to-run spec + source paths + output slots for root.
- Apply authorization on file ("apply art when Steam closed") is recorded
  but NOT acted on, because there is no finished art to apply yet.

## Backup manifest (byte-identical to live grid)

- `backups/steam-grid-20260915T050939Z-1486631847/4192595540.png`
  f0d99fe8810fc1087bc9836b89320fb7951bcab2efd9855e4a7876283b8a9d0e
- `backups/steam-grid-20260915T050939Z-1486631847/4192595540_logo.png`
  7cb3dbaaae66479f9e886e4518e6eb980f9be8eeb84766f67cc10ba65be6d445
- `backups/steam-grid-20260915T050939Z-1486631847/4192595540_hero.png`
  052881f725d0d5280f134bdcf08a569a41bf3c7c7bbd2c60abd67a952c184331
- `backups/steam-grid-20260915T050939Z-1486631847/4192595540p.png`
  ec637ea0383e40181b4f52187e6ea7840f3d459b96c1eeda739c558c28b0dc90

## Next step for root

Run the two prompts in `imagegen-prompts.md`, verify sizes/modes/hashes,
drop outputs into this dir as `wide-920x430.png` + `logo-1280x720-tight.png`
+ `preview-*.jpg`, then — with Steam fully closed — copy them to
`.../config/grid/4192595540.png` and `.../config/grid/4192595540_logo.png`
(0644, PNG signature check). Leave `4192595540p.png` and
`4192595540_hero.png` untouched. No generated UI/screenshot is to be passed
off as real Steam UI.

# Ready-to-run imagegen specs for root — JCS2 grid 2026-09-15

Tool required: raster image-edit/generation tool (referred to in prior notes
as built-in `image_gen`). NOT available in this session — no raster files
were created here and no Python image editing was used. Run the two prompts
below with the image tool, then stage outputs in THIS dir before touching
Steam `grid/`.

Global rules for both: preserve appid `4192595540`, shortcut name
`Jet Car Stunts 2`, and portrait `4192595540p.png`. Output genuine PNGs at
exact pixel sizes. No checkerboard-baked "transparency". No added words. No
generated Steam-UI screenshots passed off as real.

## Source paths (absolute, verified present)

- Portrait reference (style/scene to match, DO NOT overwrite):
  `/home/deck/Projects/JCS2/artwork/steam-proposal/revised/library-cover-600x900.png`
  600x900 RGB, sha256 ec637ea0383e40181b4f52187e6ea7840f3d459b96c1eeda739c558c28b0dc90
  (live grid copy: `/home/deck/.local/share/Steam/userdata/1486631847/config/grid/4192595540p.png`, same hash)
- Logo source (letterforms to preserve):
  `/home/deck/Projects/JCS2/artwork/steam-proposal/revised/library-logo.png`
  1280x720 RGBA, sha256 7cb3dbaa…be6d445, opaque bbox (90,199)-(1253,588)
  (live grid copy: `/home/deck/.local/share/Steam/userdata/1486631847/config/grid/4192595540_logo.png`, same hash)
- Hero background (keep as-is, reference only):
  `/home/deck/.local/share/Steam/userdata/1486631847/config/grid/4192595540_hero.png`
  1920x620 RGB, sha256 052881f7…67a952c1
- Wide scene language reference (composition cue only):
  `/home/deck/Projects/JCS2/artwork/steam-proposal/library/library-header-alternative.png`
  920x430, sha256 3d4cd0b69b4de908be804430f9b2a443e9934508b7a4bb3e08e75e6cdafee2b4
- User photos (intent only, NOT pixel sources — they are Steam Deck screen
  photos, 960x1280):
  `/tmp/paseo-attachments-jLIliF/ef68c4e83bc46c66bc644b5fa4867c3335b213f7061b1e4f8fbaf9c72699debd.png` (JCS2 wide, giant logo — the problem)
  `/tmp/paseo-attachments-jLIliF/f319354a44de871ddfc230266cdbba4546364d6ced07061210360a000073179a.png` (Fallout wide ref + JCS2 portrait w/ car — the target feel)
  `/tmp/paseo-attachments-jLIliF/8a321256c719d2c03948d9794d1d6902ff8850300a317c6d76cb38dd2dde8600.png` (JCS2 hero, small logo — the problem)
  `/tmp/paseo-attachments-jLIliF/6fe5225dbf316fc2d58d4578f9ba3e386d7ad918c47a964988ad905ff0e23942.png` (Fallout hero, large logo — the target scale)
  Fifth file `0f1097ae… .png` is an in-game Level Editor note, not artwork.

## Prompt 1 — new wide cover (replaces 4192595540.png)

Output: `<this-dir>/wide-920x430.png`, EXACTLY 920x430 RGB PNG.
Edit intent (raster edit, cohesive with existing art): take the yellow /
turquoise jet-car + gray speed-streak road look of the portrait reference
(`library-cover-600x900.png`) and recompose it natively for the 920x430
wide capsule (no stretch, no crop amputation of the car). Show car + road
scene filling the frame; place the existing `JET CAR / STUNTS 2` title
(red JET/STUNTS, white CAR, yellow 2, original angular lettering) TOP-LEFT
with side margins, sized to stay legible at Recent Games capsule scale.
Dark-sky/road palette cohesive with the hero (`library-hero.png`) and the
alternative header. No other text, no HUD, no watermark.

## Prompt 2 — tight-crop hero logo (replaces 4192595540_logo.png)

Output: `<this-dir>/logo-1280x720-tight.png`, 1280x720 genuine transparent
RGBA PNG (alpha 0–255, no baked checkerboard).
Edit intent: take the EXACT letterforms of `library-logo.png` (red JET,
white CAR, red STUNTS, yellow 2, gray/red speed trails, drop shadows) with
NO re-lettering, and re-layout them to maximize visible size when Steam
overlays the logo on the 1920x620 hero: tight-crop transparent padding to a
~12–24 px safety margin around opaque pixels, keep JET CAR above STUNTS 2
with a shared horizontal midpoint, center the group at (640,360). Target the
visible letterform height to read comparably large to the Fallout 4 hero
example at details-page scale. Verify with alpha bbox: opaque height should
fill ~85–92% of the 720 px canvas height (vs current 54%), alpha range
0–255.

## Verify + stage (root, Steam CLOSED only)

1. `file` + dimensions: `wide-920x430.png` = 920x430; `logo-…-tight.png` =
   1280x720 RGBA with real transparency (`PIL alpha getbbox()` height
   ≈ 610–660 px, margins ≈ 12–24 px).
2. Save JPG previews beside them (`preview-wide.jpg`, `preview-logo-on-hero.jpg`).
   Previews are mockups only — never present them as real Steam UI.
3. Record sha256 of both + previews in this dir's EDIT-NOTES.
4. With Steam fully exited (no `steam` process, grid mtimes old), copy:
   `wide-920x430.png` → `.../config/grid/4192595540.png` and
   `logo-1280x720-tight.png` → `.../config/grid/4192595540_logo.png`
   (0644). Leave `4192595540p.png` and `4192595540_hero.png` untouched.
5. Re-verify dest hashes + PNG signatures; report applied-vs-staged and do
   visual verification on next normal Steam launch (do not launch Steam to
   verify from this task).

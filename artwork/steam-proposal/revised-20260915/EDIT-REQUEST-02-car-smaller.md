# EDIT REQUEST 02 — wide v2: smaller car, lower-right (for root render backend)

## Source (exact, do not substitute)

`/home/deck/Projects/JCS2/artwork/steam-proposal/revised-20260915/wide-generated-master-1833x858.png`
1833x858 RGB, sha256 d4bf53a5c6ec1bf09b8af422440f50e77a5cd1faf6fa58e68172675a0f6a56e4.
This is v1, currently live in Steam grid. Edit THIS file, not the portrait.

## Exact imagegen edit prompt (copy-paste into builtin image tool)

> Raster-edit the attached JCS2 wide cover (1833x858): keep the TOP-LEFT
> title EXACTLY where it is — same words "JET CAR / STUNTS 2", same angular
> lettering, same colors (red JET, white CAR, red STUNTS, yellow 2), same
> size, same style, same drop shadow. Keep the gray radial speed-streak road
> background, palette, and lighting identical. ONLY change the yellow jet
> car: scale it down by ~12-15% and shift it DOWN and RIGHT toward the
> bottom-right corner, keeping full-car framing with safe edge clearance
> (no cropping of tires, jets, or flames at any edge). Goal: clearly more
> empty road padding around the top-left title and a wider gap between the
> "STUNTS 2" lettering and the car nose/canopy. Do not move, resize, restyle,
> or re-spell the title. Do not change road style or colors. Output same
> 1833x858 RGB PNG (or same-aspect higher res), no checkerboard, no text
> other than the existing title, no watermark.

## Output spec for root return

- Return file path + sha256 + dimensions/mode.
- Same aspect as 920:430 (v1 is 1833x858, ratio 2.1364 vs target 2.1395 —
  keep equivalent; do NOT resample to exactly 920 wide in the tool).
- RGB PNG fine for wide (no alpha needed). No baked transparency pattern.

## QA I will run on return (I own QA/install, no Python raster editing)

1. Visual Read: title pixel-identical location/style/words; car fully
   framed with edge clearance; gap logo↔car visibly larger; road/palette
   unchanged.
2. Read-only PIL: size, mode, aspect within ~0.5% of 920/430; PNG signature.
3. Copy to this dir as `wide-v2-generated-master-<WxH>.png` (cp -p), record
   sha256.
4. Install ONLY when Steam closed (`pgrep -x steam` exit 1, grid mtimes
   old): cp -p to
   `/home/deck/.local/share/Steam/userdata/1486631847/config/grid/4192595540.png`,
   chmod 644, re-verify dest hash + PNG signature. Preserve portrait
   `4192595540p.png`, hero `4192595540_hero.png`, name, appid. No Steam
   launch/stop. User authorized updated-wide apply when Steam closed.

## Previous logo alpha — REJECTED, NOT applied (re-verified just now)

- `logo-generated-REJECTED-baked-checker-2140x735.png`: 2140x735, mode RGB,
  bands ('R','G','B'), has-alpha False. Baked gray/white checkerboard.
  Live `4192595540_logo.png` still old `7cb3dbaa…be6d445`. Logo NOT applied;
  needs genuine-RGBA regen (separate request).

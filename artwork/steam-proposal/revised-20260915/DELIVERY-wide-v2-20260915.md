# Final wide delivery — v2 APPLIED 2026-09-15

## Source render (root backend)

`/home/deck/.codex/generated_images/01a095c6-60cb-7081-91ef-c6432aac6363/exec-fc85ecd6-250b-41ca-9343-451ab1b4bdef.png`
1832x858 RGB, sha256 2203cfe3c4652e05e4523d6b09087cd1a34dea1f730f35e8257452448b9cce0b.

## QA vs v1 + steering — PASS

- Title: same words/style/colors/size, top-left, untouched. PASS.
- Road: same gray radial-streak style/palette. PASS.
- Car: ~12–15% smaller, shifted down/right, full framing, edge clearance
  kept, gap between STUNTS 2 and car clearly larger. PASS.
- Aspect 2.1352 vs 920/430=2.1395 (~0.2% off) — accepted higher-res same
  aspect, no resample. No checkerboard/extra text/watermark. PASS.

## Staged + applied (Steam closed, authorized)

- Staged: `wide-v2-generated-master-1832x858.png` (same hash as source).
- Steam state: `pgrep -x steam` exit 1, never launched/stopped here.
- Applied: staged v2 → `.../config/grid/4192595540.png` (0644, PNG sig
  89504e470d0a1a0a). Backup of pre-v2 grid already in
  `backups/steam-grid-20260915T050939Z-1486631847/` (v1 wide d4bf53a5…,
  old files intact).

## Live grid hashes post-apply

- `4192595540.png` 2203cfe3…b9cce0b (NEW v2 wide, 1832x858 RGB)
- `4192595540_logo.png` 7cb3dbaa…be6d445 (old padded logo — hero still small)
- `4192595540_hero.png` 052881f7…67a952c1 (unchanged)
- `4192595540p.png` ec637ea0…0dc90 (portrait unchanged)

## Rejected logo kept out

`logo-generated-REJECTED-baked-checker-2140x735.png` (2140x735 RGB, no
alpha) NOT applied. Live logo untouched.

## Remaining logo render prompt (for root, genuine RGBA only)

Source: `/home/deck/Projects/JCS2/artwork/steam-proposal/revised/library-logo.png`
(1280x720 RGBA, opaque bbox (90,199)-(1253,588)).
Prompt: "Re-layout these EXACT JCS2 letterforms (red JET, white CAR, red
STUNTS, yellow 2, trails/shadows, no re-lettering) on a tight ~3:1
transparent canvas with ~12–24px safety margins; JET CAR above STUNTS 2,
shared midpoint, centered. Output genuine transparent RGBA PNG, alpha
0–255, NO baked checkerboard."
Accept gates: PIL mode RGBA + alpha bbox fills most of canvas + distinct
alpha levels >2 + visual with NO checker pattern. Then apply to
`.../grid/4192595540_logo.png` with Steam closed.

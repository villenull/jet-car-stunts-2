# Logo v2 — REJECTED (baked RGB, not applied) 2026-09-15

Render: `/home/deck/.codex/generated_images/01a095c6-60cb-7081-91ef-c6432aac6363/exec-ff77b15b-95be-4031-8fff-aebff59de178.png`
2172x724, PNG RGB, sha256 81ce665ce6e9ff5bdf1105dcf423dd8ac75dbc717634b03c2a402dc370778d2a.
Staged as `logo-generated-REJECTED-v2-baked-2172x724.png` (same hash, cp -p).

Actual alpha (read-only PIL, no assumption from display): mode RGB, bands
('R','G','B'), converted-alpha extrema (255,255), bbox full-canvas
(0,0,2172,724), distinct alpha levels 1. Corners/center all 255. Visual
confirms gray/white checkerboard baked behind letterforms. NOT genuine
transparency. NOT applied. Live `4192595540_logo.png` untouched
(7cb3dbaa…be6d445). No further raster generation loops per instruction.

Steam logo position/size config investigation (read-only, no invented config):
- `grid/` mechanism is 4 PNG filenames only; no JSON/XML sidecar exists.
- `librarycache/4192595540.json` = achievements stub only.
- `localconfig.vdf` for 4192595540 holds BadgeData + UI selection state only;
  no logo layout keys. `config/config.vdf` has no custom-art keys.
- `steamui_librarycache.txt`: "Unsupported app type 1073741824 for appid
  4192595540, skipping download" — non-Steam art is local-files-only, no
  server layout to tweak.
- steamui `logoPosition`-like hits are localization strings only, not a
  user-facing layout API.
Exact blocker: no supported user-facing mechanism exists to enlarge/reposition
the hero logo without replacing the PNG; the only fix is a genuine tight-crop
RGBA file, and raster loops are now closed. Wide v2 (`2203cfe3…b9cce0b`,
1832x858) remains the applied final. Portrait/hero/name/appid preserved.
Steam never launched/stopped.

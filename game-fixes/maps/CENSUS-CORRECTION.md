# CENSUS CORRECTION — Levels_IsPurchased callers (append-only correction)

SUPERSEDED IN PART by tilt round-10 audit ruling: the round-7 section
below ("one found direct caller (DLC-zip region)") is itself FALSE
(0x1605c2 is zip_close/minizip context; 0xa781c binds fdopen).
Authoritative census: EXACTLY 3 callers via true PLT 0xa592c (GOT
0x21e2fc): 0x13d2c8, 0x14bf4c, 0x158ece. See
`staging/PROVENANCE-ADDENDUM-census.json`. History preserved below;
do not use superseded sections for conclusions.

Date: 2026-09-13. Supersedes the "exactly 2 callers (row-ctor +
UpdateLevelPanelPopulation)" claim in patch_maps_ownership.py,
provenance files, and handoffs. Frozen v2 APKs/hashes/provenance are
NOT modified by this correction.

## What was wrong

Early scans matched Thumb BL targets against a stride-assumed PLT map
(base+20+idx*12 over .rel.plt order) and mis-decoded BLX-immediate
targets (±2 via Align(PC,4) handling). The sites 0x13d2c8/0x14bf4c/
0x158ece call EncriptedString Decrypt (string decryption), NOT
Levels_IsPurchased. The "2 caller" claim is WITHDRAWN.

## Corrected method

PLT entries decoded structurally per stub (add ip,pc / add ip,ip /
ldr pc,[ip]) → GOT → relocation → symbol (no stride assumption), and
BL/BLX targets calibrated against llvm-verified branches
(`bl 0x105868` ×2 exact). Repro: rerun the census scan in
compose history (documented), or `game-fixes/maps/census.py` (pending).

## Corrected facts (original v1.0.23 lib, bc7fdf9d…)

- `Levels_IsPurchased` (PLT 0xa781c): 1 found direct caller @0x1605c2
  (DLC-zip region). Level-select purchase gating does NOT route through
  it (nor through `Store_IsItemPurchased`: 1 caller, AddChallenges).
- Row-ctor / UpdateLevelPanelPopulation / shop contain NO predicate
  calls (exhaustive scans); their ownership reads use other means
  (unresolved — GetItem/struct-direct flows under analysis).
- `OnStoreShouldPurchaseRestore` (local-restore oracle) returns false;
  `Store_RestoreExistingLocalPurchases` has zero callers. Dead.
- Consequence: the 12-byte patch effect is UNPROVEN and possibly inert.
  PLAY failure is as consistent with the v1 arsc-recompression defect
  (fixed in v2 faithful builds) as with the predicate. Decided ONLY by
  the pristine-DB86 vs maps A/B on one fresh guest (both staged).
- Per-SKU hook design pivots to the TRUE gate once identified; the
  ELF-growth vehicle (LOAD0 pad, PIC, BL range) is proven viable
  independent of gate choice.

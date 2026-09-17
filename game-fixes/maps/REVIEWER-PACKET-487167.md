# Reviewer packet 487167 — decoder, call targets, ownership path (read-only)

For independent confirmation of the corrected decoder and call targets.
No patch construction, no builds, no frozen mutation. All inputs are
original bytes + repo tools.

## 1. Originals (exact)

- `backups/usb-20260910T005449Z/`: base `41043f68…6bfa9`, armeabi_v7a
  `d1ceccba…c256`, en `9d72176b…c40f`, es `1000baf0…2c2c`, xhdpi
  `59da6a86…5f49` (see SHA256SUMS).
- `lib/armeabi-v7a/libtrueaxis.so` in armeabi split: 2227488 bytes,
  SHA-256 `bc7fdf9d…408a34d`, BuildID `39f1603f…43234` (first 0x200).
- Copy used here: `/tmp/opencode/libtrueaxis-orig.so` (re-extractable
  with the composer; never the backup itself).

## 2. Key symbols (readelf -WsW, Thumb bit set on Odd addrs)

| Symbol | File addr | Size |
|---|---|---|
| `Store_IsItemPurchased(char const*)` | 0x107268 | 32 |
| `Store_SetTCPurchasedItem(char const*)` | 0x107244 | — |
| `Levels_IsPurchased(int)` | 0x133014 | 16 |
| `Levels_IsLocked(int)` | 0x133024 | 56 |
| `Levels_GetLevelPack(int)` | 0x132f48 | 204 |
| `Levels_DoProgression(unsigned,int)` | 0x132d38 | 176 |
| `IAPUnlocks(Level::Pack)` | 0x10fb4c | 204 |
| `Stats::Level::Difficulty::Unlock()` | 0x136813 | 12 |
| `AddStoreItems()` | 0x10fa84 | 200 |
| `OnStoreShouldPurchaseRestore(char const*)` | 0x10fa7e | 6 (`movs r0,#0; bx lr`) |
| `RemoveIAPCrack()` | 0x10fa7c | 2 (`bx lr`) |
| `g_bUnLockAll` (.bss OBJECT) | 0x43c299 | 1 |

## 3. llvm-verified branches (ground truth for decoder calibration)

Tool: `tools/llvm-mingw-20260908/llvm-mingw-20260908-msvcrt-ubuntu-22.04-x86_64/bin/armv7-w64-mingw32-objdump -d --arch-name=thumb`:

- `0x10726c: f7fe fafc → bl 0x105868` (llvm prints target + `#-0x1a08`)
- `0x107248: f7fe fb0e → bl 0x105868` (same target)
- Raw bytes confirm little-endian halves (h1=0xf7fe, h2=0xfafc).

Any decoder must reproduce both. Correct rule (verified): hw1∈F000–F7FF
(BL prefix); hw2 bit12=1 → BL, target=(pc+4+off); bit12=0 with top
1110 → BLX, target=Align4(pc+4)+off; J1=hw2[13], J2=hw2[11],
I1=~(J1^S), I2=~(J2^S), off=S:I1:I2:imm10:imm11:0 sign-extended.
Prior failure mode: testing `(h2&0xE800)==0xE800` skips true BLs
(0xFAFC has bits[12:11]=11b) — every pre-correction caller list is void.

## 4. PLT entries (structural decode, no stride assumption)

Per entry (12 B ARM: `add ip,pc,#X; add ip,ip,#Y; ldr pc,[ip,#-Z]`):
GOT=(base+8+ROR(X)+ROR(Y))−Z, resolved via `readelf -rW` JUMP_SLOT.
Authoritative ownership entries:

| Entry | Symbol |
|---|---|
| 0xa781c | `Levels_IsPurchased` |
| 0xa7138 | `Store_IsItemPurchased` |
| 0xa37b4 | `IsItemPurchased(char*)` |
| 0xa8a70 | `IAPUnlocks` |
| 0xa7738 | `Difficulty::Unlock` |
| 0xa35bc | `AreAdsDisabled` |
| 0xa7b34 | `IsLevelEditorUnlocked` |
| 0xa6034 | `AddStoreItems` |
| 0xa5e24 | `Levels_GetLevelPack` |
| 0xa592c | EncriptedString Decrypt106 (NOT a purchase gate) |

## 5. Caller lists awaiting your confirmation (corrected decoder)

- `Levels_IsPurchased`: 1 found caller @0x1605c2 (DLC-zip region).
- `Store_IsItemPurchased`: 1 caller @0x158bb6 (AddChallenges).
- Row-ctor / UpdateLevelPanelPopulation / shop: NO predicate calls
  (their 0x13d2c8/0x14bf4c/0x158ece sites call Decrypt106).
- `RestoreExistingLocalPurchases`: 0 callers; oracle returns false.
- Full per-entry caller tables were produced with the §3 decoder;
  challenge any entry and I will re-derive it byte-for-byte.

## 6. What I ask you to rule

(a) Decoder + entries + caller lists above (confirm or correct with
byte evidence). (b) The authoritative ownership path for level-select
paid rows (my predicate route is disproven). (c) The seed/hook contract:
allowed patch surfaces (LOAD0 pad? call-site redirect? data table?) and
verification bar before any construction resumes. I build nothing
further until you rule.

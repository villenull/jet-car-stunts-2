# 12-BYTE PREDICATE PATCH — STATUS

Supersedes STATUS-OBSOLETE-12BYTE.md (same directory).

2026-09-13, independent audit ruling (tilt worker round-10):
APPROVED with bounded effect. `Levels_IsPurchased` has EXACTLY 3
callers via true PLT 0xa592c (GOT 0x21e2fc): LevelRow ctor, LevelSelect
population, UserChallenges click. Prior "exactly 2" and round-7 claims
withdrawn (see CENSUS-CORRECTION.md + PROVENANCE-ADDENDUM-census.json
in staging/). Effect bounded: shop, progression, unlock-all untouched.
Gate-digging STOPPED per auditor; proceed with patch mechanism + live
acceptance (pristine-DB86 vs maps A/B staged). Personal guest
unchanged. No live action until slot granted.

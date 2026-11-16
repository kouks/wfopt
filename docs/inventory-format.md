# AlecaFrame `lastData.dat` format (verified 2026-11-16)

Checked against the player's real file (2.1 MB, AlecaFrame build in `%LOCALAPPDATA%\AlecaFrame\version.txt`).

## Container

- AES-128-CBC, PKCS7, static key `LEO-ALEC\tEO-ALEC` and IV `12FGB36-LE3-q=9\0`, as in PLAN §4.1. There's no IV prefix.
- The plaintext is UTF-8 JSON. **The top-level object is the raw game inventory.** There is no `InventoryJson` wrapper in this build, but the code unwraps one if a future build adds it.

## Arrays used

| Array         | Entry shape                                                              | Contents                                                                                      |
| ------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| `MiscItems`   | `{ItemType, ItemCount}`                                                  | Resources, plus prime **weapon parts** (`/Lotus/Types/Recipes/Weapons/WeaponParts/VastoPrimeBarrel`) |
| `Recipes`     | `{ItemType, ItemCount}`                                                  | Blueprints: prime main BPs (`.../Weapons/VastoPrimeBlueprint`), warframe component BPs (`.../WarframeRecipes/RhinoPrimeChassisBlueprint`), weapon-part BPs |
| `RawUpgrades` | `{ItemType, ItemCount, LastAdded:{$oid}}`                                | Unranked, unequipped mod and arcane stacks (rank 0)                                           |
| `Upgrades`    | `{ItemType, ItemId:{$oid}, UpgradeFingerprint, LastAdded?}`              | One entry per ranked or equipped instance. `UpgradeFingerprint` is always a JSON **string**   |

- `UpgradeFingerprint` for normal mods and arcanes is `{"lvl":N}`. Rank-0 instances can be `{}` (no `lvl`). Rivens carry `compat`, `buffs`, `curses`, etc.
- Rivens are `Upgrades` entries whose `ItemType` contains `/Randomized/` (98 in the real file). They are skipped.
- Arcanes live under `/Lotus/Upgrades/CosmeticEnhancers/...` in both `RawUpgrades` and `Upgrades`.

## Other useful top-level fields

- `PlayerLevel`: Mastery Rank (36). Used as the default `daily_trades`.
- `TradesRemaining`: trades left today (36 at the time of checking). Shown in the daily plan.

## Equipped detection

Upgrade `ItemId`s are referenced from equipment `Configs[].Upgrades[]` (and similar lists) in `Suits`, `LongGuns`, `Pistols`, `Melee`, `Sentinels`, `OperatorAmps`, etc. The tool walks every array except the two upgrade arrays and marks any `Upgrades` instance whose id appears there. 743 of 1456 non-riven instances were marked. This only annotates rows.

## Join to warframe.market

`ItemType == gameRef` holds for prime parts, blueprints, mods and arcanes.

| Array       | Entries | Mapped | Notes on unmapped                                                                                                                        |
| ----------- | ------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| MiscItems   | 1160    | 259    | Resources, Void projections (relics), `PrimeBucks` (ducats), captura tiles                                                               |
| Recipes     | 603     | 212    | Non-tradable blueprints, plus **Galariak Prime / Sagek Prime** (not on WFM yet)                                                          |
| RawUpgrades | 841     | 801    | Railjack/K-drive stickers, unrevealed riven stacks, Dual Source mods not on WFM                                                          |
| Upgrades    | 1554    | 1530   | Umbral and Sacrificial mods, Primed Shred / Primed Fury / Primed Vigor and others not listed on WFM                                       |

Category totals after the join: 320 prime items (670 units), 170 arcanes (7860 copies), 1254 mods (63223 copies), 151 skipped (lenses, ayatan stars, non-prime weapon parts, fish, 1 relic).

## Catalogue surprises

- 25 ordinary mods (Vitality, Streamline, Hellfire…) have `subtypes: ["regular","atragraph"]`. They stay in scope and are queried with `subtype=regular`.
- Molecular Fission and Gilded Truth are tagged `prime` but are mods. The mod tag wins.
- Kavasa Prime Buckle/Band are tagged only `prime`. They are treated as prime parts.
- `prisma_shade` and `prisma_shade_set` share a `gameRef`. The non-set item wins.

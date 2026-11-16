# Implementation steps

The plan in [PLAN.md](PLAN.md), split into steps that each end in a commit.

| #   | Step                     | Plan refs         | Done when                                                                                   |
| --- | ------------------------ | ----------------- | ------------------------------------------------------------------------------------------- |
| 0   | Scaffold                 | §7, §8, §9        | `pyproject.toml`, config loading, `.gitignore`, Typer CLI skeleton, `pytest` runs           |
| 1   | M0: Inventory            | §4.1, §5.1.1, §10 | `wfopt inventory` decrypts `lastData.dat` and prints holdings; `docs/inventory-format.md`   |
| 2   | M1: Catalogue + mapping  | §4.2, §5.1.2–3    | Catalogue + set compositions cached; `wfopt catalog` prints mapping and category stats      |
| 3   | M2: Market client/prices | §3, §4.2, §5.3    | Rate-limited, cached client; `PriceSource` interface; stats fallback; `respx` tests         |
| 4   | M3: Engine               | §5.2–§5.5         | Pure keep / sets / arcanes / mods / plan logic with unit tests listed in §10                |
| 5   | M4: Report               | §6                | `wfopt report` writes CSV + HTML, prints top 20 + daily plan; e2e test                      |
| 6   | M5: Listing sync         | §4.3, §4.4, §12   | `wfopt listings diff` / `apply` (dry run unless `--yes`), tool-managed orders, sync log     |

## Deviations from the plan (found while implementing)

- `lastData.dat` holds the raw inventory at the top level. There is no `InventoryJson` wrapper (the code still handles one).
- 25 ordinary mods (Vitality, Streamline, Hellfire…) have `subtypes: ["regular", "atragraph"]`. Excluding everything with `subtypes` would drop them, so mods whose subtypes include `regular` stay in scope and are priced with `subtype=regular`. Other subtyped items are still excluded.
- The inventory exposes `PlayerLevel` (Mastery Rank) and `TradesRemaining`. `daily_trades = 0` in config means "use MR from the inventory"; the daily plan also shows trades remaining today.
- Pricing is two-phase to keep the first run short: v1 statistics for every surplus item, then `/top` order books only for items whose estimated value reaches `pricing.orderbook_min_plat`.

# Warframe Trade Optimizer: Implementation Plan

> Hand-off plan for Claude Code. Research done 2026-11-16. Items marked **[verified]** were checked against live endpoints or source code. Items marked **[assumption]** must be confirmed early (see §13).

## 1. Goal

A local Python tool for Windows that:

1. Reads the player's Warframe inventory from AlecaFrame's local cache file (`lastData.dat`).
2. Pulls prices, volume and live orders from the warframe.market (WFM) API.
3. Works out what to sell, how (sets vs parts, fusing arcanes vs selling unranked, maxing mods, or turning parts into ducats), and at what price.
4. Outputs a ranked, explained report.
5. **Stretch goal:** syncs the player's WFM sell listings to the recommendations, after a dry run and explicit confirmation.

## 2. User decisions (fixed requirements)

| Topic                     | Decision                                                                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Platform                  | Windows PC, AlecaFrame installed, Python is fine                                                                                                 |
| In scope                  | Prime parts, prime blueprints, prime sets, **arcanes**, **mods** (e.g. archgun 60/60s, Profit-Taker / Eidolon physical-damage mods worth 10–20p) |
| Out of scope              | Relics, Ayatan sculptures, rivens, lich/sister weapons                                                                                           |
| Keep rule: prime items    | The player owns every prime, so **all** prime parts, blueprints and sets are surplus (keep 0)                                                    |
| Keep rule: mods & arcanes | **Always keep 1 max-rank copy.** Equipped copies count toward that copy                                                                          |
| Ducats                    | Include "turn into ducats" recommendations for prime parts                                                                                       |
| WFM auth (stretch goal)   | Email and password (Steam also available, but email/pw is simpler)                                                                               |
| Inventory source          | AlecaFrame `lastData.dat`. **The player confirmed the decryption works on their current file**                                                   |

## 3. Constraints and rules

- **Don't touch the game.** Only read the file AlecaFrame already writes. Never read game memory or call DE's `inventory.php` with session tokens. Tools like warframe-api-helper do this and their own README warns it may break the ToS.
- **WFM API rules** (official docs at https://docs.warframe.market):
  - Public limit is **3 requests/second**. Throttle to about 2.5 rps. Cloudflare answers **429** when you go over and **509** for too many concurrent connections. Use exponential backoff.
  - Send a **descriptive User-Agent**, e.g. `wf-trade-optimizer/0.1 (+https://github.com/<you>/wf-trade-optimizer)`. Never imitate a browser.
  - Respect cache headers and cache aggressively.
  - OAuth is **not yet available** to third parties. Authenticated integrations must use the old v1 sign-in flow (§4.3).
- **Treat statistics as a proxy.** WFM volume only counts orders closed on the site, and it can be manipulated. Show the numbers but don't over-trust them.
- **v1 statistics is deprecated** and will be removed once a v2 replacement exists. Put the price source behind an interface so it can be swapped. If statistics are unavailable, fall back to the live order book only.
- **No automatic repricing loops.** Syncing listings happens only when the user runs it.
- **Secrets:** credentials go in `.env` (gitignored). Cache the JWT locally, never log it, and keep `lastData.dat` and decrypted dumps out of git.

## 4. Data sources reference

### 4.1 Inventory: AlecaFrame `lastData.dat` [verified from source code]

- **Path:** `%LOCALAPPDATA%\AlecaFrame\lastData.dat` (`os.environ["LOCALAPPDATA"]`). Allow overriding with `--inventory-file`.
- **Encryption:** AES-128-CBC with PKCS7 padding and a static key and IV, taken from Sainan's open-source `alecaframe-inventory-parser` and `warframe-api-helper`:
  - key bytes: `76,69,79,45,65,76,69,67,9,69,79,45,65,76,69,67` (ASCII `LEO-ALEC\tEO-ALEC`)
  - IV bytes: `49,50,70,71,66,51,54,45,76,69,51,45,113,61,57,0` (ASCII `12FGB36-LE3-q=9\0`)
  - Decrypt the whole file with this key and IV. There's no IV prefix in the file.
- **Payload:** UTF-8 JSON. If the top-level object has an `InventoryJson` string field, `json.loads` it again. That inner object is the game's raw inventory.
- **Freshness:** AlecaFrame only refreshes the file at login or loading screens. Show the file's last-modified time in the report and warn if it's older than about 2 hours.
- **Arrays to read** [assumption: confirm against the real file in M0]:
  - `MiscItems[]` → `{ItemType, ItemCount}`: prime weapon parts (e.g. `/Lotus/Types/Recipes/Weapons/WeaponParts/PrimeBallisticaReceiver`), among other things.
  - `Recipes[]` → `{ItemType, ItemCount}`: blueprints, including prime main blueprints and warframe component blueprints (e.g. `/Lotus/Types/Recipes/WarframeRecipes/RhinoPrimeChassisBlueprint`).
  - `RawUpgrades[]` → `{ItemType, ItemCount}`: **unranked, unequipped** mod and arcane stacks (rank 0).
  - `Upgrades[]` → `{ItemType, ItemId:{$oid}, UpgradeFingerprint}`: individual mod or arcane instances that are ranked or equipped. `UpgradeFingerprint` is a JSON **string**. Parse it and read `lvl` (missing means 0). Each entry is one copy.
- **Arrays to ignore:** `Suits`, `LongGuns`, `Pistols`, `Melee`, `SpaceGuns` and other built-equipment arrays, plus `PendingRecipes` (things in the foundry). Built weapons and frames have `ItemType` values identical to WFM **set** `gameRef`s, e.g. `/Lotus/Powersuits/Rhino/RhinoPrime`. Reading them would wrongly count owned gear as sellable sets.
- **Rivens:** `Upgrades` entries whose `ItemType` contains `/Randomized/` are rivens. Skip them (they won't match the WFM item catalogue anyway).
- **Nice-to-have: equipped detection.** Loadout configs reference upgrade `ItemId`s. Only use this to annotate rows ("this copy may be equipped, unequip it before trading"). It doesn't change keep logic, because equipped copies count toward the kept copy.

### 4.2 warframe.market API [verified live 2026-11-16, v2 `apiVersion` 0.25.0]

- **Base URLs:** `https://api.warframe.market/v2`, and `https://api.warframe.market/v1` (statistics and auth only).
- **Response envelope (v2):** `{ "apiVersion": "...", "data": ..., "error": null }`.
- **Headers:** `User-Agent` (see §3), `Platform: pc`, `Language: en`, `Crossplay: true` (make all configurable).
- **Item URL for reports:** `https://warframe.market/items/{slug}`.

| Purpose                        | Endpoint                                | Notes                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Version for cache invalidation | `GET /v2/versions`                      | Re-download the catalogue only when this changes                                                                                                                                                                                                                                                                                           |
| Full catalogue                 | `GET /v2/items`                         | About 1.6 MB. Each item has `id`, `slug`, `gameRef`, `tags`, `i18n.en.name`. Optional: `maxRank` (mods/arcanes), `ducats` (prime parts/sets), `bulkTradable`, `subtypes`. **Join key: inventory `ItemType` == `gameRef`.** One item has an empty `gameRef`, so skip empty values                                                           |
| Item detail                    | `GET /v2/item/{slug}`                   | Adds `rarity`, `tradingTax`, `setRoot`, `setParts`, `quantityInSet`, `maxCharges`…                                                                                                                                                                                                                                                         |
| Set composition                | `GET /v2/item/{slug}/set`               | `data.items[]`: the set root (`setRoot: true`, no `quantityInSet`) plus parts with `quantityInSet` and `ducats`. Example: `rhino_prime_set` → blueprint + chassis/neuroptics/systems blueprints                                                                                                                                            |
| Live top orders                | `GET /v2/orders/item/{slug}/top?rank=N` | `data.sell[]` and `data.buy[]`, about 5 each. Every user returned was `online` or `ingame` (`user.status`). Order fields: `platinum`, `quantity`, `perTrade`, `rank`, `type`, `visible`, `user.ingameName`, `user.status`                                                                                                                  |
| All orders for an item         | `GET /v2/orders/item/{slug}`            | Heavier. Use only if `/top` isn't enough                                                                                                                                                                                                                                                                                                   |
| A user's public orders         | `GET /v2/orders/user/{ingameName}`      | No auth needed. Used to show the player's current listings in the read-only MVP                                                                                                                                                                                                                                                            |
| History and volume             | `GET /v1/items/{slug}/statistics`       | `payload.statistics_closed["48hours"\|"90days"][]` with `datetime, volume, min_price, max_price, open_price, closed_price, avg_price, wa_price, median, moving_avg, donch_top, donch_bot, mod_rank`. **Separate rows per `mod_rank`** (usually 0 and max), so filter by rank. `statistics_live` also exists (open orders) but isn't needed |

Example check (Nov 15, Arcane Energize): rank 0 median ≈ 7p, rank 5 median ≈ 130p. 21 × 7 = 147 > 130, so fusing isn't always better. The engine must compare both.

### 4.3 Authentication (stretch goal only)

- `POST https://api.warframe.market/v1/auth/signin` with JSON `{"email": ..., "password": ..., "auth_type": "header"}` and header `Authorization: JWT`. The token comes back in the **`Authorization` response header** as `JWT <token>`. Strip the prefix.
- Use it on v2 as `Authorization: Bearer <token>`.
- **Fallback:** if sign-in fails (captcha, flow changed), let the user paste the `JWT` cookie value from their logged-in browser (`--jwt` or `WFM_JWT` in `.env`).
- Check the token with `GET /v2/me` before any write.
- v2 `/auth/signin` is first-party only (needs Firebase AppCheck). **Don't use it.**

### 4.4 Order management (stretch goal) [verified paths, from client libraries]

- `GET /v2/orders/my` (auth): the player's orders.
- `POST /v2/order` with body `{"itemId": "<wfm item id>", "type": "sell", "platinum": 210, "quantity": 1, "visible": true, "rank": 5}`. Include `rank` for items with `maxRank`.
- `PATCH /v2/order/{id}` with any of `{"platinum", "quantity", "visible", "rank"}`.
- `DELETE /v2/order/{id}`.

## 5. Domain logic

### 5.1 Normalisation and join

1. Build `holdings`: a list of `{gameRef, rank, count, source_array}`. `RawUpgrades` entries become rank 0 with their count. Each `Upgrades` entry becomes count 1 at its `lvl`. `MiscItems` and `Recipes` have no rank.
2. Join to the catalogue on `gameRef`. Unmatched entries go to an `unmapped` debug list. Most are untradable resources, which is expected. Print counts by array.
3. Classify each matched item by its WFM tags:
   - **prime**: tag `prime`, with `set` / `component` / `blueprint`
   - **arcane**: tag `arcane_enhancement`
   - **mod**: tag `mod`, excluding riven tags
   - **excluded**: `relic`, `ayatan_sculpture`, anything with `subtypes` or `maxCharges` (Requiem mods, etc.), and everything else. Put these in a "skipped" list with a reason.

### 5.2 Keep rules

- **Prime:** keep 0.
- **Arcanes** (units model: a rank-r copy is worth `(r+1)(r+2)/2` rank-0 copies, and max rank m needs `U_max = (m+1)(m+2)/2` units; m comes from WFM `maxRank`, so it's 21 for rank 5 and 10 for rank 3):
  - If the player owns at least one copy at max rank, keep exactly one. The rest is surplus.
  - Otherwise, if total units ≥ `U_max`, **reserve `U_max` units** (prefer the highest-ranked copies) as "fuse to keep". The rest is surplus. Tell the user to rank one up in game.
  - Otherwise keep everything and report progress, e.g. "Arcane X: 14/21 toward your keeper".
  - [assumption] Partly ranked copies can be fed into another copy's rank-up. Put this behind config `arcane_ranked_copies_fusable` (default true) until confirmed in game.
- **Mods** (ranked with Endo, not duplicates): keep the single **highest-rank** copy, preferring a max-rank one. If none is maxed, the kept copy is noted as "needs X Endo to max". Everything else is surplus.

### 5.3 Pricing (per item and rank)

Inputs: the `/top` order book for that rank, plus v1 statistics rows filtered to `mod_rank == rank` (for items without ranks, take rows with no rank or rank 0).

- `med7` = volume-weighted mean of the daily `median` over the last 7 days of `90days` data. Fall back to the 48-hour data, then to none.
- `vol7` = average daily volume over the last 7 days.
- `ask` = lowest sell price among `ingame` users, falling back to `online`. Ignore orders below `outlier_floor × med7` (default 0.6) as trolls or mistakes.
- `bid` = highest buy price (`ingame` or `online`).
- **Suggested list price**, by strategy (config, default `balanced`):
  - `balanced`: `max(ask - undercut, max(bid, ceil(0.85 * med7)))`, with `undercut` default 0 (match the lowest ask)
  - `quick`: `max(bid, min(ask - 1, med7))`
  - `patient`: `max(med7, ask)`
  - If there's no `ask`, use `max(med7, bid)`. If there are no stats, use `ask`. If there's no data at all, mark "no market" and value it at 0.
- Also show an **instant option**: "sell to the top buyer `<ingameName>` at `bid`". Include a ready-to-paste whisper: `/w <name> Hi! I want to sell: "<Item Name>" (rank N) for <bid> platinum. (warframe.market)`.

### 5.4 Valuation and decisions

All choices maximise **net value** with one formula:

```
net = Σ realised_plat − trade_cost × trades_used + ducats × ducat_value − endo_used × endo_value/1000
```

Config defaults (tunable, shown in the report header):

- `trade_cost_plat = 5`: the opportunity cost of one of your limited daily trades (daily trades = Mastery Rank; config `daily_trades`).
- `ducat_value_plat = 0.08` plat per ducat. Using the Ducat Kiosk takes **0 trades**.
- `endo_value_per_1000 = 2.5` plat.
- `bulk_units_per_trade = 3`: for `bulkTradable` items (e.g. rank-0 arcanes), how many copies one buyer is assumed to take per trade.
- `horizon_days = 7`, `liquidity_ref = 5` (see §5.5).

**Prime sets vs parts.** For each set root:

- Let `c_p` be the surplus count of each part p and `q_p` its `quantityInSet`. The most sets you can build is `n_max = min_p floor(c_p / q_p)` (0 if any part is missing).
- For each n from 0 to `n_max`, compute `n × (set_price − trade_cost)` plus the best value of the leftover units of each part.
- A single part unit's best value is the highest of: `part_price − trade_cost` (sell), `ducats × ducat_value` (ducats), and 0 (hold).
- Pick the best n. Write a note explaining it, e.g. "selling 1 set beats parts by 12p and saves 3 trades".
- Main blueprints and component blueprints all count as parts.

**Arcanes.** Surplus units `U_s` and surplus max-rank copies `M_s`:

- For x from 0 to `floor(U_s / U_max)` (new max-rank copies built from surplus): `net(x) = (M_s + x) × (p_max − trade_cost) + (U_s − x × U_max) × (p_r0 − trade_cost / bulk_units_per_trade)`.
- Ranked partial copies go into builds first. Leftover partial copies with no market are reported as "fuse or hold".
- Choose the best x. The action is "fuse x copies to max, then list".

**Mods.** For each surplus copy:

- Rank 0: the best of selling at rank 0, maxing then selling (`p_max − trade_cost − EBC × (2^m − 1) × endo_value/1000`), or holding.
- Max rank: sell at `p_max`.
- In between: max it (Endo for the remaining ranks = `EBC × (2^m − 2^r)`) and sell, or hold.
- **EBC** (Endo base cost) by rarity: common 10, uncommon 20, rare 30 (also Amalgam / Galvanized), legendary 40. Rarity comes from WFM `rarity` or the tags. Total Endo from rank 0 to r = `EBC × (2^r − 1)`.
- Ignore credit costs in the MVP.

### 5.5 Liquidity, ranking and daily plan

- `liquidity_factor = min(1, vol7 / liquidity_ref)`. Tiers: High ≥ 10/day, Med 2–10/day, Low < 2/day.
- **Don't list more copies than the market can absorb:** `listable_qty = max(1, ceil(vol7 × horizon_days × share))` with `share` default 0.2. Surplus beyond that: prime parts become "ducat candidates", everything else becomes "hold (thin market)".
- **Priority score** = `net_per_trade × liquidity_factor`, where `net_per_trade = net / max(trades_used, 1)`.
- **Daily plan:** greedily take rows by priority until `daily_trades` is used up, then show expected plat for the day.

## 6. Report output

Write `out/report-YYYYMMDD-HHMM.csv` and `out/report-YYYYMMDD-HHMM.html` (a single self-contained HTML table with plain JS sorting and filtering, no CDN). Print the top 20 rows in the terminal with `rich`.

Columns: priority, item name, category, **action** (`List` / `Sell to top buyer` / `Fuse then list` / `Max then list` / `Ducats` / `Hold`), qty, rank, **suggested price**, ask (in game), bid, 7-day median, 7-day avg volume/day, liquidity tier, net value, plat per trade, trades needed, ducats, current WFM listing (price and qty, if the player has one), WFM link, notes.

Extra sections:

- Summary: total expected plat, trades needed, total ducats, inventory file age, config values used.
- Daily plan.
- Kept items: the keeper copies, plus arcanes still short of a keeper.
- Skipped / unmapped / no-market items, for debugging.

## 7. Config (`config.toml`, with `config.example.toml` committed)

```toml
[inventory]
path = ""                 # empty = %LOCALAPPDATA%\AlecaFrame\lastData.dat
max_age_hours = 2

[wfm]
platform = "pc"
crossplay = true
language = "en"
ingame_name = ""          # for reading your public listings without logging in
user_agent = "wf-trade-optimizer/0.1 (+https://github.com/<you>/wf-trade-optimizer)"
rps = 2.5

[cache_ttl_minutes]
catalog = 10080           # also invalidated by /v2/versions
sets = 10080
statistics = 360
top_orders = 10

[pricing]
strategy = "balanced"     # quick | balanced | patient
undercut = 0
outlier_floor = 0.6

[valuation]
trade_cost_plat = 5
daily_trades = 30         # set to your MR
ducat_value_plat = 0.08
endo_value_per_1000 = 2.5
bulk_units_per_trade = 3
horizon_days = 7
share = 0.2
liquidity_ref = 5

[scope]
categories = ["prime", "arcane", "mod"]
arcane_ranked_copies_fusable = true

[sync]                    # stretch goal
post_visible = true
manage_only_tool_orders = true
```

`.env.example`: `WFM_EMAIL=`, `WFM_PASSWORD=`, `WFM_JWT=` (optional fallback).

## 8. CLI (`wfopt`, built with Typer)

- `wfopt inventory`: decrypt, normalise and summarise. With `--dump`, write `data/inventory.json` and `data/holdings.json`.
- `wfopt catalog [--refresh]`: download or cache the catalogue and set compositions, then print mapping stats.
- `wfopt prices [--refresh]`: fetch prices for every mapped in-scope item, respecting the rate limit and caches, with a progress bar.
- `wfopt report [--strategy quick|balanced|patient] [--open]`: run everything above and write the reports.
- Stretch goal:
  - `wfopt listings diff`: compare `/v2/orders/my` with the recommendations and show creates, updates and deletes.
  - `wfopt listings apply [--yes]`: dry run by default. Applies the changes with confirmation and logs actions to `out/sync-log.jsonl`.

## 9. Project layout

```
wf-trade-optimizer/
  pyproject.toml            # Python 3.12+, uv; deps: httpx, cryptography, typer, rich, python-dotenv; dev: pytest, respx
  config.example.toml
  .env.example
  .gitignore                # .env, config.toml, cache/, data/, out/, tests/fixtures/private/
  src/wfopt/
    cli.py
    config.py
    inventory/aleca.py      # locate + decrypt lastData.dat
    inventory/normalize.py  # arrays -> holdings
    market/client.py        # httpx client: UA, headers, token-bucket limiter, retry/backoff (429/509/5xx), disk cache
    market/catalog.py       # /v2/versions, /v2/items, /v2/item/{slug}/set
    market/prices.py        # PriceSource interface; V1StatsSource + TopOrdersSource; combine -> PriceQuote
    engine/classify.py
    engine/keep.py
    engine/sets.py
    engine/arcanes.py
    engine/mods.py
    engine/pricing.py
    engine/plan.py          # scoring, listable qty, daily plan
    report/table.py         # rows model
    report/csv_out.py
    report/html_out.py
    sync/auth.py            # stretch
    sync/listings.py        # stretch
  tests/
    fixtures/               # synthetic inventory JSON + recorded API responses
    fixtures/private/       # the player's real decrypted inventory (gitignored)
```

## 10. Milestones and acceptance criteria

**M0: Inventory (start here)**

- Decrypt the real `lastData.dat`. Save the decrypted JSON to `tests/fixtures/private/`.
- **Inspect the real structure** and confirm the §4.1 assumptions: array names, the `UpgradeFingerprint` format, and where prime parts and blueprints live. Record findings in `docs/inventory-format.md`.
- Done when `wfopt inventory` prints counts per array and per category, and 5 hand-picked items (a prime part, a prime blueprint, an unranked arcane stack, a maxed arcane, a mod) show correct counts and ranks.

**M1: Catalogue and mapping**

- Done when every in-scope tradable item in the four inventory arrays maps to a slug, and the unmapped list contains only untradable or out-of-scope things (spot-check 20).
- Set compositions are cached.

**M2: Market client and prices**

- Rate limiter never goes over the configured rps.
- A second run within the TTLs makes 0 network calls.
- If statistics fail, it falls back to order-book-only pricing with a visible flag.
- Covered by unit tests with recorded responses (`respx`).

**M3: Engine**

- Unit tests for:
  - keep rules (arcane units model, 21 vs 10 copies to max, mod keeper selection)
  - set vs parts, including missing parts and `quantityInSet` > 1
  - arcane fuse vs sell (the Energize example above must choose "sell rank 0" under default config)
  - mod max-then-sell vs sell with Endo cost
  - ducat vs sell thresholds
  - listable quantity cap

**M4: Report**

- CSV and HTML produced. The terminal shows the top 20 rows and the daily plan.
- If `ingame_name` is set, current listings come from public `/v2/orders/user/{name}`.
- Every row has a human-readable note explaining its action.

**M5: Listing sync (stretch goal)**

- Auth via v1 sign-in, falling back to the JWT.
- `diff` is accurate.
- `apply` is dry run unless `--yes`. It never lists more than the surplus quantity, only touches orders the tool created (order IDs stored in `data/sync-state.json`) unless `manage_only_tool_orders = false`, and logs every write.

## 11. Testing notes

- Keep the engine pure: no network calls, only data in and data out. That makes it easy to test with fixtures.
- Record one real response per endpoint type into `tests/fixtures/` (statistics, top, set, item). Trim them to a few items.
- Add one end-to-end test: a synthetic inventory plus recorded API responses produces a deterministic report.

## 12. Stretch goal details: listing sync

- **What gets compared:** desired listings come from report rows with action `List` / `Fuse then list` / `Max then list`, with qty capped to `listable_qty`. These are compared with the player's current WFM sell orders. Match on (itemId, rank).
- **Operations:**
  - create a missing listing
  - update a listing whose price or qty is off by more than `max(2p, 5%)`
  - delete a listing for an item the player no longer has surplus of (only for tool-managed orders)
- Writes go through the same rate limiter. Stop on the first 4xx and print the error body.
- **Online status:** WFM only shows orders prominently while the seller is online or in game, and the tool doesn't manage status. The player keeps whatever they use today (the WFM site or app, or AlecaFrame).
- **Not in scope:** buying, undercutting loops, chat/whisper automation, any interaction with the game client.

## 13. Assumptions to verify first

1. Inventory array names and contents (§4.1), and that `ItemType` matches WFM `gameRef` for parts, blueprints, mods and arcanes. _(Check in M0.)_
2. `UpgradeFingerprint` is a JSON string with `lvl`; rank 0 copies may have no `lvl`. _(M0.)_
3. Whether partly ranked arcanes can be consumed to rank another copy. The player can check in game. _(Config flag until then.)_
4. v1 email/password sign-in still works for third-party clients. _(M5, with the JWT cookie fallback.)_
5. v1 statistics stays available. _(Design for fallback anyway.)_
6. `tradingTax` (credits) is shown only, not used in scoring.

## 14. Later ideas (not MVP)

- Import AlecaFrame's Stats export (CSV/JSON trade history) to calibrate the `trade_cost` and `share` defaults from real sales.
- Watch mode that re-runs the report when `lastData.dat` changes.
- Endo-to-plat value derived live from Ayatan sculpture prices.

# wfopt: Warframe trade optimizer

Reads your inventory from AlecaFrame's local cache (`%LOCALAPPDATA%\AlecaFrame\lastData.dat`), prices your surplus on warframe.market, and tells you what to sell, how (set vs parts, fuse arcanes, max mods, ducats), and for how much. It never touches the game: it only reads the file AlecaFrame already writes.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy config.example.toml config.toml   # set wfm.ingame_name to see your current listings
copy .env.example .env                 # only needed for `wfopt listings`
```

## Commands

| Command                                         | What it does                                                                    |
| ----------------------------------------------- | ------------------------------------------------------------------------------- |
| `wfopt inventory [--dump]`                      | Decrypt and summarise the inventory (`--dump` writes `data/*.json`)             |
| `wfopt catalog [--refresh]`                     | Load the WFM catalogue and set compositions, print mapping stats               |
| `wfopt prices [--refresh]`                      | Fetch statistics and order books for every surplus item (rate limited, cached)  |
| `wfopt report [--strategy quick\|balanced\|patient] [--open]` | Write `out/report-*.csv` and `.html`, print the top 20 and today's plan |
| `wfopt listings diff [--jwt ...]`               | Compare your WFM sell orders with the recommendations and save a draft         |
| `wfopt listings apply [--limit N]`              | Dry run: plans the top N listings (default and max 100) and saves them as a draft |
| `wfopt listings apply --yes`                    | Applies the saved draft exactly as reviewed (no prices recomputed). Logs writes to `out/sync-log.jsonl` |

The first `prices`/`report` run makes about 1–2k requests at 2.5 req/s (10–15 minutes). Responses are cached on disk (`cache/`), so an interrupted run resumes and later runs within the TTLs are fast.

## Notes

- Keep rules: every prime part is surplus. For mods and arcanes, one max-rank copy is kept (equipped copies count toward it).
- `--yes` refuses if the draft is older than `sync.draft_max_age_minutes` (60), was made for another account, `--limit` or `--strategy`, or your WFM listings changed since. Run the dry run again in that case.
- Listing sync only touches orders the tool created (`data/sync-state.json`) unless `sync.manage_only_tool_orders = false`.
- See [docs/PLAN.md](docs/PLAN.md), [docs/STEPS.md](docs/STEPS.md) and [docs/inventory-format.md](docs/inventory-format.md).

## Tests

```powershell
.venv\Scripts\python -m pytest
```

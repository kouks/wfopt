"""End-to-end run: inventory -> catalogue -> prices -> engine -> report model."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from wfopt.config import Config
from wfopt.engine.classify import PRIME, classify_holdings
from wfopt.engine.plan import build_rows, compute_keeps, daily_plan, daily_trades, price_needs, unknown_never_sell
from wfopt.inventory.aleca import InventoryFile
from wfopt.inventory.normalize import normalize
from wfopt.market.catalog import fetch_catalog, fetch_sets
from wfopt.market.client import WfmClient, WfmError, v2_data
from wfopt.market.prices import TopOrdersSource, V1StatsSource, fetch_quotes
from wfopt.report.table import Report, annotate_listings


def fetch_user_listings(client: WfmClient, cfg: Config) -> list[dict] | None:
    """The player's public sell orders (no auth). None if no ingame_name or the call fails."""
    name = cfg.wfm.ingame_name.strip()
    if not name:
        return None
    try:
        data = v2_data(client.get_json(f"/v2/orders/user/{name}", ttl_minutes=cfg.cache_ttl_minutes.top_orders))
    except WfmError:
        return None
    return [o for o in (data or []) if o.get("type") == "sell"]


def run(
    cfg: Config,
    client: WfmClient,
    inv: InventoryFile,
    *,
    strategy: str | None = None,
    refresh: bool = False,
    now: datetime | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> Report:
    now = now or datetime.now(timezone.utc)
    holdings = normalize(inv.data)
    catalog = fetch_catalog(client, cfg, refresh=refresh)
    classified = classify_holdings(holdings, catalog, cfg.scope.categories)
    primes = [c.item for c in classified.by_category(PRIME)]
    done = [0]

    def step(kind: str, total: int) -> Callable:
        def f(*_a) -> None:
            done[0] += 1
            if progress:
                progress(kind, done[0], total)

        return f

    fetch_sets(client, cfg, catalog, primes, refresh=refresh, progress=step("sets", len(primes)))
    keeps = compute_keeps(classified, cfg)
    needs = price_needs(keeps, catalog)
    ttl = cfg.cache_ttl_minutes
    done[0] = 0
    quotes = fetch_quotes(
        cfg, needs, V1StatsSource(client, ttl.statistics), TopOrdersSource(client, ttl.top_orders),
        refresh=refresh, now=now, progress=step("prices", len({n.item.id for n in needs}) + len(needs)),
    )
    rows = build_rows(keeps, catalog, quotes, cfg, strategy)
    listings = fetch_user_listings(client, cfg)
    if listings is not None:
        annotate_listings(rows, listings)
    budget = daily_trades(cfg, holdings.mastery_rank)
    return Report(
        generated_at=now,
        strategy=strategy or cfg.pricing.strategy,
        cfg=cfg,
        inventory_path=str(inv.path),
        inventory_age_hours=inv.age_hours,
        mastery_rank=holdings.mastery_rank,
        trades_remaining=holdings.trades_remaining,
        rows=rows,
        plan=daily_plan(rows, budget),
        keeps=keeps,
        classified=classified,
        quotes=quotes,
        listings=listings,
        network_calls=client.network_calls,
        extra={"never_sell_unknown": unknown_never_sell(cfg, catalog)},
    )


def run_rivens(
    cfg: Config,
    client: WfmClient,
    inv: InventoryFile,
    *,
    refresh: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> list:
    """Estimate every unveiled riven. Up to 3 auction searches per riven, deduplicated and cached."""
    from wfopt.engine.rivens import EXACT, MIN_COMPS, POSITIVES, WEAPON, describe, estimate, matching
    from wfopt.inventory.rivens import parse_rivens
    from wfopt.market.rivens import fetch_riven_catalog, fetch_weekly, search_auctions

    rivens = parse_rivens(inv.data)
    cat = fetch_riven_catalog(client, cfg, refresh=refresh)
    weekly = fetch_weekly(client, cfg, refresh=refresh)
    seen: dict[tuple, list | None] = {}

    def search(params: dict[str, str] | None):
        if params is None:
            return None
        key = tuple(sorted(params.items()))
        if key not in seen:
            seen[key] = search_auctions(client, cfg, params, refresh=refresh)
        return seen[key]

    out = []
    for i, r in enumerate(rivens, 1):
        est = describe(r, cat)
        results = {EXACT: search(est.tier_params(EXACT))}
        if est.searchable and len(matching(est, EXACT, results[EXACT])) < MIN_COMPS:
            results[POSITIVES] = search(est.tier_params(POSITIVES))
        found = max(len(matching(est, t, results.get(t))) for t in (EXACT, POSITIVES))
        if found < MIN_COMPS and not _has_weekly(weekly, est):
            results[WEAPON] = search(est.tier_params(WEAPON))  # last resort: the weapon's cheapest riven
        out.append(estimate(est, results, weekly))
        if progress:
            progress(i, len(rivens))
    if weekly is None:
        for e in out:
            e.notes.append("DE weekly riven stats unavailable")
    out.sort(key=lambda e: -(e.estimate or 0))
    return out


def _has_weekly(weekly, est) -> bool:
    return bool(weekly) and any((est.weapon_name.lower(), rr) in weekly for rr in (True, False))

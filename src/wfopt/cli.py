"""wfopt command-line interface."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from wfopt.config import Config, load_config
from wfopt.inventory.aleca import InventoryError, InventoryFile, load_inventory
from wfopt.inventory.normalize import Holdings, normalize

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Warframe trade optimizer")
console = Console()

_state: dict[str, object] = {}


@app.callback()
def main(
    config: Optional[Path] = typer.Option(None, "--config", help="Path to config.toml"),
    inventory_file: Optional[Path] = typer.Option(None, "--inventory-file", help="Override lastData.dat path"),
) -> None:
    cfg = load_config(config)
    if inventory_file:
        cfg.inventory.path = str(inventory_file)
    _state["cfg"] = cfg


def _cfg() -> Config:
    return _state["cfg"]  # type: ignore[return-value]


def _load_holdings(cfg: Config) -> tuple[InventoryFile, Holdings]:
    try:
        inv = load_inventory(cfg.inventory_path())
    except InventoryError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    if inv.age_hours > cfg.inventory.max_age_hours:
        console.print(
            f"[yellow]Inventory file is {inv.age_hours:.1f} h old. AlecaFrame refreshes it at login and "
            "loading screens; pass through one for fresh data.[/yellow]"
        )
    return inv, normalize(inv.data)


@app.command()
def inventory(dump: bool = typer.Option(False, "--dump", help="Write data/inventory.json and data/holdings.json")) -> None:
    """Decrypt, normalise and summarise the AlecaFrame inventory."""
    cfg = _cfg()
    inv, holdings = _load_holdings(cfg)
    console.print(f"Inventory: [bold]{inv.path}[/bold] (age {inv.age_hours:.1f} h)")
    console.print(f"Mastery rank: {holdings.mastery_rank}, trades remaining today: {holdings.trades_remaining}")

    t = Table(title="Holdings by source array")
    t.add_column("Array")
    t.add_column("Entries", justify="right")
    t.add_column("Units", justify="right")
    units: Counter[str] = Counter()
    for h in holdings.items:
        units[h.source] += h.count
    for src, n in holdings.counts_by_source().items():
        t.add_row(src, str(n), str(units[src]))
    console.print(t)
    console.print(f"Rivens skipped: {holdings.skipped_rivens}")
    console.print(f"Upgrade instances that look equipped: {sum(h.equipped for h in holdings.items)}")

    from wfopt.market.catalog import load_cached_catalog

    catalog = load_cached_catalog(cfg)
    if catalog is None:
        console.print("[dim]Run `wfopt catalog` to see counts per category.[/dim]")
    else:
        from wfopt.engine.classify import classify_holdings

        classified = classify_holdings(holdings, catalog)
        ct = Table(title="Per category (mapped via WFM catalogue)")
        ct.add_column("Category")
        ct.add_column("Distinct items", justify="right")
        ct.add_column("Units", justify="right")
        for cat, (n_items, n_units) in sorted(classified.category_counts().items()):
            ct.add_row(cat, str(n_items), str(n_units))
        console.print(ct)

    if dump:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        (cfg.data_dir / "inventory.json").write_text(json.dumps(inv.data, indent=1), encoding="utf-8")
        (cfg.data_dir / "holdings.json").write_text(
            json.dumps([h.to_dict() for h in holdings.items], indent=1), encoding="utf-8"
        )
        console.print(f"Wrote {cfg.data_dir / 'inventory.json'} and {cfg.data_dir / 'holdings.json'}")


def _client(cfg: Config):
    from wfopt.market.client import WfmClient

    return WfmClient(cfg)


def _catalog_and_classify(cfg: Config, client, holdings: Holdings, refresh: bool = False):
    from rich.progress import Progress

    from wfopt.engine.classify import PRIME, classify_holdings
    from wfopt.market.catalog import fetch_catalog, fetch_sets

    catalog = fetch_catalog(client, cfg, refresh=refresh)
    classified = classify_holdings(holdings, catalog, cfg.scope.categories)
    primes = [c.item for c in classified.by_category(PRIME)]
    with Progress(console=console, transient=True) as prog:
        task = prog.add_task("Set compositions", total=len(primes))
        fetch_sets(client, cfg, catalog, primes, refresh=refresh, progress=lambda _: prog.advance(task))
    return catalog, classified


@app.command()
def catalog(refresh: bool = typer.Option(False, "--refresh", help="Ignore caches")) -> None:
    """Download or load the WFM catalogue and set compositions, then print mapping stats."""
    cfg = _cfg()
    _, holdings = _load_holdings(cfg)
    with _client(cfg) as client:
        cat, classified = _catalog_and_classify(cfg, client, holdings, refresh)
        calls = client.network_calls
    console.print(f"Catalogue: {len(cat.items)} items (version {cat.version}), {len(cat.sets)} sets known")
    t = Table(title="Mapped holdings by category")
    t.add_column("Category")
    t.add_column("Distinct items", justify="right")
    t.add_column("Units", justify="right")
    for k, (n, u) in sorted(classified.category_counts().items()):
        t.add_row(k, str(n), str(u))
    console.print(t)
    ut = Table(title="Unmapped holdings by array (mostly untradable resources, expected)")
    ut.add_column("Array")
    ut.add_column("Entries", justify="right")
    for k, n in classified.unmapped_by_source().most_common():
        ut.add_row(k, str(n))
    console.print(ut)
    reasons = Counter(s.reason.split(":")[0] for s in classified.skipped)
    console.print("Skipped by reason: " + ", ".join(f"{r} ({n})" for r, n in reasons.most_common()))
    console.print(f"[dim]{calls} network calls[/dim]")


def _fetch_prices(cfg: Config, client, catalog, classified, refresh: bool = False):
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from wfopt.engine.plan import compute_keeps, price_needs
    from wfopt.market.prices import TopOrdersSource, V1StatsSource, fetch_quotes

    keeps = compute_keeps(classified, cfg)
    needs = price_needs(keeps, catalog)
    n_items = len({n.item.id for n in needs})
    ttl = cfg.cache_ttl_minutes
    cols = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn())
    with Progress(*cols, console=console, transient=True) as prog:
        t_stats = prog.add_task("Statistics", total=n_items)
        t_book = prog.add_task("Order books", total=len(needs))
        quotes = fetch_quotes(
            cfg,
            needs,
            V1StatsSource(client, ttl.statistics),
            TopOrdersSource(client, ttl.top_orders),
            refresh=refresh,
            progress=lambda kind: prog.advance(t_stats if kind == "stats" else t_book),
        )
    return keeps, needs, quotes


@app.command()
def prices(refresh: bool = typer.Option(False, "--refresh", help="Ignore caches")) -> None:
    """Fetch prices for every surplus in-scope item (rate limited, cached)."""
    cfg = _cfg()
    _, holdings = _load_holdings(cfg)
    with _client(cfg) as client:
        catalog, classified = _catalog_and_classify(cfg, client, holdings)
        _, needs, quotes = _fetch_prices(cfg, client, catalog, classified, refresh)
        calls = client.network_calls
    with_book = sum(q.orderbook_ok for q in quotes.values())
    no_stats = sum(not q.stats_ok for q in quotes.values())
    no_market = sum(not q.has_market for q in quotes.values())
    console.print(
        f"Priced {len(quotes)} (item, rank) pairs across {len({n.item.id for n in needs})} items: "
        f"{with_book} with live order book, {no_stats} without statistics, {no_market} with no market."
    )
    console.print(f"[dim]{calls} network calls[/dim]")


if __name__ == "__main__":
    app()

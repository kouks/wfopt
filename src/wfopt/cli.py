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


def _run_report(cfg: Config, strategy: Optional[str], refresh: bool):
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

    from wfopt.pipeline import run

    inv, _ = _load_holdings(cfg)
    cols = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn())
    with _client(cfg) as client, Progress(*cols, console=console, transient=True) as prog:
        tasks: dict[str, int] = {}

        def progress(kind: str, done: int, total: int) -> None:
            if kind not in tasks:
                tasks[kind] = prog.add_task({"sets": "Set compositions", "prices": "Prices"}.get(kind, kind), total=total)
            prog.update(tasks[kind], completed=done, total=total)

        return run(cfg, client, inv, strategy=strategy, refresh=refresh, progress=progress)


def _print_report(report, top: int = 20) -> None:
    from wfopt.engine.model import TRADE_ACTIONS

    t = Table(title=f"Top {top} recommendations ({report.strategy})", show_lines=False)
    for h, j in [("#", "right"), ("Item", "left"), ("Action", "left"), ("Qty", "right"), ("Rank", "right"),
                 ("Price", "right"), ("Bid", "right"), ("7d med", "right"), ("Vol/d", "right"), ("Net", "right"),
                 ("p/trade", "right"), ("Note", "left")]:
        t.add_column(h, justify=j, overflow="fold", max_width=60 if h == "Note" else None)
    shown = [r for r in report.rows if r.action in TRADE_ACTIONS][:top]
    for i, r in enumerate(shown, 1):
        t.add_row(
            str(i), r.name, r.action, str(r.qty), "" if r.rank is None else str(r.rank), "" if r.price is None else f"{r.price:g}",
            "" if r.bid is None else f"{r.bid:g}", "" if r.med7 is None else f"{r.med7:.0f}", f"{r.vol7:.1f}",
            f"{r.net:.0f}", f"{r.net_per_trade:.0f}", r.note,
        )
    console.print(t)

    p = report.plan
    pt = Table(title=f"Today's plan: {p.trades}/{p.budget} trades, ≈{p.plat:.0f}p (net {p.net:.0f}p)")
    for h in ("Item", "Action", "Qty", "Rank", "Price", "Trades", "Listing"):
        pt.add_column(h)
    for e in p.entries:
        r = e.row
        pt.add_row(r.name, r.action, str(e.qty), "" if r.rank is None else str(r.rank), "" if r.price is None else f"{r.price:g}", str(e.trades), r.current_listing)
    console.print(pt)
    if report.trades_remaining is not None and report.trades_remaining < p.budget:
        console.print(f"[yellow]Only {report.trades_remaining} trades left today according to the inventory file.[/yellow]")
    s = report.summary()
    console.print(" · ".join(f"{k}: [bold]{v}[/bold]" for k, v in s.items()))


@app.command()
def report(
    strategy: Optional[str] = typer.Option(None, "--strategy", help="quick | balanced | patient"),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore caches"),
    open_: bool = typer.Option(False, "--open", help="Open the HTML report in the browser"),
    top: int = typer.Option(20, "--top", help="Rows to print in the terminal"),
) -> None:
    """Run everything and write out/report-YYYYMMDD-HHMM.{csv,html}."""
    from datetime import datetime

    from wfopt.report.csv_out import write_csv
    from wfopt.report.html_out import write_html

    cfg = _cfg()
    if strategy and strategy not in ("quick", "balanced", "patient"):
        raise typer.BadParameter("strategy must be quick, balanced or patient")
    rep = _run_report(cfg, strategy, refresh)
    if rep.extra.get("never_sell_unknown"):
        console.print(f"[yellow]never_sell entries that match no item: {', '.join(rep.extra['never_sell_unknown'])}[/yellow]")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    csv_path = write_csv(rep, cfg.out_dir / f"report-{stamp}.csv")
    html_path = write_html(rep, cfg.out_dir / f"report-{stamp}.html")
    _print_report(rep, top)
    console.print(f"Wrote [bold]{csv_path}[/bold] and [bold]{html_path}[/bold] ({rep.network_calls} network calls)")
    if open_:
        import webbrowser

        webbrowser.open(html_path.resolve().as_uri())


listings_app = typer.Typer(no_args_is_help=True, help="Sync your WFM sell listings to the recommendations")
app.add_typer(listings_app, name="listings")


LIMIT_HELP = "Max open orders after syncing (WFM allows 100, the default). Top recommendations by priority win."


def _auth(cfg: Config, jwt: Optional[str]):
    from wfopt.sync.auth import AuthError, authenticate

    client = _client(cfg)
    try:
        me = authenticate(cfg, client, jwt)
    except AuthError as e:
        client.close()
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"Signed in as [bold]{me.ingame_name}[/bold] (token from {me.source})")
    return client, me


def _draft_ops(cfg: Config, client, me, strategy: Optional[str], limit: Optional[int]):
    """Build fresh recommendations and the listing ops, and save them as the draft for --yes."""
    from wfopt.pipeline import run
    from wfopt.sync.listings import SyncState, desired_from_rows, diff, fetch_my_orders, is_managed, save_draft, state_path

    limit = limit or 100
    inv, _ = _load_holdings(cfg)
    with console.status("Building recommendations…"):
        rep = run(cfg, client, inv, strategy=strategy)
    subtypes = {iid: ci.subtype for iid, ci in rep.classified.items.items()}
    desired = desired_from_rows(rep.rows, subtypes)
    orders = fetch_my_orders(client)
    state = SyncState.load(state_path(cfg))
    names = {iid: ci.item.name for iid, ci in rep.classified.items.items()}
    ops = diff(desired, orders, state, cfg.sync.manage_only_tool_orders, names, limit)
    fixed = sum(1 for o in orders if not is_managed(o, state, cfg.sync.manage_only_tool_orders))
    console.print(
        f"Listing the top {limit} recommendations ({len(desired)} wanted; "
        f"{fixed} of your orders aren't managed by the tool and use slots too)."
    )
    save_draft(cfg, ops, orders, me.id or me.ingame_name, limit, rep.strategy)
    return ops


def _print_ops(ops) -> None:
    from collections import Counter

    t = Table(title="Listing changes")
    t.add_column("Op")
    t.add_column("Change")
    style = {"create": "green", "update": "yellow", "delete": "red", "skip": "dim"}
    for op in ops:
        t.add_row(f"[{style[op.kind]}]{op.kind}[/]", op.describe())
    console.print(t)
    c = Counter(op.kind for op in ops)
    console.print(", ".join(f"{k}: {c.get(k, 0)}" for k in ("create", "update", "delete", "skip")))


def _draft_hint(cfg: Config, n: int) -> None:
    console.print(
        f"[bold]Dry run.[/bold] Draft saved. Run [bold]wfopt listings apply --yes[/bold] within "
        f"{cfg.sync.draft_max_age_minutes:g} min to apply these {n} change(s) exactly."
    )


@listings_app.command("diff")
def listings_diff(
    jwt: Optional[str] = typer.Option(None, "--jwt", help="JWT cookie value (fallback if sign-in fails)"),
    strategy: Optional[str] = typer.Option(None, "--strategy"),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, max=100, help=LIMIT_HELP),
) -> None:
    """Compare /v2/orders/my with the recommendations and save the result as the draft."""
    from wfopt.sync.listings import SKIP

    cfg = _cfg()
    client, me = _auth(cfg, jwt)
    try:
        ops = _draft_ops(cfg, client, me, strategy, limit)
    finally:
        client.close()
    _print_ops(ops)
    writes = sum(op.kind != SKIP for op in ops)
    if writes:
        _draft_hint(cfg, writes)


@listings_app.command("apply")
def listings_apply(
    yes: bool = typer.Option(False, "--yes", help="Apply the saved dry-run draft. Without it this is a dry run."),
    jwt: Optional[str] = typer.Option(None, "--jwt"),
    strategy: Optional[str] = typer.Option(None, "--strategy"),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, max=100, help=LIMIT_HELP),
) -> None:
    """Dry run saves a draft; --yes applies that draft as reviewed and logs to out/sync-log.jsonl."""
    from wfopt.sync.listings import (
        SKIP, SyncState, apply_ops, discard_draft, draft_problem, fetch_my_orders, load_draft, save_draft, state_path,
    )

    cfg = _cfg()
    client, me = _auth(cfg, jwt)
    try:
        if not yes:
            ops = _draft_ops(cfg, client, me, strategy, limit)
            _print_ops(ops)
            writes = sum(op.kind != SKIP for op in ops)
            if writes:
                _draft_hint(cfg, writes)
            else:
                console.print("Nothing to change.")
            return

        draft = load_draft(cfg)
        orders = fetch_my_orders(client)
        problem = draft_problem(draft, cfg, orders, me.id or me.ingame_name, limit, strategy)
        if problem:
            console.print(f"[red]Not applying: {problem}.[/red] Run [bold]wfopt listings apply[/bold] (dry run) again first.")
            raise typer.Exit(1)
        assert draft is not None
        console.print(f"Applying the draft from {draft.age_minutes:.0f} min ago (no prices recomputed).")
        _print_ops(draft.ops)
        writes = [op for op in draft.ops if op.kind != SKIP]
        if not writes:
            console.print("Nothing to change.")
            discard_draft(cfg)
            return
        state = SyncState.load(state_path(cfg))
        n, remaining = apply_ops(cfg, client, draft.ops, state, cfg.out_dir / "sync-log.jsonl", echo=console.print)
        console.print(f"Applied {n}/{len(writes)} change(s). Log: {cfg.out_dir / 'sync-log.jsonl'}")
        if remaining:
            # keep the rest as the draft, against the orders as they are now, so --yes resumes from the failure
            save_draft(cfg, remaining, fetch_my_orders(client), draft.user_id, draft.limit, draft.strategy, draft.created_at)
            left = sum(op.kind != SKIP for op in remaining)
            console.print(
                f"Kept the {left} unapplied change(s) as the draft. Fix the cause and run "
                "[bold]wfopt listings apply --yes[/bold] again to resume."
            )
        else:
            discard_draft(cfg)
    finally:
        client.close()


if __name__ == "__main__":
    app()

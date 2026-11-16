"""Orchestration (pure): keep -> price needs -> decisions -> scoring -> daily plan."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from wfopt.config import Config
from wfopt.engine.arcanes import decide_arcane
from wfopt.engine.classify import ARCANE, MOD, PRIME, Classified
from wfopt.engine.keep import KeepResult, arcane_units, compute_keep
from wfopt.engine.model import DUCATS, HOLD, TRADE_ACTIONS, Row
from wfopt.engine.mods import decide_mod
from wfopt.engine.pricing import PriceQuote
from wfopt.engine.sets import PartStock, decide_set
from wfopt.engine.valuation import Market
from wfopt.market.catalog import Catalog, CatalogItem
from wfopt.market.prices import PriceNeed


def compute_keeps(classified: Classified, cfg: Config) -> list[KeepResult]:
    return [compute_keep(ci, cfg.scope.arcane_ranked_copies_fusable) for ci in classified.items.values()]


@dataclass
class SetGroup:
    root: CatalogItem | None
    parts: list[PartStock]


def group_primes(keeps: list[KeepResult], catalog: Catalog) -> list[SetGroup]:
    """Group surplus prime parts by set. Parts of a set the player doesn't hold count as 0."""
    by_root: dict[str | None, dict[str, int]] = defaultdict(dict)
    standalone: list[KeepResult] = []
    for k in keeps:
        if k.item.category != PRIME or not k.surplus_units:
            continue
        root_id = catalog.part_to_set.get(k.item.item.id)
        if root_id is None:
            standalone.append(k)
        else:
            by_root[root_id][k.item.item.id] = k.surplus_units
    groups = []
    for root_id, held in by_root.items():
        root = catalog.by_id.get(root_id) if root_id else None
        parts = []
        for pid, q in catalog.sets[root_id].parts.items():
            it = catalog.by_id.get(pid)
            if it is not None:
                parts.append(PartStock(it, held.get(pid, 0), q))
        groups.append(SetGroup(root, parts))
    for k in standalone:
        groups.append(SetGroup(None, [PartStock(k.item.item, k.surplus_units, 1)]))
    return groups


def price_needs(keeps: list[KeepResult], catalog: Catalog) -> list[PriceNeed]:
    needs: dict[tuple[str, int | None], PriceNeed] = {}

    def add(item: CatalogItem, rank: int | None, units: int, subtype: str | None = None) -> None:
        key = (item.id, rank)
        if key in needs:
            old = needs[key]
            needs[key] = PriceNeed(item, rank, old.units + units, subtype or old.subtype)
        else:
            needs[key] = PriceNeed(item, rank, units, subtype)

    for g in group_primes(keeps, catalog):
        for ps in g.parts:
            if ps.surplus:
                add(ps.item, None, ps.surplus)
        if g.root is not None and g.parts and min(ps.surplus // ps.qty_in_set for ps in g.parts) >= 1:
            add(g.root, None, min(ps.surplus // ps.qty_in_set for ps in g.parts))

    for k in keeps:
        it = k.item.item
        if not k.surplus or k.item.category == PRIME:
            continue
        m = it.max_rank or 0
        sub = k.item.subtype
        s = k.surplus
        if k.item.category == ARCANE:
            u_s = sum(arcane_units(r) * n for r, n in s.items() if r is not None and r < m)
            if s.get(0):
                add(it, 0, s[0], sub)
            if s.get(m) or u_s >= arcane_units(m):
                add(it, m, s.get(m, 0) + u_s // arcane_units(m), sub)
        elif k.item.category == MOD:
            if s.get(0):
                add(it, 0, s[0], sub)
            if m > 0:
                add(it, m, sum(s.values()), sub)
    return list(needs.values())


LIQ_HIGH, LIQ_MED = 10, 2


def liquidity_tier(vol7: float) -> str:
    if vol7 >= LIQ_HIGH:
        return "High"
    if vol7 >= LIQ_MED:
        return "Med"
    return "Low"


def score(rows: list[Row], cfg: Config) -> None:
    ref = cfg.valuation.liquidity_ref
    for r in rows:
        r.liquidity = liquidity_tier(r.vol7)
        r.liquidity_factor = min(1.0, r.vol7 / ref) if ref > 0 else 1.0
        if r.action in TRADE_ACTIONS:
            r.priority = r.net_per_trade * r.liquidity_factor
        elif r.action == DUCATS:
            r.priority = 0.0
        else:
            r.priority = -1.0


def build_rows(
    keeps: list[KeepResult],
    catalog: Catalog,
    quotes: dict[tuple[str, int | None], PriceQuote],
    cfg: Config,
    strategy: str | None = None,
) -> list[Row]:
    mkt = Market(quotes, cfg, strategy)
    rows: list[Row] = []
    for g in group_primes(keeps, catalog):
        rows.extend(decide_set(g.root, [p for p in g.parts], mkt))
    for k in keeps:
        if not k.surplus:
            continue
        if k.item.category == ARCANE:
            rows.extend(decide_arcane(k, mkt, cfg.scope.arcane_ranked_copies_fusable))
        elif k.item.category == MOD:
            rows.extend(decide_mod(k, mkt))
    rows = [r for r in rows if r.qty > 0]
    score(rows, cfg)
    rows.sort(key=lambda r: (-r.priority, -r.net, r.name))
    return rows


@dataclass
class PlanEntry:
    row: Row
    qty: int
    trades: int
    net: float


@dataclass
class DailyPlan:
    budget: int
    entries: list[PlanEntry] = field(default_factory=list)
    ducat_rows: list[Row] = field(default_factory=list)

    @property
    def trades(self) -> int:
        return sum(e.trades for e in self.entries)

    @property
    def plat(self) -> float:
        return sum(e.qty * (e.row.price or 0) for e in self.entries)

    @property
    def net(self) -> float:
        return sum(e.net for e in self.entries)


def daily_trades(cfg: Config, mastery_rank: int | None) -> int:
    return cfg.valuation.daily_trades or mastery_rank or 30


def daily_plan(rows: list[Row], budget: int) -> DailyPlan:
    """Greedy by priority until the trade budget is used. Rows may be split."""
    plan = DailyPlan(budget)
    left = budget
    for r in sorted((r for r in rows if r.action in TRADE_ACTIONS and r.net > 0), key=lambda r: -r.priority):
        if left <= 0:
            break
        if r.trades <= left:
            plan.entries.append(PlanEntry(r, r.qty, r.trades, r.net))
            left -= r.trades
        else:
            per_trade_units = max(1, r.qty // max(r.trades, 1))
            qty = min(r.qty, left * per_trade_units)
            plan.entries.append(PlanEntry(r, qty, left, r.net * qty / r.qty))
            left = 0
    plan.ducat_rows = [r for r in rows if r.action == DUCATS]
    return plan


def kept_summary(keeps: list[KeepResult]) -> list[KeepResult]:
    """Keeper copies for mods/arcanes (including arcanes still short of a keeper)."""
    return [k for k in keeps if k.item.category in (ARCANE, MOD)]


def is_hold(r: Row) -> bool:
    return r.action == HOLD

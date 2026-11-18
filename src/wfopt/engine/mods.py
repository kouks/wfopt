"""Mods: sell at rank 0 by default; max with Endo only when it clearly pays.

Rank barely moves most mod prices and Endo is scarce, so rank 0 is the default. Maxing a spare
copy is recommended only when it earns at least `min_plat_per_1000_endo` extra per 1,000 Endo and
`min_max_gain_plat` per copy, and the Endo across all mods stays within `endo_budget_share` of
the player's Endo (best plat per Endo first). Spare copies that are already maxed cost no Endo and
list at whichever rank nets more.
"""

from __future__ import annotations

from dataclasses import dataclass

from wfopt.engine.classify import MOD
from wfopt.engine.keep import KeepResult, endo_to_max, equipped_warning
from wfopt.engine.model import HOLD, LIST, MAX, Row
from wfopt.engine.valuation import Market, hold_row, sale_row


def endo_plat(endo: int, mkt: Market) -> float:
    return endo * mkt.cfg.valuation.endo_value_per_1000 / 1000


@dataclass
class Upgrade:
    """Spare copies at one rank that would be worth maxing."""

    keep: KeepResult
    rank: int
    count: int
    endo_each: int
    gain_each: float  # extra plat per copy from selling at max instead of rank 0

    @property
    def per_1000_endo(self) -> float:
        return self.gain_each / (self.endo_each / 1000)


def _values(keep: KeepResult, mkt: Market) -> tuple[float, float | None]:
    """(rank-0 net per copy, or 0 if not worth selling; max-rank net per copy or None)."""
    item = keep.item.item
    m = item.max_rank or 0
    v0 = mkt.unit_sale_value(item, 0)
    vm = mkt.unit_sale_value(item, m) if m > 0 else None
    return (v0 if v0 is not None and v0 > 0 else 0.0), vm


def _maxed_at_max(keep: KeepResult, mkt: Market) -> int:
    """Spare already-maxed copies that list at max rank (no Endo needed)."""
    item = keep.item.item
    m = item.max_rank or 0
    n = keep.surplus.get(m, 0) if m > 0 else 0
    base, vm = _values(keep, mkt)
    if not n or vm is None or vm <= 0 or vm <= base:
        return 0
    return min(n, mkt.cap(item, m))


def upgrade_candidates(keep: KeepResult, mkt: Market) -> list[Upgrade]:
    item = keep.item.item
    m = item.max_rank or 0
    if m == 0:
        return []
    base, vm = _values(keep, mkt)
    if vm is None or vm <= 0:
        return []
    v = mkt.cfg.valuation
    qm = mkt.quote(item, m)
    if qm is None or qm.vol7 < v.min_max_rank_sales_per_day:
        return []  # too few max-rank sales to trust the price
    gain = vm - base
    slots = mkt.cap(item, m) - _maxed_at_max(keep, mkt)
    out = []
    for r in sorted((r for r in keep.surplus if r is not None and r < m), reverse=True):  # cheapest Endo first
        if slots <= 0:
            break
        endo = endo_to_max(item.tags, r, m)
        if endo <= 0 or gain < v.min_max_gain_plat or gain / (endo / 1000) < v.min_plat_per_1000_endo:
            continue
        k = min(keep.surplus[r], slots)
        slots -= k
        out.append(Upgrade(keep, r, k, endo, gain))
    return out


def allocate_endo(cands: list[Upgrade], budget: int | None) -> dict[tuple[str, int], int]:
    """Best plat per Endo first until the budget runs out. Returns (item id, rank) -> copies to max."""
    left = budget
    out: dict[tuple[str, int], int] = {}
    for c in sorted(cands, key=lambda c: -c.per_1000_endo):
        k = c.count if left is None else min(c.count, left // c.endo_each)
        if k > 0:
            out[(c.keep.item.item.id, c.rank)] = k
            if left is not None:
                left -= k * c.endo_each
    return out


def decide_mod(
    keep: KeepResult, mkt: Market, upgrades: dict[int, int] | None = None, over_budget: list[Upgrade] | None = None
) -> list[Row]:
    """`upgrades`: rank -> copies to max (from allocate_endo). `over_budget`: candidates cut by the Endo budget."""
    item = keep.item.item
    m = item.max_rank or 0
    upgrades = upgrades or {}
    rows: list[Row] = []
    pool: dict[int, int] = {r or 0: c for r, c in keep.surplus.items() if c}

    # 1. spare copies already at max: list at max when that nets more (no Endo)
    at_max = _maxed_at_max(keep, mkt)
    if at_max:
        pool[m] -= at_max
        rows.append(sale_row(item, MOD, at_max, m, mkt, note="spare max-rank copies (no Endo needed); max rank sells for more"))

    # 2. copies worth maxing, within the Endo budget
    for r, k in sorted(upgrades.items(), reverse=True):
        k = min(k, pool.get(r, 0))
        if not k:
            continue
        pool[r] -= k
        endo = endo_to_max(item.tags, r, m)
        base, vm = _values(keep, mkt)
        gain = (vm or 0) - base
        row = sale_row(
            item, MOD, k, m, mkt, action=MAX, extra_cost=k * endo_plat(endo, mkt),
            note=(f"max from rank {r} ({endo:,} Endo each), then list: +{gain:.0f}p per copy over rank 0, "
                  f"{gain / (endo / 1000):.0f}p per 1,000 Endo"),
        )
        row.endo = k * endo
        rows.append(row)

    # 3. everything else: rank 0
    n = sum(pool.values())
    if n:
        ranked = sum(c for r, c in pool.items() if r)
        notes = []
        if ranked:
            notes.append(f"includes {ranked} ranked cop{'ies' if ranked > 1 else 'y'}, listed at rank 0")
        if over_budget:
            best = max(over_budget, key=lambda c: c.per_1000_endo)
            notes.append(f"maxing would earn {best.per_1000_endo:.0f}p per 1,000 Endo, but this run's Endo budget is used up")
        v0 = mkt.unit_sale_value(item, 0)
        if v0 is not None and v0 > 0:
            k = min(n, mkt.cap(item, 0))
            rows.append(sale_row(item, MOD, k, 0, mkt, action=LIST, note="; ".join(notes) or "sell at rank 0"))
            if n - k:
                rows.append(hold_row(item, MOD, n - k, 0, mkt,
                                     f"hold (thin market): sells ~{mkt.cap(item, 0)} per {mkt.cfg.valuation.horizon_days:g} days"))
        else:
            q = mkt.quote(item, 0)
            reason = "no market" if q is None or not q.has_market else "not worth a trade (price below the trade cost)"
            rows.append(hold_row(item, MOD, n, 0, mkt, "; ".join([reason, *notes])))

    eq = equipped_warning(keep)
    for r in rows:
        if eq and r.action != HOLD:
            r.notes.append(eq)
        r.notes.append(keep.status)
    return rows


def decide_mods(keeps: list[KeepResult], mkt: Market, endo_available: int | None) -> tuple[list[Row], dict]:
    """All mods together, so the Endo budget goes to the best plat per Endo across the inventory."""
    keeps = [k for k in keeps if k.item.category == MOD and k.surplus]
    share = mkt.cfg.valuation.endo_budget_share
    budget = None if endo_available is None else int(endo_available * share)
    cands = [c for k in keeps for c in upgrade_candidates(k, mkt)]
    alloc = allocate_endo(cands, budget)
    cut: dict[str, list[Upgrade]] = {}
    for c in cands:
        if alloc.get((c.keep.item.item.id, c.rank), 0) < c.count:
            cut.setdefault(c.keep.item.item.id, []).append(c)
    rows: list[Row] = []
    for k in keeps:
        iid = k.item.item.id
        ups = {r: n for (i, r), n in alloc.items() if i == iid}
        rows.extend(decide_mod(k, mkt, ups, cut.get(iid)))
    used = sum(r.endo for r in rows)
    return rows, {"endo_available": endo_available, "endo_budget": budget, "endo_used": used}

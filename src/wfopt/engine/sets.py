"""Prime sets vs parts vs ducats."""

from __future__ import annotations

from dataclasses import dataclass

from wfopt.engine.classify import PRIME
from wfopt.engine.model import DUCATS, Row
from wfopt.engine.valuation import Market, base_row, hold_row, sale_row
from wfopt.market.catalog import CatalogItem


@dataclass
class PartStock:
    item: CatalogItem
    surplus: int
    qty_in_set: int = 1


@dataclass
class PartPlan:
    sell: int
    ducats: int
    hold: int
    value: float
    trades: int
    thin: bool  # some units moved to ducats/hold only because of the liquidity cap


def best_part_plan(p: CatalogItem, units: int, mkt: Market) -> PartPlan:
    """Best use of `units` spare copies of one part: sell up to the market cap, otherwise ducats or hold."""
    dv = (p.ducats or 0) * mkt.cfg.valuation.ducat_value_plat
    sv = mkt.unit_sale_value(p, None)
    if units <= 0:
        return PartPlan(0, 0, 0, 0.0, 0, False)
    if sv is not None and sv > dv and sv > 0:
        s = min(units, mkt.cap(p, None))
        rest = units - s
        d, h = (rest, 0) if dv > 0 else (0, rest)
        return PartPlan(s, d, h, s * sv + d * dv, mkt.trades_for(p, s), rest > 0)
    if dv > 0:
        return PartPlan(0, units, 0, units * dv, 0, False)
    return PartPlan(0, 0, units, 0.0, 0, False)


def evaluate_sets(root: CatalogItem | None, parts: list[PartStock], mkt: Market) -> tuple[int, float, int, dict[str, PartPlan]]:
    """Choose n (sets to sell). Returns (n, net, trades, part plans for leftovers)."""
    if root is not None and parts and all(ps.qty_in_set > 0 for ps in parts):
        n_max = min(ps.surplus // ps.qty_in_set for ps in parts)
    else:
        n_max = 0
    set_v = mkt.unit_sale_value(root, None) if root is not None else None
    if set_v is None or set_v <= 0:
        n_max = 0
    else:
        n_max = min(n_max, mkt.cap(root, None))
    best: tuple[float, int, int] | None = None
    best_plans: dict[str, PartPlan] = {}
    for n in range(n_max + 1):
        net = n * set_v if n else 0.0
        trades = n
        plans = {}
        for ps in parts:
            pp = best_part_plan(ps.item, ps.surplus - n * ps.qty_in_set, mkt)
            plans[ps.item.id] = pp
            net += pp.value
            trades += pp.trades
        key = (round(net, 6), -trades, n)
        if best is None or key > (round(best[0], 6), -best[1], best[2]):
            best = (net, trades, n)
            best_plans = plans
    assert best is not None
    return best[2], best[0], best[1], best_plans


def decide_set(root: CatalogItem | None, parts: list[PartStock], mkt: Market) -> list[Row]:
    n, net, trades, plans = evaluate_sets(root, parts, mkt)
    rows: list[Row] = []
    explain = ""
    if root is not None:
        missing = [ps.item.name for ps in parts if ps.surplus < ps.qty_in_set]
        if n > 0:
            _, net0, trades0, _ = evaluate_sets(None, parts, mkt)
            explain = f"selling {n} set{'s' if n > 1 else ''} beats selling the parts by {net - net0:.0f}p"
            if trades0 > trades:
                saved = trades0 - trades
                explain += f" and saves {saved} trade{'s' if saved > 1 else ''}"
            rows.append(sale_row(root, PRIME, n, None, mkt, note=explain))
        elif not missing and mkt.price(root, None) is None:
            explain = "no market for the full set"
        elif not missing:
            explain = "parts are worth more separately than as a set"
        elif len(missing) < len(parts):
            explain = "no full set: missing " + ", ".join(missing)

    dv_unit = mkt.cfg.valuation.ducat_value_plat
    for ps in parts:
        pp = plans[ps.item.id]
        p = ps.item
        if pp.sell:
            dv = (p.ducats or 0) * dv_unit
            sv = mkt.unit_sale_value(p, None) or 0
            note = f"sells for ~{sv:.0f}p net per unit vs {dv:.1f}p as {p.ducats} ducats"
            r = sale_row(p, PRIME, pp.sell, None, mkt, note=note)
            if explain:
                r.notes.append(explain)
            rows.append(r)
        if pp.ducats:
            r = base_row(p, PRIME, DUCATS, pp.ducats, None, mkt)
            r.price = mkt.price(p, None)
            r.ducats = pp.ducats * (p.ducats or 0)
            r.net = r.ducats * dv_unit
            if pp.thin:
                r.notes.insert(0, f"market only absorbs ~{mkt.cap(p, None)} in {mkt.cfg.valuation.horizon_days:g} days; rest to the Ducat Kiosk")
            elif r.price is None:
                r.notes.insert(0, "no market; Ducat Kiosk (0 trades)")
            else:
                r.notes.insert(0, f"{p.ducats} ducats ({p.ducats * dv_unit:.1f}p) beats selling at {r.price}p minus the trade cost")
            if explain:
                r.notes.append(explain)
            rows.append(r)
        if pp.hold:
            rows.append(hold_row(p, PRIME, pp.hold, None, mkt, "no market and no ducat value" if pp.value == 0 else "thin market"))
    return rows

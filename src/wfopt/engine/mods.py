"""Mods: every spare copy is priced and listed at rank 0, or held.

Rank barely moves most mod prices and Endo is scarce, so the tool never maxes mods and never
compares against max-rank prices. Spare ranked copies are sold as rank-0 listings (the buyer
gets a ranked copy for the rank-0 price).
"""

from __future__ import annotations

from wfopt.engine.classify import MOD
from wfopt.engine.keep import KeepResult, equipped_warning
from wfopt.engine.model import HOLD, LIST, Row
from wfopt.engine.valuation import Market, hold_row, sale_row


def decide_mod(keep: KeepResult, mkt: Market) -> list[Row]:
    item = keep.item.item
    n = sum(keep.surplus.values())
    if not n:
        return []
    ranked = sum(c for r, c in keep.surplus.items() if r)
    q = mkt.quote(item, 0)
    v0 = mkt.unit_sale_value(item, 0)
    rows: list[Row] = []

    ranked_note = (
        f"includes {ranked} ranked cop{'ies' if ranked > 1 else 'y'}, listed at rank 0 (rank barely changes the price)"
        if ranked
        else ""
    )
    if v0 is not None and v0 > 0:
        k = min(n, mkt.cap(item, 0))
        rows.append(sale_row(item, MOD, k, 0, mkt, action=LIST, note=ranked_note or "sell at rank 0"))
        if n - k:
            rows.append(
                hold_row(
                    item, MOD, n - k, 0, mkt,
                    f"hold (thin market): sells ~{mkt.cap(item, 0)} per {mkt.cfg.valuation.horizon_days:g} days",
                )
            )
    else:
        reason = "no market" if q is None or not q.has_market else "not worth a trade (price below the trade cost)"
        rows.append(hold_row(item, MOD, n, 0, mkt, reason))

    eq = equipped_warning(keep)
    for r in rows:
        if eq and r.action != HOLD:
            r.notes.append(eq)
        r.notes.append(keep.status)
    return rows

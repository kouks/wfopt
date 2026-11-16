"""Arcanes: fuse surplus into max-rank copies, or sell unranked."""

from __future__ import annotations

from dataclasses import dataclass

from wfopt.engine.classify import ARCANE
from wfopt.engine.keep import KeepResult, arcane_units, equipped_warning
from wfopt.engine.model import FUSE, HOLD, LIST, Row
from wfopt.engine.valuation import Market, hold_row, sale_row


@dataclass
class ArcanePlan:
    x: int  # new max copies fused from surplus
    sell_existing_max: int
    sell_fused: int
    sell_r0: int
    left_r0: int
    left_partial_units: int
    unsold_max: int
    net: float
    trades: int


def plan_arcane(keep: KeepResult, mkt: Market, ranked_fusable: bool = True) -> tuple[ArcanePlan, list[ArcanePlan]]:
    """Best plan plus every evaluated alternative (x = 0..floor(U_s / U_max))."""
    item = keep.item.item
    m = item.max_rank or 0
    u_max = arcane_units(m)
    s = keep.surplus
    m_s = s.get(m, 0)
    r0 = s.get(0, 0)
    partial_units = sum(arcane_units(r) * n for r, n in s.items() if r is not None and 0 < r < m)
    fusable_partial = partial_units if ranked_fusable else 0
    u_s = r0 + fusable_partial

    v_max = mkt.unit_sale_value(item, m)
    v_0 = mkt.unit_sale_value(item, 0)
    cap_max, cap_0 = mkt.cap(item, m), mkt.cap(item, 0)
    bulk = mkt.bulk(item, 0)

    plans = []
    for x in range(u_s // u_max + 1 if u_max else 1):
        need = x * u_max
        use_p = min(fusable_partial, need)
        use_r = need - use_p
        left_r = r0 - use_r
        total_max = m_s + x
        sold_max = min(total_max, cap_max) if v_max is not None and v_max > 0 else 0
        sold_r = min(left_r, cap_0) if v_0 is not None and v_0 > 0 else 0
        net = sold_max * (v_max or 0) + sold_r * (v_0 or 0)
        trades = sold_max + (-(-sold_r // bulk))
        existing = min(m_s, sold_max)
        plans.append(
            ArcanePlan(
                x, existing, sold_max - existing, sold_r, left_r - sold_r, partial_units - use_p, total_max - sold_max, net, trades
            )
        )
    best = max(plans, key=lambda p: (round(p.net, 6), -p.trades, -p.x))
    return best, plans


def decide_arcane(keep: KeepResult, mkt: Market, ranked_fusable: bool = True) -> list[Row]:
    item = keep.item.item
    m = item.max_rank or 0
    u_max = arcane_units(m)
    best, plans = plan_arcane(keep, mkt, ranked_fusable)
    rows: list[Row] = []

    # Explain against the main alternative.
    alt = None
    if best.x > 0:
        alt = plans[0]
    elif len(plans) > 1:
        alt = plans[1]
    why = ""
    if alt is not None:
        if best.x > 0:
            why = (
                f"fusing {best.x * u_max} units into {best.x} max cop{'ies' if best.x > 1 else 'y'} nets {best.net:.0f}p "
                f"vs {alt.net:.0f}p selling unranked"
            )
        else:
            why = (
                f"selling unranked nets {best.net:.0f}p vs {alt.net:.0f}p fusing {u_max} units into a rank {m} copy"
            )

    if best.sell_existing_max:
        rows.append(sale_row(item, ARCANE, best.sell_existing_max, m, mkt, note="spare max-rank copies"))
    if best.sell_fused:
        rows.append(
            sale_row(
                item, ARCANE, best.sell_fused, m, mkt, action=FUSE,
                note=f"fuse {best.sell_fused} cop{'ies' if best.sell_fused > 1 else 'y'} to rank {m} ({u_max} units each), then list; {why}",
            )
        )
    if best.sell_r0:
        r = sale_row(item, ARCANE, best.sell_r0, 0, mkt, action=LIST, note=why)
        rows.append(r)
    if best.left_r0:
        q = mkt.quote(item, 0)
        reason = "no market" if q is None or not q.has_market else (
            f"market only absorbs ~{mkt.cap(item, 0)} in {mkt.cfg.valuation.horizon_days:g} days" if best.sell_r0 else "not worth a trade"
        )
        rows.append(hold_row(item, ARCANE, best.left_r0, 0, mkt, f"hold (thin market): {reason}" if best.sell_r0 else reason))
    left_partials = {r: n for r, n in keep.surplus.items() if r is not None and 0 < r < m}
    if left_partials and best.left_partial_units:
        desc = ", ".join(f"{n}× r{r}" for r, n in sorted(left_partials.items(), reverse=True))
        rows.append(
            hold_row(item, ARCANE, sum(left_partials.values()), None, mkt, f"partly ranked copies ({desc}): fuse or hold")
        )
    if best.unsold_max:
        rows.append(hold_row(item, ARCANE, best.unsold_max, m, mkt, "hold (thin market) or no buyer at rank max"))
    eq = equipped_warning(keep)
    for r in rows:
        if eq and r.action != HOLD:
            r.notes.append(eq)
        r.notes.append(keep.status)
    return rows

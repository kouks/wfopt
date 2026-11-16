"""Mods: sell unranked, max with Endo then sell, or hold."""

from __future__ import annotations

from wfopt.engine.classify import MOD
from wfopt.engine.keep import KeepResult, endo_to_max, equipped_warning
from wfopt.engine.model import HOLD, LIST, MAX, Row
from wfopt.engine.valuation import Market, hold_row, sale_row


def endo_plat(endo: int, mkt: Market) -> float:
    return endo * mkt.cfg.valuation.endo_value_per_1000 / 1000


def max_then_sell_value(keep: KeepResult, rank: int, mkt: Market) -> tuple[float | None, int]:
    """(net per copy, endo needed) for maxing a copy at `rank` and selling it at max."""
    item = keep.item.item
    m = item.max_rank or 0
    v = mkt.unit_sale_value(item, m)
    endo = endo_to_max(item.tags, rank, m)
    if v is None:
        return None, endo
    return v - endo_plat(endo, mkt), endo


def decide_mod(keep: KeepResult, mkt: Market) -> list[Row]:
    item = keep.item.item
    m = item.max_rank or 0
    s = keep.surplus
    rows: list[Row] = []
    cap_m_left = mkt.cap(item, m)
    equipped_note = equipped_warning(keep)

    def no_market(rank: int) -> bool:
        q = mkt.quote(item, rank)
        return q is None or not q.has_market

    # 1. spare max-rank copies
    n_max = s.get(m, 0) if m > 0 else 0
    if n_max:
        v = mkt.unit_sale_value(item, m)
        if v is not None and v > 0:
            k = min(n_max, cap_m_left)
            cap_m_left -= k
            rows.append(sale_row(item, MOD, k, m, mkt, note="spare max-rank copies"))
            if n_max - k:
                rows.append(hold_row(item, MOD, n_max - k, m, mkt, f"hold (thin market): sells ~{mkt.cap(item, m)} per {mkt.cfg.valuation.horizon_days:g} days"))
        else:
            rows.append(hold_row(item, MOD, n_max, m, mkt, "no market at max rank" if no_market(m) else "not worth a trade"))

    # 2. partly ranked copies: max them (cheapest Endo first) or hold
    for r in sorted((r for r in s if r is not None and 0 < r < m), reverse=True):
        n = s[r]
        v, endo = max_then_sell_value(keep, r, mkt)
        if v is not None and v > 0 and cap_m_left > 0:
            k = min(n, cap_m_left)
            cap_m_left -= k
            rows.append(
                sale_row(
                    item, MOD, k, m, mkt, action=MAX, extra_cost=k * endo_plat(endo, mkt),
                    note=f"max from rank {r} ({endo:,} Endo ≈ {endo_plat(endo, mkt):.0f}p each), then list",
                )
            )
            n -= k
        if n:
            rows.append(hold_row(item, MOD, n, r, mkt, f"rank {r} copies: maxing isn't worth it" if v is None or v <= 0 else "hold (thin market)"))

    # 3. unranked copies
    n0 = s.get(0, 0)
    if n0:
        v0 = mkt.unit_sale_value(item, 0)
        vm, endo = max_then_sell_value(keep, 0, mkt) if m > 0 else (None, 0)
        cap0 = mkt.cap(item, 0)
        opts = []
        if v0 is not None and v0 > 0:
            opts.append(("sell", v0))
        if vm is not None and vm > 0:
            opts.append(("max", vm))
        opts.sort(key=lambda o: -o[1])
        left = n0
        cmp = ""
        if v0 is not None and vm is not None:
            cmp = f"unranked nets {v0:.0f}p/copy, maxing ({endo:,} Endo) nets {vm:.0f}p/copy"
        for kind, _ in opts:
            if not left:
                break
            if kind == "sell":
                k = min(left, cap0)
                rows.append(sale_row(item, MOD, k, 0, mkt, action=LIST, note=cmp or "sell unranked"))
            else:
                k = min(left, cap_m_left)
                if not k:
                    continue
                cap_m_left -= k
                rows.append(
                    sale_row(
                        item, MOD, k, m, mkt, action=MAX, extra_cost=k * endo_plat(endo, mkt),
                        note=f"max from rank 0 ({endo:,} Endo ≈ {endo_plat(endo, mkt):.0f}p each), then list; {cmp}".rstrip("; "),
                    )
                )
            left -= k
        if left:
            if not opts:
                reason = "no market" if no_market(0) and (m == 0 or no_market(m)) else "not worth a trade (price below trade/Endo cost)"
            else:
                reason = "hold (thin market)"
            rows.append(hold_row(item, MOD, left, 0, mkt, reason))

    for r in rows:
        if equipped_note and r.action != HOLD:
            r.notes.append(equipped_note)
        r.notes.append(keep.status)
    return rows

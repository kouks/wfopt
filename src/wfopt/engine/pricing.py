"""Pure price maths: stats -> med7/vol7, order book -> ask/bid, strategy -> suggested list price."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from wfopt.config import PricingCfg

STATUS_RANK = {"ingame": 0, "online": 1}


@dataclass
class StatRow:
    when: datetime
    volume: int
    median: float
    rank: int | None = None
    subtype: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "StatRow":
        when = datetime.fromisoformat(d["datetime"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return cls(when, int(d.get("volume") or 0), float(d.get("median") or 0), d.get("mod_rank"), d.get("subtype"))


@dataclass
class Order:
    type: str  # sell | buy
    platinum: int
    quantity: int
    rank: int | None
    status: str
    ingame_name: str
    per_trade: int = 1

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Order":
        u = d.get("user") or {}
        return cls(
            d.get("type", ""),
            int(d.get("platinum") or 0),
            int(d.get("quantity") or 0),
            d.get("rank"),
            u.get("status", ""),
            u.get("ingameName", ""),
            int(d.get("perTrade") or 1),
        )


@dataclass
class PriceQuote:
    slug: str
    rank: int | None
    med7: float | None = None
    vol7: float = 0.0
    ask: int | None = None
    ask_user: str | None = None
    bid: int | None = None
    bid_user: str | None = None
    stats_ok: bool = False
    orderbook_ok: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def has_market(self) -> bool:
        return self.med7 is not None or self.ask is not None or self.bid is not None


def filter_rows(rows: list[StatRow], rank: int | None, subtype: str | None) -> list[StatRow]:
    """Rows for one rank. Unranked items accept rows with no rank or rank 0."""
    out = []
    for r in rows:
        if rank is None:
            if r.rank not in (None, 0):
                continue
        elif (r.rank or 0) != rank:
            continue
        if subtype and r.subtype not in (None, subtype):
            continue
        out.append(r)
    return out


def _vw_median(rows: list[StatRow]) -> float | None:
    tot = sum(r.volume for r in rows)
    if tot > 0:
        return sum(r.median * r.volume for r in rows) / tot
    return None


def med_vol_7d(
    days90: list[StatRow], hours48: list[StatRow], now: datetime | None = None
) -> tuple[float | None, float, str | None]:
    """(med7, vol7, source). med7 = volume-weighted mean of daily medians over the last 7 days."""
    now = now or datetime.now(timezone.utc)
    recent = [r for r in days90 if r.when >= now - timedelta(days=7)]
    med = _vw_median(recent)
    if med is not None:
        return med, sum(r.volume for r in recent) / 7, "90days"
    recent48 = [r for r in hours48 if r.when >= now - timedelta(hours=48)]
    med = _vw_median(recent48)
    if med is not None:
        return med, sum(r.volume for r in recent48) / 2, "48hours"
    return None, 0.0, None


def best_ask(sells: list[Order], med7: float | None, floor: float, exclude_user: str = "") -> Order | None:
    """Lowest sell among in-game sellers, falling back to online. Ignores troll/mistake prices."""
    ex = exclude_user.lower()
    cands = [
        o
        for o in sells
        if o.status in STATUS_RANK
        and o.ingame_name.lower() != ex
        and o.platinum > 0
        and (med7 is None or o.platinum >= floor * med7)
    ]
    for status in ("ingame", "online"):
        group = [o for o in cands if o.status == status]
        if group:
            return min(group, key=lambda o: o.platinum)
    return None


def best_bid(buys: list[Order], exclude_user: str = "") -> Order | None:
    ex = exclude_user.lower()
    cands = [o for o in buys if o.status in STATUS_RANK and o.ingame_name.lower() != ex and o.platinum > 0]
    return max(cands, key=lambda o: (o.platinum, -STATUS_RANK[o.status])) if cands else None


def build_quote(
    slug: str,
    rank: int | None,
    *,
    stat_rows: tuple[list[StatRow], list[StatRow]] | None,
    orders: tuple[list[Order], list[Order]] | None,
    cfg: PricingCfg,
    own_name: str = "",
    subtype: str | None = None,
    now: datetime | None = None,
) -> PriceQuote:
    q = PriceQuote(slug, rank)
    if stat_rows is not None:
        d90, h48 = (filter_rows(x, rank, subtype) for x in stat_rows)
        q.med7, q.vol7, src = med_vol_7d(d90, h48, now)
        q.stats_ok = True
        if src == "48hours":
            q.flags.append("median from 48h data")
    else:
        q.flags.append("statistics unavailable: order book only")
    if orders is not None:
        sells, buys = orders
        a = best_ask(sells, q.med7, cfg.outlier_floor, own_name)
        b = best_bid(buys, own_name)
        q.orderbook_ok = True
        if a:
            q.ask, q.ask_user = a.platinum, a.ingame_name
        if b:
            q.bid, q.bid_user = b.platinum, b.ingame_name
    return q


def suggested_price(q: PriceQuote, cfg: PricingCfg, strategy: str | None = None) -> int | None:
    """List price for one unit under the chosen strategy, or None if there is no market."""
    strategy = strategy or cfg.strategy
    med, ask, bid = q.med7, q.ask, q.bid
    if med is None and ask is None and bid is None:
        return None
    if med is None:
        return ask if ask is not None else bid
    if ask is None:
        return max(math.ceil(med), bid or 0)
    if strategy == "quick":
        p = max(bid or 0, min(ask - 1, round(med)))
    elif strategy == "patient":
        p = max(math.ceil(med), ask)
    else:
        p = max(ask - cfg.undercut, max(bid or 0, math.ceil(0.85 * med)))
    return max(1, int(p))

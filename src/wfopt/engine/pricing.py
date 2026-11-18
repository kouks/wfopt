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
            max(1, int(d.get("perTrade") or 1)),
        )

    @property
    def unit(self) -> float:
        """Plat per single unit. `platinum` is the price of a `perTrade` bundle."""
        return self.platinum / self.per_trade


@dataclass
class PriceQuote:
    slug: str
    rank: int | None
    med7: float | None = None
    vol7: float = 0.0
    ask: float | None = None  # per unit
    ask_user: str | None = None
    bid: float | None = None  # per unit
    bid_user: str | None = None
    bid_qty: int = 0  # units the top buyer still wants (WFM quantity, read as units)
    bid_per_trade: int = 1
    bid_platinum: int | None = None  # the buyer's price for one perTrade bundle
    stats_ok: bool = False
    orderbook_ok: bool = False
    median_suspect: bool = False  # median looks like fake sales; live asks set the price
    ask_far_above: bool = False  # cheapest ask is far above the median; the median sets the price
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
        and (med7 is None or o.unit >= floor * med7)
    ]
    for status in ("ingame", "online"):
        group = [o for o in cands if o.status == status]
        if group:
            return min(group, key=lambda o: o.unit)
    return None


def best_bid(buys: list[Order], exclude_user: str = "") -> Order | None:
    ex = exclude_user.lower()
    cands = [o for o in buys if o.status in STATUS_RANK and o.ingame_name.lower() != ex and o.platinum > 0]
    return max(cands, key=lambda o: (o.unit, -STATUS_RANK[o.status])) if cands else None


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
    owned: int = 0,
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
            q.ask, q.ask_user = round(a.unit, 2), a.ingame_name
        if b:
            q.bid, q.bid_user = round(b.unit, 2), b.ingame_name
            q.bid_qty, q.bid_per_trade, q.bid_platinum = b.quantity, b.per_trade, b.platinum
        check_median(q, sells, cfg, owned, own_name)
        if (
            not q.median_suspect and q.med7 and q.ask is not None
            and cfg.max_ask_over_median > 0 and q.ask > cfg.max_ask_over_median * q.med7
        ):
            q.ask_far_above = True
            q.flags.append(
                f"cheapest seller asks {q.ask:g}p, far above the 7-day median {q.med7:.0f}p: pricing from the median"
            )
    return q


def check_median(q: PriceQuote, sells: list[Order], cfg: PricingCfg, owned: int, own_name: str = "") -> None:
    """Fake-sales guard. A common item (many copies owned) whose live sellers sit far below the
    7-day median was "sold" for show: trust the cheapest live sellers instead of the median.

    Normally asks under `outlier_floor × median` are dropped as trolls; here they are the real market.
    """
    if q.med7 is None or owned < cfg.suspect_median_min_copies:
        return
    raw = best_ask(sells, None, cfg.outlier_floor, own_name)  # no troll floor
    if raw is None or raw.unit >= cfg.suspect_median_ratio * q.med7:
        return
    q.median_suspect = True
    q.ask, q.ask_user = round(raw.unit, 2), raw.ingame_name
    q.flags.append(
        f"7-day median {q.med7:.0f}p looks like fake sales: you own {owned} copies and live sellers start at "
        f"{raw.unit:g}p, so the price follows live sellers"
    )


def suggested_price(q: PriceQuote, cfg: PricingCfg, strategy: str | None = None) -> int | None:
    """List price for one unit under the chosen strategy, or None if there is no market."""
    strategy = strategy or cfg.strategy
    med, ask, bid = (None if q.median_suspect else q.med7), (None if q.ask_far_above else q.ask), q.bid
    if med is None and ask is None and bid is None:
        return None
    if med is None:
        return max(1, math.floor(ask if ask is not None else bid))
    if ask is None:
        return max(math.ceil(med), math.floor(bid or 0))
    ask_i, bid_i = math.ceil(ask), math.floor(bid or 0)
    if strategy == "quick":
        p = max(bid_i, min(ask_i - 1, round(med)))
    elif strategy == "patient":
        p = max(math.ceil(med), ask_i)
    else:
        p = max(ask_i - cfg.undercut, max(bid_i, math.ceil(0.85 * med)))
    return max(1, int(p))

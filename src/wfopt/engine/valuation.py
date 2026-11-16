"""Shared valuation helpers: a pure view over quotes, liquidity caps, and sale-row construction."""

from __future__ import annotations

import math
from dataclasses import dataclass

from wfopt.config import Config
from wfopt.engine.model import HOLD, LIST, SELL_TOP, Row
from wfopt.engine.pricing import PriceQuote, suggested_price
from wfopt.market.catalog import CatalogItem


def listable_qty(q: PriceQuote | None, cfg: Config) -> int:
    """How many copies the market can absorb over the horizon: max(1, ceil(vol7 × horizon × share))."""
    vol = q.vol7 if q else 0.0
    v = cfg.valuation
    return max(1, math.ceil(vol * v.horizon_days * v.share))


@dataclass
class Market:
    quotes: dict[tuple[str, int | None], PriceQuote]
    cfg: Config
    strategy: str | None = None

    def quote(self, item: CatalogItem, rank: int | None) -> PriceQuote | None:
        return self.quotes.get((item.id, rank))

    def price(self, item: CatalogItem, rank: int | None) -> int | None:
        q = self.quote(item, rank)
        return suggested_price(q, self.cfg.pricing, self.strategy) if q else None

    def cap(self, item: CatalogItem, rank: int | None) -> int:
        return listable_qty(self.quote(item, rank), self.cfg)

    @property
    def tc(self) -> float:
        return self.cfg.valuation.trade_cost_plat

    def bulk(self, item: CatalogItem, rank: int | None = None) -> int:
        """Units one buyer takes per trade. Only unranked stacks of bulk-tradable items go in bulk."""
        if item.bulk_tradable and not rank:
            return max(1, self.cfg.valuation.bulk_units_per_trade)
        return 1

    def unit_sale_value(self, item: CatalogItem, rank: int | None) -> float | None:
        """Net plat per unit sold: price − trade_cost / units_per_trade. None without a market."""
        p = self.price(item, rank)
        if p is None:
            return None
        return p - self.tc / self.bulk(item, rank)

    def trades_for(self, item: CatalogItem, qty: int, rank: int | None = None) -> int:
        return math.ceil(qty / self.bulk(item, rank)) if qty > 0 else 0


def whisper(name: str, item_name: str, rank: int | None, plat: int) -> str:
    r = f" (rank {rank})" if rank is not None else ""
    return f'/w {name} Hi! I want to sell: "{item_name}"{r} for {plat} platinum. (warframe.market)'


def base_row(item: CatalogItem, category: str, action: str, qty: int, rank: int | None, mkt: Market) -> Row:
    q = mkt.quote(item, rank)
    return Row(
        item_id=item.id,
        slug=item.slug,
        name=item.name,
        category=category,
        action=action,
        qty=qty,
        rank=rank,
        ask=q.ask if q else None,
        bid=q.bid if q else None,
        bid_user=q.bid_user if q else None,
        med7=q.med7 if q else None,
        vol7=q.vol7 if q else 0.0,
        url=item.url,
        max_rank=item.max_rank,
        notes=list(q.flags) if q else [],
    )


def sale_row(
    item: CatalogItem,
    category: str,
    qty: int,
    rank: int | None,
    mkt: Market,
    *,
    action: str = LIST,
    extra_cost: float = 0.0,
    note: str = "",
) -> Row:
    """A List / Fuse / Max row; becomes 'Sell to top buyer' when a live bid meets the list price."""
    price = mkt.price(item, rank) or 0
    r = base_row(item, category, action, qty, rank, mkt)
    r.price = price
    if action == LIST and r.bid is not None and r.bid >= price and r.bid_user:
        r.action = SELL_TOP
        r.price = r.bid
        r.notes.insert(0, f"top buyer {r.bid_user} already pays {r.bid}p, at or above the list price")
    r.trades = mkt.trades_for(item, qty, rank)
    r.net = qty * r.price - r.trades * mkt.tc - extra_cost
    if r.bid is not None and r.bid_user:
        r.whisper = whisper(r.bid_user, item.name, rank if item.max_rank is not None else None, r.bid)
    if note:
        r.notes.insert(0, note)
    return r


def hold_row(item: CatalogItem, category: str, qty: int, rank: int | None, mkt: Market, note: str) -> Row:
    r = base_row(item, category, HOLD, qty, rank, mkt)
    r.price = mkt.price(item, rank)
    r.notes.insert(0, note)
    return r

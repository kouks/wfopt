"""Price sources behind interfaces (v1 statistics is deprecated and may go away)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from wfopt.config import Config
from wfopt.engine.pricing import Order, PriceQuote, StatRow, build_quote
from wfopt.market.catalog import CatalogItem
from wfopt.market.client import WfmClient, WfmError, v2_data


@dataclass(frozen=True)
class PriceNeed:
    item: CatalogItem
    rank: int | None
    units: int  # surplus units this quote would price, for deciding whether the order book is worth a call
    subtype: str | None = None

    @property
    def key(self) -> tuple[str, int | None]:
        return (self.item.id, self.rank)


class StatsSource(Protocol):
    def history(self, item: CatalogItem, refresh: bool = False) -> tuple[list[StatRow], list[StatRow]] | None:
        """(90-day daily rows, 48-hour hourly rows), all ranks, or None if unavailable."""


class OrderBookSource(Protocol):
    def top(
        self, item: CatalogItem, rank: int | None, subtype: str | None, refresh: bool = False
    ) -> tuple[list[Order], list[Order]] | None:
        """(sell orders, buy orders) for one rank, or None if unavailable."""


class V1StatsSource:
    def __init__(self, client: WfmClient, ttl_minutes: float):
        self.client, self.ttl = client, ttl_minutes
        self._mem: dict[str, tuple[list[StatRow], list[StatRow]] | None] = {}

    def history(self, item: CatalogItem, refresh: bool = False):
        if item.slug in self._mem and not refresh:
            return self._mem[item.slug]
        try:
            body = self.client.get_json(f"/v1/items/{item.slug}/statistics", ttl_minutes=self.ttl, refresh=refresh)
            closed = (body or {}).get("payload", {}).get("statistics_closed") or {}
            res = (
                [StatRow.from_api(r) for r in closed.get("90days", [])],
                [StatRow.from_api(r) for r in closed.get("48hours", [])],
            )
        except (WfmError, ValueError, KeyError):
            res = None
        self._mem[item.slug] = res
        return res


class TopOrdersSource:
    def __init__(self, client: WfmClient, ttl_minutes: float):
        self.client, self.ttl = client, ttl_minutes

    def top(self, item: CatalogItem, rank: int | None, subtype: str | None, refresh: bool = False):
        params: dict[str, object] = {}
        if rank is not None and item.max_rank is not None:
            params["rank"] = rank
        if subtype:
            params["subtype"] = subtype
        try:
            data = v2_data(
                self.client.get_json(f"/v2/orders/item/{item.slug}/top", params=params or None, ttl_minutes=self.ttl, refresh=refresh)
            ) or {}
        except (WfmError, ValueError):
            return None
        sells = [Order.from_api(o) for o in data.get("sell", [])]
        buys = [Order.from_api(o) for o in data.get("buy", [])]
        if rank is not None and item.max_rank is not None:
            sells = [o for o in sells if (o.rank or 0) == rank]
            buys = [o for o in buys if (o.rank or 0) == rank]
        return sells, buys


def worth_orderbook(med7: float | None, units: int, min_plat: float) -> bool:
    """Phase-2 gate: fetch the live order book only if a few units are worth at least `min_plat`."""
    if med7 is None:
        return True
    return med7 * min(max(units, 1), 3) >= min_plat


def fetch_quotes(
    cfg: Config,
    needs: list[PriceNeed],
    stats: StatsSource,
    book: OrderBookSource,
    *,
    refresh: bool = False,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[tuple[str, int | None], PriceQuote]:
    """Two phases: statistics for every item, then order books for items worth a closer look."""
    own = cfg.wfm.ingame_name
    hist: dict[str, tuple[list[StatRow], list[StatRow]] | None] = {}
    for n in needs:
        if n.item.id not in hist:
            hist[n.item.id] = stats.history(n.item, refresh=refresh)
            if progress:
                progress("stats")
    quotes: dict[tuple[str, int | None], PriceQuote] = {}
    for n in needs:
        q = build_quote(n.item.slug, n.rank, stat_rows=hist[n.item.id], orders=None, cfg=cfg.pricing, subtype=n.subtype, now=now)
        if worth_orderbook(q.med7, n.units, cfg.pricing.orderbook_min_plat):
            orders = book.top(n.item, n.rank, n.subtype, refresh=refresh)
            q = build_quote(
                n.item.slug, n.rank, stat_rows=hist[n.item.id], orders=orders, cfg=cfg.pricing,
                own_name=own, subtype=n.subtype, now=now,
            )
            if orders is None:
                q.flags.append("order book unavailable")
        else:
            q.flags.append("cheap: order book not fetched")
        quotes[n.key] = q
        if progress:
            progress("book")
    return quotes

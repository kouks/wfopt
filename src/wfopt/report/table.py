"""Report model and the column layout shared by the terminal, CSV and HTML outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from wfopt.config import Config
from wfopt.engine.classify import ARCANE, Classified
from wfopt.engine.keep import KeepResult
from wfopt.engine.model import DUCATS, HOLD, TRADE_ACTIONS, Row
from wfopt.engine.plan import DailyPlan
from wfopt.engine.pricing import PriceQuote


def _num(v: float | None, digits: int = 0) -> str:
    if v is None:
        return ""
    return f"{v:.{digits}f}"


# (header, key, getter). `key` is used by the HTML for sorting/typing.
COLUMNS: list[tuple[str, str, Callable[[Row], Any]]] = [
    ("Priority", "priority", lambda r: round(r.priority, 2)),
    ("Item", "name", lambda r: r.name),
    ("Category", "category", lambda r: r.category),
    ("Action", "action", lambda r: r.action),
    ("Qty", "qty", lambda r: r.qty),
    ("Rank", "rank", lambda r: "" if r.rank is None else r.rank),
    ("Price", "price", lambda r: "" if r.price is None else f"{r.price:g}"),
    ("Ask (in game)", "ask", lambda r: "" if r.ask is None else f"{r.ask:g}"),
    ("Bid", "bid", lambda r: "" if r.bid is None else f"{r.bid:g}"),
    ("7d median", "med7", lambda r: _num(r.med7, 1)),
    ("7d vol/day", "vol7", lambda r: _num(r.vol7, 1)),
    ("Liquidity", "liquidity", lambda r: r.liquidity),
    ("Net value", "net", lambda r: round(r.net, 1)),
    ("Plat/trade", "npt", lambda r: round(r.net_per_trade, 1) if r.action in TRADE_ACTIONS else ""),
    ("Trades", "trades", lambda r: r.trades),
    ("Ducats", "ducats", lambda r: r.ducats or ""),
    ("Your listing", "listing", lambda r: r.current_listing),
    ("WFM link", "url", lambda r: r.url),
    ("Notes", "notes", lambda r: r.note),
    ("Whisper top buyer", "whisper", lambda r: r.whisper),
]

NUMERIC = {"priority", "qty", "rank", "price", "ask", "bid", "med7", "vol7", "net", "npt", "trades", "ducats"}


def row_values(r: Row) -> list[Any]:
    return [g(r) for _, _, g in COLUMNS]


def annotate_listings(rows: list[Row], listings: list[dict]) -> None:
    by_key: dict[tuple[str, int | None], list[dict]] = {}
    for o in listings:
        by_key.setdefault((o.get("itemId", ""), o.get("rank")), []).append(o)
    for r in rows:
        rank = r.rank if r.max_rank is not None else None
        found = by_key.get((r.item_id, rank)) or (by_key.get((r.item_id, None)) if rank in (None, 0) else None)
        if found:
            r.current_listing = ", ".join(
                f"{o.get('platinum')}p × {o.get('quantity')}{'' if o.get('visible', True) else ' (hidden)'}" for o in found
            )


@dataclass
class Report:
    generated_at: datetime
    strategy: str
    cfg: Config
    inventory_path: str
    inventory_age_hours: float
    mastery_rank: int | None
    trades_remaining: int | None
    rows: list[Row]
    plan: DailyPlan
    keeps: list[KeepResult]
    classified: Classified
    quotes: dict[tuple[str, int | None], PriceQuote]
    listings: list[dict] | None = None
    network_calls: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def trade_rows(self) -> list[Row]:
        return [r for r in self.rows if r.action in TRADE_ACTIONS]

    def summary(self) -> dict[str, Any]:
        tr = self.trade_rows
        duc = [r for r in self.rows if r.action == DUCATS]
        return {
            "Expected plat (all trade rows)": sum(r.qty * (r.price or 0) for r in tr),
            "Net value (trade rows)": round(sum(r.net for r in tr), 1),
            "Trades needed": sum(r.trades for r in tr),
            "Ducats (kiosk rows)": sum(r.ducats for r in duc),
            "Rows held": sum(1 for r in self.rows if r.action == HOLD),
            "Daily trade budget": self.plan.budget,
            "Trades remaining today (from inventory)": self.trades_remaining,
            "Today's plan: plat / trades": f"{self.plan.plat:.0f}p / {self.plan.trades}",
            "Inventory file age (h)": round(self.inventory_age_hours, 1),
            "Strategy": self.strategy,
        }

    def kept_rows(self) -> list[dict[str, Any]]:
        out = []
        for k in self.keeps:
            if k.item.category not in ("arcane", "mod"):
                continue
            ranks = ", ".join(f"{n}× r{r}" for r, n in sorted(k.kept.items(), key=lambda x: -(x[0] or 0)) if n)
            out.append(
                {
                    "name": k.item.item.name,
                    "category": k.item.category,
                    "kept": ranks,
                    "status": k.status,
                    "short": k.keeper_short is not None,
                    "url": k.item.item.url,
                }
            )
        out.sort(key=lambda d: (not d["short"], d["category"] != ARCANE, d["name"]))
        return out

    def no_market_rows(self) -> list[Row]:
        return [r for r in self.rows if r.action == HOLD and r.med7 is None and r.ask is None and r.bid is None]

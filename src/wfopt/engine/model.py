"""Engine output model: one Row per (item, action, rank) recommendation."""

from __future__ import annotations

from dataclasses import dataclass, field

LIST = "List"
SELL_TOP = "Sell to top buyer"
FUSE = "Fuse then list"
MAX = "Max then list"
DUCATS = "Ducats"
HOLD = "Hold"

TRADE_ACTIONS = (LIST, SELL_TOP, FUSE, MAX)


@dataclass
class Row:
    item_id: str
    slug: str
    name: str
    category: str
    action: str
    qty: int
    rank: int | None = None
    price: float | None = None  # suggested unit price (or the bid's unit price for Sell to top buyer)
    ask: float | None = None
    bid: float | None = None
    bid_user: str | None = None
    med7: float | None = None
    vol7: float = 0.0
    net: float = 0.0
    trades: int = 0
    ducats: int = 0
    endo: int = 0  # Endo needed (Max then list)
    url: str = ""
    notes: list[str] = field(default_factory=list)
    whisper: str = ""
    # filled by plan.score
    liquidity: str = ""
    liquidity_factor: float = 0.0
    priority: float = 0.0
    # filled by the report when the player has a live listing
    current_listing: str = ""
    # sync helpers
    max_rank: int | None = None
    bulk_tradable: bool = False

    @property
    def net_per_trade(self) -> float:
        return self.net / max(self.trades, 1)

    @property
    def note(self) -> str:
        return " ".join(n.rstrip(".") + "." for n in self.notes if n)

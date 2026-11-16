"""Compare desired sell listings (from report rows) with the player's WFM orders, and apply changes."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from wfopt.config import Config
from wfopt.engine.model import FUSE, LIST, MAX, Row
from wfopt.market.client import WfmClient, WfmError, v2_data

SYNC_ACTIONS = (LIST, FUSE, MAX)

CREATE, UPDATE, DELETE, SKIP = "create", "update", "delete", "skip"


WFM_MAX_ORDERS = 100  # warframe.market's cap on open orders per account


@dataclass
class Desired:
    item_id: str
    slug: str
    name: str
    rank: int | None  # only for items with maxRank
    qty: int
    price: int
    subtype: str | None = None
    priority: float = 0.0
    per_trade: int | None = None  # WFM requires perTrade for bulk-tradable items and rejects it otherwise


@dataclass
class Op:
    kind: str
    item_id: str
    name: str
    rank: int | None
    order_id: str | None = None
    price: int | None = None
    qty: int | None = None
    old_price: int | None = None
    old_qty: int | None = None
    subtype: str | None = None
    reason: str = ""
    per_trade: int | None = None

    def describe(self) -> str:
        r = f" r{self.rank}" if self.rank is not None else ""
        if self.kind == CREATE:
            return f"create {self.name}{r}: {self.qty} × {self.price}p"
        if self.kind == UPDATE:
            return f"update {self.name}{r}: {self.old_qty} × {self.old_price}p -> {self.qty} × {self.price}p"
        if self.kind == DELETE:
            return f"delete {self.name}{r}: {self.old_qty} × {self.old_price}p ({self.reason})"
        return f"skip {self.name}{r}: {self.reason}"


@dataclass
class SyncState:
    managed: dict[str, dict[str, Any]] = field(default_factory=dict)  # order id -> {item_id, rank}

    @classmethod
    def load(cls, path: Path) -> "SyncState":
        if path.exists():
            try:
                return cls(json.loads(path.read_text(encoding="utf-8")).get("managed", {}))
            except (OSError, ValueError):
                pass
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"managed": self.managed}, indent=1), encoding="utf-8")


def state_path(cfg: Config) -> Path:
    return cfg.data_dir / "sync-state.json"


def desired_from_rows(rows: list[Row], subtypes: dict[str, str | None] | None = None) -> dict[tuple[str, int | None], Desired]:
    """Aggregate List / Fuse / Max rows by (itemId, rank), highest priority first.

    Quantities are already capped by the engine.
    """
    out: dict[tuple[str, int | None], Desired] = {}
    for r in sorted(rows, key=lambda r: -r.priority):
        if r.action not in SYNC_ACTIONS or not r.price or r.qty <= 0:
            continue
        rank = r.rank if r.max_rank is not None else None
        key = (r.item_id, rank)
        if key in out:
            out[key].qty += r.qty
            out[key].price = max(out[key].price, int(r.price))
        else:
            out[key] = Desired(
                r.item_id, r.slug, r.name, rank, r.qty, int(r.price), (subtypes or {}).get(r.item_id), r.priority,
                1 if r.bulk_tradable else None,
            )
    return out


def price_off(old: int, new: int) -> bool:
    return abs(old - new) > max(2, 0.05 * new)


def is_managed(o: dict[str, Any], state: SyncState, manage_only_tool_orders: bool = True) -> bool:
    """Sell orders the tool may change. Buy orders and (by default) manual listings are never touched."""
    return o.get("type") == "sell" and (not manage_only_tool_orders or o.get("id") in state.managed)


def diff(
    desired: dict[tuple[str, int | None], Desired],
    orders: list[dict[str, Any]],
    state: SyncState,
    manage_only_tool_orders: bool = True,
    names: dict[str, str] | None = None,
    limit: int = WFM_MAX_ORDERS,
) -> list[Op]:
    """Ops that bring the tool's listings in line with the top `limit` recommendations.

    Orders the tool doesn't manage (manual sell orders, buy orders) stay and use up slots first.
    Deletes come first in the result so the account never goes over the cap while applying.
    """
    names = names or {}
    limit = max(0, min(limit, WFM_MAX_ORDERS))
    sells = [o for o in orders if o.get("type") == "sell"]
    by_key: dict[tuple[str, int | None], list[dict]] = {}
    for o in sells:
        by_key.setdefault((o.get("itemId", ""), o.get("rank")), []).append(o)

    def managed(o: dict) -> bool:
        return is_managed(o, state, manage_only_tool_orders)

    fixed = [o for o in orders if not managed(o)]  # stays no matter what
    slots = limit - len(fixed)

    creates: list[Op] = []
    updates: list[Op] = []
    deletes: list[Op] = []
    skips: list[Op] = []
    kept: set[str] = set()
    for key, d in sorted(desired.items(), key=lambda kv: -kv[1].priority):
        existing = by_key.get(key, [])
        mine = [o for o in existing if managed(o)]
        if existing and not mine:
            o = existing[0]
            skips.append(
                Op(SKIP, d.item_id, d.name, d.rank, o.get("id"), d.price, d.qty, o.get("platinum"), o.get("quantity"),
                   reason=f"you already list it manually at {o.get('platinum')}p × {o.get('quantity')}; not touching it")
            )
            continue
        if slots <= 0:
            if not mine:
                skips.append(Op(SKIP, d.item_id, d.name, d.rank, price=d.price, qty=d.qty,
                                reason=f"outside the top {limit} listings"))
            continue  # an existing tool listing here is deleted below
        slots -= 1
        if not mine:
            creates.append(
                Op(CREATE, d.item_id, d.name, d.rank, price=d.price, qty=d.qty, subtype=d.subtype, per_trade=d.per_trade)
            )
            continue
        o = mine[0]
        kept.add(o["id"])
        op_, oq = int(o.get("platinum") or 0), int(o.get("quantity") or 0)
        if price_off(op_, d.price) or oq != d.qty:
            updates.append(Op(UPDATE, d.item_id, d.name, d.rank, o["id"], d.price, d.qty, op_, oq))

    for o in sells:
        if not managed(o) or o.get("id") in kept:
            continue
        key = (o.get("itemId", ""), o.get("rank"))
        if key not in desired:
            reason = "no surplus left"
        elif any(k.get("id") in kept for k in by_key.get(key, [])):
            reason = "duplicate tool listing"
        else:
            reason = f"outside the top {limit} listings"
        deletes.append(
            Op(DELETE, o.get("itemId", ""), names.get(o.get("itemId", ""), o.get("itemId", "")), o.get("rank"), o.get("id"),
               old_price=o.get("platinum"), old_qty=o.get("quantity"), reason=reason)
        )
    return deletes + updates + creates + skips


# -- drafts: the dry run saves its ops; --yes applies exactly those -------------------------------


@dataclass
class Draft:
    created_at: float
    user_id: str
    limit: int
    strategy: str
    orders_fingerprint: list[list[Any]]
    ops: list[Op]

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.created_at) / 60


def draft_path(cfg: Config) -> Path:
    return cfg.data_dir / "sync-draft.json"


def orders_fingerprint(orders: list[dict[str, Any]]) -> list[list[Any]]:
    """What must not change between the dry run and --yes for the draft to stay valid."""
    return sorted(
        [str(o.get("id")), o.get("type"), o.get("platinum"), o.get("quantity"), o.get("rank"), o.get("visible", True)]
        for o in orders
    )


def save_draft(cfg: Config, ops: list[Op], orders: list[dict[str, Any]], user_id: str, limit: int, strategy: str) -> Draft:
    d = Draft(time.time(), user_id, limit, strategy, orders_fingerprint(orders), ops)
    p = draft_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({**asdict(d), "ops": [asdict(o) for o in ops]}, indent=1), encoding="utf-8")
    return d


def load_draft(cfg: Config) -> Draft | None:
    p = draft_path(cfg)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return Draft(d["created_at"], d["user_id"], d["limit"], d["strategy"], d["orders_fingerprint"], [Op(**o) for o in d["ops"]])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def discard_draft(cfg: Config) -> None:
    draft_path(cfg).unlink(missing_ok=True)


def draft_problem(
    draft: Draft | None,
    cfg: Config,
    orders: list[dict[str, Any]],
    user_id: str,
    limit: int | None = None,
    strategy: str | None = None,
) -> str | None:
    """Why the saved draft can't be applied as-is, or None if it can."""
    if draft is None:
        return "no draft found"
    max_age = cfg.sync.draft_max_age_minutes
    if draft.age_minutes > max_age:
        return f"the draft is {draft.age_minutes:.0f} min old (limit {max_age:g} min, sync.draft_max_age_minutes)"
    if draft.user_id != user_id:
        return "the draft was made for a different account"
    if limit is not None and limit != draft.limit:
        return f"the draft was made with --limit {draft.limit}"
    if strategy is not None and strategy != draft.strategy:
        return f"the draft was made with --strategy {draft.strategy}"
    if orders_fingerprint(orders) != draft.orders_fingerprint:
        return "your listings on warframe.market changed since the draft"
    return None


def fetch_my_orders(client: WfmClient) -> list[dict[str, Any]]:
    resp = client.request("GET", "/v2/orders/my", auth=True)
    if resp.status_code >= 400:
        raise WfmError(f"GET /v2/orders/my -> {resp.status_code}", resp.status_code, resp.text[:500])
    return v2_data(resp.json()) or []


def apply_ops(
    cfg: Config,
    client: WfmClient,
    ops: list[Op],
    state: SyncState,
    log_path: Path,
    echo: Callable[[str], None] = print,
) -> int:
    """Execute writes in order. Stops at the first 4xx/5xx and prints the error body. Returns writes done."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    for op in ops:
        if op.kind == SKIP:
            continue
        if op.kind == CREATE:
            body: dict[str, Any] = {
                "itemId": op.item_id, "type": "sell", "platinum": op.price, "quantity": op.qty,
                "visible": cfg.sync.post_visible,
            }
            if op.per_trade:  # bulk-tradable only; 1 = buyers can take single copies at our per-unit price
                body["perTrade"] = op.per_trade
            if op.rank is not None:
                body["rank"] = op.rank
            if op.subtype:
                body["subtype"] = op.subtype
            resp = client.request("POST", "/v2/order", json_body=body, auth=True)
        elif op.kind == UPDATE:
            body = {"platinum": op.price, "quantity": op.qty}
            if op.rank is not None:
                body["rank"] = op.rank
            resp = client.request("PATCH", f"/v2/order/{op.order_id}", json_body=body, auth=True)
        else:
            body = {}
            resp = client.request("DELETE", f"/v2/order/{op.order_id}", auth=True)

        entry = {"ts": time.time(), "op": asdict(op), "request": body, "status": resp.status_code}
        if resp.status_code >= 400:
            entry["error"] = resp.text[:1000]
            _log(log_path, entry)
            echo(f"Stopped: {op.describe()} -> HTTP {resp.status_code}: {resp.text[:1000]}")
            state.save(state_path(cfg))
            return done
        if op.kind == CREATE:
            try:
                oid = (v2_data(resp.json()) or {}).get("id")
            except ValueError:
                oid = None
            if oid:
                state.managed[oid] = {"item_id": op.item_id, "rank": op.rank}
                entry["order_id"] = oid
        elif op.kind == DELETE:
            state.managed.pop(op.order_id or "", None)
        _log(log_path, entry)
        echo(f"ok: {op.describe()}")
        done += 1
    state.save(state_path(cfg))
    return done


def _log(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

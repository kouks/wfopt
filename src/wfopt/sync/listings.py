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


@dataclass
class Desired:
    item_id: str
    slug: str
    name: str
    rank: int | None  # only for items with maxRank
    qty: int
    price: int
    subtype: str | None = None


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
    """Aggregate List / Fuse / Max rows by (itemId, rank). Quantities are already capped by the engine."""
    out: dict[tuple[str, int | None], Desired] = {}
    for r in rows:
        if r.action not in SYNC_ACTIONS or not r.price or r.qty <= 0:
            continue
        rank = r.rank if r.max_rank is not None else None
        key = (r.item_id, rank)
        if key in out:
            out[key].qty += r.qty
            out[key].price = max(out[key].price, r.price)
        else:
            out[key] = Desired(r.item_id, r.slug, r.name, rank, r.qty, r.price, (subtypes or {}).get(r.item_id))
    return out


def price_off(old: int, new: int) -> bool:
    return abs(old - new) > max(2, 0.05 * new)


def diff(
    desired: dict[tuple[str, int | None], Desired],
    orders: list[dict[str, Any]],
    state: SyncState,
    manage_only_tool_orders: bool = True,
    names: dict[str, str] | None = None,
) -> list[Op]:
    names = names or {}
    sells = [o for o in orders if o.get("type") == "sell"]
    by_key: dict[tuple[str, int | None], list[dict]] = {}
    for o in sells:
        by_key.setdefault((o.get("itemId", ""), o.get("rank")), []).append(o)

    def managed(o: dict) -> bool:
        return not manage_only_tool_orders or o.get("id") in state.managed

    ops: list[Op] = []
    seen: set[str] = set()
    for key, d in sorted(desired.items(), key=lambda kv: kv[1].name):
        existing = by_key.get(key, [])
        seen.update(o["id"] for o in existing if "id" in o)
        mine = [o for o in existing if managed(o)]
        if not existing:
            ops.append(Op(CREATE, d.item_id, d.name, d.rank, price=d.price, qty=d.qty, subtype=d.subtype))
            continue
        if not mine:
            o = existing[0]
            ops.append(
                Op(SKIP, d.item_id, d.name, d.rank, o.get("id"), d.price, d.qty, o.get("platinum"), o.get("quantity"),
                   reason=f"you already list it manually at {o.get('platinum')}p × {o.get('quantity')}; not touching it")
            )
            continue
        o = mine[0]
        op_, oq = int(o.get("platinum") or 0), int(o.get("quantity") or 0)
        if price_off(op_, d.price) or oq != d.qty:
            ops.append(Op(UPDATE, d.item_id, d.name, d.rank, o["id"], d.price, d.qty, op_, oq))
        for extra in mine[1:]:
            ops.append(Op(DELETE, d.item_id, d.name, d.rank, extra["id"], old_price=extra.get("platinum"),
                          old_qty=extra.get("quantity"), reason="duplicate tool listing"))

    for o in sells:
        if o.get("id") in seen or not managed(o):
            continue
        ops.append(
            Op(DELETE, o.get("itemId", ""), names.get(o.get("itemId", ""), o.get("itemId", "")), o.get("rank"), o.get("id"),
               old_price=o.get("platinum"), old_qty=o.get("quantity"), reason="no surplus left")
        )
    return ops


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

import json

import httpx
import pytest
import respx

from wfopt.engine.model import FUSE, HOLD, LIST, MAX, SELL_TOP, Row
from wfopt.market.client import API, WfmClient
from wfopt.sync.auth import SIGNIN_URL, AuthError, authenticate
from wfopt.sync.listings import (
    CREATE, DELETE, SKIP, UPDATE, SyncState, apply_ops, desired_from_rows, diff, price_off, state_path,
)


def row(item_id, action, qty, price, rank=None, max_rank=None, priority=0.0):
    r = Row(item_id, item_id, item_id.title(), "mod", action, qty, rank, price, max_rank=max_rank)
    r.priority = priority
    return r


def test_desired_aggregates_and_filters():
    rows = [
        row("a", LIST, 1, 100, 5, 5),
        row("a", FUSE, 2, 100, 5, 5),
        row("b", MAX, 1, 50, 10, 10),
        row("c", HOLD, 9, 5),
        row("d", SELL_TOP, 1, 20),
        row("e", LIST, 3, 12),
    ]
    d = desired_from_rows(rows, {"b": "regular"})
    assert set(d) == {("a", 5), ("b", 10), ("e", None)}
    assert d[("a", 5)].qty == 3 and d[("b", 10)].subtype == "regular"


def test_price_threshold():
    assert not price_off(100, 104) and price_off(100, 106)
    assert not price_off(10, 12) and price_off(10, 13)


def test_diff_ops():
    desired = desired_from_rows([row("a", LIST, 2, 100, 5, 5), row("b", LIST, 1, 30), row("c", LIST, 1, 40)])
    orders = [
        {"id": "o1", "type": "sell", "itemId": "a", "rank": 5, "platinum": 120, "quantity": 2},  # tool, price off
        {"id": "o2", "type": "sell", "itemId": "b", "platinum": 31, "quantity": 1},  # manual -> skip
        {"id": "o3", "type": "sell", "itemId": "z", "platinum": 9, "quantity": 1},  # tool, no surplus -> delete
        {"id": "o4", "type": "sell", "itemId": "y", "platinum": 9, "quantity": 1},  # manual, untouched
        {"id": "o5", "type": "buy", "itemId": "c", "platinum": 1, "quantity": 1},  # buy orders ignored
    ]
    state = SyncState({"o1": {}, "o3": {}})
    ops = {(o.kind, o.item_id) for o in diff(desired, orders, state)}
    assert ops == {(UPDATE, "a"), (SKIP, "b"), (DELETE, "z"), (CREATE, "c")}
    # unmanaged mode: manual orders become fair game
    ops2 = {(o.kind, o.item_id) for o in diff(desired, orders, state, manage_only_tool_orders=False)}
    assert (DELETE, "y") in ops2 and (SKIP, "b") not in ops2


@respx.mock
def test_apply_writes_logs_and_stops_on_4xx(cfg):
    post = respx.post(f"{API}/v2/order").mock(
        side_effect=[httpx.Response(200, json={"data": {"id": "new1"}}), httpx.Response(400, json={"error": "bad rank"})]
    )
    delete = respx.delete(f"{API}/v2/order/o3").mock(return_value=httpx.Response(200, json={"data": None}))
    desired = desired_from_rows([row("a", LIST, 1, 100, 5, 5), row("b", LIST, 1, 30)])
    state = SyncState({"o3": {}})
    ops = diff(desired, [{"id": "o3", "type": "sell", "itemId": "z", "platinum": 9, "quantity": 1}], state)
    ops.sort(key=lambda o: o.kind != DELETE)  # delete first, then the two creates
    out = []
    with WfmClient(cfg) as c:
        c.token = "secret-token"
        n = apply_ops(cfg, c, ops, state, cfg.out_dir / "sync-log.jsonl", echo=out.append)
    assert n == 2 and delete.called and post.call_count == 2
    sent = json.loads(post.calls[0].request.content)
    assert sent["type"] == "sell" and sent["visible"] is True and sent.get("rank") in (5, None)
    assert post.calls[0].request.headers["Authorization"] == "Bearer secret-token"
    assert "Stopped" in out[-1] and "bad rank" in out[-1]
    log = (cfg.out_dir / "sync-log.jsonl").read_text()
    assert "secret-token" not in log and log.count("\n") == 3
    saved = SyncState.load(state_path(cfg))
    assert "new1" in saved.managed and "o3" not in saved.managed


@respx.mock
def test_auth_signin_then_me(cfg, monkeypatch):
    monkeypatch.delenv("WFM_JWT", raising=False)
    monkeypatch.setenv("WFM_EMAIL", "a@b.c")
    monkeypatch.setenv("WFM_PASSWORD", "pw")
    signin = respx.post(SIGNIN_URL).mock(return_value=httpx.Response(200, headers={"Authorization": "JWT tok123"}, json={}))
    respx.get(f"{API}/v2/me").mock(
        side_effect=lambda req: httpx.Response(200, json={"data": {"ingameName": "kouks", "id": "u1"}})
        if req.headers.get("Authorization") == "Bearer tok123"
        else httpx.Response(401)
    )
    with WfmClient(cfg) as c:
        me = authenticate(cfg, c)
        assert me.ingame_name == "kouks" and c.token == "tok123" and me.source == "email/password sign-in"
        assert signin.calls[0].request.headers["Authorization"] == "JWT"
        # cached token is reused without signing in again
        me2 = authenticate(cfg, c)
        assert me2.source == "cached token" and signin.call_count == 1


@respx.mock
def test_auth_bad_jwt_and_no_creds(cfg, monkeypatch):
    for k in ("WFM_JWT", "WFM_EMAIL", "WFM_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    respx.get(f"{API}/v2/me").mock(return_value=httpx.Response(401))
    with WfmClient(cfg) as c, pytest.raises(AuthError):
        authenticate(cfg, c, jwt="JWT nope")


def test_limit_keeps_top_priority_and_counts_unmanaged_orders():
    rows = [row(f"i{n}", LIST, 1, 10 + n, priority=float(n)) for n in range(6)]  # i5 is the best
    desired = desired_from_rows(rows)
    assert list(desired)[0] == ("i5", None)  # highest priority first
    orders = [
        {"id": "m1", "type": "sell", "itemId": "manual", "platinum": 5, "quantity": 1},  # manual, untouched
        {"id": "b1", "type": "buy", "itemId": "x", "platinum": 5, "quantity": 1},  # buy order uses a slot
        {"id": "t0", "type": "sell", "itemId": "i0", "platinum": 10, "quantity": 1},  # tool listing, now out of the top
        {"id": "t4", "type": "sell", "itemId": "i4", "platinum": 14, "quantity": 1},  # tool listing, still in the top
    ]
    state = SyncState({"t0": {}, "t4": {}})
    ops = diff(desired, orders, state, limit=5)  # 5 - 2 unmanaged = 3 slots: i5, i4, i3
    kinds = [(o.kind, o.item_id) for o in ops]
    assert kinds[0] == (DELETE, "i0")  # deletes run first to free slots
    assert (CREATE, "i5") in kinds and (CREATE, "i3") in kinds
    assert not any(k == CREATE and i in ("i0", "i1", "i2", "i4") for k, i in kinds)
    assert not any(o.order_id == "t4" for o in ops)  # in the top and priced right -> untouched
    after = len(orders) - sum(o.kind == DELETE for o in ops) + sum(o.kind == CREATE for o in ops)
    assert after == 5
    assert "outside the top 5" in next(o for o in ops if o.item_id == "i0").reason


def test_limit_is_capped_at_100():
    rows = [row(f"i{n}", LIST, 1, 10, priority=float(n)) for n in range(150)]
    ops = diff(desired_from_rows(rows), [], SyncState(), limit=500)
    assert sum(o.kind == CREATE for o in ops) == 100

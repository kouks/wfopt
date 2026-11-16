from datetime import datetime, timezone

import httpx
import pytest
import respx

from conftest import load_fixture
from wfopt.config import PricingCfg
from wfopt.engine.pricing import Order, PriceQuote, StatRow, best_ask, build_quote, suggested_price
from wfopt.market.client import API, RateLimiter, WfmClient, WfmError
from wfopt.market.prices import PriceNeed, TopOrdersSource, V1StatsSource, fetch_quotes

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.starts: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_rate_limiter_never_exceeds_rps():
    clk = FakeClock()
    lim = RateLimiter(2.5, clock=clk, sleep=clk.sleep)
    for _ in range(50):
        lim.acquire()
        clk.starts.append(clk.t)
        clk.t += 0.01  # request itself is fast
    for i in range(len(clk.starts)):
        window = [s for s in clk.starts if clk.starts[i] <= s < clk.starts[i] + 1.0]
        assert len(window) <= 3  # 2.5 rps -> at most 3 starts in any 1 s window
    gaps = [b - a for a, b in zip(clk.starts, clk.starts[1:])]
    assert min(gaps) >= 0.4 - 1e-9


@respx.mock
def test_retry_on_429_and_509(cfg):
    route = respx.get(f"{API}/v2/versions").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(509), httpx.Response(200, json={"data": 1})]
    )
    sleeps = []
    with WfmClient(cfg, sleep=sleeps.append) as c:
        assert c.get_json("/v2/versions") == {"data": 1}
    assert route.call_count == 3
    assert sleeps[0] >= 2 and sleeps[1] >= 2  # Retry-After honoured, then exponential


@respx.mock
def test_gives_up_after_retries(cfg):
    respx.get(f"{API}/v2/versions").mock(return_value=httpx.Response(503))
    with WfmClient(cfg, sleep=lambda s: None, max_retries=2) as c, pytest.raises(WfmError):
        c.get_json("/v2/versions")


def _mock_energize():
    respx.get(f"{API}/v1/items/arcane_energize/statistics").mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/stats_arcane_energize.json"))
    )
    respx.get(f"{API}/v2/orders/item/arcane_energize/top", params={"rank": "5"}).mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/top_arcane_energize_r5.json"))
    )
    respx.get(f"{API}/v2/orders/item/arcane_energize/top", params={"rank": "0"}).mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/top_arcane_energize_r0.json"))
    )


@respx.mock
def test_quotes_and_second_run_is_cached(cfg, catalog):
    _mock_energize()
    it = catalog.by_slug["arcane_energize"]
    needs = [PriceNeed(it, 0, 30), PriceNeed(it, 5, 1)]
    with WfmClient(cfg) as c:
        q = fetch_quotes(cfg, needs, V1StatsSource(c, 360), TopOrdersSource(c, 10), now=NOW)
        assert c.network_calls == 3  # one stats call for both ranks + one book per rank
    r0, r5 = q[(it.id, 0)], q[(it.id, 5)]
    assert r0.med7 == pytest.approx(7, abs=0.5) and r0.vol7 > 100
    assert 115 < r5.med7 < 140
    assert r5.ask is not None and r5.bid is not None and r5.orderbook_ok

    with WfmClient(cfg) as c2:  # fresh client, same disk cache
        fetch_quotes(cfg, needs, V1StatsSource(c2, 360), TopOrdersSource(c2, 10), now=NOW)
        assert c2.network_calls == 0


@respx.mock
def test_stats_failure_falls_back_to_orderbook(cfg, catalog):
    respx.get(f"{API}/v1/items/arcane_energize/statistics").mock(return_value=httpx.Response(404))
    respx.get(f"{API}/v2/orders/item/arcane_energize/top", params={"rank": "5"}).mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/top_arcane_energize_r5.json"))
    )
    it = catalog.by_slug["arcane_energize"]
    with WfmClient(cfg, sleep=lambda s: None) as c:
        q = fetch_quotes(cfg, [PriceNeed(it, 5, 1)], V1StatsSource(c, 360), TopOrdersSource(c, 10), now=NOW)[(it.id, 5)]
    assert not q.stats_ok and q.med7 is None
    assert any("order book only" in f for f in q.flags)
    assert suggested_price(q, PricingCfg()) == q.ask


@respx.mock
def test_subtype_param_is_sent(cfg, catalog):
    route = respx.get(f"{API}/v2/orders/item/vitality/top", params={"rank": "10", "subtype": "regular"}).mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/top_vitality_r10.json"))
    )
    with WfmClient(cfg) as c:
        sells, _ = TopOrdersSource(c, 10).top(catalog.by_slug["vitality"], 10, "regular")
    assert route.called and all(o.rank == 10 for o in sells)


def test_ask_prefers_ingame_and_ignores_trolls():
    sells = [
        Order("sell", 10, 1, 0, "ingame", "troll"),
        Order("sell", 60, 1, 0, "online", "a"),
        Order("sell", 70, 1, 0, "ingame", "b"),
        Order("sell", 50, 1, 0, "ingame", "kouks"),
    ]
    assert best_ask(sells, med7=80, floor=0.6, exclude_user="Kouks").ingame_name == "b"


def test_suggested_price_strategies():
    cfg = PricingCfg()
    q = PriceQuote("x", None, med7=100, ask=90, bid=70)
    assert suggested_price(q, cfg, "balanced") == 90
    assert suggested_price(q, cfg, "quick") == 89
    assert suggested_price(q, cfg, "patient") == 100
    assert suggested_price(PriceQuote("x", None, med7=100, ask=None, bid=70), cfg) == 100
    assert suggested_price(PriceQuote("x", None, med7=None, ask=55, bid=50), cfg) == 55
    assert suggested_price(PriceQuote("x", None), cfg) is None
    # ask far below median -> balanced floors at 85% of median
    assert suggested_price(PriceQuote("x", None, med7=100, ask=60, bid=10), cfg) == 85


def test_build_quote_filters_rank():
    rows = [StatRow(NOW, 10, 7.0, 0), StatRow(NOW, 2, 130.0, 5)]
    q = build_quote("x", 5, stat_rows=(rows, []), orders=None, cfg=PricingCfg(), now=NOW)
    assert q.med7 == 130 and q.vol7 == pytest.approx(2 / 7)

from collections import Counter

import pytest

from wfopt.config import Config
from wfopt.engine.arcanes import decide_arcane, plan_arcane
from wfopt.engine.classify import ARCANE, MOD, PRIME, ClassifiedItem
from wfopt.engine.keep import arcane_units, endo_to_max, keep_arcane, keep_mod
from wfopt.engine.model import DUCATS, FUSE, HOLD, LIST, MAX, SELL_TOP
from wfopt.engine.mods import decide_mod
from wfopt.engine.plan import daily_plan, score
from wfopt.engine.pricing import PriceQuote
from wfopt.engine.sets import PartStock, decide_set, evaluate_sets
from wfopt.engine.valuation import Market, listable_qty
from wfopt.inventory.normalize import Holding
from wfopt.market.catalog import Catalog, CatalogItem


def item(slug, tags=(), max_rank=None, ducats=None, bulk=False):
    return CatalogItem(slug, slug, f"/x/{slug}", slug.replace("_", " ").title(), frozenset(tags), max_rank, ducats, bulk)


def ci(it, cat, copies: dict, equipped_rank=None):
    hs = [Holding(it.game_ref, r, n, "x", equipped=(r == equipped_rank)) for r, n in copies.items()]
    return ClassifiedItem(it, cat, hs)


def q(slug, rank, med, vol=50.0, ask=None, bid=None, bid_user=None, bid_qty=1, per_trade=1):
    return PriceQuote(
        slug, rank, med7=med, vol7=vol, ask=ask, bid=bid, bid_user=bid_user, stats_ok=med is not None,
        bid_qty=bid_qty, bid_per_trade=per_trade, bid_platinum=None if bid is None else round(bid * per_trade),
    )


def mkt(quotes, cfg=None):
    return Market({(k[0].id, k[1]): v for k, v in quotes.items()}, cfg or Config())


# ---------------------------------------------------------------- keep rules


def test_arcane_units():
    assert arcane_units(5) == 21 and arcane_units(3) == 10 and arcane_units(0) == 1


def test_keep_arcane_with_max_copy():
    a = item("arc", ["arcane_enhancement"], 5)
    k = keep_arcane(ci(a, ARCANE, {5: 2, 0: 10}))
    assert k.surplus == Counter({5: 1, 0: 10}) and k.kept[5] == 1


def test_keep_arcane_reserves_21_or_10_units():
    a5 = item("a5", ["arcane_enhancement"], 5)
    k = keep_arcane(ci(a5, ARCANE, {0: 25}))
    assert k.kept[0] == 21 and k.surplus == Counter({0: 4}) and "fuse to keep" in k.status
    a3 = item("a3", ["arcane_enhancement"], 3)
    k = keep_arcane(ci(a3, ARCANE, {0: 12}))
    assert k.kept[0] == 10 and k.surplus == Counter({0: 2})


def test_keep_arcane_prefers_highest_ranked_copies():
    a = item("arc", ["arcane_enhancement"], 5)
    # r4 = 15 units, r2 = 6 units -> exactly 21, rank 0s stay surplus
    k = keep_arcane(ci(a, ARCANE, {4: 1, 2: 1, 0: 8}))
    assert k.kept == Counter({4: 1, 2: 1}) and k.surplus == Counter({0: 8})


def test_keep_arcane_short_of_keeper():
    a = item("arc", ["arcane_enhancement"], 5)
    k = keep_arcane(ci(a, ARCANE, {2: 1, 0: 8}))  # 6 + 8 = 14
    assert not k.surplus and k.keeper_short == (14, 21) and "14/21" in k.status


def test_keep_arcane_not_fusable_partials():
    a = item("arc", ["arcane_enhancement"], 5)
    # only the best partial (r2, 6 units) can be a base; other partials cannot be consumed
    k = keep_arcane(ci(a, ARCANE, {2: 2, 0: 20}), ranked_fusable=False)
    assert k.kept == Counter({2: 1, 0: 15}) and k.surplus == Counter({2: 1, 0: 5})


def test_keep_mod_highest_rank():
    m = item("mod", ["mod", "rare"], 10)
    k = keep_mod(ci(m, MOD, {10: 1, 3: 1, 0: 5}))
    assert k.kept == Counter({10: 1}) and k.surplus == Counter({3: 1, 0: 5})
    k = keep_mod(ci(m, MOD, {3: 1, 0: 5}))
    assert k.kept == Counter({3: 1}) and f"{30 * (1024 - 8):,} Endo" in k.status


def test_endo_cost():
    assert endo_to_max(frozenset({"legendary"}), 0, 10) == 40 * 1023
    assert endo_to_max(frozenset({"common"}), 3, 5) == 10 * (32 - 8)


# ---------------------------------------------------------------- sets vs parts


def _rhino():
    root = item("rhino_prime_set", ["prime", "set"], ducats=225)
    parts = [item(f"rhino_p{i}", ["prime", "component"], ducats=d) for i, d in enumerate([100, 65, 45, 15])]
    return root, parts


def test_set_beats_parts_and_saves_trades():
    root, parts = _rhino()
    quotes = {(root, None): q("s", None, 60)} | {(p, None): q(p.slug, None, 12) for p in parts}
    rows = decide_set(root, [PartStock(p, 1) for p in parts], mkt(quotes))
    sets = [r for r in rows if r.item_id == root.id]
    assert sets and sets[0].qty == 1 and sets[0].action == LIST
    # alternative: sell 3 parts (7p net each) + 100-ducat part to the kiosk (8p) -> set saves 2 trades
    assert "beats selling the parts by 26p" in sets[0].note and "saves 2 trades" in sets[0].note
    assert all(r.action != LIST for r in rows if r.item_id != root.id)  # nothing else to sell


def test_parts_beat_set():
    root, parts = _rhino()
    quotes = {(root, None): q("s", None, 30)} | {(p, None): q(p.slug, None, 20) for p in parts}
    n, net, trades, _ = evaluate_sets(root, [PartStock(p, 1) for p in parts], mkt(quotes))
    assert n == 0 and trades == 4 and net == pytest.approx(4 * 15)


def test_missing_part_means_no_set():
    root, parts = _rhino()
    quotes = {(root, None): q("s", None, 200)} | {(p, None): q(p.slug, None, 20) for p in parts}
    stock = [PartStock(p, 1) for p in parts[:3]] + [PartStock(parts[3], 0)]
    rows = decide_set(root, stock, mkt(quotes))
    assert not any(r.item_id == root.id for r in rows)
    assert "missing" in rows[0].note


def test_quantity_in_set_greater_than_one():
    root = item("dual_set", ["prime", "set"])
    blade, hilt = item("blade", ["prime", "component"], ducats=15), item("hilt", ["prime", "component"], ducats=15)
    quotes = {(root, None): q("s", None, 50), (blade, None): q("b", None, 5), (hilt, None): q("h", None, 5)}
    n, *_ = evaluate_sets(root, [PartStock(blade, 3, 2), PartStock(hilt, 2, 1)], mkt(quotes))
    assert n == 1  # 3 blades // 2 = 1 set
    n, *_ = evaluate_sets(root, [PartStock(blade, 1, 2), PartStock(hilt, 2, 1)], mkt(quotes))
    assert n == 0


# ---------------------------------------------------------------- ducats vs sell


def test_ducats_vs_sell_threshold():
    cfg = Config()  # trade cost 5, 0.08 p/ducat -> 100 ducats = 8p
    p = item("part", ["prime", "component"], ducats=100)
    rows = decide_set(None, [PartStock(p, 1)], mkt({(p, None): q("p", None, 12)}, cfg))
    assert rows[0].action == DUCATS and rows[0].ducats == 100  # 12 - 5 = 7 < 8
    rows = decide_set(None, [PartStock(p, 1)], mkt({(p, None): q("p", None, 14)}, cfg))
    assert rows[0].action == LIST  # 14 - 5 = 9 > 8
    rows = decide_set(None, [PartStock(p, 2)], mkt({}, cfg))
    assert rows[0].action == DUCATS and rows[0].qty == 2  # no market at all


# ---------------------------------------------------------------- arcanes


def _energize(p0, p5):
    a = item("arcane_energize", ["arcane_enhancement", "legendary"], 5, bulk=True)
    return a, {(a, 0): q("e", 0, p0, vol=200), (a, 5): q("e", 5, p5, vol=80)}


def test_arcane_energize_nov15_formula():
    # PLAN §4.2 numbers: r0 7p, r5 130p, 21 surplus r0 copies (keeper already owned).
    # Plan formula: fuse = 130 - 5 = 125; unranked = 21 * (7 - 5/3) = 112. Fusing wins once trade cost counts.
    a, quotes = _energize(7, 130)
    k = keep_arcane(ci(a, ARCANE, {5: 1, 0: 21}))
    best, plans = plan_arcane(k, mkt(quotes))
    assert plans[0].net == pytest.approx(112) and plans[1].net == pytest.approx(125)
    assert best.x == 1


def test_arcane_sell_rank0_when_max_is_cheap():
    a, quotes = _energize(7, 110)  # 105 < 112
    k = keep_arcane(ci(a, ARCANE, {5: 1, 0: 21}))
    rows = decide_arcane(k, mkt(quotes))
    assert [r.action for r in rows] == [LIST] and rows[0].rank == 0 and rows[0].qty == 21
    assert rows[0].trades == 7  # bulk 3 per trade
    assert "selling unranked nets" in rows[0].note


def test_arcane_fuse_rows():
    a, quotes = _energize(7, 160)
    k = keep_arcane(ci(a, ARCANE, {5: 1, 0: 44}))
    rows = decide_arcane(k, mkt(quotes))
    fuse = [r for r in rows if r.action == FUSE]
    assert fuse and fuse[0].qty == 2 and fuse[0].rank == 5
    left = [r for r in rows if r.rank == 0]
    assert left and left[0].qty == 2


# ---------------------------------------------------------------- mods


def test_mods_only_priced_and_listed_at_rank_0():
    # Even a max-rank-10 mod with a big max-rank price is never maxed or compared at max.
    m = item("primed_x", ["mod", "legendary"], 10)
    k = keep_mod(ci(m, MOD, {10: 1, 3: 1, 0: 2}))  # keeper r10; spares: one r3 + two r0
    rows = decide_mod(k, mkt({(m, 0): q("m", 0, 20), (m, 10): q("m", 10, 400)}))
    assert [(r.action, r.rank, r.qty) for r in rows] == [(LIST, 0, 3)]
    assert rows[0].price == 20 and "includes 1 ranked copy, listed at rank 0" in rows[0].note
    assert not any(r.action == MAX for r in rows)


def test_mod_rank0_needs_only():
    from wfopt.engine.plan import price_needs

    m = item("primed_x", ["mod", "legendary"], 10)
    needs = price_needs([keep_mod(ci(m, MOD, {10: 2, 5: 1, 0: 4}))], Catalog([]))
    assert [(n.rank, n.units, n.owned) for n in needs] == [(0, 6, 7)]


def test_mod_cheap_copies_are_held():
    m = item("vitality", ["mod", "common"], 10)
    k = keep_mod(ci(m, MOD, {10: 1, 0: 500}))
    rows = decide_mod(k, mkt({(m, 0): q("v", 0, 2)}))
    assert [r.action for r in rows] == [HOLD] and rows[0].qty == 500
    assert "not worth a trade" in rows[0].note


def test_sell_to_top_buyer_when_bid_meets_price():
    m = item("mod", ["mod", "rare"], 5)
    k = keep_mod(ci(m, MOD, {5: 2}))
    rows = decide_mod(k, mkt({(m, 0): q("m", 0, 50, ask=52, bid=55, bid_user="Buyer", bid_qty=5)}))
    assert rows[0].action == SELL_TOP and rows[0].price == 55 and rows[0].rank == 0
    assert rows[0].whisper == '/w Buyer Hi! I want to sell: "Mod" (rank 0) for 55 platinum. (warframe.market)'


# ---------------------------------------------------------------- fake-median guard


def _book(*asks):
    from wfopt.engine.pricing import Order

    return [Order("sell", a, 10, 0, "ingame", f"s{a}") for a in asks], []


def test_inflated_median_with_many_copies_uses_live_asks():
    from wfopt.config import PricingCfg
    from wfopt.engine.pricing import StatRow, build_quote, suggested_price
    from datetime import datetime, timezone

    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    rows = ([StatRow(now, 2, 21.7, 0)], [])  # "Revenge": thin volume, 21.7p median
    cfg = PricingCfg()
    qq = build_quote("revenge", 0, stat_rows=rows, orders=_book(2, 2, 3), cfg=cfg, owned=983, now=now)
    assert qq.median_suspect and qq.ask == 2 and suggested_price(qq, cfg) == 2
    assert "looks like fake sales" in qq.flags[-1] and "983 copies" in qq.flags[-1]
    # same market, but only a handful owned -> median trusted, cheap asks dropped as trolls as before
    few = build_quote("revenge", 0, stat_rows=rows, orders=_book(2, 2, 3), cfg=cfg, owned=5, now=now)
    assert not few.median_suspect and few.ask is None and suggested_price(few, cfg) == 22
    # a real market (sellers at the median, e.g. Buzz Kill) is left alone even with many copies
    real = build_quote("buzz", 0, stat_rows=([StatRow(now, 150, 13.3, 0)], []), orders=_book(15, 15), cfg=cfg, owned=500, now=now)
    assert not real.median_suspect and suggested_price(real, cfg) == 15


def test_fake_median_mod_row_ends_up_held():
    m = item("ammo_drum", ["mod", "uncommon"], 5)
    k = keep_mod(ci(m, MOD, {5: 1, 0: 1074}))
    fake = q("a", 0, 16, ask=1)
    fake.median_suspect = True
    rows = decide_mod(k, mkt({(m, 0): fake}))
    assert [r.action for r in rows] == [HOLD]  # 1p - 5p trade cost -> not worth a trade


# ---------------------------------------------------------------- liquidity cap and plan


def test_listable_quantity_cap():
    cfg = Config()
    assert listable_qty(q("x", None, 10, vol=0.5), cfg) == 1  # ceil(0.5 * 7 * 0.2) = 1
    assert listable_qty(q("x", None, 10, vol=10), cfg) == 14
    assert listable_qty(None, cfg) == 1
    # thin market: 5 spare parts, market absorbs 1 -> 1 listed, 4 to ducats
    p = item("part", ["prime", "component"], ducats=45)
    rows = decide_set(None, [PartStock(p, 5)], mkt({(p, None): q("p", None, 30, vol=0.5)}, cfg))
    assert [(r.action, r.qty) for r in rows] == [(LIST, 1), (DUCATS, 4)]
    assert "market only absorbs" in rows[1].note


def test_daily_plan_respects_budget():
    p1, p2 = item("a", ["prime"], ducats=1), item("b", ["prime"], ducats=1)
    m = mkt({(p1, None): q("a", None, 50, vol=20), (p2, None): q("b", None, 20, vol=20)})
    rows = decide_set(None, [PartStock(p1, 3)], m) + decide_set(None, [PartStock(p2, 3)], m)
    score(rows, Config())
    plan = daily_plan(rows, 4)
    assert plan.trades == 4
    assert plan.entries[0].row.slug == "a" and plan.entries[0].qty == 3
    assert plan.entries[1].qty == 1


def test_bundle_bid_is_priced_per_unit():
    """Regression: a buy order of 62p with perTrade 6 is ~10.3p per copy, not 62p."""
    from wfopt.engine.pricing import Order, build_quote
    from wfopt.config import PricingCfg

    buys = [Order("buy", 62, 666, 0, "ingame", "bulk", per_trade=6), Order("buy", 10, 99, 0, "ingame", "single")]
    sells = [Order("sell", 11, 2, 0, "ingame", "s")]
    qq = build_quote("x", 0, stat_rows=None, orders=(sells, buys), cfg=PricingCfg())
    assert qq.bid == pytest.approx(10.33, abs=0.01) and qq.bid_per_trade == 6 and qq.bid_platinum == 62


def test_sell_to_top_buyer_needs_enough_quantity():
    m = item("mod", ["mod", "rare"], 5)
    k = keep_mod(ci(m, MOD, {5: 4}))
    rows = decide_mod(k, mkt({(m, 0): q("m", 0, 50, ask=52, bid=55, bid_user="Buyer", bid_qty=1)}))
    assert rows[0].action == LIST and "only wants 1" in rows[0].note


def test_never_sell_keeps_every_copy(catalog):
    from wfopt.engine.classify import classify_holdings
    from wfopt.engine.plan import build_rows, compute_keeps, price_needs, unknown_never_sell
    from wfopt.inventory.normalize import normalize
    from conftest import synthetic_inventory

    cfg = Config()
    cfg.scope.never_sell = ["Primed Continuity", "arcane_energize", "vasto prime barrel", "typo mod"]
    c = classify_holdings(normalize(synthetic_inventory()), catalog)
    keeps = {k.item.item.slug: k for k in compute_keeps(c, cfg)}
    for slug in ("primed_continuity", "arcane_energize", "vasto_prime_barrel"):
        assert keeps[slug].never_sell and not keeps[slug].surplus
    assert keeps["primed_continuity"].kept == Counter({10: 1, 0: 1})  # both ranks kept
    assert keeps["vitality"].surplus  # not listed -> normal rules
    needs = {n.item.slug for n in price_needs(list(keeps.values()), catalog)}
    assert not needs & {"primed_continuity", "arcane_energize", "vasto_prime_barrel"}
    rows = build_rows(list(keeps.values()), catalog, {}, cfg)
    assert not any(r.slug in ("primed_continuity", "arcane_energize", "vasto_prime_barrel") for r in rows)
    assert unknown_never_sell(cfg, catalog) == ["typo mod"]

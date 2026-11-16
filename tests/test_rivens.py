import json

import httpx
import pytest
import respx

from wfopt.engine.rivens import EXACT, POSITIVES, WEAPON, describe, estimate
from wfopt.inventory.rivens import ROLL_SCALE, parse_rivens
from wfopt.market.client import API, WfmClient
from wfopt.market.rivens import (
    Auction, RivenAttribute, RivenCatalog, RivenWeapon, WeeklyStat, parse_js_literal, search_page_url, search_params,
)


def _fp(compat, buffs, curses=(), rerolls=0, lvl=8):
    return json.dumps({
        "compat": compat, "lvl": lvl, "rerolls": rerolls, "pol": "AP_ATTACK", "lvlReq": 12,
        "buffs": [{"Tag": t, "Value": v} for t, v in buffs], "curses": [{"Tag": t, "Value": v} for t, v in curses],
    })


INV = {
    "Upgrades": [
        {"ItemType": "/Lotus/Upgrades/Mods/Randomized/LotusRifleRandomModRare", "ItemId": {"$oid": "r1"},
         "UpgradeFingerprint": _fp("/W/Rubico", [("WeaponCritDamageMod", ROLL_SCALE // 2), ("WeaponFireIterationsMod", 0)],
                                   [("WeaponZoomFovMod", ROLL_SCALE // 4)], rerolls=3)},
        {"ItemType": "/Lotus/Upgrades/Mods/Randomized/LotusMeleeRandomModRare", "ItemId": {"$oid": "r2"},
         "UpgradeFingerprint": _fp("/W/Balla", [("WeaponMeleeDamageMod", 1), ("WeaponCritDamageMod", 1)])},
        {"ItemType": "/Lotus/Upgrades/Mods/Randomized/LotusPistolRandomModRare", "ItemId": {"$oid": "veiled"},
         "UpgradeFingerprint": "{}"},
        {"ItemType": "/Lotus/Upgrades/Mods/Rifle/WeaponCritChanceMod", "ItemId": {"$oid": "m"}, "UpgradeFingerprint": "{}"},
    ],
    "LongGuns": [{"ItemType": "/W/Rubico", "Configs": [{"Upgrades": ["r1"]}]}],
}

CAT = RivenCatalog(
    {"/W/Rubico": RivenWeapon("rubico", "/W/Rubico", "Rubico", "rifle", 0.65),
     "/W/Balla": RivenWeapon("balla", "/W/Balla", "Balla", "melee", 0.9)},
    {t: RivenAttribute(s, t, n) for t, s, n in [
        ("WeaponCritDamageMod", "critical_damage", "Critical Damage"),
        ("WeaponFireIterationsMod", "multishot", "Multishot"),
        ("WeaponZoomFovMod", "zoom", "Zoom"),
        ("WeaponDamageAmountMod", "base_damage_/_melee_damage", "Damage"),
    ]},
)


def auctions(*prices):
    return [Auction(str(i), p, ("critical_damage", "multishot"), ("zoom",), 0, "x", "ingame") for i, p in enumerate(prices)]


def test_parse_rivens():
    rs = parse_rivens(INV)
    assert [r.item_id for r in rs] == ["r1", "r2"]  # veiled and normal mods skipped
    r1 = rs[0]
    assert r1.weapon_ref == "/W/Rubico" and r1.rerolls == 3 and r1.rank == 8 and r1.polarity == "madurai"
    assert r1.equipped and r1.mastery_req == 12
    assert r1.buff_roll == pytest.approx(0.25) and r1.curse_roll == pytest.approx(0.25)
    assert r1.negative.tag == "WeaponZoomFovMod"


def test_parse_js_literal():
    text = "[\n  {\n    itemType: 'Rifle Riven Mod',\n    compatibility: 'Ack & Brunt',\n    rerolled: true,\n    median: 40,\n    note: 'it\\'s: fine'\n  },\n  {itemType: 'Melee Riven Mod', compatibility: null}\n]"
    rows = parse_js_literal(text)
    assert rows[0] == {"itemType": "Rifle Riven Mod", "compatibility": "Ack & Brunt", "rerolled": True, "median": 40, "note": "it's: fine"}
    assert rows[1]["compatibility"] is None
    assert parse_js_literal('[{"a": 1}]') == [{"a": 1}]


def test_describe_maps_melee_tags_and_search_params():
    r1, r2 = parse_rivens(INV)
    e = describe(r1, CAT)
    assert e.stats == "+Critical Damage, +Multishot, -Zoom" and e.negative_slug == "zoom" and e.searchable
    assert e.tier_params(EXACT) == search_params("rubico", ["critical_damage", "multishot"], "zoom")
    e2 = describe(r2, CAT)
    assert e2.positive_slugs == ["base_damage_/_melee_damage", "critical_damage"] and e2.negative_slug == "none"
    url = search_page_url(e2.tier_params(EXACT))
    assert "positive_stats=base_damage_/_melee_damage,critical_damage" in url and "negative_stats=none" in url


def test_unknown_tag_is_not_searchable():
    inv = {"Upgrades": [{"ItemType": "/Lotus/Upgrades/Mods/Randomized/X", "ItemId": {"$oid": "z"},
                         "UpgradeFingerprint": _fp("/W/Rubico", [("WeaponMysteryMod", 1)])}]}
    e = describe(parse_rivens(inv)[0], CAT)
    assert not e.searchable and e.tier_params(EXACT) is None and e.tier_params(WEAPON) is not None
    assert "unknown stat tags" in e.notes[0]


WEEKLY = {("rubico", True): WeeklyStat("rubico", True, 60, 90, 5, 900, 3)}


def test_estimate_exact_comparables():
    e = estimate(describe(parse_rivens(INV)[0], CAT), {EXACT: auctions(500, 120, 100, 150, 130, 140, 900)}, WEEKLY)
    assert e.estimate == 120 and e.cheapest == 100 and e.comps == 7 and e.confidence == "medium"  # median of 3 cheapest
    assert e.de_median == 60 and "equipped" in " ".join(e.notes)
    assert "negative_stats=zoom" in e.url


def test_estimate_falls_back_to_positives_then_de():
    r1 = parse_rivens(INV)[0]
    other_neg = [Auction(str(i), p, ("critical_damage", "multishot"), ("recoil",), 0, "x", "ingame") for i, p in enumerate((80, 90, 100))]
    e = estimate(describe(r1, CAT), {EXACT: auctions(200), POSITIVES: other_neg}, WEEKLY)
    assert e.estimate == 90 and e.confidence == "low" and "same positive stats" in e.basis
    e = estimate(describe(r1, CAT), {EXACT: auctions(200), POSITIVES: auctions(80)}, WEEKLY)
    assert e.estimate == 60 and e.confidence == "very low" and e.cheapest == 200
    assert "only 1 listing" in " ".join(e.notes)
    e = estimate(describe(r1, CAT), {EXACT: [], POSITIVES: [], WEAPON: auctions(15, 20)}, None)
    assert e.estimate == 15 and "cheapest riven listed" in e.basis


def test_de_rerolled_mismatch_noted():
    r2 = parse_rivens(INV)[1]  # unrolled Balla, only rerolled stats exist
    e = estimate(describe(r2, CAT), {}, {("balla", True): WeeklyStat("balla", True, 300, 400, 10, 2000, 5)})
    assert e.de_median == 300 and "only for rerolled" in " ".join(e.notes)


@respx.mock
def test_auction_limiter_slows_down_on_429(cfg):
    route = respx.get(f"{API}/v1/auctions/search").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"payload": {"auctions": []}})]
    )
    with WfmClient(cfg, sleep=lambda s: None) as c:
        lim = c.slow_limiters["/v1/auctions"]
        before = lim.interval
        c.get_json("/v1/auctions/search", params={"type": "riven", "weapon_url_name": "x"})
        assert route.call_count == 2 and lim.interval > before
        assert c.limiter.interval == pytest.approx(1 / cfg.wfm.rps)  # the normal limiter is untouched


def test_only_true_comparables_count():
    """WFM's filter returns rivens that contain our stats; an extra positive makes it a different riven."""
    r1 = parse_rivens(INV)[0]  # +CD +MS -Zoom
    better = [Auction(str(i), 2000, ("critical_damage", "multishot", "critical_chance"), ("zoom",), 0, "x", "ingame") for i in range(5)]
    other_neg = Auction("n", 50, ("critical_damage", "multishot"), ("recoil",), 0, "x", "ingame")
    e = estimate(describe(r1, CAT), {EXACT: better + [other_neg], POSITIVES: better + [other_neg]}, WEEKLY)
    assert e.estimate == 60 and e.confidence == "very low"  # nothing truly comparable -> DE median
    assert e.comps == 0

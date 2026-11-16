"""Synthetic inventory + recorded API responses -> deterministic report."""

import csv
import json
from datetime import datetime, timezone

import httpx
import respx

from conftest import load_fixture, synthetic_inventory
from wfopt.engine.model import TRADE_ACTIONS
from wfopt.inventory.aleca import encrypt_bytes, load_inventory
from wfopt.market.client import API, WfmClient
from wfopt.pipeline import run
from wfopt.report.csv_out import write_csv
from wfopt.report.html_out import render_html

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _flat_stats(median: float, volume: int = 5) -> dict:
    days = [
        {"datetime": f"2026-11-{d:02d}T00:00:00.000+00:00", "volume": volume, "median": median} for d in range(9, 16)
    ]
    return {"payload": {"statistics_closed": {"90days": days, "48hours": []}}}


def _mock_api():
    respx.get(f"{API}/v2/versions").mock(return_value=httpx.Response(200, json=load_fixture("wfm/versions.json")))
    respx.get(f"{API}/v2/items").mock(return_value=httpx.Response(200, json=load_fixture("wfm/items.json")))
    respx.get(url__regex=rf"{API}/v2/item/rhino_prime_\w+/set").mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/set_rhino_prime.json"))
    )
    respx.get(url__regex=rf"{API}/v2/item/vasto_prime_\w+/set").mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/set_vasto_prime.json"))
    )
    respx.get(url__regex=rf"{API}/v1/items/arcane_energize/statistics").mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/stats_arcane_energize.json"))
    )
    respx.get(f"{API}/v1/items/rhino_prime_set/statistics").mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/stats_rhino_prime_set.json"))
    )
    respx.get(url__regex=rf"{API}/v1/items/(rhino|vasto)_prime_\w+/statistics").mock(
        return_value=httpx.Response(200, json=_flat_stats(10))
    )
    respx.get(url__regex=rf"{API}/v1/items/\w+/statistics").mock(return_value=httpx.Response(404))
    respx.get(f"{API}/v2/orders/item/arcane_energize/top", params={"rank": "0"}).mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/top_arcane_energize_r0.json"))
    )
    respx.get(f"{API}/v2/orders/item/arcane_energize/top", params={"rank": "5"}).mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/top_arcane_energize_r5.json"))
    )
    respx.get(f"{API}/v2/orders/item/rhino_prime_set/top").mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/top_rhino_prime_set.json"))
    )
    respx.get(url__regex=rf"{API}/v2/orders/item/\w+/top.*").mock(
        return_value=httpx.Response(200, json={"data": {"sell": [], "buy": []}})
    )
    respx.get(f"{API}/v2/orders/user/tester").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"type": "sell", "itemId": "566751295dcbc186f0536bd1", "rank": 5, "platinum": 150, "quantity": 1, "visible": True}]},
        )
    )


def _run(cfg, tmp_path):
    p = tmp_path / "lastData.dat"
    p.write_bytes(encrypt_bytes(json.dumps(synthetic_inventory()).encode()))
    inv = load_inventory(p)
    with WfmClient(cfg, sleep=lambda s: None) as client:
        return run(cfg, client, inv, now=NOW)


@respx.mock
def test_end_to_end_report_is_deterministic(cfg, tmp_path):
    _mock_api()
    cfg.wfm.ingame_name = "tester"
    rep = _run(cfg, tmp_path)

    by = {(r.slug, r.action, r.rank): r for r in rep.rows}
    # Rhino: 1 BP, 2 chassis, 1 neuro, 1 systems -> one full set + a spare chassis
    rhino_set = [r for r in rep.rows if r.slug == "rhino_prime_set"]
    assert rhino_set and rhino_set[0].qty == 1 and rhino_set[0].action in TRADE_ACTIONS
    assert any(r.slug == "rhino_prime_chassis_blueprint" and r.qty == 1 for r in rep.rows)
    # Energize: 30 r0 + 1 max (kept) -> 30 surplus units -> fused or sold, with explanation
    energize = [r for r in rep.rows if r.slug == "arcane_energize"]
    assert sum(r.qty if r.rank == 0 else 21 * r.qty for r in energize if r.action in TRADE_ACTIONS) <= 30
    assert all(r.note for r in rep.rows)  # every row explains itself
    # Primed Continuity: 1 maxed (kept) + 1 rank 0 -> surplus row exists
    assert any(r.slug == "primed_continuity" for r in rep.rows)
    # daily plan within budget (MR 30 from the inventory)
    assert rep.plan.budget == 30 and rep.plan.trades <= 30
    # current listing annotated from the public user orders
    assert any(r.current_listing == "150p × 1" for r in rep.rows if r.slug == "arcane_energize" and r.rank == 5) or not any(
        r.slug == "arcane_energize" and r.rank == 5 for r in rep.rows
    )

    out = write_csv(rep, tmp_path / "out" / "r.csv")
    first = out.read_text(encoding="utf-8-sig")
    html = render_html(rep)
    assert "<script" in html and "cdn" not in html.lower()

    # second run: same inputs -> identical CSV, and everything served from cache
    rep2 = _run(cfg, tmp_path)
    second = write_csv(rep2, tmp_path / "out" / "r2.csv").read_text(encoding="utf-8-sig")
    assert first == second
    rows = list(csv.reader(first.splitlines()))
    assert rows[0][0] == "Priority" and len(rows) == len(rep.rows) + 1
    assert rep2.network_calls == 0

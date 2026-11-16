import httpx
import respx

from conftest import load_fixture, synthetic_inventory
from wfopt.engine.classify import ARCANE, MOD, PRIME, category_of, classify_holdings
from wfopt.inventory.normalize import normalize
from wfopt.market.catalog import fetch_catalog, fetch_sets, load_cached_catalog
from wfopt.market.client import API, WfmClient


def test_classify(catalog):
    c = classify_holdings(normalize(synthetic_inventory()), catalog)
    cats = {ci.item.slug: ci.category for ci in c.items.values()}
    assert cats["vasto_prime_barrel"] == PRIME
    assert cats["rhino_prime_neuroptics_blueprint"] == PRIME
    assert cats["arcane_energize"] == ARCANE
    assert cats["primed_continuity"] == MOD
    assert cats["vitality"] == MOD  # subtypes regular/atragraph stay in scope
    energize = next(ci for ci in c.items.values() if ci.item.slug == "arcane_energize")
    assert energize.copies_by_rank() == {0: 30, 5: 1}
    assert next(ci for ci in c.items.values() if ci.item.slug == "vitality").subtype == "regular"
    assert any(h.game_ref.endswith("Ferrite") for h in c.unmapped)


def test_category_rules(catalog):
    s = catalog.by_slug
    assert category_of(s["lith_a1_relic"])[0] is None
    assert category_of(s["ayatan_anasa_sculpture"])[0] is None
    assert category_of(s["rifle_riven_mod_veiled"])[0] is None
    assert category_of(s["naramon_lens"])[0] is None
    assert category_of(s["rhino_prime_set"])[0] is None
    assert category_of(s["molecular_fission"])[0] == MOD  # tagged prime but is a mod
    assert category_of(s["kavasa_prime_buckle"])[0] == PRIME
    # duplicate gameRef prefers the non-set item
    assert catalog.by_ref[s["prisma_shade"].game_ref].slug == "prisma_shade"


@respx.mock
def test_fetch_catalog_and_sets_are_cached(cfg):
    respx.get(f"{API}/v2/versions").mock(return_value=httpx.Response(200, json=load_fixture("wfm/versions.json")))
    items = respx.get(f"{API}/v2/items").mock(return_value=httpx.Response(200, json=load_fixture("wfm/items.json")))
    rhino = respx.get(f"{API}/v2/item/rhino_prime_blueprint/set").mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/set_rhino_prime.json"))
    )
    respx.get(url__regex=rf"{API}/v2/item/vasto_prime_\w+/set").mock(
        return_value=httpx.Response(200, json=load_fixture("wfm/set_vasto_prime.json"))
    )
    with WfmClient(cfg) as client:
        cat = fetch_catalog(client, cfg)
        primes = [cat.by_slug[s] for s in ["rhino_prime_blueprint", "rhino_prime_chassis_blueprint", "vasto_prime_barrel", "vasto_prime_receiver"]]
        fetch_sets(client, cfg, cat, primes)
        assert rhino.call_count == 1  # chassis covered by the rhino set fetched via the blueprint
        rs = cat.sets[cat.part_to_set[cat.by_slug["rhino_prime_chassis_blueprint"].id]]
        assert len(rs.parts) == 4 and all(q == 1 for q in rs.parts.values())
        n = client.network_calls

        cat2 = fetch_catalog(client, cfg)
        fetch_sets(client, cfg, cat2, primes)
        assert client.network_calls == n  # versions cached, catalogue cached, sets cached
    assert items.call_count == 1
    assert load_cached_catalog(cfg).part_to_set == cat.part_to_set

import json

import pytest

from conftest import synthetic_inventory
from wfopt.inventory.aleca import InventoryError, encrypt_bytes, load_inventory, parse_payload
from wfopt.inventory.normalize import normalize


def test_decrypt_roundtrip(tmp_path):
    inv = synthetic_inventory()
    p = tmp_path / "lastData.dat"
    p.write_bytes(encrypt_bytes(json.dumps(inv).encode()))
    loaded = load_inventory(p)
    assert loaded.data == inv
    assert loaded.age_hours < 1


def test_inventoryjson_wrapper_is_unwrapped():
    inner = {"MiscItems": []}
    assert parse_payload(json.dumps({"InventoryJson": json.dumps(inner)}).encode()) == inner


def test_bad_file_raises(tmp_path):
    p = tmp_path / "lastData.dat"
    p.write_bytes(b"not encrypted")
    with pytest.raises(InventoryError):
        load_inventory(p)
    with pytest.raises(InventoryError):
        load_inventory(tmp_path / "missing.dat")


def test_normalize():
    h = normalize(synthetic_inventory())
    assert h.mastery_rank == 30 and h.trades_remaining == 12
    assert h.skipped_rivens == 1
    by = {(x.game_ref.rsplit("/", 1)[-1], x.rank, x.source): x for x in h.items}
    assert by[("VastoPrimeBarrel", None, "MiscItems")].count == 2
    assert by[("RhinoPrimeChassisBlueprint", None, "Recipes")].count == 2
    assert by[("GolemArcaneRadialEnergyOnEnergyPickup", 0, "RawUpgrades")].count == 30
    assert by[("GolemArcaneRadialEnergyOnEnergyPickup", 5, "Upgrades")].equipped
    assert by[("AvatarAbilityDurationModExpert", 10, "Upgrades")].equipped
    # missing lvl means rank 0, not equipped
    m2 = by[("AvatarAbilityDurationModExpert", 0, "Upgrades")]
    assert not m2.equipped and m2.item_id == "m2"
    # built gear and pending recipes are never read as holdings
    assert not any(x.game_ref.endswith("RhinoPrime") for x in h.items)
    assert sum(1 for x in h.items if x.game_ref.endswith("RhinoPrimeBlueprint")) == 1

import json
from pathlib import Path

import pytest

from wfopt.config import Config
from wfopt.market.catalog import Catalog, CatalogItem

FIX = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(root=tmp_path)
    c.wfm.rps = 1000  # tests don't need real throttling unless they test it
    c.wfm.auction_rps = 1000
    return c


@pytest.fixture
def catalog() -> Catalog:
    raw = load_fixture("wfm/items.json")["data"]
    return Catalog([CatalogItem.from_api(x) for x in raw], "test")


def synthetic_inventory() -> dict:
    return {
        "PlayerLevel": 30,
        "TradesRemaining": 12,
        "MiscItems": [
            {"ItemType": "/Lotus/Types/Items/MiscItems/Ferrite", "ItemCount": 1000},
            {"ItemType": "/Lotus/Types/Recipes/Weapons/WeaponParts/VastoPrimeBarrel", "ItemCount": 2},
            {"ItemType": "/Lotus/Types/Recipes/Weapons/WeaponParts/VastoPrimeReceiver", "ItemCount": 1},
        ],
        "Recipes": [
            {"ItemType": "/Lotus/Types/Recipes/WarframeRecipes/RhinoPrimeBlueprint", "ItemCount": 1},
            {"ItemType": "/Lotus/Types/Recipes/WarframeRecipes/RhinoPrimeChassisBlueprint", "ItemCount": 2},
            {"ItemType": "/Lotus/Types/Recipes/WarframeRecipes/RhinoPrimeHelmetBlueprint", "ItemCount": 1},
            {"ItemType": "/Lotus/Types/Recipes/WarframeRecipes/RhinoPrimeSystemsBlueprint", "ItemCount": 1},
            {"ItemType": "/Lotus/Types/Recipes/Weapons/VastoPrimeBlueprint", "ItemCount": 1},
        ],
        "RawUpgrades": [
            {"ItemType": "/Lotus/Upgrades/CosmeticEnhancers/Utility/GolemArcaneRadialEnergyOnEnergyPickup", "ItemCount": 30},
            {"ItemType": "/Lotus/Upgrades/Mods/Warframe/AvatarHealthMaxMod", "ItemCount": 4},
        ],
        "Upgrades": [
            {
                "ItemType": "/Lotus/Upgrades/CosmeticEnhancers/Utility/GolemArcaneRadialEnergyOnEnergyPickup",
                "ItemId": {"$oid": "a1"},
                "UpgradeFingerprint": '{"lvl":5}',
            },
            {
                "ItemType": "/Lotus/Upgrades/Mods/Warframe/Expert/AvatarAbilityDurationModExpert",
                "ItemId": {"$oid": "m1"},
                "UpgradeFingerprint": '{"lvl":10}',
            },
            {
                "ItemType": "/Lotus/Upgrades/Mods/Warframe/Expert/AvatarAbilityDurationModExpert",
                "ItemId": {"$oid": "m2"},
                "UpgradeFingerprint": "{}",
            },
            {
                "ItemType": "/Lotus/Upgrades/Mods/Randomized/LotusRifleRandomModRare",
                "ItemId": {"$oid": "r1"},
                "UpgradeFingerprint": '{"lvl":8,"buffs":[]}',
            },
        ],
        "Suits": [
            {"ItemType": "/Lotus/Powersuits/Rhino/RhinoPrime", "Configs": [{"Upgrades": ["m1", "", "a1"]}]},
        ],
        "PendingRecipes": [{"ItemType": "/Lotus/Types/Recipes/WarframeRecipes/RhinoPrimeBlueprint"}],
    }

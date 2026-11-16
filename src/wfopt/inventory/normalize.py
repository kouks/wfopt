"""Turn raw inventory arrays into flat holdings: (gameRef, rank, count, source)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

# Arrays that hold tradable stacks/instances we care about.
STACK_ARRAYS = ("MiscItems", "Recipes")  # no rank
RAW_UPGRADES = "RawUpgrades"  # unranked, unequipped stacks (rank 0)
UPGRADES = "Upgrades"  # individual ranked/equipped instances

# Arrays never read for holdings: built gear shares gameRefs with WFM *sets*.
IGNORED_FOR_HOLDINGS = ("Suits", "LongGuns", "Pistols", "Melee", "SpaceGuns", "PendingRecipes")


@dataclass
class Holding:
    game_ref: str
    rank: int | None
    count: int
    source: str
    item_id: str | None = None  # Upgrades only
    equipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Holdings:
    items: list[Holding]
    skipped_rivens: int
    mastery_rank: int | None
    trades_remaining: int | None

    def counts_by_source(self) -> Counter[str]:
        c: Counter[str] = Counter()
        for h in self.items:
            c[h.source] += 1
        return c


def _parse_level(fingerprint: Any) -> int:
    if isinstance(fingerprint, str):
        try:
            fingerprint = json.loads(fingerprint) if fingerprint else {}
        except json.JSONDecodeError:
            return 0
    if isinstance(fingerprint, dict):
        try:
            return int(fingerprint.get("lvl", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _oid(v: Any) -> str | None:
    if isinstance(v, dict):
        return v.get("$oid")
    return v if isinstance(v, str) else None


def equipped_upgrade_ids(inv: dict[str, Any]) -> set[str]:
    """Upgrade ItemIds referenced anywhere outside the upgrade arrays (loadout Configs etc.).

    Annotation only: equipped copies still count toward the kept copy.
    """
    ids = {i for i in (_oid(u.get("ItemId")) for u in inv.get(UPGRADES, [])) if i}
    found: set[str] = set()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and o in ids:
            found.add(o)

    for key, val in inv.items():
        if key not in (UPGRADES, RAW_UPGRADES):
            walk(val)
    return found


def normalize(inv: dict[str, Any]) -> Holdings:
    items: list[Holding] = []
    for arr in STACK_ARRAYS:
        for e in inv.get(arr, []) or []:
            gr, n = e.get("ItemType"), int(e.get("ItemCount", 0) or 0)
            if gr and n > 0:
                items.append(Holding(gr, None, n, arr))

    for e in inv.get(RAW_UPGRADES, []) or []:
        gr, n = e.get("ItemType"), int(e.get("ItemCount", 0) or 0)
        if gr and n > 0:
            items.append(Holding(gr, 0, n, RAW_UPGRADES))

    equipped = equipped_upgrade_ids(inv)
    rivens = 0
    for e in inv.get(UPGRADES, []) or []:
        gr = e.get("ItemType")
        if not gr:
            continue
        if "/Randomized/" in gr:
            rivens += 1
            continue
        iid = _oid(e.get("ItemId"))
        items.append(
            Holding(gr, _parse_level(e.get("UpgradeFingerprint")), 1, UPGRADES, iid, bool(iid and iid in equipped))
        )

    def _int(k: str) -> int | None:
        v = inv.get(k)
        return int(v) if isinstance(v, (int, float)) else None

    return Holdings(items, rivens, _int("PlayerLevel"), _int("TradesRemaining"))

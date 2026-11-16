"""Unveiled riven instances from the inventory's `Upgrades` array."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from wfopt.inventory.normalize import UPGRADES, equipped_upgrade_ids

ROLL_SCALE = 2**30  # buff/curse `Value` is a 30-bit roll within the stat's ±10% range

POLARITY = {"AP_ATTACK": "madurai", "AP_DEFENSE": "vazarin", "AP_TACTIC": "naramon", "AP_POWER": "zenurik",
            "AP_WARD": "unairu", "AP_UMBRA": "umbra", "AP_PRECEPT": "penjaga"}


@dataclass
class RivenStat:
    tag: str
    roll: float  # 0..1 position within the roll range (stat × 0.9 … × 1.1)
    positive: bool


@dataclass
class Riven:
    item_id: str
    item_type: str
    weapon_ref: str
    stats: list[RivenStat]
    rank: int = 0
    rerolls: int = 0
    polarity: str = ""
    mastery_req: int | None = None
    equipped: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def positives(self) -> list[RivenStat]:
        return [s for s in self.stats if s.positive]

    @property
    def negative(self) -> RivenStat | None:
        return next((s for s in self.stats if not s.positive), None)

    @property
    def buff_roll(self) -> float | None:
        p = self.positives
        return sum(s.roll for s in p) / len(p) if p else None

    @property
    def curse_roll(self) -> float | None:
        """0 = mildest possible negative, 1 = harshest."""
        n = self.negative
        return n.roll if n else None


def _oid(v: Any) -> str:
    return v.get("$oid", "") if isinstance(v, dict) else str(v or "")


def parse_rivens(inv: dict[str, Any]) -> list[Riven]:
    """Unveiled rivens only (those with a `compat` weapon). Veiled ones have no stats yet."""
    equipped = equipped_upgrade_ids(inv)
    out: list[Riven] = []
    for e in inv.get(UPGRADES, []) or []:
        t = e.get("ItemType", "")
        if "/Randomized/" not in t:
            continue
        try:
            fp = json.loads(e.get("UpgradeFingerprint") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(fp, dict) or not fp.get("compat"):
            continue
        stats = [RivenStat(b["Tag"], min(1.0, int(b.get("Value", 0)) / ROLL_SCALE), True) for b in fp.get("buffs", []) if b.get("Tag")]
        stats += [RivenStat(c["Tag"], min(1.0, int(c.get("Value", 0)) / ROLL_SCALE), False) for c in fp.get("curses", []) if c.get("Tag")]
        iid = _oid(e.get("ItemId"))
        out.append(
            Riven(
                item_id=iid,
                item_type=t,
                weapon_ref=fp["compat"],
                stats=stats,
                rank=int(fp.get("lvl", 0) or 0),
                rerolls=int(fp.get("rerolls", 0) or 0),
                polarity=POLARITY.get(fp.get("pol", ""), str(fp.get("pol", "")).lower()),
                mastery_req=fp.get("lvlReq"),
                equipped=iid in equipped,
                raw=fp,
            )
        )
    return out

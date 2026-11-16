"""Riven estimates (pure): comparable auctions first, DE weekly medians as a fallback.

Auctions are asking prices, not sales, and riven value depends heavily on the exact
stat combination, so every estimate carries a confidence label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from wfopt.inventory.rivens import Riven
from wfopt.market.rivens import Auction, RivenCatalog, RivenWeapon, WeeklyStat, search_page_url, search_params

MIN_COMPS = 3
CHEAPEST_N = 3  # asks run high, so the estimate uses the cheapest few matches

EXACT = "same stats and negative"
POSITIVES = "same positive stats"
WEAPON = "any riven for the weapon"


@dataclass
class RivenEstimate:
    riven: Riven
    weapon: RivenWeapon | None
    weapon_name: str
    stats: str  # human readable: "+Crit Damage, +Multishot, -Zoom"
    positive_slugs: list[str]
    negative_slug: str | None  # attribute slug, or "none"
    estimate: float | None = None
    basis: str = ""
    confidence: str = "none"
    comps: int = 0
    cheapest: int | None = None
    weapon_floor: int | None = None
    de_median: float | None = None
    de_avg: float | None = None
    url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def searchable(self) -> bool:
        return self.weapon is not None and bool(self.positive_slugs) and self.negative_slug is not None

    def tier_params(self, tier: str) -> dict[str, str] | None:
        if self.weapon is None:
            return None
        if tier == WEAPON:
            return search_params(self.weapon.slug)
        if not self.searchable:
            return None
        if tier == EXACT:
            return search_params(self.weapon.slug, self.positive_slugs, self.negative_slug)
        return search_params(self.weapon.slug, self.positive_slugs)


def describe(riven: Riven, cat: RivenCatalog) -> RivenEstimate:
    weapon = cat.weapons.get(riven.weapon_ref)
    weapon_name = weapon.name if weapon else riven.weapon_ref.rsplit("/", 1)[-1]
    parts, pos, neg, missing = [], [], None, []
    for s in riven.stats:
        a = cat.attribute(s.tag)
        label = a.name if a else s.tag
        parts.append(("+" if s.positive else "-") + label)
        if a is None:
            missing.append(s.tag)
        elif s.positive:
            pos.append(a.slug)
        else:
            neg = a.slug
    est = RivenEstimate(riven, weapon, weapon_name, ", ".join(parts), pos, None if missing else (neg or "none"))
    if weapon is None:
        est.notes.append("weapon not in the WFM riven list")
    if missing:
        est.notes.append("unknown stat tags: " + ", ".join(missing))
    return est


def matching(est: RivenEstimate, tier: str, auctions: list[Auction] | None) -> list[Auction]:
    """WFM's stat filter returns rivens that *contain* the stats; keep only true comparables.

    A riven with an extra positive stat is a different (usually much better) riven.
    """
    if not auctions:
        return []
    if tier == WEAPON:
        return list(auctions)
    pos = set(est.positive_slugs)
    neg = set() if est.negative_slug in (None, "none") else {est.negative_slug}
    out = [a for a in auctions if set(a.positives) == pos]
    if tier == EXACT:
        out = [a for a in out if set(a.negatives) == neg]
    return out


def _price(auctions: list[Auction]) -> tuple[float, int]:
    prices = sorted(a.price for a in auctions)
    return median(prices[:CHEAPEST_N]), prices[0]


def estimate(
    est: RivenEstimate,
    results: dict[str, list[Auction] | None],
    weekly: dict[tuple[str, bool], WeeklyStat] | None,
) -> RivenEstimate:
    """Fill the estimate from auction search results per tier and DE's weekly stats."""
    results = {tier: matching(est, tier, a) for tier, a in results.items()}
    r = est.riven
    rerolled = r.rerolls > 0
    if weekly and est.weapon_name:
        w = weekly.get((est.weapon_name.lower(), rerolled)) or weekly.get((est.weapon_name.lower(), not rerolled))
        if w:
            est.de_median, est.de_avg = w.median, w.avg
            if w.rerolled != rerolled:
                est.notes.append(f"DE stats only for {'rerolled' if w.rerolled else 'unrolled'} rivens")

    floor = results.get(WEAPON)
    if floor:
        est.weapon_floor = min(a.price for a in floor)

    for tier, conf in ((EXACT, "medium"), (POSITIVES, "low")):
        auctions = results.get(tier)
        if auctions and len(auctions) >= MIN_COMPS:
            est.estimate, est.cheapest = _price(auctions)
            est.comps = len(auctions)
            est.basis = f"median of the {min(CHEAPEST_N, len(auctions))} cheapest listings with {tier}"
            est.confidence = conf if not (tier == EXACT and len(auctions) < CHEAPEST_N) else "low"
            est.url = search_page_url(est.tier_params(tier) or {})
            break
    else:
        exact = results.get(EXACT) or []
        if exact:
            est.cheapest = min(a.price for a in exact)
            est.comps = len(exact)
            est.notes.append(f"only {len(exact)} listing(s) with these exact stats")
        if est.de_median is not None:
            est.estimate, est.confidence = est.de_median, "very low"
            est.basis = "DE weekly median for this weapon (all stat combos)"
        elif est.weapon_floor is not None:
            est.estimate, est.confidence = float(est.weapon_floor), "very low"
            est.basis = "cheapest riven listed for this weapon"
        else:
            est.basis = "no market data"
        params = est.tier_params(EXACT) or est.tier_params(WEAPON)
        est.url = search_page_url(params) if params else ""

    if est.estimate is not None and est.weapon_floor is not None and est.estimate <= est.weapon_floor * 1.2:
        est.notes.append("close to the cheapest riven for this weapon: likely not a sought-after roll")
    if r.equipped:
        est.notes.append("equipped: unequip before trading")
    return est

"""Join holdings to the WFM catalogue and classify into prime / arcane / mod / skipped."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from wfopt.inventory.normalize import Holding, Holdings
from wfopt.market.catalog import Catalog, CatalogItem

PRIME, ARCANE, MOD = "prime", "arcane", "mod"


@dataclass
class ClassifiedItem:
    item: CatalogItem
    category: str
    holdings: list[Holding] = field(default_factory=list)

    @property
    def units(self) -> int:
        return sum(h.count for h in self.holdings)

    def copies_by_rank(self) -> Counter[int]:
        """rank -> number of copies (mods/arcanes)."""
        c: Counter[int] = Counter()
        for h in self.holdings:
            c[h.rank or 0] += h.count
        return c

    @property
    def any_equipped(self) -> bool:
        return any(h.equipped for h in self.holdings)

    @property
    def subtype(self) -> str | None:
        """Subtype to use for market queries (only `regular` mods today)."""
        return "regular" if "regular" in self.item.subtypes else None


@dataclass
class Skipped:
    name: str
    game_ref: str
    reason: str
    units: int


@dataclass
class Classified:
    items: dict[str, ClassifiedItem]  # item id -> classified
    skipped: list[Skipped]
    unmapped: list[Holding]

    def by_category(self, cat: str) -> list[ClassifiedItem]:
        return [c for c in self.items.values() if c.category == cat]

    def category_counts(self) -> dict[str, tuple[int, int]]:
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for c in self.items.values():
            out[c.category][0] += 1
            out[c.category][1] += c.units
        for s in self.skipped:
            out["skipped"][0] += 1
            out["skipped"][1] += s.units
        return {k: (v[0], v[1]) for k, v in out.items()}

    def unmapped_by_source(self) -> Counter[str]:
        return Counter(h.source for h in self.unmapped)


def category_of(item: CatalogItem) -> tuple[str | None, str]:
    """(category, reason). category None means skipped for `reason`."""
    t = item.tags
    if "riven_mod" in t or "riven" in t:
        return None, "riven"
    if "relic" in t:
        return None, "relic"
    if "ayatan_sculpture" in t:
        return None, "ayatan sculpture"
    if "arcane_enhancement" in t:
        return ARCANE, ""
    if "mod" in t:
        if item.subtypes and "regular" not in item.subtypes:
            return None, f"has subtypes {list(item.subtypes)}"
        return MOD, ""
    if "prime" in t:
        if "set" in t:
            return None, "set root held as built gear"
        return PRIME, ""
    return None, "out of scope: " + ",".join(sorted(t))


def classify_holdings(holdings: Holdings, catalog: Catalog, categories: list[str] | None = None) -> Classified:
    categories = categories or [PRIME, ARCANE, MOD]
    items: dict[str, ClassifiedItem] = {}
    skipped_units: dict[str, list] = {}
    unmapped: list[Holding] = []
    for h in holdings.items:
        it = catalog.by_ref.get(h.game_ref)
        if it is None:
            unmapped.append(h)
            continue
        cat, reason = category_of(it)
        if cat is not None and cat not in categories:
            cat, reason = None, f"category {cat} disabled in config"
        if cat is None:
            s = skipped_units.setdefault(it.id, [it, reason, 0])
            s[2] += h.count
            continue
        ci = items.setdefault(it.id, ClassifiedItem(it, cat))
        ci.holdings.append(h)
    skipped = [Skipped(it.name, it.game_ref, reason, n) for it, reason, n in skipped_units.values()]
    return Classified(items, skipped, unmapped)

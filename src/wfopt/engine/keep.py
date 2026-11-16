"""Keep rules: what stays in the inventory and what is surplus.

- Prime parts/blueprints: keep 0 (the player owns every prime).
- Arcanes: keep one max-rank copy (units model: rank r = (r+1)(r+2)/2 rank-0 copies).
- Mods: keep the single highest-rank copy.
Equipped copies count toward the kept copy.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from wfopt.engine.classify import ARCANE, MOD, PRIME, ClassifiedItem

ENDO_BASE = {"common": 10, "uncommon": 20, "rare": 30, "legendary": 40}


def arcane_units(rank: int) -> int:
    return (rank + 1) * (rank + 2) // 2


def endo_base_cost(tags: frozenset[str]) -> int:
    for r in ("legendary", "rare", "uncommon", "common"):
        if r in tags:
            return ENDO_BASE[r]
    if "amalgam" in tags or "galvanized" in tags:
        return ENDO_BASE["rare"]
    return ENDO_BASE["rare"]


def endo_to_max(tags: frozenset[str], rank: int, max_rank: int) -> int:
    """Endo to take a mod from `rank` to `max_rank`: EBC × (2^m − 2^r)."""
    if rank >= max_rank:
        return 0
    return endo_base_cost(tags) * (2**max_rank - 2**rank)


@dataclass
class KeepResult:
    item: ClassifiedItem
    surplus: Counter[int | None]  # rank (None for unranked) -> surplus copies
    kept: Counter[int | None] = field(default_factory=Counter)  # rank -> copies kept / reserved
    status: str = ""  # human-readable keeper status
    keeper_short: tuple[int, int] | None = None  # (units, needed) for arcanes short of a keeper

    @property
    def surplus_units(self) -> int:
        return sum(self.surplus.values())


def _clamped_copies(ci: ClassifiedItem) -> Counter[int]:
    m = ci.item.max_rank or 0
    out: Counter[int] = Counter()
    for r, n in ci.copies_by_rank().items():
        out[min(r, m)] += n
    return out


def keep_prime(ci: ClassifiedItem) -> KeepResult:
    return KeepResult(ci, Counter({None: ci.units}), status="all surplus (you own every prime)")


def keep_arcane(ci: ClassifiedItem, ranked_fusable: bool = True) -> KeepResult:
    m = ci.item.max_rank or 0
    u_max = arcane_units(m)
    copies = _clamped_copies(ci)
    surplus = Counter(copies)
    kept: Counter[int | None] = Counter()

    if copies[m] >= 1:
        surplus[m] -= 1
        kept[m] = 1
        res = KeepResult(ci, surplus, kept, status=f"keeping 1 copy at max rank {m}")
    else:
        if ranked_fusable:
            total = sum(arcane_units(r) * n for r, n in copies.items())
        else:
            # only rank-0 copies can be consumed: one base copy (the best partial) plus rank 0s fed into it
            best = max((r for r in copies if r > 0 and copies[r]), default=0)
            total = arcane_units(best) + copies[0] if best > 0 else copies[0]
        if total < u_max:
            kept = Counter({r: n for r, n in copies.items()})
            return KeepResult(
                ci,
                Counter(),
                kept,
                status=f"{total}/{u_max} units toward your keeper (rank {m}), keeping all",
                keeper_short=(total, u_max),
            )
        remaining = u_max
        if ranked_fusable:
            # prefer highest-ranked copies that fit, then rank 0s, then the smallest partial that covers the rest
            for r in sorted((r for r in copies if r > 0), reverse=True):
                while surplus[r] and arcane_units(r) <= remaining:
                    surplus[r] -= 1
                    kept[r] += 1
                    remaining -= arcane_units(r)
            take = min(surplus[0], remaining)
            surplus[0] -= take
            kept[0] += take
            remaining -= take
            for r in sorted(r for r in copies if r > 0):
                while remaining > 0 and surplus[r]:
                    surplus[r] -= 1
                    kept[r] += 1
                    remaining -= arcane_units(r)
        else:
            best = max((r for r in copies if r > 0 and copies[r]), default=0)
            surplus[best] -= 1
            kept[best] += 1
            remaining -= arcane_units(best)
            take = min(surplus[0], remaining)
            surplus[0] -= take
            kept[0] += take
        desc = " + ".join(f"{n}× r{r}" for r, n in sorted(kept.items(), reverse=True) if n)
        res = KeepResult(ci, surplus, kept, status=f"fuse to keep: reserve {desc} and rank one to {m} in game")
    res.surplus = Counter({r: n for r, n in res.surplus.items() if n > 0})
    return res


def keep_mod(ci: ClassifiedItem) -> KeepResult:
    m = ci.item.max_rank or 0
    copies = _clamped_copies(ci)
    best = max(copies)
    surplus = Counter(copies)
    surplus[best] -= 1
    surplus = Counter({r: n for r, n in surplus.items() if n > 0})
    if best >= m:
        status = f"keeping 1 copy at max rank {m}"
    else:
        endo = endo_to_max(ci.item.tags, best, m)
        status = f"keeping your rank {best} copy; needs {endo:,} Endo to max"
    return KeepResult(ci, surplus, Counter({best: 1}), status=status)


def equipped_warning(keep: KeepResult) -> str:
    """Note for surplus rows when an equipped copy may be among the surplus copies."""
    eq = [h for h in keep.item.holdings if h.equipped]
    if not eq or not keep.surplus:
        return ""
    kept_rank = max((r for r, n in keep.kept.items() if n and r is not None), default=None)
    if len(eq) > 1 or any(h.rank != kept_rank for h in eq):
        return f"{len(eq)} cop{'ies are' if len(eq) > 1 else 'y is'} equipped somewhere: unequip before trading"
    return ""


def compute_keep(ci: ClassifiedItem, ranked_fusable: bool = True) -> KeepResult:
    if ci.category == PRIME:
        return keep_prime(ci)
    if ci.category == ARCANE:
        return keep_arcane(ci, ranked_fusable)
    if ci.category == MOD:
        return keep_mod(ci)
    raise ValueError(ci.category)

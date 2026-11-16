"""WFM item catalogue (/v2/items) and set compositions (/v2/item/{slug}/set)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from wfopt.config import Config
from wfopt.market.client import WfmClient, v2_data

ITEM_URL = "https://warframe.market/items/{slug}"


@dataclass(frozen=True)
class CatalogItem:
    id: str
    slug: str
    game_ref: str
    name: str
    tags: frozenset[str]
    max_rank: int | None = None
    ducats: int | None = None
    bulk_tradable: bool = False
    subtypes: tuple[str, ...] = ()
    vaulted: bool | None = None

    @property
    def url(self) -> str:
        return ITEM_URL.format(slug=self.slug)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "CatalogItem":
        name = ((d.get("i18n") or {}).get("en") or {}).get("name") or d.get("slug", "")
        return cls(
            id=d["id"],
            slug=d["slug"],
            game_ref=d.get("gameRef") or "",
            name=name,
            tags=frozenset(d.get("tags") or ()),
            max_rank=d.get("maxRank"),
            ducats=d.get("ducats"),
            bulk_tradable=bool(d.get("bulkTradable")),
            subtypes=tuple(d.get("subtypes") or ()),
            vaulted=d.get("vaulted"),
        )


@dataclass
class SetInfo:
    root_id: str
    parts: dict[str, int]  # part item id -> quantityInSet

    def to_dict(self) -> dict[str, Any]:
        return {"root_id": self.root_id, "parts": self.parts}


@dataclass
class Catalog:
    items: list[CatalogItem]
    version: str = ""
    fetched_at: float = 0.0
    sets: dict[str, SetInfo] = field(default_factory=dict)  # root id -> SetInfo
    standalone: set[str] = field(default_factory=set)  # item ids known to belong to no set

    def __post_init__(self) -> None:
        self.by_ref: dict[str, CatalogItem] = {}
        for it in self.items:
            # Skip empty gameRefs; on duplicates prefer the non-set entry (e.g. prisma_shade vs prisma_shade_set).
            if it.game_ref and (it.game_ref not in self.by_ref or "set" in self.by_ref[it.game_ref].tags):
                self.by_ref[it.game_ref] = it
        self.by_id = {it.id: it for it in self.items}
        self.by_slug = {it.slug: it for it in self.items}
        self._reindex_sets()

    def _reindex_sets(self) -> None:
        self.part_to_set: dict[str, str] = {}
        for root, s in self.sets.items():
            for pid in s.parts:
                self.part_to_set[pid] = root

    def add_set(self, s: SetInfo) -> None:
        self.sets[s.root_id] = s
        for pid in s.parts:
            self.part_to_set[pid] = s.root_id


# -- persistence -----------------------------------------------------------------------------------


def _catalog_file(cfg: Config) -> Path:
    return cfg.cache_dir / "catalog.json"


def _sets_file(cfg: Config) -> Path:
    return cfg.cache_dir / "sets.json"


def _load_sets(cfg: Config, catalog: Catalog) -> None:
    p = _sets_file(cfg)
    if not p.exists():
        return
    d = json.loads(p.read_text(encoding="utf-8"))
    if d.get("catalog_version") != catalog.version:
        return
    if time.time() - d.get("fetched_at", 0) > cfg.cache_ttl_minutes.sets * 60:
        return
    for s in d.get("sets", []):
        catalog.add_set(SetInfo(s["root_id"], {k: int(v) for k, v in s["parts"].items()}))
    catalog.standalone = set(d.get("standalone", []))


def save_sets(cfg: Config, catalog: Catalog) -> None:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    _sets_file(cfg).write_text(
        json.dumps(
            {
                "catalog_version": catalog.version,
                "fetched_at": time.time(),
                "sets": [s.to_dict() for s in catalog.sets.values()],
                "standalone": sorted(catalog.standalone),
            }
        ),
        encoding="utf-8",
    )


def load_cached_catalog(cfg: Config) -> Catalog | None:
    """Catalogue from disk only (no network), or None if never fetched."""
    p = _catalog_file(cfg)
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    cat = Catalog([CatalogItem.from_api(x) for x in d["items"]], d.get("version", ""), d.get("fetched_at", 0))
    _load_sets(cfg, cat)
    return cat


def fetch_catalog(client: WfmClient, cfg: Config, refresh: bool = False) -> Catalog:
    """Catalogue, re-downloaded only when /v2/versions changes, the TTL expires, or refresh is set."""
    versions = v2_data(client.get_json("/v2/versions", ttl_minutes=60, refresh=refresh)) or {}
    version = ((versions.get("collections") or {}).get("items")) or versions.get("updatedAt") or ""
    cached = load_cached_catalog(cfg)
    if (
        cached
        and not refresh
        and cached.version == version
        and time.time() - cached.fetched_at < cfg.cache_ttl_minutes.catalog * 60
    ):
        return cached
    raw = v2_data(client.get_json("/v2/items")) or []
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    _catalog_file(cfg).write_text(json.dumps({"version": version, "fetched_at": now, "items": raw}), encoding="utf-8")
    cat = Catalog([CatalogItem.from_api(x) for x in raw], version, now)
    if not refresh:
        _load_sets(cfg, cat)
    return cat


def fetch_sets(
    client: WfmClient,
    cfg: Config,
    catalog: Catalog,
    items: Iterable[CatalogItem],
    refresh: bool = False,
    progress: Callable[[CatalogItem], None] | None = None,
) -> None:
    """Make sure set composition is known for each given (prime) item. One call per unseen set."""
    if refresh:
        catalog.sets.clear()
        catalog.standalone.clear()
        catalog._reindex_sets()
    changed = False
    for it in items:
        if it.id in catalog.part_to_set or it.id in catalog.standalone or it.id in catalog.sets:
            if progress:
                progress(it)
            continue
        data = v2_data(client.get_json(f"/v2/item/{it.slug}/set", allow_404=True))
        changed = True
        if progress:
            progress(it)
        members = (data or {}).get("items") or []
        root = next((m for m in members if m.get("setRoot")), None)
        if root is None or len(members) <= 1:
            catalog.standalone.add(it.id)
            continue
        parts = {m["id"]: int(m.get("quantityInSet") or 1) for m in members if not m.get("setRoot")}
        catalog.add_set(SetInfo(root["id"], parts))
    if changed or refresh:
        save_sets(cfg, catalog)

"""Riven market data: WFM riven catalogue, WFM riven auctions, DE's weekly riven trade stats."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from wfopt.config import Config
from wfopt.market.client import WfmClient, WfmError, v2_data

DE_WEEKLY_URL = "https://www-static.warframe.com/repos/weekly{name}.json"
DE_PLATFORM = {"pc": "RivensPC", "ps4": "RivensPS4", "xbox": "RivensXB1", "switch": "RivensSWI"}
AUCTION_SEARCH_PAGE = "https://warframe.market/auctions/search"

# Melee rivens use melee-specific tags for stats WFM files under the general attribute.
TAG_ALIASES = {
    "WeaponMeleeDamageMod": "WeaponDamageAmountMod",
    "WeaponMeleeFactionDamageCorpus": "WeaponFactionDamageCorpus",
    "WeaponMeleeFactionDamageGrineer": "WeaponFactionDamageGrineer",
    "WeaponMeleeFactionDamageInfested": "WeaponFactionDamageInfested",
}


@dataclass(frozen=True)
class RivenWeapon:
    slug: str
    game_ref: str
    name: str
    riven_type: str
    disposition: float | None


@dataclass(frozen=True)
class RivenAttribute:
    slug: str
    game_ref: str
    name: str


@dataclass
class RivenCatalog:
    weapons: dict[str, RivenWeapon]  # by gameRef
    attributes: dict[str, RivenAttribute]  # by tag (gameRef)

    def attribute(self, tag: str) -> RivenAttribute | None:
        return self.attributes.get(tag) or self.attributes.get(TAG_ALIASES.get(tag, ""))


@dataclass
class Auction:
    id: str
    price: int  # buyout, or the current bid / starting price for auctions without buyout
    positives: tuple[str, ...]
    negatives: tuple[str, ...]
    rerolls: int
    owner: str
    status: str


def fetch_riven_catalog(client: WfmClient, cfg: Config, refresh: bool = False) -> RivenCatalog:
    ttl = cfg.cache_ttl_minutes.riven_catalog
    weapons = v2_data(client.get_json("/v2/riven/weapons", ttl_minutes=ttl, refresh=refresh)) or []
    attrs = v2_data(client.get_json("/v2/riven/attributes", ttl_minutes=ttl, refresh=refresh)) or []

    def name(d: dict) -> str:
        return ((d.get("i18n") or {}).get("en") or {}).get("name") or d.get("slug", "")

    return RivenCatalog(
        {w["gameRef"]: RivenWeapon(w["slug"], w["gameRef"], name(w), w.get("rivenType", ""), w.get("disposition"))
         for w in weapons if w.get("gameRef")},
        {a["gameRef"]: RivenAttribute(a["slug"], a["gameRef"], name(a)) for a in attrs if a.get("gameRef")},
    )


# -- DE weekly riven trades ------------------------------------------------------------------------

_TOKEN = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"|([A-Za-z_]\w*)(\s*:)")


def parse_js_literal(text: str) -> Any:
    """DE serves the weekly file as a JS object literal (unquoted keys, single quotes)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    def sub(m: re.Match) -> str:
        if m.group(1) is not None:
            return json.dumps(m.group(1).replace("\\'", "'"))
        if m.group(2) is not None:
            return m.group(0)
        return json.dumps(m.group(3)) + m.group(4)

    return json.loads(_TOKEN.sub(sub, text))


@dataclass(frozen=True)
class WeeklyStat:
    weapon: str  # DE display name, lower-cased
    rerolled: bool
    median: float
    avg: float
    min: float
    max: float
    pop: float


def fetch_weekly(client: WfmClient, cfg: Config, refresh: bool = False) -> dict[tuple[str, bool], WeeklyStat] | None:
    url = DE_WEEKLY_URL.format(name=DE_PLATFORM.get(cfg.wfm.platform, "RivensPC"))
    try:
        text = client.get_json(url, ttl_minutes=cfg.cache_ttl_minutes.riven_weekly, refresh=refresh, text=True)
        rows = parse_js_literal(text)
    except (WfmError, ValueError):
        return None
    out: dict[tuple[str, bool], WeeklyStat] = {}
    for r in rows or []:
        if not r.get("compatibility"):
            continue  # veiled rivens
        w = str(r["compatibility"]).strip().lower()
        out[(w, bool(r.get("rerolled")))] = WeeklyStat(
            w, bool(r.get("rerolled")), float(r.get("median") or 0), float(r.get("avg") or 0),
            float(r.get("min") or 0), float(r.get("max") or 0), float(r.get("pop") or 0),
        )
    return out


# -- WFM riven auctions ----------------------------------------------------------------------------


def search_params(weapon: str, positives: list[str] | None = None, negative: str | None = None) -> dict[str, str]:
    """`negative`: an attribute slug, "none" for no negative, or None for any."""
    p = {"type": "riven", "weapon_url_name": weapon, "sort_by": "price_asc"}
    if positives:
        p["positive_stats"] = ",".join(sorted(positives))
    if negative:
        p["negative_stats"] = negative
    return p


def search_page_url(params: dict[str, str]) -> str:
    """The same search on the WFM website, for the report."""
    q = {k: v for k, v in params.items() if k != "type"}
    return f"{AUCTION_SEARCH_PAGE}?{urlencode({'type': 'riven', **q, 'polarity': 'any'}, safe=',/')}"


def search_auctions(client: WfmClient, cfg: Config, params: dict[str, str], refresh: bool = False) -> list[Auction] | None:
    try:
        body = client.get_json(
            "/v1/auctions/search", params=params, ttl_minutes=cfg.cache_ttl_minutes.auctions, refresh=refresh
        )
    except WfmError:
        return None
    own = cfg.wfm.ingame_name.lower()
    out = []
    for a in (body or {}).get("payload", {}).get("auctions", []):
        if a.get("closed") or not a.get("visible", True):
            continue
        owner = a.get("owner") or {}
        if own and owner.get("ingame_name", "").lower() == own:
            continue
        price = a.get("buyout_price")
        if not price:
            continue  # bid-only auctions (often starting at 1p) say nothing about what the riven sells for
        attrs = (a.get("item") or {}).get("attributes") or []
        out.append(
            Auction(
                a.get("id", ""),
                int(price),
                tuple(x["url_name"] for x in attrs if x.get("positive")),
                tuple(x["url_name"] for x in attrs if not x.get("positive")),
                int((a.get("item") or {}).get("re_rolls") or 0),
                owner.get("ingame_name", ""),
                owner.get("status", ""),
            )
        )
    return out

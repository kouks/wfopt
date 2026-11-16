"""Configuration: config.toml (optional) layered over built-in defaults, plus .env secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class InventoryCfg:
    path: str = ""
    max_age_hours: float = 2


@dataclass
class WfmCfg:
    platform: str = "pc"
    crossplay: bool = True
    language: str = "en"
    ingame_name: str = ""
    user_agent: str = "wf-trade-optimizer/0.1 (local personal trade report tool)"
    rps: float = 2.5


@dataclass
class CacheTtlCfg:
    catalog: float = 10080
    sets: float = 10080
    statistics: float = 360
    top_orders: float = 10


@dataclass
class PricingCfg:
    strategy: str = "balanced"
    undercut: int = 0
    outlier_floor: float = 0.6
    orderbook_min_plat: float = 8


@dataclass
class ValuationCfg:
    trade_cost_plat: float = 5
    daily_trades: int = 0
    ducat_value_plat: float = 0.08
    endo_value_per_1000: float = 2.5
    bulk_units_per_trade: int = 3
    horizon_days: float = 7
    share: float = 0.2
    liquidity_ref: float = 5


@dataclass
class ScopeCfg:
    categories: list[str] = field(default_factory=lambda: ["prime", "arcane", "mod"])
    arcane_ranked_copies_fusable: bool = True


@dataclass
class SyncCfg:
    post_visible: bool = True
    manage_only_tool_orders: bool = True


@dataclass
class Config:
    inventory: InventoryCfg = field(default_factory=InventoryCfg)
    wfm: WfmCfg = field(default_factory=WfmCfg)
    cache_ttl_minutes: CacheTtlCfg = field(default_factory=CacheTtlCfg)
    pricing: PricingCfg = field(default_factory=PricingCfg)
    valuation: ValuationCfg = field(default_factory=ValuationCfg)
    scope: ScopeCfg = field(default_factory=ScopeCfg)
    sync: SyncCfg = field(default_factory=SyncCfg)

    # Working directories (not from TOML)
    root: Path = field(default_factory=Path.cwd)

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    def inventory_path(self) -> Path:
        if self.inventory.path:
            return Path(self.inventory.path).expanduser()
        local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(local) / "AlecaFrame" / "lastData.dat"

    def as_flat_dict(self) -> dict[str, Any]:
        """Flattened `section.key -> value` view, for the report header."""
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if is_dataclass(v):
                for sf in fields(v):
                    out[f"{f.name}.{sf.name}"] = getattr(v, sf.name)
        return out


def _apply(section: Any, values: dict[str, Any], name: str) -> None:
    known = {f.name: f for f in fields(section)}
    for k, v in values.items():
        if k not in known:
            raise ValueError(f"Unknown config key [{name}] {k}")
        setattr(section, k, v)


def load_config(path: Path | None = None, root: Path | None = None) -> Config:
    """Load config.toml (if present) over defaults and read .env into the environment."""
    root = root or Path.cwd()
    cfg = Config(root=root)
    path = path or root / "config.toml"
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        for section_name, values in data.items():
            if not hasattr(cfg, section_name) or not isinstance(values, dict):
                raise ValueError(f"Unknown config section [{section_name}]")
            _apply(getattr(cfg, section_name), values, section_name)
    env = root / ".env"
    if env.exists():
        from dotenv import load_dotenv

        load_dotenv(env, override=False)
    if cfg.pricing.strategy not in {"quick", "balanced", "patient"}:
        raise ValueError(f"pricing.strategy must be quick|balanced|patient, got {cfg.pricing.strategy!r}")
    return cfg

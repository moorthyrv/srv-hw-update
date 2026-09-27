"""Settings with defaults, optionally overridden by a YAML config file."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_WEIGHTS = {
    "BIOS": 5.0, "BMC": 5.0, "CPLD": 3.0, "Storage": 3.0,
    "NIC": 2.0, "Drive": 1.0, "PSU": 1.0, "Other": 1.0,
}


@dataclass
class Scoring:
    """How the priority score is computed. See README 'Priority score'."""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    age_cap_years: float = 3.0  # age beyond this adds nothing more
    unknown_age_years: float = 1.0  # assumed age when a behind component has no date
    unknown_factor: float = 0.5  # share of the weight added for 'unknown' components
    noncompliant_multiplier: float = 1.5  # extra for components below target/baseline
    environment_multipliers: dict[str, float] = field(default_factory=dict)  # e.g. {prod: 1.5}


@dataclass
class Settings:
    target_policy: str = "n-1"  # latest | n-1 (used when no baseline applies)
    workers: int = 25
    timeout: float = 30.0
    connect_timeout: float = 10.0
    retries: int = 3
    verify_tls: bool | str = False
    collect_hpe_drives: bool = True
    dell_catalog_url: str = "https://downloads.dell.com/catalog/Catalog.xml.gz"
    dell_catalog_max_age_days: float = 7.0
    cache_dir: str = "cache"
    scoring: Scoring = field(default_factory=Scoring)

    @classmethod
    def load(cls, path: str | Path | None) -> "Settings":
        s = cls()
        if not path:
            return s
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        scoring = data.pop("scoring", None) or {}
        for k, v in data.items():
            if not hasattr(s, k):
                raise ValueError(f"{path}: unknown setting '{k}'")
            setattr(s, k, v)
        weights = copy.deepcopy(DEFAULT_WEIGHTS)
        weights.update({str(k): float(v) for k, v in (scoring.pop("weights", None) or {}).items()})
        s.scoring = Scoring(weights=weights, **scoring)
        if s.target_policy not in ("latest", "n-1"):
            raise ValueError(f"{path}: target_policy must be 'latest' or 'n-1'")
        return s

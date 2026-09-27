"""Per-model target versions (baselines.yaml) and Dell validated-stack catalogs.

baselines.yaml::

    dell:
      "PowerEdge R640":            # model, matched ignoring 'PowerEdge', case and spaces
        BIOS: "2.19.1"             # a category applies to every component in it
        BMC: "7.00.00.173"
        "25227": "7.00.00.173"     # a Dell component ID
        "PERC H740P": "51.16.0-5150"   # otherwise a regex on the component name
      "gen:14G":                   # generation-wide default, used if no model entry matches
        BMC: "7.00.00.173"
    hpe:
      "ProLiant DL380 Gen10":
        BIOS: "3.40"
        BMC: "3.10"
        "P408i-a": "5.61"
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..catalog.dell_catalog import load_bundle_baselines, model_key
from ..models import CATEGORIES, Component, ServerResult

log = logging.getLogger(__name__)


@dataclass
class Target:
    version: str
    basis: str


class Baselines:
    def __init__(self, data: dict | None = None, path: str = ""):
        self.path = path
        self._by_vendor: dict[str, dict[str, dict[str, str]]] = {}
        for vendor, models in (data or {}).items():
            vmap = {}
            for model, targets in (models or {}).items():
                key = str(model) if str(model).startswith("gen:") else model_key(str(model))
                vmap[key] = {str(k): str(v) for k, v in (targets or {}).items()}
            self._by_vendor[str(vendor).lower()] = vmap
        self.bundles: list[tuple[str, str, dict[str, dict[str, str]]]] = []  # (basis, applies_to, data)

    @classmethod
    def load(cls, path: str | Path | None) -> "Baselines":
        if not path:
            return cls()
        if not Path(path).exists():
            log.warning("Baselines file %s not found; compliance uses the target policy only", path)
            return cls()
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}, str(path))

    def add_bundle_catalog(self, path: str | Path, applies_to: str) -> None:
        """applies_to: 'os=esxi' or 'platform=vsan' - which servers the bundle baseline covers."""
        self.bundles.append((Path(path).name, applies_to, load_bundle_baselines(path)))

    def target(self, comp: Component, server: ServerResult) -> Target | None:
        vmap = self._by_vendor.get(server.vendor, {})
        entry = vmap.get(model_key(server.model)) or vmap.get(f"gen:{server.generation}")
        if entry:
            t = _match_entry(entry, comp)
            if t:
                return Target(t, "baselines.yaml")
        if server.vendor == "dell" and comp.component_id:
            for basis, applies_to, data in self.bundles:
                field_name, _, want = applies_to.partition("=")
                if (getattr(server, field_name, "") or "").lower() != want.lower():
                    continue
                models = data.get(server.system_id) or data.get(model_key(server.model)) or {}
                if comp.component_id in models:
                    return Target(models[comp.component_id], basis)
        return None


def _match_entry(entry: dict[str, str], comp: Component) -> str | None:
    if comp.component_id and comp.component_id in entry:
        return entry[comp.component_id]
    for key, ver in entry.items():
        if key in CATEGORIES or key.isdigit():
            continue
        try:
            if re.search(key, comp.name, re.I):
                return ver
        except re.error:
            if key.lower() in comp.name.lower():
                return ver
    return entry.get(comp.category)

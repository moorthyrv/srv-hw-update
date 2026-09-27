"""Dell firmware catalog (Catalog.xml / Catalog.xml.gz).

The Lifecycle Controller catalog lists the current BIOS/firmware releases per
component ID and system model. A single catalog carries little history, so
every downloaded catalog is archived and all archived catalogs are merged: the
version history grows over time and "versions behind" becomes more accurate.

The ESXi / vSAN validated-stack catalogs have the same format plus
``SoftwareBundle`` entries per model; :func:`load_bundle_baselines` turns
those into per-model baselines.
"""

from __future__ import annotations

import gzip
import logging
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..versions import parse_date, version_key

log = logging.getLogger(__name__)

DEFAULT_URL = "https://downloads.dell.com/catalog/Catalog.xml.gz"
FIRMWARE_TYPES = {"FRMW", "BIOS"}


def model_key(model: str) -> str:
    """'PowerEdge R650 XS' -> 'r650xs'; 'VxRail P570F' -> 'vxrailp570f'."""
    m = re.sub(r"^\s*(dell\s+)?(emc\s+)?poweredge\s*", "", model or "", flags=re.I)
    return re.sub(r"[\s_-]+", "", m).lower()


@dataclass(frozen=True)
class CatalogVersion:
    version: str
    release_date: date | None
    name: str
    package: str
    criticality: str = ""


@dataclass
class _Entry:
    component_ids: set[str]
    system_ids: set[str]
    models: set[str]
    version: CatalogVersion


@dataclass
class DellCatalog:
    sources: list[str] = field(default_factory=list)
    catalog_versions: list[str] = field(default_factory=list)
    # component_id -> entries
    _by_component: dict[str, list[_Entry]] = field(default_factory=dict)

    # --------------------------------------------------------------- loading
    @classmethod
    def load(cls, paths: list[str | Path]) -> "DellCatalog":
        cat = cls()
        for p in paths:
            cat.add_file(p)
        return cat

    def add_file(self, path: str | Path) -> None:
        root = _parse(path)
        self.sources.append(str(path))
        self.catalog_versions.append(root.get("version", ""))
        n = 0
        for sc in root.iter("SoftwareComponent"):
            ctype = _attr(sc.find("ComponentType"), "value")
            if ctype not in FIRMWARE_TYPES:
                continue
            version = sc.get("vendorVersion") or sc.get("dellVersion") or ""
            if not version:
                continue
            comp_ids = {d.get("componentID") for d in sc.iter("Device") if d.get("componentID")}
            if not comp_ids:
                continue
            system_ids = {m.get("systemID", "").upper() for m in sc.iter("Model") if m.get("systemID")}
            models = {model_key(_display(m)) for m in sc.iter("Model") if _display(m)}
            cv = CatalogVersion(
                version=version,
                release_date=parse_date(sc.get("releaseDate")) or parse_date(sc.get("dateTime")),
                name=_display(sc.find("Name")),
                package=(sc.get("path") or "").rsplit("/", 1)[-1],
                criticality=_display(sc.find("Criticality")),
            )
            entry = _Entry(comp_ids, system_ids, models, cv)
            for cid in comp_ids:
                self._by_component.setdefault(cid, []).append(entry)
            n += 1
        log.info("Loaded Dell catalog %s (version %s): %d firmware packages", path, root.get("version"), n)

    # ---------------------------------------------------------------- lookup
    def versions(self, component_id: str, system_id: str = "", model: str = "") -> list[CatalogVersion]:
        """All known releases for a component on a model, oldest first, one per version."""
        if not component_id:
            return []
        sid = (system_id or "").upper()
        mk = model_key(model)
        found: dict[str, CatalogVersion] = {}
        for e in self._by_component.get(component_id, []):
            if (sid and sid in e.system_ids) or (mk and mk in e.models) or (not e.system_ids and not e.models):
                prev = found.get(e.version.version)
                if prev is None or (e.version.release_date and (prev.release_date is None or e.version.release_date < prev.release_date)):
                    found[e.version.version] = e.version
        return sorted(found.values(), key=lambda v: version_key(v.version))

    def __len__(self) -> int:
        return len(self._by_component)


def _attr(el: ET.Element | None, name: str) -> str:
    return el.get(name, "") if el is not None else ""


def _display(el: ET.Element | None) -> str:
    if el is None:
        return ""
    d = el.find("Display")
    return (d.text or "").strip() if d is not None and d.text else ""


def _parse(path: str | Path) -> ET.Element:
    p = Path(path)
    with open(p, "rb") as fh:
        magic = fh.read(2)
    data = gzip.open(p).read() if magic == b"\x1f\x8b" else p.read_bytes()
    return ET.fromstring(data)


# ------------------------------------------------------------------ bundles
def load_bundle_baselines(path: str | Path) -> dict[str, dict[str, str]]:
    """Per-model baselines from an ESXi/vSAN validated-stack catalog.

    Returns {systemID or model key: {component_id: version}}.
    """
    root = _parse(path)
    by_package: dict[str, tuple[set[str], str]] = {}
    for sc in root.iter("SoftwareComponent"):
        ctype = _attr(sc.find("ComponentType"), "value")
        if ctype not in FIRMWARE_TYPES:
            continue
        pkg = (sc.get("path") or "").rsplit("/", 1)[-1]
        comp_ids = {d.get("componentID") for d in sc.iter("Device") if d.get("componentID")}
        by_package[pkg] = (comp_ids, sc.get("vendorVersion") or "")
    out: dict[str, dict[str, str]] = {}
    for b in root.iter("SoftwareBundle"):
        targets = []
        for m in b.iter("Model"):
            if m.get("systemID"):
                targets.append(m.get("systemID").upper())
            if _display(m):
                targets.append(model_key(_display(m)))
        comps: dict[str, str] = {}
        for pkg in b.iter("Package"):
            name = (pkg.get("path") or "").rsplit("/", 1)[-1]
            if name in by_package:
                ids, ver = by_package[name]
                for cid in ids:
                    if cid not in comps or version_key(ver) > version_key(comps[cid]):
                        comps[cid] = ver
        for t in targets:
            out.setdefault(t, {}).update(comps)
    log.info("Loaded %d model baselines from %s", len(out), path)
    return out


# ---------------------------------------------------------------- caching
def catalog_paths(cache_dir: str | Path) -> list[Path]:
    """Every archived catalog (the current one is always archived too), for version history."""
    d = Path(cache_dir)
    archived = sorted((d / "archive").glob("Catalog-*.xml.gz")) if (d / "archive").is_dir() else []
    if archived:
        return archived
    cur = d / "Catalog.xml.gz"
    return [cur] if cur.exists() else []


def import_catalog(src: str | Path, cache_dir: str | Path) -> Path:
    """Copy a pre-downloaded catalog into the cache and archive it (offline mode)."""
    d = Path(cache_dir)
    (d / "archive").mkdir(parents=True, exist_ok=True)
    src = Path(src)
    root = _parse(src)
    ver = re.sub(r"[^\w.-]", "_", root.get("version") or time.strftime("%Y%m%d"))
    tmp = d / "Catalog.xml.gz.tmp"
    with open(src, "rb") as fh:
        is_gz = fh.read(2) == b"\x1f\x8b"
    if is_gz:
        shutil.copyfile(src, tmp)
    else:
        with open(src, "rb") as fin, gzip.open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    tmp.replace(d / "Catalog.xml.gz")
    arch = d / "archive" / f"Catalog-{ver}.xml.gz"
    if not arch.exists():
        shutil.copyfile(d / "Catalog.xml.gz", arch)
    return d / "Catalog.xml.gz"


def refresh_catalog(
    cache_dir: str | Path,
    *,
    url: str = DEFAULT_URL,
    max_age_days: float = 7,
    force: bool = False,
    verify: bool | str = True,
    timeout: float = 120,
) -> Path | None:
    """Download the catalog if the cached copy is missing or older than max_age_days.

    This is internet traffic (not BMC traffic), so the corporate proxy from
    the environment is honoured here. Returns the cached path, or None if no
    catalog is available.
    """
    import requests  # local import keeps module import cheap

    d = Path(cache_dir)
    cur = d / "Catalog.xml.gz"
    if cur.exists() and not force and (time.time() - cur.stat().st_mtime) < max_age_days * 86400:
        return cur
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "download.tmp"
    try:
        with requests.get(url, stream=True, timeout=timeout, verify=verify) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        return import_catalog(tmp, d)
    except Exception as e:  # noqa: BLE001
        log.warning("Dell catalog download failed (%s); using cached copy if present", e)
        return cur if cur.exists() else None
    finally:
        tmp.unlink(missing_ok=True)

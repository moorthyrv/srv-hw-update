"""HPE reference data (hpe_reference.yaml).

Nothing is downloaded from HPE at run time. The file is maintained by the
operator, optionally seeded by ``fwtool hpe-reference import`` from HPE SDR
``fwpp-*`` repository metadata (primary.xml.gz) that the operator downloaded.

File layout::

    spp_level: "2025.09.00"          # optional, informational
    components:
      system-rom:U30:                 # key
        category: BIOS
        description: HPE ProLiant DL380 Gen10 (U30) Servers
        source: fwpp-gen10            # 'manual' entries are never overwritten by import
        versions:                     # newest first
          - {version: "3.66", date: 2026-04-01}
          - {version: "3.64", date: 2026-02-05, spp: "2026.03.00"}
      smartarray:p408i:
        category: Storage
        match: ['P408i-a', 'P408i-p']  # regexes tried against the component name
        generations: [Gen10, Gen10 Plus]
        versions: [...]

Keys the tool looks up automatically:
  system-rom:<ROM family>  BIOS, family from the version string (U30, P89, U54 ...)
  ilo4 / ilo5 / ilo6       BMC
  ie:<gen>                 Innovation Engine, gen like gen10, gen10plus
  sps:<ROM family>, sps:<gen>  Server Platform Services
  drive:<drive model>      drive firmware, e.g. drive:MB4000GVYZA
Any other entry is matched with its ``match`` regex list.
"""

from __future__ import annotations

import gzip
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from ..models import Component, ServerResult
from ..versions import compare, hpe_rom, parse_date, version_key

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefVersion:
    version: str
    date: date | None = None
    spp: str = ""


@dataclass
class RefEntry:
    key: str
    category: str = ""
    description: str = ""
    versions: list[RefVersion] = field(default_factory=list)  # newest first
    match: list[re.Pattern] = field(default_factory=list)
    generations: list[str] = field(default_factory=list)
    source: str = ""

    def index_of(self, version: str) -> int | None:
        for i, v in enumerate(self.versions):
            if compare(v.version, version) == 0:
                return i
        return None


def gen_slug(generation: str) -> str:
    return re.sub(r"\s+", "", generation or "").lower()


class HpeReference:
    def __init__(self, entries: dict[str, RefEntry] | None = None, spp_level: str = "", path: str = ""):
        self.entries = entries or {}
        self.spp_level = spp_level
        self.path = path
        self._regex_entries = [e for e in self.entries.values() if e.match]

    @classmethod
    def load(cls, path: str | Path | None) -> "HpeReference":
        if not path or not Path(path).exists():
            if path:
                log.warning("HPE reference file %s not found; HPE components will be no-reference", path)
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        entries: dict[str, RefEntry] = {}
        for key, raw in (data.get("components") or {}).items():
            raw = raw or {}
            versions = []
            for v in raw.get("versions") or []:
                if isinstance(v, dict) and v.get("version") is not None:
                    versions.append(RefVersion(str(v["version"]), parse_date(v.get("date")), str(v.get("spp") or "")))
                elif isinstance(v, (str, int, float)):
                    versions.append(RefVersion(str(v)))
            if raw.get("latest") and not versions:
                versions.append(RefVersion(str(raw["latest"]), parse_date(raw.get("date"))))
            patterns = []
            for m in raw.get("match") or []:
                try:
                    patterns.append(re.compile(str(m), re.I))
                except re.error as e:
                    log.warning("hpe_reference %s: bad match regex %r: %s", key, m, e)
            entries[str(key)] = RefEntry(
                key=str(key),
                category=raw.get("category", ""),
                description=raw.get("description", ""),
                versions=versions,
                match=patterns,
                generations=[gen_slug(g) for g in raw.get("generations") or []],
                source=raw.get("source", ""),
            )
        log.info("Loaded HPE reference %s: %d entries", path, len(entries))
        return cls(entries, str(data.get("spp_level") or ""), str(path))

    def find(self, comp: Component, server: ServerResult) -> RefEntry | None:
        gen = gen_slug(server.generation)
        fam = hpe_rom(server.bios_version)[0]
        candidates: list[str] = []
        if comp.ref_key:
            if comp.ref_key in ("ie", "sps"):
                if fam:
                    candidates.append(f"{comp.ref_key}:{fam}")
                candidates.append(f"{comp.ref_key}:{gen}")
            else:
                candidates.append(comp.ref_key)
        if comp.category == "Drive" and comp.name.startswith("Drive "):
            candidates.append(f"drive:{comp.name[6:].strip().upper()}")
        for key in candidates:
            e = self.entries.get(key)
            if e and e.versions:
                return e
        for e in self._regex_entries:
            if e.generations and gen not in e.generations:
                continue
            if e.category and e.category != comp.category:
                continue
            if any(p.search(comp.name) for p in e.match) and e.versions:
                return e
        return None


# ---------------------------------------------------------------- importer
_NS = {"c": "http://linux.duke.edu/metadata/common", "rpm": "http://linux.duke.edu/metadata/rpm"}
_CTRL_TOKEN = re.compile(r"\b((?:P|E|H|SR|MR)\d{3}[a-z]{0,2}(?:-[a-z])?)\b", re.I)
MAX_VERSIONS = 25


def _rom_version(ver: str) -> tuple[str, date | None]:
    """'2.76_2023_02_09' -> ('2.76', 2023-02-09); '1.32_03_05_2015' -> ('1.32', 2015-03-05)."""
    parts = ver.split("_")
    if len(parts) == 4:
        a, b, c = parts[1:]
        try:
            if len(a) == 4:
                return parts[0], date(int(a), int(b), int(c))
            return parts[0], date(int(c), int(a), int(b))
        except ValueError:
            pass
    return parts[0], None


def _classify(name: str, summary: str, provides: list[str]) -> list[tuple[str, str, dict]]:
    """Map one fwpp package to reference keys: [(key, category, extra)]."""
    n = name.lower()
    m = re.match(r"^(?:hp-)?firmware-system-([a-z]\d{2})$", n)
    if m:
        return [(f"system-rom:{m.group(1).upper()}", "BIOS", {})]
    m = re.match(r"^(?:hp-)?firmware-ilo(\d)$", n)
    if m:
        return [(f"ilo{m.group(1)}", "BMC", {})]
    m = re.match(r"^firmware-ie(gen\d+(?:plus)?)$", n)
    if m:
        return [(f"ie:{m.group(1)}", "Other", {})]
    m = re.match(r"^firmware-sps(gen\d+(?:plus)?)$", n)
    if m:
        return [(f"sps:{m.group(1)}", "Other", {})]
    m = re.match(r"^firmware-([a-z]\d{2})_me$", n)
    if m:
        return [(f"sps:{m.group(1).upper()}", "Other", {})]
    drives = [p.split(":")[-1].rstrip(")") for p in provides
              if re.match(r"firmware\(hp:sd:(sas|sata|nvme):", p)]
    if drives:
        return [(f"drive:{d.upper()}", "Drive", {}) for d in drives]
    if "smartarray" in n or re.search(r"Smart Array|Smart HBA|\bArray\b|\bHBA\b|Tri.?Mode|\b[MS]R\d{3}", summary):
        tokens = sorted({t.upper() for t in _CTRL_TOKEN.findall(summary)})
        if tokens:
            # One entry per controller family; old hp- and new package names merge.
            return [(f"storage:{tokens[0].lower()}", "Storage", {"match": [rf"\b{t}\b" for t in tokens]})]
    return []


def import_fwpp(paths: list[str | Path], existing: str | Path | None = None) -> dict:
    """Build hpe_reference.yaml content from fwpp primary.xml(.gz) files.

    Manual entries (``source: manual``) and ``spp_level`` from ``existing`` are kept.
    """
    collected: dict[str, dict] = {}
    for path in paths:
        p = Path(path)
        src = re.sub(r"-?primary.*$", "", p.name) or p.stem
        data = gzip.open(p).read() if p.read_bytes()[:2] == b"\x1f\x8b" else p.read_bytes()
        root = ET.fromstring(data)
        for pkg in root.findall("c:package", _NS):
            name = pkg.findtext("c:name", namespaces=_NS) or ""
            summary = (pkg.findtext("c:summary", namespaces=_NS) or "").strip()
            ver = pkg.find("c:version", _NS).get("ver", "")
            build = int((pkg.find("c:time", _NS).get("build") or 0))
            provides = [e.get("name", "") for e in pkg.findall("c:format/rpm:provides/rpm:entry", _NS)
                        if e.get("name", "").startswith("firmware(") and not e.get("flags")]
            for key, category, extra in _classify(name, summary, provides):
                v, d = (_rom_version(ver) if key.startswith("system-rom:") else (ver, None))
                d = d or (datetime.fromtimestamp(build, tz=timezone.utc).date() if build else None)
                entry = collected.setdefault(key, {
                    "category": category,
                    "description": re.sub(r"\s+firmware$", "", summary)[:150],
                    "source": set(),
                    "versions": {},
                    **extra,
                })
                entry["source"].add(src)
                if extra.get("match"):
                    entry["match"] = sorted(set(entry.get("match", [])) | set(extra["match"]))
                prev = entry["versions"].get(v)
                if prev is None or (d and (prev is None or d < prev)):
                    entry["versions"][v] = d

    components: dict[str, dict] = {}
    for key in sorted(collected):
        e = collected[key]
        vers = sorted(e["versions"].items(), key=lambda kv: (kv[1] or date.min, version_key(kv[0])), reverse=True)
        out = {
            "category": e["category"],
            "description": e["description"],
            "source": ",".join(sorted(e["source"])),
        }
        if e.get("match"):
            out["match"] = e["match"]
        out["versions"] = [{"version": v, "date": d.isoformat() if d else None} for v, d in vers[:MAX_VERSIONS]]
        components[key] = out

    spp_level = ""
    if existing and Path(existing).exists():
        old = yaml.safe_load(Path(existing).read_text(encoding="utf-8")) or {}
        spp_level = old.get("spp_level") or ""
        for key, raw in (old.get("components") or {}).items():
            if (raw or {}).get("source") == "manual":
                components[key] = raw
    return {
        "spp_level": spp_level,
        "generated": datetime.now(timezone.utc).date().isoformat(),
        "components": components,
    }


def write_reference(data: dict, path: str | Path) -> None:
    header = (
        "# HPE firmware reference for fwtool.\n"
        "# Entries with source: manual are yours and are never overwritten by\n"
        "# 'fwtool hpe-reference import'. Versions are listed newest first.\n"
    )
    Path(path).write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")

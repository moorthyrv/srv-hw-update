"""Version string parsing and comparison."""

from __future__ import annotations

import re
from datetime import date, datetime

_TOKEN = re.compile(r"\d+|[A-Za-z]+")
_NUMERIC_VERSION = re.compile(r"\d+(?:\.\d+)+|\d+")
_HPE_ROM_GEN8 = re.compile(r"^\s*([A-Z]\d{2})\s+(\d{1,2})/(\d{1,2})/(\d{4})\s*$", re.I)
_HPE_ROM = re.compile(r"^\s*([A-Z]\d{2})\s+v?(\d+\.\d+)\s*(?:\((\d{1,2})/(\d{1,2})/(\d{4})\))?", re.I)


def version_key(v: str) -> tuple:
    """Sort key that orders versions like 2.9 < 2.10 < 2.10.1 and A07 < A10.

    Numbers compare numerically; letters compare case-insensitively and sort
    before numbers in the same position so '1.0a' < '1.0.1'.
    """
    parts = []
    for tok in _TOKEN.findall(v or ""):
        if tok.isdigit():
            parts.append((1, int(tok), ""))
        else:
            parts.append((0, 0, tok.lower()))
    return tuple(parts)


def compare(a: str, b: str) -> int:
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def normalize(v: str) -> str:
    """Strip decoration so inventory and catalog versions compare equal."""
    return (v or "").strip()


def hpe_rom(version: str) -> tuple[str, str, date | None]:
    """Parse an HPE System ROM string like ``U30 v2.76 (02/09/2023)``.

    Returns (family, version, release date). Family/version are '' if the
    string does not look like an HPE ROM version.
    """
    m = _HPE_ROM.match(version or "")
    if not m:
        # Gen8 ROMs are versioned by date: 'P70 07/01/2015' -> ('P70', '2015.07.01', 2015-07-01)
        g8 = _HPE_ROM_GEN8.match(version or "")
        if not g8:
            return "", "", None
        try:
            d = date(int(g8.group(4)), int(g8.group(2)), int(g8.group(3)))
        except ValueError:
            return "", "", None
        return g8.group(1).upper(), d.strftime("%Y.%m.%d"), d
    fam, ver = m.group(1).upper(), m.group(2)
    d = None
    if m.group(5):
        try:
            d = date(int(m.group(5)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            d = None
    return fam, ver, d


def leading_version(v: str) -> str:
    """First dotted number in a string: 'iLO 5 v2.72 Sep 04 2022' -> '5'; use carefully."""
    m = _NUMERIC_VERSION.search(v or "")
    return m.group(0) if m else ""


def ilo_version(v: str) -> str:
    """'2.72 Sep 04 2022' or 'iLO 5 v2.72' -> '2.72'."""
    m = re.search(r"v?(\d+\.\d+)(?!\.)", re.sub(r"^\s*iLO\s*\d+\s*", "", v or "", flags=re.I))
    return m.group(1) if m else (v or "").strip()


def parse_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y_%m_%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None

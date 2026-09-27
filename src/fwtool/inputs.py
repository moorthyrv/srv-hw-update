"""Read the server list CSV exported from the CMDB."""

from __future__ import annotations

import csv
import ipaddress
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

KNOWN = ("name", "bmc_ip", "support", "site", "environment", "os", "platform", "vendor")
ALIASES = {
    "hostname": "name", "server": "name", "server_name": "name",
    "ip": "bmc_ip", "bmc": "bmc_ip", "idrac_ip": "bmc_ip", "ilo_ip": "bmc_ip", "bmc ip": "bmc_ip",
    "env": "environment",
}


@dataclass
class ServerRecord:
    name: str
    bmc_ip: str
    support: str = ""
    site: str = ""
    environment: str = ""
    os: str = ""
    platform: str = ""
    vendor: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class LoadReport:
    rows_read: int = 0
    blank: int = 0
    missing_ip: int = 0
    invalid_ip: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)


def _norm_header(h: str) -> str:
    key = (h or "").strip().lower().replace("-", "_")
    return ALIASES.get(key, key)


def _norm_vendor(v: str) -> str:
    v = (v or "").strip().lower()
    if v.startswith("dell"):
        return "dell"
    if v.startswith("hp") or "hewlett" in v:
        return "hpe"
    return ""


def load_servers(path: str | Path) -> tuple[list[ServerRecord], LoadReport]:
    """Load servers, tolerating a BOM, extra columns, blank rows and duplicate IPs.

    Duplicate IPs keep the first row and are listed in the load report.
    """
    rep = LoadReport()
    out: list[ServerRecord] = []
    seen: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        header: list[str] | None = None
        for row in reader:
            if not any(c.strip() for c in row):
                if header is not None:
                    rep.blank += 1
                continue
            if header is None:
                header = [_norm_header(h) for h in row]
                if "bmc_ip" not in header:
                    raise ValueError(f"{path}: CSV must have a 'bmc_ip' column (found: {', '.join(header)})")
                continue
            rep.rows_read += 1
            values = {header[i]: (row[i].strip() if i < len(row) else "") for i in range(len(header)) if header[i]}
            ip = values.get("bmc_ip", "")
            if not ip:
                rep.missing_ip += 1
                continue
            try:
                ip = str(ipaddress.ip_address(ip))
            except ValueError:
                # Allow DNS names but flag obviously broken values.
                if " " in ip or "/" in ip:
                    rep.invalid_ip.append(ip)
                    continue
            if ip in seen:
                rep.duplicates.append(f"{ip} ({values.get('name', '')} duplicates {seen[ip]})")
                continue
            seen[ip] = values.get("name", "") or ip
            support = values.get("support", "").lower()
            if support and support not in ("hpe", "tpm"):
                log.warning("Unknown support value %r for %s; expected hpe or tpm", support, ip)
            out.append(ServerRecord(
                name=values.get("name", "") or ip,
                bmc_ip=ip,
                support=support,
                site=values.get("site", ""),
                environment=values.get("environment", ""),
                os=values.get("os", "").lower(),
                platform=values.get("platform", "").lower(),
                vendor=_norm_vendor(values.get("vendor", "")),
                extra={k: v for k, v in values.items() if k not in KNOWN},
            ))
    return out, rep

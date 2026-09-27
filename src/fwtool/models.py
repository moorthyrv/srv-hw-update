"""Data model shared by collectors, analysis and reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

CATEGORIES = ("BIOS", "BMC", "CPLD", "Storage", "NIC", "Drive", "PSU", "Other")

# Collection status
OK = "ok"
PARTIAL = "partial"
FAILED = "failed"


@dataclass
class Component:
    category: str
    name: str
    version: str
    inventory_id: str = ""
    location: str = ""
    component_id: str = ""  # Dell catalog component ID
    updateable: bool | None = None
    source: str = ""  # which endpoint it came from
    ref_key: str = ""  # HPE reference key hint (e.g. system-rom:U30, ilo5)
    release_date: str = ""  # date embedded in the version string (HPE BIOS)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Component":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ServerResult:
    name: str
    bmc_ip: str
    support: str = ""
    site: str = ""
    environment: str = ""
    os: str = ""
    platform: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    vendor: str = ""  # dell | hpe | unknown
    manufacturer: str = ""
    model: str = ""
    system_id: str = ""  # Dell SystemID (hex, 4 digits)
    serial: str = ""
    service_tag: str = ""
    generation: str = ""
    bmc_type: str = ""
    bmc_version: str = ""
    bios_version: str = ""
    power_state: str = ""
    health: str = ""
    vxrail: bool = False
    vxrail_reason: str = ""

    collection_status: str = FAILED
    error_class: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    collected_at: str = ""
    duration_s: float = 0.0
    request_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ServerResult":
        d = dict(d)
        comps = [Component.from_dict(c) for c in d.pop("components", [])]
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        r = cls(**known)
        r.components = comps
        return r

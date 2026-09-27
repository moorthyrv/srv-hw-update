"""Flatten results + analysis into report rows."""

from __future__ import annotations

from ..analysis import ServerAnalysis
from ..models import FAILED, ServerResult
from ..redfish.errors import FIX_HINTS

SERVER_COLUMNS = [
    "rank", "name", "bmc_ip", "vendor", "model", "generation", "bmc_type", "bmc_version", "bmc_latest", "bmc_status",
    "bios_version", "bios_latest", "bios_status", "service_tag", "serial", "system_id", "power_state", "health",
    "vxrail", "appliance", "update_eligible", "support", "site", "environment", "os",
    "collection_status", "overall_status", "priority_score", "components_total", "components_behind",
    "components_noncompliant", "components_unknown", "components_no_reference",
    "oldest_component_age_days", "oldest_component", "error_class", "error", "fix_hint",
    "collected_at", "duration_s", "requests",
]

FIRMWARE_COLUMNS = [
    "name", "bmc_ip", "vendor", "model", "generation", "support", "vxrail", "category", "component",
    "installed_version", "latest_version", "installed_date", "latest_date", "age_days", "versions_behind",
    "behind_note", "status", "target_version", "compliance", "compliance_basis", "update_track",
    "location", "component_id", "inventory_id", "reference", "note", "score",
]


def update_eligible(r: ServerResult) -> str:
    if r.vxrail or r.appliance == "VxRail":
        return "no - VxRail (update via VxRail Manager only)"
    if r.appliance:
        return f"no - {r.appliance} (update via its own manager only)"
    return "yes"


def server_rows(pairs: list[tuple[ServerResult, ServerAnalysis]]) -> list[dict]:
    rows = []
    for r, a in pairs:
        hint = FIX_HINTS.get(r.error_class, "") if r.error_class else ""
        rows.append({
            "rank": None,
            "name": r.name, "bmc_ip": r.bmc_ip, "vendor": r.vendor, "model": r.model, "generation": r.generation,
            "bmc_type": r.bmc_type, "bmc_version": r.bmc_version, "bmc_latest": a.bmc_latest, "bmc_status": a.bmc_status,
            "bios_version": r.bios_version, "bios_latest": a.bios_latest, "bios_status": a.bios_status,
            "service_tag": r.service_tag, "serial": r.serial, "system_id": r.system_id,
            "power_state": r.power_state, "health": r.health,
            "vxrail": "yes" if r.vxrail else "no", "appliance": r.appliance,
            "update_eligible": update_eligible(r),
            "support": r.support, "site": r.site, "environment": r.environment, "os": r.os,
            "collection_status": r.collection_status, "overall_status": a.overall_status,
            "priority_score": a.priority_score, "components_total": a.components_total,
            "components_behind": a.components_behind, "components_noncompliant": a.components_noncompliant,
            "components_unknown": a.components_unknown, "components_no_reference": a.components_no_reference,
            "oldest_component_age_days": a.oldest_component_age_days, "oldest_component": a.oldest_component,
            "error_class": r.error_class, "error": r.error, "fix_hint": hint,
            "collected_at": r.collected_at, "duration_s": r.duration_s, "requests": r.request_count,
        })
    ranked = sorted((row for row in rows if row["collection_status"] != FAILED),
                    key=lambda x: (-x["priority_score"], -(x["oldest_component_age_days"] or 0), x["name"]))
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    failed = [row for row in rows if row["collection_status"] == FAILED]
    return ranked + sorted(failed, key=lambda x: x["name"])


def firmware_rows(pairs: list[tuple[ServerResult, ServerAnalysis]]) -> list[dict]:
    rows = []
    for r, a in pairs:
        for c, ca in zip(r.components, a.components):
            rows.append({
                "name": r.name, "bmc_ip": r.bmc_ip, "vendor": r.vendor, "model": r.model,
                "generation": r.generation, "support": r.support, "vxrail": "yes" if r.vxrail else "no",
                "category": c.category, "component": c.name, "installed_version": c.version,
                "latest_version": ca.latest_version, "installed_date": ca.installed_date, "latest_date": ca.latest_date,
                "age_days": ca.age_days, "versions_behind": ca.versions_behind, "behind_note": ca.behind_note,
                "status": ca.status, "target_version": ca.target_version, "compliance": ca.compliance,
                "compliance_basis": ca.compliance_basis, "update_track": ca.update_track,
                "location": c.location, "component_id": c.component_id, "inventory_id": c.inventory_id,
                "reference": ca.reference, "note": ca.note, "score": ca.score,
            })
    return rows

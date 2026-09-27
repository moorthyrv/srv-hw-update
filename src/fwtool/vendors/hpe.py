"""HPE iLO 4 / iLO 5 / iLO 6 adapter."""

from __future__ import annotations

import logging
import re

from ..models import Component, ServerResult
from ..normalize import categorize_hpe, hpe_bmc_type, hpe_generation
from ..redfish.client import RedfishClient, members, odata_id
from ..redfish.errors import AUTH, FORBIDDEN, RedfishError
from ..versions import hpe_rom, ilo_version
from .base import VendorAdapter, fetch_collection, first_member, link

log = logging.getLogger(__name__)


def hpe_ref_key(category: str, name: str, version: str, bmc_type: str) -> tuple[str, str]:
    """Return (reference key, release date ISO) for well-known HPE components."""
    if category == "BIOS":
        fam, _, d = hpe_rom(version)
        return (f"system-rom:{fam}" if fam else ""), (d.isoformat() if d else "")
    if category == "BMC":
        m = re.search(r"iLO\s*(\d+)", f"{name} {bmc_type}", re.I)
        return (f"ilo{m.group(1)}" if m else ""), ""
    if re.search(r"Innovation Engine", name, re.I):
        return "ie", ""
    if re.search(r"Server Platform Services|\bSPS\b", name, re.I):
        return "sps", ""
    return "", ""


def hpe_display_version(category: str, version: str) -> str:
    """Comparable version: 'U30 v2.76 (02/09/2023)' -> '2.76', '2.72 Sep 04 2022' -> '2.72'."""
    if category == "BIOS":
        _, ver, _ = hpe_rom(version)
        return ver or version
    if category == "BMC":
        return ilo_version(version)
    return version


class HpeAdapter(VendorAdapter):
    vendor = "hpe"

    def collect(self, client: RedfishClient, root: dict, result: ServerResult) -> None:
        sys_path, system = first_member(client, link(root, "Systems", "/redfish/v1/Systems"))
        if system is None:
            sys_path, system = "/redfish/v1/Systems/1", client.get_optional("/redfish/v1/Systems/1")
        if system is None:
            result.warnings.append("Systems resource not available")
            system = {}
        sys_path = sys_path or "/redfish/v1/Systems/1"

        result.manufacturer = system.get("Manufacturer", "") or "HPE"
        result.model = (system.get("Model") or "").strip()
        result.serial = (system.get("SerialNumber") or "").strip()
        result.service_tag = (system.get("SKU") or "").strip()  # HPE product number
        result.power_state = system.get("PowerState", "") or system.get("Power", "") or ""
        status = system.get("Status") or {}
        result.health = status.get("HealthRollup") or status.get("Health") or ""
        result.bios_version = system.get("BiosVersion", "") or ""
        result.generation = hpe_generation(result.model)

        _, manager = first_member(client, link(root, "Managers", "/redfish/v1/Managers"))
        manager = manager or {}
        result.bmc_version = manager.get("FirmwareVersion", "") or ""
        result.bmc_type = hpe_bmc_type(manager.get("Model", ""), result.bmc_version, result.generation)

        got = self._firmware_update_service(client, root, result)
        if not got:
            got = self._firmware_ilo4(client, sys_path, result)
        if not got:
            result.warnings.append("No firmware inventory endpoint available")

        cats = {c.category for c in result.components}
        if "BIOS" not in cats and result.bios_version:
            self._add(result, "System ROM", result.bios_version, "", "Systems.BiosVersion")
        if "BMC" not in cats and result.bmc_version:
            self._add(result, "iLO", result.bmc_version, "System Board", "Managers.FirmwareVersion")

        # iLO firmware inventory does not list drives; they live under Storage.
        if self.collect_drives:
            self._drives(client, system, sys_path, result)
        if "PSU" not in {c.category for c in result.components}:
            self._power_supplies(client, root, result)

    # ----------------------------------------------------------------- helpers
    def _add(self, result: ServerResult, name: str, version: str, context: str, source: str,
             inv_id: str = "", updateable: bool | None = None, category: str | None = None) -> None:
        cat = category or categorize_hpe(name, context)
        ref_key, rdate = hpe_ref_key(cat, name, version, result.bmc_type)
        self.add(result, Component(
            category=cat,
            name=name,
            version=hpe_display_version(cat, version),
            inventory_id=inv_id,
            location=context,
            updateable=updateable,
            source=source,
            ref_key=ref_key,
            release_date=rdate,
        ))

    def _firmware_update_service(self, client: RedfishClient, root: dict, result: ServerResult) -> bool:
        us = client.get_optional(link(root, "UpdateService", "/redfish/v1/UpdateService"))
        inv_path = odata_id((us or {}).get("FirmwareInventory"))
        if not inv_path:
            return False
        items = fetch_collection(client, inv_path, result, "FirmwareInventory")
        if not items:
            return False
        for it in items:
            name = (it.get("Name") or "").strip()
            version = (it.get("Version") or "").strip()
            if not name or not version:
                continue
            hpe = (it.get("Oem") or {}).get("Hpe") or (it.get("Oem") or {}).get("Hp") or {}
            context = hpe.get("DeviceContext", "") or ""
            self._add(result, name, version, context, "FirmwareInventory", it.get("Id", ""), it.get("Updateable"))
        return True

    def _firmware_ilo4(self, client: RedfishClient, sys_path: str, result: ServerResult) -> bool:
        """iLO 4: /Systems/1/FirmwareInventory is a dict of lists, not a collection."""
        body = client.get_optional(sys_path.rstrip("/") + "/FirmwareInventory/")
        if not body:
            return False
        current = body.get("Current") or {}
        if not isinstance(current, dict) or not current:
            return False
        result.warnings.append("iLO 4 legacy firmware inventory used")
        for key, entries in current.items():
            for e in entries if isinstance(entries, list) else []:
                name = (e.get("Name") or key).strip()
                version = (e.get("VersionString") or "").strip()
                if not version:
                    continue
                self._add(result, name, version, e.get("Location", "") or "", "Systems.FirmwareInventory", key)
        return True

    def _drives(self, client: RedfishClient, system: dict, sys_path: str, result: ServerResult) -> None:
        storage_path = odata_id(system.get("Storage"))
        found = False
        if storage_path:
            for s_uri in members(client.get_optional(storage_path)):
                storage = self._safe_get(client, s_uri, result)
                if not storage:
                    continue
                for d in storage.get("Drives", []) or []:
                    drive = self._safe_get(client, odata_id(d), result)
                    if drive and drive.get("Revision"):
                        found = True
                        loc = _drive_location(drive)
                        self._add(result, f"Drive {drive.get('Model', '').strip()}".strip(), drive["Revision"],
                                  loc, "Storage.Drives", drive.get("Id", ""), category="Drive")
        if found:
            return
        smart = odata_id(((system.get("Oem") or {}).get("Hpe") or (system.get("Oem") or {}).get("Hp") or {})
                         .get("Links", {}).get("SmartStorage")) or sys_path.rstrip("/") + "/SmartStorage"
        ss = client.get_optional(smart)
        if not ss:
            return
        ac_path = odata_id((ss.get("Links") or {}).get("ArrayControllers")) or smart.rstrip("/") + "/ArrayControllers"
        for ac_uri in members(client.get_optional(ac_path)):
            ac = self._safe_get(client, ac_uri, result)
            if not ac:
                continue
            dd_path = odata_id((ac.get("Links") or {}).get("PhysicalDrives")) or ac_uri.rstrip("/") + "/DiskDrives"
            for d_uri in members(client.get_optional(dd_path)):
                d = self._safe_get(client, d_uri, result)
                if not d:
                    continue
                fw = (d.get("FirmwareVersion") or {})
                ver = fw.get("Current", {}).get("VersionString") if isinstance(fw, dict) else str(fw)
                if ver:
                    self._add(result, f"Drive {d.get('Model', '').strip()}".strip(), ver, d.get("Location", ""),
                              "SmartStorage.DiskDrives", d.get("Id", ""), category="Drive")

    def _power_supplies(self, client: RedfishClient, root: dict, result: ServerResult) -> None:
        _, chassis = first_member(client, link(root, "Chassis", "/redfish/v1/Chassis"))
        power = client.get_optional(odata_id((chassis or {}).get("Power")))
        for psu in (power or {}).get("PowerSupplies", []) or []:
            ver = psu.get("FirmwareVersion")
            if ver:
                bay = (psu.get("Oem", {}).get("Hpe") or psu.get("Oem", {}).get("Hp") or {}).get("BayNumber", "")
                self._add(result, psu.get("Name") or psu.get("Model") or "Power Supply", ver,
                          f"Bay {bay}" if bay else "", "Chassis.Power", category="PSU")

    @staticmethod
    def _safe_get(client: RedfishClient, uri: str | None, result: ServerResult) -> dict | None:
        if not uri:
            return None
        try:
            return client.get(uri)
        except RedfishError as e:
            if e.error_class in (AUTH, FORBIDDEN):
                raise
            result.warnings.append(f"could not read {uri}: {e.error_class}")
            return None


def _drive_location(drive: dict) -> str:
    loc = drive.get("PhysicalLocation") or {}
    part = (loc.get("PartLocation") or {}).get("ServiceLabel") if isinstance(loc, dict) else None
    if part:
        return part
    locs = drive.get("Location") or []
    if isinstance(locs, list) and locs:
        return str(locs[0].get("Info", ""))
    return drive.get("Id", "")

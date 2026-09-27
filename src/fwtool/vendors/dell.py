"""Dell iDRAC8 / iDRAC9 adapter."""

from __future__ import annotations

import logging

from ..models import Component, ServerResult
from ..normalize import categorize_dell, dell_bmc_type, dell_component_id, dell_generation, dell_skip
from ..redfish.client import RedfishClient, odata_id
from .base import VendorAdapter, fetch_collection, first_member, link

log = logging.getLogger(__name__)

# Component IDs used when the firmware inventory is missing (old iDRAC8).
BIOS_COMPONENT_ID = "159"
IDRAC_COMPONENT_ID = "25227"


class DellAdapter(VendorAdapter):
    vendor = "dell"

    def collect(self, client: RedfishClient, root: dict, result: ServerResult) -> None:
        sys_path, system = first_member(client, link(root, "Systems", "/redfish/v1/Systems"))
        if system is None:
            system = client.get_optional("/redfish/v1/Systems/System.Embedded.1")
        if system is None:
            result.warnings.append("Systems resource not available")
            system = {}

        result.manufacturer = system.get("Manufacturer", "") or "Dell Inc."
        result.model = (system.get("Model") or "").strip()
        result.service_tag = (system.get("SKU") or "").strip()
        result.serial = (system.get("SerialNumber") or "").strip()
        result.power_state = system.get("PowerState", "") or ""
        status = system.get("Status") or {}
        result.health = status.get("HealthRollup") or status.get("Health") or ""
        result.bios_version = system.get("BiosVersion", "") or ""

        dell_sys = self._dell_system(client, system)
        sys_gen = dell_sys.get("SystemGeneration", "") if dell_sys else ""
        if dell_sys:
            sid = dell_sys.get("SystemID")
            if isinstance(sid, int):
                result.system_id = f"{sid:04X}"
            elif isinstance(sid, str) and sid.strip():
                s = sid.strip()
                result.system_id = f"{int(s):04X}" if s.isdigit() else s.upper().zfill(4)
            result.service_tag = result.service_tag or dell_sys.get("ChassisServiceTag", "") or ""

        _, manager = first_member(client, link(root, "Managers", "/redfish/v1/Managers"))
        manager = manager or {}
        result.bmc_version = manager.get("FirmwareVersion", "") or ""
        result.generation = dell_generation(result.model, sys_gen or manager.get("Model", ""), result.bmc_version)
        result.bmc_type = dell_bmc_type(result.bmc_version, result.generation)

        self._firmware(client, root, result)

        # Old iDRAC8 firmware lacks FirmwareInventory: keep BIOS and iDRAC at least.
        cats = {c.category for c in result.components}
        if "BIOS" not in cats and result.bios_version:
            self.add(result, Component("BIOS", "BIOS", result.bios_version, component_id=BIOS_COMPONENT_ID,
                                       location="BIOS.Setup.1-1", source="Systems.BiosVersion"))
        if "BMC" not in cats and result.bmc_version:
            self.add(result, Component("BMC", "Integrated Dell Remote Access Controller", result.bmc_version,
                                       component_id=IDRAC_COMPONENT_ID, location="iDRAC.Embedded.1-1",
                                       source="Managers.FirmwareVersion"))

    @staticmethod
    def _dell_system(client: RedfishClient, system: dict) -> dict:
        oem = ((system.get("Oem") or {}).get("Dell") or {}).get("DellSystem") or {}
        if oem and set(oem) - {"@odata.id"}:
            return oem
        uri = odata_id(oem)
        return (client.get_optional(uri) or {}) if uri else {}

    def _firmware(self, client: RedfishClient, root: dict, result: ServerResult) -> None:
        update_service = client.get_optional(link(root, "UpdateService", "/redfish/v1/UpdateService"))
        inv_path = odata_id((update_service or {}).get("FirmwareInventory")) or "/redfish/v1/UpdateService/FirmwareInventory"
        items = fetch_collection(client, inv_path, result, "FirmwareInventory")
        if items is None:
            result.warnings.append("UpdateService/FirmwareInventory not available (iDRAC firmware too old?)")
            return
        for it in items:
            inv_id = it.get("Id") or (odata_id(it) or "").rstrip("/").rsplit("/", 1)[-1]
            if dell_skip(inv_id):
                continue
            name = (it.get("Name") or "").strip()
            version = (it.get("Version") or "").strip()
            if not version:
                continue
            fqdd = inv_id.split("__", 1)[1] if "__" in inv_id else ""
            self.add(result, Component(
                category=categorize_dell(inv_id, name),
                name=name,
                version=version,
                inventory_id=inv_id,
                location=fqdd,
                component_id=dell_component_id(inv_id),
                updateable=it.get("Updateable"),
                source="FirmwareInventory",
            ))

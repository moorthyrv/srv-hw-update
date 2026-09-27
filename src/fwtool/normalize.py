"""Map vendor firmware inventory entries to common categories.

Categories: BIOS, BMC, CPLD, Storage, NIC, Drive, PSU, Other.
"""

from __future__ import annotations

import re

# Dell: the FQDD after "__" in the FirmwareInventory Id identifies the device.
_DELL_FQDD_RULES: list[tuple[str, str]] = [
    (r"^BIOS\.", "BIOS"),
    (r"^iDRAC\.", "BMC"),
    (r"^CPLD\.", "CPLD"),
    (r"^(RAID|AHCI|NonRAID|BOSS|HBA|SAS|PCIeSSD\.Embedded|Controller)\.", "Storage"),
    (r"^Enclosure\.", "Drive"),  # backplane
    (r"^Disk\.", "Drive"),
    (r"^(NIC|FC|InfiniBand)\.", "NIC"),
    (r"^PSU\.", "PSU"),
]

_DELL_NAME_RULES: list[tuple[str, str]] = [
    (r"\bBIOS\b", "BIOS"),
    (r"integrated dell remote access|\biDRAC\b", "BMC"),
    (r"\bCPLD\b", "CPLD"),
    (r"PERC|RAID|BOSS|HBA\d|\bHBA\b|Controller", "Storage"),
    (r"Backplane|Disk \d|\bSSD\b|\bHDD\b|NVMe|\bDrive\b", "Drive"),
    (r"Ethernet|Network|NIC|Broadcom|Intel\(R\) .*Gigabit|Mellanox|ConnectX|Emulex|QLogic|Fibre", "NIC"),
    (r"Power Supply|\bPSU\b", "PSU"),
]

_HPE_NAME_RULES: list[tuple[str, str]] = [
    (r"^Redundant System ROM", "Other"),
    (r"System ROM|\bBIOS\b", "BIOS"),
    (r"\biLO\b|Integrated Lights-Out", "BMC"),
    (r"Programmable Logic Device|\bCPLD\b|\bSPLD\b", "CPLD"),
    (r"Backplane|\bUBM\d?\b|Drive Cage|Storage Enclosure|Box \d+ Bay", "Drive"),
    (r"Smart Array|Smart HBA|\bSR\d{3}|\bMR\d{3}|\bNS204|Boot Controller|Storage Controller|"
     r"\bHBA\b|\bH2\d\d\b|\bP[48]\d\d[a-z]?\b|\bE208|\bP4\d\d|Tri-Mode|NVMe Controller|RAID", "Storage"),
    (r"Adapter|Ethernet|\bNIC\b|Network|FlexibleLOM|FlexFabric|\bOCP\b|ConnectX|Fibre Channel|"
     r"\bFC\b|InfiniBand|\bCNA\b|\bLOM\b", "NIC"),
    (r"\bSSD\b|\bHDD\b|\bDrive\b|\bDisk\b|NVMe", "Drive"),
    (r"Power Supply|\bPSU\b", "PSU"),
]


def _first_match(rules: list[tuple[str, str]], text: str) -> str | None:
    for pattern, cat in rules:
        if re.search(pattern, text, re.I):
            return cat
    return None


def categorize_dell(inventory_id: str, name: str) -> str:
    fqdd = inventory_id.split("__", 1)[1] if "__" in inventory_id else ""
    return _first_match(_DELL_FQDD_RULES, fqdd) or _first_match(_DELL_NAME_RULES, name or "") or "Other"


def categorize_hpe(name: str, device_context: str = "") -> str:
    return _first_match(_HPE_NAME_RULES, name or "") or _first_match(_HPE_NAME_RULES, device_context or "") or "Other"


def dell_component_id(inventory_id: str) -> str:
    """'Installed-25227-7.00.00.00__iDRAC.Embedded.1-1' -> '25227'."""
    m = re.match(r"^(?:Installed|Current)-(\d+)-", inventory_id or "")
    return m.group(1) if m and m.group(1) != "0" else ""


def dell_skip(inventory_id: str) -> bool:
    """Previous-* (rollback) and Available-* (staged) are not installed firmware."""
    return bool(re.match(r"^(Previous|Available)-", inventory_id or ""))


def dell_generation(model: str, system_generation: str = "", bmc_version: str = "") -> str:
    """Derive Dell generation, e.g. '14G'."""
    m = re.search(r"(\d{2})G", system_generation or "")
    if m:
        return f"{m.group(1)}G"
    m = re.search(r"\b(?:R|T|C|M|MX|FC|XC|XR|XE|HS)\d{2,3}(\d)", (model or "").replace(" ", ""), re.I)
    if m:
        last = m.group(1)
        return {"0": "13G", "4": "14G", "5": "15G", "6": "16G", "7": "17G"}.get(last, "")
    return ""


def dell_bmc_type(bmc_version: str, generation: str) -> str:
    major = re.match(r"(\d+)\.", bmc_version or "")
    if generation == "13G" or (major and int(major.group(1)) == 2):
        return "iDRAC8"
    if generation == "17G":
        return "iDRAC10"
    if major and int(major.group(1)) >= 3 or generation in {"14G", "15G", "16G"}:
        return "iDRAC9"
    return "iDRAC"


def hpe_generation(model: str) -> str:
    m = re.search(r"\bGen\s?(\d+)(\s*Plus)?", model or "", re.I)
    if not m:
        return ""
    return f"Gen{m.group(1)}" + (" Plus" if m.group(2) else "")


def hpe_bmc_type(manager_model: str, manager_fw: str, generation: str) -> str:
    for text in (manager_model, manager_fw):
        m = re.search(r"iLO\s*(\d+)", text or "", re.I)
        if m:
            return f"iLO {m.group(1)}"
    return {"Gen8": "iLO 4", "Gen9": "iLO 4", "Gen10": "iLO 5", "Gen10 Plus": "iLO 5", "Gen11": "iLO 6", "Gen12": "iLO 7"}.get(
        generation, "iLO"
    )

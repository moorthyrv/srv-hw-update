"""Vendor detection and helpers shared by the Dell and HPE adapters."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from ..models import Component, ServerResult
from ..redfish.client import RedfishClient, members, odata_id
from ..redfish.errors import AUTH, FORBIDDEN, RedfishError

log = logging.getLogger(__name__)

EXPAND_QUERIES = ("?$expand=*($levels=1)", "?$expand=.")


def detect_vendor(service_root: dict | None, manufacturer: str = "") -> str:
    """Return 'dell', 'hpe' or ''.

    Uses Systems Manufacturer first, then the service root's Vendor/Product
    fields and finally the Oem keys (``Dell`` vs ``Hpe``/``Hp``).
    """
    text = (manufacturer or "").lower()
    if "dell" in text:
        return "dell"
    if text.startswith("hp") or "hewlett" in text:
        return "hpe"
    root = service_root or {}
    vendor = str(root.get("Vendor", "")).lower()
    product = str(root.get("Product", "")).lower()
    if "dell" in vendor or "dell" in product or "idrac" in product:
        return "dell"
    if vendor.startswith("hp") or "ilo" in product or "lights-out" in product:
        return "hpe"
    oem = root.get("Oem") or {}
    if isinstance(oem, dict):
        keys = {k.lower() for k in oem}
        if "dell" in keys:
            return "dell"
        if "hpe" in keys or "hp" in keys:
            return "hpe"
    return ""


def first_member(client: RedfishClient, collection_path: str | None) -> tuple[str | None, dict | None]:
    coll = client.get_optional(collection_path)
    uris = members(coll)
    if not uris:
        return None, None
    return uris[0], client.get_optional(uris[0])


def link(root: dict | None, key: str, default: str) -> str:
    return odata_id((root or {}).get(key)) or default


def fetch_collection(client: RedfishClient, path: str, result: ServerResult, what: str) -> list[dict] | None:
    """Fetch every member of a collection.

    Tries ``$expand`` first (one request) and falls back to a GET per member.
    Returns None when the collection itself does not exist.
    """
    for query in EXPAND_QUERIES:
        try:
            body = client.get(path + query, retries=0)
        except RedfishError as e:
            if e.error_class in (AUTH, FORBIDDEN):
                raise
            continue
        items = body.get("Members") or []
        if items and all(isinstance(m, dict) and len(m) > 1 for m in items):
            return items
        if not items and "Members" in body:
            return []
        break  # expand ignored; plain collection returned

    coll = client.get_optional(path)
    if coll is None:
        return None
    out: list[dict] = []
    failed = 0
    for uri in members(coll):
        try:
            item = client.get(uri)
        except RedfishError as e:
            if e.error_class in (AUTH, FORBIDDEN):
                raise
            failed += 1
            log.debug("%s: %s member %s failed: %s", result.bmc_ip, what, uri, e)
            continue
        out.append(item)
    if failed:
        result.warnings.append(f"{what}: {failed} member(s) could not be read")
    return out


_VXRAIL = re.compile(r"vx\s*rail", re.I)


def detect_vxrail(result: ServerResult, *texts: str, vxrail_list: set[str] | None = None) -> None:
    """Flag VxRail nodes. They are reported but must never be updated."""
    if (result.platform or "").strip().lower() == "vxrail":
        result.vxrail, result.vxrail_reason = True, "input CSV platform=vxrail"
        return
    for text in (result.model, *texts):
        if text and _VXRAIL.search(text):
            result.vxrail, result.vxrail_reason = True, f"model/SKU contains VxRail ({text.strip()[:40]})"
            return
    if vxrail_list:
        for key in (result.bmc_ip, result.name, result.service_tag, result.serial):
            if key and key.lower() in vxrail_list:
                result.vxrail, result.vxrail_reason = True, "listed in VxRail list file"
                return


class VendorAdapter(ABC):
    vendor: str = ""

    def __init__(self, *, collect_drives: bool = True):
        self.collect_drives = collect_drives

    @abstractmethod
    def collect(self, client: RedfishClient, root: dict, result: ServerResult) -> None:
        """Fill identity and components into ``result``. Raise RedfishError on fatal errors."""

    @staticmethod
    def add(result: ServerResult, comp: Component) -> None:
        result.components.append(comp)

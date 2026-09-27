"""Collect one server: detect vendor, authenticate, run the vendor adapter."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import requests

from .credentials import CredentialProvider
from .inputs import ServerRecord
from .models import FAILED, OK, PARTIAL, ServerResult
from .redfish.client import RedfishClient
from .redfish.errors import AUTH, NO_CREDENTIALS, UNSUPPORTED, NotFound, RedfishError
from .vendors.base import VendorAdapter, detect_vendor, detect_vxrail, first_member, link
from .vendors.dell import DellAdapter
from .vendors.hpe import HpeAdapter

log = logging.getLogger(__name__)


@dataclass
class CollectOptions:
    verify: bool | str = False
    timeout: float = 30.0
    connect_timeout: float = 10.0
    retries: int = 3
    save_raw: bool = False
    collect_hpe_drives: bool = True
    vxrail_list: frozenset[str] = frozenset()


def _adapter(vendor: str, opts: CollectOptions) -> VendorAdapter:
    if vendor == "dell":
        return DellAdapter()
    return HpeAdapter(collect_drives=opts.collect_hpe_drives)


def collect_server(
    record: ServerRecord,
    provider: CredentialProvider,
    opts: CollectOptions,
    session_factory: Callable[[], requests.Session] | None = None,
) -> tuple[ServerResult, dict]:
    """Collect one server. Never raises; failures are recorded on the result.

    Returns the result and the raw Redfish responses (empty unless save_raw).
    """
    started = time.monotonic()
    result = ServerResult(
        name=record.name, bmc_ip=record.bmc_ip, support=record.support, site=record.site,
        environment=record.environment, os=record.os, platform=record.platform, extra=dict(record.extra),
    )
    client = RedfishClient(
        record.bmc_ip, None, verify=opts.verify, timeout=opts.timeout, connect_timeout=opts.connect_timeout,
        retries=opts.retries, record_raw=opts.save_raw,
        session=session_factory() if session_factory else None,
    )
    try:
        # 1. Service root is readable without credentials on iDRAC and iLO.
        root: dict | None = None
        try:
            root = client.get("/redfish/v1/", authenticated=False)
        except NotFound as e:
            raise RedfishError(UNSUPPORTED, "no Redfish service at /redfish/v1/", e.status, e.path) from e
        except RedfishError as e:
            if e.error_class != AUTH:
                raise
        vendor = (record.vendor or "").lower() or detect_vendor(root)

        # 2. Pick credentials. If the vendor is still unknown, try each vendor's account.
        candidates = [vendor] if vendor in ("dell", "hpe") else ["dell", "hpe"]
        last_err: RedfishError | None = None
        for v in candidates:
            cred = provider.get(v, record.as_dict())
            if cred is None:
                continue
            client.credential = cred
            try:
                system_root = root if root else client.get("/redfish/v1/")
                # Confirm credentials work before the adapter runs.
                client.get("/redfish/v1/Systems")
                root = system_root
                vendor = vendor or v
                last_err = None
                break
            except RedfishError as e:
                last_err = e
                client.credential = None
        if client.credential is None:
            if last_err:
                raise last_err
            raise RedfishError(NO_CREDENTIALS, f"no credentials configured for vendor '{vendor or 'unknown'}'")

        # 3. Resolve vendor via Manufacturer if the service root was not conclusive.
        if vendor not in ("dell", "hpe"):
            _, system = first_member(client, link(root, "Systems", "/redfish/v1/Systems"))
            vendor = detect_vendor(root, (system or {}).get("Manufacturer", ""))
        if vendor not in ("dell", "hpe"):
            raise RedfishError(UNSUPPORTED, "could not identify vendor (not Dell or HPE)")
        result.vendor = vendor

        _adapter(vendor, opts).collect(client, root or {}, result)
        detect_vxrail(result, result.service_tag, vxrail_list=set(opts.vxrail_list))

        if not result.components:
            result.collection_status = PARTIAL
            result.error_class = "partial"
            result.error = "no firmware components could be read"
        elif result.warnings:
            result.collection_status = PARTIAL
            result.error_class = "partial"
            result.error = "; ".join(result.warnings)[:500]
        else:
            result.collection_status = OK
    except RedfishError as e:
        result.collection_status = FAILED
        result.error_class = e.error_class
        result.error = e.detail
        # Keep whatever identity/vendor we learned before failing.
        result.vendor = result.vendor or (record.vendor or "").lower()
        log.info("%s (%s) failed: %s", record.name, record.bmc_ip, e)
    except Exception as e:  # noqa: BLE001 - one bad BMC must not stop the run
        result.collection_status = FAILED
        result.error_class = "error"
        result.error = f"{type(e).__name__}: {e}"[:500]
        log.exception("%s (%s) unexpected error", record.name, record.bmc_ip)

    result.collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result.duration_s = round(time.monotonic() - started, 2)
    result.request_count = client.request_count
    return result, client.raw

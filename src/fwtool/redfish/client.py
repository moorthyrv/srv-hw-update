"""A GET-only Redfish client.

Phase 1 is read-only. The client refuses to send any HTTP method other than
GET, and it never creates Redfish sessions (that would need a POST), so every
request uses HTTP Basic auth.

BMC traffic must go direct: ``trust_env`` is off so proxy and CA-bundle
environment variables are ignored, and ``verify`` is passed on every request.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any, Callable

import requests
from requests.auth import HTTPBasicAuth

from ..credentials import Credential
from .errors import (
    AUTH,
    FORBIDDEN,
    TLS,
    UNREACHABLE,
    UNSUPPORTED,
    NotFound,
    ReadOnlyViolation,
    RedfishError,
)

log = logging.getLogger(__name__)

ALLOWED_METHODS = frozenset({"GET"})
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
NOT_FOUND_STATUSES = frozenset({404, 405, 501})


def _make_session() -> requests.Session:
    s = requests.Session()
    # Ignore HTTP(S)_PROXY, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE and .netrc.
    s.trust_env = False
    s.proxies = {}
    return s


class RedfishClient:
    """Read-only Redfish client for one BMC."""

    def __init__(
        self,
        host: str,
        credential: Credential | None = None,
        *,
        verify: bool | str = False,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        retries: int = 3,
        backoff: float = 1.5,
        session: requests.Session | None = None,
        record_raw: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.host = host
        self.base_url = f"https://{host}" if not host.startswith("http") else host.rstrip("/")
        self.credential = credential
        self.verify = verify
        self.timeout = (connect_timeout, timeout)
        self.retries = max(0, retries)
        self.backoff = backoff
        self.session = session or _make_session()
        self.session.trust_env = False
        self.record_raw = record_raw
        self.raw: dict[str, Any] = {}
        self.request_count = 0
        self._sleep = sleep
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ safety
    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Single entry point for HTTP. Anything other than GET is refused."""
        if method.upper() not in ALLOWED_METHODS:
            raise ReadOnlyViolation(
                f"Phase 1 is read-only: refusing HTTP {method.upper()} {path}"
            )
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        auth = None
        if self.credential is not None and kwargs.pop("authenticated", True):
            auth = HTTPBasicAuth(self.credential.username, self.credential.password)
        else:
            kwargs.pop("authenticated", None)
        return self.session.request(
            "GET",
            url,
            auth=auth,
            verify=self.verify,
            timeout=self.timeout,
            headers={"Accept": "application/json", "OData-Version": "4.0"},
            allow_redirects=False,
            **kwargs,
        )

    # Explicitly disable the write verbs so nobody adds them by accident.
    def post(self, *a: Any, **k: Any) -> None:  # pragma: no cover - trivial
        raise ReadOnlyViolation("Phase 1 is read-only: POST is not allowed")

    put = patch = delete = post

    # --------------------------------------------------------------------- GET
    def get(self, path: str, *, authenticated: bool = True, retries: int | None = None) -> dict:
        """GET a Redfish resource and return its JSON body.

        Retries timeouts, connection resets, 429 and 5xx with exponential
        backoff. Raises :class:`NotFound` for 404/405/501 and
        :class:`RedfishError` for everything else.
        """
        attempts = (self.retries if retries is None else retries) + 1
        last_exc: RedfishError | None = None
        for attempt in range(attempts):
            try:
                with self._lock:
                    self.request_count += 1
                resp = self.request("GET", path, authenticated=authenticated)
            except requests.exceptions.SSLError as e:
                raise RedfishError(TLS, _short(e), path=path) from e
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
                last_exc = RedfishError(UNREACHABLE, _short(e), path=path)
                # A refused or unroutable connection rarely fixes itself; retry once.
                if attempt >= 1 or attempt >= attempts - 1:
                    raise last_exc from e
                self._backoff(attempt, None)
                continue
            except requests.exceptions.Timeout as e:
                last_exc = RedfishError(UNREACHABLE, f"timeout: {_short(e)}", path=path)
                if attempt < attempts - 1:
                    self._backoff(attempt, None)
                continue

            status = resp.status_code
            if status in RETRY_STATUSES and attempt < attempts - 1:
                last_exc = RedfishError(UNREACHABLE if status >= 500 else "error", f"HTTP {status}", status, path)
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if status == 401:
                raise RedfishError(AUTH, "HTTP 401 Unauthorized", status, path)
            if status == 403:
                raise RedfishError(FORBIDDEN, "HTTP 403 Forbidden", status, path)
            if status in NOT_FOUND_STATUSES:
                raise NotFound(f"HTTP {status} for {path}", status, path)
            if status >= 300:
                raise RedfishError("error", f"HTTP {status} for {path}", status, path)
            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError) as e:
                raise RedfishError(UNSUPPORTED, f"non-JSON response from {path}", status, path) from e
            if self.record_raw:
                with self._lock:
                    self.raw[path] = body
            return body
        assert last_exc is not None
        raise last_exc

    def get_optional(self, path: str | None) -> dict | None:
        """GET that returns ``None`` when the endpoint does not exist."""
        if not path:
            return None
        try:
            return self.get(path)
        except NotFound:
            return None

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = self.backoff * (2**attempt)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, 30.0) + random.uniform(0, 0.5)
        self._sleep(delay)


def _short(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) <= 200 else text[:197] + "..."


def odata_id(obj: Any) -> str | None:
    if isinstance(obj, dict):
        return obj.get("@odata.id")
    return None


def members(collection: dict | None) -> list[str]:
    """Return the member URIs of a Redfish collection."""
    if not collection:
        return []
    return [m["@odata.id"] for m in collection.get("Members", []) if isinstance(m, dict) and "@odata.id" in m]

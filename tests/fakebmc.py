"""A fake requests.Session that serves Redfish fixtures from JSON files.

Fixture format (tests/fixtures/<name>.json)::

    {
      "password": "secret",              # Basic auth password accepted (user: any)
      "expand": ["?$expand=."],          # $expand queries this BMC supports
      "paths": {"/redfish/v1/": {...}, ...}
    }

The service root is served without auth; everything else needs it.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, status: int, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


class FakeSession:
    def __init__(self, fixture: str | dict, *, exc: Exception | None = None):
        data = fixture if isinstance(fixture, dict) else json.loads((FIXTURES / f"{fixture}.json").read_text())
        self.paths: dict = data.get("paths", {})
        self.password = data.get("password", "secret")
        self.expand = data.get("expand", [])
        self.status_overrides: dict = data.get("status", {})
        self.exc = exc
        self.trust_env = True
        self.proxies = {}
        self.calls: list[tuple[str, str, object]] = []  # (method, path, verify)

    def _lookup(self, path: str):
        for p in (path, path.rstrip("/"), path.rstrip("/") + "/"):
            if p in self.paths:
                return self.paths[p]
        return None

    def request(self, method, url, auth=None, verify=None, timeout=None, headers=None, allow_redirects=True, **kw):
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url
        self.calls.append((method, path, verify))
        if self.exc is not None:
            raise self.exc
        base, _, query = path.partition("?")
        if base in self.status_overrides:
            return FakeResponse(int(self.status_overrides[base]))
        is_root = base.rstrip("/") == "/redfish/v1"
        if not is_root:
            if auth is None or auth.password != self.password:
                return FakeResponse(401, {"error": "unauthorized"})
        if query:
            if "?" + query not in self.expand:
                return FakeResponse(400, {"error": "bad query"})
            coll = self._lookup(base)
            if coll is None:
                return FakeResponse(404, {})
            members = []
            for m in coll.get("Members", []):
                body = self._lookup(m["@odata.id"])
                members.append(body if body is not None else m)
            return FakeResponse(200, {**coll, "Members": members})
        body = self._lookup(base)
        if body is None:
            return FakeResponse(404, {"error": "not found"})
        return FakeResponse(200, body)


def session_factory(fixture, **kw):
    return lambda: FakeSession(fixture, **kw)


class SessionPool:
    """Maps BMC IP -> fixture for runner tests."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def for_ip(self, ip: str) -> FakeSession:
        return FakeSession(self.mapping[ip])


def conn_error() -> Exception:
    return requests.exceptions.ConnectionError("Failed to establish a new connection: [Errno 113] No route to host")


def ssl_error() -> Exception:
    return requests.exceptions.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number")


def read_timeout() -> Exception:
    return requests.exceptions.ReadTimeout("Read timed out. (read timeout=30)")


class RouterSession:
    """Dispatches by host so a whole run can be faked: {ip: fixture or exception}."""

    def __init__(self, mapping: dict):
        self.trust_env = True
        self.proxies = {}
        self._s = {}
        self.hosts: list[str] = []
        for ip, fx in mapping.items():
            self._s[ip] = FakeSession("idrac9_r740", exc=fx) if isinstance(fx, Exception) else FakeSession(fx)

    def request(self, method, url, **kw):
        host = url.split("://", 1)[1].split("/", 1)[0]
        self.hosts.append(host)
        return self._s[host].request(method, url, **kw)

    @property
    def calls(self):
        return [c for s in self._s.values() for c in s.calls]

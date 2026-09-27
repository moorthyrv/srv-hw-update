"""Error classes for BMC collection.

Every failure is mapped to one of a small set of classes so reports can group
them and tell an operator what to fix.
"""

from __future__ import annotations

# Failure classes as they appear in reports.
UNREACHABLE = "unreachable"
TLS = "tls"
AUTH = "auth-401"
FORBIDDEN = "forbidden-403"
UNSUPPORTED = "redfish-unsupported"
PARTIAL = "partial"
NO_CREDENTIALS = "no-credentials"
ERROR = "error"

FIX_HINTS = {
    UNREACHABLE: "Check the BMC IP, routing/firewall to the management VLAN and that HTTPS/443 is open.",
    TLS: "TLS handshake failed. Check the BMC certificate or run without --verify-tls.",
    AUTH: "Credentials rejected. Check the read-only account exists on this BMC.",
    FORBIDDEN: "Account lacks privilege. Grant the read-only role Login/ReadOnly on this BMC.",
    UNSUPPORTED: "BMC firmware has no usable Redfish service. Upgrade BMC firmware (iDRAC8 >= 2.40, iLO 4 >= 2.30).",
    PARTIAL: "Some Redfish endpoints are missing on this BMC firmware; data is incomplete.",
    NO_CREDENTIALS: "No credentials supplied for this vendor.",
    ERROR: "Unexpected error; see the log file.",
}


class ReadOnlyViolation(RuntimeError):
    """Raised when anything tries to send a non-GET request in Phase 1."""


class RedfishError(Exception):
    """A classified failure talking to a BMC."""

    def __init__(self, error_class: str, detail: str, status: int | None = None, path: str | None = None):
        super().__init__(f"{error_class}: {detail}")
        self.error_class = error_class
        self.detail = detail
        self.status = status
        self.path = path


class NotFound(RedfishError):
    """The endpoint does not exist on this BMC (404/405/501)."""

    def __init__(self, detail: str, status: int | None = None, path: str | None = None):
        super().__init__(UNSUPPORTED, detail, status, path)

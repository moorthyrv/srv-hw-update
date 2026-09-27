"""Pluggable credential providers.

Credentials are looked up per server, so a vault provider (HashiCorp Vault,
CyberArk) can later return different accounts per site or vendor without
changing callers. Credentials are never logged or written anywhere: the
:class:`Credential` repr hides the password, and :func:`register_secret`
feeds the log redaction filter.
"""

from __future__ import annotations

import getpass
import logging
import os
import stat
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

VENDORS = ("dell", "hpe")

_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    if value:
        _SECRETS.add(value)


def known_secrets() -> frozenset[str]:
    return frozenset(_SECRETS)


@dataclass(frozen=True)
class Credential:
    username: str
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return f"Credential(username={self.username!r}, password=***)"

    __str__ = __repr__


class CredentialProvider(ABC):
    """Returns the credential to use for a server, or None if it has none."""

    @abstractmethod
    def get(self, vendor: str, server: dict | None = None) -> Credential | None:
        """``vendor`` is 'dell' or 'hpe'; ``server`` is the input CSV row."""


class StaticProvider(CredentialProvider):
    def __init__(self, creds: dict[str, Credential]):
        self._creds = dict(creds)
        for c in self._creds.values():
            register_secret(c.password)

    def get(self, vendor: str, server: dict | None = None) -> Credential | None:
        return self._creds.get(vendor)


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file without touching os.environ."""
    p = Path(path)
    _warn_if_readable_by_others(p)
    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def _warn_if_readable_by_others(p: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = p.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning("Credential file %s is readable by other users; run: chmod 600 %s", p, p)


def build_provider(
    env_file: str | None = None,
    *,
    interactive: bool = True,
    vendors: tuple[str, ...] = VENDORS,
    environ: dict[str, str] | None = None,
) -> StaticProvider:
    """Build the default provider.

    Lookup order per vendor:
      1. ``DELL_BMC_USER``/``DELL_BMC_PASS`` or ``HPE_BMC_USER``/``HPE_BMC_PASS``
      2. ``BMC_USER``/``BMC_PASS`` (shared account)
      3. The same keys in ``--env-file``
      4. Interactive prompt (blank username skips that vendor)
    """
    env = dict(environ if environ is not None else os.environ)
    file_vals = load_env_file(env_file) if env_file else {}

    def lookup(key: str) -> str | None:
        return env.get(key) or file_vals.get(key) or None

    creds: dict[str, Credential] = {}
    for vendor in vendors:
        prefix = vendor.upper()
        user = lookup(f"{prefix}_BMC_USER") or lookup("BMC_USER")
        pw = lookup(f"{prefix}_BMC_PASS") or lookup("BMC_PASS")
        if user and pw:
            creds[vendor] = Credential(user, pw)
            continue
        if interactive and sys.stdin.isatty():
            label = "Dell iDRAC" if vendor == "dell" else "HPE iLO"
            user = user or input(f"{label} read-only username (blank to skip {label}): ").strip()
            if user:
                pw = pw or getpass.getpass(f"{label} password for {user}: ")
                if pw:
                    creds[vendor] = Credential(user, pw)
        if vendor not in creds:
            log.warning("No credentials for %s; those servers will be reported as no-credentials", vendor)
    return StaticProvider(creds)

"""Firmware age, status, compliance and priority scoring."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from .catalog.dell_catalog import DellCatalog
from .config import Settings
from .models import FAILED, Component, ServerResult
from .reference.baselines import Baselines
from .reference.hpe_reference import HpeReference
from .versions import compare, parse_date, version_key

CURRENT, BEHIND, UNKNOWN, NO_REFERENCE = "current", "behind", "unknown", "no-reference"

TPM_ENTITLEMENT = {"BIOS", "CPLD"}
TPM_SECURITY_TRACK = {"BMC", "Storage", "NIC", "Drive"}


@dataclass
class ComponentAnalysis:
    status: str = NO_REFERENCE
    latest_version: str = ""
    latest_date: str = ""
    installed_date: str = ""
    age_days: int | None = None
    versions_behind: int | None = None
    behind_note: str = ""
    reference: str = ""
    target_version: str = ""
    compliance: str = "n/a"
    compliance_basis: str = ""
    update_track: str = ""
    note: str = ""
    score: float = 0.0


@dataclass
class ServerAnalysis:
    overall_status: str = UNKNOWN
    components_total: int = 0
    components_behind: int = 0
    components_noncompliant: int = 0
    components_unknown: int = 0
    components_no_reference: int = 0
    oldest_component_age_days: int | None = None
    oldest_component: str = ""
    bios_latest: str = ""
    bmc_latest: str = ""
    bios_status: str = ""
    bmc_status: str = ""
    priority_score: float = 0.0
    components: list[ComponentAnalysis] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Analyzer:
    def __init__(
        self,
        settings: Settings,
        dell_catalog: DellCatalog | None = None,
        hpe_reference: HpeReference | None = None,
        baselines: Baselines | None = None,
        as_of: date | None = None,
    ):
        self.settings = settings
        self.dell = dell_catalog or DellCatalog()
        self.hpe = hpe_reference or HpeReference()
        self.baselines = baselines or Baselines()
        self.as_of = as_of or date.today()

    # ----------------------------------------------------------- component
    def component(self, comp: Component, server: ServerResult) -> ComponentAnalysis:
        if server.vendor == "dell":
            a = self._dell(comp, server)
        elif server.vendor == "hpe":
            a = self._hpe(comp, server)
        else:
            a = ComponentAnalysis(status=UNKNOWN, note="vendor unknown")

        inst = parse_date(a.installed_date)
        if inst:
            a.age_days = max(0, (self.as_of - inst).days)

        self._compliance(a, comp, server)

        if server.vendor == "hpe" and server.support == "tpm" and a.status == BEHIND:
            if comp.category in TPM_ENTITLEMENT:
                a.update_track = "entitlement required"
            elif comp.category in TPM_SECURITY_TRACK:
                a.update_track = "security-only track"
        a.score = self._score(a, comp)
        return a

    def _dell(self, comp: Component, server: ServerResult) -> ComponentAnalysis:
        a = ComponentAnalysis(reference="dell-catalog")
        if not comp.component_id:
            a.note = "no Dell component ID in inventory"
            return a
        versions = self.dell.versions(comp.component_id, server.system_id, server.model)
        if not versions:
            a.note = "component/model not in Dell catalog"
            if server.generation == "13G":
                a.note += " (13G largely removed from Dell catalog)"
            return a
        latest = versions[-1]
        a.latest_version = latest.version
        a.latest_date = latest.release_date.isoformat() if latest.release_date else ""
        ordered = [(v.version, v.release_date) for v in versions]
        self._position(a, comp.version, ordered, "catalog")
        if server.generation == "13G":
            a.note = (a.note + "; " if a.note else "") + "13G: Dell catalog may not list the newest release"
            if a.behind_note == "newer than catalog":
                a.status = UNKNOWN  # the catalog is stale for 13G, not the server current
        return a

    def _hpe(self, comp: Component, server: ServerResult) -> ComponentAnalysis:
        a = ComponentAnalysis(reference="hpe-reference", installed_date=comp.release_date)
        entry = self.hpe.find(comp, server)
        if entry is None:
            a.note = "not in hpe_reference.yaml"
            return a
        a.reference = f"hpe-reference:{entry.key}"
        latest = entry.versions[0]
        a.latest_version = latest.version
        a.latest_date = latest.date.isoformat() if latest.date else ""
        ordered = [(v.version, v.date) for v in reversed(entry.versions)]  # oldest first
        date_from_rom = a.installed_date
        self._position(a, comp.version, ordered, "reference")
        if date_from_rom:
            a.installed_date = date_from_rom  # the ROM string's own date wins
        return a

    @staticmethod
    def _position(a: ComponentAnalysis, installed: str, ordered: list[tuple[str, date | None]], what: str) -> None:
        """Set status / versions_behind given reference versions ordered oldest -> newest."""
        if not installed or not any(ch.isdigit() for ch in installed):
            a.status = UNKNOWN
            a.note = "installed version not comparable"
            return
        idx = next((i for i, (v, _) in enumerate(ordered) if compare(v, installed) == 0), None)
        n = len(ordered)
        if idx is not None:
            d = ordered[idx][1]
            a.installed_date = a.installed_date or (d.isoformat() if d else "")
            a.versions_behind = n - 1 - idx
        else:
            newer = [v for v, _ in ordered if version_key(v) > version_key(installed)]
            if not newer:
                a.versions_behind = 0
                a.behind_note = f"newer than {what}"
            elif len(newer) == n:
                a.versions_behind = n
                a.behind_note = f"older than oldest {what} entry"
            else:
                a.versions_behind = len(newer)
                a.behind_note = f"installed version not in {what}"
        a.status = BEHIND if a.versions_behind else CURRENT

    def _compliance(self, a: ComponentAnalysis, comp: Component, server: ServerResult) -> None:
        t = self.baselines.target(comp, server)
        if t:
            a.target_version, a.compliance_basis = t.version, t.basis
            if not comp.version or not any(ch.isdigit() for ch in comp.version):
                a.compliance = "n/a"
            else:
                a.compliance = "compliant" if compare(comp.version, t.version) >= 0 else "non-compliant"
            return
        if a.versions_behind is None:
            return
        policy = self.settings.target_policy
        allowed = 1 if policy == "n-1" else 0
        a.compliance_basis = f"policy:{policy}"
        if a.behind_note.startswith("older than oldest"):
            # versions_behind is only a lower bound here; it cannot be shown to be within target.
            a.compliance = "non-compliant"
            return
        a.compliance = "compliant" if a.versions_behind <= allowed else "non-compliant"

    def _score(self, a: ComponentAnalysis, comp: Component) -> float:
        sc = self.settings.scoring
        w = float(sc.weights.get(comp.category, sc.weights.get("Other", 1.0)))
        if a.status == BEHIND:
            years = (a.age_days / 365.0) if a.age_days is not None else sc.unknown_age_years
            s = w * (1.0 + min(years, sc.age_cap_years))
            if a.compliance == "non-compliant":
                s *= sc.noncompliant_multiplier
            return round(s, 2)
        if a.status == UNKNOWN:
            return round(w * sc.unknown_factor, 2)
        if a.compliance == "non-compliant":  # current vs latest but below a baseline
            return round(w * sc.noncompliant_multiplier, 2)
        return 0.0

    # -------------------------------------------------------------- server
    def server(self, server: ServerResult) -> ServerAnalysis:
        sa = ServerAnalysis()
        if server.collection_status == FAILED:
            sa.overall_status = "failed"
            return sa
        sa.components = [self.component(c, server) for c in server.components]
        sa.components_total = len(sa.components)
        for comp, a in zip(server.components, sa.components):
            sa.components_behind += a.status == BEHIND
            sa.components_unknown += a.status == UNKNOWN
            sa.components_no_reference += a.status == NO_REFERENCE
            sa.components_noncompliant += a.compliance == "non-compliant"
            if a.age_days is not None and (sa.oldest_component_age_days is None or a.age_days > sa.oldest_component_age_days):
                sa.oldest_component_age_days = a.age_days
                sa.oldest_component = f"{comp.category}: {comp.name}"
            if comp.category in ("BIOS", "BMC") and not getattr(sa, f"{comp.category.lower()}_status"):
                setattr(sa, f"{comp.category.lower()}_latest", a.latest_version)
                setattr(sa, f"{comp.category.lower()}_status", a.status)
        if sa.components_behind:
            sa.overall_status = BEHIND
        elif any(a.status == CURRENT for a in sa.components) and not sa.components_unknown:
            sa.overall_status = CURRENT
        elif sa.components_total and sa.components_no_reference == sa.components_total:
            sa.overall_status = NO_REFERENCE
        else:
            sa.overall_status = UNKNOWN
        env_mult = {str(k).lower(): float(v) for k, v in self.settings.scoring.environment_multipliers.items()}
        mult = env_mult.get((server.environment or "").lower(), 1.0)
        sa.priority_score = round(sum(a.score for a in sa.components) * mult, 1)
        return sa

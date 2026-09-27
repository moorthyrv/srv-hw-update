"""Sanity-check a finished run and produce a shareable validation summary.

``fwtool validate`` re-reads a run's state files (no BMC traffic), runs the
analysis and checks that what was collected looks complete and plausible:
collection results, identity fields, BIOS/BMC presence, HPE ROM parsing,
reference coverage and the result files themselves. Every check is PASS,
WARN or FAIL, with a hint for anything that is not PASS.

With ``share=True`` names, IPs and serials are replaced by placeholders so the
summary can be sent to someone outside the team.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import NO_REFERENCE, Analyzer, ServerAnalysis
from .models import FAILED, OK, PARTIAL, ServerResult
from .redfish.errors import FIX_HINTS

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    items: list[str] = field(default_factory=list)


@dataclass
class Validation:
    run_dir: Path
    checks: list[Check] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    @property
    def worst(self) -> str:
        statuses = {c.status for c in self.checks}
        return FAIL if FAIL in statuses else WARN if WARN in statuses else PASS

    def add(self, name: str, status: str, detail: str = "", hint: str = "", items: list[str] | None = None) -> None:
        self.checks.append(Check(name, status, detail, hint, items or []))


def _bmc(r: ServerResult) -> str:
    """'iLO 5 v2.72' rather than 'iLO 5 iLO 5 v2.72'."""
    ver = r.bmc_version or ""
    return ver if r.bmc_type and ver.startswith(r.bmc_type) else f"{r.bmc_type} {ver}".strip()


class _Masker:
    """Stable placeholders for names, IPs and serials when sharing."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._maps: dict[str, dict[str, str]] = defaultdict(dict)

    def __call__(self, kind: str, value: str) -> str:
        if not self.enabled or not value:
            return value
        m = self._maps[kind]
        if value not in m:
            m[value] = f"{kind}{len(m) + 1:03d}"
        return m[value]


def validate_run(run_dir: str | Path, results: list[ServerResult], analyzer: Analyzer, *, share: bool = False,
                 max_items: int = 15) -> Validation:
    v = Validation(Path(run_dir))
    mask = _Masker(share)

    def label(r: ServerResult) -> str:
        return f"{mask('server', r.name)} ({mask('ip', r.bmc_ip)})"

    pairs: list[tuple[ServerResult, ServerAnalysis]] = [(r, analyzer.server(r)) for r in results]
    total = len(results)
    counts = Counter(r.collection_status for r in results)

    # 1. Collection -----------------------------------------------------------
    if total == 0:
        v.add("Servers collected", FAIL, "no collected servers in this run", "Run 'fwtool inventory' first.")
        return v
    ok_share = counts[OK] / total
    status = PASS if counts[FAILED] == 0 else (WARN if ok_share + counts[PARTIAL] / total >= 0.5 else FAIL)
    v.add("Collection", status,
          f"{total} servers: ok={counts[OK]} partial={counts[PARTIAL]} failed={counts[FAILED]}")

    by_class: dict[str, list[ServerResult]] = defaultdict(list)
    for r in results:
        if r.collection_status == FAILED:
            by_class[r.error_class].append(r)
    for cls, rs in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        v.add(f"Failed: {cls}", FAIL if len(rs) == total else WARN, f"{len(rs)} server(s)",
              FIX_HINTS.get(cls, ""), [f"{label(r)}: {r.error}" for r in rs[:max_items]])

    partial = [r for r in results if r.collection_status == PARTIAL]
    if partial:
        v.add("Partial data", WARN, f"{len(partial)} server(s) answered but some endpoints were missing",
              FIX_HINTS["partial"],
              [f"{label(r)} {_bmc(r)}: {'; '.join(r.warnings)[:160]}" for r in partial[:max_items]])

    collected = [(r, a) for r, a in pairs if r.collection_status != FAILED]
    if not collected:
        return v

    # 2. Identity ---------------------------------------------------------------
    missing = []
    for r, _ in collected:
        gaps = [f for f in ("vendor", "model", "generation", "bmc_type", "bmc_version") if not getattr(r, f)]
        if not (r.serial or r.service_tag):
            gaps.append("serial")
        if gaps:
            missing.append(f"{label(r)} {r.model or '?'}: missing {', '.join(gaps)}")
    v.add("Identity (vendor, model, generation, BMC, serial)", WARN if missing else PASS,
          f"{len(collected) - len(missing)}/{len(collected)} complete",
          "Send the raw JSON (--save-raw) of these servers so detection can be fixed." if missing else "", missing[:max_items])

    # 3. BIOS and BMC present --------------------------------------------------
    no_core = []
    for r, _ in collected:
        cats = {c.category for c in r.components}
        lacking = [c for c in ("BIOS", "BMC") if c not in cats]
        if lacking:
            no_core.append(f"{label(r)} {r.model}: no {' / '.join(lacking)} component")
    v.add("BIOS and BMC firmware found", FAIL if no_core else PASS,
          f"{len(collected) - len(no_core)}/{len(collected)} servers", "Send raw JSON for these servers." if no_core else "",
          no_core[:max_items])

    few = [f"{label(r)} {r.model}: {len(r.components)} component(s)" for r, _ in collected if len(r.components) < 4]
    if few:
        v.add("Component count", WARN, f"{len(few)} server(s) with fewer than 4 components",
              "Old BMC firmware usually exposes less; check the raw JSON.", few[:max_items])

    # 4. HPE ROM family parsed (needed for BIOS age and reference lookups) -------
    hpe = [(r, a) for r, a in collected if r.vendor == "hpe"]
    if hpe:
        bad = []
        for r, _ in hpe:
            bios = next((c for c in r.components if c.category == "BIOS"), None)
            if bios is None or not bios.ref_key:
                bad.append(f"{label(r)} {r.model}: BIOS version '{r.bios_version}' not recognised")
        v.add("HPE BIOS version parsed", WARN if bad else PASS, f"{len(hpe) - len(bad)}/{len(hpe)} servers",
              "Send these BIOS version strings so the parser can be extended." if bad else "", bad[:max_items])

    # 5. Dell SystemID (catalog matching) ------------------------------------------
    dell = [(r, a) for r, a in collected if r.vendor == "dell"]
    if dell:
        no_sid = [f"{label(r)} {r.model} ({r.bmc_type})" for r, _ in dell if not r.system_id]
        v.add("Dell SystemID found", WARN if no_sid else PASS, f"{len(dell) - len(no_sid)}/{len(dell)} servers",
              "Without SystemID the catalog is matched by model name only (normal for iDRAC8)." if no_sid else "",
              no_sid[:max_items])

    # 6. Reference coverage -------------------------------------------------------
    for vendor, rows in (("dell", dell), ("hpe", hpe)):
        if not rows:
            continue
        per_cat: dict[str, Counter] = defaultdict(Counter)
        unmatched: Counter = Counter()
        for r, a in rows:
            for c, ca in zip(r.components, a.components):
                per_cat[c.category][ca.status] += 1
                if ca.status == NO_REFERENCE:
                    unmatched[f"{c.category}: {c.name}"] += 1
        core = sum(per_cat[k][s] for k in ("BIOS", "BMC") for s in per_cat[k] if s != NO_REFERENCE)
        core_total = sum(sum(per_cat[k].values()) for k in ("BIOS", "BMC"))
        status = PASS if core_total and core == core_total else (WARN if core else FAIL)
        detail = ", ".join(
            f"{k} {sum(n for s, n in per_cat[k].items() if s != NO_REFERENCE)}/{sum(per_cat[k].values())}"
            for k in ("BIOS", "BMC", "CPLD", "Storage", "NIC", "Drive", "PSU", "Other") if per_cat[k])
        hint = ("Components without a reference get no latest version or age. "
                + ("Update the Dell catalog (fwtool catalog refresh)." if vendor == "dell"
                   else "Add manual entries to hpe_reference.yaml for the names below."))
        v.add(f"{vendor.upper()} reference coverage (components with a latest version)", status, detail,
              hint if unmatched else "",
              [f"{n}x {name}" for name, n in unmatched.most_common(max_items)])

    # 7. Firmware status overview ---------------------------------------------------
    st = Counter(a.overall_status for _, a in collected)
    ages = [a.oldest_component_age_days for _, a in collected if a.oldest_component_age_days is not None]
    v.add("Firmware status", PASS,
          ", ".join(f"{k}={n}" for k, n in st.most_common())
          + (f"; oldest firmware {max(ages)} days" if ages else "; no release dates known yet"))
    vx = [f"{label(r)} {r.model} ({r.appliance or 'VxRail'})" for r, _ in collected if r.vxrail or r.appliance]
    if vx:
        v.add("Appliance nodes flagged (never updated by fwtool)", PASS, f"{len(vx)} server(s)", "", vx[:max_items])

    # 8. Output files ---------------------------------------------------------------
    run = Path(run_dir)
    missing_files = [f for f in ("servers.csv", "firmware.csv", "report.xlsx") if not (run / f).exists()]
    if missing_files:
        v.add("Report files", FAIL, "missing: " + ", ".join(missing_files), "Run 'fwtool report --run-dir <run>'.")
    else:
        with open(run / "servers.csv", encoding="utf-8-sig") as fh:
            rows = sum(1 for _ in csv.DictReader(fh))
        v.add("Report files", PASS if rows == total else WARN,
              f"servers.csv has {rows} rows for {total} collected servers",
              "" if rows == total else "Re-run 'fwtool report --run-dir <run>' to refresh the reports.")

    # Compact per-server table ----------------------------------------------------------
    v.lines.append(f"{'server':<28} {'vendor':<6} {'model':<28} {'gen':<11} {'bmc':<14} {'status':<8} "
                   f"{'fw':<12} {'behind':>6} {'score':>6}")
    for r, a in sorted(pairs, key=lambda p: (-p[1].priority_score, p[0].name)):
        v.lines.append(f"{mask('server', r.name)[:28]:<28} {r.vendor[:6]:<6} {r.model[:28]:<28} "
                       f"{r.generation[:11]:<11} {_bmc(r)[:14]:<14} "
                       f"{r.collection_status:<8} {a.overall_status:<12} {a.components_behind:>6} {a.priority_score:>6}")
    return v


def render(v: Validation, share: bool = False) -> str:
    out = [f"fwtool validation: {v.run_dir.name}   overall: {v.worst}"
           + ("   (names/IPs/serials masked)" if share else ""), ""]
    for c in v.checks:
        out.append(f"[{c.status}] {c.name}: {c.detail}")
        if c.hint:
            out.append(f"       hint: {c.hint}")
        for item in c.items:
            out.append(f"       - {item}")
    out += ["", *v.lines, ""]
    return "\n".join(out)

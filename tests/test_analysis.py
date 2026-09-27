from datetime import date
from pathlib import Path

import pytest

from fakebmc import FakeSession
from fwtool.analysis import Analyzer
from fwtool.catalog.dell_catalog import DellCatalog
from fwtool.collector import CollectOptions, collect_server
from fwtool.config import Settings
from fwtool.credentials import Credential, StaticProvider
from fwtool.inputs import ServerRecord
from fwtool.reference.baselines import Baselines
from fwtool.reference.hpe_reference import HpeReference

FX = Path(__file__).parent / "fixtures"
AS_OF = date(2026, 9, 27)
PROVIDER = StaticProvider({"dell": Credential("ro", "secret"), "hpe": Credential("ro", "secret")})


def collected(fixture, **rec):
    sess = FakeSession(fixture)
    r, _ = collect_server(ServerRecord("srv", "10.0.0.1", **rec), PROVIDER, CollectOptions(), session_factory=lambda: sess)
    return r


@pytest.fixture
def analyzer():
    return Analyzer(Settings(), DellCatalog.load([FX / "dell_catalog.xml"]), HpeReference.load(FX / "hpe_reference.yaml"),
                    Baselines.load(FX / "baselines.yaml"), as_of=AS_OF)


def comps(server, sa):
    return {c.name: a for c, a in zip(server.components, sa.components)}


def test_dell_statuses(analyzer):
    r = collected("idrac9_r740")
    sa = analyzer.server(r)
    c = comps(r, sa)
    bios = c["BIOS"]
    assert (bios.status, bios.versions_behind, bios.latest_version) == ("behind", 1, "2.19.1")
    assert bios.installed_date == "2021-08-01" and bios.age_days == (AS_OF - date(2021, 8, 1)).days
    assert (bios.compliance, bios.compliance_basis) == ("compliant", "baselines.yaml")
    idrac = c["Integrated Dell Remote Access Controller"]
    assert (idrac.status, idrac.target_version, idrac.compliance) == ("behind", "7.00.00.173", "non-compliant")
    perc = c["PERC H740P Mini"]
    assert (perc.status, perc.versions_behind, perc.behind_note) == ("behind", 2, "older than oldest catalog entry")
    assert (perc.compliance, perc.compliance_basis) == ("non-compliant", "policy:n-1")
    assert perc.age_days is None
    nic = c["Intel(R) Ethernet 10G X710 rNDC - E4:43:4B:00:00:01"]
    assert (nic.status, nic.compliance, nic.compliance_basis) == ("current", "compliant", "policy:n-1")
    assert c["PS1 Power Supply"].status == "no-reference"
    assert c["System CPLD"].status == "no-reference"
    assert sa.overall_status == "behind"
    assert sa.components_behind == 3 and sa.components_noncompliant == 2
    assert sa.bios_latest == "2.19.1" and sa.bmc_status == "behind"
    assert sa.oldest_component.startswith("BIOS")


def test_dell_13g_note(analyzer):
    r = collected("idrac8_r630")
    sa = analyzer.server(r)
    c = comps(r, sa)
    idrac = c["Integrated Dell Remote Access Controller"]
    assert idrac.status == "unknown" and idrac.behind_note == "newer than catalog"
    assert "13G" in idrac.note
    assert "13G" in c["BIOS"].note  # not in catalog for R630


def test_vxrail_still_analysed(analyzer):
    r = collected("idrac9_vxrail")
    sa = analyzer.server(r)
    assert r.vxrail and sa.components_behind == 3  # matched by SystemID


def test_hpe_statuses(analyzer):
    r = collected("ilo5_dl380g10", support="hpe")
    sa = analyzer.server(r)
    c = comps(r, sa)
    rom = c["System ROM"]
    assert (rom.status, rom.versions_behind, rom.latest_version) == ("behind", 2, "3.66")
    assert rom.installed_date == "2023-02-09"
    assert (rom.compliance, rom.target_version) == ("non-compliant", "3.40")
    assert (c["iLO 5"].status, c["iLO 5"].versions_behind) == ("behind", 2)
    assert c["Innovation Engine (IE) Firmware"].versions_behind == 1
    sa_ctrl = c["HPE Smart Array P408i-a SR Gen10"]
    assert (sa_ctrl.status, sa_ctrl.compliance, sa_ctrl.compliance_basis) == ("behind", "compliant", "baselines.yaml")
    assert c["HPE Ethernet 1Gb 4-port 331i Adapter - NIC"].behind_note == "older than oldest reference entry"
    drives = [a for comp, a in zip(r.components, sa.components) if comp.category == "Drive"]
    assert [d.status for d in drives] == ["behind", "current"]
    assert c["System Programmable Logic Device"].status == "no-reference"
    assert all(a.update_track == "" for a in sa.components)  # hpe support: no special track


def test_hpe_tpm_tracks(analyzer):
    r = collected("ilo6_dl380g11", support="tpm")
    sa = analyzer.server(r)
    c = comps(r, sa)
    assert c["System ROM"].update_track == "entitlement required"
    assert c["iLO 6"].update_track == "security-only track"


def test_ilo4_partial_analysed(analyzer):
    r = collected("ilo4_dl380g9")
    sa = analyzer.server(r)
    c = comps(r, sa)
    rom = c["System ROM"]
    assert (rom.versions_behind, rom.behind_note) == (2, "older than oldest reference entry")
    assert rom.installed_date == "2018-05-21"  # from the ROM string even when not in the reference
    assert c["iLO"].versions_behind == 1


def test_target_policy_latest_vs_n1():
    base = dict(dell_catalog=DellCatalog.load([FX / "dell_catalog.xml"]), as_of=AS_OF)
    r = collected("idrac9_r740")
    n1 = Analyzer(Settings(target_policy="n-1"), **base).server(r)
    latest = Analyzer(Settings(target_policy="latest"), **base).server(r)
    assert n1.components_noncompliant < latest.components_noncompliant


def test_score_weights_bios_bmc_highest(analyzer):
    r = collected("idrac9_r740")
    sa = analyzer.server(r)
    by_cat = {}
    for comp, a in zip(r.components, sa.components):
        by_cat.setdefault(comp.category, 0)
        by_cat[comp.category] += a.score
    assert by_cat["BIOS"] > by_cat["Storage"] > 0
    assert sa.priority_score == round(sum(a.score for a in sa.components), 1)


def test_configurable_weights_and_env_multiplier(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("target_policy: latest\nscoring:\n  weights: {Storage: 50}\n  environment_multipliers: {Prod: 2}\n")
    s = Settings.load(cfg)
    assert s.scoring.weights["Storage"] == 50 and s.scoring.weights["BIOS"] == 5
    a = Analyzer(s, DellCatalog.load([FX / "dell_catalog.xml"]), as_of=AS_OF)
    r1 = collected("idrac9_r740", environment="prod")
    r2 = collected("idrac9_r740", environment="lab")
    assert a.server(r1).priority_score == pytest.approx(2 * a.server(r2).priority_score, rel=1e-3)


def test_failed_server(analyzer):
    r = collected("idrac9_r740")
    r.collection_status = "failed"
    assert analyzer.server(r).overall_status == "failed"


def test_bad_config_rejected(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("wrokers: 5\n")
    with pytest.raises(ValueError):
        Settings.load(cfg)


def test_older_than_oldest_is_never_n1_compliant():
    from fwtool.analysis import ComponentAnalysis
    from fwtool.models import Component, ServerResult

    a = Analyzer(Settings(target_policy="n-1"))
    ca = ComponentAnalysis()
    Analyzer._position(ca, "1.0", [("2.0", None)], "catalog")  # one entry, installed older
    assert (ca.versions_behind, ca.behind_note) == (1, "older than oldest catalog entry")
    a._compliance(ca, Component("BIOS", "BIOS", "1.0"), ServerResult("s", "1", vendor="dell"))
    assert ca.compliance == "non-compliant"

"""Model strings from the real CMDB export (Dell + HPE in scope; Cisco/IBM skipped)."""

import json
from pathlib import Path

import pytest

from fakebmc import FakeSession
from fwtool.analysis import Analyzer
from fwtool.collector import CollectOptions, collect_server
from fwtool.config import Settings
from fwtool.credentials import Credential, StaticProvider
from fwtool.inputs import ServerRecord, load_servers, vendor_from_model
from fwtool.normalize import dell_generation, hpe_bmc_type, hpe_generation
from fwtool.reference.hpe_reference import HpeReference
from fwtool.versions import hpe_rom

FX = Path(__file__).parent / "fixtures"
PROVIDER = StaticProvider({"dell": Credential("ro", "secret"), "hpe": Credential("ro", "secret")})


@pytest.mark.parametrize("model,gen", [
    ("PowerEdge R640", "14G"), ("PowerEdge R650", "15G"), ("PowerEdge R760XD2", "16G"),
    ("VxFlex integrated rack R640 C", "14G"), ("PowerEdge R630", "13G"), ("PowerEdge R730xd", "13G"),
    ("PowerEdge R7525", "15G"), ("PowerEdge MX740c", "14G"), ("PowerEdge R670", "17G"),
])
def test_dell_generation_from_model(model, gen):
    assert dell_generation(model) == gen


@pytest.mark.parametrize("model,gen,ilo", [
    ("ProLiant DL360p Gen8", "Gen8", "iLO 4"), ("ProLiant DL385p Gen8", "Gen8", "iLO 4"),
    ("ProLiant DL380 Gen9", "Gen9", "iLO 4"), ("ProLiant DL360 Gen10", "Gen10", "iLO 5"),
    ("ProLiant DL345 Gen10 Plus", "Gen10 Plus", "iLO 5"), ("ProLiant DL380 Gen11", "Gen11", "iLO 6"),
])
def test_hpe_generation_from_model(model, gen, ilo):
    assert hpe_generation(model) == gen
    assert hpe_bmc_type("", "", gen) == ilo


def test_gen8_rom_is_versioned_by_date():
    assert hpe_rom("P70 07/01/2015") == ("P70", "2015.07.01", __import__("datetime").date(2015, 7, 1))
    ref = HpeReference.load(Path(__file__).parents[1] / "hpe_reference.yaml")
    assert ref.entries["system-rom:P70"].index_of("2015.07.01") is not None


def test_gen8_ilo4_end_to_end():
    fx = json.loads((FX / "ilo4_dl380g9.json").read_text())
    p = fx["paths"]
    p["/redfish/v1/Systems/1/"].update(Model="ProLiant DL380p Gen8", BiosVersion="P70 08/02/2014")
    p["/redfish/v1/Systems/1/FirmwareInventory/"]["Current"]["SystemRomActive"][0]["VersionString"] = "P70 08/02/2014"
    r, _ = collect_server(ServerRecord("g8", "10.9.9.9", support="tpm"), PROVIDER, CollectOptions(),
                          session_factory=lambda: FakeSession(fx))
    assert (r.generation, r.bmc_type) == ("Gen8", "iLO 4")
    ref = HpeReference.load(Path(__file__).parents[1] / "hpe_reference.yaml")
    sa = Analyzer(Settings(), hpe_reference=ref).server(r)
    rom = next(a for c, a in zip(r.components, sa.components) if c.name == "System ROM")
    assert rom.installed_date == "2014-08-02" and rom.status == "behind"
    assert rom.latest_version == "2015.07.01" and rom.update_track == "entitlement required"


def test_vxflex_flagged_not_updatable():
    fx = json.loads((FX / "idrac9_r740.json").read_text())
    fx["paths"]["/redfish/v1/Systems/System.Embedded.1"]["Model"] = "VxFlex integrated rack R640 C"
    r, _ = collect_server(ServerRecord("vf", "10.9.9.8"), PROVIDER, CollectOptions(),
                          session_factory=lambda: FakeSession(fx))
    from fwtool.report.rows import update_eligible

    assert r.appliance == "VxFlex" and not r.vxrail
    assert update_eligible(r).startswith("no - VxFlex")


@pytest.mark.parametrize("text,vendor", [
    ("Dell Inc. PowerEdge R640", "dell"), ("Dell Inc. VxFlex integrated rack R640 C", "dell"),
    ("Dell PowerEdge R760XD2", "dell"), ("Hewlett-Packard ProLiant DL360 Gen9", "hpe"),
    ("HP ProLiant DL380p Gen8", "hpe"), ("HPE ProLiant DL345 Gen10 Plus", "hpe"),
    ("Oracle Corporation HP ProLiant DL380p Gen8", "hpe"), ("CISCO SYSTEMS INC UCSC-C240-M6L", "other"),
    ("IBM Corporation eServer xSeries 346", "other"), ("#N/A", ""),
])
def test_vendor_from_cmdb_model(text, vendor):
    assert vendor_from_model(text) == vendor


def test_cmdb_model_column_skips_other_vendors(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("name,bmc_ip,Modle id\n"
                 "a,10.0.0.1,Dell Inc. PowerEdge R650\n"
                 "b,10.0.0.2,CISCO SYSTEMS INC UCSX-210C-M7\n"
                 "c,10.0.0.3,HP ProLiant DL360p Gen8\n"
                 "d,10.0.0.4,#N/A\n", encoding="utf-8")
    servers, rep = load_servers(p)
    assert [(s.name, s.vendor) for s in servers] == [("a", "dell"), ("c", "hpe"), ("d", "")]
    assert len(rep.out_of_scope) == 1 and "CISCO" in rep.out_of_scope[0]


def test_ilo6_drives_not_double_counted():
    """Real DL360 Gen11: iLO 6 lists drives in FirmwareInventory and under Storage."""
    fx = json.loads((FX / "ilo5_dl380g10.json").read_text())
    p = fx["paths"]
    inv = "/redfish/v1/UpdateService/FirmwareInventory/"
    extra = [("HPE 3.84TB 12G SAS SSD", "HPD2"), ("HPE 3.84TB 12G SAS SSD", "HPD2"),
             ("8 SFF 24G x1NVMe/SAS UBM6 BC BP", "1.24")]
    for i, (name, ver) in enumerate(extra, 100):
        uri = f"{inv}{i}/"
        p[inv]["Members"].append({"@odata.id": uri})
        p[uri] = {"@odata.id": uri, "Id": str(i), "Name": name, "Version": ver, "Oem": {"Hpe": {"DeviceContext": "Box 1"}}}
    r, _ = collect_server(ServerRecord("g11", "10.9.9.7"), PROVIDER, CollectOptions(),
                          session_factory=lambda: FakeSession(fx))
    drives = [c.name for c in r.components if c.category == "Drive"]
    assert "HPE 3.84TB 12G SAS SSD" not in drives  # duplicate removed
    assert drives.count("Drive MB4000GVYZA") == 2  # Storage entries (with model) kept
    assert "8 SFF 24G x1NVMe/SAS UBM6 BC BP" in drives  # backplane kept
    ref = HpeReference.load(Path(__file__).parents[1] / "hpe_reference.yaml")
    sa = Analyzer(Settings(), hpe_reference=ref).server(r)
    bp = next(a for c, a in zip(r.components, sa.components) if "UBM6" in c.name)
    assert bp.reference == "hpe-reference:backplane:ubm6" and bp.latest_version == "1.02"

from datetime import date
from pathlib import Path

import yaml

from fwtool.models import Component, ServerResult
from fwtool.reference.hpe_reference import HpeReference, import_fwpp, write_reference
from fwtool.versions import hpe_rom, ilo_version

FX = Path(__file__).parent / "fixtures"


def test_rom_and_ilo_parsing():
    assert hpe_rom("U30 v2.76 (02/09/2023)") == ("U30", "2.76", date(2023, 2, 9))
    assert hpe_rom("P89 v2.60 (05/21/2018)")[2] == date(2018, 5, 21)
    assert hpe_rom("U54 v2.16") == ("U54", "2.16", None)
    assert hpe_rom("1.2.3") == ("", "", None)
    assert ilo_version("2.72 Sep 04 2022") == "2.72"
    assert ilo_version("iLO 5 v2.72") == "2.72"
    assert ilo_version("iLO 4 v2.55") == "2.55"


def test_import_fwpp(tmp_path):
    existing = tmp_path / "hpe_reference.yaml"
    existing.write_text(yaml.safe_dump({"spp_level": "2026.03.00", "components": {
        "my-nic": {"category": "NIC", "source": "manual", "match": ["331i"], "versions": [{"version": "1.0"}]},
        "ilo5": {"category": "BMC", "source": "old", "versions": [{"version": "0.1"}]},
    }}))
    data = import_fwpp([FX / "fwpp-test-primary.xml"], existing=existing)
    comps = data["components"]
    assert data["spp_level"] == "2026.03.00"
    assert comps["my-nic"]["source"] == "manual"  # manual entry kept
    assert [v["version"] for v in comps["ilo5"]["versions"]] == ["3.21"]  # generated entry replaced
    assert comps["system-rom:U30"]["versions"][0] == {"version": "3.66", "date": "2026-04-01"}
    assert comps["system-rom:U30"]["versions"][1] == {"version": "2.76", "date": "2023-02-09"}
    assert comps["system-rom:P89"]["versions"][0] == {"version": "1.32", "date": "2015-03-05"}  # M_D_Y form
    assert "drive:MB4000GVYZA" in comps and "drive:MB6000GVYZB" in comps
    st = [k for k in comps if k.startswith("storage:")]
    assert st and any("P408I-A" in m for m in comps[st[0]]["match"])
    out = tmp_path / "out.yaml"
    write_reference(data, out)
    ref = HpeReference.load(out)
    assert ref.entries["system-rom:U30"].versions[0].date == date(2026, 4, 1)


def test_find():
    ref = HpeReference.load(FX / "hpe_reference.yaml")
    srv = ServerResult("h", "1", vendor="hpe", generation="Gen10", bios_version="U30 v2.76 (02/09/2023)")
    assert ref.find(Component("BIOS", "System ROM", "2.76", ref_key="system-rom:U30"), srv).key == "system-rom:U30"
    assert ref.find(Component("Other", "Innovation Engine (IE) Firmware", "0.2.2.3", ref_key="ie"), srv).key == "ie:gen10"
    assert ref.find(Component("Storage", "HPE Smart Array P408i-a SR Gen10", "3.53"), srv).key == "storage:e208e-p"
    assert ref.find(Component("Drive", "Drive MB4000GVYZA", "HPG5"), srv).key == "drive:MB4000GVYZA"
    assert ref.find(Component("NIC", "HPE Ethernet 1Gb 4-port 331i Adapter - NIC", "20.14.54"), srv).key == "nic-331i"
    assert ref.find(Component("Storage", "HPE MR408i-o Gen11", "1"), srv) is None
    assert HpeReference.load(None).find(Component("BIOS", "x", "1"), srv) is None

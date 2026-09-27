"""Collection against mocked iDRAC8/9 and iLO 4/5/6 BMCs."""

import pytest

from fakebmc import FakeSession, conn_error, ssl_error
from fwtool.collector import CollectOptions, collect_server
from fwtool.credentials import Credential, StaticProvider
from fwtool.inputs import ServerRecord
from fwtool.vendors.base import detect_vendor

PROVIDER = StaticProvider({"dell": Credential("ro", "secret"), "hpe": Credential("ro", "secret")})


def collect(fixture, record=None, provider=PROVIDER, **opts):
    record = record or ServerRecord("srv", "10.0.0.1")
    sess = fixture if isinstance(fixture, FakeSession) else FakeSession(fixture)
    result, raw = collect_server(record, provider, CollectOptions(**opts), session_factory=lambda: sess)
    return result, raw, sess


def by_cat(result):
    out = {}
    for c in result.components:
        out.setdefault(c.category, []).append(c)
    return out


def test_idrac9():
    r, _, sess = collect("idrac9_r740")
    assert r.collection_status == "ok", r.error
    assert (r.vendor, r.model, r.generation, r.bmc_type) == ("dell", "PowerEdge R740", "14G", "iDRAC9")
    assert r.service_tag == "ABC1234" and r.system_id == "0716"
    assert r.bmc_version == "5.10.50.00" and r.power_state == "On" and r.health == "OK"
    assert not r.vxrail
    cats = by_cat(r)
    assert set(cats) >= {"BIOS", "BMC", "CPLD", "Storage", "NIC", "Drive", "PSU", "Other"}
    # Previous-/Available- entries skipped
    assert [c.version for c in cats["BIOS"]] == ["2.12.2"]
    assert [c.version for c in cats["BMC"]] == ["5.10.50.00"]
    assert cats["BIOS"][0].component_id == "159"
    assert cats["Storage"][0].location == "RAID.Integrated.1-1"
    assert {c.name for c in cats["Drive"]} == {"Disk 0 in Backplane 1 of Integrated RAID Controller 1", "BP14G+EXP 0:1"}
    # $expand worked: one inventory request, no per-member GETs
    assert not any("/FirmwareInventory/Installed" in p for _, p, _ in sess.calls)


def test_vxrail_detected():
    r, _, _ = collect("idrac9_vxrail")
    assert r.vxrail and "VxRail" in r.vxrail_reason
    assert r.system_id == "0716"  # still matched to the PowerEdge catalog by SystemID


def test_vxrail_from_csv_and_list():
    r, _, _ = collect("idrac9_r740", ServerRecord("x", "10.0.0.1", platform="vxrail"))
    assert r.vxrail
    r, _, _ = collect("idrac9_r740", vxrail_list=frozenset({"abc1234"}))
    assert r.vxrail and "list" in r.vxrail_reason


def test_idrac8_no_expand_falls_back_to_member_gets():
    r, _, sess = collect("idrac8_r630")
    assert r.collection_status == "ok", r.error
    assert (r.generation, r.bmc_type, r.system_id) == ("13G", "iDRAC8", "")
    assert any("/FirmwareInventory/Installed-159" in p for _, p, _ in sess.calls)
    cats = by_cat(r)
    assert cats["CPLD"][0].component_id == ""  # component ID 0 means none


def test_idrac8_without_update_service_is_partial():
    r, _, _ = collect("idrac8_partial")
    assert r.collection_status == "partial"
    assert r.error_class == "partial"
    cats = by_cat(r)
    assert cats["BIOS"][0].version == "2.4.3" and cats["BIOS"][0].component_id == "159"
    assert cats["BMC"][0].version == "2.30.30.30"


def test_ilo5():
    r, _, _ = collect("ilo5_dl380g10", ServerRecord("h", "10.0.0.2", support="hpe"))
    assert r.collection_status == "ok", r.error
    assert (r.vendor, r.generation, r.bmc_type) == ("hpe", "Gen10", "iLO 5")
    cats = by_cat(r)
    bios = cats["BIOS"][0]
    assert (bios.version, bios.ref_key, bios.release_date) == ("2.76", "system-rom:U30", "2023-02-09")
    assert cats["BMC"][0].version == "2.72" and cats["BMC"][0].ref_key == "ilo5"
    assert cats["CPLD"][0].name == "System Programmable Logic Device"
    assert cats["Storage"][0].location == "Embedded RAID"  # DeviceContext captured
    assert cats["NIC"][0].location == "Embedded LOM"
    assert [c.version for c in cats["Drive"]] == ["HPG5", "HPG6"]
    assert len(cats["PSU"]) == 2
    others = {c.name: c.ref_key for c in cats["Other"]}
    assert others["Innovation Engine (IE) Firmware"] == "ie"
    assert others["Server Platform Services (SPS) Firmware"] == "sps"
    assert "Redundant System ROM" in others


def test_ilo6():
    r, _, _ = collect("ilo6_dl380g11", ServerRecord("h", "10.0.0.3", support="tpm"))
    assert r.collection_status == "ok", r.error
    assert (r.generation, r.bmc_type, r.support) == ("Gen11", "iLO 6", "tpm")
    cats = by_cat(r)
    assert cats["BIOS"][0].ref_key == "system-rom:U54"
    assert cats["Storage"][0].name == "HPE MR408i-o Gen11"
    assert cats["NIC"][0].location == "OCP 3.0 Slot 14"


def test_ilo4_legacy_inventory_is_partial():
    r, _, _ = collect("ilo4_dl380g9")
    assert r.collection_status == "partial"
    assert (r.vendor, r.generation, r.bmc_type) == ("hpe", "Gen9", "iLO 4")
    cats = by_cat(r)
    assert cats["BIOS"][0].version == "2.60" and cats["BIOS"][0].ref_key == "system-rom:P89"
    assert cats["BMC"][0].version == "2.55"
    assert cats["Storage"][0].name == "Smart HBA H240ar"
    assert cats["Drive"][0].name == "Drive EG0600FBVFP" and cats["Drive"][0].version == "HPDC"


def test_vendor_detection_fallbacks():
    assert detect_vendor({}, "Dell Inc.") == "dell"
    assert detect_vendor({}, "HPE") == "hpe"
    assert detect_vendor({"Oem": {"Hp": {}}}) == "hpe"
    assert detect_vendor({"Oem": {"Hpe": {}}}) == "hpe"
    assert detect_vendor({"Oem": {"Dell": {}}}) == "dell"
    assert detect_vendor({"Product": "Integrated Dell Remote Access Controller"}) == "dell"
    assert detect_vendor({"Oem": {"Supermicro": {}}}) == ""


def test_wrong_password_is_auth_failure():
    bad = StaticProvider({"dell": Credential("ro", "wrong")})
    r, _, _ = collect("idrac9_r740", provider=bad)
    assert (r.collection_status, r.error_class) == ("failed", "auth-401")
    assert r.vendor == ""  # never authenticated


def test_no_credentials_for_vendor():
    r, _, _ = collect("ilo5_dl380g10", provider=StaticProvider({"dell": Credential("ro", "secret")}))
    assert (r.collection_status, r.error_class) == ("failed", "no-credentials")


@pytest.mark.parametrize("exc,cls", [(conn_error(), "unreachable"), (ssl_error(), "tls")])
def test_network_failures(exc, cls):
    r, _, _ = collect(FakeSession("idrac9_r740", exc=exc), retries=0)
    assert (r.collection_status, r.error_class) == ("failed", cls)


def test_forbidden():
    fx = FakeSession("ilo5_dl380g10")
    fx.status_overrides["/redfish/v1/Systems"] = 403
    fx.status_overrides["/redfish/v1/Systems/"] = 403
    r, _, _ = collect(fx)
    assert r.error_class == "forbidden-403"


def test_no_redfish_service():
    r, _, _ = collect(FakeSession({"paths": {}}))
    assert (r.collection_status, r.error_class) == ("failed", "redfish-unsupported")


def test_save_raw_has_no_credentials():
    r, raw, _ = collect("ilo5_dl380g10", save_raw=True)
    assert "/redfish/v1/Systems/1/" in raw
    assert "secret" not in repr(raw)

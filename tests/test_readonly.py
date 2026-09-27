"""Phase 1 safety rule: only HTTP GET may ever be sent."""

import pytest

from fakebmc import FakeSession
from fwtool.collector import CollectOptions, collect_server
from fwtool.credentials import Credential, StaticProvider
from fwtool.inputs import ServerRecord
from fwtool.redfish.client import RedfishClient
from fwtool.redfish.errors import ReadOnlyViolation

ALL_FIXTURES = ["idrac9_r740", "idrac9_vxrail", "idrac8_r630", "idrac8_partial",
                "ilo4_dl380g9", "ilo5_dl380g10", "ilo6_dl380g11"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "HEAD", "OPTIONS"])
def test_non_get_methods_are_refused(method):
    sess = FakeSession("idrac9_r740")
    client = RedfishClient("10.0.0.1", Credential("u", "secret"), session=sess)
    with pytest.raises(ReadOnlyViolation):
        client.request(method, "/redfish/v1/Systems")
    assert sess.calls == []  # nothing reached the wire


@pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
def test_write_helpers_are_refused(verb):
    client = RedfishClient("10.0.0.1", Credential("u", "secret"), session=FakeSession("idrac9_r740"))
    with pytest.raises(ReadOnlyViolation):
        getattr(client, verb)("/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset", json={})


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_full_collection_only_sends_get(fixture):
    sess = FakeSession(fixture)
    provider = StaticProvider({"dell": Credential("ro", "secret"), "hpe": Credential("ro", "secret")})
    collect_server(ServerRecord("s1", "10.0.0.1"), provider, CollectOptions(), session_factory=lambda: sess)
    assert sess.calls, "expected requests"
    assert {m for m, _, _ in sess.calls} == {"GET"}


def test_session_ignores_environment_proxies_and_passes_verify(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/ca.pem")
    client = RedfishClient("10.0.0.1", None)
    assert client.session.trust_env is False
    sess = FakeSession("idrac9_r740")
    client = RedfishClient("10.0.0.1", None, session=sess, verify="/path/ca.pem")
    assert sess.trust_env is False
    client.get("/redfish/v1/", authenticated=False)
    assert sess.calls[0][2] == "/path/ca.pem"
    client = RedfishClient("10.0.0.1", None, session=FakeSession("idrac9_r740"))
    assert client.verify is False  # TLS verification off by default

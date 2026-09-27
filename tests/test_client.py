import pytest

from fakebmc import FakeResponse, FakeSession, conn_error, read_timeout, ssl_error
from fwtool.credentials import Credential
from fwtool.redfish.client import RedfishClient
from fwtool.redfish.errors import AUTH, FORBIDDEN, TLS, UNREACHABLE, UNSUPPORTED, NotFound, RedfishError


class Scripted:
    """Session returning a scripted sequence of responses/exceptions."""

    def __init__(self, seq):
        self.seq = list(seq)
        self.trust_env = True
        self.calls = 0

    def request(self, method, url, **kw):
        self.calls += 1
        item = self.seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(seq, **kw):
    sleeps = []
    c = RedfishClient("10.1.1.1", Credential("u", "p"), session=Scripted(seq), sleep=sleeps.append, **kw)
    return c, sleeps


def test_retries_5xx_then_succeeds():
    c, sleeps = client([FakeResponse(503), FakeResponse(502), FakeResponse(200, {"ok": 1})])
    assert c.get("/x") == {"ok": 1}
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] - 0.5  # exponential backoff


def test_429_honours_retry_after():
    c, sleeps = client([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {})])
    c.get("/x")
    assert sleeps[0] >= 7


def test_timeout_retried_then_classified_unreachable():
    c, sleeps = client([read_timeout()] * 4, retries=3)
    with pytest.raises(RedfishError) as e:
        c.get("/x")
    assert e.value.error_class == UNREACHABLE and "timeout" in e.value.detail
    assert len(sleeps) == 3


@pytest.mark.parametrize("status,cls", [(401, AUTH), (403, FORBIDDEN)])
def test_auth_errors_not_retried(status, cls):
    c, sleeps = client([FakeResponse(status)])
    with pytest.raises(RedfishError) as e:
        c.get("/x")
    assert e.value.error_class == cls and sleeps == []


def test_tls_error():
    c, _ = client([ssl_error()])
    with pytest.raises(RedfishError) as e:
        c.get("/x")
    assert e.value.error_class == TLS


def test_connection_error_retried_once():
    c, sleeps = client([conn_error(), conn_error()])
    with pytest.raises(RedfishError) as e:
        c.get("/x")
    assert e.value.error_class == UNREACHABLE and len(sleeps) == 1


def test_404_is_not_found_and_get_optional_returns_none():
    c, _ = client([FakeResponse(404), FakeResponse(404)])
    with pytest.raises(NotFound):
        c.get("/x")
    assert c.get_optional("/y") is None


def test_non_json_is_unsupported():
    c, _ = client([FakeResponse(200, "<html>")])
    with pytest.raises(RedfishError) as e:
        c.get("/x")
    assert e.value.error_class == UNSUPPORTED


def test_raw_recording():
    c = RedfishClient("h", None, session=FakeSession("idrac9_r740"), record_raw=True)
    c.get("/redfish/v1/", authenticated=False)
    assert "/redfish/v1/" in c.raw

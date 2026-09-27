import logging
import os

import pytest

from fwtool.credentials import Credential, build_provider, load_env_file, register_secret
from fwtool.logging_setup import RedactFilter


def test_repr_hides_password():
    c = Credential("svc_ro", "Sup3rS3cret!")
    assert "Sup3rS3cret!" not in repr(c) and "Sup3rS3cret!" not in str(c)
    assert "svc_ro" in repr(c)


def test_per_vendor_env_then_shared():
    env = {"DELL_BMC_USER": "dell_ro", "DELL_BMC_PASS": "d", "BMC_USER": "shared", "BMC_PASS": "s"}
    p = build_provider(interactive=False, environ=env)
    assert p.get("dell").username == "dell_ro"
    assert p.get("hpe").username == "shared"


def test_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# creds\nexport HPE_BMC_USER=hpe_ro\nHPE_BMC_PASS="p@ss=word"\n\n')
    assert load_env_file(f) == {"HPE_BMC_USER": "hpe_ro", "HPE_BMC_PASS": "p@ss=word"}
    p = build_provider(str(f), interactive=False, environ={})
    assert p.get("hpe").password == "p@ss=word" and p.get("dell") is None
    assert "HPE_BMC_PASS" not in os.environ  # file is not exported into the process env


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_env_file_permission_warning(tmp_path, caplog):
    f = tmp_path / ".env"
    f.write_text("BMC_USER=a\nBMC_PASS=b\n")
    f.chmod(0o644)
    with caplog.at_level(logging.WARNING):
        load_env_file(f)
    assert "readable by other users" in caplog.text


def test_log_redaction():
    register_secret("hunter2-XYZ")
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "auth failed with %s", ("hunter2-XYZ",), None)
    RedactFilter().filter(rec)
    assert "hunter2-XYZ" not in rec.getMessage() and "***" in rec.getMessage()

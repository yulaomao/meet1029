from __future__ import annotations

import pytest

from app.web.tls import socketio_tls_options


def test_tls_is_optional_when_no_files_are_configured():
    assert socketio_tls_options({}) == {}


def test_tls_rejects_a_partial_configuration(tmp_path):
    cert = tmp_path / "server.crt"
    cert.write_text("certificate")

    with pytest.raises(RuntimeError, match="configured together"):
        socketio_tls_options({"TLS_CERT_FILE": str(cert)})


def test_tls_returns_existing_certificate_and_key_pair(tmp_path):
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("certificate")
    key.write_text("key")

    assert socketio_tls_options({"TLS_CERT_FILE": str(cert), "TLS_KEY_FILE": str(key)}) == {
        "ssl_context": (str(cert), str(key))
    }

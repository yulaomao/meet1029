"""Optional TLS setup for a trusted local-network deployment."""

from __future__ import annotations

from pathlib import Path


def socketio_tls_options(config: dict) -> dict:
    """Return ``socketio.run`` TLS options after validating the pair.

    Empty values deliberately mean plain HTTP, which keeps isolated test and
    development launches working.  A partially configured pair must fail
    early: otherwise a mobile browser falls back to an insecure origin and
    cannot capture the microphone.
    """
    certificate = str(config.get("TLS_CERT_FILE") or "").strip()
    private_key = str(config.get("TLS_KEY_FILE") or "").strip()
    if not certificate and not private_key:
        return {}
    if not certificate or not private_key:
        raise RuntimeError("TLS_CERT_FILE and TLS_KEY_FILE must be configured together")
    if not Path(certificate).is_file() or not Path(private_key).is_file():
        raise RuntimeError("configured TLS certificate or private key does not exist")
    return {"ssl_context": (certificate, private_key)}

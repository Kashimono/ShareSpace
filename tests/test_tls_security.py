from __future__ import annotations

import os
import stat

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from sharespace.networking.tls_security import (
    SessionCertificateManager,
    certificate_fingerprint,
)


def test_session_certificate_contains_all_local_ip_sans_and_is_cleaned_up(tmp_path):
    manager = SessionCertificateManager(temporary_parent=tmp_path)
    certificate = manager.generate(["192.168.4.1", "10.0.0.2", "fd00::1"])
    parsed = x509.load_pem_x509_certificate(certificate.pem)
    subject_alt_names = parsed.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    addresses = {str(address) for address in subject_alt_names.get_values_for_type(x509.IPAddress)}

    assert {"192.168.4.1", "10.0.0.2", "fd00::1", "127.0.0.1", "::1"} <= addresses
    assert certificate_fingerprint(
        parsed.public_bytes(serialization.Encoding.DER)
    ) == certificate.fingerprint
    assert len(certificate.fingerprint) == 64
    assert certificate.private_key_path.exists()
    if os.name != "nt":
        assert stat.S_IMODE(certificate.private_key_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(certificate.directory.stat().st_mode) == 0o700

    key_path = certificate.private_key_path
    manager.cleanup()
    assert not key_path.exists()
    assert manager.certificate is None


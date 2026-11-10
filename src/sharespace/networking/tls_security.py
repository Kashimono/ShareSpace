"""Ephemeral host certificates and fingerprint-pinned TLS for local sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import socket
import ssl
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TLS_MINIMUM_VERSION = ssl.TLSVersion.TLSv1_2
TLS_PROBE_TIMEOUT_SECONDS = 5.0


class TLSVerificationError(RuntimeError):
    """A host certificate could not be pinned and validated."""


@dataclass(frozen=True)
class SessionCertificate:
    certificate_path: Path
    private_key_path: Path
    fingerprint: str
    pem: bytes
    ip_addresses: tuple[str, ...]
    directory: Path


class SessionCertificateManager:
    """Create and remove one ephemeral self-signed certificate per host session."""

    def __init__(self, *, temporary_parent: str | os.PathLike[str] | None = None) -> None:
        self.temporary_parent = Path(temporary_parent) if temporary_parent is not None else None
        self.certificate: SessionCertificate | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    def generate(self, host_addresses: list[str] | tuple[str, ...]) -> SessionCertificate:
        if self.certificate is not None:
            return self.certificate
        normalized: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = {
            ipaddress.ip_address(address) for address in host_addresses
        }
        normalized.update({ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")})
        if not normalized:
            raise TLSVerificationError("At least one local IP address is required for the host certificate.")

        now = datetime.now(timezone.utc)
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "ShareSpace local session"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ShareSpace"),
        ])
        names: list[x509.GeneralName] = [x509.IPAddress(address) for address in sorted(normalized, key=str)]
        names.append(x509.DNSName("localhost"))
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
            .sign(private_key, hashes.SHA256())
        )
        certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
        key_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        temporary = tempfile.TemporaryDirectory(
            prefix="sharespace-tls-", dir=str(self.temporary_parent) if self.temporary_parent else None
        )
        directory = Path(temporary.name)
        try:
            if os.name != "nt":
                os.chmod(directory, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            certificate_path = directory / "session-cert.pem"
            private_key_path = directory / "session-key.pem"
            _write_private_file(certificate_path, certificate_pem)
            _write_private_file(private_key_path, key_pem)
        except Exception:
            temporary.cleanup()
            raise
        self._temporary_directory = temporary
        self.certificate = SessionCertificate(
            certificate_path=certificate_path,
            private_key_path=private_key_path,
            fingerprint=certificate_fingerprint(certificate.public_bytes(serialization.Encoding.DER)),
            pem=certificate_pem,
            ip_addresses=tuple(str(address) for address in sorted(normalized, key=str)),
            directory=directory,
        )
        return self.certificate

    def cleanup(self) -> None:
        temporary = self._temporary_directory
        self._temporary_directory = None
        self.certificate = None
        if temporary is not None:
            temporary.cleanup()


class TLSConnectionSecurity:
    """Pin a session certificate, then use strict TLS verification on every request."""

    def __init__(
        self,
        host: str,
        port: int,
        expected_fingerprint: str,
        *,
        timeout_seconds: float = TLS_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.host = host
        self.port = port
        self.expected_fingerprint = normalize_fingerprint(expected_fingerprint)
        if len(self.expected_fingerprint) != 64:
            raise TLSVerificationError("Enter the host's complete SHA-256 certificate fingerprint.")
        self.timeout_seconds = timeout_seconds
        self._context: ssl.SSLContext | None = None
        self._verified = False
        self._lock = threading.Lock()
        self.peer_fingerprint: str | None = None
        self.tls_version: str | None = None

    @property
    def ssl_context(self) -> ssl.SSLContext:
        if not self._verified or self._context is None:
            raise TLSVerificationError("Verify the host certificate before connecting.")
        return self._context

    @property
    def verified(self) -> bool:
        return self._verified

    async def verify_peer(self) -> tuple[str, str]:
        return await asyncio.to_thread(self._verify_peer_sync)

    def _verify_peer_sync(self) -> tuple[str, str]:
        with self._lock:
            if self._verified and self._context is not None and self.peer_fingerprint is not None:
                return self.peer_fingerprint, self.tls_version or "TLS"
            der = _inspect_peer_certificate(self.host, self.port, self.timeout_seconds)
            actual = certificate_fingerprint(der)
            if not hmac.compare_digest(actual, self.expected_fingerprint):
                raise TLSVerificationError(
                    f"Host certificate fingerprint mismatch. Presented fingerprint: {format_fingerprint(actual)}"
                )
            pem = ssl.DER_cert_to_PEM_cert(der)
            context = _strict_client_context(pem)
            try:
                negotiated = _strict_tls_probe(
                    self.host, self.port, context, self.timeout_seconds
                )
            except (OSError, ssl.SSLError, ValueError) as error:
                raise TLSVerificationError(
                    "The host certificate is invalid, expired, or does not match the connection address."
                ) from error
            self._context = context
            self._verified = True
            self.peer_fingerprint = actual
            self.tls_version = negotiated
            return actual, negotiated

    async def inspect_changed_fingerprint(self) -> str | None:
        """Inspect a replacement certificate without sending application data."""
        try:
            der = await asyncio.to_thread(
                _inspect_peer_certificate,
                self.host,
                self.port,
                self.timeout_seconds,
            )
        except (OSError, ssl.SSLError, ValueError):
            return None
        actual = certificate_fingerprint(der)
        return actual if not hmac.compare_digest(actual, self.expected_fingerprint) else None


def certificate_fingerprint(der_certificate: bytes) -> str:
    return hashlib.sha256(der_certificate).hexdigest()


def normalize_fingerprint(value: str) -> str:
    return "".join(character for character in value.lower() if character in "0123456789abcdef")


def format_fingerprint(value: str) -> str:
    normalized = normalize_fingerprint(value)
    return ":".join(normalized[index:index + 2] for index in range(0, len(normalized), 2))


def _strict_client_context(certificate_pem: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = TLS_MINIMUM_VERSION
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cadata=certificate_pem)
    return context


def _probe_context(*, verify: bool) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = TLS_MINIMUM_VERSION
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED if verify else ssl.CERT_NONE
    return context


def _inspect_peer_certificate(host: str, port: int, timeout: float) -> bytes:
    """Fetch the peer certificate only; this socket never sends HTTP or credentials."""
    context = _probe_context(verify=False)
    with socket.create_connection((host, port), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
            certificate = tls_socket.getpeercert(binary_form=True)
            if not certificate:
                raise TLSVerificationError("The host did not present a TLS certificate.")
            return certificate


def _strict_tls_probe(host: str, port: int, context: ssl.SSLContext, timeout: float) -> str:
    with socket.create_connection((host, port), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
            return tls_socket.version() or "TLS"


def _write_private_file(path: Path, contents: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    if os.name != "nt":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

"""Remote Worker の実 TLS identity と mTLS context。"""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path


def certificate_fingerprint(certificate_der: bytes) -> str:
    """TLS socket から取得した DER 証明書の SHA-256 fingerprint。"""
    if not certificate_der:
        raise ValueError("peer certificate is required")
    return hashlib.sha256(certificate_der).hexdigest()


def certificate_file_fingerprint(certificate_file: Path) -> str:
    """PEM certificate file の leaf DER fingerprint。"""
    try:
        der = ssl.PEM_cert_to_DER_cert(certificate_file.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("invalid certificate file") from exc
    return certificate_fingerprint(der)


def create_server_context(ca_file: Path, certificate_file: Path, key_file: Path) -> ssl.SSLContext:
    """クライアント証明書を必須にする TLS 1.2+ server context。"""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = False
    context.load_cert_chain(certfile=str(certificate_file), keyfile=str(key_file))
    return context


def create_client_context(ca_file: Path, certificate_file: Path, key_file: Path) -> ssl.SSLContext:
    """server CA と gateway client certificate を固定する TLS context。"""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.load_cert_chain(certfile=str(certificate_file), keyfile=str(key_file))
    return context

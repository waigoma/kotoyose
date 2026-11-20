"""broker の障害箇所と例外種別だけを保持し、本文や例外メッセージは扱わない。"""

import asyncio
from dataclasses import dataclass
import socket
import ssl

import h11


_PHASES = frozenset({
    "connection_admission", "request_header", "request_body", "request_validation", "authorization",
    "upstream_connect", "upstream_write", "upstream_response_headers", "upstream_response_body",
    "response_validation", "downstream_response",
})
_CATEGORIES = frozenset({
    "dns", "tls_certificate", "tls", "timeout", "connection_reset", "connection_refused",
    "connection", "incomplete_read", "http_protocol", "io", "validation", "unknown",
})
_EXCEPTION_CATEGORIES = (
    (socket.gaierror, "dns"),
    (ssl.SSLCertVerificationError, "tls_certificate"),
    (ssl.SSLError, "tls"),
    (TimeoutError, "timeout"),
    (ConnectionResetError, "connection_reset"),
    (ConnectionRefusedError, "connection_refused"),
    (ConnectionError, "connection"),
    (asyncio.IncompleteReadError, "incomplete_read"),
    (h11.ProtocolError, "http_protocol"),
    (OSError, "io"),
    (ValueError, "validation"),
)


@dataclass(frozen=True)
class BrokerFailureDiagnostic:
    """固定語彙だけの観測。再試行や認可、停止、会計の判断には使わない。"""

    phase: str
    category: str

    def __post_init__(self):
        object.__setattr__(self, "phase", self.phase if type(self.phase) is str and self.phase in _PHASES else "unknown")
        object.__setattr__(self, "category", self.category if type(self.category) is str and self.category in _CATEGORIES else "unknown")


@dataclass
class BrokerObservation:
    """接続ごとの現在箇所。並行する別接続の進行を混ぜない。"""

    phase: str = "connection_admission"

    def failure(self, error: BaseException) -> BrokerFailureDiagnostic:
        """例外の本文、属性、型名を反射せず、既知の型との一致だけを残す。"""
        category = next((name for kind, name in _EXCEPTION_CATEGORIES if isinstance(error, kind)), "unknown")
        return BrokerFailureDiagnostic(self.phase, category)

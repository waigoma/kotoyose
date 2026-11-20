"""Transport-only media validation; repository evidence roles remain a caller concern.

Accept generic bounded MIME tokens without enabling additional image input formats.
Do not use artifact media declarations to authorize HTML/script execution in viewers.
"""
from __future__ import annotations
import re

_MEDIA_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+\Z", re.ASCII)

def validate_artifact_media_type(value: str) -> str:
    """Reject header injection, parameters, whitespace and unbounded media declarations."""
    if not isinstance(value, str) or not 3 <= len(value) <= 127 or not _MEDIA_TYPE.fullmatch(value):
        raise ValueError("artifact media_type must be a bounded MIME type without parameters")
    return value.lower()

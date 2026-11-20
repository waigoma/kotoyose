"""画像 Artifact の完全性と実行入力の共通契約。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from .execution_resources import ExecutionError


MAX_IMAGE_BYTES = 20 * 1024 * 1024
# Local finite admission/transport contract, not a provider/model maximum.
MAX_IMAGE_COUNT = 10
MAX_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024
_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
IMAGE_ARTIFACT_REF_PATTERN = re.compile(r"^artifact:[^:]+:.+:[0-9a-f]{64}$")


def validate_image_artifact(
    ref: str,
    artifact: dict,
    *,
    expected_digest: str | None = None,
    expected_media_type: str | None = None,
) -> dict:
    """Artifact bytes の digest・媒体・signature を admission と execute で再検証する。"""
    if IMAGE_ARTIFACT_REF_PATTERN.fullmatch(ref) is None:
        raise ExecutionError("invalid_request", "Image input requires an existing ArtifactRef", 400)
    content = artifact.get("content")
    media_type = artifact.get("media_type")
    digest = artifact.get("digest")
    if not isinstance(content, (bytes, bytearray)) or len(content) > MAX_IMAGE_BYTES:
        raise ExecutionError("invalid_request", "Image artifact exceeds the size limit", 400)
    if media_type not in _IMAGE_MEDIA_TYPES or not isinstance(digest, str):
        raise ExecutionError("unsupported_capability", "Image media type is unsupported", 501)
    actual = hashlib.sha256(content).hexdigest()
    if actual != digest or not ref.endswith(":" + digest):
        raise ExecutionError("request_conflict", "Image artifact digest changed", 409)
    if expected_digest is not None and digest != expected_digest:
        raise ExecutionError("request_conflict", "Image artifact digest changed", 409)
    if expected_media_type is not None and media_type != expected_media_type:
        raise ExecutionError("request_conflict", "Image artifact media type changed", 409)
    if not image_signature_valid(media_type, content):
        raise ExecutionError("invalid_request", "Image artifact signature does not match media type", 400)
    return {"digest": digest, "media_type": media_type, "size": len(content)}


@dataclass(frozen=True, slots=True)
class NativeImageInput:
    """Admission 済み ArtifactRef の execute 時 image payload。"""

    artifact_ref: str
    digest: str
    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    content: bytes


def image_signature_valid(media_type: str, content: bytes) -> bool:
    """MIME self-report だけでなく最小 magic bytes を検証する。"""
    return {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }.get(media_type, False)

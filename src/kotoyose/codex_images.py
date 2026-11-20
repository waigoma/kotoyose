"""Admission 済み画像だけを Codex の image wire に変換する。永続化はしない。"""

import base64

from .execution_images import MAX_IMAGE_COUNT, MAX_IMAGE_TOTAL_BYTES, NativeImageInput, validate_image_artifact
from .execution_resources import ExecutionError


def image_data_uris(images: tuple[NativeImageInput, ...]) -> tuple[str, ...]:
    """直接 adapter 呼出しでも bytes・digest・MIME・magic と合算上限を再照合する。"""
    if not isinstance(images, tuple) or len(images) > MAX_IMAGE_COUNT:
        raise ValueError("invalid_job")
    total = 0
    result = []
    for image in images:
        if not isinstance(image, NativeImageInput) or not isinstance(image.content, bytes):
            raise ValueError("invalid_job")
        try:
            validate_image_artifact(image.artifact_ref, {
                "content": image.content, "digest": image.digest, "media_type": image.media_type,
            })
        except (ExecutionError, TypeError):
            raise ValueError("invalid_job") from None
        total += len(image.content)
        if total > MAX_IMAGE_TOTAL_BYTES:
            raise ValueError("invalid_job")
        result.append(f"data:{image.media_type};base64," + base64.b64encode(image.content).decode("ascii"))
    return tuple(result)

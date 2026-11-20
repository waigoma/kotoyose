"""互換 message の順序を保ち、画像 bytes を Native の immutable Artifact へ移す。"""

from datetime import datetime

from .artifact_blob_store import ArtifactStorageError
from .execution_contract import ExecutionInput
from .execution_images import MAX_IMAGE_COUNT, MAX_IMAGE_TOTAL_BYTES
from .execution_resources import ExecutionError, canonical_json
from .input_artifacts import InputArtifactLimits, InputArtifactUpload, decode_input_upload
from .models import ChatCompletionRequest, ImageURLContentPart


def validate_chat_images(
    request: ChatCompletionRequest, *, expires_at: datetime, limits: InputArtifactLimits,
) -> dict[tuple[int, int], InputArtifactUpload]:
    """画像を含む要求全体を先に検証し、途中までの upload を作らない。"""
    images: dict[tuple[int, int], InputArtifactUpload] = {}
    transcript = []
    total_bytes = 0
    for message_index, message in enumerate(request.messages):
        parts = []
        if isinstance(message.content, str):
            parts.append({"type": "text", "text": message.content})
        else:
            for part_index, part in enumerate(message.content):
                if not isinstance(part, ImageURLContentPart):
                    parts.append({"type": "text", "text": part.text})
                    continue
                if message.role != "user":
                    raise ExecutionError("unsupported_capability", "Native chat images require user role", 501)
                url = part.image_url.url
                if not url.startswith("data:"):
                    raise ExecutionError("unsupported_capability", "External image fetch is unavailable with network:none", 501)
                if part.image_url.detail not in {None, "auto"}:
                    raise ExecutionError("unsupported_capability", "The runtime cannot enforce image detail", 501)
                if len(images) >= MAX_IMAGE_COUNT:
                    raise ExecutionError("invalid_request", "Chat exceeds the image count limit", 400)
                header, encoded = url.split(",", 1)
                upload = InputArtifactUpload(
                    schema_version="1", media_type=header[5:].removesuffix(";base64"),
                    data_base64=encoded, expires_at=expires_at,
                )
                try:
                    content, digest = decode_input_upload(upload, limits)
                except ValueError as error:
                    raise ExecutionError("invalid_request", str(error), 400) from error
                total_bytes += len(content)
                if total_bytes > MAX_IMAGE_TOTAL_BYTES:
                    raise ExecutionError("invalid_request", "Chat exceeds the aggregate image byte limit", 400)
                images[(message_index, part_index)] = upload
                parts.append({"type": "image", "digest": digest})
        if not message.text_content().strip() and not any(item.get("type") == "image" for item in parts):
            raise ExecutionError("invalid_request", "Chat messages must not be empty", 400)
        transcript.append({"role": message.role, "content": parts})
    if len(canonical_json(transcript).encode()) > 262144:
        raise ExecutionError("invalid_request", "Chat history exceeds the input byte limit", 400)
    return images


async def store_message_images(
    store, principal: str, limits: InputArtifactLimits,
    message_index: int, message, images: dict[tuple[int, int], InputArtifactUpload],
) -> tuple[str | list[dict], list[ExecutionInput]]:
    """文章内の参照と image input を同じ順番で結び付け、権威を昇格しない。"""
    if isinstance(message.content, str):
        return message.content, []
    parts, inputs = [], []
    for part_index, part in enumerate(message.content):
        if not isinstance(part, ImageURLContentPart):
            parts.append({"type": "text", "text": part.text})
            continue
        try:
            artifact = await store.put_input_artifact(principal, images[(message_index, part_index)], limits)
        except ArtifactStorageError as error:
            raise ExecutionError("artifact_storage_error", "Input artifact storage is unavailable", 503) from error
        except ValueError as error:
            raise ExecutionError("invalid_request", str(error), 400) from error
        parts.append({"type": "image", "artifact_ref": artifact["ref"], "digest": artifact["digest"]})
        inputs.append(ExecutionInput(kind="image", provenance="external_untrusted", content_ref=artifact["ref"]))
    return parts, inputs

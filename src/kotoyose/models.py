"""
OpenAI 互換のリクエスト/レスポンス Pydantic モデル。

OpenAI の /v1/chat/completions が期待する JSON 形式を定義。
必要最小限のフィールドのみ。
"""

import base64
import binascii
import re
from typing import Annotated, Literal

from pydantic.json_schema import SkipJsonSchema
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .capabilities import ChatMetadata
from .runtime.base import RuntimeImageInput, RuntimePrompt, RuntimeTextInput


_IMAGE_DATA_URL_RE = re.compile(
    r"^data:(image/(?:jpeg|png|gif|webp));base64,([A-Za-z0-9+/]+={0,2})$"
)
_MAX_IMAGE_DATA_BYTES = 20 * 1024 * 1024
_MAX_IMAGES_PER_MESSAGE = 20


class ImageURL(BaseModel):
    """OpenAI Chat Completions の image_url オブジェクト。"""

    url: str
    detail: Literal["auto", "low", "high"] | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """provider が扱える HTTPS / HTTP / base64 data URL だけを受理する。"""
        if value.startswith(("https://", "http://")):
            return value
        match = _IMAGE_DATA_URL_RE.fullmatch(value)
        if match is None:
            raise ValueError(
                "image_url.url must be an http(s) URL or a base64 JPEG/PNG/GIF/WebP data URL"
            )
        encoded = match.group(2)
        if len(encoded) > 4 * ((_MAX_IMAGE_DATA_BYTES + 2) // 3):
            raise ValueError("image_url.url exceeds the 20 MiB image limit")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image_url.url contains invalid base64 data") from exc
        if len(decoded) > _MAX_IMAGE_DATA_BYTES:
            raise ValueError("image_url.url exceeds the 20 MiB image limit")
        return value


class TextContentPart(BaseModel):
    """Chat Completions のテキスト content part。"""

    type: Literal["text"]
    text: str


class ImageURLContentPart(BaseModel):
    """Chat Completions の画像 content part。"""

    type: Literal["image_url"]
    image_url: ImageURL


class FileContentPart(BaseModel):
    type: Literal["file", "input_file", "input_image", "image", "image_file", "attachment"]


ChatContentPart = Annotated[
    TextContentPart | ImageURLContentPart | FileContentPart,
    Field(discriminator="type"),
]


class ChatMessage(BaseModel):
    """文字列または OpenAI 互換 content parts を持つチャットメッセージ。"""

    role: str       # "user" / "assistant" / "system"
    content: str | list[ChatContentPart]
    attachments: SkipJsonSchema[object] = Field(default=None, exclude=True)
    files: SkipJsonSchema[object] = Field(default=None, exclude=True)
    images: SkipJsonSchema[object] = Field(default=None, exclude=True)
    tool_calls: SkipJsonSchema[object] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_image_count(self) -> "ChatMessage":
        """1 message の画像数を provider の現実的な上限内に制限する。"""
        if isinstance(self.content, list):
            image_count = sum(
                isinstance(part, ImageURLContentPart) for part in self.content
            )
            if image_count > _MAX_IMAGES_PER_MESSAGE:
                raise ValueError("content contains more than 20 images")
        return self

    def text_content(self) -> str:
        """ログ・token 推定用に、画像データを除いたテキストだけを返す。"""
        if isinstance(self.content, str):
            return self.content
        return "\n".join(
            part.text for part in self.content if isinstance(part, TextContentPart)
        )

    def runtime_prompt(self) -> RuntimePrompt:
        """ユーザー content を provider 共通の runtime 入力へ変換する。"""
        if isinstance(self.content, str):
            return self.content
        result: list[RuntimeTextInput | RuntimeImageInput] = []
        for part in self.content:
            if isinstance(part, TextContentPart):
                result.append(RuntimeTextInput(text=part.text))
            elif isinstance(part, ImageURLContentPart):
                result.append(
                    RuntimeImageInput(
                        url=part.image_url.url,
                        detail=part.image_url.detail,
                    )
                )
        if not any(isinstance(part, RuntimeImageInput) for part in result):
            return "\n".join(
                part.text for part in result if isinstance(part, RuntimeTextInput)
            )
        return result

    def canonical_content(self) -> str | list[dict[str, object]]:
        """会話キー計算用に JSON 化可能な安定表現を返す。"""
        if isinstance(self.content, str):
            return self.content
        return [part.model_dump(mode="json", exclude_none=True) for part in self.content]


class ChatCompletionRequest(BaseModel):
    model: str                  # ← これを会話キーに流用する
    messages: list[ChatMessage]
    stream: bool = False
    tools: SkipJsonSchema[object] = Field(default=None, exclude=True)
    tool_choice: SkipJsonSchema[object] = Field(default=None, exclude=True)
    functions: SkipJsonSchema[object] = Field(default=None, exclude=True)
    function_call: SkipJsonSchema[object] = Field(default=None, exclude=True)
    attachments: SkipJsonSchema[object] = Field(default=None, exclude=True)
    files: SkipJsonSchema[object] = Field(default=None, exclude=True)
    images: SkipJsonSchema[object] = Field(default=None, exclude=True)
    # per-request cwd 上書き。preset の allow_cwd_override=True の場合のみ有効。
    # OpenAI 互換クライアントはこのフィールドを送らないだけで、既存動作は変わらない。
    cwd: str | None = None
    # この turn だけ alias の effort を上書きする。未対応の runtime と、
    # 判明している候補の外の値は 422 になる。
    reasoning_effort: str | None = None
    # OpenAI と同じ欄。priority だけが Fast。default / auto / 未指定は速度を変えない。
    # 未対応の runtime と、catalog が Fast を出していないモデルは 422 になる。
    service_tier: str | None = None
    # Kotoyose 専用 metadata は strict に検証し、未知の保証要求を落とさない。
    metadata: ChatMetadata | None = None

    model_config = ConfigDict(json_schema_extra={
        "examples": [
            {
                "model": "kotoyose-chat",
                "messages": [
                    {"role": "user", "content": "今日のタスク整理を手伝って"}
                ],
                "stream": False,
            },
            {
                "model": "kotoyose-news",
                "messages": [
                    {"role": "user", "content": "以下のニュースを要約してください\n\n[ニュース本文...]"}
                ],
                "stream": False,
            },
            {
                "model": "kotoyose-chat",
                "messages": [
                    {"role": "user", "content": "ストリーミングで応答して"}
                ],
                "stream": True,
            },
            {
                "model": "coding-default",
                "messages": [
                    {"role": "user", "content": "src/main.py にバグがある。修正してください。"}
                ],
                "stream": False,
                "cwd": "/workspace/worktrees/task-42-run-1",
            },
        ]
    })


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class CompletionTokenDetails(BaseModel):
    reasoning_tokens: int = 0


class ChatCompletionUsage(BaseModel):
    """OpenAI 互換応答へ返す token 使用量。未取得の料金は null とする。"""

    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    completion_tokens_details: CompletionTokenDetails | None = Field(default=None, exclude_if=lambda value: value is None)


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    # pool alias を名指したときだけ、選ばれた候補 alias。
    resolved_model: str | None = Field(default=None, exclude_if=lambda value: value is None)

    model_config = ConfigDict(json_schema_extra={
        "examples": [
            {
                "id": "chatcmpl-abc123def456",
                "object": "chat.completion",
                "created": 1712707200,
                "model": "kotoyose-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "承知しました。今日のタスクを整理しましょう。",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        ]
    })


class ErrorDetail(BaseModel):
    message: str
    type: str = "internal_error"
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorDetail

    model_config = ConfigDict(json_schema_extra={
        "examples": [
            {
                "error": {
                    "message": "Claude SDK error: connection refused",
                    "type": "internal_error",
                    "request_id": "req_abc12345",
                }
            }
        ]
    })


class ChatCompletionChunkDelta(BaseModel):
    role: str | None = None
    content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]

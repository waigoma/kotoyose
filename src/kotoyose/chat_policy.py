"""Chat request に適用する provider 非依存の受付 policy。"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ModelPreset


_IMAGE_INPUT_PROVIDERS = frozenset({"claude", "codex", "cursor"})


class ChatPolicyViolation(ValueError):
    """Provider を起動する前に拒否すべき chat request を表す。"""


def validate_required_headers(
    preset: ModelPreset,
    headers: Mapping[str, str],
) -> None:
    """Preset が要求する request header がすべて存在することを検証する。

    Args:
        preset: 適用する model preset。
        headers: 大文字小文字を区別しない HTTP request header mapping。

    Raises:
        ChatPolicyViolation: 必須 header が 1 件以上不足している場合。
    """
    missing_headers = [
        name for name in preset.required_request_headers if not headers.get(name)
    ]
    if missing_headers:
        raise ChatPolicyViolation(
            f"missing required request header(s): {', '.join(missing_headers)}"
        )


def validate_cwd(
    preset: ModelPreset,
    model: str,
    requested_cwd: str | None,
) -> None:
    """Request 単位の cwd 上書きが preset の許可境界内か検証する。

    Args:
        preset: 適用する model preset。
        model: Error message に使用する公開 model 名。
        requested_cwd: Request body 由来の cwd。未指定なら検証しない。

    Raises:
        ChatPolicyViolation: 上書き未許可、directory 不在、または許可 root 外の場合。
    """
    if requested_cwd is None:
        return
    if not preset.allow_cwd_override:
        raise ChatPolicyViolation(
            f"preset '{model}' は allow_cwd_override=False のため"
            " cwd の上書きを許可していません"
        )

    expanded_cwd = os.path.expanduser(requested_cwd)
    if not os.path.isdir(expanded_cwd):
        raise ChatPolicyViolation(
            f"cwd does not exist or is not a directory: {expanded_cwd}"
        )

    resolved_cwd = Path(expanded_cwd).resolve()
    allowed_roots = [
        Path(os.path.expanduser(root)).resolve()
        for root in (preset.cwd_allowed_roots or [])
    ]
    if not any(resolved_cwd.is_relative_to(root) for root in allowed_roots):
        raise ChatPolicyViolation(
            f"cwd {resolved_cwd} は許可ルート外です。"
            f" 許可ルート: {', '.join(str(root) for root in allowed_roots)}"
        )


def validate_image_input(provider: str, image_urls: Sequence[str]) -> None:
    """画像入力を provider が欠落なく安全に受け取れることを検証する。

    Args:
        provider: Runtime provider 名。
        image_urls: Request 全体から抽出した画像 URL。

    Raises:
        ChatPolicyViolation: Provider 非対応、または local-only provider に remote URL がある場合。
    """
    if image_urls and provider not in _IMAGE_INPUT_PROVIDERS:
        raise ChatPolicyViolation(
            f"provider '{provider}' does not support image input"
        )
    if provider in {"codex", "cursor"} and any(
        not url.startswith("data:") for url in image_urls
    ):
        raise ChatPolicyViolation(
            f"provider '{provider}' requires image_url.url to be a base64 data URL"
        )


"""Chat request policy の単体テスト。"""

from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from kotoyose.chat_policy import (
    ChatPolicyViolation,
    validate_cwd,
    validate_image_input,
    validate_required_headers,
)


def _preset(**overrides):
    values = {
        "required_request_headers": [],
        "allow_cwd_override": False,
        "cwd_allowed_roots": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_required_headers_accept_case_insensitive_match():
    """HTTP header 名の大文字小文字を区別せず、宣言済み header を受理する。"""
    preset = _preset(required_request_headers=["X-Taskhub-Mcp-Token"])

    validate_required_headers(
        preset,
        Headers({"x-taskhub-mcp-token": "secret"}),
    )


def test_required_headers_report_all_missing_names():
    """不足 header が複数ある場合、設定名をまとめて報告する。"""
    preset = _preset(required_request_headers=["X-One", "X-Two"])

    with pytest.raises(ChatPolicyViolation, match="X-One, X-Two"):
        validate_required_headers(preset, Headers())


def test_cwd_rejects_override_when_policy_disallows_it(tmp_path):
    """存在する directory でも opt-in されていなければ拒否する。"""
    with pytest.raises(ChatPolicyViolation, match="allow_cwd_override=False"):
        validate_cwd(_preset(), "example", str(tmp_path))


def test_cwd_accepts_allowed_root_and_descendant(tmp_path):
    """許可 root 自身とその配下を受理する。"""
    child = tmp_path / "worktree"
    child.mkdir()
    preset = _preset(
        allow_cwd_override=True,
        cwd_allowed_roots=[str(tmp_path)],
    )

    validate_cwd(preset, "example", str(tmp_path))
    validate_cwd(preset, "example", str(child))


def test_cwd_rejects_sibling_with_same_text_prefix(tmp_path):
    """文字列 prefix が同じだけの隣接 directory を許可 root と誤認しない。"""
    allowed = tmp_path / "workspace"
    sibling = tmp_path / "workspace-evil"
    allowed.mkdir()
    sibling.mkdir()
    preset = _preset(
        allow_cwd_override=True,
        cwd_allowed_roots=[str(allowed)],
    )

    with pytest.raises(ChatPolicyViolation, match="許可ルート外"):
        validate_cwd(preset, "example", str(sibling))


def test_cwd_rejects_symlink_escape(tmp_path):
    """許可 root 内の symlink から外部 directory へ抜ける経路を拒否する。"""
    allowed = tmp_path / "workspace"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    link = allowed / "escape"
    link.symlink_to(outside, target_is_directory=True)
    preset = _preset(
        allow_cwd_override=True,
        cwd_allowed_roots=[str(allowed)],
    )

    with pytest.raises(ChatPolicyViolation, match="許可ルート外"):
        validate_cwd(preset, "example", str(link))


@pytest.mark.parametrize("provider", ["antigravity"])
def test_image_input_rejects_provider_without_image_path(provider):
    """画像を安全に運べない provider は黙って破棄せず拒否する。"""
    with pytest.raises(ChatPolicyViolation, match="does not support image input"):
        validate_image_input(provider, ["data:image/png;base64,AA=="])


@pytest.mark.parametrize("provider", ["codex", "cursor"])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_local_image_input_requires_data_url(provider, scheme):
    """Codex では remote URL を拒否し、data URL だけを受理する。"""
    with pytest.raises(ChatPolicyViolation, match="requires image_url.url"):
        validate_image_input(provider, [f"{scheme}://example.com/image.png"])

    validate_image_input(provider, ["data:image/png;base64,AA=="])


def test_claude_image_input_accepts_remote_url():
    """Claude の native image path では remote URL を受理する。"""
    validate_image_input("claude", ["https://example.com/image.png"])


"""child と host が共有するフレーム、登録コード、拒否理由。

stream 0 は制御です。turn はそのほかの stream を使います。data stream は上限付きの
queue に置き、満杯の frame は捨てます。制御フレームの取り出しは待ちません。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
from datetime import datetime
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from .connection_state_store import VALID_AUTH_STATES, VALID_QUOTA_SOURCES
from .provider_spec import CODEX_TURNS_FEATURE as CODEX_TURNS_FEATURE

PROTOCOL_VERSION = 2
LOGIN_STATUS_FEATURE = "login-status"
SDK_SKILLS_FEATURE = "sdk-skills/1"
SKILL_FOLDERS_FEATURE = "skill-folders/1"
REPOSITORY_SKILLS_FEATURE = "repository-skills/1"
MAX_RECORD = 1024 * 1024
# 1 MiB のファイルを Base64 にした分と、turn のプロンプト・メタデータを収める。
MAX_SKILL_TURN_RECORD = 2 * MAX_RECORD
PROMPT_PREFIX_SKILLS_FEATURE = "prompt-prefix-skills/1"
PROVIDER_DATA_FEATURE = "provider-data/1"
PROVIDER_DATA_MCP_FEATURE = "provider-data-mcp/1"
PROVIDER_DATA_UNRESTRICTED_FEATURE = "provider-data-unrestricted/1"
WORKSPACE_POLICY_FEATURE = "workspace-policy/1"
LOCAL_LLM_FEATURE = "local-llm/1"
QUOTA_POLICY_REFUSAL = "quota refused by child policy"
CONTROL_STREAM = 0
CHILD_CONNECTION_FIELDS = frozenset({"provider", "kind", "data_policy", "on", "profile"})
LOCAL_LLM_PRESET_FIELDS = frozenset({"route", "on", "timeout_seconds", "cwd_allowed_roots"})
# data stream の frame の上限。溢れたら黙って捨てず、その stream を失敗にする (本文が欠けるため)。
STREAM_QUEUE_LIMIT = 4096
# ログインの PTY 出力と、残量の解析結果。token のファイルは載せない。
LOGIN_CAP_SECONDS = 600
LOGIN_OUTPUT_LIMIT = 4096
LOGIN_INPUT_LIMIT = 512
LOGIN_MESSAGE_LIMIT = 200
QUOTA_WINDOW_LIMIT = 64
QUOTA_RESULT_LIMIT = 8192
ARGV_MAX = 16
ARG_LIMIT = 128
_LOGIN_FLOWS = frozenset({"paste_code", "browser_wait", "device_code"})
_QUOTA_KEYS = frozenset({"auth", "quota", "quota_source", "plan", "expires_at", "last_error"})
_QUOTA_ITEM_KEYS = frozenset({"window", "label", "used_percent", "resets_at", "resets_text", "source"})


class ChildRejected(Exception):
    """host が child の要求を拒んだ。retryable でなければ run は終了する。"""

    def __init__(self, reason: str, *, retryable: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class HostFingerprintMismatch(ChildRejected):
    """TLS の相手が、登録コードに書いた host fingerprint ではない。"""

    def __init__(self, reason: str = "host certificate fingerprint mismatch"):
        super().__init__(reason, retryable=False)


def rejection(reason: str) -> ChildRejected:
    """版の不一致と revoke は繋ぎ直しても同じなので、run を止める。"""
    permanent = (
        "revoked" in reason
        or "version mismatch" in reason
        or "protocol mismatch" in reason
        or "unknown child" in reason
        or "not enrolled" in reason
        or "certificate required" in reason
    )
    return ChildRejected(reason, retryable=not permanent)


def current_package_version() -> str:
    """インストールされている kotoyose の版。hello はこれの完全一致を求める。"""
    try:
        return version("kotoyose")
    except PackageNotFoundError:
        return "0"


@dataclass(frozen=True, slots=True)
class Frame:
    stream: int
    kind: str
    payload: dict


def is_skill_folder_turn(envelope: object) -> bool:
    """大きなレコードを許容するフォルダー付き turn の外形だけを判定する。"""
    if not isinstance(envelope, dict) or envelope.get("kind") != "turn":
        return False
    payload = envelope.get("payload")
    return isinstance(payload, dict) and isinstance(payload.get("skill_folders"), list)


def encode_frame(stream: int, kind: str, payload: dict | None = None) -> str:
    body = {"stream": stream, "kind": kind, "payload": {} if payload is None else payload}
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def decode_frame(raw: str | bytes) -> Frame:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("frame is not json") from exc
    if not isinstance(body, dict):
        raise ValueError("frame is not an object")
    stream = body.get("stream")
    kind = body.get("kind")
    payload = body.get("payload", {})
    if isinstance(stream, bool) or not isinstance(stream, int) or stream < 0:
        raise ValueError("frame stream is invalid")
    if not isinstance(kind, str) or not kind:
        raise ValueError("frame kind is invalid")
    if not isinstance(payload, dict):
        raise ValueError("frame payload is invalid")
    if kind == "provider_catalog":
        if stream != CONTROL_STREAM:
            raise ValueError("invalid catalog stream")
        _validate_provider_catalog(payload)
    elif kind in _SPECIAL and (stream == CONTROL_STREAM or stream > 1_000_000_000):
        raise ValueError("invalid operation stream")
    _validate_special(kind, payload)
    return Frame(stream=stream, kind=kind, payload=payload)


def _validate_provider_catalog(payload: dict) -> None:
    """Bound a declaration snapshot. Each document is checked again by the child."""
    from .provider_data import MAX_CATALOG, MAX_DOCUMENT, MAX_OFFERS

    _exact(payload, frozenset({"offers"}))
    offers = payload.get("offers")
    if not isinstance(offers, list) or len(offers) > MAX_OFFERS:
        raise ValueError("invalid provider catalog")
    total = 0
    for item in offers:
        if not isinstance(item, dict) or set(item) != {"id", "sha256", "document"}:
            raise ValueError("invalid provider catalog")
        document = item["document"]
        if not isinstance(document, str) or len(document.encode("utf-8")) > MAX_DOCUMENT:
            raise ValueError("invalid provider catalog")
        _name(item.get("id"), "provider")
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid provider catalog")
        total += len(document.encode("utf-8"))
    if total > MAX_CATALOG:
        raise ValueError("invalid provider catalog")


def _validate_special(kind: str, payload: dict) -> None:
    """ログイン・状態・残量の frame を形と大きさで検証する。"""
    checker = _SPECIAL.get(kind)
    if checker is not None:
        checker(payload)


def _exact(payload: dict, allowed: frozenset[str]) -> None:
    if set(payload) - allowed:
        raise ValueError("frame payload is invalid")


def _name(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"frame {label} is invalid")
    if any(char.isspace() or char == "\x00" for char in value):
        raise ValueError(f"frame {label} is invalid")


def _text(value: object, limit: int) -> None:
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise ValueError("frame text is invalid")


def _argv(value: object) -> None:
    if not isinstance(value, list) or not value or len(value) > ARGV_MAX:
        raise ValueError("frame command is invalid")
    for item in value:
        if not isinstance(item, str) or not item or len(item) > ARG_LIMIT or "\x00" in item:
            raise ValueError("frame command is invalid")


def _profile(payload: dict) -> None:
    if "profile" not in payload or payload["profile"] is None:
        return
    _name(payload["profile"], "profile")


def _validate_login(payload: dict) -> None:
    _exact(payload, frozenset({"provider", "profile", "flow", "command"}))
    _name(payload.get("provider"), "provider")
    _profile(payload)
    flow = payload.get("flow")
    if not isinstance(flow, str) or flow not in _LOGIN_FLOWS:
        raise ValueError("frame login is invalid")
    _argv(payload.get("command"))


def _validate_login_input(payload: dict) -> None:
    _exact(payload, frozenset({"code"}))
    code = payload.get("code")
    _text(code, LOGIN_INPUT_LIMIT)
    if not code or any(ch.isspace() or not ch.isprintable() for ch in code):
        raise ValueError("frame login code is invalid")


def _validate_login_output(payload: dict) -> None:
    _exact(payload, frozenset({"url", "user_code"}))
    _text(payload.get("url"), LOGIN_OUTPUT_LIMIT)
    code = payload.get("user_code")
    if code is not None and (not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9-]{1,32}", code) is None):
        raise ValueError("frame device code is invalid")


def _validate_login_exit(payload: dict) -> None:
    _exact(payload, frozenset({"returncode", "success"}))
    if not isinstance(payload.get("success"), bool):
        raise ValueError("frame login result is invalid")
    code = payload.get("returncode")
    if isinstance(code, bool) or not isinstance(code, int) or not -1_000_000 < code < 1_000_000:
        raise ValueError("frame returncode is invalid")


def _validate_login_cancel(payload: dict) -> None:
    _exact(payload, frozenset())


def _validate_message(payload: dict) -> None:
    _exact(payload, frozenset({"message"}))
    _text(payload.get("message"), LOGIN_MESSAGE_LIMIT)
    if not payload.get("message"):
        raise ValueError("frame message is invalid")


def _validate_quota(payload: dict) -> None:
    _exact(payload, frozenset({"provider", "profile", "command"}))
    _name(payload.get("provider"), "provider")
    _profile(payload)
    _argv(payload.get("command"))


def _validate_status(payload: dict) -> None:
    _exact(payload, frozenset({"provider", "profile"}))
    _name(payload.get("provider"), "provider")
    _profile(payload)


def _validate_status_result(payload: dict) -> None:
    _exact(payload, frozenset({"state"}))
    if payload.get("state") not in ("logged_in", "logged_out", "unknown"):
        raise ValueError("frame login status is invalid")


def _validate_quota_result(payload: dict) -> None:
    _exact(payload, _QUOTA_KEYS)
    auth = payload.get("auth")
    if not isinstance(auth, str) or auth not in VALID_AUTH_STATES:
        raise ValueError("frame quota is invalid")
    source = payload.get("quota_source")
    if source is not None and (not isinstance(source, str) or source not in VALID_QUOTA_SOURCES):
        raise ValueError("frame quota source is invalid")
    for key in ("plan", "last_error"):
        if payload.get(key) is not None:
            _text(payload[key], LOGIN_MESSAGE_LIMIT)
    _date(payload.get("expires_at"))
    quota = payload.get("quota")
    if not isinstance(quota, list) or len(quota) > 32:
        raise ValueError("frame quota is invalid")
    for item in quota:
        if not isinstance(item, dict) or set(item) - _QUOTA_ITEM_KEYS:
            raise ValueError("frame quota is invalid")
        window = item.get("window")
        if not isinstance(window, str) or not window or len(window) > QUOTA_WINDOW_LIMIT:
            raise ValueError("frame quota is invalid")
        used = item.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100 or not math.isfinite(used):
            raise ValueError("frame quota is invalid")
        for key in ("label", "resets_text", "source"):
            if item.get(key) is not None:
                _text(item[key], LOGIN_MESSAGE_LIMIT)
        _date(item.get("resets_at"))
    if len(json.dumps(payload)) > QUOTA_RESULT_LIMIT:
        raise ValueError("frame quota is too large")


def _date(value: object) -> None:
    if value is not None:
        _text(value, 64)
        datetime.fromisoformat(value)


_SPECIAL = {
    "login": _validate_login,
    "login_input": _validate_login_input,
    "login_output": _validate_login_output,
    "login_exit": _validate_login_exit,
    "login_cancel": _validate_login_cancel,
    "login_error": _validate_message,
    "status": _validate_status,
    "status_cancel": _validate_login_cancel,
    "status_result": _validate_status_result,
    "status_error": _validate_message,
    "quota": _validate_quota,
    "quota_cancel": _validate_login_cancel,
    "quota_result": _validate_quota_result,
    "quota_error": _validate_message,
}


class StreamMux:
    """stream ごとの queue。制御 stream は上限を持たず、データ stream が塞いでも進む。"""

    def __init__(self, max_queue: int = STREAM_QUEUE_LIMIT):
        self.max_queue = max_queue
        self.control: asyncio.Queue[Frame | None] = asyncio.Queue()
        self._streams: dict[int, asyncio.Queue[Frame]] = {}
        self._overflowed: set[int] = set()
        self.mcp_turns: dict[int, object] = {}

    def queue(self, stream: int) -> asyncio.Queue[Frame]:
        existing = self._streams.get(stream)
        if existing is None:
            existing = asyncio.Queue(maxsize=self.max_queue)
            self._streams[stream] = existing
        return existing

    def discard(self, stream: int) -> None:
        self._streams.pop(stream, None)
        self._overflowed.discard(stream)

    def dispatch(self, frame: Frame) -> None:
        if frame.kind == "mcp_request":
            if turn := self.mcp_turns.get(frame.stream):
                turn.accept(frame.payload)
            return
        if frame.kind == "mcp_response":
            return
        if frame.stream == CONTROL_STREAM:
            self.control.put_nowait(frame)
            return
        if frame.kind in _SPECIAL and frame.stream not in self._streams:
            return
        if frame.stream in self._overflowed:
            return
        if frame.kind in {"result", "error"} and (turn := self.mcp_turns.get(frame.stream)):
            turn.cancel()
        queue = self.queue(frame.stream)
        if queue.qsize() >= self.max_queue - 1:
            # 最後の 1 枠に失敗の frame を入れ、以後のこの stream の frame は捨てる。読む側は error として終わる。
            self._overflowed.add(frame.stream)
            queue.put_nowait(Frame(stream=frame.stream, kind="error", payload={
                "message": "child stream overflowed", "diagnostics": {"terminal_reason": "stream_overflow"},
                "retryable": False,
            }))
            return
        queue.put_nowait(frame)

    def close(self) -> None:
        for turn in self.mcp_turns.values():
            turn.cancel()
        self.control.put_nowait(None)
        # 待っている turn を、制御 stream を塞がずに起こす。
        for queue in self._streams.values():
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(Frame(stream=0, kind="closed", payload={}))
            except asyncio.QueueFull:
                pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def enrollment_parts(code: str) -> tuple[dict, bytes, bytes]:
    """登録コードの claims と、署名の対象バイトと署名。署名の正しさは見ない。"""
    pieces = code.split(".")
    if len(pieces) != 3 or pieces[0] != "kyc1" or not pieces[1] or not pieces[2]:
        raise ChildRejected("enrollment code rejected")
    try:
        body = _b64decode(pieces[1])
        signature = _b64decode(pieces[2])
        payload = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        raise ChildRejected("enrollment code rejected") from None
    if not isinstance(payload, dict):
        raise ChildRejected("enrollment code rejected")
    fingerprint = payload.get("fp")
    host = payload.get("host")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
    ):
        raise ChildRejected("enrollment code rejected")
    if not isinstance(host, str) or ":" not in host:
        raise ChildRejected("enrollment code rejected")
    return payload, body, signature


def pack_enrollment_code(body: bytes, signature: bytes) -> str:
    return "kyc1." + _b64encode(body) + "." + _b64encode(signature)

SESSION_COLLECT_FEATURE = "session-collect/1"
SESSION_COLLECT_POLICY_REFUSAL = "session collection refused by child policy"
SESSION_COLLECT_BATCH_MAX_BYTES = 512 * 1024

"""Built-in agy output hooks for the print CLI driver."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from typing import TYPE_CHECKING, Any

from .print_cli_events import (
    PrintCliStreamOutcome,
    _RESPONSE_STEP_TYPES,
    _make_runtime_event,
    _values_from_payload,
    extract_session_id,
    extract_text_delta,
    extract_usage,
    parse_json_text,
    runtime_event_from_step,
    structured_error_fields,
)

if TYPE_CHECKING:
    from .print_cli_hooks import ParsedQuota
    from .runtime.base import RuntimeEvent

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_AGY_GROUP_SHORT_NAMES = {
    "gemini models": "Gemini",
    "claude and gpt models": "Claude & GPT",
    "gemini 4 argon": "Argon",
}
_AGY_LOGIN_REQUIRED_MARKERS = (
    "sign in required",
    "sign-in required",
    "sign in is required",
    "sign-in is required",
    "please sign in",
    "not signed in",
    "requires sign-in",
    "requires sign in",
    "must sign in",
    "need to sign in",
    "not logged in",
    "please log in",
    "login required",
    "requires login",
    "logged out",
    "login expired",
    "session expired",
    "re-authenticate",
    "reauthenticate",
    "authentication required",
    "authentication_failed",
    "unauthenticated",
)


def parse_agy_error(stderr: str) -> dict[str, str | int | bool]:
    """Extract structured AGY_ERROR metadata from stderr tail."""
    for line in reversed(stderr.splitlines()):
        if line.startswith("AGY_ERROR:"):
            value = parse_json_text(line.removeprefix("AGY_ERROR:").strip())
            if value is not None:
                return structured_error_fields(value)
    return {}


class AgyStreamJsonParser:
    """Parser for `agy --output-format stream-json` (`agy_stream_json_v1`) NDJSON."""

    def __init__(self) -> None:
        self._saw_init = False
        self._saw_result = False
        self._session_id: str | None = None
        self._result_status: str | None = None
        self._text_parts: list[str] = []
        self._final_response: str | None = None
        self._usage_tokens: dict[str, int] = {}
        self._turns: int | None = None
        self._malformed_lines = 0
        self._diagnostics: dict[str, Any] = {}
        self._raw_error_texts: list[str] = []

    def feed_line(self, line: str) -> list[RuntimeEvent]:
        stripped = line.strip()
        if not stripped:
            return []
        payload = parse_json_text(stripped)
        if payload is None:
            self._malformed_lines += 1
            return []

        session_id = extract_session_id(payload)
        if session_id and self._session_id is None:
            self._session_id = session_id

        event_type = payload.get("event")
        if not isinstance(event_type, str):
            event_type = payload.get("type") if isinstance(payload.get("type"), str) else ""

        if event_type == "init":
            self._saw_init = True
            init_obj = payload.get("init") if isinstance(payload.get("init"), dict) else {}
            sanitized_payload: dict[str, Any] = {}
            for key in ("model", "permission_mode"):
                value = init_obj.get(key)
                if isinstance(value, str) and value:
                    sanitized_payload[key] = value
            return [_make_runtime_event(kind="init", summary="init", payload=sanitized_payload or None)]

        if event_type == "step_update":
            step = payload.get("step_update")
            step_dict = step if isinstance(step, dict) else payload
            usage = extract_usage(payload)
            if usage:
                self._usage_tokens.update(usage)

            step_event = runtime_event_from_step(step_dict)
            if step_event is not None:
                return [step_event]

            step_type = str(step_dict.get("step_type") or "").lower()
            state = step_dict.get("state")
            if step_type in _RESPONSE_STEP_TYPES:
                delta = extract_text_delta(step_dict)
                if delta:
                    self._text_parts.append(delta)
            elif step_type == "error":
                self._collect_error_metadata(step_dict)

            progress_payload: dict[str, Any] = {}
            for key in ("step_index", "state", "duration_seconds"):
                value = step_dict.get(key)
                if isinstance(value, (str, int, float, bool)):
                    progress_payload[key] = value
            if step_type:
                progress_payload["step_type"] = step_type
            summary = " ".join(part for part in (step_type or "step_update", str(state) if state else "") if part)
            return [
                _make_runtime_event(
                    kind="step_update",
                    summary=summary or None,
                    payload=progress_payload or None,
                )
            ]

        if event_type == "result":
            self._saw_result = True
            result_obj = payload.get("result")
            result_dict = result_obj if isinstance(result_obj, dict) else payload
            status = result_dict.get("status") or payload.get("status")
            if isinstance(status, str) and status.strip():
                self._result_status = status.strip().upper()
            response = result_dict.get("response")
            if isinstance(response, str):
                self._final_response = response
            num_turns = result_dict.get("num_turns")
            if not isinstance(num_turns, int) or isinstance(num_turns, bool):
                num_turns = result_dict.get("turns")
            if isinstance(num_turns, int) and not isinstance(num_turns, bool) and num_turns >= 0:
                self._turns = num_turns
            usage = extract_usage(payload)
            if usage:
                self._usage_tokens.update(usage)
            self._collect_error_metadata(payload)
            if result_dict is not payload:
                self._collect_error_metadata(result_dict)

            result_payload: dict[str, Any] = {}
            if self._result_status:
                result_payload["status"] = self._result_status
            if self._turns is not None:
                result_payload["num_turns"] = self._turns
            duration = result_dict.get("duration_seconds")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                result_payload["duration_seconds"] = duration
            return [
                _make_runtime_event(
                    kind="result",
                    summary=(self._result_status or "result").lower(),
                    payload=result_payload or None,
                )
            ]

        if event_type == "error":
            self._collect_error_metadata(payload)
        return []

    def _collect_error_metadata(self, obj: dict[str, Any]) -> None:
        self._diagnostics.update(structured_error_fields(obj))
        err = obj.get("error")
        if isinstance(err, dict):
            self._diagnostics.update(structured_error_fields(err))
            for key in ("message", "detail", "reason", "status", "code"):
                val = err.get(key)
                if isinstance(val, str) and val.strip():
                    self._raw_error_texts.append(val.strip())
        elif isinstance(err, str) and err.strip():
            self._raw_error_texts.append(err.strip())
        for key in ("error_message", "message", "detail", "reason"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                self._raw_error_texts.append(val.strip())

    def finalize(self) -> PrintCliStreamOutcome:
        content = self._final_response if self._final_response is not None else "".join(self._text_parts)
        input_tokens = self._usage_tokens.get("input_tokens")
        output_tokens = self._usage_tokens.get("output_tokens")
        has_usage = input_tokens is not None or output_tokens is not None
        usage: dict[str, Any] = {
            "source": "reported" if has_usage else "unknown",
            "cost": None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        return PrintCliStreamOutcome(
            saw_init=self._saw_init,
            saw_result=self._saw_result,
            session_id=self._session_id,
            result_status=self._result_status,
            content=content,
            usage=usage,
            turns=self._turns,
            malformed_lines=self._malformed_lines,
            diagnostics=dict(self._diagnostics),
            raw_error_texts=list(self._raw_error_texts),
        )


def _antigravity_cli_model_details(stdout: bytes) -> list[dict[str, Any]]:
    """Parse `<id>\\t<name>` lines (or JSON fallback) from `agy models` into model details."""
    text = stdout.decode(errors="replace").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    details: list[dict[str, Any]] = []
    seen: set[str] = set()
    if parsed is not None:
        for item in _values_from_payload(parsed):
            if isinstance(item, str):
                model_id = item.strip()
                display_name = model_id
            elif isinstance(item, dict):
                model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
                display_name = str(
                    item.get("name") or item.get("displayName") or item.get("label") or model_id
                ).strip()
            else:
                continue
            if model_id and model_id not in seen:
                seen.add(model_id)
                details.append({"id": model_id, "name": display_name or model_id, "label": display_name or model_id})
        return details

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "\t" in line:
            raw_id, raw_name = line.split("\t", 1)
            model_id = raw_id.strip()
            display_name = raw_name.strip() or model_id
        else:
            model_id = line
            display_name = line
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        details.append({"id": model_id, "name": display_name, "label": display_name})
    return details


def _agy_short_group(group_name: Any) -> str:
    raw = str(group_name or "").strip()
    if not raw:
        return ""
    return _AGY_GROUP_SHORT_NAMES.get(raw.lower(), raw)


def _agy_window_label(bucket: Mapping[str, Any]) -> str:
    raw_window = str(bucket.get("window") or "").strip().lower()
    if raw_window in {"weekly", "7d", "seven_day"}:
        return "Weekly"
    if raw_window in {"5h", "five_hour", "session"}:
        return "Session (5h)"
    bucket_name = str(bucket.get("name") or "").strip()
    return bucket_name or raw_window or "Quota"


def _agy_error_text(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    parts: list[str] = []
    for key in ("error", "message", "response", "status"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
        elif isinstance(val, Mapping):
            msg = val.get("message") or val.get("detail") or val.get("code")
            if isinstance(msg, str) and msg.strip():
                parts.append(msg)
    return "\n".join(parts)


def parse_antigravity_usage_output(
    raw_stdout: bytes | str,
    *,
    stderr: bytes | str = "",
) -> ParsedQuota:
    """Parse `agy -p /usage --output-format json` stdout/stderr without storing raw output."""
    stdout_text = (
        raw_stdout.decode("utf-8", errors="replace") if isinstance(raw_stdout, bytes) else str(raw_stdout)
    )
    stdout_clean = _ANSI_ESCAPE_RE.sub("", stdout_text).strip()
    stderr_text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr)
    stderr_clean = _ANSI_ESCAPE_RE.sub("", stderr_text).strip()

    payload: dict[str, Any] | None = None
    if stdout_clean:
        try:
            decoded = json.loads(stdout_clean)
            if isinstance(decoded, dict):
                payload = decoded
        except ValueError:
            payload = None

    if payload is not None:
        status = str(payload.get("status") or "").strip().upper()
        is_error = bool(payload.get("is_error")) or (bool(status) and status != "SUCCESS")
        command_obj = payload.get("command")
        data_obj = command_obj.get("data") if isinstance(command_obj, Mapping) else None
        groups = data_obj.get("groups") if isinstance(data_obj, Mapping) else None

        if not is_error and isinstance(groups, list):
            windows: list[dict[str, Any]] = []
            for group in groups:
                if not isinstance(group, Mapping):
                    continue
                short_group = _agy_short_group(group.get("name"))
                buckets = group.get("buckets")
                if not isinstance(buckets, list):
                    continue
                for bucket in buckets:
                    if not isinstance(bucket, Mapping):
                        continue
                    bucket_id = str(bucket.get("id") or bucket.get("window") or "").strip()
                    if not bucket_id:
                        continue
                    rem_raw = bucket.get("remaining_fraction")
                    if (
                        isinstance(rem_raw, bool)
                        or not isinstance(rem_raw, (int, float))
                        or not math.isfinite(float(rem_raw))
                    ):
                        continue
                    used_percent = round(max(0.0, min(100.0, (1.0 - float(rem_raw)) * 100.0)), 1)
                    win_label = _agy_window_label(bucket)
                    label = f"{short_group} · {win_label}" if short_group else win_label
                    raw_reset = bucket.get("reset_time")
                    resets_at = raw_reset.strip() if isinstance(raw_reset, str) and raw_reset.strip() else None
                    windows.append(
                        {
                            "window": bucket_id,
                            "label": label,
                            "used_percent": used_percent,
                            "resets_at": resets_at,
                            "source": "agy_cli_usage",
                        }
                    )
            if windows:
                return {
                    "auth": "ok",
                    "quota": windows,
                    "quota_source": "agy_cli_usage",
                    "last_error": None,
                }

    payload_text = _agy_error_text(payload) if payload is not None else stdout_clean
    combined_lower = f"{payload_text}\n{stderr_clean}".lower()
    if any(marker in combined_lower for marker in _AGY_LOGIN_REQUIRED_MARKERS):
        return {
            "auth": "logged_out",
            "quota": [],
            "quota_source": "agy_cli_usage",
            "last_error": None,
        }

    return {
        "auth": "unknown",
        "quota": [],
        "quota_source": "agy_cli_usage",
        "last_error": "Antigravity usage unavailable",
    }



"""Cross-model session continue / fork: handoff context, privacy policy, and fork metadata."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from .config import HarnessConfig
from .data_policy import alias_data_policy
from .session_import.redaction import sanitize_text, truncate_utf8

DEFAULT_MAX_TURNS = 20
DEFAULT_MAX_CONTEXT_BYTES = 32 * 1024

# A very long parent key must not use up the byte budget meant for the turns.
_MAX_HEADER_KEY_BYTES = 256
_FOOTER = "[End of Handoff Context]"


def parse_turn_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the turn metadata as a dict, or ``{}`` when it is absent or malformed."""
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, RecursionError):
            return {}
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def active_turns(turns: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return the turns that can be handed off: not soft-deleted and not empty."""
    return [
        row for row in turns
        if row.get("deleted_at") is None
        and ((row.get("prompt") or "").strip() or (row.get("response") or "").strip())
    ]


def derive_origin_hint(turns: Sequence[Mapping[str, Any]]) -> str | None:
    """Return the strictest origin hint of any turn: ``work``, then ``personal``, else ``None``."""
    hints = [parse_turn_metadata(row).get("origin_hint") for row in turns]
    for hint in ("work", "personal"):
        if hint in hints:
            return hint
    return None


def check_handoff_data_policy(
    config: HarnessConfig, target_alias: str, *, origin_hint: str | None,
) -> str | None:
    """Return why the history must not be handed off to ``target_alias``, or ``None`` if it may.

    ``work`` history is refused when the alias, or any member of a pool alias, uses a
    configured connection whose ``data_policy`` is not ``no_training``. Training
    acknowledgements do not lift this refusal.
    """
    if origin_hint != "work":
        return None
    pool = config.pools.get(target_alias)
    members = pool.candidates if pool is not None else [target_alias]
    decisions = [alias_data_policy(config, member) for member in members]
    blocked = [f"'{item.alias}' (data_policy={item.policy})" for item in decisions if item.restricted]
    if not blocked:
        return None
    return (
        f"Cannot hand off work-origin conversation history to alias '{target_alias}': "
        f"{', '.join(blocked)} does not have data_policy=no_training."
    )


def build_handoff_context(
    turns: Sequence[Mapping[str, Any]], *, parent_conversation_key: str,
    max_turns: int = DEFAULT_MAX_TURNS, max_bytes: int = DEFAULT_MAX_CONTEXT_BYTES,
) -> tuple[str, int, bool]:
    """Format the most recent turns as a credential-redacted handoff block.

    The oldest turns are dropped first to fit ``max_bytes``. A single turn that is
    still too large is truncated. Turn numbers are positions among all active turns.

    Returns:
        ``(handoff_text, included_count, was_truncated)``.
    """
    candidates = active_turns(turns)
    total = len(candidates)
    selected = candidates[-max_turns:] if max_turns > 0 else []
    was_truncated = len(selected) < total
    key = parent_conversation_key.encode("utf-8")[:_MAX_HEADER_KEY_BYTES].decode("utf-8", errors="ignore")

    entries: list[tuple[int, str, str]] = []
    for number, row in enumerate(selected, start=total - len(selected) + 1):
        # Redaction runs on the full text, before any budget cut can split a secret.
        prompt, _, prompt_cut = sanitize_text(row.get("prompt") or "")
        response, _, response_cut = sanitize_text(row.get("response") or "")
        was_truncated = was_truncated or prompt_cut or response_cut
        entries.append((number, prompt, response))

    # Each block costs its own bytes plus the newline that joins it to the frame.
    sizes = [_utf8_size(_render_turn(*entry)) + 1 for entry in entries]
    remaining = sum(sizes)
    dropped = 0
    while (
        len(entries) - dropped > 1
        and _utf8_size(_render(key, [], len(entries) - dropped, total)) + remaining > max_bytes
    ):
        remaining -= sizes[dropped]
        dropped += 1
    entries = entries[dropped:]
    was_truncated = was_truncated or dropped > 0

    blocks = [_render_turn(*entry) for entry in entries]
    text = _render(key, blocks, len(blocks), total)
    if _utf8_size(text) > max_bytes and len(entries) == 1:
        number, prompt, response = entries[0]
        frame = _render(key, [_render_turn(number, "", "")], 1, total)
        prompt, response = _fit_turn(prompt, response, max_bytes - _utf8_size(frame))
        text = _render(key, [_render_turn(number, prompt, response)], 1, total)
        was_truncated = True
    # Final guard for budgets too small to hold even the frame.
    text, cut = truncate_utf8(text, max_bytes)
    return text, len(entries), was_truncated or cut


def compose_fork_prompt(handoff_context: str, user_message: str) -> str:
    """Join the handoff block and the new user message into the first fork prompt."""
    return f"{handoff_context}\n\n{user_message.strip()}"


def new_fork_conversation_key() -> str:
    """Return a new random conversation key for a fork."""
    return f"fork:{uuid4().hex[:16]}"


def build_fork_metadata(
    *, parent_conversation_key: str, parent_provider: str | None, parent_runtime_session_id: str | None,
    included_turns: int, total_turns: int, truncated: bool, origin_hint: str | None,
) -> dict[str, Any]:
    """Build the interaction metadata that links a fork to its parent session."""
    return {
        "forked_from": parent_conversation_key,
        "parent_conversation_key": parent_conversation_key,
        "parent_provider": parent_provider,
        "parent_runtime_session_id": parent_runtime_session_id,
        "handoff_turns_included": included_turns,
        "handoff_turns_total": total_turns,
        "handoff_truncated": truncated,
        **({"origin_hint": origin_hint} if origin_hint else {}),
    }


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _render_turn(number: int, prompt: str, response: str) -> str:
    return f"--- Turn {number} (user) ---\n{prompt}\n--- Turn {number} (assistant) ---\n{response}"


def _render(key: str, blocks: list[str], included: int, total: int) -> str:
    header = (
        "[Conversation Handoff Context]\n"
        f"Prior conversation history from session '{key}' (included {included} of {total} turns). "
        "Continue seamlessly from where the conversation left off.\n"
    )
    return "\n".join([header, *blocks, _FOOTER])


def _fit_turn(prompt: str, response: str, budget: int) -> tuple[str, str]:
    """Truncate one turn to ``budget`` bytes, giving each side half unless the other needs less."""
    prompt, _ = truncate_utf8(prompt, max(budget // 2, budget - _utf8_size(response)))
    response, _ = truncate_utf8(response, budget - _utf8_size(prompt))
    return prompt, response

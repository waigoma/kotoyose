"""Canonical JSON shared with Hub's JavaScript digest."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Mapping


def agent_canonical_json(value: object) -> str:
    def string(value: str) -> str:
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise ValueError('surrogate code point is not allowed')
        return json.dumps(value, ensure_ascii=False)

    def key_order(key: str):
        if re.fullmatch(r'0|[1-9][0-9]*', key) and int(key) <= 4294967294:
            return (0, int(key))
        return (1, key.encode('utf-16-be'))

    def encode(item):
        if item is None:
            return 'null'
        if isinstance(item, bool):
            return 'true' if item else 'false'
        if isinstance(item, int):
            if abs(item) > 2**53 - 1:
                raise ValueError('integer outside safe range')
            return str(item)
        if isinstance(item, str):
            return string(item)
        if isinstance(item, list):
            return '[' + ','.join(encode(child) for child in item) + ']'
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError('object keys must be strings')
            for key in item:
                string(key)
            return '{' + ','.join(string(key) + ':' + encode(item[key]) for key in sorted(item, key=key_order)) + '}'
        raise ValueError('only JSON safe integers are supported')

    return encode(value)


def agent_digest(value: object) -> str:
    return hashlib.sha256(agent_canonical_json(value).encode('utf-8')).hexdigest()


def agent_work_digest(raw: Mapping) -> str:
    body = raw['execution']
    return agent_digest({key: body[key] for key in ('work', 'money', 'predecessor', 'time', 'resources', 'recovery', 'authority_ceilings')})


def agent_request_digest(raw: Mapping) -> str:
    return agent_digest({'execution': raw['execution']})


def read_agent_json(body: bytes) -> dict:
    from .agent_execution_contract import AgentExecutionError

    def reject(_):
        raise ValueError('non-integer number')

    try:
        value = json.loads(body, parse_float=reject, parse_constant=reject)
        if not isinstance(value, dict):
            raise ValueError('expected an object')
        agent_canonical_json(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AgentExecutionError('invalid_request', 'Invalid JSON body', 422) from exc


def agent_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError('timezone required')
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parse_agent_utc(value: str) -> datetime:
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z', value):
        raise ValueError('UTC timestamp required')
    return datetime.fromisoformat(value[:-1] + '+00:00')

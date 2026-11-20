"""Allowlisted provider limit evidence, without provider messages or headers."""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import re
from typing import Literal

from .execution_limits import ProviderUsageLimit
from .print_cli_profiles import PrintCliProfile, get_print_cli_profile

_WINDOWS = frozenset({'five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage'})
_CODEX_CODES = {'usageLimitExceeded': 'unknown', 'rateLimitExceeded': 'rate'}
_WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
_MONTHS = {name: index for index, name in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
_MONTH = r'(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
_TIME = r'(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})'
_HTTP_DATES = tuple(re.compile(pattern, re.ASCII) for pattern in (
    rf'(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun), (?P<day>[0-9]{{2}}) {_MONTH} (?P<year>[0-9]{{4}}) {_TIME} GMT',
    rf'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), (?P<day>[0-9]{{2}})-{_MONTH}-(?P<year>[0-9]{{2}}) {_TIME} GMT',
    rf'(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun) {_MONTH} (?P<day> [1-9]|[0-9]{{2}}) {_TIME} (?P<year>[0-9]{{4}})',
))


def _retry_after(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        if value.isascii() and value.isdecimal() and len(value) <= 10:
            return now + timedelta(seconds=int(value))
        match = next(((index, parts) for index, pattern in enumerate(_HTTP_DATES)
                      if (parts := pattern.fullmatch(value)) is not None), None)
        if match is None:
            return None
        index, parts = match
        fields = parts.groupdict()
        year = int(fields['year'])
        month, day = _MONTHS[fields['month']], int(fields['day'])
        hour, minute, second = (int(fields[name]) for name in ('hour', 'minute', 'second'))
        if index == 1:
            year += now.year // 100 * 100
            if (year, month, day, hour, minute, second) > (
                    now.year + 50, now.month, now.day, now.hour, now.minute, now.second):
                year -= 100
        date = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        weekday = _WEEKDAYS[date.weekday()]
        if (weekday if index == 1 else weekday[:3]) != fields['weekday']:
            return None
        return date
    except (ValueError, TypeError, OverflowError):
        return None


def _diagnostic_text(evidence: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in ('text', 'stderr_tail', 'message'):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    lines = evidence.get('error_lines')
    if isinstance(lines, (list, tuple)):
        for item in lines:
            if isinstance(item, str) and item.strip():
                parts.append(item)
    return '\n'.join(parts)


def classify_print_cli_usage_limit(
    profile: PrintCliProfile,
    evidence: Mapping[str, object],
    detected_at: datetime,
) -> ProviderUsageLimit | None:
    """Classify structured/diagnostic evidence against a print CLI profile's quota rules."""
    now = detected_at.astimezone(timezone.utc)
    http_status = evidence.get('http_status')
    if http_status is None:
        http_status = evidence.get('api_error_status')
    if type(http_status) is int and http_status in (401, 403):
        return None
    raw_status = evidence.get('status')
    canonical_status = raw_status.strip().upper() if isinstance(raw_status, str) else ''
    if canonical_status in {'UNAUTHENTICATED', 'PERMISSION_DENIED'}:
        return None

    diag_text = _diagnostic_text(evidence)
    lowered = diag_text.lower()
    if any(pattern.lower() in lowered for pattern in profile.auth_error_patterns):
        return None

    matched_rule = None
    for rule in profile.quota_error_patterns:
        if canonical_status and canonical_status in rule.statuses:
            matched_rule = rule
            break
        if type(http_status) is int and http_status in rule.http_statuses:
            matched_rule = rule
            break
        if diag_text and any(re.search(pat, diag_text, re.IGNORECASE) for pat in rule.patterns):
            matched_rule = rule
            break
    if matched_rule is None:
        return None

    window = matched_rule.window if matched_rule.window in (_WINDOWS | {'rate', 'unknown'}) else 'unknown'
    reset = _retry_after(evidence.get('retry_after'), now)
    if reset is None:
        stamp = evidence.get('resets_at')
        if type(stamp) is int and stamp >= 0:
            try:
                reset = datetime.fromtimestamp(stamp, timezone.utc)
            except (ValueError, OverflowError, OSError):
                reset = None
    if reset is None and matched_rule.default_retry_seconds is not None:
        reset = now + timedelta(seconds=matched_rule.default_retry_seconds)
    return ProviderUsageLimit(
        provider='print_cli',
        limit_window=window,
        reset_at=reset,
        detected_at=now,
        resume='unavailable',
        unavailable_reason='no_session',
    )


def normalize_provider_usage_limit(provider: Literal['codex', 'claude', 'print_cli'], evidence: Mapping[str, object],
                                   detected_at: datetime) -> ProviderUsageLimit | None:
    """Convert known terminal evidence to the fixed, safe wire contract."""
    now = detected_at.astimezone(timezone.utc)
    reset = None
    if provider == 'claude':
        http_status = evidence.get('http_status')
        if type(http_status) is int and http_status in (401, 403):
            return None
        if type(http_status) is int and http_status == 429:
            window = 'rate'
        elif evidence.get('status') == 'rejected' or evidence.get('subtype') == 'error_rate_limit':
            window = evidence.get('rate_limit_type')
        else:
            return None
        window = window if isinstance(window, str) and window in _WINDOWS | {'rate'} else 'unknown'
        stamp = evidence.get('overage_resets_at') if window == 'overage' else evidence.get('resets_at')
        if type(stamp) is int and stamp >= 0:
            try:
                reset = datetime.fromtimestamp(stamp, timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
    elif provider == 'codex':
        status = evidence.get('http_status')
        if status in (401, 403):
            return None
        code = evidence.get('code')
        if type(status) is int and status == 429:
            window = 'rate'
            reset = _retry_after(evidence.get('retry_after'), now)
        elif isinstance(code, str) and code in _CODEX_CODES:
            window = _CODEX_CODES[code]
        else:
            return None
    elif provider == 'print_cli':
        profile_name = evidence.get('profile')
        profile_id = profile_name if isinstance(profile_name, str) and profile_name else 'agy'
        try:
            profile = get_print_cli_profile(profile_id)
        except KeyError:
            return None
        return classify_print_cli_usage_limit(profile, evidence, detected_at)
    else:
        return None
    return ProviderUsageLimit(provider=provider, limit_window=window, reset_at=reset, detected_at=now,
                              resume='unavailable', unavailable_reason='no_session')

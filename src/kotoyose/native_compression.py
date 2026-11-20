"""Runtime が報告した圧縮・context 使用量だけを、本文なしで観測する。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable


CONTEXT_SAMPLE_SECONDS = 1.0
MAX_COMPACTION_EVENTS = 128


class NativeCompressionObserver:
    """欠測をゼロと扱わず、観測のために実行期限を延長しない。"""

    def __init__(self, emit: Callable[[str, dict], Awaitable[None]] | None):
        self.emit = emit
        self.boundaries = 0

    async def boundary(self, data: dict) -> None:
        """SDK の compact_boundary から公開できる数値と分類だけを採る。"""
        if self.emit is None:
            return
        self.boundaries += 1
        if self.boundaries > MAX_COMPACTION_EVENTS:
            if self.boundaries == MAX_COMPACTION_EVENTS + 1:
                await self.emit('compression_observation_limited', {'source': 'provider_runtime', 'recorded_boundaries': MAX_COMPACTION_EVENTS})
            return
        metadata = data.get('compact_metadata') if isinstance(data, dict) else None
        metadata = metadata if isinstance(metadata, dict) else {}
        trigger = metadata.get('trigger')
        tokens = metadata.get('pre_tokens')
        await self.emit('compression_boundary', {
            'mode': 'runtime_managed', 'source': 'provider_runtime',
            'trigger': trigger if isinstance(trigger, str) and trigger in {'auto', 'manual'} else 'unobserved',
            'pre_tokens': tokens if type(tokens) is int and tokens >= 0 else None,
            'coverage': 'partial',
        })

    async def sample(self, client, deadline: float) -> None:
        """最終応答後に有限時間で採取し、raw memory / Tool / prompt 情報は捨てる。"""
        if self.emit is None:
            return
        observation = {'state': 'unavailable', 'source': 'provider_runtime', 'reason': 'runtime_observation_unavailable'}
        remaining = min(CONTEXT_SAMPLE_SECONDS, deadline - asyncio.get_running_loop().time())
        if remaining >= CONTEXT_SAMPLE_SECONDS:
            try:
                async with asyncio.timeout(remaining):
                    raw = await client.get_context_usage()
                observation = context_usage_projection(raw)
            except Exception:
                # 観測に失敗しても「圧縮なし」や context 使用量 0 に置き換えない。
                pass
        await self.emit('context_usage', observation)


def context_usage_projection(raw: dict) -> dict:
    """CLI の optional data を検査してから、本文を含まない観測値に変換する。"""
    if not isinstance(raw, dict):
        raise ValueError('Invalid context observation')
    for key in ('totalTokens', 'maxTokens', 'rawMaxTokens'):
        if type(raw.get(key)) is not int or raw[key] < 0:
            raise ValueError('Invalid context token count')
    percentage = raw.get('percentage')
    if type(percentage) not in {int, float} or not math.isfinite(percentage) or not 0 <= percentage <= 100:
        raise ValueError('Invalid context percentage')
    if type(raw.get('isAutoCompactEnabled')) is not bool or raw['maxTokens'] < 1 or raw['rawMaxTokens'] < raw['maxTokens']:
        raise ValueError('Invalid context window')
    return {'state': 'observed', 'source': 'provider_runtime', 'total_tokens': raw['totalTokens'],
            'max_tokens': raw['maxTokens'], 'raw_max_tokens': raw['rawMaxTokens'],
            'percentage': percentage, 'automatic_enabled': raw['isAutoCompactEnabled']}

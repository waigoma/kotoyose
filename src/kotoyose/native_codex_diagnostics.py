"""Native Codex の失敗を、既知の分類と数値だけで投影する。"""

from .runtime.base import RuntimeEvent
from .runtime.codex import CodexTurnError
from .codex_broker_diagnostics import BrokerFailureDiagnostic


_STAGES = frozenset({"rpc_response", "thread_start", "turn_completed", "error_notification", "broker"})
_LIMIT_STAGES = frozenset({
    "protocol_frame_bytes", "protocol_line_bytes", "rpc_pending_count", "settings_pending_count",
    "rpc_pending_bytes", "settings_pending_bytes",
    "tool_result_bytes", "agent_message_bytes", "final_response_bytes", "redacted_output_bytes",
})


class NativeCodexLimitError(ValueError):
    """既存の出力上限理由を保ち、固定段階と数値だけを診断へ渡す。

    未知の段階は unknown、不正な数値は null とし、入力本文を保持しない。
    observed は検出時に観測できた量であり、未受信分の推定値ではない。
    """

    def __init__(self, stage: str, observed: object = None, limit: object = None):
        super().__init__("output_limit_exceeded")
        self.stage = stage if isinstance(stage, str) and stage in _LIMIT_STAGES else "unknown"
        self.observed = observed if type(observed) is int and observed >= 0 else None
        self.limit = limit if type(limit) is int and limit > 0 else None

    def as_event(self) -> RuntimeEvent:
        """既存 journal へ保存する固定キーだけの上限診断を返す。"""
        return RuntimeEvent(kind="error", summary="output_limit_exceeded", payload={
            "schema_version": 1, "failure_kind": "output_limit_exceeded",
            "failure_stage": self.stage, "observed": self.observed, "limit": self.limit,
        })


class NativeCodexProviderError(ValueError):
    """従来の provider_error を維持し、安全な診断だけを既存 journal へ渡す。

    stage は既知の段階以外を unknown にし、error は本文を保持せず分類する。
    この例外は再試行、認可、停止、会計の判断を変更しない。
    """

    def __init__(self, stage: str, error: object = None, *, turn_status: object = None, http_status: object = None,
                 broker_diagnostic: BrokerFailureDiagnostic | None = None):
        super().__init__("provider_error")
        self.stage = stage if isinstance(stage, str) and stage in _STAGES else "unknown"
        self.classification = (CodexTurnError.from_turn_error(error, status=turn_status) if error is not None
                               else CodexTurnError(status=turn_status, http_status_code=http_status))
        self.broker_diagnostic = (broker_diagnostic if self.stage == "broker"
                                  and isinstance(broker_diagnostic, BrokerFailureDiagnostic) else None)

    def as_event(self) -> RuntimeEvent:
        """既存の Attempt ID と時刻で相関する、固定キーの診断を一件返す。"""
        return RuntimeEvent(kind="error", summary="provider_error", payload={
            "schema_version": 1,
            "failure_stage": self.stage,
            "provider_code": self.classification.code,
            "http_status": self.classification.http_status_code,
            "turn_status": self.classification.status,
            **({"broker_phase": self.broker_diagnostic.phase, "broker_error": self.broker_diagnostic.category}
               if self.broker_diagnostic is not None else {}),
        })

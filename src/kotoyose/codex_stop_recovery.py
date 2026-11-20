"""通常 reconcile で保存済みの停止証拠と同じ所有範囲の終了を照合する。"""

import asyncio
from collections.abc import Awaitable, Callable

from .codex_process_stop import stop_codex_process
from .codex_provider_scope import stop_recovered_provider_scope
from .codex_stop_evidence import (
    EgressBasis, egress_recovery_source, make_egress_stop_evidence, make_namespace_stop_evidence,
    namespace_recovery_source, stop_evidence_context, validate_egress_stop_evidence,
    validate_namespace_stop_evidence, validate_stop_evidence,
)
from .process_identity import ProcessIdentity


EgressOwnerBasis = Callable[[dict, dict], tuple[EgressBasis, dict] | None]


async def recover_codex_stop(attempt: dict, result: dict, *,
                             record_namespace_stop: Callable[[dict], Awaitable[None]] | None = None,
                             record_egress_stop: Callable[[dict], Awaitable[None]] | None = None,
                             egress_owner_absent: EgressOwnerBasis | None = None) -> str:
    """保存証拠または同じ所有範囲の停止を照合する。欠落・改ざんは unconfirmed。

    scope だけが未確認の終端では、保存済み private init と provider を pidfd で停止し、
    観測を永続化してから確定できる。親 PID や cgroup の不在だけでは確定しない。

    egress だけが未確認の終端では、egress proxy を持っていた Kotoyose process が居ないことを
    ``egress_owner_absent`` が肯定したときだけ、その観測を永続化してから確定する。
    肯定できなければ process に触れずに unconfirmed を返す (process も cgroup も
    既に無いので、再停止では何も証明できない)。
    """
    try:
        ownership = dict(result)
        raw = ownership.pop("codex_stop_evidence", None)
        recovered = ownership.pop("codex_namespace_stop_evidence", None)
        egress = ownership.pop("codex_egress_stop_evidence", None)
        context = (stop_evidence_context(attempt)
                   if raw is not None or recovered is not None or egress is not None else None)
        if raw is not None:
            proof = validate_stop_evidence(raw, context, ownership)
            if proof.confirmed():
                return "confirmed"
        if recovered is not None:
            validate_namespace_stop_evidence(recovered, context, ownership, raw)
            return "confirmed"
        if egress is not None:
            validate_egress_stop_evidence(egress, context, ownership, raw)
            return "confirmed"
        if raw is not None and attempt.get("finished_at") is not None:
            try:
                egress_recovery_source(context, ownership, raw)
                egress_only = True
            except ValueError:
                egress_only = False
            if egress_only:
                observed = egress_owner_absent(attempt, ownership) if egress_owner_absent is not None else None
                if observed is None or record_egress_stop is None:
                    return "unconfirmed"
                basis, recovered_by = observed
                evidence = make_egress_stop_evidence(context, ownership, raw, basis=basis, recovered_by=recovered_by)
                try:
                    await record_egress_stop(evidence.model_dump())
                except Exception:
                    # 永続化できない観測は確定に使わない。次の照合で同じ判断をやり直す。
                    return "unconfirmed"
                return "confirmed"
        recoverable = False
        if raw is not None and attempt.get("finished_at") is not None and record_namespace_stop is not None:
            try:
                namespace_recovery_source(context, ownership, raw)
                recoverable = True
            except ValueError:
                pass
        scope = ownership.get("codex_provider_scope")
        init = ProcessIdentity(**scope["namespace_init"]) if recoverable else None
        root = {key: value for key, value in ownership.items() if key != "codex_provider_scope"}
        outcomes = await asyncio.gather(
            stop_codex_process(ProcessIdentity(**root), 0, namespace_init=init),
            stop_recovered_provider_scope(attempt["id"], attempt["generation"], scope))
        if outcomes == ["confirmed", "confirmed"]:
            return "confirmed"
        if recoverable and outcomes[0] == "confirmed":
            evidence = make_namespace_stop_evidence(context, ownership, raw)
            await record_namespace_stop(evidence.model_dump())
            return "confirmed"
        return "unconfirmed"
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        return "unconfirmed"

"""Benchmark manifest の保持要求を既存 Kotoyose retention API へ接続する。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


async def request_retention(client: Any, cases: Sequence[Any]) -> list[dict[str, Any]]:
    """利用可能な CAS API は適用し、API がない参照も要求 record を失わない。"""
    retain = getattr(client, "retain", None)
    records: list[dict[str, Any]] = []
    for case in cases:
        for reference in case.retention_references:
            base = {"case_id": case.case_id, "kind": reference.kind, "ref": reference.ref}
            request = getattr(reference, "request", None)
            if request is not None:
                base["request_id"] = request.request_id
            if reference.kind == "fixture":
                records.append({
                    **base, "status": "record_only", "receipt": None,
                    "reason": "Kotoyose has no mutable fixture retention API; caller must retain fixture storage.",
                })
                continue
            if not callable(retain):
                records.append({
                    **base, "status": "record_only", "receipt": None,
                    "reason": "NativeExecutionClient does not expose the Kotoyose retention API.",
                })
                continue
            try:
                receipt = await retain(reference)
                if not isinstance(receipt, Mapping):
                    raise TypeError("retention receipt is not an object")
                records.append({
                    **base, "status": "applied", "receipt": dict(receipt), "reason": None,
                })
            except Exception as exc:
                records.append({
                    **base, "status": "failed", "receipt": None, "reason": type(exc).__name__,
                })
    return records

"""Offline JSON output validation; provider schema hints alone are not a guarantee."""
from __future__ import annotations

import json
from dataclasses import replace

from jsonschema import Draft202012Validator
from referencing import Registry

from .runtime.base import RuntimeEvent, RuntimeResult, RuntimeTurnError


def validate_output_schema(schema: dict) -> None:
    if schema.get("type") != "object":
        raise ValueError("output_schema must declare an object root")
    if len(json.dumps(schema, allow_nan=False)) > 65536:
        raise ValueError("output_schema exceeds 64 KiB")
    def inspect(value, depth=0):
        if depth > 32:
            raise ValueError("output_schema exceeds maximum depth")
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$id" or (key in {"$ref", "$dynamicRef", "$recursiveRef"} and (not isinstance(child, str) or not child.startswith("#"))):
                    raise ValueError("output_schema permits local fragment references only")
                inspect(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                inspect(child, depth + 1)
    inspect(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError("output_schema is not a valid JSON Schema") from exc


def validate_structured_result(result: RuntimeResult, schema: dict) -> RuntimeResult:
    try:
        value = result.structured_output
        if value is None:
            value = json.loads(result.text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        Draft202012Validator(schema, registry=Registry()).validate(value)
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except Exception as exc:
        raise RuntimeTurnError("Provider output failed the configured JSON schema", partial_result=result,
                               diagnostics={"terminal_reason": "output_contract_error"}) from exc
    return replace(result, text=text, structured_output=value)


class StructuredOutputSession:
    """Buffer opt-in JSON content until local validation, while forwarding progress."""
    def __init__(self, session, schema):
        self.session = session
        self.schema = schema
        self.provider = session.provider

    @property
    def invalidated(self):
        return getattr(self.session, "invalidated", False)

    async def disconnect(self):
        await self.session.disconnect()

    async def ask(self, prompt):
        return validate_structured_result(await self.session.ask(prompt), self.schema)

    async def stream(self, prompt):
        iterator = self.session.stream(prompt)
        result = None
        try:
            async for item in iterator:
                if isinstance(item, RuntimeEvent):
                    yield item
                elif isinstance(item, RuntimeResult):
                    result = item
            if result is None:
                raise RuntimeTurnError("Provider stream ended without a result", partial_result=RuntimeResult("", provider=self.provider),
                                       diagnostics={"terminal_reason": "missing_result"})
            result = validate_structured_result(result, self.schema)
            yield result.text
            yield result
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

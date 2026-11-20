"""Validate local JSON schemas and successful output without remote retrieval."""
import json

from jsonschema import Draft202012Validator, exceptions
from referencing import Registry

from .agent_execution_contract import AgentExecutionError


def validate_result_schema(schema: dict) -> None:
    """ローカル参照だけの 2020-12 schema を検証し、不正なら 422 を送出する。"""
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == '$schema' and item != 'https://json-schema.org/draft/2020-12/schema':
                    raise ValueError('Unsupported JSON Schema dialect')
                if key in ('$ref', '$dynamicRef') and (not isinstance(item, str) or not item.startswith('#')):
                    raise ValueError('Only local JSON Schema references are supported')
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    try:
        visit(schema)
        Draft202012Validator.check_schema(schema)
    except (ValueError, exceptions.SchemaError, RecursionError) as exc:
        raise AgentExecutionError('invalid_request', 'Invalid result schema', 422) from exc


def check_output(result_contract: dict, content: bytes) -> str | None:
    """JSON 出力を検証し、不一致なら故障 reason を返す。"""
    if result_contract['format'] != 'json':
        return None
    try:
        value = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        schema = result_contract['json_schema']
        if schema is not None:
            Draft202012Validator(schema, registry=Registry()).validate(value)
    except (ValueError, UnicodeDecodeError, exceptions.ValidationError, exceptions._WrappedReferencingError, RecursionError):
        return 'output_schema_mismatch'
    return None

"""管理者の MCP 上限と Caller が固定する tool surface。credential 値を含めない。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Literal
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, SchemaError
from pydantic import Field, model_validator

from .execution_contract import ContractModel, Ref


def mcp_content_digest(value: object) -> str:
    """同じ MCP metadata を順序に依存せず pin する。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_mcp_schema(schema: dict) -> None:
    """schema の外部取得と過大な定義を拒み、ローカル $defs 参照だけを許可する。"""
    if schema.get('type') != 'object' or len(json.dumps(schema).encode()) > 64 * 1024:
        raise ValueError('MCP input schema must be a bounded object')
    visited_nodes = 0
    def visit(value, ancestors=(), depth=0):
        nonlocal visited_nodes
        visited_nodes += 1
        if depth > 32 or visited_nodes > 2048:
            raise ValueError('MCP schema expansion exceeds complexity limit')
        if isinstance(value, dict):
            if id(value) in ancestors:
                raise ValueError('Recursive MCP schemas are unsupported')
            ancestors = (*ancestors, id(value))
            for key, item in value.items():
                if key in {'$id', '$dynamicRef', 'pattern', 'patternProperties'}:
                    raise ValueError('MCP schema uses an unsupported resolution or regular expression keyword')
                if key == '$ref':
                    if not isinstance(item, str) or not item.startswith('#/'):
                        raise ValueError('MCP schema must not resolve external references')
                    target = schema
                    try:
                        for part in unquote(item[2:]).split('/'):
                            part = part.replace('~1', '/').replace('~0', '~')
                            target = target[int(part)] if isinstance(target, list) else target[part]
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        raise ValueError('MCP schema contains an unresolved local reference') from exc
                    visit(target, ancestors, depth + 1)
                elif key in {'properties', '$defs', 'definitions', 'dependentSchemas'} and isinstance(item, dict):
                    for child in item.values():
                        visit(child, ancestors, depth + 1)
                elif key in {'allOf', 'anyOf', 'oneOf', 'prefixItems'} and isinstance(item, list):
                    for child in item:
                        visit(child, ancestors, depth + 1)
                elif key in {'additionalProperties', 'items', 'contains', 'not', 'if', 'then', 'else', 'propertyNames', 'unevaluatedItems', 'unevaluatedProperties', 'contentSchema'}:
                    visit(item, ancestors, depth + 1)
    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError('MCP input schema is invalid') from exc


_NARROWING_KEYWORDS = frozenset({
    'type', 'const', 'enum', 'required', 'properties', 'additionalProperties',
})


def _validate_narrowing_shape(schema: dict, path: str = '$') -> None:
    """一般の schema 包含判定を避け、単調性を証明できる語彙だけに限定する。"""
    unsupported = set(schema) - _NARROWING_KEYWORDS
    if unsupported:
        raise ValueError(f'MCP narrowing uses unsupported keywords at {path}')
    schema_type = schema.get('type')
    if schema_type is not None and (
        not isinstance(schema_type, str)
        or schema_type not in {'object', 'array', 'string', 'number', 'integer', 'boolean', 'null'}
    ):
        raise ValueError(f'MCP narrowing uses an unsupported type at {path}')
    if 'const' in schema and 'enum' in schema:
        raise ValueError(f'MCP narrowing must not combine const and enum at {path}')
    required = schema.get('required', [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError(f'MCP narrowing required must contain strings at {path}')
    if len(set(required)) != len(required):
        raise ValueError(f'MCP narrowing required must be unique at {path}')
    properties = schema.get('properties', {})
    if not isinstance(properties, dict) or any(not isinstance(item, dict) for item in properties.values()):
        raise ValueError(f'MCP narrowing properties must contain schemas at {path}')
    if 'additionalProperties' in schema and not isinstance(schema['additionalProperties'], bool):
        raise ValueError(f'MCP narrowing additionalProperties must be boolean at {path}')
    for name, child in properties.items():
        _validate_narrowing_shape(child, f'{path}.properties.{name}')


def _json_identity(value: object) -> str:
    """Python の bool/int 同値化を使わず JSON 値として enum 包含を比べる。"""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def validate_narrowing_constraints(ceiling: dict, narrowing: dict) -> None:
    """Caller 制約が管理者制約と同等以下だと証明できない場合は拒否する。"""
    validate_mcp_schema(narrowing)
    _validate_narrowing_shape(ceiling, '$ceiling')
    _validate_narrowing_shape(narrowing, '$narrowing')

    def compare(upper: dict, lower: dict, path: str) -> None:
        upper_type = upper.get('type')
        lower_type = lower.get('type')
        if upper_type is not None:
            type_is_narrower = lower_type == upper_type or (upper_type == 'number' and lower_type == 'integer')
            if not type_is_narrower:
                raise ValueError(f'MCP narrowing removes or changes type at {path}')

        if 'const' in upper:
            if 'const' not in lower or _json_identity(lower['const']) != _json_identity(upper['const']):
                raise ValueError(f'MCP narrowing removes or changes const at {path}')
        elif 'enum' in upper:
            allowed = {_json_identity(item) for item in upper['enum']}
            if 'const' in lower:
                selected = {_json_identity(lower['const'])}
            elif 'enum' in lower:
                selected = {_json_identity(item) for item in lower['enum']}
            else:
                raise ValueError(f'MCP narrowing removes enum at {path}')
            if not selected.issubset(allowed):
                raise ValueError(f'MCP narrowing expands enum at {path}')

        upper_required = set(upper.get('required', []))
        lower_required = set(lower.get('required', []))
        if not upper_required.issubset(lower_required):
            raise ValueError(f'MCP narrowing removes required properties at {path}')

        upper_properties = upper.get('properties', {})
        lower_properties = lower.get('properties', {})
        for name, upper_child in upper_properties.items():
            lower_child = lower_properties.get(name)
            if lower_child is None:
                raise ValueError(f'MCP narrowing removes property constraints at {path}.{name}')
            compare(upper_child, lower_child, f'{path}.{name}')
        added = set(lower_properties) - set(upper_properties)
        if added and upper.get('additionalProperties') is False:
            raise ValueError(f'MCP narrowing adds properties outside a closed ceiling at {path}')
        if upper.get('additionalProperties') is False and lower.get('additionalProperties') is not False:
            raise ValueError(f'MCP narrowing reopens additional properties at {path}')

    compare(ceiling, narrowing, '$')


class McpToolCeiling(ContractModel):
    """Tool annotation ではなく、管理者が定める効果と引数・通信の上限。"""

    effect: Literal['read', 'write']
    argument_constraints: dict = Field(default_factory=lambda: {'type': 'object'})
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_argument_bytes: int = Field(default=65536, ge=1, le=1024 * 1024)
    max_result_bytes: int = Field(default=256 * 1024, ge=1, le=1024 * 1024)

    @model_validator(mode='after')
    def valid_constraints(self) -> McpToolCeiling:
        """渡せる対象を JSON Schema で絞り、Caller や Agent に上限を変更させない。"""
        validate_mcp_schema(self.argument_constraints)
        return self


class ExternalMcpServerConfig(ContractModel):
    """接続先と credential source は管理者設定のみ。MCP 内容の provenance とは分離する。"""

    endpoint: str = Field(min_length=1, max_length=2048)
    server_name: Ref
    server_version: Ref
    trust_profile_ref: Ref
    credential_env: str | None = Field(default=None, pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    tools: dict[str, McpToolCeiling] = Field(min_length=1, max_length=64)
    allow_insecure_loopback: bool = False
    connect_timeout_seconds: int = Field(default=10, ge=1, le=60)
    max_metadata_bytes: int = Field(default=1024 * 1024, ge=1024, le=4 * 1024 * 1024)

    @model_validator(mode='after')
    def fixed_endpoint(self) -> ExternalMcpServerConfig:
        """redirect、userinfo、query token、任意 HTTP 接続を許可しない。"""
        url = urlsplit(self.endpoint)
        if url.port is not None and url.port < 1:
            raise ValueError('MCP endpoint port is invalid')
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('MCP endpoint must not contain credentials, query or fragment')
        if url.scheme != 'https':
            try:
                loopback = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                loopback = False
            if url.scheme != 'http' or not self.allow_insecure_loopback or not loopback:
                raise ValueError('MCP requires HTTPS except explicit loopback fixtures')
        if any(not name or len(name) > 128 for name in self.tools):
            raise ValueError('MCP tool names must be bounded')
        return self

    def config_digest(self) -> str:
        """credential の値の rotation は接続先・上限の変更と混同しない。"""
        return mcp_content_digest(self.model_dump(mode='json'))


class PinnedMcpTool(ContractModel):
    """provider alias と remote tool の対応、本文・schema・上限を一緒に固定する。"""

    name: str = Field(min_length=1, max_length=128)
    alias: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    description: str = Field(max_length=8192)
    input_schema: dict
    tool_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    ceiling: McpToolCeiling
    narrowing_constraints: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    narrowing_digest: str | None = Field(
        default=None, pattern=r'^[0-9a-f]{64}$', exclude_if=lambda value: value is None,
    )

    @model_validator(mode='after')
    def pinned_schema(self) -> PinnedMcpTool:
        """Caller が登録した schema 自体も admission 前に検査する。"""
        validate_mcp_schema(self.input_schema)
        if (self.narrowing_constraints is None) != (self.narrowing_digest is None):
            raise ValueError('MCP narrowing schema and digest must be supplied together')
        if self.narrowing_constraints is not None:
            validate_narrowing_constraints(self.ceiling.argument_constraints, self.narrowing_constraints)
            if mcp_content_digest(self.narrowing_constraints) != self.narrowing_digest:
                raise ValueError('MCP narrowing digest does not match schema')
        return self


class McpBindingResource(ContractModel):
    """取得時点の MCP 設定・server 宣言版・Tool metadata を保持する immutable resource。"""

    schema_version: Literal['1']
    kind: Literal['mcp_binding']
    server_id: Ref
    config_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    server_name: Ref
    server_version: Ref
    protocol_version: Ref
    trust_profile_ref: Ref
    provenance: Literal['external_untrusted'] = 'external_untrusted'
    tools: list[PinnedMcpTool] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def distinct_tools(self) -> McpBindingResource:
        """同名 Tool や provider alias の上書きを防ぐ。"""
        if len({tool.name for tool in self.tools}) != len(self.tools) or len({tool.alias for tool in self.tools}) != len(self.tools):
            raise ValueError('Pinned MCP names and aliases must be distinct')
        return self


class McpPinRequest(ContractModel):
    """Caller は登録済み接続先の Tool と公開 alias だけを選択する。"""

    schema_version: Literal['1']
    server_id: Ref
    tools: dict[str, str] = Field(min_length=1, max_length=32)
    narrowing_constraints: dict[str, dict] = Field(default_factory=dict, max_length=32)

    @model_validator(mode='after')
    def narrowing_targets_selected_tools(self) -> McpPinRequest:
        """別 Tool の制約を binding に紛れ込ませず、対象名を tools と一致させる。"""
        if set(self.narrowing_constraints) - set(self.tools):
            raise ValueError('MCP narrowing targets an unselected tool')
        for schema in self.narrowing_constraints.values():
            validate_mcp_schema(schema)
            _validate_narrowing_shape(schema)
        return self


def validate_pinned_mcp_arguments(tool: PinnedMcpTool, arguments: dict) -> None:
    """送信対象を remote、管理者、Caller の独立した 3 制約すべてで検査する。"""
    Draft202012Validator(tool.input_schema).validate(arguments)
    Draft202012Validator(tool.ceiling.argument_constraints).validate(arguments)
    if tool.narrowing_constraints is not None:
        Draft202012Validator(tool.narrowing_constraints).validate(arguments)


class McpReconciliationRequest(ContractModel):
    """再送を伴わない、特定 Operation に対する Caller の照合証拠。"""

    schema_version: Literal['1']
    tool_ref: Ref
    arguments_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    generation: int = Field(ge=0)
    disposition: Literal['applied', 'not_applied']
    result: dict
    evidence_ref: str = Field(min_length=1, max_length=2048)

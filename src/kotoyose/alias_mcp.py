"""MCP references shared by configuration and child turn admission."""

from __future__ import annotations

from .delegation import MCP_NAME


def mcp_reference(ref: str) -> tuple[str, str]:
    scope, marker, name = ref.partition(":")
    return (scope, name) if marker else ("", scope)


def validate_mcp_references(refs: list[str]) -> list[str]:
    seen = set()
    for ref in refs:
        scope, name = mcp_reference(ref)
        if scope not in ("", "host", "local") or (":" in ref and not scope) or not valid_public_name(name):
            raise ValueError("mcp_refs: expected a name, host:<name>, or local:<name>")
        if scope == "local" and name == MCP_NAME:
            raise ValueError("mcp_refs: kotoyose is reserved")
        if name in seen:
            raise ValueError(f"mcp_refs: duplicate public MCP name '{name}' (参照を重複指定できません)")
        seen.add(name)
    return refs


def valid_public_name(name: object) -> bool:
    return isinstance(name, str) and bool(name and name == name.strip() and ":" not in name and name.isprintable())


def is_host_mcp_reference(ref: str, *, on_child: bool) -> bool:
    return not on_child or mcp_reference(ref)[0] == "host"


def require_child_for_scoped_reference(ref: str, *, on_child: bool) -> None:
    """`host:` and `local:` choose a catalog. Only a child alias has two catalogs."""
    scope, _name = mcp_reference(ref)
    if not on_child and scope in {"host", "local"}:
        raise ValueError(
            f"mcp_refs: '{ref}' is only valid on a child alias; use the catalog name without a prefix"
        )

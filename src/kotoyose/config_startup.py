"""Project a startup config onto the provider declarations available on this host."""

from collections.abc import Callable

from .print_cli_profiles import list_print_cli_profiles
from .provider_data import reserved_declaration_names
from .provider_data_host import Catalog


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _reference(value: object, removed: set[str], *, strip: bool = False) -> str | None:
    if isinstance(value, str):
        name = value.strip() if strip else value
        if name in removed:
            return name
    return None


def _list_reference(value: object, removed: set[str]) -> str | None:
    if isinstance(value, list):
        return next((name for item in value if (name := _reference(item, removed)) is not None), None)
    return None


def omit_unavailable_data_profiles(data: dict, catalog: Catalog) -> tuple[list[str], set[str]]:
    """Remove dependencies of unavailable data profiles from the in-memory YAML mapping."""
    # Without a stored identity, an unknown name could be a typo for a built-in provider or profile.
    known_ids = {row.id for row in (*catalog.rows, *catalog.invalid)}
    known_profiles = {row.profile_id for row in (*catalog.rows, *catalog.invalid)}

    available = {profile.id for profile in list_print_cli_profiles()}
    available_providers = {row.id for row in catalog.rows}
    reserved = reserved_declaration_names()
    warnings: list[str] = []

    def unavailable(profile: object, provider: object) -> str | None:
        # A built-in provider's unknown profile remains a validation error.
        if isinstance(provider, str) and provider.lower() in reserved:
            return None
        known_provider = isinstance(provider, str) and provider in known_ids
        if profile is None and known_provider and provider not in available_providers:
            return f"data provider {provider!r} has no available profile"
        if (
            isinstance(profile, str) and profile not in available and profile not in reserved
            and (profile in known_profiles or profile in known_ids or known_provider)
        ):
            return f"data profile {profile!r} is unavailable"
        return None

    def omit(section: str, reason_for: Callable[[dict], str | None]) -> set[str]:
        current = _mapping(data.get(section))
        removed = {}
        for name, entry in current.items():
            if isinstance(entry, dict) and (reason := reason_for(entry)):
                removed[name] = reason
        if removed:
            data[section] = {name: entry for name, entry in current.items() if name not in removed}
            warnings.extend(f"Skipped {section}.{name!r} at startup: {reason}." for name, reason in removed.items())
        return set(removed)

    connections = _mapping(data.get("connections"))
    skipped_connections = omit("connections", lambda entry: unavailable(entry.get("profile"), entry.get("provider")))

    def quota_reason(entry: dict) -> str | None:
        target = _reference(entry.get("quota_from"), skipped_connections, strip=True)
        return None if target is None else f"quota connection {target!r} was skipped"

    while skipped := omit("connections", quota_reason):
        skipped_connections.update(skipped)

    def route_reason(entry: dict) -> str | None:
        connection = entry.get("connection")
        if entry.get("driver") in ("cli.acp", "cli.print") and isinstance(connection, str):
            profile = entry.get("profile")
            provider = _mapping(connections.get(connection)).get("provider")
            if isinstance(profile, str) and profile not in available:
                return unavailable(profile, provider)
        if (name := _reference(connection, skipped_connections)) is not None:
            return f"connection {name!r} was skipped"
        return None

    skipped_routes = omit("routes", route_reason)
    skipped_models: set[str] = set()

    def model_reason(entry: dict) -> str | None:
        if (name := _reference(entry.get("route"), skipped_routes)) is not None:
            return f"route {name!r} was skipped"
        if (name := _list_reference(entry.get("delegates"), skipped_models)) is not None:
            return f"delegate {name!r} was skipped"
        if (name := _list_reference(entry.get("candidates"), skipped_models)) is not None:
            return f"pool candidate {name!r} was skipped"
        return None

    while skipped := omit("models", model_reason) | omit("pools", model_reason):
        skipped_models.update(skipped)

    for section, field, skipped_names in (
        ("risk_acks", "route", skipped_routes),
        ("training_acks", "alias", skipped_models),
    ):
        if isinstance(entries := data.get(section), list) and skipped_names:
            kept = []
            for entry in entries:
                if (name := _reference(_mapping(entry).get(field), skipped_names)) is None:
                    kept.append(entry)
                else:
                    warnings.append(f"Skipped {section} entry for {name!r} at startup.")
            data[section] = kept

    clients = _mapping(data.get("clients"))
    if skipped_models and clients:
        data["clients"] = dict(clients)
        omitted = skipped_models - {"*"}
        for name, entry in clients.items():
            models = _mapping(entry).get("models")
            if isinstance(models, list):
                removed = {alias for item in models if (alias := _reference(item, omitted, strip=True)) is not None}
                if removed:
                    data["clients"][name] = {
                        **entry, "models": [item for item in models if _reference(item, removed, strip=True) is None],
                    }
                    warnings.extend(f"Skipped clients.{name!r}.models entry {alias!r} at startup."
                                    for alias in sorted(removed))

    return warnings, skipped_models

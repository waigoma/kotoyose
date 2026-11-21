"""route と agent が付いた alias の解決。

model は route が持ち、機械の binary と認証ファイルは drivers が持つ。
agent の無い alias は chat 専用で、ここでは解かない。
"""

from __future__ import annotations


from .agent_spec import (
    driver_manifest, external_execution_environment, native_bypass_error,
    resolved_sdk_sandbox, stores_sdk_sandbox,
)
from .print_cli_profiles import get_print_cli_profile, resolve_print_cli_binary, validate_print_cli_connection


def _bind_surface(resolved, surface: str):
    """snapshot に出さない。accepted_runs が上限として読む。"""
    resolved._agent_surface = surface
    return resolved


def _install_for(settings, route):
    """この機械の drivers だけを読む。宣言が無い driver では agent を動かさない。"""
    from .execution_resources import DriverInstall

    drivers = getattr(settings, "drivers", None)
    if drivers is None:
        raise ValueError("agent execution requires drivers")
    # The jail materials and binary overrides are shared by both CLI transports.
    found = drivers.get("cli.print" if route.driver == "cli.acp" else route.driver)
    if found is None:
        raise ValueError(f"driver {route.driver} is not installed on this machine")
    return found if isinstance(found, DriverInstall) else DriverInstall.model_validate(found)


def _claude_credential(route, conn) -> tuple[str, str | None, str]:
    """env:/secret:/endpoint は preset 側で解決する。subscription (runtime_managed) は機械の既定名を使う。"""
    if conn is None:
        raise ValueError(f"unknown connection: {route.connection}")
    if conn.provider != "claude":
        raise ValueError("Claude alias cannot use a non-Claude connection")
    if conn.config_dir is not None:
        raise ValueError(
            f"Claude config_dir connection '{route.connection}' supports host placement only; "
            "isolated/jail routes are not supported"
        )
    return "runtime_managed", None, "CLAUDE_CODE_OAUTH_TOKEN"


def _codex_credential(route, conn) -> tuple[str, str | None, str | None, str, object]:
    if conn is None:
        raise ValueError(f"unknown connection: {route.connection}")
    if conn.provider != "codex":
        raise ValueError("Codex route connection is not for codex")
    if conn.kind == "subscription":
        if conn.auth_file is None:
            raise ValueError(f"Codex subscription connection '{route.connection}' requires auth_file")
        return "chatgpt_subscription", None, None, "CODEX_CHATGPT_AUTH", conn.auth_file
    if conn.env_name:
        return "environment", conn.env_name, None, "OPENAI_API_KEY", None
    if conn.secret_name:
        return "secret", None, conn.secret_name, "OPENAI_API_KEY", None
    raise ValueError("Codex route connection is not a subscription or secret/env reference")


def _resolve_claude(alias, preset, settings, config, route, install):
    from .execution_resources import AliasRuntimeProfile, resolve_runtime_profile

    cli = install.cli_binary
    if cli is None:
        raise ValueError("claude cli_binary is not configured")
    conn = config.connections.get(route.connection)
    mode, env_name, name = _claude_credential(route, conn)
    profile = AliasRuntimeProfile(
        alias=alias, cli_binary=cli, credential_mode=mode, credential_env=env_name, credential_name=name,
        compression_mode=getattr(settings, "agent_compression_mode", "runtime_managed"),
    )
    # preset_for が route の model と endpoint を既に重ねてある。
    return resolve_runtime_profile(profile, config=config)


def _resolve_codex(alias, preset, settings, config, route, install, *, resolve_preferences: bool, resolve_prompts: bool):
    from .codex_preferences import resolve_codex_preferences
    from .execution_resources import NativeRuntimeProfile, ResolvedCodexRuntimeProfile
    from .preset_prompts import resolve_preset_prompts

    manifest = driver_manifest(route.driver)
    cell = next((item for item in manifest.surfaces if item.surface == preset.agent.surface), None)
    if cell is None or not cell.admitted or cell.execution_mode is None:
        raise ValueError(f"surface {preset.agent.surface!r} is not admitted for {route.driver}")
    message = native_bypass_error("codex", preset.codex.get("dangerously_bypass_approvals_and_sandbox"))
    if message:
        raise ValueError(message)
    conn = config.connections.get(route.connection)
    cred_mode, cred_env, cred_secret, cred_name, auth_file = _codex_credential(route, conn)
    cli = install.cli_binary
    if cli is None:
        raise ValueError("codex cli_binary is not configured")
    fields = {
        "provider": "codex",
        "model": route.model,
        "cli_binary": cli,
        "codex_execution_mode": cell.execution_mode,
        "credential_mode": cred_mode,
        "credential_env": cred_env,
        "credential_secret": cred_secret,
        "credential_name": cred_name,
        "codex_auth_file": auth_file,
        "compression_mode": getattr(settings, "agent_compression_mode", "runtime_managed"),
    }
    # 種類は agent_spec の cell だけが決める。別 cell の rootfs や egress は借りない。
    # cells が空なのは、profile が rootfs を持たなかった機械。種類を書き始めた機械で
    # 必要な種類が無いときだけ、ここで落とす。
    kind = cell.rootfs_digest
    if kind is not None and install.cells:
        materials = install.cells.get(kind)
        if materials is None:
            raise ValueError(f"driver {route.driver} has no {kind} cell")
        if materials.rootfs is None or materials.rootfs_digest is None:
            raise ValueError(f"driver {route.driver} {kind} cell requires rootfs and rootfs_digest")
        fields["codex_rootfs"] = materials.rootfs
        fields["codex_rootfs_digest"] = materials.rootfs_digest
        if materials.codex_egress_limits is not None:
            fields["codex_egress_limits"] = materials.codex_egress_limits
    profile = NativeRuntimeProfile(**fields)
    # 境界は alias の execution_environment ではなく、cell の external_sandbox と place。
    environment = external_execution_environment(cell, preset.agent.place)
    if not resolve_preferences:
        if environment == "isolated":
            return ResolvedCodexRuntimeProfile(**profile.model_dump(), execution_environment="isolated")
        return profile
    sandbox = resolved_sdk_sandbox(cell, preset.agent.place, preset.codex.get("sandbox"))
    preferences = resolve_codex_preferences(
        route.model, preset.effort, preset.codex.get("timeout", preset.timeout_seconds),
    )
    prompts = resolve_preset_prompts(config, preset) if resolve_prompts else None
    return ResolvedCodexRuntimeProfile(
        **profile.model_dump(), codex_preferences=preferences, execution_environment=environment,
        codex_sandbox=(sandbox if stores_sdk_sandbox("codex", cell.execution_mode) else None),
        preset_prompt_snapshot=prompts,
    )


def _resolve_print_cli(
    preset,
    settings,
    config,
    route,
    *,
    execution_environment: str = "host",
    image_id: str | None = None,
):
    from .execution_resources import PrintCliRuntimeProfile

    if not route.profile:
        raise ValueError(f"Route '{preset.route}' requires profile for {route.driver}")
    cli_profile = get_print_cli_profile(route.profile)
    conn = config.connections.get(route.connection)
    if conn is None:
        raise ValueError(f"Unknown connection '{route.connection}' on route '{preset.route}'")
    validate_print_cli_connection(cli_profile, conn, placement=execution_environment)

    cli_binary = resolve_print_cli_binary(cli_profile, settings)
    if cli_binary is None:
        raise ValueError(f"print CLI binary '{cli_profile.binary}' is unavailable; set drivers.cli.print.profile_binaries.{cli_profile.id}")

    return PrintCliRuntimeProfile(
        profile=cli_profile.id,
        acp=route.driver == "cli.acp",
        acp_tools=(tuple(preset.tools) if preset.tools is not None else None) if route.driver == "cli.acp" else None,
        acp_deny=tuple(preset.deny) if route.driver == "cli.acp" else (),
        model=route.model,
        cli_binary=cli_binary,
        connection_id=route.connection,
        login_home=conn.config_dir,
        api_key_secret=conn.secret_name if conn.kind == "api_key" else None,
        execution_environment=execution_environment,
        image_id=image_id,
    )


def resolve_declared_agent(alias: str, preset, settings, config, *, resolve_preferences: bool, resolve_prompts: bool):
    """route と agent が揃った alias を、今日の解決器と同じ profile にする。"""
    if preset.agent is None or preset.route is None:
        raise ValueError("alias agent binding requires both route and agent")
    if getattr(settings, "drivers", None) is None:
        raise ValueError("agent execution requires drivers")
    route = config.routes.get(preset.route)
    if route is None:
        raise ValueError(f"Unknown route: {preset.route}")
    manifest = driver_manifest(route.driver)
    if preset.agent.place not in manifest.places:
        raise ValueError(f"agent.place={preset.agent.place!r} is not supported for {route.driver}")
    if preset.agent.place == "host":
        if manifest.unmapped_backend == "print_cli":
            return _bind_surface(
                _resolve_print_cli(preset, settings, config, route),
                preset.agent.surface,
            )
        raise ValueError(f"Host agent execution is not supported for {route.driver}")
    install = _install_for(settings, route)
    cell = next((item for item in manifest.surfaces if item.surface == preset.agent.surface), None)
    if cell is None or not cell.admitted:
        raise ValueError(f"surface {preset.agent.surface!r} is not admitted for {route.driver}")
    if manifest.provider == "claude":
        resolved = _resolve_claude(alias, preset, settings, config, route, install)
    elif manifest.provider == "codex":
        resolved = _resolve_codex(
            alias, preset, settings, config, route, install,
            resolve_preferences=resolve_preferences, resolve_prompts=resolve_prompts,
        )
    elif manifest.unmapped_backend == "print_cli":
        coding_cell = install.cells.get("coding")
        if coding_cell is None or coding_cell.image_id is None:
            raise ValueError(f"driver {route.driver} requires cells.coding.image_id for isolated execution")
        resolved = _resolve_print_cli(
            preset,
            settings,
            config,
            route,
            execution_environment="isolated",
            image_id=coding_cell.image_id,
        )
    else:
        raise ValueError(f"{route.driver} cannot run isolated")
    return _bind_surface(resolved, preset.agent.surface)


def published_agent(alias: str, policy, settings, *, config) -> dict | None:
    """/v1/models の agent。Caller の agent_models と places と max_surface を通った alias だけ出す。"""
    from .execution_resources import permitted_agent_execution

    if config is None or permitted_agent_execution(alias, policy, settings, config=config) is None:
        return None
    preset = config.preset_for(alias, resolve_execution_environment=False)
    if preset.route is None or preset.agent is None:
        return None
    driver = config.routes[preset.route].driver
    manifest = driver_manifest(driver)
    provider = "print_cli" if manifest.unmapped_backend == "print_cli" else manifest.provider
    place, surface = preset.agent.place, preset.agent.surface
    return {
        "place": place,
        "surface": surface,
        "provider": provider,
        "driver": driver,
        "enforced": place == "isolated",
    }


def route_name_for_alias(alias: str, config) -> str:
    """snapshot の runtime_profile が profile 名だった場所に書く route 名。"""
    preset = config.preset_for(alias, resolve_execution_environment=False)
    if preset.route is None:
        raise ValueError("alias agent binding requires route")
    return preset.route

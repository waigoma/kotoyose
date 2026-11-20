"""Admission-time billing constraints over the actual resolved runtime plan.

Credential names and model labels are not evidence of subscription billing.
Only a worker route whose provider and fallback behavior are established can
satisfy ``subscription``. Unknown routes fail before an Attempt is admitted.
"""

from .execution_resources import ExecutionError, codex_authentication_binding, content_digest


def require_runtime_selection(runtime: dict, *, provider: str | None, billing_mode: str | None) -> dict | None:
    """Check the same runtime dict that admission will persist, without resolving again."""
    if provider is not None and runtime.get("provider") != provider:
        raise ExecutionError("permission_denied", "Resolved runtime provider does not meet request", 403,
                             reason_code="runtime_provider_mismatch")
    if billing_mode is None:
        return None
    if billing_mode != "subscription":
        raise ExecutionError("invalid_request", "Unsupported billing requirement", 422)
    if runtime.get("backend") == "native" and runtime.get("provider") == "codex":
        binding = codex_authentication_binding(runtime)
        if binding["mode"] != "chatgpt_subscription":
            raise ExecutionError("permission_denied", "Runtime does not use subscription billing", 403,
                                 reason_code="billing_mode_mismatch")
        # CodexEgress pins chatgpt.com/backend-api/codex/responses, authorizes
        # each request and stops on authentication/quota failure. It never
        # falls back to its API-key branch (codex_egress.py).
        policy = {"adapter": "codex_chatgpt_v1", "model": runtime["model"],
                  "authentication": binding, "metered_fallback": "forbidden"}
        return {"mode": "subscription", "metered_fallback": "forbidden",
                "policy_digest": content_digest(policy)}
    billing = owner_claude_subscription_billing(runtime, adapter="native_claude")
    if billing is not None:
        return billing
    raise ExecutionError("unsupported_capability", "Subscription-only billing is not established for this runtime", 501,
                         reason_code="billing_policy_unavailable")


def owner_claude_subscription_billing(runtime: dict, *, adapter: str) -> dict | None:
    """Share owner policy, while keeping each adapter's actual auth convention.

    This is an owner assertion for an exact endpoint and credential source,
    including quota/auth failures. It is not independent upstream attestation.
    Adapter choices are internal, closed, and never accepted from request data.
    """
    bindings = {
        "native_claude": ("native", "ANTHROPIC_AUTH_TOKEN", "claude_owner_route_v1"),
        "direct_claude_sdk": ("direct", "ANTHROPIC_API_KEY", "claude_chat_owner_route_v1"),
    }
    if adapter not in bindings:
        raise ValueError("Unknown owner-policy adapter")
    backend, credential_name, policy_adapter = bindings[adapter]
    if not (runtime.get("backend") == backend and runtime.get("provider") == "claude"
            and runtime.get("subscription_only") is True):
        return None
    if (adapter == "native_claude"
            and runtime.get("base_url") is None
            and runtime.get("credential_mode") == "runtime_managed"
            and runtime.get("credential_name") == "CLAUDE_CODE_OAUTH_TOKEN"
            and runtime.get("credential_env") is None
            and runtime.get("credential_secret") is None):
        policy = {"adapter": "claude_oauth_owner_route_v1", "metered_fallback": "forbidden",
                  **{key: runtime[key] for key in ("backend", "provider", "model",
                     "credential_mode", "credential_name", "subscription_only")}}
        return {"mode": "subscription", "metered_fallback": "forbidden",
                "policy_digest": content_digest(policy)}
    if not (runtime.get("base_url")
            and runtime.get("credential_mode") == "environment"
            and runtime.get("credential_name") == credential_name and runtime.get("credential_env")):
        return None
    policy = {"adapter": policy_adapter, "metered_fallback": "forbidden",
              **{key: runtime[key] for key in ("backend", "provider", "model", "base_url",
                 "credential_mode", "credential_name", "credential_env", "subscription_only")}}
    return {"mode": "subscription", "metered_fallback": "forbidden",
            "policy_digest": content_digest(policy)}

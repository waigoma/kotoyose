"""OpenAI messages を共通 Native Core の不変入力 / Grant へ変換する。"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from uuid import uuid4

from .compatibility_config import CompatibilityConfig, CompatibilityProfile
from .compatibility_inputs import store_message_images, validate_chat_images
from .compatibility_store import CompatibilityRequestStore
from .execution_contract import AgentExecution, ExecutionInput, ExecutionRequirements
from .output_guard import CredentialLeakSuspected, LEAK_MESSAGE, LEAK_REASON, check_answer, guard_enabled
from .execution_resources import (
    AgentDefinition, CallerPolicy, ExecutionBudget, ExecutionError, ExecutionGrant,
    InputText, WorkspaceRegistration, agent_execution_mode, agent_profile_allowed, caller_allows_isolation,
    canonical_json, content_digest,
)
from .execution_service import ExecutionService
from .models import ChatCompletionRequest


class CompatibilityExecution:
    """権限・停止・artifact の処理を既存 service へ委ねる protocol adapter。"""

    def __init__(self, service: ExecutionService, config: CompatibilityConfig):
        self.service = service
        self.config = config
        self.store = CompatibilityRequestStore(service.store.pool)
        self._prepare_lock = asyncio.Lock()
        self._initialized = False

    def profile(self, principal: str, policy: CallerPolicy, model: str) -> CompatibilityProfile:
        """管理者設定と認証済み Caller の共通範囲を確認する。"""
        profile = self.config.profile(principal, model)
        if not agent_profile_allowed(model, policy, self.service.config):
            raise ExecutionError("permission_denied", f"Model '{model}' is outside this API key's client scope", 403)
        if profile.runtime_profile is not None:
            # profile 名の目録は無い。許可は alias の agent_models で見る。実行は admission が model を要求する。
            isolation = "sandbox"
        else:
            mode = agent_execution_mode(model, self.service.config)
            if mode is None:
                raise ExecutionError("permission_denied", "Model is outside Client agent models", 403)
            isolation = "sandbox" if mode == "sandbox" else "native"
        if not caller_allows_isolation(policy, isolation) or "trusted" not in policy.provenance:
            raise ExecutionError("permission_denied", "Client cannot issue the configured Agent Definition", 403)
        if (
            profile.timeout.hard_seconds > policy.max_seconds
            or profile.memory_bytes > policy.max_memory_bytes
            or profile.pids_limit > policy.max_pids
            or profile.cpus > policy.max_cpus
        ):
            raise ExecutionError("permission_denied", "Chat defaults exceed Client limits", 403)
        return profile

    def validate_request(self, request: ChatCompletionRequest, profile: CompatibilityProfile) -> None:
        """表現できない機能を入力保存や provider 接続より先に拒む。"""
        if not request.messages or not any(message.role == "user" for message in request.messages):
            raise ExecutionError("invalid_request", "Chat requires a user message", 400)
        if request.cwd is not None:
            raise ExecutionError("unsupported_capability", "Chat workspace is fixed by Client defaults", 501)
        if len(request.messages) > 1024:
            raise ExecutionError("invalid_request", "Too many chat messages", 400)
        if request.metadata and request.metadata.kotoyose:
            required = request.metadata.kotoyose.required_capabilities
            unsupported = [name for name in required if
                self.service.manifest.capabilities[name].status != "supported"
                # safe_resume は Native の resume manifest と fencing でのみ成立し、互換 chat の Session 継続では保証しない。
                or name in {"command_execution", "native", "client_tools", "safe_resume"}]
            if unsupported:
                raise ExecutionError("unsupported_capability", "Chat does not support: " + ", ".join(unsupported), 501)
        for message in request.messages:
            if message.role not in {"system", "developer", "user", "assistant"}:
                raise ExecutionError("unsupported_capability", "Unsupported chat message role", 501)

    async def dispatch(
        self, principal: str, policy: CallerPolicy, request: ChatCompletionRequest,
        *, request_key: str | None = None, conversation_key: str | None = None,
        api_key_name: str | None = None,
    ) -> tuple[str, str]:
        """同じ request key の再接続を、別 Attempt の再実行から分離する。"""
        profile = self.profile(principal, policy, request.model)
        self.validate_request(request, profile)
        images = validate_chat_images(
            request, expires_at=datetime.now(timezone.utc) + timedelta(seconds=profile.timeout.hard_seconds),
            limits=self.service.config.input_artifact_limits,
        )
        key = request_key if request_key is not None else str(uuid4())
        if not key.strip() or len(key) > 256 or any(ord(char) < 32 for char in key):
            raise ExecutionError("invalid_request", "Invalid X-Kotoyose-Request-Key", 400)
        if conversation_key is not None and (not conversation_key.strip() or len(conversation_key) > 256):
            raise ExecutionError("invalid_request", "Invalid conversation key", 400)
        digest = content_digest({
            "request": request.model_dump(mode="json", exclude={"stream"}),
            "profile": profile.model_dump(mode="json"), "conversation_key": conversation_key,
        })
        # pool の全 connection が advisory lock 待ちで埋まり、resource 保存を妨げない。
        async with self._prepare_lock:
            if not self._initialized:
                await self.store.initialize()
                self._initialized = True
            execution = await self.store.prepare(
                principal, key, digest,
                lambda: self._prepare(principal, policy, request, profile, key, conversation_key, images),
            )
        attempt_id = await self.service.dispatch(principal, policy, execution, api_key_name)
        return attempt_id, key

    async def _prepare(
        self, principal: str, policy: CallerPolicy, request: ChatCompletionRequest,
        profile: CompatibilityProfile, key: str, conversation_key: str | None, images: dict,
    ) -> AgentExecution:
        now = datetime.now(timezone.utc)
        deadline = profile.execution_deadline(now)
        isolation = "sandbox" if (profile.runtime_profile is not None
                                  or agent_execution_mode(request.model, self.service.config) == "sandbox") else "native"
        if deadline <= now:
            raise ExecutionError("permission_denied", "Chat default policy expired", 403)
        images = {position: upload.model_copy(update={"expires_at": deadline}) for position, upload in images.items()}
        workspace = await self.service.register_workspace(principal, policy, WorkspaceRegistration(
            schema_version="1", path=str(profile.workspace_path),
            caller_workspace_ref=profile.caller_workspace_ref,
        ))
        run_ref = "compatibility:" + content_digest([principal, key])

        async def register(resource):
            return await self.service.register_resource(principal, policy, resource)

        timeout_ref = await register(profile.timeout)
        definition_ref = await register(AgentDefinition(
            schema_version="1", kind="agent_definition", instructions=profile.instructions,
            output_contract="text", max_turns=profile.max_turns, timeout_policy_ref=timeout_ref,
        ))
        budget_ref = await register(ExecutionBudget(
            schema_version="1", kind="budget", reservation_ref=run_ref + ":budget", mode="observed",
            deadline=deadline, memory_bytes=profile.memory_bytes, pids_limit=profile.pids_limit,
            cpus=profile.cpus, output_limit_bytes=profile.output_limit_bytes,
        ))
        inputs = []
        for index, message in enumerate(request.messages):
            message_content, image_inputs = await store_message_images(
                self.service.store, principal, self.service.config.input_artifact_limits, index, message, images,
            )
            trusted = message.role in {"system", "developer"} and profile.accept_system_instructions
            provenance = "trusted" if trusted else (
                "agent_generated" if message.role == "assistant" else "external_untrusted"
            )
            content_ref = await register(InputText(
                schema_version="1", kind="text", provenance=provenance,
                text=message.text_content() if trusted else canonical_json({
                    "message_index": index, "role": message.role, "content": message_content,
                    "conversation_ref": content_digest([principal, conversation_key]) if conversation_key else None,
                }),
            ))
            inputs.append(ExecutionInput(
                kind="instructions" if trusted else "task_request", provenance=provenance,
                content_ref=content_ref,
            ))
            inputs.extend(image_inputs)
        grant_ref = await register(ExecutionGrant(
            schema_version="1", kind="grant", grant_id=run_ref + ":grant:" + str(uuid4()), version=profile.policy_version,
            caller_run_ref=run_ref, workspace_ref=workspace,
            isolation=isolation,
            network="none", runtime_profiles=[profile.runtime_profile] if profile.runtime_profile else [],
            **({"agent_models": [request.model]} if profile.runtime_profile is None else {}), command_spec_refs=[],
            resource_refs=[definition_ref, timeout_ref, budget_ref, *profile.tool_refs,
                           *(item.content_ref for item in inputs)],
            policy_ref=profile.policy_ref, not_before=now - timedelta(seconds=1), expires_at=deadline,
            max_seconds=profile.timeout.hard_seconds, delegation="none",
        ))
        return AgentExecution(
            schema_version="1", kind="agent", idempotency_key=run_ref,
            caller_run_ref=run_ref, workspace_ref=workspace, execution_grant_ref=grant_ref,
            budgets_ref=budget_ref, requirements=ExecutionRequirements(isolation=isolation),
            agent_definition_ref=definition_ref, runtime_profile=profile.runtime_profile,
            model=request.model if profile.runtime_profile is None else None,
            inputs=inputs, tool_refs=profile.tool_refs,
        )

    async def completion(self, principal: str, attempt_id: str, model: str) -> dict:
        """確定した成功と保存済み bytes を照合してから補完応答を作る。"""
        attempt = await self.service.describe(principal, attempt_id)
        if attempt.get("reason") == LEAK_REASON:
            raise ExecutionError(LEAK_REASON, LEAK_MESSAGE, 502, safe_to_retry=False)
        if attempt["status"] != "succeeded" or attempt["stop_confirmation"] != "confirmed" or attempt["side_effect_resolution"] != "resolved":
            raise ExecutionError(attempt.get("reason") or "execution_incomplete", "Chat execution did not complete with confirmed effects", 409, safe_to_retry=False)
        outputs = [item for item in attempt["artifacts"] if item["name"] == "output"]
        if len(outputs) != 1:
            raise ExecutionError("output_contract_error", "Chat output Artifact is unavailable", 502, safe_to_retry=False)
        output = outputs[0]
        artifact = await self.service.store.artifact(principal, output["ref"])
        if hashlib.sha256(artifact["content"]).hexdigest() != output["digest"]:
            raise ExecutionError("output_contract_error", "Chat output Artifact digest changed", 502, safe_to_retry=False)
        try:
            content = artifact["content"].decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ExecutionError("output_contract_error", "Chat output is not UTF-8", 502, safe_to_retry=False) from error
        if guard_enabled():
            try:
                check_answer(content)
            except CredentialLeakSuspected:
                raise ExecutionError(LEAK_REASON, LEAK_MESSAGE, 502, safe_to_retry=False) from None
        usage = attempt["usage"]
        prompt, completion = usage.get("input_tokens"), usage.get("output_tokens")
        return {
            "id": "chatcmpl-" + attempt_id, "object": "chat.completion",
            "created": int(datetime.fromisoformat(attempt["created_at"]).timestamp()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": None if prompt is None or completion is None else {
                "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
            },
            "kotoyose": {"schema_version": "1", "attempt_id": attempt_id, "usage": usage,
                    "output_artifact_ref": output["ref"], "session_mode": "fresh_with_client_history"},
        }

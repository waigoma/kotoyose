# Native Codex sandbox adapter

This adapter provides an opt-in Codex route through the existing durable execution API.
It does not enable any live alias, profile, Caller, credential or service configuration.
Hub automatic-only Definition/profile selection and Task/Run budgets remain separate work.

## Admission and prerequisites

`NativeRuntimeProfile.provider: codex` requires a fixed model, executable CLI and a
reviewed `codex_rootfs` with its `codex_rootfs_digest`. The normal automatic route
uses explicitly selected ChatGPT subscription authentication:

```yaml
provider: codex
credential_mode: chatgpt_subscription
credential_name: CODEX_CHATGPT_AUTH
codex_auth_file: /absolute/path/to/existing/codex-home/auth.json
```

The file must be canonical, regular, owned by the Kotoyose user and owner-only readable
(for example mode 0600). Kotoyose reads
it without copying or changing it. Every path component is opened with no-follow
directory descriptors, including protection against a parent symlink replacement
between resolution and open. A safe atomic replacement at the same path is allowed
for same-account renewal; changed ownership/mode, symlinks or another account stop
the request. A changed configured path is profile drift and requires a new execution. Its explicit `auth_mode: chatgpt`, absence of an
API key, access-token expiration and account identity are checked. This does not
prove model eligibility or remaining quota. No refresh/id token is retained or sent.
Existing host CLI login/renewal owns authentication updates; the broker rereads on
each request and accepts updated tokens for the same admitted account. Missing,
expired or revoked auth requires renewal/re-login outside the execution. Kotoyose does
not refresh tokens, purchase credits, or create persistent authentication state.

For callers who explicitly select paid API usage, the existing `credential_mode:
environment`, `credential_env` and `credential_name: OPENAI_API_KEY` profile remains
available. Authentication mode is required; omitted/unknown modes cannot select API.
A subscription profile rejects credential_env, API keys and arbitrary endpoints.
Endpoint inheritance, MCP, client tools and approval bindings are rejected.
Only `builtin:sandbox-exec@1`, or no tool at all, is supported.

An agent alias names a `route` and an `agent` place/surface. The route names the
driver, model and credential. The machine's `drivers` entry holds the CLI and
credential file. Rootfs, digest and coding egress limits are per cell
(`cells.standard` for the jail, `cells.coding` for host coding). Alias cwd, HOME, host sandbox flags, endpoints, config and MCP
are not inherited into the agent run. Caller `agent_models`, `places` and
`max_surface` continue to apply. An alias without `agent` is chat-only and cannot
run as an agent. A Codex alias resolved into the write cell at place `isolated`
keeps `execution_environment=isolated`: the container is the boundary and the SDK
sandbox is inactive. `codex.sandbox` is retained as that inactive alias
(`full-access` is rewritten to `danger-full-access`). The exec jail cell keeps
`execution_environment` unset and stores no `codex_sandbox`.

## Read room

Surface `read` on `codex.app-server` is a room (`room_read_v1`), not a jail.
The tool ref is exactly `builtin:workspace-read@1`. It uses the coding rootfs
and the coding egress limits, ChatGPT subscription credentials, and the same
container room capacity as host coding. When that capacity is full, admission
refuses with `capacity_exhausted` / `containers` (HTTP 429) before creating an
attempt, the same refusal host coding already uses.

`/workspace` is mounted read-only. Codex state stays in the container home
(`/home/agent`, writable, not inside the workspace). A review clone does not
need the `.codex` or `.agents` directories that host coding requires. The saved
SDK sandbox is fixed to `read-only` (a saved `workspace-write` value is rejected),
but like host coding the thread is delegated to `externalSandbox` before any model
input: the container has no bubblewrap, so Codex's own sandbox cannot run commands
there. The read-only `/workspace` mount is what prevents writes.
The sandbox policy name is `workspace_read_sandbox_v1`, with its own tool-policy
digest, so a resume cannot swap a read attempt into a write room or the reverse.
Claude `read` stays closed.

Admission records CLI content digest, SDK version, model, authentication source and
rootfs digest. Subscription snapshots also pin `codex_auth_file` and
`codex_auth_identity` (SHA-256 of the account identifier, without token values).
Token renewal preserves identity; another account/source needs a new execution.
Dispatch rechecks the selected profile/alias and artifact contents.
The CLI is independently required to report `codex-cli 0.170.0` inside the jail before
app-server starts. Kotoyose also checks the installed SDK pin independently.
Failed prerequisites stop dispatch without host fallback.

## Provider filesystem and network

The provider launcher uses bubblewrap with private network/PID/user namespaces,
no capabilities, the selected CLI binary and a dedicated read-only rootfs. Codex
has no broad host `/usr`, `/bin`, `/lib`, `/lib64` or `/etc/ssl` mount. The rootfs
inventory/content/modes must match the reviewed digest; symlinks, writable entries,
controller/package directories and private certificate directories are rejected.
The artifact digest requires review of its actual contents; a digest alone does not
establish that an arbitrary library tree is safe. No production rootfs is registered
by this change. `/home`, `/tmp` and `/runtime` are fresh tmpfs; provider HOME and
CODEX_HOME are empty. The workspace and host credentials/config are not mounted.

A private loopback bridge can reach only its per-run UNIX socket broker. The broker
accepts only local POST `/v1/responses`. Subscription requests are mapped exclusively
to fixed TLS `chatgpt.com:443`, POST `/backend-api/codex/responses`; the broker injects
`Authorization: Bearer <access token>` and `ChatGPT-Account-Id`. CLI content/accept/
originator/user-agent headers are preserved under the existing allowlist. Real OAuth
never enters CLI environment, RPC, model/tool input, workspace or persisted artifacts.
The CLI receives only a random per-execution broker capability in
`KOTOYOSE_CODEX_BROKER_CAPABILITY`. No host authentication file is mounted in the jail.
Only explicitly selected API profiles reach `api.openai.com:443` `/v1/responses`.
Both fixed routes check every DNS answer is public, pin the selected IP, and fix
certificate verification/SNI.
It does not interpret host proxy or certificate override environment variables.
Absolute targets, other paths/authorities, CONNECT, WebSocket upgrade, proxy headers,
organization/project overrides, private/loopback/link-local/metadata destinations,
duplicate/framing headers and unknown headers are rejected. Fixed CLI session/diagnostic
headers are accepted by name and removed before upstream. Request and response traffic,
connection counts/concurrency and wall time are bounded; broker shutdown closes pending
connections before confirmed completion.

Each request must match the admitted model, broker capability/explicit API credential
and exact dynamic tool schema. The existing transactional operation guard rechecks
Grant validity/revocation, workspace generation/writer and unresolved operations
before every provider request. Subscription responses are bounded and parsed before
forwarding; chunked framing is decoded so token/account reflection across chunks is
redacted before CLI consumption. Error bodies and arbitrary response headers are not
forwarded. HTTP 401 maps to `authentication_failed`, 403 to `subscription_denied`,
429 to `subscription_limit_reached`. The first failure closes the run to further
upstream requests, with no API fallback.
Standard text/history forms are accepted; remote media/file references, AdditionalTools,
unknown input forms, hosted/builtin tools, paid tier overrides and ambiguous JSON keys
are rejected before upstream dispatch. Client metadata is bounded and removed so it
cannot carry routing/credit hints. Canonical JSON is forwarded with recomputed framing.
Production code never logs headers, credentials, API bodies or provider stderr.

## Image Artifact inputs

Native Codex accepts the existing `ExecutionInput.kind: image` with an admitted
ArtifactRef. Caller/principal, Grant resource scope and provenance checks remain
mandatory. Admission and execution both check SHA-256, the ref's digest suffix,
MIME and image magic. PNG, JPEG, GIF and WebP allow at most ten images per
Attempt, with 20 MiB of aggregate original bytes (also 20 MiB per image). This is
a finite local admission/transport limit, not a provider or model maximum. Execution
rechecks saved size, count and aggregate size; direct adapter calls receive the same
byte/digest/MIME/magic and aggregate checks. Snapshots and events contain ref,
digest, MIME and size, never image bytes or base64.

Only these original bytes become canonical data URIs. Pinned CLI 0.160.0 receives
`turn/start.input` with text and `{type: "image", url: "data:<MIME>;base64,..."}`.
Its default image detail is omitted in that input, echoed as null in userMessage,
and emitted as `input_image` with `detail: "high"` in standard Responses. The
broker accepts only the exact ordered URI tuple on user messages, including the
admitted multiplicity on every follow-up request. Extra, missing, reordered or
replaced images, other detail values, assistant/tool/reasoning images, remote URLs,
local paths, file IDs and extra fields are denied before upstream send. A CLI
decode error or transformation that changes/removes an image fails this check;
the adapter does not silently resize, drop or substitute text for it. `view_image`
and native utility/file tools remain disabled.

The launcher allows exactly the fixed text catalog or its fixed text+image variant;
all other model metadata remains identical. This is a local transport declaration.
It does not prove the live model's vision eligibility, accuracy or review quality.

Image input has a separate finite transport allowance. If `I` is the sum of the
canonical data-URI byte lengths, each body remains bounded by the original 2 MiB
body cap plus `I`. At most `min(32, max_turns + 1)` requests can consume image
allowance, totaling at most that count times `I`. Only the exact validated image
strings are deducted from input accounting. Original text, headers and response
traffic still share the original broker byte limit; no image allowance can pay
for response/SSE traffic. The existing output and tool-output caps are unchanged.
The stdout line framing cap adds one `I`, while only the two exact scoped
userMessage started/completed echoes can exclude it from output accounting.
Echoed raw/base64 is removed before queueing. Other notifications, malformed
echoes and stderr still consume the original output/protocol budget.

An image execution requires both a scoped final-answer/turn-completed CLI result
and independent broker evidence: the exact images sent, HTTP 200 EndOfMessage,
and a valid Responses SSE `response.created`/`response.completed` pair with the
same response ID and completed status. Empty HTTP 200, interrupted/failed SSE,
ambiguous JSON or a fake app-server final without delivery evidence cannot succeed.
Every initiated broker connection must complete that evidence, including follow-up
requests. One completed request cannot hide another request still awaiting headers,
connection or response when CLI completion triggers cleanup; cleanup cancellation
does not turn that request into success.
The first broker failure terminates the image execution instead of waiting for
CLI retries. Image cancellation/timeout closes pending broker connections before
process-stop convergence and keeps its original reason. Unknown broker/process/
tool stop remains unconfirmed and cannot become success or a resumable proof.
Broker shutdown closes writers and cancels handlers before awaiting the server;
server transport closure and handler termination share the existing bounded grace.
Either remaining pending makes shutdown unconfirmed.
Durable resume readmits and rereads the Artifact under the new Grant/generation;
the preceding attempt's image-delivery evidence is never reused.

Conformance uses the actual pinned CLI with fake API and subscription fixtures,
small and over-1-MiB complete PNGs, sandbox_exec and no-tools, a ten-image JSON
review-shaped result, denied image substitutions and bounded input/response tests.
Owned PostgreSQL tests also cover Artifact drift and four-way concurrent durable
resume. These prove transport and safety boundaries only. No real model, live
provider/DNS, host credential or production configuration is used by these tests.

## Pinned app-server contract

The adapter sends the experimental dynamic-tool wire fields directly, using the
SDK generated agent-message model and existing final-answer phase selection. It
sets `thread/start.environments=[]` and a dedicated provider using the official
public custom-provider env-key contract, fresh CODEX_HOME and disabled WebSockets.
For subscription this env key is a broker capability, not an OpenAI API key. The
internal-only `ChatgptAuthTokens` login RPC is not used. Model/entitlement information
is not inferred from that capability.
The returned model and provider must match before `turn/start`.

Environment removal and feature flags alone do not eliminate all native tools in
0.160.0. A generated startup-only static model catalog keeps the admitted model slug
and selects direct tools, disabled multi-agent metadata and standard Responses.
It disables model-selected utility tools, plan/input tools and local shell tools.
The launcher validates the catalog against the fixed policy before binding it.
The egress schema independently rejects any additional tool inventory. Relevant
pinned contracts: [ModelInfo](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/protocol/src/openai_models.rs),
[tool registration](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/src/tools/spec_plan.rs),
[provider auth](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/model-provider/src/auth.rs).

Only scoped `sandbox_exec` RPCs with exact argv/cwd are admitted. Unknown methods,
unknown tools, malformed arguments, namespace widening and missing grants fail the
attempt; explicit foreign-thread/turn noise is separately denied. Duplicate RPC
and semantic call IDs within one live turn replay the recorded result; mutations
fail. These caches do not claim exactly-once effects across fresh model inference.
Every actual command still uses the existing operation journal and current Grant/
generation checks, then the credential-free, network-free Docker workspace worker.
Existing CPU, memory, PID, time and output bounds continue to apply.

## Completion, accounting and recovery

Unique tool calls plus assistant items are conservatively bounded by `max_turns`.
Reported token counts are observations. Cost is unknown and is not a hard ceiling
on provider billing. Missing/failing completion, commentary-only output, invalid
JSON, tool denial, limits and unknown process/tool/broker stop cannot produce success.
Bounded partial completed output is preserved, with the provider credential redacted.

Provider process stop uses the existing process identity/pidfd mechanism for the
bounded observed subtree, and requires the private PID namespace init identity and
exit as proof that newly forked/reparented children also stopped. Recovery/reconcile
uses the same Codex proof. A vanished outer monitor without namespace proof remains
unconfirmed and retains workspace writer/generation; workspace operations independently
require confirmed stop. When a terminal Attempt has a validated partial receipt with
only the cgroup scope unconfirmed, reconcile may instead verify exit of its saved
private PID namespace init and provider identities. Linux terminates all processes
in a PID namespace when its init exits ([pid_namespaces(7)](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)).
The controller must durably append a receipt bound to the original ownership,
workspace generation and partial receipt before confirming stop. It preserves the
original cgroup observation and terminal verdict. Missing namespace ownership,
unfinished tasks/pipes/egress, or failed persistence keeps stop unconfirmed; pending
external operations continue to hold the workspace writer. A saved stop receipt
does not depend on current executor configuration or reopening a vanished cgroup.
Recovery
uses the existing durable-state manifest and new-generation Grant. Resume also
requires the same canonical rootfs path and reviewed content digest across generations;
another valid artifact requires a fresh execution instead. Ephemeral thread IDs do
not provide native session resume. Repeated inference can select new commands;
operation IDs and grants fence execution, and no exactly-once model guarantee is made.

An administrator may separately release explicitly identified terminal Attempts
from admission accounting when a human accepts that their stop state is unknown.
The private `admission_release.release_terminal_admissions` operation requires the
Caller, exact Attempt/request/snapshot/workspace/generation identities and a human
authorization reference. It atomically appends immutable audit records, requires
an idle Caller for new releases, and rejects changed targets. It does not expire
the human decision or infer confirmed stop. Original Attempts, operations, workspace
writers, artifacts and budgets remain unchanged; old workspaces cannot be reused
on this basis. Changed/running Attempts and revoked releases count again. New
Attempts still obey the configured concurrency limit. This administrative operation
is not exposed through HTTP resources, MCP tools or automatic reconciliation.

## Verification and remaining limits

`tests/test_native_codex.py` uses fake app-server processes and real bubblewrap for
protocol/limits/denials, host config/credential exclusion, direct network rejection,
CLI/rootfs drift, descendant stop and secret-free results. Real-jail cases skip when
bwrap is absent; a generic CI skip is not isolation evidence.

`tests/test_codex_cli_conformance.py` opts in through
`KOTOYOSE_CODEX_CONFORMANCE_BINARY`. It executes the real pinned CLI with a fake key
and fake loopback Responses API: subscription OAuth injection or explicitly selected
API auth, exact model/auth, empty tool inventory, or
sandbox_exec RPC → Kotoyose callback → tool-result followup request → final completion.
Only the broker's fixed TLS connection is replaced; jail and policy are exercised.

`tests/test_native_codex_postgres.py` additionally requires a database whose name
starts `ahs_execution_test_codex_` and immutable `KOTOYOSE_DOCKER_TEST_IMAGE`. It covers
concurrent dispatch, real Docker tool effects, journal convergence, profile/CLI/rootfs
drift, confirmed cancellation and durable-state replay rejection. With the explicit
CLI fixture it joins the real CLI/fake API to actual PostgreSQL journal and Docker.
All fixture secrets are false values; no real provider or model is called.

Live upstream authentication/TLS/subscription eligibility and quota behavior,
rootfs distribution and production rollout are not verified. All production prerequisites
remain unset/unchanged here;
the opt-in route fails closed when prerequisites are missing. Passing fake-provider
conformance does not enable a live Luna route or complete the Hub implementation.

## Hub binding contract

Hub first calls authenticated `POST /v1/runtime-authentication/resolve` with
`{"schema_version":"1","model":"<reviewed-alias>"}` or `runtime_profile` instead of
`model` (exactly one). Only an administrator-configured native Codex profile within
the caller's allowed alias/profile scope can be resolved. The response is
`{"schema_version":"1","runtime_authentication":{"mode":"chatgpt_subscription","binding_digest":"<64-lowercase-hex>"}}`.
This read does not launch a process, call a model, contact a provider, renew tokens,
or create an attempt. It returns no path, environment name, account identifier or
credential; responses use `Cache-Control: private, no-store`. Unavailable/wrong
authentication returns `authentication_failed` (401); caller scope denial is 403,
Unsupported Agent bindings fail request validation.

The subscription Hub binding pins this result and sends both
`AgentExecution.required_authentication_mode: "chatgpt_subscription"` and
`expected_authentication_binding_digest: "<binding_digest>"`. Both are mandatory
when a subscription profile or required subscription mode is selected. Missing
either returns `invalid_request` (422), reason_code `authentication_binding_required`.
A mode mismatch returns `permission_denied` (403), reason_code
`authentication_mode_mismatch`; a source/account digest mismatch returns
`request_conflict` (409), reason_code `authentication_binding_mismatch`. Admission
rejects these before creating an attempt. The fixed expected digest is retained in
the snapshot and rechecked against current profile/source/account before provider
start and each upstream send, including after DNS/TLS awaits. A changed binding
stops further requests; it does not silently accept a fresh readback or fall back
to API. Token renewal for the same account and source keeps the digest unchanged.
Hub must stop automatic dispatch when preflight readback/pinning is unavailable;
a rejected changed binding requires an explicit binding review before a new pin.
Existing explicitly selected API/Claude profiles preserve omitted-field behavior.
An API request may explicitly require `"api_key"` and optionally pin its digest.

Attempt responses expose `runtime_authentication: {mode, binding_digest}` for native
Codex. `mode` is `chatgpt_subscription` or `api_key`. `binding_digest` is SHA-256 over
canonical JSON of credential_mode, credential_name, credential_env, codex_auth_file
and codex_auth_identity (including nulls). It detects source/account/mode changes,
without exposing host paths, environment names or credentials. Attempt readback is
an audit of the admitted binding; the preflight endpoint supplies the pin before
dispatch. Hub also checks the returned attempt digest against its expected binding.
API selection is a separate explicit choice.

Light uses no tools. The ordinary native Codex profile exposes only sandbox_exec;
existing Claude orchestration/client-tools behavior is unchanged. Arbitrary assistant coordinator
client tools cannot be added to this Codex boundary by widening a Hub binding.

## Deployment conditions

No production alias/profile, credentials, rootfs, firewall or endpoint permissions
are enabled by this change. Before activation the operator must identify the existing
subscription auth source, confirm owner-only read access by Kotoyose, review/pin the CLI
0.170.0 and dedicated rootfs, and authorize the broker's fixed ChatGPT TLS destination.
See the [rootfs upgrade procedure](runbooks/codex-rootfs-build.md#activate-the-01700-release).
No credential copy or new persistent store is required. If permissions or network
policy changes are needed, describe and approve them before applying. A separately
approved real subscription-only smoke test must verify live compatibility and model
eligibility; fake-provider success does not establish either.

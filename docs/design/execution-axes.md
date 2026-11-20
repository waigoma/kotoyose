# 実行の軸 (model / tools / place / allow) 設計書

> 2026-11-20 に旧 bearer 認証、旧 host Agent 実行、`defaults` preset を撤去し、alias の `execution: legacy` は `execution: direct` に改名しました。同日に旧管理 UI も撤去しました。以下は当時の記録です。

- 状態: 提案 ([ADR-0010](../adr/0010-execution-axes.md) の詳細)。Fable のレビューは [execution-axes-review.md](execution-axes-review.md)。所有者の判断 (2026-11-15): 推奨で進める。判断と実装の順は ADR-0010 に記録。本文と食い違う所は ADR-0010 とレビューが優先
- 日付: 2026-11-15
- 根拠のコード: commit `6cd8d4f` (main)。`file:line` はこの commit の行番号

## 0. 範囲と、読めなかったもの

この文書は、Kotoyose が CLI や API を動かすときの「どこで」「どこまで」を全モデル共通にするための設計である。対象は `/v1/chat/completions`・`/v1/executions` 系・`/v1/conversations`・pool・remote worker。

次は確かめていない。推測で埋めた箇所はその都度「未確認」と書く。

- 下書きは本番の設定を読まずに書いた。§5.2 の表だけは、運用担当が本番の `config.yml` (22 alias) と過去 90 日の利用記録で書き直した。ほかの節で recipes から引いた alias 名は、本番とずれている可能性がある。
- `kotoyose-oneshot`・sample-client-key・n8n が使う alias 名は、このリポジトリからは分からない。
- Codex SDK (`openai-codex`) と Cursor SDK が子 process に環境変数をどう渡すかは、SDK の中を読んでいない。

## 1. 目的と原則

所有者の意図はこうである。「変わるのは model と MCP (道具) だけ。どこで動くか・どこまで許すかは共通で、model を足しても中は共有のまま」。今は chat / legacy / bwrap / docker という名前が経路ごとにあり、同じ model でも呼び方で安全性が変わる。

原則は 5 つにする。

1. alias が持つのは **model・tools・place・allow** の 4 つだけ。
2. place と allow の保証は、adapter (CLI ごとの差分) ではなく **1 つの runner** が与える。adapter は保証を弱められない。
3. chat と execution の違いは **出入口の形** だけにする。会話を返すか、作業場に結果を残すか。閉じ込めの仕組みは同じ。
4. 呼び出し元の key ごとに **呼べる alias と place・allow の上限** を持つ。上限を超える alias は呼べないし、一覧にも出ない。
5. サブスクは公式 CLI が自分のログインで動く形だけ ([ADR-0001](../adr/0001-subscriptions-through-official-clients.md))。v1 までは互換層を作らず、旧実装は置き換えて消す ([ADR-0009](../adr/0009-breaking-changes-before-v1.md))。

## 2. 今の姿

### 2.1 設定は 2 か所に分かれている

| 置き場 | 読む所 | 中身 |
|---|---|---|
| `config.yml` (`HarnessConfig`) | `config.py:915-935` | `models` (alias)・`routes`・`connections`・pool・skills/MCP/prompt/agents の定義 |
| `KOTOYOSE_EXECUTION_CONFIG` の JSON (`NativeExecutionConfig`) | `execution_bootstrap.py:19-30`, `execution_resources.py:1023-1045` | `callers` (CallerPolicy)・`drivers` (CLI binary と rootfs/image)・`agent_container`・`native_worker`・`legacy_bearer`・`worker_capabilities` |

呼び出し元の上限 (`CallerPolicy`) は JSON 側にあり、chat が読む `config.yml` 側には無い。API key の行は `kotoyose.api_keys.caller` で JSON の caller 名を指す (`api_key_store.py:7-18`)。

### 2.2 経路の一覧

| 経路 | 入口 | どこで動くか | 認証が CLI に届く形 | filesystem | network | 環境変数 |
|---|---|---|---|---|---|---|
| chat / Claude | `/v1/chat/completions` → `routes/chat.py:205` → `session.py:118` → `config.build_options` (`config.py:1720`) → `runtime/claude_options.py:60` | Kotoyose の子 process (claude CLI) として host 上。Kotoyose と同じ user | `_build_auth_env` が token を `options.env` へ (`config.py:1696-1717`) | 制限なし。cwd は preset の `cwd` (`claude_options.py:97-101,126`)。`permission_mode` 既定 `bypassPermissions` (`config.py:450`)、`deny` は文字列パターン (`claude_options.py:127-128`) | 制限なし | SDK が Kotoyose の `os.environ` を丸ごと継承し、その上に `options.env` を重ねる (`claude_agent_sdk/_internal/transport/subprocess_cli.py:819-821`、SDK 0.2.163) |
| chat / Codex | 同上 → `runtime/codex.py` | host。Codex SDK の app-server | `CODEX_HOME` (alias の `codex_home`) の auth.json を CLI 自身が読む | Codex 自身の sandbox (`codex.sandbox`、`runtime/codex.py:589-606`) | Codex sandbox 次第 | `env={"CODEX_HOME": ...}` だけ指定 (`runtime/codex.py:418`)。他の継承は未確認 |
| chat / Cursor | 同上 → `runtime/cursor.py` | host。Cursor SDK の local agent (`runtime/cursor.py:405`) | API key を secret / env から (`runtime/cursor.py:379-383`) | Cursor 側の `sandbox_options` (`runtime/cursor.py:645`) | 未確認 | 未確認 |
| chat / Gemini | 同上 → `runtime/gemini.py` | host の subprocess | CLI 自身の home | 制限なし | 制限なし | `env` 指定なしで `os.environ` を継承 (`runtime/gemini.py:90-93`) |
| chat / agy | 同上 → `runtime/antigravity.py` | host の subprocess | connection の login home を `HOME` に (`runtime/antigravity.py:577-590`) | 制限なし (agy の permission 次第) | 制限なし | allowlist (PATH・LANG・TERM) + HOME (`print_cli_profiles.py:380`) |
| chat / native | `execution: native` の alias。`routes/chat.py:212` → `compatibility_http.py:84` | execution の Attempt と同じ (下の bwrap 行) | 同下 | 同下 | 同下 | 同下 |
| execution / legacy | `agent.place: host` で print 以外 (`agent_route.py:206-217`) → `legacy_agent.py:52` → 子 `legacy_agent_runner.py` → chat と同じ `runtime/*` | host の子 process | chat と同じ | 作業場が cwd。閉じ込めなし | `network: unrestricted` と自己申告 (`legacy_runtime.py:54`) | `env={**os.environ, ...}` で丸ごと継承 (`legacy_agent.py:77`) |
| execution / Claude jail | `agent.place: isolated`, surface `none` (`agent_spec.py:194-199`) → `native_claude.py:222` | bwrap (`provider_jail.py:44-131`)。systemd scope は使わない | `CLAUDE_CODE_OAUTH_TOKEN` 等 1 つを env で (`native_claude.py:813-816`, `provider_jail.py:133-170`) | root は空 tmpfs、`/usr` `/bin` `/lib` を ro、HOME は tmpfs。作業場なし | **host の network を共有** (`--share-net`, `provider_jail.py:79`) | allowlist (PATH・HOME・LANG・credential・base URL) を `execve` で渡す (`provider_jail.py:156-170,186`) |
| execution / Codex jail | surface `exec` (`agent_spec.py:239-244`) → `native_codex.py` → `codex_provider_jail.py` | systemd user scope (`codex_provider_scope.py:35-40`) + bwrap | broker が host の `auth.json` を読み、upstream へ `Authorization` と `chatgpt-account-id` を付ける (`codex_egress.py:260-271`)。CLI は per-run capability だけ | 審査済み rootfs を ro、HOME/tmp は tmpfs (`provider_jail.py:83-90`) | private namespace。UNIX socket の broker だけ (`docs/native-codex-sandbox.md`) | allowlist + capability (`provider_jail.py:156-165`) |
| execution / container (Claude・Codex の coding と Codex の read) | surface `write` / `read` → `native_claude.py:231` → `container_claude.py`、`native_codex.py:325` → `container_codex.py` | Run ごとの Docker container (`agent_container.py:492-526`) | HTTP を終端する broker が実 credential を注入 (`provider_egress.py:147-166`, `container_claude.py:135-155`)。Codex は auth.json を読む broker | `--read-only` root、作業場を `/workspace` に rw (read は ro)、HOME は Run ごと | `--network none`。出口は ro bind した UNIX socket だけ | `HOME` 固定、秘密らしい名前の env は拒否 (`agent_container.py:467-478`) |
| execution / cli.print host | `agent.place: host` + `cli.print` (`agent_route.py:207-211`) → `print_cli_agent.py:239` | host の subprocess | login home を `HOME` に | 作業場が cwd。surface write なら agy が host 上で `command(*)` と書き込みを許される (`print_cli_profiles.py:260-271`) | 制限なし | allowlist + HOME |
| execution / cli.print jail | `agent.place: isolated` + `cli.print` → `container_print.py` | Docker container | login home を `/home/agent` に rw bind (`container_print.py:42,99,130,145`)。Kotoyose は中を読まない | 同上 | `--network none` + CONNECT の素通し broker (`print_cli_egress.py:1-9`)。許可先は profile の 6 host (`print_cli_profiles.py:339-348`) | allowlist + HOME |
| tool の command | `builtin:sandbox-exec@1` | `local-docker` (`local_worker.py:106`)、`local-systemd-bwrap-v1` (`native_worker.py:171`, `execution_service.py:80-84`)、remote | credential なし | 作業場だけ | なし | allowlist |
| `/v1/conversations` | `routes/conversations.py:99` → `conversation/provider.py` | Kotoyose の process 内。CLI なし。Anthropic Messages API を直接呼ぶ | Kotoyose が endpoint の key を持つ (ADR-0001 の (3)) | 関係なし | Kotoyose から endpoint へ | 関係なし |
| pool | `models` に書いた pool alias。chat は `routes/chat.py:194-202` | 候補 1 つを選んで、その候補の経路に乗る | 候補次第 | 候補次第 | 候補次第 | 候補次第 |
| remote worker | `worker_capabilities` の remote | 別の機械。server 側は `DockerLocalWorker` だけ (`remote_worker_server.py:20`) | credential なし | 共有マウントの作業場 (`SharedWorkspaceBinding`, `execution_resources.py:838-863`) | なし | command 専用 |

### 2.3 同じ言葉が別の意味で使われている

| 言葉 | 意味 | 根拠 |
|---|---|---|
| `isolation: native` | host で動かす | `execution_contract.py:32`, `legacy_runtime.py:70` |
| `backend: native` | jail (bwrap / container) で動かす | `agent_spec.py:661-665` |
| `execution: native` (alias) | chat を Native Attempt に回す | `config.py:366`, `compatibility_http.py:26-36` |
| `place: isolated` / `execution_environment: isolated` / `agent_execution: sandbox` | どれも「閉じ込めあり」 | `agent_spec.py:53`, `config.py:369`, `agent_spec.py:668-670` |
| `jail` / `room` | surface none・exec は bwrap、read・write は container | `agent_spec.py:27` |
| `legacy` | host 実行の backend 名で、chat の alias 区分でもある | `config.py:366`, `routes/models_api.py:34` |

`native` が「host」と「閉じ込めあり」の両方を指す。所有者の「分かりにくい」はここが一番大きい。

### 2.4 設定欄が効く所と無視される所

| 欄 | 効く所 | 無視される所 |
|---|---|---|
| alias `agent.place` / `agent.surface` (`config.py:140-145`) | execution の解決 (`agent_route.py:194-243`)、Caller の `places`・`max_surface` 判定 (`execution_resources.py:788-803`)、pool の形の一致 (`config.py:1191-1204`)、`/v1/models` の `agent` 欄 (`agent_route.py:246-264`) | **chat 全体**。`routes/chat.py` は `agent` を読まない |
| host の surface | — | host は surface を強制しない (`surface_enforced=False`, `agent_spec.py:172-186`)。`/v1/models` も `enforced: place == "isolated"` と出す (`agent_route.py:263`) |
| Caller `agent_models` | execution の admission (`execution_service.py:528`)、native chat (`compatibility_execution.py:34`)、`/v1/conversations` (`routes/conversations.py:69-73`) | **legacy chat**。`/v1/models` も `execution == "legacy"` の alias は全部返す (`routes/models_api.py:34,41`) |
| Caller `places` / `max_surface` | execution と native chat | legacy chat |
| API key の `caller` | `caller` があれば principal と policy が付く (`execution_http.py:34-41`) | `caller` が空の key は policy なしで chat を全部呼べる。旧 bearer も `legacy_bearer: true` (既定、`execution_resources.py:1024`) なら同じ |
| alias `deny` / `permission_mode` / `tools` | chat の Claude (`claude_options.py:116-128`) | jail の Claude は `tools=[]`・`dontAsk` に固定 (`native_claude.py:817-835`) |
| `execution_environment` | Codex の host / isolated 選択 (`agent_spec.py:475-522`) | Claude では使えない |
| `bare` / `setting_sources` / `require_isolation` | chat の Claude | jail は常に `setting_sources=[]` (`native_claude.py:834`) |

### 2.5 分かったこと

1. **chat には閉じ込めが無い**。Claude の chat は host 上の Kotoyose と同じ user で、Kotoyose の環境変数を全部持ち (`DATABASE_URL`・secret store の鍵の場所・他 provider の API key を含みうる)、既定で全 built-in tool と `bypassPermissions` が付く。`deny: ["Bash(curl*)"]` は `python -c` で回り込める。当時の運用 preset には、この形で Bash を使えるものがあった。
2. **chat は呼べる alias を絞っていない**。key の `caller` が空なら policy が無く、あっても legacy chat は policy を見ない。
3. **legacy execution は環境変数を丸ごと継承する** (`legacy_agent.py:77`)。`credential_isolation: False` と自分で申告している (`legacy_runtime.py:52-58`)。
4. **閉じ込めの実装が 3 つある**。Claude の bwrap (network 共有)、Codex の bwrap (rootfs + broker)、Docker (network none + broker)。どれが選ばれるかは provider と surface の組で決まり (`agent_spec.py:27`, `native_claude.py:231`, `native_codex.py:325`)、使う人からは見えない。
5. **ADR-0001 の線が経路で違う**。print は CLI が自分の home で動き CONNECT 素通し。Claude の container は broker が setup-token を upstream へ付ける。Codex は broker が auth.json を読む (ADR-0001 が「残り」と認めている)。
6. Claude の `config_dir` 接続 (公式 `/login`) は jail で使えない (`agent_route.py:44-48`, `config.py:1146-1155`)。理由はコードからは読めない (未確認)。
7. 良い部品はもうある。print の `PrintCliProfile` (データで CLI 差分を持つ)、Docker の per-Run container、egress の 2 種の broker、MCP を host 側で呼ぶ `ExternalMcpBroker` (`mcp_broker.py:17-40`)、Attempt の pin と durable resume。設計はこれらを 1 本に並べ直すことである。

## 3. 新しい軸

### 3.1 alias の形

```yaml
aliases:                       # 今の models。pool もここ
  assistant-chat:
    route: claude-sonnet       # model の軸。route → driver (adapter) + model + connection
    tools:                     # tools の軸
      prompts: [assistant-identity, assistant-principles, assistant-user, assistant-memory]
      skills: []
      mcp: [memory_gateway, mem0]
      agents: []
    place: host                # place の軸
    allow:                     # allow の軸
      tools: none
  assistant-cron:
    route: claude-sonnet
    tools: { prompts: [memory-refresh], mcp: [memory_gateway, mem0] }
    place: jail
    allow:
      tools: write
      workspace: ~/kotoyose/assistant-workspace
      protect: [policy/identity.md, policy/principles.md, policy/user.md]
      egress: []               # adapter 既定 (vendor) に足す分

callers:                       # 今の JSON の callers をここへ移す (§9 問い 6)
  assistant:
    keys: [assistant-client, sample-client-key]
    aliases: [assistant-chat, assistant-cron, assistant-classify]
    places: [host, jail]
    max_allow: { tools: write, egress: vendor }
    workspaces: [~/kotoyose/assistant-workspace]
    limits: { seconds: 1800, memory: 4GiB, pids: 512, cpus: 2, concurrent: 2 }
```

alias から消える欄: `provider`・`model`・`anthropic_*` (route が持つ)、`execution`・`execution_environment`・`native`・`agent`、`permission_mode`・`deny`・`tools` (built-in の列)・`bare`・`setting_sources`・`require_isolation`・`strict_mcp_config` (runner が固定する)、`cwd`・`allow_cwd_override`・`cwd_allowed_roots` (`allow.workspace` と caller の `workspaces` に置き換え)、`codex.sandbox` などの provider 別の閉じ込め欄。provider 別の欄で残すのは effort・thinking・timeout・`model_params` のような **model の調整** だけで、route か alias の `tune:` に置く。

### 3.2 model

- 値: route 名。route は `driver`・`model`・`connection` (と `cli.print` なら `profile`) を持つ (`config.py:293-353` のまま)。
- driver が adapter を決める。adapter の一覧は §4.6。
- 保証: なし。model は「何で」だけで、どこで・どこまでには関わらない。route を変えても place と allow の保証は変わらない。
- 既定: なし (必須)。

### 3.3 tools

- 値: `prompts` (prompt file)・`skills`・`mcp`・`agents` (subagent) の参照名の列。今の `*_refs` と inline 定義をまとめる。
- adapter が native に注入できないものは本文を system prompt へ前置する (今の skills の扱い、`config.example.yml:29-33`)。
- MCP は **常に Kotoyose が呼ぶ**。CLI に MCP の接続先と認証 header を渡さない。Claude は SDK の in-process MCP server で中継し (`native_claude.py:578-611` と同じ仕組み)、Codex は app-server の dynamic tool で中継する。print 系 CLI は中継の口が無いので、jail では MCP を断る (§9 問い 12)。これで jail の network に loopback や私設アドレスを開けずに済み、`X-Pm-Mcp-Token` のような header の値も CLI の process に入らない。
- 保証: tools の軸は allow を広げない。MCP の tool は allow.tools が `none` でも使える (Kotoyose 側で動くので place の外に出ない)。

### 3.4 place

| 値 | 意味 | filesystem | network | env | 資源 |
|---|---|---|---|---|---|
| `host` | CLI の process は host で動く。**model が選んだ操作は host で 1 つも走らない** | cwd は Run ごとの空の private dir。HOME は connection の login home か空の dir。設定の自動検出は切る (Claude は `bare` + `setting_sources=[]`)。作業場は見せない | CLI の process は host の network を持つ (止められない)。model が使える道具は MCP と client tool だけなので、model から network は操作できない | adapter の allowlist + connection の 1 項目だけ。Kotoyose の `os.environ` は渡さない | process group で時間・出力の上限。memory/pids は観測だけ |
| `jail` | Run (chat なら会話) ごとの container | root は ro。作業場は allow に従って ro / rw / なし。HOME は login home の bind か Run ごとの dir | `--network none`。出口は UNIX socket の broker だけ。許可先は §3.7 | 同上 | memory・pids・cpus・wall time を container で強制 |
| `child:<名前>` (v2) | 別の機械の runner に渡す | child の place の保証に従う | 同 | 同 | 同 |

既定: `host`。`host` で許す allow は `tools: none` だけにする (§3.5)。読み書きや shell が要る alias は `jail` を書く。

`host` の保証は「CLI は host にいるが、model の手は host に届かない」である。今の chat は「CLI も model の手も host にいる」なので、ここが一番大きく変わる。

### 3.5 allow

allow は 2 つの欄を持つ。

**allow.tools** (順序付き。`none < read < write`)

| 値 | 意味 | place |
|---|---|---|
| `none` | CLI の built-in tool (shell・file・web fetch) を全部切る。使えるのは tools 軸の MCP と、呼び出し元の client tool と承認 tool だけ | host / jail |
| `read` | 作業場を ro で見せ、jail の中で shell を使ってよい | jail だけ |
| `write` | 作業場を rw (`protect` の列は ro)、jail の中で shell を使ってよい | jail だけ |

今の `exec` (`sandbox_exec_v1`: model の command を credential の無い別 container で 1 本ずつ走らせる) は値から外し、`read` / `write` に畳むことを提案する。ただし `exec` には「model の command が provider の credential を読めない」という性質がある。jail の中で CLI と shell が同じ container にいると、shell から login home が読める (§3.8)。この性質を残すかは §9 問い 3 で決める。

adapter は allow.tools を CLI の flag に写す。Claude は `tools=[]` と `allowed_tools` (`native_claude.py:805-838`)、agy は surface ごとの `--mode` と `--settings` (`print_cli_profiles.py:255-271`)、Codex は動的 tool と sandbox mode。**写せない値は adapter が「対応しない」と宣言し、runner は起動前に断る**。弱い近似で黙って通さない。

**allow.egress**

- 値: `vendor` (既定。adapter の profile が持つ許可先だけ)、`[host:port, ...]` (vendor に足す完全一致の列)、`open` (公開アドレスなら全部)。
- 私設・loopback・link-local・metadata のアドレスは、どの値でも許さない (今の `public_address` の判定、`codex_egress.py` 由来)。host 上のサービスへは MCP 経由で届く。
- `place: host` では egress は意味を持たない (model に network の道具が無い)。設定に書いたら読み込みで断る。

**allow.workspace / protect**

- `workspace` は alias に固定のパスか、`request` (呼び出し元が送った cwd を caller の `workspaces` の中で受ける)。今の `allow_cwd_override` + `cwd_allowed_roots` (`config.py:452-462`) と execution の `WorkspaceRegistration` を 1 つにする。
- `protect` は作業場の中で ro にするパスの列。例えば `deny: Write(.../identity.md)` で表していた保護を mount で強制する。Codex coding の `protected_workspace` (`provider_jail.py:93-97`) と同じ考え方である。

### 3.6 呼び出し元の上限との合成

- `aliases`: key の caller が列に持つ alias だけ呼べる。`*` は全部。pool は候補のどれかが通れば呼べる (今の `agent_profile_allowed` の扱い、`execution_resources.py:773-785`)。
- `places`: alias の place が caller の `places` に無ければ呼べない。
- `max_allow.tools`: alias の `allow.tools` が上限を超えたら **呼べない (403、一覧にも出ない)**。黙って下げない。`write` の alias を `read` に下げて動かすと、失敗の理由が分かりにくくなるからである。要求側が下げるのは許す (`min(要求, alias)`)。今の `max_surface` と同じ扱い (`execution_resources.py:798-802`)。
- `max_allow.egress`: `vendor` < `list` < `open`。alias の追加先が caller の上限に収まらなければ呼べない。caller が `[github.com:443]` を持つなら、alias の追加先はその部分集合でなければならない。
- `limits`: runner が caller の上限と alias (adapter) の既定の小さい方を使う。今の `CompatibilityExecution.profile` の比較 (`compatibility_execution.py:46-52`) と同じで、超えたら断る。
- 判定は 1 か所 (runner の手前の admission) に置き、chat・execution・conversations・`/v1/models` が同じ関数を呼ぶ。

### 3.7 出口の許可先の書き方

- adapter の profile が `vendor` の許可先を持つ。agy はもう持っている (`print_cli_profiles.py:339-348`)。Claude と Codex は今は broker の固定 upstream (`container_claude.py:86`, `docs/native-codex-sandbox.md`) で、CONNECT 素通しに寄せるなら、ログイン更新の先まで実測して決める。ADR-0005 の教訓 (agy の `oauth2.googleapis.com` は jail で塞いで初めて分かった) をそのまま当てはめる。
- alias は `allow.egress` で足すだけ。profile の許可先を減らす欄は作らない (CLI が動かなくなるだけなので)。
- broker は 2 種のまま runner の部品にする。
  - **素通し** (`print_cli_egress.py`): `CONNECT host:port` の行だけ見る。TLS はほどかない。CLI が自分で認証するとき (サブスク) に使う。
  - **終端して注入** (`provider_egress.py`): HTTP を読み、per-run capability を照合し、Kotoyose が持つ API key を付ける。Kotoyose が key を持ってよいとき (api_key / endpoint) だけ使う。
- どちらを使うかは adapter ではなく connection の kind で決まる (§3.8)。

### 3.8 秘密とログインが CLI に届く形

| connection | 例 | host | jail | ADR-0001 |
|---|---|---|---|---|
| subscription + `config_dir` | `claude-host`、agy、(提案) Codex の `CODEX_HOME` | login home を HOME (または `CLAUDE_CONFIG_DIR` / `CODEX_HOME`) に指す | login home を `/home/agent` に rw bind。Kotoyose は開かない。出口は素通し | (1) 公式 CLI が自分の home で動く |
| subscription + `secret` | `claude-oauth` (setup-token) | env の 1 項目 (`CLAUDE_CODE_OAUTH_TOKEN`) で CLI に渡す | 同じく env で渡し、出口は素通し (提案)。今は broker が upstream へ付けている (`container_claude.py:135-155`) | (2) 公式コマンドが発行した長期 token を本人の機械で公式 CLI に渡す |
| subscription + `auth_file` | 今の Codex | 廃止を提案。`config_dir` に寄せる (ADR-0004 の未決: `codex-host` と `codex-chatgpt` を 1 つに) | 同 | 今は broker が auth.json を読む。ADR-0001 の「残り」 |
| `api_key` / `endpoint` | Cursor、OpenAI API key、gateway | env の 1 項目 | broker が注入。jail に key は入らない | (3) API key |

素通しにすると、jail の中の CLI は token を持つ。`read` / `write` では model の shell も同じ container にいるので、login home や env の token を読める。出口が vendor の許可先だけなら持ち出し先は vendor の host に限られるが、vendor の公開 API (例: 保存系の API) を使った持ち出しは理屈の上では残る。broker 注入なら token は jail に入らないが、Kotoyose が token を CLI の request に付けることになり、ADR-0001 の「intermediate しない」に触れるおそれがある。どちらを取るかは §9 問い 4。

## 4. runner と adapter の境界

### 4.1 層

```
入口 (形だけが違う)
  /v1/chat/completions ─┐   会話: 会話キー → 温かい session、SSE で返す
  /v1/conversations ────┤   会話: 明示履歴 (endpoint adapter)
  /v1/executions ───────┘   実行: Attempt (admission・pin・Grant・resume) → 作業場と artifact
        │
  admission (1 か所): caller の上限 × alias の値 → RunPlan
        │
  runner: place backend (host / jail / child) と allow を適用し、adapter を起動して止める
        │
  adapter (CLI ごと): 起動の形・出力の読み方・止め方・usage・残量・ログイン
```

### 4.2 adapter の interface

```python
class Adapter(Protocol):
    id: str                      # "claude.agent-sdk" / "codex.app-server" / "cli.print:agy" ...
    capabilities: AdapterCaps    # 写せる allow.tools、MCP 中継、画像、resume、構造化出力

    def plan(self, run: RunSpec) -> LaunchPlan:
        """argv・追加 env の名前・必要な file (settings/model catalog)・vendor 許可先・
        login home の置き場を返す。process は作らない。place には触れない。"""

    async def open(self, proc: RunnerProcess, run: RunSpec) -> AdapterSession:
        """runner が起こした process の stdio で protocol を始める (SDK の connect、
        app-server の initialize、-p なら何もしない)。"""

    def turn(self, session: AdapterSession, input: TurnInput) -> AsyncIterator[Event]:
        """text delta・tool event・tool call (runner の bridge へ)・最終 result を順に返す。"""

    async def stop(self, session: AdapterSession, reason: StopReason) -> None:
        """protocol 上の中断 (interrupt RPC など)。process の停止確認は runner が持つ。"""

    def usage(self, result: Result) -> Usage            # 報告された値だけ。欠けは unknown
    def classify(self, exit: ExitInfo, tail: bytes) -> Outcome   # 残量切れ・認証切れ・timeout
    quota: QuotaProbe | None       # 公式の口だけ (ADR-0007)
    login: LoginSpec | None        # 公式 CLI の PTY 中継 (ADR-0006)
    resume: ResumeSpec | None      # provider の session id と session store の使い方
```

`PrintCliProfile` (`print_cli_profiles.py:104-128`) はこの interface の `plan`・`classify`・`quota`・`login` をデータで書いたものに当たる。-p 系 CLI はこれまでどおり profile 1 つで足せる。SDK や RPC を話す CLI (Claude・Codex・Cursor) は、`open` と `turn` をコードで持つ adapter になる。

### 4.3 runner が持つもの (adapter から移すもの)

| runner の責務 | 今ある場所 (移して消す) |
|---|---|
| 子の env を allowlist で組む | `provider_jail.py:133-170`, `build_print_cli_env` (`print_cli_profiles.py:380`), `agent_container.py:467-478`, chat の `_build_auth_env` の env 部分 |
| host の process: 空の cwd、process group、clean env の launcher | `native_claude.py` の empty dir と launcher (`native_claude.py:283-299,805-838`), `legacy_agent.py:52-130`, `print_cli_agent.py` の host 部分 |
| jail の container: 作成・mount・資源・停止確認 | `container_claude.py`, `container_codex.py`, `container_print.py` (3 つの launcher), `agent_container.py` (これは残して唯一の土台にする) |
| egress broker の起動と許可先 | `provider_egress.py`, `print_cli_egress.py`, `codex_egress.py` (Codex を素通しにすれば消える) |
| tool bridge (MCP・client tool・承認・sandbox_exec) | `mcp_broker.py`, `client_tool_bridge.py`, `native_claude.py:578-611` |
| 期限・出力上限・cancel・停止の証拠 | `native_claude.py` の watchdog, `legacy_agent.py:215` `_watch_limits`, `codex_stop_evidence.py`, `process_identity.py` |
| login home ごとの同時数 | `print_cli_agent.py` の `_HOME_SEMAPHORES`。connection の欄にする |

adapter に残るのは protocol だけである。Claude なら `ClaudeAgentOptions` の組み立て (`cli_path` は runner の launcher)、Codex なら app-server の RPC・dynamic tool・model catalog、print なら profile。

### 4.4 place backend

```python
class Place(Protocol):
    async def start(self, plan: LaunchPlan, allow: Allow, limits: Limits) -> RunnerProcess
    async def stop(self, proc: RunnerProcess, grace: float) -> StopEvidence   # confirmed / unconfirmed
    async def reattach(self, ownership: dict) -> RunnerProcess | None         # 再起動後の回収
```

- `HostPlace`: launcher で `execve` し、env を allowlist に置き換える。`allow.tools` が `none` 以外なら起動前に断る。
- `JailPlace`: `AgentContainer` で container を作り、`docker exec` で CLI を起こす。chat の会話は 1 会話 1 container (TTL は今の session の 1800 秒)。execution は 1 Attempt 1 container。
- `ChildPlace` (v2): §7。

### 4.5 会話と実行の上に載るもの

- **会話の継続**: 会話キー → `(RunnerProcess, AdapterSession)` を温かいまま持つ。今の `session.py` の pool と同じ役目。再開は adapter の `resume` (Claude は session id + Postgres の session store、`native_claude.py:293-299` は jail の中でもう動いている)。再開の前に、pin した `(adapter, model, connection, place, allow, tools digest)` が今の解決と一致するかを見る。違えば新しい会話にする (今の `X-Session-Event: created` と同じ通知)。
- **実行**: 今の admission・snapshot・Grant・workspace generation・Attempt・durable resume はそのまま上に載る。snapshot に入れる値を `(adapter id と版、model、connection binding digest、place、allow、tools digest、limits)` に揃える。`runtime_profile` / `backend` / `execution_environment` / `isolation` は snapshot から消す (ADR-0008 に従い CHECK 制約は起動時に作り直す)。
- **pool**: 候補の `(place, allow)` が一致することを読み込みで確かめる (今の `config.py:1191-1204` の place/surface を置き換える)。

### 4.6 今の backend の写像

| 今 | adapter | 写し方 | 消えるもの |
|---|---|---|---|
| chat の Claude (`runtime/claude.py`)、jail の Claude (`native_claude.py`)、container の Claude (`container_claude.py`) | `claude.agent-sdk` | `ClaudeSDKClient` + runner の launcher。allow.tools は `tools` / `allowed_tools`、MCP は in-process 中継 | `deny` 文字列・`bypassPermissions`・3 つの起動経路 |
| chat の Codex (`runtime/codex.py`)、Codex jail (`native_codex.py`)、container (`container_codex.py`) | `codex.app-server` | app-server RPC。jail では Codex 自身の sandbox は使わない (container が境界、今の `external_sandbox` と同じ)。host では allow none なので sandbox は read-only に固定 | `codex.sandbox`・`execution_environment`・bwrap の rootfs (§9 問い 1) |
| cli.print (`print_cli_agent.py`, `container_print.py`)、chat の agy (`runtime/antigravity.py`) | `cli.print:<profile>` | profile のまま | chat 用の agy runtime |
| chat の Gemini (`runtime/gemini.py`) | `cli.print:gemini` (新しい profile) | `gemini -p` の profile を足す。版と出力形は実測で決める (未確認) | `runtime/gemini.py` |
| chat の Cursor (`runtime/cursor.py`) | `cursor.sdk` か `cli.print:cursor-agent` | §9 問い 7 | どちらかが消える |
| `antigravity.sdk` (`runtime/antigravity_sdk.py`) | endpoint adapter か廃止 | API key / Vertex の endpoint なので、CLI ではなく endpoint の扱い | — |
| `/v1/conversations` (`conversation/provider.py`) | `endpoint.messages` | Kotoyose の process 内の HTTP。place は `host`、allow は `none` に固定 (client tool だけ) | — |
| legacy execution (`legacy_agent.py`, `legacy_agent_runner.py`) | なし | 各 adapter に吸収 | ファイルごと |

## 5. chat の閉じ込め

### 5.1 提案

- chat の既定は `place: host` + `allow.tools: none`。CLI は host で温かく動き、model は MCP と client tool しか使えない。遅延は今と変わらない。
- 読み書きや shell が要る chat alias は `place: jail` を明示する。会話ごとに container を 1 つ持つ。
- chat の入口も admission を通す。key の caller が無ければ chat も断る。`/v1/models` は caller の上限を通った alias だけ返す。
- Kotoyose の環境変数は、どの place でも CLI に渡らない。

### 5.2 alias ごとの行き先の例

本番の chat 用 alias は、設定では `agent: {place: isolated, surface: write}` と書かれている。しかし chat の経路はこの値を見ず、host で全 built-in tool を使える状態で動いている (§2.4)。設定に書かれた意図と実際の動きがずれていることが、この設計書が直す一番の点になる。

| alias | 経路 / driver | 今の道具 | 提案 |
|---|---|---|---|
| `assistant-chat`・`assistant-triage` | chat / claude.agent-sdk | 全 tool (文字列 deny)、MCP 4 | host + none、MCP はそのまま。ファイルが要るなら jail + read の別 alias |
| `assistant-briefing` | chat / claude.agent-sdk | Bash/Write/Edit 拒否 | host + none |
| `assistant-classify` | chat / claude.agent-sdk | 全 tool (文字列 deny) | host + none |
| `example-client-chat`・`example-client-test` | chat / claude.agent-sdk | 全 tool、MCP 3 | host + none |
| `kotoyose-oneshot` | chat / claude.agent-sdk | 全 tool | host + none (道具が要る試しは jail + write の別 alias) |
| `orchestrator-pm`・`orchestrator-decomposer` | chat / claude.agent-sdk | Bash/Write/Edit 拒否、PM は MCP 2 | host + none、MCP は Kotoyose が中継 |
| `orchestrator-task-agent` | chat / claude.agent-sdk | 全 tool、MCP 1 | jail + read |
| `orchestrator-structurer`・`orchestrator-sidecar-consult`・`orchestrator-sidecar-research` | execution / codex.app-server (codex-host) | host 実行 | 用途で jail + read か host + none |
| `orchestrator-review`・`orchestrator-review-judge`・`orchestrator-sidecar-prepare`・`orchestrator-sidecar-review`・`orchestrator-sidecar-risk` | execution / claude.agent-sdk | 今の execution の閉じ込めどおり | jail + read (review は作業場を読むだけ) |
| `orchestrator-coding`・`orchestrator-review-terra` | execution / codex.app-server (codex-chatgpt) | coding の Docker / 読み取りの部屋 | jail + write / jail + read |
| `agy-coding` | execution / cli.print | Docker + CONNECT の許可先 | jail + write (今の形そのまま) |
| `agy-hydrogen` | chat / antigravity.agy | host、全 tool | host + none |

### 5.3 壊れるものと直し方

| 使う側 | 壊れること | 直し方 |
|---|---|---|
| assistant (assistant-client) | アシスタントの会話で shell とファイル操作ができなくなる。key に caller が無いと 401/403 | 使っているかを owner が確認 (§9 問い 11)。要るなら jail + read の alias を別に作る。assistant 用の key を発行し caller を付ける |
| cron (`assistant-cron`) | 週次の refresh は jail の container で動く。`/etc` や他の home には書けない (今も書くべきでない) | `protect` と workspace を設定。初回は手で 1 回流し、memory.md の差分を見る |
| example-client | `fixed_conversation_key` の会話は続く。旧 bearer を使っているなら止まる | example-client 用の key を発行。会話の継続は pin の比較で 1 度だけ `created` になりうる (切り替え時に周知) |
| sample-client-key | 使う alias が未確認 | key を分け、使う alias を caller に列挙 |
| n8n | `X-Conversation-Key` はそのまま。key を差し替える | n8n の credential を新しい key に |
| OpenWebUI | `/v1/models` が caller の範囲だけ返すので、見える model が減る。画像入力は host + none の Claude で今のまま通る | OpenWebUI 用の caller に見せたい alias を列挙 |
| orchestrator | PM・structurer・decomposer は動作が変わらない。task-agent は jail に移る。execution 側は §8 S3 まで変わらない | orchestrator の割り当てを §8 の各段で同時に直す (ADR-0009) |

### 5.4 key ごとの alias 許可

- 1 クライアント 1 key。key の行は caller を必ず持つ (`kotoyose.api_keys.caller` を NOT NULL にする)。
- caller は `aliases`・`places`・`max_allow`・`workspaces`・`limits` を持つ (§3.1)。
- 全クライアントを移したら `legacy_bearer: false` にし、旧 bearer の経路 (`execution_http.py:46-80`) と「admin token で model を呼ぶ」逃げ道 (`execution_http.py:62-71`) を消す。移行の進み具合は `api_key_store.record_legacy` の数 (`api_key_store.py:119-129`) で見る。

## 6. 名前

使う人に見せる名前は、この 3 つの軸の値だけにする。

| 見せる名前 | 値 | 置き換える今の名前 |
|---|---|---|
| place | `host` / `jail` / `child:<名前>` | `isolated`・`sandbox`・`native` (どちらの意味でも)・`legacy`・`execution_environment`・`isolation` |
| allow.tools | `none` / `read` / `write` | surface `none/read/exec/write`、`permission_mode`、`deny`、built-in の `tools` 列、`codex.sandbox`、agy の `--mode` |
| allow.egress | `vendor` / host の列 / `open` | (今は無い。broker の固定 upstream と profile の `egress_hosts`) |
| 入口 | 会話 (`/v1/chat/completions`, `/v1/conversations`)・実行 (`/v1/executions`) | alias の `execution: legacy/native` |

画面では「場所: host / jail」「道具: なし / 読む / 書く」「出口: 提供元だけ / 追加 / 公開」と出す。

内部だけに残す名前: `bwrap`・`docker`・`room`・`cell`・`rootfs` の種類 (`standard` / `coding`)・execution mode (`sandbox_exec_v1` など)・`credential_mode` の値・broker の種類 (素通し / 注入)。これらは log と snapshot の診断に出てよいが、設定にも `/v1/models` にも出さない。`/v1/models` の `agent` 欄は `{place, allow, adapter}` にし、`enforced` は消す (host でも allow none を runner が強制するので、常に強制されている)。

## 7. child への伸び方 (v2、方向だけ)

- `place: child:<名前>` は「その機械の runner に RunPlan を渡す」こと。child の上でも place は `host` / `jail` の保証を持ち、child の place backend が与える。alias から見れば `child:win1` の中身は child の宣言 (`jail` が出来るか、どの adapter があるか) で決まる。
- ログインは child の上にある。公式 CLI は child の自分の home で動き、token を host へも child へも運ばない (ADR-0001)。console のログインは今の PTY 中継 (ADR-0006) を child まで延ばす。host が持つのは connection の定義 (`on: child:win1`) と残量の写しだけ。
- 今の remote worker (`remote_worker_*.py`) は command 専用で、server 側は Docker しか無く (`remote_worker_server.py:20`)、作業場は共有マウント前提 (`execution_resources.py:838-863`)。child にするには、(1) agent の Run を受ける口 (RunPlan を受けて adapter を起こす)、(2) event の stream を host へ返す口 (今の Execution SSE の形)、(3) 共有マウントでない作業場の受け渡し (git の bundle か artifact)、(4) ログインと残量の中継、(5) adapter と CLI の版の申告、が要る。mTLS の接続と journal は今のものを使える。
- Windows: bwrap も Linux の Docker も無い。child の `jail` を何で作るか (Windows Sandbox・Hyper-V の VM・WSL の container・機械そのものを使い捨てにする) は未調査。最初は Windows child を `host` + `none` と「機械自体が境界」の申告だけで始め、`jail` は後で足すのが現実的。browser use / computer use は `allow.tools` の新しい段 (例: `desktop`) として child だけに許す。
- Kotoyose を MCP サーバーにする道具は、`run(alias, input, workspace_ref, place?)` を 1 つ公開し、呼んだ key の caller 上限で admission する。place を指定できるのは caller が許す範囲だけ。結果は Execution の SSE をそのまま MCP の進捗にする。

## 8. 移行の段取り

各段の共通の手順: (a) `config.yml`・execution の JSON・`kotoyose.api_keys` の控えを取る、(b) S0 の道具で全 alias の解決後の設定を旧新で出して突き合わせ、意図した差分だけであることを確かめる、(c) 呼び出し元の割り当てを同時に直す、(d) クライアントごとに 1 回ずつ呼んで確かめ、log の 401/403 を見る。

| 段 | やること | 消すもの | 確かめ方 | 危険度 |
|---|---|---|---|---|
| S0 | 解決後の設定を出す道具。alias ごとに `(adapter, model, connection, place 相当, 実効の tool, env の名前, mount, 出口, 上限)` を正準 JSON で出す。今のコードで本番の基準を取る (所有者が実行) | なし | 出力が全 alias で揃うこと。recipes と本番の差が分かること | 低 |
| S1 | 全入口 (chat・`/v1/models`・conversations・executions) で caller の `aliases` を見る。key ごとに caller を付けて発行、クライアントを順に移す。最後に `legacy_bearer: false` | 旧 bearer の経路、caller 無しの key、admin token で model を呼ぶ逃げ道 | 旧 token の利用数が 0。各クライアントの smoke | 中 (鍵の付け間違いで止まる。1 クライアントずつ) |
| S2 | host で起こす子 process を全部 clean env の launcher 経由にする (Claude chat の `cli_path`、Gemini、Codex chat、legacy runner) | `os.environ` の継承 (`legacy_agent.py:77`、SDK の合成) | 子の env を dump する試験。各 provider の smoke | 中 (proxy・PATH・node の場所など、隠れた依存が出る) |
| S3 | runner と `JailPlace` を入れ、execution の container 経路 (Claude coding、Codex read/coding、print jail) を 1 つの launcher に寄せる。挙動は変えない | `container_claude.py`・`container_codex.py`・`container_print.py` の起動部分の重複 | 既存の container 試験、orchestrator の coding と review の縦断 | 高 (orchestrator の本線) |
| S4 | 新しい設定の形 (§3.1) と変換 script。chat を runner の `HostPlace` に載せ、会話の alias を host + none に。jail の chat (`assistant-cron`・`orchestrator-task-agent`) と MCP 中継も同じ段で出す | alias の `execution`・`execution_environment`・`native`・`agent`・`permission_mode`・`deny`・`tools`・`bare`・`setting_sources`・`require_isolation`・`allow_cwd_override`・`cwd_allowed_roots`、`session.py` の provider 別起動 | S0 の差分が §5.2 の表どおり。アシスタントの会話・cron・PM・task-agent を 1 回ずつ。`X-Session-Event` の `created` が切り替え時の 1 回だけ | 高 (アシスタントの挙動が変わる。§9 問い 11 を先に決める) |
| S5 | legacy を消す。Gemini は `cli.print` の profile に、agy の chat は `cli.print` に、Cursor は問い 7 の結論に | `legacy_agent.py`・`legacy_agent_runner.py`・`legacy_runtime.py`・`LegacyRuntimeProfile`・`runtime/gemini.py`・`runtime/antigravity.py` (chat 用)・`agent_spec.py` の `_HOST_LEGACY` | Gemini・agy・Cursor の chat と execution の smoke | 中 |
| S6 | bwrap の provider jail を消す。Claude の surface none と Codex の exec を `JailPlace` (または host + none) に移す | `provider_jail.py`・`codex_provider_jail.py`・`codex_provider_scope.py`・`codex_rootfs*.py`・`scripts/native-claude`・`scripts/native-codex`・`native_worker.py` (sandbox_exec を残すなら Docker の worker だけにする) | orchestrator が Codex exec / Claude none を使っているかを先に調べる。使っていれば縦断 | 高 |
| S7 | Codex のサブスクを `config_dir` + 素通しに、`codex-host` と `codex-chatgpt` を 1 つに (ADR-0004)。問い 4 の結論で Claude の setup-token も素通しに | `codex_egress.py` の注入、`codex_subscription.py` の auth.json 読み、`auth_file` 欄 | 許可先をログイン更新まで含めて実測 (agy と同じやり方)。jail で 1 日動かして更新が通ること | 中 |
| S8 | 名前を揃える。`/v1/models` の `agent` 欄、console、snapshot の欄名。DB の CHECK を作り直す (ADR-0008) | `isolation`・`backend`・`sandbox` などの公開語彙 | 古い CHECK から始める DB 試験 | 低 |

最初の実装段階 (ADR-0010 build step 1: `process_env.scrub`) は、`.env` と設定の読み込み直後に秘密らしい変数名および設定が参照する環境変数名を Kotoyose 内のモジュール辞書へ移して `os.environ` から削除し、SDK や子 process による `os.environ` 継承からの偶発的な秘密漏洩を防ぐ。一方で、systemd の `Environment=` で起動時に渡された変数は Kotoyose が `os.environ` から消しても同一 OS ユーザーから `/proc/<pid>/environ` で読める状態が残り、`.env` 由来の機微情報も含め同一ユーザー上の agent に対する本質的な防御にはならない。実効的な遮断は後続段の confinement (container / 名前空間による隔離) が担う。

S4 は大きい。会話の alias (host + none) と jail の chat を分けて出すと、その間だけ旧 chat 経路が jail 向けの alias のために残る。ADR-0009 は同居を作らないと決めているので、開発は 2 つの PR に分けても、配備は 1 回にまとめることを勧める。

## 9. 未決の問い

1. **jail の実装を 1 つにするか**
   - (a) Docker だけ、(b) bwrap だけ、(c) 両方を内部で使い分け。
   - 推奨: (a)。network none・ro root・資源の上限・image・egress socket が揃っていて、coding と agy で本番に出ている。bwrap の Claude jail は network を共有していて弱い。欠点は起動の遅さと同時数 (例の `max_containers: 2`)。chat の container は execution と別の枠にする。
2. **host で許す allow を `none` だけにするか**
   - (a) none だけ、(b) read まで、(c) 今のように何でも。
   - 推奨: (a)。host の保証を「model の手は host に届かない」の 1 文にできる。CLI 自身の sandbox (Codex の workspace-write、agy の mode) は CLI ごとに意味が違い、runner が保証できない。
3. **`exec` (command を credential の無い別 container で走らせる) を残すか**
   - (a) 畳んで read / write だけ、(b) `allow.tools` の値として残す、(c) jail の性質 (`credential_sealed: true`) として残す。
   - 事実 (運用担当が確認、2026-11-15): orchestrator は `builtin:sandbox-exec@1` を今も使っている。過去 60 日の execution で 43 回 (最後は 11-13)。`host-coding@1` は 116 回、`workspace-read@1` は 5 回。orchestrator の定義は `apps/api/src/services/agent-tool-refs.ts:9-11`。
   - 推奨 (改): (a) は orchestrator を壊すので採らない。(b) か (c) で残す。どちらにするかは問い 4 と一緒に決める。
4. **サブスクの token を jail に入れるか**
   - (a) CLI が持ち、出口は素通し (model の shell から読める)、(b) broker が注入 (ADR-0001 の intermediate に触れるおそれ)、(c) CLI と shell を別 uid にする (CLI が子を起こすので難しい)。
   - 推奨: (a)。ADR-0001 が最優先で、出口を vendor の許可先に絞れば持ち出し先は限られる。API key は常に (b) で jail に入れない。所有者に「token が model の shell から見える」ことを承知してもらう必要がある。
5. **caller の上限を超える alias を下げて動かすか、断るか**
   - 推奨: 断る (403、一覧に出さない)。要求側が下げるのは許す。
6. **caller の上限をどこに置くか**
   - (a) `config.yml` の `callers:` に移す、(b) 今の execution JSON のまま。
   - 推奨: (a)。chat が読むのは `config.yml` で、2 か所あることが「chat は絞っていない」の原因になっている。JSON には機械の材料 (image・drivers・container の上限) だけを残す。
7. **Cursor をどうするか**
   - (a) Cursor SDK の adapter を残す (jail で動くかは未確認)、(b) `agent -p` を `cli.print` の profile にする (ROADMAP v2 の項目)、(c) 外す。
   - 推奨: (b)。決まるまでは `host` + `none` だけ許す。
8. **Codex のサブスクの移行を v1 に入れるか**
   - 推奨: v1 に入れる (S7)。ADR-0001 の残りを v1 で閉じられる。許可先の実測で詰まったら v2 に送る。
9. **jail の chat の container の寿命**
   - (a) 会話ごとに 1 つ (TTL は session と同じ)、(b) alias ごとの pool で使い回す。
   - 推奨: (a)。会話をまたいで作業場や home が混ざらない。数が問題になったら TTL を短くする。
10. **`allow.egress: open` を許すか**
    - 推奨: 値としては持つが、caller の上限に `open` を持つ caller だけが使える。既定の caller には付けない。
11. **アシスタントの会話は shell とファイルを使っているか**
    - 所有者の確認が要る。使っていなければ host + none。使っているなら、どのファイルかを聞いて jail + read の別 alias にする。
12. **print 系 CLI の jail で MCP を断るか**
    - (a) 断る、(b) CLI の MCP 設定で Kotoyose の中継 server を指す (loopback を開ける必要がある)。
    - 推奨: (a)。agy の MCP の要望が出たら、Kotoyose 側の stdio 中継を jail の中に置く形を別に設計する。
13. **`jail` という名前**
    - (a) `jail`、(b) `sandbox`、(c) `box`。
    - 推奨: (a)。ADR-0010 と ROADMAP が既に使っていて、`sandbox` は Codex の sandbox や `isolation: sandbox` と混ざる。
14. **Claude の `config_dir` (公式 `/login`) を jail で使えるようにするか**
    - 今は断っている理由がコードから読めない。agy と同じく login home を bind すれば ADR-0001 の (1) で動くはずだが、ログインの更新と同時実行の挙動は未確認。
    - 推奨: 実測してから決める。それまで Claude の jail は setup-token (`claude-oauth`) だけ。

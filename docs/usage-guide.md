# Kotoyose 利用ガイド

Kotoyose は Claude を OpenAI 互換 API でラップする AI 実行ハーネスです。
Open WebUI / n8n / curl など、OpenAI 互換クライアントからそのまま接続できます。

---

## 1. エンドポイント一覧

| メソッド | パス | 説明 |
|----------|------|------|
| POST | `/v1/chat/completions` | チャット補完 (メイン) |
| GET | `/v1/models` | モデル一覧 |
| GET | `/health` | ヘルスチェック |
| GET | `/docs` | Swagger UI (対話的 API ドキュメント) |
| GET | `/openapi.json` | OpenAPI スキーマ (JSON) |

---

## 2. 認証

管理画面 (`/admin/ui`) の Basic 認証には `KOTOYOSE_BASIC_USER` と `KOTOYOSE_TOKEN` を使います。`KOTOYOSE_TOKEN` は管理者パスワードです。

モデル利用には管理画面の **API keys** でクライアントごとにキーを発行し、Bearer ヘッダーに指定します。

```text
Authorization: Bearer <API key>
```

キーは発行時に一度だけ表示され、サーバーには SHA-256 ハッシュのみ保存されます。キー名はクライアントの識別に使います。すべての API キーには `config.yml` の `clients` に設定済みの Client を結び付ける必要があり（Client なしのキー発行は 422、Client なしの既存キーは認証時に 401 で再発行を促します）、`/v1/chat/completions`・`/v1/models`・`/v1/conversations` を含むモデル入口と native execution のすべてがその Client の `models`・`places`・`max_surface` の範囲に制限されます。`workspace_roots` が空 (`[]`) のチャット専用 Client は workspace を要する `/v1/executions` や `/v1/workspaces` を呼べません（403）。失効は即時に反映され、失効済みキーは削除できます。

### Clients の設定 (`config.yml` / Endpoint 画面)

Client の定義は `config.yml` の `clients:` に置きます（名前は `^[a-z0-9][a-z0-9-]{0,63}$`）。管理画面 Console の **Endpoint** ページ（`/admin/clients` API）から再起動なしで作成・更新・削除できます（有効な API キーが結び付いている間の削除は 409 で拒否されます）。旧 `execution.json` の `callers` は廃止され、残っているファイルは起動時に拒否されます。

### 2026-11-20 の更新前に必要な移行

| ファイル | 旧キー | 対応 |
|----------|--------|------|
| `config.yml` | `defaults` | 必要な preset 設定を `models.<alias>` に移し、`defaults` を削除。呼び出し側の `model` を登録済み alias に変更 |
| `config.yml` | `models.<alias>.execution` | `legacy` を `direct` に書き換え（`native` は維持）。Agent として公開する alias は `route` と `agent: {place, surface}` を設定 |
| `config.yml` | `clients.<client>.token_env` | 削除。Endpoint で該当 Client にひも付くキーを発行し、各呼び出し側の Bearer トークンを切り替え |
| `config.yml` | `runtime_profiles` | 削除。Agent は `routes` と alias の `route`・`agent` で構成 |
| `config.yml` | `skill_profiles`、`mcp_profiles` (トップレベルと alias 内) | 削除。個別リソースを `skill_definitions`・`mcps` に登録し、alias の `skill_refs`・`mcp_refs` で参照 |
| `config.yml` | `models.<alias>.system_prompt_files` | パスを `prompt_files` に登録し、alias の `prompt_file_refs` に元の順序で並べてから旧キーを削除 |
| `config.yml` | `models.<alias>.mcp_servers.<name>` の URL のみの定義 | `type: http` または `type: sse` をサーバーの方式に合わせて明示 |
| `execution.json` | `legacy_bearer` | 値にかかわらず削除。モデル用 Bearer 認証には Client API キーを使う |
| `execution.json` | `runtime_profiles`、`agent_sandbox_profiles` | 削除。モデル選択は `config.yml` の `routes` と `models.<alias>.route`、Agent の実行方式は `models.<alias>.agent` で定義 |
| `execution.json` | `agent_cli_binary` | 削除。必要な CLI パスを `drivers.<driver>.cli_binary` へ移す |
| `execution.json` | `agent_credential_mode`、`agent_credential_env`、`agent_credential_name` | 削除。認証は `config.yml` の `connections` で設定 |
| `execution.json` | `callers` | 削除。呼び出し元の設定を `config.yml` の `clients` に移す |
| `execution.json` | `worker_capabilities.local-legacy-agent` | 明示設定している場合は削除。host の print CLI 用には `local-print-cli` が自動登録される |

`KOTOYOSE_TOKEN` は管理画面の Basic 認証パスワードとして使います。orchestrator がセッションを復旧するときは、Client API キーで `GET /v1/sessions/{conversation_key}` を呼びます。参照できる interaction は同じキー名で記録されたものです。

DB 未設定時の API キーはメモリに保持され、再起動すると消えます。認証なしモードは開発専用です。

旧管理 UI は 2026-11-20 に撤去しました。管理画面は `/admin/ui` のコンソールです。

---

## 3. curl からの呼び出し

### 3.1 通常の会話 (同期)

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KOTOYOSE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kotoyose-chat",
    "messages": [
      {"role": "user", "content": "今日のタスク整理を手伝って"}
    ]
  }'
```

### 3.2 ストリーミング

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KOTOYOSE_API_KEY" \
  -H "Content-Type: application/json" \
  --no-buffer \
  -d '{
    "model": "kotoyose-chat",
    "messages": [
      {"role": "user", "content": "長めの回答をストリーミングで"}
    ],
    "stream": true
  }'
```

SSE (Server-Sent Events) 形式で `data: {...}` が順次返されます。
最後に `data: [DONE]` が送信されます。

### 3.3 画像入力

Open WebUI と同じ OpenAI 互換の `image_url` content part を送信できます。
Claude / Codex / Cursor provider が対応し、Codex は base64 data URL のみ受理します。
Antigravity provider は画像を黙って破棄せず `400` を返します。
1 画像は最大 20 MiB、1 message は最大 20 画像です。runtime へ送るのは最新の user message で、過去履歴は session から復元されます。

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KOTOYOSE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kotoyose-chat",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "この画像を説明してください"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
      ]
    }]
  }'
```

### 3.4 会話キー指定 (セッション継続)

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KOTOYOSE_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Conversation-Key: my-session-001" \
  -d '{
    "model": "kotoyose-chat",
    "messages": [
      {"role": "user", "content": "さっきの続きだけど..."}
    ]
  }'
```

同じ `X-Conversation-Key` を使えば、前回の会話文脈を引き継ぎます。

---

## 4. n8n からの呼び出し

### 4.1 HTTP Request ノードの設定

| 項目 | 値 |
|------|-----|
| Method | POST |
| URL | `http://<kotoyose-host>:8000/v1/chat/completions` |
| Authentication | Header Auth |
| Header Name | `Authorization` |
| Header Value | `Bearer <API key>` |
| Body Content Type | JSON |

### 4.2 Body (JSON)

```json
{
  "model": "kotoyose-chat",
  "messages": [
    {
      "role": "user",
      "content": "{{ $json.message }}"
    }
  ]
}
```

### 4.3 cron トリガー + HTTP Request の接続例

```
[Cron Trigger] → [HTTP Request]
  毎朝 8:00          POST /v1/chat/completions
```

**Cron Trigger 設定:**
- Mode: Every Day
- Hour: 8
- Minute: 0

**HTTP Request Body:**

```json
{
  "model": "kotoyose-news",
  "messages": [
    {
      "role": "user",
      "content": "今日の主要ニュースを要約してください"
    }
  ]
}
```

**ヘッダ追加 (Send Headers → Add Header):**

| Name | Value |
|------|-------|
| `X-Conversation-Key` | `cron:morning-briefing:{{ $now.format('yyyy-MM-dd') }}` |

---

## 5. Discord webhook (n8n 経由)

Discord bot を常駐させず、webhook で n8n が中継するアーキテクチャです。

### 5.1 フロー

```
[Discord Trigger] → [Function] → [HTTP Request] → [Discord (Send)]
  webhook 受信      メッセージ加工    Assistant 呼出     応答返却
```

### 5.2 Discord Trigger 設定

n8n の Discord Trigger ノードで webhook を受信します。

### 5.3 Function ノード (メッセージ加工)

```javascript
// Discord の webhook payload からメッセージとチャンネル ID を抽出
const channelId = $input.first().json.channel_id;
const message = $input.first().json.content;

return {
  json: {
    model: "kotoyose-chat",
    messages: [
      { role: "user", content: message }
    ],
    headers: {
      "X-Conversation-Key": `discord:channel:${channelId}`
    }
  }
};
```

### 5.4 HTTP Request ノード

| 項目 | 値 |
|------|-----|
| URL | `http://<kotoyose-host>:8000/v1/chat/completions` |
| Headers | `X-Conversation-Key: {{ $json.headers["X-Conversation-Key"] }}` |
| Body | `{ "model": "{{ $json.model }}", "messages": {{ $json.messages }} }` |

### 5.5 Discord Send ノード

HTTP Request の応答から `choices[0].message.content` を取り出して Discord に返します。

---

## 6. Open WebUI からの接続

### 6.1 設定

1. Open WebUI の Settings → Connections を開く
2. OpenAI API の接続を追加:
   - **API Base URL**: `http://<kotoyose-host>:8000/v1`
   - **API Key**: 発行した API キー
3. 保存後、モデル選択で `kotoyose-chat` などが表示される

### 6.2 動作

- Open WebUI は `X-Conversation-Key` ヘッダを送らない
- Kotoyose は messages 履歴のハッシュで会話を自動追跡 (v2 ハッシュパス)
- ストリーミング (`stream: true`) に対応済み

---

## 7. MCP サーバーの設定

`config.yml` でモデルごとに MCP サーバーを宣言できます。
Claude SDK が起動時に MCP サーバーへ接続します。

### 7.1 HTTP / SSE 型

```yaml
models:
  kotoyose-chat:
    mcp_servers:
      mem0:
        type: http
        url: "http://mem0-host:3001/mcp"
      memU:
        type: sse
        url: "http://memu-host:3002/sse"
```

### 7.2 stdio 型

```yaml
models:
  kotoyose-dev:
    mcp_servers:
      filesystem:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/srv/sample-user/docs"]
      custom-tool:
        command: "python"
        args: ["my_mcp_server.py"]
        env:
          API_KEY: "xxx"
```

### 7.3 対応する MCP サーバータイプ

| type | 説明 | 必須フィールド |
|------|------|-------------|
| `http` | HTTP Streamable トランスポート | `url` |
| `sse` | SSE トランスポート | `url` |
| (省略) | stdio トランスポート | `command` |

`headers` (dict) を追加で指定可能 (http / sse のみ)。
`env` (dict) を追加で指定可能 (stdio のみ)。

---

## 8. 会話キーの設計ガイド

会話キーはセッションを一意に識別する文字列です。
推奨フォーマット: `<source>:<scope>:<id>`

### 例

| 用途 | 会話キー例 |
|------|-----------|
| Discord チャンネル | `discord:channel:123456789` |
| Discord スレッド | `discord:thread:987654321` |
| cron: 朝のブリーフィング | `cron:morning-briefing:2026-04-13` |
| cron: 日記壁打ち | `cron:journal:2026-04-13` |
| 手動タスク | `task:deploy-review:001` |

### ルール

- **ヘッダあり**: `X-Conversation-Key` の値がそのままセッションキーになる
- **ヘッダなし**: messages 履歴のハッシュで自動生成 (Open WebUI 用)
- 同じキー → 同じ Claude セッションに接続 (会話継続)
- 異なるキー → 別のセッション (新規会話)

---

## 9. レスポンス形式

### 9.1 成功 (200)

```json
{
  "id": "chatcmpl-abc123def456",
  "object": "chat.completion",
  "created": 1712707200,
  "model": "kotoyose-chat",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "承知しました。今日のタスクを整理しましょう。"
      },
      "finish_reason": "stop"
    }
  ]
}
```

### 9.2 エラー

```json
{
  "error": {
    "message": "Claude SDK error: connection refused",
    "type": "internal_error",
    "request_id": "req_abc12345"
  }
}
```

| コード | 意味 |
|--------|------|
| 400 | リクエスト不正 (messages 空など) |
| 401 | 認証失敗 |
| 500 | Claude SDK 内部エラー |
| 503 | Claude SDK 接続失敗 |

---

## 10. Agent 実行の設定

chat と Agent の alias は `route` で共通の接続先を参照する。route を持つ alias に provider、model、Claude の endpoint や credential を重ねて書かない。
Agent Attempt として動かす alias は、さらに `agent`（`place` と `surface`）を宣言する。
`routes` が driver、model、credential を持ち、機械の `drivers` が CLI と認証ファイルを持つ。Codex の rootfs と egress limits は `cells.standard` と `cells.coding` に分ける。
Client は `models`、`places`、`max_surface` で許可する。
`runtime_profiles`、`agent_sandbox_profiles`、`agent_cli_binary`、`agent_credential_*`、`callers`（`execution.json`）は読み込めない。
Codex の `write` cell を `isolated` で解く alias は、容器を境界にして `execution_environment=isolated` を残す。`codex.sandbox` はそのとき働かない SDK 別名として pin し、`full-access` だけ `danger-full-access` に読み替える。jail（`exec`）は `execution_environment` も `codex_sandbox` も持たない。

### 10.1 公式の口と risk_acks (段階 1: 判定と印のみ)

各 provider は既定で公式の口だけから使う。route が外れる理由は `sanction_reasons` が返す。
`unofficial_endpoint`（Claude Agent SDK を Anthropic 以外の endpoint へ向ける）、`cross_provider_model`（driver の provider と model の家族が違う）、`custom_model_provider`（Codex route が `model_provider` を持つ）。

route の判定は 3 つ。`official`（理由なし）、`acknowledged`（理由があり、一致する承諾がある）、`needs_ack`（理由があり、承諾がない）。
承諾は config.yml の最上位 `risk_acks` に書く。

```yaml
risk_acks:
  - route: router            # routes の名前。存在しない名前は読込時に落ちる
    digest: <64 桁の hex>     # 下記。route を編集すると一致しなくなり、承諾は外れる
    reasons: [unofficial_endpoint]
    acknowledged_by: owner
    at: 2026-11-14T00:00:00+09:00   # ISO 8601
    note: 任意
```

`digest` は SHA-256。入力は route の `driver`、`endpoint`（無ければ null）、`model`、credential の種類（`env:NAME` は `env`、それ以外は credential 名のまま）、`model_provider` を、キー順固定・空白なしの JSON にしたもの。env 変数名は digest に入らない。
貼る行は `uv run python scripts/sanction_candidates.py config.yml` が出す。`acknowledged_by` と `at` は埋めてから使う（`at` は埋めるまで検証で落ちる）。credential の値は読まない。

段階 1 は印を付けるだけで、何も止めない。
`/v1/models` と `/admin/models` の `agent`、`/admin/config` の `route_sanctions` に `sanction` と `sanction_reasons` が付く（`/v1/models` と `/admin/models` は `official` の alias では省略する。無い = 公式）。
Attempt の受理時に、`official` でなければ snapshot へ任意の `sanction`（`{status, reasons}`）を残す（無い = 公式。既存 snapshot の pin を動かさないため）。参考情報で、resume の同一性比較には入らない。`needs_ack` の route を使うと、プロセスごとに route 1 つにつき 1 回だけ警告ログ（`route.needs_acknowledgement`）を出す。
`NativeExecutionConfig.sanction_enforcement`（`off` 既定 / `agent` / `all`）は休眠スイッチ。`agent` は `needs_ack` の route への Agent 受理を `route_needs_acknowledgement`（403）で断り、`all` は chat completions も断る。機械ごとの方針なので、admin UI が書き換える config.yml の `settings` ではなく Native 実行設定に置く。

### 10.2 pool alias (候補から 1 つを受理時に選ぶ)

Hub は用途ごとに 1 つの名前を選び続け、Kotoyose がその名前の候補 alias から、利用枠の状況で 1 つを選ぶ。`models` に、`route` も `agent` も持たない次の形の項目を書く。

```yaml
models:
  coding-a: {route: claude-main, agent: {place: isolated, surface: write}}
  coding-b: {route: codex-sub, agent: {place: isolated, surface: write}}
  coding:                       # pool alias
    candidates: [coding-a, coding-b]
    policy: first_available     # first_available | balanced
    requires: [images]          # 任意。候補の driver が満たせるものだけ選ぶ
```

読込時に落ちるもの: 候補が無い、候補が存在しない、候補が pool alias (入れ子)、候補に `agent` が無い、候補の `agent.place` か `agent.surface` が揃っていない、未知の `policy`、未知の `requires`。pool alias が 1 つも無い config は、これまでと何も変わらない。

**pool と状態。** route の `pool`（無ければ route 名）が 1 つの利用枠を表す。状態は `available | limited(until) | auth_expired | unknown` で、`kotoyose.model_pool_state` に保存する（スキーマは `ExecutionStore.initialize` が流す）。Kotoyose が既に持つ証拠でだけ更新し、問い合わせはしない。
- attempt が provider の利用枠の観測つきで終わると `limited`。`until` は観測された reset に 5 秒足した時刻。reset が無ければ観測時刻から 15 分（hub の probe backoff の最長）。
- その pool の attempt が成功すると `available`。`until` を過ぎた `limited` は使えるものとして扱う。
- 受理中の解決が `authentication_failed`（Codex の ChatGPT login が無い）で落ちると `auth_expired`（5 分後に自然に再試行される）。それ以外の認証失敗には正規化された信号が無いので、この状態にはならない。
- 書き込むのは pool alias の候補が使う pool だけ。chat の legacy 経路には利用枠の正規化が無く、証拠にならない（native 経路は attempt なので証拠になる）。

**戦略。** `src/kotoyose/pool_strategies.py` の registry（`名前 -> (候補, 文脈) -> 並べた候補`）。`first_available` は使える（`limited`/`auth_expired` の `until` が過ぎている、または `available`/`unknown`）かつ `requires` を満たす最初の候補。`balanced` は最後に選ばれた時刻が古い順（未選択が先）で、選択履歴は `kotoyose.model_pool_selections` に pool alias の排他の下で記録するので、再起動と同時の受理に耐える。戦略は `@register_strategy("名前")` を持つ関数を足すだけで増やせる（受理側のコードは触らない）。

**選択。** pool alias を名指した Agent の受理（Hub の Execution 作成を含む）と chat で、受理時に 1 回だけ候補 1 つに解き、以後は「その候補が名指された」と同じ検査に進む（権限は pool alias の名前に対して見る）。選んだ候補は attempt の snapshot の任意 key `resolved_model` と `requested_model` に pin し、resume は pin された候補だけを使って選び直さない（pin が無い元 attempt の resume は `resume_unsafe` / `runtime_mismatch`）。Hub が predecessor で safe_resume するときも predecessor が選んだ候補を使う。動いている attempt は別の候補へ移さない。`describe` と chat の応答には `resolved_model`（chat は `X-Resolved-Model` header も）が付く。`sanction_enforcement` が断る設定のとき（agent 受理は `agent`/`all`、chat は `all`）、`needs_ack` の route を持つ候補は選ばない。全候補が使えなければ `429 capacity_exhausted`（`reason_code: provider_usage_limit`）を返し、本文に最も早い回復時刻 `recovery_at` を含める。`requires` や sanction で候補が 1 つも残らないときは `422 pool_no_candidate`。

**公開。** `/v1/models` は pool alias を、候補が共有する `agent.place`/`surface` と、`candidates`・`policy` つきで出す。`agent.provider`/`driver` は現在の第一候補のもの（状態を読めない、または使える候補が無いときは config で最初の候補）。`sanction` は最も重い候補に従う。Client の `models` に pool alias の名前が要り、候補の `places`/`max_surface` はどれか 1 つが通ればよい。`GET /v1/pools`（`/v1/models` と同じ認証、読み取りのみ）が pool alias ごとの `policy`・`first_choice`・候補の状態（`alias`/`pool`/`state`/`until`/`last_selected_at`）を返す。
`runtime-authentication` の解決（`resolve_runtime_authentication`）は、どの候補を使うか決まる前なので pool alias を受け付けない。

### 10.3 管理 UI から設定を編集する (route / alias / pool / clients / 承諾)

`/admin/ui` から、config.yml を手で書き換えずに route・alias・pool alias・client・risk 承諾を編集できる。保存は MCP などと同じ経路（複製 → 変更 → `save_config` の全体検証 → config.yml の書き込みと再読込）で、読込時の規則に反する変更は 422 で拒否され、config.yml は変わらない。保存しても無関係な項目（mcps・settings・skill など）は意味を変えない（既定値は書き出される）。機械ごとの設定（`execution.json` の driver・`sanction_enforcement`）は UI の対象外で、これまでどおりファイルで編集する。

サーバーパス欄では **Browse…** でファイルやディレクトリを選択できます。パスの直接入力もできます。ブラウズ範囲はサービスのホーム、alias の `cwd` / `cwd_allowed_roots`、および `KOTOYOSE_BROWSE_ROOTS` で指定したディレクトリです。

| 画面 | API (すべて `/admin` 配下) | 内容 |
| --- | --- | --- |
| ROUTES | `POST/PUT/DELETE /admin/routes/{name}` | driver は driver 表から選ぶ。`credential` は `env:NAME` か credential 名で、トークンや API key の値は書かない（UI は秘密らしい値を拒否する）。検証は `AgentRoute` |
| Machine | `POST /admin/reload` | **Reload config.yml** でディスク上の設定を読み込み、コンソールの表示を更新 |
| Secrets | `GET /admin/settings`、`PUT /admin/settings` | **Server settings** で Anthropic base URL と認証トークンを設定。トークンは設定状態のみ表示し、空文字で解除 |
| ROUTES | `POST /admin/routes/{name}/rename` | route 名と、それを指す alias・`risk_acks` を 1 回の保存で変更する。digest は route の名前を含まないので、改名しても承諾は有効のまま |
| ROUTES | `DELETE /admin/routes/{name}` | alias または、その alias を候補に持つ pool alias が使っている間は 409 で、参照元を列挙して拒否する。消せるときは、その route への承諾も一緒に消える |
| MODELS | `POST/PUT /admin/models`、`POST /admin/models/{id}/rename` | alias は `route` と `agent: {place, surface}` を編集する。選べる place / surface は route の driver が受け付ける組み合わせ（`agent_spec` の表）。route を持つ alias には provider / model / endpoint / token の欄は出ない。route のない alias では直接指定欄を使う。alias の改名は pool alias の候補も追従させ、pool alias の候補になっている alias の削除は 409 |
| POOLS | `POST/PUT/DELETE /admin/pools/{name}` | 候補（優先順）・`policy`（戦略 registry から選ぶ）・`requires`。候補の存在・agent の有無・place と surface の一致・入れ子の禁止は読込時と同じ規則で検証する。名前は alias と衝突できない |
| ROUTES | `POST/DELETE /admin/routes/{name}/ack` | `needs_ack` の route に、その時点の digest と理由を持つ承諾を記録する（Acknowledge risk）。`acknowledged_by` は認証した admin の名前、`at` は UTC の現在時刻、`note` は任意。Revoke は承諾を消す。公式の route への承諾と、有効な承諾への二重記録は 409 |

route の判定（`official` / `acknowledged` / `needs_ack` と理由）は ROUTES の画面と `GET /admin/config` の `route_sanctions` に出る。route を書き換えて digest が合わなくなった承諾は `ack_stale: true` で、画面に「古くなっている」と出て再承諾できる（再承諾は古い記録を置き換える）。env 変数名だけの変更は digest を動かさない。
承諾を書く経路は `/admin` の Basic 認証（`KOTOYOSE_BASIC_USER` と `KOTOYOSE_TOKEN`）だけで、agent や Client の Bearer token では 401 になる。選択肢（driver 表・戦略・`requires`）は `GET /admin/config` の `catalog` が返す。

## 11. Encrypted Secrets Store

API key・MCP header/env・endpoint 認証鍵などの秘密値は、`config.yml` に平文で置かず AES-256-GCM で暗号化して `kotoyose.secrets` に保存し、名前で参照できる（write-only: 管理 API も管理 UI も一度保存した秘密値を読み返せない）。

> **認証必須・管理者の信頼境界**:
> - `KOTOYOSE_TOKEN` が未設定（Basic 認証無効）の場合、起動時に secret store の有効化を拒否する（秘密値を認証なしで登録・削除させないため）。
> - 管理者は信頼された主体として扱う（管理者が MCP probe や route の endpoint に任意のホストを指定すれば、保存済み secret をそのホストへ送信できる）。

### Master key の作成 (ファイルまたは systemd credential)

Kotoyose は起動時に 32 バイトの master key を次の順で読み込む。1・2 のどちらも未設定の場合は 3 のデフォルト場所を使用する（ファイル未作成なら起動時に自動生成し、バックアップを促す警告ログを 1 行出力する）。

1. **systemd credential**: `$CREDENTIALS_DIRECTORY/kotoyose-master`
   `systemd` ユニットの `LoadCredential=kotoyose-master:/etc/kotoyose/master.key` などで渡す（ファイル権限は `0400` または `0600`、シンボリックリンク不可）。
2. **key file**: 環境変数 `KOTOYOSE_MASTER_KEY_FILE` が指すファイル（権限 `0600`・実行ユーザー所有・シンボリックリンク不可が必須。緩い権限や所有者不一致では secret store の起動を拒否する）。
3. **デフォルト key file**: `${XDG_DATA_HOME:-~/.local/share}/kotoyose/master.key`
   存在しない場合は起動時にディレクトリを `0700`、ファイルを `0600`（32 バイト乱数、`O_CREAT|O_EXCL`）で自動作成する。既存の場合は 2 と同じ検査を行って読み込む。

新しい key file を手動作成するには同梱スクリプトを使う（既存ファイルの上書きは拒否する）:

```bash
uv run python scripts/create_master_key.py /etc/kotoyose/master.key
```

### Master key のバックアップとローテーション

- **バックアップ**: `kotoyose.secrets` の暗号文はすべてこの master key（SHA-256 先頭 8 hex 文字の `key_id` と行名・version を AAD に束縛）でのみ復号できる。**key file を失うと保存済み secret はすべて永久に復号できなくなる**ため、自動作成された場合も含め安全なオフライン保管先へ必ずバックアップすること。
- **ローテーション**: **必ず先に Kotoyose サーバーを停止してから**（stop the server first）新しい key file を作成して全行を再暗号化し（トランザクション内で `LOCK TABLE kotoyose.secrets IN ACCESS EXCLUSIVE MODE` を取得）、`KOTOYOSE_MASTER_KEY_FILE` / systemd credential / デフォルト key file を切り替えて**後から再起動する**（restart after）:

```bash
# 1. Stop the Kotoyose server first
uv run python scripts/create_master_key.py /etc/kotoyose/master.key.new
uv run python scripts/rotate_master_key.py --old-key /etc/kotoyose/master.key --new-key /etc/kotoyose/master.key.new
# 2. Switch the key file / configuration and restart the Kotoyose server after
```

### 既存キーからの移行 (`secret:` / `{"secret": "NAME"}`)

- **Console Secrets 画面**（`/admin/ui` → `Secrets`）下部の **Not managed — move to Secrets** に、まだ `env:NAME` のままの route、平文リテラルの MCP `env` / `headers`、`settings.cursor_api_key` が一覧表示される。UI が環境変数や既存値を自動読み取りすることはないため、「Move to Secrets」で名前を事前入力し、値を貼り付けて保存する。
- **参照の書き方**（すべて起動・probe 時にだけ復号されるため、UI/API で値を差し替えると再起動なしで次回起動から反映される）:
  - Route `credential`: `secret:NAME`
  - MCP `env` / `headers`: `{"secret": "NAME"}`（Routes・MCPs エディタからも選択可）
  - `settings.cursor_api_key`: `secret:NAME`
- **削除の保護**: route・MCP・alias・settings から参照されている secret は `DELETE /admin/secrets/{name}` で `409` となり、参照を外すまで削除できない。


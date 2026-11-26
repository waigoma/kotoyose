# Kotoyose 利用ガイド

Kotoyose は Claude を OpenAI 互換 API でラップする AI 実行ハーネスです。
Open WebUI / n8n / curl など、OpenAI 互換クライアントからそのまま接続できます。

---

## 1. エンドポイント一覧

| メソッド | パス | 説明 |
|----------|------|------|
| POST | `/v1/chat/completions` | チャット補完 (メイン) |
| POST | `/v1/chat/approvals/{request_id}` | チャット中のツール承認に回答 |
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

### 3.5 Fast (`service_tier`)

Codex と Cursor の、Fast に対応する alias では OpenAI と同じ `service_tier` を送れます。
`priority` がそのモデルの Fast です。`default`、`auto`、省略は通常の速度のままです。
対応しない runtime やモデル、それ以外の値は 422 になります。
`/v1/models` の `fast` に、その alias の表示名と説明が出ます。

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KOTOYOSE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kotoyose-chat",
    "messages": [
      {"role": "user", "content": "急いで要点だけ"}
    ],
    "service_tier": "priority"
  }'
```

### 3.6 OpenAI 互換の接続先

`openai.chat` で、Bearer 認証が必要な Chat Completions の上流へテキストチャットを送れます。
`endpoint` には API の base URL を、`secret` には Secrets Store の名前または `env:変数名` を指定します。

```yaml
connections:
  gemini-free:
    provider: openai_compat
    kind: endpoint
    endpoint: https://example.invalid/v1
    secret: gemini-api-key
    data_policy: unknown

routes:
  gemini-flash:
    driver: openai.chat
    model: gemini-2.0-flash
    connection: gemini-free

models:
  gemini-flash:
    description: テキストだけのチャット
    route: gemini-flash
```

`/v1/chat/completions` の `model` に `gemini-flash` を指定します。
同期と SSE の両方で毎回 `messages` の履歴を送ってください。
道具や画像を指定したリクエストには HTTP 400 を返します。`tools` が空配列または `null` なら受け付けます。
上流には HTTPS が必要です。ローカル検証では `127.0.0.0/8`、`::1`、`localhost` に限り HTTP も使えます。
alias の `timeout_seconds` は SSE では受信が途切れてからの上限 (既定 60 秒)、同期チャットでは HTTP 応答全体の上限 (既定 300 秒) です。
モデル一覧の取得に失敗した場合は、上流の Model ID を手入力してください。

`data_policy` の既定は `unknown` です。
同じ鍵を使うモデルでも学習方針が異なる場合は connection を分けてください。
`trains` または `unknown` の接続で `skill_refs` の本文を送るには、その alias の `training_acks` が必要です。

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

### 別の alias に頼む (MCP)

親 alias の `mcp_refs` に `kotoyose` を追加し、`delegates` に依頼先を列挙します。親を呼ぶ Client の `models` にも依頼先が必要です。

```yaml
models:
  kotoyose-chat:
    description: 作業の取りまとめを担当する
    delegates: [codex-coding]
    mcp_refs: [kotoyose]
  codex-coding:
    provider: codex
    description: コード変更をレビューする
```

人格や個人の記憶を持つ alias など、委譲の先にしたくない alias には `delegable: false` を設定します。`list_agents` に出ず、`ask_agent` と `delegates` でも使えません。

組み込み MCP の接続先は既定で `http://127.0.0.1:8000/v1/mcp` です。別のポートやベース URL で起動する場合は、Kotoyose サーバーの `KOTOYOSE_SELF_URL` にベース URL を設定します。

外部の Claude Code では、Client API キーを使って Streamable HTTP サーバーとして登録します。下の `KOTOYOSE_URL` は Kotoyose のベース URL です。

```bash
claude mcp add --transport http kotoyose "${KOTOYOSE_URL}/v1/mcp" \
  --header "Authorization: Bearer ${KOTOYOSE_CLIENT_API_KEY}"
```

頼む側の agent には、取扱説明書の skill `kotoyose-delegation` (`skills/kotoyose-delegation-bundle`) を持たせます。alias なら `skill_definitions` に登録して `skill_refs` で参照し、外の Claude Code などには skill として入れます。alias の選び方、依頼文の書き方、続きの頼み方、頼まない方がよい場合が書いてあります。

`list_agents` で依頼先を確認し、`ask_agent` に alias と単独で理解できる依頼文を渡します。続けて頼むときは、応答の `conversation_id` を指定します。

返事を待たずに並べるときは `start_agent` を使います。すぐに `task_id` と `conversation_id` が返り、子は背景で動きます。結果は `agent_status(task_id)` で見ます。状態は `running`、`completed`、`failed`、`cancelled` です。終わっていれば `text` または `error` と、取得できた `usage` が付きます。要らなくなった依頼は `cancel_agent(task_id)` で止めます。`agent_status` は短い間隔で繰り返さず、区切りのときに呼びます。この 3 つはホスト上のチャットと Client API キーで使え、隔離実行の中では使えません。`start_agent` も `ask_agent` と同じ同時実行と 1 時間の上限を使います。

親のチャットが `X-Kotoyose-Events: 1` でストリーミングしているとき、`ask_agent` の進捗は親の SSE に道具呼び出しとして出ます。子の返答本文はそこには入りません。

### 隔離実行から同じワークスペースを調べる

`/v1/executions` で動く Claude / Codex の alias も委譲できます。以下は設定済みの Claude 用接続 `claude` を使い、`coding` から `researcher` に調査を頼む例です。

```yaml
routes:
  claude-coding:
    driver: claude.agent-sdk
    connection: claude
    model: claude-sonnet-4-6
  claude-research:
    driver: claude.agent-sdk
    connection: claude
    model: claude-haiku-4-5
models:
  coding:
    route: claude-coding
    agent: {place: isolated, surface: write}
    mcp_refs: [kotoyose]
    delegates: [researcher]
  researcher:
    route: claude-research
    description: ワークスペースの実装を調査する
    agent: {place: isolated, surface: read}
```

Client の `models` に両方の alias を追加し、親子が同時に動ける実行枠を用意します。`coding` を通常どおり `builtin:host-coding@1` で実行すると、`ask_agent(alias="researcher", task="設定の読み込み順を調べて")` を呼べます。子は親と同じワークスペースを読み取り専用で開きます。依頼ごとに新しい子が起動するため、`conversation_id` は省略します。

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
子機で動くエージェント alias を許可するには、Client の `places` に `child:<名前>` を指定する。すべての登録済み子機を許可する場合は `child` を指定する。`max_surface` の上限は子機にも適用する。`agent` のないチャット専用 alias は、子機で動く場合も従来どおり `models` で許可する。
これまで `host` で子機のエージェント alias を使っていた Client には、`child` または対象の `child:<名前>` を追加する。
`runtime_profiles`、`agent_sandbox_profiles`、`agent_cli_binary`、`agent_credential_*`、`callers`（`execution.json`）は読み込めない。
Codex の `write` cell を `isolated` で解く alias は、容器を境界にして `execution_environment=isolated` を残す。`codex.sandbox` はそのとき働かない SDK 別名として pin し、`full-access` だけ `danger-full-access` に読み替える。jail（`exec`）は `execution_environment` も `codex_sandbox` も持たない。

`models.<alias>.approval` は、サーバー上で動く direct チャットの Claude Agent SDK と `cli.acp` の許可要求を制御します。

| 値 | 動作 |
|----|------|
| `server` (既定) | サーバー側のツールポリシーで処理します。 |
| `client` | サーバー側のポリシーが許可し、ランタイムが承認を求めた呼び出しについて、クライアントの回答を待ちます。 |

`client` でも `tools`、`deny`、読み取り専用の surface などの制限は適用されます。
承認には SSE と `X-Kotoyose-Events: 1`、`X-Kotoyose-Approvals: 1` が必要です。
ヘッダーが片方でも欠ける場合や非ストリーミングのチャットでは、クライアント承認を必要とする呼び出しを拒否します。
Claude の `permission_mode: bypassPermissions` を使う alias では、クライアントに確認を求めません。
Claude の `deny` に `Bash(rm:*)` のような入力パターン付きのルールがある場合、そのツールの許可を SDK が求めてもクライアントに提示せず拒否します。
承認待ちはサーバープロセスごとに保持します。回答が別のワーカーに届くと HTTP 404 になり、有効な回答が期限内に届かなければツール呼び出しを拒否します。
Codex / Cursor SDK、Native 実行、委譲されたターンではサーバー側のポリシーが適用されます。
別のマシン上で動く `on: child:<名前>` の Claude / `cli.acp` チャットで `client` を指定すると、直接のチャットリクエストは HTTP 501 になります。
ACP クライアントでの設定と回答 API は [ACP エージェント](acp-agent.md#ツールの実行承認) を参照してください。

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

**公開。** `/v1/models` は pool alias を、候補が共有する `agent.place`/`surface` と、`candidates`・`policy` つきで出す。`agent.provider`/`driver` は現在の第一候補のもの（状態を読めない、または使える候補が無いときは Client が許可する config 上の最初の候補）。`sanction` は最も重い候補に従う。Client の `models` に pool alias の名前が要り、候補の `places`/`max_surface` はどれか 1 つが通ればよい。`GET /v1/pools`（`/v1/models` と同じ認証、読み取りのみ）が pool alias ごとの `policy`・`first_choice`・候補の状態（`alias`/`pool`/`state`/`until`/`last_selected_at`）を返す。候補の公開と実行はどちらも Client が許可するものに限る。
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

## 12. child を登録する

別の機械で alias を動かす前に、その機械の child を host へ登録します。alias と connection の `on` に同じ child を指定すると、その機械で turn が動きます。

### Windows 11 x64 にインストールする

1. Kotoyose の [GitHub Releases](https://github.com/waigoma/kotoyose/releases) から、host と同じ版の `KotoyoseChild-Setup-<version>.exe` をダウンロードし、ダブルクリックします。Python、uv、PowerShell の準備は不要です。インストーラーは未署名です。SmartScreen が表示された場合は、取得元と添付の SHA-256 を確認してください。
2. セットアップでインストール先を選びます。既定は `%LOCALAPPDATA%\Programs\Kotoyose Child` です。基本の実行部品はセットアップに同梱しています。**Start at sign-in** は既定で有効です。
3. 完了画面で **Launch now** を選ぶと設定画面が開きます。未登録の画面に、host の管理コンソールの **Children** が発行したコードと child 名を入力します。
4. **Providers** で使う provider の **SDK** を開いて **Install** を選び、**Log in** と **Use on this child** を設定します。**Workspace** で作業フォルダーを追加します。

**Providers** には同梱 Python 内の SDK のバージョンと、既存の公式 CLI が見つかったかが表示されます。CLI のバージョンは **Check** で確認します。SDK の **Install**、**Update**、**Remove**、入れ直しの **Repair** を選ぶと進捗が表示されます。実行中の仕事がある場合は、変更する前に中断の確認が出ます。

Claude は、既定で Claude Agent SDK の wheel に含まれる CLI を使います。別に入れた公式 CLI を使う場合は **Use the installed CLI** を有効にします。Windows では PATH と `%USERPROFILE%\.local\bin\claude.exe` を探します。どちらの場合も SDK の導入と、同じ Windows ユーザーでの通常の CLI ログインが必要です。Codex も SDK と同梱 CLI を管理でき、child で turn を実行できます。PATH 上の公式 CLI を使うときは **Use the installed CLI** を有効にします。

基本の実行部品の導入に失敗した場合は、トレイから Kotoyose Child を終了し、セットアップを開き直します。選んだインストール先の `<version>\bin` で `kotoyose-child-app.exe repair` を実行する方法もあります。

セットアップから手動で更新する場合、前の版に入っていた provider は新しい版のピンで入れ直します。PyPI に届かない場合は **Providers** の **Repair** から入れ直します。削除する場合はトレイを終了し、Windows の **インストールされているアプリ** から Kotoyose Child をアンインストールします。**Settings and logs** を選ぶと登録情報・方針・ログも削除します。別のフォルダーに既に入っている場合、セットアップはそのフォルダーを使うか、先にアンインストールするかを尋ねます。

### Ubuntu/Linux x64 に子機を入れる

同じ版の `kotoyose-child-<version>-linux-x64.tar.gz` と `.sha256` を取得し、一般ユーザーで検証して展開します。
ホスト管理者は Ubuntu/Linux x64 で `./scripts/build-child-linux.sh` を実行して配布ファイルを作れます。
配布先で使う glibc の版に合わせ、対応する Ubuntu/Linux x64 環境でビルドします。

```bash
sha256sum -c kotoyose-child-1.3.0-linux-x64.tar.gz.sha256
tar -xzf kotoyose-child-1.3.0-linux-x64.tar.gz
./kotoyose-child-1.3.0/install.sh --systemd
~/.local/bin/kotoyose-child-app enroll '<コード>' --name laptop
systemctl --user start kotoyose-child.service
```

端末で起動する場合は `--systemd` を省き、登録後に `~/.local/bin/kotoyose-child-app run` を実行します。
systemd ユーザーセッションに接続できない場合も端末で起動できます。
`~/.local/bin/kotoyose-child-app ui` で設定画面を開きます。
端末起動では起動元の端末、ヘッドレスのサービス起動では `journalctl --user -u kotoyose-child.service -n 30` に設定 URL が出ます。

ロールバックするには、サービスを `systemctl --user stop kotoyose-child.service` で止めます。
端末起動の場合は Ctrl+C で終了します。
`versions` で直前の版のディレクトリ名を確認し、次の `<version>-<build>` に入れて `current` を切り替えます。

```bash
cd ~/.local/share/kotoyose-child-app
ls versions
stage=$(mktemp -d .rollback.XXXXXX)
ln -s 'versions/<version>-<build>' "$stage/current"
mv -Tf "$stage/current" current
rmdir "$stage"
```

切り替え後は前と同じ方法で起動します。
登録、方針、ログは `~/.local/share/kotoyose-child` にあり、版を切り替えても同じ状態を使います。

### Settings から子機を更新する

1. ホスト管理者は Windows のセットアップを `./scripts/build-child-installer.sh`、Linux x64 の tar.gz を `./scripts/build-child-linux.sh` で作ります。公開するファイルと同名の `.json` をそろえ、ホストのサービスと同じ実行ユーザーで `./scripts/publish-child-update.sh <dist dir>` を実行します。既定の公開先は `${XDG_DATA_HOME:-~/.local/share}/kotoyose/child-updates/` です。変更する場合はホストと公開スクリプトの両方に `KOTOYOSE_CHILD_UPDATES_DIR` を指定します。
2. 公開後に子機が接続すると、ホストの **Children** にそのプラットフォームの公開版が表示されます。Windows と Linux のパッケージ版では **Settings → App update** で版とビルドを確認し、**Update** を押します。子機は登録先のホストとのリンクでファイルを取得します。Linux のソース版は、新しい版のソースを取得して `uv sync` と後述の `cargo build` を実行し、子機を再起動します。
3. 取得と検証が終わると、実行中と待機中の仕事が終わるまで待ちます。中断して進めるときは **Interrupt and update** を選んで確認します。取得時に **Install on restart** を選んだ場合は、後で **App controls → Restart** を押して適用します。**Restart** はこの保留中か、インストール済みのビルドに切り替える必要があるときに表示します。
4. 適用後は子機が再接続し、設定画面を開き直します。Linux では前の版で使っていた provider を新しい版のピンで入れ直します。**Providers** に Needs reinstall が表示された場合は、該当する provider の **Repair** を選びます。適用時のエラーは **Settings** に表示されます。基本ランタイムの導入ログは **Recent logs → Open log folder** の `repair.log` で確認できます。

### child の状態と host 側の準備

child の状態は、Linux では `~/.local/share/kotoyose-child`、Windows では `%LOCALAPPDATA%\kotoyose-child` に置きます。両 OS で `KOTOYOSE_CHILD_HOME` を指定できます。中身は child の鍵と自己署名証明書、host の証明書 fingerprint、方針ファイル `policy.json` です。方針ファイルは child の機械で編集します。Windows の Rust 版ではタスクトレイの設定画面からも編集できます。

`policy.json` には次の項目があります。

- `workspace_roots`: 作業場として許す絶対パスの配列
- `adapters`: 許す adapter 名の配列 (例: `claude.agent-sdk`)
- `mcp`: 名前から stdio の起動定義への対応。`command` と `args` が必要で、`description` と `env` は任意です。
- `local_llm`: 子機で許可するローカル LLM 接続の ID と、その URL・モデル ID。省略または空のオブジェクトで無効です。
- `logins` は、host の残量確認と、対応する Linux child の既存のログイン中継を許す provider 名の配列です (例 `["claude"]`)。省略または空配列の場合は両方を拒否します。`claude` と `anthropic` は同じ provider として扱います。
- `codex_sandbox_max` は Codex の実行時に child が許す sandbox の上限です。`read-only`、`workspace-write`、`danger-full-access` から選べます。既定は `workspace-write` です。

host では、child からの待ち受けを公開 API とは別の port で開きます。未設定のときは待ち受けません。

```bash
# host の環境。公開 API の 8000 とは別の port です。
KOTOYOSE_CHILD_PORT=9443
```

証明書は、その port で最初に起動したとき `${XDG_DATA_HOME:-~/.local/share}/kotoyose/child/` に作ります。CA は使いません。登録後の child は自分の自己署名証明書をクライアント証明書として出し、host はその fingerprint を照合します。

管理コンソールの **Children**、または管理 API で、10 分・失敗 3 回までの登録コードを発行します。コードには、child がダイヤルするアドレスと host 証明書の fingerprint が入ります。待ち受けが止まっているときは発行できず、409 になります。

```bash
# host。アドレスは child から届く host:port です。
curl -sS -u "$KOTOYOSE_BASIC_USER:$KOTOYOSE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"address":"host.example:9443"}' \
  http://127.0.0.1:8000/admin/children/enrollment-codes

# child
kotoyose-child enroll '<コード>' --name laptop
kotoyose-child run
```

`enroll` は、TLS の相手の証明書 fingerprint がコードと一致することを確かめてからコードを送ります。一致しなければコードは送りません。`run` は同じ鍵で接続します。package の版が host と完全に一致しなければ、その理由を表示して終了します。heartbeat は約 20 秒ごとで、3 回届かなければ host は offline にします。接続が切れたあとは、間隔を広げながら繋ぎ直します。

登録済みの child は `GET /admin/children` とコンソールの Children で見ます。名前、online / offline、最後の heartbeat、版、adapter、MCP の名前が出ます。revoke はコンソールの Revoke、または `POST /admin/children/<名前>/revoke` です。generation が進み、張ってあった接続は切れ、同じ証明書では再接続できません。

child の台帳は Postgres ではなく、host の data ディレクトリにある `children.json` です。待ち受けの証明書と同じ場所に置き、DB が無い起動でも登録を残すためです。

### ソースから Rust 版 Link app を起動する (開発者向け)

`kotoyose-child-app` は host との接続を維持し、turn・ログイン・残量確認を受けたときに同じ版の Python Runner を起動します。Python 版の `kotoyose-child` から移る場合は、旧プロセスを終了します。同じ状態ディレクトリなら、登録と `policy.json` をそのまま使えます。

host と同じ版のソースを取得し、リポジトリのルートで Rust stable と Python 3.13 の開発環境を使います。`uv sync` で Runner と依存を環境に入れます。

Linux

```bash
uv sync
cargo build --locked --release --manifest-path child-app/Cargo.toml
export KOTOYOSE_CHILD_PYTHON="$PWD/.venv/bin/python"
child-app/target/release/kotoyose-child-app enroll '<コード>' --name laptop
child-app/target/release/kotoyose-child-app status
child-app/target/release/kotoyose-child-app run
```

Linux では `status` で登録先と起動中のインスタンスを確認できます。
接続結果は `run` の端末と host の **Children** に表示されます。
端末で Ctrl+C を押すと Link app と Runner が終了します。
引数を省略すると `run` と同じように起動し、初回登録が必要なら設定 URL を端末に出します。

パッケージ版は `bin` の隣にある `python/bin/python3` (Linux) または `python/python.exe` (Windows) を使います。
ソース版では `KOTOYOSE_CHILD_PYTHON`、有効な仮想環境、PATH の順で探します。

#### 設定画面を端末から開く

Linux と Windows の端末から `ui` を指定すると、リンクと設定サーバーが起動します。
Linux では、リポジトリのルートで次のコマンドを実行し、標準出力の URL を同じ端末のブラウザーで開きます。

```bash
~/.cargo/bin/cargo run --locked --manifest-path child-app/Cargo.toml -- ui
```

空の状態で表示だけ確認する場合は、コマンドの前に `KOTOYOSE_CHILD_HOME="$(mktemp -d)"` を付けます。
起動済みの Rust 版 child と同じ状態ディレクトリを指定すると、既存のプロセスが設定を開きます。
既存プロセスがトレイで起動していればブラウザーが開き、端末で起動していればその端末に新しい URL が表示されます。
URL は発行から 2 分間、1 回だけ使えます。

#### Windows でソースから起動する

Windows 11 では、Windows 用 Rust stable と C/C++ ビルドツールを用意し、PowerShell で実行します。

```powershell
uv sync
cargo build --locked --release --manifest-path child-app/Cargo.toml
$env:KOTOYOSE_CHILD_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
.\child-app\target\release\kotoyose-child-app.exe run
```

Python Runner は `KOTOYOSE_CHILD_PYTHON`、有効な venv、`uv tool install` で入れた Kotoyose (`uv tool dir` の `kotoyose`)、PATH の順に探します。wheel を `uv tool install --python 3.13 -c constraints.txt <wheel>` で入れておけば、環境変数なしで exe を起動するだけで動きます (自動起動でも同じです)。

### Windows のタスクトレイと設定画面

タスクトレイの Kotoyose Child から **Open Settings** を選ぶと、既定のブラウザーで **Home** が開きます。
接続中は接続先、両方のバージョン、Runner の状態が表示されます。
初回またはリンクが停止した場合は、host の登録コードと child 名をその場で入力します。
停止理由があるときは、登録フォームの上に表示されます。
登録済みの child で再登録するときは **Enroll again** を使います。接続中は **Settings** にあります。

**Providers** では、**Use on this child** で turn に使う provider、**Show quota to host** で host に残量確認を許す provider を選べます。**Workspace** では、エクスプローラーからコピーした絶対パスを貼り付けて作業フォルダーを追加できます。MCP の起動定義と **Codex sandbox limit** もここで設定します。方針を保存すると、実行中の仕事が終わったあとに host に再接続します。

**Settings** には Runner の待機時間、サインイン時の自動起動、**Recent logs** と **Open log folder**、再起動と停止の操作があります。待機時間の変更は接続を維持し、次の Runner から適用されます。既定は 600 秒です。0 秒では仕事の後始末が済み次第、Runner が終了します。再起動や停止で実行中・起動待ちの仕事が中断される場合は確認が出ます。未登録でアプリを起動すると、設定画面が自動で開きます。

設定画面に 10 分間リクエストがなければ待受が終了します。ブラウザーで再読み込みした場合や待受が終了した場合は、トレイから再度 **Open Settings** を選びます。

#### Windows のサインイン時に起動する

**Settings** の **Autostart** を開き、**Start at Windows sign-in** を有効にします。登録先は同じフォルダーにある GUI 用の `kotoyose-child-background.exe` です。サインインしたユーザーとして起動し、コンソールを開きません。旧 Python 版の `KotoyoseChild` ログオンタスクがあれば、有効にする操作の一環で削除し、結果を表示します。

ソースから起動する環境で Runner の Python を `KOTOYOSE_CHILD_PYTHON` で指定する場合は、絶対パスを Windows のユーザー環境変数にも設定してください。自動起動ではサインイン時のユーザー環境を使います。

```powershell
.\child-app\target\release\kotoyose-child-app.exe autostart on
.\child-app\target\release\kotoyose-child-app.exe autostart status
.\child-app\target\release\kotoyose-child-app.exe autostart off
```

端末で操作するには `kotoyose-child-app.exe enroll '<コード>' --name win` または `kotoyose-child-app.exe status` を使います。トレイを使わずに接続だけを維持する場合は `kotoyose-child-app.exe run --no-tray` を実行します。停止したアプリは `kotoyose-child-background.exe` を開いて起動できます。

### child のログイン・残量確認と alias

child の設定画面で **Providers** を開き、Claude または Codex の **Log in** を選びます。
Windows は公式 CLI のコンソールを開くので、そこで指示に従います。
Linux では設定画面に公式サインインページへのリンクが出ます。
Codex のデバイスコードは公式ページに入力し、Claude の入力欄が出た場合は公式ページの認証コードを設定画面に貼り付けます。
コードが受理されずログインが続いている場合は、入力欄から再送できます。
どちらの OS でも、進行中のログインは **Cancel** で終了できます。
ログイン状態は **Providers** を開いたときと、CLI のログインが終了したときに更新されます。

**Show quota to host** を有効にすると、対応する provider の残量を、child が接続している間に host から定期確認または **Check now** で取得できます。対応する Linux child の既存の host 経由ログインも、この許可を参照します。ローカルの **Log in** はこの設定にかかわらず使えます。

child で動かす alias は、connection と同じ `on: child:<名前>` を書きます。この版では subscription だけです。secret も `config_dir` も書きません。ログインは child を起動した OS ユーザーの、公式 CLI の普段のログインを使います。無修飾の `mcp_refs` と `local:<名前>` は child の `policy.json` の登録を参照します。Linux child でホストの HTTP MCP を使う場合は、以下の公開と承認を行い、`host:<名前>` で参照します。

```yaml
connections:
  dev-claude:
    provider: claude
    kind: subscription
    data_policy: no_training
    on: child:dev
routes:
  dev-claude:
    driver: claude.agent-sdk
    model: claude-sonnet-4-5
    connection: dev-claude
models:
  dev:
    route: dev-claude
    on: child:dev
    description: child の作業場
    allow_cwd_override: true
    cwd_allowed_roots: [/srv/dev/projects]
    mcp_refs: [files]
```

child の `policy.json` では、`adapters` に `claude.agent-sdk` を入れ、`workspace_roots` に作業場の絶対パスを入れ、`mcp.files` にその機械の起動定義を書きます。cwd の上書きには、alias の `allow_cwd_override` と `cwd_allowed_roots` が要ります。host は cwd を解決せず、文字列として `cwd_allowed_roots` の中かだけを確かめます。child が実パスにして、`workspace_roots` と alias の `cwd_allowed_roots` の両方の中でなければ、turn の前に拒否します。

child の機械では、host のチャット API に向けた `kotoyose-acp --forward-cwd` を使います。`--forward-cwd` が、その機械の cwd を文字列のまま host に渡します。

```bash
# child の機械。KOTOYOSE_URL は host のチャット API です。
kotoyose-child run
kotoyose-acp --model dev --forward-cwd
```

### Codex の alias を child に追加する

child の **Providers** で Codex の **Log in** を選びます。端末から設定する場合は、child を起動する OS ユーザーで公式の `codex login` を済ませます。同梱 CLI を使う場合は、Runner と同じ Python で `python -c "from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())"` を実行し、表示された CLI に `login` を渡します。child は起動環境の `CODEX_HOME` を使い、指定がなければ通常の `~/.codex` を使います。host の connection に認証用のパスや secret を書く必要はありません。

child の Python 環境に `openai-codex` と Codex CLI を用意し、`policy.json` の `adapters` に `codex.app-server` を追加して接続し直します。通常の Kotoyose の Python インストールには SDK と CLI wheel が含まれます。Windows のセットアップで入れた Link app を使う場合は、Runner 用の Python に同じ版の SDK と CLI wheel を追加します。通常は SDK 同梱の CLI を優先します。PATH 上の Codex を指定する場合は、child の状態ディレクトリにある `app.json` に `"codex_cli": "system"` を追加して再起動します。接続後、host の **Children** で `codex.app-server` の表示を確認できます。

host の設定例です。`dev` は登録済み child 名、`/srv/dev/projects` は child 上のパスです。

```yaml
connections:
  dev-codex:
    provider: codex
    kind: subscription
    data_policy: no_training
    on: child:dev
routes:
  dev-codex:
    driver: codex.app-server
    model: gpt-5
    connection: dev-codex
models:
  codex-dev:
    route: dev-codex
    on: child:dev
    description: child の Codex
    allow_cwd_override: true
    cwd_allowed_roots: [/srv/dev/projects]
```

child の `policy.json` の `workspace_roots` に同じ作業場を許可すると、`kotoyose-acp --model codex-dev --forward-cwd` で cwd を渡せます。子機のローカル MCP を使う場合は child の `policy.json` の `mcp` に起動定義を置き、alias の `mcp_refs` にその名前を書きます。Codex の有効な MCP 構成に方針外のサーバーや競合する定義が残る場合は、thread の起動前にエラーを返します。

### 子機でローカル LLM を使う

1. 子機上で Ollama や LM Studio の OpenAI 互換サーバーを起動し、ローカルモデルを用意します。待受を `127.0.0.1` または `::1` に限定し、鍵なしで Chat Completions を受け付けるようにします。Ollama の例は `http://127.0.0.1:11434/v1`、LM Studio の例は `http://127.0.0.1:1234/v1` です。実際の待受ポートとモデル ID を確認してください。
2. 子機の設定で **Workspace → Local LLMs → Add local LLM** を選び、接続 ID、ポート付きの `/v1` URL、許可するモデル ID を入力して **Save policy** を選びます。手動で編集する場合は、既存の `policy.json` に次の例のように `local_llm` を加え、子機を再接続します。

   ```json
   {
     "local_llm": {
       "ollama": {
         "url": "http://127.0.0.1:11434/v1",
         "models": ["qwen3:4b"]
       }
     }
   }
   ```

   ポートは 1024～65535 を指定します。登録先の制御プレーンのポートと、子機の設定 UI が動的に割り当てた現在のポートは使用できません。子機は turn の開始時にもポートを検査します。接続は 16 件まで、モデルは 1 接続につき 64 件まで、モデル ID は UTF-8 で 256 バイトまでです。

3. 再接続後、ホストコンソールの **Children** で接続 ID と許可モデルを確認します。ホストの YAML 設定で、接続と alias の両方に同じ子機を指定します。`model` には子機で許可した ID をそのまま指定し、`data_policy` はサーバーの実際のデータ取り扱いに合わせます。

   ```yaml
   connections:
     dev-ollama:
       provider: openai_compat
       kind: local_llm
       on: child:dev
       local_llm: ollama
       data_policy: unknown
   routes:
     dev-ollama:
       driver: openai.chat
       model: qwen3:4b
       connection: dev-ollama
   models:
     ollama-dev:
       route: dev-ollama
       on: child:dev
   ```

4. チャット API で alias `ollama-dev` を指定します。子機は許可された URL に `/chat/completions` を付けて直接接続します。子機が受け付ける応答は非圧縮で最大 8 MiB、SSE の各行は最大 1 MiB です。子機では `timeout_seconds` を 1～3600 秒に収め、ストリームを含む応答全体に適用します。子機のストリームは既定で 60 秒です。初版はテキストチャット専用です。ホストから Local LLM に未対応と表示された場合は、子機を対応版に更新します。無効にするには、子機の **Local LLMs** から接続を削除して保存します。

### ホストの HTTP MCP と stdio MCP を Linux child で使う

1. ホスト管理者がコンソールの **MCPs** で HTTP または stdio の MCP を開き、**Publish to children** を有効にします。YAML では `mcps.<名前>.publish_to_children: true` を設定します。説明欄には子機所有者が承認時に確認できるよう、ツールが扱うデータの範囲を記載します。HTTP の接続 URL、ヘッダー、secret 参照、stdio の command、引数、環境変数、cwd はホストの MCP 定義に置きます。stdio のプロセスはホストで起動し、ホストのファイルを扱います。子機には command を情報として見せるだけで、子機はそれを実行しません。

   ```yaml
   mcps:
     search:
       description: "Search the internal documentation index"
       publish_to_children: true
       server:
         type: http
         url: https://search.example.com/mcp
         headers:
           Authorization: {secret: SEARCH_MCP_AUTHORIZATION}
         enabled_tools: [search]
     files:
       description: "Read and write files on the host"
       publish_to_children: true
       server:
         type: stdio
         command: /usr/local/bin/host-files
         args: ["--serve"]
         enabled_tools: [read_file, write_file]
   ```

2. Linux child の所有者が Link app の **Workspace → Host MCP publications** を開き、ホストの fingerprint、MCP 名、公開版を確認して **Approve this version** を選びます。stdio の場合はホストの command、引数、cwd、環境変数の名前も表示されます。環境変数の値は表示しません。これは確認用で、子機では実行しません。ツールの引数はホストと上流 MCP に渡り、結果は child に戻ります。承認・取り消しは次の heartbeat でホストに伝わります。
3. 対象の child alias の `mcp_refs` に `host:search` を指定します。同じ alias に `host:search` と `local:search` のように同じ公開名を重ねると、設定エラーになります。

ホストの公開設定や fingerprint が変わったら、子機所有者が新しい公開版を承認します。上流サービスのツール構成やデータへのアクセス範囲だけを変更した場合は、ホストの **Publication revision** (`publication_revision`) も変更してください。

子機の alias は `mcp_refs` に `host:kotoyose` を書くと、同じ中継でホストの組み込み委譲を使えます。`delegates` に書いたホストの alias へ `list_agents` と `ask_agent` で頼めます。子機に委譲トークンやホストの資格情報は渡りません。親の turn を始めるたびに、その alias の `delegates` と呼び出し元 Client の `models` の共通部分を求め、委譲先の範囲を設定します。孫への再委譲はできず、親の turn を取り消すと委譲先も止まります。親の turn ごとに利用枠を作り、同時 4 件、直近 1 時間 32 件を超えるとツールエラーになります。ホスト全体で組み込み委譲の中継は同時 8 件までです。待ち時間は親の turn と委譲先 alias の timeout に従います。

```yaml
models:
  child-helper:
    route: claude-dev
    on: child:dev
    mcp_refs: [host:kotoyose]
    delegates: [reader]
  reader:
    route: claude-host
    description: Read the repository
```

Linux child の所有者は、他のホスト MCP と同じ承認画面で **Kotoyose delegation** と委譲先の全 alias 名を確認して、その公開版を承認します。子機 alias の `delegates` または `on` が変わると公開版が変わるので、承認を更新します。

HTTP 中継とホスト stdio の中継は Claude、Codex、HTTP MCP を広告するデータ宣言 ACP で利用できます。組み込み委譲も同じです。子機の CLI から見えるのは、いずれもターンごとのループバック HTTP です。ACP では既存の書き込み surface と MCP の承認も必要です。中継は 1 メッセージ 128 KiB、上流への呼び出し 1 回 30 秒、ターン内の同時呼び出し 8 件、ホスト全体の stdio プロセス 8 個、1 つの子機リンクでは 2 個が上限です。プロセスが終了するか標準出力が閉じると、その枠はターンの終了を待たずに空きます。stdio プロセスはターンの終了、取消、切断、ホストの再起動でプロセスグループごと終了します。cwd を絶対パスで指定しないプロセスの cwd と HOME は、セッションごとの空の一時ディレクトリです。リンク切断などで実行結果を確認できないツール呼び出しは結果不明として扱います。中継は自動再送しません。

ホストの stdio ツールは、ホストのサービスアカウントの権限で動きます。触れるホスト上のファイルは、そのプロセスの cwd と引数の範囲です。これは公開と承認で受け入れるリスクです。応答に秘密がそのまま含まれる場合は中継が伏せますが、符号化を変えた秘密までは捉えられません。

### alias で複数ファイルのスキルを使う

ホストの `skill_definitions` にプラグインのパスとスキルの実名を登録し、alias の `skill_refs` に登録名を指定します。
プラグインには `.claude-plugin/plugin.json` が必要です。
たとえば実名が `exskill` の場合、`skills/exskill/SKILL.md` と `skills/exskill/source/index.html` を同じプラグインに置きます。
`SKILL.md` から添付を参照するときは、スキルのフォルダーを基準に `source/index.html` と書きます。

```yaml
skill_definitions:
  example:
    plugin: /opt/kotoyose-plugins/example
    skill: exskill
models:
  dev:
    route: dev-route
    skill_refs: [example]
```

- Claude はセッション専用のプラグインを使います。Codex はスキルのフォルダーをセッションの探索ルートに登録し、毎 turn に `SKILL.md` の参照を渡します。`prompt_file_refs` も引き続き alias に指定できます。
- `cli.acp` / `cli.print` は入力に実スキル名と本文を前置します。本文は `fallback_prompt` を優先し、未指定なら frontmatter を除いた `SKILL.md` を使います。フォルダーがある場合はパスとファイル一覧が続くので、CLI が自身の読み取りツールで開けます。隔離 Agent では `/opt/agent/alias-skills` 以下を読み取り専用で提供します。
- 配信対象はスキルフォルダー内の非隠しファイルです。どの階層でも名前が `.` で始まる項目、`node_modules`、`__pycache__` を除外します。symlink、hardlink、特殊ファイル、絶対パスやフォルダー外を指すパスは拒否します。展開したファイルは読み取り専用です。
- `SKILL.md` を含めて 1 スキル 64 ファイル・1 MiB、1 turn 16 スキル・フォルダー合計 1 MiB です。添付はバイナリも扱います。Windows の一時領域を含むパス長に余裕を持たせるため、相対パスを UTF-8 で 100 バイトに制限します。CLI の前置本文は各 32 KiB、スキル計 128 KiB、プロンプトファイルを含め 256 KiB までです。合成した入力には CLI profile の上限も適用されます。

子機が `skill-folders/1` を通知すると、CLI の前置に添付ファイルも付けられます。Claude・Codex の SDK 形式で添付を配信するには、子機が `sdk-skills/1` も通知する必要があります。添付は子機のセッション専用領域に展開します。
`sdk-skills/1` または `prompt-prefix-skills/1` までの子機には、従来どおり SDK 向けの `SKILL.md` または CLI 向けの前置本文だけを送ります。それ以前の子機も既存の本文形式を使います。

### Linux x64 child に Grok Build を追加する

1. host 管理者がコンソールの **Providers** を開き、**Data declarations** で [`grok-build.json`](../examples/provider-declarations/grok-build.json) をアップロードします。
2. child の所有者が Link app の **Providers** にある **Host declarations** を開きます。Grok Build の版、配布 URL、SHA-256、コマンド、通信先、権限の範囲を確認し、**Approve read**、**Approve write**、**Approve MCP** から必要な能力を選びます。続けて **Install** を選びます。取得先は宣言された HTTPS ホストに限られ、サイズと時間に上限があります。配布物は SHA-256 と版の照合後に child の管理下に配置されます。取得や照合が失敗した場合は画面に理由が表示されます。
3. 同じカードで **Log in** を選びます。表示された公式ページを開き、画面に出たデバイスコードを公式ページに入力します。ログインは child の OS ユーザーで行い、状態は **Providers** と host の接続チェックから確認できます。
4. **Use on this child** を有効にします。child は方針を保存し、実行中の仕事が終わると host に再接続します。

host の設定例です。登録済み child 名に合わせて `dev` を変更し、`model` にそのアカウントで使う Grok Build のモデル ID を設定します。この ACP alias を使うには、データが学習に使われないことをアカウントの条件で確認し、`data_policy: no_training` を設定します。接続には child のローカルログインを使うため、host の secret や `config_dir` は不要です。

```yaml
connections:
  dev-grok:
    provider: grok-build
    kind: subscription
    profile: grok-build
    data_policy: no_training
    on: child:dev
routes:
  dev-grok:
    driver: cli.acp
    profile: grok-build
    model: grok-build-0.1
    connection: dev-grok
models:
  grok-read:
    route: dev-grok
    on: child:dev
```

この alias は、Grok 自身の `read-only` sandbox (Linux は Landlock) で起動します。ファイルは読めますが、書き込みは Grok の設定フォルダーと一時フォルダーに限られ、Grok が起動するコマンドはネットワークを使えません。さらに ACP の権限判定で書き込み、コマンド実行、MCP を拒否します。読み取りの範囲は作業フォルダーに限らず、child の OS ユーザーが読めるファイル全体です。作業フォルダーを渡す場合は、他の child alias と同様に **Workspace** と alias の両方で許可してください。

Landlock が使えないと、Grok の組み込み sandbox は警告を出して制約なしで続行します。読み取り、書き込み、MCP のすべての ACP turn に Linux カーネル 6.7 以降 (Landlock ABI 4 以上) が必要です。子機はシステムコールで ABI を確認し、要件を満たさなければ起動を拒否します。

書き込みを使う場合は、child の **Providers** で同じ宣言を **Approve write** で承認します。読み取り承認済みのカードからも切り替えられます。宣言の更新で SHA-256 が変わった場合は、新しい宣言を承認してください。書き込みを承認したあとも、`grok-read` は読み取り用のまま使えます。

上の `models` に書き込み用の alias を追加します。`/srv/work` は child の **Workspace** で許可した実際のパスに置き換えてください。この alias のリクエストには、両方の許可ルート内にある作業フォルダーを `cwd` で指定します。

```yaml
  grok-write:
    route: dev-grok
    on: child:dev
    agent:
      place: host
      surface: write
    allow_cwd_override: true
    cwd_allowed_roots: [/srv/work]
```

API キーに紐づく Client で `grok-write` を許可します。`places: [child:dev]` は `dev` だけを許可します。すべての登録済み child を許可する場合は `places: [child]` を指定します。

```yaml
clients:
  t3code:
    models: [grok-write]
    places: [child:dev]
    max_surface: write
```

書き込み turn は Grok の `strict` sandbox で動きます。ファイルを変更できるのは作業フォルダー内と Grok 自身の状態領域です。作業フォルダー外の OS ユーザーのホームは Grok 自身の状態を除いて読み取れず、起動したコマンドはネットワークを使えません。読み取りと書き込みの ACP turn は child 管理の専用 `HOME` で起動します。Grok のログイン状態は通常の `~/.grok` から引き継ぎます。

作業フォルダーは、子機ホームや `~/.grok` と重ならず、OS ユーザーのホーム全体を含まないパスを指定してください。OS のホームと `~/.grok` は現在のユーザーが所有する既存ディレクトリに限り、他のユーザーに書き込み権限がある場合は ACP を起動できません。グループの書き込み権限は、そのグループがユーザー専用 (Ubuntu の既定の user private group) の場合だけ認めます。

#### Grok Build で MCP を使う

child の **Providers** で **Approve MCP** を選びます。この承認には読み取りと書き込みも含まれます。child の **Workspace** に stdio の MCP サーバーを登録し、ホストの書き込み alias の `mcp_refs` に登録名を指定します。接続コマンド、引数、環境変数は child 側の登録から渡します。

```yaml
  grok-mcp:
    route: dev-grok
    on: child:dev
    agent:
      place: host
      surface: write
    allow_cwd_override: true
    cwd_allowed_roots: [/srv/work]
    mcp_refs: [files]
```

`files` は Workspace に登録した名前に置き換え、Client の `models` に `grok-mcp` を加えます。Grok Build の MCP サーバー名は ASCII の英数字で始め、以降は ASCII の英数字とハイフンだけを使えます。この alias にも許可ルート内の `cwd` が必要です。alias の `tools` を指定する場合は、許可したい MCP ツールの完全名 (`files__search` など) を含めます。ホストは `provider-data-mcp/1` を通知した子機にだけこの turn を送ります。

子機は、新しい ACP セッションに対して Grok が空のサーバー一覧を報告してからプロンプトを送ります。Grok 自身が設定から読み込んだ MCP サーバーを報告すると、turn の途中でも ACP とその子プロセスを停止し、サーバー名を表示します。子機の OS ユーザーで、該当サーバーごとに `grok mcp disable <name>` を実行してから再試行してください。新しいセッションの一覧が届かない場合は、宣言が指定する Grok Build のバージョンを確認してください。

MCP サーバーも Grok の `strict` sandbox 内で動作し、作業フォルダーとシステムパスだけを読み取れます。ホームディレクトリの設定ファイルなどを必要とするサーバーは、そのままでは動作しません。

### Windows x64 child に Grok Build を追加する

Windows 版の Grok は **この PC 上でログイン中のユーザーと同じ権限**で実行されます。
Grok がアクセスできるファイルや通信先を OS は制限せず、alias のツール制限や作業フォルダーによる閉じ込めも効きません。

1. ホストの **Providers → Data declarations** に [Grok Build の宣言](../examples/provider-declarations/grok-build.json) をアップロードします。
2. 子機の **Providers → Host declarations** で **Review unrestricted approval** を押し、警告を読んで確認欄にチェックを入れ、**Approve unrestricted** を押します。確認は表示から 60 秒間有効です。
3. **Install** で公式 EXE を取得します。配布物の SHA-256 と版を照合してから子機の管理下に置きます。
4. **Log in** を押し、新しく開いたコンソールの案内に従います。完了後にカードがログイン状態を確認します。
5. **Use on this child** を有効にし、子機の **Workspace** で作業フォルダーを許可します。

Windows の DLL 検索順序では、起動したプログラムが作業フォルダーにある DLL を読み込む場合があります。
作業フォルダー自体を信頼できる場所に置き、ほかのユーザーが書き込めるフォルダーや、出所を確認していない DLL があるフォルダーを指定しないでください。

ホストの設定例です。
`win-dev` は登録済みの子機名、`model` はそのアカウントで使うモデル ID、`C:\work` は子機で許可したパスに置き換えます。
`data_policy: no_training` は、この接続で学習に使われないことを確認してから設定します。

```yaml
connections:
  windows-grok:
    provider: grok-build
    profile: grok-build
    kind: subscription
    data_policy: no_training
    on: child:win-dev
routes:
  windows-grok:
    driver: cli.acp
    profile: grok-build
    connection: windows-grok
    model: grok-build-0.1
models:
  grok-windows:
    route: windows-grok
    on: child:win-dev
    agent:
      place: host
      surface: write
    allow: {full: true}
    allow_cwd_override: true
    cwd_allowed_roots: ['C:\work']
clients:
  win-client:
    models: [grok-windows]
    places: [child:win-dev]
    max_surface: write
    allow_full: true
```

API キーは `win-client` に紐づけ、リクエストの `cwd` に子機の Workspace と alias の両方が許可する実在のフォルダーを指定します。
MCP を使う場合は **Workspace** に stdio サーバーを登録し、alias の `mcp_refs` にその登録名を加えます。
Grok 自身が読み込んだ MCP サーバーを報告した場合は子機が turn を止めるので、子機上で該当するサーバーを `grok mcp disable <name>` で無効にしてから再試行します。
ホストが `provider-data-unrestricted/1` を確認できるよう、子機も Windows 対応版に更新してください。

### Windows で Python 版の child を使う (開発者向け)

Windows 11 では、その機械の Python 3.13 で child を動かします。Claude Agent SDK と Codex のチャットに対応します。ログインは child を起動するユーザーの通常の CLI ログインを使います。

Python 3.13 と uv を入れたあと、Kotoyose をツールとして入れます。配布物の URL は、host と同じ版の wheel か git の URL に読み替えてください。

```powershell
uv tool install <wheel or git URL>
```

Claude には、公式の手順でログインします。npm の `claude.cmd` は使いません。Kotoyose は、Claude Agent SDK の wheel に同梱された `claude.exe`、または **Providers** で選んだ既存の公式 `claude.exe` を `cli_path` に渡します。

登録コードは host のコンソールで発行します。child では次のように登録し、方針ファイルを書きます。`workspace_roots` は、その機械の作業場の絶対パスです。

```powershell
kotoyose-child enroll '<コード>' --name win
notepad $env:LOCALAPPDATA\kotoyose-child\policy.json
```

`policy.json` の例です。`adapters` には `claude.agent-sdk` を入れます。

```json
{
  "adapters": ["claude.agent-sdk"],
  "mcp": {},
  "workspace_roots": ["C:\\work"]
}
```

作業フォルダーの path は、エクスプローラーからコピーしたもの (`D:\work\proj`) をそのまま貼れます。JSON のエスケープ (`D:\\work\\proj`) で書いても同じ意味です。エスケープしないと `\t` や `\n` が別の文字に化ける JSON の規則は、child が見分けて path として読み直します。

接続は、ログオンしている間だけ張れば足ります。サービスにはしません。セッション 0 では、あとから desktop や browser を使えないためです。前面で動かすか、ログオン時のタスクにします。タスクは、そのユーザー (`USERDOMAIN\USERNAME`) の対話セッションで、昇格せずに動きます。実行時間の上限は無く、バッテリーでも動きます。`install-task` は Task Scheduler の XML を child の home に書き、登録したあと、そのファイルを消します。

```powershell
# 前面で動かすとき
kotoyose-child run

# ログオンのたびに対話セッションで動かすとき。外すときは uninstall-task
kotoyose-child install-task
```

`install-task` と `uninstall-task` は Windows 以外ではメッセージを出して終了コード 2 になります。

鍵と方針ファイルの Unix の mode (`chmod 600` など) は、Windows では意味を持ちません。Kotoyose はそこで chmod を呼ばず、失敗にもしません。ファイルは `%LOCALAPPDATA%\kotoyose-child` にあり、境界はそのユーザーの profile の ACL です。`kotoyose-acp` が API キーファイルの mode を見る検査も、Windows ではしません。Windows の `st_mode` は ACL を表さないためです。キーは環境変数 `KOTOYOSE_API_KEY` か、そのユーザーだけが読める `KOTOYOSE_API_KEY_FILE` に置きます。

同じ機械のエディタから host の alias を使うときは、`KOTOYOSE_URL` を host のチャット API に向け、作業ディレクトリを渡します。

```powershell
$env:KOTOYOSE_URL = "https://host.example:8000"
kotoyose-acp --model dev --forward-cwd
```

host の alias は `on: child:win` とし、`cwd_allowed_roots` には `C:\work` のように、child の `workspace_roots` の中に入る path を書きます。大文字と小文字の違いは無視されます。


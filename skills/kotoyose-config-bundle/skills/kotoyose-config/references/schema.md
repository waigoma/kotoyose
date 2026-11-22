# config.yml 仕様表

`src/kotoyose/config.py` が正本。本書と食い違ったときはソースを信じ、本書を直すこと。
行番号は 2026-07-29 時点のもの。

## 1. トップレベル 8 セクション

| セクション | 型 | 役割 |
|---|---|---|
| `models` | `dict[str, ModelPreset]` | **キーが `/v1/models` で返るモデル名**になる。実体はここ |
| `mcps` | `dict[str, McpDefinition]` | 共有 MCP server 定義。`mcp_refs` で参照 |
| `prompt_files` | `dict[str, PromptFileDefinition]` | 共有 system prompt file。`prompt_file_refs` で参照 |
| `skill_definitions` | `dict[str, SkillDefinition]` | 共有 Skill 定義。`skill_refs` で参照 |
| `runtime_models` | `dict[str, RuntimeModelDefinition]` | provider + model + 接続先の組。`runtime_ref` で参照 |
| `settings` | `HarnessSettings` | provider 認証情報。Admin UI の Settings 画面が書き込む |

## 2. モデル alias と既定値

`models` で alias を定義します。省略項目には `ModelPreset` の既定値が適用され、未定義のモデル名は 404 になります。

## 3. 参照の解決順序

`*_refs` で共有設定を合成する。

### `skill_refs` / `mcp_refs` — `config.py:566-588`

1. `skill_refs` を**記載順**に走査し、各 `SkillDefinition` の `plugin` を `plugins` へ積み、
   `skill` を `skills` へ積む
2. `mcp_refs` を記載順に走査し、`mcps[名前].server` を `mcp_servers[名前]` へ入れる
3. preset の inline `plugins` を末尾へ追加
4. preset の inline `skills` を適用 — **`"all"` または `[]` は refs 由来を丸ごと上書き**。
   リストなら refs 由来の後ろへ連結
5. preset の inline `mcp_servers` で同名キーを**上書き**

### skills 非対応 provider への fallback — `translation.py`

`skill_refs` を native 注入できるのは **`claude` / `codex` だけ**。
`cursor` / `antigravity` では、各 `SkillDefinition` の本文を
system prompt へ**前置する** (`prompt_file_refs` の後ろに記載順で連結)。

本文の出どころは `SkillDefinition.fallback_prompt` があればその path、
無ければ `{plugin}/skills/{skill}/SKILL.md`。いずれも先頭の YAML frontmatter を
落としてから使う — frontmatter は "Triggers: ..." 等の読み込み条件を書く場所で、
常時前置する本文としては「条件が合えば読む」= 任意、と誤読されうるため。

読めない path は黙ってスキップする (`prompt_file_refs` と同じ寛容さ)。
注入したぶんは `runtime.skill_fallback_injected` に skill 名と文字数が出る。

前置した skill は `TranslatedRuntimeConfig.skills` から外れ、その `plugin` も
`plugin_paths` から外れる。「落とした」ではなく「別経路で渡した」ため、
adapter の未対応リソース warning に混ざらないようにしている。
inline の `plugins` は skill_refs 由来ではないので残る。

**既定で有効。** 黙って捨てると config は規範が効いているかのように読めてしまう。
無効化する手段は「その preset から `skill_refs` を外す」だけ — `skills: []` は
`skill_refs` と併記できず validation で弾かれる (pitfalls #11)。
`skills: "all"` を併記しても fallback は `skill_refs` を見るため前置は起きる。

### `prompt_file_refs` — `config.py:590-592`

`prompt_files[名前].path` を**記載順**に並べるだけ。inline との合成は無い。
本文は `resolve_prompt_texts` が連結して system prompt へ append する。
**存在しないファイルは黙ってスキップされる** (`config.py:766-784`)。

### 別 alias への委譲

provider の subagent (`agent_definitions` / `agent_refs` / inline `agents`) は撤去した。
設定に残っていると load が失敗する。委譲は `mcp_refs: [kotoyose]` と `delegates` で行う。
Claude の Task / Agent ツールは常に無効。

### `runtime_ref` — `config.py:522-564`

`runtime_models[名前]` の `provider` / `model` が preset の同名フィールドを**上書きする**
(preset 側が負ける唯一の経路)。preset 側にも明示があって値が違う場合は
`preset.runtime_ref.override` の warning ログが出るだけで、上書きは止まらない。

`anthropic_base_url` / `anthropic_auth_token_env` は逆で、**preset 側が優先**され、
preset が `null` のときだけ definition の値が使われる。
`model_provider` は `codex` dict に未設定のときだけ注入される。

## 4. ModelPreset 全キー

`config.py:84-201`。「provider」列は値が実際に効く provider。

### 共通

| キー | 型 | 既定 | provider | 備考 |
|---|---|---|---|---|
| `provider` | `claude` \| `codex` \| `cursor` \| `antigravity` | `claude` | 全 | |
| `model` | `str \| null` | `null` | 全 | provider ごとのモデル ID |
| `runtime_ref` | `str \| null` | `null` | 全 | §3 参照 |
| `cwd` | `str` | `~/kotoyose-workspace` | 全 | 明示・省略いずれも `expanduser` される |
| `timeout_seconds` | `float > 0 \| null` | `null` | claude | CLI provider は各 provider 設定の timeout が優先 |
| `fixed_conversation_key` | `str \| null` | `null` | 全 | ヘッダ無しでも会話キーを固定する |

### Claude 実行制御

| キー | 型 | 既定 | 備考 |
|---|---|---|---|
| `bare` | `bool` | `true` | `--bare`。CLAUDE.md / MCP / skills / agents / commands の自動検出を無効化 |
| `tools` | `list[str] \| null` | `null` | built-in tool の allowlist。`null` = 全部、`[]` = built-in 無し (MCP tool は別枠で影響なし) |
| `deny` | `list[str]` | `[]` | `disallowed_tools`。`tools` と併用可。Claude では `Task` と `Agent` が常に追加される |
| `permission_mode` | `default` \| `acceptEdits` \| `bypassPermissions` \| `plan` \| `dontAsk` | `bypassPermissions` | |
| `setting_sources` | `list["user"\|"project"\|"local"] \| null` | `null` | `null` は `build_options` で `["project"]` へ落ちる。`[]` = 一切読まない |
| `require_isolation` | `bool` | `false` | `true` かつ `bare: false` なら `setting_sources: []` を強制 |
| `thinking` | `adaptive` \| `enabled` \| `disabled` \| `null` | `null` | `enabled` は `thinking_budget_tokens` 必須 |
| `thinking_budget_tokens` | `int \| null` | `null` | |
| `effort` | `low` \| `medium` \| `high` \| `xhigh` \| `max` \| `null` | `null` | Haiku 4.5 / Sonnet 4.5 は非対応 |
| `anthropic_base_url` | `str \| null` | `null` | claude 専用 |
| `anthropic_auth_token_env` | `str \| null` | `null` | claude 専用。トークン本体ではなく**環境変数名** |

### リソース参照

| キー | 型 | 既定 | 備考 |
|---|---|---|---|
| `mcp_refs` / `prompt_file_refs` / `skill_refs` | `list[str]` | `[]` | 同一リスト内の重複は ValidationError。`mcp_refs: [kotoyose]` は組み込みの委譲 MCP |
| `delegates` | `list[str]` | `[]` | 委譲先 alias。`mcp_refs: [kotoyose]` と組で使う |
| `mcp_servers` | `dict[str, dict]` | `{}` | inline 定義。§6 のスキーマ検証あり |
| `plugins` | `list[str]` | `[]` | 束ディレクトリ。存在と `.claude-plugin/plugin.json` を起動時に検証 |
| `skills` | `list[str]` \| `"all"` \| `null` | `null` | §3 の 4 番 |
| `required_request_headers` | `list[str]` | `[]` | MCP header の `{{header:X}}` を使うなら宣言必須 |

### per-request cwd 上書き

| キー | 型 | 既定 | 備考 |
|---|---|---|---|
| `allow_cwd_override` | `bool` | `false` | `true` にすると `cwd_allowed_roots` が必須 |
| `cwd_allowed_roots` | `list[str] \| null` | `null` | `resolve()` + `is_relative_to()` で検証。空要素も拒否 |

### 旧 memory hook の後方互換 (新規設定では使わない)

`memory_enabled` / `memory_force` / `memory_source` / `memory_channel` / `memory_sender`。
hook 自体は commit `d1e51c5` で撤去済み。YAML の round-trip 維持のためだけに残っている。

### provider 固有ブロック

`codex` / `cursor` / `antigravity` はいずれも自由形式の `dict`。
**スキーマ検証が無いため、綴りを誤っても loud には落ちない。**
正本は各 adapter:

| provider | adapter | 主なキー |
|---|---|---|
| `codex` | `runtime/codex.py` | `sandbox`, `approval_mode`, `codex_bin`, `config`, `profile`, `model_provider`, `dangerously_bypass_approvals_and_sandbox`, `timeout` |
| `cursor` | `runtime/cursor.py` | SDK 移行済み。§5 の `_IGNORED_CLI_KEYS` に注意 |
| `antigravity` | `runtime/antigravity_sdk.py` | `sdk` 内の `api_key_env`, `vertex`, `project`, `location`, `timeout_seconds` |

## 5. サブ定義のスキーマ

### `SkillDefinition`

`description` (任意) / `plugin` (束ディレクトリ、存在検証あり) / `skill` (束内の skill 名) /
`fallback_prompt` (任意、§3 の skills 非対応 provider 向け凝縮版の path。
未指定なら `{plugin}/skills/{skill}/SKILL.md` を使う)。

### `McpDefinition` (`config.py:386-396`)

`description` (任意) / `server` (inline `mcp_servers` と同一スキーマ)。

### `PromptFileDefinition` (`config.py:439-451`)

`description` (任意) / `path` (保存時に `expanduser` される)。

### `RuntimeModelDefinition` (`config.py:399-436`)

`provider` (必須) / `model` (必須) / `model_provider` (**codex 専用**) /
`anthropic_base_url`・`anthropic_auth_token_env` (**claude 専用**)。

### `HarnessSettings` (`config.py:454-459`)

`cursor_api_key` / `anthropic_auth_token` / `anthropic_base_url`。
`resolve_setting` により **settings が環境変数より優先**される。

## 6. 起動時に loud に落ちる不変条件

config ロード時に `ValidationError` となり、サーバーが起動しない。

| 条件 | 場所 |
|---|---|
| `*_refs` が未定義リソースを指す | `config.py:487-498` |
| `runtime_ref` が未定義 runtime を指す | `config.py:483-486` |
| 同一 `*_refs` 内に重複参照 | `config.py:219-231` |
| `allow_cwd_override: true` なのに `cwd_allowed_roots` が空 / 空要素あり | `config.py:312-336` |
| `require_isolation: true` かつ `bare: false` かつ `setting_sources != []` | `config.py:338-351` |
| `skill_refs` と `skills: []` の同時指定 | `config.py:353-361` |
| `thinking: enabled` なのに `thinking_budget_tokens` 未指定 | `config.py:304-310` |
| `mcp_servers`: `url` があるのに `type` が `http`/`sse` でない | `config.py:254-257` |
| `mcp_servers`: `type: http/sse` なのに `url` 欠落 | `config.py:258-261` |
| `mcp_servers`: `type: stdio` なのに `command` 欠落 | `config.py:262-265` |
| `mcp_servers`: `type` も `command` も `url` も無い | `config.py:266-270` |
| `plugins`: ディレクトリ不存在 / `.claude-plugin/plugin.json` 欠落 | `config.py:292-300` |
| `plugins` が文字列リストでない | `config.py:284-287` |
| MCP header に `{{header:X}}` があるのに `required_request_headers` に `X` が無い | `config.py:513-519` |
| `model_provider` を `provider != codex` で指定 | `config.py:428-429` |
| `anthropic_base_url` / `anthropic_auth_token_env` を `provider != claude` で指定 | `config.py:430-435` |
| 廃止済み `system_prompt_files` キーの使用 | `config.py:203-212` |
| 廃止済みプロファイル束キーの使用 | `config.py:691-720` |

## 7. 検証と反映

### 検証 (書き込み前に必ず走らせる)

`config.py:736` が import 時に `_load_yaml_config()` を呼ぶため、import がそのまま全検証になる。

```bash
KOTOYOSE_CONFIG=config.yml .venv/bin/python -c "import kotoyose.config as c; c.get_config()"
```

無言で終了すれば通過。失敗すれば `ValidationError` が該当キーとともに出る。

### 反映

| 手段 | 効果 |
|---|---|
| `POST /admin/reload` (Basic 認証) | `config.yml` を読み直す。**進行中のセッションは維持される** |
| サービス再起動 (`./scripts/deploy.sh`) | コード変更を伴うときのみ必要 |

`/admin/reload` は `reload_config()` を呼ぶだけなので、設定だけの変更に再起動は要らない。

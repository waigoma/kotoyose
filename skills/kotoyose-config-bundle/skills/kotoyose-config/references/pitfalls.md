# config.yml の落とし穴

過去に実際に踏まれた事故と、コードを読んで確認した罠。
上から順に「沈黙して間違う」度合いが高い。

## 1. モデル alias と既定値

`models` で alias を定義します。省略項目には `ModelPreset` の既定値が適用され、未定義のモデル名は 404 になります。

## 2. `config.yml` に書いたコメントは消える

`config.py:751` の `save_config` は `yaml.dump(validated.model_dump())` でファイル全体を
書き潰す。Admin UI から一度でも保存すると、人手で書いたコメント・キーの並び・
アンカー参照はすべて失われ、全キーが既定値まで展開される。

**対処**: 設定の意図を `config.yml` の中に書かない。本 skill の references か
`config.example.yml` に書く。`config.example.yml` は `save_config` の対象外なので安全。

## 3. 既定値にはバリデータが走らない (2026-07-30 に 1 件修正済み)

Pydantic v2 は **既定値をバリデータに通さない**。`Field(..., validate_default=True)` を
付けない限り、値を省略したときだけ正規化が飛ぶ。

実際にこれで壊れていた箇所: `ModelPreset.cwd` の既定値 `~/kotoyose-workspace` は
`expand_cwd` を通らず、`cwd` を省いた `provider: claude` の preset で
文字列 `~/kotoyose-workspace` がそのまま SDK へ渡っていた。SDK は起動前に
`Path(cwd).exists()` を検査するため、リクエスト時に

```
Working directory does not exist: ~/kotoyose-workspace
```

で失敗する。CLI provider (codex / cursor / antigravity) は各 adapter が
`Path(cwd).expanduser()` を自前で施していたため、claude 経路だけが露出していた。

現在は `cwd` に `validate_default=True` を付けて修正済み
(`test_omitted_cwd_is_expanded_like_explicit_cwd` でロック)。

**対処**: `ModelPreset` に「既定値を持ち、かつバリデータで正規化されるフィールド」を
新設するときは `validate_default=True` を必ず付ける。付け忘れても型検査は通り、
値を明示している限りテストも通るため、**省略したときだけ本番で壊れる**。

## 4. `cursor:` ブロックの CLI 由来キーは黙って無視される

Cursor は SDK へ移行済み (2026-07-14)。`runtime/cursor.py:22`:

```python
_IGNORED_CLI_KEYS = {"command", "args", "output_format", "stream_partial_output", "force", "resume_flag"}
```

これらを書いても debug ログが 1 行出るだけで、**エラーにも警告にもならない**。

より一般に、`codex` / `cursor` / `antigravity` の provider 固有ブロックは
自由形式の `dict` でスキーマ検証が無い。**キーを綴り間違えても loud には落ちない。**

**対処**: provider 固有キーを足すときは、対応する adapter (`src/kotoyose/runtime/<provider>.py`)
でそのキーが実際に読まれていることを確認する。

## 5. `permission_mode: plan` は「全ツール実行禁止」

SDK 上 `plan` は no tool execution を意味する。MCP ツールの呼び出しまで塞ぐため、
「会話専任で filesystem を触らせたくない」という意図には使えない (commit `cf80678`)。

**対処**: `permission_mode: bypassPermissions` のまま `deny` で絞る。

```yaml
permission_mode: bypassPermissions
deny:
  - "Bash(*)"
  - "Write(*)"
  - "Edit(*)"
```

## 6. `allow_cwd_override: true` は許可根とセットで書く

`cwd_allowed_roots` が空だと任意ディレクトリを指定できてしまうため、
`config.py:312-336` が起動時に拒否する。空文字列の要素も `Path("").resolve()` が
cwd になって許可範囲が黙って広がるため個別に拒否される (commit `215bc69`)。

```yaml
allow_cwd_override: true
cwd_allowed_roots: [~/kotoyose-workspace, ~/.task-hub/workspaces]
```

## 7. `bare: false` は信頼境界を変える

`bare: false` にすると CLAUDE.md / `.claude/agents` / project MCP の自動検出が復活する。
未信頼の cwd を扱う preset では、これが設定の混入経路になる。

このため `require_isolation: true` が用意されており、`bare: false` かつ
`setting_sources: []` 以外の組み合わせを `ModelPreset` レベルで拒否する
(`config.py:338-351`)。admin API 経由の更新でも発火する。

**対処**: 未信頼 cwd を扱う preset には `require_isolation: true` を付ける。

なお `bare: false` 時に Claude サブプロセスへ認証 env が渡らない不具合は
commit `b0e4904` で修正済み。現在は `--bare` フラグと env 注入が分離されている。

## 8. `codex_bin` は preset ごとに固定する

省略時の解決順は PATH -> SDK 同梱。実行環境によって拾うバイナリが変わり、
preset ごとに挙動が揺れる (commit `dc4f096`)。

```yaml
codex:
  codex_bin: ~/.local/bin/codex
```

## 9. `prompt_file_refs` の参照先が無くても落ちない

`resolve_prompt_texts` (`config.py:766-784`) は存在しないファイルを黙ってスキップする。
参照名の検証 (`config.py:496-498`) は `prompt_files` に定義があるかを見るだけで、
**`path` の実在は確認しない**。system prompt が静かに空になる。

**対処**: prompt file を足したら、実際にファイルが存在することを自分で確かめる。

## 10. `agents` / `agent_definitions` の `tools` 省略は全権継承

`AgentPreset.tools` が `null` (既定) の subagent は、**親セッションの全ツールを継承する**
(Bash を含む)。読み取り専用のつもりの調査係が書き込めてしまう。

**対処**: read-only 用途では `tools` を必ず明示列挙する。

## 11. `skills: []` と `skill_refs` は同時に書けない

`skill_refs` で個別 Skill を選びながら `skills: []` で全消灯するのは矛盾のため
`config.py:353-361` が拒否する。`skills: "all"` や `[]` は refs 由来の選別を
**丸ごと上書きする** (`config.py:583-584`) 点にも注意。

## 12. `plugins` は空値と未指定が別物

`null` は `[]` に正規化されるが、リスト以外の型は ValueError になる (commit `531c469`)。
また各パスは存在と `.claude-plugin/plugin.json` を起動時に検証されるので、
束を移動したら参照元の preset も直すこと。

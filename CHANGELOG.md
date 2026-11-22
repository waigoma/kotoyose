# Kotoyose の変更履歴

## 1.2.0 (2026-11-22)

- SDK の新しい runtime session には、それまでの会話を先頭に付けて渡すようにした。
- チャットの `reasoning_effort` で、対応する alias の effort をその turn だけ上書きできるようにした。`/v1/models` と `kotoyose-acp` にも、その alias が対応する effort を出した。
- 組み込み MCP の `list_agents` と `ask_agent` で、許可された別の alias にチャットを委譲できるようにした。
- 隔離された Claude / Codex の実行から、同じワークスペースを読み取り専用で開く alias に委譲できるようにした。
- alias の `delegable: false` で、その alias を委譲の先から外せるようにした。
- 委譲の取扱説明書の skill `kotoyose-delegation` を追加した。
- Kotoyose の alias を ACP クライアントから選べる `kotoyose-acp` コマンドを追加した。
- `X-Kotoyose-Events: 1` を指定したチャット SSE にツールイベントを追加した。
- `kotoyose-acp` は API キーを持ち主だけが読めるファイル (`KOTOYOSE_API_KEY_FILE`) から読める。
- 使える effort を起動時と以後 4 時間ごとに取得し、再起動のあとも `/v1/models` と `kotoyose-acp` に出す。

### Removed

- `agent_refs` / `agents` / `agent_definitions` を削除した。別 alias への委譲は `mcp_refs: [kotoyose]` と `delegates` を使う。Claude の Task / Agent ツールは常に無効。

## 1.1.0 (2026-11-21)

- コンソールから Antigravity CLI 1.5.13、Cursor Agent 2026.11.19-f005e9d、Grok Build 1.0.89 の公式配布物を固定ハッシュで検証して導入できるようにした (Linux x86_64)。
- Claude Code は Claude Agent SDK の同梱 CLI を既定で使い、ログインと接続プローブでも同じ実行ファイルを使用する。
- agy、Cursor CLI、Grok Build を明示的な `cli.acp` route で実行できるようにした。専用ログイン home と既存の jail・egress 設定を共用する。
- ACP のホストチャットで会話セッションの再利用と再開、MCP の受け渡し、tool call と許可判定のイベント、取消に対応した。
- ACP の turn の使用量を記録する。Grok Build は拡張の欄から token 数と費用も読む (Cursor の ACP は使用量を返さない)。

### Removed

- `antigravity.agy` ドライバーを削除した。agy は `cli.acp` で実行する。

## 1.0.1 (2026-11-21)

- Grok Build を公式配布の実行ファイルから直接導入するようにした (sha256 を固定して照合、node 不要)。
- 導入時に node を PATH から探すよう修正。
- CI の安定化。

## 1.0.0 (2026-11-21)

- Claude、Codex、Cursor、Antigravity、Grok Build の公式 CLI または SDK に対応。
- alias ごとにプロンプト、スキル、MCP、ツール、実行場所を構成。jail では外向き通信を許可リストで制限する。
- 接続ごとにデータ利用方針を宣言し、必要な場合は同意を求める。対応する接続では利用枠を取得できる。
- コンソールで接続、モデル、alias、利用枠を管理し、対応 CLI をバージョン固定と整合性検証付きで導入できる。
- OpenAI 互換チャット API と、タスクオーケストレーター向けの実行 API v1 を提供する。

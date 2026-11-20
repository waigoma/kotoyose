# Kotoyose ロードマップ

## いまの到達点

2026 年 11 月時点。

- Cursor CLI と Grok Build を print CLI provider として追加。チャットはホストで、コーディングは jail で実行できる。
- コンソールの Install ボタンから、バージョンを固定し、配布物の整合性を検証して CLI を導入できる。
- エージェント SDK を更新。Claude Agent SDK 0.2.185、Codex SDK 0.170.0、Cursor SDK 1.3.1、Antigravity SDK 0.1.23。
- Grok のプラン利用枠を、CLI の ACP メソッド `_x.ai/billing` から取得する。
- コンソールを刷新。provider ごとのパネルと色付きマーク、ツリー表示、provider の並べ替えに対応した。
- 共通トークンによる API 認証、`defaults` preset、旧管理 UI、旧 runtime profile、`gemini.cli`、実験的な外部プラグインローダーを撤去した。
- README、Apache-2.0 ライセンス、セキュリティポリシーを整備。現行の文書は [文書一覧](README.md) にまとめた。

## v1.0 までの残り

- `src/`、`tests/`、設定例から、個人や運用環境を特定できる情報を取り除く。
- 個人情報チェックを備えたリリース CI を作る。`release` ブランチから許可リストに載ったパスを、所有者の個人アカウントにある公開リポジトリへコピーする。
- provider の追加手順を書く。
- v1.0 のタグを付ける。

## v1.0 のあと

1. **汎用の `acp` driver。** [Agent Client Protocol](https://agentclientprotocol.com/) を話すエージェントを、起動の宣言だけで足せるようにする。まず Grok と Cursor (と ACP を話せる agy) で chat と jail を通し、MCP の受け渡し (`session/new`) と道具の許可 (`session/request_permission`) を alias の設定から一か所で効かせる。Claude と Codex は今の SDK / app-server の経路を残す。
2. **Kotoyose を ACP のエージェントとして公開する。** T3 Code や Zed のような ACP クライアントから、alias を 1 つのエージェントとして呼べるようにする。alias の prompt・skill・MCP・jail・学習の方針が効いたまま、既存の UI とスマホからの承認を使える。独自のチャット UI はこれで足りるかを見極めてから判断する。
3. **OpenAI 互換 endpoint を provider として扱う。** Gemini の無料枠や OpenRouter に接続する。あわせて、Claude Code から Anthropic 互換 endpoint へ、Codex から OpenAI 互換 model provider へ接続できるか確かめる。
4. 1 と 3 の実際の追加を通じて、provider とプラグインの構成を固める。[プラグイン設計記録](design/plugin-architecture-2026-11.md) を参照。
5. その後、ブラウザ操作やコンピューター操作を担う子ホスト (例として Windows) と、Kotoyose の MCP サーバー化を検討する。

## やらないと決めたこと

- コンソールでプラグインの JavaScript を動かす UI mod。
- Web UI から pip でコードプラグインを導入する機能。
- ログイン PTY、リソース、実行の受け入れ判定、外向き通信制御、認証情報の注入、jail の起動のプラグイン化。

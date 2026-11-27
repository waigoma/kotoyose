# Kotoyose ロードマップ

## いまの到達点

2026 年 11 月時点の最新版は 1.4.0。

- 1.4.0 で、Claude / Codex / Antigravity / Cursor / Grok のローカル会話履歴の冪等インポート (CLI・コンソール・増分台帳・ライブ重複回避・資格情報 Redaction)、別モデルへ会話を引き継ぐ Cross-Model Continue / Fork (`work` → `no_training` 強制ガード付き)、Child 端末からの送信前サニタイズ付きマルチマシン収集 (`session-collect/1`)、読み取り専用 Export API (`GET /v1/sessions/export`)、および委譲・サブエージェントログの既定非表示を入れた。

- 1.0.0 で、Claude、Codex、Cursor、Antigravity、Grok Build の公式 CLI と SDK に対応した。alias、jail、学習の方針、コンソール、OpenAI 互換の chat API、実行 API を備える。公開は `release` ブランチからの CI で行う。
- 1.1.0 で、agy、Cursor、Grok Build を共通の ACP driver (`cli.acp`) で動かすようにした。ホストのチャットと jail のコーディングの両方で、本物の CLI で確かめた。
- driver の優先順は SDK → ACP → CLI。Claude と Codex は SDK に同梱の CLI を使い、別途の導入は要らない。ACP を話さない CLI には `cli.print` (`-p`) を残す。
- 導入する CLI は公式の配布物だけを使い、固定したハッシュで照合する。配布物の script は実行しない。
- ACP の turn の使用量を記録する。Grok は token 数と費用も返す。Cursor の ACP は使用量を返さない。
- agy 専用の古い `antigravity.agy` driver は削除した。
- 1.2.0 で、Kotoyose を ACP のエージェントとして使えるようにした (`kotoyose-acp`。T3 Code で確認)。途中で model を切り替えても会話を引き継ぎ、effort は model ごとに使えるものだけを選べる。
- 1.2.0 で、agent が別の alias に作業を頼めるようにした (組み込みの MCP、`list_agents` / `ask_agent`)。jail のコーディングの agent からも、同じ作業場を読み取り専用で開く alias に頼める。孫は作れない。provider ごとのサブエージェント (`agent_refs`) はこれに置き換えて削除した。
- 1.3.0 で、child (別の機械の Linux / Windows) の provider を、コードを変えずにデータの宣言で足せるようにした。Grok Build は Linux では閉じ込め (Landlock) で、Windows では `unrestricted` の別承認で使える。
- 1.3.0 で、同じ alias ならどの機械でも同じ skill (フォルダーごと)・prompt・MCP を使えるようにした。ホストの MCP の中継と、child の alias からホストの alias への委譲も入った。
- 1.3.0 で、child の機械のローカル LLM (loopback だけ、既定は無効) を使えるようにした。
- 1.3.0 で、Ubuntu 向けの child の tar.gz を用意し、Windows と Ubuntu の child を設定画面から更新できるようにした。新しい版は、ホストが既存の接続で配る。

## v1.0 のあと

1. **Kotoyose を ACP のエージェントとして公開する。** T3 Code や Zed のような ACP クライアントから、alias を 1 つのエージェントとして呼べるようにする。第 1 段 (alias の選択、本文と道具の進捗の中継、取消、使用量) は実装済み ([ACP エージェント](acp-agent.md))。残りは、道具の実行許可をクライアントの承認 (スマホからの承認) に回すことと、会話の再開 (`session/load`)。独自のチャット UI はこれで足りるかを見極めてから判断する。
2. **委譲の第 2 段。** 頼んだ子の途中経過を親に流す。非同期で頼み、状態の確認と取消をする。
3. **OpenAI 互換 endpoint を provider として扱う。** Gemini の無料枠や OpenRouter に接続する。あわせて、Claude Code から Anthropic 互換 endpoint へ、Codex から OpenAI 互換 model provider へ接続できるか確かめる。
4. 3 の実際の追加を通じて、provider とプラグインの構成を固める。
5. **child**: 別の機械 (例として Windows) に child を入れ、host の alias をその機械のファイルで動かす。配布の単位にもなる。1.3.0 までに、Windows と Ubuntu の配布物、データの宣言による provider、alias の同一性、画面からの更新が入った。残りは、モデルと実行場所を選べる alias のテンプレートと、Windows の Grok での MCP。
6. その後、Kotoyose の MCP サーバーを、委譲以外の用途 (別の環境での実行など) にも広げることを検討する。

## やらないと決めたこと

- コンソールでプラグインの JavaScript を動かす UI mod。
- Web UI から pip でコードプラグインを導入する機能。
- ログイン PTY、リソース、実行の受け入れ判定、外向き通信制御、認証情報の注入、jail の起動のプラグイン化。

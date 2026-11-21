# Kotoyose ロードマップ

## いまの到達点

2026 年 11 月時点の最新版は 1.1.0。

- 1.0.0 で、Claude、Codex、Cursor、Antigravity、Grok Build の公式 CLI と SDK に対応した。alias、jail、学習の方針、コンソール、OpenAI 互換の chat API、実行 API を備える。公開は `release` ブランチからの CI で行う。
- 1.1.0 で、agy、Cursor、Grok Build を共通の ACP driver (`cli.acp`) で動かすようにした。ホストのチャットと jail のコーディングの両方で、本物の CLI で確かめた。
- driver の優先順は SDK → ACP → CLI。Claude と Codex は SDK に同梱の CLI を使い、別途の導入は要らない。ACP を話さない CLI には `cli.print` (`-p`) を残す。
- 導入する CLI は公式の配布物だけを使い、固定したハッシュで照合する。配布物の script は実行しない。
- ACP の turn の使用量を記録する。Grok は token 数と費用も返す。Cursor の ACP は使用量を返さない。
- agy 専用の古い `antigravity.agy` driver は削除した。

## v1.0 のあと

1. **Kotoyose を ACP のエージェントとして公開する。** T3 Code や Zed のような ACP クライアントから、alias を 1 つのエージェントとして呼べるようにする。alias の prompt・skill・MCP・jail・学習の方針が効いたまま、既存の UI とスマホからの承認を使える。独自のチャット UI はこれで足りるかを見極めてから判断する。
2. **OpenAI 互換 endpoint を provider として扱う。** Gemini の無料枠や OpenRouter に接続する。あわせて、Claude Code から Anthropic 互換 endpoint へ、Codex から OpenAI 互換 model provider へ接続できるか確かめる。
3. 2 の実際の追加を通じて、provider とプラグインの構成を固める。[プラグイン設計記録](design/plugin-architecture-2026-11.md) と [ACP driver の設計](design/acp-driver.md) を参照。
4. その後、ブラウザ操作やコンピューター操作を担う子ホスト (例として Windows) と、Kotoyose の MCP サーバー化を検討する。

## やらないと決めたこと

- コンソールでプラグインの JavaScript を動かす UI mod。
- Web UI から pip でコードプラグインを導入する機能。
- ログイン PTY、リソース、実行の受け入れ判定、外向き通信制御、認証情報の注入、jail の起動のプラグイン化。

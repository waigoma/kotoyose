# Kotoyose の文書

- [使い方](usage-guide.md) — チャット API、クライアント接続、運用設定。
- [実行 API v1](hub-execution-v1.md) — タスクオーケストレーターが使う実行、権限、再開の契約。
- [ロードマップ](ROADMAP.md) — v1.0 の到達点と今後の予定。
- [ADR](adr/README.md) — 設計判断の記録。
- [実行の軸](design/execution-axes.md) — モデル、道具、実行場所、権限の設計。
- [実行の軸のレビュー](design/execution-axes-review.md) — 実行の軸の設計判断を支えるレビュー記録。
- [provider の追加手順](adding-a-provider.md) は現行の CLI と SDK の組み込み方を説明します。
- [Provider パッケージ](design/provider-packages.md) — Provider と CLI インストーラーの設計。
- [プラグイン設計記録](design/plugin-architecture-2026-11.md) — 外部 provider を追加する際の方針と判断。
- [Native Codex サンドボックス](native-codex-sandbox.md) — コーディング環境の隔離、作業領域、通信制御。
- [Codex rootfs の構築](runbooks/codex-rootfs-build.md) — コーディング環境用 rootfs の作成と設定。
- [Codex の外向き通信制限](runbooks/codex-egress-limits.md) — ブローカーの上限と運用。
- [Native Codex の診断](runbooks/native-codex-diagnostics.md) — 実行が失敗した際の調査手順。
- [systemd デプロイ](runbooks/systemd-deploy.md) — インストール、更新、動作確認。
- [エージェント開発標準 v1](standards/agent-development-standard-v1.md) — 実行契約、評価、設定の雛形。
- [適合チェックリスト](standards/conformance-v1.md) — 実装と運用に必要な確認項目。

作業履歴と旧資料は `archive/` に保管し、公開対象から除外します。

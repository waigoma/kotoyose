# 0002 9router を廃止し、Claude は setup-token と公式 /login に

- 状態: 採用
- 日付: 2026-11-15

## 背景
アシスタント・example-client・oneshot などの Claude の alias は 9router 経由だった。9router は自分でトークンを持ち、Claude Code を装って送るので ADR-0001 の線を越える。

## 決定
- connection `claude-oauth` (kind subscription, secret `claude-setup-token`) を Claude の実行に使う。トークンは console から `claude setup-token` を中継して発行・保存できる。
- connection `claude-host` (kind subscription, config_dir) は公式 `/login` のログイン。残量 (`/usage`) を読むためと host 実行のため。`claude-oauth` は `quota_from: claude-host` で残量を借りる。
- 9router の connection・route・未使用 alias を削除 (alias 35 → 20、route 17 → 7)。example-client は残す。

## 結果
- 変わらないはずの alias は解決後の設定が旧新で一致することを確かめてから切り替えた。
- `bare: true` の alias は `CLAUDE_CODE_OAUTH_TOKEN` を読まないので外した。

# 0004 アカウントと接続先を connection という 1 つの entity に

- 状態: 採用
- 日付: 2026-11-14

## 決定
- `config.yml` の `connections` に、アカウントのログイン (subscription)・API key・endpoint を置く。route は `connection` を指す。
- subscription は `secret` (長期トークン) か `config_dir` (CLI 自身の home) か `auth_file` (Codex) を持つ。残量の表示は `quota_from` と `show_quota` で調整する。
- ログイン状態と残量は `kotoyose.connection_state` に置く (ADR-0007)。

## 結果
- route と同じ鍵で規約の判定・pool・残量を見られるようになった。
- **未決**: `codex-host` と `codex-chatgpt` は同じアカウントで、違いは実行場所だけ。ADR-0010 の place の軸に寄せて 1 つにまとめる。

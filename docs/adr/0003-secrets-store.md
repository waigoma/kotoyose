# 0003 秘密は暗号化した store に置き、画面からは書くだけ

- 状態: 採用
- 日付: 2026-11-14

## 決定
- 秘密は Postgres の `kotoyose.secrets` に AES-256-GCM で置く。鍵は `~/.local/share/kotoyose/master.key` (初回に作成、所有者が控えを持つ)。
- 設定からは `secret:NAME` / `{"secret": NAME}` で参照する。console の Secrets は書き込みだけで、値は表示しない。
- 呼び出し元の API key は sha256 だけを `kotoyose.api_keys` に置き、発行時に 1 回だけ表示する。

## 結果
- 平文の key を含んでいた `config.yml` の控えは削除した。

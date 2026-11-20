# 0008 DB の CHECK 制約は起動のたびに作り直す

- 状態: 採用
- 日付: 2026-11-15

## 背景
`CREATE TABLE IF NOT EXISTS` や `IF NOT EXISTS` 付きの `ADD CONSTRAINT` は、既存の表の CHECK を更新しない。許可する値を Python 側で足しても本番の表は古いままで、記録が `CheckViolationError` で黙って失敗した。`connection_state` (#117) と `execution_attempt_limits_valid` (#128) で 2 回踏んだ。in-memory の store だけの試験では見えなかった。

## 決定
- 許可値の集合を持つ CHECK は、起動の初期化で DROP してから Python 側の集合から作り直す。
- 古い CHECK の表から始める DB 試験を付け、修正を外すと落ちることを確かめる。

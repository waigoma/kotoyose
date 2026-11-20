# Architecture Decision Records

Kotoyose の大きな判断を 1 件 1 ファイルで残す。長い作業の途中で決めたことを忘れないため、そして後から「なぜこうなっているか」を辿れるようにするため。

書き方: `NNNN-短い名前.md`。見出しは 状態 / 背景 / 決定 / 結果。状態は `採用` `提案` `置き換え (→ NNNN)` のどれか。決定を変えるときは古い ADR を消さず、新しい ADR を書いて古い方の状態を置き換えにする。

| # | 題 | 状態 | 日付 |
|---|---|---|---|
| [0001](0001-subscriptions-through-official-clients.md) | サブスクは公式 CLI を通してだけ使う | 採用 | 2026-11-14 |
| [0002](0002-drop-9router.md) | 9router を廃止し、Claude は setup-token と公式 /login に | 採用 | 2026-11-15 |
| [0003](0003-secrets-store.md) | 秘密は暗号化した store に置き、画面からは書くだけ | 採用 | 2026-11-14 |
| [0004](0004-connections.md) | アカウントと接続先を connection という 1 つの entity に | 採用 | 2026-11-14 |
| [0005](0005-print-mode-cli-driver.md) | -p 系 CLI は共通 driver とデータのプロファイルで | 採用 | 2026-11-15 |
| [0006](0006-console-login-via-cli-pty.md) | console のログインは公式 CLI を疑似端末で中継する | 採用 | 2026-11-15 |
| [0007](0007-quota-from-official-sources.md) | 残量と有効性は公式の口からだけ読む | 採用 | 2026-11-15 |
| [0008](0008-rebuild-db-checks-on-startup.md) | DB の CHECK 制約は起動のたびに作り直す | 採用 | 2026-11-15 |
| [0009](0009-breaking-changes-before-v1.md) | v1 までは互換を作らず破壊的に変える | 採用 | 2026-11-15 |
| [0010](0010-execution-axes.md) | 実行の軸を model / 道具 / place / allow に共通化する | 採用 | 2026-11-15 |
| [0011](0011-generalize-for-release.md) | リリースに向けて Hub 固有・個人固有の名前と場所を無くす | 採用 | 2026-11-16 |
| [0012](0012-provider-packages.md) | AI の入口を provider package として宣言で足す | 採用 | 2026-11-18 |

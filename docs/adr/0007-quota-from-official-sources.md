# 0007 残量と有効性は公式の口からだけ読む

- 状態: 採用
- 日付: 2026-11-15

## 決定
- Codex: app-server `account/read`・`account/rateLimits/read`。期限 3 日前から `refreshToken` で CLI 自身に更新させる。
- Claude: 公式 `/login` の config dir で `claude auth status --json` と `claude -p /usage` (課金なし)。setup-token は推論専用で残量を読めない。
- agy: `agy -p /usage --output-format json` (turn なし・token 0)。
- API key / endpoint: 公式の model 一覧 (`/v1/models`) で有効性だけを見る。残高や残り枠の公式の口は無い (Anthropic は Admin API が必要で個人では使えない、Cursor も個人向けの口が無い)。非公開のダッシュボード API は使わない。
- 30 分ごと・起動時・Check now で `connection_state` に書き、Providers と Dashboard に出す。

## 結果
- 本番で Claude (session / 週 / Fable)・Codex (5h / 7d)・agy (Gemini / Claude & GPT / Argon) の残量が見える。

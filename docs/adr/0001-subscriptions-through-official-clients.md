# 0001 サブスクは公式 CLI を通してだけ使う

- 状態: 採用
- 日付: 2026-11-14 (所有者の裁定)

## 背景
Claude・ChatGPT・Antigravity のサブスクは、各社の規約で公式アプリ以外からの利用が制限されている。違反するとアカウントが止められる (ban) おそれがある。Anthropic の docs は、第三者が Claude.ai の資格情報やセッショントークンを「collect, store, or intermediate」することを禁じ、`claude setup-token` は「CI やスクリプトで `CLAUDE_CODE_OAUTH_TOKEN` に入れて使う」と明記している。

以前の Kotoyose は Claude Code の認証ファイルを読み、Claude Code の client_id で自分でトークンを更新していた (`runtime/claude_auth.py`)。9router は Claude Code を装って自分でリクエストを送っていた。

## 決定
- サブスクは、その会社の公式 CLI (Claude Code・Codex・agy) が自分のログインで動く形でだけ使う。
- Kotoyose は CLI のログインファイルを開かない・写さない・自分で更新しない・OAuth の交換をしない。
- 許される形は 3 つ: (1) 公式 CLI が自分の home / config dir のログインで動く、(2) 公式コマンドが発行した長期トークン (`claude setup-token`) を本人の機械で公式 CLI に渡す、(3) API key。
- -p 系 CLI は起動の形も通信の中身も変えない (ADR-0005)。

## 結果
- `claude_auth.py` の自前 refresh を削除 (#115)。9router を廃止 (ADR-0002)。
- **残り**: Codex の jail は、host の broker が `auth.json` のトークンを読んで upstream に付ける形 (`codex_subscription.py`)。(1) の形に寄せられるかを v2 で詰める。

# 共通 ACP driver

公式 SDK、ACP、print CLI の順に実行インターフェースを選ぶ。
agy、Cursor CLI、Grok Build を ACP で使うには、route に `driver: cli.acp` と `profile` を指定する。
Claude、Codex、Cursor SDK の route は従来の driver を使う。
ACP 非対応の CLI を使う場合は `cli.print` を選ぶ。
選択した driver の起動やネゴシエーションが失敗した場合は、呼び出し元へ失敗を返す。

## 設定とプロセス

`PrintCliProfile.acp` は、実行ファイルに続く引数、必要な固定環境変数、ログイン済み home に対応する認証方式を宣言する。
実行ファイル、インストール、専用ログイン home、egress の許可先、利用枠取得、データ利用方針は既存の profile と connection の設定を共用する。
バイナリの上書きは `drivers["cli.print"].profile_binaries`、jail のイメージは `drivers["cli.print"].cells.coding.image_id` で指定する。
いずれも `execution.json` の機械設定である。
ホストチャットでは、alias に実在する `cwd` を設定する。

共通クライアントは 1 行 1 オブジェクトの JSON-RPC 2.0 を読み書きし、`initialize` で ACP v1 を確認する。
制御 RPC の上限は各 30 秒、1 行の上限は 1 MiB で、turn 全体には alias または attempt の時間・出力上限も適用する。
Agent が `auth_required` を返した場合は、profile が許可し、Agent が公開した非対話方式に限り `authenticate` を使う。
ログイン操作自体は既存のログインフローで行う。

`session/update` のテキストは逐次配信し、tool call、状態更新、計画、思考の通知は活動イベントに変換する。
`usage_update` のコンテキスト使用量とセッション累計費用は、turn 単位の入出力 token 数や費用とは別の値として扱う。
turn の使用量は `session/prompt` の結果の標準の `usage` を読む。
標準の欄に置かない Agent は、profile の `AcpSpec.usage_parser` が名指す hook で拡張から取り出す。
Grok Build 1.0.89 は `_meta.usage` に token 数と `costUsdTicks` (1e-10 USD) を置く。
Cursor Agent 2026.11.19 の ACP は使用量を返さないため、Cursor の turn の使用量は不明として記録する。
取消時は `session/cancel` を送信し、短い応答待ちの後に起動したプロセスを回収する。
CLI の親プロセスが先に終了した場合も、stdout や stderr を保持する同一セッションの子孫を回収する。 [ACP v1 schema](https://agentclientprotocol.com/protocol/v1/schema)

## チャットと jail

ホストチャットでは会話キーごとに 1 セッションを保持し、同じ会話の turn は直列に実行する。
プロセスは最大 16 個で、最後の turn から 120 秒で停止する。
全体または専用 home の上限に達したときは、該当する待機中のプロセスを回収できる場合に限り新たに起動する。
次の turn では、`loadSession` を公開した Agent に対して保存済み ID で `session/load` を呼ぶ。
このとき Agent が再送する履歴は新しい応答に含めない。
再開機能がなければ `session/new` で新しいセッションを作り、リクエストにある履歴を渡す。
会話の再利用は route、接続、cwd、MCP、ツール方針に束縛する。

alias の MCP 設定は、設定済みの参照とヘッダーを解決して `mcpServers` に渡す。
stdio の環境変数と HTTP・SSE のヘッダーは ACP の名前・値の配列へ変換する。
HTTP・SSE を指定した場合は、その transport が Agent の capabilities にあることを確認する。

`/v1/executions` の isolated alias では、既存の CLI コンテナと CONNECT relay の内側で Agent を起動し、`/workspace` を cwd にして attempt ごとに 1 turn を実行する。
起動記録、出力、イベント、時間・リソース制限、終了確認には既存の CLI 実行契約を使う。
実行 API の provider は共通の `print_cli` として扱い、使用するプロトコルは attempt の snapshot に固定する。

## ツール許可

`session/request_permission` は、`tools` と `deny` で tool call の `title` を完全一致で照合する。
通知済みの同じ tool call ID の情報を使って、依頼で省略された項目を補う。
`deny` には ACP の `kind` も指定できる。
`read` と `write` では読み取り・検索・思考を、isolated の `write` では編集・削除・移動・実行も許可候補にできる。
既定は拒否で、許可・拒否とも 1 回限りの選択肢を使う。
適切な選択肢がなければ取消を返し、判断は活動イベントに記録する。

クライアント側のファイル・端末機能は未対応として交渉する。
許可応答の対象は Agent が問い合わせた操作である。
Agent 自身が持つファイルアクセスや通信に OS レベルの境界が必要な場合は、jail のマウントと egress 制限を使う。

## ベンダーごとの接続

| Profile | 起動とモデル選択 | 公開資料で確認した機能 |
| --- | --- | --- |
| agy | `agy acp`。ACP のモデル選択肢に route のモデルを指定する。 | キャッシュ済みログインとセッションの読み込みに対応する。HTTP MCP を渡す場合は `--process-per-session` を付ける。[Antigravity ACP](https://antigravity.google/docs/ide/extensions/acp/) |
| Cursor | `agent acp`。ACP のモデル選択肢に route のモデルを指定する。 | `cursor_login`、`session/load`、tool permission、MCP を公開している。[Cursor ACP](https://prod.cursor.com/docs/cli/acp) |
| Grok Build | `grok agent --no-leader --model <model> stdio`。専用プロセスを使う。 | セッション作成・再開、tool permission、モデル起動引数を公開している。利用枠は既存の `_x.ai/billing` probe を使う。[Grok Agent mode](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/15-agent-mode.md) |

再開と HTTP・SSE MCP の可否は接続ごとの capabilities を使う。
固定した CLI バージョンでの認証、モデル選択、実際の応答、および jail の外向き宛先は、運用者が実 CLI で確認する。

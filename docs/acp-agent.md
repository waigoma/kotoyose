# ACP エージェント

`kotoyose-acp` は、起動済みの Kotoyose サーバーに ACP v1 クライアントを接続するコマンドです。
クライアントがコマンドを起動し、標準入出力で通信します。
API キーで利用できる Kotoyose の alias が、セッションのモデル選択欄に並びます。

道具の呼び出しは ACP の tool call として、本文と同じ順でクライアントに表示されます。
turn の使用量と費用も、取れる alias では応答に載ります。

## ツールの実行承認

alias に `approval: client` を設定すると、サーバーのツールポリシーで許可され、ランタイムが承認を求めた呼び出しを ACP クライアントで確認できます。
T3 Code ではスマホから接続した画面でも、表示された呼び出しを 1 回だけ許可するか拒否するかを選べます。
既定の `approval: server` では、従来どおりサーバー側のポリシーで処理します。

対象は Kotoyose サーバー上で動く direct チャットの Claude Agent SDK と `cli.acp` です。
Claude の `permission_mode` は維持されます。`bypassPermissions` の alias では、クライアントに確認を求めません。
Codex / Cursor SDK と `ask_agent` / `start_agent` で委譲されたターンはサーバー側のポリシーを使います。

`kotoyose-acp` はチャットの SSE に `X-Kotoyose-Events: 1` と `X-Kotoyose-Approvals: 1` を付けます。
どちらかが欠ける場合や非ストリーミングのチャットでは、クライアント承認を必要とする呼び出しを拒否し、ツールの応答に理由を添えます。
クライアントが承認を取り消した場合、300 秒以内に回答しなかった場合、ターンを取り消した場合、ストリームが閉じた場合も拒否します。
ターンの残り時間が短い場合は、プロバイダーに拒否を返せるよう承認期限を繰り上げます。

独自のクライアントは SSE の `permission_request` イベントに含まれる `request_id` を使い、`POST /v1/chat/approvals/{request_id}` に `{"decision":"allow_once"}` または `{"decision":"reject_once"}` を送れます。
同じ Client ID で認証した回答を 1 回だけ受け付け、成功時は HTTP 204 を返します。
不明・期限切れ・使用済み・別の Client の ID には HTTP 404 を返します。
承認待ちは Client ごとに最大 64 件で、サーバープロセスごとにメモリに保持します。
複数ワーカー構成で回答が別のワーカーに届くと HTTP 404 になり、有効な回答が期限内に届かなければツール呼び出しを拒否します。

## 会話の引き継ぎと effort

alias を途中で変えると、Kotoyose はその alias の新しい runtime session を始めます。
SDK の alias では、それまでの発言を新しい session の最初の入力の先頭に付けます。
`/v1/models` が `reasoning_efforts` を返す alias には、Effort という選択が加わります。
Default 以外を選ぶと、その値が `reasoning_effort` としてチャット API へ渡り、会話キーにも入ります。
選んだ値が新しい alias の候補に無いときは、Default に戻します。
`fast` を返す alias には、Fast という選択が加わります。
On にすると `service_tier` が `priority` としてチャット API へ渡り、会話キーにも入ります。
Fast を出さない alias へ切り替えると、Off に戻します。

## 会話の保存と再開

`kotoyose-acp` は、完了または取消した turn の履歴を、エージェントを起動した機械の状態ディレクトリに `sessions/<session-id>.json` として保存します。
取消した turn はユーザーの発言までを保存します。
接続に使う API キーや HTTP ヘッダーは保存しません。

クライアントが `session/load` に対応していれば、同じ作業ディレクトリで保存済みの会話を開き直せます。
履歴を表示したあと、保存したモデルとオプションで会話を続けます。
利用できなくなった設定があれば、既定値に戻したことを会話欄に表示します。
クライアントが `session/list` に対応していれば、更新日時の新しい順に会話を一覧できます。

状態ディレクトリは Linux と macOS では `$XDG_STATE_HOME/kotoyose-acp`、`XDG_STATE_HOME` が未設定の場合は `~/.local/state/kotoyose-acp` です。
Windows では `%LOCALAPPDATA%\kotoyose-acp` です。
保存先は `KOTOYOSE_ACP_STATE_DIR` で変更できます。
更新から 30 日を超えた履歴と、更新順で 200 件を超えた古い履歴は、次の保存時に削除します。
ファイルが 8 MiB を超える場合は、収まる範囲の最新の発言を残し、切り詰めたことをファイルに記録します。

履歴を削除するには、クライアントで対象の会話を閉じてエージェントを終了し、`sessions` 内の該当する JSON ファイルを削除します。
すべてのローカル履歴を削除するには、同じ状態ディレクトリを使うエージェントを終了してから `sessions` ディレクトリを削除します。

## どこで動かすか

ACP クライアントは、エージェントのコマンドを自分と同じ機械で起動します。
コーディングに使う場合は、ACP クライアントの server と Kotoyose とリポジトリを同じ機械に置きます。
T3 Code なら、その機械で `t3 serve` を動かし、画面は別の端末やスマホから接続します。

## 環境変数とオプション

| 設定 | 内容 |
| --- | --- |
| `KOTOYOSE_URL` | Kotoyose サーバーのベース URL です。既定値は `http://127.0.0.1:8000` です。 |
| `KOTOYOSE_API_KEY` | サーバーへの Bearer 認証に使用します。これか `KOTOYOSE_API_KEY_FILE` のどちらかが必須です。 |
| `KOTOYOSE_API_KEY_FILE` | API キーを書いたファイルのパスです。持ち主だけが読める (`chmod 600`) ファイルにします。クライアントの設定にキーの値を書かずに済みます。 |
| `KOTOYOSE_ACP_STATE_DIR` | 会話履歴の保存先を上書きします。指定したディレクトリの `sessions` に保存します。 |
| `--model <alias>` | セッションの初期 alias を指定します。省略すると `/v1/models` の先頭の alias を選びます。 |
| `--forward-cwd` | クライアントが指定した作業ディレクトリをチャット API に渡します。既定では無効です。 |

## T3 Code

1. **Settings → Providers → Add provider → Show more → Local ACP command** を開きます。
2. 実行ファイルに Kotoyose の仮想環境内の `kotoyose-acp` の絶対パスを指定します。
3. 環境変数の上書き欄に `KOTOYOSE_URL` と、`KOTOYOSE_API_KEY_FILE` (または `KOTOYOSE_API_KEY`) を設定します。

## Zed

Zed の設定で `agent_servers` にカスタムエージェントを追加します。
下のパスとキーを環境に合わせて置き換えます。
初期 alias を指定する場合は `args` に `["--model", "my-alias"]` を設定します。

```json
{
  "agent_servers": {
    "Kotoyose": {
      "type": "custom",
      "command": "/path/to/kotoyose/.venv/bin/kotoyose-acp",
      "args": [],
      "env": {
        "KOTOYOSE_URL": "http://127.0.0.1:8000",
        "KOTOYOSE_API_KEY": "YOUR_API_KEY"
      }
    }
  }
}
```

## 現在の制限

- クライアントが指定した MCP サーバーは無視します。MCP はサーバー側の alias で設定します。
- 作業ディレクトリの上書きには `--forward-cwd` と、上書きを許可する alias が必要です。
- 画像を利用できるかは、選んだ alias の対応範囲で決まります。

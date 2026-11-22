# ACP エージェント

`kotoyose-acp` は、起動済みの Kotoyose サーバーに ACP v1 クライアントを接続するコマンドです。
クライアントがコマンドを起動し、標準入出力で通信します。
API キーで利用できる Kotoyose の alias が、セッションのモデル選択欄に並びます。

道具の呼び出しは ACP の tool call として、本文と同じ順でクライアントに表示されます。
turn の使用量と費用も、取れる alias では応答に載ります。

## 会話の引き継ぎと effort

alias を途中で変えると、Kotoyose はその alias の新しい runtime session を始めます。
SDK の alias では、それまでの発言を新しい session の最初の入力の先頭に付けます。
`/v1/models` が `reasoning_efforts` を返す alias には、Effort という選択が加わります。
Default 以外を選ぶと、その値が `reasoning_effort` としてチャット API へ渡り、会話キーにも入ります。
選んだ値が新しい alias の候補に無いときは、Default に戻します。

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

- ツールの実行許可は、サーバー側の alias ポリシーが決めます。クライアントへの承認要求は送信しません。
- `session/load` に対応していません。会話履歴はエージェントプロセス内に保持します。
- クライアントが指定した MCP サーバーは無視します。MCP はサーバー側の alias で設定します。
- 作業ディレクトリの上書きには `--forward-cwd` と、上書きを許可する alias が必要です。
- 画像を利用できるかは、選んだ alias の対応範囲で決まります。

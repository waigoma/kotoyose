# 0005 -p 系 CLI は共通 driver とデータのプロファイルで

- 状態: 採用
- 日付: 2026-11-15 (所有者「jail の中で agy -p で。トークンを抜くと ban なので絶対に -p で。-p 系は今後も増えるので agy 以外も対応できるように」「agy -p と挙動を変えないように」)

## 決定
- 共通 driver `cli.print` と、CLI ごとの違いを持つ `PrintCliProfile` (データ) に分ける。プロファイルは起動の引数・surface ごとの許可 (`--settings` の allow-rule)・出力の parser・ログインの home を指す env・ログイン / 残量 / モデル一覧のコマンド・終了コード・残量切れの判定・出口の許可先・同時実行数を持つ。
- 子の env は allowlist (PATH・LANG・TERM)、プロファイルの固定値、login home から作る。作業場には何も書かない。
- jail では、本体を ro で bind、connection の login home を `/home/agent` に rw で bind (Kotoyose は読まない)、argv は host と同じ関数で作る。出口は `print_cli_egress`: `CONNECT host:port` の行だけを見て、プロファイルに完全一致する許可先とポート、および公開アドレスなら素通しの tunnel。無通信の上限もプロファイルで指定する。TLS はほどかず、ヘッダーも中身も触らない。

## 結果
- agy は `HTTPS_PROXY` に従い、直接の通信は 0 件 (strace で確認)。許可先は実測の 6 ホスト。うち `oauth2.googleapis.com` は agy 自身のトークン更新で、host の実測ではトークンが新しく出ず、jail で塞いで初めて分かった。**許可先を決めるときは更新の時も見ること。**
- 新しい -p 系 CLI はプロファイル 1 つ + connection + route で足せる。

## プロファイルと実行ファイルの解決

- `cli.print` の route には `profile` を指定する。参照先の subscription connection にも同じ `profile` を指定する。
- API キー connection で `profile` が省略されている場合は、参照元 route のプロファイルが 1 種類に定まる場合に限り使う。CLI ログインやクォータ確認に使う subscription connection には `profile` を明記する。
- `execution.json` の `drivers["cli.print"].profile_binaries` にプロファイル ID と実行ファイルのパスを設定できる。上書きがなければ管理インストールの宣言に従う。管理インストールの宣言がないプロファイルでは実行ファイルを `PATH` から探す。
- プロファイルは `parser`、`classify_stderr`、`parse_models`、`parse_quota`、`login_flow` を名前で参照する。残量コマンドやパーサーを持たないプロファイルの残量は不明として扱う。現在の対話ログインの種別は `paste_code`。

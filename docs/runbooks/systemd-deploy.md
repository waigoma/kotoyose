# systemd で Kotoyose を運用する

専用のサービスユーザーを用意し、そのユーザーが管理するディレクトリ
(例 `/srv/kotoyose`) にリポジトリを配置する。同じディレクトリで
`uv sync --locked` を実行し、`.env.example` を元にサービスが使用する環境変数を設定する。
環境ファイルの読み取り権限は、管理者とサービスの運用に必要な範囲に限る。

## サービスユニット

次の例を `/etc/systemd/system/harness.service` に配置する。ユーザー名、ディレクトリ、
ポートは環境に合わせる。

```ini
[Unit]
Description=Kotoyose
After=network.target

[Service]
Type=simple
User=kotoyose
WorkingDirectory=/srv/kotoyose
EnvironmentFile=/etc/kotoyose/kotoyose.env
ExecStart=/srv/kotoyose/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now harness.service
curl --fail http://127.0.0.1:8000/health
```

依存関係を更新したらサービスを再起動し、`/health` と
`journalctl -u harness.service` で起動を確認する。ユニットを編集した場合は
再起動の前に `systemctl daemon-reload` を実行する。

## native 実行と user scope

native 実行が systemd の user scope を使う場合、サービスと同じ UID の user manager と
D-Bus ソケットが必要になる。既存の user manager を確認してから、サービスの環境に
`XDG_RUNTIME_DIR=/run/user/<UID>` と
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<UID>/bus` を設定する。
`<UID>` にはサービスユーザーの UID を使用する。

変更後はサービス環境と同じ UID・環境変数で `systemctl --user show -p Version` が
成功することを確認する。診断ログには環境変数の一覧や認証情報を出力しない。
過去の Attempt の停止を確認するには、その Attempt の PID や scope の
識別情報と停止の証拠が必要になる。復旧後の疎通だけで過去の停止を確定しない。

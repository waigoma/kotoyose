# 0006 console のログインは公式 CLI を疑似端末で中継する

- 状態: 採用
- 日付: 2026-11-15

## 決定
- console の Log in / Issue token は、公式 CLI (`claude auth login`・`claude setup-token`) を疑似端末で起動し、出てきた URL を画面に出し、貼られたコードを CLI に渡す。交換と保存は CLI が行う。Kotoyose は端末の代わりをするだけ (`cli_login.py`)。
- Codex は公式 app-server の device code (`account/login/start`)。
- URL の state・コード・トークンはログに残さない。1 connection 1 セッション、期限付き。agy はコードを 60 秒しか待たない。

`cli.print` はプロファイルで 3 つのフローを宣言する。`paste_code` (agy) では画面から貼り付けた認可コードを PTY に送る。`browser_wait` では公式 URL を開いてブラウザーで認証し、`device_code` では公式 URL と CLI が発行した短いユーザーコードを画面に表示する。後者 2 つは CLI の終了後にプロファイルのステータスコマンドで認証を確認する。画面に出す URL はプロファイルの許可ホストに限定する。

## 結果
- ログインのたびにターミナルへ行かなくてよくなった。child を足すときは、この流れを child へ中継する (ログインは child の上で行い、トークンは運ばない)。

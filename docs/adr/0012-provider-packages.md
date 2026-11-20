# 0012 AI の入口を provider package として宣言で足す

- 状態: 採用
- 日付: 2026-11-18 (所有者「あとで追加しやすいように必ず共通化処理や plugin を見据えた設計に」「AI は subscription の cli の -p か oauth 的なやつか token, api key で使えるようになっていて欲しい。全てのルートが使えるように」「導入は webui 上から」「無料枠は mcp とか制限をかける」)
- 設計: [docs/design/provider-packages.md](../design/provider-packages.md)

## 背景
サブスク CLI の `-p`、公式 token、SDK、API key / endpoint の 4 つの入口が別々の作りで生えている。`-p` 系 CLI は ADR-0005 で共通 driver にしたが、載っているのは agy だけで、agy 固有の分岐が runner・probe・login・egress・model 発見に散らばっている。Cursor CLI と Grok Build を v1.0 に入れるにあたり、足すたびに同じ場所を触る作りを先に畳む。

## 決定
1. AI 製品を **provider package** という単位で足す。中身は宣言 (auth method・runtime・installer・data policy) と、宣言で書けない部分のコード (出力の parser、SDK の session)。builtin も同じ宣言で書く。新しい型を権限の根拠にせず、今の `PrintCliProfile` を宣言として育てる (実行は今どおり `driver + profile + connection + model` で解決し、受付の検査を通る)。
2. 認証は `cli_login` / `official_token` / `api_key` / `endpoint` の 4 種、実行は `print_cli` / `sdk` / `http` の 3 種。runtime ごとに使える認証・place・surface を宣言する。
3. 公式 SDK も同じ考えで揃える。SDK は公式 CLI を子プロセスで動かす薄い層なので、共通の runner と provider ごとの差し替え口 (auth・options・events・control・resume・egress の 6 つ) に分け、chat (host) と実行 (jail) が同じ adapter を使う。
4. 共通エンジン (print_cli runner、login の 3 流れ、installer、egress、probe、data policy) は宣言だけを見て動き、provider 固有の分岐を持たない。
5. 外部の宣言は plugin に同梱し、所有者の grant (version + digest) を通ったときだけ効く (実行するバイナリと通信先を宣言が決めるため)。
6. 導入は WebUI から。公式 CLI は Kotoyose の tools ディレクトリに版を固定して入れる。入れてよいのは検証できる配布物 (integrity を確かめる npm package、hash / 署名を確かめた binary) だけで、`curl | bash` を Kotoyose のユーザーで動かさない。
7. 学習に使われうる経路 (`may_train`、`unknown` も同じ) は実行時に allow を `none` に固定し、MCP・道具・作業場を渡さない。
8. サブスクの token をクライアントへ渡す経路は作らない (ADR-0001)。API key / endpoint は将来、鍵を付けて中継する proxy でクライアント側実行を許す。
9. 既存の Codex の jail は broker が `auth.json` を読んで認証を付けており ADR-0001 に反する。新しい provider より先に CONNECT 素通しへ移す。
10. v1 では外部の provider plugin を読み込まない (差し替え口の形だけ作る)。plugin の grant は隔離ではなく、所有者が入れたコードを信頼する以上の保証は無い。

    2026-11-20: the experimental kotoyose.plugin/1 loader was removed; external provider packages will be redesigned as kotoyose.provider/1 after v1.0 ([see design record](../design/plugin-architecture-2026-11.md)).

## 結果
- ADR-0005 を一般化する (置き換えではなく拡張。`PrintCliProfile` は `print_cli` runtime の宣言になる)。
- ADR-0010 の「Cursor adapter は v1 の対象外」を改め、Cursor CLI・Grok Build を v1.0 に入れる。
- 進め方は設計書の P0〜P6 (9 章)。P0 は `cli.print` の共通化で、agy の振る舞いは変えない。

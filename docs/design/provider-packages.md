# Provider package: AI の入口を 1 つの部品として足す

- 状態: 提案 (ADR-0012)。外部パッケージの検討は保留。 [2026-11-20 の設計記録](plugin-architecture-2026-11.md)
- 日付: 2026-11-18
- 関連: ADR-0001 (サブスクは公式 CLI だけ)、0004 (connection)、0005 (-p 系 CLI の共通 driver)、0006 (console login)、0007 (残量は公式の口)、0010 (実行の軸)、0011 (一般化)

## 1. 何を解きたいか

所有者の願い: **どの AI も「サブスクの公式 CLI の `-p`」「OAuth 的な公式 token」「API key」「OpenAI 互換の endpoint」のどれでも使え、新しい AI を後から足すのが簡単であること。** 導入 (install) と login は WebUI から行う。無料枠のように学習に使われうる経路は、道具や MCP を自動で絞る。

今の Kotoyose は、この 4 つの入口が別々の作りで生えている。

| 入口 | 今の実装 | 足すときに触る場所 |
|---|---|---|
| サブスク CLI `-p` | `cli.print` + `PrintCliProfile` (agy だけ) | profile、parser、stderr 分類、quota probe、model 発見、login、egress。agy 固有の分岐が 6 か所に散らばる |
| 公式 token (Claude setup-token) | `claude.agent-sdk` + secret | Claude 専用のコード |
| SDK (Claude / Codex / Cursor) | `RuntimeSession` の builder (host だけ) | Python のコード |
| endpoint / API key | connection `kind: api_key` / `endpoint` | provider ごとに分岐 |

新しい `-p` 系 CLI (Cursor `agent -p`、Grok Build) を足すたびに、これらを少しずつ触ることになる。**足す側の仕事を「1 つの宣言 (+ 必要なら parser 1 つ)」に縮める**のがこの設計の目的。

## 2. 用語

- **Provider package**: 1 つの AI 製品 (Claude, Codex, Antigravity, Cursor, Grok, 任意の OpenAI 互換 endpoint) を Kotoyose に足す単位。宣言 (データ) と、宣言では書けない部分のコード (parser・SDK の session) からなる。
- **Auth method**: その製品に「どう認証するか」。`cli_login` / `official_token` / `api_key` / `endpoint` の 4 種。
- **Runtime**: その製品を「どう動かすか」。`print_cli` (公式 CLI の非対話モード) / `sdk` (公式 SDK) / `http` (HTTP API を Kotoyose が直接呼ぶ) の 3 種。runtime ごとに、使える auth method・place (host / jail)・surface を宣言する。
- **Installer**: 公式 CLI の入れ方。
- **Data policy**: その経路の入力が学習に使われうるか。

既存の用語との対応: connection (ADR-0004) は「auth method の 1 つを、あるアカウント・鍵で実体化したもの」。route は「runtime + connection + model」。alias は今どおり利用者に見せる名前。

## 3. 宣言の形 (ProviderSpec)

builtin の provider も外部 plugin も、同じ宣言で書く (builtin を特別扱いしない。足す側の手本になる)。

```yaml
contract: kotoyose.provider/1
id: grok                      # provider id。connection.provider と同じ文字列
name: Grok Build
family: grok                   # console のカードの家族
auth:
  - kind: cli_login           # 公式 CLI の login。Kotoyose は認証ファイルを読まない
    flow: device_code         # paste_code | browser_wait | device_code
    command: [login, --device-auth]
    home_env: GROK_HOME       # login home を指す env。無ければ HOME を差し替える
    url_pattern: 'https://\S+'
    code_pattern: '\b[A-Z0-9]{4}-[A-Z0-9]{4}\b'
    status: {command: [models], success_exit_code: 0}
runtimes:
  - kind: print_cli
    auth: [cli_login]
    places: [host, jail]
    binary: grok
    min_version: 1.0.89
    argv: [--output-format, streaming-json, -m, '{model}', '{extra_args}', '--single={prompt}']
    surfaces:
      read:  {args: [--permission-mode, dontAsk], allow: [read]}
      write: {args: [--permission-mode, bypassPermissions]}
    output: {format: ndjson, parser: grok_streaming_json_v1}   # 宣言の mapper か、名前付きの parser
    env: {}                    # 固定で足す env (例: Cursor の NODE_USE_ENV_PROXY=1)
    egress: {hosts: [cli-chat-proxy.grok.com, auth.x.ai], ports: [443], idle_seconds: 900}
    errors: {quota: [...], auth: [...], exit_codes: {...}}
    quota: null                # 公式の口が無ければ書かない (ADR-0007: 推測しない)
    models: {command: [models]}
install:
  method: npm_platform_binary
  version: 1.0.89
  packages:
    linux-x64: {name: '@xai-official/grok-linux-x64', integrity: 'sha512-…'}
    linux-arm64: {name: '@xai-official/grok-linux-arm64', integrity: unset}
  member: package/bin/grok.br
  compression: brotli
```

宣言はデータなので、
- **digest を取れる**。外部の宣言は所有者の grant (version + digest) と合ったときだけ効く。実行するバイナリ・通信先・login の方法を宣言が決めるので、builtin 以外は必ず grant を通す。
- **console に出せる**。Providers ページの「追加」は builtin の宣言と grant 済みの宣言の一覧から選ぶ。

宣言で書けない部分は、名前を付けたコードとして plugin が登録する。
- `parser`: CLI の出力 (NDJSON 等) を Kotoyose の event (text / tool / usage / result / session id) に写す関数。単純な CLI は宣言の mapper (JSON のキーの対応表) で済ませ、コードは要るときだけ書く。
- `sdk` runtime の session factory。

## 4. 共通エンジン (足す側は触らない)

| エンジン | 役目 | 今のどこから作るか |
|---|---|---|
| print_cli runner | argv の組み立て、process の起動・期限・停止、parser の呼び出し、終了の判定 | `print_cli_agent.py` から agy 固有の分岐を抜く |
| login | 3 つの流れ。`paste_code` (agy: URL を出し、利用者がコードを貼る)、`browser_wait` (Cursor: URL を出し、CLI の終了を待つ)、`device_code` (Grok: CLI が出すコードを利用者に見せ、終了を待つ) | `cli_login.py` を流れごとの小さな state machine に分ける |
| installer | 宣言の方法で `~/.local/share/kotoyose/tools/<id>/<version>/` に入れる。SHA-512 を検証し、実行ファイルと manifest を版ごとに原子的に配置する。導入は所有者の操作で行う | `cli_installer.py` |
| egress | jail の CONNECT allowlist。host・port・無通信の上限を runtime ごとに | `print_cli_egress.py` の定数を宣言へ |
| probe | login 状態・残量・model 一覧。宣言にある公式の口だけを叩く | `connections_probe.py` / `model_discovery.py` の agy 分岐を宣言へ |
| data policy | `trains` の経路では allow を `none` に固定し、MCP・道具・作業場を渡さない。使える client も所有者が選んだものに限る | 新規 (admission の 1 か所) |

## 5. 4 つの入口の扱い

1. **サブスク CLI `-p` (`cli_login` + `print_cli`)**: ADR-0001 のとおり、公式 CLI を公式 login のまま動かす。Kotoyose は認証ファイルを読まず、login home を CLI に渡すだけ。jail では login home を bind し、通信は宣言の host だけに CONNECT で素通しする (TLS を終端しない)。
2. **公式 token (`official_token`)**: 公式が発行する長期 token (Claude の setup-token)。secret store に置き、公式 CLI / SDK に公式の env 名で渡す。自前で API を叩かない。
3. **API key (`api_key`)**: secret store に置き、SDK・CLI・HTTP に渡す。jail には渡さず broker 側で付ける形を目標にする (ADR-0010 の認証境界)。公式 SDK がそれを許さない場合は host だけにする。
4. **endpoint (`endpoint` + `http`)**: OpenAI 互換などの base_url + key。Kotoyose が HTTP で呼ぶ。道具は Kotoyose 側の tool loop で扱う (将来)。

## 6. 無料枠・学習の扱い

各 connection に `data_policy` を必ず指定する。値は `no_training`、`trains`、`unknown` のいずれかとする。
provider や接続種別による既定値は廃止した。利用するアカウントや endpoint の設定を確認し、学習への利用を確認できない場合は `unknown` を指定する。
`trains` と `unknown` では、テキストのみの chat を提供できる runtime で、道具・MCP・skills・作業場・添付を既定で制限する。
pool では候補ごとに要求された能力を照合する。
旧形式の route 未指定 preset は、connection に束縛された学習ポリシーの適用範囲外とする。

### alias 単位の承諾

所有者は console の alias 編集で、許可する能力を選び、用途を記したメモとともに保存する。
`training_acks` には alias、能力の一覧、承諾者、日時、理由、メモ、digest が残る。
digest は alias の設定、参照先リソース、route、connection、実効ポリシーと、選択した能力の一覧を結び付ける。
対象が変わると承諾は失効し、その能力を付与する設定の読込・保存には再承諾が必要になる。
承諾は runtime が元からサポートする能力に適用する。
作業場へアクセスできる組み込みの道具を指定する場合は、道具と作業場の両方を選ぶ。
console と `/v1/models` には承諾の有無にかかわらず実効ポリシーを表示する。

`cli.print` で API key を使う場合は、profile が `api_key_env` を宣言し、connection の `secret` に secret store の名前を指定する。
Kotoyose は host 起動時にその環境変数へ値を設定する。
API key を要求する profile の jail 実行は設定検証で拒否する。

## 7. 将来: 認証だけ Kotoyose、実行はクライアント側

API key / endpoint の経路では、クライアントが自分の手元で SDK を動かしたい場合がある。鍵を配らずにそれを許すため、Kotoyose が **鍵を付けて中継する proxy** (`/v1/proxy/<connection>/...`) を出す。クライアントは Kotoyose の鍵で認証し、Kotoyose が provider の鍵を付けて転送する。使用量は Kotoyose が数える。

サブスク (`cli_login` / `official_token`) は対象にしない。token をクライアントへ渡すのは ADR-0001 に反し、BAN の危険がある。サブスクを別の機械で使いたいときは、その機械に公式 CLI を入れて login する (child、v2)。

## 7b. sdk runtime の共通契約

公式 SDK は、どれも公式 CLI (または公式の local agent) を子プロセスで動かす薄い層である (Claude Agent SDK → Claude Code、Codex SDK → codex、Cursor SDK → local bridge、Antigravity SDK → agy)。認証は CLI 自身が行うので、`-p` と同じく裏技にならない。Kotoyose には SDK の runtime が 4 つあり (`runtime/claude.py`・`codex.py`・`cursor.py`・`antigravity_sdk.py`)、chat は共通の `RuntimeSession` に載っているが、jail の実行は `container_claude.py`・`container_codex.py` と provider ごとに別の作りになっている。

`print_cli` と同じく、SDK も「共通の runner + provider ごとの差し替え口」に揃える。差し替え口は次の 6 つだけにする。

| 差し替え口 | 役目 | 例 |
|---|---|---|
| auth | connection から、公式 CLI に渡す env 名・設定ディレクトリ・login home を決める。Kotoyose は値を読まない (secret store から公式の env 名で渡すだけ) | Claude: `CLAUDE_CODE_OAUTH_TOKEN` か `CLAUDE_CONFIG_DIR`、Codex: `CODEX_HOME` |
| options | model・allow (surface)・道具・作業場を SDK の options に写す | Claude の permission_mode / disallowed_tools、Codex の sandbox / approval |
| events | SDK の stream を Kotoyose の event (text / tool / usage / result / session id) に写す | |
| control | 停止・期限・子プロセスの回収 | |
| resume | session id の取り出しと再開 | |
| egress | jail で素通しする公式の host | Claude: api.anthropic.com 等 |

これで chat (host) と実行 (jail) が同じ adapter を使い、`container_claude` / `container_codex` の provider 固有の起動は JailPlace の共通の起動に畳める。新しい SDK を足す仕事は、この 6 つを書くことになる。

ホストの `cli.print` route も `RuntimeSession` を通じて `/v1/chat/completions` の同期応答と SSE に対応する。
実行 API と同じ print CLI ワーカーを使い、プロファイルの `read` surface で、ターンごとに空の専用一時ディレクトリから起動する。
設定された login home を使い、子プロセスには最小限の環境変数とプロファイルが指定する環境変数を渡す。
system・過去の発言・今回の user message は 1 つのテキストにまとめる。現在のプロファイルは画像・添付に対応していない。
毎回新しい `-p` プロセスを起動するため resume は非対応。CLI が返すセッション ID は実行記録に残す。

## 8. レビューで直したこと (2026-11-18、gpt-6.2-sol の静的レビュー)

1. **新しい宣言の型を権限の根拠にしない。** `ProviderSpec` を別に作ると、既存の受付 (`execution_admission`) をすり抜ける道ができる。今の `PrintCliProfile` を builtin の宣言として育て、実行は今どおり `driver + profile + connection + model` で解決する。上の 3 章の YAML は「最終的に宣言が持つ項目」の見取り図として残す。
2. **jail の中は標準ライブラリだけ。** `agent_spec` → `registry` は jail 内の python からも読まれる。宣言の検証 (pydantic、YAML、entry point) は host に置き、jail には検証済みの実行データだけを渡す。
3. **read を本当に読み取り専用にする。** 今の `cli.print` の jail は作業場を書き込み可で mount する。CLI に渡す権限の引数だけでは足りないので、read の surface では作業場を ro で mount する。
4. **実行物の版を固定する。** 今は driver ごとに実行ファイル 1 つを bind する。runtime ごとに版と依存一式 (node の package など) を固定した置き場を持ち、更新は原子的に切り替え、実行中の run は旧版のまま終える。
5. **login home の排他。** 同じ login home を使う host の run・jail の run・login・probe を、connection 単位でまとめて排他する (CLI の認証ファイルの更新が衝突しないように)。
6. **resume は初期版では非対応と明示する。** parser は session id を拾えるが、worker は返していない。
7. **導入は検証できる配布物だけ。** `curl | bash` を Kotoyose のユーザーで動かす方式はやめる。版を固定した npm package (integrity を確かめる) か、hash / 署名を確かめた binary だけを入れる。公式が script しか出していない製品は、資格情報を持たない使い捨ての container で script を動かし、取り出した成果物の hash を所有者が承認する。
8. **既存の Codex の jail は ADR-0001 に反する。** broker が `auth.json` を読み、上流への通信に認証を付けている (`codex_egress.py`)。公式 CLI に自分で認証させる CONNECT 素通しへ移す (ADR-0010 段 3 の残り)。新しい provider より先にやる。
9. **外部 provider パッケージは v1.0 後に再設計する。** 信頼境界と再開条件は設計記録に残した。 [外部 provider パッケージ設計記録](plugin-architecture-2026-11.md)
10. **学習の制限は新しい経路を出す前に。** `unknown` を公開するなら、制限 (chat と実行の両方、pool の候補ごと、入力と道具を組み立てる前) を先に入れる。道具を止められない runtime は `trains` の経路で拒む。

## 9. 進め方 (B3、各段で本番に入れて確かめる)

| 段 | 内容 | 完了の条件 |
|---|---|---|
| P0 (完了) | `cli.print` の共通化。profile と版付き実行物の解決を route・login・model 発見・probe で 1 つに。agy 固有の stream・stderr・models・quota を名前付きの差し替え口へ。login の 3 流れと状態確認。jail の実行物の bind・固定 env・無通信の上限・read の ro mount。login home の排他 | agy の host / jail が今どおり動く。試験用の 2 つ目の profile が core に分岐を足さずに host / jail で通る |
| P1 | Codex の jail を CONNECT 素通しへ (Kotoyose が `auth.json` を読まない) | Codex の Task が本番で PR まで通る。broker が認証を付けない |
| P2 | connection の認証の一般化 (`print_cli` で API key も使える等) と学習の制限 | `trains` / `unknown` の経路で道具が渡らないことを試験で示す |
| P3 (host 実装済み) | Cursor CLI (`agent -p`) の host プロファイルと console のブラウザーログイン。偽 CLI で read / write の引数、イベント、認証状態、モデル一覧を検証 | 実機で公式 login と read / write を検証し、jail で許可外の通信が拒まれることを確認 |
| P4 (実装済み) | Grok Build 1.0.89 の host / jail プロファイルと device-code login | 実機で認証、モデル一覧、read / write、egress と長時間ストリームを確認 |
| P5 (Grok 対応) | Providers から Grok の Linux x64 版を SHA-512 検証付きで導入。Linux arm64 は integrity の確定後に有効化 | 実機で導入から login までを確認。agy・Cursor の管理導入は検証可能な配布物の特定後に対応 |
| P6 | sdk runtime の共通契約 (7b 章) に Claude・Codex・Cursor・Antigravity SDK を載せ、jail の起動を JailPlace の共通の起動に畳む。Cursor SDK を 1.3.1 へ (API key) | chat と jail が同じ adapter を使う。console の Providers がすべて同じ形 |

Brotli で圧縮された配布物の導入には、Kotoyose ホストで実行できる Node.js が必要になる。
ホストの `cli.print` route は chat でも利用できる。同期応答と SSE は偽 CLI で検証する。

公開の仕組み (release branch → 許可リストのファイルだけを公開リポジトリへ) は B3 と別の作業にする。

## 10. 先送りするもの (形だけ用意し、v1 では作らない)

- 外部の provider plugin の読み込み (差し替え口と宣言の形は v1 で作る)
- 出力の対応表 (JSON mapper) の宣言。v1 の parser はコード
- `http` runtime の道具の loop (v1 は chat だけ)
- 鍵を付けて中継する proxy (7 章)

## 11. 捨てた案

- **宣言を YAML ファイルだけで足せるようにする案**: 実行するバイナリと通信先を決める宣言なので、ファイルを置くだけで効くのは危ない。
- **install を OS の package manager に任せる案**: 版の固定と jail への持ち込みができない。
- **`curl | bash` をそのまま動かす案**: Kotoyose のユーザーで任意の script を実行することになる (8 章の 7)。

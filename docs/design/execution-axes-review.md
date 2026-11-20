# 実行の軸 設計書への Fable のレビュー (2026-11-15)

> 2026-11-20 に旧 bearer 認証、旧 host Agent 実行、`defaults` preset を撤去し、alias の `execution: legacy` は `execution: direct` に改名しました。同日に旧管理 UI も撤去しました。以下は当時の記録です。

- 対象: [execution-axes.md](execution-axes.md) の下書き (commit を push した版)
- 形式: 1 回きりのレビュー (往復なし)。所有者の判断の材料として残す
- 反映: 下の「段 0」と問いへの推奨は、所有者の判断の後に設計書本文へ反映する

## 1. 総評

4 軸と 1 runner の形は所有者の意図に合っており、採ってよいです。現状の記述は概ねコードと一致しました。ただし次の 5 点は直してから判断に回すべきです。

- **`host + none` は Claude でしか成り立ちません。** Codex の read-only sandbox と agy の plan は、host のファイルを読めます。「model の手は host に届かない」を証明できた adapter だけ host を許し、ほかは `jail + none` にします。
- **`exec` は「どこまで」ではなく「手をどこで動かすか」です。** CLI は道具なし、shell は credential の無い別 container という形で、Claude の surface `none` も今これで動いています。allow の段ではなく jail の性質 (sealed) として残します。
- **`allow.tools` の 1 本の順序は v2 で破綻します。** browser / desktop / web 検索は `none < read < write` に並びません。`workspace: none|read|write` と追加能力の列に分けます。
- **`place: child:<名前>` は機械と閉じ込めを 1 欄に混ぜています。** 機械は `on:`、閉じ込めは `place` に分けます。connection も機械に属するので、`on` を今から持たせます。
- **web 検索の扱いが抜けています。** `none` で WebSearch も消えるので、アシスタントの機能が落ちます。

作りすぎは 4 つです。

- jail の chat (会話ごとの container)。cron は会話ではなく実行なので `/v1/executions` に移せば v1 では要りません。
- host での MCP 中継。model に道具が無ければ CLI が直接つないでも header は読めません。
- `allow.egress: open` と caller 側の部分集合の計算。
- `Place.reattach` と 9 項目の adapter Protocol を先に固めること。

## 2. 事実の誤り

- **Claude の surface `none` は「作業場なし」ではありません (§2.2、§3.5)。** `none` は host-coding 以外の道具を全部受けます (`agent_spec.py:195-199`、`tool_match="without"`)。sandbox-exec は in-process MCP から worker へ渡ります (`native_claude.py:578-611`)。orchestrator の 43 回は Codex exec ではなく Claude review の可能性があります。
- **Claude の `read` は閉じています (§5.2)。** `agent_spec.py:200` が `_closed("read")` です。`orchestrator-review` を「今の閉じ込めどおり jail + read」とするのは新機能です。
- **host で `bare` を使う案は ADR-0002 と衝突します (§3.4)。** `bare` は `CLAUDE_CODE_OAUTH_TOKEN` を読みません。jail 側は `setting_sources=[]`・`settings=None`・`strict_mcp_config` で `bare` なしです (`native_claude.py:817-835`)。
- **「未確認」の env 継承は 2 つとも丸ごと継承でした (§2.2)。** Codex SDK は `openai_codex/client.py:258-260` (0.160.0) で `os.environ.copy()` します。Cursor SDK は `cursor_sdk/_bridge.py:35` (1.0.35) で `dict(os.environ)` します。
- **agy に `none` はありません (§5.2 の `agy-hydrogen`)。** profile は read / write だけです (`print_cli_profiles.py:255-271`)。`_PRINT_CLI_SURFACES` の none も閉じています。
- **Codex の「host は sandbox read-only に固定」は `none` ではありません (§4.6)。** read-only は shell 実行と全ファイルの読み取りを許します。§3.5 の「弱い近似で通さない」と矛盾します。
- **Claude container の upstream は固定ではありません (§3.7)。** 任意の base URL を `allow_private=True` で受けます (`container_claude.py:132`)。§3.5 の「私設アドレスはどの値でも許さない」をそのまま入れると、LAN の endpoint が壊れます。
- **`/v1/conversations` の `agent_models` は policy があるときだけ効きます (§2.4)。** caller の無い key と旧 bearer は素通りです (`routes/conversations.py:69-73`)。
- **旧 token の利用数は再起動で 0 に戻ります (§5.4)。** memory だけの計数です (`api_key_store.py:119-129`)。配備直後の 0 は根拠になりません。
- **chat の agy は login home が無いと実 `~` を HOME にします (§2.2)。** `runtime/antigravity.py:590` です。

## 3. 危険な点 (重い順)

1. **本番の chat が無防備です。** 全 tool、`bypassPermissions`、Kotoyose の env 継承で動いています。同じ user なので `master.key` と DB から全 secret を復号でき、各 CLI のログインも読めます。MCP の記憶や web 経由の注入 1 回で届きます。設計の完成を待つ理由がありません (§5 の段 0)。
2. **ADR-0001 に触れる経路が orchestrator の本線で動いています。** Claude container の broker は TLS を終端し、header を 5 つに削り、token を付け替えます (`container_claude.py:86-89,135-155`)。Codex の broker は `auth.json` を読みます (`codex_egress.py:258-271`)。設計書は S7 と問い 4 に送っていますが、S3 と S6 より前に置くべきです。
3. **`host + none` を Codex・agy・Gemini・Cursor に適用すること。** 設計書の表どおりに移すと、名前だけ安全な経路ができます。
4. **素通しの jail で shell と login home が同居すること。** 2 つあります。
   - 許可先が多テナントです (`www.googleapis.com`、`api.anthropic.com`)。攻撃者の key で同じ host へ token を送れるので、「持ち出し先は vendor に限られる」は言いすぎです。
   - rw bind した home に model が設定を書けます。同じ home を host の実行でも使うと、jail から host へ抜けます。shell のある jail に見せた home は host で使わない規則が要ります。agy の home には refresh token もあります。
5. **§3.4 の `bare` をそのまま実装すると、setup-token の chat が全部認証で落ちます。**
6. **S6 で bwrap と native worker を消すと sandbox-exec が止まります。** orchestrator は 60 日で 43 回使っています。先に Docker の worker へ載せ替えます。
7. **`legacy_bearer: false` で orchestrator の caller token も止まります。** 旧 bearer の経路は orchestrator の認証も通しています (`execution_http.py:57-66`)。orchestrator の key 移行が先です。
8. **S4 を 1 回の配備にまとめること。** 設定の形、chat の経路、MCP 中継、jail の chat が同時に変わると、失敗の切り分けができません。
9. **pin に place / allow を足すと、全会話が 1 度 `created` になります。** example-client の固定会話は文脈を失います。周知で足ります。

## 4. 未決の問い

1. **jail の実装**: (a) Docker だけ、に賛成です。command 用の `local-docker` worker は残します。
2. **host の allow**: (a) に賛成ですが、さらに絞ります。道具を切れると試験で示した adapter だけ host を許し、ほかは `jail + none` です。web 検索は tools 軸の項目として別に許します。
3. **exec**: (c) です。(b) にしない理由は、範囲 (read / write) と直交する仕組みだからです。review の既定は sealed にします。信頼できない差分を読み、native の道具が要らないからです。
4. **token を jail に入れるか**: (a) に賛成です。ただし 3 つの条件を付けます。
   - shell のある jail は setup-token を優先する。
   - その home を host で使い回さない。
   - 注入に弱い仕事は sealed にする。
5. **上限超え**: 断る、に賛成です。
6. **caller の置き場**: (a) に賛成です。ただし S1 には不要で、chat は今の JSON の callers を読めば足ります。移すのは設定の形を変える段です。
7. **Cursor**: (c) で v1 から外します。SDK の local agent は道具を切れる証明が無く、(b) は ROADMAP で v2 です。推奨の「決まるまで host + none」は、問い 2 と同じ理由で反対です。
8. **Codex の移行**: 2 つに分けます。Claude container の素通し化は v1 必須です。Codex は期限を決めて実測し、通れば v1、詰まれば v2 です。
9. **jail の chat の寿命**: v1 では作りません。作るなら (a) です。
10. **`open`**: v1 では値ごと作りません。使い道が出てから足します。推奨の「値は持つ」には反対です。
11. **アシスタントの shell とファイル**: メモリ更新では、目次からトピック別の記録を必要時に読みます。アシスタントは読み取りを前提にしていますが、`memory_gateway` の読み取りで代替できます。shell は不要と見て `tools: []` にし、困ったら戻します。
12. **print の jail の MCP**: (a) 断る、に賛成です。pool の検証に「候補が同じ道具を扱えること」を足します。
13. **名前**: `jail` に賛成です。`host` は v2 で「親機」の意味とぶつかるので、機械を `on:` に分けるか `direct` に変えます。
14. **Claude の `config_dir` を jail で**: 実測まで保留、に賛成です。問い 4 の理由で、通っても shell のある jail では setup-token を既定にします。

## 5. 移行の段取りへの修正

**段 0 (今日、設定だけ)** を足します。Claude の chat alias に `tools: []` を書きます。

- コードの変更は要りません。`config.py:396-404` が空の列を有効値と明記していて、SDK は `--tools ""` を渡します (`subprocess_cli.py:592-596`)。
- MCP は今のまま動きます。
- `assistant-cron` と `orchestrator-task-agent` は、必要な道具だけの列にします。task-agent は 90 日で 2 回なので、止めても構いません。
- `agy-hydrogen` は chat から外します。
- host の Codex alias (structurer など) は `codex.sandbox: read-only` にします。

これで危険 1 の大半が、設計の採否と無関係に消えます。この hotfix 自体は動かして確かめていません。1 alias で試してから広げてください。

以降の順序は次のとおりです。

- **S2 は launcher ではなく、Kotoyose 自身の env を消します。** 起動時に秘密を読み込んだら `os.environ` から削ります。Claude・Codex・Cursor の SDK は内部で env を写すので、launcher を足すより確実で小さく済みます。段 0 の直後に置きます。
- **S1 を 3 つに分けます。**
  1. caller のある key で chat と `/v1/models` を絞る。
  2. クライアントごとに key を発行する (orchestrator を含む)。
  3. `legacy_bearer: false` にする。利用数は、長く動かした後の値で見る。
- **S7 のうち Claude container の素通し化を、S3 の前に出します。** 許可先は、ログイン更新まで含めて実測します。
- **S3 は「挙動を変えない大改修」をやめます。** runner と `JailPlace` は print の launcher を土台に作ります。その上に print、Claude、Codex の順で 1 本ずつ載せ替えて配備します。経路ごとの置き換えなので、ADR-0009 の「同居」には当たりません。
- **S4 を割ります。**
  - 4a: 会話を `HostPlace` に載せる。段 0 の後なので、挙動の差は小さいです。
  - 4b: 設定の形と変換 script。
  - `assistant-cron` は実行の入口へ移す。
  - jail の chat と host の MCP 中継は v2 へ送る。
- **S6 の前に、sandbox-exec を Docker の worker だけで動かす段を入れます。**
- **各段の合格条件を 1 つの適合試験にします。** adapter × place ごとに、env の dump、host ファイルの読み取り、外への接続を試します。`host` を名乗れる adapter は、この試験で決めます。

## 6. v1 の線への意見

v1 は「どの入口から呼んでも、model の手が host に届かず、サブスクが公式の形だけで動く」に絞るのがよいです。

**足すもの**
- ADR-0001 の残りを閉じること。Claude container の素通し化は必須で、Codex は実測が通れば入れます。ROADMAP は Codex を v2 に置いていますが、ban の危険は機能より重いです。
- adapter × place の適合試験。これを v1 の完了条件にします。
- 全経路での道具の event 記録。今は `orchestrator-pm` しか残っておらず、問い 11 のような判断ができません。
- sealed (今の sandbox-exec) を正式な性質として残すこと。
- `master.key` の控えと復旧を、運用 docs に含めること。

**削るもの**
- console の chat 画面。OpenWebUI があり、v2 の簡単な chat UI と一緒に作るほうが無駄がありません。
- jail の chat、`allow.egress: open`、Cursor と Gemini の adapter。使われていないものは消して、v2 で `cli.print` の 2 本目として戻します。
- snapshot と DB の欄名の統一 (S8 の内部側)。公開の語彙 (設定、`/v1/models`、console) だけ v1 で揃えます。

**v1 で形だけ決めておくもの** (実装は v2)
- `on:` (機械)。connection と alias の両方に持たせ、既定は local にします。
- allow の追加能力の列 (web・browser・desktop)。
- 承認の扱い。「許可を得て computer use」は、既存の `request-approval` を allow のどこに置くかで決まります。

この 3 つを後から足すと、設定の形をもう 1 度壊すことになります。
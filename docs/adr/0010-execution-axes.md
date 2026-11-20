# 0010 実行の軸を model / 道具 / place / allow に共通化する

> 2026-11-20 に旧 bearer 認証、旧 host Agent 実行、`defaults` preset を撤去し、alias の `execution: legacy` は `execution: direct` に改名しました。同日に旧管理 UI も撤去しました。以下は当時の記録です。

- 状態: 採用 (2026-11-15、所有者「推奨で問題ない」。応急処置はせず、段を追って普通に実装する)
- 日付: 2026-11-15

## 背景
今の閉じ込めは、機能を作った時期と経路で決まっていて、同じモデルでも呼び方で安全性が変わる。
- chat: Kotoyose のプロセスから Claude SDK を起動。`bypassPermissions` と `deny` の文字列だけで、閉じ込めなし。
- legacy (Cursor・Gemini など): 子プロセス。実行の経路は Kotoyose の環境変数を丸ごと引き継ぐ。
- bwrap: Claude / Codex の coding 以外の実行。
- Docker: coding と `cli.print`。
実行環境の名前もモデルや経路ごとに違って分かりにくい (所有者の指摘)。chat の key は呼べる alias を絞っていない。

## 決定 (案)
- alias が持つのは 4 つだけ: **何で** (model = route → CLI の adapter)、**道具** (prompt・skills・MCP・agents)、**どこで** (place: `host` / `jail` / v2 で `child:<名前>`)、**どこまで** (allow: 道具の権限と出口)。
- 中の処理は 1 つの runner。CLI ごとの adapter は「起動・出力の読み方・止め方・usage」だけを持つ (`cli.print` のプロファイルが試作)。
- chat と execution は出入口の形 (会話を返す / 作業場で結果を返す) だけが違い、閉じ込めは同じ runner が place と allow で決める。
- 呼び出し元の key ごとに呼べる alias と place・allow の上限を持つ。

## 結果 (見込み)
- 新しいモデルや CLI を足しても、変わるのは adapter と alias の値だけになる。plugin も入れやすくなる。
- chat の閉じ込めなし・legacy の環境変数の継承が無くなる。
- 詳細と移行の段取りは設計書 (`docs/design/execution-axes.md`、作成中) に書く。

## 所有者の判断 (2026-11-15)
- 設計書と Fable のレビューの推奨で進める。応急処置 (設定だけで chat の道具を切る) はせず、下の順で実装する。
- **便利さも段として選べるようにする**: 所有者が目の前で使う Claude Code が何でも読めるのと同じ便利さは、allow の `full` (この機械で何でも) として残す。使えるのは caller の上限が `full` の key (所有者自身の key) だけで、どの alias の既定にもしない。sample-client-key・n8n などの key は上限を低くする。違いは「誰が呼び、入口に何が混ざりうるか」で決める。
- Fable の指摘を取り込む: 機械は `on:` (既定 local) と閉じ込め (`place`) を分ける、allow は作業場の範囲 (none / read / write) と追加能力 (web・browser・desktop) と `full` に分ける、`host` を許すのは道具を切れると適合試験で示した adapter だけ、sandbox-exec は sealed として残す、jail の chat と egress `open` は作らない、Cursor の adapter は v1 から外す。

## 実装の順 (各段 1 PR・配備・確認)
1. [x] Kotoyose 自身の環境変数から秘密を消す (起動時に読み込んだら `os.environ` から削る。SDK が env を写しても漏れない) — #137
2. [x] caller のある key で chat と `/v1/models` を絞る (済) → クライアントごとに key を発行 (Hub を含む) → `legacy_bearer: false` — #138、#140 (クライアント別に key 発行済み、本番は `legacy_bearer: false`)
3. [-] Claude の jail の broker を CONNECT の素通しに (ADR-0001)。許可先はログイン更新まで含めて実測。Codex は実測して通れば v1 — #141 (Claude の CONNECT 素通しは実装済み、Codex は未実測)
4. [x] runner と jail の place を print の launcher を土台に作り、print → Claude → Codex の順に 1 本ずつ載せ替え — #142 (JailPlace)、#144、#145 (HostPlace)
5. [x] adapter × place の適合試験 (env の dump・host ファイルの読み取り・外への接続)。これで `host` を名乗れる adapter を決める — #143
6. [-] chat を runner の host place に載せる。設定の形 (model / tools / on / place / allow) と変換 script。`assistant-cron` は実行の入口へ — #146 (4 軸設定・変換 script、host の chat alias は道具なし。`assistant-cron` は未移行)
7. [x] sandbox-exec を Docker の worker で動かし、bwrap と legacy を削除 — #147、#148 (legacy agent と旧 bwrap 経路を削除。`local-docker` は `DockerLocalWorker` で sandbox-exec を実行。provider jail は引き続き bwrap を使用)
8. [-] 全経路で道具の event を記録 — #143 (同期 chat の event を記録、他の経路は未確認)

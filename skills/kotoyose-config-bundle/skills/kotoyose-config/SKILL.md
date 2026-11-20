---
name: kotoyose-config
description: kotoyose の config.yml を読む・preset を足す・MCP や Skill や prompt file を紐付ける・provider を切り替えるときに必ず使う。設定の仕様表と過去の事故録を references に持つ。
---

# kotoyose-config

`kotoyose` の `config.yml` を安全に編集するための作法ですわ。

## 0. まず足元を確かめる

この skill は kotoyose の preset 経由で読み込まれるため、**自分の `cwd` がリポジトリとは
限りません**。編集を始める前に、対象リポジトリの場所を必ず確定させること。

```bash
pwd
ls config.yml src/kotoyose/config.py    # 両方あればリポジトリ直下
```

見つからない場合は、`cwd` を上書きできる preset か確認するか、依頼元に場所を尋ねる。
**推測でパスを決め打ちしない。**

## 1. 触る前に知っておくこと

1. **`config.yml` は gitignore 対象で、認証トークンが平文で同居している。**
   内容を会話ログや外部へ貼らない。差分を見せるときは該当キーだけに絞る
2. **`models:` に alias を定義する。** 省略項目には `ModelPreset` の既定値が適用される。
3. **書いたコメントは Admin UI の保存 1 回で消える。** 意図は `config.yml` の中ではなく
   `references/` か `config.example.yml` に書く

## 2. 編集の手順

1. **読む** — 対象 preset の現状と、似た役割の既存 preset を 1 つ読む。
   このリポジトリは「既存 preset に倣う」のが最も安全
2. **仕様を引く** — キーの意味・型・既定値・解決順序は `references/schema.md`
3. **罠を避ける** — 触る領域が `references/pitfalls.md` に載っていないか確かめる
4. **書く** — 既存のキー順を保つ。不要なキーを消さない (Admin UI が書き戻す前提の形)
5. **検証する** — 下記コマンドが無言で終わるまで直す
6. **反映する** — `POST /admin/reload`。設定だけの変更に再起動は要らない

## 3. 検証コマンド

`config.py` は import 時に設定を読み込んで全検証を走らせますの。

```bash
KOTOYOSE_CONFIG=config.yml .venv/bin/python -c "import kotoyose.config as c; c.get_config()"
```

- 無言で終了 = 通過
- `ValidationError` = 該当キーとともに理由が出る。**直してから反映すること**

`config.example.yml` を編集した場合も同じ手順で確認する。

```bash
KOTOYOSE_CONFIG=config.example.yml .venv/bin/python -c "import kotoyose.config as c; c.get_config()"
```

検証を飛ばして `/admin/reload` を叩くと、不正な設定でサーバーが起動不能になりますわ。

## 4. 迷ったら

- キーの意味が分からない -> `references/schema.md`
- 「これで合っているか」が不安 -> `references/pitfalls.md`
- どちらにも無い -> `src/kotoyose/config.py` が正本。読んで確かめ、
  分かったことを references へ追記する

仕様表と実装が食い違っていたら、**実装を信じて references を直す**。

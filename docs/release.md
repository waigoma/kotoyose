# 公開リポジトリへのリリース

公開リポジトリの `main` には、元のリポジトリの `release` から書き出した
スナップショットを順に追加する。対象は `release/public-paths.txt` で管理する。

## 初期設定

1. 所有者の個人 GitHub アカウントで公開リポジトリを作成する。
   空の状態か、`main` が存在する状態で用意する。
2. この連携専用の SSH 鍵を用意する。公開リポジトリで公開鍵を Deploy key として
   登録し、書き込みを許可する。秘密鍵は元のリポジトリの Actions secret
   `PUBLIC_DEPLOY_KEY` に設定する。
3. 元のリポジトリの Actions variable `PUBLIC_REPOSITORY` に
   `owner/kotoyose` 形式で公開先を設定する。
4. 元のリポジトリの Actions secret `PUBLIC_DENYLIST` に、公開してはいけない語を
   1 行に 1 つ設定する。個人名、アカウント名、端末名、内部ドメイン、
   社内プロジェクト名、個人用プリセット名などが対象。検査は大文字・小文字を
   区別せず、部分文字列で照合する。

## 守っていること

- workflow は 2 つの job に分けている。`build` は書き出し、検査、gitleaks、試験を行い、deploy key を受け取らない。`publish` は新しい runner で、同じ commit から標準 library だけの書き出し script で snapshot を作り直し、`build` が記録した sha256 と照合してから、runner 自身の tar / git / ssh / rsync だけで push する。deploy key を受け取るのは `publish` だけ。tar は名前・時刻・所有者を固定しているので、作り直しても同じ byte になる。artifact は使わない (org の保存容量に左右されない)。
- action はすべて commit SHA、gitleaks の container は digest で固定している。版を上げるときは、tag が指す SHA と digest を確かめてから書き換える。

## ローカルで書き出しを確認する

元のリポジトリのルートで実行する。拒否リストのファイルはリポジトリ外で管理し、
`denylist_file` にそのパスを設定する。書き出しは Git が追跡しているパスを対象に、
作業ツリーの内容をコピーする。

```bash
out=$(mktemp -d)
denylist_file="$HOME/private-public-denylist.txt"
python scripts/export-public.py --out "$out"
PUBLIC_DENYLIST="$(cat "$denylist_file")" python scripts/check-public.py "$out"
```

## リリースする

`pyproject.toml` のバージョンを確認し、`main` を `release` へマージして
プッシュする。手動実行時は Actions のブランチ選択で `release` を指定する。
検査とテストが成功すると、公開先の `main` に変更のあるスナップショットを追加する。
対応する `v<version>` タグがなければ作成する。既存タグを移動するには所有者の
別途の判断が必要なので、新しいバージョンを出す際は事前にバージョンを更新する。

# skills/ — preset 別スキル束

各サブディレクトリが 1 プラグイン束です。 `config.yml` の preset から
`plugins` (パス) で読み込み、`skills` (名前) で有効化を選別します。

## 束の最小構造

```
skills/<bundle-name>/
  .claude-plugin/
    plugin.json          # { "name": "<bundle-name>", "version": "0.0.1" }
  skills/
    <skill-name>/
      SKILL.md           # frontmatter に name / description が必須
```

## config.yml での使い方

```yaml
dev:
  bare: true
  plugins:
    - ~/kotoyose/skills/example-bundle
  skills: ["hello-kotoyose"]   # 束の中の特定 skill のみ有効化

design:
  bare: true
  plugins:
    - ~/kotoyose/skills/example-bundle
  skills: "all"                       # 束の中を全部有効化
```

## 注意

- `plugins` のパスは **絶対パス または ~/ 展開** (system_prompt_files と同じ流儀)。
- 存在しないパス / plugin.json 欠落は config ロード時に loud に失敗します。
- `--bare` は hooks を skip します。hooks を使う束は別途 `bare: false` が必要です
  (本イテレーションでは hooks 非対応)。

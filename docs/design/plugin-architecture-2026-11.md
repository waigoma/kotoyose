# Kotoyose 外部 provider パッケージ設計記録

2026-11-20 決定。状態は保留。
第 6 provider (OpenAI 互換エンドポイントを予定) を追加するときに再開し、その事例が必要とする拡張点だけを採用する。
外部パッケージの契約は v1.0 後に `kotoyose.provider/1` として設計する。 [ADR-0012](../adr/0012-provider-packages.md)

## 目的と既存の結合

provider を追加するときの core の変更を減らし、WebUI から接続と利用条件を設定できるようにする。
利用者の認証情報は公式のインターフェースで扱い、学習に使われる経路への承諾は alias 単位で管理する。

現在、provider を追加すると、設定の provider 列挙と接続検証、console の表示と入力欄、利用枠の取得と DB 上の検証、probe とモデル探索、driver 契約と pool の能力表に変更が生じる。
print CLI は `PrintCliProfile` と名前付き hook に整理されている。
SDK セッションには `RuntimeSession` の共通契約があり、jail での起動は provider ごとの実装を使う。
第 6 provider の実装で変更点を確認し、既存の宣言と実行契約を再利用できる箇所を選ぶ。

実験的な `kotoyose.plugin/1` は host driver と pool router を読み込めたが、利用実績がなく、バージョンと digest の照合前に Python コードを import していた。
宣言の digest では配布物のコードを固定できないため、ローダーを削除した。

## 宣言と hook

`ProviderSpec` は標準ライブラリだけで表現する、権限を付与しない宣言とする。
候補は provider の ID と表示名、接続種別と入力欄、既存の `PrintCliProfile`、モデルや利用枠を表示するための情報である。
外部 provider の `data_policy` は、所有者が確認するまで `unknown` とする。

外部パッケージの hook に渡す入力はバイト列に限定し、戻り値を core が検証する。
login PTY、リソース、admission、egress、認証情報の注入、jail の起動は core が管理する。
配布物の Python コードを実行する場合はコード自体への信頼が必要で、宣言や hook の引数制限はコードの隔離を保証しない。

## 配布物の検証と管理画面

将来のローダーは、所有者が固定したバージョンと `RECORD` のハッシュを import 前に検証する。
配布物と照合値をどう固定し、推移的な依存をどう扱うかは、ローダーを設計する時点で決める。

WebUI は core が定義する入力欄と状態を表示する。
同一オリジンで管理者 Bearer トークンを扱うため、UI mod は対象外とする。
WebUI はコードを pip でインストールしない。

## 第 6 provider での見直し

初稿では、全組み込み provider の移行、provider 別 alias オプションの統合、DB の列挙制約の変更、汎用 catalog と適合試験の提供を候補に挙げた。
採用の判断は第 6 provider に必要な変更が分かるまで保留する。
試験が必要になった場合は、既存の第 2 print CLI profile の試験と同様に、偽の CLI や SDK で宣言と hook を確認できる形を検討する。

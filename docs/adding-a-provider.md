# provider を追加する

Kotoyose v1.0 では、provider を組み込みとして追加する。
既存の driver に接続先を設定するだけで済む場合もあるため、まず公式の実行インターフェースと必要な実行場所を確認する。

## 実行インターフェースを選ぶ

- **公式 SDK** を優先する。新しい SDK driver には `RuntimeSession` を実装し、`ask`、`stream`、`disconnect` を備える。実行 API を提供する場合は、driver の許可表と実行経路も実装する。 [RuntimeSession](../src/kotoyose/runtime/base.py) · [builder 登録](../src/kotoyose/session.py)
- **ACP 対応 CLI** は `PrintCliProfile.acp` に起動引数と専用 home で使える認証方式を宣言し、`cli.acp` route に同じ profile ID を指定する。初期化、セッション、MCP、許可応答、取消は共通クライアントが扱う。
- **ACP 非対応 CLI** は `cli.print` を明示指定する。print モードの起動引数、パーサー、名前付きフックを profile に追加する。
- **Anthropic 互換 endpoint** は、`provider: claude`、`kind: endpoint` の接続に `endpoint` と `secret` を指定し、`claude.agent-sdk` の route から参照する。
  `secret` には登録済み secret の名前または `env:変数名` を指定する。
  設定解決で URL が `anthropic_base_url` に入り、Claude SDK のプロセスには `ANTHROPIC_BASE_URL` として渡る。
  対応する隔離実行では Claude の relay が上流に使う。
  Anthropic 以外への接続に必要な route の許可は利用ガイドを参照する。 [設定解決](../src/kotoyose/config.py#L1458) · [Claude 認証](../src/kotoyose/runtime/claude_auth.py#L64) · [Claude relay](../src/kotoyose/container_claude.py#L150) · [利用ガイド](usage-guide.md)
- **OpenAI 互換 endpoint** は、`provider: codex` の `endpoint` 接続で認証の probe とモデル一覧取得ができる。
  Codex チャットの実行先は別に `route.model_provider` または alias の `codex.model_provider` で指定する。
  カスタム定義には `base_url` が必要で、API は Responses 互換が前提となる。
  認証が必要なら Codex 側の `env_key` なども設定する。
  チャット用の設定解決では接続の URL や secret はそこへ自動転記されない。
  Chat Completions のみの接続先はこの経路の対象外で、Codex の jail は endpoint の上書きを引き継がない。
  汎用の OpenAI 互換 upstream driver はロードマップ上の予定である。 [probe](../src/kotoyose/connections_probe.py#L1493) · [モデル一覧](../src/kotoyose/model_discovery.py#L1077) · [Codex の設定変換](../src/kotoyose/runtime/codex.py#L981) · [jail の制約](../src/kotoyose/execution_resources.py#L563) · [ロードマップ](ROADMAP.md)

## CLI profile の追加

Grok Build の profile は ACP と print モードの両方を宣言している。
以下の手順のうち、print 専用の起動引数とストリームパーサーは `cli.print` を提供する場合に用意する。

1. **起動方法と許可する操作を宣言する。** Grok Build は `grok_profile()` で profile ID、provider ID、バイナリ、最低対応バージョン、`argv_template`、surface ごとの引数を定義する。
   プロンプトはシェルを介さず `--single={prompt}` の 1 引数で渡す。
   `read` は読み取り用ツールに限定し、`write` は jail の隔離と組み合わせて検証する。
   新しい profile は `print_cli_profiles.py` の組み込み登録に追加する。 [Grok の宣言](../src/kotoyose/print_cli_grok_profile.py#L13) · [組み込み登録](../src/kotoyose/print_cli_profiles.py#L530)
2. **実測した出力でパーサーを書く。** 固定したバージョンの公式 CLI を専用 home で実行し、成功、ログアウト、利用制限、異常終了時の出力を確認する。
   テストに残す前にセッション ID、リクエスト ID、署名、パス、入力内容をダミーに置き換え、認証情報とログインコードは除去する。
   Grok の実測形式を再現するテストには `available_commands`、`text.data`、`usage`、`end` が並ぶ。
   `feed_line` と `finalize` で本文、セッション ID、使用量、終了状態を共通の結果へ変換し、エラー分類とモデル一覧は個別の関数にする。 [Grok のパーサー](../src/kotoyose/print_cli_grok.py#L75) · [実測形式のテスト](../tests/test_print_cli_grok.py#L236)
3. **フックを名前で登録する。** `print_cli_hooks.py` の組み込み登録で `register_parser`、`register_stderr_classifier`、`register_models_parser`、必要に応じて `register_quota_parser` を呼ぶ。
   profile の `parser`、`classify_stderr`、`parse_models` と quota 用のパーサー名を登録名にそろえる。
   `models_command` と `models_source` も宣言すると、接続を指定したモデル探索がこのフックを使う。 [フック登録](../src/kotoyose/print_cli_hooks.py#L98) · [モデル探索](../src/kotoyose/model_discovery.py#L1019)
4. **公式配布物を固定する。** 公式インストーラーのソースを読み、プラットフォームの変換規則と配布 URL を確認する。
   マニフェストを参照している場合は、対象バージョンの URL と公開チェックサムを取得する。
   配布物をダウンロードし、`sha256sum` または `sha512sum` でハッシュを計算する。公開チェックサムがあれば照合する。
   `CliInstallSpec` に正確なバージョンを、各 `CliArtifact` に取得順の URL テンプレート、ベンダーの OS・アーキテクチャ名、`sha256:` または `sha512:` に続く 16 進ハッシュを指定する。
   未検証のプラットフォームは `unset` にするか宣言から省く。
   実行ファイル単体は `raw`、単一ファイルを取り出す場合は `tar.gz` と `member`、パッケージ全体が必要な場合は `tar.gz` と `directory`、`entrypoint` を指定する。`entrypoint` は `directory` 内の相対パスで指定する。 [配布物の宣言](../src/kotoyose/cli_install_spec.py) · [インストーラー](../src/kotoyose/cli_installer.py)
5. **ログイン方式と専用 home を決める。** 公式 CLI の挙動に合う `paste_code`、`browser_wait`、`device_code` を選び、`PrintCliLoginSpec` にコマンド、URL の抽出とホスト制限、必要なコードのパターン、タイムアウト、ログイン状態の確認方法を指定する。
   Grok は `device_code` を使い、`models` が終了コード 0 でも `not authenticated` が出ればログアウトと扱う。
   `login_home_env` と必要な補助環境変数を専用ディレクトリに結び付ける。
   Grok では `GROK_HOME` と `HOME` を指定する。 [Grok のログイン設定](../src/kotoyose/print_cli_grok_profile.py#L78) · [ログイン実装](../src/kotoyose/cli_login.py#L734)
6. **公式インターフェースで利用枠を取得する。** 単発コマンドなら `quota_command` と `parse_quota` を組み合わせる。
   JSON-RPC なら `quota_rpc` に起動コマンド、要求の順序、結果 ID、パーサー名を指定する。
   両方式は排他的で、Grok は `agent stdio` に `initialize` と `_x.ai/billing` を送り、週次利用枠を `grok_acp_billing` として保存する。
   取得手段がなければ利用枠を推定せず、公式 CLI が返す制限エラーは `quota_error_patterns` で分類する。 [Grok の quota 設定](../src/kotoyose/print_cli_grok_profile.py#L51) · [probe](../src/kotoyose/connections_probe.py#L1036)
7. **実際の jail で外向き通信を絞る。** 対象の CLI バージョンで代表的な操作を実行し、jail のブローカーで観測した宛先のうち実行に必要なものだけを `egress_hosts` に入れる。
   Grok の現行 jail 設定は `cli-chat-proxy.grok.com` と `auth.x.ai` を許可する。
   ブラウザー向けログイン URL の `url_hosts` は別の制約である。
   許可外の宛先が遮断されることも確認する。 [Grok の許可リスト](../src/kotoyose/print_cli_grok_profile.py#L92) · [egress テスト](../tests/test_print_cli_egress.py)
8. **接続と route を設定して検証する。** Grok の例では subscription 接続に `provider`、`profile`、専用の `config_dir`、`data_policy` を指定する。
   `cli.acp` または `cli.print` の route に接続 ID、同じ profile ID、モデルを指定し、alias から route を参照する。
   プロファイルを差し替える偽 CLI のテストでは、ホストチャット、モデル探索、quota probe、ログイン完了後の更新、同じ home の実行とログインの競合、異なる profile の組み合わせの拒否を確認する。
   新しい profile の実測 fixture では、出力の変換、jail の起動引数、利用枠が取得できない場合、タイムアウト時の後処理も確かめる。 [設定例](../config.example.yml#L118) · [偽 CLI の統合テスト](../tests/test_print_cli_second_profile.py#L175) · [Grok のテスト](../tests/test_print_cli_grok.py)

公式 CLI または SDK だけを駆動し、ログイン済みの CLI からトークンを抽出したり、取り出したトークンを再送したりしない。
ログインは Kotoyose 専用の home で行い、実行に不要なテレメトリーと外向き通信は無効にする。
`data_policy` は profile 共通の属性ではなく、契約と設定を確認したうえで接続ごとに `no_training`、`trains`、`unknown` のいずれかを宣言する。[同意の処理](../src/kotoyose/data_policy.py#L17)

## 手編集する core の箇所

CLI profile の追加では既存の `cli.acp` または `cli.print` driver の行を再利用できる。
SDK driver や新しい能力まで追加するときは、以下の該当箇所にも宣言と配線が必要になる。

| ファイル | 手編集が必要な条件と変更箇所 |
| --- | --- |
| [print_cli_profiles.py](../src/kotoyose/print_cli_profiles.py#L530)、[print_cli_hooks.py](../src/kotoyose/print_cli_hooks.py#L98) | print CLI の profile と名前付きフックを組み込み登録に追加する。既存のログイン方式で扱えない場合は [cli_login.py](../src/kotoyose/cli_login.py#L734) のフロー実装と登録も追加する。 |
| [config.py](../src/kotoyose/config.py#L421) | provider を直接指定する preset を公開するなら `ModelPreset.provider` の `Literal` と関連する provider 固有設定の検証を更新する。Grok の route は driver に応じて共通の CLI runtime に解決される。新しい接続種別や認証の解決が必要な場合は `Connection`、route 検証、[設定の投影](../src/kotoyose/config.py#L1458) も更新する。 |
| [agent_spec.py](../src/kotoyose/agent_spec.py#L248)、[session.py](../src/kotoyose/session.py#L74) | 新規 driver は manifest の表に公式インターフェース、place、surface、実行モード、権限を宣言し、SDK runtime の builder を登録する。実行 API を提供する場合は [agent_route.py](../src/kotoyose/agent_route.py#L179) の解決と [agent_execution_runner.py](../src/kotoyose/agent_execution_runner.py) の dispatch にも対応経路が必要になる。 |
| [execution_resources.py](../src/kotoyose/execution_resources.py#L509)、[execution_agent.py](../src/kotoyose/execution_agent.py#L205)、[provider_jail.py](../src/kotoyose/provider_jail.py#L54) | 新規 SDK に native 実行を提供する場合は、対応 provider が固定された runtime profile、executor の選択、jail の起動と認証の制約を拡張する。再開を提供する場合は [agent_execution_resume.py](../src/kotoyose/agent_execution_resume.py#L17) の対象も確認する。 |
| [execution_contract.py](../src/kotoyose/execution_contract.py#L101)、[agent_execution_contract.py](../src/kotoyose/agent_execution_contract.py#L90) | 実行 API の `required_provider` や実行時の利用制限を新しい provider 名で公開する場合は、契約の型を更新する。利用制限の新形式が必要なら [provider_usage_limits.py](../src/kotoyose/provider_usage_limits.py#L125) の正規化と [execution_limits.py](../src/kotoyose/execution_limits.py#L28) の型と DB 制約を更新する。print CLI の実行時制限は `print_cli` を共用する。 |
| [connection_state_store.py](../src/kotoyose/connection_state_store.py#L12) | 新しい `quota_source` を `VALID_QUOTA_SOURCES` に追加する。PostgreSQL の [CHECK](../src/kotoyose/connection_state_store.py#L147) は起動時にこの集合から再生成される。新しい probe 経路が必要なら [connections_probe.py](../src/kotoyose/connections_probe.py#L1571) の dispatch も更新する。 |
| [provider-ui.js](../src/kotoyose/static/console/js/provider-ui.js#L1)、[console.css](../src/kotoyose/static/console/console.css#L1133) | コンソールの `PROVIDER_NAMES` と `PROVIDER_TONES` を更新し、API 接続用の別名が必要なら `API_PROVIDERS` も更新する。新しい tone を使う場合は CSS 変数と `.provider-mark.is-*` の規則も追加する。 |
| [providers.js](../src/kotoyose/static/console/js/pages/providers.js#L36) | 接続作成 UI の CLI profile、バイナリ、既定 home、provider の対応表と、パネルのラベルやモノグラムを追加する。ログイン案内に固有の表示が要る場合は [案内の描画](../src/kotoyose/static/console/js/pages/providers.js#L1603) も確認する。 |
| [place_conformance.py](../src/kotoyose/place_conformance.py#L20) | 新しい adapter の適合性を宣言する場合は `ADAPTER_PLACE_MATRIX` に実証済みの条件を追加する。Grok の専用行は現時点の表にない。 |
| [pool_strategies.py](../src/kotoyose/pool_strategies.py#L18) | pool の `requires` に応えられる provider を追加する場合は `CAPABILITY_PROVIDERS` を更新する。現行のキーは `images`。 |
| [model_discovery.py](../src/kotoyose/model_discovery.py#L70) | print CLI は登録済み profile と `parse_models` で探索できる。新しい SDK 等の探索経路や同梱候補が必要なら `SUPPORTED_PROVIDERS` の構成元、`BUNDLED_CATALOGS`、[接続別の dispatch](../src/kotoyose/model_discovery.py#L1077) を更新する。探索元に専用の表示名を付ける場合は [routes.js](../src/kotoyose/static/console/js/pages/routes.js#L48) も更新する。 |
| [agent_sanction.py](../src/kotoyose/agent_sanction.py#L21) | 新しいモデル系統を識別できるよう `model_family` を更新し、driver との組み合わせに対する route の許可判定を確認する。 |

手編集箇所を減らすのは v1.0 後の計画である。

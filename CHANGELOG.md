# Kotoyose の変更履歴

## 1.3.0 (2026-11-26)

- 公開の GitHub Release に、Windows のインストーラーに加えて Ubuntu/Linux x64 の tar.gz と SHA-256 も添付する。
- Ubuntu/Linux x64 の子機を tar.gz からユーザー単位で導入できるようにした。端末からの起動と systemd ユーザーサービスに対応し、更新時は直前の版を残す。
- Windows と Linux のパッケージ版の子機を **Settings** から更新できるようにした。ホストが同じプラットフォームの成果物を相互ピン留め済みのリンクで配信し、子機は実行中と待機中の仕事が終わってから適用する。

- 子機の **Restart** は、更新を保留した場合か、インストール済みのビルドへの切り替えが必要な場合に表示する。更新後は子機が再接続し、設定画面を開く。

- Windows の child の Claude と Codex にも元の SKILL.md で skill を渡し、Claude の alias が道具を絞っていても Skill の道具を残すようにした。
- skill の束で skills/<名前> や plugin 自体が symlink のとき (共有の skill リポジトリへのリンク)、child への skill のフォルダーの配信が断られていたのを直した。
- 子機所有者が許可した名前付きループバック接続を `openai.chat` で使えるようにした。既定は無効。
- Linux の子機 alias が `host:kotoyose` でホストの組み込み委譲を使い、許可したホスト alias へ `list_agents` と `ask_agent` で頼めるようにした。委譲トークンはホストに残り、範囲外、再入、取消、利用枠超過はホストが拒否する。
- child の Codex が、承認済みの MCP (host から中継したものと child に登録したもの) の道具を呼べなかったのを直した (default_tools_approval_mode=approve)。
- Linux の子機 alias で、ホストが公開した stdio MCP をホスト上で起動して中継し、同じ alias の `local:` stdio と並べて使えるようにした。プロセスは子機リンクあたり 2 個までとし、空の作業ディレクトリと制限した環境で動かし、再起動時に残ったプロセスグループを止める。承認画面には引数、cwd、環境変数の名前を出す。
- Linux の子機 alias で `host:` / `local:` MCP 参照を指定し、ホストが公開して子機所有者が版を承認した HTTP MCP を中継できるようにした。
- 固定の Linux x64 Grok Build でセッション専用のネイティブスキル配置を使い、alias にリポジトリスキルの探索設定を追加した。

- ホストと対応する子機で alias のスキルをフォルダーごと配信し、ACP・print CLI からセッション専用の添付ファイルを参照できるようにした。
- ホストの ACP・print CLI と対応する子機のデータ宣言 CLI で、alias のスキル本文を実スキル名付きで毎 turn の入力に前置するようにした。
- ホストと子機の Codex alias で、選択したスキルをセッション内の探索ルートに登録し、毎 turn で呼び出せるようにした。
- 対応する Linux 子機の Claude・Codex alias で、ホストと同じ順のプロンプトと、実スキル名・frontmatter を含む `SKILL.md` を SDK に渡すようにした。
- 子アプリの宣言カードは、有効にしたあと「In use on this child」と止めるボタンを出すようにした。
- Windows のセットアップとアンインストールで、確認後に実行中の Kotoyose Child を終了できるようにした。サイレント実行では自動で終了する。
- Windows の unrestricted の Grok で、シェルの道具が PowerShell や git を見つけられるよう PATH などコマンドに要る環境変数を渡すようにした。
- 子アプリの宣言カードの Use on this child が driver 名を保存して断られていたのを直し、ホストの宣言の欄を Providers の下に移して説明を足した。
- Windows の child で、管理者アカウントのプロファイル (Administrators / SYSTEM が所有者) でも宣言を保存できるようにし、child home 直下のファイルのアクセス権も締め直すようにした。
- Windows の子機ホームが LocalAppData から継承した余分な ACE を子機自身の保護 DACL に置き換え、宣言の保存失敗と次の操作を Providers の同じカードに出すようにした。
- Windows x64 child の Grok Build に、子機での明示的な `unrestricted` 承認、公式 EXE の導入、ローカルログインを追加した。
- Grok の MCP の確認で、session id が付く前に届く空の一覧 (Grok 1.0.89 の実際の順序) を受け入れるようにした。
- Linux の子アプリ (Rust) が hello で `provider-data-mcp/1` を通知していなかったため、Grok の MCP turn がホストで断られていたのを直した。
- Linux child の Grok Build で、子機所有者が MCP を承認し、書き込み alias が Workspace の登録名を指定した場合に MCP を使えるようにした。
- 子機のエージェント alias は Client の `places` に `child` または `child:<名前>` が必要になった。従来 `host` で利用していた Client は該当する値を追加する。
- child の Grok で、Ubuntu の既定 (user private group と umask 002) で作られた 775 の `~/.grok` を受け入れるようにした。
- Linux child の Grok Build で、子機所有者と alias が明示的に許可した場合に限り、作業フォルダー内の書き込みとネットワークを使わないコマンド実行に対応した。
- ホストコンソールに子機の作業フォルダーと Codex sandbox 上限を表示し、alias の cwd 許可ルートや sandbox 要求が子機の方針と合わない場合に警告するようにした。
- ホストコンソールで子機の切断を最後の heartbeat からの経過時間付きで表示し、初回接続待ち、登録取り消し、アダプター未有効、ログイン状態と区別するようにした。
- 子アプリのプロバイダーカードに Install・Repair と操作結果を表示し、Windows セットアップの修復ログを `logs\repair.log` に保存するようにした。
- 保存済みのデータ宣言が無効な場合は起動時に依存する alias だけを外し、宣言の検証理由と除外内容をコンソールに表示するようにした。
- child の Grok Build の読み取りを Grok の `read-only` sandbox で守るようにし、ACP client が agent の内部応答 (文字列 id) で turn を失敗させないようにした。
- Linux x64 child で Grok Build の固定配布物を宣言から導入し、ローカルのデバイスログインで読み取り専用の ACP alias を使えるようにした。
- データ宣言 `kotoyose.provider-data/1` をホストから子機へ配信し、子機の所有者が承認した宣言だけを既存の CLI 実行経路で使えるようにした。
- 子機の Providers、インストーラーの依存関係と修復、ログインと状態確認、ホストの CLI 接続案内を組み込みプロバイダー宣言から生成するようにした。
- 子アプリの接続状態と詳細をサイドバー下部に移し、有効なプロバイダーのログイン状態を残量共有の設定にかかわらずホストに表示するようにした。
- Claude / Codex の子機 turn を、プロバイダー宣言に基づく共通の設定投影と起動経路に移した。
- 子アプリの設定を Home、Providers、Workspace、Settings に再編し、Claude / Codex のローカルログインと Codex sandbox 上限の設定を追加した。
- 子アプリのローカルログインにキャンセルと認証コードの再入力を追加した。リンクが停止した場合は Home の登録フォームの上に理由を表示する。
- `codex.app-server` の child alias で、子のローカルログインを使った Codex のチャットを実行できるようにした。
- Windows child のセットアップと設定画面を英語化し、インストール先の選択と、Components からの Claude / Codex パッケージ管理を追加した。版の更新では前の版の provider を新しいピンから入れ直し、届かない場合は Repair に残す。
- Windows 向けに、トレイ起動とサインイン時の自動起動を設定し、実行部品を公式 PyPI から取得するユーザー単位のインストーラーを追加した。
- Rust 版 child の設定画面をホストコンソールに揃え、英語のログ、ダブルクリックでの起動と初回登録、`ui` プレビュー、同梱 Python の自動検出に対応した。
- Windows の Rust 版 child にタスクトレイ、登録・方針・ログを扱うローカル設定画面、サインイン時の自動起動を追加した。
- Rust 版の child Link app が host との接続を常時維持し、turn・ログイン・残量確認を受けたときに Python Runner を起動するようにした。
- child の `policy.json` に、エクスプローラーからコピーした Windows の path をエスケープせずに貼れるようにした (`\t` などが別の文字に化けない)。
- `openai.chat` で、Bearer 認証の OpenAI 互換 endpoint に同期・SSE のテキストチャットを送れるようにした。
- `kotoyose-acp` が会話履歴をローカルに保存し、ACP の `session/load` と `session/list` で再開・一覧表示できるようにした。
- Linux child の公式 CLI に console からログインでき、接続中の child から subscription の残量を取得できるようにしました。
- `approval: client` を設定した Claude Agent SDK と `cli.acp` のチャットで、ツールの許可要求に ACP クライアントから回答できるようにしました。
- Windows の child で、Claude SDK の turn を Job Object で止め、ログオン時の task から `kotoyose-child` を起動できるようにした。task は XML で登録し、実行時間の上限は無く、バッテリーでも動く。
- 委譲中の子の進捗を、親チャットの SSE (`X-Kotoyose-Events: 1`) と隔離実行の `delegation_progress` に出す。ホストのチャットと API キーでは `start_agent` / `agent_status` / `cancel_agent` で非同期に頼める。
- チャットの `service_tier: priority` で、対応する alias の Fast をその turn だけ選べるようにした。`/v1/models` と `kotoyose-acp` にも出した。
- child を `kotoyose-child enroll` / `run` で host に相互 pin で登録し、hello と heartbeat と revoke をコンソールから見られるようにした。
- `on: child:<名前>` の alias で、登録済みの child に chat の turn を流し、cwd と adapter と MCP の名は child の方針で狭めるようにした。

## 1.2.0 (2026-11-22)

- SDK の新しい runtime session には、それまでの会話を先頭に付けて渡すようにした。
- チャットの `reasoning_effort` で、対応する alias の effort をその turn だけ上書きできるようにした。`/v1/models` と `kotoyose-acp` にも、その alias が対応する effort を出した。
- 組み込み MCP の `list_agents` と `ask_agent` で、許可された別の alias にチャットを委譲できるようにした。
- 隔離された Claude / Codex の実行から、同じワークスペースを読み取り専用で開く alias に委譲できるようにした。
- alias の `delegable: false` で、その alias を委譲の先から外せるようにした。
- 委譲の取扱説明書の skill `kotoyose-delegation` を追加した。
- Kotoyose の alias を ACP クライアントから選べる `kotoyose-acp` コマンドを追加した。
- `X-Kotoyose-Events: 1` を指定したチャット SSE にツールイベントを追加した。
- `kotoyose-acp` は API キーを持ち主だけが読めるファイル (`KOTOYOSE_API_KEY_FILE`) から読める。
- 使える effort を起動時と以後 4 時間ごとに取得し、再起動のあとも `/v1/models` と `kotoyose-acp` に出す。

### Removed

- `agent_refs` / `agents` / `agent_definitions` を削除した。別 alias への委譲は `mcp_refs: [kotoyose]` と `delegates` を使う。Claude の Task / Agent ツールは常に無効。

## 1.1.0 (2026-11-21)

- コンソールから Antigravity CLI 1.5.13、Cursor Agent 2026.11.19-f005e9d、Grok Build 1.0.89 の公式配布物を固定ハッシュで検証して導入できるようにした (Linux x86_64)。
- Claude Code は Claude Agent SDK の同梱 CLI を既定で使い、ログインと接続プローブでも同じ実行ファイルを使用する。
- agy、Cursor CLI、Grok Build を明示的な `cli.acp` route で実行できるようにした。専用ログイン home と既存の jail・egress 設定を共用する。
- ACP のホストチャットで会話セッションの再利用と再開、MCP の受け渡し、tool call と許可判定のイベント、取消に対応した。
- ACP の turn の使用量を記録する。Grok Build は拡張の欄から token 数と費用も読む (Cursor の ACP は使用量を返さない)。

### Removed

- `antigravity.agy` ドライバーを削除した。agy は `cli.acp` で実行する。

## 1.0.1 (2026-11-21)

- Grok Build を公式配布の実行ファイルから直接導入するようにした (sha256 を固定して照合、node 不要)。
- 導入時に node を PATH から探すよう修正。
- CI の安定化。

## 1.0.0 (2026-11-21)

- Claude、Codex、Cursor、Antigravity、Grok Build の公式 CLI または SDK に対応。
- alias ごとにプロンプト、スキル、MCP、ツール、実行場所を構成。jail では外向き通信を許可リストで制限する。
- 接続ごとにデータ利用方針を宣言し、必要な場合は同意を求める。対応する接続では利用枠を取得できる。
- コンソールで接続、モデル、alias、利用枠を管理し、対応 CLI をバージョン固定と整合性検証付きで導入できる。
- OpenAI 互換チャット API と、タスクオーケストレーター向けの実行 API v1 を提供する。

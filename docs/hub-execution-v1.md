# 実行 API v1

タスクオーケストレーターのクライアント (以下、クライアント) は、自身の Run を Kotoyose の論理 execution に 1 対 1 で対応させる。エージェント実行を作成する際は `POST /v1/executions` に `execution.schema_version: "agent_execution_v1"` を渡す。受理時は 202 と `execution_id`、`idempotency_key`、`request_digest` を返す。同じ principal と key の同一要求は同じ受理証を返し、異なる要求は 409 になる。command は従来の `schema_version: "1"` と Attempt 受理証を使う。旧 agent 要求は 422 で拒否する。`GET /v1/capabilities` の `agent_execution_v1` は実行 service が構成されたときだけ `supported` であり、未構成時は `unsupported` である。
同じ manifest は OpenAI 互換チャットの能力を `direct_chat` として公開する。

照会は `GET /v1/executions/by-key/{key}?after_receipt=0`、event の読みは `GET /v1/executions/by-key/{key}/events?after=<cursor>&limit=<1..500, 既定 100>`（cursor は `<activation_ordinal 6 桁>:<sequence 10 桁>`、応答は `events`・`next_cursor`・`complete`。所有の照合と 404 は照会と同じ）、権限更新は `PUT .../authorization`、即時試行は `POST .../try-now`、取消は `POST .../cancel` を使う。key は principal ごとに名前空間を持ち、1～256 文字で `/` を含めない。path の key は URL encode する。応答を失っても、新しい key を発行せずに同じ Create を再送する。旧 `POST /v1/attempts/{id}/resume` と `GET /v1/attempts/{id}/limits` は公開しない。既存の Attempt 照会や command 操作は残る。

wire は snake_case、version と enum は固定値で、未知 key と未知 enum を拒否する。nullable の field も key を省略しない。整数は JSON safe integer、units は小数 12 桁以内の非負 decimal string、日時は UTC の ISO 8601 (`Z`) で渡す。`model` と `runtime_profile` は一方だけを指定する。`work.policy_ref` は scope grant の `policy_ref` と一致させ、budget resource は `resource_refs` に含める。scope や budget の不一致は 403 になる。

`work_digest` は `work`, `money`, `predecessor`, `time`, `resources`, `recovery`, `authority_ceilings` の object、`request_digest` は `execution` を含む Create 全体を、それぞれ canonical JSON にした UTF-8 の SHA-256 小文字 hex である。canonical JSON は空白なし、配列順を維持し、object の配列 index 形式の key (0～4294967294、先頭ゼロなし) を数値順で先に、残りを UTF-16 code unit 順で並べる。文字列は JavaScript `JSON.stringify` の escape に合わせ、非 ASCII や `/` はそのまま出力する。孤立 surrogate と safe integer 範囲外の値を拒否する。同じ key の再送で nonce、予約や initial authorization を変更してはならない。

## 権限と状態

クライアント は初回 activation の nonce を作り `initial_authorization` に入れる。後続 nonce は Kotoyose が作り、`authority_required: true` の Snapshot に出す。クライアント はその nonce にだけ Allow / Deny を返す。lease の壁時計期限は発行から最大 `3 × run_hard_ms + grace_ms` である (係数 3 は `LEASE_WALL_FACTOR`)。クライアントの更新間隔の例は 10 秒。期限は `min(issued_at + 3 × run_hard_ms + grace_ms, authority_ceiling_at - grace_ms)`、ceiling が null の場合は前者とする。旧 60 秒の許可も上限の内側なので受理される。`allowed_active_ms` は累積 agent 稼働時間の停止点であり、lease の壁時計期限とは別である。時間を使い切った場合や権限が期限切れになった場合、Kotoyose は停止する。

状態は `queued`、`running`、`waiting_provider`、`resuming`、`retrying`、`terminal`。承認待ちは `queued` と `pause` で表す。`sequence` は usage の checkpoint を含め Snapshot の値が変わるたび増える。同じ sequence の内容は receipt ページを除き同一であり、クライアント は古い sequence を破棄する。`state_revision` は状態に対する操作の競合検出に使う。`workspace_ref` は最後に停止を確認した workspace の世代であり、どの activation も起動していなければ null。クライアント は世代を推測せず、後続 Run や検証 source に観測した ref を使う。

## 会計と再開

initial、provider resume、fault retry、approval continue は新しい charge 単位を開く。lease resume は中断した単位を続け、同じ charge と予約を再掲する。charge を閉じたときの receipt の outcome は、起動が証明された `executed`、provider 拒否だけで完了 turn が 0 の `provider_zero_turn`、一度も起動せず今後も起動できない `not_started_closed` の 3 種類。`execution_confirmed` はその単位で agent の起動が証明されたことを意味し、`executed` では true。起動は証明できても実測が欠けた場合、既知の下限で receipt を出し、`usage.complete` を false にする。起動の有無を証明できない単位には receipt を出さず、予約は held のままにする。unbudgeted の charge、allocation、units は null。

receipt は最大 100 件ずつ取得する。`next_receipt_cursor` は返したページの最後の cursor、空ページなら要求した `after_receipt`、`final_receipt_cursor` は発行済み最大 cursor。`receipts_complete` は terminal かつ今後 receipt を作らないことを示す。クライアント は会計と最終実測が揃うまで成果物を採用しない。

provider 拒否の reset が分かり検出時刻より後なら、次の probe は `max(now + 5秒, reset_at + 5秒)`。reset が不明、または検出時刻以前なら 60、120、240、480、900 秒の順に待ち、以後は 900 秒。try-now は 30 秒間の重複を抑止し、権限を自動発行しない。session がない場合も次の probe まで待ってから `resume_unavailable` にする。

同じ execution の provider 待ち、approval continue、lease resume は常に同じ native session を使う。後続 Run の `safe_resume` は同一 principal の predecessor が terminal、停止 confirmed、副作用 resolved であり、workspace 世代、runtime と認証 binding が一致し、session が保持されている場合だけ使える。不一致は `predecessor_mismatch`、session 不在は `no_session`、保持されていない場合は `not_retained`、native resume 未対応は `adapter_unsupported`。legacy backend など native resume 非対応の場合、execution 内の再開も `adapter_unsupported` となる。クライアント は fresh の後続 Run と handoff を使う。

`handoff` 入力の `content_ref` が `artifact:` なら、Kotoyose は同一 principal の artifact を UTF-8 text として読み、`agent_generated` のまま入力へ渡す。上限は他の text 入力と同じ 262144 byte で、空の artifact は入力から省く。

故障 retry は最大 1 回。起動前の worker / SDK / provider 故障、worker lost、起動後の SDK / provider / protocol 故障、出力不正が対象で、認証や権限拒否などは対象外。実測が不完全な場合も retry しない。JSON 出力は構文を検査し、schema がある場合はローカル参照のみの JSON Schema 2020-12 で検査する。外部 `$ref` は拒否する。

## client tools、取消、再起動

client tools は execution key の下の `client-connections`、`client-calls`、`client-calls/{call_id}/results`、`client-calls/{call_id}/reconciliation` を使う。activation が変われば connection generation を execution 内で単調に増やす。古い activation からの新規配送は拒否し、未解決 call の証拠付き照合は可能。`GET client-calls/unresolved?limit=` は全 activation の unknown call を、状態を変えずに各 call 自身の activation nonce 付きで返す (limit は 1〜100、既定 100)。Operation identity により session の再実行での重複を防ぐ。宣言した `builtin:request-approval@1` は即座に pause し、クライアント の Allow に approved / denied の判断を載せて同じ session を続ける。

取消は全 nonce の admission を閉じる。まだ受理していない key に対しては、その principal の tombstone を作り `200 not_admitted` を返す。その principal の後着 Create は 409 `request_conflict`、`current: null` となる。他 principal の同じ key は独立しており、取消はその execution を変更しない。停止確認と副作用の解決が確認できれば、usage が不完全でも取消と物理 slot の解放を完了できる。使用量が不完全な場合は クライアント が `usage_incomplete` として扱う。

再起動後も execution、activation、認可、receipt、取消 tombstone と client operation の durable な記録は残る。process の停止や使用量を証明できない場合は不完全として扱い、推測で予約を解放しない。

## エラー

実行 API の 4xx と未対応 501 は `{ "error": { "type": "hub_execution_error", "code": ..., "message": ..., "current": ... } }`。shape は 422 `invalid_request`、認証 / scope は 403 `permission_denied`、所有者に見えない key は 404 `not_found`、競合は 409、未対応は 501 `unsupported_capability`。409 の current は同じ principal にだけ返す。5xx はこの envelope の外であり、クライアント は未受理と断定せず unknown outcome として回収する。

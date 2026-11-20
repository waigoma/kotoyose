# Native Codex の安全な失敗診断

`NativeCodexWorker` が `provider_error` で終了した場合、既存の
`provider_progress` journal に `kind=error`、`summary=provider_error` の診断を一件追加する。
既存の Attempt ID と sequence と時刻で相関するため、新たな相関IDやDB schemaは追加しない。

診断payloadのキーは次に限定する。

| キー | 保存する値 |
| --- | --- |
| `schema_version` | `1` |
| `failure_stage` | `rpc_response`、`thread_start`、`turn_completed`、`error_notification`、`broker`、`unknown` |
| `provider_code` | 既存 `CodexTurnError` の既知code。未知や欠損はnull |
| `http_status` | 100～599の整数。boolean、文字列、範囲外、欠損はnull |
| `turn_status` | 既存分類器の既知status。未知や欠損はnull |

app-serverの構造化errorとbrokerが観測した非200の数値statusだけを使う。
エラー本文、headers、additionalDetails、任意の例外文、raw stderr、認証情報は保存しない。
未知の例外は `unknown` とnullだけを記録する。
providerのretry助言を新しい再送やfallbackの権限には使わない。
成功、timeout、取消、既に分類された認証エラーの結果は変更しない。
診断callbackの障害も、元の終端結果や停止証拠を変更しない。

診断が有効になるのは、この変更を含む Kotoyose を別途配備してサービスへ反映し、
native Codex経路で新しいAttemptが実行され、journal writerが動作している場合である。
この変更は既存Attemptへ遡って詳細を補完しない。
旧Codex runtimeは既存の構造化分類を維持する。

このeventは分類の手掛かりであり、プロセス停止、効果解決、writer解放、会計精算の証拠ではない。
それらの契約、予算、deadline、権限、通信先は変更しない。

## 通信量と保存量の境界

正常なJSON-RPCフレーム列とstderrは、累積量だけでは停止しない。
stdoutは改行までの完全なフレームとして解析し、処理済みフレームを保持しない。
stderrは4096 bytesずつ読み捨て、保存せず排出し続ける。
任意の進行journalは既存の5000件上限で保存を省略できる。
これはRPCの要求・応答・終端や、最終JSON・tool応答の途中切断ではない。

有限の保護は次のまま残る。Bは新しいAttemptにpinされたoutput_limit_bytesである。

| 境界 | 上限と結果 |
| --- | --- |
| 単一stdout行の読み取りbuffer | B + 64 KiB。image URIの既知echo分だけ読み取り枠を追加 |
| 解析した単一フレーム | B + 64 KiB。厳密に検証したimage echo bytesを除く |
| EOFで未完の行 | 成功にせずinvalid_protocol。待ち続ける部分行は既存deadline/cancelで停止 |
| RPC応答待ちのpending通知 | 512件まで、保持中の合計bytesはB + 64 KiB以内 |
| settings適用確認 | 512通知以内に確認できなければ停止。deferredとpendingの合計保持bytesもB + 64 KiB以内 |
| sandbox_exec結果 | status/exit/stdout/stderrを含むJSONの累積UTF-8 bytesをB以内 |
| 完了した回答本文とtool結果 | 回答本文の合計とtool結果を合わせてB以内 |
| 選択した最終回答とtool結果 | credential置換後の本文とtool結果を合わせてB以内 |
| 最終redact後の出力 | B超過は従来どおり失敗。保存用の部分出力は成功の証拠にしない |

正常な小さいフレームを繰り返す場合、総受信量に比例するpayloadは蓄積しない。
単一フレーム、pending件数、保持する回答/tool結果、既存の時間・turn数・実資源の上限は残る。
Pythonの解析オブジェクトには追加メモリが必要であり、これらの数値はprovider全体の最悪RSS保証ではない。
保留bytesはフレームの計数値であり、Pythonオブジェクトの実メモリ量そのものではない。
大きな未完フレームなどで終了する際は、停止後のpipeを有限chunkで排出し、EOF待ちの逆流を防ぐ。
旧Taskやsnapshotの上限を補完・変更するものではなく、モデル通信のegress制限・認証・権限も変更しない。

## 出力境界の安全な診断

output_limit_exceeded時にも、既存journalへ本文を持たない診断を一件追加する。

| キー | 値 |
| --- | --- |
| schema_version | 1 |
| failure_kind | output_limit_exceeded |
| failure_stage | protocol_frame_bytes、protocol_line_bytes、rpc_pending_count、settings_pending_count、rpc_pending_bytes、settings_pending_bytes、tool_result_bytes、agent_message_bytes、final_response_bytes、redacted_output_bytes。未知はunknown |
| observed | 検出時に観測できた非負整数。不正・欠損はnull |
| limit | 正の整数。不正・欠損はnull |

buffer超過時のobservedは、検出までに受信した量であり、まだ受信していないフレーム全体の推定値ではない。
コード、コマンド、本文、headers、任意の例外文、raw stderr、認証情報は診断に渡さない。
以前は一般的なprovider_errorになり得た単一行buffer超過を、有限境界の失敗として分類する。
診断の観測先が壊れても元の終端理由と停止確認を変更しない。

反映には、この版の Kotoyose の別途配備とサービス反映が必要である。
新しい実行でも最終回答やtool結果そのものがBを超えれば失敗するため、
この変更は大きな最終結果の成功や実E2Eの完走を保証しない。

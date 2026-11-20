# Agent Development Standard v1 conformance checklist

実行した adapter、SDK / CLI revision、OS、Worker、image digest、Caller policy、試験日を結果とともに保存する。
一つの backend の成功を他の provider / OS / Worker へ転用しない。

| ID | 適用 | 必須確認 | 現在の確認先 |
| --- | --- | --- | --- |
| C01 | 共通 | principal を認証から解決し、他 Caller の ref を拒否 | `tests/test_execution_contract.py` / `tests/test_execution_store.py` |
| C02 | 共通 | 同じ key / payload は一つの Attempt、異なる payload は conflict | `tests/test_execution_store.py` |
| C03 | 実行環境 | sandbox の credential / mount / network / process 制限 | `tests/test_execution_service.py` / `tests/test_native_worker.py` |
| C04 | 実行環境 | native の明示許可と backend 条件を照合 | Native Worker の opt-in 適合試験 |
| C05 | 実行環境 | cancel 応答と実停止を区別し、旧 writer を fence | Execution store / service / cancellation 試験 |
| C06 | 共通 | events の重複・欠落・再接続で二重実行しない | orchestrator `runtime-attempts.test.ts`、実 HTTP conformance |
| C07 | artifact | 保存済み bytes / digest / MIME / principal を照合 | Kotoyose artifact / image 試験、orchestrator stage 試験 |
| C08 | resource | Pack / Skill の版・依存・provenance・失効を検証 | `tests/test_execution_resource_pins.py` |
| C09 | 副作用 | 応答消失で Operation unknown、read-only 照合、二重変更なし | orchestrator `external-operations.test.ts`、PR broker 試験 |
| C10 | Approval | 対象・版・期限・取消・一つの消費先・Audit を照合 | orchestrator `approvals.test.ts` |
| C11 | Coding | 固定候補の全必須 stage と evidence が合格 | orchestrator `verification-batch-conformance.test.ts` |
| C12 | Coding | FAIL → 別 Repair → 新候補 PASS、元 FAIL 保持 | 同実 provider conformance |
| C13 | Coding | Judge 不正出力・上限到達で必須指摘を自動解除しない | orchestrator review / Judge 試験 |
| C14 | PR | 公開 screenshot の匿名 URL と保存 bytes を照合 | orchestrator Publication Registry の実 HTTP / DB 試験 |
| C15 | merge | base / head / method / tree の原子的な一致 | 有効化する backend ごとの実証が必要 |
| C16 | 予算 | 予約・精算・未知費用・取消時の保持、二重計上なし | orchestrator `execution-budgets.test.ts` / `native-run-budget.test.ts` |
| C17 | Memory | namespace、principal、snapshot の失効を照合 | 未提供の Native 能力として有効化しない |
| C18 | client tool | 接続世代、なりすまし拒否、timeout / unknown の区別 | `test_client_tools.py` と orchestrator の実 HTTP 適合試験。Caller の接続・Approval 方針を必要とする |
| C19 | Subagent | Role 上限、親子予算・cancel・deadlock 回避 | provider-native Subagent は未提供。orchestrator の Platform Child Run は別 Attempt として有限観測・祖先予算・取消連鎖を検証 |
| C20 | 圧縮 | mode の事前許可・制御・観測限界を申告 | `test_native_compression.py`。caller-controlled / manual は unsupported |
| C21 | remote Worker | mTLS、失効、証明書更新、世代、partition の fencing | `test_remote_worker.py` / `test_remote_worker_cli.py` / `test_remote_execution_service.py`。明示構成の command と共有 filesystem の検証範囲。remote agent へ拡張しない |
| C22 | 別 Caller | product history / artifacts の正本を Caller に保持 | Native 契約試験と各製品の adapter 試験を分ける |
| C23 | workspace 回復 | 停止済み世代、保存 bytes、fresh 復元、参照保持、GC 競合を照合 | `test_checkpoint_archive.py` / `test_checkpoint_store.py` / `test_checkpoint_restore_recovery.py` と orchestrator 実 HTTP。選択ファイルの回復と、未完了復元の照会・明示破棄 |
| C24 | workspace GC | Caller の世代 CAS、停止・操作・回復 bytes、削除 fence と tombstone | `test_workspace_retention.py` / `test_workspace_deletion.py` と orchestrator の実 HTTP / DB 適合 |
| C25 | client tool digest | nested object / list、Unicode key、`1.0`、`-0.0`、指数表記、不正な数値・surrogate を共有 vector と照合し、Caller と Kotoyose の canonical text / SHA-256 が一致 | `tests/test_client_tool_digest.py` と `tests/fixtures/client_tool_arguments_digest_vectors.json`。orchestrator adapter は同じ vector を通す |
| C26 | client tool Caller | handler 配送前に Operation intent を保存し、`in_flight` の handler を再実行せず、`read` 以外の timeout / error / 応答消失を `unknown` として照合まで再送しない | orchestrator `role-tool-bridge.ts` の adapter 試験と実 HTTP 適合試験 |
| C27 | 非 coding Role | 所有元、request key、Definition / execution template、初回の期限・workspace・reservation を固定し、同じ key の入力変更を conflict にする。会話 Role は ledger を fresh Attempt へ渡し、provider session resume を使わない | orchestrator `role-invocations.ts` / `role-execution-controller.ts` と PM / Coordinator Role Run 試験 |
| C28 | Role 予算待機 | dispatch 未保存の Role Run が admission 上限不足または cap 版競合で `awaiting_approval` になり、人の増額承認後に同じ初回 dispatch key、Grant identity / version、reservation ref で `queued` へ戻る。送信済み Attempt はこの経路で再開しない | orchestrator `role-execution-controller.ts` と `agent-platform-v2-budget-increase.md` の Role Run 試験 |
| C29 | checkpoint 回復範囲 | capture / restore が `selected_files` だけを復元し、Git control state、Session、未選択ファイルを復元済みと扱わない。Coding の fresh Run は repository identity を別途固定する | Kotoyose checkpoint 試験と orchestrator `agent-platform-v2-checkpoints.md` の実 HTTP / DB 適合 |
| C30 | 最小非 coding Caller | simple chat / Desktop Pet 級の Caller で、節 1〜5 の該当必須項目を実装し、未使用の Tool、Approval、Memory、Coding workflow を optional / unsupported と記録する | 各製品の adapter 試験。orchestrator の Coding workflow の成功を代用しない |

2026-09-12 の Kotoyose Linux / Docker / systemd-bwrap / Claude adapter の試験結果は内部の実装記録に保管している。
本表の試験先は確認すべき証拠の所在を示すものであり、表の全項目が完了した宣言ではない。
backend 未設定時や能力不足時の拒否を、対応 backend での正常実行に代わる受入結果として数えない。

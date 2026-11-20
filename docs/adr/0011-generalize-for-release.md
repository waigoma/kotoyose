# 0011 リリースに向けて orchestrator 固有・個人固有の名前と場所を無くす

- 状態: 採用
- 日付: 2026-11-16

## 背景
Kotoyose は orchestrator と一緒に育ったので、orchestrator 固有・個人固有のものが 3 層に残っている。
1. **用語**: `caller`・`agent_models`・`max_surface` など開発者向けの言葉が、設定と画面にそのまま出ている。caller は機械の設定ファイルにあって、画面からは編集できない。
2. **API と内部の名前**: 実行の API の契約名が `hub_execution_v1`、`hub_execution_*.py` が 15 ファイル。orchestrator 以外のクライアントが使う API に見えない。
3. **個人の場所**: 実行設定と rootfs の既定値が別のアプリケーションのローカルディレクトリに依存していた。

## 決定 (v1 までに、ADR-0009 に従い互換を作らずに変える)
1. **Clients**: 呼び出し元の登録を「Client」と呼び、`config.yml` の `clients:` に置く。console の Endpoint から作成・編集・削除できる。項目は「使えるモデル (alias)」「置ける場所」「権限の上限」「作業場のディレクトリ」。API key は Client に結び付く。機械の設定 (`execution.json`) には機械のことだけを残す。
2. **API と内部の名前**: 実行の API を orchestrator に依らない名前と契約に整理し、orchestrator 側の呼び出しも同時に直す。
3. **場所**: Kotoyose の設定とデータは Kotoyose 自身の場所 (既定 `~/.config/kotoyose/` と `~/.local/share/kotoyose/`) に置き、個人のパスの直書きを無くす。

## 結果
- 1 は今すぐ (Client を画面で作れないと、クライアントごとの key 発行に進めないため)。2 と 3 は実行の軸の設定の形を変える段 (ADR-0010 段 6) と合わせる。

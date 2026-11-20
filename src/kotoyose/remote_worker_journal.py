"""Remote Worker の enrollment / operation を durable に保存する SQLite journal。"""

from __future__ import annotations

import json
import sqlite3
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .remote_worker_contract import RemoteCommandResult, RemoteOperationState, RemoteWorkerRef


@dataclass(frozen=True, slots=True)
class RemoteWorkspace:
    ref: str
    path: Path
    generation: int
    device: int
    inode: int
    challenge_relative: str = ""
    challenge_digest: str = ""


class RemoteWorkerJournal:
    """単一 private DB を transaction lock で直列化する。"""

    def __init__(self, path: Path):
        if not path.is_absolute():
            raise ValueError("journal path must be absolute")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("journal path must not be a symlink")
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise PermissionError("journal parent must be private")
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        path.chmod(0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS workers(
              worker_id TEXT NOT NULL, generation INTEGER NOT NULL, principal TEXT NOT NULL,
              fingerprint TEXT NOT NULL, server_fingerprint TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
              PRIMARY KEY(worker_id,generation),
              UNIQUE(worker_id,fingerprint)
            );
            CREATE TABLE IF NOT EXISTS operations(
              operation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, generation INTEGER NOT NULL,
              request_digest TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT,
              worker_id TEXT NOT NULL DEFAULT '', worker_generation INTEGER NOT NULL DEFAULT 0,
              workspace_ref TEXT NOT NULL DEFAULT '', workspace_generation INTEGER NOT NULL DEFAULT -1,
              worker_principal TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS workspace_claims(
              workspace_ref TEXT PRIMARY KEY, workspace_generation INTEGER NOT NULL,
              operation_id TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS workspaces(
              ref TEXT PRIMARY KEY, path TEXT NOT NULL, generation INTEGER NOT NULL,
              device INTEGER NOT NULL, inode INTEGER NOT NULL,
              challenge_relative TEXT NOT NULL DEFAULT '', challenge_digest TEXT NOT NULL DEFAULT ''
            );
            """
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(operations)")}
        if "worker_id" not in columns:
            self._db.execute("ALTER TABLE operations ADD COLUMN worker_id TEXT NOT NULL DEFAULT ''")
        if "worker_generation" not in columns:
            self._db.execute("ALTER TABLE operations ADD COLUMN worker_generation INTEGER NOT NULL DEFAULT 0")
        if "workspace_ref" not in columns:
            self._db.execute("ALTER TABLE operations ADD COLUMN workspace_ref TEXT NOT NULL DEFAULT ''")
        if "workspace_generation" not in columns:
            self._db.execute("ALTER TABLE operations ADD COLUMN workspace_generation INTEGER NOT NULL DEFAULT -1")
        if "worker_principal" not in columns:
            self._db.execute("ALTER TABLE operations ADD COLUMN worker_principal TEXT NOT NULL DEFAULT ''")
        worker_columns = {row[1] for row in self._db.execute("PRAGMA table_info(workers)")}
        if "server_fingerprint" not in worker_columns:
            self._db.execute("ALTER TABLE workers ADD COLUMN server_fingerprint TEXT NOT NULL DEFAULT ''")
        workspace_columns = {row[1] for row in self._db.execute("PRAGMA table_info(workspaces)")}
        if "challenge_relative" not in workspace_columns:
            self._db.execute("ALTER TABLE workspaces ADD COLUMN challenge_relative TEXT NOT NULL DEFAULT ''")
        if "challenge_digest" not in workspace_columns:
            self._db.execute("ALTER TABLE workspaces ADD COLUMN challenge_digest TEXT NOT NULL DEFAULT ''")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def enroll(self, ref: RemoteWorkerRef) -> None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT state,principal,fingerprint,server_fingerprint FROM workers WHERE worker_id=? AND generation=?",
                (ref.worker_id, ref.generation),
            ).fetchone()
            if row:
                expected = ("active", ref.principal, ref.certificate_fingerprint, ref.server_certificate_fingerprint or "")
                if tuple(row) != expected:
                    raise ValueError("worker generation is already enrolled with another identity")
                return
            latest = db.execute(
                "SELECT generation FROM workers WHERE worker_id=? ORDER BY generation DESC LIMIT 1",
                (ref.worker_id,),
            ).fetchone()
            if latest and ref.generation <= latest[0]:
                raise ValueError("worker generation is stale")
            db.execute("UPDATE workers SET state='revoked' WHERE worker_id=? AND state='active'", (ref.worker_id,))
            db.execute(
                "INSERT INTO workers(worker_id,generation,principal,fingerprint,server_fingerprint,state) VALUES(?,?,?,?,?,?)",
                (ref.worker_id, ref.generation, ref.principal, ref.certificate_fingerprint,
                 ref.server_certificate_fingerprint or "", "active"),
            )

    def revoke(self, worker_id: str, generation: int) -> None:
        with self._transaction() as db:
            db.execute("UPDATE workers SET state='revoked' WHERE worker_id=? AND generation=?", (worker_id, generation))

    def active_worker(self, worker_id: str, generation: int, fingerprint: str) -> RemoteWorkerRef | None:
        with self._lock:
            row = self._db.execute(
                "SELECT worker_id,generation,principal,fingerprint,server_fingerprint FROM workers "
                "WHERE worker_id=? AND generation=? AND fingerprint=? AND state='active'",
                (worker_id, generation, fingerprint),
            ).fetchone()
        if not row:
            return None
        values = dict(zip(("worker_id", "generation", "principal", "certificate_fingerprint", "server_certificate_fingerprint"), row))
        values["server_certificate_fingerprint"] = values["server_certificate_fingerprint"] or None
        return RemoteWorkerRef(**values)

    def active_worker_by_fingerprint(self, worker_id: str, generation: int, fingerprint: str) -> RemoteWorkerRef | None:
        return self.active_worker(worker_id, generation, fingerprint)

    def register_workspace(self, workspace: RemoteWorkspace) -> None:
        """登録済み workspace の物理 identity を durable に固定する。"""
        with self._transaction() as db:
            row = db.execute(
                "SELECT path,generation,device,inode,challenge_relative,challenge_digest FROM workspaces WHERE ref=?",
                (workspace.ref,),
            ).fetchone()
            values = (str(workspace.path), workspace.generation, workspace.device, workspace.inode,
                      workspace.challenge_relative, workspace.challenge_digest)
            if row:
                if tuple(row) != values:
                    raise ValueError("workspace ref is already registered with another identity")
                return
            db.execute(
                "INSERT INTO workspaces(ref,path,generation,device,inode,challenge_relative,challenge_digest) VALUES(?,?,?,?,?,?,?)",
                (workspace.ref, *values),
            )

    def workspace(self, ref: str, generation: int) -> RemoteWorkspace | None:
        with self._lock:
            row = self._db.execute(
                "SELECT ref,path,generation,device,inode,challenge_relative,challenge_digest FROM workspaces WHERE ref=? AND generation=?",
                (ref, generation),
            ).fetchone()
        return RemoteWorkspace(ref=row[0], path=Path(row[1]), generation=row[2], device=row[3], inode=row[4], challenge_relative=row[5], challenge_digest=row[6]) if row else None

    def prepare_operation(
        self,
        operation_id: str,
        attempt_id: str,
        generation: int,
        digest: str,
        worker: RemoteWorkerRef | None = None,
        workspace_ref: str | None = None,
        workspace_generation: int | None = None,
    ) -> RemoteOperationState:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row:
                if row[1] != attempt_id or row[3] != digest or row[2] != generation:
                    raise ValueError("operation identity or request digest changed")
                if worker is not None and (row[6], row[7]) != (worker.worker_id, worker.generation):
                    if row[6] != worker.worker_id or worker.generation < row[7] or row[10] != worker.principal:
                        raise ValueError("operation worker identity changed")
                    db.execute(
                        "UPDATE operations SET worker_generation=? WHERE operation_id=?",
                        (worker.generation, operation_id),
                    )
                elif worker is not None and row[10] != worker.principal:
                    raise ValueError("operation principal changed")
                if workspace_ref is not None and (row[8], row[9]) != (workspace_ref, workspace_generation):
                    raise ValueError("operation workspace identity changed")
                return self._operation(row)
            try:
                if workspace_ref is not None:
                    db.execute(
                        "INSERT INTO workspace_claims(workspace_ref,workspace_generation,operation_id) VALUES(?,?,?)",
                        (workspace_ref, workspace_generation, operation_id),
                    )
                db.execute(
                    "INSERT INTO operations("
                    "operation_id,attempt_id,generation,request_digest,state,worker_id,worker_generation,"
                    "workspace_ref,workspace_generation,worker_principal) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation_id,
                        attempt_id,
                        generation,
                        digest,
                        "prepared",
                        worker.worker_id if worker else "",
                        worker.generation if worker else 0,
                        workspace_ref or "",
                        workspace_generation if workspace_generation is not None else -1,
                        worker.principal if worker else "",
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("workspace is already claimed by another operation") from exc
            return RemoteOperationState(operation_id=operation_id, request_digest=digest, state="prepared")

    def mark_running(self, operation_id: str) -> bool:
        with self._transaction() as db:
            row = db.execute("SELECT state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                raise KeyError(operation_id)
            if row[0] == "prepared":
                db.execute("UPDATE operations SET state='running' WHERE operation_id=? AND state='prepared'", (operation_id,))
                return True
            return False

    def save_result(self, result: RemoteCommandResult) -> RemoteOperationState:
        with self._transaction() as db:
            db.execute(
                "UPDATE operations SET state=?,result_json=? WHERE operation_id=? AND state IN ('prepared','running','unknown')",
                (
                    "confirmed" if result.stop_confirmation == "confirmed" else "unknown",
                    json.dumps(result.model_dump(mode="json"), sort_keys=True),
                    result.operation_id,
                ),
            )
            if result.stop_confirmation == "confirmed":
                db.execute("DELETE FROM workspace_claims WHERE operation_id=?", (result.operation_id,))
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (result.operation_id,)).fetchone()
            if not row:
                raise KeyError(result.operation_id)
            return self._operation(row)

    def mark_unknown(self, operation_id: str) -> None:
        with self._transaction() as db:
            db.execute("UPDATE operations SET state='unknown' WHERE operation_id=? AND state!='confirmed'", (operation_id,))

    def operation(self, operation_id: str) -> RemoteOperationState | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        return self._operation(row) if row else None

    def operation_for_worker(self, operation_id: str, worker: RemoteWorkerRef) -> RemoteOperationState | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM operations WHERE operation_id=? AND worker_id=? "
                "AND worker_generation<=? AND worker_principal=?",
                (operation_id, worker.worker_id, worker.generation, worker.principal),
            ).fetchone()
        return self._operation(row) if row else None

    def operation_identity(self, operation_id: str) -> tuple[str, int] | None:
        """再起動時の停止対象を operation の durable identity から得る。"""
        with self._lock:
            row = self._db.execute(
                "SELECT attempt_id,generation FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return (str(row[0]), int(row[1])) if row else None

    def unsettled_operations(self) -> list[RemoteOperationState]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM operations WHERE state!='confirmed'").fetchall()
        return [self._operation(row) for row in rows]

    def _operation(self, row: tuple[Any, ...]) -> RemoteOperationState:
        result = RemoteCommandResult.model_validate(json.loads(row[5])) if row[5] else None
        return RemoteOperationState(operation_id=row[0], request_digest=row[3], state=row[4], result=result)

    class _Transaction:
        def __init__(self, owner: RemoteWorkerJournal):
            self.owner = owner
        def __enter__(self):
            self.owner._lock.acquire()
            try:
                self.owner._db.execute("BEGIN IMMEDIATE")
            except BaseException:
                self.owner._lock.release()
                raise
            return self.owner._db
        def __exit__(self, typ, value, trace):
            try:
                self.owner._db.execute("ROLLBACK" if typ else "COMMIT")
            finally:
                self.owner._lock.release()

    def _transaction(self) -> _Transaction:
        return self._Transaction(self)

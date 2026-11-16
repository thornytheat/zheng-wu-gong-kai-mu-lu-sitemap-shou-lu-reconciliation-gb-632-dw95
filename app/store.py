"""SQLite 持久化：runs 表存巡检证据，responsibility_changes 表存地址责任变更链。

表设计说明：
- batch_key 唯一，用来挡住同一输入的重复入库（幂等重跑的直接依据）；
- run_id 由 batch_key 派生，同输入必然同号，重跑不产生新行；
- 明细（目录行、收录行、探测、缺陷、隔离）整包进 payload，导出与对账同源。
- 责任变更按地址存一条带生效时点的链：(url_key, effective_at) 唯一，
  同址同时点只允许一条；status 驱动迁移器，pending 才参与迁移，处理过不再动。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    batch_key     TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL,
    input_digest  TEXT NOT NULL,
    rule_version  TEXT NOT NULL,
    sites         TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    elapsed_ms    INTEGER DEFAULT 0,
    findings_total INTEGER DEFAULT 0,
    updated_at    TEXT NOT NULL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE TABLE IF NOT EXISTS responsibility_changes (
    change_id       TEXT PRIMARY KEY,
    url_key         TEXT NOT NULL,
    from_site       TEXT NOT NULL,
    to_site         TEXT NOT NULL,
    effective_at    TEXT NOT NULL,
    basis           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    applied_at      TEXT,
    failure_reason  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(url_key, effective_at)
);
CREATE INDEX IF NOT EXISTS idx_resp_status ON responsibility_changes(status, effective_at);
CREATE INDEX IF NOT EXISTS idx_resp_url ON responsibility_changes(url_key, effective_at);
"""

SUMMARY_FIELDS = ("run_id", "batch_key", "status", "input_digest", "rule_version",
                  "started_at", "finished_at", "elapsed_ms", "findings_total")


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(os.path.dirname(self.path) or ".").mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- 写入 ---------------------------------------------------------------
    def save_run(self, run: dict) -> dict:
        payload = json.dumps(run, ensure_ascii=False, sort_keys=True)
        self._conn.execute(
            """INSERT INTO runs (run_id, batch_key, status, input_digest, rule_version,
                                 sites, started_at, finished_at, elapsed_ms,
                                 findings_total, updated_at, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                 status=excluded.status, finished_at=excluded.finished_at,
                 elapsed_ms=excluded.elapsed_ms, findings_total=excluded.findings_total,
                 updated_at=excluded.updated_at, payload=excluded.payload""",
            (run["run_id"], run["batch_key"], run["status"], run["input_digest"],
             run["rule_version"], ",".join(run["sites"]), run["started_at"],
             run.get("finished_at"), int(run.get("elapsed_ms") or 0),
             int(run.get("totals", {}).get("findings_total") or 0),
             run.get("finished_at") or run["started_at"], payload),
        )
        self._conn.commit()
        return run

    # -- 读取 ---------------------------------------------------------------
    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def find_by_batch_key(self, key: str) -> dict | None:
        row = self._conn.execute("SELECT payload FROM runs WHERE batch_key = ?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_run(self) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def list_runs(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_id, batch_key, status, input_digest, rule_version, started_at,"
            " finished_at, elapsed_ms, findings_total FROM runs"
            " ORDER BY started_at DESC, run_id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def count_runs(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])

    # -- 责任变更 -----------------------------------------------------------
    RESP_FIELDS = ("change_id", "url_key", "from_site", "to_site", "effective_at",
                   "basis", "status", "applied_at", "failure_reason",
                   "created_at", "updated_at")

    def get_change(self, change_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM responsibility_changes WHERE change_id = ?",
            (change_id,)).fetchone()
        return dict(row) if row else None

    def find_change(self, url_key: str, effective_at: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM responsibility_changes WHERE url_key = ? AND effective_at = ?",
            (url_key, effective_at)).fetchone()
        return dict(row) if row else None

    def list_changes(self, status: str | None = None,
                     url_key: str | None = None) -> list[dict]:
        sql = "SELECT * FROM responsibility_changes WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if url_key:
            sql += " AND url_key = ?"
            args.append(url_key)
        sql += " ORDER BY url_key, effective_at, change_id"
        return [dict(row) for row in self._conn.execute(sql, args).fetchall()]

    def list_active_changes(self) -> list[dict]:
        return self.list_changes(status="active")

    def list_pending_due(self, as_of: str) -> list[dict]:
        """到期的待生效变更：只挑 pending 且生效时点已到，处理过的不再返回。"""
        rows = self._conn.execute(
            "SELECT * FROM responsibility_changes WHERE status = 'pending'"
            " AND effective_at <= ? ORDER BY effective_at, change_id", (as_of,)).fetchall()
        return [dict(row) for row in rows]

    def insert_change(self, change: dict) -> dict:
        self._conn.execute(
            """INSERT INTO responsibility_changes
               (change_id, url_key, from_site, to_site, effective_at, basis, status,
                applied_at, failure_reason, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (change["change_id"], change["url_key"], change["from_site"],
             change["to_site"], change["effective_at"], change.get("basis", ""),
             change["status"], change.get("applied_at"),
             change.get("failure_reason", ""), change["created_at"], change["updated_at"]))
        self._conn.commit()
        return change

    def update_change_status(self, change_id: str, status: str, updated_at: str,
                             applied_at: str | None = None,
                             failure_reason: str = "") -> None:
        self._conn.execute(
            """UPDATE responsibility_changes
               SET status = ?, applied_at = ?, failure_reason = ?, updated_at = ?
               WHERE change_id = ? AND status = 'pending'""",
            (status, applied_at, failure_reason, updated_at, change_id))

    def transaction(self):
        """返回事务上下文：正常退出提交，抛错整体回滚（切换失败保留旧账）。"""
        return _Transaction(self._conn)

    def close(self) -> None:
        self._conn.close()


class _Transaction:
    """显式事务：迁移器在一个事务里批量处理 pending，任一失败整批回滚。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()

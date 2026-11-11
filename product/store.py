"""小规模 Demo 的持久化任务与事件存储。"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or os.getenv("TRAVEL_DB_PATH", "product_data/agent.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trips (
                    id TEXT PRIMARY KEY, query TEXT NOT NULL, version INTEGER NOT NULL,
                    constraints_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, version INTEGER NOT NULL,
                    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(trip_id) REFERENCES trips(id)
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
            """)
            # 进程意外退出后允许任务重跑；工作流必须是只读工具调用。
            db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (_now(),))

    def create_trip(self, query: str) -> Dict[str, str]:
        trip_id, job_id, now = uuid.uuid4().hex, uuid.uuid4().hex, _now()
        with self._connect() as db:
            db.execute("INSERT INTO trips(id,query,version,created_at,updated_at) VALUES(?,?,1,?,?)",
                       (trip_id, query, now, now))
            db.execute("INSERT INTO jobs(id,trip_id,version,status,created_at,updated_at) VALUES(?,?,1,'queued',?,?)",
                       (job_id, trip_id, now, now))
        self.event(job_id, "queued", {"message": "任务已加入队列"})
        return {"trip_id": trip_id, "job_id": job_id}

    def revise(self, trip_id: str, instruction: str) -> Optional[Dict[str, str]]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT query,version FROM trips WHERE id=?", (trip_id,)).fetchone()
            if row is None:
                return None
            version, job_id, now = row["version"] + 1, uuid.uuid4().hex, _now()
            query = row["query"] + "\n用户最新修改：" + instruction
            db.execute("UPDATE jobs SET status='superseded',updated_at=? WHERE trip_id=? AND status='queued'",
                       (now, trip_id))
            db.execute("UPDATE trips SET query=?,version=?,updated_at=? WHERE id=?",
                       (query, version, now, trip_id))
            db.execute("INSERT INTO jobs(id,trip_id,version,status,created_at,updated_at) VALUES(?,?,?,'queued',?,?)",
                       (job_id, trip_id, version, now, now))
        self.event(job_id, "queued", {"message": "修改已提交，正在更新方案"})
        return {"trip_id": trip_id, "job_id": job_id}

    def claim(self) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT j.id,j.trip_id,j.version,t.query,t.result_json
                FROM jobs j JOIN trips t ON j.trip_id=t.id
                WHERE j.status='queued' AND j.version=t.version
                ORDER BY j.created_at,j.id LIMIT 1""").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (_now(), row["id"]))
            job = dict(row)
            job["previous"] = json.loads(job.pop("result_json") or "null")
            return job

    def finish(self, job: Dict[str, Any], result: Optional[Dict[str, Any]], error: Optional[str] = None) -> None:
        status = "failed" if error else "completed"
        with self._connect() as db:
            db.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?",
                       (status, error, _now(), job["id"]))
            if result is not None:
                db.execute("UPDATE trips SET result_json=?,updated_at=? WHERE id=? AND version=?",
                           (json.dumps(result, ensure_ascii=False), _now(), job["trip_id"], job["version"]))
        self.event(job["id"], status, {"message": error or "方案已完成"})

    def event(self, job_id: str, kind: str, payload: Dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO events(job_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                       (job_id, kind, json.dumps(payload, ensure_ascii=False), _now()))

    def events(self, job_id: str, after: int = 0) -> List[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id", (job_id, after)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload_json"]),
                 "created_at": r["created_at"]} for r in rows]

    def trip(self, trip_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if row is None:
                return None
            jobs = db.execute("SELECT id,version,status,error,created_at,updated_at FROM jobs WHERE trip_id=? ORDER BY version DESC",
                              (trip_id,)).fetchall()
        return {"id": trip_id, "query": row["query"], "version": row["version"],
                "result": json.loads(row["result_json"] or "null"), "jobs": [dict(j) for j in jobs]}

    def job_status(self, job_id: str) -> Optional[str]:
        with self._connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["status"] if row else None

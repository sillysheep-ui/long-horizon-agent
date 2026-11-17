"""小规模 Demo 的持久化任务与事件存储。"""

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from product.intent import classify_intent, describe_trip


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or os.getenv("TRAVEL_DB_PATH", "product_data/agent.db"))
        try:
            self.idempotency_seconds = int(os.getenv("IDEMPOTENCY_WINDOW_SECONDS", "60"))
        except ValueError:
            self.idempotency_seconds = 60
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
                    result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    conversation_id TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, engine TEXT NOT NULL DEFAULT 'classic',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, job_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(trips)")}
            if "conversation_id" not in columns:
                db.execute("ALTER TABLE trips ADD COLUMN conversation_id TEXT")
            if "previous_json" not in columns:
                db.execute("ALTER TABLE trips ADD COLUMN previous_json TEXT")
            job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "revision" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN revision TEXT")
            if "dedupe_key" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN dedupe_key TEXT")
            # 进程意外退出后允许任务重跑；工作流必须是只读工具调用。
            db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (_now(),))

    def create_trip(self, query: str, conversation_id: str | None = None) -> dict[str, str]:
        trip_id, job_id, now = uuid.uuid4().hex, uuid.uuid4().hex, _now()
        with self._connect() as db:
            db.execute("INSERT INTO trips(id,query,version,created_at,updated_at,conversation_id) VALUES(?,?,1,?,?,?)",
                       (trip_id, query, now, now, conversation_id))
            db.execute("INSERT INTO jobs(id,trip_id,version,status,created_at,updated_at) VALUES(?,?,1,'queued',?,?)",
                       (job_id, trip_id, now, now))
        self.event(job_id, "queued", {"message": "任务已加入队列"})
        return {"trip_id": trip_id, "job_id": job_id}

    @staticmethod
    def dedupe_key(trip_id: str, instruction: str) -> str:
        """幂等键：同一行程 + 归一化后相同的指令；版本由查询条件约束。"""
        normalized = re.sub(r"[\s，。！？、,.!?~～]+", "", instruction or "")
        raw = f"{trip_id}:{normalized}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def revise(self, trip_id: str, instruction: str) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT query,version FROM trips WHERE id=?", (trip_id,)).fetchone()
            if row is None:
                return None
            key = self.dedupe_key(trip_id, instruction)
            threshold = (datetime.now(UTC) - timedelta(seconds=self.idempotency_seconds)).isoformat()
            existing = db.execute(
                "SELECT id,version FROM jobs WHERE dedupe_key=? AND status='completed' "
                "AND version=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                (key, row["version"], threshold)).fetchone()
            if existing is not None:
                return {"trip_id": trip_id, "job_id": existing["id"], "version": existing["version"],
                        "duplicate": True}
            version, job_id, now = row["version"] + 1, uuid.uuid4().hex, _now()
            query = row["query"] + "\n用户最新修改：" + instruction
            db.execute("UPDATE jobs SET status='superseded',updated_at=? WHERE trip_id=? AND status='queued'",
                       (now, trip_id))
            db.execute("UPDATE trips SET query=?,version=?,updated_at=? WHERE id=?",
                       (query, version, now, trip_id))
            db.execute("INSERT INTO jobs(id,trip_id,version,status,created_at,updated_at,revision,dedupe_key) "
                       "VALUES(?,?,?,'queued',?,?,?,?)",
                       (job_id, trip_id, version, now, now, instruction, key))
        self.event(job_id, "queued", {"message": "修改已提交，正在更新方案"})
        return {"trip_id": trip_id, "job_id": job_id}

    def claim(self) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT j.id,j.trip_id,j.version,j.revision,t.query,t.result_json
                FROM jobs j JOIN trips t ON j.trip_id=t.id
                WHERE j.status='queued' AND j.version=t.version
                ORDER BY j.created_at,j.id LIMIT 1""").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (_now(), row["id"]))
            job = dict(row)
            job["previous"] = json.loads(job.pop("result_json") or "null")
            return job

    def finish(self, job: dict[str, Any], result: dict[str, Any] | None, error: str | None = None) -> None:
        status = "failed" if error else "completed"
        with self._connect() as db:
            db.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?",
                       (status, error, _now(), job["id"]))
            if result is not None:
                db.execute("UPDATE trips SET previous_json=result_json,result_json=?,updated_at=? "
                           "WHERE id=? AND version=?",
                           (json.dumps(result, ensure_ascii=False), _now(), job["trip_id"], job["version"]))
        self.event(job["id"], status, {"message": error or "方案已完成"})

    def event(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO events(job_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                       (job_id, kind, json.dumps(payload, ensure_ascii=False), _now()))

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id", (job_id, after)).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload_json"]),
                 "created_at": r["created_at"]} for r in rows]

    def trip(self, trip_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if row is None:
                return None
            jobs = db.execute("SELECT id,version,status,error,created_at,updated_at FROM jobs WHERE trip_id=? ORDER BY version DESC",
                              (trip_id,)).fetchall()
        return {"id": trip_id, "query": row["query"], "version": row["version"],
                "conversation_id": row["conversation_id"],
                "result": json.loads(row["result_json"] or "null"), "jobs": [dict(j) for j in jobs]}

    def job_status(self, job_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["status"] if row else None

    # ---- 会话：一个会话可以有多条行程版本，消息只追加不改写 ----

    def create_conversation(self, title: str = "新对话", engine: str = "classic") -> dict[str, str]:
        conversation_id, now = uuid.uuid4().hex, _now()
        with self._connect() as db:
            db.execute("INSERT INTO conversations(id,title,engine,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (conversation_id, title[:60] or "新对话", engine, now, now))
        return {"conversation_id": conversation_id, "title": title[:60] or "新对话"}

    def conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                return None
            messages = db.execute("SELECT id,role,content,job_id,created_at FROM messages "
                                  "WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
            trips = db.execute("SELECT id,query,version,created_at,updated_at FROM trips "
                               "WHERE conversation_id=? ORDER BY created_at", (conversation_id,)).fetchall()
        return {"id": row["id"], "title": row["title"], "engine": row["engine"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "messages": [dict(m) for m in messages],
                "trips": [dict(t) for t in trips]}

    def append_message(self, conversation_id: str, role: str, content: str,
                       job_id: str | None = None) -> dict[str, Any]:
        now = _now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO messages(conversation_id,role,content,job_id,created_at) VALUES(?,?,?,?,?)",
                (conversation_id, role, content[:4000], job_id, now))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            message_id = cursor.lastrowid
        return {"id": message_id, "conversation_id": conversation_id, "role": role,
                "content": content[:4000], "job_id": job_id, "created_at": now}

    def latest_trip(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM trips WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
                             (conversation_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "query": row["query"], "version": row["version"],
                "conversation_id": row["conversation_id"]}

    def _duplicate_reply(self, conversation_id: str, content: str) -> dict[str, Any] | None:
        """同一条消息在排队或执行中时直接返回原任务，避免重试生成两个版本。"""
        with self._connect() as db:
            row = db.execute("""SELECT m.id,m.content,m.job_id,j.status,j.trip_id,j.version
                FROM messages m JOIN jobs j ON m.job_id=j.id
                WHERE m.conversation_id=? AND m.role='user' AND m.content=?
                ORDER BY m.id DESC LIMIT 1""", (conversation_id, content)).fetchone()
        if row is None or row["status"] not in ("queued", "running"):
            return None
        kind = "trip" if row["version"] == 1 else "revision"
        return {"conversation_id": conversation_id, "kind": kind, "duplicate": True,
                "job_id": row["job_id"], "trip_id": row["trip_id"], "version": row["version"]}


    def undo_last(self, conversation_id: str) -> dict[str, Any] | None:
        """退回上一版：把上一版内容作为新版本恢复，版本号继续递增。"""
        latest = self.latest_trip(conversation_id)
        if latest is None:
            return None
        with self._connect() as db:
            row = db.execute("SELECT previous_json FROM trips WHERE id=?", (latest["id"],)).fetchone()
            if row is None or not row["previous_json"]:
                return None
            db.execute("UPDATE trips SET version=version+1,result_json=?,previous_json=NULL,updated_at=? "
                       "WHERE id=?", (row["previous_json"], _now(), latest["id"]))
        return {"trip_id": latest["id"], "version": latest["version"] + 1}

    def latest_result(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT result_json FROM trips WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
                             (conversation_id,)).fetchone()
        if row is None or not row["result_json"]:
            return None
        try:
            return json.loads(row["result_json"])
        except json.JSONDecodeError:
            return None

    def continue_conversation(self, conversation_id: str, content: str,
                              intent: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """先判断意图：新行程、改行程、问行程、闲聊。intent 可由外层（含模型兜底）传入。"""
        latest = self.latest_trip(conversation_id)
        duplicate = self._duplicate_reply(conversation_id, content)
        if duplicate is not None:
            return duplicate
        intent = dict(intent) if intent else classify_intent(content, has_trip=latest is not None)
        prior = self.latest_result(conversation_id) if latest is not None else None
        if prior is not None and prior.get("status") == "needs_clarification" and intent["intent"] != "new_trip":
            intent = {"intent": "revise_trip", "reason": "上一轮在等用户补充信息", "target_days": []}
        action = intent.get("action") or "modify"
        if action in ("cancel", "undo"):
            message = self.append_message(conversation_id, "user", content)
            if latest is None:
                answer, kind = "现在还没有行程，直接告诉我想去哪、玩几天就行。", "chat_reply"
            elif action == "undo":
                undone = self.undo_last(conversation_id)
                answer = "已恢复到上一版行程。" if undone else "已经是第一版行程了，没有更早的版本可退回。"
                kind = "undo" if undone else "answer"
            else:
                answer, kind = "好的，先不安排了。需要时把新的想法告诉我就行。", "cancelled"
            reply = self.append_message(conversation_id, "assistant", answer)
            return {"conversation_id": conversation_id, "kind": kind, "intent": intent,
                    "message": message, "reply": reply}
        if intent["intent"] == "unclear":
            message = self.append_message(conversation_id, "user", content)
            reply = self.append_message(conversation_id, "assistant",
                                        "你是想改行程，还是想了解行程里的安排？直接说一句就行。")
            return {"conversation_id": conversation_id, "kind": "clarify_reply", "intent": intent,
                    "message": message, "reply": reply}
        if latest is not None and intent["intent"] in ("ask", "chat"):
            message = self.append_message(conversation_id, "user", content)
            if intent["intent"] == "ask":
                answer = describe_trip(self.latest_result(conversation_id), intent["target_days"])
            else:
                answer = "我先按现有行程理解。想改哪一天，或者想重新规划去哪里，直接说就行。"
            reply = self.append_message(conversation_id, "assistant", answer)
            with self._connect() as db:
                db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=? AND title IN ('新对话','')",
                           (content[:30], _now(), conversation_id))
            return {"conversation_id": conversation_id, "kind": "answer", "intent": intent,
                    "message": message, "reply": reply}
        if latest is None or intent["intent"] == "new_trip":
            created = self.create_trip(content, conversation_id=conversation_id)
            kind = "trip"
        else:
            created = self.revise(latest["id"], content)
            if created is None:
                return None
            kind = "revision"
        message = self.append_message(conversation_id, "user", content, created["job_id"])
        with self._connect() as db:
            db.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=? AND title IN ('新对话','')",
                       (content[:30], _now(), conversation_id))
        return {"conversation_id": conversation_id, "kind": kind, "intent": intent, "message": message, **created}

    def conversation_of_job(self, job_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT t.conversation_id FROM jobs j JOIN trips t ON j.trip_id=t.id "
                             "WHERE j.id=?", (job_id,)).fetchone()
        return row["conversation_id"] if row else None

    def record_reply(self, job_id: str, result: dict[str, Any] | None, error: str | None = None) -> None:
        conversation_id = self.conversation_of_job(job_id)
        if not conversation_id:
            return
        if error:
            content = "执行失败：" + error
        elif not result:
            content = "没有拿到结果，请重试。"
        elif result.get("status") == "needs_clarification":
            content = str(result.get("question") or "还需要你补充一些信息。")
        elif result.get("status") == "partial":
            content = str(result.get("question") or "地点查询暂不可用，无法给出可信的具体行程。")
        else:
            content = str(result.get("summary") or "方案已完成。")
        self.append_message(conversation_id, "assistant", content, job_id)

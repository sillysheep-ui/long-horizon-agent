"""会话存储与续聊 API 的单元测试。"""

import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault("TRAVEL_DB_PATH", os.path.join(tempfile.gettempdir(), "travel-test-default.db"))

import product.api as api_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from product.store import Store  # noqa: E402


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "agent.db"))

    def test_new_conversation_is_empty(self):
        created = self.store.create_conversation()
        conversation = self.store.conversation(created["conversation_id"])
        self.assertEqual(conversation["title"], "新对话")
        self.assertEqual(conversation["engine"], "classic")
        self.assertEqual(conversation["messages"], [])
        self.assertEqual(conversation["trips"], [])

    def test_first_message_creates_trip(self):
        conversation_id = self.store.create_conversation()["conversation_id"]
        result = self.store.continue_conversation(conversation_id, "从上海去杭州玩两天")
        self.assertEqual(result["kind"], "trip")
        conversation = self.store.conversation(conversation_id)
        self.assertEqual(len(conversation["trips"]), 1)
        self.assertEqual(conversation["trips"][0]["id"], result["trip_id"])
        self.assertEqual([m["role"] for m in conversation["messages"]], ["user"])
        self.assertEqual(conversation["title"], "从上海去杭州玩两天")

    def test_second_message_revises_same_trip(self):
        conversation_id = self.store.create_conversation()["conversation_id"]
        first = self.store.continue_conversation(conversation_id, "从上海去杭州玩两天")
        second = self.store.continue_conversation(conversation_id, "第二天换成室内")
        self.assertEqual(second["kind"], "revision")
        self.assertEqual(second["trip_id"], first["trip_id"])
        trip = self.store.trip(first["trip_id"])
        self.assertEqual(trip["version"], 2)
        self.assertEqual(trip["conversation_id"], conversation_id)
        self.assertEqual([m["role"] for m in self.store.conversation(conversation_id)["messages"]],
                         ["user", "user"])

    def test_reply_is_appended_once(self):
        conversation_id = self.store.create_conversation()["conversation_id"]
        created = self.store.continue_conversation(conversation_id, "从上海去杭州玩两天")
        self.store.record_reply(created["job_id"], {"status": "complete", "summary": "两天行程已排好。"})
        conversation = self.store.conversation(conversation_id)
        self.assertEqual(conversation["messages"][-1]["role"], "assistant")
        self.assertEqual(conversation["messages"][-1]["content"], "两天行程已排好。")
        # 没有会话的任务不应写入消息。
        orphan = self.store.create_trip("孤儿行程")
        self.store.record_reply(orphan["job_id"], {"status": "complete", "summary": "x"})
        self.assertEqual(len(self.store.conversation(conversation_id)["messages"]), 2)

    def test_reply_error_and_clarification(self):
        conversation_id = self.store.create_conversation()["conversation_id"]
        created = self.store.continue_conversation(conversation_id, "从上海去杭州玩两天")
        self.store.record_reply(created["job_id"], None, "模型超时")
        self.assertTrue(self.store.conversation(conversation_id)["messages"][-1]["content"].startswith("执行失败"))
        second = self.store.continue_conversation(conversation_id, "第二天换成室内")
        self.store.record_reply(second["job_id"], {"status": "needs_clarification", "question": "去哪个城市？"})
        self.assertEqual(self.store.conversation(conversation_id)["messages"][-1]["content"], "去哪个城市？")

    def test_old_database_gets_conversation_column(self):
        path = os.path.join(self.tmp.name, "old.db")
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE trips (
            id TEXT PRIMARY KEY, query TEXT NOT NULL, version INTEGER NOT NULL,
            constraints_json TEXT NOT NULL DEFAULT '{}', result_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.commit()
        db.close()
        store = Store(path)
        columns = {row["name"] for row in store._connect().execute("PRAGMA table_info(trips)")}
        self.assertIn("conversation_id", columns)


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        api_module.store = Store(os.path.join(self.tmp.name, "api.db"))
        self.client = TestClient(api_module.app)

    def test_create_and_reply(self):
        created = self.client.post("/api/conversations").json()
        conversation_id = created["conversation_id"]
        response = self.client.post("/api/conversations/%s/messages" % conversation_id,
                                    json={"content": "从上海去杭州玩两天"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["kind"], "trip")
        detail = self.client.get("/api/conversations/%s" % conversation_id).json()
        self.assertEqual(detail["title"], "从上海去杭州玩两天")
        self.assertEqual(len(detail["trips"]), 1)

    def test_unknown_conversation_returns_404(self):
        self.assertEqual(self.client.get("/api/conversations/nope").status_code, 404)
        self.assertEqual(
            self.client.post("/api/conversations/nope/messages", json={"content": "你好"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()

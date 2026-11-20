import tempfile
import unittest
from pathlib import Path

from product.intent import classify_intent, describe_trip
from product.store import Store


class IntentTests(unittest.TestCase):
    def test_new_trip(self):
        self.assertEqual(classify_intent("重新规划去成都玩三天", True)["intent"], "new_trip")
        self.assertEqual(classify_intent("从上海去杭州", False)["intent"], "new_trip")

    def test_revision_keeps_target_day(self):
        result = classify_intent("第二天换成博物馆", True)
        self.assertEqual(result["intent"], "revise_trip")
        self.assertEqual(result["target_days"], [2])

    def test_question(self):
        self.assertEqual(classify_intent("几点出发？", True)["intent"], "ask")
        self.assertEqual(classify_intent("第三天去哪里", True)["intent"], "ask")

    def test_chat(self):
        self.assertEqual(classify_intent("好的", True)["intent"], "chat")

    def test_describe_trip(self):
        result = {"days": [{"day": 1, "activities": [{"start": "09:30", "title": "拙政园"}]},
                           {"day": 2, "activities": [{"start": "10:00", "title": "苏州博物馆"}]}]}
        text = describe_trip(result)
        self.assertIn("第1天：09:30 拙政园", text)
        self.assertIn("第2天：10:00 苏州博物馆", text)
        self.assertEqual(describe_trip(result, [2]).count("第1天"), 0)

    def test_conversation_routes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "intent.db"))
        cid = store.create_conversation()["conversation_id"]
        self.assertEqual(store.continue_conversation(cid, "从上海去杭州玩两天")["kind"], "trip")
        self.assertEqual(store.continue_conversation(cid, "几点出发？")["kind"], "answer")
        self.assertEqual(store.continue_conversation(cid, "第二天换成博物馆")["kind"], "revision")
        self.assertEqual(store.continue_conversation(cid, "重新规划去成都")["kind"], "trip")


if __name__ == "__main__":
    unittest.main()


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_message_returns_same_job(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "dup.db"))
        cid = store.create_conversation()["conversation_id"]
        first = store.continue_conversation(cid, "从上海去杭州玩两天")
        second = store.continue_conversation(cid, "从上海去杭州玩两天")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(first["job_id"], second["job_id"])
        with store._connect() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        self.assertEqual(count, 1)


def test_greeting_without_trip_is_chat():
    assert classify_intent("你好", has_trip=False)["intent"] == "chat"
    assert classify_intent("谢谢", has_trip=False)["intent"] == "chat"
    assert classify_intent("门票多少钱", has_trip=False)["intent"] == "ask"
    assert classify_intent("杭州", has_trip=False)["intent"] == "new_trip"


def test_describe_trip_answers_feasibility_question():
    result = {"days": [{"day": 3, "activities": [
        {"title": "A", "start": "09:30", "duration": "3 小时", "transport": "打车约 15 分钟"},
        {"title": "B", "start": "13:30", "duration": "3 小时", "transport": ""},
    ]}], "constraints": {"day_windows": [{"day": 3, "hours": 10}]}}
    text = describe_trip(result, [3], "第三天会不会太赶")
    assert "第3天" in text and "节奏正常" in text
    text = describe_trip(result, [3], "第三天时间够吗")
    assert "第3天" in text

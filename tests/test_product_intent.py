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

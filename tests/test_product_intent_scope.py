import tempfile
import unittest
from pathlib import Path

from product.intent import classify_intent
from product.store import Store


class IntentScopeTests(unittest.TestCase):
    def test_cancel(self):
        result = classify_intent("算了，不去了", True)
        self.assertEqual(result["intent"], "revise_trip")
        self.assertEqual(result["action"], "cancel")

    def test_undo(self):
        self.assertEqual(classify_intent("退回上一版", True)["action"], "undo")

    def test_targeted_scope(self):
        result = classify_intent("第二天换成博物馆", True)
        self.assertEqual(result["scope"], "targeted")
        self.assertEqual(result["target_days"], [2])

    def test_global_scope(self):
        result = classify_intent("整体轻松一点", True)
        self.assertEqual(result["scope"], "global")
        self.assertEqual(result["action"], "modify")


class StoreActionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(str(Path(tmp.name) / "action.db"))
        self.cid = self.store.create_conversation()["conversation_id"]
        self.created = self.store.create_trip("杭州两天", conversation_id=self.cid)

    def _finish(self, summary):
        job = self.store.claim()
        self.store.finish(job, {"status": "complete", "summary": summary, "days": []})

    def test_cancel_keeps_single_version(self):
        out = self.store.continue_conversation(self.cid, "算了，不去了",
                                               intent={"intent": "revise_trip", "action": "cancel"})
        self.assertEqual(out["kind"], "cancelled")
        self.assertEqual(self.store.latest_trip(self.cid)["version"], 1)

    def test_undo_restores_previous_result(self):
        self._finish("v1")
        self.store.revise(self.created["trip_id"], "改一下")
        self._finish("v2")
        self.assertEqual(self.store.latest_result(self.cid)["summary"], "v2")
        out = self.store.continue_conversation(self.cid, "退回上一版",
                                               intent={"intent": "revise_trip", "action": "undo"})
        self.assertEqual(out["kind"], "undo")
        self.assertEqual(self.store.latest_result(self.cid)["summary"], "v1")
        self.assertEqual(self.store.latest_trip(self.cid)["version"], 3)
        again = self.store.continue_conversation(self.cid, "再退回一版",
                                                 intent={"intent": "revise_trip", "action": "undo"})
        self.assertEqual(again["kind"], "answer")


if __name__ == "__main__":
    unittest.main()

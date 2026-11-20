import asyncio
import tempfile
import unittest
from pathlib import Path

from product.intent import classify_with_model, route_intent
from product.store import Store


class FakeModel:
    def __init__(self, content, error=None):
        self.content = content
        self.error = error
        self.calls = 0

    async def complete(self, role, system, user, max_tokens=1200):
        self.calls += 1
        if self.error:
            raise self.error
        return {"content": self.content, "provider": "fake", "model": "fake", "role": role}


class ModelFallbackTests(unittest.TestCase):
    def test_model_used_when_rules_say_chat(self):
        model = FakeModel('{"intent":"revise_trip","confidence":0.9,"target_days":[2]}')
        result = asyncio.run(route_intent(model, "换个安静点的地方", True))
        self.assertEqual(result["intent"], "revise_trip")
        self.assertEqual(result["source"], "model")

    def test_low_confidence_falls_back_to_rules(self):
        model = FakeModel('{"intent":"revise_trip","confidence":0.3}')
        self.assertEqual(asyncio.run(route_intent(model, "嗯……", True))["intent"], "chat")

    def test_model_failure_falls_back_to_rules(self):
        result = asyncio.run(classify_with_model(FakeModel("", error=RuntimeError("boom")), "几点出发？", True))
        self.assertEqual(result["intent"], "ask")
        self.assertEqual(result["source"], "rules_fallback")

    def test_store_unclear_reply(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(str(Path(tmp.name) / "u.db"))
        cid = store.create_conversation()["conversation_id"]
        out = store.continue_conversation(cid, "嗯……", intent={"intent": "unclear"})
        self.assertEqual(out["kind"], "clarify_reply")
        self.assertIn("改行程", out["reply"]["content"])


if __name__ == "__main__":
    unittest.main()


class ModelFirstTests(unittest.TestCase):
    def test_model_overrides_wrong_rules(self):
        model = FakeModel('{"intent":"ask","confidence":0.9,"target_days":[3]}')
        result = asyncio.run(route_intent(model, "第三天远不远", True))
        self.assertEqual(result["intent"], "ask")
        self.assertEqual(result["source"], "model")

    def test_cancel_stays_on_rules(self):
        model = FakeModel('{"intent":"chat","confidence":0.9}')
        result = asyncio.run(route_intent(model, "取消吧", True))
        self.assertEqual(result["action"], "cancel")
        self.assertEqual(model.calls, 0)

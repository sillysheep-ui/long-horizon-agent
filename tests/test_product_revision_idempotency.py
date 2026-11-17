import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from product.store import Store


class RevisionIdempotencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(str(Path(tmp.name) / "idem.db"))
        self.created = self.store.create_trip("杭州两天")
        self.trip_id = self.created["trip_id"]
        self._finish(self.store.claim())

    def _finish(self, job):
        self.store.finish(job, {"status": "complete", "summary": "ok", "days": []})

    def _version(self):
        with self.store._connect() as db:
            return db.execute("SELECT version FROM trips WHERE id=?", (self.trip_id,)).fetchone()["version"]

    def test_same_instruction_is_duplicate(self):
        first = self.store.revise(self.trip_id, "第二天换成博物馆")
        self._finish(self.store.claim())
        second = self.store.revise(self.trip_id, "第二天 换成博物馆。")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(self._version(), 2)

    def test_different_instruction_creates_new_version(self):
        self.store.revise(self.trip_id, "第二天换成博物馆")
        self._finish(self.store.claim())
        other = self.store.revise(self.trip_id, "第三天换成公园")
        self.assertFalse(other.get("duplicate"))
        self.assertEqual(self._version(), 3)

    def test_expired_window_creates_new_version(self):
        first = self.store.revise(self.trip_id, "第二天换成博物馆")
        self._finish(self.store.claim())
        old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET created_at=? WHERE id=?", (old, first["job_id"]))
        again = self.store.revise(self.trip_id, "第二天换成博物馆")
        self.assertFalse(again.get("duplicate"))
        self.assertNotEqual(again["job_id"], first["job_id"])

    def test_environment_window_is_configurable(self):
        import os

        os.environ["IDEMPOTENCY_WINDOW_SECONDS"] = "5"
        self.addCleanup(os.environ.pop, "IDEMPOTENCY_WINDOW_SECONDS", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(Store(str(Path(tmp.name) / "w.db")).idempotency_seconds, 5)


if __name__ == "__main__":
    unittest.main()

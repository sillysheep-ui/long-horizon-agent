"""失败路径：把异常翻译成人话，并保证任务不会永远卡住。"""

import os
import tempfile

os.environ.setdefault("TRAVEL_DB_PATH", os.path.join(tempfile.gettempdir(), "travel-errors-test.db"))

import asyncio  # noqa: E402

import httpx  # noqa: E402

import product.api as api_module  # noqa: E402
from product.errors import friendly_error  # noqa: E402


def test_friendly_error_mapping():
    assert friendly_error(TimeoutError())["code"] == "timeout"
    assert friendly_error(httpx.ConnectError("down"))["code"] == "network"
    assert friendly_error(httpx.ReadTimeout("slow"))["code"] == "timeout"
    assert friendly_error(RuntimeError("HTTP 429 too many"))["code"] == "rate_limited"
    assert friendly_error(ValueError("模型输出超出长度限制"))["code"] == "too_long"
    assert friendly_error(ValueError("规划 Agent 返回的天数与需求不一致"))["code"] == "bad_plan"
    internal = friendly_error(RuntimeError("boom"))
    assert internal["code"] == "internal" and internal["retryable"] is True


def test_worker_turns_exception_into_friendly_reply(monkeypatch):
    async def boom(_job):
        raise httpx.ConnectError("connection refused")

    from product.store import Store

    monkeypatch.setattr(api_module.workflow, "run", boom)
    with tempfile.TemporaryDirectory() as folder:
        store = Store(os.path.join(folder, "errors.db"))
        monkeypatch.setattr(api_module, "store", store)
        cid = store.create_conversation("失败路径")["conversation_id"]
        trip = store.create_trip("从上海去杭州玩两天", conversation_id=cid)
        job = store.trip(trip["trip_id"])["jobs"][0]
        asyncio.run(api_module._execute_job(job))
        messages = store.conversation(cid)["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert "连不上模型服务" in assistant[-1]["content"]


def test_requeue_stale_jobs():
    from product.store import Store
    store = Store(os.path.join(tempfile.mkdtemp(), "queue.db"))
    store.create_trip("从上海去杭州玩两天")
    job = store.claim()
    with store._connect() as db:  # noqa: SLF001 - 测试需要改时间戳
        db.execute("UPDATE jobs SET status='running', updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                   (job["id"],))
    assert store.requeue_stale(60) >= 1
    with store._connect() as db:  # noqa: SLF001
        row = db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
    assert row["status"] == "queued"


def test_resend_after_failure_revises_same_trip(monkeypatch):
    from product.store import Store

    tmp = tempfile.TemporaryDirectory()
    store = Store(os.path.join(tmp.name, "retry.db"))
    cid = store.create_conversation("重试")["conversation_id"]
    first = store.create_trip("从上海去杭州玩两天", conversation_id=cid)
    job = store.trip(first["trip_id"])["jobs"][0]
    store.finish(job, None, "连不上模型服务", "network", True)
    again = store.continue_conversation(cid, "从上海去杭州玩两天")
    assert again["kind"] == "revision"
    assert again["trip_id"] == first["trip_id"]
    assert again["job_id"] != first["job_id"]
    tmp.cleanup()

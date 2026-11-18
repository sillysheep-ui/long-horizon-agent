"""浏览器可独立使用的旅行规划 API。"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from product.errors import friendly_error
from product.intent import route_intent
from product.model_gateway import ModelGateway
from product.orchestrator import AgentWorkflow
from product.profile import attach_pending, extract_profile, handle_confirmation, should_extract
from product.store import Store

load_dotenv()
logger = logging.getLogger(__name__)
store = Store()
workflow = AgentWorkflow(store)
gateway = ModelGateway()
_profile_tasks: set[asyncio.Task] = set()


async def _extract_profile_bg(conversation_id: str) -> None:
    try:
        stats = await extract_profile(gateway, store, conversation_id)
        if stats.get("facts"):
            logger.info("已记住 %s 条偏好: %s", stats["facts"], conversation_id)
    except Exception:
        logger.exception("用户偏好抽取失败: %s", conversation_id)


JOB_TIMEOUT_SECONDS = float(os.getenv("JOB_TIMEOUT_SECONDS", "300"))


async def _execute_job(job: dict) -> None:
    """执行单个任务；任何异常都转成用户能看懂的话，绝不让 worker 退出。"""
    try:
        result = await asyncio.wait_for(workflow.run(job), timeout=JOB_TIMEOUT_SECONDS)
        await asyncio.to_thread(store.finish, job, result)
        await asyncio.to_thread(store.record_reply, job["id"], result)
    except Exception as exc:
        info = friendly_error(exc)
        logger.exception("任务执行失败: %s (%s)", job["id"], info["code"])
        try:
            await asyncio.to_thread(store.finish, job, None, str(info["message"]),
                                    str(info["code"]), bool(info["retryable"]))
            await asyncio.to_thread(store.record_reply, job["id"], None, str(info["message"]))
        except Exception:
            logger.exception("记录失败状态也失败了: %s", job["id"])


async def _worker() -> None:
    last_sweep = 0.0
    while True:
        now = time.monotonic()
        if now - last_sweep > 60:
            last_sweep = now
            try:
                stale = await asyncio.to_thread(store.requeue_stale, 900)
                if stale:
                    logger.warning("把 %s 个超时任务放回队列", stale)
            except Exception:
                logger.exception("清理僵死任务失败")
        job = await asyncio.to_thread(store.claim)
        if job is None:
            await asyncio.sleep(0.5)
            continue
        store.event(job["id"], "running", {"message": "开始执行"})
        await _execute_job(job)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    worker = asyncio.create_task(_worker())
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


app = FastAPI(title="旅行规划 Agent", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.info("请求格式不对: %s", exc.errors()[:2])
    return JSONResponse(status_code=422, content={"detail": "请求格式不对，请检查输入后再试。"})


@app.exception_handler(Exception)
async def _unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("接口未处理异常: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "服务出了点问题，请稍后重试。"})
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["GET", "POST"], allow_headers=["*"], allow_credentials=False)


class TripRequest(BaseModel):
    query: str = Field(min_length=5, max_length=3000)


class RevisionRequest(BaseModel):
    instruction: str = Field(min_length=2, max_length=1000)


class MessageRequest(BaseModel):
    content: str = Field(min_length=2, max_length=3000)


@app.post("/api/conversations")
def create_conversation() -> dict:
    return store.create_conversation()


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    conversation = store.conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conversation


@app.post("/api/conversations/{conversation_id}/messages")
async def post_message(conversation_id: str, request: MessageRequest) -> dict:
    if store.conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    content = request.content.strip()
    acked = handle_confirmation(store, conversation_id, content)
    if acked is not None:
        return acked
    last_seen = store.last_message_at(conversation_id)
    latest = store.latest_trip(conversation_id)
    prior = store.latest_result(conversation_id) if latest else None
    intent = await route_intent(gateway, content, has_trip=latest is not None,
                                prior_status=(prior or {}).get("status"))
    result = store.continue_conversation(conversation_id, content, intent=intent)
    if result is None:
        raise HTTPException(status_code=404, detail="行程不存在")
    if result.get("reply"):
        attach_pending(store, result["reply"]["id"])
    if should_extract(last_seen):
        task = asyncio.create_task(_extract_profile_bg(conversation_id))
        _profile_tasks.add(task)
        task.add_done_callback(_profile_tasks.discard)
    return result


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/trips")
def create_trip(request: TripRequest) -> dict:
    return store.create_trip(request.query.strip())


@app.get("/api/trips/{trip_id}")
def get_trip(trip_id: str) -> dict:
    trip = store.trip(trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="行程不存在")
    return trip


@app.post("/api/trips/{trip_id}/revisions")
def revise_trip(trip_id: str, request: RevisionRequest) -> dict:
    result = store.revise(trip_id, request.instruction.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="行程不存在")
    return result


@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str, after: int = 0) -> StreamingResponse:
    if store.job_status(job_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def generate() -> AsyncIterator[str]:
        last = max(0, after)
        while True:
            events = await asyncio.to_thread(store.events, job_id, last)
            for event in events:
                last = event["id"]
                yield "id: %s\nevent: %s\ndata: %s\n\n" % (
                    last, event["kind"], json.dumps(event, ensure_ascii=False))
            if await asyncio.to_thread(store.job_status, job_id) in ("completed", "failed"):
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/api/jobs/{job_id}/events/list")
def list_events(job_id: str, after: int = 0) -> list:
    if store.job_status(job_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return store.events(job_id, max(0, after))


web_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
if web_dist.exists():
    app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="web")

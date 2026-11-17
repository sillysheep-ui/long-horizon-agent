"""浏览器可独立使用的旅行规划 API。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from product.intent import route_intent
from product.model_gateway import ModelGateway
from product.orchestrator import AgentWorkflow
from product.profile import extract_profile, should_extract
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


async def _worker() -> None:
    while True:
        job = await asyncio.to_thread(store.claim)
        if job is None:
            await asyncio.sleep(0.5)
            continue
        store.event(job["id"], "running", {"message": "开始执行"})
        try:
            result = await workflow.run(job)
            await asyncio.to_thread(store.finish, job, result)
            await asyncio.to_thread(store.record_reply, job["id"], result)
        except Exception as exc:
            logger.exception("任务执行失败: %s", job["id"])
            await asyncio.to_thread(store.finish, job, None, str(exc)[:300])
            await asyncio.to_thread(store.record_reply, job["id"], None, str(exc)[:300])


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
    last_seen = store.last_message_at(conversation_id)
    latest = store.latest_trip(conversation_id)
    prior = store.latest_result(conversation_id) if latest else None
    intent = await route_intent(gateway, content, has_trip=latest is not None,
                                prior_status=(prior or {}).get("status"))
    result = store.continue_conversation(conversation_id, content, intent=intent)
    if result is None:
        raise HTTPException(status_code=404, detail="行程不存在")
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

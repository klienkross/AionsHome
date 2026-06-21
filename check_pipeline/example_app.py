"""
FastAPI 接线示例 — 展示如何把 check pipeline 接入 HTTP 服务。
不是核心代码，仅供参考。

启动: uvicorn check_pipeline.example_app:app --reload
测试: curl -X POST http://localhost:8000/check -H "Content-Type: application/json" \
      -d '{"type":"heartbeat","device_id":"phone-1","ts":1750500000,"payload":{"battery":80}}'
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .event_bus import EventBus
from .schemas import HeartbeatSchema, SensorSchema, CheckSchema
from .recorder import Recorder, MemoryStore
from .notifier import Notifier, MentionInChat, PushNotify, WebhookCallback
from .watchdog import Watchdog
from .ingress import Ingress

bus = EventBus()
store = MemoryStore()
recorder = Recorder(store)
notifier = Notifier(bus, recorder)
notifier.register(MentionInChat())
notifier.register(PushNotify())
notifier.register(WebhookCallback())

watchdog = Watchdog(bus, notifier)

bus.on("*", recorder.log)
bus.on("heartbeat", watchdog.feed)
bus.on("check", watchdog.feed)

schemas = {
    "heartbeat": HeartbeatSchema(),
    "sensor": SensorSchema(),
    "check": CheckSchema(),
}
ingress = Ingress(bus, schemas)


async def _tick_loop():
    while True:
        await watchdog.tick()
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_tick_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.post("/check")
async def check_endpoint(request: Request):
    raw = await request.json()
    result = await ingress.handle(raw)
    status = 200 if result.get("ok") else 400
    return JSONResponse(content=result, status_code=status)

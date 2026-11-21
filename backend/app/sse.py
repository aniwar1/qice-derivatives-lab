from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import AsyncIterator

from fastapi import Request

from .db import RunNotFound, StrategyStore, TERMINAL_STATUSES
from .models import RunEvent


def encode_sse(event: RunEvent) -> str:
    payload = event.model_dump(mode="json")
    return (
        f"id: {event.event_id}\n"
        f"event: {event.type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def encode_sse_heartbeat(*, run_id: str, status: str) -> str:
    """Encode a transport heartbeat as a named browser event.

    A line beginning with ``:`` is an SSE comment.  It keeps a socket alive,
    but the browser's ``EventSource`` deliberately does not deliver it to
    ``onmessage`` or named listeners.  The dashboard needs a visible liveness
    signal while the Codex turn is running, so synthetic heartbeats are sent
    as regular events without an ``id`` (they are not persisted checkpoints).
    """

    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    payload = {
        "run_id": run_id,
        "type": "heartbeat",
        "stage": "agent" if status in {"queued", "running"} else status,
        "message": (
            "Codex 请求仍在运行，等待结构化结果。"
            if status == "running"
            else "策略任务仍在队列中，等待 Codex 接管。"
        ),
        "occurred_at": now,
        "payload": {"status": status, "transport_keepalive": True},
    }
    return (
        "event: heartbeat\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def run_event_stream(
    *,
    request: Request,
    store: StrategyStore,
    run_id: str,
    after_event_id: int,
    heartbeat_seconds: int,
    poll_milliseconds: int,
) -> AsyncIterator[str]:
    cursor = max(0, after_event_id)
    last_emit = asyncio.get_running_loop().time()
    while True:
        if await request.is_disconnected():
            return
        events = await store.get_events_after(run_id, cursor)
        for raw in events:
            event = RunEvent.model_validate(raw)
            cursor = event.event_id
            last_emit = asyncio.get_running_loop().time()
            yield encode_sse(event)

        run = await store.get_run(run_id)
        if run is None:
            raise RunNotFound(run_id)
        if run["status"] in TERMINAL_STATUSES and not events:
            return

        now = asyncio.get_running_loop().time()
        if now - last_emit >= heartbeat_seconds:
            yield encode_sse_heartbeat(run_id=run_id, status=run["status"])
            last_emit = now
        await asyncio.sleep(poll_milliseconds / 1000)

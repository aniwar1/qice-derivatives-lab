from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from datetime import datetime, timezone
import math
from typing import AsyncIterator
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import requests

from . import __version__
from .codex_gateway import CodexGateway, LocalCodexGateway
from .config import Settings
from .db import ActiveRunConflict, StrategyStore
from .models import StrategyRunAccepted, StrategyRunRequest, StrategyRunView
from .service import InvalidStrategyScope, StrategyService
from .sse import run_event_stream


def create_app(
    *,
    settings: Settings | None = None,
    store: StrategyStore | None = None,
    gateway: CodexGateway | None = None,
) -> FastAPI:
    configured = settings or Settings.from_env()
    repository = store or StrategyStore(configured.storage_url)
    codex = gateway or LocalCodexGateway(configured)
    service = StrategyService(settings=configured, store=repository, gateway=codex)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="中国期货与买方期权决策 Agent",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = configured
    app.state.store = repository
    app.state.gateway = codex
    app.state.strategy_service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(configured.allowed_hosts))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    @app.exception_handler(ActiveRunConflict)
    async def active_run_conflict(_: Request, exc: ActiveRunConflict) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "strategy_run_already_active",
                "message": "已有策略更新正在运行。",
                "active_run_id": exc.active_run_id,
            },
        )

    @app.exception_handler(InvalidStrategyScope)
    async def invalid_scope(_: Request, exc: InvalidStrategyScope) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": "invalid_strategy_scope", "message": str(exc)},
        )

    @app.get("/api/health")
    async def health() -> dict:
        database_ok = await repository.ping()
        codex_status = await codex.status()
        codex_ready = codex_status.authenticated and (
            not configured.require_chatgpt_auth or codex_status.auth_mode == "chatgpt_managed"
        )
        return {
            "status": "ready" if database_ok else "degraded",
            "service": configured.service_name,
            "version": __version__,
            "checks": {
                "database": "ready" if database_ok else "unavailable",
                "codex": "ready" if codex_ready else "authentication_required",
                "chatgpt_managed_auth": codex_status.auth_mode == "chatgpt_managed",
            },
        }

    @app.get("/api/codex/status")
    async def codex_status() -> dict:
        return (await codex.status()).model_dump(mode="json")

    @app.get("/api/dashboard")
    async def dashboard() -> dict:
        return await service.dashboard.project()

    @app.get("/api/market/quotes/{product}")
    async def market_quote(product: str) -> dict:
        symbol = product.upper()
        if symbol not in service.domain.allowed_products():
            raise HTTPException(status_code=404, detail="该品种不在研究范围内。")

        from scripts.fetch_futures import direct_quote

        try:
            quote = await asyncio.to_thread(direct_quote, symbol, timeout=5)
            observed = datetime.strptime(quote["quote_time"], "%Y-%m-%d %H:%M:%S")
            if not math.isfinite(quote["price"]) or quote["price"] <= 0:
                raise ValueError("无有效价格")
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise HTTPException(status_code=502, detail="公开行情暂不可用，请稍后重试。") from exc
        return {
            "product": symbol,
            "price": quote["price"],
            "observed_at": observed.replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat(),
            "source_url": quote["source_url"],
        }

    @app.get("/api/research/runs")
    async def research_runs(limit: int = Query(30, ge=1, le=100)) -> list[dict]:
        return await service.research_runs(limit)

    @app.get("/api/research/runs/{run_id}")
    async def research_detail(run_id: str) -> dict:
        try:
            detail = await service.research_detail(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="研究档案未通过完整性校验。") from exc
        if detail is None:
            raise HTTPException(status_code=404, detail="研究任务不存在。")
        return detail

    @app.get("/api/research/runs/{run_id}/evidence/{digest}")
    async def research_evidence(run_id: str, digest: str) -> Response:
        if await service.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="研究任务不存在。")
        try:
            blob = await asyncio.to_thread(service.research_archive.evidence, run_id, digest)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="行情原件未通过完整性校验。") from exc
        if blob is None:
            raise HTTPException(status_code=404, detail="本轮没有该行情原件。")
        return Response(blob, media_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="market-evidence.json"',
            "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/research/runs/{run_id}/replay")
    async def replay(run_id: str, at: datetime | None = None) -> dict:
        cutoff = at or datetime.now(timezone.utc)
        if cutoff.utcoffset() is None or cutoff > datetime.now(timezone.utc):
            raise HTTPException(status_code=422, detail="请选择包含时区且不晚于当前时间的回放时点。")
        try:
            report = await service.replay(run_id, cutoff)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="研究档案未通过回放校验。") from exc
        if report is None:
            raise HTTPException(status_code=404, detail="研究任务不存在。")
        return report

    @app.get("/api/strategy/latest")
    async def latest_strategy() -> dict:
        resolution = await service.latest_strategy_resolution()
        if resolution.record is None:
            messages = {
                "missing": "尚无已完成策略。",
                "stale": "上一版策略仍可查看；请在需要时手动更新。",
                # Keep a defensive compatibility branch for records produced
                # by an older build.  Current freshness resolution never
                # returns ``expired`` for a valid record.
                "expired": "上一版策略仍可查看；请手动更新。",
                "invalid": "最近一次策略记录无效，已停止公开。",
            }
            error_codes = {
                "missing": "latest_strategy_unavailable",
                "stale": "latest_strategy_manual_refresh",
                "expired": "latest_strategy_expired",
                "invalid": "latest_strategy_invalid",
            }
            raise HTTPException(
                status_code=404,
                detail={
                    "error": error_codes[resolution.freshness.state],
                    "message": messages[resolution.freshness.state],
                    "status": resolution.freshness.model_dump(),
                },
            )
        return resolution.record.model_dump(mode="json")

    @app.post(
        "/api/strategy-runs",
        response_model=StrategyRunAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_strategy_run(payload: StrategyRunRequest) -> StrategyRunAccepted:
        return await service.trigger(payload)

    @app.get("/api/strategy-runs/{run_id}", response_model=StrategyRunView)
    async def get_strategy_run(run_id: str) -> StrategyRunView:
        run = await service.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="策略任务不存在。")
        return run

    @app.get("/api/runs/{run_id}/events")
    async def strategy_events(
        request: Request,
        run_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        run = await repository.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="策略任务不存在。")
        try:
            cursor = int(last_event_id or "0")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID 必须为整数。") from exc
        if cursor < 0:
            raise HTTPException(status_code=400, detail="Last-Event-ID 不能为负数。")
        stream = run_event_stream(
            request=request,
            store=repository,
            run_id=run_id,
            after_event_id=cursor,
            heartbeat_seconds=configured.sse_heartbeat_seconds,
            poll_milliseconds=configured.sse_poll_milliseconds,
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    frontend_dist = configured.workspace_root / "frontend" / "dist"
    if (frontend_dist / "index.html").is_file():
        # The source trees stay separated; production serves the immutable UI
        # build from the same loopback origin to avoid a second resident server.
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()

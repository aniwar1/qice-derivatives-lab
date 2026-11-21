from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

import app.service as service_module
from app.codex_gateway import CodexGatewayError
from app.config import Settings
from app.db import StrategyStore
from app.main import create_app
from app.models import LatestStrategyRecord
from conftest import FakeDomain, FakeGateway, strategy_payload


BJT = ZoneInfo("Asia/Shanghai")


async def test_clean_install_runs_without_codex(tmp_path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    for directory in ("config", "schemas"):
        (tmp_path / directory).symlink_to(root / directory, target_is_directory=True)
    (tmp_path / "SKILL.md").symlink_to(root / "SKILL.md")
    monkeypatch.setenv("CFDA_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CFDA_CODEX_BIN", str(tmp_path / "missing-codex"))
    monkeypatch.delenv("CFDA_DATABASE_URL", raising=False)
    monkeypatch.delenv("CFDA_AUTO_REFRESH_IF_EMPTY", raising=False)
    app = create_app(settings=Settings.from_env())
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = (await client.get("/api/health")).json()
            dashboard = (await client.get("/api/dashboard")).json()
    assert health["status"] == "ready"
    assert health["checks"]["codex"] == "authentication_required"
    assert dashboard["system"]["status"] == "ready"
    assert dashboard["latest_strategy"] is None
    assert dashboard["active_run"] is None
    assert len(dashboard["product_catalog"]) == 22
    assert (tmp_path / "local-data" / "qice.sqlite3").is_file()


async def test_market_quote_validates_product_and_preserves_provider_time(settings, monkeypatch) -> None:
    from scripts import fetch_futures

    def quote(symbol: str, timeout: int) -> dict:
        assert symbol == "JM"
        assert timeout == 5
        return {"price": 1150.5, "quote_time": "2026-09-11 15:00:00", "source_url": "https://example.com/quote"}

    monkeypatch.setattr(fetch_futures, "direct_quote", quote)
    app = create_app(settings=settings, gateway=FakeGateway())
    app.state.strategy_service.domain = FakeDomain(settings.workspace_root)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/api/market/quotes/UNKNOWN")).status_code == 404
        response = await client.get("/api/market/quotes/jm")
    assert response.status_code == 200
    assert response.json() == {
        "product": "JM", "price": 1150.5,
        "observed_at": "2026-09-11T15:00:00+08:00", "source_url": "https://example.com/quote",
    }


async def wait_for_terminal(client: httpx.AsyncClient, run_id: str) -> dict:
    for _ in range(50):
        response = await client.get(f"/api/strategy-runs/{run_id}")
        payload = response.json()
        if payload["status"] in {"completed", "failed"}:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish")


async def wait_for_event(store: StrategyStore, run_id: str, event_type: str) -> dict:
    """Poll the repository briefly without depending on wall-clock timing."""

    for _ in range(100):
        events = await store.get_events_after(run_id, 0)
        match = next((event for event in events if event["type"] == event_type), None)
        if match is not None:
            return match
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not emit {event_type} event")


@pytest.mark.asyncio
async def test_api_refresh_dashboard_and_sse(settings) -> None:
    store = StrategyStore(settings.database_url)
    gateway = FakeGateway()
    app = create_app(settings=settings, store=store, gateway=gateway)
    app.state.strategy_service.domain = FakeDomain(settings.workspace_root)
    app.state.strategy_service.dashboard.domain = app.state.strategy_service.domain
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            health = await client.get("/api/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ready"

            started = await client.post("/api/strategy-runs", json={})
            assert started.status_code == 202
            run_id = started.json()["run_id"]
            terminal = await wait_for_terminal(client, run_id)
            assert terminal["status"] == "completed"
            assert terminal["result"]["strategy"]["session_daily_bar"]["bar_state"] == "session_partial"

            latest = await client.get("/api/strategy/latest")
            assert latest.status_code == 200
            assert latest.json()["source"]["provider"] == "codex"
            dashboard = await client.get("/api/dashboard")
            assert dashboard.status_code == 200
            projected = dashboard.json()
            assert projected["system"]["hidden_reasoning_persisted"] is False
            assert projected["latest_strategy"]["directional_view"] == "long"
            assert projected["latest_strategy_record"]["source"]["provider"] == "codex"
            assert projected["evidence"][0]["status"]["integrity"] == "unverified"
            assert projected["latest_strategy_status"]["state"] == "current"
            assert projected["generated_at"] == latest.json()["updated_at"]
            assert projected["strategy_updated_at"] == latest.json()["updated_at"]
            assert projected["strategy_as_of"] == latest.json()["strategy"]["as_of"]
            assert projected["projected_at"]

            projected_again = (await client.get("/api/dashboard")).json()
            assert projected_again["generated_at"] == projected["generated_at"]

            events = await client.get(f"/api/runs/{run_id}/events")
            assert events.status_code == 200
            assert "event: completed" in events.text
            assert "Codex" in events.text
    assert gateway.calls and "session_partial" in gateway.calls[0]
    assert gateway.workspace_roots and gateway.workspace_roots[0] is not None
    assert "local-data/codex-runs/" in gateway.calls[0]
    assert not gateway.workspace_roots[0].exists()


@pytest.mark.asyncio
async def test_api_emits_visible_codex_heartbeat_during_long_turn(settings, monkeypatch) -> None:
    """A slow Codex call must produce a named SSE event, not just a comment."""

    monkeypatch.setattr(service_module, "_CODEX_PROGRESS_INTERVAL_SECONDS", 0.01)
    store = StrategyStore(settings.database_url)
    gateway = FakeGateway()
    gateway.release = asyncio.Event()
    app = create_app(settings=settings, store=store, gateway=gateway)
    app.state.strategy_service.domain = FakeDomain(settings.workspace_root)
    app.state.strategy_service.dashboard.domain = app.state.strategy_service.domain
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started = await client.post("/api/strategy-runs", json={})
            assert started.status_code == 202
            run_id = started.json()["run_id"]

            heartbeat = await wait_for_event(store, run_id, "heartbeat")
            assert heartbeat["stage"] == "agent"
            assert heartbeat["payload"]["codex_call_active"] is True
            assert "Codex 正在生成策略" in heartbeat["message"]

            # The gateway is still blocked at this point; release it and make
            # sure the same run reaches a terminal state rather than leaking
            # the progress task.
            gateway.release.set()
            terminal = await wait_for_terminal(client, run_id)
            assert terminal["status"] == "completed"

            events_response = await client.get(f"/api/runs/{run_id}/events")
            assert events_response.status_code == 200
            assert "event: heartbeat" in events_response.text
            assert "Codex 已回调并返回结构化策略" in events_response.text
            assert '"codex_callback":true' in events_response.text


def expired_record(kind: str, now: datetime) -> LatestStrategyRecord:
    bar_state = "completed" if kind == "reassess_by" else "session_partial"
    payload = strategy_payload(
        bar_state=bar_state,
        entry_style="confirmed_breakout" if bar_state == "completed" else "anticipatory_breakout",
    ).model_dump()
    as_of = now - timedelta(hours=3)
    payload["as_of"] = as_of
    payload["reassess_by"] = (
        now - timedelta(minutes=1)
        if kind == "reassess_by"
        else now + timedelta(hours=3)
    )
    bar = payload["session_daily_bar"]
    assert bar is not None
    bar["trade_date"] = as_of.date()
    bar["as_of"] = as_of
    bar["source"]["retrieved_at"] = as_of - timedelta(minutes=1)
    if bar["calendar_evidence"] is not None:
        bar["calendar_evidence"]["trade_date"] = as_of.date()
        bar["calendar_evidence"]["retrieved_at"] = as_of - timedelta(minutes=1)
    bar["coverage"].update(
        {
            "session_started_at": as_of - timedelta(hours=2),
            "checkpoint_at": as_of - timedelta(hours=1),
            "observed_through": as_of - timedelta(minutes=1),
            "expected_daily_close_at": (
                now - timedelta(minutes=1)
                if kind == "session_partial"
                else as_of + timedelta(hours=1)
            ),
        }
    )
    return LatestStrategyRecord.model_validate(
        {
            "run_id": f"expired-{kind}",
            "updated_at": as_of + timedelta(minutes=2),
            "source": {
                "provider": "codex",
                "transport": "cli_exec_json",
                "auth_mode": "chatgpt_managed",
                "authenticated": True,
                "model": "configured_default",
                "model_resolution": "inherited",
                "browser_direct_access": False,
                "credentials_exposed": False,
            },
            "strategy": payload,
        }
    )


class DomainWithStaleStrategy(FakeDomain):
    def build(self) -> dict:
        return {
            **super().build(),
            "evidence": [{"domain": "stale-domain"}],
            "underlying_thesis": {"product": "JM", "directional_view": "long"},
            "option_expression": {"capital_action": "buy_to_open"},
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expiry_kind", "reason"),
    [
        ("reassess_by", "reassess_by_elapsed"),
        ("session_partial", "session_partial_daily_close_elapsed"),
    ],
)
async def test_public_latest_and_dashboard_keep_stale_strategy_for_manual_refresh(
    settings,
    expiry_kind: str,
    reason: str,
) -> None:
    now = datetime.now(tz=BJT).replace(microsecond=0)
    record = expired_record(expiry_kind, now)
    store = StrategyStore(settings.database_url)
    await store.initialize()
    await store.create_run(
        run_id=record.run_id,
        request={"mode": "all_market_scan", "option_analysis_requested": True},
        runtime={"provider": "codex"},
    )
    await store.mark_running(record.run_id)
    await store.complete_run(record.run_id, record.model_dump(mode="json"))

    app = create_app(settings=settings, store=store, gateway=FakeGateway())
    app.state.strategy_service.domain = DomainWithStaleStrategy(settings.workspace_root)
    app.state.strategy_service.dashboard.domain = app.state.strategy_service.domain
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            latest = await client.get("/api/strategy/latest")
            assert latest.status_code == 200
            assert latest.json()["run_id"] == record.run_id

            projected = (await client.get("/api/dashboard")).json()
            assert projected["latest_strategy"] is not None
            assert projected["latest_strategy_record"]["run_id"] == record.run_id
            assert projected["latest_strategy_status"]["state"] == "stale"
            assert projected["latest_strategy_status"]["reason"] == reason
            assert projected["latest_strategy_status"]["requires_manual_refresh"] is True
            assert projected["generated_at"] == record.updated_at.isoformat()
            assert projected["strategy_updated_at"] == record.updated_at.isoformat()
            assert projected["strategy_as_of"] == record.strategy.as_of.isoformat()
            assert projected["underlying_thesis"] is not None
            assert projected["option_expression"] is None
            assert projected["evidence"]
            assert projected["domain_snapshot"]["underlying_thesis"] is not None
            assert projected["domain_snapshot"]["evidence"]

            historical = await client.get(f"/api/strategy-runs/{record.run_id}")
            assert historical.status_code == 200
            assert historical.json()["result"]["run_id"] == record.run_id


@pytest.mark.asyncio
async def test_service_revalidates_quality_review_before_persistence(settings) -> None:
    payload = strategy_payload()
    # Simulate a custom gateway mutating an already-created model instance.
    # The service persistence boundary must not trust that instance blindly.
    payload.quality_review.critic_revisions = 0  # type: ignore[assignment]
    store = StrategyStore(settings.database_url)
    app = create_app(settings=settings, store=store, gateway=FakeGateway(payload))
    app.state.strategy_service.domain = FakeDomain(settings.workspace_root)
    app.state.strategy_service.dashboard.domain = app.state.strategy_service.domain
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started = await client.post("/api/strategy-runs", json={})
            terminal = await wait_for_terminal(client, started.json()["run_id"])

            assert terminal["status"] == "failed"
            assert terminal["error_code"] == "strategy_validation_failed"
            assert terminal["result"] is None
            assert await store.get_latest_completed() is None


@pytest.mark.asyncio
async def test_api_records_a_codex_timeout_as_a_failed_run(settings) -> None:
    class TimeoutGateway(FakeGateway):
        async def run_strategy(self, prompt: str, *, workspace_root=None):
            self.calls.append(prompt)
            raise CodexGatewayError(
                "codex_timeout", "Codex 决策超时，任务已安全终止。"
            )

    store = StrategyStore(settings.database_url)
    gateway = TimeoutGateway()
    app = create_app(settings=settings, store=store, gateway=gateway)
    app.state.strategy_service.domain = FakeDomain(settings.workspace_root)
    app.state.strategy_service.dashboard.domain = app.state.strategy_service.domain
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started = await client.post("/api/strategy-runs", json={})
            terminal = await wait_for_terminal(client, started.json()["run_id"])

            assert terminal["status"] == "failed"
            assert terminal["error_code"] == "codex_timeout"
            assert terminal["result"] is None
            assert await store.get_latest_completed() is None


@pytest.mark.asyncio
async def test_api_rejects_overlapping_refreshes(settings) -> None:
    store = StrategyStore(settings.database_url)
    gateway = FakeGateway()
    gateway.release = asyncio.Event()
    app = create_app(settings=settings, store=store, gateway=gateway)
    app.state.strategy_service.domain = FakeDomain(settings.workspace_root)
    app.state.strategy_service.dashboard.domain = app.state.strategy_service.domain
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post("/api/strategy-runs", json={})
            assert first.status_code == 202
            second = await client.post("/api/strategy-runs", json={})
            assert second.status_code == 409
            assert second.json()["active_run_id"] == first.json()["run_id"]
            gateway.release.set()
            await wait_for_terminal(client, first.json()["run_id"])


@pytest.mark.asyncio
async def test_api_rejects_product_outside_universe(settings) -> None:
    store = StrategyStore(settings.database_url)
    app = create_app(settings=settings, store=store, gateway=FakeGateway())
    app.state.strategy_service.domain = FakeDomain(settings.workspace_root)
    app.state.strategy_service.dashboard.domain = app.state.strategy_service.domain
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/strategy-runs",
                json={"mode": "single_analysis", "product": "CU", "option_analysis_requested": False},
            )
            assert response.status_code == 422
            assert response.json()["error"] == "invalid_strategy_scope"

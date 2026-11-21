from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.models import CodexRuntimeMetadata, LatestStrategyRecord, StrategyRunRequest
from app.service import StrategyService
from conftest import FakeDomain, FakeGateway, strategy_payload


BJT = ZoneInfo("Asia/Shanghai")


class StartupStore:
    def __init__(self, *, latest: dict[str, Any] | None, active: dict[str, Any] | None) -> None:
        self.latest = latest
        self.active = active
        self.initialized = False

    async def initialize(self) -> list[str]:
        self.initialized = True
        return []

    async def get_latest_completed(self) -> dict[str, Any] | None:
        return self.latest

    async def get_active_run(self) -> dict[str, Any] | None:
        return self.active


def current_record() -> dict[str, Any]:
    now = datetime.now(tz=BJT).replace(microsecond=0)
    return LatestStrategyRecord(
        run_id="startup-current",
        updated_at=now,
        source=CodexRuntimeMetadata(
            transport="cli_exec_json",
            auth_mode="chatgpt_managed",
            authenticated=True,
            model="configured_default",
            model_resolution="inherited",
        ),
        strategy=strategy_payload(
            bar_state="completed",
            entry_style="confirmed_breakout",
        ),
    ).model_dump(mode="json")


def expired_record() -> dict[str, Any]:
    now = datetime.now(tz=BJT).replace(microsecond=0)
    record = current_record()
    record["strategy"]["as_of"] = (now - timedelta(minutes=10)).isoformat()
    record["strategy"]["reassess_by"] = (now - timedelta(minutes=1)).isoformat()
    return record


async def start_service(settings, *, latest: dict[str, Any] | None, active=None):
    startup_store = StartupStore(latest=latest, active=active)
    service = StrategyService(
        settings=replace(settings, auto_refresh_if_empty=True),
        store=startup_store,  # type: ignore[arg-type]
        gateway=FakeGateway(),
        domain=FakeDomain(settings.workspace_root),
    )
    service.trigger = AsyncMock()  # type: ignore[method-assign]
    await service.start()
    assert startup_store.initialized is True
    return service.trigger


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("latest", "should_refresh"),
    [
        (None, True),
        ({"result": {"schema_version": "latest_strategy_record.v1"}}, True),
        ({"result": expired_record()}, False),
    ],
    ids=["missing", "invalid", "stale"],
)
async def test_start_only_refreshes_when_no_valid_record_exists(settings, latest, should_refresh) -> None:
    trigger = await start_service(settings, latest=latest)

    if should_refresh:
        trigger.assert_awaited_once()
        request = trigger.await_args.args[0]
        assert request == StrategyRunRequest()
    else:
        trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_does_not_refresh_when_latest_is_publishable(settings) -> None:
    trigger = await start_service(settings, latest={"result": current_record()})

    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_does_not_refresh_while_a_run_is_active(settings) -> None:
    trigger = await start_service(
        settings,
        latest={"result": {"schema_version": "latest_strategy_record.v1"}},
        active={"id": "already-running", "status": "running"},
    )

    trigger.assert_not_awaited()

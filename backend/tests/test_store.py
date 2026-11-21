from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.db import ActiveRunConflict, StrategyStore, strategy_runs
from conftest import strategy_payload


def test_postgres_partial_unique_index_is_a_cross_process_mutex() -> None:
    index = next(
        item for item in strategy_runs.indexes if item.name == "uq_strategy_runs_single_active"
    )
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "CREATE UNIQUE INDEX" in ddl
    assert "(id IS NOT NULL)" in ddl
    assert "WHERE status IN ('queued', 'running')" in ddl


@pytest.mark.asyncio
async def test_store_enforces_one_active_run_and_recovers_restart(settings) -> None:
    store = StrategyStore(settings.database_url)
    await store.initialize()
    await store.create_run(
        run_id="00000000-0000-0000-0000-000000000001",
        request={"mode": "all_market_scan", "product": None, "requested_contract": None, "option_analysis_requested": True},
        runtime={"provider": "codex"},
    )
    with pytest.raises(ActiveRunConflict) as conflict:
        await store.create_run(
            run_id="00000000-0000-0000-0000-000000000002",
            request={"mode": "all_market_scan", "product": None, "requested_contract": None, "option_analysis_requested": True},
            runtime={"provider": "codex"},
        )
    assert conflict.value.active_run_id == "00000000-0000-0000-0000-000000000001"
    await store.close()

    restarted = StrategyStore(settings.database_url)
    interrupted = await restarted.initialize()
    assert interrupted == ["00000000-0000-0000-0000-000000000001"]
    row = await restarted.get_run(interrupted[0])
    assert row and row["status"] == "failed"
    assert row["public_error_code"] == "service_restarted"
    events = await restarted.get_events_after(interrupted[0], 0)
    assert events[-1]["stage"] == "recovery"
    await restarted.close()


@pytest.mark.asyncio
async def test_completed_result_becomes_latest(settings) -> None:
    store = StrategyStore(settings.database_url)
    await store.initialize()
    run_id = "00000000-0000-0000-0000-000000000003"
    await store.create_run(
        run_id=run_id,
        request={"mode": "all_market_scan", "product": None, "requested_contract": None, "option_analysis_requested": True},
        runtime={"provider": "codex"},
    )
    await store.mark_running(run_id)
    result = {"schema_version": "example", "strategy": strategy_payload().model_dump(mode="json")}
    await store.complete_run(run_id, result)
    latest = await store.get_latest_completed()
    assert latest and latest["id"] == run_id and latest["result"] == result
    await store.close()

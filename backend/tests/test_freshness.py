from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.freshness import resolve_latest_strategy
from app.models import CodexRuntimeMetadata, LatestStrategyRecord
from conftest import strategy_payload


BJT = ZoneInfo("Asia/Shanghai")


def record_for(
    *,
    as_of: datetime,
    reassess_by: datetime,
    bar_state: str = "session_partial",
    expected_daily_close_at: datetime | None = None,
) -> LatestStrategyRecord:
    payload = strategy_payload(
        bar_state=bar_state,
        entry_style="confirmed_breakout" if bar_state == "completed" else "anticipatory_breakout",
    ).model_dump()
    payload["as_of"] = as_of
    payload["reassess_by"] = reassess_by
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
            "expected_daily_close_at": expected_daily_close_at
            or as_of + timedelta(hours=12),
        }
    )
    return LatestStrategyRecord.model_validate(
        {
            "run_id": "freshness-test",
            "updated_at": as_of + timedelta(minutes=2),
            "source": CodexRuntimeMetadata(
                transport="cli_exec_json",
                auth_mode="chatgpt_managed",
                authenticated=True,
                model="configured_default",
                model_resolution="inherited",
            ),
            "strategy": payload,
        }
    )


def test_missing_and_invalid_records_fail_closed() -> None:
    now = datetime(2026, 9, 14, 10, 0, tzinfo=BJT)

    missing = resolve_latest_strategy(None, now=now)
    invalid = resolve_latest_strategy({"schema_version": "latest_strategy_record.v1"}, now=now)

    assert missing.record is None
    assert missing.freshness.state == "missing"
    assert missing.freshness.reason == "no_completed_strategy"
    assert invalid.record is None
    assert invalid.freshness.state == "invalid"
    assert invalid.freshness.reason == "stored_record_invalid"


def test_reassess_by_marks_manual_refresh_without_hiding_record() -> None:
    now = datetime(2026, 9, 14, 10, 0, tzinfo=BJT)
    record = record_for(
        as_of=now - timedelta(hours=2),
        reassess_by=now,
        bar_state="completed",
    )

    resolution = resolve_latest_strategy(record, now=now)

    assert resolution.record == record
    assert resolution.freshness.state == "stale"
    assert resolution.freshness.requires_manual_refresh is True
    assert resolution.freshness.reason == "reassess_by_elapsed"
    assert resolution.freshness.valid_until == now


def test_partial_bar_marks_manual_refresh_at_expected_daily_close() -> None:
    now = datetime(2026, 9, 14, 15, 0, tzinfo=BJT)
    daily_close = now - timedelta(minutes=1)
    record = record_for(
        as_of=now - timedelta(hours=3),
        reassess_by=now + timedelta(hours=3),
        expected_daily_close_at=daily_close,
    )

    resolution = resolve_latest_strategy(record, now=now)

    assert resolution.record == record
    assert resolution.freshness.state == "stale"
    assert resolution.freshness.requires_manual_refresh is True
    assert resolution.freshness.reason == "session_partial_daily_close_elapsed"
    assert resolution.freshness.valid_until == daily_close


def test_current_partial_bar_publishes_until_earliest_deadline() -> None:
    now = datetime(2026, 9, 14, 10, 0, tzinfo=BJT)
    daily_close = now + timedelta(hours=5)
    reassess_by = now + timedelta(hours=2)
    record = record_for(
        as_of=now - timedelta(minutes=30),
        reassess_by=reassess_by,
        expected_daily_close_at=daily_close,
    )

    resolution = resolve_latest_strategy(record, now=now)

    assert resolution.record == record
    assert resolution.freshness.state == "current"
    assert resolution.freshness.reason is None
    assert resolution.freshness.valid_until == reassess_by
    assert resolution.freshness.valid_until_reason == "reassess_by_elapsed"


def test_completed_bar_is_not_expired_by_its_historical_daily_close() -> None:
    now = datetime(2026, 9, 14, 18, 0, tzinfo=BJT)
    reassess_by = now + timedelta(hours=2)
    record = record_for(
        as_of=now - timedelta(hours=3),
        reassess_by=reassess_by,
        bar_state="completed",
        expected_daily_close_at=now - timedelta(hours=1),
    )

    resolution = resolve_latest_strategy(record, now=now)

    assert resolution.record == record
    assert resolution.freshness.state == "current"
    assert resolution.freshness.valid_until == reassess_by
    assert resolution.freshness.valid_until_reason == "reassess_by_elapsed"

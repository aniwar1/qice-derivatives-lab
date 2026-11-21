from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path

import pytest

from scripts import build_dashboard_snapshot, run_decision, session_aware_bar


BJT = session_aware_bar.BJT
OFFICIAL_SCHEDULE_URLS = {
    "SHFE": "https://www.shfe.com.cn/upload/20211021/1634804081608.pdf",
    "INE": "https://www.ine.cn/services/calenderandholidays/tradinghours/",
    "DCE": "https://www.dce.com.cn/dalianshangpin/resource/cms/2019/04/2019042612023697006.pdf",
    "CZCE": "https://www.czce.com.cn/cn/flfg/zcjywgz/pzxz/webinfo/2024/02/1708568084281851.htm",
    "GFEX": "https://www.gfex.com.cn/gfex/sytslqhhy/202307/9ad927b8ec4c458594e172fd1ada2a9b.shtml",
}


def make_bar(
    *,
    product: str,
    contract: str,
    trade_date: date,
    session_started_at: datetime,
    observed_through: datetime,
    bar_state: str = "session_partial",
    semantics: str = "anticipatory",
) -> dict:
    session_kind, intervals, required, expected_close = session_aware_bar._regular_intervals(
        product=product,
        trade_date=trade_date,
        session_started_at=session_started_at,
    )
    checkpoint = session_aware_bar._checkpoint_at(intervals, required)
    source = {
        "kind": "file",
        "locator": f"evidence/{contract}-{trade_date}.json",
        "retrieved_at": observed_through.isoformat(),
        "content_sha256": "a" * 64,
    }
    completed = bar_state == "completed"
    exchange = str(session_aware_bar._product_spec(product)["exchange"])
    calendar_evidence = None if completed else {
        "exchange": exchange,
        "trade_date": trade_date.isoformat(),
        "regular_session_confirmed": True,
        "url": OFFICIAL_SCHEDULE_URLS[exchange],
        "retrieved_at": observed_through.isoformat(),
        "locator": f"{trade_date.isoformat()} regular session calendar entry",
        "content_sha256": "c" * 64,
    }
    return {
        "schema_version": "session_aware_daily_bar.v1",
        "product": product,
        "analysis_contract": contract,
        "trade_date": trade_date.isoformat(),
        "bar_state": bar_state,
        "as_of": observed_through.isoformat(),
        "decision_semantics": semantics,
        "ohlcv": {
            "open": 2500,
            "high": 2520,
            "low": 2490,
            "close": 2510,
            "volume": 100,
            "open_interest": 200,
        },
        "coverage": {
            "session_kind": session_kind,
            "schedule_group": session_aware_bar.schedule_group_for_product(product),
            "session_started_at": session_started_at.isoformat(),
            "checkpoint_at": checkpoint.isoformat(),
            "observed_through": observed_through.isoformat(),
            "expected_daily_close_at": expected_close.isoformat(),
            "covered_trading_seconds": session_aware_bar._covered_seconds(
                intervals, observed_through
            ),
            "required_checkpoint_seconds": required,
        },
        "source": source,
        "calendar_evidence": calendar_evidence,
        "claim_boundary": {
            "completed_bar_claim_allowed": completed,
            "confirmed_daily_breakout_claim_eligible": completed,
        },
    }


@pytest.mark.parametrize(
    ("product", "contract", "group", "start", "checkpoint", "seconds"),
    [
        ("MA", "MA2701", "23:00", "2026-09-14T21:00:00+08:00", "2026-09-14T23:00:00+08:00", 7200),
        ("CU", "CU2701", "01:00", "2026-09-14T21:00:00+08:00", "2026-09-15T01:00:00+08:00", 14400),
    ],
)
def test_post_night_close_checkpoint_is_product_specific(
    product: str,
    contract: str,
    group: str,
    start: str,
    checkpoint: str,
    seconds: int,
) -> None:
    start_at = datetime.fromisoformat(start)
    checkpoint_at = datetime.fromisoformat(checkpoint)
    bar = make_bar(
        product=product,
        contract=contract,
        trade_date=date(2026, 9, 15),
        session_started_at=start_at,
        observed_through=checkpoint_at,
    )
    result = session_aware_bar.validate_session_aware_daily_bar(
        bar, now=checkpoint_at + timedelta(seconds=1)
    )
    assert result["bar_state"] == "session_partial"
    assert result["schedule_group"] == group
    assert result["checkpoint_at"] == checkpoint_at.isoformat()
    assert result["covered_trading_seconds"] == seconds
    assert result["confirmed_daily_breakout_claim_eligible"] is False


def test_partial_requires_exact_fresh_official_calendar_evidence() -> None:
    start = datetime(2026, 9, 14, 21, 0, tzinfo=BJT)
    night_close = datetime(2026, 9, 14, 23, 0, tzinfo=BJT)
    bar = make_bar(
        product="MA",
        contract="MA2701",
        trade_date=date(2026, 9, 15),
        session_started_at=start,
        observed_through=night_close,
    )

    missing = deepcopy(bar)
    missing["calendar_evidence"] = None
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="exact official"):
        session_aware_bar.validate_session_aware_daily_bar(
            missing, now=night_close + timedelta(seconds=1)
        )

    extra = deepcopy(bar)
    extra["calendar_evidence"]["note"] = "not allowed"
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="exact official"):
        session_aware_bar.validate_session_aware_daily_bar(
            extra, now=night_close + timedelta(seconds=1)
        )

    stale = deepcopy(bar)
    stale["calendar_evidence"]["retrieved_at"] = (
        night_close - timedelta(days=1, seconds=1)
    ).isoformat()
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="too old"):
        session_aware_bar.validate_session_aware_daily_bar(
            stale, now=night_close + timedelta(seconds=1)
        )

    unconfirmed = deepcopy(bar)
    unconfirmed["calendar_evidence"]["regular_session_confirmed"] = False
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="regular session"):
        session_aware_bar.validate_session_aware_daily_bar(
            unconfirmed, now=night_close + timedelta(seconds=1)
        )


def test_partial_calendar_evidence_must_match_product_exchange_and_domain() -> None:
    start = datetime(2026, 9, 14, 21, 0, tzinfo=BJT)
    night_close = datetime(2026, 9, 14, 23, 0, tzinfo=BJT)
    bar = make_bar(
        product="MA",
        contract="MA2701",
        trade_date=date(2026, 9, 15),
        session_started_at=start,
        observed_through=night_close,
    )

    wrong_exchange = deepcopy(bar)
    wrong_exchange["calendar_evidence"]["exchange"] = "SHFE"
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="exchange conflicts"):
        session_aware_bar.validate_session_aware_daily_bar(
            wrong_exchange, now=night_close + timedelta(seconds=1)
        )

    wrong_domain = deepcopy(bar)
    wrong_domain["calendar_evidence"]["url"] = OFFICIAL_SCHEDULE_URLS["SHFE"]
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="official HTTPS domain"):
        session_aware_bar.validate_session_aware_daily_bar(
            wrong_domain, now=night_close + timedelta(seconds=1)
        )

    secret_query = deepcopy(bar)
    secret_query["calendar_evidence"]["url"] = (
        "https://www.czce.com.cn/calendar?access_token=private"
    )
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="official HTTPS domain"):
        session_aware_bar.validate_session_aware_daily_bar(
            secret_query, now=night_close + timedelta(seconds=1)
        )


def test_before_night_close_is_not_decision_eligible() -> None:
    start = datetime(2026, 9, 14, 21, 0, tzinfo=BJT)
    before_close = datetime(2026, 9, 14, 22, 59, 59, tzinfo=BJT)
    bar = make_bar(
        product="MA",
        contract="MA2701",
        trade_date=date(2026, 9, 15),
        session_started_at=start,
        observed_through=before_close,
    )
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="after the night close"):
        session_aware_bar.validate_session_aware_daily_bar(
            bar, now=before_close + timedelta(seconds=1)
        )


def test_regular_trade_date_mapping_and_timezone_are_fail_closed() -> None:
    start = datetime(2026, 9, 14, 21, 0, tzinfo=BJT)
    night_close = datetime(2026, 9, 14, 23, 0, tzinfo=BJT)
    bar = make_bar(
        product="MA",
        contract="MA2701",
        trade_date=date(2026, 9, 15),
        session_started_at=start,
        observed_through=night_close,
    )
    wrong_trade_date = deepcopy(bar)
    wrong_trade_date["trade_date"] = "2026-09-16"
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="weekday calendar"):
        session_aware_bar.validate_session_aware_daily_bar(
            wrong_trade_date, now=night_close + timedelta(seconds=1)
        )

    with pytest.raises(session_aware_bar.SessionAwareBarError, match="validator clock"):
        session_aware_bar.validate_session_aware_daily_bar(
            bar, now=datetime(2026, 9, 14, 23, 0, 1)
        )


def test_no_night_product_cannot_use_partial_exception() -> None:
    start = datetime(2026, 9, 15, 9, 0, tzinfo=BJT)
    partial = datetime(2026, 9, 15, 11, 15, tzinfo=BJT)
    bar = make_bar(
        product="AP",
        contract="AP2701",
        trade_date=date(2026, 9, 15),
        session_started_at=start,
        observed_through=partial,
    )
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="without a night session"):
        session_aware_bar.validate_session_aware_daily_bar(
            bar, now=partial + timedelta(seconds=1)
        )


def test_partial_claim_and_future_coverage_fail_closed() -> None:
    start = datetime(2026, 9, 14, 21, 0, tzinfo=BJT)
    night_close = datetime(2026, 9, 14, 23, 0, tzinfo=BJT)
    bar = make_bar(
        product="MA",
        contract="MA2701",
        trade_date=date(2026, 9, 15),
        session_started_at=start,
        observed_through=night_close,
    )
    false_claim = deepcopy(bar)
    false_claim["decision_semantics"] = "confirmed_daily_breakout"
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="cannot claim"):
        session_aware_bar.validate_session_aware_daily_bar(
            false_claim, now=night_close + timedelta(seconds=1)
        )
    future = deepcopy(bar)
    future["as_of"] = (night_close + timedelta(minutes=2)).isoformat()
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="future-dated"):
        session_aware_bar.validate_session_aware_daily_bar(
            future, now=night_close + timedelta(minutes=1)
        )


def test_completed_no_night_bar_is_valid_and_confirmation_eligible() -> None:
    start = datetime(2026, 9, 15, 9, 0, tzinfo=BJT)
    close = datetime(2026, 9, 15, 15, 0, tzinfo=BJT)
    bar = make_bar(
        product="AP",
        contract="AP2701",
        trade_date=date(2026, 9, 15),
        session_started_at=start,
        observed_through=close,
        bar_state="completed",
        semantics="confirmed_daily_breakout",
    )
    result = session_aware_bar.validate_session_aware_daily_bar(
        bar, now=close + timedelta(seconds=1)
    )
    assert result["schedule_group"] == "NONE"
    assert result["confirmed_daily_breakout_claim_eligible"] is True

    calendar_tampered = deepcopy(bar)
    calendar_tampered["calendar_evidence"] = {
        "exchange": "CZCE",
        "trade_date": "2026-09-15",
        "regular_session_confirmed": True,
        "url": OFFICIAL_SCHEDULE_URLS["CZCE"],
        "retrieved_at": close.isoformat(),
        "locator": "2026-09-15 regular session calendar entry",
        "content_sha256": "c" * 64,
    }
    with pytest.raises(session_aware_bar.SessionAwareBarError, match="do not use"):
        session_aware_bar.validate_session_aware_daily_bar(
            calendar_tampered, now=close + timedelta(seconds=1)
        )


def test_session_bar_schema_declares_state_specific_calendar_evidence() -> None:
    schema = json.loads(
        Path("schemas/session_aware_daily_bar.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert "calendar_evidence" in schema["required"]
    calendar = schema["$defs"]["calendarEvidence"]
    assert calendar["additionalProperties"] is False
    assert set(calendar["required"]) == session_aware_bar.CALENDAR_EVIDENCE_FIELDS
    assert calendar["properties"]["regular_session_confirmed"] == {"const": True}
    partial_rule, completed_rule = schema["allOf"][:2]
    assert partial_rule["then"]["properties"]["calendar_evidence"] == {
        "$ref": "#/$defs/calendarEvidence"
    }
    assert completed_rule["then"]["properties"]["calendar_evidence"] == {
        "type": "null"
    }


def test_schedule_groups_cover_the_current_22_product_universe() -> None:
    policy = json.loads(
        Path("config/futures_main_report_policy.json").read_text(encoding="utf-8")
    )
    groups = policy["market_data_freshness"]["regular_session_schedule_bjt"][
        "night_end_groups"
    ]
    products = [symbol for values in groups.values() for symbol in values]
    assert len(products) == len(set(products)) == 22
    assert set(products) == set(session_aware_bar.runtime_universe.allowed_product_symbols())


def test_partial_bar_is_preserved_in_decision_run_and_dashboard() -> None:
    now = datetime(2026, 9, 14, 23, 5, tzinfo=BJT)
    run = run_decision.create_run(
        mode="single_analysis",
        run_id="partial-session-run",
        product="MA",
        requested_contract="MA2701",
        now=now,
    )
    bar = make_bar(
        product="MA",
        contract="MA2701",
        trade_date=date(2026, 9, 15),
        session_started_at=datetime(2026, 9, 14, 21, 0, tzinfo=BJT),
        observed_through=datetime(2026, 9, 14, 23, 0, tzinfo=BJT),
    )
    packet = {
        "schema_version": "evidence_packet.v1",
        "packet_id": "technical-partial",
        "run_id": run["run_id"],
        "domain": "technical_structure",
        "product": "MA",
        "analysis_contract": "MA2701",
        "observed_at": datetime(2026, 9, 14, 23, 1, tzinfo=BJT).isoformat(),
        "valid_until": datetime(2026, 9, 15, 15, 0, tzinfo=BJT).isoformat(),
        "status": {
            "availability": "partial",
            "freshness": "fresh",
            "integrity": "verified",
            "identity": "verified",
            "executability": "not_applicable",
        },
        "source_refs": [bar["source"]],
        "facts": {"session_daily_bar": bar},
        "known_gaps": ["session_partial_daily_bar_not_completed"],
        "does_not_select_action": True,
    }
    run = run_decision.attach_evidence(run, packet, now=now)
    snapshot = build_dashboard_snapshot.build_snapshot(run=run, now=now)
    rendered = snapshot["evidence"][0]["facts"]["session_daily_bar"]
    assert rendered["bar_state"] == "session_partial"
    assert rendered["as_of"] == bar["as_of"]
    assert rendered["coverage"]["checkpoint_at"].endswith("23:00:00+08:00")
    assert rendered["claim_boundary"]["confirmed_daily_breakout_claim_eligible"] is False

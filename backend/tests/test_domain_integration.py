from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.dashboard import DomainSnapshotProvider


def test_backend_delegates_partial_bar_truth_to_domain_validator() -> None:
    root = Path(__file__).resolve().parents[2]
    provider = DomainSnapshotProvider(root)
    bjt = ZoneInfo("Asia/Shanghai")
    checkpoint = datetime(2026, 9, 14, 23, 0, tzinfo=bjt)
    bar = {
        "schema_version": "session_aware_daily_bar.v1",
        "product": "MA",
        "analysis_contract": "MA2701",
        "trade_date": date(2026, 9, 15).isoformat(),
        "bar_state": "session_partial",
        "as_of": checkpoint.isoformat(),
        "decision_semantics": "anticipatory",
        "ohlcv": {
            "open": 2500,
            "high": 2520,
            "low": 2490,
            "close": 2510,
            "volume": 100,
            "open_interest": 200,
        },
        "coverage": {
            "session_kind": "night",
            "schedule_group": "23:00",
            "session_started_at": datetime(2026, 9, 14, 21, 0, tzinfo=bjt).isoformat(),
            "checkpoint_at": checkpoint.isoformat(),
            "observed_through": checkpoint.isoformat(),
            "expected_daily_close_at": datetime(2026, 9, 15, 15, 0, tzinfo=bjt).isoformat(),
            "covered_trading_seconds": 7200,
            "required_checkpoint_seconds": 7200,
        },
        "source": {
            "kind": "file",
            "locator": "evidence/MA2701-2026-09-15.json",
            "retrieved_at": checkpoint.isoformat(),
            "content_sha256": "a" * 64,
        },
        "calendar_evidence": {
            "exchange": "CZCE",
            "trade_date": "2026-09-15",
            "regular_session_confirmed": True,
            "url": "https://www.czce.com.cn/cn/flfg/zcjywgz/pzxz/webinfo/2024/02/1708568084281851.htm",
            "retrieved_at": checkpoint.isoformat(),
            "locator": "2026-09-15 regular session calendar entry",
            "content_sha256": "c" * 64,
        },
        "claim_boundary": {
            "completed_bar_claim_allowed": False,
            "confirmed_daily_breakout_claim_eligible": False,
        },
    }

    validation = provider.validate_session_bar(
        bar,
        expected_product="MA",
        expected_contract="MA2701",
        packet_observed_at=checkpoint,
        now=datetime(2026, 9, 14, 23, 5, tzinfo=bjt),
    )

    assert validation["valid"] is True
    assert validation["bar_state"] == "session_partial"
    assert validation["confirmed_daily_breakout_claim_eligible"] is False

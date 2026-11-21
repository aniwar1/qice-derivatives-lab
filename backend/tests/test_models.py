from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models import AgentStrategyPayload, StrategyRunRequest
from conftest import strategy_payload


def test_partial_night_bar_can_drive_anticipatory_decision() -> None:
    payload = strategy_payload(bar_state="session_partial", entry_style="anticipatory_breakout")
    assert payload.decision == "futures"
    assert payload.session_daily_bar is not None
    assert payload.session_daily_bar.bar_state == "session_partial"
    assert payload.session_daily_bar.claim_boundary.confirmed_daily_breakout_claim_eligible is False


def test_partial_bar_cannot_claim_confirmed_breakout() -> None:
    raw = strategy_payload().model_dump(mode="json")
    raw["entry_style"] = "confirmed_breakout"
    with pytest.raises(ValidationError, match="confirmed_breakout"):
        AgentStrategyPayload.model_validate(raw)


def test_partial_bar_requires_exact_fresh_official_calendar_evidence() -> None:
    missing = strategy_payload().model_dump(mode="json")
    missing["session_daily_bar"]["calendar_evidence"] = None
    with pytest.raises(ValidationError, match="requires exact official|calendar_evidence|requires.*calendar"):
        AgentStrategyPayload.model_validate(missing)

    extra = strategy_payload().model_dump(mode="json")
    extra["session_daily_bar"]["calendar_evidence"]["note"] = "not allowed"
    with pytest.raises(ValidationError, match="calendar_evidence.*note|extra_forbidden|Extra inputs"):
        AgentStrategyPayload.model_validate(extra)

    stale = strategy_payload().model_dump(mode="json")
    calendar = stale["session_daily_bar"]["calendar_evidence"]
    as_of = datetime.fromisoformat(stale["session_daily_bar"]["as_of"])
    calendar["retrieved_at"] = (as_of - timedelta(days=1, seconds=1)).isoformat()
    with pytest.raises(ValidationError, match="too old"):
        AgentStrategyPayload.model_validate(stale)

    unconfirmed = strategy_payload().model_dump(mode="json")
    unconfirmed["session_daily_bar"]["calendar_evidence"][
        "regular_session_confirmed"
    ] = False
    with pytest.raises(ValidationError, match="regular_session_confirmed"):
        AgentStrategyPayload.model_validate(unconfirmed)


def test_partial_calendar_must_match_product_exchange_and_official_domain() -> None:
    wrong_exchange = strategy_payload().model_dump(mode="json")
    calendar = wrong_exchange["session_daily_bar"]["calendar_evidence"]
    calendar["exchange"] = "SHFE"
    calendar["url"] = "https://www.shfe.com.cn/calendar/"
    with pytest.raises(ValidationError, match="exchange conflicts with product"):
        AgentStrategyPayload.model_validate(wrong_exchange)

    wrong_domain = strategy_payload().model_dump(mode="json")
    wrong_domain["session_daily_bar"]["calendar_evidence"]["url"] = (
        "https://example.com/calendar"
    )
    with pytest.raises(ValidationError, match="official HTTPS domain"):
        AgentStrategyPayload.model_validate(wrong_domain)

    secret_query = strategy_payload().model_dump(mode="json")
    secret_query["session_daily_bar"]["calendar_evidence"]["url"] = (
        "https://www.shfe.com.cn/calendar?token=private"
    )
    with pytest.raises(ValidationError, match="official HTTPS domain"):
        AgentStrategyPayload.model_validate(secret_query)


def test_completed_bar_requires_null_calendar_evidence() -> None:
    completed = strategy_payload(
        bar_state="completed", entry_style="confirmed_breakout"
    ).model_dump(mode="json")
    partial_calendar = strategy_payload().model_dump(mode="json")["session_daily_bar"][
        "calendar_evidence"
    ]
    completed["session_daily_bar"]["calendar_evidence"] = partial_calendar
    with pytest.raises(ValidationError, match="completed.*calendar"):
        AgentStrategyPayload.model_validate(completed)


def test_correlated_product_prefix_does_not_pass_by_string_prefix() -> None:
    with pytest.raises(ValidationError, match="conflicts with product"):
        StrategyRunRequest(
            mode="single_analysis",
            product="M",
            requested_contract="CU2701",
        )


def test_long_levels_must_have_ordered_invalidation_trigger_target() -> None:
    raw = strategy_payload().model_dump(mode="json")
    raw["market_levels"]["invalidation"] = 3600
    with pytest.raises(ValidationError, match="invalidation < trigger < target"):
        AgentStrategyPayload.model_validate(raw)


def test_scan_coverage_rejects_duplicate_or_implicit_products() -> None:
    raw = strategy_payload().model_dump(mode="json")
    raw["scan_coverage"]["products"][1]["product"] = "M"
    raw["scan_coverage"]["products"][1]["actual_main_contract"] = "M2701"
    with pytest.raises(ValidationError, match="duplicate products"):
        AgentStrategyPayload.model_validate(raw)


def test_source_url_cannot_persist_embedded_credentials() -> None:
    raw = strategy_payload().model_dump(mode="json")
    raw["technical_structure"]["sources"][0]["url"] = (
        "https://example.com/market?access_token=private"
    )
    with pytest.raises(ValidationError, match="cannot contain credentials"):
        AgentStrategyPayload.model_validate(raw)


def test_quality_review_is_required() -> None:
    raw = strategy_payload().model_dump(mode="json")
    del raw["quality_review"]
    with pytest.raises(ValidationError, match="quality_review"):
        AgentStrategyPayload.model_validate(raw)


def test_quality_review_requires_exactly_one_revision() -> None:
    raw = strategy_payload().model_dump(mode="json")
    raw["quality_review"]["critic_revisions"] = 0
    with pytest.raises(ValidationError, match="critic_revisions"):
        AgentStrategyPayload.model_validate(raw)


def test_quality_review_cannot_publish_an_unresolved_material_issue() -> None:
    raw = strategy_payload().model_dump(mode="json")
    raw["quality_review"]["unresolved_material_issue"] = "主力合约身份仍有冲突"
    with pytest.raises(ValidationError, match="unresolved_material_issue"):
        AgentStrategyPayload.model_validate(raw)


@pytest.mark.parametrize("field", ["strongest_challenge", "resolution"])
def test_quality_review_text_must_be_nonblank_and_bounded(field: str) -> None:
    blank = strategy_payload().model_dump(mode="json")
    blank["quality_review"][field] = "   "
    with pytest.raises(ValidationError, match=field):
        AgentStrategyPayload.model_validate(blank)

    too_long = strategy_payload().model_dump(mode="json")
    too_long["quality_review"][field] = "反" * 1001
    with pytest.raises(ValidationError, match=field):
        AgentStrategyPayload.model_validate(too_long)


def test_wait_may_retain_unknown_product_and_unobservable_bar() -> None:
    raw = strategy_payload(decision="wait", product=None).model_dump(mode="json")
    raw["session_daily_bar"] = None
    raw["market_levels"] = {
        "current_price": None,
        "quote_at": None,
        "strongest_support": None,
        "strongest_resistance": None,
        "trigger": None,
        "invalidation": None,
        "target": None,
        "tick_size": None,
    }
    payload = AgentStrategyPayload.model_validate(raw)
    assert payload.decision == "wait"

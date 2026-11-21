from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta

import pytest

from scripts import run_decision, update_latest_strategy
from tests.test_session_aware_bar import make_bar


BJT = run_decision.BJT


def _packet(run: dict, domain: str, now: datetime, *, bar: dict | None = None) -> dict:
    observed = now - timedelta(seconds=1)
    source = (
        bar["source"]
        if bar is not None
        else {
            "kind": "file",
            "locator": f"evidence/{domain}.json",
            "retrieved_at": (observed - timedelta(seconds=1)).isoformat(),
            "content_sha256": "b" * 64,
        }
    )
    partial = bar is not None and bar["bar_state"] == "session_partial"
    return {
        "schema_version": "evidence_packet.v1",
        "packet_id": "technical-session" if bar is not None else f"packet-{domain}",
        "run_id": run["run_id"],
        "domain": domain,
        "product": run["request"]["product"],
        "analysis_contract": run["request"]["requested_contract"],
        "observed_at": observed.isoformat(),
        "valid_until": (now + timedelta(hours=1)).isoformat(),
        "status": {
            "availability": "partial" if partial else "complete",
            "freshness": "fresh",
            "integrity": "verified",
            "identity": "verified",
            "executability": "executable" if domain == "market_contract" else "not_applicable",
        },
        "source_refs": [source],
        "facts": {"session_daily_bar": bar} if bar is not None else {"summary": domain},
        "known_gaps": ["session_partial_daily_bar_not_completed"] if partial else [],
        "does_not_select_action": True,
    }


def _thesis(now: datetime, bar: dict, *, path: str | None = None) -> dict:
    return {
        "schema_version": "underlying_thesis.v1",
        "product": "MA",
        "analysis_contract": "MA2701",
        "directional_view": "long",
        "path": path or (
            "confirmed_breakout"
            if bar["decision_semantics"] == "confirmed_daily_breakout"
            else "anticipatory_breakout"
        ),
        "trigger": "条件触发后生效",
        "target": "唯一现实压力目标",
        "futures_structure_invalidation": "完成日线收于结构失效位下方",
        "latest_realization_time": (now + timedelta(days=2)).isoformat(),
        "why_now": "当前会话信息提高了突破路径的条件概率。",
        "why_not_opposite": "反向路径不能同时解释结构和驱动。",
        "why_not_wait": "等待会放弃部分凸性，但仍保留条件执行。",
        "strongest_counterevidence": "日盘尚可能逆转夜盘变化。",
        "falsifiers": ["日盘回到失效位", "驱动证据反转"],
        "selected_by_agent": True,
        "daily_bar_basis": {
            "evidence_packet_id": "technical-session",
            "bar_state": bar["bar_state"],
            "as_of": bar["as_of"],
            "decision_semantics": bar["decision_semantics"],
        },
    }


def _finalized_partial_run() -> tuple[dict, datetime]:
    now = datetime(2026, 9, 14, 23, 5, tzinfo=BJT)
    bar = make_bar(
        product="MA",
        contract="MA2701",
        trade_date=date(2026, 9, 15),
        session_started_at=datetime(2026, 9, 14, 21, 0, tzinfo=BJT),
        observed_through=datetime(2026, 9, 14, 23, 0, tzinfo=BJT),
    )
    run = run_decision.create_run(
        mode="single_analysis",
        run_id="latest-partial",
        product="MA",
        requested_contract="MA2701",
        now=now,
    )
    for domain in (
        "market_contract",
        "technical_structure",
        "fundamentals_news",
        "member_flow",
    ):
        run = run_decision.attach_evidence(
            run,
            _packet(
                run,
                domain,
                now,
                bar=bar if domain == "technical_structure" else None,
            ),
            now=now,
        )
    run = run_decision.attach_thesis(run, _thesis(now, bar), now=now)
    run = run_decision.record_critic(run, unresolved_material_issue=None, now=now)
    return run_decision.finalize_run(run, now=now), now


def test_partial_bar_supports_anticipatory_thesis_but_not_confirmed_claim() -> None:
    run, now = _finalized_partial_run()
    assert run["phase"] == "finalized"
    strategy = update_latest_strategy.build_latest_strategy(
        run, now=now + timedelta(seconds=1)
    )
    assert strategy["technical_basis"]["bar_state"] == "session_partial"
    assert strategy["technical_basis"]["decision_semantics"] == "anticipatory"
    assert strategy["technical_basis"]["confirmed_daily_breakout_claim_eligible"] is False
    assert strategy["boundaries"]["order_placement_authorized"] is False
    assert strategy["valid_until"] == min(
        packet["valid_until"]
        for packet in run["evidence_packets"]
        if packet["valid_until"] is not None
    )
    update_latest_strategy.validate_latest_strategy(
        strategy, now=now + timedelta(seconds=1)
    )

    with pytest.raises(update_latest_strategy.LatestStrategyError, match="expired"):
        update_latest_strategy.validate_latest_strategy(
            strategy, now=datetime.fromisoformat(strategy["valid_until"])
        )


def test_partial_bar_cannot_attach_confirmed_breakout_thesis() -> None:
    run, now = _finalized_partial_run()
    reopened = deepcopy(run)
    reopened["phase"] = "ready_for_agent"
    reopened["underlying_thesis"] = None
    reopened["quality_review"] = {
        "critic_revisions": 0,
        "unresolved_material_issue": None,
    }
    bar = next(
        packet["facts"]["session_daily_bar"]
        for packet in reopened["evidence_packets"]
        if packet["domain"] == "technical_structure"
    )
    thesis = _thesis(now, bar, path="confirmed_breakout")
    with pytest.raises(run_decision.DecisionRunError, match="requires a completed daily bar"):
        run_decision.attach_thesis(reopened, thesis, now=now)


def test_partial_evidence_requires_explicit_thesis_basis() -> None:
    run, now = _finalized_partial_run()
    reopened = deepcopy(run)
    reopened["phase"] = "ready_for_agent"
    reopened["underlying_thesis"] = None
    reopened["quality_review"] = {
        "critic_revisions": 0,
        "unresolved_material_issue": None,
    }
    bar = next(
        packet["facts"]["session_daily_bar"]
        for packet in reopened["evidence_packets"]
        if packet["domain"] == "technical_structure"
    )
    thesis = _thesis(now, bar)
    thesis.pop("daily_bar_basis")
    with pytest.raises(run_decision.DecisionRunError, match="must disclose daily_bar_basis"):
        run_decision.attach_thesis(reopened, thesis, now=now)


def test_latest_partial_strategy_expires_at_daily_close() -> None:
    run, _ = _finalized_partial_run()
    at_close = datetime(2026, 9, 15, 15, 0, tzinfo=BJT)
    with pytest.raises(update_latest_strategy.LatestStrategyError, match="expired at the daily close"):
        update_latest_strategy.build_latest_strategy(run, now=at_close)


def test_latest_strategy_hash_and_boundaries_fail_closed() -> None:
    run, now = _finalized_partial_run()
    strategy = update_latest_strategy.build_latest_strategy(
        run, now=now + timedelta(seconds=1)
    )
    tampered = deepcopy(strategy)
    tampered["boundaries"]["order_placement_authorized"] = True
    with pytest.raises(update_latest_strategy.LatestStrategyError, match="boundaries"):
        update_latest_strategy.validate_latest_strategy(
            tampered, now=now + timedelta(seconds=1)
        )
    tampered_hash = deepcopy(strategy)
    tampered_hash["technical_basis"]["decision_semantics"] = "conditional"
    with pytest.raises(update_latest_strategy.LatestStrategyError, match="content hash mismatch"):
        update_latest_strategy.validate_latest_strategy(
            tampered_hash, now=now + timedelta(seconds=1)
        )


def test_legacy_implicit_daily_bar_cannot_publish_latest_strategy() -> None:
    run, now = _finalized_partial_run()
    run["underlying_thesis"].pop("daily_bar_basis")
    technical = next(
        packet
        for packet in run["evidence_packets"]
        if packet["domain"] == "technical_structure"
    )
    technical["facts"] = {"summary": "legacy completed-history structure"}
    technical["status"]["availability"] = "complete"
    technical["known_gaps"] = []
    with pytest.raises(update_latest_strategy.LatestStrategyError, match="explicit daily_bar_basis"):
        update_latest_strategy.build_latest_strategy(
            run, now=now + timedelta(seconds=1)
        )

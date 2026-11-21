from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from scripts import run_decision


BJT = run_decision.BJT
NOW = datetime(2026, 9, 13, 20, 0, tzinfo=BJT)


def _run(mode: str = "single_analysis") -> dict:
    return run_decision.create_run(
        mode=mode,
        run_id=f"test-{mode}",
        product="MA" if mode not in {"education", "universe_maintenance", "all_market_scan", "trade_review"} else None,
        requested_contract="MA2701" if mode not in {"education", "universe_maintenance", "all_market_scan", "trade_review"} else None,
        now=NOW,
    )


def _packet(run: dict, domain: str, *, packet_id: str | None = None) -> dict:
    observed = NOW - timedelta(minutes=1)
    product = run["request"]["product"]
    contract = run["request"]["requested_contract"]
    if run["task_mode"] == "trade_review":
        product = None
        contract = None
    return {
        "schema_version": "evidence_packet.v1",
        "packet_id": packet_id or f"packet-{domain}",
        "run_id": run["run_id"],
        "domain": domain,
        "product": product,
        "analysis_contract": contract,
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(hours=1)).isoformat(),
        "status": {
            "availability": "complete",
            "freshness": "fresh",
            "integrity": "verified",
            "identity": "verified",
            "executability": "executable" if domain in {"market_contract", "option_chain"} else "not_applicable",
        },
        "source_refs": [
            {
                "kind": "file",
                "locator": f"evidence/{domain}.json",
                "retrieved_at": (observed - timedelta(seconds=1)).isoformat(),
                "content_sha256": "a" * 64,
            }
        ],
        "facts": {"summary": f"verified {domain}"},
        "known_gaps": [],
        "does_not_select_action": True,
    }


def _thesis() -> dict:
    return {
        "schema_version": "underlying_thesis.v1",
        "product": "MA",
        "analysis_contract": "MA2701",
        "directional_view": "long",
        "path": "anticipatory_breakout",
        "trigger": "完成K线突破已定义压力后生效",
        "target": "到达报告内唯一现实压力目标",
        "futures_structure_invalidation": "完成K线收于结构失效位下方",
        "latest_realization_time": (NOW + timedelta(days=7)).isoformat(),
        "why_now": "驱动窗口和结构蓄势同时存在。",
        "why_not_opposite": "反向假设不能解释持仓与结构组合。",
        "why_not_wait": "等待会放弃突破前凸性，但并非自动入场。",
        "strongest_counterevidence": "需求证据仍可能转弱。",
        "falsifiers": ["库存拐点与主假设相反", "完成K线触发结构失效"],
        "selected_by_agent": True,
    }


def test_router_freezes_scope_and_never_authorizes_orders() -> None:
    run = _run("option_entry")
    assert run["scope"]["product_count"] == 22
    assert run["request"]["option_analysis_requested"] is True
    assert run["permissions"] == {
        "order_placement_authorized": False,
        "short_option_open_allowed": False,
    }
    option_node = next(item for item in run["nodes"] if item["id"] == "option_expression")
    assert option_node["status"] == "waiting"


def test_router_rejects_out_of_scope_product_before_work() -> None:
    with pytest.raises(ValueError, match="允许范围"):
        run_decision.create_run(mode="single_analysis", run_id="bad", product="SS", now=NOW)


def test_evidence_packets_complete_required_domains_without_voting() -> None:
    run = _run()
    required = [
        "market_contract",
        "technical_structure",
        "fundamentals_news",
        "member_flow",
    ]
    for domain in required:
        run = run_decision.attach_evidence(run, _packet(run, domain), now=NOW)
    assert run["phase"] == "ready_for_agent"
    assert all(item["does_not_select_action"] for item in run["evidence_packets"])
    validation = run_decision.validate_run(run, now=NOW)
    assert validation["valid"] is True
    assert validation["ready_for_agent"] is True
    assert validation["machine_selected_action"] is False


def test_complete_evidence_requires_source_and_rejects_future_data() -> None:
    run = _run()
    missing_source = _packet(run, "market_contract")
    missing_source["source_refs"] = []
    with pytest.raises(run_decision.DecisionRunError, match="source reference"):
        run_decision.validate_evidence_packet(missing_source, run, now=NOW)
    future = _packet(run, "market_contract")
    future["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(run_decision.DecisionRunError, match="future-dated"):
        run_decision.validate_evidence_packet(future, run, now=NOW)


def test_agent_thesis_is_separate_from_machine_evidence_and_can_finalize() -> None:
    run = _run()
    for domain in ("market_contract", "technical_structure", "fundamentals_news", "member_flow"):
        run = run_decision.attach_evidence(run, _packet(run, domain), now=NOW)
    run = run_decision.attach_thesis(run, _thesis(), now=NOW)
    assert run["underlying_thesis"]["selected_by_agent"] is True
    assert "action" not in run["underlying_thesis"]
    run = run_decision.record_critic(run, unresolved_material_issue=None, now=NOW)
    finalized = run_decision.finalize_run(run, now=NOW)
    assert finalized["phase"] == "finalized"
    assert finalized["permissions"]["order_placement_authorized"] is False


def test_critic_is_bounded_to_one_pass() -> None:
    run = _run()
    for domain in ("market_contract", "technical_structure", "fundamentals_news", "member_flow"):
        run = run_decision.attach_evidence(run, _packet(run, domain), now=NOW)
    run = run_decision.attach_thesis(run, _thesis(), now=NOW)
    run = run_decision.record_critic(run, unresolved_material_issue=None, now=NOW)
    with pytest.raises(run_decision.DecisionRunError, match="limit"):
        run_decision.record_critic(run, unresolved_material_issue=None, now=NOW)


def test_one_critic_issue_allows_one_thesis_revision_then_finalize() -> None:
    run = _run()
    for domain in ("market_contract", "technical_structure", "fundamentals_news", "member_flow"):
        run = run_decision.attach_evidence(run, _packet(run, domain), now=NOW)
    run = run_decision.attach_thesis(run, _thesis(), now=NOW)
    run = run_decision.record_critic(
        run, unresolved_material_issue="兑现窗口与事件时点尚未闭合", now=NOW
    )
    revised = _thesis()
    revised["why_now"] = "修订后已把事件可得时点与兑现窗口闭合。"
    run = run_decision.attach_thesis(run, revised, now=NOW)
    assert run["quality_review"] == {
        "critic_revisions": 1,
        "unresolved_material_issue": None,
    }
    assert run_decision.finalize_run(run, now=NOW)["phase"] == "finalized"


def test_option_attachment_uses_validator_without_delegating_direction(monkeypatch, tmp_path) -> None:
    from scripts import validate_option_decision

    run = _run("option_entry")
    for domain in (
        "market_contract", "technical_structure", "fundamentals_news", "member_flow", "option_chain"
    ):
        run = run_decision.attach_evidence(run, _packet(run, domain), now=NOW)
    run = run_decision.attach_thesis(run, _thesis(), now=NOW)

    def validated(_decision, *, artifact_root):
        assert artifact_root == tmp_path
        return {
            "valid": True,
            "analysis_contract": "MA2701",
            "underlying_action": "long",
            "capital_action": "buy_to_open",
            "option_route": "long_call",
            "validator_selected_direction": False,
            "validation_is_trade_permission": False,
            "order_authorized": False,
        }

    monkeypatch.setattr(validate_option_decision, "validate_option_decision", validated)
    run = run_decision.attach_option_decision(
        run, {"schema_version": "option_decision.v1"}, artifact_root=tmp_path, now=NOW
    )
    assert run["option_expression"]["schema_version"] == "option_decision.v1"
    run = run_decision.record_critic(run, unresolved_material_issue=None, now=NOW)
    assert run_decision.finalize_run(run, now=NOW)["phase"] == "finalized"


def test_current_scope_hash_drift_fails_closed() -> None:
    run = _run()
    run["scope"]["universe_sha256"] = "0" * 64
    result = run_decision.validate_run(run, now=NOW)
    assert result["valid"] is False
    assert "scope" in result["errors"][0]


def test_option_mode_cannot_finalize_without_option_expression() -> None:
    run = _run("option_entry")
    for domain in (
        "market_contract", "technical_structure", "fundamentals_news", "member_flow", "option_chain"
    ):
        run = run_decision.attach_evidence(run, _packet(run, domain), now=NOW)
    run = run_decision.attach_thesis(run, _thesis(), now=NOW)
    with pytest.raises(run_decision.DecisionRunError, match="option expression"):
        run_decision.finalize_run(run, now=NOW)


def test_lifecycle_is_orderless_and_requires_position_evidence_before_entered() -> None:
    run = _run("option_entry")
    run = run_decision.advance_lifecycle(run, state="screened", now=NOW)
    run = run_decision.advance_lifecycle(run, state="shortlisted", now=NOW)
    run = run_decision.advance_lifecycle(run, state="evidence_ready", now=NOW)
    run = run_decision.advance_lifecycle(run, state="armed", now=NOW)
    with pytest.raises(run_decision.DecisionRunError, match="account_position"):
        run_decision.advance_lifecycle(run, state="entered", now=NOW)
    position = _packet(run, "account_position")
    run = run_decision.attach_evidence(run, position, now=NOW)
    entered = run_decision.advance_lifecycle(run, state="entered", now=NOW)
    assert entered["lifecycle_state"] == "entered"
    assert entered["permissions"]["order_placement_authorized"] is False

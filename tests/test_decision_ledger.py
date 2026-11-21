from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import decision_ledger


BJT = ZoneInfo("Asia/Shanghai")
PROFIT_OBJECTIVE = "long_term_verifiable_net_profit_after_costs"
REAL_REPORT_REPLAY = decision_ledger._replay_futures_report


@pytest.fixture(autouse=True)
def _stub_report_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most ledger tests isolate ledger semantics from the heavy report fixture."""

    def replay(report: dict, report_policy: dict, **_: object) -> dict:
        judgment = report["agent_judgment"]
        return {
            "action": report["action"],
            "analysis_contract": report["target"]["analysis_contract"],
            "directional_view": judgment["directional_view"],
            "expected_net_value": judgment["expected_net_value"],
        }

    monkeypatch.setattr(decision_ledger, "_replay_futures_report", replay)


def _at(base: datetime, *, hours: int = 0, minutes: int = 0) -> str:
    return (base + timedelta(hours=hours, minutes=minutes)).isoformat(timespec="seconds")


def _write_artifact(root: Path, name: str, value: dict) -> dict:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path = root / name
    path.write_bytes(raw)
    return {
        "path": name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": value["schema_version"],
    }


def _bind_report(root: Path, payload: dict) -> dict:
    low = payload["confidence_range"]["low"]
    high = payload["confidence_range"]["high"]
    if payload["action"] in {"long", "short"}:
        ledger_plan = payload["trade_plan"]
        plan_direction = payload["action"]
        point_plan = {
            "direction": plan_direction,
            "trigger": {
                "text": ledger_plan["trigger"],
                "price": ledger_plan["entry_price"],
            },
            "entry_zone": {
                "lower": ledger_plan["entry_price"],
                "upper": ledger_plan["entry_price"],
            },
            "invalidation": {"price": ledger_plan["invalidation_price"]},
            "target": {"price": ledger_plan["target_price"]},
        }
    elif payload["wait_counterfactual"]["status"] == "defined":
        ledger_plan = payload["wait_counterfactual"]
        plan_direction = ledger_plan["direction"]
        point_plan = {
            "direction": plan_direction,
            "trigger": {
                "text": ledger_plan["trigger"],
                "price": ledger_plan["trigger_price"],
            },
            "entry_zone": {
                "lower": ledger_plan["trigger_price"],
                "upper": ledger_plan["trigger_price"],
            },
            "invalidation": {"price": ledger_plan["invalidation_price"]},
            "target": {"price": ledger_plan["target_price"]},
        }
    else:
        point_plan = None
    judgment = {
        "objective": payload["objective"],
        "directional_view": payload["directional_view"],
        "domains": {
            "technical_structure": {"confidence": {"value": high}},
            "fundamentals_and_news": {"confidence": {"value": low}},
            "member_flow": {"confidence": {"value": high}},
        },
        "synthesis_thesis": payload["main_hypothesis"],
        "expected_net_value": payload["expected_net_value"],
        "coverage_uncertainty": {
            "known_gaps": ["synthetic ledger fixture gap"]
        },
        "costs_and_slippage_considered": "synthetic frozen cost evidence",
        "final_action": payload["action"],
    }
    if payload["action"] == "wait":
        judgment["reassess_by"] = payload["reassess_by"]
    report = {
        "schema_version": "futures_main_report.v3",
        "observed_at": payload["observed_at"],
        "action": payload["action"],
        "target": {"analysis_contract": payload["analysis_contract"]},
        "point_plan": point_plan,
        "agent_judgment": judgment,
    }
    payload["agent_judgment_sha256"] = hashlib.sha256(
        json.dumps(
            judgment,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload["report_evidence"] = _write_artifact(
        root,
        f"{payload['decision_id']}.report.json",
        report,
    )
    payload["report_policy_evidence"] = _write_artifact(
        root,
        f"{payload['decision_id']}.report-policy.json",
        {"schema_version": "futures_main_report_policy.v3"},
    )
    return payload


def _trade_decision(base: datetime, *, decision_id: str = "M2609-actual-1") -> dict:
    return {
        "decision_id": decision_id,
        "observed_at": _at(base, minutes=-1),
        "report_evidence": None,
        "report_policy_evidence": None,
        "analysis_contract": "M2609",
        "main_hypothesis": "需求改善经由增仓突破形成两日延续，但跌回失效位即证伪。",
        "objective": PROFIT_OBJECTIVE,
        "expected_net_value": "positive",
        "agent_judgment_sha256": "0" * 64,
        "directional_view": "long",
        "action": "long",
        "confidence_range": {"low": "0.55", "high": "0.68"},
        "cost_assumptions": {
            "complete": True,
            "commission_cny": "5",
            "slippage_cny": "3",
            "roll_cny": "0",
            "basis": "冻结时可得费率表与一档盘口的保守估计",
            "missing_fields": [],
            "reason": None,
        },
        "trade_plan": {
            "trigger": "完成K线站上100后，下一交易时段100限价一次入场",
            "entry_price": "100",
            "invalidation_price": "98",
            "target_price": "106",
            "quantity": "2",
            "contract_multiplier": "10",
            "latest_entry_at": _at(base, hours=2),
        },
        "wait_counterfactual": None,
        "reassess_by": _at(base, hours=5),
    }


def _actual_outcome(decision_event: dict, base: datetime, root: Path) -> dict:
    outcome = {
        "decision_id": decision_event["payload"]["decision_id"],
        "decision_hash": decision_event["event_hash"],
        "outcome_observed_at": _at(base, hours=4),
        "result_kind": "actual_trade",
        "entry": {
            "at": _at(base, hours=1),
            "price": "100",
            "quantity": "2",
            "side": "buy",
        },
        "exit": {
            "at": _at(base, hours=3),
            "price": "105",
            "quantity": "2",
            "side": "sell",
        },
        "contract_multiplier": "10",
        "costs_complete": True,
        "commission_cny": "5",
        "slippage_cny": "3",
        "slippage_accounting": "embedded_in_actual_fill_prices",
        "roll": {"occurred": False, "cost_cny": "0", "details": []},
        "gross_pnl_cny": "100",
        "net_pnl_cny": "95",
        "mfe_cny": "120",
        "mae_cny": "20",
        "execution_evidence": None,
    }
    evidence_core = {
        **{key: value for key, value in outcome.items() if key != "execution_evidence"},
        "analysis_contract": decision_event["payload"]["analysis_contract"],
    }
    outcome["execution_evidence"] = _write_artifact(
        root,
        f"{outcome['decision_id']}.execution.json",
        {"schema_version": "decision_ledger_execution_evidence.v1", "outcome": evidence_core},
    )
    return outcome


def _wait_decision(base: datetime, *, decision_id: str = "M2609-wait-1") -> dict:
    return {
        "decision_id": decision_id,
        "observed_at": _at(base, minutes=-1),
        "report_evidence": None,
        "report_policy_evidence": None,
        "analysis_contract": "M2609",
        "main_hypothesis": "驱动尚未被价格确认，等待一次明确触发的机会成本低于误判成本。",
        "objective": PROFIT_OBJECTIVE,
        "expected_net_value": "uncertain",
        "agent_judgment_sha256": "0" * 64,
        "directional_view": "long",
        "action": "wait",
        "confidence_range": {"low": "0.48", "high": "0.62"},
        "cost_assumptions": {
            "complete": True,
            "commission_cny": "2",
            "slippage_cny": "1",
            "roll_cny": "0",
            "basis": "冻结时可得费率表与一档盘口的保守估计",
            "missing_fields": [],
            "reason": None,
        },
        "trade_plan": None,
        "wait_counterfactual": {
            "status": "defined",
            "direction": "long",
            "trigger": "完成bar收盘不低于100后，下一bar开盘建立反事实仓位",
            "trigger_price": "100",
            "entry_rule": "next_bar_open_after_close_trigger",
            "invalidation_price": "98",
            "target_price": "106",
            "quantity": "1",
            "contract_multiplier": "10",
            "evaluation_start_at": _at(base),
            "evaluation_end_at": _at(base, hours=2),
            "exit_rule": "first_touch_invalidation_or_target_else_evaluation_end",
            "bar_interval": "30m",
        },
        "reassess_by": _at(base, hours=3),
    }


def _wait_outcome(decision_event: dict, base: datetime, root: Path) -> dict:
    market_evidence = _write_artifact(
        root,
        f"{decision_event['payload']['decision_id']}.wait-bars.json",
        {
            "schema_version": "decision_ledger_wait_bars_evidence.v1",
            "decision_id": decision_event["payload"]["decision_id"],
            "analysis_contract": decision_event["payload"]["analysis_contract"],
            "bar_interval": "30m",
            "bars": [
                {
                    "start_at": _at(base, minutes=0),
                    "end_at": _at(base, minutes=30),
                    "open": "99",
                    "high": "102",
                    "low": "99",
                    "close": "101",
                },
                {
                    "start_at": _at(base, minutes=30),
                    "end_at": _at(base, hours=1),
                    "open": "100",
                    "high": "102",
                    "low": "99",
                    "close": "101",
                },
                {
                    "start_at": _at(base, hours=1),
                    "end_at": _at(base, hours=1, minutes=30),
                    "open": "101",
                    "high": "104",
                    "low": "100",
                    "close": "102",
                },
                {
                    "start_at": _at(base, hours=1, minutes=30),
                    "end_at": _at(base, hours=2),
                    "open": "102",
                    "high": "104",
                    "low": "101",
                    "close": "103",
                },
            ],
        },
    )
    return {
        "decision_id": decision_event["payload"]["decision_id"],
        "decision_hash": decision_event["event_hash"],
        "outcome_observed_at": _at(base, hours=3),
        "result_kind": "wait_counterfactual",
        "triggered": True,
        "entry": {
            "at": _at(base, minutes=30),
            "price": "100",
            "quantity": "1",
            "side": "buy",
        },
        "evaluation_exit": {
            "at": _at(base, hours=2),
            "price": "103",
            "reason": "evaluation_end",
        },
        "commission_cny": "2",
        "slippage_cny": "1",
        "roll": {"occurred": False, "cost_cny": "0", "details": []},
        "gross_pnl_cny": "30",
        "net_pnl_cny": "27",
        "mfe_cny": "40",
        "mae_cny": "10",
        "market_evidence": market_evidence,
    }


def _unavailable_wait_outcome(decision_event: dict, base: datetime, root: Path) -> dict:
    outcome = {
        "decision_id": decision_event["payload"]["decision_id"],
        "decision_hash": decision_event["event_hash"],
        "outcome_observed_at": _at(base, hours=3),
        "result_kind": "wait_unscorable",
        "reason": "复评时仍缺完成结构，不能计算成本后反事实净盈亏",
        "missing_evidence": ["completed_daily_structure", "account_costs"],
        "market_evidence": None,
    }
    evidence_core = {
        **{key: value for key, value in outcome.items() if key != "market_evidence"},
        "analysis_contract": decision_event["payload"]["analysis_contract"],
    }
    outcome["market_evidence"] = _write_artifact(
        root,
        f"{outcome['decision_id']}.unscorable.json",
        {"schema_version": "decision_ledger_unscorable_evidence.v1", "outcome": evidence_core},
    )
    return outcome


def _trade_not_entered_outcome(
    decision_event: dict,
    observed_at: str,
    root: Path,
) -> dict:
    outcome = {
        "decision_id": decision_event["payload"]["decision_id"],
        "decision_hash": decision_event["event_hash"],
        "outcome_observed_at": observed_at,
        "result_kind": "trade_not_entered",
        "reason": "冻结的最迟入场窗口结束，未产生任何成交",
        "no_fill_evidence": None,
    }
    evidence_core = {
        **{key: value for key, value in outcome.items() if key != "no_fill_evidence"},
        "analysis_contract": decision_event["payload"]["analysis_contract"],
    }
    outcome["no_fill_evidence"] = _write_artifact(
        root,
        f"{outcome['decision_id']}.no-fill.json",
        {
            "schema_version": "decision_ledger_no_fill_evidence.v1",
            "outcome": evidence_core,
            "order_status": "not_filled",
            "entry_fills": [],
        },
    )
    return outcome


def _set_clock(monkeypatch: pytest.MonkeyPatch, value: datetime) -> None:
    monkeypatch.setattr(decision_ledger, "_current_time", lambda: value)


def test_forward_actual_arithmetic_and_wait_are_reported_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "forward.jsonl"
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)

    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)
    actual_decision = decision_ledger.append_decision(
        ledger, _bind_report(tmp_path, _trade_decision(base))
    )
    _set_clock(monkeypatch, base + timedelta(hours=5))
    decision_ledger.append_outcome(ledger, _actual_outcome(actual_decision, base, tmp_path))

    wait_base = base + timedelta(hours=6)
    _set_clock(monkeypatch, wait_base)
    wait_decision = decision_ledger.append_decision(
        ledger, _bind_report(tmp_path, _wait_decision(wait_base))
    )
    _set_clock(monkeypatch, wait_base + timedelta(hours=4))
    decision_ledger.append_outcome(ledger, _wait_outcome(wait_decision, wait_base, tmp_path))

    validation = decision_ledger.validate_ledger(
        ledger, now=wait_base + timedelta(hours=4)
    )
    assert validation["valid"] is True
    assert validation["decision_count"] == 2
    assert validation["outcome_count"] == 2
    assert validation["profit_verified"] is False
    assert validation["validation_status"] == "valid_local_accounting_artifacts_replayed"
    assert "not_broker_exchange_or_worm_authenticated" in validation["evidence_assurance"]
    assert "privileged rewrite" in validation["head_assurance"]

    summary = decision_ledger.summarize_ledger(ledger)
    assert summary["validation_status"] == "insufficient_forward_accounting_data"
    assert summary["meets_minimum_accounting_data_for_external_review"] is False
    assert summary["observation_span_days"] == 0
    assert summary["profit_verified"] is False
    assert summary["actual"]["kind"] == "realized_actual_trades"
    assert summary["actual"]["closed_count"] == 1
    assert summary["actual"]["total_net_pnl_cny"] == "95"
    assert summary["wait_counterfactual"]["kind"].startswith("hypothetical_")
    assert summary["wait_counterfactual"]["closed_count"] == 1
    assert summary["wait_counterfactual"]["triggered_count"] == 1
    assert summary["wait_counterfactual"]["total_net_pnl_cny"] == "27"


def test_short_wait_replay_uses_short_price_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "short-wait.jsonl"
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)
    decision_payload = _wait_decision(base, decision_id="M2609-short-wait")
    decision_payload["directional_view"] = "short"
    decision_payload["wait_counterfactual"].update(
        {
            "direction": "short",
            "trigger": "完成bar收盘不高于100后，下一bar开盘建立反事实空仓",
            "invalidation_price": "102",
            "target_price": "94",
        }
    )

    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)
    decision = decision_ledger.append_decision(
        ledger, _bind_report(tmp_path, decision_payload)
    )
    market_evidence = _write_artifact(
        tmp_path,
        "M2609-short-wait.wait-bars.json",
        {
            "schema_version": "decision_ledger_wait_bars_evidence.v1",
            "decision_id": decision["payload"]["decision_id"],
            "analysis_contract": "M2609",
            "bar_interval": "30m",
            "bars": [
                {
                    "start_at": _at(base),
                    "end_at": _at(base, minutes=30),
                    "open": "101",
                    "high": "101",
                    "low": "98",
                    "close": "99",
                },
                {
                    "start_at": _at(base, minutes=30),
                    "end_at": _at(base, hours=1),
                    "open": "100",
                    "high": "101",
                    "low": "98",
                    "close": "99",
                },
                {
                    "start_at": _at(base, hours=1),
                    "end_at": _at(base, hours=1, minutes=30),
                    "open": "99",
                    "high": "100",
                    "low": "96",
                    "close": "97",
                },
                {
                    "start_at": _at(base, hours=1, minutes=30),
                    "end_at": _at(base, hours=2),
                    "open": "97",
                    "high": "98",
                    "low": "95",
                    "close": "96",
                },
            ],
        },
    )
    outcome = {
        "decision_id": decision["payload"]["decision_id"],
        "decision_hash": decision["event_hash"],
        "outcome_observed_at": _at(base, hours=3),
        "result_kind": "wait_counterfactual",
        "triggered": True,
        "entry": {
            "at": _at(base, minutes=30),
            "price": "100",
            "quantity": "1",
            "side": "sell",
        },
        "evaluation_exit": {
            "at": _at(base, hours=2),
            "price": "96",
            "reason": "evaluation_end",
        },
        "commission_cny": "2",
        "slippage_cny": "1",
        "roll": {"occurred": False, "cost_cny": "0", "details": []},
        "gross_pnl_cny": "40",
        "net_pnl_cny": "37",
        "mfe_cny": "50",
        "mae_cny": "10",
        "market_evidence": market_evidence,
    }
    _set_clock(monkeypatch, base + timedelta(hours=4))
    decision_ledger.append_outcome(ledger, outcome)

    summary = decision_ledger.summarize_ledger(ledger)
    assert summary["wait_counterfactual"]["closed_count"] == 1
    assert summary["wait_counterfactual"]["total_net_pnl_cny"] == "37"


def test_wrong_net_pnl_missing_cost_and_partial_exit_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "forward.jsonl"
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)
    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)
    decision = decision_ledger.append_decision(
        ledger, _bind_report(tmp_path, _trade_decision(base))
    )
    _set_clock(monkeypatch, base + timedelta(hours=5))

    wrong_net = _actual_outcome(decision, base, tmp_path)
    # 92 would subtract the already embedded slippage a second time; actual
    # fill PnL is 100 - 5 commission = 95.
    wrong_net["net_pnl_cny"] = "92"
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="expected=95"):
        decision_ledger.append_outcome(ledger, wrong_net)

    missing_cost = _actual_outcome(decision, base, tmp_path)
    missing_cost["commission_cny"] = None
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="plain decimal string"):
        decision_ledger.append_outcome(ledger, missing_cost)

    partial_exit = _actual_outcome(decision, base, tmp_path)
    partial_exit["exit"]["quantity"] = "1"
    partial_exit["gross_pnl_cny"] = "50"
    partial_exit["net_pnl_cny"] = "45"
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="one planned entry and one full exit"):
        decision_ledger.append_outcome(ledger, partial_exit)

    incomplete_assumption = _trade_decision(base, decision_id="M2609-incomplete-cost")
    incomplete_assumption["cost_assumptions"]["complete"] = False
    _bind_report(tmp_path, incomplete_assumption)
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="fail closed"):
        decision_ledger.append_decision(ledger, incomplete_assumption)


def test_artifact_descriptors_must_exist_match_hash_and_bind_normalized_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)

    missing_ledger = tmp_path / "missing-report.jsonl"
    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(missing_ledger)
    missing_report = _trade_decision(base, decision_id="M2609-missing-report")
    missing_report["report_evidence"] = {
        "path": "does-not-exist.json",
        "sha256": "a" * 64,
        "schema_version": "futures_main_report.v3",
    }
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="not a readable file"):
        decision_ledger.append_decision(missing_ledger, missing_report)

    self_signed_wrapper = _bind_report(
        tmp_path, _trade_decision(base, decision_id="M2609-self-signed-wrapper")
    )
    self_signed_wrapper["report_evidence"] = _write_artifact(
        tmp_path,
        "M2609-self-signed-wrapper.report.json",
        {
            "schema_version": "decision_ledger_report_snapshot.v1",
            "decision": {"action": "long"},
        },
    )
    with pytest.raises(
        decision_ledger.DecisionLedgerValidationError,
        match="report_evidence.schema_version is unsupported",
    ):
        decision_ledger.append_decision(missing_ledger, self_signed_wrapper)

    report_mismatch = _bind_report(
        tmp_path, _trade_decision(base, decision_id="M2609-report-mismatch")
    )
    report_mismatch["main_hypothesis"] = "报告快照生成后才改写的假设"
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="does not exactly bind"):
        decision_ledger.append_decision(missing_ledger, report_mismatch)

    report_ledger = tmp_path / "report-delete.jsonl"
    decision_ledger.init_ledger(report_ledger)
    report_payload = _bind_report(
        tmp_path, _trade_decision(base, decision_id="M2609-report-delete")
    )
    decision_ledger.append_decision(report_ledger, report_payload)
    (tmp_path / report_payload["report_evidence"]["path"]).unlink()
    validation = decision_ledger.validate_ledger(report_ledger, now=base)
    assert validation["valid"] is False
    assert "not a readable file" in validation["errors"][0]

    policy_ledger = tmp_path / "report-policy-delete.jsonl"
    decision_ledger.init_ledger(policy_ledger)
    policy_payload = _bind_report(
        tmp_path, _trade_decision(base, decision_id="M2609-report-policy-delete")
    )
    decision_ledger.append_decision(policy_ledger, policy_payload)
    (tmp_path / policy_payload["report_policy_evidence"]["path"]).unlink()
    validation = decision_ledger.validate_ledger(policy_ledger, now=base)
    assert validation["valid"] is False
    assert "not a readable file" in validation["errors"][0]

    execution_ledger = tmp_path / "execution-bind.jsonl"
    decision_ledger.init_ledger(execution_ledger)
    decision = decision_ledger.append_decision(
        execution_ledger,
        _bind_report(tmp_path, _trade_decision(base, decision_id="M2609-execution-bind")),
    )
    _set_clock(monkeypatch, base + timedelta(hours=5))
    resigned_mismatch = _actual_outcome(decision, base, tmp_path)
    descriptor = resigned_mismatch["execution_evidence"]
    artifact = json.loads((tmp_path / descriptor["path"]).read_text(encoding="utf-8"))
    artifact["outcome"]["commission_cny"] = "6"
    resigned_mismatch["execution_evidence"] = _write_artifact(
        tmp_path, descriptor["path"], artifact
    )
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="does not exactly bind"):
        decision_ledger.append_outcome(execution_ledger, resigned_mismatch)

    fake_hash = _actual_outcome(decision, base, tmp_path)
    fake_hash["execution_evidence"]["sha256"] = "f" * 64
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="SHA-256 mismatch"):
        decision_ledger.append_outcome(execution_ledger, fake_hash)

    valid = _actual_outcome(decision, base, tmp_path)
    decision_ledger.append_outcome(execution_ledger, valid)
    (tmp_path / valid["execution_evidence"]["path"]).unlink()
    validation = decision_ledger.validate_ledger(
        execution_ledger, now=base + timedelta(hours=5)
    )
    assert validation["valid"] is False
    assert validation["profit_verified"] is False


def test_wait_same_bar_target_and_stop_is_replayed_as_unscorable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "ambiguous-wait.jsonl"
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)
    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)
    decision = decision_ledger.append_decision(
        ledger,
        _bind_report(tmp_path, _wait_decision(base, decision_id="M2609-ambiguous-wait")),
    )
    attempted = _wait_outcome(decision, base, tmp_path)
    descriptor = attempted["market_evidence"]
    artifact = json.loads((tmp_path / descriptor["path"]).read_text(encoding="utf-8"))
    artifact["bars"][1]["high"] = "107"
    artifact["bars"][1]["low"] = "97"
    attempted["market_evidence"] = _write_artifact(tmp_path, descriptor["path"], artifact)

    _set_clock(monkeypatch, base + timedelta(hours=4))
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="same_bar.*unobservable"):
        decision_ledger.append_outcome(ledger, attempted)

    decision_ledger.append_outcome(
        ledger,
        {
            "decision_id": decision["payload"]["decision_id"],
            "decision_hash": decision["event_hash"],
            "outcome_observed_at": _at(base, hours=3),
            "result_kind": "wait_unscorable",
            "reason": "same_bar_target_and_invalidation_order_unobservable",
            "missing_evidence": ["intrabar_trade_path"],
            "market_evidence": attempted["market_evidence"],
        },
    )
    summary = decision_ledger.summarize_ledger(ledger)
    assert summary["wait_counterfactual"]["closed_count"] == 0
    assert summary["wait_counterfactual"]["resolved_without_sample_count"] == 1
    assert summary["wait_counterfactual"]["unscorable_wait"][
        "defined_but_intrabar_ambiguous_count"
    ] == 1
    assert summary["profit_verified"] is False


def test_wait_bar_replay_rejects_omitted_prefix_and_internal_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)

    for suffix, mutate in (
        ("missing-prefix", lambda bars: bars.pop(0)),
        (
            "internal-gap",
            lambda bars: bars[2].update(
                {
                    "start_at": _at(base, hours=1, minutes=15),
                    "end_at": _at(base, hours=1, minutes=45),
                }
            ),
        ),
    ):
        ledger = tmp_path / f"{suffix}.jsonl"
        _set_clock(monkeypatch, base)
        decision_ledger.init_ledger(ledger)
        decision = decision_ledger.append_decision(
            ledger,
            _bind_report(
                tmp_path,
                _wait_decision(base, decision_id=f"M2609-wait-{suffix}"),
            ),
        )
        outcome = _wait_outcome(decision, base, tmp_path)
        descriptor = outcome["market_evidence"]
        artifact = json.loads((tmp_path / descriptor["path"]).read_text(encoding="utf-8"))
        mutate(artifact["bars"])
        outcome["market_evidence"] = _write_artifact(
            tmp_path, descriptor["path"], artifact
        )
        _set_clock(monkeypatch, base + timedelta(hours=4))
        with pytest.raises(
            decision_ledger.DecisionLedgerValidationError,
            match="exact frozen evaluation interval|contiguous",
        ):
            decision_ledger.append_outcome(ledger, outcome)


def test_unavailable_wait_and_incomplete_costs_are_recorded_but_never_scored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "unscorable.jsonl"
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)
    decision_payload = _wait_decision(base, decision_id="M2609-unscorable-wait")
    decision_payload["cost_assumptions"] = {
        "complete": False,
        "commission_cny": None,
        "slippage_cny": None,
        "roll_cny": "0",
        "basis": None,
        "missing_fields": ["commission_cny", "slippage_cny"],
        "reason": "没有账户费率和可成交盘口，不能伪造成本",
    }
    decision_payload["wait_counterfactual"] = {
        "status": "unavailable",
        "reason": "完成K线结构缺失，无法冻结不存在的入场和失效点",
        "missing_evidence": ["completed_daily_structure", "executable_quote"],
        "report_gap_refs": [
            "agent_judgment.coverage_uncertainty.known_gaps"
        ],
    }
    _bind_report(tmp_path, decision_payload)
    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)
    decision = decision_ledger.append_decision(ledger, decision_payload)

    _set_clock(monkeypatch, base + timedelta(hours=4))
    decision_ledger.append_outcome(
        ledger,
        _unavailable_wait_outcome(decision, base, tmp_path),
    )

    summary = decision_ledger.summarize_ledger(ledger)
    assert summary["ledger_validation"]["valid"] is True
    assert summary["wait_counterfactual"]["closed_count"] == 0
    assert summary["wait_counterfactual"]["unscorable_wait"] == {
        "decision_count": 1,
        "closed_count": 1,
        "open_count": 0,
        "defined_but_intrabar_ambiguous_count": 0,
        "profit_sample_inclusion": False,
        "reason": "counterfactual or cost evidence was unavailable when frozen",
    }
    assert summary["profit_verified"] is False


def test_wait_report_point_plan_and_counterfactual_status_are_bidirectionally_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "wait-plan-binding.jsonl"
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)
    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)

    hides_defined_plan = _bind_report(
        tmp_path,
        _wait_decision(base, decision_id="M2609-hide-defined-wait-plan"),
    )
    hides_defined_plan["wait_counterfactual"] = {
        "status": "unavailable",
        "reason": "试图把已有点位计划改记为不可评分",
        "missing_evidence": ["claimed_missing_costs"],
        "report_gap_refs": [
            "agent_judgment.costs_and_slippage_considered"
        ],
    }
    with pytest.raises(
        decision_ledger.DecisionLedgerValidationError,
        match="with a point_plan requires.*defined",
    ):
        decision_ledger.append_decision(ledger, hides_defined_plan)

    invents_plan_outside_report = _bind_report(
        tmp_path,
        _wait_decision(base, decision_id="M2609-invent-plan-outside-report"),
    )
    descriptor = invents_plan_outside_report["report_evidence"]
    report = json.loads((tmp_path / descriptor["path"]).read_text(encoding="utf-8"))
    report["point_plan"] = None
    invents_plan_outside_report["report_evidence"] = _write_artifact(
        tmp_path, descriptor["path"], report
    )
    with pytest.raises(
        decision_ledger.DecisionLedgerValidationError,
        match="point_plan=null requires.*unavailable",
    ):
        decision_ledger.append_decision(ledger, invents_plan_outside_report)


def test_duplicate_close_and_future_event_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "forward.jsonl"
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)
    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)
    decision = decision_ledger.append_decision(
        ledger, _bind_report(tmp_path, _trade_decision(base))
    )
    _set_clock(monkeypatch, base + timedelta(hours=5))
    outcome = _actual_outcome(decision, base, tmp_path)
    future_observation = _actual_outcome(decision, base, tmp_path)
    future_observation["outcome_observed_at"] = _at(base, hours=6)
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="forward ordered"):
        decision_ledger.append_outcome(ledger, future_observation)
    decision_ledger.append_outcome(ledger, outcome)
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="already closed"):
        decision_ledger.append_outcome(ledger, outcome)

    assert decision_ledger.validate_ledger(
        ledger, now=base - timedelta(days=1)
    )["valid"] is False
    assert "future" in decision_ledger.validate_ledger(
        ledger, now=base - timedelta(days=1)
    )["errors"][0]


def test_checkpoint_detects_tamper_deletion_reordering_when_head_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = datetime(2026, 9, 13, 9, tzinfo=BJT)
    ledger = tmp_path / "source.jsonl"
    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)
    first = decision_ledger.append_decision(
        ledger, _bind_report(tmp_path, _trade_decision(base))
    )
    _set_clock(monkeypatch, base + timedelta(hours=1))
    second_payload = _trade_decision(base + timedelta(hours=1), decision_id="M2609-actual-2")
    second_payload["trade_plan"]["latest_entry_at"] = _at(base, hours=3)
    second_payload["reassess_by"] = _at(base, hours=6)
    _bind_report(tmp_path, second_payload)
    decision_ledger.append_decision(ledger, second_payload)
    original = ledger.read_bytes()
    original_head = decision_ledger._head_path(ledger).read_bytes()

    tampered = tmp_path / "tampered.jsonl"
    tampered.write_bytes(original.replace("需求改善".encode(), "供应改善".encode()))
    decision_ledger._head_path(tampered).write_bytes(original_head)
    result = decision_ledger.validate_ledger(tampered, now=base + timedelta(hours=1))
    assert result["valid"] is False
    assert "checkpoint" in result["errors"][0]

    deleted = tmp_path / "deleted.jsonl"
    deleted.write_bytes(original.splitlines(keepends=True)[0])
    decision_ledger._head_path(deleted).write_bytes(original_head)
    result = decision_ledger.validate_ledger(deleted, now=base + timedelta(hours=1))
    assert result["valid"] is False
    assert "deletion" in result["errors"][0]

    reordered = tmp_path / "reordered.jsonl"
    lines = original.splitlines(keepends=True)
    reordered.write_bytes(lines[1] + lines[0])
    decision_ledger._head_path(reordered).write_bytes(original_head)
    result = decision_ledger.validate_ledger(reordered, now=base + timedelta(hours=1))
    assert result["valid"] is False
    assert "checkpoint" in result["errors"][0]

    # Recalculating only the in-file chain is detected while the separately
    # stored head is unchanged. Rewriting both is explicitly outside assurance.
    resigned = tmp_path / "resigned.jsonl"
    events = [json.loads(line) for line in original.decode().splitlines()]
    events[0]["payload"]["main_hypothesis"] = "事后改写的假设"
    first_without_hash = {key: value for key, value in events[0].items() if key != "event_hash"}
    events[0]["event_hash"] = decision_ledger._event_hash(first_without_hash)
    events[1]["previous_event_hash"] = events[0]["event_hash"]
    second_without_hash = {key: value for key, value in events[1].items() if key != "event_hash"}
    events[1]["event_hash"] = decision_ledger._event_hash(second_without_hash)
    resigned.write_bytes(
        b"".join(decision_ledger._canonical_bytes(event) + b"\n" for event in events)
    )
    decision_ledger._head_path(resigned).write_bytes(original_head)
    result = decision_ledger.validate_ledger(resigned, now=base + timedelta(hours=1))
    assert result["valid"] is False
    assert "checkpoint" in result["errors"][0]
    assert first["event_hash"] != events[0]["event_hash"]


def test_external_review_floor_needs_twenty_natural_closures_and_two_year_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = tmp_path / "long-span.jsonl"
    start = datetime(2024, 8, 31, 9, tzinfo=BJT)
    last_base = start
    _set_clock(monkeypatch, start)
    decision_ledger.init_ledger(ledger)
    for index in range(20):
        base = start + timedelta(days=index * 39)
        last_base = base
        _set_clock(monkeypatch, base)
        decision = decision_ledger.append_decision(
            ledger,
            _bind_report(
                tmp_path,
                _trade_decision(base, decision_id=f"M2609-forward-{index:02d}"),
            ),
        )
        _set_clock(monkeypatch, base + timedelta(hours=5))
        decision_ledger.append_outcome(ledger, _actual_outcome(decision, base, tmp_path))

    summary = decision_ledger.summarize_ledger(ledger)
    assert summary["actual"]["closed_count"] == 20
    assert summary["observation_span_days"] >= 730
    assert summary["all_due_decisions_have_outcomes"] is True
    assert summary["validation_status"] == "meets_minimum_accounting_data_for_external_review"
    assert summary["meets_minimum_accounting_data_for_external_review"] is True
    assert summary["profit_verified"] is False

    # A far-future reassess_by cannot hide an already-expired entry window.
    open_base = last_base + timedelta(days=1)
    _set_clock(monkeypatch, open_base)
    open_payload = _trade_decision(open_base, decision_id="M2609-due-but-open")
    open_payload["reassess_by"] = _at(open_base + timedelta(days=3650), hours=5)
    open_decision = decision_ledger.append_decision(
        ledger,
        _bind_report(tmp_path, open_payload),
    )
    _set_clock(monkeypatch, open_base + timedelta(hours=6))
    summary = decision_ledger.summarize_ledger(ledger)
    assert summary["all_due_decisions_have_outcomes"] is False
    assert summary["missing_due_outcome_decision_ids"] == ["M2609-due-but-open"]
    assert summary["meets_minimum_accounting_data_for_external_review"] is False
    assert summary["profit_verified"] is False

    decision_ledger.append_outcome(
        ledger,
        _trade_not_entered_outcome(
            open_decision,
            _at(open_base, hours=6),
            tmp_path,
        ),
    )
    summary = decision_ledger.summarize_ledger(ledger)
    assert summary["actual"]["trade_not_entered_count"] == 1
    assert summary["actual"]["closed_count"] == 20
    assert summary["actual"]["open_count"] == 0
    assert summary["meets_minimum_accounting_data_for_external_review"] is True
    assert summary["profit_verified"] is False


def test_decision_replays_real_v3_report_and_frozen_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The integration path must not fall back to a ledger-authored wrapper."""

    from tests.test_decision_contract import base_report, bind_real_raw_and_structure

    monkeypatch.setattr(decision_ledger, "_replay_futures_report", REAL_REPORT_REPLAY)
    report = base_report()
    bind_real_raw_and_structure(report, tmp_path)
    report_descriptor = _write_artifact(tmp_path, "real-v3-report.json", report)
    report_policy = json.loads(
        (decision_ledger.ROOT / "config" / "futures_main_report_policy.json").read_text(
            encoding="utf-8"
        )
    )
    policy_descriptor = _write_artifact(
        tmp_path, "frozen-report-policy.json", report_policy
    )
    judgment = report["agent_judgment"]
    base = datetime(2026, 9, 13, 12, 1, tzinfo=BJT)
    payload = {
        "decision_id": "M2701-real-v3",
        "observed_at": report["observed_at"],
        "report_evidence": report_descriptor,
        "report_policy_evidence": policy_descriptor,
        "analysis_contract": "M2701",
        "main_hypothesis": judgment["synthesis_thesis"],
        "objective": PROFIT_OBJECTIVE,
        "expected_net_value": judgment["expected_net_value"],
        "agent_judgment_sha256": hashlib.sha256(
            json.dumps(
                judgment,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "directional_view": judgment["directional_view"],
        "action": "long",
        "confidence_range": {"low": "0.1", "high": "0.62"},
        "cost_assumptions": {
            "complete": True,
            "commission_cny": "5",
            "slippage_cny": "3",
            "roll_cny": "0",
            "basis": "冻结时可得费率与已验可成交ask的保守成本",
            "missing_fields": [],
            "reason": None,
        },
        "trade_plan": {
            "trigger": report["point_plan"]["trigger"]["text"],
            "entry_price": "105",
            "invalidation_price": "97",
            "target_price": "107",
            "quantity": "1",
            "contract_multiplier": "10",
            "latest_entry_at": _at(base, minutes=3),
        },
        "wait_counterfactual": None,
        "reassess_by": _at(base, minutes=4),
    }
    ledger = tmp_path / "real-v3-ledger.jsonl"
    _set_clock(monkeypatch, base)
    decision_ledger.init_ledger(ledger)
    event = decision_ledger.append_decision(ledger, payload)
    assert event["payload"]["agent_judgment_sha256"] == payload["agent_judgment_sha256"]
    assert decision_ledger.validate_ledger(ledger, now=base)["valid"] is True

    invalid_report = json.loads(json.dumps(report, ensure_ascii=False))
    invalid_report["agent_judgment"]["domains"].pop("member_flow")
    invalid_descriptor = _write_artifact(
        tmp_path, "real-v3-report-missing-domain.json", invalid_report
    )
    invalid_payload = dict(payload)
    invalid_payload.update(
        {
            "decision_id": "M2701-real-v3-missing-domain",
            "report_evidence": invalid_descriptor,
            "agent_judgment_sha256": hashlib.sha256(
                json.dumps(
                    invalid_report["agent_judgment"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
    )
    with pytest.raises(
        decision_ledger.DecisionLedgerValidationError,
        match="production replay",
    ):
        decision_ledger.append_decision(ledger, invalid_payload)


def test_cli_empty_ledger_and_validation_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "empty.jsonl"
    with pytest.raises(decision_ledger.DecisionLedgerValidationError, match="run init explicitly"):
        decision_ledger.append_decision(ledger, _trade_decision(datetime.now(tz=BJT)))
    assert decision_ledger.main(["init", str(ledger)]) == 0
    capsys.readouterr()
    assert decision_ledger.main(["validate", str(ledger)]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["valid"] is True
    assert validation["profit_verified"] is False
    assert decision_ledger.main(["summary", str(ledger)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["validation_status"] == "insufficient_forward_accounting_data"
    assert summary["profit_verified"] is False

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.validate_option_decision import (
    OptionDecisionValidationError,
    main,
    validate_option_decision,
)


OBSERVED = "2026-09-13T10:00:00+08:00"
LATEST = "2026-10-01T15:00:00+08:00"
HASH = "a" * 64


def source(at: str = "2026-09-13T09:59:40+08:00") -> dict:
    return {
        "url": "https://example.test/evidence",
        "retrieved_at": at,
        "content_sha256": HASH,
        "locator": "fixture row",
    }


def bind_underlying_report(
    tmp_path: Path,
    *,
    action: str,
    directional_view: str,
    product: str = "MA",
    analysis_contract: str = "MA2701",
    actual_main_contract: str | None = "MA2701",
) -> dict:
    report = {
        "schema_version": "futures_main_report.v3",
        "observed_at": "2026-09-13T09:59:50+08:00",
        "action": action,
        "target": {
            "product": product,
            "analysis_contract": analysis_contract,
            "actual_main_contract": actual_main_contract,
        },
        "agent_judgment": {"directional_view": directional_view},
        "market_data": {
            "quote": {
                "contract": analysis_contract,
                "observed_at": "2026-09-13T09:59:20+08:00",
                "price": 2500,
            }
        },
        "point_plan": {
            "direction": action,
            "trigger": {
                "condition": (
                    "completed_close_at_or_below"
                    if action == "short"
                    else "completed_close_at_or_above"
                ),
                "price": 2490 if action == "short" else 2510,
            },
            "target": {"price": 2400 if action == "short" else 2600},
            "invalidation": {
                "condition": (
                    "completed_close_at_or_above"
                    if action == "short"
                    else "completed_close_at_or_below"
                ),
                "price": 2550 if action == "short" else 2450,
            },
        },
    }
    path = tmp_path / "underlying.json"
    content = (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode()
    path.write_bytes(content)
    return {
        "path": path.name,
        "sha256": hashlib.sha256(content).hexdigest(),
        "schema_version": "futures_main_report.v3",
    }


def economics(*, premium: float = 20, basis: str = "current_ask") -> dict:
    quantity = 2
    multiplier = 10
    entry_fees = 4
    entry_slippage = 2
    return {
        "currency": "CNY",
        "quantity": quantity,
        "premium_basis": basis,
        "premium_per_unit": premium,
        "contract_multiplier": multiplier,
        "premium_notional": premium * multiplier * quantity,
        "entry_fees_total": entry_fees,
        "exit_fees_total": 4,
        "entry_slippage_total": entry_slippage,
        "exit_slippage_total": 2,
        "total_cost_basis": premium * multiplier * quantity + entry_fees + entry_slippage,
    }


def scenario_grid(decision: dict) -> list[dict]:
    thesis = decision["underlying_thesis"]
    expression = decision["option_expression"]
    cost = decision["economics"]
    prices = {
        "target": thesis["target"]["price"],
        "option_execution_stop": expression["option_execution_stop"]["price"],
        "futures_structure_invalidation": thesis["futures_structure_invalidation"]["price"],
        "time_failure": thesis["time_failure_reference_price"],
    }
    values = {
        "target": {"down": 60, "flat": 70, "up": 80},
        "option_execution_stop": {"down": 5, "flat": 7, "up": 9},
        "futures_structure_invalidation": {"down": 2, "flat": 3, "up": 4},
        "time_failure": {"down": 10, "flat": 12, "up": 14},
    }
    ivs = {"down": 0.15, "flat": 0.2, "up": 0.25}
    rows = []
    for underlying_case in (
        "target",
        "option_execution_stop",
        "futures_structure_invalidation",
        "time_failure",
    ):
        for iv_case in ("down", "flat", "up"):
            option_value = values[underlying_case][iv_case]
            net = (
                (option_value - cost["premium_per_unit"])
                * cost["contract_multiplier"]
                * cost["quantity"]
                - cost["entry_fees_total"]
                - cost["exit_fees_total"]
                - cost["entry_slippage_total"]
                - cost["exit_slippage_total"]
            )
            rows.append(
                {
                    "underlying_case": underlying_case,
                    "iv_case": iv_case,
                    "evaluation_time": (
                        LATEST
                        if underlying_case == "time_failure"
                        else "2026-09-20T15:00:00+08:00"
                    ),
                    "underlying_price": prices[underlying_case],
                    "implied_volatility": ivs[iv_case],
                    "estimated_option_value_per_unit": option_value,
                    "estimated_net_pnl": net,
                    "pricing_method": "scenario model, not an executable quote",
                    "model_or_source_ref": "model.fixture.v1",
                }
            )
    return rows


def base_decision(tmp_path: Path, *, put: bool = False) -> dict:
    route = "long_put" if put else "long_call"
    action = "short" if put else "long"
    option_type = "put" if put else "call"
    target_price = 2400 if put else 2600
    trigger_price = 2490 if put else 2510
    stop_price = 2525 if put else 2475
    structure_stop = 2550 if put else 2450
    target_condition = "underlying_at_or_below" if put else "underlying_at_or_above"
    trigger_condition = "completed_close_at_or_below" if put else "completed_close_at_or_above"
    stop_condition = "underlying_at_or_above" if put else "underlying_at_or_below"
    structure_condition = (
        "completed_close_at_or_above" if put else "completed_close_at_or_below"
    )
    delta = -0.5 if put else 0.5
    decision = {
        "schema_version": "option_decision.v1",
        "observed_at": OBSERVED,
        "decision_mode": "new_entry",
        "underlying_thesis": {
            "source_report": bind_underlying_report(
                tmp_path, action=action, directional_view=action
            ),
            "product": "MA",
            "actual_main_contract": "MA2701",
            "analysis_contract": "MA2701",
            "directional_view": action,
            "underlying_action": action,
            "underlying_snapshot": {
                "contract": "MA2701",
                "observed_at": "2026-09-13T09:59:20+08:00",
                "price": 2500,
                "source": source(),
            },
            "trigger": {
                "condition": trigger_condition,
                "price": trigger_price,
                "source_ref": "underlying.point_plan.trigger",
            },
            "target": {
                "condition": target_condition,
                "price": target_price,
                "source_ref": "underlying.point_plan.target",
            },
            "futures_structure_invalidation": {
                "condition": structure_condition,
                "price": structure_stop,
                "timeframe": "completed 1d bar",
                "source_ref": "underlying.point_plan.invalidation",
            },
            "time_failure_reference_price": 2505,
            "latest_realization_time": LATEST,
            "summary": "A sourced futures thesis supplied by the Agent.",
        },
        "option_market": {
            "status": "fresh",
            "freshness_limit_seconds": 300,
            "quote_age_seconds": 30,
            "selected_contract": {
                "symbol": f"MA2701-{option_type.upper()}-2500",
                "option_type": option_type,
                "underlying_contract": "MA2701",
                "expiration_date": "2026-10-15",
                "expiration_at": "2026-10-15T15:00:00+08:00",
                "strike": 2500,
                "contract_multiplier": 10,
                "contract_source": source(),
                "quote": {
                    "observed_at": "2026-09-13T09:59:30+08:00",
                    "bid": 19,
                    "ask": 20,
                    "last": 19.5,
                    "volume": 100,
                    "open_interest": 200,
                    "tick_size": 1,
                    "source": source(),
                },
                "analytics": {
                    "observed_at": "2026-09-13T09:59:30+08:00",
                    "implied_volatility": 0.2,
                    "delta": delta,
                    "gamma": 0.01,
                    "theta": -0.1,
                    "vega": 0.2,
                    "method": "provider Greeks with documented convention",
                    "source": source(),
                },
            },
            "liquidity": {
                "status": "executable",
                "spread": 1,
                "spread_ticks": 1,
                "reason": "Fresh two-sided quote with positive volume and open interest.",
            },
        },
        "option_expression": {
            "capital_action": "buy_to_open",
            "option_route": route,
            "entry_timing": "now",
            "entry_style": "anticipatory_breakout",
            "conditional_precondition": None,
            "option_execution_stop": {
                "condition": stop_condition,
                "price": stop_price,
                "source_ref": "option fast execution plan",
            },
            "one_entry_only": True,
            "one_full_exit_only": True,
            "pyramiding": False,
            "averaging_down": False,
        },
        "position": None,
        "economics": economics(),
        "scenario_grid": [],
        "rationale": {
            "decision_method": "agent_discretionary_synthesis",
            "action_reason": "Agent selected this route after comparing live alternatives.",
            "why_not_alternative": "The alternative had weaker cost-adjusted timing.",
            "strongest_counterevidence": "The breakout may fail before acceleration.",
            "why_enter_before_confirmation": "Current Gamma and premium justify defined-risk anticipation.",
        },
        "reassessment": {
            "reassess_by": "2026-09-14T10:00:00+08:00",
            "update_triggers": ["Refresh on main-contract roll, IV repricing or stop contact."],
        },
        "claim_boundary": {
            "validator_selected_direction": False,
            "validation_is_trade_permission": False,
            "automatic_ordering": False,
            "order_authorized": False,
        },
    }
    decision["scenario_grid"] = scenario_grid(decision)
    return decision


def make_wait(decision: dict) -> None:
    decision["option_expression"].update(
        {
            "capital_action": "wait",
            "option_route": "wait",
            "entry_timing": "wait",
            "entry_style": "wait",
            "conditional_precondition": None,
            "option_execution_stop": None,
        }
    )
    decision["economics"] = None
    decision["scenario_grid"] = []
    decision["rationale"]["why_enter_before_confirmation"] = None


def make_position_review(decision: dict, *, capital_action: str) -> None:
    selected = decision["option_market"]["selected_contract"]
    decision["decision_mode"] = "position_review"
    decision["option_expression"].update(
        {
            "capital_action": capital_action,
            "entry_timing": "not_applicable",
            "entry_style": "position_review",
            "conditional_precondition": None,
        }
    )
    decision["position"] = {
        "side": "long",
        "contract": selected["symbol"],
        "option_type": selected["option_type"],
        "underlying_contract": selected["underlying_contract"],
        "quantity": 2,
        "average_entry_premium": 18,
        "opened_at": "2026-09-10T10:00:00+08:00",
        "entry_fees_total": 4,
        "source": source(),
    }
    decision["economics"] = economics(premium=18, basis="position_average")
    decision["rationale"]["why_enter_before_confirmation"] = None
    decision["scenario_grid"] = scenario_grid(decision)
    if capital_action == "close":
        decision["scenario_grid"] = []
        decision["reassessment"] = None


def rejected(decision: dict, tmp_path: Path, match: str) -> None:
    with pytest.raises(OptionDecisionValidationError, match=match):
        validate_option_decision(decision, artifact_root=tmp_path)


def test_schema_is_valid_json_and_declares_exact_contract() -> None:
    path = Path(__file__).resolve().parents[1] / "schemas" / "option_decision.v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"]["const"] == "option_decision.v1"
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("put", [False, True])
def test_valid_new_entry_call_and_put_are_recorded_without_authority(
    tmp_path: Path, put: bool
) -> None:
    result = validate_option_decision(base_decision(tmp_path, put=put), artifact_root=tmp_path)
    assert result["valid"] is True
    assert result["option_route"] == ("long_put" if put else "long_call")
    assert result["scenario_count"] == 12
    assert result["underlying_report_bytes_verified"] is True
    assert result["validator_selected_direction"] is False
    assert result["validation_is_trade_permission"] is False
    assert result["order_authorized"] is False


def test_valid_wait_can_preserve_a_directional_underlying_thesis(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    make_wait(decision)
    result = validate_option_decision(decision, artifact_root=tmp_path)
    assert result["underlying_action"] == "long"
    assert result["capital_action"] == "wait"
    assert result["option_route"] == "wait"
    assert result["scenario_count"] == 0


def test_wait_can_fail_closed_with_an_unobservable_option_chain(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    make_wait(decision)
    decision["option_market"] = {
        "status": "unobservable",
        "freshness_limit_seconds": 300,
        "quote_age_seconds": None,
        "selected_contract": None,
        "liquidity": {
            "status": "unobservable",
            "spread": None,
            "spread_ticks": None,
            "reason": "No reliable same-month chain was observable.",
        },
    }
    result = validate_option_decision(decision, artifact_root=tmp_path)
    assert result["capital_action"] == "wait"
    assert result["selected_option_contract"] is None


@pytest.mark.parametrize("capital_action, expected_scenarios", [("hold", 12), ("close", 0)])
def test_valid_position_hold_and_close(
    tmp_path: Path, capital_action: str, expected_scenarios: int
) -> None:
    decision = base_decision(tmp_path)
    make_position_review(decision, capital_action=capital_action)
    result = validate_option_decision(decision, artifact_root=tmp_path)
    assert result["decision_mode"] == "position_review"
    assert result["capital_action"] == capital_action
    assert result["scenario_count"] == expected_scenarios


def test_close_may_record_that_the_original_realization_deadline_has_passed(
    tmp_path: Path,
) -> None:
    decision = base_decision(tmp_path)
    make_position_review(decision, capital_action="close")
    decision["underlying_thesis"]["latest_realization_time"] = (
        "2026-09-12T15:00:00+08:00"
    )
    result = validate_option_decision(decision, artifact_root=tmp_path)
    assert result["capital_action"] == "close"


def test_option_underlying_must_equal_analysis_month(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_market"]["selected_contract"]["underlying_contract"] = "MA2705"
    rejected(decision, tmp_path, "same underlying month")


def test_underlying_report_bytes_and_fields_are_bound(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["underlying_thesis"]["source_report"]["sha256"] = "0" * 64
    rejected(decision, tmp_path, "SHA-256 mismatch")

    decision = base_decision(tmp_path)
    decision["underlying_thesis"]["underlying_action"] = "short"
    rejected(decision, tmp_path, "conflicts with source report action")

    decision = base_decision(tmp_path)
    decision["underlying_thesis"]["target"]["price"] = 2610
    decision["scenario_grid"] = scenario_grid(decision)
    rejected(decision, tmp_path, "target.price")


def test_quote_time_status_and_bid_ask_are_replayed(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_market"]["status"] = "stale"
    rejected(decision, tmp_path, "must be fresh")

    decision = base_decision(tmp_path)
    decision["option_market"]["selected_contract"]["quote"].update({"bid": 21, "ask": 20})
    rejected(decision, tmp_path, "inverted")

    decision = base_decision(tmp_path)
    decision["option_market"]["quote_age_seconds"] = 29
    rejected(decision, tmp_path, "arithmetic mismatch")


def test_now_requires_fresh_executable_quote(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_market"]["liquidity"]["status"] = "incomplete"
    rejected(decision, tmp_path, "requires fresh executable")


def test_conditional_requires_explicit_recheck_precondition(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_expression"]["entry_timing"] = "conditional"
    rejected(decision, tmp_path, "requires an execution precondition")

    decision["option_expression"]["conditional_precondition"] = {
        "condition": "Execute only after the underlying trigger and a fresh chain refresh.",
        "requires_fresh_quote": True,
        "requires_same_underlying_contract": True,
        "requires_executable_bid_ask": True,
    }
    assert validate_option_decision(decision, artifact_root=tmp_path)["valid"] is True

    decision["option_market"]["selected_contract"]["quote"]["observed_at"] = (
        "2026-09-13T09:50:00+08:00"
    )
    decision["option_market"]["quote_age_seconds"] = 600
    decision["option_market"]["status"] = "stale"
    decision["option_market"]["liquidity"]["status"] = "incomplete"
    assert validate_option_decision(decision, artifact_root=tmp_path)["valid"] is True


def test_route_type_and_underlying_action_must_align(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_expression"]["option_route"] = "long_put"
    rejected(decision, tmp_path, "UnderlyingThesis action")

    decision = base_decision(tmp_path)
    decision["option_market"]["selected_contract"]["option_type"] = "put"
    decision["option_market"]["selected_contract"]["analytics"]["delta"] = -0.5
    rejected(decision, tmp_path, "selected contract type")


def test_expiration_must_cover_realization_horizon(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_market"]["selected_contract"]["expiration_date"] = "2026-09-30"
    decision["option_market"]["selected_contract"]["expiration_at"] = (
        "2026-09-30T15:00:00+08:00"
    )
    rejected(decision, tmp_path, "does not cover")


def test_iv_and_greeks_need_sourced_consistent_values(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_market"]["selected_contract"]["analytics"] = None
    rejected(decision, tmp_path, "sourced analytics")

    decision = base_decision(tmp_path)
    decision["option_market"]["selected_contract"]["analytics"]["delta"] = -0.2
    rejected(decision, tmp_path, "call delta")

    decision = base_decision(tmp_path)
    decision["option_market"]["selected_contract"]["analytics"]["source"][
        "retrieved_at"
    ] = "2026-09-13T10:01:00+08:00"
    rejected(decision, tmp_path, "cannot be after observed_at")

    decision = base_decision(tmp_path)
    decision["option_market"]["selected_contract"]["contract_source"][
        "retrieved_at"
    ] = "2026-09-13T10:01:00+08:00"
    rejected(decision, tmp_path, "cannot be after observed_at")


def test_fast_stop_and_completed_bar_invalidation_are_distinct(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_expression"]["option_execution_stop"][
        "condition"
    ] = "completed_close_at_or_below"
    rejected(decision, tmp_path, "condition is unsupported")

    decision = base_decision(tmp_path)
    decision["underlying_thesis"]["futures_structure_invalidation"][
        "condition"
    ] = "underlying_at_or_below"
    rejected(decision, tmp_path, "completed-close")


def test_directional_price_geometry_is_consistent(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["option_expression"]["option_execution_stop"]["price"] = 2501
    decision["scenario_grid"] = scenario_grid(decision)
    rejected(decision, tmp_path, "target > current underlying")


def test_scenario_grid_is_complete_and_arithmetically_bound(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["scenario_grid"].pop()
    rejected(decision, tmp_path, "exactly 12")

    decision = base_decision(tmp_path)
    decision["scenario_grid"][0]["estimated_net_pnl"] += 1
    rejected(decision, tmp_path, "arithmetic mismatch")

    decision = base_decision(tmp_path)
    flat = next(
        row
        for row in decision["scenario_grid"]
        if row["underlying_case"] == "target" and row["iv_case"] == "flat"
    )
    flat["implied_volatility"] = 0.21
    rejected(decision, tmp_path, "arithmetic mismatch")

    decision = base_decision(tmp_path)
    decision["economics"]["total_cost_basis"] += 1
    rejected(decision, tmp_path, "total_cost_basis")


def test_anticipatory_entry_requires_specific_rationale(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["rationale"]["why_enter_before_confirmation"] = None
    rejected(decision, tmp_path, "why_enter_before_confirmation")


def test_wait_and_position_modes_cannot_be_mixed(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    make_wait(decision)
    decision["option_expression"]["entry_timing"] = "now"
    rejected(decision, tmp_path, "wait requires")

    decision = base_decision(tmp_path)
    make_position_review(decision, capital_action="hold")
    decision["position"] = None
    rejected(decision, tmp_path, "requires a position")


def test_close_requires_executable_bid_and_no_deferred_reassessment(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    make_position_review(decision, capital_action="close")
    decision["option_market"]["liquidity"]["status"] = "incomplete"
    rejected(decision, tmp_path, "fresh executable bid")

    decision = base_decision(tmp_path)
    make_position_review(decision, capital_action="close")
    decision["reassessment"] = {
        "reassess_by": "2026-09-14T10:00:00+08:00",
        "update_triggers": ["later"],
    }
    rejected(decision, tmp_path, "must not defer")


def test_claim_boundary_cannot_turn_validation_into_permission(tmp_path: Path) -> None:
    decision = base_decision(tmp_path)
    decision["claim_boundary"]["order_authorized"] = True
    rejected(decision, tmp_path, "must be false")


def test_cli_returns_machine_readable_success_and_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    decision = base_decision(tmp_path)
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    assert main([str(path)]) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["valid"] is True
    assert success["validation_is_trade_permission"] is False

    decision["claim_boundary"]["automatic_ordering"] = True
    path.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    assert main([str(path)]) == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["valid"] is False
    assert failure["automatic_ordering"] is False


def test_unknown_fields_and_duplicate_json_keys_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    decision = base_decision(tmp_path)
    decision["machine_selected_direction"] = "long"
    rejected(decision, tmp_path, "unsupported fields")

    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"option_decision.v1","schema_version":"forged"}', encoding="utf-8")
    assert main([str(path)]) == 2
    failure = json.loads(capsys.readouterr().err)
    assert "duplicate key" in failure["error"]

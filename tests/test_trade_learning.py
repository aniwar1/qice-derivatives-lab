from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import trade_learning


BJT = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 9, 13, 9, 0, tzinfo=BJT)


def _time(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def _artifact(root: Path, name: str, content: bytes) -> tuple[str, str]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path.relative_to(root)), hashlib.sha256(content).hexdigest()


def _intent(
    root: Path,
    *,
    trade_id: str = "MA2701-option-1",
    product: str = "MA",
    contract: str = "MA2701",
    option_contract: str = "MA2701C2500",
    route: str = "long_call",
    captured_at: datetime = BASE - timedelta(minutes=1),
) -> dict:
    path, digest = _artifact(root, f"{trade_id}.decision.json", trade_id.encode())
    return {
        "schema_version": "trade_intent.v1",
        "trade_id": trade_id,
        "decision_id": f"decision:{trade_id}",
        "instrument": {
            "product": product,
            "analysis_contract": contract,
            "option_contract": option_contract,
            "option_route": route,
        },
        "strategy_context": {
            "directional_view": "long" if route == "long_call" else "short",
            "entry_style": "anticipatory_breakout",
            "market_regime": "transition",
        },
        "evidence": [
            {
                "kind": "decision_artifact",
                "path": path,
                "sha256": digest,
                "captured_at": _time(captured_at),
                "supports": ["decision"],
            }
        ],
    }


def _outcome(
    root: Path,
    intent: dict,
    *,
    entry_at: datetime,
    exit_at: datetime,
    entry_premium: str = "100",
    exit_premium: str = "120",
    commission: str = "5",
    other_costs: str = "0",
    result_class: str = "win",
    tags: list[str] | None = None,
) -> dict:
    path, digest = _artifact(
        root,
        f"{intent['trade_id']}.execution.json",
        f"actual fills {intent['trade_id']}".encode(),
    )
    gross = (
        (trade_learning.Decimal(exit_premium) - trade_learning.Decimal(entry_premium))
        * trade_learning.Decimal("1")
        * trade_learning.Decimal("10")
    )
    net = gross - trade_learning.Decimal(commission) - trade_learning.Decimal(other_costs)
    attribution = {
        "win_strength_tags": tags or ["timely_entry"],
        "loss_cause_tags": [],
        "breakeven_tags": [],
    }
    if result_class == "loss":
        attribution = {
            "win_strength_tags": [],
            "loss_cause_tags": tags or ["false_breakout"],
            "breakeven_tags": [],
        }
    elif result_class == "breakeven":
        attribution = {
            "win_strength_tags": [],
            "loss_cause_tags": [],
            "breakeven_tags": tags or ["costs_absorbed_edge"],
        }
    return {
        "schema_version": "trade_outcome.v1",
        "trade_id": intent["trade_id"],
        "decision_id": intent["decision_id"],
        "instrument": intent["instrument"],
        "strategy_context": intent["strategy_context"],
        "execution": {
            "entry_at": _time(entry_at),
            "exit_at": _time(exit_at),
            "quantity": "1",
            "contract_multiplier": "10",
            "entry_premium": entry_premium,
            "exit_premium": exit_premium,
            "gross_pnl_cny": trade_learning._decimal_text(gross),
            "commission_cny": commission,
            "slippage_cny": "2",
            "slippage_accounting": "embedded_in_actual_fill_prices",
            "other_costs_cny": other_costs,
            "net_pnl_cny": trade_learning._decimal_text(net),
        },
        "result_class": result_class,
        "attribution": attribution,
        "evidence": [
            {
                "kind": "broker_execution_export",
                "path": path,
                "sha256": digest,
                "captured_at": _time(exit_at),
                "supports": ["entry", "exit", "fees"],
            }
        ],
    }


def test_forward_registration_outcome_and_idempotency(tmp_path: Path) -> None:
    ledger = tmp_path / "learning.jsonl"
    intent = _intent(tmp_path)
    registered = trade_learning.register_intent(
        ledger, intent, evidence_root=tmp_path, now=BASE
    )
    assert registered["status"] == "appended"
    assert trade_learning.register_intent(
        ledger, intent, evidence_root=tmp_path, now=BASE
    )["status"] == "already_recorded"

    outcome = _outcome(
        tmp_path,
        intent,
        entry_at=BASE + timedelta(minutes=1),
        exit_at=BASE + timedelta(minutes=3),
    )
    appended = trade_learning.append_outcome(
        ledger,
        outcome,
        evidence_root=tmp_path,
        now=BASE + timedelta(minutes=4),
    )
    assert appended["status"] == "appended"
    assert appended["event"]["payload"]["result_class"] == "win"
    assert trade_learning.append_outcome(
        ledger,
        outcome,
        evidence_root=tmp_path,
        now=BASE + timedelta(minutes=5),
    )["status"] == "already_recorded"
    assert len(trade_learning.read_events(ledger)) == 2

    changed = json.loads(json.dumps(outcome))
    changed["attribution"]["win_strength_tags"] = ["trend_alignment"]
    with pytest.raises(trade_learning.TradeLearningValidationError, match="different immutable outcome"):
        trade_learning.append_outcome(
            ledger,
            changed,
            evidence_root=tmp_path,
            now=BASE + timedelta(minutes=5),
        )


def test_rejects_backfill_and_outcome_without_intent(tmp_path: Path) -> None:
    ledger = tmp_path / "learning.jsonl"
    intent = _intent(tmp_path)
    outcome = _outcome(
        tmp_path,
        intent,
        entry_at=BASE - timedelta(minutes=1),
        exit_at=BASE + timedelta(minutes=1),
    )
    with pytest.raises(trade_learning.TradeLearningValidationError, match="requires intent"):
        trade_learning.append_outcome(
            ledger,
            outcome,
            evidence_root=tmp_path,
            now=BASE + timedelta(minutes=2),
        )
    trade_learning.register_intent(ledger, intent, evidence_root=tmp_path, now=BASE)
    with pytest.raises(trade_learning.TradeLearningValidationError, match="backfill rejected"):
        trade_learning.append_outcome(
            ledger,
            outcome,
            evidence_root=tmp_path,
            now=BASE + timedelta(minutes=2),
        )


def test_rejects_bad_evidence_pnl_classification_and_hidden_reasoning(tmp_path: Path) -> None:
    ledger = tmp_path / "learning.jsonl"
    intent = _intent(tmp_path)
    bad_intent = json.loads(json.dumps(intent))
    bad_intent["evidence"][0]["sha256"] = "f" * 64
    with pytest.raises(trade_learning.TradeLearningValidationError, match="hash mismatch"):
        trade_learning.register_intent(
            ledger, bad_intent, evidence_root=tmp_path, now=BASE
        )
    hidden_reasoning = json.loads(json.dumps(intent))
    hidden_reasoning["reasoning"] = "not allowed"
    with pytest.raises(trade_learning.TradeLearningValidationError, match="unsupported fields"):
        trade_learning.register_intent(
            ledger, hidden_reasoning, evidence_root=tmp_path, now=BASE
        )

    trade_learning.register_intent(ledger, intent, evidence_root=tmp_path, now=BASE)
    outcome = _outcome(
        tmp_path,
        intent,
        entry_at=BASE + timedelta(minutes=1),
        exit_at=BASE + timedelta(minutes=2),
    )
    bad_net = json.loads(json.dumps(outcome))
    bad_net["execution"]["net_pnl_cny"] = "999"
    with pytest.raises(trade_learning.TradeLearningValidationError, match="net PnL mismatch"):
        trade_learning.append_outcome(
            ledger,
            bad_net,
            evidence_root=tmp_path,
            now=BASE + timedelta(minutes=3),
        )
    bad_class = json.loads(json.dumps(outcome))
    bad_class["result_class"] = "loss"
    bad_class["attribution"] = {
        "win_strength_tags": [],
        "loss_cause_tags": ["false_breakout"],
        "breakeven_tags": [],
    }
    with pytest.raises(trade_learning.TradeLearningValidationError, match="result_class mismatch"):
        trade_learning.append_outcome(
            ledger,
            bad_class,
            evidence_root=tmp_path,
            now=BASE + timedelta(minutes=3),
        )


@pytest.mark.parametrize(
    ("exit_premium", "commission", "result_class", "tags", "expected_net"),
    [
        ("90", "5", "loss", ["false_breakout"], "-105"),
        ("100", "0", "breakeven", ["timely_scratch_exit"], "0"),
    ],
)
def test_accepts_derived_loss_and_breakeven_classes(
    tmp_path: Path,
    exit_premium: str,
    commission: str,
    result_class: str,
    tags: list[str],
    expected_net: str,
) -> None:
    ledger = tmp_path / f"{result_class}.jsonl"
    intent = _intent(tmp_path, trade_id=f"trade-{result_class}")
    trade_learning.register_intent(ledger, intent, evidence_root=tmp_path, now=BASE)
    outcome = _outcome(
        tmp_path,
        intent,
        entry_at=BASE + timedelta(minutes=1),
        exit_at=BASE + timedelta(minutes=2),
        exit_premium=exit_premium,
        commission=commission,
        result_class=result_class,
        tags=tags,
    )
    receipt = trade_learning.append_outcome(
        ledger,
        outcome,
        evidence_root=tmp_path,
        now=BASE + timedelta(minutes=3),
    )
    assert receipt["event"]["payload"]["result_class"] == result_class
    assert receipt["event"]["payload"]["execution"]["net_pnl_cny"] == expected_net


@pytest.mark.parametrize("entry_style", ["pre_event_positioning", "post_event_continuation"])
def test_entry_style_names_match_option_decision_contract(
    tmp_path: Path, entry_style: str
) -> None:
    ledger = tmp_path / f"{entry_style}.jsonl"
    intent = _intent(tmp_path, trade_id=f"trade-{entry_style}")
    intent["strategy_context"]["entry_style"] = entry_style
    receipt = trade_learning.register_intent(
        ledger, intent, evidence_root=tmp_path, now=BASE
    )
    assert receipt["event"]["payload"]["strategy_context"]["entry_style"] == entry_style


def test_hash_chain_detects_source_rewrite(tmp_path: Path) -> None:
    ledger = tmp_path / "learning.jsonl"
    intent = _intent(tmp_path)
    trade_learning.register_intent(ledger, intent, evidence_root=tmp_path, now=BASE)
    event = json.loads(ledger.read_text().splitlines()[0])
    event["payload"]["instrument"]["option_contract"] = "MA2701C2600"
    ledger.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(trade_learning.TradeLearningValidationError, match="payload hash mismatch"):
        trade_learning.read_events(ledger)


def test_rebuild_is_deterministic_and_emits_candidate_lessons(tmp_path: Path) -> None:
    ledger = tmp_path / "learning.jsonl"
    products = [
        ("MA", "MA2701", "MA2701C2500"),
        ("SA", "SA2701", "SA2701C1400"),
        ("MA", "MA2701", "MA2701C2500"),
        ("SA", "SA2701", "SA2701C1400"),
        ("MA", "MA2701", "MA2701C2500"),
    ]
    for index, (product, contract, option_contract) in enumerate(products):
        registered_at = BASE + timedelta(minutes=index * 10)
        intent = _intent(
            tmp_path,
            trade_id=f"trade-{index}",
            product=product,
            contract=contract,
            option_contract=option_contract,
            captured_at=registered_at - timedelta(minutes=1),
        )
        trade_learning.register_intent(
            ledger, intent, evidence_root=tmp_path, now=registered_at
        )
        outcome = _outcome(
            tmp_path,
            intent,
            entry_at=registered_at + timedelta(minutes=1),
            exit_at=registered_at + timedelta(minutes=2),
            tags=["timely_entry", "trend_alignment"],
        )
        trade_learning.append_outcome(
            ledger,
            outcome,
            evidence_root=tmp_path,
            now=registered_at + timedelta(minutes=3),
        )

    markdown = tmp_path / "learning.md"
    summary_json = tmp_path / "dashboard" / "summary.json"
    first = trade_learning.rebuild_outputs(
        ledger, markdown_path=markdown, summary_json_path=summary_json
    )
    first_markdown = markdown.read_bytes()
    first_json = summary_json.read_bytes()
    second = trade_learning.rebuild_outputs(
        ledger, markdown_path=markdown, summary_json_path=summary_json
    )
    assert second == first
    assert markdown.read_bytes() == first_markdown
    assert summary_json.read_bytes() == first_json
    rows = first["patterns"]["winning_strengths"]
    timely = next(row for row in rows if row["tag"] == "timely_entry")
    assert timely["status"] == "candidate_lesson"
    assert timely["support_count"] == 5
    assert timely["distinct_products"] == ["MA", "SA"]
    assert timely["rule_change_authorized"] is False
    assert first["lesson_policy"]["automatic_rule_updates"] is False
    assert first["claim_boundary"]["no_chain_of_thought_is_stored"] is True
    assert first["claim_boundary"]["artifact_hashes_preserve_bytes_not_source_authenticity"] is True
    assert "盈利交易的优点共性" in markdown.read_text(encoding="utf-8")


def test_empty_rebuild_produces_dashboard_ready_summary(tmp_path: Path) -> None:
    summary = trade_learning.rebuild_outputs(
        tmp_path / "missing.jsonl",
        markdown_path=tmp_path / "empty.md",
        summary_json_path=tmp_path / "empty.json",
    )
    assert summary["schema_version"] == "trade_learning_summary.v1"
    assert summary["source"]["closed_trade_count"] == 0
    assert summary["source"]["head_event_hash"] == trade_learning.ZERO_HASH
    assert summary["statistics"]["win_share"] is None
    assert json.loads((tmp_path / "empty.json").read_text()) == summary

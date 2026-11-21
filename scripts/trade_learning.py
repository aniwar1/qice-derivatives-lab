#!/usr/bin/env python3
"""Forward-only learning ledger for closed long-option trades.

The source ledger contains only a pre-registered final trade intent, actual
execution facts, evidence hashes and finite post-trade attribution tags.  It
does not accept free-form reasoning, infer a trading direction, or edit any
strategy rule.  Markdown and JSON summaries are deterministic projections of
the append-only source ledger.

The hash chain detects accidental or partial rewriting.  It is not a signed or
remote WORM store: an actor able to replace the ledger in full can also replace
the chain.  Evidence artifacts remain the source of truth for actual fills.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "local-data" / "trade-learning" / "trade-events.jsonl"
DEFAULT_MARKDOWN = ROOT / "local-data" / "trade-learning" / "TRADE_LEARNING.md"
DEFAULT_SUMMARY_JSON = ROOT / "local-data" / "trade-learning" / "summary.json"
BJT = ZoneInfo("Asia/Shanghai")
ZERO_HASH = "0" * 64
MAX_FUTURE_SKEW = timedelta(minutes=5)
MIN_LESSON_SAMPLES = 5
MIN_LESSON_PRODUCTS = 2

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PRODUCT_RE = re.compile(r"^[A-Z]{1,3}$")
CONTRACT_RE = re.compile(r"^[A-Z]{1,3}[0-9]{4}$")
OPTION_CONTRACT_RE = re.compile(r"^[A-Za-z0-9-]{3,40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
NONNEGATIVE_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")

ENTRY_STYLES = {
    "anticipatory_breakout",
    "confirmed_breakout",
    "trend_pullback",
    "structural_reversal",
    "pre_event_positioning",
    "post_event_continuation",
    "other_explained",
}
MARKET_REGIMES = {"trend", "range", "event", "transition", "unknown"}
EVIDENCE_KINDS = {
    "decision_artifact",
    "broker_statement",
    "broker_execution_export",
    "exchange_confirmation",
    "account_statement",
    "other_verifiable_artifact",
}
EVIDENCE_SUPPORTS = {"decision", "entry", "exit", "fees", "other_costs"}

WIN_TAGS = {
    "timely_entry": "入场时机匹配兑现窗口",
    "anticipatory_entry_with_tight_invalidation": "提前入场且执行失效约束清晰",
    "confirmed_breakout_follow_through": "确认突破后出现延续",
    "trend_alignment": "标的方向与趋势结构一致",
    "catalyst_realized": "预期催化在持仓期兑现",
    "favorable_iv": "隐含波动率变化有利",
    "controlled_spread": "买卖价差与执行成本受控",
    "sufficient_time_to_expiry": "剩余期限覆盖兑现窗口",
    "disciplined_exit": "按既定退出条件执行",
}
LOSS_TAGS = {
    "late_entry": "入场过晚导致盈亏比恶化",
    "false_breakout": "突破未延续或迅速回落",
    "underlying_thesis_wrong": "期货标的主假设被证伪",
    "trigger_not_confirmed": "未满足原定触发条件即入场",
    "adverse_iv_change": "隐含波动率变化不利",
    "theta_decay": "时间价值衰减超过标的兑现",
    "poor_liquidity": "流动性不足影响成交或退出",
    "wide_spread": "买卖价差侵蚀收益",
    "exit_delay": "满足退出条件后执行延迟",
    "execution_stop_not_followed": "未遵守期权执行止损",
    "event_gap": "事件跳空使既定执行路径失效",
    "insufficient_time_to_expiry": "剩余期限不足以等待观点兑现",
}
BREAKEVEN_TAGS = {
    "edge_not_realized": "方向优势未充分兑现",
    "costs_absorbed_edge": "交易成本吸收了毛收益",
    "timely_scratch_exit": "及时退出避免扩大损失",
    "mixed_signal_resolution": "多空证据在持仓期相互抵消",
}


class TradeLearningError(ValueError):
    """Base error for invalid records or ledger state."""


class TradeLearningValidationError(TradeLearningError):
    """Raised when a record, evidence artifact or hash chain is invalid."""


def _now_bjt() -> datetime:
    return datetime.now(tz=BJT).replace(microsecond=0)


def _recording_clock(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TradeLearningValidationError("recording clock must include an explicit UTC offset")
    return value.astimezone(BJT).replace(microsecond=0)


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TradeLearningValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_loads(text: str, *, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_object)
    except (json.JSONDecodeError, TradeLearningValidationError) as exc:
        raise TradeLearningValidationError(f"invalid {label}: {exc}") from exc


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TradeLearningValidationError(f"value is not canonical JSON: {exc}") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_object(
    value: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TradeLearningValidationError(f"{label} must be an object")
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise TradeLearningValidationError(f"{label} missing fields: {sorted(missing)}")
    if extra:
        raise TradeLearningValidationError(f"{label} has unsupported fields: {sorted(extra)}")
    return value


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value) is None:
        raise TradeLearningValidationError(f"{label} must be a stable identifier")
    return value


def _timestamp(value: Any, *, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise TradeLearningValidationError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TradeLearningValidationError(f"{label} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TradeLearningValidationError(f"{label} must include an explicit UTC offset")
    normalized = parsed.astimezone(BJT).replace(microsecond=0)
    return normalized.isoformat(timespec="seconds"), normalized


def _decimal(value: Any, *, label: str, nonnegative: bool = False, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise TradeLearningValidationError(f"{label} must be a base-10 string")
    pattern = NONNEGATIVE_DECIMAL_RE if nonnegative or positive else DECIMAL_RE
    if pattern.fullmatch(value) is None:
        raise TradeLearningValidationError(f"{label} is not a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise TradeLearningValidationError(f"{label} is not a decimal") from exc
    if nonnegative and parsed < 0:
        raise TradeLearningValidationError(f"{label} must be nonnegative")
    if positive and parsed <= 0:
        raise TradeLearningValidationError(f"{label} must be positive")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _normalize_instrument(value: Any) -> dict[str, str]:
    item = _exact_object(
        value,
        required={"product", "analysis_contract", "option_contract", "option_route"},
        label="instrument",
    )
    product = item["product"]
    contract = item["analysis_contract"]
    option_contract = item["option_contract"]
    route = item["option_route"]
    if not isinstance(product, str) or PRODUCT_RE.fullmatch(product) is None:
        raise TradeLearningValidationError("instrument.product must be an uppercase product code")
    if not isinstance(contract, str) or CONTRACT_RE.fullmatch(contract) is None:
        raise TradeLearningValidationError("instrument.analysis_contract must be a concrete contract")
    if not contract.startswith(product):
        raise TradeLearningValidationError("instrument product and analysis contract do not match")
    if not isinstance(option_contract, str) or OPTION_CONTRACT_RE.fullmatch(option_contract) is None:
        raise TradeLearningValidationError("instrument.option_contract has an unsupported format")
    if route not in {"long_call", "long_put"}:
        raise TradeLearningValidationError("instrument.option_route must be long_call or long_put")
    return {
        "product": product,
        "analysis_contract": contract,
        "option_contract": option_contract,
        "option_route": route,
    }


def _normalize_strategy_context(value: Any) -> dict[str, str]:
    item = _exact_object(
        value,
        required={"directional_view", "entry_style", "market_regime"},
        optional={"entry_style_detail"},
        label="strategy_context",
    )
    view = item["directional_view"]
    style = item["entry_style"]
    regime = item["market_regime"]
    if view not in {"long", "short"}:
        raise TradeLearningValidationError("strategy_context.directional_view must be long or short")
    if style not in ENTRY_STYLES:
        raise TradeLearningValidationError("strategy_context.entry_style is unsupported")
    if regime not in MARKET_REGIMES:
        raise TradeLearningValidationError("strategy_context.market_regime is unsupported")
    detail = item.get("entry_style_detail")
    if style == "other_explained":
        if not isinstance(detail, str) or not 1 <= len(detail.strip()) <= 160:
            raise TradeLearningValidationError(
                "other_explained requires a concise entry_style_detail (1-160 characters)"
            )
    elif detail is not None:
        raise TradeLearningValidationError(
            "entry_style_detail is allowed only for other_explained"
        )
    result = {"directional_view": view, "entry_style": style, "market_regime": regime}
    if detail is not None:
        result["entry_style_detail"] = detail.strip()
    return result


def _resolve_evidence_path(path_text: str, evidence_root: Path) -> Path:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        candidate = evidence_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise TradeLearningValidationError(f"evidence artifact does not exist: {path_text}") from exc
    if not resolved.is_file():
        raise TradeLearningValidationError(f"evidence artifact is not a file: {path_text}")
    return resolved


def _normalize_evidence(
    value: Any,
    *,
    required_supports: set[str],
    evidence_root: Path | None,
    no_later_than: datetime,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise TradeLearningValidationError("evidence must contain at least one artifact")
    normalized: list[dict[str, Any]] = []
    covered: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        item = _exact_object(
            raw,
            required={"kind", "path", "sha256", "captured_at", "supports"},
            label=f"evidence[{index}]",
        )
        kind = item["kind"]
        path_text = item["path"]
        digest = item["sha256"]
        if kind not in EVIDENCE_KINDS:
            raise TradeLearningValidationError(f"evidence[{index}].kind is unsupported")
        if not isinstance(path_text, str) or not 1 <= len(path_text) <= 1024:
            raise TradeLearningValidationError(f"evidence[{index}].path is invalid")
        if not isinstance(digest, str) or HEX64_RE.fullmatch(digest) is None:
            raise TradeLearningValidationError(f"evidence[{index}].sha256 is invalid")
        captured_text, captured_at = _timestamp(
            item["captured_at"], label=f"evidence[{index}].captured_at"
        )
        if captured_at > no_later_than + MAX_FUTURE_SKEW:
            raise TradeLearningValidationError(f"evidence[{index}] is future-dated")
        supports = item["supports"]
        if (
            not isinstance(supports, list)
            or not supports
            or any(not isinstance(entry, str) or entry not in EVIDENCE_SUPPORTS for entry in supports)
            or len(set(supports)) != len(supports)
        ):
            raise TradeLearningValidationError(f"evidence[{index}].supports is invalid")
        identity = (path_text, digest)
        if identity in identities:
            raise TradeLearningValidationError("duplicate evidence artifact reference")
        identities.add(identity)
        if evidence_root is not None:
            artifact = _resolve_evidence_path(path_text, evidence_root)
            actual = _sha256(artifact.read_bytes())
            if actual != digest:
                raise TradeLearningValidationError(
                    f"evidence hash mismatch for {path_text}: expected {digest}, got {actual}"
                )
        support_list = sorted(supports)
        covered.update(support_list)
        normalized.append(
            {
                "kind": kind,
                "path": path_text,
                "sha256": digest,
                "captured_at": captured_text,
                "supports": support_list,
            }
        )
    missing = required_supports - covered
    if missing:
        raise TradeLearningValidationError(f"evidence does not support: {sorted(missing)}")
    return sorted(normalized, key=lambda item: (item["kind"], item["path"], item["sha256"]))


def normalize_intent(
    value: Any,
    *,
    evidence_root: Path | None,
    recorded_at: datetime,
) -> dict[str, Any]:
    item = _exact_object(
        value,
        required={
            "schema_version",
            "trade_id",
            "decision_id",
            "instrument",
            "strategy_context",
            "evidence",
        },
        label="trade intent",
    )
    if item["schema_version"] != "trade_intent.v1":
        raise TradeLearningValidationError("unsupported trade intent schema")
    instrument = _normalize_instrument(item["instrument"])
    context = _normalize_strategy_context(item["strategy_context"])
    expected_view = "long" if instrument["option_route"] == "long_call" else "short"
    if context["directional_view"] != expected_view:
        raise TradeLearningValidationError("option route conflicts with directional view")
    normalized_evidence = _normalize_evidence(
        item["evidence"],
        required_supports={"decision"},
        evidence_root=evidence_root,
        no_later_than=recorded_at,
    )
    if not any(
        ref["kind"] == "decision_artifact" and "decision" in ref["supports"]
        for ref in normalized_evidence
    ):
        raise TradeLearningValidationError(
            "intent requires a decision_artifact supporting the final decision"
        )
    return {
        "schema_version": "trade_intent.v1",
        "trade_id": _identifier(item["trade_id"], label="trade_id"),
        "decision_id": _identifier(item["decision_id"], label="decision_id"),
        "instrument": instrument,
        "strategy_context": context,
        "evidence": normalized_evidence,
    }


def _normalize_tags(value: Any, *, allowed: dict[str, str], label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
        raise TradeLearningValidationError(f"{label} must be an array of attribution tags")
    if len(set(value)) != len(value):
        raise TradeLearningValidationError(f"{label} contains duplicate tags")
    unknown = set(value) - set(allowed)
    if unknown:
        raise TradeLearningValidationError(f"{label} has unsupported tags: {sorted(unknown)}")
    return sorted(value)


def normalize_outcome(
    value: Any,
    *,
    intent: dict[str, Any],
    intent_recorded_at: datetime,
    evidence_root: Path | None,
    recorded_at: datetime,
) -> dict[str, Any]:
    item = _exact_object(
        value,
        required={
            "schema_version",
            "trade_id",
            "decision_id",
            "instrument",
            "strategy_context",
            "execution",
            "result_class",
            "attribution",
            "evidence",
        },
        label="trade outcome",
    )
    if item["schema_version"] != "trade_outcome.v1":
        raise TradeLearningValidationError("unsupported trade outcome schema")
    trade_id = _identifier(item["trade_id"], label="trade_id")
    decision_id = _identifier(item["decision_id"], label="decision_id")
    instrument = _normalize_instrument(item["instrument"])
    context = _normalize_strategy_context(item["strategy_context"])
    if trade_id != intent["trade_id"] or decision_id != intent["decision_id"]:
        raise TradeLearningValidationError("outcome identifiers do not match the registered intent")
    if instrument != intent["instrument"] or context != intent["strategy_context"]:
        raise TradeLearningValidationError("outcome changed immutable intent fields")

    execution = _exact_object(
        item["execution"],
        required={
            "entry_at",
            "exit_at",
            "quantity",
            "contract_multiplier",
            "entry_premium",
            "exit_premium",
            "gross_pnl_cny",
            "commission_cny",
            "slippage_cny",
            "slippage_accounting",
            "other_costs_cny",
            "net_pnl_cny",
        },
        label="execution",
    )
    entry_text, entry_at = _timestamp(execution["entry_at"], label="execution.entry_at")
    exit_text, exit_at = _timestamp(execution["exit_at"], label="execution.exit_at")
    if entry_at < intent_recorded_at:
        raise TradeLearningValidationError(
            "backfill rejected: actual entry predates the forward intent registration"
        )
    if exit_at < entry_at:
        raise TradeLearningValidationError("execution.exit_at cannot precede entry_at")
    if exit_at > recorded_at + MAX_FUTURE_SKEW:
        raise TradeLearningValidationError("execution.exit_at is future-dated")
    quantity_text = execution["quantity"]
    if not isinstance(quantity_text, str) or POSITIVE_INTEGER_RE.fullmatch(quantity_text) is None:
        raise TradeLearningValidationError("execution.quantity must be a positive integer string")
    quantity = Decimal(quantity_text)
    multiplier = _decimal(
        execution["contract_multiplier"], label="execution.contract_multiplier", positive=True
    )
    entry_premium = _decimal(
        execution["entry_premium"], label="execution.entry_premium", nonnegative=True
    )
    exit_premium = _decimal(
        execution["exit_premium"], label="execution.exit_premium", nonnegative=True
    )
    gross = _decimal(execution["gross_pnl_cny"], label="execution.gross_pnl_cny")
    commission = _decimal(
        execution["commission_cny"], label="execution.commission_cny", nonnegative=True
    )
    slippage = _decimal(
        execution["slippage_cny"], label="execution.slippage_cny", nonnegative=True
    )
    if execution["slippage_accounting"] != "embedded_in_actual_fill_prices":
        raise TradeLearningValidationError(
            "slippage must be informational and embedded in actual fill prices"
        )
    other_costs = _decimal(
        execution["other_costs_cny"], label="execution.other_costs_cny", nonnegative=True
    )
    net = _decimal(execution["net_pnl_cny"], label="execution.net_pnl_cny")
    expected_gross = (exit_premium - entry_premium) * quantity * multiplier
    if gross != expected_gross:
        raise TradeLearningValidationError(
            f"gross PnL mismatch: expected {_decimal_text(expected_gross)}"
        )
    expected_net = gross - commission - other_costs
    if net != expected_net:
        raise TradeLearningValidationError(
            f"net PnL mismatch: expected {_decimal_text(expected_net)}"
        )
    expected_class = "win" if net > 0 else "loss" if net < 0 else "breakeven"
    if item["result_class"] != expected_class:
        raise TradeLearningValidationError(
            f"result_class mismatch: net PnL requires {expected_class}"
        )

    attribution = _exact_object(
        item["attribution"],
        required={"win_strength_tags", "loss_cause_tags", "breakeven_tags"},
        label="attribution",
    )
    win_tags = _normalize_tags(
        attribution["win_strength_tags"], allowed=WIN_TAGS, label="win_strength_tags"
    )
    loss_tags = _normalize_tags(
        attribution["loss_cause_tags"], allowed=LOSS_TAGS, label="loss_cause_tags"
    )
    breakeven_tags = _normalize_tags(
        attribution["breakeven_tags"], allowed=BREAKEVEN_TAGS, label="breakeven_tags"
    )
    tag_groups = {
        "win": (win_tags, loss_tags, breakeven_tags),
        "loss": (loss_tags, win_tags, breakeven_tags),
        "breakeven": (breakeven_tags, win_tags, loss_tags),
    }
    primary, incompatible_a, incompatible_b = tag_groups[expected_class]
    if not primary or incompatible_a or incompatible_b:
        raise TradeLearningValidationError(
            f"attribution tags must be non-empty only for result_class={expected_class}"
        )

    evidence_required = {"entry", "exit", "fees"}
    if other_costs > 0:
        evidence_required.add("other_costs")
    normalized_evidence = _normalize_evidence(
        item["evidence"],
        required_supports=evidence_required,
        evidence_root=evidence_root,
        no_later_than=recorded_at,
    )
    execution_evidence_kinds = {
        "broker_statement",
        "broker_execution_export",
        "exchange_confirmation",
        "account_statement",
    }
    execution_coverage: set[str] = set()
    for ref in normalized_evidence:
        if ref["kind"] in execution_evidence_kinds:
            execution_coverage.update(ref["supports"])
    missing_execution_coverage = {"entry", "exit", "fees"} - execution_coverage
    if missing_execution_coverage:
        raise TradeLearningValidationError(
            "broker, exchange or account evidence does not support: "
            f"{sorted(missing_execution_coverage)}"
        )
    exit_evidence_times = [
        _timestamp(ref["captured_at"], label="evidence.captured_at")[1]
        for ref in normalized_evidence
        if "exit" in ref["supports"]
    ]
    if not exit_evidence_times or max(exit_evidence_times) < exit_at:
        raise TradeLearningValidationError("exit evidence must be captured at or after the exit")

    return {
        "schema_version": "trade_outcome.v1",
        "trade_id": trade_id,
        "decision_id": decision_id,
        "instrument": instrument,
        "strategy_context": context,
        "execution": {
            "entry_at": entry_text,
            "exit_at": exit_text,
            "quantity": str(int(quantity)),
            "contract_multiplier": _decimal_text(multiplier),
            "entry_premium": _decimal_text(entry_premium),
            "exit_premium": _decimal_text(exit_premium),
            "gross_pnl_cny": _decimal_text(gross),
            "commission_cny": _decimal_text(commission),
            "slippage_cny": _decimal_text(slippage),
            "slippage_accounting": "embedded_in_actual_fill_prices",
            "other_costs_cny": _decimal_text(other_costs),
            "net_pnl_cny": _decimal_text(net),
        },
        "result_class": expected_class,
        "attribution": {
            "win_strength_tags": win_tags,
            "loss_cause_tags": loss_tags,
            "breakeven_tags": breakeven_tags,
        },
        "evidence": normalized_evidence,
    }


def _event_without_hash(
    *, sequence: int, event_type: str, recorded_at: str, payload: dict[str, Any], previous: str
) -> dict[str, Any]:
    return {
        "schema_version": "trade_learning_event.v1",
        "sequence": sequence,
        "event_type": event_type,
        "recorded_at": recorded_at,
        "payload_sha256": _sha256(_canonical_bytes(payload)),
        "previous_event_hash": previous,
        "payload": payload,
    }


def _read_events_unlocked(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []
    raw = ledger_path.read_bytes()
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise TradeLearningValidationError("ledger ends with an incomplete event")
    events: list[dict[str, Any]] = []
    intents: dict[str, tuple[dict[str, Any], datetime]] = {}
    outcomes: set[str] = set()
    previous = ZERO_HASH
    previous_recorded: datetime | None = None
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        event = _json_loads(raw_line.decode("utf-8"), label=f"ledger line {line_number}")
        item = _exact_object(
            event,
            required={
                "schema_version",
                "sequence",
                "event_type",
                "recorded_at",
                "payload_sha256",
                "previous_event_hash",
                "payload",
                "event_hash",
            },
            label=f"ledger event {line_number}",
        )
        if item["schema_version"] != "trade_learning_event.v1":
            raise TradeLearningValidationError(f"unsupported ledger event at line {line_number}")
        if item["sequence"] != line_number:
            raise TradeLearningValidationError(f"non-contiguous sequence at line {line_number}")
        if item["event_type"] not in {"intent_registered", "outcome_recorded"}:
            raise TradeLearningValidationError(f"unsupported event type at line {line_number}")
        recorded_text, recorded_at = _timestamp(item["recorded_at"], label="event.recorded_at")
        if recorded_text != item["recorded_at"]:
            raise TradeLearningValidationError(f"non-canonical event time at line {line_number}")
        if previous_recorded is not None and recorded_at < previous_recorded:
            raise TradeLearningValidationError(f"event time moved backwards at line {line_number}")
        if item["previous_event_hash"] != previous:
            raise TradeLearningValidationError(f"broken previous hash at line {line_number}")
        if item["payload_sha256"] != _sha256(_canonical_bytes(item["payload"])):
            raise TradeLearningValidationError(f"payload hash mismatch at line {line_number}")
        unsigned = {key: value for key, value in item.items() if key != "event_hash"}
        expected_event_hash = _sha256(_canonical_bytes(unsigned))
        if item["event_hash"] != expected_event_hash:
            raise TradeLearningValidationError(f"event hash mismatch at line {line_number}")
        if item["event_type"] == "intent_registered":
            payload = normalize_intent(
                item["payload"], evidence_root=None, recorded_at=recorded_at
            )
            trade_id = payload["trade_id"]
            if trade_id in intents:
                raise TradeLearningValidationError(f"duplicate registered trade_id: {trade_id}")
            intents[trade_id] = (payload, recorded_at)
        else:
            trade_id_raw = item["payload"].get("trade_id") if isinstance(item["payload"], dict) else None
            if trade_id_raw not in intents:
                raise TradeLearningValidationError(
                    f"outcome has no earlier registered intent at line {line_number}"
                )
            if trade_id_raw in outcomes:
                raise TradeLearningValidationError(f"duplicate outcome for trade_id: {trade_id_raw}")
            intent, intent_time = intents[trade_id_raw]
            payload = normalize_outcome(
                item["payload"],
                intent=intent,
                intent_recorded_at=intent_time,
                evidence_root=None,
                recorded_at=recorded_at,
            )
            outcomes.add(payload["trade_id"])
        if payload != item["payload"]:
            raise TradeLearningValidationError(f"non-canonical payload at line {line_number}")
        events.append(item)
        previous = item["event_hash"]
        previous_recorded = recorded_at
    return events


def read_events(ledger_path: Path = DEFAULT_LEDGER) -> list[dict[str, Any]]:
    return _read_events_unlocked(Path(ledger_path))


def _lock_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(f".{ledger_path.name}.lock")


@contextmanager
def _exclusive_lock(ledger_path: Path) -> Iterator[None]:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(ledger_path)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _append_event(
    ledger_path: Path,
    *,
    event_type: str,
    payload: dict[str, Any],
    recorded_at: datetime,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    recorded_at = _recording_clock(recorded_at)
    if events:
        _, latest_recorded_at = _timestamp(
            events[-1]["recorded_at"], label="latest event recorded_at"
        )
        if recorded_at < latest_recorded_at:
            raise TradeLearningValidationError(
                "recording clock moved backwards; ledger was not modified"
            )
    previous = events[-1]["event_hash"] if events else ZERO_HASH
    unsigned = _event_without_hash(
        sequence=len(events) + 1,
        event_type=event_type,
        recorded_at=recorded_at.isoformat(timespec="seconds"),
        payload=payload,
        previous=previous,
    )
    event = {**unsigned, "event_hash": _sha256(_canonical_bytes(unsigned))}
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("ab") as stream:
        stream.write(_canonical_bytes(event) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return event


def register_intent(
    ledger_path: Path,
    value: Any,
    *,
    evidence_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    ledger_path = Path(ledger_path)
    observed_now = _recording_clock(now or _now_bjt())
    with _exclusive_lock(ledger_path):
        events = _read_events_unlocked(ledger_path)
        payload = normalize_intent(
            value, evidence_root=Path(evidence_root), recorded_at=observed_now
        )
        existing = [
            event
            for event in events
            if event["event_type"] == "intent_registered"
            and event["payload"]["trade_id"] == payload["trade_id"]
        ]
        if existing:
            event = existing[0]
            if event["payload_sha256"] != _sha256(_canonical_bytes(payload)):
                raise TradeLearningValidationError(
                    "trade_id already has a different immutable intent"
                )
            return {"status": "already_recorded", "event": event}
        event = _append_event(
            ledger_path,
            event_type="intent_registered",
            payload=payload,
            recorded_at=observed_now,
            events=events,
        )
        return {"status": "appended", "event": event}


def append_outcome(
    ledger_path: Path,
    value: Any,
    *,
    evidence_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    ledger_path = Path(ledger_path)
    observed_now = _recording_clock(now or _now_bjt())
    with _exclusive_lock(ledger_path):
        events = _read_events_unlocked(ledger_path)
        if not isinstance(value, dict):
            raise TradeLearningValidationError("trade outcome must be an object")
        trade_id = _identifier(value.get("trade_id"), label="trade_id")
        intent_events = [
            event
            for event in events
            if event["event_type"] == "intent_registered"
            and event["payload"]["trade_id"] == trade_id
        ]
        if not intent_events:
            raise TradeLearningValidationError(
                "forward-only policy requires intent registration before entry"
            )
        intent_event = intent_events[0]
        _, intent_time = _timestamp(intent_event["recorded_at"], label="intent.recorded_at")
        payload = normalize_outcome(
            value,
            intent=intent_event["payload"],
            intent_recorded_at=intent_time,
            evidence_root=Path(evidence_root),
            recorded_at=observed_now,
        )
        existing = [
            event
            for event in events
            if event["event_type"] == "outcome_recorded"
            and event["payload"]["trade_id"] == trade_id
        ]
        if existing:
            event = existing[0]
            if event["payload_sha256"] != _sha256(_canonical_bytes(payload)):
                raise TradeLearningValidationError(
                    "trade_id already has a different immutable outcome"
                )
            return {"status": "already_recorded", "event": event}
        event = _append_event(
            ledger_path,
            event_type="outcome_recorded",
            payload=payload,
            recorded_at=observed_now,
            events=events,
        )
        return {"status": "appended", "event": event}


def _ratio_text(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    value = (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))
    return format(value, ".4f")


def _pattern_rows(
    outcomes: list[dict[str, Any]],
    *,
    result_class: str,
    tag_field: str,
    labels: dict[str, str],
    pattern_kind: str,
) -> list[dict[str, Any]]:
    cohort = [outcome for outcome in outcomes if outcome["result_class"] == result_class]
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for outcome in cohort:
        for tag in outcome["attribution"][tag_field]:
            by_tag.setdefault(tag, []).append(outcome)
    rows: list[dict[str, Any]] = []
    for tag, matches in by_tag.items():
        products = sorted({item["instrument"]["product"] for item in matches})
        styles = sorted({item["strategy_context"]["entry_style"] for item in matches})
        routes = sorted({item["instrument"]["option_route"] for item in matches})
        candidate = len(matches) >= MIN_LESSON_SAMPLES and len(products) >= MIN_LESSON_PRODUCTS
        candidate_id = "lesson-" + _sha256(f"{pattern_kind}:{tag}".encode("utf-8"))[:16]
        rows.append(
            {
                "candidate_id": candidate_id,
                "pattern_kind": pattern_kind,
                "tag": tag,
                "label_zh": labels[tag],
                "support_count": len(matches),
                "cohort_size": len(cohort),
                "cohort_share": _ratio_text(len(matches), len(cohort)),
                "distinct_products": products,
                "entry_styles": styles,
                "option_routes": routes,
                "supporting_trade_ids": sorted(item["trade_id"] for item in matches),
                "status": "candidate_lesson" if candidate else "observation",
                "rule_change_authorized": False,
            }
        )
    return sorted(rows, key=lambda row: (-row["support_count"], row["tag"]))


def build_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = [
        event["payload"] for event in events if event["event_type"] == "outcome_recorded"
    ]
    classes = {name: sum(item["result_class"] == name for item in outcomes) for name in ("win", "loss", "breakeven")}
    gross = sum((Decimal(item["execution"]["gross_pnl_cny"]) for item in outcomes), Decimal("0"))
    net = sum((Decimal(item["execution"]["net_pnl_cny"]) for item in outcomes), Decimal("0"))
    commission = sum((Decimal(item["execution"]["commission_cny"]) for item in outcomes), Decimal("0"))
    slippage = sum((Decimal(item["execution"]["slippage_cny"]) for item in outcomes), Decimal("0"))
    other = sum((Decimal(item["execution"]["other_costs_cny"]) for item in outcomes), Decimal("0"))
    count = len(outcomes)
    average_net = net / Decimal(count) if count else Decimal("0")
    base: dict[str, Any] = {
        "schema_version": "trade_learning_summary.v1",
        "source": {
            "event_count": len(events),
            "registered_intent_count": sum(
                event["event_type"] == "intent_registered" for event in events
            ),
            "closed_trade_count": count,
            "head_event_hash": events[-1]["event_hash"] if events else ZERO_HASH,
            "as_of": events[-1]["recorded_at"] if events else None,
        },
        "classification_policy": {
            "basis": "verified_net_pnl_cny",
            "win": "net_pnl_cny > 0",
            "loss": "net_pnl_cny < 0",
            "breakeven": "net_pnl_cny = 0",
            "gross_formula": "(actual_exit_premium - actual_entry_premium) * quantity * contract_multiplier",
            "net_formula": "gross_pnl_cny - commission_cny - other_costs_cny",
            "slippage_accounting": "embedded_in_actual_fill_prices_and_not_deducted_twice",
        },
        "lesson_policy": {
            "minimum_supporting_trades": MIN_LESSON_SAMPLES,
            "minimum_distinct_products": MIN_LESSON_PRODUCTS,
            "candidate_is_association_not_causation": True,
            "agent_review_required": True,
            "explicit_user_confirmation_required": True,
            "separate_authorized_rule_change_required": True,
            "automatic_rule_updates": False,
        },
        "statistics": {
            "wins": classes["win"],
            "losses": classes["loss"],
            "breakevens": classes["breakeven"],
            "win_share": _ratio_text(classes["win"], count),
            "loss_share": _ratio_text(classes["loss"], count),
            "breakeven_share": _ratio_text(classes["breakeven"], count),
            "gross_pnl_cny": _decimal_text(gross),
            "net_pnl_cny": _decimal_text(net),
            "average_net_pnl_cny": _decimal_text(average_net),
            "commission_cny": _decimal_text(commission),
            "observed_slippage_cny": _decimal_text(slippage),
            "other_costs_cny": _decimal_text(other),
        },
        "patterns": {
            "winning_strengths": _pattern_rows(
                outcomes,
                result_class="win",
                tag_field="win_strength_tags",
                labels=WIN_TAGS,
                pattern_kind="winning_strength",
            ),
            "losing_causes": _pattern_rows(
                outcomes,
                result_class="loss",
                tag_field="loss_cause_tags",
                labels=LOSS_TAGS,
                pattern_kind="losing_cause",
            ),
            "breakeven_observations": _pattern_rows(
                outcomes,
                result_class="breakeven",
                tag_field="breakeven_tags",
                labels=BREAKEVEN_TAGS,
                pattern_kind="breakeven_observation",
            ),
        },
        "claim_boundary": {
            "descriptive_not_profitability_proof": True,
            "tags_are_review_attributions_not_verified_causality": True,
            "no_direction_or_trade_is_recommended": True,
            "no_chain_of_thought_is_stored": True,
            "artifact_hashes_preserve_bytes_not_source_authenticity": True,
            "full_privileged_ledger_replacement_is_not_detectable_without_external_anchor": True,
        },
    }
    base["summary_sha256"] = _sha256(_canonical_bytes(base))
    return base


def render_markdown(summary: dict[str, Any]) -> str:
    source = summary["source"]
    stats = summary["statistics"]
    policy = summary["lesson_policy"]
    lines = [
        "# 真实交易自进化摘要",
        "",
        "> 本文档由已验证的前向交易记录确定性重建。它只展示描述性统计与归因标签共现，不保存隐性推理，不证明因果或盈利能力，也不授权交易或自动修改规则。",
        "",
        "## 数据状态",
        "",
        f"- 截止事件时间：`{source['as_of'] or '暂无记录'}`",
        f"- 已登记意图：{source['registered_intent_count']}；已闭环交易：{source['closed_trade_count']}",
        f"- 源账本头哈希：`{source['head_event_hash']}`",
        "",
        "## 净收益口径",
        "",
        "- 毛收益 =（实际平仓权利金 − 实际开仓权利金）× 手数 × 合约乘数。",
        "- 净收益 = 毛收益 − 手续费 − 其他实际现金成本。",
        "- 滑点由真实成交价体现，只单独展示执行质量估计，不重复扣减。",
        "- `net_pnl_cny > 0` 为盈利，`< 0` 为亏损，`= 0` 为盈亏平衡。",
        "",
        "## 样本概览",
        "",
        "| 闭环交易 | 盈利 | 亏损 | 盈亏平衡 | 毛收益 CNY | 净收益 CNY | 平均净收益 CNY | 手续费 CNY | 观察滑点 CNY | 其他成本 CNY |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {source['closed_trade_count']} | {stats['wins']} | {stats['losses']} | {stats['breakevens']} | {stats['gross_pnl_cny']} | {stats['net_pnl_cny']} | {stats['average_net_pnl_cny']} | {stats['commission_cny']} | {stats['observed_slippage_cny']} | {stats['other_costs_cny']} |",
        "",
    ]

    def add_patterns(title: str, rows: list[dict[str, Any]]) -> None:
        lines.extend(
            [
                f"## {title}",
                "",
                "| 候选 ID | 标签 | 事实化说明 | 支持样本/同类样本 | 跨品种 | 状态 |",
                "|---|---|---|---:|---|---|",
            ]
        )
        if not rows:
            lines.append("| — | — | 暂无闭环样本 | 0/0 | — | observation |")
        for row in rows:
            products = "、".join(row["distinct_products"]) or "—"
            lines.append(
                f"| `{row['candidate_id']}` | `{row['tag']}` | {row['label_zh']} | {row['support_count']}/{row['cohort_size']} | {products} | `{row['status']}` |"
            )
        lines.append("")

    add_patterns("盈利交易的优点共性", summary["patterns"]["winning_strengths"])
    add_patterns("亏损交易的原因共性", summary["patterns"]["losing_causes"])
    add_patterns("盈亏平衡交易观察", summary["patterns"]["breakeven_observations"])
    lines.extend(
        [
            "## 候选经验治理",
            "",
            f"标签至少出现在 {policy['minimum_supporting_trades']} 笔同类交易、覆盖至少 {policy['minimum_distinct_products']} 个品种后，才成为 `candidate_lesson`。候选只代表共现，不代表因果。",
            "",
            "候选经验必须经过 Agent 对原始证据、市场状态、选择偏差、成本和反例的审查，再由用户明确确认，并在独立、已授权的规则修改任务中配套行为测试，才可能进入 Skill。脚本没有规则提升或方向推荐入口。",
            "",
            f"摘要哈希：`{summary['summary_sha256']}`",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def rebuild_outputs(
    ledger_path: Path,
    *,
    markdown_path: Path,
    summary_json_path: Path,
) -> dict[str, Any]:
    events = read_events(Path(ledger_path))
    summary = build_summary(events)
    json_bytes = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    markdown_bytes = render_markdown(summary).encode("utf-8")
    _atomic_write(Path(summary_json_path), json_bytes)
    _atomic_write(Path(markdown_path), markdown_bytes)
    return summary


def _load_input(path: Path) -> Any:
    try:
        return _json_loads(path.read_text(encoding="utf-8"), label=str(path))
    except OSError as exc:
        raise TradeLearningValidationError(f"cannot read input {path}: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("register-intent", "register immutable intent before the actual option entry"),
        ("append-outcome", "append a verified closed-trade outcome"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("--input", type=Path, required=True)
        command_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
        command_parser.add_argument(
            "--evidence-root",
            type=Path,
            default=ROOT,
            help="base directory for relative evidence paths",
        )
    verify_parser = subparsers.add_parser("verify", help="replay and verify the complete ledger")
    verify_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    rebuild_parser = subparsers.add_parser(
        "rebuild", help="deterministically rebuild Markdown and dashboard JSON"
    )
    rebuild_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    rebuild_parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    rebuild_parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY_JSON)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "register-intent":
            receipt = register_intent(
                args.ledger,
                _load_input(args.input),
                evidence_root=args.evidence_root,
            )
            output = {
                "status": receipt["status"],
                "event_type": receipt["event"]["event_type"],
                "trade_id": receipt["event"]["payload"]["trade_id"],
                "event_hash": receipt["event"]["event_hash"],
            }
        elif args.command == "append-outcome":
            receipt = append_outcome(
                args.ledger,
                _load_input(args.input),
                evidence_root=args.evidence_root,
            )
            output = {
                "status": receipt["status"],
                "event_type": receipt["event"]["event_type"],
                "trade_id": receipt["event"]["payload"]["trade_id"],
                "result_class": receipt["event"]["payload"]["result_class"],
                "event_hash": receipt["event"]["event_hash"],
            }
        elif args.command == "verify":
            events = read_events(args.ledger)
            output = {
                "status": "verified",
                "event_count": len(events),
                "head_event_hash": events[-1]["event_hash"] if events else ZERO_HASH,
            }
        else:
            summary = rebuild_outputs(
                args.ledger,
                markdown_path=args.markdown,
                summary_json_path=args.summary_json,
            )
            output = {
                "status": "rebuilt",
                "event_count": summary["source"]["event_count"],
                "closed_trade_count": summary["source"]["closed_trade_count"],
                "markdown": str(args.markdown),
                "summary_json": str(args.summary_json),
                "summary_sha256": summary["summary_sha256"],
            }
    except TradeLearningError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

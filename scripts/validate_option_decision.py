#!/usr/bin/env python3
"""Validate ``option_decision.v1`` without selecting a trading direction.

The contract deliberately separates the already-formed futures thesis from the
option expression.  This validator checks artifact bytes, timestamps, contract
identity, executable quotes, plan geometry and scenario arithmetic.  A passing
result is never an order permission and never means that a route is profitable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "option_decision.v1.schema.json"
SCHEMA_VERSION = "option_decision.v1"
VALIDATION_SCHEMA_VERSION = "option_decision_validation.v1"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
CONTRACT_RE = re.compile(r"^([A-Z]{1,3})([0-9]{4})$")
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024

TOP_LEVEL_FIELDS = {
    "schema_version",
    "observed_at",
    "decision_mode",
    "underlying_thesis",
    "option_market",
    "option_expression",
    "position",
    "economics",
    "scenario_grid",
    "rationale",
    "reassessment",
    "claim_boundary",
}

UNDERLYING_FIELDS = {
    "source_report",
    "product",
    "actual_main_contract",
    "analysis_contract",
    "directional_view",
    "underlying_action",
    "underlying_snapshot",
    "trigger",
    "target",
    "futures_structure_invalidation",
    "time_failure_reference_price",
    "latest_realization_time",
    "summary",
}

OPTION_MARKET_FIELDS = {
    "status",
    "freshness_limit_seconds",
    "quote_age_seconds",
    "selected_contract",
    "liquidity",
}

OPTION_CONTRACT_FIELDS = {
    "symbol",
    "option_type",
    "underlying_contract",
    "expiration_date",
    "expiration_at",
    "strike",
    "contract_multiplier",
    "contract_source",
    "quote",
    "analytics",
}

OPTION_EXPRESSION_FIELDS = {
    "capital_action",
    "option_route",
    "entry_timing",
    "entry_style",
    "conditional_precondition",
    "option_execution_stop",
    "one_entry_only",
    "one_full_exit_only",
    "pyramiding",
    "averaging_down",
}

ENTRY_STYLES = {
    "anticipatory_breakout",
    "confirmed_breakout",
    "trend_pullback",
    "structural_reversal",
    "pre_event_positioning",
    "post_event_continuation",
    "other_explained",
}

UNDERLYING_CASES = {
    "target",
    "option_execution_stop",
    "futures_structure_invalidation",
    "time_failure",
}
IV_CASES = {"down", "flat", "up"}


class OptionDecisionValidationError(ValueError):
    """Raised when an option decision violates the deterministic contract."""


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise OptionDecisionValidationError(f"JSON contains duplicate key: {key}")
        value[key] = item
    return value


def _json_loads(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            parse_float=Decimal,
            object_pairs_hook=_no_duplicate_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation) as exc:
        raise OptionDecisionValidationError(f"cannot parse {label} as strict JSON") from exc
    if not isinstance(value, dict):
        raise OptionDecisionValidationError(f"{label} must be a JSON object")
    return value


def _read_bytes(path: Path, *, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise OptionDecisionValidationError(f"{label} exceeds the bounded artifact size")
        return path.read_bytes()
    except OSError as exc:
        raise OptionDecisionValidationError(f"cannot read {label}: {path}") from exc


def _exact_fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OptionDecisionValidationError(f"{label} must be an object")
    keys = set(value)
    missing = expected - keys
    unexpected = keys - expected
    if missing:
        raise OptionDecisionValidationError(
            f"{label} is missing fields: {', '.join(sorted(missing))}"
        )
    if unexpected:
        raise OptionDecisionValidationError(
            f"{label} has unsupported fields: {', '.join(sorted(str(v) for v in unexpected))}"
        )
    return dict(value)


def _text(value: Any, label: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OptionDecisionValidationError(f"{label} must be non-empty text")
    rendered = value.strip()
    if len(rendered) > maximum:
        raise OptionDecisionValidationError(f"{label} is too long")
    return rendered


def _decimal(
    value: Any,
    label: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise OptionDecisionValidationError(f"{label} must be a JSON number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise OptionDecisionValidationError(f"{label} must be finite") from exc
    if not number.is_finite():
        raise OptionDecisionValidationError(f"{label} must be finite")
    if positive and number <= 0:
        raise OptionDecisionValidationError(f"{label} must be positive")
    if nonnegative and number < 0:
        raise OptionDecisionValidationError(f"{label} must be non-negative")
    return number


def _optional_decimal(
    value: Any,
    label: str,
    *,
    nonnegative: bool = False,
) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, label, nonnegative=nonnegative)


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OptionDecisionValidationError(f"{label} must be an integer")
    if value < minimum:
        raise OptionDecisionValidationError(f"{label} must be >= {minimum}")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OptionDecisionValidationError(f"{label} must be an ISO timestamp")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise OptionDecisionValidationError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OptionDecisionValidationError(f"{label} must include a timezone")
    return parsed


def _date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise OptionDecisionValidationError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise OptionDecisionValidationError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise OptionDecisionValidationError(f"{label} must use YYYY-MM-DD")
    return parsed


def _contract(value: Any, label: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise OptionDecisionValidationError(f"{label} must be a canonical PRODUCTYYMM contract")
    match = CONTRACT_RE.fullmatch(value)
    if match is None:
        raise OptionDecisionValidationError(f"{label} must be a canonical PRODUCTYYMM contract")
    month = int(match.group(2)[-2:])
    if not 1 <= month <= 12:
        raise OptionDecisionValidationError(f"{label} has an invalid delivery month")
    return value, match.group(1)


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX64_RE.fullmatch(value) is None:
        raise OptionDecisionValidationError(f"{label} must be lowercase SHA-256")
    return value


def _same_decimal(actual: Decimal, expected: Decimal, label: str) -> None:
    tolerance = Decimal("0.00000001")
    if abs(actual - expected) > tolerance:
        raise OptionDecisionValidationError(
            f"{label} arithmetic mismatch: expected {expected}, got {actual}"
        )


def _source(value: Any, label: str, *, observed_at: datetime) -> dict[str, Any]:
    source = _exact_fields(
        value,
        {"url", "retrieved_at", "content_sha256", "locator"},
        label,
    )
    url = _text(source["url"], f"{label}.url", maximum=2048)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OptionDecisionValidationError(f"{label}.url must be an absolute HTTP(S) URL")
    retrieved = _timestamp(source["retrieved_at"], f"{label}.retrieved_at")
    if retrieved > observed_at:
        raise OptionDecisionValidationError(f"{label}.retrieved_at cannot be after observed_at")
    _sha256(source["content_sha256"], f"{label}.content_sha256")
    _text(source["locator"], f"{label}.locator")
    return source


def _underlying_level(
    value: Any,
    label: str,
    *,
    allowed_conditions: set[str],
) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _exact_fields(value, {"condition", "price", "source_ref"}, label)
    if item["condition"] not in allowed_conditions:
        raise OptionDecisionValidationError(f"{label}.condition is unsupported")
    _decimal(item["price"], f"{label}.price", positive=True)
    _text(item["source_ref"], f"{label}.source_ref")
    return item


def _load_schema() -> tuple[dict[str, Any], str]:
    content = _read_bytes(SCHEMA_PATH, label="option decision schema")
    schema = _json_loads(content, label="option decision schema")
    properties = schema.get("properties")
    if (
        schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or not isinstance(properties, Mapping)
        or not isinstance(properties.get("schema_version"), Mapping)
        or properties["schema_version"].get("const") != SCHEMA_VERSION
    ):
        raise OptionDecisionValidationError("option decision schema identity is invalid")
    return schema, hashlib.sha256(content).hexdigest()


def _source_report(
    value: Any,
    *,
    artifact_root: Path,
    observed_at: datetime,
    expected_product: str,
    expected_actual_main: str | None,
    expected_analysis_contract: str,
    expected_action: str,
    expected_directional_view: str,
    expected_snapshot: Mapping[str, Any],
    expected_trigger: Mapping[str, Any] | None,
    expected_target: Mapping[str, Any] | None,
    expected_invalidation: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    descriptor = _exact_fields(value, {"path", "sha256", "schema_version"}, "source_report")
    if descriptor["schema_version"] != "futures_main_report.v3":
        raise OptionDecisionValidationError("source_report.schema_version must be futures_main_report.v3")
    relative_text = _text(descriptor["path"], "source_report.path", maximum=1024)
    relative = Path(relative_text)
    if relative.is_absolute():
        raise OptionDecisionValidationError("source_report.path must be relative to the decision file")
    root = artifact_root.resolve()
    report_path = (root / relative).resolve()
    try:
        report_path.relative_to(root)
    except ValueError as exc:
        raise OptionDecisionValidationError("source_report.path escapes the decision directory") from exc
    content = _read_bytes(report_path, label="underlying futures report")
    digest = hashlib.sha256(content).hexdigest()
    if digest != _sha256(descriptor["sha256"], "source_report.sha256"):
        raise OptionDecisionValidationError("source_report SHA-256 mismatch")
    report = _json_loads(content, label="underlying futures report")
    if report.get("schema_version") != "futures_main_report.v3":
        raise OptionDecisionValidationError("underlying report is not futures_main_report.v3")
    report_observed = _timestamp(report.get("observed_at"), "underlying report observed_at")
    if report_observed > observed_at:
        raise OptionDecisionValidationError("underlying report cannot be observed after option decision")
    target = report.get("target")
    if not isinstance(target, Mapping):
        raise OptionDecisionValidationError("underlying report target is missing")
    if target.get("product") != expected_product:
        raise OptionDecisionValidationError("underlying thesis product conflicts with source report")
    if target.get("analysis_contract") != expected_analysis_contract:
        raise OptionDecisionValidationError(
            "underlying thesis analysis_contract conflicts with source report"
        )
    if target.get("actual_main_contract") != expected_actual_main:
        raise OptionDecisionValidationError(
            "underlying thesis actual_main_contract conflicts with source report"
        )
    if report.get("action") != expected_action:
        raise OptionDecisionValidationError("underlying_action conflicts with source report action")
    judgment = report.get("agent_judgment")
    if not isinstance(judgment, Mapping) or judgment.get("directional_view") != expected_directional_view:
        raise OptionDecisionValidationError(
            "underlying directional_view conflicts with source report agent_judgment"
        )
    market_data = report.get("market_data")
    report_quote = market_data.get("quote") if isinstance(market_data, Mapping) else None
    if not isinstance(report_quote, Mapping):
        raise OptionDecisionValidationError("underlying report market_data.quote is missing")
    if report_quote.get("contract") != expected_analysis_contract:
        raise OptionDecisionValidationError("underlying snapshot contract conflicts with source report")
    _same_decimal(
        _decimal(report_quote.get("price"), "underlying report market_data.quote.price", positive=True),
        _decimal(expected_snapshot.get("price"), "underlying snapshot price", positive=True),
        "underlying snapshot price",
    )
    if _timestamp(
        report_quote.get("observed_at"), "underlying report market_data.quote.observed_at"
    ) != _timestamp(expected_snapshot.get("observed_at"), "underlying snapshot observed_at"):
        raise OptionDecisionValidationError(
            "underlying snapshot observed_at conflicts with source report"
        )

    plan = report.get("point_plan")
    if any(item is not None for item in (expected_trigger, expected_target, expected_invalidation)):
        if not isinstance(plan, Mapping):
            raise OptionDecisionValidationError(
                "underlying thesis levels require a hash-bound source report point_plan"
            )
        for name, expected in (
            ("trigger", expected_trigger),
            ("target", expected_target),
            ("invalidation", expected_invalidation),
        ):
            if expected is None:
                continue
            actual = plan.get(name)
            if not isinstance(actual, Mapping):
                raise OptionDecisionValidationError(
                    f"underlying thesis {name} is missing from source report point_plan"
                )
            _same_decimal(
                _decimal(actual.get("price"), f"source report point_plan.{name}.price", positive=True),
                _decimal(expected.get("price"), f"underlying thesis {name}.price", positive=True),
                f"underlying thesis {name}.price",
            )
            if name != "target" and actual.get("condition") != expected.get("condition"):
                raise OptionDecisionValidationError(
                    f"underlying thesis {name}.condition conflicts with source report"
                )
    return report, digest


def _validate_underlying(
    value: Any,
    *,
    observed_at: datetime,
    artifact_root: Path,
) -> dict[str, Any]:
    thesis = _exact_fields(value, UNDERLYING_FIELDS, "underlying_thesis")
    product = _text(thesis["product"], "underlying_thesis.product", maximum=3)
    if re.fullmatch(r"[A-Z]{1,3}", product) is None:
        raise OptionDecisionValidationError("underlying_thesis.product must be uppercase letters")
    analysis_contract, analysis_product = _contract(
        thesis["analysis_contract"], "underlying_thesis.analysis_contract"
    )
    if analysis_product != product:
        raise OptionDecisionValidationError("analysis_contract product conflicts with product")
    actual_main = thesis["actual_main_contract"]
    if actual_main is not None:
        _, actual_product = _contract(actual_main, "underlying_thesis.actual_main_contract")
        if actual_product != product:
            raise OptionDecisionValidationError("actual_main_contract product conflicts with product")
    if thesis["directional_view"] not in {"long", "short", "neutral", "uncertain"}:
        raise OptionDecisionValidationError("underlying_thesis.directional_view is unsupported")
    if thesis["underlying_action"] not in {"long", "short", "wait"}:
        raise OptionDecisionValidationError("underlying_thesis.underlying_action is unsupported")

    snapshot = _exact_fields(
        thesis["underlying_snapshot"],
        {"contract", "observed_at", "price", "source"},
        "underlying_thesis.underlying_snapshot",
    )
    if snapshot["contract"] != analysis_contract:
        raise OptionDecisionValidationError("underlying snapshot must bind analysis_contract")
    snapshot_time = _timestamp(
        snapshot["observed_at"], "underlying_thesis.underlying_snapshot.observed_at"
    )
    if snapshot_time > observed_at:
        raise OptionDecisionValidationError("underlying snapshot cannot be from the future")
    _decimal(snapshot["price"], "underlying_thesis.underlying_snapshot.price", positive=True)
    snapshot_source = _source(
        snapshot["source"],
        "underlying_thesis.underlying_snapshot.source",
        observed_at=observed_at,
    )
    if _timestamp(
        snapshot_source["retrieved_at"],
        "underlying_thesis.underlying_snapshot.source.retrieved_at",
    ) < snapshot_time:
        raise OptionDecisionValidationError(
            "underlying source retrieval cannot precede the quoted observation"
        )

    trigger = _underlying_level(
        thesis["trigger"],
        "underlying_thesis.trigger",
        allowed_conditions={
            "underlying_at_or_above",
            "underlying_at_or_below",
            "completed_close_at_or_above",
            "completed_close_at_or_below",
        },
    )
    target = _underlying_level(
        thesis["target"],
        "underlying_thesis.target",
        allowed_conditions={"underlying_at_or_above", "underlying_at_or_below"},
    )
    invalidation = thesis["futures_structure_invalidation"]
    if invalidation is not None:
        invalidation = _exact_fields(
            invalidation,
            {"condition", "price", "timeframe", "source_ref"},
            "underlying_thesis.futures_structure_invalidation",
        )
        if invalidation["condition"] not in {
            "completed_close_at_or_above",
            "completed_close_at_or_below",
        }:
            raise OptionDecisionValidationError(
                "futures_structure_invalidation must use a completed-close condition"
            )
        _decimal(
            invalidation["price"],
            "underlying_thesis.futures_structure_invalidation.price",
            positive=True,
        )
        _text(
            invalidation["timeframe"],
            "underlying_thesis.futures_structure_invalidation.timeframe",
        )
        _text(
            invalidation["source_ref"],
            "underlying_thesis.futures_structure_invalidation.source_ref",
        )
    time_failure_price = thesis["time_failure_reference_price"]
    if time_failure_price is not None:
        _decimal(
            time_failure_price,
            "underlying_thesis.time_failure_reference_price",
            positive=True,
        )
    latest = _timestamp(
        thesis["latest_realization_time"],
        "underlying_thesis.latest_realization_time",
    )
    _text(thesis["summary"], "underlying_thesis.summary")

    report, report_sha = _source_report(
        thesis["source_report"],
        artifact_root=artifact_root,
        observed_at=observed_at,
        expected_product=product,
        expected_actual_main=actual_main,
        expected_analysis_contract=analysis_contract,
        expected_action=thesis["underlying_action"],
        expected_directional_view=thesis["directional_view"],
        expected_snapshot=snapshot,
        expected_trigger=trigger,
        expected_target=target,
        expected_invalidation=invalidation,
    )
    return {
        "value": thesis,
        "product": product,
        "analysis_contract": analysis_contract,
        "underlying_price": _decimal(
            snapshot["price"], "underlying_thesis.underlying_snapshot.price", positive=True
        ),
        "trigger": trigger,
        "target": target,
        "invalidation": invalidation,
        "time_failure_price": (
            None
            if time_failure_price is None
            else _decimal(
                time_failure_price,
                "underlying_thesis.time_failure_reference_price",
                positive=True,
            )
        ),
        "latest": latest,
        "report": report,
        "report_sha256": report_sha,
    }


def _validate_option_contract(
    value: Any,
    *,
    observed_at: datetime,
    analysis_contract: str,
    latest_realization_time: datetime,
) -> dict[str, Any]:
    contract = _exact_fields(value, OPTION_CONTRACT_FIELDS, "option_market.selected_contract")
    symbol = _text(contract["symbol"], "option_market.selected_contract.symbol", maximum=128)
    option_type = contract["option_type"]
    if option_type not in {"call", "put"}:
        raise OptionDecisionValidationError("selected option_type must be call or put")
    underlying, _ = _contract(
        contract["underlying_contract"],
        "option_market.selected_contract.underlying_contract",
    )
    if underlying != analysis_contract:
        raise OptionDecisionValidationError(
            "selected option must have the same underlying month as analysis_contract"
        )
    expiration = _date(
        contract["expiration_date"], "option_market.selected_contract.expiration_date"
    )
    expiration_at = _timestamp(
        contract["expiration_at"], "option_market.selected_contract.expiration_at"
    )
    if expiration_at.date() != expiration:
        raise OptionDecisionValidationError(
            "selected option expiration_date conflicts with expiration_at"
        )
    if expiration_at <= observed_at:
        raise OptionDecisionValidationError("selected option is already expired")
    _decimal(contract["strike"], "option_market.selected_contract.strike", positive=True)
    multiplier = _decimal(
        contract["contract_multiplier"],
        "option_market.selected_contract.contract_multiplier",
        positive=True,
    )
    _source(
        contract["contract_source"],
        "option_market.selected_contract.contract_source",
        observed_at=observed_at,
    )

    quote = _exact_fields(
        contract["quote"],
        {
            "observed_at",
            "bid",
            "ask",
            "last",
            "volume",
            "open_interest",
            "tick_size",
            "source",
        },
        "option_market.selected_contract.quote",
    )
    quote_time = _timestamp(
        quote["observed_at"], "option_market.selected_contract.quote.observed_at"
    )
    if quote_time > observed_at:
        raise OptionDecisionValidationError("option quote cannot be from the future")
    bid = _optional_decimal(
        quote["bid"], "option_market.selected_contract.quote.bid", nonnegative=True
    )
    ask = _optional_decimal(
        quote["ask"], "option_market.selected_contract.quote.ask", nonnegative=True
    )
    _optional_decimal(
        quote["last"], "option_market.selected_contract.quote.last", nonnegative=True
    )
    if bid is not None and ask is not None and ask < bid:
        raise OptionDecisionValidationError("option bid/ask is inverted")
    volume = _integer(
        quote["volume"], "option_market.selected_contract.quote.volume", minimum=0
    )
    open_interest = _integer(
        quote["open_interest"],
        "option_market.selected_contract.quote.open_interest",
        minimum=0,
    )
    tick_size = _decimal(
        quote["tick_size"], "option_market.selected_contract.quote.tick_size", positive=True
    )
    quote_source = _source(
        quote["source"],
        "option_market.selected_contract.quote.source",
        observed_at=observed_at,
    )
    if _timestamp(
        quote_source["retrieved_at"],
        "option_market.selected_contract.quote.source.retrieved_at",
    ) < quote_time:
        raise OptionDecisionValidationError("option quote source retrieval precedes quote observation")

    analytics_value = contract["analytics"]
    analytics: dict[str, Any] | None = None
    current_iv: Decimal | None = None
    if analytics_value is not None:
        analytics = _exact_fields(
            analytics_value,
            {
                "observed_at",
                "implied_volatility",
                "delta",
                "gamma",
                "theta",
                "vega",
                "method",
                "source",
            },
            "option_market.selected_contract.analytics",
        )
        analytics_time = _timestamp(
            analytics["observed_at"],
            "option_market.selected_contract.analytics.observed_at",
        )
        if analytics_time > observed_at:
            raise OptionDecisionValidationError("option analytics cannot be from the future")
        current_iv = _decimal(
            analytics["implied_volatility"],
            "option_market.selected_contract.analytics.implied_volatility",
            positive=True,
        )
        delta = _decimal(
            analytics["delta"], "option_market.selected_contract.analytics.delta"
        )
        if not Decimal("-1") <= delta <= Decimal("1"):
            raise OptionDecisionValidationError("option delta must be between -1 and 1")
        if option_type == "call" and not Decimal("0") <= delta <= Decimal("1"):
            raise OptionDecisionValidationError("call delta must be between 0 and 1")
        if option_type == "put" and not Decimal("-1") <= delta <= Decimal("0"):
            raise OptionDecisionValidationError("put delta must be between -1 and 0")
        _decimal(
            analytics["gamma"],
            "option_market.selected_contract.analytics.gamma",
            nonnegative=True,
        )
        _decimal(analytics["theta"], "option_market.selected_contract.analytics.theta")
        _decimal(
            analytics["vega"],
            "option_market.selected_contract.analytics.vega",
            nonnegative=True,
        )
        _text(analytics["method"], "option_market.selected_contract.analytics.method")
        analytics_source = _source(
            analytics["source"],
            "option_market.selected_contract.analytics.source",
            observed_at=observed_at,
        )
        if _timestamp(
            analytics_source["retrieved_at"],
            "option_market.selected_contract.analytics.source.retrieved_at",
        ) < analytics_time:
            raise OptionDecisionValidationError(
                "option analytics source retrieval precedes analytics observation"
            )

    return {
        "value": contract,
        "symbol": symbol,
        "option_type": option_type,
        "underlying_contract": underlying,
        "expiration": expiration,
        "expiration_at": expiration_at,
        "expiration_covers_latest": expiration_at >= latest_realization_time,
        "multiplier": multiplier,
        "quote_time": quote_time,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "open_interest": open_interest,
        "tick_size": tick_size,
        "analytics": analytics,
        "current_iv": current_iv,
    }


def _validate_liquidity(
    value: Any,
    *,
    selected: dict[str, Any] | None,
    market_status: str,
) -> dict[str, Any]:
    liquidity = _exact_fields(
        value, {"status", "spread", "spread_ticks", "reason"}, "option_market.liquidity"
    )
    status = liquidity["status"]
    if status not in {"executable", "incomplete", "unobservable"}:
        raise OptionDecisionValidationError("option_market.liquidity.status is unsupported")
    _text(liquidity["reason"], "option_market.liquidity.reason")
    spread = _optional_decimal(
        liquidity["spread"], "option_market.liquidity.spread", nonnegative=True
    )
    spread_ticks = _optional_decimal(
        liquidity["spread_ticks"],
        "option_market.liquidity.spread_ticks",
        nonnegative=True,
    )
    if selected is None:
        if status != "unobservable" or spread is not None or spread_ticks is not None:
            raise OptionDecisionValidationError(
                "missing selected_contract requires unobservable liquidity and null spread"
            )
        return liquidity

    bid = selected["bid"]
    ask = selected["ask"]
    if bid is None or ask is None:
        if spread is not None or spread_ticks is not None:
            raise OptionDecisionValidationError("missing bid/ask requires null spread fields")
        if status == "executable":
            raise OptionDecisionValidationError("executable liquidity requires bid and ask")
    else:
        expected_spread = ask - bid
        expected_ticks = expected_spread / selected["tick_size"]
        if spread is None or spread_ticks is None:
            raise OptionDecisionValidationError("observable bid/ask requires spread and spread_ticks")
        _same_decimal(spread, expected_spread, "option_market.liquidity.spread")
        _same_decimal(spread_ticks, expected_ticks, "option_market.liquidity.spread_ticks")

    if status == "unobservable":
        raise OptionDecisionValidationError(
            "a selected contract must use incomplete or executable liquidity"
        )
    if status == "executable" and not (
        market_status == "fresh"
        and bid is not None
        and bid > 0
        and ask is not None
        and ask > 0
        and selected["volume"] > 0
        and selected["open_interest"] > 0
    ):
        raise OptionDecisionValidationError(
            "executable liquidity requires fresh positive bid/ask, volume and open interest"
        )
    return liquidity


def _validate_option_market(
    value: Any,
    *,
    observed_at: datetime,
    analysis_contract: str,
    latest_realization_time: datetime,
) -> dict[str, Any]:
    market = _exact_fields(value, OPTION_MARKET_FIELDS, "option_market")
    status = market["status"]
    if status not in {"fresh", "stale", "unobservable"}:
        raise OptionDecisionValidationError("option_market.status is unsupported")
    freshness_limit = _integer(
        market["freshness_limit_seconds"],
        "option_market.freshness_limit_seconds",
        minimum=1,
    )
    selected_value = market["selected_contract"]
    selected = (
        None
        if selected_value is None
        else _validate_option_contract(
            selected_value,
            observed_at=observed_at,
            analysis_contract=analysis_contract,
            latest_realization_time=latest_realization_time,
        )
    )
    quote_age = _optional_decimal(
        market["quote_age_seconds"], "option_market.quote_age_seconds", nonnegative=True
    )
    if selected is None:
        if status != "unobservable" or quote_age is not None:
            raise OptionDecisionValidationError(
                "no selected contract requires unobservable market and null quote_age_seconds"
            )
    else:
        actual_age = Decimal(str((observed_at - selected["quote_time"]).total_seconds()))
        if actual_age < 0:
            raise OptionDecisionValidationError("option quote age cannot be negative")
        if quote_age is None:
            raise OptionDecisionValidationError("selected contract requires quote_age_seconds")
        _same_decimal(quote_age, actual_age, "option_market.quote_age_seconds")
        expected_status = "fresh" if actual_age <= freshness_limit else "stale"
        if status != expected_status:
            raise OptionDecisionValidationError(
                f"option_market.status must be {expected_status} for the declared quote age"
            )
    liquidity = _validate_liquidity(
        market["liquidity"], selected=selected, market_status=status
    )
    return {
        "value": market,
        "status": status,
        "freshness_limit": freshness_limit,
        "selected": selected,
        "liquidity": liquidity,
    }


def _validate_expression(value: Any) -> dict[str, Any]:
    expression = _exact_fields(value, OPTION_EXPRESSION_FIELDS, "option_expression")
    if expression["capital_action"] not in {"buy_to_open", "wait", "hold", "close"}:
        raise OptionDecisionValidationError("option_expression.capital_action is unsupported")
    if expression["option_route"] not in {"long_call", "long_put", "wait"}:
        raise OptionDecisionValidationError("option_expression.option_route is unsupported")
    if expression["entry_timing"] not in {"now", "conditional", "wait", "not_applicable"}:
        raise OptionDecisionValidationError("option_expression.entry_timing is unsupported")
    if expression["entry_style"] not in ENTRY_STYLES | {"wait", "position_review"}:
        raise OptionDecisionValidationError("option_expression.entry_style is unsupported")
    precondition = expression["conditional_precondition"]
    if precondition is not None:
        precondition = _exact_fields(
            precondition,
            {
                "condition",
                "requires_fresh_quote",
                "requires_same_underlying_contract",
                "requires_executable_bid_ask",
            },
            "option_expression.conditional_precondition",
        )
        _text(precondition["condition"], "option_expression.conditional_precondition.condition")
        for field in (
            "requires_fresh_quote",
            "requires_same_underlying_contract",
            "requires_executable_bid_ask",
        ):
            if precondition[field] is not True:
                raise OptionDecisionValidationError(
                    f"option_expression.conditional_precondition.{field} must be true"
                )
    stop = _underlying_level(
        expression["option_execution_stop"],
        "option_expression.option_execution_stop",
        allowed_conditions={"underlying_at_or_above", "underlying_at_or_below"},
    )
    if expression["one_entry_only"] is not True:
        raise OptionDecisionValidationError("one_entry_only must be true")
    if expression["one_full_exit_only"] is not True:
        raise OptionDecisionValidationError("one_full_exit_only must be true")
    if expression["pyramiding"] is not False:
        raise OptionDecisionValidationError("pyramiding must be false")
    if expression["averaging_down"] is not False:
        raise OptionDecisionValidationError("averaging_down must be false")
    return {"value": expression, "precondition": precondition, "stop": stop}


def _validate_position(
    value: Any,
    *,
    observed_at: datetime,
    analysis_contract: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    position = _exact_fields(
        value,
        {
            "side",
            "contract",
            "option_type",
            "underlying_contract",
            "quantity",
            "average_entry_premium",
            "opened_at",
            "entry_fees_total",
            "source",
        },
        "position",
    )
    if position["side"] != "long":
        raise OptionDecisionValidationError("position.side must be long; net short options are forbidden")
    contract = _text(position["contract"], "position.contract", maximum=128)
    if position["option_type"] not in {"call", "put"}:
        raise OptionDecisionValidationError("position.option_type is unsupported")
    underlying, _ = _contract(position["underlying_contract"], "position.underlying_contract")
    if underlying != analysis_contract:
        raise OptionDecisionValidationError("position underlying must equal analysis_contract")
    quantity = _integer(position["quantity"], "position.quantity", minimum=1)
    average = _decimal(
        position["average_entry_premium"],
        "position.average_entry_premium",
        nonnegative=True,
    )
    opened_at = _timestamp(position["opened_at"], "position.opened_at")
    if opened_at > observed_at:
        raise OptionDecisionValidationError("position.opened_at cannot be in the future")
    entry_fees = _decimal(
        position["entry_fees_total"], "position.entry_fees_total", nonnegative=True
    )
    _source(position["source"], "position.source", observed_at=observed_at)
    return {
        "value": position,
        "contract": contract,
        "option_type": position["option_type"],
        "underlying_contract": underlying,
        "quantity": quantity,
        "average": average,
        "entry_fees": entry_fees,
    }


def _validate_economics(
    value: Any,
    *,
    selected: dict[str, Any] | None,
    position: dict[str, Any] | None,
    decision_mode: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    economics = _exact_fields(
        value,
        {
            "currency",
            "quantity",
            "premium_basis",
            "premium_per_unit",
            "contract_multiplier",
            "premium_notional",
            "entry_fees_total",
            "exit_fees_total",
            "entry_slippage_total",
            "exit_slippage_total",
            "total_cost_basis",
        },
        "economics",
    )
    if economics["currency"] != "CNY":
        raise OptionDecisionValidationError("economics.currency must be CNY")
    quantity = _integer(economics["quantity"], "economics.quantity", minimum=1)
    if economics["premium_basis"] not in {"current_ask", "position_average"}:
        raise OptionDecisionValidationError("economics.premium_basis is unsupported")
    premium = _decimal(
        economics["premium_per_unit"], "economics.premium_per_unit", nonnegative=True
    )
    multiplier = _decimal(
        economics["contract_multiplier"], "economics.contract_multiplier", positive=True
    )
    premium_notional = _decimal(
        economics["premium_notional"], "economics.premium_notional", nonnegative=True
    )
    entry_fees = _decimal(
        economics["entry_fees_total"], "economics.entry_fees_total", nonnegative=True
    )
    exit_fees = _decimal(
        economics["exit_fees_total"], "economics.exit_fees_total", nonnegative=True
    )
    entry_slippage = _decimal(
        economics["entry_slippage_total"],
        "economics.entry_slippage_total",
        nonnegative=True,
    )
    exit_slippage = _decimal(
        economics["exit_slippage_total"],
        "economics.exit_slippage_total",
        nonnegative=True,
    )
    total_cost_basis = _decimal(
        economics["total_cost_basis"], "economics.total_cost_basis", nonnegative=True
    )
    _same_decimal(
        premium_notional,
        premium * multiplier * quantity,
        "economics.premium_notional",
    )
    _same_decimal(
        total_cost_basis,
        premium_notional + entry_fees + entry_slippage,
        "economics.total_cost_basis",
    )
    if selected is None:
        raise OptionDecisionValidationError("economics requires a selected option contract")
    _same_decimal(multiplier, selected["multiplier"], "economics.contract_multiplier")
    if decision_mode == "new_entry":
        if economics["premium_basis"] != "current_ask":
            raise OptionDecisionValidationError("new entry economics must use current_ask")
        if selected["ask"] is None:
            raise OptionDecisionValidationError("new entry economics requires an observable ask")
        _same_decimal(premium, selected["ask"], "economics.premium_per_unit")
    else:
        if position is None:
            raise OptionDecisionValidationError("position economics requires a position")
        if economics["premium_basis"] != "position_average":
            raise OptionDecisionValidationError("position review economics must use position_average")
        if quantity != position["quantity"]:
            raise OptionDecisionValidationError("economics.quantity conflicts with position.quantity")
        _same_decimal(premium, position["average"], "economics.premium_per_unit")
        _same_decimal(entry_fees, position["entry_fees"], "economics.entry_fees_total")
    return {
        "value": economics,
        "quantity": quantity,
        "premium": premium,
        "multiplier": multiplier,
        "entry_fees": entry_fees,
        "exit_fees": exit_fees,
        "entry_slippage": entry_slippage,
        "exit_slippage": exit_slippage,
    }


def _validate_rationale(value: Any, *, entry_style: str) -> dict[str, Any]:
    rationale = _exact_fields(
        value,
        {
            "decision_method",
            "action_reason",
            "why_not_alternative",
            "strongest_counterevidence",
            "why_enter_before_confirmation",
        },
        "rationale",
    )
    if rationale["decision_method"] != "agent_discretionary_synthesis":
        raise OptionDecisionValidationError(
            "rationale.decision_method must preserve Agent decision ownership"
        )
    for field in ("action_reason", "why_not_alternative", "strongest_counterevidence"):
        _text(rationale[field], f"rationale.{field}")
    anticipatory = rationale["why_enter_before_confirmation"]
    if entry_style == "anticipatory_breakout":
        _text(anticipatory, "rationale.why_enter_before_confirmation")
    elif anticipatory is not None:
        raise OptionDecisionValidationError(
            "why_enter_before_confirmation is only valid for anticipatory_breakout"
        )
    return rationale


def _validate_reassessment(
    value: Any,
    *,
    observed_at: datetime,
    latest_realization_time: datetime,
) -> dict[str, Any] | None:
    if value is None:
        return None
    reassessment = _exact_fields(value, {"reassess_by", "update_triggers"}, "reassessment")
    reassess_by = _timestamp(reassessment["reassess_by"], "reassessment.reassess_by")
    if reassess_by <= observed_at:
        raise OptionDecisionValidationError("reassessment.reassess_by must be in the future")
    if reassess_by > latest_realization_time:
        raise OptionDecisionValidationError(
            "reassessment.reassess_by cannot be after latest_realization_time"
        )
    triggers = reassessment["update_triggers"]
    if not isinstance(triggers, list) or not triggers:
        raise OptionDecisionValidationError("reassessment.update_triggers must be non-empty")
    for index, trigger in enumerate(triggers):
        _text(trigger, f"reassessment.update_triggers[{index}]")
    return reassessment


def _validate_claim_boundary(value: Any) -> dict[str, Any]:
    boundary = _exact_fields(
        value,
        {
            "validator_selected_direction",
            "validation_is_trade_permission",
            "automatic_ordering",
            "order_authorized",
        },
        "claim_boundary",
    )
    for field in boundary:
        if boundary[field] is not False:
            raise OptionDecisionValidationError(f"claim_boundary.{field} must be false")
    return boundary


def _route_option_type(route: str) -> str | None:
    return {"long_call": "call", "long_put": "put", "wait": None}[route]


def _validate_directional_geometry(
    *,
    route: str,
    underlying_price: Decimal,
    trigger: dict[str, Any] | None,
    target: dict[str, Any],
    option_stop: dict[str, Any],
    futures_invalidation: dict[str, Any],
) -> None:
    target_price = _decimal(target["price"], "underlying_thesis.target.price", positive=True)
    stop_price = _decimal(
        option_stop["price"], "option_expression.option_execution_stop.price", positive=True
    )
    invalidation_price = _decimal(
        futures_invalidation["price"],
        "underlying_thesis.futures_structure_invalidation.price",
        positive=True,
    )
    if route == "long_call":
        if target["condition"] != "underlying_at_or_above":
            raise OptionDecisionValidationError("long_call target must be underlying_at_or_above")
        if option_stop["condition"] != "underlying_at_or_below":
            raise OptionDecisionValidationError(
                "long_call option_execution_stop must be underlying_at_or_below"
            )
        if futures_invalidation["condition"] != "completed_close_at_or_below":
            raise OptionDecisionValidationError(
                "long_call futures_structure_invalidation must close at or below"
            )
        if not target_price > underlying_price > stop_price:
            raise OptionDecisionValidationError(
                "long_call requires target > current underlying > option_execution_stop"
            )
        if not underlying_price > invalidation_price:
            raise OptionDecisionValidationError(
                "long_call current underlying must be above futures_structure_invalidation"
            )
        if trigger is not None and trigger["condition"] not in {
            "underlying_at_or_above",
            "completed_close_at_or_above",
        }:
            raise OptionDecisionValidationError("long_call trigger must use an at-or-above condition")
    else:
        if target["condition"] != "underlying_at_or_below":
            raise OptionDecisionValidationError("long_put target must be underlying_at_or_below")
        if option_stop["condition"] != "underlying_at_or_above":
            raise OptionDecisionValidationError(
                "long_put option_execution_stop must be underlying_at_or_above"
            )
        if futures_invalidation["condition"] != "completed_close_at_or_above":
            raise OptionDecisionValidationError(
                "long_put futures_structure_invalidation must close at or above"
            )
        if not target_price < underlying_price < stop_price:
            raise OptionDecisionValidationError(
                "long_put requires target < current underlying < option_execution_stop"
            )
        if not underlying_price < invalidation_price:
            raise OptionDecisionValidationError(
                "long_put current underlying must be below futures_structure_invalidation"
            )
        if trigger is not None and trigger["condition"] not in {
            "underlying_at_or_below",
            "completed_close_at_or_below",
        }:
            raise OptionDecisionValidationError("long_put trigger must use an at-or-below condition")


def _validate_scenarios(
    value: Any,
    *,
    observed_at: datetime,
    latest_realization_time: datetime,
    target: dict[str, Any],
    option_stop: dict[str, Any],
    futures_invalidation: dict[str, Any],
    time_failure_price: Decimal,
    current_iv: Decimal,
    economics: dict[str, Any],
    required: bool,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise OptionDecisionValidationError("scenario_grid must be an array")
    if not value:
        if required:
            raise OptionDecisionValidationError(
                "buy_to_open and hold require the complete 4 x 3 scenario grid"
            )
        return []
    if len(value) != len(UNDERLYING_CASES) * len(IV_CASES):
        raise OptionDecisionValidationError("scenario_grid must contain exactly 12 rows")

    expected_prices = {
        "target": _decimal(target["price"], "underlying_thesis.target.price", positive=True),
        "option_execution_stop": _decimal(
            option_stop["price"],
            "option_expression.option_execution_stop.price",
            positive=True,
        ),
        "futures_structure_invalidation": _decimal(
            futures_invalidation["price"],
            "underlying_thesis.futures_structure_invalidation.price",
            positive=True,
        ),
        "time_failure": time_failure_price,
    }
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(value):
        label = f"scenario_grid[{index}]"
        row = _exact_fields(
            raw,
            {
                "underlying_case",
                "iv_case",
                "evaluation_time",
                "underlying_price",
                "implied_volatility",
                "estimated_option_value_per_unit",
                "estimated_net_pnl",
                "pricing_method",
                "model_or_source_ref",
            },
            label,
        )
        underlying_case = row["underlying_case"]
        iv_case = row["iv_case"]
        if underlying_case not in UNDERLYING_CASES or iv_case not in IV_CASES:
            raise OptionDecisionValidationError(f"{label} has an unsupported scenario case")
        key = (underlying_case, iv_case)
        if key in rows:
            raise OptionDecisionValidationError(f"scenario_grid duplicates {key}")
        evaluation_time = _timestamp(row["evaluation_time"], f"{label}.evaluation_time")
        if evaluation_time < observed_at or evaluation_time > latest_realization_time:
            raise OptionDecisionValidationError(
                f"{label}.evaluation_time must be within the decision horizon"
            )
        if underlying_case == "time_failure" and evaluation_time != latest_realization_time:
            raise OptionDecisionValidationError(
                "time_failure scenarios must be evaluated at latest_realization_time"
            )
        underlying_price = _decimal(row["underlying_price"], f"{label}.underlying_price", positive=True)
        _same_decimal(
            underlying_price,
            expected_prices[underlying_case],
            f"{label}.underlying_price",
        )
        iv = _decimal(row["implied_volatility"], f"{label}.implied_volatility", positive=True)
        if iv_case == "down" and not iv < current_iv:
            raise OptionDecisionValidationError(f"{label} down IV must be below current IV")
        if iv_case == "flat":
            _same_decimal(iv, current_iv, f"{label}.implied_volatility")
        if iv_case == "up" and not iv > current_iv:
            raise OptionDecisionValidationError(f"{label} up IV must be above current IV")
        option_value = _decimal(
            row["estimated_option_value_per_unit"],
            f"{label}.estimated_option_value_per_unit",
            nonnegative=True,
        )
        reported_net = _decimal(row["estimated_net_pnl"], f"{label}.estimated_net_pnl")
        expected_net = (
            (option_value - economics["premium"])
            * economics["multiplier"]
            * economics["quantity"]
            - economics["entry_fees"]
            - economics["exit_fees"]
            - economics["entry_slippage"]
            - economics["exit_slippage"]
        )
        _same_decimal(reported_net, expected_net, f"{label}.estimated_net_pnl")
        _text(row["pricing_method"], f"{label}.pricing_method")
        _text(row["model_or_source_ref"], f"{label}.model_or_source_ref")
        rows[key] = {"value": row, "iv": iv, "option_value": option_value}

    expected_keys = {(underlying, iv) for underlying in UNDERLYING_CASES for iv in IV_CASES}
    if set(rows) != expected_keys:
        raise OptionDecisionValidationError("scenario_grid is not a complete 4 x 3 grid")
    for underlying_case in UNDERLYING_CASES:
        down = rows[(underlying_case, "down")]["option_value"]
        flat = rows[(underlying_case, "flat")]["option_value"]
        up = rows[(underlying_case, "up")]["option_value"]
        if not down <= flat <= up:
            raise OptionDecisionValidationError(
                f"{underlying_case} option values must be non-decreasing as IV rises"
            )
    return [dict(row) for row in value]


def validate_option_decision(
    decision: Mapping[str, Any],
    *,
    artifact_root: Path,
) -> dict[str, Any]:
    """Validate one decision and return non-authoritative consistency metadata."""

    _, schema_sha256 = _load_schema()
    report = _exact_fields(decision, TOP_LEVEL_FIELDS, "option decision")
    if report["schema_version"] != SCHEMA_VERSION:
        raise OptionDecisionValidationError(f"schema_version must be {SCHEMA_VERSION}")
    observed_at = _timestamp(report["observed_at"], "observed_at")
    decision_mode = report["decision_mode"]
    if decision_mode not in {"new_entry", "position_review"}:
        raise OptionDecisionValidationError("decision_mode is unsupported")

    underlying = _validate_underlying(
        report["underlying_thesis"],
        observed_at=observed_at,
        artifact_root=artifact_root,
    )
    market = _validate_option_market(
        report["option_market"],
        observed_at=observed_at,
        analysis_contract=underlying["analysis_contract"],
        latest_realization_time=underlying["latest"],
    )
    expression = _validate_expression(report["option_expression"])
    position = _validate_position(
        report["position"],
        observed_at=observed_at,
        analysis_contract=underlying["analysis_contract"],
    )
    economics = _validate_economics(
        report["economics"],
        selected=market["selected"],
        position=position,
        decision_mode=decision_mode,
    )
    _validate_rationale(
        report["rationale"], entry_style=expression["value"]["entry_style"]
    )
    reassessment = _validate_reassessment(
        report["reassessment"],
        observed_at=observed_at,
        latest_realization_time=underlying["latest"],
    )
    _validate_claim_boundary(report["claim_boundary"])

    capital_action = expression["value"]["capital_action"]
    route = expression["value"]["option_route"]
    timing = expression["value"]["entry_timing"]
    style = expression["value"]["entry_style"]
    selected = market["selected"]
    stop = expression["stop"]

    if capital_action != "close" and underlying["latest"] <= observed_at:
        raise OptionDecisionValidationError(
            "latest_realization_time must be in the future unless an existing position is closing"
        )

    if decision_mode == "new_entry":
        if position is not None:
            raise OptionDecisionValidationError("new_entry must not contain a position")
        if capital_action not in {"buy_to_open", "wait"}:
            raise OptionDecisionValidationError("new_entry capital_action must be buy_to_open or wait")
        if capital_action == "wait":
            if (route, timing, style) != ("wait", "wait", "wait"):
                raise OptionDecisionValidationError(
                    "new-entry wait requires option_route=wait, entry_timing=wait and entry_style=wait"
                )
            if economics is not None or report["scenario_grid"]:
                raise OptionDecisionValidationError("new-entry wait cannot claim economics or scenarios")
            if expression["precondition"] is not None or stop is not None:
                raise OptionDecisionValidationError(
                    "new-entry wait cannot claim an execution precondition or option stop"
                )
            if reassessment is None:
                raise OptionDecisionValidationError("new-entry wait requires reassessment")
        else:
            if route not in {"long_call", "long_put"}:
                raise OptionDecisionValidationError("buy_to_open requires long_call or long_put")
            if timing not in {"now", "conditional"} or style not in ENTRY_STYLES:
                raise OptionDecisionValidationError(
                    "buy_to_open requires an actionable entry timing and entry style"
                )
            if selected is None or economics is None or selected["analytics"] is None:
                raise OptionDecisionValidationError(
                    "buy_to_open requires a selected contract, sourced analytics and economics"
                )
            expected_underlying_action = "long" if route == "long_call" else "short"
            if underlying["value"]["underlying_action"] != expected_underlying_action:
                raise OptionDecisionValidationError(
                    "buy_to_open route conflicts with UnderlyingThesis action"
                )
            if selected["option_type"] != _route_option_type(route):
                raise OptionDecisionValidationError("option route conflicts with selected contract type")
            if not selected["expiration_covers_latest"]:
                raise OptionDecisionValidationError(
                    "selected option expiration does not cover latest_realization_time"
                )
            if timing == "now":
                if expression["precondition"] is not None:
                    raise OptionDecisionValidationError("entry_timing=now must not have a precondition")
                if market["status"] != "fresh" or market["liquidity"]["status"] != "executable":
                    raise OptionDecisionValidationError(
                        "entry_timing=now requires fresh executable option bid/ask"
                    )
            else:
                if expression["precondition"] is None:
                    raise OptionDecisionValidationError(
                        "entry_timing=conditional requires an execution precondition"
                    )
            if reassessment is None:
                raise OptionDecisionValidationError("buy_to_open requires reassessment")
    else:
        if capital_action not in {"hold", "close"}:
            raise OptionDecisionValidationError("position_review capital_action must be hold or close")
        if route not in {"long_call", "long_put"}:
            raise OptionDecisionValidationError("position_review must retain its long option route")
        if timing != "not_applicable" or style != "position_review":
            raise OptionDecisionValidationError(
                "position_review requires not_applicable timing and position_review style"
            )
        if expression["precondition"] is not None:
            raise OptionDecisionValidationError("position_review cannot contain an entry precondition")
        if position is None or selected is None or economics is None or selected["analytics"] is None:
            raise OptionDecisionValidationError(
                "position_review requires position, current contract, analytics and economics"
            )
        if (
            position["contract"] != selected["symbol"]
            or position["option_type"] != selected["option_type"]
            or position["underlying_contract"] != selected["underlying_contract"]
        ):
            raise OptionDecisionValidationError("position conflicts with selected option contract")
        if selected["option_type"] != _route_option_type(route):
            raise OptionDecisionValidationError("position route conflicts with held contract type")
        if not selected["expiration_covers_latest"]:
            raise OptionDecisionValidationError(
                "held option expiration does not cover latest_realization_time"
            )
        if capital_action == "hold":
            expected_underlying_action = "long" if route == "long_call" else "short"
            if underlying["value"]["underlying_action"] != expected_underlying_action:
                raise OptionDecisionValidationError("hold conflicts with UnderlyingThesis action")
            if reassessment is None:
                raise OptionDecisionValidationError("hold requires reassessment")
        else:
            if reassessment is not None:
                raise OptionDecisionValidationError("close must not defer to a future reassessment")
            if market["status"] != "fresh" or market["liquidity"]["status"] != "executable":
                raise OptionDecisionValidationError("close requires a fresh executable bid")

    actionable_or_position = capital_action in {"buy_to_open", "hold", "close"}
    if actionable_or_position:
        if (
            stop is None
            or underlying["target"] is None
            or underlying["invalidation"] is None
            or underlying["time_failure_price"] is None
        ):
            raise OptionDecisionValidationError(
                "buy/hold/close must keep target, option stop, futures invalidation and time failure distinct"
            )
    if capital_action in {"buy_to_open", "hold"}:
        _validate_directional_geometry(
            route=route,
            underlying_price=underlying["underlying_price"],
            trigger=underlying["trigger"],
            target=underlying["target"],
            option_stop=stop,
            futures_invalidation=underlying["invalidation"],
        )

    scenarios_required = capital_action in {"buy_to_open", "hold"}
    if report["scenario_grid"] and (
        economics is None
        or selected is None
        or selected["current_iv"] is None
        or stop is None
        or underlying["target"] is None
        or underlying["invalidation"] is None
        or underlying["time_failure_price"] is None
    ):
        raise OptionDecisionValidationError(
            "scenario_grid requires complete option analytics, economics and four scenario levels"
        )
    scenario_rows = (
        _validate_scenarios(
            report["scenario_grid"],
            observed_at=observed_at,
            latest_realization_time=underlying["latest"],
            target=underlying["target"],
            option_stop=stop,
            futures_invalidation=underlying["invalidation"],
            time_failure_price=underlying["time_failure_price"],
            current_iv=selected["current_iv"],
            economics=economics,
            required=scenarios_required,
        )
        if report["scenario_grid"] or scenarios_required
        else []
    )

    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "valid": True,
        "decision_mode": decision_mode,
        "analysis_contract": underlying["analysis_contract"],
        "underlying_action": underlying["value"]["underlying_action"],
        "capital_action": capital_action,
        "option_route": route,
        "entry_timing": timing,
        "entry_style": style,
        "selected_option_contract": None if selected is None else selected["symbol"],
        "scenario_count": len(scenario_rows),
        "underlying_report_bytes_verified": True,
        "underlying_report_sha256": underlying["report_sha256"],
        "underlying_report_validation_scope": (
            "hash_and_cross_layer_bindings_only; run futures_main_report.py separately"
        ),
        "schema_sha256": schema_sha256,
        "validator_selected_direction": False,
        "validation_is_trade_permission": False,
        "automatic_ordering": False,
        "order_authorized": False,
    }


def validate_option_decision_file(path: str | Path) -> dict[str, Any]:
    decision_path = Path(path).expanduser().resolve()
    decision = _json_loads(_read_bytes(decision_path, label="option decision"), label="option decision")
    return validate_option_decision(decision, artifact_root=decision_path.parent)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate option_decision.v1 facts and consistency without authorizing orders"
    )
    parser.add_argument("decision", type=Path, help="option_decision.v1 JSON file")
    args = parser.parse_args(argv)
    try:
        result = validate_option_decision_file(args.decision)
    except OptionDecisionValidationError as exc:
        print(
            json.dumps(
                {
                    "schema_version": VALIDATION_SCHEMA_VERSION,
                    "valid": False,
                    "error": str(exc),
                    "validator_selected_direction": False,
                    "validation_is_trade_permission": False,
                    "automatic_ordering": False,
                    "order_authorized": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

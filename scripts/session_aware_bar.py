#!/usr/bin/env python3
"""Validate current-session partial or completed daily-bar evidence.

The completed daily history remains the causal structural base.  A
``session_partial`` bar is an explicitly timestamped overlay that becomes
decision-eligible only after that product's night session has closed; it can
support anticipatory or conditional reasoning but never a completed/confirmed
daily-breakout claim. Products without a night session do not receive this
exception.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

try:
    from . import runtime_universe
except ImportError:  # pragma: no cover - direct CLI execution
    import runtime_universe


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "futures_main_report_policy.json"
SPECS_PATH = ROOT / "config" / "product_contract_specs.json"
UNIVERSE_PATH = ROOT / "config" / "trading_universe.json"
BJT = ZoneInfo("Asia/Shanghai")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_URL_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "signature",
    "token",
    "x-amz-credential",
    "x-amz-signature",
}
BAR_FIELDS = {
    "schema_version",
    "product",
    "analysis_contract",
    "trade_date",
    "bar_state",
    "as_of",
    "decision_semantics",
    "ohlcv",
    "coverage",
    "source",
    "calendar_evidence",
    "claim_boundary",
}
OHLCV_FIELDS = {"open", "high", "low", "close", "volume", "open_interest"}
COVERAGE_FIELDS = {
    "session_kind",
    "schedule_group",
    "session_started_at",
    "checkpoint_at",
    "observed_through",
    "expected_daily_close_at",
    "covered_trading_seconds",
    "required_checkpoint_seconds",
}
SOURCE_FIELDS = {"kind", "locator", "retrieved_at", "content_sha256"}
CALENDAR_EVIDENCE_FIELDS = {
    "exchange",
    "trade_date",
    "regular_session_confirmed",
    "url",
    "retrieved_at",
    "locator",
    "content_sha256",
}
CLAIM_FIELDS = {
    "completed_bar_claim_allowed",
    "confirmed_daily_breakout_claim_eligible",
}


class SessionAwareBarError(ValueError):
    """Raised when a session-aware daily-bar claim is not trustworthy."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionAwareBarError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SessionAwareBarError(f"JSON root must be an object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SessionAwareBarError(f"{label} must be a timezone-aware ISO timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SessionAwareBarError(f"{label} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SessionAwareBarError(f"{label} must include a UTC offset")
    return parsed.astimezone(BJT)


def _date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise SessionAwareBarError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SessionAwareBarError(f"{label} must be YYYY-MM-DD") from exc


def _clock(value: Any, label: str) -> time:
    if not isinstance(value, str):
        raise SessionAwareBarError(f"{label} must be HH:MM or HH:MM:SS")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise SessionAwareBarError(f"{label} must be HH:MM or HH:MM:SS") from exc
    if parsed.tzinfo is not None:
        raise SessionAwareBarError(f"{label} must be a Beijing wall clock without offset")
    return parsed


def _policy() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    policy = _read_json(POLICY_PATH)
    try:
        schedule = policy["market_data_freshness"]["regular_session_schedule_bjt"]
        partial = policy["market_data_freshness"]["session_aware_partial_daily_bar"]
    except (KeyError, TypeError) as exc:
        raise SessionAwareBarError("session-aware daily-bar policy is missing") from exc
    if partial.get("schema_version") != "session_aware_daily_bar_policy.v1":
        raise SessionAwareBarError("unsupported session-aware daily-bar policy")
    if (
        partial.get("decision_use_after_checkpoint") is not True
        or partial.get("checkpoint_basis") != "post_regular_night_session_close"
        or partial.get("confirmed_daily_breakout_requires_completed_bar") is not True
        or partial.get("partial_is_not_completed_or_confirmed") is not True
        or partial.get("partial_bar_is_current_session_overlay_not_pivot_confirmation") is not True
        or partial.get("temporary_schedule_or_calendar_uncertainty_fails_closed") is not True
        or partial.get("partial_calendar_evidence_required") is not True
        or partial.get("completed_calendar_evidence_must_be_null") is not True
        or partial.get("calendar_evidence_regular_session_confirmed_required") is not True
        or partial.get("calendar_evidence_official_exchange_domain_required") is not True
        or "calendar_evidence" not in partial.get("partial_must_disclose", [])
    ):
        raise SessionAwareBarError("session-aware daily-bar policy weakens claim boundaries")
    groups = schedule.get("night_end_groups")
    if not isinstance(groups, dict):
        raise SessionAwareBarError("night-session groups are missing")
    scheduled = [str(item).upper() for values in groups.values() for item in values]
    expected = set(runtime_universe.allowed_product_symbols())
    if len(scheduled) != len(set(scheduled)) or set(scheduled) != expected:
        raise SessionAwareBarError("night-session groups must cover the current universe exactly once")
    expected_groups = {"NONE", "23:00", "01:00", "02:30"}
    night_groups = expected_groups - {"NONE"}
    if set(groups) != expected_groups:
        raise SessionAwareBarError("night-session group set is not canonical")
    checkpoints = partial.get("checkpoint_by_night_end_group_bjt")
    minimums = partial.get("minimum_covered_trading_seconds")
    if not isinstance(checkpoints, dict) or set(checkpoints) != night_groups:
        raise SessionAwareBarError("checkpoint schedule does not cover every night-session group")
    if not isinstance(minimums, dict) or set(minimums) != night_groups:
        raise SessionAwareBarError("checkpoint coverage does not cover every night-session group")
    if partial.get("no_night_product_partial_policy") != "forbidden_until_completed_daily_close":
        raise SessionAwareBarError("no-night products must not use the partial-bar exception")
    return policy, schedule, partial


def schedule_group_for_product(product: str) -> str:
    normalized = runtime_universe.require_allowed_product(product)
    _, schedule, _ = _policy()
    for group, products in schedule["night_end_groups"].items():
        if normalized in products:
            return str(group)
    raise SessionAwareBarError(f"product {normalized} has no configured session group")


def _local_at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=BJT)


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _regular_intervals(
    *, product: str, trade_date: date, session_started_at: datetime
) -> tuple[str, list[tuple[datetime, datetime]], int, datetime]:
    _, schedule, partial = _policy()
    group = schedule_group_for_product(product)
    day_intervals: list[tuple[datetime, datetime]] = []
    raw_day_intervals = schedule.get("day_intervals")
    if not isinstance(raw_day_intervals, list) or not raw_day_intervals:
        raise SessionAwareBarError("regular day-session intervals are missing")
    for index, pair in enumerate(raw_day_intervals):
        if not isinstance(pair, list) or len(pair) != 2:
            raise SessionAwareBarError(f"day interval {index} is malformed")
        start_clock = _clock(pair[0], f"day_intervals[{index}].start")
        end_clock = _clock(pair[1], f"day_intervals[{index}].end")
        start, end = _local_at(trade_date, start_clock), _local_at(trade_date, end_clock)
        if end <= start:
            raise SessionAwareBarError("day-session interval must have positive duration")
        day_intervals.append((start, end))

    if group == "NONE":
        expected_start = day_intervals[0][0]
        intervals = day_intervals
        session_kind = "day"
        required_raw = sum(int((end - start).total_seconds()) for start, end in intervals)
    else:
        expected_start_clock = _clock(
            partial.get("night_session_start_bjt"), "night_session_start_bjt"
        )
        if session_started_at.timetz().replace(tzinfo=None) != expected_start_clock:
            raise SessionAwareBarError("night session must start at the configured Beijing clock")
        if session_started_at.weekday() >= 5:
            raise SessionAwareBarError("regular night session cannot start on a weekend")
        if session_started_at.date() >= trade_date:
            raise SessionAwareBarError("night session must precede its labelled trade date")
        if _next_weekday(session_started_at.date()) != trade_date:
            raise SessionAwareBarError(
                "night session and trade date do not match the regular weekday calendar; "
                "an official holiday or temporary-session override is required"
            )
        end_clock = _clock(group, "night-session group")
        end_day = session_started_at.date()
        if end_clock <= expected_start_clock:
            end_day += timedelta(days=1)
        expected_start = _local_at(session_started_at.date(), expected_start_clock)
        intervals = [(expected_start, _local_at(end_day, end_clock)), *day_intervals]
        session_kind = "night"

    if session_started_at != expected_start:
        raise SessionAwareBarError("coverage.session_started_at conflicts with the configured session")
    if group != "NONE":
        required_raw = partial["minimum_covered_trading_seconds"].get(group)
    if isinstance(required_raw, bool) or not isinstance(required_raw, int) or required_raw <= 0:
        raise SessionAwareBarError("configured checkpoint coverage must be a positive integer")
    expected_close = day_intervals[-1][1]
    return session_kind, intervals, required_raw, expected_close


def _covered_seconds(
    intervals: list[tuple[datetime, datetime]], observed_through: datetime
) -> int:
    total = 0
    for start, end in intervals:
        if observed_through <= start:
            continue
        total += int((min(observed_through, end) - start).total_seconds())
    return total


def _checkpoint_at(
    intervals: list[tuple[datetime, datetime]], required_seconds: int
) -> datetime:
    remaining = required_seconds
    for start, end in intervals:
        length = int((end - start).total_seconds())
        if remaining <= length:
            return start + timedelta(seconds=remaining)
        remaining -= length
    raise SessionAwareBarError("checkpoint exceeds configured trading intervals")


def _product_spec(product: str) -> Mapping[str, Any]:
    payload = _read_json(SPECS_PATH)
    if payload.get("universe_sha256") != _file_sha256(UNIVERSE_PATH):
        raise SessionAwareBarError("product specs are not bound to the current universe")
    rows = payload.get("products")
    matches = (
        [
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get("symbol", "")).upper() == product
        ]
        if isinstance(rows, list)
        else []
    )
    if len(matches) != 1:
        raise SessionAwareBarError(f"product {product} has no unique tick specification")
    return matches[0]


def _tick_size(product: str) -> Decimal:
    spec = _product_spec(product)
    try:
        tick = Decimal(str(spec["tick_size"]))
    except Exception as exc:
        raise SessionAwareBarError(f"product {product} tick specification is invalid") from exc
    if tick <= 0:
        raise SessionAwareBarError(f"product {product} tick specification must be positive")
    return tick


def _validate_calendar_evidence(
    value: Any,
    *,
    product: str,
    trade_day: date,
    as_of: datetime,
    max_age_seconds: int,
) -> None:
    if not isinstance(value, Mapping) or set(value) != CALENDAR_EVIDENCE_FIELDS:
        raise SessionAwareBarError(
            "partial daily bar requires exact official trading-calendar evidence"
        )
    spec = _product_spec(product)
    exchange = str(spec.get("exchange", "")).upper()
    if value.get("exchange") != exchange:
        raise SessionAwareBarError("calendar evidence exchange conflicts with product")
    if _date(value.get("trade_date"), "calendar_evidence.trade_date") != trade_day:
        raise SessionAwareBarError("calendar evidence trade date conflicts with bar")
    if value.get("regular_session_confirmed") is not True:
        raise SessionAwareBarError(
            "partial bar requires evidence that the regular session is open and unchanged"
        )
    official_domains = {
        "SHFE": "shfe.com.cn",
        "INE": "ine.cn",
        "DCE": "dce.com.cn",
        "CZCE": "czce.com.cn",
        "GFEX": "gfex.com.cn",
    }
    expected_domain = official_domains.get(exchange)
    raw_url = value.get("url")
    if not isinstance(raw_url, str) or not raw_url:
        raise SessionAwareBarError("calendar evidence URL is missing")
    parsed = urlsplit(raw_url)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or expected_domain is None
        or not (hostname == expected_domain or hostname.endswith(f".{expected_domain}"))
        or parsed.username is not None
        or parsed.password is not None
        or any(
            key.lower() in SENSITIVE_URL_KEYS
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
    ):
        raise SessionAwareBarError(
            "calendar evidence must use the product exchange's public official HTTPS domain"
        )
    if (
        not isinstance(value.get("locator"), str)
        or not value["locator"].strip()
        or len(value["locator"]) > 1000
    ):
        raise SessionAwareBarError("calendar evidence locator must be 1..1000 characters")
    digest = value.get("content_sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise SessionAwareBarError("calendar evidence content_sha256 is invalid")
    retrieved = _timestamp(value.get("retrieved_at"), "calendar_evidence.retrieved_at")
    if retrieved > as_of:
        raise SessionAwareBarError("calendar evidence retrieval cannot exceed bar as_of")
    if (as_of - retrieved).total_seconds() > max_age_seconds:
        raise SessionAwareBarError("calendar evidence is too old for a partial-bar decision")


def _finite_number(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise SessionAwareBarError(f"{label} must be a finite number")
    decimal = Decimal(str(value))
    if not decimal.is_finite() or (positive and decimal <= 0):
        raise SessionAwareBarError(f"{label} must be {'positive' if positive else 'finite'}")
    return decimal


def validate_session_aware_daily_bar(
    value: Mapping[str, Any],
    *,
    expected_product: str | None = None,
    expected_contract: str | None = None,
    packet_observed_at: datetime | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate one self-contained bar and return a compact trusted summary."""

    if not isinstance(value, Mapping) or set(value) != BAR_FIELDS:
        raise SessionAwareBarError("session-aware daily bar has unexpected or missing fields")
    if value.get("schema_version") != "session_aware_daily_bar.v1":
        raise SessionAwareBarError("unsupported session-aware daily-bar version")
    product = runtime_universe.require_allowed_product(str(value.get("product", "")))
    if expected_product is not None and product != str(expected_product).strip().upper():
        raise SessionAwareBarError("session-aware daily bar product conflicts with evidence")
    contract = runtime_universe.canonical_delivery_contract(str(value.get("analysis_contract", "")))
    if runtime_universe.product_prefix(contract) != product:
        raise SessionAwareBarError("session-aware daily bar contract conflicts with product")
    if expected_contract is not None and contract != runtime_universe.canonical_delivery_contract(expected_contract):
        raise SessionAwareBarError("session-aware daily bar contract conflicts with evidence")

    trade_day = _date(value.get("trade_date"), "trade_date")
    if trade_day.weekday() >= 5:
        raise SessionAwareBarError("trade_date cannot be a weekend")
    as_of = _timestamp(value.get("as_of"), "as_of")
    current_input = now or datetime.now(tz=BJT)
    if current_input.tzinfo is None or current_input.utcoffset() is None:
        raise SessionAwareBarError("validator clock must be timezone-aware")
    current = current_input.astimezone(BJT)
    if as_of > current:
        raise SessionAwareBarError("session-aware daily bar is future-dated")
    if packet_observed_at is not None:
        if isinstance(packet_observed_at, str):
            packet_time = _timestamp(packet_observed_at, "packet_observed_at")
        else:
            if (
                packet_observed_at.tzinfo is None
                or packet_observed_at.utcoffset() is None
            ):
                raise SessionAwareBarError("packet_observed_at must be timezone-aware")
            packet_time = packet_observed_at.astimezone(BJT)
        if as_of > packet_time:
            raise SessionAwareBarError("session-aware bar as_of exceeds its evidence packet")

    coverage = value.get("coverage")
    if not isinstance(coverage, Mapping) or set(coverage) != COVERAGE_FIELDS:
        raise SessionAwareBarError("coverage must disclose the exact session checkpoint and extent")
    started = _timestamp(coverage.get("session_started_at"), "coverage.session_started_at")
    observed_through = _timestamp(coverage.get("observed_through"), "coverage.observed_through")
    checkpoint = _timestamp(coverage.get("checkpoint_at"), "coverage.checkpoint_at")
    expected_close_input = _timestamp(
        coverage.get("expected_daily_close_at"), "coverage.expected_daily_close_at"
    )
    if observed_through > as_of:
        raise SessionAwareBarError("coverage cannot include observations after bar as_of")
    session_kind, intervals, required_seconds, expected_close = _regular_intervals(
        product=product, trade_date=trade_day, session_started_at=started
    )
    group = schedule_group_for_product(product)
    if coverage.get("session_kind") != session_kind or coverage.get("schedule_group") != group:
        raise SessionAwareBarError("coverage session kind or group conflicts with product schedule")
    computed_checkpoint = _checkpoint_at(intervals, required_seconds)
    policy, _, partial_policy = _policy()
    if group == "NONE":
        configured_checkpoint = expected_close.timetz().replace(tzinfo=None)
    else:
        configured_checkpoint = _clock(
            partial_policy["checkpoint_by_night_end_group_bjt"][group],
            "configured checkpoint",
        )
    if computed_checkpoint.timetz().replace(tzinfo=None) != configured_checkpoint:
        raise SessionAwareBarError("configured checkpoint clock conflicts with computed trading time")
    if checkpoint != computed_checkpoint or expected_close_input != expected_close:
        raise SessionAwareBarError("coverage checkpoint or daily close is not schedule-derived")
    computed_covered = _covered_seconds(intervals, observed_through)
    reported_covered = coverage.get("covered_trading_seconds")
    reported_required = coverage.get("required_checkpoint_seconds")
    if (
        isinstance(reported_covered, bool)
        or not isinstance(reported_covered, int)
        or reported_covered != computed_covered
        or isinstance(reported_required, bool)
        or not isinstance(reported_required, int)
        or reported_required != required_seconds
    ):
        raise SessionAwareBarError("coverage trading seconds must be recomputed from the schedule")

    bar_state = value.get("bar_state")
    semantics = value.get("decision_semantics")
    claim = value.get("claim_boundary")
    if not isinstance(claim, Mapping) or set(claim) != CLAIM_FIELDS:
        raise SessionAwareBarError("claim_boundary is missing or malformed")
    if bar_state == "session_partial":
        if group == "NONE":
            raise SessionAwareBarError("products without a night session cannot use the partial daily-bar exception")
        if observed_through < checkpoint or as_of >= expected_close:
            raise SessionAwareBarError("partial daily bar is usable only after the night close and before daily close")
        if semantics not in {"anticipatory", "conditional"}:
            raise SessionAwareBarError("partial daily bar cannot claim confirmed daily breakout")
        expected_claim = {
            "completed_bar_claim_allowed": False,
            "confirmed_daily_breakout_claim_eligible": False,
        }
        max_calendar_age = policy.get("market_data_freshness", {}).get(
            "calendar_evidence_max_age_seconds"
        )
        if (
            isinstance(max_calendar_age, bool)
            or not isinstance(max_calendar_age, int)
            or max_calendar_age <= 0
        ):
            raise SessionAwareBarError("calendar evidence freshness policy is invalid")
        _validate_calendar_evidence(
            value.get("calendar_evidence"),
            product=product,
            trade_day=trade_day,
            as_of=as_of,
            max_age_seconds=max_calendar_age,
        )
    elif bar_state == "completed":
        if observed_through != expected_close or as_of < expected_close:
            raise SessionAwareBarError("completed daily bar must cover the 15:00 close")
        if semantics not in {"anticipatory", "conditional", "confirmed_daily_breakout"}:
            raise SessionAwareBarError("completed daily bar decision semantics are invalid")
        expected_claim = {
            "completed_bar_claim_allowed": True,
            "confirmed_daily_breakout_claim_eligible": True,
        }
        if value.get("calendar_evidence") is not None:
            raise SessionAwareBarError(
                "completed daily bars do not use the partial-session calendar gate"
            )
    else:
        raise SessionAwareBarError("bar_state must be session_partial or completed")
    if dict(claim) != expected_claim:
        raise SessionAwareBarError("claim_boundary conflicts with bar completion state")

    source = value.get("source")
    if not isinstance(source, Mapping) or set(source) != SOURCE_FIELDS:
        raise SessionAwareBarError("source must include kind, locator, retrieval time and SHA-256")
    if source.get("kind") not in {"url", "file"}:
        raise SessionAwareBarError("source.kind must be url or file")
    if not isinstance(source.get("locator"), str) or not source["locator"].strip():
        raise SessionAwareBarError("source.locator must be non-empty")
    retrieved = _timestamp(source.get("retrieved_at"), "source.retrieved_at")
    if retrieved > as_of:
        raise SessionAwareBarError("source retrieval time cannot exceed bar as_of")
    if not isinstance(source.get("content_sha256"), str) or SHA256_RE.fullmatch(source["content_sha256"]) is None:
        raise SessionAwareBarError("source.content_sha256 must be lowercase SHA-256")

    ohlcv = value.get("ohlcv")
    if not isinstance(ohlcv, Mapping) or set(ohlcv) != OHLCV_FIELDS:
        raise SessionAwareBarError("ohlcv must contain exact OHLC, volume and open_interest fields")
    prices = {name: _finite_number(ohlcv.get(name), f"ohlcv.{name}", positive=True) for name in ("open", "high", "low", "close")}
    if prices["high"] < max(prices.values()) or prices["low"] > min(prices.values()):
        raise SessionAwareBarError("OHLC ordering is impossible")
    tick = _tick_size(product)
    if any(price % tick != 0 for price in prices.values()):
        raise SessionAwareBarError("OHLC prices must align to the configured tick size")
    for name in ("volume", "open_interest"):
        item = ohlcv.get(name)
        if item is not None and _finite_number(item, f"ohlcv.{name}") < 0:
            raise SessionAwareBarError(f"ohlcv.{name} must be non-negative or null")

    return {
        "valid": True,
        "schema_version": "session_aware_daily_bar_validation.v1",
        "product": product,
        "analysis_contract": contract,
        "trade_date": trade_day.isoformat(),
        "bar_state": bar_state,
        "decision_semantics": semantics,
        "as_of": as_of.isoformat(timespec="seconds"),
        "schedule_group": group,
        "session_kind": session_kind,
        "checkpoint_at": checkpoint.isoformat(timespec="seconds"),
        "covered_trading_seconds": computed_covered,
        "required_checkpoint_seconds": required_seconds,
        "confirmed_daily_breakout_claim_eligible": expected_claim[
            "confirmed_daily_breakout_claim_eligible"
        ],
        "machine_selected_action": False,
        "order_authorized": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a session-aware partial or completed daily bar without selecting a trade."
    )
    parser.add_argument("bar", help="session_aware_daily_bar.v1 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = validate_session_aware_daily_bar(_read_json(Path(args.bar).expanduser().resolve()))
    except (SessionAwareBarError, ValueError, OSError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

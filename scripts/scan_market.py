#!/usr/bin/env python3
"""Build an evidence-only scan manifest for the configured product universe.

The scanner inventories observable market and structure evidence.  It never
selects a product or emits a trade action; those remain Agent judgements made
after technical, fundamental/news, and member-flow evidence are synthesized.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo
from akshare.futures.futures_daily_bar import calendar as AKSHARE_TRADE_DATES


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_universe import (  # noqa: E402
    allowed_products,
    canonical_delivery_contract,
    provider_exchange_identity,
    product_prefix,
    require_exact_allowed_products,
    sina_provider_symbol_for_contract,
)
from tick_evidence import TickEvidenceError, replay_exact_tick_evidence  # noqa: E402
from session_schedule import is_regular_night_session_clock  # noqa: E402
from verified_daily_loader import (  # noqa: E402
    DailyCacheVerificationError,
    replay_verified_daily_evidence,
)


SCHEMA_VERSION = "scan_manifest.v2"
POLICY_PATH = SKILL_DIR / "config/futures_main_report_policy.json"
UNIVERSE_PATH = SKILL_DIR / "config/trading_universe.json"
SPECS_PATH = SKILL_DIR / "config/product_contract_specs.json"
STATUS_VALUES = ("available", "partial", "unobservable")
CHAIN_CLOCK_SKEW_SECONDS = 5

FetchResult = Mapping[str, Any]
StructureResult = Mapping[str, Any]
Fetcher = Callable[[str], FetchResult]
Analyzer = Callable[[FetchResult], StructureResult]


def _now_beijing() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _snapshot_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _next_template_trade_day(after: date) -> date | None:
    values = [
        date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
        for value in sorted(set(AKSHARE_TRADE_DATES))
        if re.fullmatch(r"\d{8}", value)
        and f"{value[:4]}-{value[4:6]}-{value[6:]}" > after.isoformat()
    ]
    return values[0] if values else None


def _is_template_trade_day(value: date) -> bool:
    encoded = value.strftime("%Y%m%d")
    return encoded in set(AKSHARE_TRADE_DATES)


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def canonical_json_bytes(value: Any, *, trailing_newline: bool = False) -> bytes:
    """Return the stable JSON representation used by every manifest hash."""

    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )
    if trailing_newline:
        rendered += "\n"
    return rendered.encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Hash an embedded object, including a report's embedded scan manifest."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, value: Any, *, canonical: bool = False) -> str:
    """Write JSON atomically and return the SHA-256 of the exact file bytes."""

    if canonical:
        content = canonical_json_bytes(value, trailing_newline=True)
    else:
        content = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
                default=_json_default,
            )
            + "\n"
        ).encode("utf-8")
    _write_bytes_atomic(path, content)
    return hashlib.sha256(content).hexdigest()


def write_content_addressed_json(
    directory: Path,
    symbol: str,
    value: Any,
) -> tuple[Path, str]:
    """Persist immutable canonical bytes under a filename containing their hash."""

    content = canonical_json_bytes(value, trailing_newline=True)
    digest = hashlib.sha256(content).hexdigest()
    safe_symbol = str(symbol).strip().upper()
    if re.fullmatch(r"[A-Z]+", safe_symbol) is None:
        raise ValueError("content-addressed artifact symbol is invalid")
    path = directory / f"{safe_symbol}.{digest}.json"
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError("content-addressed artifact path contains conflicting bytes")
    else:
        _write_bytes_atomic(path, content)
    return path, digest


def _as_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        converted = asdict(value)
        if isinstance(converted, dict):
            return converted
    if hasattr(value, "to_dict") and callable(value.to_dict):
        converted = value.to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise TypeError(f"{label} must return a mapping")


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_present(mapping: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> Any:
    for path in paths:
        value = _nested(mapping, *path)
        if value is not None:
            return value
    return None


def _contract_code(
    value: Any, *, reference_date: date | None = None
) -> str | None:
    if isinstance(value, Mapping):
        value = next(
            (
                value.get(key)
                for key in ("symbol", "contract", "contract_code", "code")
                if value.get(key) is not None
            ),
            None,
        )
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper().removeprefix("NF_")
    # Provider-native CZCE three-digit codes are canonicalized by fetch_futures;
    # internal scan artifacts only accept an unambiguous PRODUCTYYMM delivery month.
    try:
        return canonical_delivery_contract(normalized, reference_date=reference_date)
    except ValueError:
        return None


def _actual_main(raw: Mapping[str, Any]) -> str | None:
    value = _first_present(
        raw,
        (
            ("actual_main_contract",),
            ("actual_main",),
            ("main_contract",),
            ("contract_selection", "actual_main_contract"),
            ("contract_selection", "actual_main"),
        ),
    )
    fetch_time = _snapshot_timestamp(raw.get("fetch_time"))
    return _contract_code(
        value,
        reference_date=None if fetch_time is None else fetch_time.date(),
    )


def _recompute_actual_main(
    raw: Mapping[str, Any], product: str
) -> tuple[str | None, str, list[str]]:
    """Recompute the OI leader from the preserved actual-month chain.

    ``actual_main`` is a provider/adapter claim.  A scan may only repeat it
    after ranking the exact chain carried by the raw artifact.  Incomplete or
    unknown rows remain useful evidence but are explicitly ``partial``.
    """

    gaps: list[str] = []
    chain = raw.get("actual_contract_chain")
    if not isinstance(chain, list) or not chain:
        return None, "unobservable", ["actual_main_chain_missing"]
    fetch_time = _snapshot_timestamp(raw.get("fetch_time"))
    if fetch_time is None:
        return None, "unobservable", ["actual_main_chain_fetch_time_unobservable"]
    reference_date = fetch_time.date()

    ranked: list[tuple[str, float, float | None]] = []
    contracts: list[str] = []
    unknown_oi_contracts: list[str] = []
    for index, item in enumerate(chain):
        if not isinstance(item, Mapping):
            gaps.append(f"actual_main_chain_row_invalid:{index}")
            continue
        contract = _contract_code(
            item.get("symbol", item.get("contract")),
            reference_date=reference_date,
        )
        if contract is None or product_prefix(contract) != product:
            gaps.append(f"actual_main_chain_contract_invalid:{index}")
            continue
        contracts.append(contract)
        configured_exchange = str(allowed_products()[product]["exchange"]).upper()
        exchange_status = provider_exchange_identity(
            configured_exchange, item.get("provider_exchange")
        )
        if item.get("exchange") != configured_exchange:
            gaps.append(f"actual_main_chain_configured_exchange_conflict:{contract}")
        if item.get("exchange_identity_status") != exchange_status:
            gaps.append(f"actual_main_chain_exchange_status_conflict:{contract}")
        if exchange_status in {
            "provider_exchange_conflict",
            "provider_exchange_unobservable",
            "configured_exchange_invalid",
        }:
            gaps.append(f"actual_main_chain_exchange_identity_{exchange_status}:{contract}")
            continue
        provider_quote_time = _snapshot_timestamp(item.get("provider_quote_time"))
        normalized_quote_time = _snapshot_timestamp(item.get("quote_time"))
        normalization = item.get("quote_time_normalization")
        if provider_quote_time is None or normalized_quote_time is None:
            gaps.append(f"actual_main_chain_provider_time_unobservable:{contract}")
        elif normalization == "provider_calendar_time_unchanged":
            direct_quote_time = _snapshot_timestamp(_nested(raw, "quote", "quote_time"))
            if (
                provider_quote_time != normalized_quote_time
                or direct_quote_time is None
                or abs((normalized_quote_time - direct_quote_time).total_seconds()) > 60
            ):
                gaps.append(f"actual_main_chain_time_normalization_conflict:{contract}")
        elif normalization == (
            "derived_calendar_time_from_direct_quote_for_next_trade_day_night_session"
        ):
            direct_quote_time = _snapshot_timestamp(_nested(raw, "quote", "quote_time"))
            is_night_clock = is_regular_night_session_clock(
                product,
                normalized_quote_time.timetz().replace(tzinfo=None),
                POLICY_PATH,
            )
            session_anchor_date = (
                normalized_quote_time.date()
                if normalized_quote_time.hour >= 21
                else normalized_quote_time.date() - timedelta(days=1)
            )
            expected_trade_day = _next_template_trade_day(
                session_anchor_date
            )
            if (
                direct_quote_time is None
                or not _is_template_trade_day(session_anchor_date)
                or not is_night_clock
                or expected_trade_day is None
                or provider_quote_time.date() != expected_trade_day
                or provider_quote_time.date() <= normalized_quote_time.date()
                or provider_quote_time.time() != normalized_quote_time.time()
                or abs(
                    (normalized_quote_time - direct_quote_time).total_seconds()
                )
                > 60
            ):
                gaps.append(f"actual_main_chain_time_normalization_conflict:{contract}")
        else:
            gaps.append(f"actual_main_chain_time_normalization_unverified:{contract}")
        oi = _positive_finite_number(item.get("open_interest"))
        raw_oi = item.get("open_interest")
        if oi is None:
            # A true zero cannot become main, but a missing/invalid OI can.
            try:
                numeric_oi = float(raw_oi)
            except (TypeError, ValueError):
                numeric_oi = float("nan")
            if raw_oi is None or not isfinite(numeric_oi) or numeric_oi < 0:
                unknown_oi_contracts.append(contract)
            continue
        volume = _positive_finite_number(item.get("volume"))
        if item.get("volume") == 0:
            volume = 0.0
        ranked.append((contract, oi, volume))

    if len(contracts) != len(set(contracts)):
        gaps.append("actual_main_chain_duplicate_contract")
    if unknown_oi_contracts:
        gaps.append(
            "actual_main_chain_unknown_open_interest:"
            + ",".join(unknown_oi_contracts)
        )
    if not ranked:
        return None, "unobservable", list(dict.fromkeys(gaps + ["actual_main_chain_no_rankable_row"]))

    chain_times = [
        _snapshot_timestamp(item.get("quote_time"))
        if isinstance(item, Mapping)
        else None
        for item in chain
    ]
    if any(item is None for item in chain_times):
        gaps.append("actual_main_chain_time_unobservable")
    else:
        observed_chain_times = [item for item in chain_times if item is not None]
        if (max(observed_chain_times) - min(observed_chain_times)).total_seconds() > 60:
            gaps.append("actual_main_chain_mixed_snapshot_time")
        if any(
            item > fetch_time + timedelta(seconds=CHAIN_CLOCK_SKEW_SECONDS)
            for item in observed_chain_times
        ):
            gaps.append("actual_main_chain_time_after_fetch")

    ranked.sort(
        key=lambda item: (
            -item[1],
            -(item[2] if item[2] is not None else -1),
            item[0],
        )
    )
    leader = ranked[0][0]
    top_oi = ranked[0][1]
    tied = [item for item in ranked if item[1] == top_oi]
    if len(tied) > 1 and any(item[2] is None for item in tied):
        gaps.append("actual_main_chain_top_volume_unobservable")

    selection = raw.get("main_selection")
    complete = False
    if not isinstance(selection, Mapping):
        gaps.append("actual_main_selection_metadata_missing")
    else:
        for field, expected in (
            ("source_record_count", len(chain)),
            ("available_contract_count", len(ranked)),
            ("rankable_contract_count", len(ranked)),
        ):
            if selection.get(field) != expected:
                gaps.append(f"actual_main_selection_{field}_conflict")
        declared_chain_hash = selection.get("actual_contract_chain_sha256")
        if declared_chain_hash != canonical_sha256(chain):
            gaps.append("actual_main_chain_sha256_conflict")
        chain_time_evidence = selection.get("chain_time_evidence")
        if not isinstance(chain_time_evidence, Mapping):
            gaps.append("actual_main_chain_time_evidence_missing")
        elif any(item is None for item in chain_times):
            if chain_time_evidence.get("status") != "mixed_or_unobservable_time":
                gaps.append("actual_main_chain_time_evidence_conflict")
        else:
            observed_chain_times = [item for item in chain_times if item is not None]
            span_seconds = (
                max(observed_chain_times) - min(observed_chain_times)
            ).total_seconds()
            expected_status = (
                "same_snapshot"
                if span_seconds <= 60
                and fetch_time is not None
                and all(
                    item
                    <= fetch_time + timedelta(seconds=CHAIN_CLOCK_SKEW_SECONDS)
                    for item in observed_chain_times
                )
                else "mixed_or_unobservable_time"
            )
            if (
                chain_time_evidence.get("status") != expected_status
                or chain_time_evidence.get("span_seconds") != span_seconds
                or chain_time_evidence.get("min_observed_at")
                != min(observed_chain_times).isoformat(timespec="seconds")
                or chain_time_evidence.get("max_observed_at")
                != max(observed_chain_times).isoformat(timespec="seconds")
            ):
                gaps.append("actual_main_chain_time_evidence_conflict")
        complete = (
            selection.get("chain_complete_for_product") is True
            and selection.get("pagination_complete") is True
            and selection.get("selection_claim") == "verified_actual_main"
            and not gaps
        )
        if not complete:
            reason = selection.get("chain_completeness_reason")
            if not isinstance(reason, str) or not reason.strip():
                gaps.append("actual_main_chain_incomplete_reason_missing")

    status = "complete" if complete else "partial"
    if status == "partial":
        gaps.append("actual_main_chain_partial")
    return leader, status, list(dict.fromkeys(gaps))


def _analysis_contract(raw: Mapping[str, Any], actual_main: str | None) -> str | None:
    value = _first_present(
        raw,
        (
            ("analysis_contract",),
            ("contract_selection", "analysis_contract"),
            ("requested_contract",),
        ),
    )
    fetch_time = _snapshot_timestamp(raw.get("fetch_time"))
    reference_date = None if fetch_time is None else fetch_time.date()
    explicit = _contract_code(value, reference_date=reference_date)
    if explicit is not None:
        return explicit
    requested = _contract_code(
        raw.get("requested_symbol"), reference_date=reference_date
    )
    return requested or actual_main


def _recompute_actual_main_liquidity(
    raw: Mapping[str, Any], actual_main: str | None
) -> tuple[str, list[str]]:
    """Describe execution reality for the OI leader without changing its identity."""

    if actual_main is None:
        return "unobservable", ["actual_main_liquidity_unobservable"]
    chain = raw.get("actual_contract_chain")
    fetch_time = _snapshot_timestamp(raw.get("fetch_time"))
    reference_date = None if fetch_time is None else fetch_time.date()
    matches = (
        [
            item
            for item in chain
            if isinstance(item, Mapping)
            and _contract_code(
                item.get("symbol"), reference_date=reference_date
            )
            == actual_main
        ]
        if isinstance(chain, list)
        else []
    )
    if len(matches) != 1:
        return "unobservable", ["actual_main_liquidity_row_unobservable"]
    row = matches[0]
    volume = _positive_finite_number(row.get("volume"))
    if row.get("volume") == 0:
        status = "no_trade"
    else:
        bid = _positive_finite_number(row.get("bid"))
        ask = _positive_finite_number(row.get("ask"))
        if volume is None:
            status = "unobservable"
        elif bid is None or ask is None or ask < bid:
            status = "execution_quote_incomplete"
        else:
            status = "observable_trade"
    gaps: list[str] = []
    selection = raw.get("main_selection")
    if (
        not isinstance(selection, Mapping)
        or selection.get("main_liquidity_status") != status
    ):
        gaps.append("actual_main_liquidity_claim_conflict")
    if status != "observable_trade":
        gaps.append(f"actual_main_liquidity_{status}")
    return status, gaps


def _completed_as_of(
    structure: Mapping[str, Any], raw: Mapping[str, Any]
) -> str | None:
    value = _first_present(
        structure,
        (
            ("as_of",),
            ("latest_completed_bar", "date"),
            ("latest_completed_bar", "at"),
        ),
    )
    if value is None:
        value = _first_present(
            raw,
            (
                ("latest_completed_bar", "date"),
                ("latest_completed_bar", "at"),
                ("technical", "last_daily_date"),
                ("technical", "latest_completed_date"),
            ),
        )
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    rows = _nested(raw, "technical", "completed_daily_rows")
    if isinstance(rows, list):
        completed_dates = [
            str(row.get("date", row.get("at"))).strip()
            for row in rows
            if isinstance(row, Mapping)
            and row.get("completed") is True
            and row.get("date", row.get("at")) is not None
        ]
        if completed_dates:
            return max(completed_dates)
    return None


def _technical_direction(structure: Mapping[str, Any]) -> str | None:
    value = _first_present(
        structure,
        (("trend", "direction"), ("technical_direction",), ("direction",)),
    )
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower()
    # Some future analyzers may expose a directional bias using trade words.
    # Preserve the observation while ensuring this evidence layer never emits
    # a trade action.
    if normalized == "long":
        return "up"
    if normalized == "short":
        return "down"
    if normalized == "wait":
        return None
    return normalized


def _daily_data_evidence_status(
    structure: Mapping[str, Any], raw: Mapping[str, Any]
) -> str | None:
    structure_value = _nested(structure, "daily_data_evidence", "status")
    raw_value = _nested(raw, "technical", "daily_data_evidence", "status")
    normalized_structure = (
        structure_value.strip().lower()
        if isinstance(structure_value, str) and structure_value.strip()
        else None
    )
    normalized_raw = (
        raw_value.strip().lower()
        if isinstance(raw_value, str) and raw_value.strip()
        else None
    )
    if (
        normalized_structure is not None
        and normalized_raw is not None
        and normalized_structure != normalized_raw
    ):
        return f"conflict:{normalized_raw}:{normalized_structure}"
    value = normalized_raw or normalized_structure
    if value is None:
        return None
    return value


def _positive_finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def _tick_evidence_gaps(
    raw: Mapping[str, Any],
    structure: Mapping[str, Any],
    *,
    analysis_contract: str | None,
    configured_tick: Decimal,
) -> list[str]:
    """Recompute tick provenance instead of trusting descriptive source text."""

    gaps: list[str] = []
    raw_evidence = _nested(raw, "technical", "tick_size_evidence")
    structure_evidence = structure.get("tick_size_evidence")
    if not isinstance(raw_evidence, Mapping):
        return ["raw_tick_size_evidence_missing"]
    if not isinstance(structure_evidence, Mapping):
        gaps.append("structure_tick_size_evidence_missing")
    elif canonical_sha256(raw_evidence) != canonical_sha256(structure_evidence):
        gaps.append("structure_tick_size_evidence_conflict")
    exact = _nested(raw, "technical", "tick_size_exact")
    evidence_tick = _positive_finite_number(raw_evidence.get("tick_size"))
    if (
        analysis_contract is None
        or raw_evidence.get("contract") != analysis_contract
    ):
        gaps.append("raw_tick_size_evidence_contract_conflict")
    if raw_evidence.get("exact") is not exact:
        gaps.append("raw_tick_size_evidence_exact_conflict")
    if evidence_tick is None or Decimal(str(evidence_tick)) != configured_tick:
        gaps.append("raw_tick_size_evidence_value_conflict")
    digest = raw_evidence.get("content_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        gaps.append("raw_tick_size_evidence_hash_invalid")
    raw_value = raw_evidence.get("raw_value")
    raw_number_match = re.search(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)",
        raw_value if isinstance(raw_value, str) else "",
    )
    if (
        raw_number_match is None
        or Decimal(raw_number_match.group(0)) != configured_tick
    ):
        gaps.append("raw_tick_size_evidence_raw_value_conflict")
    request_started = _snapshot_timestamp(raw.get("request_started_at"))
    retrieved = _snapshot_timestamp(raw_evidence.get("retrieved_at"))
    fetch_completed = _snapshot_timestamp(raw.get("fetch_time"))
    if (
        request_started is None
        or retrieved is None
        or fetch_completed is None
        or not request_started <= retrieved <= fetch_completed
    ):
        gaps.append("raw_tick_size_evidence_time_conflict")

    if exact is True:
        try:
            if analysis_contract is None:
                raise TickEvidenceError("analysis contract is unavailable")
            replay_exact_tick_evidence(
                raw_evidence,
                contract=analysis_contract,
                expected_tick_size=configured_tick,
            )
        except (TickEvidenceError, ValueError):
            gaps.append("raw_tick_size_exact_archive_invalid")
        provider_symbol = raw_evidence.get("provider_symbol")
        try:
            expected_provider_symbol = (
                None
                if analysis_contract is None
                else sina_provider_symbol_for_contract(analysis_contract)
            )
        except ValueError:
            expected_provider_symbol = None
        if (
            raw_evidence.get("kind") != "actual_contract_http_detail"
            or raw_evidence.get("hash_scope") != "http_response_body"
            or raw_evidence.get("raw_item") != "最小变动价位"
            or raw_evidence.get("locator")
            != "contract detail table item=最小变动价位"
            or expected_provider_symbol is None
            or provider_symbol != expected_provider_symbol
            or raw_evidence.get("source_url")
            != f"https://finance.sina.com.cn/futures/quotes/{provider_symbol}.shtml"
        ):
            gaps.append("raw_tick_size_exact_provenance_invalid")
    elif exact is False:
        expected_locator = (
            None
            if analysis_contract is None
            else f"products[symbol={product_prefix(analysis_contract)}].tick_size"
        )
        try:
            source_path = Path(str(raw_evidence.get("source_path"))).resolve()
        except (OSError, RuntimeError, ValueError):
            source_path = None
        if (
            raw_evidence.get("kind") != "product_specs_snapshot_proxy"
            or raw_evidence.get("provider_symbol") is not None
            or raw_evidence.get("hash_scope") != "complete_product_specs_file"
            or raw_evidence.get("raw_item") != "tick_size"
            or raw_evidence.get("locator") != expected_locator
            or source_path != SPECS_PATH.resolve()
            or digest != _file_sha256(SPECS_PATH)
        ):
            gaps.append("raw_tick_size_proxy_provenance_invalid")
    else:
        gaps.append("raw_tick_size_exactness_invalid")
    return gaps


def _tick_aligned(value: Any, tick_size: Decimal) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return number.is_finite() and number > 0 and number % tick_size == 0


def _structure_geometry_gaps(
    structure: Mapping[str, Any], *, tick_size: Decimal
) -> list[str]:
    """Identify missing geometry even when an analyzer reports success=True."""

    gaps: list[str] = []
    for level_name in ("support", "resistance"):
        level = structure.get(level_name)
        if not isinstance(level, Mapping):
            gaps.append(f"missing_{level_name}")
            continue
        bounds: dict[str, float] = {}
        for bound_name in ("lower", "upper"):
            raw_value = level.get(bound_name)
            number = _positive_finite_number(raw_value)
            if number is None:
                prefix = "missing" if raw_value is None else "invalid"
                gaps.append(f"{prefix}_{level_name}_{bound_name}")
            else:
                bounds[bound_name] = number
                if not _tick_aligned(raw_value, tick_size):
                    gaps.append(f"{level_name}_{bound_name}_not_tick_aligned")
        if set(bounds) == {"lower", "upper"} and bounds["lower"] > bounds["upper"]:
            gaps.append(f"invalid_{level_name}_bounds_order")

    trend = structure.get("trend")
    if not isinstance(trend, Mapping):
        gaps.append("missing_trend")
    else:
        trend_value = trend.get("value")
        if _positive_finite_number(trend_value) is None:
            prefix = "missing" if trend_value is None else "invalid"
            gaps.append(f"{prefix}_trend_value")
        elif not _tick_aligned(trend_value, tick_size):
            gaps.append("trend_value_not_tick_aligned")
    return gaps


def _configured_tick_sizes() -> dict[str, Decimal]:
    try:
        payload = json.loads(SPECS_PATH.read_text(encoding="utf-8"))
        rows = payload["products"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("product contract specs cannot be loaded") from error
    result: dict[str, Decimal] = {}
    if not isinstance(rows, list):
        raise ValueError("product contract specs products must be a list")
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("product contract spec row must be an object")
        symbol = str(row.get("symbol", "")).upper().strip()
        try:
            tick = Decimal(str(row.get("tick_size")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValueError(f"invalid configured tick for {symbol}") from error
        if not symbol or symbol in result or not tick.is_finite() or tick <= 0:
            raise ValueError(f"invalid configured tick for {symbol}")
        result[symbol] = tick
    expected = set(allowed_products())
    if set(result) != expected:
        raise ValueError("product contract specs must exactly cover the configured universe")
    return result


def _relative_path(path: Path, base: Path) -> str:
    try:
        relative = path.resolve().relative_to(base.resolve())
    except ValueError as error:
        raise ValueError("evidence_dir must stay inside the manifest directory") from error
    return relative.as_posix()


def _error_record(
    *, symbol: str, attempted_at: str, stage: str, error: Exception | str
) -> dict[str, Any]:
    return {
        "success": False,
        "requested_symbol": symbol,
        "attempted_at": attempted_at,
        "scanner_generated_error_record": True,
        "stage": stage,
        "error_type": type(error).__name__ if isinstance(error, Exception) else "Error",
        "error": str(error),
    }


def _status_counts(products: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    data = {
        status: sum(item.get("data_status") == status for item in products)
        for status in STATUS_VALUES
    }
    structure = {
        status: sum(item.get("structure_status") == status for item in products)
        for status in STATUS_VALUES
    }
    return {
        "data_status": data,
        "structure_status": structure,
        "with_evidence_gaps": sum(bool(item.get("evidence_gaps")) for item in products),
    }


def _default_fetcher(*, timeout: int, limit: int) -> Fetcher:
    fetch_script = SCRIPT_DIR / "fetch_futures.py"

    def bounded_fetch(symbol: str) -> Mapping[str, Any]:
        command = [
            sys.executable,
            str(fetch_script),
            symbol,
            "--timeout",
            str(timeout),
            "--limit",
            str(limit),
            "--tick-evidence-policy",
            "configured_research",
            "--full",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout + 2,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(
                f"fetch_futures exceeded the {timeout}-second product deadline"
            ) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            detail = completed.stderr.strip() or "invalid JSON from fetch subprocess"
            raise RuntimeError(detail) from error
        if not isinstance(payload, Mapping):
            raise RuntimeError("fetch subprocess returned a non-object payload")
        return payload

    return bounded_fetch


def _default_analyzer(raw: FetchResult) -> StructureResult:
    import market_structure

    # Prefer the future public adapter while remaining compatible with the
    # current compact analyzer.
    analyzer = getattr(market_structure, "analyze_fetch_snapshot", None)
    if analyzer is None:
        analyzer = getattr(market_structure, "_compact_fetch_structure")
    return analyzer(raw)


def _scan_one(
    symbol: str,
    metadata: Mapping[str, Any],
    *,
    fetcher: Fetcher,
    analyzer: Analyzer,
    attempted_at: str,
    configured_tick: Decimal,
    evidence_dir: Path,
    manifest_dir: Path,
) -> dict[str, Any]:
    evidence_gaps: list[str] = []
    source_warnings: list[str] = []

    try:
        raw = _as_mapping(fetcher(symbol), label="fetcher")
    except Exception as error:
        raw = _error_record(
            symbol=symbol, attempted_at=attempted_at, stage="fetch", error=error
        )
        evidence_gaps.append(f"fetch_failed:{type(error).__name__}:{error}")

    try:
        raw_path, raw_hash = write_content_addressed_json(
            evidence_dir / "raw", symbol, raw
        )
    except Exception as error:
        raw = _error_record(
            symbol=symbol,
            attempted_at=attempted_at,
            stage="raw_serialization",
            error=error,
        )
        evidence_gaps.append(
            f"fetch_evidence_not_serializable:{type(error).__name__}:{error}"
        )
        raw_path, raw_hash = write_content_addressed_json(
            evidence_dir / "raw", symbol, raw
        )

    fetch_succeeded = raw.get("success") is True
    if not fetch_succeeded and not evidence_gaps:
        evidence_gaps.append(f"fetch_unsuccessful:{raw.get('error', 'unknown_error')}")

    warnings = raw.get("warnings")
    if isinstance(warnings, list):
        source_warnings.extend(str(value) for value in warnings if str(value).strip())

    if fetch_succeeded:
        try:
            structure = _as_mapping(analyzer(raw), label="analyzer")
        except Exception as error:
            structure = _error_record(
                symbol=symbol,
                attempted_at=attempted_at,
                stage="structure",
                error=error,
            )
            evidence_gaps.append(
                f"structure_failed:{type(error).__name__}:{error}"
            )
    else:
        structure = _error_record(
            symbol=symbol,
            attempted_at=attempted_at,
            stage="structure",
            error="skipped because fetch was unsuccessful",
        )

    declared_input_hash = structure.get("input_snapshot_sha256")
    if declared_input_hash is not None and declared_input_hash != raw_hash:
        structure = {
            **_error_record(
                symbol=symbol,
                attempted_at=attempted_at,
                stage="structure_input_binding",
                error="analyzer input_snapshot_sha256 conflicts with raw evidence",
            ),
            "analyzer_declared_input_snapshot_sha256": declared_input_hash,
        }
        evidence_gaps.append("structure_input_snapshot_sha256_conflict")
    # The scanner owns the serialized raw artifact and therefore injects the
    # only hash that can bind this structure output to those exact file bytes.
    structure["input_snapshot_sha256"] = raw_hash

    try:
        structure_path, structure_hash = write_content_addressed_json(
            evidence_dir / "structure", symbol, structure
        )
    except Exception as error:
        structure = _error_record(
            symbol=symbol,
            attempted_at=attempted_at,
            stage="structure_serialization",
            error=error,
        )
        evidence_gaps.append(
            f"structure_evidence_not_serializable:{type(error).__name__}:{error}"
        )
        structure["input_snapshot_sha256"] = raw_hash
        structure_path, structure_hash = write_content_addressed_json(
            evidence_dir / "structure", symbol, structure
        )

    structure_succeeded = structure.get("success") is True
    if not structure_succeeded and not any(
        gap.startswith("structure_") for gap in evidence_gaps
    ):
        evidence_gaps.append(
            f"structure_unobservable:{structure.get('error', 'unknown_error')}"
        )

    declared_actual_main = _actual_main(raw)
    recomputed_actual_main, actual_main_chain_status, main_chain_gaps = (
        _recompute_actual_main(raw, symbol)
        if fetch_succeeded
        else (None, "unobservable", [])
    )
    evidence_gaps.extend(main_chain_gaps)
    # Never promote an unverified provider label into the recomputed field.
    # ``declared_actual_main_contract`` preserves that claim separately; when
    # every OI row is unknown the actual main is genuinely unobservable.
    actual_main = recomputed_actual_main
    actual_main_liquidity_status, liquidity_gaps = _recompute_actual_main_liquidity(
        raw, actual_main
    )
    evidence_gaps.extend(liquidity_gaps)
    if (
        fetch_succeeded
        and recomputed_actual_main is not None
        and declared_actual_main != recomputed_actual_main
    ):
        evidence_gaps.append(
            "actual_main_claim_conflict:"
            f"declared={declared_actual_main}:recomputed={recomputed_actual_main}"
        )
    analysis_contract = _analysis_contract(raw, actual_main)
    if (
        fetch_succeeded
        and actual_main is not None
        and analysis_contract != actual_main
    ):
        evidence_gaps.append(
            "analysis_contract_not_actual_main_for_product_scan:"
            f"actual={actual_main}:analysis={analysis_contract}"
        )

    # A successful analyzer result is still only a claim until its identity,
    # cutoff and tick metadata agree with the exact raw snapshot that was
    # serialized above.  Keep mismatches as evidence gaps instead of silently
    # publishing a structurally valid but differently-scoped artifact.
    raw_fetch_timestamp = _snapshot_timestamp(raw.get("fetch_time"))
    raw_reference_date = (
        None if raw_fetch_timestamp is None else raw_fetch_timestamp.date()
    )
    structure_actual_main = _contract_code(
        structure.get("actual_main"), reference_date=raw_reference_date
    )
    structure_analysis_contract = _contract_code(
        structure.get("analysis_contract"), reference_date=raw_reference_date
    )
    if structure_succeeded:
        if structure_actual_main is None:
            evidence_gaps.append("structure_missing_actual_main")
        elif actual_main is None or structure_actual_main != actual_main:
            evidence_gaps.append(
                "structure_actual_main_conflict:"
                f"raw={actual_main}:structure={structure_actual_main}"
            )
        if structure_analysis_contract is None:
            evidence_gaps.append("structure_missing_analysis_contract")
        elif analysis_contract is None or structure_analysis_contract != analysis_contract:
            evidence_gaps.append(
                "structure_analysis_contract_conflict:"
                f"raw={analysis_contract}:structure={structure_analysis_contract}"
            )

        raw_as_of = _completed_as_of({}, raw)
        structure_as_of = _completed_as_of(structure, {})
        if raw_as_of is None:
            evidence_gaps.append("structure_raw_completed_as_of_missing")
        elif structure_as_of != raw_as_of:
            evidence_gaps.append(
                "structure_completed_as_of_conflict:"
                f"raw={raw_as_of}:structure={structure_as_of}"
            )

        raw_fetch_time = raw.get("fetch_time")
        structure_fetch_time = structure.get("fetch_time")
        if structure_fetch_time is not None and structure_fetch_time != raw_fetch_time:
            evidence_gaps.append("structure_fetch_time_conflict")

        raw_tick_size = _positive_finite_number(
            _nested(raw, "technical", "tick_size")
        )
        structure_tick_size = _positive_finite_number(structure.get("tick_size"))
        if raw_tick_size is None or structure_tick_size is None:
            evidence_gaps.append("structure_tick_size_missing")
        elif structure_tick_size != raw_tick_size:
            evidence_gaps.append(
                "structure_tick_size_conflict:"
                f"raw={raw_tick_size}:structure={structure_tick_size}"
            )
        if raw_tick_size is not None and Decimal(str(raw_tick_size)) != configured_tick:
            evidence_gaps.append(
                "raw_tick_size_config_conflict:"
                f"raw={raw_tick_size}:config={configured_tick}"
            )
        if (
            structure_tick_size is not None
            and Decimal(str(structure_tick_size)) != configured_tick
        ):
            evidence_gaps.append(
                "structure_tick_size_config_conflict:"
                f"structure={structure_tick_size}:config={configured_tick}"
            )
        raw_tick_contract = _nested(raw, "technical", "tick_size_contract")
        structure_tick_contract = structure.get("tick_size_contract")
        if (
            not isinstance(raw_tick_contract, str)
            or analysis_contract is None
            or raw_tick_contract.strip().upper() != analysis_contract
        ):
            evidence_gaps.append("raw_tick_size_contract_conflict")
        if (
            not isinstance(structure_tick_contract, str)
            or analysis_contract is None
            or structure_tick_contract.strip().upper() != analysis_contract
        ):
            evidence_gaps.append("structure_tick_size_contract_conflict")
        raw_tick_source = _nested(raw, "technical", "tick_size_source")
        structure_tick_source = structure.get("tick_size_source")
        if not isinstance(raw_tick_source, str) or not raw_tick_source.strip():
            evidence_gaps.append("raw_tick_size_source_missing")
        if (
            not isinstance(structure_tick_source, str)
            or not structure_tick_source.strip()
        ):
            evidence_gaps.append("structure_tick_size_source_missing")
        elif structure_tick_source != raw_tick_source:
            evidence_gaps.append("structure_tick_size_source_conflict")
        raw_tick_exact = _nested(raw, "technical", "tick_size_exact")
        structure_tick_exact = structure.get("tick_size_exact")
        if structure_tick_exact is not raw_tick_exact:
            evidence_gaps.append("structure_tick_size_exact_conflict")
        if structure_tick_exact is not True:
            evidence_gaps.append("structure_tick_not_exact_order_compatible")
        expected_precision = (
            "exact_actual_contract"
            if raw_tick_exact is True
            else "approximate_same_product_proxy"
        )
        if _nested(raw, "technical", "tick_size_precision") != expected_precision:
            evidence_gaps.append("raw_tick_size_precision_conflict")
        if structure.get("tick_size_precision") != expected_precision:
            evidence_gaps.append("structure_tick_size_precision_conflict")
        expected_actionability = (
            "exact_order_compatible"
            if structure_tick_exact is True
            else "research_only_not_for_exact_order"
        )
        if structure.get("tick_size_actionability") != expected_actionability:
            evidence_gaps.append("structure_tick_actionability_conflict")
        evidence_gaps.extend(
            _tick_evidence_gaps(
                raw,
                structure,
                analysis_contract=analysis_contract,
                configured_tick=configured_tick,
            )
        )
    fetch_time_value = raw.get("fetch_time")
    fetch_time = (
        fetch_time_value.strip()
        if isinstance(fetch_time_value, str) and fetch_time_value.strip()
        else None
    )
    as_of = _completed_as_of(structure, raw)
    direction = _technical_direction(structure)
    daily_data_evidence_status = _daily_data_evidence_status(structure, raw)
    if daily_data_evidence_status is None:
        evidence_gaps.append("missing_daily_data_evidence_status")
    elif daily_data_evidence_status != "verified":
        evidence_gaps.append(
            f"daily_data_evidence_not_verified:{daily_data_evidence_status}"
        )
    else:
        raw_daily_evidence = _nested(raw, "technical", "daily_data_evidence")
        structure_daily_evidence = structure.get("daily_data_evidence")
        if raw_daily_evidence != structure_daily_evidence:
            evidence_gaps.append("daily_data_evidence_object_conflict")
        try:
            technical = raw.get("technical")
            if analysis_contract is None:
                raise DailyCacheVerificationError("analysis contract is unavailable")
            replay_verified_daily_evidence(
                technical,
                contract=analysis_contract,
            )
        except (DailyCacheVerificationError, OSError, ValueError) as error:
            evidence_gaps.append(
                "daily_data_evidence_replay_failed:"
                f"{type(error).__name__}:{error}"
            )
    structure_schema_version = structure.get("schema_version")
    if not isinstance(structure_schema_version, str) or not structure_schema_version.strip():
        structure_schema_version = None
        evidence_gaps.append("missing_structure_schema_version")
    else:
        structure_schema_version = structure_schema_version.strip()
    structure_algorithm_version = structure.get("algorithm_version")
    if (
        not isinstance(structure_algorithm_version, str)
        or not structure_algorithm_version.strip()
    ):
        structure_algorithm_version = None
        evidence_gaps.append("missing_structure_algorithm_version")
    else:
        structure_algorithm_version = structure_algorithm_version.strip()

    expected_prefix = symbol.upper()
    for label, contract in (
        ("actual_main_contract", actual_main),
        ("analysis_contract", analysis_contract),
    ):
        if contract is None:
            evidence_gaps.append(f"missing_{label}")
        elif product_prefix(contract) != expected_prefix:
            evidence_gaps.append(f"{label}_product_mismatch:{contract}")
    if fetch_time is None:
        evidence_gaps.append("missing_fetch_time")
    if as_of is None:
        evidence_gaps.append("missing_completed_as_of")
    if direction is None:
        evidence_gaps.append("missing_technical_direction")
    evidence_gaps.extend(_structure_geometry_gaps(structure, tick_size=configured_tick))

    data_missing = any(
        gap.startswith(
            (
                "fetch_",
                "missing_actual_main_contract",
                "missing_analysis_contract",
                "actual_main_contract_product_mismatch",
                "analysis_contract_product_mismatch",
                "analysis_contract_not_actual_main_for_product_scan",
                "missing_fetch_time",
                "missing_daily_data_evidence_status",
                "daily_data_evidence_not_verified",
                "actual_main_",
            )
        )
        for gap in evidence_gaps
    )
    if not fetch_succeeded:
        data_status = "unobservable"
    elif data_missing:
        data_status = "partial"
    else:
        data_status = "available"

    structure_missing = any(
        gap.startswith(
            (
                "structure_",
                "missing_completed_as_of",
                "missing_technical_direction",
                "missing_structure_schema_version",
                "missing_structure_algorithm_version",
                "missing_daily_data_evidence_status",
                "daily_data_evidence_not_verified",
                "missing_support",
                "invalid_support",
                "missing_resistance",
                "invalid_resistance",
                "missing_trend",
                "invalid_trend",
            )
        )
        for gap in evidence_gaps
    )
    if not structure_succeeded:
        structure_status = "unobservable"
    elif structure_missing:
        structure_status = "partial"
    else:
        structure_status = "available"

    # ``scan_status`` describes how much evidence the scan preserved; it is not
    # a trade gate.  A successful provider fetch and structure pass can still be
    # useful to the Agent when the chain, daily source, tick or geometry is only
    # partially bound.  Calling that evidence ``unobservable`` erased exactly
    # the uncertainty the Agent needs to reason about.
    if not fetch_succeeded or not structure_succeeded:
        scan_status = "unobservable"
    elif (
        data_status == "available"
        and structure_status == "available"
        and not evidence_gaps
    ):
        scan_status = "success"
    else:
        scan_status = "partial"
    unique_gaps = list(dict.fromkeys(evidence_gaps))
    machine_hint = {
        "review_state": {
            "success": "ready_for_agent_review",
            "partial": "ready_for_agent_review_with_uncertainty",
            "unobservable": "evidence_unobservable",
        }[scan_status],
        "technical_direction": direction,
        "attention_reasons": unique_gaps
        + [f"source_warning:{warning}" for warning in source_warnings],
    }

    return {
        "symbol": symbol,
        "exchange": metadata.get("exchange"),
        "scan_status": scan_status,
        "actual_main_contract": actual_main,
        "declared_actual_main_contract": declared_actual_main,
        "actual_main_chain_status": actual_main_chain_status,
        "actual_main_liquidity_status": actual_main_liquidity_status,
        "analysis_contract": analysis_contract,
        "attempted_at": attempted_at,
        "fetch_time": fetch_time,
        "as_of": as_of,
        "data_status": data_status,
        "daily_data_evidence_status": daily_data_evidence_status,
        "structure_status": structure_status,
        "raw_snapshot_path": _relative_path(raw_path, manifest_dir),
        "raw_snapshot_sha256": raw_hash,
        "structure_snapshot_path": _relative_path(structure_path, manifest_dir),
        "structure_snapshot_sha256": structure_hash,
        "structure_input_snapshot_sha256": structure.get(
            "input_snapshot_sha256"
        ),
        "structure_schema_version": structure_schema_version,
        "structure_algorithm_version": structure_algorithm_version,
        "structure_actual_main_contract": structure_actual_main,
        "structure_analysis_contract": structure_analysis_contract,
        "structure_tick_size": structure.get("tick_size"),
        "structure_tick_size_contract": structure.get("tick_size_contract"),
        "structure_tick_size_source": structure.get("tick_size_source"),
        "structure_tick_size_exact": structure.get("tick_size_exact"),
        "structure_tick_size_precision": structure.get("tick_size_precision"),
        "structure_tick_size_actionability": structure.get(
            "tick_size_actionability"
        ),
        "structure_tick_size_evidence": structure.get("tick_size_evidence"),
        "evidence_gaps": unique_gaps,
        "source_warnings": source_warnings,
        "machine_hint": machine_hint,
    }


def build_scan_manifest(
    *,
    fetcher: Fetcher,
    analyzer: Analyzer,
    evidence_dir: Path,
    manifest_dir: Path | None = None,
    universe: Mapping[str, Mapping[str, Any]] | None = None,
    generated_at: str | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    """Scan every allowed product exactly once with injectable data adapters."""

    universe_sha256 = _file_sha256(UNIVERSE_PATH)
    policy_sha256 = _file_sha256(POLICY_PATH)
    product_specs_sha256 = _file_sha256(SPECS_PATH)
    configured_ticks = _configured_tick_sizes()
    authoritative_products = allowed_products()
    products_by_symbol = dict(
        authoritative_products if universe is None else universe
    )
    expected_symbols = list(
        require_exact_allowed_products(list(products_by_symbol), label="scan universe")
    )
    for symbol in expected_symbols:
        if str(products_by_symbol[symbol].get("exchange", "")).upper() != str(
            authoritative_products[symbol]["exchange"]
        ).upper():
            raise ValueError(f"scan universe exchange mismatch for {symbol}")
    if workers < 1:
        raise ValueError("workers must be at least 1")

    attempted_at = generated_at or _now_beijing()
    evidence_dir = evidence_dir.expanduser()
    manifest_dir = (manifest_dir or evidence_dir.parent).expanduser()
    try:
        evidence_dir.resolve().relative_to(manifest_dir.resolve())
    except ValueError as error:
        raise ValueError(
            "evidence_dir must stay inside the manifest directory"
        ) from error

    entries_by_symbol: dict[str, dict[str, Any]] = {}
    if workers == 1:
        for symbol in expected_symbols:
            entries_by_symbol[symbol] = _scan_one(
                symbol,
                products_by_symbol[symbol],
                fetcher=fetcher,
                analyzer=analyzer,
                attempted_at=attempted_at,
                configured_tick=configured_ticks[symbol],
                evidence_dir=evidence_dir,
                manifest_dir=manifest_dir,
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _scan_one,
                    symbol,
                    products_by_symbol[symbol],
                    fetcher=fetcher,
                    analyzer=analyzer,
                    attempted_at=attempted_at,
                    configured_tick=configured_ticks[symbol],
                    evidence_dir=evidence_dir,
                    manifest_dir=manifest_dir,
                ): symbol
                for symbol in expected_symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                # _scan_one captures provider failures; this is only for scanner bugs.
                entries_by_symbol[symbol] = future.result()

    entries = [entries_by_symbol[symbol] for symbol in expected_symbols]
    attempted_symbols = [entry["symbol"] for entry in entries]
    require_exact_allowed_products(attempted_symbols, label="scan attempts")
    successful_symbols = [
        entry["symbol"] for entry in entries if entry["scan_status"] == "success"
    ]
    partial_symbols = [
        entry["symbol"] for entry in entries if entry["scan_status"] == "partial"
    ]
    unobservable_symbols = [
        entry["symbol"]
        for entry in entries
        if entry["scan_status"] == "unobservable"
    ]
    status_partitions = (successful_symbols, partial_symbols, unobservable_symbols)
    if any(
        set(left).intersection(right)
        for index, left in enumerate(status_partitions)
        for right in status_partitions[index + 1 :]
    ) or sorted(successful_symbols + partial_symbols + unobservable_symbols) != expected_symbols:
        raise AssertionError("scan status partition is not exhaustive")
    if _file_sha256(UNIVERSE_PATH) != universe_sha256:
        raise RuntimeError("trading universe changed while the scan was running")
    if _file_sha256(POLICY_PATH) != policy_sha256:
        raise RuntimeError("report policy changed while the scan was running")
    if _file_sha256(SPECS_PATH) != product_specs_sha256:
        raise RuntimeError("product contract specs changed while the scan was running")

    return {
        "schema_version": SCHEMA_VERSION,
        # One immutable batch timestamp also appears in every row as attempted_at.
        # Per-provider fetch_time records the later observation within the batch.
        "generated_at": attempted_at,
        "universe_sha256": universe_sha256,
        "policy_sha256": policy_sha256,
        "product_specs_sha256": product_specs_sha256,
        "expected_count": len(expected_symbols),
        "attempted_count": len(entries),
        "successful_count": len(successful_symbols),
        "partial_count": len(partial_symbols),
        "unobservable_count": len(unobservable_symbols),
        "expected_symbols": expected_symbols,
        "attempted_symbols": attempted_symbols,
        "successful_symbols": successful_symbols,
        "partial_symbols": partial_symbols,
        "unobservable_symbols": unobservable_symbols,
        "products": entries,
        "counts": _status_counts(entries),
        "machine_notice": (
            "本清单只盘点证据完整性并提示核查顺序；不包含候选排名、"
            "交易方向或交易授权，最终 long、short 或 wait 由 Agent 完成三面因果判断。"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="为允许范围内每个商品期货生成一条证据扫描记录"
    )
    parser.add_argument("--output", type=Path, required=True, help="扫描清单 JSON")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="原始与结构证据目录；须位于清单目录内，默认使用 <stem>.evidence",
    )
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    output_path = args.output.expanduser()
    if args.evidence_dir is None:
        evidence_dir = output_path.parent / f"{output_path.stem}.evidence"
    else:
        requested_evidence_dir = args.evidence_dir.expanduser()
        evidence_dir = (
            requested_evidence_dir
            if requested_evidence_dir.is_absolute()
            else output_path.parent / requested_evidence_dir
        )
    try:
        manifest = build_scan_manifest(
            fetcher=_default_fetcher(
                timeout=max(1, args.timeout), limit=max(2, args.limit)
            ),
            analyzer=_default_analyzer,
            evidence_dir=evidence_dir,
            manifest_dir=output_path.parent,
            workers=max(1, min(args.workers, 16)),
        )
        write_json_atomic(output_path, manifest)
    except Exception as error:
        sys.stderr.write(f"scan manifest generation failed: {error}\n")
        return 1

    summary = {
        "success": True,
        "schema_version": manifest["schema_version"],
        "output": str(output_path),
        "expected_count": manifest["expected_count"],
        "attempted_count": manifest["attempted_count"],
        "successful_count": manifest["successful_count"],
        "partial_count": manifest["partial_count"],
        "unobservable_count": manifest["unobservable_count"],
        "counts": manifest["counts"],
        "manifest_sha256": canonical_sha256(manifest),
    }
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return 0


__all__ = [
    "SCHEMA_VERSION",
    "build_scan_manifest",
    "canonical_json_bytes",
    "canonical_sha256",
    "main",
    "write_content_addressed_json",
    "write_json_atomic",
]


if __name__ == "__main__":
    sys.exit(main())

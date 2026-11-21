#!/usr/bin/env python3
"""Validate futures evidence and record an Agent's discretionary decision.

Version 3 deliberately limits machine validation to facts, arithmetic,
provenance, and internal consistency.  Evidence states are not votes or trade
gates, a passing report is not an order authorisation, and only ``agent_judgment``
owns the final long/short/wait decision.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping
from urllib.parse import urlparse

try:
    from scripts.tick_evidence import (
        EXACT_TICK_EVIDENCE_FIELDS,
        TickEvidenceError,
        replay_exact_tick_evidence,
    )
    from scripts.runtime_universe import (
        canonical_delivery_contract,
        product_prefix,
        provider_exchange_identity,
        sina_provider_symbol_for_contract,
    )
    from scripts.session_aware_bar import (
        SessionAwareBarError,
        validate_session_aware_daily_bar,
    )
    from scripts.verified_daily_loader import (
        DailyCacheVerificationError,
        replay_daily_artifact_evidence,
        replay_verified_daily_evidence,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from tick_evidence import (
        EXACT_TICK_EVIDENCE_FIELDS,
        TickEvidenceError,
        replay_exact_tick_evidence,
    )
    from runtime_universe import (
        canonical_delivery_contract,
        product_prefix,
        provider_exchange_identity,
        sina_provider_symbol_for_contract,
    )
    from session_aware_bar import (
        SessionAwareBarError,
        validate_session_aware_daily_bar,
    )
    from verified_daily_loader import (
        DailyCacheVerificationError,
        replay_daily_artifact_evidence,
        replay_verified_daily_evidence,
    )


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PROJECT / "config/futures_main_report_policy.json"
UNIVERSE_PATH = PROJECT / "config/trading_universe.json"
PRODUCT_SPECS_PATH = PROJECT / "config/product_contract_specs.json"
REPORT_SCHEMA_VERSION = "futures_main_report.v3"
POLICY_SCHEMA_VERSION = "futures_main_report_policy.v3"
SCAN_MANIFEST_SCHEMA_VERSION = "scan_manifest.v2"
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
FORBIDDEN_OPTION_KEY_MARKERS = {
    "option",
    "impliedvolatility",
    "greek",
    "delta",
    "premium",
    "strike",
    "callcontract",
    "putcontract",
    "期权",
    "权利金",
    "行权价",
}
EXCHANGE_SOURCE_HOSTS = {
    "DCE": ("dce.com.cn",),
    "SHFE": ("shfe.com.cn",),
    "INE": ("ine.cn", "shfe.com.cn"),
    "CZCE": ("czce.com.cn",),
    "GFEX": ("gfex.com.cn",),
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
V3_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "action",
        "scan_scope",
        "observed_at",
        "tick_size",
        "target",
        "scan_manifest",
        "scan_manifest_sha256",
        "focus_refresh_from_scan",
        "candidate_comparison",
        "actual_main",
        "market_data",
        "structure_evidence",
        "fundamentals",
        "important_news",
        "member_flow",
        "point_plan",
        "agent_judgment",
    }
)
V3_REQUIRED_TOP_LEVEL_FIELDS = V3_TOP_LEVEL_FIELDS - {
    "scan_manifest",
    "scan_manifest_sha256",
    "focus_refresh_from_scan",
    "candidate_comparison",
}
DECISION_AUTHORITY_KEY_MARKERS = (
    "score",
    "rank",
    "vote",
    "voting",
    "gate",
    "permission",
    "machineselected",
    "autoauthorize",
    "rulespassed",
)


class FuturesMainReportError(ValueError):
    """Raised when a report violates the futures-main reporting contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise FuturesMainReportError(f"无法读取 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise FuturesMainReportError(f"JSON 顶层必须是对象：{path}")
    return value


def _allowed_products() -> set[str]:
    return {str(row["symbol"]).upper() for row in _allowed_product_rows()}


def _allowed_product_rows() -> list[Mapping[str, Any]]:
    """Return the authoritative configured-product rows in declared order."""
    payload = _read_json(UNIVERSE_PATH)
    products = payload.get("permission_scope", {}).get("allowed_products")
    expected_count = payload.get("product_count")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
        or not isinstance(products, list)
        or len(products) != expected_count
    ):
        raise FuturesMainReportError("无法读取配置品种正向清单")
    rows: list[Mapping[str, Any]] = []
    symbols: list[str] = []
    for index, item in enumerate(products):
        if not isinstance(item, Mapping):
            raise FuturesMainReportError(f"正向清单第 {index} 项格式错误")
        symbol = _non_empty_text(item.get("symbol"), f"正向清单第 {index} 项 symbol").upper()
        _non_empty_text(item.get("exchange"), f"正向清单 {symbol} exchange")
        rows.append(item)
        symbols.append(symbol)
    if len(symbols) != len(set(symbols)):
        raise FuturesMainReportError("配置品种正向清单包含重复品种")
    return rows


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise FuturesMainReportError(f"无法读取哈希目标：{path}") from exc


def _sha256_text(value: Any, label: str) -> str:
    digest = _non_empty_text(value, label)
    if SHA256_RE.fullmatch(digest) is None:
        raise FuturesMainReportError(f"{label} 必须是64位小写SHA-256")
    return digest


def _json_native(value: Any) -> Any:
    """Return a JSON-serialisable value without silently rounding Decimals."""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_native(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_native(child) for child in value]
    return value


def _canonical_file_json_native(value: Any) -> Any:
    """Normalize number types that can drift across a report file reload."""
    if isinstance(value, Decimal):
        # ``parse_float=Decimal`` remembers whether the JSON token used a
        # decimal point.  Restore that token class for canonical *file* hashes:
        # 13530.0 must remain distinct from the integer token 13530 and match
        # artifacts emitted by the standard JSON float encoder.
        return float(value)
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_file_json_native(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_canonical_file_json_native(child) for child in value]
    return value


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        _json_native(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _canonical_json_file_sha256(value: Any) -> str:
    payload = json.dumps(
        _canonical_file_json_native(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    return _sha256_bytes(payload)


def _configured_product_spec(product: str) -> Mapping[str, Any]:
    payload = _read_json(PRODUCT_SPECS_PATH)
    if payload.get("universe_sha256") != _file_sha256(UNIVERSE_PATH):
        raise FuturesMainReportError("品种合约规格未绑定当前配置品种清单")
    if payload.get("product_count") != len(_allowed_product_rows()):
        raise FuturesMainReportError("品种合约规格product_count与配置品种数不一致")
    products = payload.get("products")
    if not isinstance(products, list):
        raise FuturesMainReportError("无法读取品种合约规格")
    matches = [
        item
        for item in products
        if isinstance(item, Mapping)
        and str(item.get("symbol", "")).strip().upper() == product
    ]
    if len(matches) != 1:
        raise FuturesMainReportError(f"品种 {product} 的tick规格缺失或重复")
    return matches[0]


def _configured_tick_size(product: str) -> Decimal:
    spec = _configured_product_spec(product)
    tick_size = _decimal(spec.get("tick_size"), f"品种 {product} 的tick规格")
    if tick_size <= 0:
        raise FuturesMainReportError("配置tick必须为正数")
    return tick_size


def _finite_number(value: Any) -> bool:
    if isinstance(value, Decimal):
        return value.is_finite()
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _decimal(value: Any, label: str) -> Decimal:
    if not _finite_number(value):
        raise FuturesMainReportError(f"{label} 必须是有限数字")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FuturesMainReportError(f"{label} 必须是带时区时间")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FuturesMainReportError(f"{label} 不是有效 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FuturesMainReportError(f"{label} 必须包含时区")
    return parsed


def _evidence_time(value: Any, precision: Any, label: str) -> datetime | date:
    """Validate source-supported temporal precision without inventing a clock time."""
    if precision in (None, "timestamp"):
        return _timestamp(value, label)
    if precision == "date":
        if not isinstance(value, str):
            raise FuturesMainReportError(f"{label} 日期格式错误")
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise FuturesMainReportError(f"{label} 必须是 YYYY-MM-DD") from exc
    raise FuturesMainReportError(f"{label} 时间精度必须是 date 或 timestamp")


def _not_after_observation(
    marker: datetime | date,
    observed_at: datetime,
    label: str,
) -> None:
    if isinstance(marker, datetime):
        if marker > observed_at:
            raise FuturesMainReportError(f"{label}不得晚于报告观察时间")
    elif marker > observed_at.date():
        raise FuturesMainReportError(f"{label}不得晚于报告观察日期")


def _next_weekday(value: date) -> date:
    cursor = value + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


def _night_end_minutes(policy: Mapping[str, Any], product: str) -> int | None:
    schedule = policy["market_data_freshness"]["regular_session_schedule_bjt"]
    for label, products in schedule["night_end_groups"].items():
        if product in products:
            if label == "NONE":
                return None
            hours, minutes = (int(part) for part in label.split(":"))
            return hours * 60 + minutes
    raise FuturesMainReportError(f"品种 {product} 缺少常规交易时段")


def _expected_market_session(
    observed_at: datetime,
    product: str,
    policy: Mapping[str, Any],
    closed_dates: set[date],
) -> str:
    """Derive the regular Beijing-time session, with holiday closures applied.

    A Friday night belongs to the next weekday's trading day, so early Saturday
    may be active.  Holiday-eve and temporary cancellations must be represented
    by the exchange calendar evidence supplied with the report.
    """
    minute = observed_at.hour * 60 + observed_at.minute
    current_day = observed_at.date()
    night_end = _night_end_minutes(policy, product)

    if minute >= 21 * 60:
        assigned_trading_day = _next_weekday(current_day)
        within_night_window = night_end is not None and (
            night_end < 21 * 60 or minute < night_end
        )
        if (
            observed_at.weekday() < 5
            and current_day not in closed_dates
            and assigned_trading_day not in closed_dates
            and within_night_window
        ):
            return "active"
        return "closed"

    if night_end is not None and night_end < 21 * 60 and minute < night_end:
        previous_day = current_day - timedelta(days=1)
        assigned_trading_day = _next_weekday(previous_day)
        if (
            previous_day.weekday() < 5
            and previous_day not in closed_dates
            and assigned_trading_day not in closed_dates
        ):
            return "active"
        return "closed"

    if observed_at.weekday() >= 5 or current_day in closed_dates:
        return "closed"
    if 9 * 60 <= minute < 10 * 60 + 15:
        return "active"
    if 10 * 60 + 15 <= minute < 10 * 60 + 30:
        return "break"
    if 10 * 60 + 30 <= minute < 11 * 60 + 30:
        return "active"
    if 11 * 60 + 30 <= minute < 13 * 60 + 30:
        return "break"
    if 13 * 60 + 30 <= minute < 15 * 60:
        return "active"
    return "closed"


def _latest_regular_session_boundary(
    observed_at: datetime,
    product: str,
    policy: Mapping[str, Any],
    closed_dates: set[date],
) -> datetime:
    """Return the latest regular session end no later than observation time."""
    candidates: list[datetime] = []
    night_end = _night_end_minutes(policy, product)
    for days_back in range(8):
        calendar_day = observed_at.date() - timedelta(days=days_back)
        if calendar_day.weekday() < 5 and calendar_day not in closed_dates:
            for hour, minute in ((10, 15), (11, 30), (15, 0)):
                candidates.append(
                    observed_at.replace(
                        year=calendar_day.year,
                        month=calendar_day.month,
                        day=calendar_day.day,
                        hour=hour,
                        minute=minute,
                        second=0,
                        microsecond=0,
                    )
                )
        if (
            night_end is not None
            and calendar_day.weekday() < 5
            and calendar_day not in closed_dates
        ):
            assigned_trading_day = _next_weekday(calendar_day)
            if assigned_trading_day not in closed_dates:
                end_day = (
                    calendar_day
                    if night_end >= 21 * 60
                    else calendar_day + timedelta(days=1)
                )
                candidates.append(
                    observed_at.replace(
                        year=end_day.year,
                        month=end_day.month,
                        day=end_day.day,
                        hour=night_end // 60,
                        minute=night_end % 60,
                        second=0,
                        microsecond=0,
                    )
                )
    completed_boundaries = [item for item in candidates if item <= observed_at]
    if not completed_boundaries:
        raise FuturesMainReportError("无法派生最近常规交易时段边界")
    return max(completed_boundaries)


def _validate_exchange_contract_code(
    contract: str,
    product: str,
    exchange: str,
) -> None:
    digit_count = 3 if exchange == "CZCE" else 4
    if re.fullmatch(rf"{re.escape(product)}\d{{{digit_count}}}", contract) is None:
        raise FuturesMainReportError(
            f"{exchange} 实际月份合约代码必须使用 {digit_count} 位年月数字"
        )
    suffix = contract[len(product) :]
    if suffix in {"0", "1", "88", "888", "8888", "999", "9999"} or not (
        1 <= int(suffix[-2:]) <= 12
    ):
        raise FuturesMainReportError("实际月份合约不得是连续/指数别名，且交割月必须为01..12")


def _non_empty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuturesMainReportError(f"{label} 不能为空")
    return value.strip()


def _normalise_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def _reject_option_fields(value: Any, path: str = "report") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalised = _normalise_key(str(key))
            if any(marker in normalised for marker in FORBIDDEN_OPTION_KEY_MARKERS):
                raise FuturesMainReportError(f"期权字段超出本报告范围：{path}.{key}")
            _reject_option_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_option_fields(child, f"{path}[{index}]")


def _validate_v3_top_level_fields(report: Mapping[str, Any]) -> None:
    """Reject both unknown fields and disguised decision-authority outputs."""

    present = set(report)
    missing = V3_REQUIRED_TOP_LEVEL_FIELDS - present
    if missing:
        raise FuturesMainReportError(
            "v3报告缺少顶层必填字段：" + ", ".join(sorted(missing))
        )
    unexpected = present - V3_TOP_LEVEL_FIELDS
    authority_fields = [
        str(field)
        for field in unexpected
        if any(
            marker in _normalise_key(str(field))
            for marker in DECISION_AUTHORITY_KEY_MARKERS
        )
    ]
    if authority_fields:
        raise FuturesMainReportError(
            "v3顶层不得夹带score/rank/vote/gate/permission或machine-selected决策权威字段："
            + ", ".join(sorted(authority_fields))
        )
    if unexpected:
        raise FuturesMainReportError(
            "v3报告顶层字段不受支持：" + ", ".join(sorted(str(v) for v in unexpected))
        )


def _validate_v3_policy(policy: Mapping[str, Any]) -> None:
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise FuturesMainReportError("v3报告策略版本不受支持")
    scope = policy.get("scope")
    if not isinstance(scope, Mapping) or scope.get("allowed_actions") != [
        "long",
        "short",
        "wait",
    ]:
        raise FuturesMainReportError("v3报告动作必须固定为 long/short/wait")
    if scope.get("futures_order_authorized_by_report") is not False:
        raise FuturesMainReportError("报告不得授权期货订单")
    if scope.get("automatic_ordering") is not False:
        raise FuturesMainReportError("报告不得自动下单")

    decision = policy.get("decision_contract")
    expected_decision_values = {
        "final_action_owner": "agent",
        "validator_role": "fact_arithmetic_provenance_and_internal_consistency_only",
        "evidence_completeness_is_trade_permission": False,
        "evidence_gap_mechanically_forces_wait": False,
        "majority_vote_allowed": False,
        "numeric_score_is_decision_authority": False,
        "profit_guaranteed": False,
        "passing_validation_authorizes_order": False,
    }
    if not isinstance(decision, Mapping) or any(
        decision.get(key) != expected
        for key, expected in expected_decision_values.items()
    ):
        raise FuturesMainReportError("v3必须把最终决定交给Agent，机器只校验事实与自洽")
    _non_empty_text(decision.get("objective"), "Agent决策目标")

    manifest = policy.get("scan_manifest")
    configured_count = len(_allowed_product_rows())
    if not isinstance(manifest, Mapping):
        raise FuturesMainReportError("v3策略缺少 scan_manifest 规则")
    if (
        manifest.get("schema_version") != SCAN_MANIFEST_SCHEMA_VERSION
        or manifest.get("all_market_expected_count") != configured_count
        or _decimal(
            manifest.get("decision_use_max_age_seconds"), "scan decision-use max age"
        ) <= 0
        or _decimal(
            manifest.get("acquisition_max_batch_span_seconds"),
            "scan acquisition batch span",
        ) <= 0
        or manifest.get("scan_age_must_be_disclosed") is not True
        or manifest.get("scan_statuses") != ["success", "partial", "unobservable"]
        or manifest.get("counts_must_be_recomputed_from_products") is not True
        or manifest.get("manifest_is_evidence_not_trade_authority") is not True
    ):
        raise FuturesMainReportError("scan_manifest 必须精确覆盖全部配置品种且不得充当决策权威")

    sources = policy.get("source_evidence")
    if not isinstance(sources, Mapping) or sources.get("required_fields") != [
        "url",
        "retrieved_at",
        "content_sha256",
        "locator",
    ]:
        raise FuturesMainReportError("v3来源证据必须绑定URL、采集时间、内容哈希和定位符")
    if sources.get("member_ranking_must_use_official_exchange_domain") is not True:
        raise FuturesMainReportError("会员榜必须来自对应交易所官方域名")

    freshness = policy.get("market_data_freshness")
    if (
        not isinstance(freshness, Mapping)
        or freshness.get(
            "latest_completed_bar_requires_verified_final_raw_last_row"
        )
        is not True
        or freshness.get("official_exchange_daily_requires_official_domain")
        is not True
        or freshness.get(
            "completed_bar_daily_cache_artifact_requires_strict_replay"
        )
        is not True
        or freshness.get("degraded_completed_bar_provenance_status")
        != "bound_to_raw_bytes_cache_artifact_not_replayed"
        or freshness.get("latest_completed_bar_evidence_kinds")
        != [
            "official_exchange_daily",
            "actual_main_day_close_snapshot",
            "degraded_raw_completed_daily_row",
        ]
        or freshness.get("completed_bar_provenance_dimensions")
        != [
            "completed_daily_bar_bound_to_raw_bytes",
            "daily_cache_artifact_replay_verified",
            "upstream_response_identity_verified",
        ]
    ):
        raise FuturesMainReportError(
            "v3市场完成K线与官方日线必须绑定可重放证据"
        )
    schedule = freshness.get("regular_session_schedule_bjt")
    if not isinstance(schedule, Mapping):
        raise FuturesMainReportError("v3策略缺少交易时段模板")
    night_groups = schedule.get("night_end_groups")
    if not isinstance(night_groups, Mapping):
        raise FuturesMainReportError("v3策略缺少夜盘分组")
    scheduled = [str(symbol).upper() for values in night_groups.values() for symbol in values]
    if len(scheduled) != len(set(scheduled)) or set(scheduled) != _allowed_products():
        raise FuturesMainReportError("v3交易时段必须无重复覆盖全部配置品种")
    partial_bar_policy = freshness.get("session_aware_partial_daily_bar")
    if (
        not isinstance(partial_bar_policy, Mapping)
        or partial_bar_policy.get("schema_version")
        != "session_aware_daily_bar_policy.v1"
        or partial_bar_policy.get("checkpoint_basis")
        != "post_regular_night_session_close"
        or partial_bar_policy.get("no_night_product_partial_policy")
        != "forbidden_until_completed_daily_close"
        or partial_bar_policy.get("confirmed_daily_breakout_requires_completed_bar")
        is not True
        or partial_bar_policy.get("partial_is_not_completed_or_confirmed") is not True
        or partial_bar_policy.get(
            "partial_bar_is_current_session_overlay_not_pivot_confirmation"
        )
        is not True
    ):
        raise FuturesMainReportError(
            "v3盘中日线必须在品种夜盘收盘后才可作为partial overlay，且不得冒充完成确认"
        )

    actual_main = policy.get("actual_main")
    if not isinstance(actual_main, Mapping) or actual_main.get("chain_statuses") != [
        "complete",
        "partial",
        "unobservable",
    ]:
        raise FuturesMainReportError("v3实际主力必须区分完整、不完整和不可观测")
    if actual_main.get("available_contract_count_must_equal_contract_rows") is not True:
        raise FuturesMainReportError("可用合约数必须由合约明细重算")
    if actual_main.get("snapshot_freshness_statuses") != [
        "current_session",
        "historical_or_unbound",
    ]:
        raise FuturesMainReportError("实际主力必须区分当前批次与历史/未绑定快照")
    if actual_main.get("liquidity_statuses") != [
        "observable_trade",
        "no_trade",
        "execution_quote_incomplete",
        "unobservable",
    ]:
        raise FuturesMainReportError("实际主力必须披露由volume推导的流动性状态")

    structure = policy.get("structure_evidence")
    if not isinstance(structure, Mapping):
        raise FuturesMainReportError("v3策略缺少结构证据绑定规则")
    if structure.get("bare_previously_confirmed_on_forbidden") is not True:
        raise FuturesMainReportError("v3必须封死裸历史确认日期")
    if structure.get("historical_confirmation_requires_bound_confirming_bar_and_complete_subsequent_bar_chain") is not True:
        raise FuturesMainReportError("v3历史确认必须绑定确认K线和后续K线链")
    if (
        structure.get(
            "historical_confirmation_requires_verified_final_raw_contiguous_subsequence"
        )
        is not True
    ):
        raise FuturesMainReportError("v3历史确认必须来自已验final raw连续完成K线")
    if (
        structure.get(
            "historical_confirmation_requires_frozen_structure_and_final_raw_snapshots"
        )
        is not True
        or structure.get(
            "historical_confirmation_requires_independent_frozen_daily_artifact_replay"
        )
        is not True
        or structure.get("frozen_structure_rows_must_overlap_final_raw_exactly")
        is not True
        or structure.get("non_historical_structure_and_final_raw_must_be_identical")
        is not True
    ):
        raise FuturesMainReportError("v3历史确认必须使用可重放的S1冻结结构与S2最终raw")
    if (
        structure.get("daily_data_evidence_statuses")
        != ["verified", "degraded", "unobservable"]
        or structure.get("verified_daily_requires")
        != [
            "exact_configured_product_scope",
            "archive_manifest",
            "independent_anchor",
            "gzip_payload",
            "contract_slice_replay",
        ]
        or structure.get("degraded_daily_requires_agent_coverage_gap")
        != "daily_data_evidence_not_verified"
        or structure.get("degraded_daily_mechanically_forces_direction_or_wait")
        is not False
    ):
        raise FuturesMainReportError(
            "v3必须区分已重放日线与degraded日线，且缺口只进入Agent覆盖披露"
        )

    point_plan = policy.get("point_plan")
    if not isinstance(point_plan, Mapping):
        raise FuturesMainReportError("v3策略缺少点位计划规则")
    if point_plan.get("minimum_reference_reward_risk") is not None:
        raise FuturesMainReportError("v3不得把固定收益风险比当作放行条件")
    if point_plan.get("reference_reward_risk_is_arithmetic_not_permission") is not True:
        raise FuturesMainReportError("收益风险比只能作为算术事实")
    if (
        point_plan.get(
            "conditional_directional_action_requires_fresh_executable_quote_precondition"
        )
        is not True
    ):
        raise FuturesMainReportError("conditional方向动作必须声明执行前行情与盘口条件")

    judgment = policy.get("agent_judgment")
    if not isinstance(judgment, Mapping):
        raise FuturesMainReportError("v3策略缺少Agent判断合同")
    if (
        judgment.get("decision_method") != "agent_discretionary_synthesis"
        or judgment.get("three_domains_must_not_be_counted_as_votes") is not True
        or judgment.get("profit_guaranteed_must_be_false") is not True
    ):
        raise FuturesMainReportError("Agent判断不得退化成多数投票或盈利承诺")
    if judgment.get("domain_ids") != [
        "technical_structure",
        "fundamentals_and_news",
        "member_flow",
    ]:
        raise FuturesMainReportError("Agent判断必须覆盖技术、基本面新闻、会员资金三面")
    if judgment.get("directional_views") != [
        "long",
        "short",
        "neutral",
        "uncertain",
    ]:
        raise FuturesMainReportError("Agent方向观点必须与资本动作分开表达")
    if judgment.get("wait_requires_structured_reassessment") != [
        "wait_for",
        "reassess_by",
        "update_triggers",
    ]:
        raise FuturesMainReportError("Agent wait必须保存结构化等待条件与重评触发器")
    if (
        judgment.get(
            "directional_action_view_divergence_requires_structured_rationale"
        )
        is not True
    ):
        raise FuturesMainReportError("方向观点与资本动作分离时必须解释其偏离")

    comparison = policy.get("candidate_comparison")
    if (
        not isinstance(comparison, Mapping)
        or comparison.get("required_for_all_market") is not True
        or comparison.get("method")
        != "agent_comparison_without_score_or_rank_authority"
    ):
        raise FuturesMainReportError("全市场报告必须保留Agent跨品种候选比较")

def validate_policy(policy: Mapping[str, Any]) -> None:
    _validate_v3_policy(policy)


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    policy = _read_json(path)
    validate_policy(policy)
    return policy


def _validate_zone(value: Any, label: str) -> tuple[Decimal, Decimal]:
    if not isinstance(value, Mapping):
        raise FuturesMainReportError(f"{label}必须是区间")
    lower = _decimal(value.get("lower"), f"{label}下沿")
    upper = _decimal(value.get("upper"), f"{label}上沿")
    if lower > upper:
        raise FuturesMainReportError(f"{label}下沿不得高于上沿")
    return lower, upper


def _source_url(value: Any, label: str) -> str:
    source_url = _non_empty_text(value, label)
    if not source_url.startswith(("https://", "http://")):
        raise FuturesMainReportError(f"{label}必须是可追溯 URL")
    return source_url


def _exchange_source_url(value: Any, exchange: str, label: str) -> str:
    source_url = _source_url(value, label)
    hostname = (urlparse(source_url).hostname or "").casefold()
    allowed_hosts = EXCHANGE_SOURCE_HOSTS.get(exchange)
    if not allowed_hosts or not any(
        hostname == host or hostname.endswith(f".{host}")
        for host in allowed_hosts
    ):
        raise FuturesMainReportError(f"{label}必须来自 {exchange} 官方域名")
    return source_url


def _validate_market_calendar(
    report: Mapping[str, Any],
    observed_at: datetime,
    exchange: str,
    policy: Mapping[str, Any],
) -> set[date]:
    calendar = report.get("market_calendar")
    if not isinstance(calendar, Mapping):
        raise FuturesMainReportError("报告缺少交易所交易日历证据")
    _exchange_source_url(
        calendar.get("source_url"),
        exchange,
        "market_calendar.source_url",
    )
    if calendar.get("as_of_precision") not in {None, "timestamp"}:
        raise FuturesMainReportError("交易日历证据必须使用带时区时间戳")
    calendar_as_of = _timestamp(calendar.get("as_of"), "market_calendar.as_of")
    calendar_age = Decimal(str((observed_at - calendar_as_of).total_seconds()))
    if not Decimal("0") <= calendar_age <= _decimal(
        policy["market_data_freshness"]["calendar_evidence_max_age_seconds"],
        "交易日历证据年龄上限",
    ):
        raise FuturesMainReportError("交易日历证据必须在报告前一天内取得")
    closed_dates_value = calendar.get("closed_dates")
    if not isinstance(closed_dates_value, list):
        raise FuturesMainReportError("market_calendar.closed_dates 必须是列表")
    closed_dates: set[date] = set()
    for closure_index, closure in enumerate(closed_dates_value):
        if not isinstance(closure, Mapping):
            raise FuturesMainReportError("休市日证据格式错误")
        closure_value = _non_empty_text(
            closure.get("date"),
            f"market_calendar.closed_dates[{closure_index}].date",
        )
        try:
            closure_date = datetime.strptime(closure_value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise FuturesMainReportError("休市日必须是 YYYY-MM-DD") from exc
        if closure_date.weekday() >= 5:
            raise FuturesMainReportError("周末无需伪装成交易所休市日证据")
        _non_empty_text(
            closure.get("reason"),
            f"market_calendar.closed_dates[{closure_index}].reason",
        )
        _exchange_source_url(
            closure.get("source_url"),
            exchange,
            f"market_calendar.closed_dates[{closure_index}].source_url",
        )
        closed_dates.add(closure_date)
    if len(closed_dates) != len(closed_dates_value):
        raise FuturesMainReportError("休市日证据不得重复")
    return closed_dates


def _latest_completed_session_date(
    observed_at: datetime,
    closed_dates: set[date],
) -> date:
    minute = observed_at.hour * 60 + observed_at.minute
    if observed_at.weekday() < 5 and observed_at.date() not in closed_dates and minute >= 15 * 60:
        cursor = observed_at.date()
    else:
        cursor = observed_at.date() - timedelta(days=1)
    while cursor.weekday() >= 5 or cursor in closed_dates:
        cursor -= timedelta(days=1)
    return cursor


def _validate_sources(
    report: Mapping[str, Any],
    observed_at: datetime,
    plan_direction: str,
    contract: str,
) -> str:
    evidence_directions: list[str] = []
    contract_month_match = re.search(r"(\d{3,4})$", contract)
    assert contract_month_match is not None
    contract_month = contract_month_match.group(1)

    def append_direction(value: Any, label: str) -> None:
        if value not in {"bullish", "bearish", "mixed", "neutral", "unobservable"}:
            raise FuturesMainReportError(f"{label}方向状态不受支持")
        if value in {"mixed", "neutral"}:
            evidence_directions.append("mixed")
        elif value == "unobservable":
            evidence_directions.append("unobservable")
        else:
            supports_plan = (
                value == "bullish" if plan_direction == "long" else value == "bearish"
            )
            evidence_directions.append("supportive" if supports_plan else "contrary")

    def validate_affected_months(value: Any, label: str) -> None:
        if not isinstance(value, list) or not value:
            raise FuturesMainReportError(f"{label}必须说明受影响合约月份")
        months = [str(item).strip() for item in value]
        if (
            len(months) != len(set(months))
            or any(re.fullmatch(r"\d{3,4}", item) is None for item in months)
            or contract_month not in months
            or any(not 1 <= int(item[-2:]) <= 12 for item in months)
        ):
            raise FuturesMainReportError(
                f"{label}必须使用合法月份代码并包含当前实际主力月份"
            )

    fundamentals = report.get("fundamentals")
    if not isinstance(fundamentals, list) or not fundamentals:
        raise FuturesMainReportError("报告必须包含基本面汇报")
    for index, item in enumerate(fundamentals):
        if not isinstance(item, Mapping):
            raise FuturesMainReportError("基本面条目格式错误")
        _non_empty_text(item.get("topic"), f"fundamentals[{index}].topic")
        append_direction(item.get("direction"), f"fundamentals[{index}]")
        as_of = _evidence_time(
            item.get("as_of"),
            item.get("as_of_precision"),
            f"fundamentals[{index}].as_of",
        )
        _not_after_observation(as_of, observed_at, f"fundamentals[{index}].as_of")
        _source_url(item.get("source_url"), f"fundamentals[{index}].source_url")
        _non_empty_text(item.get("transmission"), f"fundamentals[{index}].transmission")
        validate_affected_months(
            item.get("affected_contract_months"),
            "基本面条目",
        )

    news = report.get("important_news")
    if not isinstance(news, list) or not news:
        raise FuturesMainReportError("报告必须包含重要新闻")
    for index, item in enumerate(news):
        if not isinstance(item, Mapping):
            raise FuturesMainReportError("新闻条目格式错误")
        if item.get("status") not in {"material", "search_completed_no_material_news"}:
            raise FuturesMainReportError("新闻必须标记 material 或 search_completed_no_material_news")
        append_direction(item.get("direction"), f"important_news[{index}]")
        _non_empty_text(item.get("headline"), f"important_news[{index}].headline")
        news_times: dict[str, datetime | date] = {}
        for field in ("event_at", "published_at", "available_at"):
            news_times[field] = _evidence_time(
                item.get(field),
                item.get(f"{field}_precision"),
                f"important_news[{index}].{field}",
            )
        _not_after_observation(
            news_times["published_at"],
            observed_at,
            f"important_news[{index}].published_at",
        )
        _not_after_observation(
            news_times["available_at"],
            observed_at,
            f"important_news[{index}].available_at",
        )
        published_day = (
            news_times["published_at"].date()
            if isinstance(news_times["published_at"], datetime)
            else news_times["published_at"]
        )
        available_day = (
            news_times["available_at"].date()
            if isinstance(news_times["available_at"], datetime)
            else news_times["available_at"]
        )
        if published_day > available_day or (
            isinstance(news_times["published_at"], datetime)
            and isinstance(news_times["available_at"], datetime)
            and news_times["published_at"] > news_times["available_at"]
        ):
            raise FuturesMainReportError("新闻可得时间不得早于发布时间")
        _source_url(item.get("source_url"), f"important_news[{index}].source_url")
        _non_empty_text(item.get("impact"), f"important_news[{index}].impact")
        _non_empty_text(item.get("transmission"), f"important_news[{index}].transmission")
        validate_affected_months(
            item.get("affected_contract_months"),
            "新闻条目",
        )

    observed_directions = [
        value for value in evidence_directions if value != "unobservable"
    ]
    if not observed_directions:
        derived_modifier = "unobservable"
    elif len(set(observed_directions)) == 1 and observed_directions[0] in {
        "supportive",
        "contrary",
    }:
        derived_modifier = observed_directions[0]
    else:
        derived_modifier = "mixed"
    if report["evidence_modifiers"]["fundamental_and_news"] != derived_modifier:
        raise FuturesMainReportError("基本面与新闻总方向必须由逐条结构化方向推导")
    return derived_modifier


def _validate_member_flow(
    report: Mapping[str, Any],
    policy: Mapping[str, Any],
    contract: str,
    latest_completed_date: date,
    observed_at: datetime,
    plan_direction: str,
    closed_dates: set[date],
    latest_close: Decimal,
    latest_volume: Decimal,
    latest_open_interest: Decimal,
) -> str:
    member_flow = report.get("member_flow")
    if not isinstance(member_flow, Mapping):
        raise FuturesMainReportError("报告必须包含近三日主力资金")
    sessions = member_flow.get("sessions")
    expected_count = int(policy["member_flow"]["completed_session_count"])
    if (
        not isinstance(sessions, list)
        or len(sessions) != expected_count
        or len(set(sessions)) != expected_count
        or sessions != sorted(sessions)
    ):
        raise FuturesMainReportError("主力资金必须使用按时间升序的三个不同完成交易日")
    parsed_sessions: list[date] = []
    for session in sessions:
        if not isinstance(session, str):
            raise FuturesMainReportError("会员资金交易日格式错误")
        try:
            parsed = datetime.strptime(session, "%Y-%m-%d").date()
        except ValueError as exc:
            raise FuturesMainReportError("会员资金交易日必须是 YYYY-MM-DD") from exc
        if parsed.weekday() >= 5:
            raise FuturesMainReportError("会员资金交易日不得落在周末")
        parsed_sessions.append(parsed)
    if parsed_sessions[-1] != latest_completed_date:
        raise FuturesMainReportError("会员资金最后一日必须等于行情最近完成交易日")
    if plan_direction not in {"long", "short"}:
        raise FuturesMainReportError("会员资金方向必须绑定 long 或 short 点位计划")

    expected_latest_sessions: list[date] = []
    cursor = latest_completed_date
    while len(expected_latest_sessions) < expected_count:
        if cursor.weekday() < 5 and cursor not in closed_dates:
            expected_latest_sessions.append(cursor)
        cursor -= timedelta(days=1)
    expected_latest_sessions.reverse()
    if parsed_sessions != expected_latest_sessions:
        raise FuturesMainReportError(
            "主力资金必须是最近三个连续完成交易日；工作日缺口必须附休市证据"
        )

    contexts = member_flow.get("session_context")
    if not isinstance(contexts, list) or len(contexts) != expected_count:
        raise FuturesMainReportError("近三日资金必须附带同合约价格、成交量与总持仓背景")
    context_dates: list[str] = []
    context_close_change_total = Decimal("0")
    context_open_interest_change_total = Decimal("0")
    for index, context in enumerate(contexts):
        if not isinstance(context, Mapping):
            raise FuturesMainReportError("主力资金日背景格式错误")
        if str(context.get("contract", "")).upper() != contract:
            raise FuturesMainReportError("近三日资金不得跨合约直接拼接")
        context_date = _non_empty_text(context.get("date"), f"session_context[{index}].date")
        context_dates.append(context_date)
        for field in (
            "close",
            "close_change",
            "volume",
            "total_open_interest",
            "total_open_interest_change",
        ):
            _decimal(context.get(field), f"session_context[{index}].{field}")
        context_close_change_total += _decimal(
            context.get("close_change"),
            f"session_context[{index}].close_change",
        )
        context_open_interest_change_total += _decimal(
            context.get("total_open_interest_change"),
            f"session_context[{index}].total_open_interest_change",
        )
        _non_empty_text(context.get("price_open_interest_regime"), f"session_context[{index}].price_open_interest_regime")
    if context_dates != sessions:
        raise FuturesMainReportError("主力资金日背景必须与三个交易日一一对应")
    latest_context = contexts[-1]
    if (
        _decimal(latest_context.get("close"), "最后一日资金背景收盘价") != latest_close
        or _decimal(latest_context.get("volume"), "最后一日资金背景成交量") != latest_volume
        or _decimal(
            latest_context.get("total_open_interest"),
            "最后一日资金背景总持仓量",
        )
        != latest_open_interest
    ):
        raise FuturesMainReportError("最后一日资金背景必须与最近完成日线一致")

    allowed_states = set(policy["member_flow"]["group_statuses"])

    def validate_attempts(value: Mapping[str, Any], label: str) -> None:
        attempts = value.get("source_attempts")
        if not isinstance(attempts, list) or not attempts:
            raise FuturesMainReportError(f"{label}部分或不可观测时必须记录来源尝试")
        for attempt_index, attempt in enumerate(attempts):
            if not isinstance(attempt, Mapping):
                raise FuturesMainReportError("会员来源尝试格式错误")
            _non_empty_text(
                attempt.get("source"),
                f"{label}.source_attempts[{attempt_index}].source",
            )
            attempted_at = _timestamp(
                attempt.get("attempted_at"),
                f"{label}.source_attempts[{attempt_index}].attempted_at",
            )
            if attempted_at > observed_at:
                raise FuturesMainReportError("会员来源尝试时间不得晚于报告观察时间")
            _non_empty_text(
                attempt.get("result"),
                f"{label}.source_attempts[{attempt_index}].result",
            )

    def validate_source_urls(value: Mapping[str, Any], label: str) -> None:
        urls = value.get("source_urls")
        if not isinstance(urls, list) or not urls:
            raise FuturesMainReportError(f"{label}有观测值时必须给出来源 URL")
        for source_index, source in enumerate(urls):
            _source_url(source, f"{label}.source_urls[{source_index}]")

    required_metrics = list(policy["member_flow"]["required_metrics"])

    def reject_partial_certainty(value: Any, label: str) -> None:
        conclusion = _non_empty_text(value, label)
        certainty_words = (
            r"(?:确认|确定|明确|无可置疑|肯定|证实|已成定局|必然|足以|"
            r"压倒性|绝对|毫无疑问|毋庸置疑)"
        )
        direction_claims = (
            r"(?:净多|净空|偏多|偏空|看多|看空|多头|空头|买方|卖方|"
            r"做多|做空|利多|利空|多空方向)"
        )
        superiority_claims = (
            r"(?:占(?:据)?(?:压倒性)?(?:上风|优势)|主导|支配|胜过|"
            r"强于|弱于|更强|更弱|力量.{0,8}(?:上风|优势))"
        )
        if (
            re.search(certainty_words, conclusion)
            and re.search(direction_claims, conclusion)
        ) or re.search(superiority_claims, conclusion):
            raise FuturesMainReportError(f"{label}部分可观测时不得声称确定净方向")

    def validate_daily(
        values: Any,
        label: str,
        metrics: list[str] | None = None,
    ) -> Decimal | None:
        active_metrics = required_metrics if metrics is None else metrics
        if not isinstance(values, list) or len(values) != expected_count:
            raise FuturesMainReportError(f"{label}必须包含三个交易日的逐日数据")
        dates: list[str] = []
        imbalance_change_total = Decimal("0")
        for value_index, value in enumerate(values):
            if not isinstance(value, Mapping):
                raise FuturesMainReportError(f"{label}逐日数据格式错误")
            dates.append(_non_empty_text(value.get("date"), f"{label}[{value_index}].date"))
            for field in active_metrics:
                _decimal(value.get(field), f"{label}[{value_index}].{field}")
            for position_field in ("long_open_interest", "short_open_interest"):
                if position_field in active_metrics and _decimal(
                    value.get(position_field),
                    f"{label}[{value_index}].{position_field}",
                ) < 0:
                    raise FuturesMainReportError(f"{label}持仓量不得为负")
            observed_positions = [
                _decimal(value.get(field), f"{label}[{value_index}].{field}")
                for field in ("long_open_interest", "short_open_interest")
                if field in active_metrics
            ]
            if observed_positions and any(position <= 0 for position in observed_positions):
                raise FuturesMainReportError(f"{label}有观测持仓侧必须为正，缺失侧不得补零")
            if {
                "long_open_interest",
                "short_open_interest",
                "member_position_imbalance",
            }.issubset(active_metrics):
                expected_imbalance = _decimal(
                    value.get("long_open_interest"),
                    f"{label}[{value_index}].long_open_interest",
                ) - _decimal(
                    value.get("short_open_interest"),
                    f"{label}[{value_index}].short_open_interest",
                )
                if _decimal(
                    value.get("member_position_imbalance"),
                    f"{label}[{value_index}].member_position_imbalance",
                ) != expected_imbalance:
                    raise FuturesMainReportError(f"{label}会员持仓不平衡必须由多单减空单推导")
            if {"long_change", "short_change", "imbalance_change"}.issubset(active_metrics):
                expected_change = _decimal(
                    value.get("long_change"),
                    f"{label}[{value_index}].long_change",
                ) - _decimal(
                    value.get("short_change"),
                    f"{label}[{value_index}].short_change",
                )
                if _decimal(
                    value.get("imbalance_change"),
                    f"{label}[{value_index}].imbalance_change",
                ) != expected_change:
                    raise FuturesMainReportError(f"{label}不平衡变化必须由多单变化减空单变化推导")
                imbalance_change_total += expected_change
        if dates != sessions:
            raise FuturesMainReportError(f"{label}必须与报告交易日逐日对齐")
        if "imbalance_change" in active_metrics:
            return imbalance_change_total
        return None

    def validate_partial_daily(value: Mapping[str, Any], label: str) -> None:
        observed_metrics = value.get("observed_metrics")
        unobserved_metrics = value.get("unobserved_metrics")
        if (
            not isinstance(observed_metrics, list)
            or not observed_metrics
            or len(observed_metrics) != len(set(observed_metrics))
            or any(metric not in required_metrics for metric in observed_metrics)
        ):
            raise FuturesMainReportError(f"{label}部分可观测时必须列出有效 observed_metrics")
        if (
            not isinstance(unobserved_metrics, list)
            or set(unobserved_metrics) != set(required_metrics) - set(observed_metrics)
            or len(unobserved_metrics) != len(set(unobserved_metrics))
        ):
            raise FuturesMainReportError(f"{label}必须明确列出全部 unobserved_metrics")
        raw_pairs = (
            {"long_open_interest", "long_change"},
            {"short_open_interest", "short_change"},
        )
        if not any(pair.issubset(observed_metrics) for pair in raw_pairs):
            raise FuturesMainReportError(f"{label}部分观测至少要有一侧持仓量及变化")
        if (
            "member_position_imbalance" in observed_metrics
            and not {"long_open_interest", "short_open_interest"}.issubset(observed_metrics)
        ):
            raise FuturesMainReportError(f"{label}缺少多空两侧时不得声称持仓不平衡")
        if (
            "imbalance_change" in observed_metrics
            and not {"long_change", "short_change"}.issubset(observed_metrics)
        ):
            raise FuturesMainReportError(f"{label}缺少多空变化时不得声称不平衡变化")
        daily_values = value.get("daily")
        if not isinstance(daily_values, list):
            raise FuturesMainReportError(f"{label}.daily 必须是列表")
        allowed_row_fields = {"date", *observed_metrics}
        for row in daily_values:
            if not isinstance(row, Mapping) or set(row) != allowed_row_fields:
                raise FuturesMainReportError(
                    f"{label}部分观测逐日行只能包含 date 与 observed_metrics"
                )
        validate_daily(daily_values, f"{label}.daily", observed_metrics)

    def validate_named_member_continuity(
        daily_values: Any,
        label: str,
        observed_metrics: set[str] | None = None,
    ) -> None:
        if not isinstance(daily_values, list):
            raise FuturesMainReportError(f"{label}逐日数据格式错误")
        active_metrics = set(required_metrics) if observed_metrics is None else observed_metrics
        for position_field, change_field in (
            ("long_open_interest", "long_change"),
            ("short_open_interest", "short_change"),
        ):
            if not {position_field, change_field}.issubset(active_metrics):
                continue
            for value_index in range(1, len(daily_values)):
                previous = daily_values[value_index - 1]
                current = daily_values[value_index]
                if _decimal(
                    current.get(position_field),
                    f"{label}[{value_index}].{position_field}",
                ) - _decimal(
                    previous.get(position_field),
                    f"{label}[{value_index - 1}].{position_field}",
                ) != _decimal(
                    current.get(change_field),
                    f"{label}[{value_index}].{change_field}",
                ):
                    raise FuturesMainReportError(
                        f"{label}相邻日持仓差必须等于当日持仓变化"
                    )

    allowed_directions = set(policy["member_flow"]["direction_statuses"])
    for key in ("top20", "domestic_members", "foreign_members"):
        group = member_flow.get(key)
        if not isinstance(group, Mapping) or group.get("status") not in allowed_states:
            raise FuturesMainReportError(
                f"member_flow.{key} 必须标记 available、partial 或 unobservable"
            )
        if group.get("direction") not in allowed_directions:
            raise FuturesMainReportError(f"member_flow.{key} 缺少结构化方向判断")
        if group["status"] == "unobservable" and group["direction"] != "unobservable":
            raise FuturesMainReportError(f"member_flow.{key} 不可观测时方向也必须不可观测")
        if group["status"] == "partial" and group["direction"] != "mixed":
            raise FuturesMainReportError(
                f"member_flow.{key} 部分可观测时只能标记 mixed，不能声称确定净方向"
            )
        if group["status"] in {"partial", "unobservable"}:
            reject_partial_certainty(
                group.get("conclusion"),
                f"member_flow.{key}.conclusion",
            )
        else:
            _non_empty_text(group.get("conclusion"), f"member_flow.{key}.conclusion")
        if group["status"] in {"available", "partial"}:
            validate_source_urls(group, f"member_flow.{key}")
        if group["status"] in {"partial", "unobservable"}:
            validate_attempts(group, f"member_flow.{key}")

    def direction_from_imbalance_change(change: Decimal) -> str:
        if change == 0:
            return "mixed"
        supports_plan = change > 0 if plan_direction == "long" else change < 0
        return "supportive" if supports_plan else "contrary"

    derived_group_directions: dict[str, str] = {}

    top20 = member_flow["top20"]
    if top20["status"] == "available":
        top20_change = validate_daily(top20.get("daily"), "member_flow.top20.daily")
        assert top20_change is not None
        derived_group_directions["top20"] = direction_from_imbalance_change(
            top20_change
        )
    elif top20["status"] == "partial":
        validate_partial_daily(top20, "member_flow.top20")
        derived_group_directions["top20"] = "mixed"
    else:
        derived_group_directions["top20"] = "unobservable"
    if top20["direction"] != derived_group_directions["top20"]:
        raise FuturesMainReportError("Top20方向必须由三日不平衡变化和点位方向推导")

    for key, configured_names in (
        ("domestic_members", policy["member_flow"]["domestic_members"]),
        ("foreign_members", policy["member_flow"]["foreign_members"]),
    ):
        group = member_flow[key]
        if group["status"] == "unobservable":
            derived_group_directions[key] = "unobservable"
            continue
        members = group.get("members")
        if not isinstance(members, list):
            raise FuturesMainReportError(f"member_flow.{key}.members 必须是列表")
        names = [member.get("name") for member in members if isinstance(member, Mapping)]
        if names != configured_names:
            raise FuturesMainReportError(f"member_flow.{key} 必须按配置覆盖全部重点席位")
        observed_member_count = 0
        unavailable_member_count = 0
        complete_member_directions: list[str] = []
        for member_index, member in enumerate(members):
            if not isinstance(member, Mapping):
                raise FuturesMainReportError(f"member_flow.{key}.members 格式错误")
            label = f"member_flow.{key}.members[{member_index}]"
            member_status = member.get("status", "available")
            if group["status"] == "available" and member_status != "available":
                raise FuturesMainReportError(f"{label}在完整可用组中必须是 available")
            if member_status not in allowed_states:
                raise FuturesMainReportError(f"{label}.status 不受支持")
            if member_status in {"partial", "unobservable"}:
                reject_partial_certainty(
                    member.get("conclusion"),
                    f"{label}.conclusion",
                )
            else:
                _non_empty_text(member.get("conclusion"), f"{label}.conclusion")
            if member_status == "available":
                observed_member_count += 1
                member_daily = member.get("daily")
                member_change = validate_daily(member_daily, f"{label}.daily")
                validate_named_member_continuity(member_daily, f"{label}.daily")
                assert member_change is not None
                complete_member_directions.append(
                    direction_from_imbalance_change(member_change)
                )
            elif member_status == "partial":
                observed_member_count += 1
                unavailable_member_count += 1
                validate_attempts(member, label)
                validate_partial_daily(member, label)
                validate_named_member_continuity(
                    member.get("daily"),
                    f"{label}.daily",
                    set(member["observed_metrics"]),
                )
            else:
                unavailable_member_count += 1
                validate_attempts(member, label)
        if group["status"] == "partial" and (
            observed_member_count == 0 or unavailable_member_count == 0
        ):
            raise FuturesMainReportError(
                f"member_flow.{key} 标记 partial 时必须同时存在已观测和未完整观测席位"
            )
        derived_group_directions[key] = (
            "mixed"
            if group["status"] == "partial"
            else complete_member_directions[0]
            if len(set(complete_member_directions)) == 1
            else "mixed"
        )
        if group["direction"] != derived_group_directions[key]:
            raise FuturesMainReportError(
                f"member_flow.{key} 方向必须由完整逐日数据推导"
            )

    if context_open_interest_change_total <= 0 or context_close_change_total == 0:
        context_direction = "mixed"
    else:
        context_supports_plan = (
            context_close_change_total > 0
            if plan_direction == "long"
            else context_close_change_total < 0
        )
        context_direction = "supportive" if context_supports_plan else "contrary"
    member_direction_values = list(derived_group_directions.values())
    group_direction_values = [*member_direction_values, context_direction]
    if all(value == "unobservable" for value in member_direction_values):
        derived_modifier = "unobservable"
    elif any(value in {"mixed", "unobservable"} for value in group_direction_values):
        derived_modifier = "mixed"
    elif len(set(group_direction_values)) == 1:
        derived_modifier = group_direction_values[0]
    else:
        derived_modifier = "mixed"
    modifier_status = report["evidence_modifiers"]["three_day_member_flow"]
    if modifier_status != derived_modifier:
        raise FuturesMainReportError("近三日会员资金总方向必须由三组结构化方向推导")
    return derived_modifier


def _validate_candidate_comparison(
    report: Mapping[str, Any],
    action: str,
    contract: str,
    plan_direction: str,
) -> None:
    comparison = report.get("candidate_comparison")
    if not isinstance(comparison, Mapping):
        raise FuturesMainReportError("报告必须包含选中/拒绝原因")
    focus = comparison.get("focus")
    if not isinstance(focus, Mapping):
        raise FuturesMainReportError("候选比较缺少 focus")
    if str(focus.get("contract", "")).upper() != contract:
        raise FuturesMainReportError("focus 合约必须与顶层实际主力一致")
    focus_direction = focus.get("direction")
    if focus_direction not in {"long", "short"}:
        raise FuturesMainReportError("focus 必须给出 long 或 short 方向")
    if focus_direction != plan_direction:
        raise FuturesMainReportError("focus 方向必须与条件点位计划一致")
    if action in {"long", "short"} and focus_direction != action:
        raise FuturesMainReportError("focus 方向必须与非等待动作一致")
    focus_status = focus.get("status")
    expected_status = "selected" if action in {"long", "short"} else "watch"
    if focus_status != expected_status:
        raise FuturesMainReportError(f"当前动作要求 focus.status={expected_status}")
    reasons = focus.get("reasons")
    if not isinstance(reasons, list) or not reasons or any(not str(item).strip() for item in reasons):
        raise FuturesMainReportError("候选必须给出明确选中或观察原因")
    rejected = comparison.get("rejected")
    if not isinstance(rejected, list):
        raise FuturesMainReportError("candidate_comparison.rejected 必须是列表")
    if report.get("scan_scope") == "all_market" and not rejected:
        raise FuturesMainReportError("全市场报告必须列出至少一个被拒绝候选及原因")
    for index, item in enumerate(rejected):
        if not isinstance(item, Mapping):
            raise FuturesMainReportError("拒绝候选格式错误")
        _non_empty_text(item.get("contract"), f"rejected[{index}].contract")
        item_reasons = item.get("reasons")
        if not isinstance(item_reasons, list) or not item_reasons:
            raise FuturesMainReportError("拒绝候选必须给出原因")


def _tick_aligned(value: Decimal, tick_size: Decimal) -> bool:
    return value % tick_size == 0


def _validate_point_plan(
    report: Mapping[str, Any],
    policy: Mapping[str, Any],
    action: str,
    levels: Mapping[str, Any],
    tick_size: Decimal,
    current_main_price: Decimal,
    latest_completed_close: Decimal,
) -> Decimal:
    plan = report.get("point_plan")
    if not isinstance(plan, Mapping):
        raise FuturesMainReportError("报告必须包含具体点位计划")
    direction = plan.get("direction")
    if direction not in {"long", "short"}:
        raise FuturesMainReportError("点位方向必须是 long 或 short")
    if action in {"long", "short"} and action != direction:
        raise FuturesMainReportError("非等待动作必须与点位方向一致")
    setup_mode = plan.get("setup_mode")
    if setup_mode not in {"trend_continuation", "breakout_ignition", "trend_reversal"}:
        raise FuturesMainReportError("点位计划缺少受支持的竞争模式")
    confirmation_status = plan.get("confirmation_status")
    if confirmation_status not in {"confirmed", "pending", "invalid"}:
        raise FuturesMainReportError("点位确认状态必须是 confirmed、pending 或 invalid")
    if action in {"long", "short"} and confirmation_status != "confirmed":
        raise FuturesMainReportError("非等待动作必须已有完成确认")
    _non_empty_text(plan.get("trigger"), "点位触发条件")
    trigger_price = _decimal(plan.get("trigger_price"), "触发价")
    entry_lower, entry_upper = _validate_zone(plan.get("entry_zone"), "入场区间")
    invalidation = _decimal(plan.get("invalidation"), "失效位")
    target = _decimal(plan.get("target"), "目标位")
    reported_ratio = _decimal(plan.get("reference_reward_risk"), "参考收益风险比")
    if reported_ratio <= 0:
        raise FuturesMainReportError("参考收益风险比必须为正数")
    _non_empty_text(plan.get("valid_until_or_recalculate_condition"), "计划有效期或重算条件")

    if direction == "long":
        entry_reference = entry_upper
        risk = entry_reference - invalidation
        reward = target - entry_reference
    else:
        entry_reference = entry_lower
        risk = invalidation - entry_reference
        reward = entry_reference - target
    if risk <= 0:
        raise FuturesMainReportError("失效位必须位于入场区间的不利方向")
    if reward <= 0:
        raise FuturesMainReportError("目标位必须位于入场区间的有利方向")
    support_lower, support_upper = _validate_zone(levels.get("support"), "最强支撑")
    resistance_lower, resistance_upper = _validate_zone(levels.get("resistance"), "最强压力")
    if support_upper >= resistance_lower:
        raise FuturesMainReportError("最强支撑必须严格低于最强压力")
    aligned_values = [
        support_lower,
        support_upper,
        resistance_lower,
        resistance_upper,
        _decimal(levels.get("trend"), "趋势位"),
        trigger_price,
        entry_lower,
        entry_upper,
        invalidation,
        target,
    ]
    if any(not _tick_aligned(value, tick_size) for value in aligned_values):
        raise FuturesMainReportError("结构位与计划点位必须按最小变动价位对齐")
    if setup_mode == "breakout_ignition":
        if direction == "long":
            if trigger_price != resistance_upper + tick_size:
                raise FuturesMainReportError("向上突破触发价必须是冻结压力区上沿外一个tick")
            if entry_lower < trigger_price:
                raise FuturesMainReportError("多头突破入场区间必须位于触发价上方")
            if invalidation != resistance_lower - tick_size:
                raise FuturesMainReportError("多头突破失效位必须是冻结压力区下沿外一个tick")
        else:
            if trigger_price != support_lower - tick_size:
                raise FuturesMainReportError("向下突破触发价必须是冻结支撑区下沿外一个tick")
            if entry_upper > trigger_price:
                raise FuturesMainReportError("空头突破入场区间必须位于触发价下方")
            if invalidation != support_upper + tick_size:
                raise FuturesMainReportError("空头突破失效位必须是冻结支撑区上沿外一个tick")
    else:
        if not support_upper < entry_lower <= entry_upper < resistance_lower:
            raise FuturesMainReportError("非突破计划入场区间必须位于最强支撑与压力之间")
        if not entry_lower <= trigger_price <= entry_upper:
            raise FuturesMainReportError("非突破计划触发价必须落在入场区间")
        if direction == "long":
            if invalidation != support_lower - tick_size:
                raise FuturesMainReportError("非突破多头失效位必须是最强支撑下沿外一个tick")
            if target != resistance_lower:
                raise FuturesMainReportError("非突破多头目标必须是最强压力下沿")
        else:
            if invalidation != resistance_upper + tick_size:
                raise FuturesMainReportError("非突破空头失效位必须是最强压力上沿外一个tick")
            if target != support_upper:
                raise FuturesMainReportError("非突破空头目标必须是最强支撑上沿")

    if "previously_confirmed_on" in plan:
        raise FuturesMainReportError(
            "裸 previously_confirmed_on 已禁用；历史确认必须改用v3并绑定结构、确认K线与后续K线证据"
        )
    currently_confirmed = (
        latest_completed_close >= trigger_price
        if direction == "long"
        else latest_completed_close <= trigger_price
    )
    derived_confirmation = (
        "confirmed"
        if currently_confirmed
        else "pending"
    )
    if confirmation_status != derived_confirmation:
        raise FuturesMainReportError("确认状态必须由最近完成日线收盘与触发/失效位推导")
    structure_gate = report["hard_gates"]["structure_and_direction"]
    expected_structure_gate = {
        "confirmed": "pass",
        "pending": "pending",
        "invalid": "fail",
    }[derived_confirmation]
    if structure_gate != expected_structure_gate:
        raise FuturesMainReportError("结构门禁必须由最近完成收盘推导，不得自行覆盖")

    minimum = _decimal(policy["point_plan"]["minimum_reference_reward_risk"], "最低参考收益风险比")
    if setup_mode == "breakout_ignition":
        exact_target = (
            entry_reference + minimum * risk
            if direction == "long"
            else entry_reference - minimum * risk
        )
        if target != exact_target:
            raise FuturesMainReportError("突破目标必须等于保守入场边界计算的最低2R目标")
    if confirmation_status == "confirmed" and not (
        entry_lower <= current_main_price <= entry_upper
    ):
        raise FuturesMainReportError("已确认计划的当前主力价格必须仍在入场区间")
    if confirmation_status == "pending":
        if direction == "long" and current_main_price > entry_upper:
            raise FuturesMainReportError("待确认多头计划的主力价格已经越过入场区间，必须重算")
        if direction == "short" and current_main_price < entry_lower:
            raise FuturesMainReportError("待确认空头计划的主力价格已经越过入场区间，必须重算")
    computed_ratio = reward / risk
    if abs(reported_ratio - computed_ratio) > Decimal("0.000001"):
        raise FuturesMainReportError("报告收益风险比必须等于按保守入场边界重算值")

    ratio_gate = report["hard_gates"]["point_plan_reward_risk"]
    ratio_passes = reported_ratio >= minimum and computed_ratio >= minimum
    expected_ratio_gate = "pass" if ratio_passes else "fail"
    if ratio_gate != expected_ratio_gate:
        raise FuturesMainReportError("点位收益风险门禁必须由保守边界重算结果推导")
    return computed_ratio


def _exact_integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    number = _decimal(value, label)
    if number != number.to_integral_value():
        raise FuturesMainReportError(f"{label} 必须是精确整数")
    result = int(number)
    if minimum is not None and result < minimum:
        raise FuturesMainReportError(f"{label} 不得小于 {minimum}")
    return result


def _date_value(value: Any, label: str) -> date:
    text_value = _non_empty_text(value, label)
    try:
        return datetime.strptime(text_value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise FuturesMainReportError(f"{label} 必须是 YYYY-MM-DD") from exc


def _validate_contract_for_product(
    value: Any,
    product: str,
    exchange: str,
    label: str,
    *,
    reference_date: date | None = None,
) -> str:
    contract = _non_empty_text(value, label).upper()
    try:
        # Every v3 artifact uses the same unambiguous PRODUCTYYMM form.  When a
        # fact observation date is known, format validity is insufficient: an
        # expired century alias such as PB9912 must not become believable merely
        # because a caller re-signed the surrounding JSON.  The provider-facing
        # lifecycle is therefore constrained to the inclusive 0..48 month
        # horizon at the timestamp that produced this particular fact.
        canonical = canonical_delivery_contract(
            contract,
            exchange=exchange,
            reference_date=reference_date,
            allow_czce_three_digit=False,
        )
        if canonical != contract or product_prefix(canonical) != product:
            raise ValueError("contract is not the requested product")
    except ValueError as exc:
        raise FuturesMainReportError(
            f"{label} 不是 {product} 在对应观察时点可观测的具体月份合约：{exc}"
        ) from exc
    return contract


def _validate_evidence_source(
    value: Any,
    observed_at: datetime,
    label: str,
    *,
    official_exchange: str | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuturesMainReportError(f"{label} 必须是结构化来源证据")
    if official_exchange is None:
        _source_url(value.get("url"), f"{label}.url")
    else:
        _exchange_source_url(value.get("url"), official_exchange, f"{label}.url")
    retrieved_at = _timestamp(value.get("retrieved_at"), f"{label}.retrieved_at")
    if retrieved_at > observed_at:
        raise FuturesMainReportError(f"{label}.retrieved_at 不得晚于报告观察时间")
    _sha256_text(value.get("content_sha256"), f"{label}.content_sha256")
    _non_empty_text(value.get("locator"), f"{label}.locator")
    return value


def _validate_tick_size_evidence_v3(
    value: Any,
    *,
    contract: str,
    tick_size: Decimal,
    exact: bool,
    observed_at: datetime,
    not_before: datetime | None = None,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuturesMainReportError(f"{label}必须是结构化tick来源证据")
    kind = value.get("kind")
    common_fields = {
        "kind",
        "contract",
        "provider_symbol",
        "retrieved_at",
        "content_sha256",
        "hash_scope",
        "locator",
        "raw_item",
        "raw_value",
        "tick_size",
        "exact",
    }
    if kind == "actual_contract_http_detail":
        expected_fields = set(EXACT_TICK_EVIDENCE_FIELDS)
    elif kind == "product_specs_snapshot_proxy":
        expected_fields = common_fields | {"source_path"}
    else:
        raise FuturesMainReportError(f"{label}.kind不受支持")
    if set(value) != expected_fields:
        raise FuturesMainReportError(f"{label}必须完整绑定tick证据字段")
    if _raw_contract_symbol(value.get("contract")) != contract:
        raise FuturesMainReportError(f"{label}.contract必须绑定analysis_contract")
    retrieved_at = _timestamp(value.get("retrieved_at"), f"{label}.retrieved_at")
    if retrieved_at > observed_at:
        raise FuturesMainReportError(f"{label}.retrieved_at不得晚于相关快照完成时间")
    if not_before is not None and retrieved_at < not_before:
        raise FuturesMainReportError(f"{label}.retrieved_at不得早于本次raw请求开始时间")
    _sha256_text(value.get("content_sha256"), f"{label}.content_sha256")
    _non_empty_text(value.get("locator"), f"{label}.locator")
    raw_item = _non_empty_text(value.get("raw_item"), f"{label}.raw_item")
    raw_value = _non_empty_text(value.get("raw_value"), f"{label}.raw_value")
    evidence_tick = _decimal(value.get("tick_size"), f"{label}.tick_size")
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", raw_value)
    try:
        reproduced_tick = None if match is None else Decimal(match.group(0))
    except InvalidOperation as exc:  # defensive even though the regex is numeric
        raise FuturesMainReportError(f"{label}.raw_value无法重算tick_size") from exc
    if reproduced_tick != evidence_tick:
        raise FuturesMainReportError(f"{label}.raw_value必须可重算tick_size")
    if evidence_tick != tick_size or value.get("exact") is not exact:
        raise FuturesMainReportError(f"{label}的tick数值/exact与结构声明不一致")

    if exact:
        if kind != "actual_contract_http_detail":
            raise FuturesMainReportError("exact tick只能来自实际合约HTTP详情原文")
        provider_symbol = _non_empty_text(
            value.get("provider_symbol"), f"{label}.provider_symbol"
        ).upper()
        expected_provider_symbol = sina_provider_symbol_for_contract(contract)
        if provider_symbol != expected_provider_symbol:
            raise FuturesMainReportError(f"{label}.provider_symbol与合约品种不一致")
        expected_source_url = (
            f"https://finance.sina.com.cn/futures/quotes/{provider_symbol}.shtml"
        )
        if _source_url(value.get("source_url"), f"{label}.source_url") != expected_source_url:
            raise FuturesMainReportError(f"{label}.source_url未绑定新浪实际合约详情页")
        if value.get("hash_scope") != "http_response_body":
            raise FuturesMainReportError(f"{label}.hash_scope必须绑定HTTP响应体")
        if raw_item != "最小变动价位" or value.get("locator") != (
            "contract detail table item=最小变动价位"
        ):
            raise FuturesMainReportError(f"{label}.raw_item必须是最小变动价位")
        try:
            replay_exact_tick_evidence(
                value,
                contract=contract,
                expected_tick_size=tick_size,
            )
        except (TickEvidenceError, ValueError) as exc:
            raise FuturesMainReportError(
                f"{label}的HTTP原文归档无法独立重放实际合约tick"
            ) from exc
    else:
        if kind != "product_specs_snapshot_proxy":
            raise FuturesMainReportError("proxy tick必须来自当前product specs快照")
        if value.get("provider_symbol") is not None:
            raise FuturesMainReportError(f"{label}.provider_symbol在proxy来源下必须为null")
        source_path = Path(
            _non_empty_text(value.get("source_path"), f"{label}.source_path")
        )
        if source_path.resolve() != PRODUCT_SPECS_PATH.resolve():
            raise FuturesMainReportError(f"{label}.source_path未绑定当前product specs")
        if value.get("content_sha256") != _file_sha256(PRODUCT_SPECS_PATH):
            raise FuturesMainReportError(f"{label}.content_sha256未绑定当前product specs字节")
        if value.get("hash_scope") != "complete_product_specs_file":
            raise FuturesMainReportError(f"{label}.hash_scope必须覆盖完整product specs文件")
        product_match = re.match(r"[A-Z]+", contract)
        assert product_match is not None
        if value.get("locator") != f"products[symbol={product_match.group(0)}].tick_size":
            raise FuturesMainReportError(f"{label}.locator未定位当前品种tick")
        if raw_item != "tick_size":
            raise FuturesMainReportError(f"{label}.raw_item在proxy来源下必须为tick_size")
    return value


def _validate_source_attempts_v3(
    value: Any,
    observed_at: datetime,
    label: str,
    *,
    official_exchange: str | None = None,
) -> None:
    if not isinstance(value, list) or not value:
        raise FuturesMainReportError(f"{label} 必须记录至少一次来源尝试")
    for index, attempt in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(attempt, Mapping):
            raise FuturesMainReportError(f"{item_label} 格式错误")
        if official_exchange is None:
            _source_url(attempt.get("url"), f"{item_label}.url")
        else:
            _exchange_source_url(
                attempt.get("url"), official_exchange, f"{item_label}.url"
            )
        attempted_at = _timestamp(
            attempt.get("attempted_at"), f"{item_label}.attempted_at"
        )
        if attempted_at > observed_at:
            raise FuturesMainReportError(f"{item_label} 不得晚于报告观察时间")
        _non_empty_text(attempt.get("locator"), f"{item_label}.locator")
        _non_empty_text(attempt.get("result"), f"{item_label}.result")


def _validate_analysis_target_v3(
    report: Mapping[str, Any],
    observed_at: datetime,
) -> tuple[str, str, str | None, str, Decimal]:
    target = report.get("target")
    if not isinstance(target, Mapping):
        raise FuturesMainReportError("v3报告缺少 target")
    product = _non_empty_text(target.get("product"), "target.product").upper()
    if product not in _allowed_products():
        raise FuturesMainReportError("分析品种不在当前配置品种清单")
    spec = _configured_product_spec(product)
    exchange = _non_empty_text(spec.get("exchange"), f"品种 {product} 交易所")
    mode = target.get("mode")
    if mode not in {"actual_main", "user_specified_contract"}:
        raise FuturesMainReportError("target.mode 必须是 actual_main 或 user_specified_contract")
    analysis_contract = _validate_contract_for_product(
        target.get("analysis_contract"),
        product,
        exchange,
        "target.analysis_contract",
        reference_date=observed_at.date(),
    )
    actual_main_value = target.get("actual_main_contract")
    actual_main_contract = (
        None
        if actual_main_value is None
        else _validate_contract_for_product(
            actual_main_value,
            product,
            exchange,
            "target.actual_main_contract",
            reference_date=observed_at.date(),
        )
    )
    if mode == "actual_main":
        if actual_main_contract is None or analysis_contract != actual_main_contract:
            raise FuturesMainReportError("actual_main模式必须分析本次识别的实际主力")
        if target.get("requested_contract") is not None:
            raise FuturesMainReportError("actual_main模式不得伪造用户指定合约")
    else:
        requested = _validate_contract_for_product(
            target.get("requested_contract"),
            product,
            exchange,
            "target.requested_contract",
            reference_date=observed_at.date(),
        )
        if requested != analysis_contract:
            raise FuturesMainReportError("用户指定合约必须等于 analysis_contract")
        _non_empty_text(target.get("roll_context"), "target.roll_context")
    tick_size = _decimal(report.get("tick_size"), "最小变动价位")
    if tick_size <= 0 or tick_size != _configured_tick_size(product):
        raise FuturesMainReportError("报告tick必须为正并与品种合约规格一致")
    return product, analysis_contract, actual_main_contract, exchange, tick_size


def _verify_relative_artifact(
    path_value: Any,
    expected_sha256: str,
    artifact_root: Path | None,
    label: str,
) -> bool:
    """Verify a scanner artifact when the report file supplies a path base.

    Mapping-only API callers have no trustworthy base directory.  They still
    receive canonical manifest/hash validation, while the CLI additionally
    verifies exact referenced file bytes relative to the report directory.
    """
    path_text = _non_empty_text(path_value, label)
    relative = Path(path_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise FuturesMainReportError(f"{label}必须是manifest/report目录内的相对路径")
    if artifact_root is None:
        return False
    root = artifact_root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FuturesMainReportError(f"{label}越出报告证据目录") from exc
    if not candidate.is_file():
        raise FuturesMainReportError(f"{label}指向的证据文件不存在：{candidate}")
    if _file_sha256(candidate) != expected_sha256:
        raise FuturesMainReportError(f"{label}指向文件的字节SHA-256不一致")
    return True


def _load_verified_json_artifact(
    path_value: Any,
    expected_sha256: str,
    artifact_root: Path | None,
    label: str,
) -> Mapping[str, Any] | None:
    """Return verified JSON bytes in CLI mode; mapping-only validation stays explicit."""
    verified = _verify_relative_artifact(
        path_value, expected_sha256, artifact_root, label
    )
    if not verified:
        return None
    assert artifact_root is not None
    path = (artifact_root.resolve() / Path(str(path_value))).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FuturesMainReportError(f"{label}不是可读的JSON证据") from exc
    if not isinstance(value, Mapping):
        raise FuturesMainReportError(f"{label}的JSON根必须是对象")
    return value


def _raw_contract_symbol(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("symbol")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper()


def _raw_completed_as_of(raw: Mapping[str, Any]) -> date | None:
    technical = raw.get("technical")
    if not isinstance(technical, Mapping):
        return None
    for field in ("last_daily_date", "latest_completed_date"):
        value = technical.get(field)
        if isinstance(value, str) and value.strip():
            return _date_value(value, f"raw.technical.{field}")
    rows = technical.get("completed_daily_rows")
    if not isinstance(rows, list):
        return None
    dates = [
        _date_value(row.get("date", row.get("at")), "raw completed bar date")
        for row in rows
        if isinstance(row, Mapping) and row.get("completed") is True
    ]
    return max(dates) if dates else None


def _raw_observed_at(value: Any, label: str) -> datetime:
    text_value = _non_empty_text(value, label).replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError as exc:
        raise FuturesMainReportError(f"{label}不是可解析时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
    return parsed.astimezone(BEIJING_TIMEZONE)


def _validate_scan_row_artifact_content_v3(
    item: Mapping[str, Any],
    *,
    label: str,
    symbol: str,
    exchange: str,
    configured_tick: Decimal,
    artifact_root: Path | None,
    attempted_at: str,
) -> None:
    """Cross-check every observable scan-row claim against its exact artifacts.

    Partial is an uncertainty state, not permission to loosen facts already
    observed.  This replay therefore applies equally to success and partial
    rows whenever the scanner persisted successful raw/structure artifacts.
    """

    if artifact_root is None:
        return
    raw_sha = _sha256_text(
        item.get("raw_snapshot_sha256"), f"{label}.raw_snapshot_sha256"
    )
    structure_sha = _sha256_text(
        item.get("structure_snapshot_sha256"),
        f"{label}.structure_snapshot_sha256",
    )
    raw = _load_verified_json_artifact(
        item.get("raw_snapshot_path"),
        raw_sha,
        artifact_root,
        f"{label}.raw_snapshot_path",
    )
    structure = _load_verified_json_artifact(
        item.get("structure_snapshot_path"),
        structure_sha,
        artifact_root,
        f"{label}.structure_snapshot_path",
    )
    assert raw is not None and structure is not None

    raw_succeeded = raw.get("success") is True
    structure_succeeded = structure.get("success") is True
    if item.get("scan_status") in {"success", "partial"} and not (
        raw_succeeded and structure_succeeded
    ):
        raise FuturesMainReportError(
            f"{label}.scan_status不得把失败raw/structure重标为可观测证据"
        )
    if not raw_succeeded:
        if (
            item.get("scan_status") != "unobservable"
            or item.get("data_status") != "unobservable"
            or item.get("structure_status") != "unobservable"
        ):
            raise FuturesMainReportError(
                f"{label}失败raw必须保持scan/data/structure不可观测"
            )
        for field in (
            "actual_main_contract",
            "declared_actual_main_contract",
            "analysis_contract",
            "fetch_time",
            "as_of",
            "daily_data_evidence_status",
        ):
            if item.get(field) is not None:
                raise FuturesMainReportError(
                    f"{label}.{field}不得从失败raw中伪造"
                )
    if not structure_succeeded and item.get("structure_status") != "unobservable":
        raise FuturesMainReportError(
            f"{label}.structure_status不得把失败structure重标为可观测"
        )

    expected_structure_identity = {
        "structure_schema_version": structure.get("schema_version"),
        "structure_algorithm_version": structure.get("algorithm_version"),
        "structure_actual_main_contract": _raw_contract_symbol(
            structure.get("actual_main")
        ),
        "structure_analysis_contract": _raw_contract_symbol(
            structure.get("analysis_contract")
        ),
        "structure_tick_size": structure.get("tick_size"),
        "structure_tick_size_contract": _raw_contract_symbol(
            structure.get("tick_size_contract")
        ),
        "structure_tick_size_source": structure.get("tick_size_source"),
        "structure_tick_size_exact": structure.get("tick_size_exact"),
        "structure_tick_size_precision": structure.get("tick_size_precision"),
        "structure_tick_size_actionability": structure.get(
            "tick_size_actionability"
        ),
        "structure_tick_size_evidence": structure.get("tick_size_evidence"),
        "structure_input_snapshot_sha256": structure.get(
            "input_snapshot_sha256"
        ),
    }
    for row_field, expected in expected_structure_identity.items():
        actual = item.get(row_field)
        if row_field in {
            "structure_actual_main_contract",
            "structure_analysis_contract",
            "structure_tick_size_contract",
        } and actual is not None:
            actual = str(actual).upper()
        if actual != expected:
            raise FuturesMainReportError(
                f"{label}.{row_field}与structure artifact不一致"
            )

    if raw_succeeded:
        raw_fetch_time = _timestamp(raw.get("fetch_time"), f"{label}.raw.fetch_time")
        row_fetch_time = _timestamp(item.get("fetch_time"), f"{label}.fetch_time")
        if raw_fetch_time != row_fetch_time:
            raise FuturesMainReportError(f"{label}.fetch_time与raw artifact不一致")
        contract_reference_date = raw_fetch_time.date()
        declared_main = _raw_contract_symbol(
            raw.get("actual_main", raw.get("main_contract"))
        )
        row_declared = item.get("declared_actual_main_contract")
        normalized_row_declared = (
            None if row_declared is None else str(row_declared).upper()
        )
        if declared_main != normalized_row_declared:
            raise FuturesMainReportError(f"{label}声明主力与raw artifact不一致")
        if declared_main is not None:
            _validate_contract_for_product(
                declared_main,
                symbol,
                exchange,
                f"{label}.raw.declared_actual_main",
                reference_date=contract_reference_date,
            )

        raw_analysis = _raw_contract_symbol(
            raw.get("analysis_contract", raw.get("actual_main", raw.get("main_contract")))
        )
        row_analysis = item.get("analysis_contract")
        normalized_row_analysis = (
            None if row_analysis is None else str(row_analysis).upper()
        )
        if raw_analysis != normalized_row_analysis:
            raise FuturesMainReportError(f"{label}.analysis_contract与raw artifact不一致")
        if raw_analysis is not None:
            _validate_contract_for_product(
                raw_analysis,
                symbol,
                exchange,
                f"{label}.raw.analysis_contract",
                reference_date=contract_reference_date,
            )

        raw_chain = raw.get("actual_contract_chain")
        if not isinstance(raw_chain, list):
            raise FuturesMainReportError(f"{label}.raw缺少actual_contract_chain")
        ranked: list[tuple[str, int, int | None]] = []
        for chain_index, row in enumerate(raw_chain):
            if not isinstance(row, Mapping):
                raise FuturesMainReportError(
                    f"{label}.raw.actual_contract_chain[{chain_index}]格式错误"
                )
            contract = _raw_contract_symbol(row)
            if contract is None:
                raise FuturesMainReportError(f"{label}.raw合约链缺少symbol")
            _validate_contract_for_product(
                contract,
                symbol,
                exchange,
                f"{label}.raw.actual_contract_chain[{chain_index}].symbol",
                reference_date=contract_reference_date,
            )
            try:
                oi = _decimal(
                    row.get("open_interest"),
                    f"{label}.raw.actual_contract_chain[{chain_index}].open_interest",
                )
            except FuturesMainReportError:
                continue
            if oi <= 0 or oi != oi.to_integral_value():
                continue
            volume_value = row.get("volume")
            volume = (
                None
                if volume_value is None
                else _exact_integer(
                    volume_value,
                    f"{label}.raw.actual_contract_chain[{chain_index}].volume",
                    minimum=0,
                )
            )
            ranked.append((contract, int(oi), volume))
        if len({row[0] for row in ranked}) != len(ranked):
            raise FuturesMainReportError(f"{label}.raw可排名合约不得重复")
        ranked.sort(
            key=lambda row: (
                -row[1],
                -(row[2] if row[2] is not None else -1),
                row[0],
            )
        )
        recomputed_main = ranked[0][0] if ranked else None
        row_main = item.get("actual_main_contract")
        normalized_row_main = None if row_main is None else str(row_main).upper()
        if normalized_row_main != recomputed_main:
            raise FuturesMainReportError(f"{label}.actual_main_contract与raw全链重算不一致")

        technical = raw.get("technical")
        if isinstance(technical, Mapping):
            raw_tick = _decimal(
                technical.get("tick_size"), f"{label}.raw.technical.tick_size"
            )
            if raw_tick != configured_tick and not any(
                str(gap).startswith("raw_tick_size_config_conflict")
                for gap in item.get("evidence_gaps", [])
            ):
                raise FuturesMainReportError(
                    f"{label}.raw tick与当前品种规格冲突却未披露gap"
                )
            if raw_analysis is not None and _raw_contract_symbol(
                technical.get("tick_size_contract")
            ) != raw_analysis:
                raise FuturesMainReportError(f"{label}.raw tick合约未绑定analysis_contract")
            if item.get("structure_tick_size_source") is not None:
                if technical.get("tick_size_source") != item.get(
                    "structure_tick_size_source"
                ) and "structure_tick_size_source_conflict" not in item.get(
                    "evidence_gaps", []
                ):
                    raise FuturesMainReportError(f"{label}.raw tick来源与scan row不一致")
            raw_daily = technical.get("daily_data_evidence")
            raw_daily_status = (
                raw_daily.get("status") if isinstance(raw_daily, Mapping) else None
            )
            reported_daily_status = item.get("daily_data_evidence_status")
            if raw_daily_status != reported_daily_status and not (
                isinstance(reported_daily_status, str)
                and reported_daily_status.startswith(f"conflict:{raw_daily_status}:")
            ):
                raise FuturesMainReportError(f"{label}.日线证据状态与raw artifact不一致")

    if structure_succeeded:
        expected_structure_values = {
            "structure_schema_version": structure.get("schema_version"),
            "structure_algorithm_version": structure.get("algorithm_version"),
            "structure_actual_main_contract": _raw_contract_symbol(
                structure.get("actual_main")
            ),
            "structure_analysis_contract": _raw_contract_symbol(
                structure.get("analysis_contract")
            ),
            "structure_tick_size": structure.get("tick_size"),
            "structure_tick_size_contract": _raw_contract_symbol(
                structure.get("tick_size_contract")
            ),
            "structure_tick_size_source": structure.get("tick_size_source"),
            "structure_tick_size_exact": structure.get("tick_size_exact"),
            "structure_tick_size_precision": structure.get("tick_size_precision"),
            "structure_tick_size_actionability": structure.get(
                "tick_size_actionability"
            ),
            "structure_tick_size_evidence": structure.get("tick_size_evidence"),
            "structure_input_snapshot_sha256": structure.get(
                "input_snapshot_sha256"
            ),
            "as_of": structure.get("as_of"),
        }
        for row_field, expected in expected_structure_values.items():
            actual = item.get(row_field)
            if row_field in {
                "structure_actual_main_contract",
                "structure_analysis_contract",
                "structure_tick_size_contract",
            } and actual is not None:
                actual = str(actual).upper()
            if actual != expected:
                raise FuturesMainReportError(
                    f"{label}.{row_field}与structure artifact不一致"
                )
        if structure.get("input_snapshot_sha256") != raw_sha:
            raise FuturesMainReportError(f"{label}结构artifact未绑定raw artifact字节")
        structure_tick = _decimal(
            structure.get("tick_size"), f"{label}.structure.tick_size"
        )
        if structure_tick != configured_tick and not any(
            str(gap).startswith("structure_tick_size_config_conflict")
            for gap in item.get("evidence_gaps", [])
        ):
            raise FuturesMainReportError(
                f"{label}.structure tick与当前品种规格冲突却未披露gap"
            )
        structure_daily = structure.get("daily_data_evidence")
        structure_daily_status = (
            structure_daily.get("status")
            if isinstance(structure_daily, Mapping)
            else None
        )
        reported_daily_status = item.get("daily_data_evidence_status")
        if structure_daily_status != reported_daily_status and not (
            isinstance(reported_daily_status, str)
            and reported_daily_status.endswith(f":{structure_daily_status}")
        ):
            raise FuturesMainReportError(f"{label}.日线证据状态与structure artifact不一致")

    # Rebuild the complete structure from the exact raw bytes with the one
    # supported algorithm before replaying scan status.  Passing the persisted
    # structure back as ``analyzer=lambda: structure`` would only prove that a
    # forged structure agrees with itself: an attacker could move a tick-aligned
    # level, rewrite the structure/row/manifest hashes, and retain a superficially
    # valid scan.  Canonical content and its exact scanner-file hash must both be
    # consequences of the bound raw snapshot.
    if raw_succeeded and structure_succeeded:
        try:
            try:
                from scripts.market_structure import _compact_fetch_structure
            except ImportError:  # pragma: no cover - direct script execution
                from market_structure import _compact_fetch_structure
            regenerated_structure = _compact_fetch_structure(
                dict(raw), input_snapshot_sha256=raw_sha
            )
        except (TypeError, ValueError) as exc:
            raise FuturesMainReportError(
                f"{label}无法用受支持算法从raw重算structure artifact"
            ) from exc
        if dict(regenerated_structure) != dict(structure):
            raise FuturesMainReportError(
                f"{label}.structure artifact内容不是绑定raw的确定性算法产物"
            )
        if _canonical_json_file_sha256(regenerated_structure) != structure_sha:
            raise FuturesMainReportError(
                f"{label}.structure_snapshot_sha256不是确定性结构文件哈希"
            )

        # Reuse the scanner's own derivation instead of maintaining a second
        # set of status/gap rules in the report validator.  This is intentionally
        # done for success and partial evidence alike: `partial` may relax
        # availability, never factual identity or algorithm provenance.
        try:
            try:
                from scripts.scan_market import _scan_one
            except ImportError:  # pragma: no cover - direct script execution
                from scan_market import _scan_one
            with tempfile.TemporaryDirectory() as temporary:
                replay_root = Path(temporary)
                replayed = _scan_one(
                    symbol,
                    {"exchange": exchange},
                    fetcher=lambda _symbol: dict(raw),
                    analyzer=lambda replay_raw: _compact_fetch_structure(
                        dict(replay_raw), input_snapshot_sha256=raw_sha
                    ),
                    attempted_at=attempted_at,
                    configured_tick=configured_tick,
                    evidence_dir=replay_root / "evidence",
                    manifest_dir=replay_root,
                )
        except Exception as exc:
            raise FuturesMainReportError(f"{label}无法用scan_market原逻辑重放") from exc
        ignored_replay_fields = {"raw_snapshot_path", "structure_snapshot_path"}
        for field, expected in replayed.items():
            if field in ignored_replay_fields:
                continue
            if item.get(field) != expected:
                raise FuturesMainReportError(
                    f"{label}.{field}必须由raw/structure artifact按scan_market逻辑重算"
                )


def _validate_scan_manifest_v3(
    report: Mapping[str, Any],
    observed_at: datetime,
    product: str,
    analysis_contract: str,
    actual_main_contract: str | None,
    *,
    policy_sha256: str,
    artifact_root: Path | None,
    scan_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    scope = report.get("scan_scope")
    if scope not in {"all_market", "single_product"}:
        raise FuturesMainReportError("scan_scope 必须是 all_market 或 single_product")
    manifest = report.get("scan_manifest")
    manifest_hash = report.get("scan_manifest_sha256")
    if scope == "single_product" and manifest is None:
        if manifest_hash is not None:
            raise FuturesMainReportError("缺少scan_manifest时不得单独填写其哈希")
        return None
    if not isinstance(manifest, Mapping):
        raise FuturesMainReportError("全市场报告必须内嵌scan_market生成的scan_manifest.v2")
    reported_manifest_hash = _sha256_text(
        manifest_hash, "scan_manifest_sha256"
    )
    if reported_manifest_hash != _canonical_json_sha256(manifest):
        raise FuturesMainReportError("scan_manifest_sha256 与内嵌manifest不一致")
    if manifest.get("schema_version") != SCAN_MANIFEST_SCHEMA_VERSION:
        raise FuturesMainReportError("scan_manifest版本不受支持")
    generated_at = _timestamp(manifest.get("generated_at"), "scan_manifest.generated_at")
    if generated_at > observed_at:
        raise FuturesMainReportError("scan_manifest不得晚于报告观察时间")
    scan_age = Decimal(str((observed_at - generated_at).total_seconds()))
    active_scan_policy = scan_policy or {
        "decision_use_max_age_seconds": 7200,
        "acquisition_max_batch_span_seconds": 900,
    }
    decision_use_max_age_seconds = _decimal(
        active_scan_policy.get("decision_use_max_age_seconds"),
        "scan_manifest.decision_use_max_age_seconds",
    )
    max_batch_span_seconds = _decimal(
        active_scan_policy.get("acquisition_max_batch_span_seconds"),
        "scan_manifest.acquisition_max_batch_span_seconds",
    )
    if decision_use_max_age_seconds <= 0 or max_batch_span_seconds <= 0:
        raise FuturesMainReportError("scan_manifest时效与批次窗口必须为正")
    if scan_age > decision_use_max_age_seconds:
        raise FuturesMainReportError("scan_manifest已超过决策使用时效，需重扫或改做单品种分析")
    if _sha256_text(
        manifest.get("universe_sha256"), "scan_manifest.universe_sha256"
    ) != _file_sha256(UNIVERSE_PATH):
        raise FuturesMainReportError("scan_manifest未绑定当前配置品种清单")
    if _sha256_text(
        manifest.get("product_specs_sha256"),
        "scan_manifest.product_specs_sha256",
    ) != _file_sha256(PRODUCT_SPECS_PATH):
        raise FuturesMainReportError("scan_manifest未绑定当前品种合约规格")
    if _sha256_text(
        manifest.get("policy_sha256"), "scan_manifest.policy_sha256"
    ) != policy_sha256:
        raise FuturesMainReportError("scan_manifest未绑定当前报告策略")

    universe_rows = _allowed_product_rows()
    expected_symbols = [str(row["symbol"]).upper() for row in universe_rows]
    exchange_by_symbol = {
        str(row["symbol"]).upper(): str(row["exchange"]).upper()
        for row in universe_rows
    }
    if manifest.get("expected_symbols") != expected_symbols:
        raise FuturesMainReportError("scan_manifest.expected_symbols必须精确披露配置品种清单")
    if manifest.get("attempted_symbols") != expected_symbols:
        raise FuturesMainReportError("全市场扫描必须逐一尝试全部配置品种")
    configured_count = len(expected_symbols)
    if _exact_integer(manifest.get("expected_count"), "scan expected_count") != configured_count:
        raise FuturesMainReportError("全市场expected_count必须等于配置品种数")
    if _exact_integer(manifest.get("attempted_count"), "scan attempted_count") != configured_count:
        raise FuturesMainReportError("全市场attempted_count必须等于配置品种数")

    products = manifest.get("products")
    if not isinstance(products, list) or len(products) != configured_count:
        raise FuturesMainReportError("scan_manifest.products必须包含全部配置品种明细")
    product_symbols: list[str] = []
    successful_symbols: list[str] = []
    partial_symbols: list[str] = []
    unobservable_symbols: list[str] = []
    data_counts = {status: 0 for status in ("available", "partial", "unobservable")}
    structure_counts = {status: 0 for status in ("available", "partial", "unobservable")}
    with_gaps = 0
    comparable_as_of_dates: set[date] = set()
    focus_row: Mapping[str, Any] | None = None
    focus_index: int | None = None
    artifact_hashes_verified = 0
    for index, item in enumerate(products):
        label = f"scan_manifest.products[{index}]"
        if not isinstance(item, Mapping):
            raise FuturesMainReportError(f"{label} 格式错误")
        symbol = _non_empty_text(item.get("symbol"), f"{label}.symbol").upper()
        product_symbols.append(symbol)
        if symbol not in exchange_by_symbol:
            raise FuturesMainReportError(f"{label}.symbol不在允许范围")
        if str(item.get("exchange", "")).upper() != exchange_by_symbol[symbol]:
            raise FuturesMainReportError(f"{label}.exchange与正向清单不一致")
        scan_status = item.get("scan_status")
        data_status = item.get("data_status")
        structure_status = item.get("structure_status")
        actual_main_chain_status = item.get("actual_main_chain_status")
        actual_main_liquidity_status = item.get("actual_main_liquidity_status")
        if scan_status not in {"success", "partial", "unobservable"}:
            raise FuturesMainReportError(f"{label}.scan_status不受支持")
        if data_status not in data_counts or structure_status not in structure_counts:
            raise FuturesMainReportError(f"{label}的数据或结构状态不受支持")
        if actual_main_chain_status not in {"complete", "partial", "unobservable"}:
            raise FuturesMainReportError(f"{label}.actual_main_chain_status不受支持")
        if actual_main_liquidity_status not in {
            "observable_trade",
            "no_trade",
            "execution_quote_incomplete",
            "unobservable",
        }:
            raise FuturesMainReportError(f"{label}.actual_main_liquidity_status不受支持")
        data_counts[str(data_status)] += 1
        structure_counts[str(structure_status)] += 1
        gaps = item.get("evidence_gaps")
        if not isinstance(gaps, list) or any(not str(gap).strip() for gap in gaps):
            raise FuturesMainReportError(f"{label}.evidence_gaps必须是文本列表")
        if gaps:
            with_gaps += 1
        hint = item.get("machine_hint")
        if not isinstance(hint, Mapping) or not set(hint).issubset(
            {"review_state", "technical_direction", "attention_reasons"}
        ):
            raise FuturesMainReportError(f"{label}.machine_hint只能提供审阅提示")
        attempted_at = _timestamp(item.get("attempted_at"), f"{label}.attempted_at")
        if attempted_at > observed_at or attempted_at != generated_at:
            raise FuturesMainReportError(
                f"{label}.attempted_at必须等于manifest.generated_at且不晚于报告"
            )
        row_fetch_time = (
            None
            if item.get("fetch_time") is None
            else _timestamp(item.get("fetch_time"), f"{label}.fetch_time")
        )
        if row_fetch_time is not None and row_fetch_time > observed_at:
            raise FuturesMainReportError(f"{label}.fetch_time不得晚于报告观察时间")

        required_success_values = [
            item.get("actual_main_contract"),
            item.get("actual_main_liquidity_status"),
            item.get("analysis_contract"),
            item.get("fetch_time"),
            item.get("as_of"),
            item.get("raw_snapshot_path"),
            item.get("raw_snapshot_sha256"),
            item.get("structure_snapshot_path"),
            item.get("structure_snapshot_sha256"),
            item.get("structure_input_snapshot_sha256"),
            item.get("structure_schema_version"),
            item.get("structure_algorithm_version"),
            item.get("structure_actual_main_contract"),
            item.get("structure_analysis_contract"),
            item.get("structure_tick_size"),
            item.get("structure_tick_size_exact"),
            item.get("structure_tick_size_precision"),
            item.get("structure_tick_size_actionability"),
            item.get("structure_tick_size_contract"),
            item.get("structure_tick_size_source"),
            item.get("structure_tick_size_evidence"),
            item.get("daily_data_evidence_status"),
        ]
        mechanically_successful = (
            data_status == "available"
            and structure_status == "available"
            and actual_main_chain_status == "complete"
            and actual_main_liquidity_status == "observable_trade"
            and item.get("daily_data_evidence_status") == "verified"
            and not gaps
            and all(value is not None and str(value).strip() for value in required_success_values)
        )
        expected_scan_status = (
            "unobservable"
            if data_status == "unobservable" or structure_status == "unobservable"
            else "success"
            if mechanically_successful
            else "partial"
        )
        if scan_status != expected_scan_status:
            raise FuturesMainReportError(f"{label}.scan_status必须由证据字段重算")
        for contract_field in (
            "actual_main_contract",
            "declared_actual_main_contract",
            "analysis_contract",
            "structure_actual_main_contract",
            "structure_analysis_contract",
            "structure_tick_size_contract",
        ):
            if item.get(contract_field) is not None:
                if row_fetch_time is None:
                    raise FuturesMainReportError(
                        f"{label}.{contract_field}缺少fetch_time，无法验证合约生命周期"
                    )
                _validate_contract_for_product(
                    item.get(contract_field),
                    symbol,
                    exchange_by_symbol[symbol],
                    f"{label}.{contract_field}",
                    reference_date=row_fetch_time.date(),
                )
        configured_tick = _configured_tick_size(symbol)
        if scan_status == "success":
            successful_symbols.append(symbol)
            if gaps:
                raise FuturesMainReportError(f"{label}成功状态不得同时声称证据缺口")
            assert row_fetch_time is not None
            fetch_time = row_fetch_time
            fetch_batch_offset = Decimal(str((fetch_time - generated_at).total_seconds()))
            if not Decimal("0") <= fetch_batch_offset <= max_batch_span_seconds:
                raise FuturesMainReportError(f"{label}.fetch_time必须位于同一扫描批次")
            as_of = _date_value(item.get("as_of"), f"{label}.as_of")
            if as_of > observed_at.date():
                raise FuturesMainReportError(f"{label}.as_of不得晚于观察日期")
            comparable_as_of_dates.add(as_of)
            actual_contract = str(item.get("actual_main_contract")).upper()
            row_analysis_contract = str(item.get("analysis_contract")).upper()
            declared_contract = _non_empty_text(
                item.get("declared_actual_main_contract"),
                f"{label}.declared_actual_main_contract",
            ).upper()
            if declared_contract != actual_contract:
                raise FuturesMainReportError(f"{label}声明主力与完整raw链重算主力不一致")
            if actual_main_liquidity_status != "observable_trade":
                raise FuturesMainReportError(f"{label}成功扫描必须有可观测双边主力盘口")
            if str(item.get("structure_actual_main_contract")).upper() != actual_contract:
                raise FuturesMainReportError(f"{label}结构主力必须与扫描raw主力一致")
            if str(item.get("structure_analysis_contract")).upper() != row_analysis_contract:
                raise FuturesMainReportError(f"{label}结构分析合约必须与扫描raw一致")
            if _decimal(
                item.get("structure_tick_size"), f"{label}.structure_tick_size"
            ) != configured_tick:
                raise FuturesMainReportError(f"{label}结构tick必须与品种配置一致")
            if item.get("structure_tick_size_exact") is not True:
                raise FuturesMainReportError(f"{label}成功结构必须使用精确tick")
            if item.get("structure_tick_size_actionability") != "exact_order_compatible":
                raise FuturesMainReportError(f"{label}成功结构tick行动语义不一致")
            if str(item.get("structure_tick_size_contract")).upper() != row_analysis_contract:
                raise FuturesMainReportError(f"{label}结构tick合约未绑定analysis_contract")
            _non_empty_text(
                item.get("structure_tick_size_source"),
                f"{label}.structure_tick_size_source",
            )
            _validate_tick_size_evidence_v3(
                item.get("structure_tick_size_evidence"),
                contract=row_analysis_contract,
                tick_size=configured_tick,
                exact=True,
                observed_at=fetch_time,
                label=f"{label}.structure_tick_size_evidence",
            )
            raw_sha256 = _sha256_text(
                item.get("raw_snapshot_sha256"), f"{label}.raw_snapshot_sha256"
            )
            structure_sha256 = _sha256_text(
                item.get("structure_snapshot_sha256"),
                f"{label}.structure_snapshot_sha256",
            )
            if _sha256_text(
                item.get("structure_input_snapshot_sha256"),
                f"{label}.structure_input_snapshot_sha256",
            ) != raw_sha256:
                raise FuturesMainReportError(
                    f"{label}的结构输入哈希必须等于raw_snapshot_sha256"
                )
            if item.get("structure_schema_version") != "cn_futures_market_structure.v1":
                raise FuturesMainReportError(f"{label}.structure_schema_version不受支持")
            if item.get("structure_algorithm_version") != (
                "causal_pivots_zones_theil_sen_tick_aligned.v1"
            ):
                raise FuturesMainReportError(f"{label}.structure_algorithm_version不受支持")
            artifact_hashes_verified += int(
                _verify_relative_artifact(
                    item.get("raw_snapshot_path"),
                    raw_sha256,
                    artifact_root,
                    f"{label}.raw_snapshot_path",
                )
            )
            artifact_hashes_verified += int(
                _verify_relative_artifact(
                    item.get("structure_snapshot_path"),
                    structure_sha256,
                    artifact_root,
                    f"{label}.structure_snapshot_path",
                )
            )
        else:
            if scan_status == "partial":
                partial_symbols.append(symbol)
            else:
                unobservable_symbols.append(symbol)
            if not gaps:
                raise FuturesMainReportError(f"{label}非完整状态必须披露证据缺口")
            for path_field, hash_field in (
                ("raw_snapshot_path", "raw_snapshot_sha256"),
                ("structure_snapshot_path", "structure_snapshot_sha256"),
            ):
                path_value = item.get(path_field)
                hash_value = item.get(hash_field)
                if (path_value is None) != (hash_value is None):
                    raise FuturesMainReportError(
                        f"{label}.{path_field}与{hash_field}必须成对出现"
                    )
                if path_value is not None:
                    evidence_sha256 = _sha256_text(
                        hash_value, f"{label}.{hash_field}"
                    )
                    artifact_hashes_verified += int(
                        _verify_relative_artifact(
                            path_value,
                            evidence_sha256,
                            artifact_root,
                            f"{label}.{path_field}",
                        )
                    )
            if item.get("fetch_time") is not None:
                assert row_fetch_time is not None
                fetch_time = row_fetch_time
                if abs((fetch_time - generated_at).total_seconds()) > float(
                    max_batch_span_seconds
                ):
                    raise FuturesMainReportError(f"{label}.fetch_time越出同一扫描批次")
            if scan_status == "partial" and item.get("as_of") is not None:
                partial_as_of = _date_value(item.get("as_of"), f"{label}.as_of")
                if partial_as_of > observed_at.date():
                    raise FuturesMainReportError(f"{label}.as_of不得晚于观察日期")
                comparable_as_of_dates.add(partial_as_of)
        for path_field, hash_field in (
            ("raw_snapshot_path", "raw_snapshot_sha256"),
            ("structure_snapshot_path", "structure_snapshot_sha256"),
        ):
            if item.get(path_field) is None or item.get(hash_field) is None:
                raise FuturesMainReportError(
                    f"{label}.{path_field}/{hash_field}必须保留成功或失败证据"
                )
        _validate_scan_row_artifact_content_v3(
            item,
            label=label,
            symbol=symbol,
            exchange=exchange_by_symbol[symbol],
            configured_tick=configured_tick,
            artifact_root=artifact_root,
            attempted_at=str(item.get("attempted_at")),
        )
        if symbol == product:
            focus_row = item
            focus_index = index

    if product_symbols != expected_symbols or len(product_symbols) != len(set(product_symbols)):
        raise FuturesMainReportError("scan_manifest.products必须按清单顺序唯一覆盖全部配置品种")
    partitions = (successful_symbols, partial_symbols, unobservable_symbols)
    if any(
        set(left) & set(right)
        for index, left in enumerate(partitions)
        for right in partitions[index + 1 :]
    ) or set(successful_symbols + partial_symbols + unobservable_symbols) != set(
        expected_symbols
    ):
        raise FuturesMainReportError("成功、部分与不可观测品种必须构成互斥完备三分区")
    for field, derived in (
        ("successful_symbols", successful_symbols),
        ("partial_symbols", partial_symbols),
        ("unobservable_symbols", unobservable_symbols),
    ):
        if manifest.get(field) != derived:
            raise FuturesMainReportError(f"scan_manifest.{field}必须由products重算")
    if _exact_integer(manifest.get("successful_count"), "successful_count") != len(
        successful_symbols
    ):
        raise FuturesMainReportError("successful_count必须由products重算")
    if _exact_integer(manifest.get("unobservable_count"), "unobservable_count") != len(
        unobservable_symbols
    ):
        raise FuturesMainReportError("unobservable_count必须由products重算")
    if _exact_integer(manifest.get("partial_count"), "partial_count") != len(
        partial_symbols
    ):
        raise FuturesMainReportError("partial_count必须由products重算")
    expected_counts = {
        "data_status": data_counts,
        "structure_status": structure_counts,
        "with_evidence_gaps": with_gaps,
    }
    if manifest.get("counts") != expected_counts:
        raise FuturesMainReportError("scan_manifest.counts必须从products逐项重算")
    if len(comparable_as_of_dates) > 1:
        raise FuturesMainReportError("scan_status=success/partial的可比品种必须共享同一最近完成交易日")
    _non_empty_text(manifest.get("machine_notice"), "scan_manifest.machine_notice")
    assert focus_row is not None and focus_index is not None
    return {
        "successful_count": len(successful_symbols),
        "partial_count": len(partial_symbols),
        "unobservable_count": len(unobservable_symbols),
        "focus_scan_status": focus_row.get("scan_status"),
        "artifact_hashes_verified": artifact_hashes_verified,
        "scan_age_seconds": scan_age,
        "acquisition_max_batch_span_seconds": max_batch_span_seconds,
        "focus": dict(focus_row),
        "focus_index": focus_index,
        "comparable_as_of": (
            None if not comparable_as_of_dates else next(iter(comparable_as_of_dates))
        ),
    }


def _validate_focus_refresh_link_v3(
    report: Mapping[str, Any],
    *,
    product: str,
    analysis_contract: str,
    scan_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Link the decision-time focus refresh to the earlier candidate scan.

    The scan remains immutable candidate evidence.  The final target is allowed
    to use a later raw snapshot (and even a newly selected delivery month), but
    the relationship to the scan row must be explicit and hash-bound.
    """

    link = report.get("focus_refresh_from_scan")
    if scan_summary is None:
        if link is not None:
            raise FuturesMainReportError("single_product不得伪造focus_refresh_from_scan")
        return None
    if not isinstance(link, Mapping):
        raise FuturesMainReportError("all_market必须用focus_refresh_from_scan关联候选扫描与决策前刷新")
    focus = scan_summary.get("focus")
    if not isinstance(focus, Mapping):
        raise FuturesMainReportError("扫描焦点证据缺失")
    if _non_empty_text(link.get("relationship"), "focus_refresh_from_scan.relationship") != (
        "same_product_decision_refresh"
    ):
        raise FuturesMainReportError("focus refresh关系语义不受支持")
    if _non_empty_text(link.get("product"), "focus_refresh_from_scan.product").upper() != product:
        raise FuturesMainReportError("focus_refresh_from_scan.product必须等于报告焦点品种")
    if _sha256_text(
        link.get("scan_manifest_sha256"),
        "focus_refresh_from_scan.scan_manifest_sha256",
    ) != _sha256_text(report.get("scan_manifest_sha256"), "scan_manifest_sha256"):
        raise FuturesMainReportError("focus refresh未绑定本报告的scan manifest")
    if _exact_integer(
        link.get("scan_row_index"), "focus_refresh_from_scan.scan_row_index", minimum=0
    ) != scan_summary.get("focus_index"):
        raise FuturesMainReportError("focus refresh未绑定正确scan row")

    scan_contract = (
        None
        if focus.get("analysis_contract") is None
        else str(focus.get("analysis_contract")).upper()
    )
    reported_scan_contract = link.get("scan_analysis_contract")
    if reported_scan_contract is not None:
        reported_scan_contract = _non_empty_text(
            reported_scan_contract, "focus_refresh_from_scan.scan_analysis_contract"
        ).upper()
    if reported_scan_contract != scan_contract:
        raise FuturesMainReportError("focus refresh的原扫描合约与scan row不一致")
    if _non_empty_text(
        link.get("refreshed_analysis_contract"),
        "focus_refresh_from_scan.refreshed_analysis_contract",
    ).upper() != analysis_contract:
        raise FuturesMainReportError("focus refresh的新分析合约与target不一致")
    for link_field, row_field in (
        ("scan_raw_snapshot_sha256", "raw_snapshot_sha256"),
        ("scan_structure_snapshot_sha256", "structure_snapshot_sha256"),
    ):
        linked_value = link.get(link_field)
        expected_value = focus.get(row_field)
        if linked_value is not None:
            linked_value = _sha256_text(
                linked_value, f"focus_refresh_from_scan.{link_field}"
            )
        if linked_value != expected_value:
            raise FuturesMainReportError(f"focus refresh的{link_field}未绑定scan row")
    if link.get("scan_actual_main_liquidity_status") != focus.get(
        "actual_main_liquidity_status"
    ):
        raise FuturesMainReportError("focus refresh未绑定scan row的实际主力流动性状态")
    expected_transition = (
        "newly_observable"
        if scan_contract is None
        else "unchanged"
        if scan_contract == analysis_contract
        else "rolled_or_reselected"
    )
    if link.get("contract_transition") != expected_transition:
        raise FuturesMainReportError("focus refresh合约迁移必须由扫描与刷新合约对比推导")
    _non_empty_text(
        link.get("transition_reason"), "focus_refresh_from_scan.transition_reason"
    )
    return {
        "scan_analysis_contract": scan_contract,
        "refreshed_analysis_contract": analysis_contract,
        "contract_transition": expected_transition,
    }


def _validate_candidate_comparison_v3(
    report: Mapping[str, Any],
    *,
    product: str,
    analysis_contract: str,
    scan_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    comparison = report.get("candidate_comparison")
    if scan_summary is None:
        if comparison is not None:
            raise FuturesMainReportError("single_product不得伪装成全市场候选比较")
        return None
    if not isinstance(comparison, Mapping):
        raise FuturesMainReportError("all_market必须给出跨品种candidate_comparison")
    expected_comparison_keys = {
        "method",
        "focus",
        "rejected_or_watch",
        "agent_selection_reason",
    }
    if set(comparison) != expected_comparison_keys:
        raise FuturesMainReportError("candidate_comparison必须使用决策合同字段白名单")
    if comparison.get("method") != "agent_comparison_without_score_or_rank_authority":
        raise FuturesMainReportError("候选比较必须由Agent完成，不得用score/rank自动决策")

    def reject_numeric_authority(value: Any, label: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if str(key).lower() in {"score", "rank", "numeric_score"}:
                    raise FuturesMainReportError(f"{label}不得包含score/rank决策权威")
                reject_numeric_authority(child, f"{label}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                reject_numeric_authority(child, f"{label}[{index}]")

    reject_numeric_authority(comparison, "candidate_comparison")
    manifest = report.get("scan_manifest")
    assert isinstance(manifest, Mapping)
    rows = manifest.get("products")
    assert isinstance(rows, list)
    row_by_symbol = {
        str(row.get("symbol")).upper(): (index, row)
        for index, row in enumerate(rows)
        if isinstance(row, Mapping)
    }

    def validate_entry(
        value: Any,
        label: str,
        *,
        expected_symbol: str | None = None,
        require_disposition: bool,
    ) -> str:
        if not isinstance(value, Mapping):
            raise FuturesMainReportError(f"{label}格式错误")
        expected_entry_keys = {
            "symbol",
            "contract",
            "scan_status",
            "actual_main_liquidity_status",
            "raw_snapshot_sha256",
            "structure_snapshot_sha256",
            "evidence_refs",
            "causal_summary",
            "cost_adjusted_opportunity_cost_reason",
            "coverage_gap_impact",
        }
        if require_disposition:
            expected_entry_keys.add("disposition")
        if set(value) != expected_entry_keys:
            raise FuturesMainReportError(f"{label}必须使用候选证据字段白名单")
        symbol = _non_empty_text(value.get("symbol"), f"{label}.symbol").upper()
        if expected_symbol is not None and symbol != expected_symbol:
            raise FuturesMainReportError(f"{label}.symbol与焦点品种不一致")
        if symbol not in row_by_symbol:
            raise FuturesMainReportError(f"{label}.symbol不在scan_manifest")
        row_index, row = row_by_symbol[symbol]
        assert isinstance(row, Mapping)
        if value.get("scan_status") != row.get("scan_status"):
            raise FuturesMainReportError(f"{label}.scan_status必须如实引用scan row")
        if value.get("actual_main_liquidity_status") != row.get(
            "actual_main_liquidity_status"
        ):
            raise FuturesMainReportError(f"{label}.actual_main_liquidity_status必须如实引用scan row")
        row_contract = row.get("analysis_contract")
        contract_value = value.get("contract")
        contract = None if contract_value is None else _non_empty_text(
            contract_value, f"{label}.contract"
        ).upper()
        if contract != (
            None if row_contract is None else str(row_contract).upper()
        ):
            raise FuturesMainReportError(f"{label}.contract与扫描分析合约不一致")
        raw_sha_value = value.get("raw_snapshot_sha256")
        structure_sha_value = value.get("structure_snapshot_sha256")
        raw_sha = None if raw_sha_value is None else _sha256_text(
            raw_sha_value, f"{label}.raw_snapshot_sha256"
        )
        structure_sha = None if structure_sha_value is None else _sha256_text(
            structure_sha_value, f"{label}.structure_snapshot_sha256"
        )
        if (
            raw_sha != row.get("raw_snapshot_sha256")
            or structure_sha != row.get("structure_snapshot_sha256")
        ):
            raise FuturesMainReportError(f"{label}候选哈希未绑定对应scan row")
        expected_refs = {
            f"scan_manifest.products[{row_index}].scan_status",
            f"scan_manifest.products[{row_index}].actual_main_liquidity_status",
        }
        if row.get("evidence_gaps"):
            expected_refs.add(f"scan_manifest.products[{row_index}].evidence_gaps")
        if raw_sha is not None:
            expected_refs.add(
                f"scan_manifest.products[{row_index}].raw_snapshot_sha256"
            )
        if structure_sha is not None:
            expected_refs.add(
                f"scan_manifest.products[{row_index}].structure_snapshot_sha256"
            )
        refs = value.get("evidence_refs")
        if (
            not isinstance(refs, list)
            or set(refs) != expected_refs
            or len(refs) != len(expected_refs)
        ):
            raise FuturesMainReportError(f"{label}.evidence_refs必须精确引用scan row状态、缺口与现有哈希")
        for index, ref in enumerate(refs):
            _resolve_evidence_ref(report, ref, f"{label}.evidence_refs[{index}]")
        _non_empty_text(value.get("causal_summary"), f"{label}.causal_summary")
        _non_empty_text(
            value.get("cost_adjusted_opportunity_cost_reason"),
            f"{label}.cost_adjusted_opportunity_cost_reason",
        )
        _non_empty_text(
            value.get("coverage_gap_impact"), f"{label}.coverage_gap_impact"
        )
        if require_disposition and value.get("disposition") not in {"rejected", "watch"}:
            raise FuturesMainReportError(f"{label}.disposition必须是rejected或watch")
        return symbol

    focus_symbol = validate_entry(
        comparison.get("focus"),
        "candidate_comparison.focus",
        expected_symbol=product,
        require_disposition=False,
    )
    alternatives = comparison.get("rejected_or_watch")
    if not isinstance(alternatives, list) or not alternatives:
        raise FuturesMainReportError("全市场报告必须比较至少一个落选或观察候选，并说明缺口影响")
    alternative_symbols = [
        validate_entry(
            item,
            f"candidate_comparison.rejected_or_watch[{index}]",
            require_disposition=True,
        )
        for index, item in enumerate(alternatives)
    ]
    if focus_symbol in alternative_symbols or len(alternative_symbols) != len(
        set(alternative_symbols)
    ):
        raise FuturesMainReportError("跨品种候选必须与焦点不同且互不重复")
    _non_empty_text(
        comparison.get("agent_selection_reason"),
        "candidate_comparison.agent_selection_reason",
    )
    return {
        "alternative_count": len(alternative_symbols),
        "strongest_alternative_symbol": alternative_symbols[0],
    }


def _validate_actual_main_v3(
    report: Mapping[str, Any],
    observed_at: datetime,
    product: str,
    exchange: str,
    target_actual_main: str | None,
) -> dict[str, Any]:
    value = report.get("actual_main")
    if not isinstance(value, Mapping):
        raise FuturesMainReportError("v3报告缺少actual_main")
    status = value.get("status")
    if status not in {"complete", "partial", "unobservable"}:
        raise FuturesMainReportError("actual_main.status不受支持")
    contracts = value.get("contracts")
    if not isinstance(contracts, list):
        raise FuturesMainReportError("actual_main.contracts必须是列表")
    available_count = _exact_integer(
        value.get("available_contract_count"), "actual_main.available_contract_count", minimum=0
    )
    if available_count != len(contracts):
        raise FuturesMainReportError("available_contract_count必须等于contracts明细数")
    selected_value = value.get("selected_contract")
    if status == "unobservable":
        if available_count != 0 or contracts or selected_value is not None:
            raise FuturesMainReportError("主力不可观测时不得伪造合约链或选中合约")
        if target_actual_main is not None:
            raise FuturesMainReportError("主力不可观测时target.actual_main_contract必须为空")
        if value.get("chain_complete_for_product") is not False:
            raise FuturesMainReportError("主力不可观测时合约链不得标为完整")
        if value.get("snapshot_status") != "historical_or_unbound":
            raise FuturesMainReportError("主力不可观测时快照必须标记为historical_or_unbound")
        if value.get("liquidity_status") != "unobservable":
            raise FuturesMainReportError("主力不可观测时流动性也必须标记为unobservable")
        _non_empty_text(value.get("unobservable_reason"), "actual_main.unobservable_reason")
        _validate_source_attempts_v3(
            value.get("source_attempts"), observed_at, "actual_main.source_attempts"
        )
        return {
            "status": status,
            "selected_contract": None,
            "available_count": 0,
            "snapshot_status": "historical_or_unbound",
            "snapshot_sha256": None,
            "chain_observed_at": None,
            "liquidity_status": "unobservable",
        }

    chain_observed_at = _timestamp(
        value.get("chain_observed_at"), "actual_main.chain_observed_at"
    )
    if chain_observed_at > observed_at:
        raise FuturesMainReportError("合约链时间不得晚于报告观察时间")
    selected_contract = _validate_contract_for_product(
        selected_value,
        product,
        exchange,
        "actual_main.selected_contract",
        reference_date=chain_observed_at.date(),
    )
    if target_actual_main != selected_contract:
        raise FuturesMainReportError("actual_main.selected_contract必须等于target.actual_main_contract")
    if value.get("selection_basis") != "highest_open_interest_then_volume_then_contract_code":
        raise FuturesMainReportError("实际主力排序口径不受支持")
    _validate_evidence_source(value.get("source"), observed_at, "actual_main.source")
    snapshot_sha256 = _sha256_text(
        value.get("snapshot_sha256"), "actual_main.snapshot_sha256"
    )
    snapshot_path = _non_empty_text(
        value.get("snapshot_path"), "actual_main.snapshot_path"
    )
    relative_snapshot_path = Path(snapshot_path)
    if relative_snapshot_path.is_absolute() or ".." in relative_snapshot_path.parts:
        raise FuturesMainReportError("actual_main.snapshot_path必须是报告证据目录内的相对路径")
    snapshot_status = value.get("snapshot_status")
    if snapshot_status not in {"current_session", "historical_or_unbound"}:
        raise FuturesMainReportError("actual_main.snapshot_status不受支持")
    source_record_count = _exact_integer(
        value.get("source_record_count"), "actual_main.source_record_count", minimum=0
    )
    if source_record_count < available_count:
        raise FuturesMainReportError("来源记录数不得少于已列合约数")
    ranked: list[tuple[str, int, int | None]] = []
    timestamps: list[datetime] = []
    validated_contracts: list[dict[str, Any]] = []
    for index, item in enumerate(contracts):
        label = f"actual_main.contracts[{index}]"
        if not isinstance(item, Mapping):
            raise FuturesMainReportError(f"{label}格式错误")
        contract = _validate_contract_for_product(
            item.get("contract"),
            product,
            exchange,
            f"{label}.contract",
            reference_date=chain_observed_at.date(),
        )
        open_interest = _exact_integer(
            item.get("open_interest"), f"{label}.open_interest", minimum=0
        )
        volume_value = item.get("volume")
        volume = (
            None
            if volume_value is None
            else _exact_integer(volume_value, f"{label}.volume", minimum=0)
        )
        item_time = _timestamp(item.get("observed_at"), f"{label}.observed_at")
        if item_time > observed_at:
            raise FuturesMainReportError(f"{label}.observed_at不得晚于报告观察时间")
        ranked.append((contract, open_interest, volume))
        timestamps.append(item_time)
        validated_contracts.append(
            {
                "contract": contract,
                "open_interest": open_interest,
                "volume": volume,
                "observed_at": item_time,
            }
        )
    if not ranked or len({item[0] for item in ranked}) != len(ranked):
        raise FuturesMainReportError("可观测主力链必须包含至少一条唯一月份合约")
    if Decimal(str((max(timestamps) - min(timestamps)).total_seconds())) > Decimal("60"):
        raise FuturesMainReportError("实际主力合约链必须来自同一分钟快照")
    if any(abs((item_time - chain_observed_at).total_seconds()) > 60 for item_time in timestamps):
        raise FuturesMainReportError("合约链明细时间必须与chain_observed_at对齐")
    ranked.sort(
        key=lambda row: (
            -row[1],
            -(row[2] if row[2] is not None else -1),
            row[0],
        )
    )
    top_open_interest = ranked[0][1]
    top_oi_rows = [row for row in ranked if row[1] == top_open_interest]
    leader_volume_uncertain = len(top_oi_rows) > 1 and any(
        row[2] is None for row in top_oi_rows
    )
    if ranked[0][0] != selected_contract or ranked[0][1] <= 0:
        raise FuturesMainReportError("选中合约必须是已列范围内持仓量最大的有效月份合约")
    selected_volume = next(
        row[2] for row in ranked if row[0] == selected_contract
    )
    volume_only_liquidity_status = (
        "unobservable"
        if selected_volume is None
        else "no_trade"
        if selected_volume == 0
        else "observable_trade"
    )
    reported_liquidity_status = value.get("liquidity_status")
    allowed_reported_liquidity = (
        {"observable_trade", "execution_quote_incomplete"}
        if volume_only_liquidity_status == "observable_trade"
        else {volume_only_liquidity_status}
    )
    if reported_liquidity_status not in allowed_reported_liquidity:
        raise FuturesMainReportError(
            "actual_main.liquidity_status必须保留OI首位合约的volume/盘口事实，不得换月份冒充主力"
        )
    complete = (
        value.get("chain_complete_for_product") is True
        and value.get("pagination_complete") is True
        and source_record_count == available_count
    )
    if status == "complete":
        expected_claim = (
            "verified_actual_main"
            if snapshot_status == "current_session"
            else "complete_historical_chain_leader"
        )
        if (
            not complete
            or leader_volume_uncertain
            or value.get("selection_claim") != expected_claim
        ):
            raise FuturesMainReportError(
                "完整主力链声明必须与快照时效一致；历史链不得冒充verified_actual_main"
            )
    else:
        selection_claim = value.get("selection_claim")
        if complete or selection_claim not in {
            "leader_of_available_rows",
            "provisional_leader_of_observed_oi_rows",
        }:
            raise FuturesMainReportError("不完整主力链必须如实声明可得行leader或provisional leader")
        if leader_volume_uncertain and selection_claim != (
            "provisional_leader_of_observed_oi_rows"
        ):
            raise FuturesMainReportError("最高OI并列且volume缺失时主力只能声明provisional")
        _non_empty_text(value.get("incomplete_reason"), "actual_main.incomplete_reason")
        if selection_claim == "provisional_leader_of_observed_oi_rows":
            uncertainties = value.get("selection_uncertainty")
            if not isinstance(uncertainties, list) or not uncertainties or any(
                not isinstance(item, str) or not item.strip() for item in uncertainties
            ):
                raise FuturesMainReportError("provisional leader必须显式列出selection_uncertainty")
    return {
        "status": status,
        "selected_contract": selected_contract,
        "available_count": available_count,
        "chain_observed_at": chain_observed_at,
        "snapshot_status": snapshot_status,
        "snapshot_sha256": snapshot_sha256,
        "snapshot_path": snapshot_path,
        "contracts": validated_contracts,
        "selection_claim": value.get("selection_claim"),
        "liquidity_status": reported_liquidity_status,
    }


def _validate_bar_v3(
    value: Any,
    observed_at: datetime,
    contract: str,
    tick_size: Decimal,
    label: str,
    *,
    official_exchange: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FuturesMainReportError(f"{label}必须是完整K线证据")
    if str(value.get("contract", "")).upper() != contract:
        raise FuturesMainReportError(f"{label}.contract必须等于analysis_contract")
    bar_date = _date_value(value.get("date"), f"{label}.date")
    if bar_date > observed_at.date():
        raise FuturesMainReportError(f"{label}.date不得晚于报告观察日期")
    prices = {
        field: _decimal(value.get(field), f"{label}.{field}")
        for field in ("open", "high", "low", "close")
    }
    if any(price <= 0 or not _tick_aligned(price, tick_size) for price in prices.values()):
        raise FuturesMainReportError(f"{label}的OHLC必须为正并按tick对齐")
    if (
        prices["low"] > min(prices["open"], prices["close"])
        or prices["high"] < max(prices["open"], prices["close"])
        or prices["low"] > prices["high"]
    ):
        raise FuturesMainReportError(f"{label}的OHLC顺序无效")
    volume = _exact_integer(value.get("volume"), f"{label}.volume", minimum=0)
    open_interest = _exact_integer(
        value.get("open_interest"), f"{label}.open_interest", minimum=0
    )
    _validate_evidence_source(
        value.get("source"),
        observed_at,
        f"{label}.source",
        official_exchange=official_exchange,
    )
    return {
        "date": bar_date,
        **prices,
        "volume": volume,
        "open_interest": open_interest,
    }


def _normalize_raw_completed_daily_rows_v3(
    raw: Mapping[str, Any],
    *,
    analysis_contract: str,
    tick_size: Decimal,
    observed_at: datetime,
) -> list[dict[str, Any]]:
    """Normalize the exact completed-bar facts carried by the verified raw bytes.

    Report bars deliberately carry their own source metadata, while fetch rows
    use ``hold`` for open interest.  The normalized identity below contains only
    the facts that both representations must share.  It is also the sole payload
    used for a historical confirmation chain hash.
    """

    technical = raw.get("technical")
    if not isinstance(technical, Mapping):
        raise FuturesMainReportError("raw快照缺少technical证据")
    if _raw_contract_symbol(technical.get("symbol")) != analysis_contract:
        raise FuturesMainReportError(
            "raw.technical.symbol必须精确绑定analysis_contract"
        )
    raw_rows = technical.get("completed_daily_rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise FuturesMainReportError(
            "raw.technical.completed_daily_rows必须保留至少一根完成K线"
        )

    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(raw_rows):
        label = f"raw.technical.completed_daily_rows[{index}]"
        if not isinstance(row, Mapping) or row.get("completed") is not True:
            raise FuturesMainReportError(f"{label}必须是显式completed=true的K线")
        bar_date = _date_value(row.get("date", row.get("at")), f"{label}.date")
        if bar_date > observed_at.date():
            raise FuturesMainReportError(f"{label}.date不得晚于报告观察日期")
        prices = {
            field: _decimal(row.get(field), f"{label}.{field}")
            for field in ("open", "high", "low", "close")
        }
        if any(
            price <= 0 or not _tick_aligned(price, tick_size)
            for price in prices.values()
        ):
            raise FuturesMainReportError(f"{label}的OHLC必须为正并按tick对齐")
        if (
            prices["low"] > min(prices["open"], prices["close"])
            or prices["high"] < max(prices["open"], prices["close"])
            or prices["low"] > prices["high"]
        ):
            raise FuturesMainReportError(f"{label}的OHLC顺序无效")
        volume = _exact_integer(row.get("volume"), f"{label}.volume", minimum=0)
        raw_open_interest = row.get("open_interest", row.get("hold"))
        open_interest = _exact_integer(
            raw_open_interest, f"{label}.open_interest", minimum=0
        )
        if row.get("open_interest") is not None and row.get("hold") is not None:
            if _exact_integer(row.get("hold"), f"{label}.hold", minimum=0) != (
                open_interest
            ):
                raise FuturesMainReportError(
                    f"{label}.open_interest与hold不得互相冲突"
                )
        normalized.append(
            {
                "contract": analysis_contract,
                "date": bar_date,
                **prices,
                "volume": volume,
                "open_interest": open_interest,
            }
        )

    dates = [row["date"] for row in normalized]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise FuturesMainReportError(
            "raw.technical.completed_daily_rows必须按日期严格升序且不重复"
        )
    declared_count = technical.get("completed_daily_row_count")
    if declared_count is not None and _exact_integer(
        declared_count, "raw.technical.completed_daily_row_count", minimum=1
    ) != len(normalized):
        raise FuturesMainReportError(
            "raw.technical.completed_daily_row_count必须由完成K线重算"
        )
    latest = normalized[-1]
    declared_latest_date = technical.get(
        "last_daily_date", technical.get("latest_completed_date")
    )
    if declared_latest_date is not None and _date_value(
        declared_latest_date, "raw.technical.last_daily_date"
    ) != latest["date"]:
        raise FuturesMainReportError(
            "raw.technical.last_daily_date必须等于完成K线最后日期"
        )
    if technical.get("last_daily_close") is not None and _decimal(
        technical.get("last_daily_close"), "raw.technical.last_daily_close"
    ) != latest["close"]:
        raise FuturesMainReportError(
            "raw.technical.last_daily_close必须等于完成K线最后收盘"
        )
    if technical.get("last_daily_open_interest") is not None and _exact_integer(
        technical.get("last_daily_open_interest"),
        "raw.technical.last_daily_open_interest",
        minimum=0,
    ) != latest["open_interest"]:
        raise FuturesMainReportError(
            "raw.technical.last_daily_open_interest必须等于完成K线最后持仓"
        )
    return normalized


def _completed_bar_hash_payload(bar: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical raw-derived fact identity for a completed bar."""

    bar_date = bar.get("date")
    return {
        "contract": bar["contract"],
        "date": bar_date.isoformat() if isinstance(bar_date, date) else bar_date,
        "open": bar["open"],
        "high": bar["high"],
        "low": bar["low"],
        "close": bar["close"],
        "volume": bar["volume"],
        "open_interest": bar["open_interest"],
    }


def _completed_bar_facts_equal(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    return all(
        left.get(field) == right.get(field)
        for field in (
            "contract",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        )
    )


def _validate_raw_daily_evidence_v3(
    technical: Mapping[str, Any],
    *,
    analysis_contract: str,
    label: str,
) -> tuple[str, bool, bool]:
    """Separate raw-byte binding, cache replay, and upstream response identity.

    A degraded slice can still be a useful Agent input once its exact raw bytes
    are bound.  It is not, however, a replayed cache artifact or upstream
    response identity.
    Only the strict verified-daily cache replay may set the second return value
    true.  The third dimension is narrower still: it records whether the
    evidence explicitly replays an identity-bearing upstream response.  A
    cache artifact replay must never be described as an official HTTP archive.
    """

    evidence = technical.get("daily_data_evidence")
    if not isinstance(evidence, Mapping):
        raise FuturesMainReportError(f"{label}.daily_data_evidence缺失")
    status = evidence.get("status")
    if status not in {"verified", "degraded", "unobservable"}:
        raise FuturesMainReportError(
            f"{label}.daily_data_evidence.status不受支持"
        )
    evidence_contract = evidence.get("contract")
    if evidence_contract is not None and str(evidence_contract).upper() != (
        analysis_contract
    ):
        raise FuturesMainReportError(
            f"{label}.daily_data_evidence.contract必须绑定analysis_contract"
        )
    if status != "verified":
        return str(status), False, False
    try:
        replay_verified_daily_evidence(
            dict(technical),
            contract=analysis_contract,
        )
    except (DailyCacheVerificationError, OSError, ValueError) as exc:
        raise FuturesMainReportError(
            f"{label}.daily_data_evidence无法从归档manifest与日线对象重放"
        ) from exc
    verification = evidence.get("verification")
    upstream_response_identity_verified = bool(
        evidence.get("source_contract_identity_status")
        == "verified_sina_cross_endpoint_response_archives"
        and isinstance(verification, Mapping)
        and verification.get("dce_source_contract_identity") is True
    )
    return "verified", True, upstream_response_identity_verified


def _daily_methodology_identity_v3(
    technical: Mapping[str, Any],
    *,
    analysis_contract: str,
    label: str,
) -> dict[str, Any]:
    """Return the source/method identity that must survive S1 -> S2 replay."""

    evidence = technical.get("daily_data_evidence")
    if not isinstance(evidence, Mapping):
        raise FuturesMainReportError(f"{label}.daily_data_evidence缺失")
    contract_value = _non_empty_text(
        evidence.get("contract"), f"{label}.daily_data_evidence.contract"
    ).upper()
    if contract_value != analysis_contract:
        raise FuturesMainReportError(
            f"{label}.daily_data_evidence.contract必须绑定analysis_contract"
        )
    identity = {
        "daily_rows_source": _non_empty_text(
            technical.get("daily_rows_source"), f"{label}.daily_rows_source"
        ),
        "daily_completion_rule": _non_empty_text(
            technical.get("daily_completion_rule"),
            f"{label}.daily_completion_rule",
        ),
        "daily_completion_basis": _non_empty_text(
            technical.get("daily_completion_basis"),
            f"{label}.daily_completion_basis",
        ),
        "status": evidence.get("status"),
        "contract": contract_value,
        "source_kind": _non_empty_text(
            evidence.get("source_kind"),
            f"{label}.daily_data_evidence.source_kind",
        ),
        "quality_tier": _non_empty_text(
            evidence.get("quality_tier"),
            f"{label}.daily_data_evidence.quality_tier",
        ),
        "scope_kind": _non_empty_text(
            evidence.get("scope_kind"),
            f"{label}.daily_data_evidence.scope_kind",
        ),
    }
    quality_equivalence = evidence.get("quality_equivalence")
    if quality_equivalence is not None:
        identity["quality_equivalence"] = _non_empty_text(
            quality_equivalence,
            f"{label}.daily_data_evidence.quality_equivalence",
        )
    return identity


def _validate_frozen_final_daily_lineage_v3(
    frozen_rows: list[Mapping[str, Any]],
    final_rows: list[Mapping[str, Any]],
) -> None:
    """Require S1 and S2 to overlap exactly, with S2 extending beyond S1."""

    if not frozen_rows or not final_rows:
        raise FuturesMainReportError("冻结结构与final raw都必须包含完成K线")
    frozen_as_of = frozen_rows[-1]["date"]
    final_as_of = final_rows[-1]["date"]
    if frozen_as_of >= final_as_of:
        raise FuturesMainReportError(
            "historical_confirmation要求冻结structure raw早于final raw最新完成日"
        )
    frozen_by_date = {row["date"]: row for row in frozen_rows}
    final_by_date = {row["date"]: row for row in final_rows}
    common_dates = sorted(set(frozen_by_date) & set(final_by_date))
    if not common_dates:
        raise FuturesMainReportError(
            "冻结structure raw与final raw至少需要一段可逐行复核的重叠K线"
        )
    frozen_dates = [row["date"] for row in frozen_rows]
    final_dates = [row["date"] for row in final_rows]
    if common_dates != frozen_dates[-len(common_dates) :] or common_dates != (
        final_dates[: len(common_dates)]
    ):
        raise FuturesMainReportError(
            "冻结structure raw与final raw的重叠必须是S1后缀和S2前缀"
        )
    for bar_date in common_dates:
        if not _completed_bar_facts_equal(
            frozen_by_date[bar_date], final_by_date[bar_date]
        ):
            raise FuturesMainReportError(
                "冻结structure raw与final raw重叠完成K线必须逐字段全等"
            )


def _validate_completed_bar_provenance_claim_v3(
    report: Mapping[str, Any],
    *,
    daily_status: str | None,
    daily_cache_artifact_replay_verified: bool,
) -> None:
    market = report.get("market_data")
    if not isinstance(market, Mapping) or market.get("status") == "unobservable":
        return
    latest = market.get("latest_completed_bar")
    if not isinstance(latest, Mapping):
        raise FuturesMainReportError("market_data.latest_completed_bar缺失")
    evidence_kind = latest.get("evidence_kind")
    provenance_status = latest.get("provenance_status")
    if daily_status == "verified":
        if evidence_kind == "degraded_raw_completed_daily_row":
            raise FuturesMainReportError(
                "verified日线不得伪装成degraded完成K线证据"
            )
        if provenance_status != "daily_cache_artifact_replay_required":
            raise FuturesMainReportError(
                "verified完成K线必须声明daily_cache_artifact_replay_required"
            )
        if not daily_cache_artifact_replay_verified:
            # Mapping-only audit can inspect the claim but may never promote it
            # to a verified output.  CLI failures are raised during replay.
            return
    else:
        if (
            evidence_kind != "degraded_raw_completed_daily_row"
            or provenance_status
            != "bound_to_raw_bytes_cache_artifact_not_replayed"
        ):
            raise FuturesMainReportError(
                "degraded完成K线必须明确声明仅绑定raw字节且daily cache artifact未重放"
            )


def _validate_raw_snapshot_binding_v3(
    report: Mapping[str, Any],
    *,
    analysis_contract: str,
    selected_actual_main_contract: str | None,
    tick_size: Decimal,
    main_summary: Mapping[str, Any],
    structure_summary: Mapping[str, Any],
    scan_summary: Mapping[str, Any] | None,
    artifact_root: Path | None,
    product: str | None = None,
    exchange: str | None = None,
    market_summary: Mapping[str, Any] | None = None,
    raw_binding_out: dict[str, Any] | None = None,
) -> bool:
    """Bind the decision-time raw and, when needed, a frozen structure raw."""
    main_observable = main_summary["status"] != "unobservable"
    structure_available = structure_summary["status"] == "available"
    point_plan = report.get("point_plan")
    historical_confirmation = (
        point_plan.get("historical_confirmation")
        if isinstance(point_plan, Mapping)
        else None
    )
    uses_frozen_structure_snapshot = isinstance(historical_confirmation, Mapping)
    frozen_structure_snapshot_verified = False
    if not main_observable and not structure_available:
        return False
    if uses_frozen_structure_snapshot and (
        not main_observable or not structure_available
    ):
        raise FuturesMainReportError(
            "historical_confirmation要求独立可验证的冻结structure raw与final actual_main raw"
        )
    if main_observable:
        raw_path = _non_empty_text(
            main_summary.get("snapshot_path"), "actual_main.snapshot_path"
        )
        raw_sha256 = _sha256_text(
            main_summary.get("snapshot_sha256"), "actual_main.snapshot_sha256"
        )
        raw_label = "actual_main.snapshot_path"
    else:
        # A requested contract can have observable structure even when the
        # product-wide main chain is unavailable.  Missing main evidence must
        # not bypass byte verification and deterministic structure replay.
        raw_path = _non_empty_text(
            structure_summary.get("input_snapshot_path"),
            "structure_evidence.input_snapshot_path",
        )
        raw_sha256 = _sha256_text(
            structure_summary.get("input_sha256"),
            "structure_evidence.input_sha256",
        )
        raw_label = "structure_evidence.input_snapshot_path"
    if structure_available and not uses_frozen_structure_snapshot:
        if (
            structure_summary.get("input_snapshot_path") != raw_path
            or structure_summary.get("input_sha256") != raw_sha256
        ):
            raise FuturesMainReportError(
                "actual_main与structure_evidence必须共用同一raw快照路径与字节哈希"
            )
    if uses_frozen_structure_snapshot and (
        structure_summary.get("input_snapshot_path") == raw_path
        and structure_summary.get("input_sha256") == raw_sha256
    ):
        raise FuturesMainReportError(
            "historical_confirmation必须使用早于final raw的独立冻结structure snapshot"
        )
    raw = _load_verified_json_artifact(
        raw_path,
        raw_sha256,
        artifact_root,
        raw_label,
    )
    if raw is None:
        return False
    if raw.get("success") is not True:
        raise FuturesMainReportError("可观测主力或结构不得绑定失败的raw快照")
    raw_actual_main = _raw_contract_symbol(
        raw.get("actual_main", raw.get("main_contract"))
    )
    raw_analysis = _raw_contract_symbol(
        raw.get("analysis_contract", raw.get("actual_main", raw.get("main_contract")))
    )
    if raw_analysis != analysis_contract or (
        main_observable and raw_actual_main != selected_actual_main_contract
    ):
        raise FuturesMainReportError("raw快照的主力/分析合约与报告target不一致")
    raw_request_started_at = _timestamp(
        raw.get("request_started_at"), "raw.request_started_at"
    )
    raw_chain_observed_at = _timestamp(
        raw.get("chain_observed_at"), "raw.chain_observed_at"
    )
    raw_fetch_time = _timestamp(raw.get("fetch_time"), "raw.fetch_time")
    report_observed_at = _timestamp(report.get("observed_at"), "报告观察时间")
    if not raw_request_started_at <= raw_chain_observed_at <= raw_fetch_time:
        raise FuturesMainReportError(
            "raw.request_started_at、chain_observed_at与fetch_time顺序不闭合"
        )
    if raw_fetch_time > report_observed_at:
        raise FuturesMainReportError("raw.fetch_time不得晚于报告观察时间")
    if scan_summary is not None:
        focus_fetch_value = scan_summary["focus"].get("fetch_time")
        if focus_fetch_value is not None:
            focus_fetch_time = _timestamp(
                focus_fetch_value, "scan focus.fetch_time"
            )
            if raw_request_started_at < focus_fetch_time:
                raise FuturesMainReportError(
                    "决策前focus refresh必须在scan焦点抓取完成后发起"
                )
    chain_exchange_identity_verified: list[bool] | None = None
    if product is not None and exchange is not None:
        _validate_contract_for_product(
            raw_analysis,
            product,
            exchange,
            "raw.analysis_contract",
            reference_date=raw_fetch_time.date(),
        )
        if raw_actual_main is not None:
            _validate_contract_for_product(
                raw_actual_main,
                product,
                exchange,
                "raw.actual_main",
                reference_date=raw_fetch_time.date(),
            )
        target = report.get("target")
        if not isinstance(target, Mapping):
            raise FuturesMainReportError("v3报告缺少target，无法核对raw请求模式")
        requested_symbol = _non_empty_text(
            raw.get("requested_symbol"), "raw.requested_symbol"
        ).upper()
        target_mode = target.get("mode")
        if target_mode == "user_specified_contract":
            requested_contract = _validate_contract_for_product(
                target.get("requested_contract"),
                product,
                exchange,
                "target.requested_contract",
                reference_date=raw_fetch_time.date(),
            )
            if (
                requested_symbol != requested_contract
                or raw.get("analysis_mode") != "requested_specific_contract"
            ):
                raise FuturesMainReportError(
                    "user_specified_contract必须由同一指定月份请求且raw.analysis_mode一致"
                )
        elif target_mode == "actual_main":
            if (
                requested_symbol != f"{product}0"
                or raw.get("analysis_mode") != "product_actual_main"
                or raw_actual_main != raw_analysis
            ):
                raise FuturesMainReportError(
                    "actual_main模式必须从品种入口请求并分析raw重算的实际主力"
                )
        else:  # already rejected by target validation; keep this helper safe in isolation
            raise FuturesMainReportError("target.mode不受支持")

        verified_exchange_statuses = {
            "verified_exact",
            "verified_named_alias",
            "verified_known_ine_under_shfe_alias",
        }
        provider_alias_seen = False

        def validate_exchange_identity(
            row: Any,
            label: str,
            *,
            require_verified: bool,
        ) -> str:
            nonlocal provider_alias_seen
            if not isinstance(row, Mapping):
                raise FuturesMainReportError(f"{label}必须是带交易所身份的合约行")
            # ``exchange`` is our configured identity, not a provider label.
            # Requiring the exact configured token prevents a lower-cased or
            # missing value from being silently normalised into agreement.
            if row.get("exchange") != exchange:
                raise FuturesMainReportError(
                    f"{label}.exchange必须精确等于品种配置交易所{exchange}"
                )
            derived_status = provider_exchange_identity(
                exchange, row.get("provider_exchange")
            )
            if row.get("exchange_identity_status") != derived_status:
                raise FuturesMainReportError(
                    f"{label}.exchange_identity_status必须由provider_exchange重算"
                )
            if derived_status == "verified_known_ine_under_shfe_alias":
                provider_alias_seen = True
            if require_verified and derived_status not in verified_exchange_statuses:
                raise FuturesMainReportError(
                    f"{label}.provider_exchange缺失或与配置交易所冲突"
                )
            return derived_status

        raw_exchange_rows = raw.get("actual_contract_chain")
        if isinstance(raw_exchange_rows, list):
            chain_exchange_identity_verified = []
            chain_identity_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
            for index, row in enumerate(raw_exchange_rows):
                status = validate_exchange_identity(
                    row,
                    f"raw.actual_contract_chain[{index}]",
                    require_verified=False,
                )
                chain_exchange_identity_verified.append(
                    status in verified_exchange_statuses
                )
                assert isinstance(row, Mapping)
                symbol = _raw_contract_symbol(row)
                if symbol is None:
                    raise FuturesMainReportError(
                        f"raw.actual_contract_chain[{index}].symbol缺失"
                    )
                _validate_contract_for_product(
                    symbol,
                    product,
                    exchange,
                    f"raw.actual_contract_chain[{index}].symbol",
                    reference_date=raw_fetch_time.date(),
                )
                chain_identity_by_symbol.setdefault(symbol, []).append(row)
        else:
            chain_identity_by_symbol = {}

        headline_rows = (
            ("raw.actual_main", raw.get("actual_main"), raw_actual_main),
            ("raw.analysis_contract", raw.get("analysis_contract"), raw_analysis),
        )
        for label, headline, headline_symbol in headline_rows:
            if headline_symbol is None and label == "raw.actual_main":
                if headline is not None:
                    raise FuturesMainReportError(
                        "raw.actual_main不可观测时必须显式为null"
                    )
                continue
            validate_exchange_identity(headline, label, require_verified=True)
            assert isinstance(headline, Mapping)
            matching_chain_rows = chain_identity_by_symbol.get(
                str(headline_symbol), []
            )
            if len(matching_chain_rows) > 1:
                raise FuturesMainReportError(
                    f"{label}在actual_contract_chain中必须唯一"
                )
            if matching_chain_rows and any(
                headline.get(field) != matching_chain_rows[0].get(field)
                for field in (
                    "exchange",
                    "provider_exchange",
                    "exchange_identity_status",
                )
            ):
                raise FuturesMainReportError(
                    f"{label}交易所身份必须与actual_contract_chain同合约行一致"
                )
        if provider_alias_seen:
            _non_empty_text(
                target.get("provider_exchange_alias_disclosure"),
                "target.provider_exchange_alias_disclosure",
            )

        # Reuse the scanner's pure chain replay for the decision-time refresh.
        # A final focus snapshot is newer than the candidate scan, but it must
        # not be validated by a weaker definition of provider identity, time
        # normalisation, chain hashing, completeness, or main selection.
        try:
            try:
                from scripts.scan_market import (
                    _recompute_actual_main,
                    _recompute_actual_main_liquidity,
                )
            except ImportError:  # pragma: no cover - direct script execution
                from scan_market import (  # type: ignore[no-redef]
                    _recompute_actual_main,
                    _recompute_actual_main_liquidity,
                )
            (
                replayed_actual_main,
                replayed_chain_status,
                replayed_chain_gaps,
            ) = _recompute_actual_main(raw, product)
            replayed_liquidity_status, replayed_liquidity_gaps = (
                _recompute_actual_main_liquidity(raw, replayed_actual_main)
            )
        except Exception as exc:
            raise FuturesMainReportError(
                "final focus raw无法用scan_market主力全链逻辑重放"
            ) from exc

        # Coverage limitations may legitimately yield a partial leader.  A
        # contradiction inside signed bytes is different: it cannot be washed
        # into a lower confidence label and must be rejected even when all
        # enclosing hashes have been recomputed.
        fatal_replay_gap_markers = (
            "conflict",
            "duplicate",
            "time_normalization_unverified",
            "provider_time_unobservable",
            "selection_metadata_missing",
            "time_evidence_missing",
            "incomplete_reason_missing",
        )
        fatal_replay_gaps = [
            gap
            for gap in replayed_chain_gaps
            if any(marker in gap for marker in fatal_replay_gap_markers)
        ]
        if fatal_replay_gaps:
            raise FuturesMainReportError(
                "final focus raw主力全链重放发现不可降级的事实冲突："
                + ",".join(fatal_replay_gaps)
            )
        if raw_actual_main != replayed_actual_main:
            raise FuturesMainReportError(
                "raw.actual_main必须等于完整actual_contract_chain重算结果"
            )
        if selected_actual_main_contract != replayed_actual_main:
            raise FuturesMainReportError(
                "报告actual_main必须等于final focus raw全链重算结果"
            )
        if main_summary.get("status") != replayed_chain_status:
            raise FuturesMainReportError(
                "actual_main.status必须由final focus raw的全链完整性重算"
            )
        selection = raw.get("main_selection")
        if not isinstance(selection, Mapping):
            raise FuturesMainReportError("raw快照缺少main_selection重算口径")
        raw_chain_for_replay = raw.get("actual_contract_chain")
        if not isinstance(raw_chain_for_replay, list):
            raise FuturesMainReportError("raw快照缺少actual_contract_chain")
        if selection.get("actual_contract_chain_sha256") != _canonical_json_sha256(
            raw_chain_for_replay
        ):
            raise FuturesMainReportError(
                "raw.main_selection.actual_contract_chain_sha256必须由全链内容重算"
            )
        report_main = report.get("actual_main")
        if not isinstance(report_main, Mapping):
            raise FuturesMainReportError("报告缺少actual_main")
        for field in ("chain_complete_for_product", "pagination_complete"):
            if selection.get(field) != report_main.get(field):
                raise FuturesMainReportError(
                    f"actual_main.{field}必须等于raw.main_selection重放值"
                )
        provisional_gap_prefixes = (
            "actual_main_chain_unknown_open_interest",
            "actual_main_chain_top_volume_unobservable",
            "actual_main_chain_mixed_snapshot_time",
            "actual_main_chain_time_unobservable",
            "actual_main_chain_time_after_fetch",
        )
        expected_raw_selection_claim = (
            "unobservable_no_rankable_row"
            if replayed_actual_main is None
            else "verified_actual_main"
            if replayed_chain_status == "complete"
            else "provisional_leader_of_observed_oi_rows"
            if any(
                gap.startswith(provisional_gap_prefixes)
                for gap in replayed_chain_gaps
            )
            else "leader_of_available_rows"
        )
        if (
            selection.get("selection_claim") != expected_raw_selection_claim
            or (
                main_observable
                and main_summary.get("selection_claim")
                != expected_raw_selection_claim
            )
        ):
            raise FuturesMainReportError(
                "selection_claim必须由主力全链缺口与完整性重算"
            )
        if selection.get("main_liquidity_status") != replayed_liquidity_status:
            raise FuturesMainReportError(
                "raw.main_selection.main_liquidity_status必须由OI首位行重算"
            )
        if main_observable and main_summary.get(
            "liquidity_status"
        ) != replayed_liquidity_status:
            raise FuturesMainReportError(
                "actual_main.liquidity_status必须等于final raw重算结果"
            )
        if any("claim_conflict" in gap for gap in replayed_liquidity_gaps):
            raise FuturesMainReportError(
                "final focus raw主力流动性持久化声明与重算结果冲突"
            )
    plan = report.get("point_plan")
    requires_verified_now_quote = (
        report.get("action") in {"long", "short"}
        and isinstance(plan, Mapping)
        and plan.get("order_timing") == "now"
    )
    requires_directional_quote_binding = report.get("action") in {"long", "short"}
    if requires_directional_quote_binding:
        raw_analysis_value = raw.get("analysis_contract")
        market = report.get("market_data")
        report_quote = market.get("quote") if isinstance(market, Mapping) else None
        if not isinstance(raw_analysis_value, Mapping) or not isinstance(
            report_quote, Mapping
        ):
            raise FuturesMainReportError("long/short必须绑定已验raw中的分析合约报价")
        if _raw_contract_symbol(raw_analysis_value) != analysis_contract:
            raise FuturesMainReportError("long/short的raw报价合约与target不一致")
        raw_price = _decimal(
            raw_analysis_value.get("price"), "raw.analysis_contract.price"
        )
        report_price = _decimal(report_quote.get("price"), "market_data.quote.price")
        if raw_price != report_price:
            raise FuturesMainReportError("方向性报告价格必须等于已验raw分析合约价格")
        raw_quote_time = _raw_observed_at(
            raw_analysis_value.get("quote_time"),
            "raw.analysis_contract.quote_time",
        )
        report_quote_time = _timestamp(
            report_quote.get("observed_at"), "market_data.quote.observed_at"
        ).astimezone(BEIJING_TIMEZONE)
        if abs((raw_quote_time - report_quote_time).total_seconds()) > 1:
            raise FuturesMainReportError("方向性报告报价时间必须等于已验raw时间")
        raw_volume = _exact_integer(
            raw_analysis_value.get("volume"),
            "raw.analysis_contract.volume",
            minimum=0,
        )
        if raw_volume <= 0:
            raise FuturesMainReportError(
                "long/short requires positive verified raw analysis-contract volume"
            )
        report_volume = _exact_integer(
            report_quote.get("volume"), "market_data.quote.volume", minimum=0
        )

        def optional_quote_price(value: Any, label: str) -> Decimal | None:
            if value is None:
                return None
            return _decimal(value, label)

        raw_bid = optional_quote_price(
            raw_analysis_value.get("bid"), "raw.analysis_contract.bid"
        )
        raw_ask = optional_quote_price(
            raw_analysis_value.get("ask"), "raw.analysis_contract.ask"
        )
        report_bid = optional_quote_price(
            report_quote.get("bid"), "market_data.quote.bid"
        )
        report_ask = optional_quote_price(
            report_quote.get("ask"), "market_data.quote.ask"
        )
        if (report_volume, report_bid, report_ask) != (raw_volume, raw_bid, raw_ask):
            raise FuturesMainReportError(
                "long/short report volume/bid/ask must equal verified raw analysis quote"
            )
    if requires_verified_now_quote:
        assert isinstance(report_quote, Mapping)
        if (
            raw_bid is None
            or raw_ask is None
            or raw_bid <= 0
            or raw_ask <= 0
            or raw_bid > raw_ask
            or not _tick_aligned(raw_bid, tick_size)
            or not _tick_aligned(raw_ask, tick_size)
        ):
            raise FuturesMainReportError(
                "order_timing=now requires positive non-inverted tick-aligned verified bid/ask"
            )
        spread = _decimal(report_quote.get("spread"), "market_data.quote.spread")
        if spread != raw_ask - raw_bid:
            raise FuturesMainReportError(
                "market_data.quote.spread must be recomputed from verified bid/ask"
            )
        expected_side = "ask" if report.get("action") == "long" else "bid"
        executable_price = raw_ask if expected_side == "ask" else raw_bid
        if (
            report_quote.get("executable_side") != expected_side
            or _decimal(
                report_quote.get("executable_price"),
                "market_data.quote.executable_price",
            )
            != executable_price
        ):
            raise FuturesMainReportError(
                "order_timing=now must bind the directionally executable quote side"
            )
        entry_zone = plan.get("entry_zone")
        if not isinstance(entry_zone, Mapping):
            raise FuturesMainReportError("order_timing=now is missing entry_zone")
        entry_lower = _decimal(entry_zone.get("lower"), "point_plan.entry_zone.lower")
        entry_upper = _decimal(entry_zone.get("upper"), "point_plan.entry_zone.upper")
        if not entry_lower <= executable_price <= entry_upper:
            raise FuturesMainReportError(
                "order_timing=now executable-side price must be inside entry_zone"
            )
        _non_empty_text(
            report_quote.get("spread_cost_assumption"),
            "market_data.quote.spread_cost_assumption",
        )
    technical = raw.get("technical")
    if not isinstance(technical, Mapping):
        raise FuturesMainReportError("raw快照缺少technical证据")
    if _decimal(technical.get("tick_size"), "raw.technical.tick_size") != tick_size:
        raise FuturesMainReportError("raw快照tick数值必须与合约配置一致")
    raw_tick_exact = technical.get("tick_size_exact")
    if raw_tick_exact not in {True, False} or (
        structure_summary["status"] == "available"
        and not uses_frozen_structure_snapshot
        and raw_tick_exact != structure_summary.get("tick_size_exact")
    ):
        raise FuturesMainReportError("raw与structure artifact的tick精度声明不一致")
    if _raw_contract_symbol(technical.get("tick_size_contract")) != analysis_contract:
        raise FuturesMainReportError("raw.technical.tick_size_contract必须绑定analysis_contract")
    _non_empty_text(technical.get("tick_size_source"), "raw.technical.tick_size_source")
    expected_raw_tick_precision = (
        "exact_actual_contract"
        if raw_tick_exact is True
        else "approximate_same_product_proxy"
    )
    if technical.get("tick_size_precision") != expected_raw_tick_precision:
        raise FuturesMainReportError("raw.technical.tick_size_precision与exact语义不一致")
    raw_tick_evidence = _validate_tick_size_evidence_v3(
        technical.get("tick_size_evidence"),
        contract=analysis_contract,
        tick_size=tick_size,
        exact=bool(raw_tick_exact),
        observed_at=raw_fetch_time,
        not_before=raw_request_started_at,
        label="raw.technical.tick_size_evidence",
    )
    (
        final_daily_status,
        final_daily_cache_artifact_replay_verified,
        final_upstream_response_identity_verified,
    ) = (
        _validate_raw_daily_evidence_v3(
            technical,
            analysis_contract=analysis_contract,
            label="raw.technical",
        )
    )
    if (
        structure_available
        and not uses_frozen_structure_snapshot
        and raw_tick_evidence != structure_summary.get("tick_size_evidence")
    ):
        raise FuturesMainReportError("raw与structure artifact的tick_size_evidence必须完全一致")
    verified_completed_daily_rows = _normalize_raw_completed_daily_rows_v3(
        raw,
        analysis_contract=analysis_contract,
        tick_size=tick_size,
        observed_at=report_observed_at,
    )
    if market_summary is not None and market_summary.get("latest_bar") is not None:
        report_latest = {
            "contract": analysis_contract,
            **dict(market_summary["latest_bar"]),
        }
        if not _completed_bar_facts_equal(
            report_latest, verified_completed_daily_rows[-1]
        ):
            raise FuturesMainReportError(
                "market_data.latest_completed_bar必须逐字段等于已验final raw完成K线最后一根"
            )
    if structure_available:
        structure_raw = raw
        structure_raw_sha256 = raw_sha256
        structure_technical = technical
        structure_rows = verified_completed_daily_rows
        if uses_frozen_structure_snapshot:
            structure_raw = _load_verified_json_artifact(
                structure_summary.get("input_snapshot_path"),
                _sha256_text(
                    structure_summary.get("input_sha256"),
                    "structure_evidence.input_sha256",
                ),
                artifact_root,
                "structure_evidence.input_snapshot_path",
            )
            if structure_raw is None:
                raise FuturesMainReportError(
                    "historical_confirmation生产校验必须读取冻结structure raw字节"
                )
            if structure_raw.get("success") is not True:
                raise FuturesMainReportError("冻结structure raw必须是成功fetch artifact")
            structure_raw_sha256 = _sha256_text(
                structure_summary.get("input_sha256"),
                "structure_evidence.input_sha256",
            )
            frozen_analysis = _raw_contract_symbol(
                structure_raw.get(
                    "analysis_contract",
                    structure_raw.get(
                        "actual_main", structure_raw.get("main_contract")
                    ),
                )
            )
            if frozen_analysis != analysis_contract:
                raise FuturesMainReportError(
                    "冻结structure raw.analysis_contract必须等于最终analysis_contract"
                )
            frozen_request_started_at = _timestamp(
                structure_raw.get("request_started_at"),
                "冻结structure raw.request_started_at",
            )
            frozen_chain_observed_at = _timestamp(
                structure_raw.get("chain_observed_at"),
                "冻结structure raw.chain_observed_at",
            )
            frozen_fetch_time = _timestamp(
                structure_raw.get("fetch_time"), "冻结structure raw.fetch_time"
            )
            if not (
                frozen_request_started_at
                <= frozen_chain_observed_at
                <= frozen_fetch_time
                <= raw_request_started_at
            ):
                raise FuturesMainReportError(
                    "冻结structure raw必须在final raw请求前完成且内部时间顺序闭合"
                )
            if product is not None and exchange is not None:
                _validate_contract_for_product(
                    frozen_analysis,
                    product,
                    exchange,
                    "冻结structure raw.analysis_contract",
                    reference_date=frozen_fetch_time.date(),
                )
                frozen_analysis_value = structure_raw.get("analysis_contract")
                validate_exchange_identity(
                    frozen_analysis_value,
                    "冻结structure raw.analysis_contract",
                    require_verified=True,
                )
            structure_technical_value = structure_raw.get("technical")
            if not isinstance(structure_technical_value, Mapping):
                raise FuturesMainReportError("冻结structure raw缺少technical证据")
            structure_technical = structure_technical_value
            frozen_daily_status, _, _ = _validate_raw_daily_evidence_v3(
                structure_technical,
                analysis_contract=analysis_contract,
                label="冻结structure raw.technical",
            )
            try:
                replay_daily_artifact_evidence(
                    dict(structure_technical),
                    contract=analysis_contract,
                )
            except (DailyCacheVerificationError, OSError, ValueError) as exc:
                raise FuturesMainReportError(
                    "historical_confirmation的冻结S1必须独立重放已锚定日线artifact；"
                    "仅有S1/S2互相一致不能证明历史未被协同重签"
                ) from exc
            if _decimal(
                structure_technical.get("tick_size"),
                "冻结structure raw.technical.tick_size",
            ) != tick_size:
                raise FuturesMainReportError("冻结structure raw tick与配置不一致")
            frozen_tick_exact = structure_technical.get("tick_size_exact")
            if (
                frozen_tick_exact not in {True, False}
                or frozen_tick_exact != structure_summary.get("tick_size_exact")
            ):
                raise FuturesMainReportError(
                    "冻结structure raw与artifact tick精度声明不一致"
                )
            if _raw_contract_symbol(
                structure_technical.get("tick_size_contract")
            ) != analysis_contract:
                raise FuturesMainReportError(
                    "冻结structure raw.tick_size_contract必须绑定analysis_contract"
                )
            frozen_tick_evidence = _validate_tick_size_evidence_v3(
                structure_technical.get("tick_size_evidence"),
                contract=analysis_contract,
                tick_size=tick_size,
                exact=bool(frozen_tick_exact),
                observed_at=frozen_fetch_time,
                not_before=frozen_request_started_at,
                label="冻结structure raw.technical.tick_size_evidence",
            )
            if frozen_tick_evidence != structure_summary.get("tick_size_evidence"):
                raise FuturesMainReportError(
                    "冻结structure raw与structure artifact的tick证据必须完全一致"
                )
            structure_rows = _normalize_raw_completed_daily_rows_v3(
                structure_raw,
                analysis_contract=analysis_contract,
                tick_size=tick_size,
                observed_at=report_observed_at,
            )
            if _daily_methodology_identity_v3(
                structure_technical,
                analysis_contract=analysis_contract,
                label="冻结structure raw.technical",
            ) != _daily_methodology_identity_v3(
                technical,
                analysis_contract=analysis_contract,
                label="final raw.technical",
            ):
                raise FuturesMainReportError(
                    "冻结structure raw与final raw必须使用同一日线来源和完成口径"
                )
            if frozen_daily_status != final_daily_status:
                raise FuturesMainReportError(
                    "冻结structure raw与final raw日线证据状态不得漂移"
                )
            _validate_frozen_final_daily_lineage_v3(
                structure_rows, verified_completed_daily_rows
            )

        structure_raw_as_of = _raw_completed_as_of(structure_raw)
        if structure_raw_as_of != structure_summary.get("as_of"):
            raise FuturesMainReportError("冻结/绑定structure raw最近完成日与结构as_of不一致")
        daily_evidence = structure_technical.get("daily_data_evidence")
        artifact = report.get("structure_evidence", {}).get("artifact")
        artifact_daily = (
            artifact.get("daily_data_evidence")
            if isinstance(artifact, Mapping)
            else None
        )
        if not isinstance(daily_evidence, Mapping) or artifact_daily != daily_evidence:
            raise FuturesMainReportError("raw与structure artifact的日线证据状态不一致")
        try:
            try:
                from scripts.market_structure import _compact_fetch_structure
            except ImportError:  # pragma: no cover - direct script execution
                from market_structure import _compact_fetch_structure
            regenerated = _compact_fetch_structure(dict(structure_raw))
        except (TypeError, ValueError) as exc:
            raise FuturesMainReportError("raw快照无法按声明算法重算结构") from exc
        regenerated["input_snapshot_sha256"] = structure_raw_sha256
        if _canonical_json_file_sha256(regenerated) != structure_summary.get(
            "artifact_sha256"
        ):
            raise FuturesMainReportError("内嵌结构artifact不是由绑定raw按声明算法重算所得")
        if uses_frozen_structure_snapshot:
            # Set only after S1 bytes, independent daily artifact replay,
            # S1/S2 lineage, tick evidence and deterministic structure replay
            # have all succeeded.
            frozen_structure_snapshot_verified = True

    if not main_observable:
        raw_chain = raw.get("actual_contract_chain")
        if not isinstance(raw_chain, list):
            raise FuturesMainReportError(
                "主力不可观测也必须在raw中显式保留actual_contract_chain检查结果"
            )
        rankable_count = 0
        for index, row in enumerate(raw_chain):
            if not isinstance(row, Mapping):
                raise FuturesMainReportError(
                    f"raw.actual_contract_chain[{index}]格式错误"
                )
            symbol = _raw_contract_symbol(row)
            if symbol is None:
                raise FuturesMainReportError("主力不可观测的raw合约行仍必须标明具体月份")
            if product is not None and exchange is not None:
                _validate_contract_for_product(
                    symbol,
                    product,
                    exchange,
                    f"raw.actual_contract_chain[{index}].symbol",
                    reference_date=raw_fetch_time.date(),
                )
            try:
                oi = _decimal(
                    row.get("open_interest"),
                    f"raw.actual_contract_chain[{index}].open_interest",
                )
            except FuturesMainReportError:
                continue
            identity_verified = (
                chain_exchange_identity_verified is None
                or chain_exchange_identity_verified[index]
            )
            if identity_verified and oi > 0 and oi == oi.to_integral_value():
                rankable_count += 1
        if rankable_count:
            raise FuturesMainReportError(
                "raw已有可排名正OI合约行时不得声称actual_main不可观测"
            )
        selection = raw.get("main_selection")
        if (
            not isinstance(selection, Mapping)
            or _exact_integer(
                selection.get("source_record_count"),
                "raw.main_selection.source_record_count",
                minimum=0,
            )
            != len(raw_chain)
            or _exact_integer(
                selection.get("available_contract_count"),
                "raw.main_selection.available_contract_count",
                minimum=0,
            )
            != 0
            or _exact_integer(
                selection.get("rankable_contract_count"),
                "raw.main_selection.rankable_contract_count",
                minimum=0,
            )
            != 0
            or selection.get("actual_main_symbol") is not None
            or selection.get("selection_claim")
            != "unobservable_no_rankable_row"
            or selection.get("main_liquidity_status") != "unobservable"
            or selection.get("chain_complete_for_product") is not False
        ):
            raise FuturesMainReportError(
                "主力不可观测必须与raw.main_selection的零可排名行语义闭合"
            )
        if raw_binding_out is not None:
            raw_binding_out.update(
                {
                    "completed_daily_rows": verified_completed_daily_rows,
                    "completed_daily_status": final_daily_status,
                    "daily_cache_artifact_replay_verified": (
                        final_daily_cache_artifact_replay_verified
                    ),
                    "upstream_response_identity_verified": (
                        final_upstream_response_identity_verified
                    ),
                    "frozen_structure_snapshot_verified": (
                        frozen_structure_snapshot_verified
                    ),
                }
            )
        return True

    raw_chain = raw.get("actual_contract_chain")
    if not isinstance(raw_chain, list) or not raw_chain:
        raise FuturesMainReportError("raw快照缺少未截断的actual_contract_chain")
    all_raw_rows: dict[str, Mapping[str, Any]] = {}
    rankable_raw_rows: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(raw_chain):
        if not isinstance(row, Mapping):
            raise FuturesMainReportError(f"raw合约链[{index}]格式错误")
        symbol = _raw_contract_symbol(row)
        if symbol is None or symbol in all_raw_rows:
            raise FuturesMainReportError("raw合约链必须唯一列出具体合约")
        if product is not None and exchange is not None:
            _validate_contract_for_product(
                symbol,
                product,
                exchange,
                f"raw.actual_contract_chain[{index}].symbol",
                reference_date=raw_fetch_time.date(),
            )
        all_raw_rows[symbol] = row
        try:
            oi = _decimal(row.get("open_interest"), "raw contract open_interest")
        except FuturesMainReportError:
            oi = Decimal("-1")
        identity_verified = (
            chain_exchange_identity_verified is None
            or chain_exchange_identity_verified[index]
        )
        if identity_verified and oi > 0 and oi == oi.to_integral_value():
            rankable_raw_rows[symbol] = row
    reported_contracts = main_summary.get("contracts")
    if not isinstance(reported_contracts, list) or {
        item["contract"] for item in reported_contracts
    } != set(rankable_raw_rows):
        raise FuturesMainReportError("actual_main.contracts必须精确覆盖raw中可排名的正OI合约行")
    for item in reported_contracts:
        row = rankable_raw_rows[item["contract"]]
        raw_volume = (
            None
            if row.get("volume") is None
            else _exact_integer(row.get("volume"), "raw contract volume", minimum=0)
        )
        if (
            _exact_integer(row.get("open_interest"), "raw contract open_interest", minimum=0)
            != item["open_interest"]
            or raw_volume != item["volume"]
        ):
            raise FuturesMainReportError("actual_main持仓量/成交量必须逐行等于raw合约链")
        raw_time = _raw_observed_at(row.get("quote_time"), "raw contract quote_time")
        if abs((raw_time - item["observed_at"]).total_seconds()) > 1:
            raise FuturesMainReportError("actual_main合约链观察时间必须等于raw快照")
    selection = raw.get("main_selection")
    actual_main_report = report.get("actual_main")
    if not isinstance(selection, Mapping) or not isinstance(actual_main_report, Mapping):
        raise FuturesMainReportError("raw快照缺少main_selection重算口径")
    if (
        _raw_contract_symbol(selection.get("actual_main_symbol"))
        != selected_actual_main_contract
        or _exact_integer(
            selection.get("available_contract_count"),
            "raw main_selection.available_contract_count",
            minimum=0,
        )
        != len(rankable_raw_rows)
        or _exact_integer(
            selection.get("source_record_count"),
            "raw main_selection.source_record_count",
            minimum=0,
        )
        != len(all_raw_rows)
        or _exact_integer(
            selection.get("rankable_contract_count"),
            "raw main_selection.rankable_contract_count",
            minimum=0,
        )
        != len(rankable_raw_rows)
        or _exact_integer(
            selection.get("source_record_count"),
            "raw main_selection.source_record_count",
            minimum=0,
        )
        != _exact_integer(
            actual_main_report.get("source_record_count"),
            "actual_main.source_record_count",
            minimum=0,
        )
        or (
            main_summary.get("status") == "partial"
            and selection.get("selection_claim")
            != main_summary.get("selection_claim")
        )
    ):
        raise FuturesMainReportError("actual_main主力/计数与raw.main_selection不一致")
    if selected_actual_main_contract not in rankable_raw_rows:
        raise FuturesMainReportError(
            "actual_main选中合约必须来自provider_exchange身份已核验的可排名行"
        )
    selected_raw_row = rankable_raw_rows[selected_actual_main_contract]
    selected_raw_volume = selected_raw_row.get("volume")
    if selected_raw_volume is None:
        raw_liquidity_status = "unobservable"
    elif _exact_integer(selected_raw_volume, "raw actual main volume", minimum=0) == 0:
        raw_liquidity_status = "no_trade"
    else:
        raw_bid_value = selected_raw_row.get("bid")
        raw_ask_value = selected_raw_row.get("ask")
        raw_bid = (
            _decimal(raw_bid_value, "raw actual main bid")
            if _finite_number(raw_bid_value)
            else None
        )
        raw_ask = (
            _decimal(raw_ask_value, "raw actual main ask")
            if _finite_number(raw_ask_value)
            else None
        )
        raw_liquidity_status = (
            "observable_trade"
            if raw_bid is not None
            and raw_ask is not None
            and raw_bid > 0
            and raw_ask >= raw_bid
            else "execution_quote_incomplete"
        )
    if (
        selection.get("main_liquidity_status") != raw_liquidity_status
        or main_summary.get("liquidity_status") != raw_liquidity_status
    ):
        raise FuturesMainReportError(
            "actual_main.liquidity_status必须等于raw OI首位行及main_selection重算结果"
        )
    raw_complete = (
        selection.get("chain_complete_for_product") is True
        and selection.get("pagination_complete") is True
        and (
            chain_exchange_identity_verified is None
            or all(chain_exchange_identity_verified)
        )
        and _exact_integer(
            selection.get("source_record_count"), "raw source_record_count", minimum=0
        )
        == len(all_raw_rows)
    )
    if (main_summary["status"] == "complete") != raw_complete:
        raise FuturesMainReportError("完整/不完整主力链状态必须由raw分页与计数证据重算")
    if raw_binding_out is not None:
        raw_binding_out.update(
            {
                "completed_daily_rows": verified_completed_daily_rows,
                "completed_daily_status": final_daily_status,
                "daily_cache_artifact_replay_verified": (
                    final_daily_cache_artifact_replay_verified
                ),
                "upstream_response_identity_verified": (
                    final_upstream_response_identity_verified
                ),
                "frozen_structure_snapshot_verified": (
                    frozen_structure_snapshot_verified
                ),
            }
        )
    return True


def _validate_market_data_v3(
    report: Mapping[str, Any],
    policy: Mapping[str, Any],
    observed_at: datetime,
    analysis_contract: str,
    product: str,
    exchange: str,
    tick_size: Decimal,
) -> dict[str, Any]:
    market = report.get("market_data")
    if not isinstance(market, Mapping):
        raise FuturesMainReportError("v3报告缺少market_data")
    status = market.get("status")
    if status not in {"fresh", "stale", "unobservable"}:
        raise FuturesMainReportError("market_data.status不受支持")
    if status == "unobservable":
        if any(
            market.get(key) is not None
            for key in (
                "quote",
                "latest_completed_bar",
                "market_calendar",
                "session_aware_daily_bar",
            )
        ):
            raise FuturesMainReportError("行情不可观测时不得填造报价、完成K线或交易日历")
        _non_empty_text(market.get("unobservable_reason"), "market_data.unobservable_reason")
        _validate_source_attempts_v3(
            market.get("source_attempts"), observed_at, "market_data.source_attempts"
        )
        return {
            "status": "unobservable",
            "latest_bar": None,
            "latest_completed_date": None,
            "closed_dates": set(),
            "quote_price": None,
        }

    calendar = market.get("market_calendar")
    if not isinstance(calendar, Mapping):
        raise FuturesMainReportError("可观测行情必须包含交易所交易日历")
    calendar_as_of = _timestamp(calendar.get("as_of"), "market_calendar.as_of")
    if calendar_as_of > observed_at:
        raise FuturesMainReportError("交易日历证据不得晚于报告观察时间")
    _validate_evidence_source(
        calendar.get("source"),
        observed_at,
        "market_calendar.source",
        official_exchange=exchange,
    )
    closed_values = calendar.get("closed_dates")
    if not isinstance(closed_values, list):
        raise FuturesMainReportError("market_calendar.closed_dates必须是列表")
    closed_dates: set[date] = set()
    for index, item in enumerate(closed_values):
        if not isinstance(item, Mapping):
            raise FuturesMainReportError("休市日证据格式错误")
        closure_date = _date_value(item.get("date"), f"closed_dates[{index}].date")
        if closure_date.weekday() >= 5:
            raise FuturesMainReportError("周末无需伪装成交易所休市日")
        _non_empty_text(item.get("reason"), f"closed_dates[{index}].reason")
        if closure_date in closed_dates:
            raise FuturesMainReportError("休市日证据不得重复")
        closed_dates.add(closure_date)

    latest_bar_value = market.get("latest_completed_bar")
    if not isinstance(latest_bar_value, Mapping):
        raise FuturesMainReportError("可观测行情必须包含最近完成K线")
    evidence_kind = _non_empty_text(
        latest_bar_value.get("evidence_kind"), "latest_completed_bar.evidence_kind"
    )
    if evidence_kind not in set(
        policy["market_data_freshness"]["latest_completed_bar_evidence_kinds"]
    ):
        raise FuturesMainReportError("最近完成K线证据类型不受支持")
    latest_bar = _validate_bar_v3(
        latest_bar_value,
        observed_at,
        analysis_contract,
        tick_size,
        "market_data.latest_completed_bar",
        official_exchange=(
            exchange if evidence_kind == "official_exchange_daily" else None
        ),
    )
    latest_completed_date = latest_bar["date"]

    session_bar_summary: dict[str, Any] | None = None
    session_bar_value = market.get("session_aware_daily_bar")
    if session_bar_value is not None:
        if not isinstance(session_bar_value, Mapping):
            raise FuturesMainReportError(
                "market_data.session_aware_daily_bar必须是结构化对象"
            )
        try:
            session_bar_summary = validate_session_aware_daily_bar(
                session_bar_value,
                expected_product=product,
                expected_contract=analysis_contract,
                packet_observed_at=observed_at,
                now=observed_at,
            )
        except (SessionAwareBarError, ValueError) as exc:
            raise FuturesMainReportError(
                f"market_data.session_aware_daily_bar无效：{exc}"
            ) from exc
        session_trade_date = _date_value(
            session_bar_summary["trade_date"],
            "session_aware_daily_bar.trade_date",
        )
        if session_bar_summary["bar_state"] == "session_partial" and not (
            latest_completed_date < session_trade_date
        ):
            raise FuturesMainReportError(
                "partial daily bar必须晚于报告内最近完成日线，不能覆盖或回写历史"
            )

    quote = market.get("quote")
    if not isinstance(quote, Mapping):
        raise FuturesMainReportError("可观测行情必须包含报价")
    if str(quote.get("contract", "")).upper() != analysis_contract:
        raise FuturesMainReportError("quote.contract必须等于analysis_contract")
    quote_observed_at = _timestamp(quote.get("observed_at"), "quote.observed_at")
    if quote_observed_at > observed_at:
        raise FuturesMainReportError("报价时间不得晚于报告观察时间")
    quote_price = _decimal(quote.get("price"), "quote.price")
    if quote_price <= 0 or not _tick_aligned(quote_price, tick_size):
        raise FuturesMainReportError("报价必须为正并按tick对齐")
    _validate_evidence_source(quote.get("source"), observed_at, "quote.source")
    market_session = quote.get("market_session")
    expected_session = _expected_market_session(
        observed_at, product, policy, closed_dates
    )
    if market_session != expected_session:
        raise FuturesMainReportError("market_session必须由北京时间、品种时段与日历推导")

    freshness = policy["market_data_freshness"]
    quote_age = Decimal(str((observed_at - quote_observed_at).total_seconds()))
    if "age_seconds" in quote and abs(
        _decimal(quote.get("age_seconds"), "quote.age_seconds") - quote_age
    ) > Decimal("1"):
        raise FuturesMainReportError("quote.age_seconds必须由观察时间和报价时间推导")
    quote_is_fresh = False
    session_closed_at: datetime | None = None
    expected_boundary: datetime | None = None
    if market_session == "active":
        quote_is_fresh = quote_age <= _decimal(
            freshness["active_quote_max_age_seconds"], "活跃报价年龄上限"
        )
    else:
        session_closed_at = _timestamp(
            quote.get("session_closed_at"), "quote.session_closed_at"
        )
        expected_boundary = _latest_regular_session_boundary(
            observed_at, product, policy, closed_dates
        )
        boundary_gap = abs((session_closed_at - quote_observed_at).total_seconds())
        boundary_age = Decimal(str((observed_at - session_closed_at).total_seconds()))
        quote_is_fresh = (
            abs((session_closed_at - expected_boundary).total_seconds()) <= 1
            and quote.get("latest_session_observation") is True
            and boundary_gap <= int(freshness["session_boundary_quote_max_gap_seconds"])
            and Decimal("0") <= boundary_age <= _decimal(
                freshness["non_active_latest_session_max_age_seconds"],
                "休市后行情最长年龄",
            )
        )

    expected_latest_date = _latest_completed_session_date(observed_at, closed_dates)
    bar_is_latest = latest_completed_date == expected_latest_date
    calendar_is_fresh = Decimal(
        str((observed_at - calendar_as_of).total_seconds())
    ) <= _decimal(freshness["calendar_evidence_max_age_seconds"], "日历证据年龄上限")
    derived_status = "fresh" if quote_is_fresh and bar_is_latest and calendar_is_fresh else "stale"
    if status != derived_status:
        raise FuturesMainReportError("market_data.status必须由行情、完成日和日历时效重算")

    if evidence_kind == "actual_main_day_close_snapshot":
        if market_session != "closed" or session_closed_at is None or expected_boundary is None:
            raise FuturesMainReportError("收盘快照只能在日盘收盘后形成完成K线")
        if session_closed_at.hour != 15 or session_closed_at.minute != 0:
            raise FuturesMainReportError("只有15:00边界快照可形成完成日线")
        bar_observed_at = _timestamp(
            latest_bar_value.get("observed_at"), "latest_completed_bar.observed_at"
        )
        if (
            bar_observed_at != quote_observed_at
            or latest_bar["close"] != quote_price
            or latest_bar_value.get("source") != quote.get("source")
        ):
            raise FuturesMainReportError("收盘边界K线必须与报价同合约、同时间、同价且同源")
    return {
        "status": status,
        "latest_bar": latest_bar,
        "latest_completed_date": latest_completed_date,
        "closed_dates": closed_dates,
        "quote_price": quote_price,
        "quote_observed_at": quote_observed_at,
        "session_aware_daily_bar": session_bar_summary,
    }


DISPLAY_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+(?:\.\d+)?")
EVIDENCE_REF_SEGMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?")


def _resolve_evidence_ref(
    report: Mapping[str, Any], ref_value: Any, label: str
) -> Any:
    """Resolve a deliberately small report-local evidence path language."""
    ref = _non_empty_text(ref_value, label)
    current: Any = report
    for segment in ref.split("."):
        match = EVIDENCE_REF_SEGMENT_RE.fullmatch(segment)
        if match is None or not isinstance(current, Mapping):
            raise FuturesMainReportError(f"{label}不是可解析的报告内证据路径")
        key, index_text = match.groups()
        if key not in current:
            raise FuturesMainReportError(f"{label}指向不存在的报告字段")
        current = current[key]
        if index_text is not None:
            if not isinstance(current, list):
                raise FuturesMainReportError(f"{label}对非列表字段使用了索引")
            index = int(index_text)
            if index >= len(current):
                raise FuturesMainReportError(f"{label}列表索引越界")
            current = current[index]
    if current is None or current == "" or current == [] or current == {}:
        raise FuturesMainReportError(f"{label}不得指向空证据")
    return current


def _validate_display_numbers(
    text_value: Any,
    expected: list[Decimal],
    label: str,
) -> None:
    display = _non_empty_text(text_value, label)
    numbers = [Decimal(match) for match in DISPLAY_NUMBER_RE.findall(display)]
    if numbers != expected:
        raise FuturesMainReportError(f"{label}中的数字必须与结构化数值逐项一致")


def _validate_structure_v3(
    report: Mapping[str, Any],
    observed_at: datetime,
    analysis_contract: str,
    selected_actual_main_contract: str | None,
    tick_size: Decimal,
    latest_completed_date: date | None,
    scan_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    structure = report.get("structure_evidence")
    if not isinstance(structure, Mapping):
        raise FuturesMainReportError("v3报告缺少structure_evidence")
    status = structure.get("status")
    if status not in {"available", "unobservable"}:
        raise FuturesMainReportError("structure_evidence.status不受支持")
    if status == "unobservable":
        if structure.get("levels") is not None:
            raise FuturesMainReportError("结构不可观测时levels必须为null，不得编造点位")
        if structure.get("direction") != "unobservable":
            raise FuturesMainReportError("结构不可观测时direction必须为unobservable")
        _non_empty_text(structure.get("unobservable_reason"), "structure_evidence.unobservable_reason")
        _validate_source_attempts_v3(
            structure.get("source_attempts"),
            observed_at,
            "structure_evidence.source_attempts",
        )
        artifact = structure.get("artifact")
        artifact_sha_value = structure.get("artifact_sha256")
        if (artifact is None) != (artifact_sha_value is None):
            raise FuturesMainReportError("不可观测结构的artifact与artifact_sha256必须成对出现")
        if artifact is not None:
            if not isinstance(artifact, Mapping) or artifact.get("success") is not False:
                raise FuturesMainReportError("不可观测结构只能绑定失败的结构artifact")
            artifact_sha = _sha256_text(
                artifact_sha_value, "structure_evidence.artifact_sha256"
            )
            if artifact_sha != _canonical_json_file_sha256(artifact):
                raise FuturesMainReportError("不可观测结构artifact的canonical字节哈希不一致")
        if report.get("point_plan") is not None:
            raise FuturesMainReportError("结构不可观测时point_plan必须为null")
        if structure.get("provenance_status") != "unobservable":
            raise FuturesMainReportError(
                "不可观测结构的provenance_status必须为unobservable"
            )
        return {
            "status": status,
            "structure_id": None,
            "levels": None,
            "as_of": None,
            "provenance_status": "unobservable",
            "daily_data_evidence_status": None,
        }

    contract = _non_empty_text(structure.get("contract"), "structure_evidence.contract").upper()
    if contract != analysis_contract:
        raise FuturesMainReportError("structure_evidence必须绑定analysis_contract")
    algorithm_version = _non_empty_text(
        structure.get("algorithm_version"), "structure_evidence.algorithm_version"
    )
    if algorithm_version != "causal_pivots_zones_theil_sen_tick_aligned.v1":
        raise FuturesMainReportError("structure_evidence.algorithm_version不受支持")
    input_sha256 = _sha256_text(
        structure.get("input_sha256"), "structure_evidence.input_sha256"
    )
    input_snapshot_path = _non_empty_text(
        structure.get("input_snapshot_path"),
        "structure_evidence.input_snapshot_path",
    )
    input_relative = Path(input_snapshot_path)
    if input_relative.is_absolute() or ".." in input_relative.parts:
        raise FuturesMainReportError("structure_evidence.input_snapshot_path必须是报告证据目录内的相对路径")
    as_of = _date_value(structure.get("as_of"), "structure_evidence.as_of")
    if as_of > observed_at.date() or (
        latest_completed_date is not None and as_of > latest_completed_date
    ):
        raise FuturesMainReportError("结构as_of不得晚于报告或最近完成K线")
    direction = structure.get("direction")
    direction_labels = {
        "bullish": "多头结构",
        "bearish": "空头结构",
        "sideways": "区间结构",
    }
    if direction not in direction_labels:
        raise FuturesMainReportError("结构方向必须是bullish、bearish或sideways")
    if structure.get("direction_text") != direction_labels[direction]:
        raise FuturesMainReportError("结构方向文本必须与结构化方向一致")
    artifact = structure.get("artifact")
    if not isinstance(artifact, Mapping) or artifact.get("success") is not True:
        raise FuturesMainReportError("可观测结构必须内嵌scan_market生成的成功结构artifact")
    artifact_sha256 = _sha256_text(
        structure.get("artifact_sha256"), "structure_evidence.artifact_sha256"
    )
    if artifact_sha256 != _canonical_json_file_sha256(artifact):
        raise FuturesMainReportError("structure artifact的canonical文件字节哈希不一致")
    if artifact.get("schema_version") != "cn_futures_market_structure.v1":
        raise FuturesMainReportError("结构artifact schema不受支持")
    if artifact.get("algorithm_version") != algorithm_version:
        raise FuturesMainReportError("结构algorithm_version与artifact不一致")
    if artifact.get("input_snapshot_sha256") != input_sha256:
        raise FuturesMainReportError("结构input_sha256与artifact原始输入绑定不一致")
    if str(artifact.get("analysis_contract", "")).upper() != analysis_contract:
        raise FuturesMainReportError("结构artifact未绑定analysis_contract")
    point_plan = report.get("point_plan")
    historical_confirmation = (
        point_plan.get("historical_confirmation")
        if isinstance(point_plan, Mapping)
        else None
    )
    if (
        historical_confirmation is None
        and selected_actual_main_contract is not None
        and str(artifact.get("actual_main", "")).upper()
        != selected_actual_main_contract
    ):
        raise FuturesMainReportError("结构artifact的actual_main未绑定本次主力链")
    artifact_tick_size = _decimal(artifact.get("tick_size"), "artifact.tick_size")
    if artifact_tick_size != tick_size:
        raise FuturesMainReportError("结构artifact的tick_size与品种配置不一致")
    tick_size_exact = artifact.get("tick_size_exact")
    if tick_size_exact not in {True, False}:
        raise FuturesMainReportError("结构artifact必须明示tick_size_exact")
    expected_tick_actionability = (
        "exact_order_compatible"
        if tick_size_exact is True
        else "research_only_not_for_exact_order"
    )
    if artifact.get("tick_size_actionability") != expected_tick_actionability:
        raise FuturesMainReportError("结构artifact的tick行动语义与精度标记不一致")
    expected_tick_precision = (
        "exact_actual_contract"
        if tick_size_exact is True
        else "approximate_same_product_proxy"
    )
    if artifact.get("tick_size_precision") != expected_tick_precision:
        raise FuturesMainReportError("结构artifact的tick_size_precision与exact语义不一致")
    if _raw_contract_symbol(artifact.get("tick_size_contract")) != analysis_contract:
        raise FuturesMainReportError("结构artifact.tick_size_contract必须绑定analysis_contract")
    _non_empty_text(artifact.get("tick_size_source"), "artifact.tick_size_source")
    artifact_fetch_time_value = artifact.get("fetch_time")
    artifact_fetch_time = (
        observed_at
        if artifact_fetch_time_value is None
        else _timestamp(artifact_fetch_time_value, "artifact.fetch_time")
    )
    if artifact_fetch_time > observed_at:
        raise FuturesMainReportError("artifact.fetch_time不得晚于报告观察时间")
    tick_size_evidence = _validate_tick_size_evidence_v3(
        artifact.get("tick_size_evidence"),
        contract=analysis_contract,
        tick_size=tick_size,
        exact=tick_size_exact,
        observed_at=artifact_fetch_time,
        label="artifact.tick_size_evidence",
    )
    if artifact.get("as_of") != as_of.isoformat():
        raise FuturesMainReportError("结构as_of与artifact不一致")
    artifact_daily_evidence = artifact.get("daily_data_evidence")
    daily_data_evidence_status = (
        artifact_daily_evidence.get("status")
        if isinstance(artifact_daily_evidence, Mapping)
        else "unobservable"
    )
    if daily_data_evidence_status not in {"verified", "degraded", "unobservable"}:
        raise FuturesMainReportError("artifact.daily_data_evidence.status不受支持")
    expected_provenance_status = (
        "verified"
        if daily_data_evidence_status == "verified"
        else "partial_daily_data_evidence_not_verified"
    )
    if structure.get("provenance_status") != expected_provenance_status:
        raise FuturesMainReportError(
            "structure_evidence.provenance_status必须由artifact日线证据status重算"
        )
    artifact_direction = artifact.get("trend", {}).get("direction") if isinstance(
        artifact.get("trend"), Mapping
    ) else None
    expected_direction = {"up": "bullish", "down": "bearish", "range": "sideways"}.get(
        str(artifact_direction)
    )
    if direction != expected_direction:
        raise FuturesMainReportError("结构方向必须由artifact趋势方向映射")
    levels = structure.get("levels")
    if not isinstance(levels, Mapping) or set(levels) != {"support", "resistance", "trend"}:
        raise FuturesMainReportError("结构必须且只能给出一个支撑、一个压力和一个趋势位")
    support = levels["support"]
    resistance = levels["resistance"]
    trend = levels["trend"]
    support_lower, support_upper = _validate_zone(support, "最强支撑")
    resistance_lower, resistance_upper = _validate_zone(resistance, "最强压力")
    if not isinstance(support, Mapping) or not isinstance(resistance, Mapping) or not isinstance(trend, Mapping):
        raise FuturesMainReportError("结构点位格式错误")
    trend_price = _decimal(trend.get("price"), "趋势位")
    prices = [support_lower, support_upper, resistance_lower, resistance_upper, trend_price]
    if any(price <= 0 or not _tick_aligned(price, tick_size) for price in prices):
        raise FuturesMainReportError("结构点位必须为正并按tick对齐")
    if support_upper >= resistance_lower:
        raise FuturesMainReportError("最强支撑必须严格低于最强压力")
    _validate_display_numbers(
        support.get("text"), [support_lower, support_upper], "最强支撑文本"
    )
    _validate_display_numbers(
        resistance.get("text"), [resistance_lower, resistance_upper], "最强压力文本"
    )
    _validate_display_numbers(trend.get("text"), [trend_price], "趋势位文本")
    artifact_support = artifact.get("support")
    artifact_resistance = artifact.get("resistance")
    artifact_trend = artifact.get("trend")
    if not all(
        isinstance(value, Mapping)
        for value in (artifact_support, artifact_resistance, artifact_trend)
    ):
        raise FuturesMainReportError("结构artifact缺少支撑、压力或趋势明细")
    assert isinstance(artifact_support, Mapping)
    assert isinstance(artifact_resistance, Mapping)
    assert isinstance(artifact_trend, Mapping)
    if (
        _decimal(artifact_support.get("lower"), "artifact.support.lower") != support_lower
        or _decimal(artifact_support.get("upper"), "artifact.support.upper") != support_upper
        or _decimal(artifact_resistance.get("lower"), "artifact.resistance.lower") != resistance_lower
        or _decimal(artifact_resistance.get("upper"), "artifact.resistance.upper") != resistance_upper
        or _decimal(artifact_trend.get("value"), "artifact.trend.value") != trend_price
    ):
        raise FuturesMainReportError("报告点位必须逐项等于绑定structure artifact的输出")
    binding = {
        "contract": contract,
        "algorithm_version": algorithm_version,
        "as_of": as_of.isoformat(),
        "input_sha256": input_sha256,
        "input_snapshot_path": input_snapshot_path,
        "artifact_sha256": artifact_sha256,
    }
    expected_structure_id = _canonical_json_sha256(binding)
    structure_id = _sha256_text(
        structure.get("structure_id"), "structure_evidence.structure_id"
    )
    if structure_id != expected_structure_id:
        raise FuturesMainReportError("structure_id必须由输入哈希、算法版本、as_of、方向与点位共同派生")
    return {
        "status": status,
        "structure_id": structure_id,
        "algorithm_version": algorithm_version,
        "input_sha256": input_sha256,
        "input_snapshot_path": input_snapshot_path,
        "artifact_sha256": artifact_sha256,
        "as_of": as_of,
        "levels": {
            "support": (support_lower, support_upper),
            "resistance": (resistance_lower, resistance_upper),
            "trend": trend_price,
        },
        "direction": direction,
        "tick_size_exact": tick_size_exact,
        "tick_size_actionability": expected_tick_actionability,
        "tick_size_evidence": tick_size_evidence,
        "provenance_status": expected_provenance_status,
        "daily_data_evidence_status": daily_data_evidence_status,
    }


def _validate_affected_months_v3(
    value: Any,
    analysis_contract: str,
    label: str,
) -> None:
    contract_match = re.search(r"(\d{3,4})$", analysis_contract)
    assert contract_match is not None
    contract_month = contract_match.group(1)
    if not isinstance(value, list) or not value:
        raise FuturesMainReportError(f"{label}必须说明受影响合约月份")
    months = [str(item).strip() for item in value]
    if (
        len(months) != len(set(months))
        or contract_month not in months
        or any(re.fullmatch(r"\d{3,4}", month) is None for month in months)
        or any(not 1 <= int(month[-2:]) <= 12 for month in months)
    ):
        raise FuturesMainReportError(f"{label}必须包含analysis_contract的合法月份代码")


def _validate_fundamentals_v3(
    report: Mapping[str, Any],
    observed_at: datetime,
    analysis_contract: str,
) -> str:
    fundamentals = report.get("fundamentals")
    if not isinstance(fundamentals, Mapping):
        raise FuturesMainReportError("v3 fundamentals必须是带状态的对象")
    status = fundamentals.get("status")
    if status not in {"available", "partial", "unobservable"}:
        raise FuturesMainReportError("fundamentals.status不受支持")
    items = fundamentals.get("items")
    gaps = fundamentals.get("gaps")
    if not isinstance(items, list) or not isinstance(gaps, list) or any(
        not str(gap).strip() for gap in gaps
    ):
        raise FuturesMainReportError("fundamentals.items/gaps格式错误")
    if status == "available" and (not items or gaps):
        raise FuturesMainReportError("基本面available必须有证据条目且无覆盖缺口")
    if status == "partial" and (not items or not gaps):
        raise FuturesMainReportError("基本面partial必须同时有已观测条目和缺口")
    if status == "unobservable" and (items or not gaps):
        raise FuturesMainReportError("基本面unobservable不得填造条目且必须披露缺口")
    if status in {"partial", "unobservable"}:
        _validate_source_attempts_v3(
            fundamentals.get("source_attempts"),
            observed_at,
            "fundamentals.source_attempts",
        )
    for index, item in enumerate(items):
        label = f"fundamentals.items[{index}]"
        if not isinstance(item, Mapping):
            raise FuturesMainReportError(f"{label}格式错误")
        _non_empty_text(item.get("topic"), f"{label}.topic")
        if item.get("direction") not in {
            "bullish",
            "bearish",
            "mixed",
            "neutral",
            "unobservable",
        }:
            raise FuturesMainReportError(f"{label}.direction不受支持")
        as_of = _evidence_time(
            item.get("as_of"), item.get("as_of_precision"), f"{label}.as_of"
        )
        _not_after_observation(as_of, observed_at, f"{label}.as_of")
        _validate_evidence_source(item.get("source"), observed_at, f"{label}.source")
        _non_empty_text(item.get("transmission"), f"{label}.transmission")
        _non_empty_text(item.get("counterevidence"), f"{label}.counterevidence")
        _validate_affected_months_v3(
            item.get("affected_contract_months"), analysis_contract, label
        )
    return str(status)


def _validate_news_search_v3(
    value: Any,
    observed_at: datetime,
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise FuturesMainReportError(f"{label}必须是完整检索判别结构")
    started_at = _timestamp(value.get("started_at"), f"{label}.started_at")
    completed_at = _timestamp(value.get("completed_at"), f"{label}.completed_at")
    if not started_at <= completed_at <= observed_at:
        raise FuturesMainReportError(f"{label}检索时间顺序无效")
    query_terms = value.get("query_terms")
    if (
        not isinstance(query_terms, list)
        or not query_terms
        or len(query_terms) != len(set(str(item).strip() for item in query_terms))
        or any(not str(item).strip() for item in query_terms)
    ):
        raise FuturesMainReportError(f"{label}.query_terms必须是非空唯一查询词")
    _non_empty_text(value.get("materiality_criteria"), f"{label}.materiality_criteria")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise FuturesMainReportError(f"{label}.sources必须记录实际检索来源")
    for index, source in enumerate(sources):
        _validate_evidence_source(
            source, observed_at, f"{label}.sources[{index}]"
        )
    _non_empty_text(value.get("result_summary"), f"{label}.result_summary")


def _validate_news_v3(
    report: Mapping[str, Any],
    observed_at: datetime,
    analysis_contract: str,
) -> str:
    news = report.get("important_news")
    if not isinstance(news, Mapping):
        raise FuturesMainReportError("v3 important_news必须使用判别结构")
    status = news.get("status")
    if status not in {
        "material_events_found",
        "search_completed_no_material_news",
        "unobservable",
    }:
        raise FuturesMainReportError("important_news.status不受支持")
    items = news.get("items")
    if not isinstance(items, list):
        raise FuturesMainReportError("important_news.items必须是列表")
    if status == "unobservable":
        if items or news.get("search") is not None:
            raise FuturesMainReportError("新闻不可观测时不得伪造事件或已完成检索")
        _non_empty_text(news.get("unobservable_reason"), "important_news.unobservable_reason")
        _validate_source_attempts_v3(
            news.get("source_attempts"), observed_at, "important_news.source_attempts"
        )
        return str(status)

    _validate_news_search_v3(news.get("search"), observed_at, "important_news.search")
    if status == "search_completed_no_material_news":
        if items:
            raise FuturesMainReportError("无重大新闻判别不得同时包含material事件")
        forbidden_placeholders = {
            "headline",
            "event_at",
            "published_at",
            "available_at",
            "impact",
            "transmission",
            "affected_contract_months",
        }
        if forbidden_placeholders & set(news):
            raise FuturesMainReportError("无重大新闻不得用伪事件字段填充栏目")
        return str(status)
    if not items:
        raise FuturesMainReportError("material_events_found必须至少包含一个真实事件")
    for index, item in enumerate(items):
        label = f"important_news.items[{index}]"
        if not isinstance(item, Mapping):
            raise FuturesMainReportError(f"{label}格式错误")
        _non_empty_text(item.get("headline"), f"{label}.headline")
        if item.get("direction") not in {
            "bullish",
            "bearish",
            "mixed",
            "neutral",
        }:
            raise FuturesMainReportError(f"{label}.direction不受支持")
        times: dict[str, datetime | date] = {}
        for field in ("event_at", "published_at", "available_at"):
            times[field] = _evidence_time(
                item.get(field), item.get(f"{field}_precision"), f"{label}.{field}"
            )
            _not_after_observation(times[field], observed_at, f"{label}.{field}")
        published_day = (
            times["published_at"].date()
            if isinstance(times["published_at"], datetime)
            else times["published_at"]
        )
        available_day = (
            times["available_at"].date()
            if isinstance(times["available_at"], datetime)
            else times["available_at"]
        )
        if published_day > available_day or (
            isinstance(times["published_at"], datetime)
            and isinstance(times["available_at"], datetime)
            and times["published_at"] > times["available_at"]
        ):
            raise FuturesMainReportError("新闻可得时间不得早于发布时间")
        _validate_evidence_source(item.get("source"), observed_at, f"{label}.source")
        _non_empty_text(item.get("impact"), f"{label}.impact")
        _non_empty_text(item.get("transmission"), f"{label}.transmission")
        _non_empty_text(item.get("counterevidence"), f"{label}.counterevidence")
        _validate_affected_months_v3(
            item.get("affected_contract_months"), analysis_contract, label
        )
    return str(status)


def _validate_member_sources_v3(
    value: Any,
    observed_at: datetime,
    exchange: str,
    label: str,
) -> None:
    if not isinstance(value, list) or not value:
        raise FuturesMainReportError(f"{label}必须包含会员榜官方来源")
    for index, source in enumerate(value):
        _validate_evidence_source(
            source,
            observed_at,
            f"{label}[{index}]",
            official_exchange=exchange,
        )


def _validate_member_daily_v3(
    values: Any,
    sessions: list[str],
    session_total_open_interest: Mapping[str, int],
    previous_session_total_open_interest: Mapping[str, int],
    label: str,
    *,
    metrics: list[str],
    enforce_adjacent_position_change: bool,
    is_top20: bool,
    prior_positions: Mapping[str, Any] | None,
) -> None:
    if not isinstance(values, list) or len(values) != len(sessions):
        raise FuturesMainReportError(f"{label}必须逐日覆盖会员资金窗口")
    expected_fields = {"date", *metrics, "normalization", "flow_regime"}
    if is_top20:
        expected_fields.add("coverage")
    previous_row: Mapping[str, Any] | None = None
    for index, row in enumerate(values):
        row_label = f"{label}[{index}]"
        if not isinstance(row, Mapping) or set(row) != expected_fields:
            raise FuturesMainReportError(f"{row_label}只能包含date与声明的观测指标")
        row_date = _non_empty_text(row.get("date"), f"{row_label}.date")
        if row_date != sessions[index]:
            raise FuturesMainReportError(f"{label}必须与三个交易日逐日对齐")
        parsed: dict[str, int] = {
            metric: _exact_integer(row.get(metric), f"{row_label}.{metric}")
            for metric in metrics
        }
        for position_field in ("long_open_interest", "short_open_interest"):
            if position_field in parsed:
                if parsed[position_field] <= 0:
                    raise FuturesMainReportError(
                        f"{row_label}.{position_field}必须为正；榜外或缺侧必须记不可观测"
                    )
                if parsed[position_field] > session_total_open_interest[row_date]:
                    raise FuturesMainReportError(
                        f"{row_label}.{position_field}不得超过该日合约总持仓"
                    )
        if {
            "long_open_interest",
            "short_open_interest",
            "member_position_imbalance",
        }.issubset(parsed):
            if parsed["member_position_imbalance"] != (
                parsed["long_open_interest"] - parsed["short_open_interest"]
            ):
                raise FuturesMainReportError(f"{row_label}会员持仓不平衡算术错误")
        if {"long_change", "short_change", "imbalance_change"}.issubset(parsed):
            if parsed["imbalance_change"] != parsed["long_change"] - parsed["short_change"]:
                raise FuturesMainReportError(f"{row_label}会员不平衡变化算术错误")
        normalization = row.get("normalization")
        if not isinstance(normalization, Mapping):
            raise FuturesMainReportError(f"{row_label}.normalization格式错误")
        previous_total_oi = _exact_integer(
            normalization.get("previous_total_open_interest"),
            f"{row_label}.normalization.previous_total_open_interest",
            minimum=1,
        )
        if previous_total_oi != previous_session_total_open_interest[row_date]:
            raise FuturesMainReportError(
                f"{row_label}归一化分母必须等于同合约上一完成日总持仓"
            )
        expected_normalized_keys = {"previous_total_open_interest"}
        for change_field, ratio_field in (
            ("long_change", "long_change_to_previous_oi"),
            ("short_change", "short_change_to_previous_oi"),
            ("imbalance_change", "imbalance_change_to_previous_oi"),
        ):
            if change_field in parsed:
                expected_normalized_keys.add(ratio_field)
                reported = _decimal(
                    normalization.get(ratio_field),
                    f"{row_label}.normalization.{ratio_field}",
                )
                expected_ratio = Decimal(parsed[change_field]) / Decimal(previous_total_oi)
                if abs(reported - expected_ratio) > Decimal("0.000000001"):
                    raise FuturesMainReportError(
                        f"{row_label}.normalization.{ratio_field}算术错误"
                    )
        if set(normalization) != expected_normalized_keys:
            raise FuturesMainReportError(f"{row_label}.normalization字段必须与可观测变化项一致")
        if {"long_change", "short_change"}.issubset(parsed):
            long_change = parsed["long_change"]
            short_change = parsed["short_change"]
            expected_regime = (
                "long_build_short_cover"
                if long_change > 0 and short_change < 0
                else "long_exit_short_build"
                if long_change < 0 and short_change > 0
                else "both_build"
                if long_change > 0 and short_change > 0
                else "deleveraging"
                if long_change < 0 and short_change < 0
                else "mixed_or_flat"
            )
        else:
            expected_regime = "partial"
        if row.get("flow_regime") != expected_regime:
            raise FuturesMainReportError(
                f"{row_label}.flow_regime必须由ΔL与ΔS推导；双减只能标deleveraging"
            )
        if is_top20:
            coverage = row.get("coverage")
            if not isinstance(coverage, Mapping):
                raise FuturesMainReportError(f"{row_label}.coverage格式错误")
            expected_coverage_keys: set[str] = set()
            for position_field, coverage_field in (
                ("long_open_interest", "long_to_total_oi"),
                ("short_open_interest", "short_to_total_oi"),
            ):
                if position_field in parsed:
                    expected_coverage_keys.add(coverage_field)
                    reported = _decimal(
                        coverage.get(coverage_field),
                        f"{row_label}.coverage.{coverage_field}",
                    )
                    expected_coverage = Decimal(parsed[position_field]) / Decimal(
                        session_total_open_interest[row_date]
                    )
                    if not Decimal("0") < reported <= Decimal("1") or abs(
                        reported - expected_coverage
                    ) > Decimal("0.000000001"):
                        raise FuturesMainReportError(
                            f"{row_label}.coverage.{coverage_field}算术或边界错误"
                        )
            if set(coverage) != expected_coverage_keys:
                raise FuturesMainReportError(f"{row_label}.coverage字段与可观测持仓侧不一致")
        if enforce_adjacent_position_change:
            comparison_row = previous_row if previous_row is not None else prior_positions
            if comparison_row is None:
                raise FuturesMainReportError(f"{label}首日变化缺少前一完成日席位基线")
            for position_field, change_field in (
                ("long_open_interest", "long_change"),
                ("short_open_interest", "short_change"),
            ):
                if {position_field, change_field}.issubset(parsed):
                    previous_position = _exact_integer(
                        comparison_row.get(position_field),
                        f"{label}前一完成日.{position_field}",
                    )
                    if parsed[position_field] - previous_position != parsed[change_field]:
                        raise FuturesMainReportError(
                            f"{row_label}.{change_field}必须等于相邻日持仓差"
                        )
        previous_row = row


def _validate_partial_metrics_v3(value: Mapping[str, Any], label: str) -> list[str]:
    required = {
        "long_open_interest",
        "long_change",
        "short_open_interest",
        "short_change",
        "member_position_imbalance",
        "imbalance_change",
    }
    observed = value.get("observed_metrics")
    unobserved = value.get("unobserved_metrics")
    if (
        not isinstance(observed, list)
        or not observed
        or len(observed) != len(set(observed))
        or not set(observed).issubset(required)
    ):
        raise FuturesMainReportError(f"{label}.observed_metrics无效")
    if (
        not isinstance(unobserved, list)
        or len(unobserved) != len(set(unobserved))
        or set(unobserved) != required - set(observed)
    ):
        raise FuturesMainReportError(f"{label}.unobserved_metrics必须精确披露缺失指标")
    if not any(
        pair.issubset(set(observed))
        for pair in (
            {"long_open_interest", "long_change"},
            {"short_open_interest", "short_change"},
        )
    ):
        raise FuturesMainReportError(f"{label}部分观测至少包含一侧持仓及变化")
    if "member_position_imbalance" in observed and not {
        "long_open_interest",
        "short_open_interest",
    }.issubset(observed):
        raise FuturesMainReportError(f"{label}缺少多空两侧时不得声称持仓不平衡")
    if "imbalance_change" in observed and not {
        "long_change",
        "short_change",
    }.issubset(observed):
        raise FuturesMainReportError(f"{label}缺少多空变化时不得声称不平衡变化")
    return [str(metric) for metric in observed]


def _validate_member_entity_v3(
    value: Mapping[str, Any],
    sessions: list[str],
    totals: Mapping[str, int],
    previous_totals: Mapping[str, int],
    observed_at: datetime,
    exchange: str,
    label: str,
    prior_session_date: str | None,
) -> str:
    status = value.get("status")
    if status not in {"available", "partial", "unobservable"}:
        raise FuturesMainReportError(f"{label}.status不受支持")
    _non_empty_text(value.get("conclusion"), f"{label}.conclusion")
    if status == "available":
        _validate_member_sources_v3(
            value.get("sources"), observed_at, exchange, f"{label}.sources"
        )
        metrics = [
            "long_open_interest",
            "long_change",
            "short_open_interest",
            "short_change",
            "member_position_imbalance",
            "imbalance_change",
        ]
        observed_metrics = metrics
    elif status == "partial":
        _validate_member_sources_v3(
            value.get("sources"), observed_at, exchange, f"{label}.sources"
        )
        _validate_source_attempts_v3(
            value.get("source_attempts"),
            observed_at,
            f"{label}.source_attempts",
            official_exchange=exchange,
        )
        observed_metrics = _validate_partial_metrics_v3(value, label)
    else:
        observed_metrics = []

    is_top20 = label.endswith("top20")
    prior_positions: Mapping[str, Any] | None = None
    if status in {"available", "partial"} and is_top20:
        if (
            _exact_integer(value.get("top_n"), f"{label}.top_n", minimum=1) != 20
            or value.get("composition_basis")
            != "exchange_published_top20_components"
            or value.get("change_basis")
            != "exchange_published_change_because_membership_can_change"
        ):
            raise FuturesMainReportError(
                f"{label}必须明示Top20组成与交易所披露change口径"
            )
        if value.get("prior_positions") is not None:
            raise FuturesMainReportError("Top20成分可换榜，不得用聚合持仓差伪造前日change")
    elif status in {"available", "partial"}:
        prior = value.get("prior_positions")
        if not isinstance(prior, Mapping) or prior_session_date is None:
            raise FuturesMainReportError(f"{label}必须绑定首日变化的前一完成日席位")
        if prior.get("date") != prior_session_date:
            raise FuturesMainReportError(f"{label}.prior_positions.date与会员窗口基线不一致")
        _validate_evidence_source(
            prior.get("source"),
            observed_at,
            f"{label}.prior_positions.source",
            official_exchange=exchange,
        )
        required_prior_fields = {
            position_field
            for position_field in ("long_open_interest", "short_open_interest")
            if position_field in observed_metrics
        }
        if not required_prior_fields:
            raise FuturesMainReportError(f"{label}.prior_positions缺少可观测席位侧")
        expected_prior_keys = {"date", "source", *required_prior_fields}
        if set(prior) != expected_prior_keys:
            raise FuturesMainReportError(f"{label}.prior_positions字段必须与可观测持仓侧一致")
        prior_total = previous_totals[sessions[0]]
        for position_field in required_prior_fields:
            position = _exact_integer(
                prior.get(position_field),
                f"{label}.prior_positions.{position_field}",
                minimum=1,
            )
            if position > prior_total:
                raise FuturesMainReportError(f"{label}.prior_positions持仓不得超过基线合约总OI")
        prior_positions = prior

    if status == "available":
        _validate_member_daily_v3(
            value.get("daily"),
            sessions,
            totals,
            previous_totals,
            f"{label}.daily",
            metrics=metrics,
            enforce_adjacent_position_change=not is_top20,
            is_top20=is_top20,
            prior_positions=prior_positions,
        )
    elif status == "partial":
        metrics = observed_metrics
        _validate_member_daily_v3(
            value.get("daily"),
            sessions,
            totals,
            previous_totals,
            f"{label}.daily",
            metrics=metrics,
            enforce_adjacent_position_change=not is_top20,
            is_top20=is_top20,
            prior_positions=prior_positions,
        )
    else:
        if value.get("daily") not in (None, []):
            raise FuturesMainReportError(f"{label}不可观测时不得补零或填造逐日数据")
        _validate_source_attempts_v3(
            value.get("source_attempts"),
            observed_at,
            f"{label}.source_attempts",
            official_exchange=exchange,
        )
    return str(status)


def _validate_member_flow_v3(
    report: Mapping[str, Any],
    observed_at: datetime,
    analysis_contract: str,
    exchange: str,
    tick_size: Decimal,
    latest_completed_date: date | None,
    closed_dates: set[date],
    latest_bar: Mapping[str, Any] | None,
) -> str:
    member_flow = report.get("member_flow")
    if not isinstance(member_flow, Mapping):
        raise FuturesMainReportError("v3报告必须包含带状态的member_flow")
    status = member_flow.get("status")
    if status not in {"available", "partial", "unobservable"}:
        raise FuturesMainReportError("member_flow.status不受支持")
    sessions_value = member_flow.get("sessions")
    if not isinstance(sessions_value, list):
        raise FuturesMainReportError("member_flow.sessions必须是列表")
    if status == "unobservable" and latest_completed_date is None:
        if sessions_value:
            raise FuturesMainReportError("行情也不可观测时不得伪造会员交易日窗口")
        sessions: list[str] = []
    else:
        if len(sessions_value) != 3 or len(set(sessions_value)) != 3:
            raise FuturesMainReportError("会员资金必须覆盖三个不同完成交易日")
        parsed_sessions = [
            _date_value(session, f"member_flow.sessions[{index}]")
            for index, session in enumerate(sessions_value)
        ]
        if parsed_sessions != sorted(parsed_sessions):
            raise FuturesMainReportError("会员资金交易日必须升序")
        if latest_completed_date is not None and parsed_sessions[-1] != latest_completed_date:
            raise FuturesMainReportError("会员资金最后一日必须等于行情最近完成日")
        expected: list[date] = []
        cursor = parsed_sessions[-1]
        while len(expected) < 3:
            if cursor.weekday() < 5 and cursor not in closed_dates:
                expected.append(cursor)
            cursor -= timedelta(days=1)
        expected.reverse()
        if parsed_sessions != expected:
            raise FuturesMainReportError("会员资金必须是最近三个连续完成交易日")
        sessions = [str(session) for session in sessions_value]

    contexts = member_flow.get("session_context")
    totals: dict[str, int] = {}
    previous_totals: dict[str, int] = {}
    prior_session_date: str | None = None
    if status == "unobservable":
        if contexts not in (None, []):
            raise FuturesMainReportError("会员榜不可观测时session_context必须为空")
        if member_flow.get("prior_session_context") is not None:
            raise FuturesMainReportError("会员榜不可观测时不得伪造prior_session_context")
        totals = {session: 0 for session in sessions}
        previous_totals = {session: 0 for session in sessions}
    else:
        if not isinstance(contexts, list) or len(contexts) != len(sessions):
            raise FuturesMainReportError("可观测会员资金必须有逐日价量仓背景")
        prior_context = member_flow.get("prior_session_context")
        if not isinstance(prior_context, Mapping):
            raise FuturesMainReportError("可观测会员资金必须绑定窗口前一完成日基线")
        if str(prior_context.get("contract", "")).upper() != analysis_contract:
            raise FuturesMainReportError("prior_session_context必须是同analysis_contract")
        cursor = _date_value(sessions[0], "member_flow.sessions[0]") - timedelta(days=1)
        while cursor.weekday() >= 5 or cursor in closed_dates:
            cursor -= timedelta(days=1)
        prior_session_date = cursor.isoformat()
        if prior_context.get("date") != prior_session_date:
            raise FuturesMainReportError("prior_session_context.date必须是三日窗口前一完成日")
        prior_close = _decimal(prior_context.get("close"), "prior_session_context.close")
        prior_total_oi = _exact_integer(
            prior_context.get("total_open_interest"),
            "prior_session_context.total_open_interest",
            minimum=1,
        )
        if prior_close <= 0 or not _tick_aligned(prior_close, tick_size):
            raise FuturesMainReportError("prior_session_context.close必须为正且tick对齐")
        _validate_evidence_source(
            prior_context.get("source"),
            observed_at,
            "member_flow.prior_session_context.source",
            official_exchange=exchange,
        )
        previous: Mapping[str, Any] | None = {
            "close": prior_close,
            "total_open_interest": prior_total_oi,
        }
        for index, context in enumerate(contexts):
            label = f"member_flow.session_context[{index}]"
            if not isinstance(context, Mapping):
                raise FuturesMainReportError(f"{label}格式错误")
            if str(context.get("contract", "")).upper() != analysis_contract:
                raise FuturesMainReportError("会员资金不得跨合约直接拼接")
            session = _non_empty_text(context.get("date"), f"{label}.date")
            if session != sessions[index]:
                raise FuturesMainReportError("会员价量仓背景必须逐日对齐")
            close = _decimal(context.get("close"), f"{label}.close")
            close_change = _decimal(context.get("close_change"), f"{label}.close_change")
            if close <= 0 or not _tick_aligned(close, tick_size) or not _tick_aligned(
                close_change, tick_size
            ):
                raise FuturesMainReportError("会员背景价格必须按tick对齐")
            volume = _exact_integer(context.get("volume"), f"{label}.volume", minimum=0)
            total_oi = _exact_integer(
                context.get("total_open_interest"), f"{label}.total_open_interest", minimum=1
            )
            total_oi_change = _exact_integer(
                context.get("total_open_interest_change"), f"{label}.total_open_interest_change"
            )
            expected_price_oi_regime = (
                "price_up_oi_up"
                if close_change > 0 and total_oi_change > 0
                else "price_up_oi_down"
                if close_change > 0 and total_oi_change < 0
                else "price_down_oi_up"
                if close_change < 0 and total_oi_change > 0
                else "price_down_oi_down"
                if close_change < 0 and total_oi_change < 0
                else "mixed_or_flat"
            )
            if context.get("price_open_interest_regime") != expected_price_oi_regime:
                raise FuturesMainReportError(
                    f"{label}.price_open_interest_regime必须由价格与总持仓变化推导"
                )
            _validate_evidence_source(
                context.get("source"),
                observed_at,
                f"{label}.source",
                official_exchange=exchange,
            )
            assert previous is not None
            previous_close = _decimal(previous.get("close"), "前一日close")
            previous_oi = _exact_integer(
                previous.get("total_open_interest"), "前一日total_open_interest"
            )
            if close - previous_close != close_change:
                raise FuturesMainReportError("close_change必须等于相邻完成日收盘差")
            if total_oi - previous_oi != total_oi_change:
                raise FuturesMainReportError("total_open_interest_change必须等于相邻日持仓差")
            totals[session] = total_oi
            previous_totals[session] = previous_oi
            previous = context
            _ = volume
        if latest_bar is not None and contexts:
            latest_context = contexts[-1]
            if (
                _decimal(latest_context.get("close"), "末日会员背景close")
                != latest_bar["close"]
                or _exact_integer(latest_context.get("volume"), "末日会员背景volume")
                != latest_bar["volume"]
                or _exact_integer(
                    latest_context.get("total_open_interest"),
                    "末日会员背景total_open_interest",
                )
                != latest_bar["open_interest"]
            ):
                raise FuturesMainReportError("会员资金末日价量仓必须等于最近完成K线")

    top20 = member_flow.get("top20")
    domestic = member_flow.get("domestic_members")
    foreign = member_flow.get("foreign_members")
    if not all(isinstance(group, Mapping) for group in (top20, domestic, foreign)):
        raise FuturesMainReportError("member_flow必须分别给出Top20、五内资和三外资")
    assert isinstance(top20, Mapping) and isinstance(domestic, Mapping) and isinstance(foreign, Mapping)
    top20_status = _validate_member_entity_v3(
        top20,
        sessions,
        totals,
        previous_totals,
        observed_at,
        exchange,
        "member_flow.top20",
        prior_session_date,
    )
    group_statuses: list[str] = [top20_status]
    for key, group, configured_names in (
        ("domestic_members", domestic, ["中信期货", "永安期货", "国泰君安", "银河期货", "东证期货"]),
        ("foreign_members", foreign, ["乾坤期货", "摩根大通", "瑞银期货"]),
    ):
        members = group.get("members")
        if not isinstance(members, list) or [
            member.get("name") if isinstance(member, Mapping) else None for member in members
        ] != configured_names:
            raise FuturesMainReportError(f"member_flow.{key}必须按配置覆盖全部重点席位")
        member_statuses: list[str] = []
        for index, member in enumerate(members):
            if not isinstance(member, Mapping):
                raise FuturesMainReportError(f"member_flow.{key}.members[{index}]格式错误")
            member_statuses.append(
                _validate_member_entity_v3(
                    member,
                    sessions,
                    totals,
                    previous_totals,
                    observed_at,
                    exchange,
                    f"member_flow.{key}.members[{index}]",
                    prior_session_date,
                )
            )
        derived_group_status = (
            "available"
            if all(item == "available" for item in member_statuses)
            else "unobservable"
            if all(item == "unobservable" for item in member_statuses)
            else "partial"
        )
        if group.get("status") != derived_group_status:
            raise FuturesMainReportError(f"member_flow.{key}.status必须由逐席位状态重算")
        _non_empty_text(group.get("conclusion"), f"member_flow.{key}.conclusion")
        group_statuses.append(derived_group_status)
    derived_status = (
        "available"
        if all(item == "available" for item in group_statuses)
        else "unobservable"
        if all(item == "unobservable" for item in group_statuses)
        else "partial"
    )
    if status != derived_status:
        raise FuturesMainReportError("member_flow.status必须由Top20、五内资和三外资状态重算")
    return str(status)


def _condition_satisfied(condition: str, close: Decimal, price: Decimal) -> bool:
    if condition == "completed_close_at_or_above":
        return close >= price
    if condition == "completed_close_at_or_below":
        return close <= price
    raise FuturesMainReportError(f"不受支持的完成收盘条件：{condition}")


def _next_completed_dates(
    start_exclusive: date,
    end_inclusive: date,
    closed_dates: set[date],
) -> list[date]:
    result: list[date] = []
    cursor = start_exclusive + timedelta(days=1)
    while cursor <= end_inclusive:
        if cursor.weekday() < 5 and cursor not in closed_dates:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def _validate_structure_ref_v3(
    value: Any,
    structure: Mapping[str, Any],
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise FuturesMainReportError(f"{label}必须绑定结构证据")
    expected = {
        "structure_id": structure["structure_id"],
        "algorithm_version": structure["algorithm_version"],
        "as_of": structure["as_of"].isoformat(),
        "input_sha256": structure["input_sha256"],
    }
    if dict(value) != expected:
        raise FuturesMainReportError(f"{label}必须精确绑定structure_id、算法、as_of与输入哈希")


def _validate_point_plan_v3(
    report: Mapping[str, Any],
    observed_at: datetime,
    action: str,
    analysis_contract: str,
    tick_size: Decimal,
    structure: Mapping[str, Any],
    market: Mapping[str, Any],
    *,
    verified_completed_daily_rows: list[Mapping[str, Any]] | None = None,
    require_verified_bar_binding: bool = False,
) -> Decimal | None:
    if "previously_confirmed_on" in report or (
        isinstance(report.get("point_plan"), Mapping)
        and "previously_confirmed_on" in report["point_plan"]
    ):
        raise FuturesMainReportError(
            "裸 previously_confirmed_on 已永久禁用；必须提供绑定结构的确认K线链"
        )
    plan = report.get("point_plan")
    if plan is None:
        if action == "wait":
            return None
        raise FuturesMainReportError("方向性动作必须有可核算的点位计划")
    if structure["status"] == "unobservable":
        raise FuturesMainReportError("结构不可观测时不得编造点位计划")
    if not isinstance(plan, Mapping):
        raise FuturesMainReportError("结构可观测时必须给出条件点位计划")
    direction = plan.get("direction")
    if direction not in {"long", "short"}:
        raise FuturesMainReportError("point_plan.direction必须是long或short")
    if action in {"long", "short"} and action != direction:
        raise FuturesMainReportError("方向性最终动作必须与点位计划方向一致")
    if plan.get("setup_mode") not in {
        "trend_continuation",
        "breakout_ignition",
        "trend_reversal",
    }:
        raise FuturesMainReportError("point_plan.setup_mode不受支持")
    order_timing = plan.get("order_timing")
    if order_timing not in {"now", "conditional"}:
        raise FuturesMainReportError("point_plan.order_timing必须是now或conditional")
    if action in {"long", "short"} and (
        structure.get("tick_size_exact") is not True
        or structure.get("tick_size_actionability") != "exact_order_compatible"
    ):
        raise FuturesMainReportError("资本方向动作必须绑定精确可委托tick；近似结构只能作研究观点")
    if action == "wait" and order_timing == "now":
        raise FuturesMainReportError("wait的观察计划不得声称order_timing=now")
    _validate_structure_ref_v3(plan.get("structure_ref"), structure, "point_plan.structure_ref")

    trigger = plan.get("trigger")
    invalidation = plan.get("invalidation")
    target = plan.get("target")
    entry_zone = plan.get("entry_zone")
    if not all(isinstance(value, Mapping) for value in (trigger, invalidation, target, entry_zone)):
        raise FuturesMainReportError("点位计划必须结构化给出触发、入场、失效和目标")
    assert isinstance(trigger, Mapping) and isinstance(invalidation, Mapping)
    assert isinstance(target, Mapping) and isinstance(entry_zone, Mapping)
    trigger_condition = trigger.get("condition")
    invalidation_condition = invalidation.get("condition")
    expected_trigger = (
        "completed_close_at_or_above" if direction == "long" else "completed_close_at_or_below"
    )
    expected_invalidation = (
        "completed_close_at_or_below" if direction == "long" else "completed_close_at_or_above"
    )
    if trigger_condition != expected_trigger or invalidation_condition != expected_invalidation:
        raise FuturesMainReportError("触发与失效条件必须和点位方向一致")
    trigger_price = _decimal(trigger.get("price"), "point_plan.trigger.price")
    invalidation_price = _decimal(
        invalidation.get("price"), "point_plan.invalidation.price"
    )
    target_price = _decimal(target.get("price"), "point_plan.target.price")
    entry_lower, entry_upper = _validate_zone(entry_zone, "point_plan.entry_zone")
    all_prices = [
        trigger_price,
        invalidation_price,
        target_price,
        entry_lower,
        entry_upper,
    ]
    if any(price <= 0 or not _tick_aligned(price, tick_size) for price in all_prices):
        raise FuturesMainReportError("计划点位必须为正并按tick对齐")
    _validate_display_numbers(trigger.get("text"), [trigger_price], "触发条件文本")
    _validate_display_numbers(
        entry_zone.get("text"), [entry_lower, entry_upper], "入场区间文本"
    )
    _validate_display_numbers(
        invalidation.get("text"), [invalidation_price], "失效条件文本"
    )
    _validate_display_numbers(target.get("text"), [target_price], "目标位文本")
    _non_empty_text(invalidation.get("thesis"), "point_plan.invalidation.thesis")
    target_evidence = target.get("evidence")
    if not isinstance(target_evidence, Mapping):
        raise FuturesMainReportError("目标位必须有结构化证据依据")
    _non_empty_text(target_evidence.get("basis"), "point_plan.target.evidence.basis")
    _non_empty_text(
        target_evidence.get("description"), "point_plan.target.evidence.description"
    )
    target_refs = target_evidence.get("evidence_refs")
    if not isinstance(target_refs, list) or not target_refs or any(
        not isinstance(ref, str)
        or not ref.startswith(("structure_evidence.", "fundamentals.", "important_news.", "member_flow."))
        for ref in target_refs
    ):
        raise FuturesMainReportError("目标位必须引用本报告内的可追溯证据路径")
    for index, ref in enumerate(target_refs):
        _resolve_evidence_ref(report, ref, f"point_plan.target.evidence_refs[{index}]")
    _non_empty_text(
        plan.get("valid_until_or_recalculate_condition"), "点位计划有效期或重算条件"
    )

    if direction == "long":
        entry_reference = entry_upper
        risk = entry_reference - invalidation_price
        reward = target_price - entry_reference
    else:
        entry_reference = entry_lower
        risk = invalidation_price - entry_reference
        reward = entry_reference - target_price
    if risk <= 0 or reward <= 0:
        raise FuturesMainReportError("失效位和目标位必须位于入场区间相应的不利/有利方向")
    if (direction == "long" and trigger_price <= invalidation_price) or (
        direction == "short" and trigger_price >= invalidation_price
    ):
        raise FuturesMainReportError("触发价与失效位必须在方向上严格分离，不能同时成立")
    computed_ratio = reward / risk
    reported_ratio = _decimal(
        plan.get("reference_reward_risk"), "point_plan.reference_reward_risk"
    )
    if reported_ratio <= 0 or abs(reported_ratio - computed_ratio) > Decimal("0.000001"):
        raise FuturesMainReportError("reference_reward_risk只能如实等于点位算术结果")

    confirmation_status = plan.get("confirmation_status")
    if confirmation_status not in {"confirmed", "pending", "invalid"}:
        raise FuturesMainReportError("point_plan.confirmation_status不受支持")
    historical = plan.get("historical_confirmation")
    latest_bar = market.get("latest_bar")
    closed_dates = market.get("closed_dates", set())
    if verified_completed_daily_rows:
        if latest_bar is None:
            raise FuturesMainReportError(
                "已验final raw含完成K线时market_data.latest_completed_bar不得缺失"
            )
        normalized_latest = {"contract": analysis_contract, **dict(latest_bar)}
        if not _completed_bar_facts_equal(
            normalized_latest, verified_completed_daily_rows[-1]
        ):
            raise FuturesMainReportError(
                "market_data.latest_completed_bar必须逐字段等于已验final raw完成K线最后一根"
            )
    elif require_verified_bar_binding:
        raise FuturesMainReportError(
            "生产CLI的点位确认必须绑定已验final raw completed_daily_rows"
        )
    if historical is None:
        if latest_bar is None:
            raise FuturesMainReportError("没有最新完成K线时必须提供完整历史确认K线链")
        close = latest_bar["close"]
        invalid_now = _condition_satisfied(
            str(invalidation_condition), close, invalidation_price
        )
        confirmed_now = _condition_satisfied(str(trigger_condition), close, trigger_price)
        derived_confirmation = (
            "invalid" if invalid_now else "confirmed" if confirmed_now else "pending"
        )
        if confirmed_now and structure["as_of"] >= latest_bar["date"]:
            raise FuturesMainReportError("确认K线必须晚于被冻结结构的as_of，避免同K线回看")
    else:
        if not isinstance(historical, Mapping):
            raise FuturesMainReportError("historical_confirmation格式错误")
        if not verified_completed_daily_rows:
            raise FuturesMainReportError(
                "historical_confirmation必须绑定已验final raw completed_daily_rows"
            )
        _validate_structure_ref_v3(
            historical.get("structure_ref"),
            structure,
            "historical_confirmation.structure_ref",
        )
        confirming_raw = historical.get("confirming_bar")
        confirming = _validate_bar_v3(
            confirming_raw,
            observed_at,
            analysis_contract,
            tick_size,
            "historical_confirmation.confirming_bar",
        )
        if confirming["date"] <= structure["as_of"]:
            raise FuturesMainReportError("历史确认K线必须晚于被冻结结构as_of")
        if not _condition_satisfied(
            str(trigger_condition), confirming["close"], trigger_price
        ):
            raise FuturesMainReportError("历史确认K线收盘没有真正满足冻结触发条件")
        if _condition_satisfied(
            str(invalidation_condition), confirming["close"], invalidation_price
        ):
            raise FuturesMainReportError("历史确认K线不得在确认同时满足失效条件")
        subsequent_raw = historical.get("subsequent_bars")
        if not isinstance(subsequent_raw, list) or not subsequent_raw:
            raise FuturesMainReportError("历史确认必须包含确认后至最新完成日的全部K线")
        subsequent = [
            _validate_bar_v3(
                bar,
                observed_at,
                analysis_contract,
                tick_size,
                f"historical_confirmation.subsequent_bars[{index}]",
            )
            for index, bar in enumerate(subsequent_raw)
        ]
        dates = [bar["date"] for bar in subsequent]
        if dates != sorted(dates) or len(dates) != len(set(dates)):
            raise FuturesMainReportError("历史确认后的K线必须按日期升序且不重复")
        final_date = latest_bar["date"] if latest_bar is not None else dates[-1]
        expected_dates = _next_completed_dates(
            confirming["date"], final_date, set(closed_dates)
        )
        if dates != expected_dates:
            raise FuturesMainReportError("历史确认后的K线链必须连续覆盖到最近完成交易日")
        if latest_bar is not None:
            last = subsequent[-1]
            if any(
                last[field] != latest_bar[field]
                for field in ("date", "open", "high", "low", "close", "volume", "open_interest")
            ):
                raise FuturesMainReportError("历史确认K线链末端必须等于报告最近完成K线")
        raw_index_by_date = {
            row["date"]: index
            for index, row in enumerate(verified_completed_daily_rows)
        }
        reported_chain = [confirming, *subsequent]
        raw_chain: list[Mapping[str, Any]] = []
        raw_positions: list[int] = []
        for index, reported_bar in enumerate(reported_chain):
            raw_index = raw_index_by_date.get(reported_bar["date"])
            if raw_index is None:
                raise FuturesMainReportError(
                    "历史确认K线日期必须存在于已验final raw completed_daily_rows"
                )
            raw_bar = verified_completed_daily_rows[raw_index]
            normalized_reported_bar = {
                "contract": analysis_contract,
                **dict(reported_bar),
            }
            if not _completed_bar_facts_equal(normalized_reported_bar, raw_bar):
                raise FuturesMainReportError(
                    f"historical_confirmation K线[{index}]必须逐字段等于已验final raw"
                )
            raw_chain.append(raw_bar)
            raw_positions.append(raw_index)
        if raw_positions != list(
            range(raw_positions[0], raw_positions[0] + len(raw_positions))
        ):
            raise FuturesMainReportError(
                "历史确认链必须是final raw completed_daily_rows中的连续子序列"
            )
        expected_chain_hash = _canonical_json_sha256(
            {
                "confirming_bar": _completed_bar_hash_payload(raw_chain[0]),
                "subsequent_bars": [
                    _completed_bar_hash_payload(bar) for bar in raw_chain[1:]
                ],
            }
        )
        if _sha256_text(
            historical.get("bar_chain_sha256"), "historical_confirmation.bar_chain_sha256"
        ) != expected_chain_hash:
            raise FuturesMainReportError("历史确认bar_chain_sha256与完整K线证据不一致")
        invalid_after = any(
            _condition_satisfied(
                str(invalidation_condition), bar["close"], invalidation_price
            )
            for bar in subsequent
        )
        derived_confirmation = "invalid" if invalid_after else "confirmed"
    if confirmation_status != derived_confirmation:
        raise FuturesMainReportError("confirmation_status必须由绑定结构的完成K线证据推导")
    if action in {"long", "short"}:
        if confirmation_status == "invalid":
            raise FuturesMainReportError("冻结计划已失效时必须重建结构与计划，不能沿用旧计划")
        if confirmation_status == "pending":
            _non_empty_text(
                plan.get("timing_override_rationale"),
                "pending方向动作的timing_override_rationale",
            )
        if order_timing == "now":
            if market.get("status") != "fresh":
                raise FuturesMainReportError("order_timing=now只能基于事实上fresh的当前行情")
            if structure.get("tick_size_exact") is not True or structure.get(
                "tick_size_actionability"
            ) != "exact_order_compatible":
                raise FuturesMainReportError("order_timing=now必须使用精确且可委托的tick证据")
            quote_price = market.get("quote_price")
            if quote_price is None or not entry_lower <= quote_price <= entry_upper:
                raise FuturesMainReportError("order_timing=now时可观测报价必须位于有限入场区间")
    return computed_ratio


def _validate_agent_judgment_v3(
    report: Mapping[str, Any],
    action: str,
    *,
    observed_at: datetime,
    structure_status: str,
    fundamentals_status: str,
    news_status: str,
    member_status: str,
    market_status: str,
    main_chain_status: str,
    main_snapshot_status: str,
    daily_data_evidence_status: str | None,
    scan_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    judgment = report.get("agent_judgment")
    if not isinstance(judgment, Mapping):
        raise FuturesMainReportError("v3报告必须包含agent_judgment")
    required_judgment_keys = {
        "decision_method",
        "objective",
        "majority_vote_used",
        "rules_auto_authorized_action",
        "profit_guaranteed",
        "directional_view",
        "domains",
        "synthesis_thesis",
        "strongest_case_for",
        "strongest_case_against",
        "why_now",
        "why_not_wait",
        "why_not_opposite",
        "alternative",
        "invalidation",
        "expected_net_value",
        "expected_net_value_reasoning",
        "capital_allocation_verdict",
        "coverage_uncertainty",
        "costs_and_slippage_considered",
        "long_term_validation_plan",
        "competing_hypotheses",
        "final_action",
    }
    allowed_judgment_keys = required_judgment_keys | {
        "wait_for",
        "reassess_by",
        "update_triggers",
        "view_action_divergence",
    }
    if action == "wait":
        required_judgment_keys |= {
            "wait_for",
            "reassess_by",
            "update_triggers",
        }
    judgment_keys = set(judgment)
    missing_judgment_keys = required_judgment_keys - judgment_keys
    unexpected_judgment_keys = judgment_keys - allowed_judgment_keys
    if missing_judgment_keys:
        raise FuturesMainReportError(
            "agent_judgment缺少必填字段："
            + ", ".join(sorted(missing_judgment_keys))
        )
    if unexpected_judgment_keys:
        raise FuturesMainReportError(
            "agent_judgment不得夹带投票、分数或门禁结果："
            + ", ".join(sorted(unexpected_judgment_keys))
        )
    if judgment.get("decision_method") != "agent_discretionary_synthesis":
        raise FuturesMainReportError("最终动作必须由Agent三面综合判断")
    if judgment.get("objective") != "long_term_verifiable_net_profit_after_costs":
        raise FuturesMainReportError("Agent判断必须以长期可验证成本后净收益为目标")
    if judgment.get("majority_vote_used") is not False:
        raise FuturesMainReportError("技术、基本面、会员资金不得二比一投票")
    if judgment.get("rules_auto_authorized_action") is not False:
        raise FuturesMainReportError("规则满足不得自动放行交易")
    if judgment.get("profit_guaranteed") is not False:
        raise FuturesMainReportError("Agent判断不得承诺盈利")
    if judgment.get("final_action") != action:
        raise FuturesMainReportError("agent_judgment.final_action必须等于报告action")
    directional_view = judgment.get("directional_view")
    if directional_view not in {"long", "short", "neutral", "uncertain"}:
        raise FuturesMainReportError("agent_judgment.directional_view不受支持")

    if action == "wait":
        wait_for = judgment.get("wait_for")
        if not isinstance(wait_for, list) or not wait_for:
            raise FuturesMainReportError("wait必须至少给出一个结构化wait_for条件")
        for index, item in enumerate(wait_for):
            label = f"agent_judgment.wait_for[{index}]"
            if not isinstance(item, Mapping) or set(item) != {
                "condition",
                "decision_relevance",
                "evidence_refs",
            }:
                raise FuturesMainReportError(f"{label}字段不受支持")
            _non_empty_text(item.get("condition"), f"{label}.condition")
            _non_empty_text(
                item.get("decision_relevance"), f"{label}.decision_relevance"
            )
            refs = item.get("evidence_refs")
            if not isinstance(refs, list) or not refs:
                raise FuturesMainReportError(f"{label}.evidence_refs不能为空")
            for ref_index, ref in enumerate(refs):
                _resolve_evidence_ref(
                    report, ref, f"{label}.evidence_refs[{ref_index}]"
                )
        reassess_by = _timestamp(
            judgment.get("reassess_by"),
            "agent_judgment.reassess_by",
        ).astimezone(BEIJING_TIMEZONE)
        if reassess_by <= observed_at.astimezone(BEIJING_TIMEZONE):
            raise FuturesMainReportError(
                "agent_judgment.reassess_by必须晚于报告观察时间，作为未来重评deadline"
            )
        update_triggers = judgment.get("update_triggers")
        if not isinstance(update_triggers, list) or not update_triggers:
            raise FuturesMainReportError("wait必须至少给出一个结构化update_trigger")
        for index, item in enumerate(update_triggers):
            label = f"agent_judgment.update_triggers[{index}]"
            if not isinstance(item, Mapping) or set(item) != {
                "trigger",
                "required_update",
                "evidence_refs",
            }:
                raise FuturesMainReportError(f"{label}字段不受支持")
            _non_empty_text(item.get("trigger"), f"{label}.trigger")
            _non_empty_text(item.get("required_update"), f"{label}.required_update")
            refs = item.get("evidence_refs")
            if not isinstance(refs, list) or not refs:
                raise FuturesMainReportError(f"{label}.evidence_refs不能为空")
            for ref_index, ref in enumerate(refs):
                _resolve_evidence_ref(
                    report, ref, f"{label}.evidence_refs[{ref_index}]"
                )

    divergence = judgment.get("view_action_divergence")
    directional_divergence = action in {"long", "short"} and directional_view != action
    if directional_divergence and divergence is None:
        raise FuturesMainReportError(
            "方向观点与long/short资本动作不一致时必须给出view_action_divergence"
        )
    if divergence is not None:
        if not isinstance(divergence, Mapping) or set(divergence) != {
            "directional_view",
            "final_action",
            "rationale",
            "capital_allocation_logic",
            "evidence_refs",
        }:
            raise FuturesMainReportError("view_action_divergence字段不受支持")
        if (
            divergence.get("directional_view") != directional_view
            or divergence.get("final_action") != action
        ):
            raise FuturesMainReportError(
                "view_action_divergence必须精确绑定directional_view与final_action"
            )
        _non_empty_text(
            divergence.get("rationale"), "view_action_divergence.rationale"
        )
        _non_empty_text(
            divergence.get("capital_allocation_logic"),
            "view_action_divergence.capital_allocation_logic",
        )
        refs = divergence.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            raise FuturesMainReportError("view_action_divergence.evidence_refs不能为空")
        for index, ref in enumerate(refs):
            _resolve_evidence_ref(
                report, ref, f"view_action_divergence.evidence_refs[{index}]"
            )

    domains = judgment.get("domains")
    domain_ids = ["technical_structure", "fundamentals_and_news", "member_flow"]
    if not isinstance(domains, Mapping) or set(domains) != set(domain_ids):
        raise FuturesMainReportError("agent_judgment.domains必须完整给出技术、基本面新闻、会员资金三面")
    status_by_domain = {
        "technical_structure": structure_status,
        "fundamentals_and_news": (
            "unobservable"
            if fundamentals_status == "unobservable" and news_status == "unobservable"
            else "observed"
        ),
        "member_flow": member_status,
    }
    ref_prefixes = {
        "technical_structure": ("structure_evidence", "market_data"),
        "fundamentals_and_news": ("fundamentals", "important_news"),
        "member_flow": ("member_flow",),
    }
    allowed_stances = {
        "supports_long",
        "supports_short",
        "mixed",
        "neutral",
        "unobservable",
    }
    for domain_id in domain_ids:
        domain = domains[domain_id]
        label = f"agent_judgment.domains.{domain_id}"
        if not isinstance(domain, Mapping):
            raise FuturesMainReportError(f"{label}格式错误")
        if set(domain) != {
            "stance",
            "supporting_case",
            "counter_case",
            "confidence",
            "causal_transmission",
            "evidence_refs",
        }:
            raise FuturesMainReportError(f"{label}必须使用三面判断字段白名单")
        stance = domain.get("stance")
        if stance not in allowed_stances:
            raise FuturesMainReportError(f"{label}.stance不受支持")
        fact_unobservable = status_by_domain[domain_id] == "unobservable"
        if fact_unobservable != (stance == "unobservable"):
            raise FuturesMainReportError(f"{label}.stance必须如实反映该面的可观测状态")
        _non_empty_text(domain.get("supporting_case"), f"{label}.supporting_case")
        _non_empty_text(domain.get("counter_case"), f"{label}.counter_case")
        _non_empty_text(
            domain.get("causal_transmission"), f"{label}.causal_transmission"
        )
        confidence = domain.get("confidence")
        if not isinstance(confidence, Mapping):
            raise FuturesMainReportError(f"{label}.confidence格式错误")
        if set(confidence) != {"value", "rationale"}:
            raise FuturesMainReportError(f"{label}.confidence字段不受支持")
        confidence_value = _decimal(confidence.get("value"), f"{label}.confidence.value")
        if not Decimal("0") <= confidence_value <= Decimal("1"):
            raise FuturesMainReportError(f"{label}.confidence.value必须在0到1之间")
        _non_empty_text(confidence.get("rationale"), f"{label}.confidence.rationale")
        refs = domain.get("evidence_refs")
        if not isinstance(refs, list) or not refs or any(
            not isinstance(ref, str)
            or not ref.startswith(ref_prefixes[domain_id])
            for ref in refs
        ):
            raise FuturesMainReportError(f"{label}.evidence_refs必须引用对应证据面")
        for index, ref in enumerate(refs):
            _resolve_evidence_ref(
                report, ref, f"{label}.evidence_refs[{index}]"
            )

    for field in (
        "synthesis_thesis",
        "strongest_case_for",
        "strongest_case_against",
        "why_now",
        "why_not_wait",
        "why_not_opposite",
        "invalidation",
        "expected_net_value_reasoning",
        "costs_and_slippage_considered",
        "long_term_validation_plan",
    ):
        _non_empty_text(judgment.get(field), f"agent_judgment.{field}")
    expected_net_value = judgment.get("expected_net_value")
    if expected_net_value not in {"positive", "negative", "uncertain"}:
        raise FuturesMainReportError("agent_judgment.expected_net_value不受支持")
    if action in {"long", "short"} and expected_net_value != "positive":
        raise FuturesMainReportError("方向性动作必须是Agent主观判断的成本后正期望")
    capital_verdict = judgment.get("capital_allocation_verdict")
    expected_capital_verdict = (
        "deploy_conditionally" if action in {"long", "short"} else "preserve_capital"
    )
    if capital_verdict != expected_capital_verdict:
        raise FuturesMainReportError("capital_allocation_verdict必须与Agent最终动作一致")

    alternative = judgment.get("alternative")
    if not isinstance(alternative, Mapping):
        raise FuturesMainReportError("agent_judgment.alternative必须是显式替代方案")
    if set(alternative) != {"action", "reason"}:
        raise FuturesMainReportError("agent_judgment.alternative字段不受支持")
    alternative_action = alternative.get("action")
    if alternative_action not in {"long", "short", "wait"} or alternative_action == action:
        raise FuturesMainReportError("alternative.action必须是未被选择的long/short/wait之一")
    _non_empty_text(alternative.get("reason"), "agent_judgment.alternative.reason")

    hypotheses = judgment.get("competing_hypotheses")
    if not isinstance(hypotheses, Mapping) or list(hypotheses) != ["long", "short", "wait"]:
        raise FuturesMainReportError("Agent必须显式比较long、short、wait三个竞争假设")
    selected: list[str] = []
    for hypothesis_action in ("long", "short", "wait"):
        hypothesis = hypotheses[hypothesis_action]
        label = f"agent_judgment.competing_hypotheses.{hypothesis_action}"
        if not isinstance(hypothesis, Mapping):
            raise FuturesMainReportError(f"{label}格式错误")
        if set(hypothesis) != {"case", "reason", "disposition"}:
            raise FuturesMainReportError(f"{label}必须使用竞争假设字段白名单")
        _non_empty_text(hypothesis.get("case"), f"{label}.case")
        _non_empty_text(hypothesis.get("reason"), f"{label}.reason")
        disposition = hypothesis.get("disposition")
        if disposition not in {"selected", "rejected"}:
            raise FuturesMainReportError(f"{label}.disposition不受支持")
        if disposition == "selected":
            selected.append(hypothesis_action)
    if selected != [action]:
        raise FuturesMainReportError("三个竞争假设中必须且只能选中final_action")

    coverage = judgment.get("coverage_uncertainty")
    if not isinstance(coverage, Mapping):
        raise FuturesMainReportError("agent_judgment.coverage_uncertainty格式错误")
    if set(coverage) != {"level", "known_gaps", "decision_impact"}:
        raise FuturesMainReportError("coverage_uncertainty字段不受支持")
    if coverage.get("level") not in {"low", "medium", "high"}:
        raise FuturesMainReportError("coverage_uncertainty.level不受支持")
    known_gaps = coverage.get("known_gaps")
    if not isinstance(known_gaps, list) or any(not str(gap).strip() for gap in known_gaps):
        raise FuturesMainReportError("coverage_uncertainty.known_gaps必须是文本列表")
    _non_empty_text(coverage.get("decision_impact"), "coverage_uncertainty.decision_impact")
    objective_gaps = (
        market_status != "fresh"
        or main_chain_status != "complete"
        or main_snapshot_status != "current_session"
        or structure_status != "available"
        or daily_data_evidence_status != "verified"
        or fundamentals_status != "available"
        or news_status == "unobservable"
        or member_status != "available"
        or (
            scan_summary is not None
            and (
                scan_summary["partial_count"] > 0
                or scan_summary["unobservable_count"] > 0
            )
        )
    )
    if objective_gaps and not known_gaps:
        raise FuturesMainReportError("存在客观覆盖缺口时Agent必须在known_gaps中显式承认")
    if daily_data_evidence_status != "verified" and not any(
        "daily_data_evidence_not_verified" in str(gap)
        for gap in known_gaps
    ):
        raise FuturesMainReportError(
            "日线证据未验证时coverage_uncertainty.known_gaps必须显式引用"
            "daily_data_evidence_not_verified"
        )
    return {
        "expected_net_value": expected_net_value,
        "directional_view": directional_view,
        "coverage_uncertainty": coverage.get("level"),
        "competing_hypotheses_compared": True,
        "daily_data_evidence_status": daily_data_evidence_status,
    }


def _assess_v3_report_at(
    report: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    validated_at: datetime,
    policy_sha256: str,
    artifact_root: Path | None,
) -> dict[str, Any]:
    _validate_v3_policy(policy)
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise FuturesMainReportError("v3报告版本不受支持")
    _validate_v3_top_level_fields(report)
    _reject_option_fields(report)
    if "hard_gates" in report or "evidence_modifiers" in report:
        raise FuturesMainReportError("v3不接受硬门禁或投票式evidence_modifiers")
    action = report.get("action")
    if action not in {"long", "short", "wait"}:
        raise FuturesMainReportError("报告动作必须是long、short或wait")
    observed_at = _timestamp(report.get("observed_at"), "报告观察时间").astimezone(
        BEIJING_TIMEZONE
    )
    if validated_at.tzinfo is None or validated_at.utcoffset() is None:
        raise FuturesMainReportError("校验时间必须带时区")
    validation_time = validated_at.astimezone(BEIJING_TIMEZONE)
    future_skew = Decimal(str((observed_at - validation_time).total_seconds()))
    if future_skew > _decimal(
        policy["market_data_freshness"]["future_clock_skew_seconds"], "未来时钟容差"
    ):
        raise FuturesMainReportError("报告观察时间不得来自未来")
    report_age = Decimal(str((validation_time - observed_at).total_seconds()))
    requested_plan = report.get("point_plan")
    requests_now_execution = (
        action in {"long", "short"}
        and isinstance(requested_plan, Mapping)
        and requested_plan.get("order_timing") == "now"
    )
    if requests_now_execution and report_age > _decimal(
        policy["market_data_freshness"]["live_report_max_age_seconds"],
        "实时报表年龄上限",
    ):
        raise FuturesMainReportError(
            "order_timing=now的报告必须在live_report_max_age_seconds内验证"
        )

    product, analysis_contract, target_actual_main, exchange, tick_size = (
        _validate_analysis_target_v3(report, observed_at)
    )
    scan_summary = _validate_scan_manifest_v3(
        report,
        observed_at,
        product,
        analysis_contract,
        target_actual_main,
        policy_sha256=policy_sha256,
        artifact_root=artifact_root,
        scan_policy=policy["scan_manifest"],
    )
    focus_refresh_summary = _validate_focus_refresh_link_v3(
        report,
        product=product,
        analysis_contract=analysis_contract,
        scan_summary=scan_summary,
    )
    candidate_summary = _validate_candidate_comparison_v3(
        report,
        product=product,
        analysis_contract=analysis_contract,
        scan_summary=scan_summary,
    )
    main_summary = _validate_actual_main_v3(
        report, observed_at, product, exchange, target_actual_main
    )
    market_summary = _validate_market_data_v3(
        report,
        policy,
        observed_at,
        analysis_contract,
        product,
        exchange,
        tick_size,
    )
    if main_summary.get("selected_contract") == analysis_contract:
        quote_value = report.get("market_data", {}).get("quote")
        selected_rows = [
            row
            for row in main_summary.get("contracts", [])
            if row.get("contract") == analysis_contract
        ]
        if len(selected_rows) != 1:
            raise FuturesMainReportError("actual_main流动性无法定位OI首位合约")
        selected_volume = selected_rows[0].get("volume")
        if isinstance(quote_value, Mapping) and quote_value.get("volume") is not None:
            report_analysis_volume = _exact_integer(
                quote_value.get("volume"), "market_data.quote.volume", minimum=0
            )
            if report_analysis_volume != selected_volume:
                raise FuturesMainReportError(
                    "market_data.quote.volume必须等于同一分析合约的actual_main成交量"
                )
        if selected_volume is None:
            derived_main_liquidity = "unobservable"
        elif selected_volume == 0:
            derived_main_liquidity = "no_trade"
        else:
            bid_value = quote_value.get("bid") if isinstance(quote_value, Mapping) else None
            ask_value = quote_value.get("ask") if isinstance(quote_value, Mapping) else None
            bid = _decimal(bid_value, "market_data.quote.bid") if _finite_number(bid_value) else None
            ask = _decimal(ask_value, "market_data.quote.ask") if _finite_number(ask_value) else None
            derived_main_liquidity = (
                "observable_trade"
                if bid is not None and ask is not None and bid > 0 and ask >= bid
                else "execution_quote_incomplete"
            )
        if main_summary.get("liquidity_status") != derived_main_liquidity:
            raise FuturesMainReportError(
                "actual_main.liquidity_status必须由OI首位合约的volume及当前bid/ask重算"
            )
    if action in {"long", "short"}:
        quote_value = report.get("market_data", {}).get("quote")
        if not isinstance(quote_value, Mapping):
            raise FuturesMainReportError(
                "long/short资本动作必须有被分析合约的成交事实"
            )
        analysis_volume = _exact_integer(
            quote_value.get("volume"), "market_data.quote.volume", minimum=0
        )
        if analysis_volume <= 0:
            raise FuturesMainReportError(
                "被分析合约无volume>0交易事实时只能wait并保留directional_view"
            )
        bid_value = quote_value.get("bid")
        ask_value = quote_value.get("ask")
        bid = _decimal(bid_value, "market_data.quote.bid") if _finite_number(bid_value) else None
        ask = _decimal(ask_value, "market_data.quote.ask") if _finite_number(ask_value) else None
        executable_book = (
            bid is not None
            and ask is not None
            and bid > 0
            and ask >= bid
            and _tick_aligned(bid, tick_size)
            and _tick_aligned(ask, tick_size)
        )
        plan = report.get("point_plan")
        if not isinstance(plan, Mapping):
            raise FuturesMainReportError("long/short资本动作必须有结构化point_plan")
        if plan.get("order_timing") == "conditional":
            expected_precondition = {
                "requires_fresh_quote": True,
                "minimum_volume": 1,
                "requires_positive_non_inverted_bid_ask": True,
                "executable_side": "ask" if action == "long" else "bid",
            }
            if plan.get("execution_precondition") != expected_precondition:
                raise FuturesMainReportError(
                    "execution_precondition必须声明执行前重抓fresh quote、"
                    "volume>0及完整非倒挂可成交侧"
                )
        if not executable_book:
            if plan.get("order_timing") != "conditional":
                raise FuturesMainReportError(
                    "盘口不完整或倒挂时long/short只能使用conditional执行语义"
                )
            _non_empty_text(
                plan.get("timing_override_rationale"),
                "盘口不完整时的timing_override_rationale",
            )
    if (
        scan_summary is not None
        and scan_summary.get("comparable_as_of") is not None
        and market_summary.get("latest_completed_date") is not None
        and scan_summary["comparable_as_of"]
        != market_summary["latest_completed_date"]
    ):
        raise FuturesMainReportError("scan成功品种as_of必须等于报告最近完成交易日")
    quote_time = market_summary.get("quote_observed_at")
    chain_time = main_summary.get("chain_observed_at")
    snapshot_is_current = (
        market_summary.get("status") == "fresh"
        and quote_time is not None
        and chain_time is not None
        and abs((quote_time - chain_time).total_seconds())
        <= int(policy["actual_main"]["contract_chain_snapshot_max_span_seconds"])
    )
    derived_snapshot_status = (
        "current_session" if snapshot_is_current else "historical_or_unbound"
    )
    if main_summary.get("snapshot_status") != derived_snapshot_status:
        raise FuturesMainReportError("actual_main.snapshot_status必须由fresh行情与同批次主力链重算")
    structure_summary = _validate_structure_v3(
        report,
        observed_at,
        analysis_contract,
        main_summary.get("selected_contract"),
        tick_size,
        market_summary["latest_completed_date"],
        scan_summary,
    )
    raw_binding: dict[str, Any] = {}
    raw_provenance_verified = _validate_raw_snapshot_binding_v3(
        report,
        analysis_contract=analysis_contract,
        selected_actual_main_contract=main_summary.get("selected_contract"),
        tick_size=tick_size,
        main_summary=main_summary,
        structure_summary=structure_summary,
        scan_summary=scan_summary,
        artifact_root=artifact_root,
        product=product,
        exchange=exchange,
        market_summary=market_summary,
        raw_binding_out=raw_binding,
    )
    completed_daily_status = raw_binding.get(
        "completed_daily_status",
        structure_summary.get("daily_data_evidence_status"),
    )
    daily_cache_artifact_replay_verified = bool(
        raw_provenance_verified
        and raw_binding.get("daily_cache_artifact_replay_verified") is True
    )
    upstream_response_identity_verified = bool(
        raw_provenance_verified
        and raw_binding.get("upstream_response_identity_verified") is True
    )
    _validate_completed_bar_provenance_claim_v3(
        report,
        daily_status=(
            None
            if completed_daily_status is None
            else str(completed_daily_status)
        ),
        daily_cache_artifact_replay_verified=(
            daily_cache_artifact_replay_verified
        ),
    )
    if (
        requests_now_execution
        and report.get("target", {}).get("mode") == "actual_main"
        and main_summary.get("liquidity_status") != "observable_trade"
    ):
        raise FuturesMainReportError(
            "order_timing=now要求OI首位实际主力的liquidity_status=observable_trade"
        )
    if requests_now_execution and not raw_provenance_verified:
        raise FuturesMainReportError(
            "order_timing=now必须有已核对原始字节的当前报价证据"
        )
    fundamentals_status = _validate_fundamentals_v3(
        report, observed_at, analysis_contract
    )
    news_status = _validate_news_v3(report, observed_at, analysis_contract)
    member_status = _validate_member_flow_v3(
        report,
        observed_at,
        analysis_contract,
        exchange,
        tick_size,
        market_summary["latest_completed_date"],
        market_summary["closed_dates"],
        market_summary["latest_bar"],
    )
    computed_ratio = _validate_point_plan_v3(
        report,
        observed_at,
        str(action),
        analysis_contract,
        tick_size,
        structure_summary,
        market_summary,
        verified_completed_daily_rows=raw_binding.get("completed_daily_rows"),
        require_verified_bar_binding=artifact_root is not None,
    )
    judgment_summary = _validate_agent_judgment_v3(
        report,
        str(action),
        observed_at=observed_at,
        structure_status=structure_summary["status"],
        fundamentals_status=fundamentals_status,
        news_status=news_status,
        member_status=member_status,
        market_status=market_summary["status"],
        main_chain_status=main_summary["status"],
        main_snapshot_status=derived_snapshot_status,
        daily_data_evidence_status=structure_summary.get(
            "daily_data_evidence_status"
        ),
        scan_summary=scan_summary,
    )
    report_currentness = (
        "current"
        if report_age <= _decimal(
            policy["market_data_freshness"]["live_report_max_age_seconds"],
            "实时报表年龄上限",
        )
        else "historical_or_stale"
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "action": action,
        "analysis_contract": analysis_contract,
        "actual_main_contract": target_actual_main,
        "decision_status": (
            "agent_direction_recorded" if action in {"long", "short"} else "agent_wait_recorded"
        ),
        "validation_scope": "fact_arithmetic_provenance_and_internal_consistency_only",
        "machine_selected_action": False,
        "validation_is_trade_permission": False,
        "rules_auto_authorized_action": False,
        "majority_vote_used": False,
        "profit_guaranteed": False,
        "futures_order_authorized": False,
        "report_currentness": report_currentness,
        "evidence_status": {
            "market_data": market_summary["status"],
            "actual_main_chain": main_summary["status"],
            "actual_main_liquidity": main_summary["liquidity_status"],
            "technical_structure": structure_summary["status"],
            "technical_daily_data_evidence": structure_summary.get(
                "daily_data_evidence_status"
            ),
            "technical_structure_provenance": structure_summary.get(
                "provenance_status"
            ),
            "technical_session_daily_bar": (
                None
                if market_summary.get("session_aware_daily_bar") is None
                else market_summary["session_aware_daily_bar"]["bar_state"]
            ),
            "fundamentals": fundamentals_status,
            "important_news": news_status,
            "member_flow": member_status,
        },
        "scan_coverage": (
            None
            if scan_summary is None
            else {
                "successful_count": scan_summary["successful_count"],
                "partial_count": scan_summary["partial_count"],
                "unobservable_count": scan_summary["unobservable_count"],
                "focus_scan_status": scan_summary["focus_scan_status"],
                "scan_age_seconds": float(scan_summary["scan_age_seconds"]),
                "artifact_hashes_verified": scan_summary["artifact_hashes_verified"],
                "artifact_bytes_verified": artifact_root is not None,
            }
        ),
        "computed_underlying_reward_risk": (
            None if computed_ratio is None else float(computed_ratio)
        ),
        "expected_net_value": judgment_summary["expected_net_value"],
        "directional_view": judgment_summary["directional_view"],
        "coverage_uncertainty": judgment_summary["coverage_uncertainty"],
        "competing_hypotheses_compared": True,
        "candidate_comparison": candidate_summary,
        "focus_refresh_from_scan": focus_refresh_summary,
        "raw_provenance_bytes_verified": raw_provenance_verified,
        "completed_daily_bar_bound_to_raw_bytes": bool(
            raw_provenance_verified and raw_binding.get("completed_daily_rows")
        ),
        "daily_cache_artifact_replay_verified": (
            daily_cache_artifact_replay_verified
        ),
        "upstream_response_identity_verified": (
            upstream_response_identity_verified
        ),
        "frozen_structure_snapshot_bytes_verified": bool(
            raw_provenance_verified
            and raw_binding.get("frozen_structure_snapshot_verified") is True
        ),
        "validation_mode": (
            "cli_artifact_bytes_verified"
            if artifact_root is not None
            else "mapping_internal_consistency_only"
        ),
        "actual_main_snapshot_status": derived_snapshot_status,
        "derivative_selection": "user_owned_out_of_scope",
    }


def _assess_report_at(
    report: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    *,
    validated_at: datetime,
    artifact_root: Path | None = None,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    """Deterministic validation hook with an injected clock."""
    active_policy = dict(policy) if policy is not None else load_policy(policy_path)
    version = report.get("schema_version")
    if version == REPORT_SCHEMA_VERSION:
        if active_policy.get("schema_version") != POLICY_SCHEMA_VERSION:
            raise FuturesMainReportError("v3报告必须使用v3策略")
        return _assess_v3_report_at(
            report,
            active_policy,
            validated_at=validated_at,
            policy_sha256=_file_sha256(policy_path),
            artifact_root=artifact_root,
        )
    raise FuturesMainReportError("报告版本不受支持")


def assess_report(
    report: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    *,
    artifact_root: Path | None = None,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    """Validate a live report against the real current clock.

    Clock injection is intentionally unavailable on the public entry point so
    callers cannot revive a stale report by passing its own observation time.
    """
    observed_at = _timestamp(report.get("observed_at"), "报告观察时间")
    return _assess_report_at(
        report,
        policy,
        validated_at=datetime.now(observed_at.tzinfo),
        artifact_root=artifact_root,
        policy_path=policy_path,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="校验期货实际主力方向与点位报告")
    parser.add_argument("report", type=Path, help="待校验的报告 JSON")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args()
    result = assess_report(
        _read_json(args.report),
        load_policy(args.policy),
        artifact_root=args.report.resolve().parent,
        policy_path=args.policy.resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""获取中国商品期货当前主力、收盘边界证据和技术快照。"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
from contextlib import contextmanager
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
from akshare.futures.futures_daily_bar import calendar as AKSHARE_TRADE_DATES
import pandas as pd
import requests

try:
    from scripts.runtime_universe import (
        allowed_products,
        canonical_delivery_contract,
        require_allowed_product,
        provider_exchange_identity,
        sina_provider_symbol_for_contract,
    )
    from scripts.verified_daily_loader import (
        DAILY_PATH as VERIFIED_DAILY_PATH,
        MANIFEST_PATH as VERIFIED_DAILY_MANIFEST_PATH,
        DailyCacheVerificationError,
        load_verified_contract_history,
    )
    from scripts.tick_evidence import (
        archived_http_response_fields,
        extract_contract_tick_from_html,
        replay_exact_tick_evidence,
    )
    from scripts.session_schedule import is_regular_night_session_clock
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from runtime_universe import (
        allowed_products,
        canonical_delivery_contract,
        require_allowed_product,
        provider_exchange_identity,
        sina_provider_symbol_for_contract,
    )
    from verified_daily_loader import (
        DAILY_PATH as VERIFIED_DAILY_PATH,
        MANIFEST_PATH as VERIFIED_DAILY_MANIFEST_PATH,
        DailyCacheVerificationError,
        load_verified_contract_history,
    )
    from tick_evidence import (
        archived_http_response_fields,
        extract_contract_tick_from_html,
        replay_exact_tick_evidence,
    )
    from session_schedule import is_regular_night_session_clock


BEIJING = ZoneInfo("Asia/Shanghai")
PROJECT = Path(__file__).resolve().parents[1]
PRODUCT_SPECS_PATH = PROJECT / "config" / "product_contract_specs.json"
REPORT_POLICY_PATH = PROJECT / "config" / "futures_main_report_policy.json"
QUOTE_URL = "https://hq.sinajs.cn/list=nf_{symbol}"
CONTRACT_CHAIN_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQFuturesData"
)
CONTRACT_DETAIL_URL = "https://finance.sina.com.cn/futures/quotes/{symbol}.shtml"
HEADERS = {
    "User-Agent": "Mozilla/5.0 Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}
FINANCIAL_PREFIXES = {"IF", "IH", "IC", "IM", "T", "TF", "TS", "TL"}
SINA_PRODUCT_ALIASES = {
    "苹果": "鲜苹果",
    "甲醇": "郑醇",
    "天然橡胶": "橡胶",
    "铜": "沪铜",
}
DAILY_HISTORY_LIMIT = 320
TICK_EVIDENCE_POLICIES = (
    "configured_research",
    "third_party_cross_check",
)
COMPLETED_DAILY_FIELDS = (
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "hold",
)
COMPACT_QUOTE_FIELDS = (
    "symbol",
    "product_name",
    "price",
    "open",
    "high",
    "low",
    "bid",
    "ask",
    "previous_settlement",
    "change_percent",
    "open_interest",
    "volume",
    "quote_time",
    "data_source",
    "source_url",
)
COMPACT_CONTRACT_FIELDS = (
    "symbol",
    "exchange",
    "provider_trade_date",
    "price",
    "open",
    "high",
    "low",
    "bid",
    "ask",
    "previous_settlement",
    "change_percent",
    "volume",
    "open_interest",
    "quote_time",
    "data_source",
    "source_url",
)
COMPACT_TERM_FIELDS = (
    "symbol",
    "exchange",
    "provider_trade_date",
    "price",
    "previous_settlement",
    "change_percent",
    "volume",
    "open_interest",
    "quote_time",
)


def now_beijing() -> datetime:
    return datetime.now(BEIJING)


@contextmanager
def wall_clock_deadline(seconds: int):
    """Bound the complete CLI fetch, including AKShare calls without timeouts."""

    if (
        seconds <= 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
    ):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expire(_signum, _frame):
        raise TimeoutError(f"行情完整采集超过 {seconds} 秒总时限")

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper().removeprefix("NF_")
    if re.fullmatch(r"[A-Z]{1,3}", symbol):
        symbol += "0"
    if re.fullmatch(r"[A-Z]{1,3}0", symbol):
        require_allowed_product(symbol)
        return symbol
    if not re.fullmatch(r"[A-Z]{1,3}\d{4}", symbol):
        raise ValueError("请输入商品品种或实际合约代码，例如 UR、UR0、UR2701")
    return canonical_delivery_contract(symbol, reference_date=now_beijing().date())


def optional_number(value: Any) -> float | None:
    try:
        if value in (None, "", "--") or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_date(value: Any) -> str | None:
    """Normalize a provider date without guessing missing session semantics."""
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y-%m-%d")


def is_specific_contract(symbol: str) -> bool:
    """Return true only for a concrete delivery month, never a product/continuous code."""

    try:
        canonical_delivery_contract(symbol)
    except ValueError:
        return False
    return True


def provider_symbol_for_sina(symbol: str) -> str:
    """Return the canonical symbol used at every current Sina request boundary."""

    normalized = normalize_symbol(symbol)
    if not is_specific_contract(normalized):
        return normalized
    return sina_provider_symbol_for_contract(normalized)


def infer_completed_trade_date(observed_at: datetime) -> dict[str, Any]:
    """Infer the latest completed date from a calendar template, with honest lineage.

    The bundled AKShare calendar is useful for weekends and ordinary holidays but
    is not treated as a freshly verified exchange calendar.  Callers that have
    official calendar evidence should bind ``completed_trade_date`` explicitly.
    """

    observation = observed_at
    if observation.tzinfo is None:
        observation = observation.replace(tzinfo=BEIJING)
    else:
        observation = observation.astimezone(BEIJING)
    eligible_calendar_date = observation.date()
    if observation.time() < time(15, 5):
        eligible_calendar_date -= timedelta(days=1)
    eligible = [
        value
        for value in AKSHARE_TRADE_DATES
        if value <= eligible_calendar_date.strftime("%Y%m%d")
    ]
    if eligible:
        digits = eligible[-1]
        completed = date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:]}")
        method = "akshare_exchange_calendar_template"
    else:
        completed = eligible_calendar_date
        while completed.weekday() >= 5:
            completed -= timedelta(days=1)
        method = "weekday_template"
    return {
        "date": completed.isoformat(),
        "source": "template_inferred",
        "method": method,
        "official_calendar_verified": False,
    }


def direct_quote(symbol: str, timeout: int) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    provider_symbol = provider_symbol_for_sina(symbol)
    response = requests.get(
        QUOTE_URL.format(symbol=provider_symbol), headers=HEADERS, timeout=timeout
    )
    response.raise_for_status()
    match = re.search(r'=\s*"([^"]*)"', response.content.decode("gb18030"))
    if not match:
        raise ValueError("新浪行情响应格式异常")
    fields = match.group(1).split(",")
    if len(fields) < 19 or optional_number(fields[8]) is None:
        raise ValueError("商品期货行情字段不完整")
    previous_settlement = optional_number(fields[10])
    price = optional_number(fields[8])
    quote_time = f"{fields[17]} {fields[1][:2]}:{fields[1][2:4]}:{fields[1][4:6]}"
    return {
        "symbol": symbol,
        "product_name": fields[16] or fields[0].removesuffix("连续"),
        "price": price,
        "open": optional_number(fields[2]),
        "high": optional_number(fields[3]),
        "low": optional_number(fields[4]),
        "bid": optional_number(fields[6]),
        "ask": optional_number(fields[7]),
        "previous_settlement": previous_settlement,
        "change_percent": (
            (price / previous_settlement - 1) * 100
            if price is not None and previous_settlement
            else None
        ),
        "open_interest": optional_number(fields[13]),
        "volume": optional_number(fields[14]),
        "quote_time": quote_time,
        "data_source": "新浪公开期货行情",
        "source_url": QUOTE_URL.format(symbol=provider_symbol),
        "provider_symbol": provider_symbol,
    }


def actual_contract_rows(product_name: str, prefix: str) -> list[dict[str, Any]]:
    prefix = require_allowed_product(prefix)
    configured_exchange = str(allowed_products()[prefix]["exchange"]).upper()
    canonical_name = SINA_PRODUCT_ALIASES.get(product_name, product_name)
    frame = ak.futures_zh_realtime(canonical_name)
    if frame.empty:
        raise ValueError("实际合约链为空")
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame = frame[
        frame["symbol"].str.fullmatch(rf"{re.escape(prefix)}\d{{3,4}}")
    ]
    if frame.empty:
        raise ValueError("没有实际月份合约行")
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        price = optional_number(row.get("trade"))
        previous_settlement = optional_number(row.get("presettlement"))
        raw_exchange = row.get("exchange")
        provider_exchange = (
            None
            if raw_exchange is None or pd.isna(raw_exchange)
            else str(raw_exchange)
        )
        open_interest = optional_number(row.get("position"))
        exchange_identity_status = provider_exchange_identity(
            configured_exchange, provider_exchange
        )
        provider_trade_date = optional_date(row.get("tradedate"))
        reference_date = (
            date.fromisoformat(provider_trade_date)
            if provider_trade_date is not None
            else now_beijing().date()
        )
        canonical_symbol = canonical_delivery_contract(
            str(row["symbol"]),
            exchange=configured_exchange,
            reference_date=reference_date,
            allow_czce_three_digit=True,
        )
        provider_quote_time = f"{row.get('tradedate')} {row.get('ticktime')}"
        rows.append(
            {
                "symbol": canonical_symbol,
                "exchange": configured_exchange,
                "provider_exchange": provider_exchange,
                "exchange_identity_status": exchange_identity_status,
                "provider_trade_date": provider_trade_date,
                "price": price,
                "previous_settlement": previous_settlement,
                "change_percent": (
                    (price / previous_settlement - 1) * 100
                    if price is not None and previous_settlement
                    else None
                ),
                "open": optional_number(row.get("open")),
                "high": optional_number(row.get("high")),
                "low": optional_number(row.get("low")),
                "bid": optional_number(row.get("bidprice1")),
                "ask": optional_number(row.get("askprice1")),
                "volume": optional_number(row.get("volume")),
                "open_interest": open_interest,
                "chain_row_status": (
                    "provider_exchange_conflict"
                    if exchange_identity_status == "provider_exchange_conflict"
                    else "provider_exchange_unobservable"
                    if exchange_identity_status == "provider_exchange_unobservable"
                    else "open_interest_unobservable"
                    if open_interest is None
                    else "inactive_zero_open_interest"
                    if open_interest == 0
                    else "rankable_positive_open_interest"
                ),
                "provider_quote_time": provider_quote_time,
                "quote_time": provider_quote_time,
                "quote_time_normalization": "provider_calendar_time_unchanged",
                "data_source": "新浪公开期货合约链（AKShare结构化）",
                "source_url": CONTRACT_CHAIN_URL,
            }
        )
    return rows


def normalize_chain_calendar_observation(
    row: dict[str, Any],
    *,
    direct_quote_time: str,
    product: str,
) -> None:
    """Derive calendar observation time only for an explainable night-session label.

    The provider's raw trade-date timestamp is never overwritten: it remains in
    ``provider_quote_time``.  Unexplained future dates stay visible and will make
    the downstream chain comparison partial instead of being washed backward.
    """

    raw_value = row.get("provider_quote_time", row.get("quote_time"))
    try:
        provider_timestamp = pd.Timestamp(raw_value)
        direct_timestamp = pd.Timestamp(direct_quote_time)
    except (TypeError, ValueError):
        row["quote_time_normalization"] = "unobservable_invalid_provider_or_direct_time"
        return
    if pd.isna(provider_timestamp) or pd.isna(direct_timestamp):
        row["quote_time_normalization"] = "unobservable_invalid_provider_or_direct_time"
        return
    if provider_timestamp.date() <= direct_timestamp.date():
        row["quote_time_normalization"] = (
            "provider_calendar_time_unchanged"
            if abs((provider_timestamp - direct_timestamp).total_seconds()) <= 60
            else "unexplained_provider_direct_time_divergence"
        )
        return
    provider_clock = provider_timestamp.time()
    direct_clock = direct_timestamp.time()
    clock_seconds = (
        provider_clock.hour * 3600 + provider_clock.minute * 60 + provider_clock.second
    )
    direct_seconds = (
        direct_clock.hour * 3600 + direct_clock.minute * 60 + direct_clock.second
    )
    is_night_clock = is_regular_night_session_clock(
        product,
        provider_clock,
        REPORT_POLICY_PATH,
    )
    known_calendar_days = sorted(
        date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
        for value in set(AKSHARE_TRADE_DATES)
        if re.fullmatch(r"\d{8}", value)
    )
    session_anchor_date = (
        direct_timestamp.date()
        if direct_seconds >= 21 * 3600
        else direct_timestamp.date() - timedelta(days=1)
    )
    next_trade_days = [
        value for value in known_calendar_days if value > session_anchor_date
    ]
    expected_trade_day = next_trade_days[0] if next_trade_days else None
    if (
        session_anchor_date not in set(known_calendar_days)
        or not is_night_clock
        or abs(clock_seconds - direct_seconds) > 60
        or expected_trade_day is None
        or provider_timestamp.date() != expected_trade_day
    ):
        row["quote_time_normalization"] = "unexplained_future_provider_trade_date"
        return
    row["quote_time"] = datetime.combine(
        direct_timestamp.date(), provider_clock
    ).isoformat(sep=" ")
    row["quote_time_normalization"] = (
        "derived_calendar_time_from_direct_quote_for_next_trade_day_night_session"
    )


def completed_chain_close_asof(
    rows: list[dict[str, Any]], observed_at: Any = None
) -> str | None:
    """Prove a same-day post-close, pre-night contract-chain OI snapshot."""
    if not rows:
        return None
    try:
        observation = pd.Timestamp(observed_at or now_beijing())
    except (TypeError, ValueError):
        return None
    if pd.isna(observation):
        return None
    if observation.tzinfo is None:
        observation = observation.tz_localize(BEIJING)
    else:
        observation = observation.tz_convert(BEIJING)
    trade_dates = {optional_date(row.get("provider_trade_date")) for row in rows}
    if None in trade_dates or len(trade_dates) != 1:
        return None
    trade_date = next(iter(trade_dates))
    if (
        observation.strftime("%Y-%m-%d") != trade_date
        or observation.time() < datetime.strptime("15:00:00", "%H:%M:%S").time()
        or observation.time() >= datetime.strptime("20:00:00", "%H:%M:%S").time()
    ):
        return None
    for row in rows:
        try:
            timestamp = pd.Timestamp(row.get("quote_time"))
        except (TypeError, ValueError):
            return None
        boundary = pd.Timestamp(f"{trade_date} 15:00:00")
        if (
            pd.isna(timestamp)
            or timestamp.strftime("%Y-%m-%d") != trade_date
            or abs((timestamp - boundary).total_seconds()) > 60
            or timestamp > observation.tz_localize(None)
        ):
            return None
    return trade_date


def rank_main_contract_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank currently available month rows without deriving runner or roll state."""
    rankable = [
        row
        for row in rows
        if row.get("exchange_identity_status")
        not in {"provider_exchange_conflict", "provider_exchange_unobservable", "configured_exchange_invalid"}
        and optional_number(row.get("open_interest")) is not None
        and float(row["open_interest"]) > 0
    ]
    return sorted(
        rankable,
        key=lambda row: (
            -(row["open_interest"] or 0),
            -(row["volume"] or 0),
            row["symbol"],
        ),
    )


def contract_chain_time_evidence(
    rows: list[dict[str, Any]], observed_at: datetime
) -> dict[str, Any]:
    """Describe whether OI rows are comparable as one timestamped snapshot."""

    observation = observed_at
    if observation.tzinfo is None:
        observation = observation.replace(tzinfo=BEIJING)
    else:
        observation = observation.astimezone(BEIJING)
    timestamps: list[datetime] = []
    invalid_contracts: list[str] = []
    for row in rows:
        try:
            parsed = pd.Timestamp(row.get("quote_time"))
        except (TypeError, ValueError):
            parsed = pd.NaT
        if pd.isna(parsed):
            invalid_contracts.append(str(row.get("symbol")))
            continue
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize(BEIJING)
        else:
            parsed = parsed.tz_convert(BEIJING)
        timestamps.append(parsed.to_pydatetime())
    span_seconds = (
        None
        if len(timestamps) != len(rows) or not timestamps
        else (max(timestamps) - min(timestamps)).total_seconds()
    )
    future_contracts = [
        str(row.get("symbol"))
        for row, timestamp in zip(
            [row for row in rows if str(row.get("symbol")) not in invalid_contracts],
            timestamps,
        )
        if timestamp > observation + timedelta(seconds=5)
    ]
    comparable = (
        not invalid_contracts
        and not future_contracts
        and span_seconds is not None
        and span_seconds <= 60
    )
    return {
        "status": "same_snapshot" if comparable else "mixed_or_unobservable_time",
        "min_observed_at": (
            None if not timestamps else min(timestamps).isoformat(timespec="seconds")
        ),
        "max_observed_at": (
            None if not timestamps else max(timestamps).isoformat(timespec="seconds")
        ),
        "span_seconds": span_seconds,
        "invalid_time_contracts": invalid_contracts,
        "future_time_contracts": future_contracts,
    }


def build_day_close_evidence(
    main: dict[str, Any],
    observed_at: datetime,
    completed_close_asof: str | None,
) -> dict[str, Any] | None:
    """Build a report-ready completed bar from a proven 15:00 main snapshot."""
    if completed_close_asof is None:
        return None
    required = ("symbol", "open", "high", "low", "price", "volume", "open_interest")
    if any(main.get(field) is None for field in required):
        return None
    try:
        quote_timestamp = pd.Timestamp(main.get("quote_time"))
    except (TypeError, ValueError):
        return None
    if pd.isna(quote_timestamp):
        return None
    if quote_timestamp.tzinfo is None:
        quote_timestamp = quote_timestamp.tz_localize(BEIJING)
    else:
        quote_timestamp = quote_timestamp.tz_convert(BEIJING)
    return {
        "evidence_kind": "actual_main_day_close_snapshot",
        "date": completed_close_asof,
        "contract": main["symbol"],
        "open": main["open"],
        "high": main["high"],
        "low": main["low"],
        "close": main["price"],
        "volume": main["volume"],
        "open_interest": main["open_interest"],
        "observed_at": quote_timestamp.isoformat(),
        "retrieved_at": observed_at.isoformat(timespec="seconds"),
        "source_url": main.get("source_url", CONTRACT_CHAIN_URL),
    }


def expiry_key(symbol: str) -> tuple[int, int, str]:
    digits = re.search(r"(\d{3,4})$", symbol).group(1)
    current_year = now_beijing().year
    if len(digits) == 4:
        return 2000 + int(digits[:2]), int(digits[2:]), symbol
    year = current_year // 10 * 10 + int(digits[0])
    if year < current_year - 2:
        year += 10
    return year, int(digits[1:]), symbol


def contract_tick_size_evidence(symbol: str, timeout: int = 20) -> dict[str, Any]:
    """Archive the current product tick shown on one concrete-contract page.

    The retained ``exact`` fields are v3 schema compatibility labels for byte
    replay and page identity, not a claim of exchange-signed provenance.
    """

    canonical_symbol = normalize_symbol(symbol)
    provider_symbol = provider_symbol_for_sina(canonical_symbol)
    source_url = CONTRACT_DETAIL_URL.format(symbol=provider_symbol)
    response = requests.get(
        source_url,
        headers=HEADERS,
        timeout=max(1, timeout),
        allow_redirects=False,
    )
    response.raise_for_status()
    content = response.content
    try:
        label, raw_value, parsed_value = extract_contract_tick_from_html(
            content, provider_symbol=provider_symbol
        )
    except ValueError as error:
        raise ValueError("实际合约详情页面无法解析") from error
    value = float(parsed_value)
    final_url = str(getattr(response, "url", source_url) or source_url)
    status_code = getattr(response, "status_code", 200)
    archive = archived_http_response_fields(
        content,
        final_url=final_url,
        status_code=status_code,
    )
    evidence = {
        "kind": "actual_contract_http_detail",
        "contract": canonical_symbol,
        "provider_symbol": provider_symbol,
        "source_url": source_url,
        "retrieved_at": now_beijing().isoformat(timespec="seconds"),
        **archive,
        "hash_scope": "http_response_body",
        "locator": "contract detail table item=最小变动价位",
        "raw_item": label,
        "raw_value": str(raw_value),
        "tick_size": value,
        "exact": True,
    }
    replay_exact_tick_evidence(
        evidence,
        contract=canonical_symbol,
        expected_tick_size=parsed_value,
    )
    return evidence


def contract_tick_size(symbol: str, timeout: int = 20) -> float:
    """Compatibility wrapper returning the byte-bound exact contract tick."""

    return float(contract_tick_size_evidence(symbol, timeout)["tick_size"])


def product_spec_tick_size(
    symbol: str, path: Path = PRODUCT_SPECS_PATH,
) -> float:
    """Read the same-product research tick proxy used for approximate levels."""
    normalized = normalize_symbol(symbol)
    prefix_match = re.match(r"[A-Z]+", normalized)
    if prefix_match is None:
        raise ValueError("无法识别实际合约品种")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("同品种tick研究代理不可用") from exc
    if not isinstance(payload, dict) or payload.get("universe_sha256") != hashlib.sha256(
        (PROJECT / "config" / "trading_universe.json").read_bytes()
    ).hexdigest():
        raise ValueError("同品种tick研究代理未绑定当前配置品种范围")
    products = payload.get("products")
    if not isinstance(products, list):
        raise ValueError("同品种tick研究代理不可用")
    matches = [
        optional_number(item.get("tick_size"))
        for item in products
        if isinstance(item, dict) and item.get("symbol") == prefix_match.group(0)
    ]
    matches = [value for value in matches if value is not None and value > 0]
    if len(matches) != 1:
        raise ValueError("同品种tick研究代理缺失或不唯一")
    return matches[0]


def product_spec_tick_size_evidence(
    symbol: str, path: Path = PRODUCT_SPECS_PATH
) -> dict[str, Any]:
    """Bind a research-only proxy tick to the exact local specs artifact."""

    canonical_symbol = normalize_symbol(symbol)
    prefix = re.match(r"[A-Z]+", canonical_symbol).group(0)
    raw_bytes = path.read_bytes()
    value = product_spec_tick_size(canonical_symbol, path)
    return {
        "kind": "product_specs_snapshot_proxy",
        "contract": canonical_symbol,
        "provider_symbol": None,
        "source_path": str(path),
        "retrieved_at": now_beijing().isoformat(timespec="seconds"),
        "content_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "hash_scope": "complete_product_specs_file",
        "locator": f"products[symbol={prefix}].tick_size",
        "raw_item": "tick_size",
        "raw_value": str(value),
        "tick_size": value,
        "exact": False,
    }


def resolved_tick_size_evidence(
    symbol: str,
    *,
    timeout: int,
    policy: str,
) -> tuple[dict[str, Any], str | None]:
    """按用途选择tick证据，并阻止第三方错误值污染结构计算。"""

    if policy not in TICK_EVIDENCE_POLICIES:
        raise ValueError(f"未知tick证据策略：{policy}")

    configured = product_spec_tick_size_evidence(symbol)
    if policy == "configured_research":
        return configured, None

    try:
        observed = contract_tick_size_evidence(symbol, timeout=timeout)
    except Exception as exc:
        return configured, (
            "第三方具体合约页tick交叉检查失败，已使用同品种规格快照进行研究计算；"
            f"精确报单前须以交易所或柜台规则复核：{exc}"
        )

    observed_tick = Decimal(str(observed.get("tick_size")))
    configured_tick = Decimal(str(configured.get("tick_size")))
    if observed_tick != configured_tick:
        return configured, (
            f"第三方具体合约页tick={observed_tick}与同品种规格快照"
            f"tick={configured_tick}不一致，已忽略第三方值并使用规格快照；"
            "该差异仅记为外部参考异常，不再标记为行情字段冲突；"
            "精确报单前须以交易所或柜台规则复核"
        )
    return observed, None


def technical_snapshot(
    symbol: str,
    live_price: float | None,
    completed_before: Any = None,
    include_cutoff_as_completed: bool = False,
    *,
    completed_through: Any = None,
    completion_basis: str | None = None,
    daily_path: Path = VERIFIED_DAILY_PATH,
    manifest_path: Path = VERIFIED_DAILY_MANIFEST_PATH,
    prefer_verified_cache: bool = True,
) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    bound_cutoff = optional_date(completed_through)
    if completed_through is not None and bound_cutoff is None:
        raise ValueError("绑定完成交易日不可解析")
    provider_cutoff = optional_date(completed_before)
    inclusive_cutoff = bound_cutoff is not None or include_cutoff_as_completed
    if bound_cutoff is not None:
        cutoff_value = bound_cutoff
        completion_rule = "daily_date_through_bound_completed_trade_date"
        completion_source = completion_basis or "caller_bound_unverified"
    elif provider_cutoff is None:
        cutoff_value = now_beijing().date().isoformat()
        completion_rule = "daily_date_strictly_before_fetch_calendar_date"
        completion_source = "legacy_calendar_exclusive"
        inclusive_cutoff = False
    elif include_cutoff_as_completed:
        cutoff_value = provider_cutoff
        completion_rule = "daily_date_through_proven_contract_chain_day_close"
        completion_source = completion_basis or "contract_chain_close_snapshot"
    else:
        cutoff_value = provider_cutoff
        completion_rule = "daily_date_strictly_before_live_provider_trade_date"
        completion_source = completion_basis or "legacy_provider_date_exclusive"

    cache_error: str | None = None
    cache_rows: list[dict[str, Any]] | None = None
    data_evidence: dict[str, Any] | None = None
    # The verified cache path requires an inclusive, explicitly bound completed
    # trade date.  Legacy exclusive callers retain their original behavior.
    if prefer_verified_cache and inclusive_cutoff:
        try:
            cache_rows, data_evidence = load_verified_contract_history(
                symbol,
                completed_through=cutoff_value,
                limit=DAILY_HISTORY_LIMIT,
                daily_path=daily_path,
                manifest_path=manifest_path,
            )
        except DailyCacheVerificationError as exc:
            cache_error = str(exc)

    if cache_rows is not None:
        frame = pd.DataFrame(cache_rows)
        frame = frame.rename(columns={"open_interest": "cache_open_interest"})
        # ``hold`` is kept as the compatibility column expected by downstream
        # price/OI calculations and historical callers.
        frame = frame.loc[:, COMPLETED_DAILY_FIELDS].copy()
        data_source = "本地配置品种已发布实际合约日线缓存"
    else:
        frame = ak.futures_zh_daily_sina(symbol=provider_symbol_for_sina(symbol))
        if frame.empty:
            raise ValueError("实际合约日线为空")
        missing = set(COMPLETED_DAILY_FIELDS) - set(frame.columns)
        if missing:
            raise ValueError(f"实际合约日线缺少字段：{sorted(missing)}")
        frame = frame.loc[:, COMPLETED_DAILY_FIELDS].copy()
        data_source = "新浪实际合约未调整日线（AKShare结构化）"

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if frame["date"].isna().any():
        raise ValueError("实际合约日线日期不可解析")
    if frame["date"].duplicated().any():
        raise ValueError("实际合约日线日期重复")
    if not frame["date"].is_monotonic_increasing:
        raise ValueError("实际合约日线日期未严格递增")
    cutoff = pd.Timestamp(cutoff_value)
    completed_mask = frame["date"] <= cutoff if inclusive_cutoff else frame["date"] < cutoff
    frame = frame[completed_mask].tail(DAILY_HISTORY_LIMIT).copy()
    if frame.empty:
        raise ValueError("没有可确认已完成的实际合约日线")
    if bound_cutoff is not None and frame.iloc[-1]["date"].strftime("%Y-%m-%d") != bound_cutoff:
        raise ValueError(f"实际合约在绑定完成交易日 {bound_cutoff} 缺少有效日线")
    for field in COMPLETED_DAILY_FIELDS[1:]:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    invalid = (
        frame[["open", "high", "low", "close", "volume", "hold"]].isna().any(axis=1)
        | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (frame["volume"] <= 0)
        | (frame["hold"] < 0)
        | (frame["high"] < frame[["open", "low", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "high", "close"]].min(axis=1))
    )
    if bool(invalid.any()):
        raise ValueError("实际合约已完成日线含无效 OHLC、零成交或量仓")
    if data_evidence is None:
        data_evidence = {
            "status": "degraded",
            "quality_tier": "single_provider_unverified_fallback",
            "quality_equivalence": "not_equivalent_to_verified_cache",
            "source_kind": "sina_single_actual_contract_daily_fallback",
            "contract": symbol,
            "bound_completed_trade_date": (
                bound_cutoff
                if bound_cutoff is not None
                else frame.iloc[-1]["date"].strftime("%Y-%m-%d")
            ),
            "slice_date_range": {
                "start": frame.iloc[0]["date"].strftime("%Y-%m-%d"),
                "end": frame.iloc[-1]["date"].strftime("%Y-%m-%d"),
            },
            "slice_row_count": int(len(frame)),
            "cache_error": cache_error
            or "verified_cache_not_attempted_for_legacy_exclusive_cutoff",
            "verification": {
                "manifest_schema": False,
                "content_sha256": False,
                "configured_product_scope": False,
                "declared_date_range": False,
                "inclusive_cutoff": bound_cutoff is not None,
            },
        }
    data_evidence["completion_basis"] = completion_source
    data_evidence["completion_rule"] = completion_rule
    completed_daily_rows = [
        {
            "date": row["date"].strftime("%Y-%m-%d"),
            "open": optional_number(row["open"]),
            "high": optional_number(row["high"]),
            "low": optional_number(row["low"]),
            "close": optional_number(row["close"]),
            "volume": optional_number(row["volume"]),
            "hold": optional_number(row["hold"]),
            "completed": True,
        }
        for _, row in frame.iterrows()
    ]
    result: dict[str, Any] = {
        "symbol": symbol,
        "last_daily_date": frame.iloc[-1]["date"].strftime("%Y-%m-%d"),
        "last_daily_close": optional_number(frame.iloc[-1]["close"]),
        "last_daily_open_interest": optional_number(frame.iloc[-1]["hold"]),
        "completed_daily_rows": completed_daily_rows,
        "completed_daily_row_count": len(completed_daily_rows),
        "completed_daily_history_limit": DAILY_HISTORY_LIMIT,
        "daily_completion_cutoff_exclusive": (
            cutoff_value if not inclusive_cutoff else None
        ),
        "daily_completion_cutoff_inclusive": (
            cutoff_value if inclusive_cutoff else None
        ),
        "daily_completion_rule": completion_rule,
        "daily_completion_basis": completion_source,
        "daily_rows_source": data_source,
        "daily_data_evidence": data_evidence,
    }
    for window in (5, 20, 60):
        if len(frame) < window:
            continue
        sample = frame.tail(window)
        result[f"ma{window}"] = float(sample["close"].mean())
        result[f"high{window}"] = float(sample["high"].max())
        result[f"low{window}"] = float(sample["low"].min())
    if len(frame) >= 2:
        result["open_interest_change_1d"] = int(
            frame.iloc[-1]["hold"] - frame.iloc[-2]["hold"]
        )
    if all(result.get(f"ma{x}") is not None for x in (5, 20, 60)):
        ma5, ma20, ma60 = (result[f"ma{x}"] for x in (5, 20, 60))
        result["moving_average_structure"] = (
            "多头排列" if ma5 > ma20 > ma60 else
            "空头排列" if ma5 < ma20 < ma60 else "均线交错"
        )
    if live_price is not None:
        result["live_price"] = live_price
    return result


def price_open_interest_context(
    main: dict[str, Any], technical: dict[str, Any]
) -> dict[str, Any]:
    """Build a price/OI change whose observation window matches the quote.

    During a night session Sina's contract chain commonly labels the quote with
    the following exchange trade date while the daily endpoint still ends on
    the preceding completed session.  In that case the latest live open
    interest must be compared with the last completed daily value.  Reusing the
    preceding daily-to-daily change would combine two different sessions.
    """
    quote_trade_date = optional_date(main.get("provider_trade_date"))
    quote_timestamp: pd.Timestamp | None = None
    try:
        candidate_timestamp = pd.Timestamp(main.get("quote_time"))
        if not pd.isna(candidate_timestamp):
            quote_timestamp = candidate_timestamp
    except (TypeError, ValueError):
        pass
    quote_calendar_date = optional_date(quote_timestamp)
    last_daily_date = optional_date(technical.get("last_daily_date"))
    live_open_interest = optional_number(main.get("open_interest"))
    last_daily_open_interest = optional_number(
        technical.get("last_daily_open_interest")
    )
    daily_change = optional_number(technical.get("open_interest_change_1d"))

    change: float | None = None
    basis = "unavailable"
    if last_daily_date is not None:
        night_quote_after_daily_close = bool(
            quote_timestamp is not None
            and quote_calendar_date == last_daily_date
            and quote_timestamp.hour >= 20
        )
        quote_is_after_daily_bar = bool(
            (quote_trade_date is not None and quote_trade_date > last_daily_date)
            or (quote_calendar_date is not None and quote_calendar_date > last_daily_date)
            or night_quote_after_daily_close
        )
        if (
            quote_is_after_daily_bar
            and live_open_interest is not None
            and last_daily_open_interest is not None
        ):
            change = live_open_interest - last_daily_open_interest
            basis = "live_quote_vs_last_completed_daily"
        elif quote_trade_date == last_daily_date and daily_change is not None:
            change = daily_change
            basis = "completed_daily_bar"

    return {
        "price_open_interest_change": change,
        "price_open_interest_change_basis": basis,
        "quote_trade_date": quote_trade_date,
        "quote_calendar_date": quote_calendar_date,
        "open_interest_reference_date": last_daily_date,
        "price_open_interest_session_aligned": change is not None,
    }


def fetch(
    symbol: str,
    timeout: int,
    limit: int,
    *,
    completed_trade_date: Any = None,
    completed_trade_date_source: str | None = None,
    tick_evidence_policy: str = "third_party_cross_check",
    daily_path: Path = VERIFIED_DAILY_PATH,
    manifest_path: Path = VERIFIED_DAILY_MANIFEST_PATH,
) -> dict[str, Any]:
    request_started_at = now_beijing()
    if completed_trade_date_source is not None and completed_trade_date is None:
        raise ValueError("completed_trade_date_source 需要同时绑定 completed_trade_date")
    if completed_trade_date_source == "official_exchange_calendar":
        raise ValueError(
            "仅凭source字符串不能证明官方交易日历；请使用caller_bound_unverified，"
            "或由上层报告绑定URL、检索时间、原文哈希和定位信息"
        )
    requested = normalize_symbol(symbol)
    prefix = re.match(r"[A-Z]+", requested).group(0)
    requested_specific_contract = is_specific_contract(requested)
    quote = direct_quote(requested, timeout)
    rows = actual_contract_rows(str(quote["product_name"]), prefix)
    # Timestamp the chain after the provider response, not before the network
    # request.  Otherwise a perfectly valid quote stamped a few seconds after
    # request start looks like impossible future data to downstream binders.
    chain_observed_at = now_beijing()
    contract_codes = [str(row.get("symbol") or "").upper() for row in rows]
    if len(contract_codes) != len(set(contract_codes)):
        raise ValueError("实际合约链含重复月份合约，不能可靠识别主力")
    for row in rows:
        normalize_chain_calendar_observation(
            row,
            direct_quote_time=str(quote["quote_time"]),
            product=prefix,
        )
    completed_close_asof = completed_chain_close_asof(rows, chain_observed_at)
    ranked = rank_main_contract_rows(rows)
    actual_main = ranked[0] if ranked else None
    chain_time = contract_chain_time_evidence(rows, chain_observed_at)
    unknown_oi_rows = [
        row for row in rows if row.get("open_interest") is None
    ]
    inactive_rows = [
        row for row in rows if row.get("open_interest") == 0
    ]
    leading_oi = None if actual_main is None else actual_main["open_interest"]
    leading_ties = (
        []
        if actual_main is None
        else [row for row in ranked if row.get("open_interest") == leading_oi]
    )
    unresolved_volume_tie = len(leading_ties) > 1 and any(
        row.get("volume") is None for row in leading_ties
    )
    if requested_specific_contract:
        matches = [row for row in rows if row.get("symbol") == requested]
        if len(matches) != 1:
            raise ValueError(
                f"指定合约 {requested} 不在当前可交易实际合约链中，不得改用主力替代分析"
            )
        analysis_contract = matches[0]
        analysis_mode = "requested_specific_contract"
    else:
        if actual_main is None:
            raise ValueError(
                "产品级请求的实际合约链没有持仓量可观测且为正的月份；"
                "无法擅自改选分析合约"
            )
        analysis_contract = actual_main
        analysis_mode = "product_actual_main"

    bound_completed_date = optional_date(completed_trade_date)
    if completed_trade_date is not None and bound_completed_date is None:
        raise ValueError("--completed-trade-date 不是有效日期")
    if bound_completed_date is not None:
        completion_evidence = {
            "date": bound_completed_date,
            "source": completed_trade_date_source or "caller_bound_unverified",
            "method": "caller_bound",
            "official_calendar_verified": False,
        }
    elif completed_close_asof is not None:
        bound_completed_date = completed_close_asof
        completion_evidence = {
            "date": bound_completed_date,
            "source": "contract_chain_close_snapshot",
            "method": "all_chain_rows_within_60_seconds_of_15_00",
            "official_calendar_verified": False,
        }
    else:
        completion_evidence = infer_completed_trade_date(chain_observed_at)
        bound_completed_date = completion_evidence["date"]

    day_close_evidence = build_day_close_evidence(
        analysis_contract,
        chain_observed_at,
        completed_close_asof,
    )
    by_expiry = sorted(
        [row for row in rows if row["price"] is not None],
        key=lambda row: expiry_key(row["symbol"]),
    )[:limit]
    warnings: list[str] = []
    if actual_main is None:
        warnings.append(
            "实际月份合约链没有持仓量可观测且为正的行；主力身份不可观测，"
            "但仍保留用户显式指定月份的事实与结构分析。"
        )
    if unknown_oi_rows:
        warnings.append(
            "实际月份合约链存在持仓量不可观测行；当前只能称为已观测持仓行中的首位，"
            "不得声明完整实际主力。"
        )
    if unresolved_volume_tie:
        warnings.append(
            "最高持仓量并列且至少一行成交量不可观测；次级排序无法闭合。"
        )
    if chain_time["status"] != "same_snapshot":
        warnings.append(
            "实际合约链行不在同一60秒证据窗；持仓排序只能作为待核查线索。"
        )
    try:
        technical = technical_snapshot(
            analysis_contract["symbol"],
            analysis_contract["price"],
            completed_before=analysis_contract.get("provider_trade_date"),
            include_cutoff_as_completed=True,
            completed_through=bound_completed_date,
            completion_basis=str(completion_evidence["source"]),
            daily_path=daily_path,
            manifest_path=manifest_path,
        )
        try:
            tick_evidence, tick_warning = resolved_tick_size_evidence(
                analysis_contract["symbol"],
                timeout=timeout,
                policy=tick_evidence_policy,
            )
            tick_exact = tick_evidence.get("exact") is True
            technical["tick_size"] = tick_evidence["tick_size"]
            technical["tick_size_source"] = (
                "新浪具体合约页当前展示的品种最小变动价位（非交易所签名）"
                if tick_exact
                else "config/product_contract_specs.json同品种研究代理"
            )
            technical["tick_size_contract"] = analysis_contract["symbol"]
            technical["tick_size_exact"] = tick_exact
            technical["tick_size_precision"] = (
                "exact_actual_contract"
                if tick_exact
                else "approximate_same_product_proxy"
            )
            technical["tick_size_evidence"] = tick_evidence
            if tick_warning is not None:
                warnings.append(tick_warning)
        except Exception as exc:
            technical["tick_size"] = None
            technical["tick_size_source"] = None
            technical["tick_size_contract"] = analysis_contract["symbol"]
            technical["tick_size_exact"] = False
            technical["tick_size_precision"] = "unavailable"
            technical["tick_size_evidence"] = None
            warnings.append(f"品种tick研究证据不可用：{exc}")
        technical.update(price_open_interest_context(analysis_contract, technical))
        daily_evidence = technical.get("daily_data_evidence")
        if isinstance(daily_evidence, dict) and daily_evidence.get("status") != "verified":
            warnings.append(
                "配置品种日线缓存未通过完整验证，技术面已显式降级为单合约来源；"
                f"原因：{daily_evidence.get('cache_error')}"
            )
        if not technical["price_open_interest_session_aligned"]:
            warnings.append("实时价格与持仓变化缺少同一交易时段基准，不得判定价格—持仓共振")
    except Exception as exc:  # 降级而不补造日线
        technical = None
        warnings.append(f"分析合约日线获取失败：{exc}")
    quote_date = str(analysis_contract["quote_time"]).split()[0]
    fetch_completed_at = now_beijing()
    if quote_date != fetch_completed_at.date().isoformat():
        warnings.append("行情日期不是抓取当日，只能称为最新交易快照")
    return {
        "success": True,
        "requested_symbol": requested,
        "request_started_at": request_started_at.isoformat(timespec="seconds"),
        "chain_observed_at": chain_observed_at.isoformat(timespec="seconds"),
        "fetch_time": fetch_completed_at.isoformat(timespec="seconds"),
        "quote": quote,
        # ``main_contract`` is retained as a compatibility alias for scanners
        # built before explicit-contract analysis was separated from main selection.
        "main_contract": actual_main,
        "actual_main": actual_main,
        "analysis_contract": analysis_contract,
        "analysis_mode": analysis_mode,
        "completed_trade_date_evidence": completion_evidence,
        "day_close_evidence": day_close_evidence,
        "main_selection": {
            "selection_basis": (
                "completed_actual_contract_chain_close"
                if completed_close_asof is not None
                else "current_contract_chain_open_interest"
            ),
            "selection_asof": (
                completed_close_asof
                or (
                    actual_main.get("quote_time")
                    if isinstance(actual_main, dict)
                    else chain_observed_at.isoformat(timespec="seconds")
                )
            ),
            "ranking": "highest_open_interest_then_volume_then_contract_code",
            "available_contract_count": len(ranked),
            "rankable_contract_count": len(ranked),
            "source_record_count": len(rows),
            "unknown_open_interest_count": len(unknown_oi_rows),
            "inactive_zero_open_interest_count": len(inactive_rows),
            "chain_scope": "all_actual_month_rows_returned_by_provider",
            "chain_complete_for_product": False,
            "chain_completeness_reason": (
                "第三方接口未提供官方总记录数或分页闭合证明；"
                "只能声明当前可得行中的持仓量首位"
            ),
            "selection_claim": (
                "unobservable_no_rankable_row"
                if actual_main is None
                else "provisional_leader_of_observed_oi_rows"
                if unknown_oi_rows
                or unresolved_volume_tie
                or chain_time["status"] != "same_snapshot"
                else "leader_of_available_rows"
            ),
            "selection_uncertainty": {
                "unknown_open_interest_contracts": [
                    row["symbol"] for row in unknown_oi_rows
                ],
                "unresolved_volume_tie": unresolved_volume_tie,
                "chain_time_not_comparable": chain_time["status"]
                != "same_snapshot",
            },
            "chain_time_evidence": chain_time,
            "actual_contract_chain_sha256": hashlib.sha256(
                json.dumps(
                    rows,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
            "day_close_snapshot_available": completed_close_asof is not None,
            "actual_main_symbol": (
                actual_main.get("symbol") if isinstance(actual_main, dict) else None
            ),
            "analysis_contract_symbol": analysis_contract.get("symbol"),
            "analysis_matches_actual_main": (
                isinstance(actual_main, dict)
                and analysis_contract.get("symbol") == actual_main.get("symbol")
            ),
            "main_liquidity_status": (
                "unobservable"
                if actual_main is None
                else "no_trade"
                if optional_number(actual_main.get("volume")) == 0
                else "observable_trade"
                if optional_number(actual_main.get("volume")) is not None
                and float(actual_main["volume"]) > 0
                and optional_number(actual_main.get("bid")) is not None
                and float(actual_main["bid"]) > 0
                and optional_number(actual_main.get("ask")) is not None
                and float(actual_main["ask"]) >= float(actual_main["bid"])
                else "execution_quote_incomplete"
            ),
        },
        # Preserve every provider-returned actual-month row in the file written
        # with ``--output``.  ``term_structure`` below remains a display slice;
        # it must never be mistaken for the complete evidence used to rank the
        # leader of the available chain.
        "actual_contract_chain": rows,
        "term_structure": by_expiry,
        "technical": technical,
        "warnings": warnings,
    }


def _selected_fields(payload: Any, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return {field: payload[field] for field in fields if field in payload}


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a stdout-safe summary while keeping ``fetch`` fully compatible."""
    if not result.get("success"):
        return {
            field: result[field]
            for field in (
                "success",
                "requested_symbol",
                "fetch_time",
                "error",
                "warnings",
            )
            if field in result
        }

    quote = _selected_fields(result.get("quote"), COMPACT_QUOTE_FIELDS)
    main_contract = _selected_fields(
        result.get("main_contract"), COMPACT_CONTRACT_FIELDS
    )
    actual_main = _selected_fields(
        result.get("actual_main", result.get("main_contract")),
        COMPACT_CONTRACT_FIELDS,
    )
    analysis_contract = _selected_fields(
        result.get("analysis_contract", result.get("main_contract")),
        COMPACT_CONTRACT_FIELDS,
    )
    term_structure = [
        selected
        for row in result.get("term_structure", [])
        if (selected := _selected_fields(row, COMPACT_TERM_FIELDS)) is not None
    ]
    technical = result.get("technical")
    compact_technical = None
    if isinstance(technical, dict):
        compact_technical = {
            key: value
            for key, value in technical.items()
            if key != "completed_daily_rows"
            and not isinstance(value, (dict, list, tuple, set))
        }
        if isinstance(technical.get("daily_data_evidence"), dict):
            compact_technical["daily_data_evidence"] = technical["daily_data_evidence"]

    return {
        "success": True,
        "requested_symbol": result.get("requested_symbol"),
        "fetch_time": result.get("fetch_time"),
        "product": {
            "symbol": quote.get("symbol") if quote else None,
            "name": quote.get("product_name") if quote else None,
        },
        "quote": quote,
        "main_contract": main_contract,
        "actual_main": actual_main,
        "analysis_contract": analysis_contract,
        "analysis_mode": result.get("analysis_mode"),
        "completed_trade_date_evidence": result.get(
            "completed_trade_date_evidence"
        ),
        "main_selection": result.get("main_selection"),
        "day_close_evidence": result.get("day_close_evidence"),
        "term_structure": term_structure,
        "technical": compact_technical,
        "warnings": result.get("warnings", []),
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON beside its destination, then atomically replace the target."""
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="中国商品期货实时主力与结构快照")
    parser.add_argument("symbol", help="商品品种或合约，例如 UR、UR0、UR2701")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument(
        "--tick-evidence-policy",
        choices=TICK_EVIDENCE_POLICIES,
        default="third_party_cross_check",
        help=(
            "configured_research用于全市场宽筛并避免第三方规格误差污染；"
            "third_party_cross_check用于单品种交叉检查"
        ),
    )
    parser.add_argument(
        "--completed-trade-date",
        type=date.fromisoformat,
        help="调用方绑定的最近完成交易日（含当日）",
    )
    parser.add_argument(
        "--completed-trade-date-source",
        choices=(
            "caller_bound_unverified",
            "template_inferred",
        ),
        help="完成交易日调用方口径；官方日历须在上层另带可验证来源，不能靠字符串自证",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--full",
        action="store_true",
        help="向 stdout 打印包含已完成日线数组的完整 JSON",
    )
    output_group.add_argument(
        "--output",
        type=Path,
        help="将完整 JSON 原子写入文件；stdout 仍只打印紧凑摘要",
    )
    args = parser.parse_args()
    if (
        args.completed_trade_date_source is not None
        and args.completed_trade_date is None
    ):
        parser.error("--completed-trade-date-source 必须与 --completed-trade-date 同时使用")
    try:
        with wall_clock_deadline(max(1, args.timeout)):
            result = fetch(
                args.symbol,
                args.timeout,
                max(2, args.limit),
                completed_trade_date=args.completed_trade_date,
                completed_trade_date_source=args.completed_trade_date_source,
                tick_evidence_policy=args.tick_evidence_policy,
            )
    except Exception as exc:
        result = {
            "success": False,
            "requested_symbol": args.symbol,
            "fetch_time": now_beijing().isoformat(timespec="seconds"),
            "error": str(exc),
            "warnings": ["行情获取失败，不得推算或补造价格"],
        }
    if args.output is not None:
        try:
            write_json_atomic(args.output, result)
        except Exception as exc:
            result = {
                "success": False,
                "requested_symbol": args.symbol,
                "fetch_time": now_beijing().isoformat(timespec="seconds"),
                "error": f"完整行情写入失败：{exc}",
                "warnings": ["完整行情未持久化，不得假定输出文件可用"],
            }
    stdout_result = result if args.full else compact_result(result)
    sys.stdout.write(f"{json.dumps(stdout_result, ensure_ascii=False, indent=2)}\n")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

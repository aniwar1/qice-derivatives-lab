#!/usr/bin/env python3
"""Refresh completed daily bars for the configured product universe.

The refresh is deliberately fail closed.  A trade date is published only when
all configured commodity products have at least one valid actual-contract bar.
SHFE, INE, CZCE and GFEX use AKShare's exchange daily adapters.  DCE archives
Sina's symbol-less actual-contract daily response and binds it to an
identity-bearing exact-contract quote endpoint by an exact terminal OHLCV/OI
crosscheck.  That remains non-official third-party evidence.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable

import akshare as ak
from akshare.futures.futures_daily_bar import calendar as AKSHARE_TRADE_DATES
import pandas as pd

try:
    from scripts.fetch_futures import (
        actual_contract_rows,
        direct_quote,
        infer_completed_trade_date,
    )
    from scripts.dce_daily_identity import (
        INDEX_SCHEMA as DCE_IDENTITY_INDEX_SCHEMA,
        DceDailyIdentityError,
        build_identity_bundle,
        descriptor_for_bundle,
        fetch_bound_contract_history,
        load_manifest_identity_rows,
        replay_contract_evidence,
        replay_identity_bundle,
        verify_bundle_covers_cache_rows,
        verify_identity_rows_cover_cache_rows,
    )
    from scripts.runtime_universe import (
        canonical_delivery_contract,
        product_prefix,
        provider_exchange_identity,
    )
    from scripts.verified_daily_loader import (
        DailyCacheVerificationError,
        _resolve_manifest_daily_path,
        _verify_immutable_anchor,
        _post_anchor_calendar_sha256,
        _post_anchor_trade_dates,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from fetch_futures import (
        actual_contract_rows,
        direct_quote,
        infer_completed_trade_date,
    )
    from dce_daily_identity import (
        INDEX_SCHEMA as DCE_IDENTITY_INDEX_SCHEMA,
        DceDailyIdentityError,
        build_identity_bundle,
        descriptor_for_bundle,
        fetch_bound_contract_history,
        load_manifest_identity_rows,
        replay_contract_evidence,
        replay_identity_bundle,
        verify_bundle_covers_cache_rows,
        verify_identity_rows_cover_cache_rows,
    )
    from runtime_universe import (
        canonical_delivery_contract,
        product_prefix,
        provider_exchange_identity,
    )
    from verified_daily_loader import (
        DailyCacheVerificationError,
        _resolve_manifest_daily_path,
        _verify_immutable_anchor,
        _post_anchor_calendar_sha256,
        _post_anchor_trade_dates,
    )


PROJECT = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = PROJECT / "config/trading_universe.json"
SPECS_PATH = PROJECT / "config/product_contract_specs.json"
DAILY_PATH = PROJECT / "local-data/market-history/daily/contract_daily_latest.csv.gz"
MANIFEST_PATH = PROJECT / "local-data/market-history/daily/latest_manifest.json"
INTEGRITY_POLICY_PATH = PROJECT / "config/daily_history_integrity.json"
BEIJING = timezone(timedelta(hours=8))
OFFICIAL_MARKETS = ("SHFE", "INE", "CZCE", "GFEX")
CSV_FIELDS = (
    "date",
    "product",
    "exchange",
    "contract",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "volume",
    "open_interest",
)


class RefreshError(RuntimeError):
    """Raised when a new completed date cannot pass the publication gate."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def coverage_sha256(frame: pd.DataFrame) -> str:
    """Hash the immutable set of published (date, product) coverage keys."""

    pairs = (
        frame.loc[:, ["date", "product"]]
        .astype(str)
        .assign(product=lambda value: value["product"].str.upper())
        .drop_duplicates()
        .sort_values(["date", "product"], kind="stable")
        .to_dict("records")
    )
    payload = json.dumps(
        pairs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _number(value: Any, *, positive: bool = False) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        return None
    return result


def _trade_date(value: Any) -> date | None:
    raw = str(value).strip()
    if re.fullmatch(r"\d{8}(?:\.0)?", raw):
        digits = raw[:8]
        try:
            return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:]}")
        except ValueError:
            return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def _resolve_czce_contract(contract: str, trade_day: date) -> str:
    try:
        return canonical_delivery_contract(
            contract,
            exchange="CZCE",
            reference_date=trade_day,
            allow_czce_three_digit=True,
        )
    except ValueError as error:
        raise RefreshError(f"郑商所实际合约代码异常：{contract}：{error}") from error


def _product_from_contract(contract: str) -> str | None:
    try:
        canonical = canonical_delivery_contract(contract)
    except ValueError:
        return None
    return re.match(r"[A-Z]+", canonical).group(0)


def _valid_bar(row: dict[str, Any]) -> bool:
    prices = [_number(row.get(field), positive=True) for field in ("open", "high", "low", "close")]
    settle = _number(row.get("settle"), positive=True)
    volume = _number(row.get("volume"))
    open_interest = _number(row.get("open_interest"))
    if (
        any(value is None for value in prices)
        or settle is None
        or volume is None
        or volume <= 0
        or open_interest is None
    ):
        return False
    open_price, high, low, close = (float(value) for value in prices)
    return high >= max(open_price, close) and low <= min(open_price, close) and high >= low


def classify_bar(row: dict[str, Any]) -> str:
    """Classify a source row without equating open interest with trading activity."""

    volume = _number(row.get("volume"))
    open_interest = _number(row.get("open_interest"))
    prices = [_number(row.get(field), positive=True) for field in ("open", "high", "low", "close")]
    if volume == 0:
        return (
            "settlement_only_no_trade"
            if open_interest is not None and open_interest > 0
            else "empty_no_trade"
        )
    if _valid_bar(row):
        return "valid_trading_bar"
    if volume is not None and volume > 0 and any(value is None for value in prices):
        return "active_bar_incomplete"
    return "invalid_active_or_incomplete"


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefreshError(f"无法读取{label}：{path}") from exc
    if not isinstance(value, dict):
        raise RefreshError(f"{label}顶层必须是对象：{path}")
    return value


def _integrity_anchor_end() -> str:
    policy = _read_json_object(INTEGRITY_POLICY_PATH, "独立日线完整性策略")
    anchor = policy.get("anchor")
    end = anchor.get("date_range", {}).get("end") if isinstance(anchor, dict) else None
    if not isinstance(end, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) is None:
        raise RefreshError("独立日线完整性策略缺少有效anchor结束日")
    return end


def _metadata(
    universe_path: Path = UNIVERSE_PATH,
    specs_path: Path = SPECS_PATH,
) -> list[dict[str, str]]:
    universe_payload = _read_json_object(universe_path, "交易范围")
    allowed = universe_payload.get("permission_scope", {}).get("allowed_products")
    if not isinstance(allowed, list):
        raise RefreshError("交易范围缺少 allowed_products")
    expected_count = universe_payload.get("product_count")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        raise RefreshError("交易范围缺少有效 product_count")

    universe: list[dict[str, str]] = []
    for item in allowed:
        if not isinstance(item, dict):
            raise RefreshError("交易范围品种格式错误")
        symbol = str(item.get("symbol") or "").upper().strip()
        exchange = str(item.get("exchange") or "").upper().strip()
        if not symbol or exchange not in {*OFFICIAL_MARKETS, "DCE"}:
            raise RefreshError("交易范围品种缺少有效 symbol/exchange")
        universe.append({"symbol": symbol, "exchange": exchange})
    symbols = {item["symbol"] for item in universe}
    if len(universe) != expected_count or len(symbols) != expected_count:
        raise RefreshError("日线刷新要求交易范围恰好覆盖声明的全部不重复品种")

    specs_payload = _read_json_object(specs_path, "合约规格")
    if specs_payload.get("universe_sha256") != sha256_file(universe_path):
        raise RefreshError("合约规格未绑定当前配置品种交易范围文件")
    if specs_payload.get("product_count") != expected_count:
        raise RefreshError("合约规格product_count必须等于配置品种数")
    raw_specs = specs_payload.get("products")
    if not isinstance(raw_specs, list):
        raise RefreshError("合约规格缺少 products")
    specs: dict[str, str] = {}
    for item in raw_specs:
        if not isinstance(item, dict):
            raise RefreshError("合约规格品种格式错误")
        symbol = str(item.get("symbol") or "").upper().strip()
        exchange = str(item.get("exchange") or "").upper().strip()
        if not symbol or not exchange or symbol in specs:
            raise RefreshError("合约规格含重复品种或缺少 symbol/exchange")
        specs[symbol] = exchange
    if set(specs) != symbols:
        raise RefreshError("合约规格与配置品种交易范围不一致")
    for item in universe:
        if specs[item["symbol"]] != item["exchange"]:
            raise RefreshError(f"合约规格交易所不一致：{item['symbol']}")
    return universe


def normalize_exchange_frame(
    frame: pd.DataFrame,
    *,
    exchange: str,
    trade_day: date,
    expected_products: set[str],
    skipped_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    required = {
        "symbol", "date", "open", "high", "low", "close", "volume", "open_interest"
    }
    if frame.empty or not required.issubset(frame.columns):
        raise RefreshError(f"{exchange} {trade_day.isoformat()} 日线为空或字段不完整")
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for source in frame.to_dict("records"):
        if _trade_date(source.get("date")) != trade_day:
            continue
        contract = str(source.get("symbol") or "").upper().strip()
        if exchange == "CZCE":
            # The official daily adapter returns every listed CZCE product,
            # including products intentionally outside this Agent's exact
            # configured product scope (for example CY). Filter those rows by their
            # provider prefix before asking the strict in-scope canonicalizer
            # to validate a delivery month.  An invalid code for an allowed
            # product still fails closed below.
            if product_prefix(contract) not in expected_products:
                continue
            contract = _resolve_czce_contract(contract, trade_day)
        product = _product_from_contract(contract)
        if product not in expected_products:
            continue
        row = {
            "date": trade_day.isoformat(),
            "product": product,
            "exchange": exchange,
            "contract": contract,
            "open": source.get("open"),
            "high": source.get("high"),
            "low": source.get("low"),
            "close": source.get("close"),
            "settle": source.get("settle", source.get("close")),
            "volume": source.get("volume"),
            "open_interest": source.get("open_interest"),
        }
        status = classify_bar(row)
        if status != "valid_trading_bar":
            excluded.append(
                {
                    "date": trade_day.isoformat(),
                    "exchange": exchange,
                    "product": product,
                    "contract": contract,
                    "status": status,
                    "volume": _number(row.get("volume")),
                    "open_interest": _number(row.get("open_interest")),
                }
            )
            continue
        rows.append(row)
    if skipped_rows is not None:
        skipped_rows.extend(excluded)
    observed = {row["product"] for row in rows}
    if observed != expected_products:
        missing = sorted(expected_products - observed)
        unexpected = sorted(observed - expected_products)
        raise RefreshError(
            f"{exchange} {trade_day.isoformat()} 品种覆盖不完整；"
            f"缺失={missing}，越界={unexpected}"
        )
    max_valid_oi = {
        product: max(
            float(row["open_interest"])
            for row in rows
            if row["product"] == product
        )
        for product in observed
    }
    possible_missing_main = [
        item
        for item in excluded
        if (
            item["open_interest"] is None
            or float(item["open_interest"]) >= max_valid_oi[str(item["product"])]
        )
    ]
    if possible_missing_main:
        contracts = [str(item["contract"]) for item in possible_missing_main]
        raise RefreshError(
            f"{exchange} {trade_day.isoformat()} 可能主力的实际合约 bar 无效：{contracts}"
        )
    return _deduplicate(rows)


def fetch_official_day(
    exchange: str,
    trade_day: date,
    expected_products: set[str],
    skipped_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    try:
        frame = ak.get_futures_daily(
            start_date=trade_day.strftime("%Y%m%d"),
            end_date=trade_day.strftime("%Y%m%d"),
            market=exchange,
        )
    except Exception as exc:
        raise RefreshError(
            f"{exchange} {trade_day.isoformat()} 官方日线获取失败：{exc}"
        ) from exc
    return normalize_exchange_frame(
        frame,
        exchange=exchange,
        trade_day=trade_day,
        expected_products=expected_products,
        skipped_rows=skipped_rows,
    )


def _fetch_sina_contract_history(
    contract: str,
    wanted_days: set[date],
    currently_listed: bool = True,
) -> tuple[
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    if not wanted_days:
        raise RefreshError(f"新浪实际合约日线缺少请求日期：{contract}")
    try:
        source_rows, identity_evidence = fetch_bound_contract_history(
            contract,
            completed_through=max(wanted_days),
            timeout=20,
        )
    except DceDailyIdentityError as exc:
        raise RefreshError(f"新浪实际合约日线来源身份不可验证：{contract}：{exc}") from exc
    frame = pd.DataFrame(source_rows)
    required = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "settle",
        "volume",
        "open_interest",
    }
    if frame.empty or not required.issubset(frame.columns):
        raise RefreshError(f"新浪实际合约日线为空或字段不完整：{contract}")
    values = []
    skipped: list[dict[str, Any]] = []
    observed_dates = sorted(
        {
            parsed
            for value in frame["date"].tolist()
            if (parsed := _trade_date(value)) is not None
        }
    )
    if not observed_dates:
        raise RefreshError(f"新浪实际合约日线没有可解析日期：{contract}")
    first_returned = observed_dates[0]
    last_returned = observed_dates[-1]
    for source in frame.to_dict("records"):
        trade_day = _trade_date(source.get("date"))
        if trade_day not in wanted_days:
            continue
        row = {
            "date": trade_day.isoformat(),
            "exchange": "DCE",
            "contract": contract,
            "open": source.get("open"),
            "high": source.get("high"),
            "low": source.get("low"),
            "close": source.get("close"),
            "settle": source.get("settle", source.get("close")),
            "volume": source.get("volume"),
            "open_interest": source.get("open_interest"),
        }
        status = classify_bar(row)
        if status != "valid_trading_bar":
            skipped.append(
                {
                    "date": trade_day.isoformat(),
                    "exchange": "DCE",
                    "product": _product_from_contract(contract),
                    "contract": contract,
                    "status": status,
                    "volume": _number(row.get("volume")),
                    "open_interest": _number(row.get("open_interest")),
                }
            )
            continue
        values.append(row)
    represented_days = {
        date.fromisoformat(str(item["date"])) for item in [*values, *skipped]
    }
    for missing_day in sorted(wanted_days - represented_days):
        # Endpoint first/last-seen dates are not independent listing or expiry
        # evidence.  A missing requested day therefore remains unknown even if
        # it sits outside the returned range or the contract is absent from the
        # latest chain; otherwise an upstream omission could erase the true OI
        # leader.  A future lifecycle exemption must bind an official dated
        # contract catalog, not infer it from these same response bytes.
        status = "missing_contract_day_row"
        skipped.append(
            {
                "date": missing_day.isoformat(),
                "exchange": "DCE",
                "product": _product_from_contract(contract),
                "contract": contract,
                "status": status,
                "volume": None,
                "open_interest": None,
                "endpoint_first_returned_date": first_returned.isoformat(),
                "endpoint_last_returned_date": last_returned.isoformat(),
                "currently_listed_in_latest_chain": currently_listed,
                "official_lifecycle_evidence": None,
            }
        )
    return contract, values, skipped, identity_evidence


def fetch_dce_days(
    trade_days: list[date],
    *,
    products: list[str],
    workers: int,
    baseline_contracts: dict[str, list[str]] | None = None,
    skipped_rows: list[dict[str, Any]] | None = None,
    source_identity_evidence: list[dict[str, Any]] | None = None,
) -> dict[date, list[dict[str, Any]]]:
    wanted_days = set(trade_days)
    contract_products: dict[str, str] = {}
    current_contracts: set[str] = set()
    for product in products:
        prior = [] if baseline_contracts is None else baseline_contracts.get(product, [])
        if baseline_contracts is not None and not prior:
            raise RefreshError(f"DCE {product} 缺少上一发布日实际合约枚举基线")
        for contract in prior:
            normalized = str(contract).upper().strip()
            if _product_from_contract(normalized) != product:
                raise RefreshError(f"DCE {product} 历史合约枚举含越界合约：{contract}")
            contract_products[normalized] = product
    for product in products:
        try:
            quote = direct_quote(product, timeout=20)
            chain = actual_contract_rows(str(quote["product_name"]), product)
        except Exception as exc:
            raise RefreshError(f"DCE {product} 实际合约链获取失败：{exc}") from exc
        if not chain:
            raise RefreshError(f"DCE {product} 实际合约链为空")
        for row in chain:
            if not isinstance(row, dict) or row.get("exchange") != "DCE":
                raise RefreshError(
                    f"DCE {product} 实际合约链exchange必须为DCE"
                )
            recomputed_exchange_identity = provider_exchange_identity(
                "DCE", row.get("provider_exchange")
            )
            if not recomputed_exchange_identity.startswith("verified_"):
                raise RefreshError(
                    f"DCE {product} 实际合约链provider_exchange身份不可验证："
                    f"{recomputed_exchange_identity}"
                )
            if row.get("exchange_identity_status") != recomputed_exchange_identity:
                raise RefreshError(
                    f"DCE {product} 实际合约链exchange_identity_status"
                    "与重算不一致"
                )
            contract = str(row.get("symbol") or "").upper().strip()
            if _product_from_contract(contract) != product:
                raise RefreshError(f"DCE {product} 合约链含越界合约：{contract}")
            contract_products[contract] = product
            current_contracts.add(contract)

    histories: dict[str, list[dict[str, Any]]] = {}
    collected_skipped: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                _fetch_sina_contract_history,
                contract,
                wanted_days,
                contract in current_contracts,
            ): contract
            for contract in sorted(contract_products)
        }
        for future in as_completed(futures):
            contract = futures[future]
            try:
                key, values, skipped, identity_evidence = future.result()
            except Exception as exc:
                raise RefreshError(f"DCE 新浪实际合约日线获取失败：{contract}：{exc}") from exc
            try:
                replayed_source_rows = replay_contract_evidence(identity_evidence)
            except DceDailyIdentityError as exc:
                raise RefreshError(
                    f"DCE 新浪实际合约日线身份归档不可重放：{contract}：{exc}"
                ) from exc
            if key != contract or identity_evidence.get("contract") != contract:
                raise RefreshError(
                    f"DCE 新浪实际合约日线身份归档合约冲突：{contract}"
                )
            expected_values: list[dict[str, Any]] = []
            for source in replayed_source_rows:
                source_day = _trade_date(source.get("date"))
                if source_day not in wanted_days:
                    continue
                projected = {
                    "date": source_day.isoformat(),
                    "exchange": "DCE",
                    "contract": contract,
                    "open": source.get("open"),
                    "high": source.get("high"),
                    "low": source.get("low"),
                    "close": source.get("close"),
                    "settle": source.get("settle"),
                    "volume": source.get("volume"),
                    "open_interest": source.get("open_interest"),
                }
                if classify_bar(projected) == "valid_trading_bar":
                    expected_values.append(projected)
            if values != expected_values:
                raise RefreshError(
                    f"DCE {contract} 新浪日线行与来源身份归档不一致，不得重标发布"
                )
            histories[key] = values
            collected_skipped.extend(skipped)
            if source_identity_evidence is not None:
                source_identity_evidence.append(identity_evidence)
            if skipped_rows is not None:
                skipped_rows.extend(skipped)

    represented_contract_days = {
        (contract, date.fromisoformat(str(item["date"])))
        for contract, values in histories.items()
        for item in values
    } | {
        (str(item.get("contract")), date.fromisoformat(str(item["date"])))
        for item in collected_skipped
        if item.get("contract") is not None and item.get("date") is not None
    }
    missing_contract_days = [
        f"{contract}@{trade_day.isoformat()}"
        for contract in sorted(contract_products)
        for trade_day in sorted(wanted_days)
        if (contract, trade_day) not in represented_contract_days
    ]
    if missing_contract_days:
        raise RefreshError(
            "DCE 实际合约历史返回未覆盖且未显式解释的contract×date："
            f"{missing_contract_days[:20]}"
        )

    by_day: dict[date, list[dict[str, Any]]] = {day: [] for day in trade_days}
    for contract, values in histories.items():
        product = contract_products[contract]
        for source in values:
            trade_day = date.fromisoformat(str(source["date"]))
            if source.get("exchange") != "DCE":
                raise RefreshError(
                    f"DCE {contract} 新浪日线源exchange缺失或冲突，不得重标发布"
                )
            row = {
                **source,
                "product": product,
            }
            if _valid_bar(row):
                by_day[trade_day].append(row)

    expected = set(products)
    for trade_day, rows in by_day.items():
        observed = {row["product"] for row in rows}
        if observed != expected:
            raise RefreshError(
                f"DCE {trade_day.isoformat()} 新浪降级日线品种覆盖不完整；"
                f"缺失={sorted(expected - observed)}"
            )
        max_valid_oi = {
            product: max(
                float(row["open_interest"])
                for row in rows
                if row["product"] == product
            )
            for product in observed
        }
        possible_missing_main = [
            item
            for item in collected_skipped
            if item.get("date") == trade_day.isoformat()
            and item.get("product") in observed
            and item.get("status") == "missing_contract_day_row"
            and (
                item.get("open_interest") is None
                or float(item["open_interest"])
                >= max_valid_oi[str(item["product"])]
            )
        ]
        if possible_missing_main:
            raise RefreshError(
                f"DCE {trade_day.isoformat()} 可能主力的实际合约 bar 无效："
                f"{[item['contract'] for item in possible_missing_main]}"
            )
        by_day[trade_day] = _deduplicate(rows)
    return by_day


def _deduplicate(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["date"]), str(row["product"]), str(row["contract"]))
        if key in result and result[key] != row:
            raise RefreshError(f"同一实际合约日线出现冲突重复：{'/'.join(key)}")
        result[key] = row
    return [result[key] for key in sorted(result)]


def read_daily(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, compression="gzip", dtype={"date": str, "product": str, "contract": str})
    except (OSError, ValueError) as exc:
        raise RefreshError(f"无法读取日线缓存：{path}") from exc
    missing = set(CSV_FIELDS) - set(frame.columns)
    if frame.empty or missing:
        raise RefreshError(f"日线缓存为空或缺少字段：{sorted(missing)}")
    frame = frame.loc[:, CSV_FIELDS].copy()
    parsed = pd.to_datetime(frame["date"], errors="coerce")
    if parsed.isna().any():
        raise RefreshError("日线缓存含不可解析日期")
    frame["date"] = parsed.dt.strftime("%Y-%m-%d")
    if frame.duplicated(["date", "product", "contract"]).any():
        raise RefreshError("日线缓存含重复 actual-contract key")
    contract_parts = frame["contract"].astype(str).str.extract(
        r"^([A-Z]+)(\d{4})$", expand=True
    )
    contract_months = pd.to_numeric(contract_parts[1].str[-2:], errors="coerce")
    invalid_contract = contract_parts[0].isna() | ~contract_months.between(1, 12)
    if invalid_contract.any():
        raise RefreshError("日线缓存含非法实际合约代码")
    numeric = frame.loc[
        :, ["open", "high", "low", "close", "settle", "volume", "open_interest"]
    ].apply(pd.to_numeric, errors="coerce")
    invalid_values = (
        numeric.isna().any(axis=1)
        | (numeric[["open", "high", "low", "close", "settle"]] <= 0).any(axis=1)
        | (numeric["volume"] <= 0)
        | (numeric["open_interest"] < 0)
        | (numeric["high"] < numeric[["open", "low", "close"]].max(axis=1))
        | (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1))
    )
    if bool(invalid_values.any()):
        raise RefreshError("日线缓存含无效OHLC、结算价或量仓")
    return frame


def verify_existing_baseline(
    daily_path: Path,
    manifest_path: Path,
    universe: list[dict[str, str]],
) -> pd.DataFrame:
    """Verify the immutable cache baseline before appending and re-signing it."""

    manifest = _read_json_object(manifest_path, "日线manifest")
    if manifest.get("schema_version") != "decision_agent_daily_history_latest.v1":
        raise RefreshError("旧日线manifest schema不受支持")
    try:
        resolved_daily_path = _resolve_manifest_daily_path(daily_path, manifest)
    except DailyCacheVerificationError as exc:
        raise RefreshError(str(exc)) from exc
    declared_hash = manifest.get("sha256")
    if not isinstance(declared_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
        raise RefreshError("旧日线manifest.sha256缺失或格式错误")
    if sha256_file(resolved_daily_path) != declared_hash:
        raise RefreshError("旧日线缓存SHA-256与manifest不一致")

    frame = read_daily(resolved_daily_path)
    if isinstance(manifest.get("rows"), bool) or manifest.get("rows") != len(frame):
        raise RefreshError("旧日线manifest.rows与缓存行数不一致")
    declared_coverage_hash = manifest.get("coverage_sha256")
    if (
        not isinstance(declared_coverage_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", declared_coverage_hash)
        or declared_coverage_hash != coverage_sha256(frame)
    ):
        raise RefreshError("旧日线manifest.coverage_sha256与日期品种覆盖不一致")
    normalized_universe = {
        str(item.get("symbol") or "").strip().upper(): str(
            item.get("exchange") or ""
        ).strip().upper()
        for item in universe
        if isinstance(item, dict)
    }
    if (
        len(normalized_universe) != len(universe)
        or "" in normalized_universe
        or "" in normalized_universe.values()
    ):
        raise RefreshError("旧日线校验收到空白或重复的交易范围")
    if manifest.get("product_count") != len(universe):
        raise RefreshError("旧日线manifest.product_count与交易范围不一致")
    configured_scope = {
        item["symbol"]: item["exchange"] for item in _metadata()
    }
    production_scope = normalized_universe == configured_scope
    if production_scope and manifest.get("universe_sha256") != sha256_file(
        UNIVERSE_PATH
    ):
        raise RefreshError("旧日线manifest.universe_sha256与当前配置品种范围不一致")
    boundary = manifest.get("claim_boundary")
    if not isinstance(boundary, dict) or any(
        boundary.get(field) is not True
        for field in ("completed_daily_bars_only", "actual_month_contracts_only")
    ):
        raise RefreshError("旧日线manifest未声明完成日线与实际月份边界")

    expected_exchange = normalized_universe
    products = frame["product"].astype(str).str.upper()
    exchanges = frame["exchange"].astype(str).str.upper()
    contracts = frame["contract"].astype(str).str.upper()
    if set(products) != set(expected_exchange):
        raise RefreshError("旧日线缓存品种范围与当前配置品种清单不一致")
    contract_parts = contracts.str.extract(r"^([A-Z]+)(\d{4})$", expand=True)
    contract_products = contract_parts[0]
    contract_months = pd.to_numeric(contract_parts[1].str[-2:], errors="coerce")
    if (
        contract_products.isna().any()
        or contract_months.isna().any()
        or not bool(contract_months.between(1, 12).all())
        or not bool((contract_products == products).all())
    ):
        raise RefreshError("旧日线缓存合约与品种字段不一致")
    expected_exchanges = products.map(expected_exchange)
    if expected_exchanges.isna().any() or not bool((expected_exchanges == exchanges).all()):
        raise RefreshError("旧日线缓存交易所与品种配置不一致")

    numeric = frame.loc[:, ["open", "high", "low", "close", "settle", "volume", "open_interest"]].apply(
        pd.to_numeric, errors="coerce"
    )
    invalid = (
        numeric.isna().any(axis=1)
        | (numeric[["open", "high", "low", "close", "settle"]] <= 0).any(axis=1)
        | (numeric["volume"] <= 0)
        | (numeric["open_interest"] < 0)
        | (numeric["high"] < numeric[["open", "low", "close"]].max(axis=1))
        | (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1))
    )
    if bool(invalid.any()):
        raise RefreshError("旧日线缓存含无效OHLC、结算价或量仓")

    expected_products = set(expected_exchange)
    scoped = frame.assign(_product=products)
    first_seen = scoped.groupby("_product")["date"].min().to_dict()
    # Products listed after the cache start cannot exist on older dates.  Once
    # every currently allowed product has appeared, however, each subsequently
    # published date must preserve the full configured-product gate.
    full_scope_start = max(str(value) for value in first_seen.values())
    coverage_scope = manifest.get("coverage_scope")
    if production_scope:
        try:
            pinned_full_scope_start, _ = _verify_immutable_anchor(
                frame,
                manifest=manifest,
                universe_path=UNIVERSE_PATH,
            )
        except DailyCacheVerificationError as exc:
            raise RefreshError(str(exc)) from exc
        if (
            not isinstance(coverage_scope, dict)
            or coverage_scope.get("full_product_scope_start")
            != pinned_full_scope_start
        ):
            raise RefreshError(
                "旧日线manifest.coverage_scope未绑定当前配置品种完整覆盖起点"
            )
        if full_scope_start > pinned_full_scope_start:
            raise RefreshError("旧日线完整品种覆盖起点晚于独立策略")
        full_scope_start = pinned_full_scope_start
    for trade_day, day_rows in scoped[scoped["date"] >= full_scope_start].groupby("date"):
        observed = set(day_rows["_product"])
        if observed != expected_products:
            raise RefreshError(
                f"旧日线缓存 {trade_day} 未完整覆盖配置品种；"
                f"缺失={sorted(expected_products - observed)}"
            )

    date_range = manifest.get("date_range")
    if not isinstance(date_range, dict) or date_range != {
        "start": str(frame["date"].min()),
        "end": str(frame["date"].max()),
    }:
        raise RefreshError("旧日线manifest.date_range与缓存实际范围不一致")
    return frame


def _trading_days_after(last_day: date, through: date) -> list[date]:
    known = set(AKSHARE_TRADE_DATES)
    values: list[date] = []
    cursor = last_day + timedelta(days=1)
    while cursor <= through:
        if cursor.strftime("%Y%m%d") in known:
            values.append(cursor)
        cursor += timedelta(days=1)
    return values


def default_completed_through(observed_at: datetime | None = None) -> date:
    """Return the last completed daily session for the refresh default.

    A wall-clock calendar date is not necessarily a completed bar.  In
    particular, requesting Monday while the day session is still open turns an
    already current Friday cache into a false provider failure.
    """

    evidence = infer_completed_trade_date(observed_at or datetime.now(BEIJING))
    return date.fromisoformat(str(evidence["date"]))


def _write_daily_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _write_deterministic_gzip_csv(frame, temporary)
        # Re-read the derived artifact before publication.
        checked = read_daily(temporary)
        if len(checked) != len(frame):
            raise RefreshError("写入后的日线缓存行数不一致")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_deterministic_gzip_csv(frame: pd.DataFrame, path: Path) -> None:
    """Write stable gzip bytes without leaking the temporary filename header."""

    with path.open("wb") as raw_stream:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw_stream,
            mtime=0,
        ) as compressed_stream:
            with io.TextIOWrapper(
                compressed_stream,
                encoding="utf-8",
                newline="",
            ) as text_stream:
                frame.to_csv(
                    text_stream,
                    index=False,
                    columns=CSV_FIELDS,
                    float_format="%.10g",
                )


def _write_daily_content_addressed(
    frame: pd.DataFrame, pointer_path: Path
) -> tuple[Path, str]:
    """Publish an immutable gzip object; the manifest is the sole latest pointer."""

    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{pointer_path.name}.", suffix=".tmp", dir=pointer_path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _write_deterministic_gzip_csv(frame, temporary)
        checked = read_daily(temporary)
        if len(checked) != len(frame):
            raise RefreshError("写入后的日线缓存行数不一致")
        digest = sha256_file(temporary)
        base = pointer_path.name
        if base.endswith(".csv.gz"):
            base = base[: -len(".csv.gz")]
        target = pointer_path.with_name(f"{base}.sha256-{digest}.csv.gz")
        if target.exists():
            if sha256_file(target) != digest:
                raise RefreshError("内容寻址日线对象存在但哈希冲突")
        else:
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        return target, digest
    finally:
        temporary.unlink(missing_ok=True)


def _write_dce_identity_content_addressed(
    bundle: dict[str, Any], directory: Path
) -> tuple[Path, str]:
    """Durably publish one replayable DCE response-evidence object."""

    try:
        replay_identity_bundle(bundle)
    except DceDailyIdentityError as exc:
        raise RefreshError(f"DCE 来源身份归档发布前重放失败：{exc}") from exc
    content = (
        json.dumps(
            bundle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"dce_daily_identity.sha256-{digest}.json"
    if target.exists():
        if sha256_file(target) != digest:
            raise RefreshError("内容寻址DCE来源身份对象存在但哈希冲突")
        return target, digest
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".dce_daily_identity.", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            persisted = json.loads(temporary.read_text(encoding="utf-8"))
            replay_identity_bundle(persisted)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, DceDailyIdentityError) as exc:
            raise RefreshError("写入后的DCE来源身份对象不可重放") from exc
        if sha256_file(temporary) != digest:
            raise RefreshError("写入后的DCE来源身份对象SHA-256不一致")
        os.replace(temporary, target)
        _fsync_directory(directory)
        return target, digest
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        json.loads(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _refresh_unlocked(
    *,
    through: date,
    daily_path: Path = DAILY_PATH,
    manifest_path: Path = MANIFEST_PATH,
    workers: int = 8,
    dry_run: bool = False,
) -> dict[str, Any]:
    latest_completed = default_completed_through()
    if through > latest_completed:
        raise RefreshError(
            f"through={through.isoformat()} 晚于当前可确认完成交易日"
            f" {latest_completed.isoformat()}；不得发布进行中日线"
        )
    universe = _metadata()
    by_exchange = {
        exchange: {item["symbol"] for item in universe if item["exchange"] == exchange}
        for exchange in {*OFFICIAL_MARKETS, "DCE"}
    }
    if not manifest_path.is_file():
        raise RefreshError(
            f"latest 日线manifest不存在，无法在缺少基线时刷新：{manifest_path}"
        )
    frame = verify_existing_baseline(daily_path, manifest_path, universe)
    baseline_manifest = _read_json_object(manifest_path, "日线manifest")
    baseline_last_day = max(
        date.fromisoformat(value) for value in frame["date"].unique()
    )
    try:
        baseline_identity_rows, baseline_identity_status = (
            load_manifest_identity_rows(
                baseline_manifest,
                artifact_directory=manifest_path.parent,
            )
        )
        baseline_identity_index = baseline_manifest.get("dce_source_identity")
        if isinstance(baseline_identity_index, dict):
            baseline_identity_boundary = str(
                baseline_identity_index["legacy_unverified_through"]
            )
            baseline_dce_rows = frame.loc[
                (frame["exchange"].astype(str).str.upper() == "DCE")
                & (frame["date"].astype(str) > baseline_identity_boundary)
            ].to_dict("records")
            if baseline_dce_rows:
                verify_identity_rows_cover_cache_rows(
                    baseline_identity_rows,
                    baseline_dce_rows,
                    require_exact_key_set=True,
                )
            elif baseline_identity_rows:
                raise DceDailyIdentityError(
                    "DCE identity rows exist without post-legacy cache rows"
                )
    except DceDailyIdentityError as exc:
        raise RefreshError(f"旧DCE来源身份归档不可验证：{exc}") from exc
    if isinstance(baseline_identity_index, dict):
        legacy_unverified_through = str(
            baseline_identity_index["legacy_unverified_through"]
        )
        prior_identity_descriptors = list(
            baseline_identity_index["evidence_objects"]
        )
    else:
        # The legacy manifest attests the cache object's bytes only.  It did
        # not archive each symbol-less Sina response or bind its contract.
        legacy_unverified_through = baseline_last_day.isoformat()
        prior_identity_descriptors = []
    last_day = baseline_last_day
    candidate_days = _trading_days_after(last_day, through)
    fetched_by_day: dict[date, list[dict[str, Any]]] = {}
    skipped_rows: list[dict[str, Any]] = []
    dce_identity_entries: list[dict[str, Any]] = []
    dce_identity_bundle: dict[str, Any] | None = None

    # Stop at the first official date that is not yet fully published.  This is
    # the normal pre-close path and also prevents jumping over a provider gap.
    publishable_days: list[date] = []
    unavailable_reason: str | None = None
    for trade_day in candidate_days:
        rows: list[dict[str, Any]] = []
        try:
            for exchange in OFFICIAL_MARKETS:
                rows.extend(
                    fetch_official_day(
                        exchange,
                        trade_day,
                        by_exchange[exchange],
                        skipped_rows,
                    )
                )
        except RefreshError as exc:
            unavailable_reason = str(exc)
            break
        fetched_by_day[trade_day] = rows
        publishable_days.append(trade_day)

    if publishable_days:
        dce_rows = fetch_dce_days(
            publishable_days,
            products=sorted(by_exchange["DCE"]),
            workers=workers,
            baseline_contracts={
                product: sorted(
                    set(
                        frame.loc[
                            (frame["date"] == last_day.isoformat())
                            & (frame["exchange"] == "DCE")
                            & (frame["product"] == product),
                            "contract",
                        ].astype(str)
                    )
                )
                for product in sorted(by_exchange["DCE"])
            },
            skipped_rows=skipped_rows,
            source_identity_evidence=dce_identity_entries,
        )
        for trade_day in publishable_days:
            rows = _deduplicate([*fetched_by_day[trade_day], *dce_rows[trade_day]])
            observed = {row["product"] for row in rows}
            expected = {item["symbol"] for item in universe}
            if observed != expected:
                raise RefreshError(
                    f"{trade_day.isoformat()} 配置品种发布门未通过；"
                    f"缺失={sorted(expected - observed)}，越界={sorted(observed - expected)}"
                )
            fetched_by_day[trade_day] = rows

        additions = pd.DataFrame(
            [row for trade_day in publishable_days for row in fetched_by_day[trade_day]],
            columns=CSV_FIELDS,
        )
        try:
            dce_identity_bundle = build_identity_bundle(
                dce_identity_entries,
                published_dates=publishable_days,
            )
            verify_bundle_covers_cache_rows(
                dce_identity_bundle,
                additions.loc[
                    additions["exchange"].astype(str).str.upper() == "DCE"
                ].to_dict("records"),
            )
        except DceDailyIdentityError as exc:
            raise RefreshError(
                f"DCE symbol-less日线与交叉端点身份归档无法闭环：{exc}"
            ) from exc
        frame = pd.concat([frame, additions], ignore_index=True)
        frame = frame.sort_values(["date", "product", "contract"], kind="stable")
        if frame.duplicated(["date", "product", "contract"]).any():
            raise RefreshError("合并后的日线缓存含重复 actual-contract key")
        last_day = publishable_days[-1]

    status = (
        "partially_published_blocked"
        if unavailable_reason is not None and publishable_days
        else "blocked"
        if unavailable_reason is not None
        else "validated_dry_run"
        if dry_run and publishable_days
        else "published"
        if publishable_days
        else "up_to_date"
    )
    skipped_status_counts: dict[str, int] = {}
    for item in skipped_rows:
        label = str(item["status"])
        skipped_status_counts[label] = skipped_status_counts.get(label, 0) + 1
    result = {
        "status": status,
        "daily_path": str(daily_path),
        "last_complete_trade_date": last_day.isoformat(),
        "published_dates": [value.isoformat() for value in publishable_days],
        "product_count": len(universe),
        "row_count": int(len(frame)),
        "unavailable_reason": unavailable_reason,
        "excluded_non_trading_rows": {
            "count": len(skipped_rows),
            "by_status": skipped_status_counts,
            "rows": skipped_rows,
        },
        "dce_source_identity": {
            "artifact_integrity_status": "verified",
            "new_contract_identity_status": (
                "validated_cross_endpoint_response_archives"
                if dce_identity_bundle is not None
                else "not_applicable_no_new_rows"
            ),
            "baseline_identity_status": baseline_identity_status,
            "legacy_unverified_through": legacy_unverified_through,
            "new_response_count": len(dce_identity_entries),
            "claim_boundary": "sina_non_official_cross_endpoint_identity",
        },
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    if not publishable_days:
        # Preserve both data and manifest when the next expected date is
        # blocked; an unchanged cache must not receive a misleading new
        # ``updated_at`` timestamp.
        return result
    if dce_identity_bundle is None:
        raise RefreshError("待发布DCE日线缺少来源身份归档")
    identity_path, identity_hash = _write_dce_identity_content_addressed(
        dce_identity_bundle,
        manifest_path.parent,
    )
    try:
        new_identity_descriptor = descriptor_for_bundle(
            file=identity_path.name,
            sha256=identity_hash,
            bundle=dce_identity_bundle,
        )
    except DceDailyIdentityError as exc:
        raise RefreshError(f"DCE来源身份对象描述符无效：{exc}") from exc
    published_path, published_hash = _write_daily_content_addressed(frame, daily_path)
    persisted_frame = read_daily(published_path)
    try:
        verify_bundle_covers_cache_rows(
            dce_identity_bundle,
            persisted_frame.loc[
                (persisted_frame["exchange"].astype(str).str.upper() == "DCE")
                & persisted_frame["date"].astype(str).isin(
                    {value.isoformat() for value in publishable_days}
                )
            ].to_dict("records"),
        )
    except DceDailyIdentityError as exc:
        raise RefreshError(
            f"持久化DCE日线与来源身份归档不一致；manifest未切换：{exc}"
        ) from exc
    manifest = {
        "schema_version": "decision_agent_daily_history_latest.v1",
        "file": published_path.name,
        "sha256": published_hash,
        "rows": int(len(frame)),
        "coverage_sha256": coverage_sha256(frame),
        "universe_sha256": sha256_file(UNIVERSE_PATH),
        "integrity_policy_sha256": sha256_file(INTEGRITY_POLICY_PATH),
        "post_anchor_calendar_sha256": _post_anchor_calendar_sha256(
            _post_anchor_trade_dates(_integrity_anchor_end(), last_day.isoformat())
        ),
        "coverage_scope": {
            "full_product_scope_start": max(
                frame.assign(_product=frame["product"].astype(str).str.upper())
                .groupby("_product")["date"]
                .min()
                .astype(str)
            ),
            "pre_full_scope_policy": (
                "allow_historical_listed_subset_bound_by_coverage_sha256"
            ),
        },
        "product_count": len(universe),
        "date_range": {
            "start": str(frame["date"].min()),
            "end": last_day.isoformat(),
        },
        "updated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
        "source_lineage": {
            "SHFE": "AKShare exchange official daily adapter",
            "INE": "AKShare exchange official daily adapter",
            "CZCE": "AKShare exchange official daily adapter",
            "GFEX": "AKShare exchange official daily adapter",
            "DCE": (
                "Sina actual-contract daily fallback; non-official; "
                "symbol-less history bound by archived identity-bearing "
                "cross-endpoint terminal OHLCV/OI"
            ),
        },
        "dce_source_identity": {
            "schema_version": DCE_IDENTITY_INDEX_SCHEMA,
            "legacy_unverified_through": legacy_unverified_through,
            "evidence_objects": [
                *prior_identity_descriptors,
                new_identity_descriptor,
            ],
        },
        "publication_gate": "every published date covers all configured products",
        "claim_boundary": {
            "completed_daily_bars_only": True,
            "actual_month_contracts_only": True,
            "all_exchange_rows_officially_sourced": False,
            "live_execution_quote": False,
        },
        "excluded_non_trading_rows": {
            "count": len(skipped_rows),
            "by_status": skipped_status_counts,
        },
    }
    _write_json_atomic(manifest, manifest_path)
    result["daily_path"] = str(published_path)
    return result


def refresh(
    *,
    through: date,
    daily_path: Path = DAILY_PATH,
    manifest_path: Path = MANIFEST_PATH,
    workers: int = 8,
    dry_run: bool = False,
) -> dict[str, Any]:
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = daily_path.with_name(f".{daily_path.name}.refresh.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _refresh_unlocked(
            through=through,
            daily_path=daily_path,
            manifest_path=manifest_path,
            workers=workers,
            dry_run=dry_run,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="刷新配置品种的已完成实际合约日线")
    parser.add_argument("--through", type=date.fromisoformat, default=None)
    parser.add_argument("--daily", type=Path, default=DAILY_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=600,
        help="完整刷新子进程总时限；超时保留旧缓存并返回blocked",
    )
    parser.add_argument("--_supervised-worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def _run_refresh_worker(args: argparse.Namespace) -> int:
    through = args.through or default_completed_through()
    try:
        result = refresh(
            through=through,
            daily_path=args.daily,
            manifest_path=args.manifest,
            workers=args.workers,
            dry_run=args.dry_run,
        )
    except RefreshError as exc:
        result = {
            "status": "blocked",
            "daily_path": str(args.daily),
            "manifest_path": str(args.manifest),
            "through": through.isoformat(),
            "unavailable_reason": str(exc),
            "dry_run": args.dry_run,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 2 if result["status"] in {"blocked", "partially_published_blocked"} else 0


def _run_supervised(args: argparse.Namespace) -> int:
    through = args.through or default_completed_through()
    deadline = max(1, int(args.deadline_seconds))
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--through",
        through.isoformat(),
        "--daily",
        str(args.daily),
        "--manifest",
        str(args.manifest),
        "--workers",
        str(max(1, int(args.workers))),
        "--deadline-seconds",
        str(deadline),
        "--_supervised-worker",
    ]
    if args.dry_run:
        command.append("--dry-run")
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=deadline,
        )
    except subprocess.TimeoutExpired:
        result = {
            "status": "blocked",
            "daily_path": str(args.daily),
            "manifest_path": str(args.manifest),
            "through": through.isoformat(),
            "unavailable_reason": (
                f"完整刷新超过 {deadline} 秒总时限；子进程已终止，旧缓存保持不变"
            ),
            "dry_run": args.dry_run,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 2
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


def main() -> int:
    args = parse_args()
    # Tests and library callers that provide the pre-supervision Namespace keep
    # the direct worker path; the real CLI always has this parsed flag.
    if getattr(args, "_supervised_worker", True):
        return _run_refresh_worker(args)
    return _run_supervised(args)


if __name__ == "__main__":
    raise SystemExit(main())

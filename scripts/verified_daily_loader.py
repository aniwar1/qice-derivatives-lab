#!/usr/bin/env python3
"""Load a contract slice from the published configured-universe daily cache.

The cache is an evidence artifact, not merely a convenient CSV.  This module
verifies the manifest, content hash, declared range and product scope before a
caller may label any returned bar as cache-verified.  It deliberately contains
no trading decision logic.
"""

from __future__ import annotations

import base64
import binascii
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

import pandas as pd
from akshare.futures.futures_daily_bar import calendar as AKSHARE_TRADE_DATES

try:
    from scripts.dce_daily_identity import (
        DceDailyIdentityError,
        load_manifest_identity_rows,
        verify_identity_rows_cover_cache_rows,
    )
except ModuleNotFoundError:  # Direct execution puts scripts/ on sys.path.
    from dce_daily_identity import (
        DceDailyIdentityError,
        load_manifest_identity_rows,
        verify_identity_rows_cover_cache_rows,
    )


PROJECT = Path(__file__).resolve().parents[1]
DAILY_PATH = PROJECT / "local-data/market-history/daily/contract_daily_latest.csv.gz"
MANIFEST_PATH = PROJECT / "local-data/market-history/daily/latest_manifest.json"
UNIVERSE_PATH = PROJECT / "config/trading_universe.json"
INTEGRITY_POLICY_PATH = PROJECT / "config/daily_history_integrity.json"
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
SUPPORTED_SCHEMA_VERSIONS = {"decision_agent_daily_history_latest.v1"}


class DailyCacheVerificationError(RuntimeError):
    """Raised when the local daily artifact cannot support a verified claim."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _coverage_sha256(frame: pd.DataFrame) -> str:
    pairs = (
        frame.loc[:, ["date", "product"]]
        .astype(str)
        .assign(product=lambda value: value["product"].str.upper())
        .drop_duplicates()
        .sort_values(["date", "product"], kind="stable")
        .to_dict("records")
    )
    return hashlib.sha256(
        json.dumps(
            pairs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _post_anchor_trade_dates(anchor_end: str, dataset_end: str) -> list[str]:
    return [
        f"{value[:4]}-{value[4:6]}-{value[6:]}"
        for value in sorted(set(AKSHARE_TRADE_DATES))
        if re.fullmatch(r"\d{8}", value)
        and anchor_end < f"{value[:4]}-{value[4:6]}-{value[6:]}" <= dataset_end
    ]


def _post_anchor_calendar_sha256(values: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _iso_date(value: Any, field: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise DailyCacheVerificationError(f"{field} 不是有效 ISO 日期") from exc
    return parsed


def _expected_product_exchanges(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DailyCacheVerificationError(f"无法读取交易范围：{path}") from exc
    raw = (
        payload.get("permission_scope", {}).get("allowed_products")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(raw, list):
        raise DailyCacheVerificationError("交易范围缺少 allowed_products")
    products = {
        str(item.get("symbol") or "").strip().upper(): str(
            item.get("exchange") or ""
        ).strip().upper()
        for item in raw
        if isinstance(item, dict)
    }
    if "" in products or "" in products.values() or len(products) != len(raw):
        raise DailyCacheVerificationError("交易范围含空白或重复品种")
    declared_count = payload.get("product_count")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != len(products)
    ):
        raise DailyCacheVerificationError("交易范围 product_count 与品种清单不一致")
    return products


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise DailyCacheVerificationError(f"无法读取日线 manifest：{path}") from exc
    return _parse_manifest_bytes(content, path)


def _parse_manifest_bytes(content: bytes, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DailyCacheVerificationError(f"无法解析日线 manifest：{path}") from exc
    if not isinstance(payload, dict):
        raise DailyCacheVerificationError("日线 manifest 顶层必须是对象")
    return payload


def _slice_sha256(rows: list[dict[str, Any]]) -> str:
    fields = ("date", "open", "high", "low", "close", "volume", "hold", "completed")
    normalized = [
        {field: row.get(field) for field in fields}
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _resolve_manifest_daily_path(requested_path: Path, manifest: dict[str, Any]) -> Path:
    """Resolve the manifest's immutable content object beside the latest pointer.

    Older manifests name ``requested_path`` directly.  New manifests name a
    content-addressed sibling so publishing can atomically switch only the
    manifest after the complete gzip object is durable.
    """

    declared = manifest.get("file")
    if not isinstance(declared, str) or not declared.strip():
        raise DailyCacheVerificationError("manifest.file 缺失或非法")
    declared_path = Path(declared)
    if declared_path.name != declared or declared_path.suffixes[-2:] != [".csv", ".gz"]:
        raise DailyCacheVerificationError("manifest.file 必须是同目录内的 gzip CSV 文件名")
    return requested_path if declared == requested_path.name else requested_path.parent / declared


def _verify_immutable_anchor(
    frame: pd.DataFrame,
    *,
    manifest: dict[str, Any],
    universe_path: Path,
) -> tuple[str, str]:
    """Bind all pre-refresh history to an independent, immutable anchor file."""

    try:
        policy = json.loads(INTEGRITY_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DailyCacheVerificationError("无法读取独立日线完整性策略") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != "daily_history_integrity.v1":
        raise DailyCacheVerificationError("独立日线完整性策略 schema 不受支持")
    policy_hash = _sha256_file(INTEGRITY_POLICY_PATH)
    if manifest.get("integrity_policy_sha256") != policy_hash:
        raise DailyCacheVerificationError("manifest 未绑定当前独立日线完整性策略")
    if policy.get("universe_sha256") != _sha256_file(universe_path):
        raise DailyCacheVerificationError("独立日线完整性策略未绑定当前交易范围")
    anchor = policy.get("anchor")
    if not isinstance(anchor, dict):
        raise DailyCacheVerificationError("独立日线完整性策略缺少 anchor")
    relative = anchor.get("file")
    if not isinstance(relative, str) or not relative.strip():
        raise DailyCacheVerificationError("独立日线完整性策略 anchor.file 非法")
    anchor_path = (PROJECT / relative).resolve()
    try:
        anchor_path.relative_to(PROJECT.resolve())
    except ValueError as exc:
        raise DailyCacheVerificationError("独立日线 anchor 越出项目目录") from exc
    declared_anchor_hash = anchor.get("sha256")
    if (
        not isinstance(declared_anchor_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", declared_anchor_hash)
        or _sha256_file(anchor_path) != declared_anchor_hash
    ):
        raise DailyCacheVerificationError("独立日线 anchor SHA-256 不一致")
    try:
        anchor_frame = pd.read_csv(
            anchor_path,
            compression="gzip",
            dtype={"date": str, "product": str, "exchange": str, "contract": str},
        ).loc[:, CSV_FIELDS]
    except (OSError, ValueError, KeyError) as exc:
        raise DailyCacheVerificationError("独立日线 anchor 无法解析") from exc
    _validate_prices(anchor_frame)
    if anchor.get("rows") != len(anchor_frame):
        raise DailyCacheVerificationError("独立日线 anchor 行数不一致")
    if anchor.get("coverage_sha256") != _coverage_sha256(anchor_frame):
        raise DailyCacheVerificationError("独立日线 anchor 覆盖哈希不一致")
    date_range = anchor.get("date_range")
    if not isinstance(date_range, dict):
        raise DailyCacheVerificationError("独立日线 anchor 日期范围缺失")
    anchor_start = str(date_range.get("start"))
    anchor_end = str(date_range.get("end"))
    if {
        "start": str(anchor_frame["date"].min()),
        "end": str(anchor_frame["date"].max()),
    } != {"start": anchor_start, "end": anchor_end}:
        raise DailyCacheVerificationError("独立日线 anchor 日期范围不一致")
    prefix = frame.loc[frame["date"] <= anchor_end, CSV_FIELDS].reset_index(drop=True)
    expected_prefix = anchor_frame.loc[:, CSV_FIELDS].reset_index(drop=True)
    if not prefix.equals(expected_prefix):
        raise DailyCacheVerificationError("已发布 anchor 历史被删除、改写或重排")
    dataset_end = str(frame["date"].max())
    expected_post_anchor_dates = _post_anchor_trade_dates(anchor_end, dataset_end)
    observed_post_anchor_dates = sorted(
        set(str(value) for value in frame.loc[frame["date"] > anchor_end, "date"])
    )
    if observed_post_anchor_dates != expected_post_anchor_dates:
        raise DailyCacheVerificationError(
            "anchor之后的已完成交易日不连续；存在整日缺失或非交易日"
        )
    expected_calendar_hash = _post_anchor_calendar_sha256(expected_post_anchor_dates)
    if manifest.get("post_anchor_calendar_sha256") != expected_calendar_hash:
        raise DailyCacheVerificationError("manifest未绑定anchor后的交易日日历序列")
    full_scope_start = str(policy.get("full_product_scope_start"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", full_scope_start):
        raise DailyCacheVerificationError("独立日线完整性策略的完整范围起点非法")
    return full_scope_start, declared_anchor_hash


def _validate_prices(frame: pd.DataFrame) -> None:
    for field in ("open", "high", "low", "close", "settle"):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    for field in ("volume", "open_interest"):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    invalid = (
        frame[["open", "high", "low", "close", "settle", "volume", "open_interest"]]
        .isna()
        .any(axis=1)
        | (frame[["open", "high", "low", "close", "settle"]] <= 0).any(axis=1)
        | (frame["volume"] <= 0)
        | (frame["open_interest"] < 0)
        | (frame["high"] < frame[["open", "low", "close"]].max(axis=1))
        | (frame["low"] > frame[["open", "high", "close"]].min(axis=1))
    )
    if bool(invalid.any()):
        raise DailyCacheVerificationError("日线缓存含无效 OHLC/结算价或非成交量仓行")


def load_verified_contract_history(
    contract: str,
    *,
    completed_through: date | str,
    limit: int = 320,
    daily_path: Path = DAILY_PATH,
    manifest_path: Path = MANIFEST_PATH,
    universe_path: Path = UNIVERSE_PATH,
    expected_products: Iterable[str] | None = None,
    manifest_bytes: bytes | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return completed rows only after verifying the complete cache artifact.

    ``completed_through`` is an inclusive, caller-bound evidence cutoff.  The
    loader refuses a cache whose terminal date is older than that cutoff; this
    prevents a valid but stale manifest from silently shortening an analysis.
    """

    normalized_contract = str(contract).strip().upper()
    matched = re.fullmatch(r"([A-Z]+)(\d{4})", normalized_contract)
    if matched is None or not 1 <= int(matched.group(2)[-2:]) <= 12:
        raise DailyCacheVerificationError("缓存只接受实际月份合约代码")
    if limit < 1:
        raise DailyCacheVerificationError("日线切片 limit 必须为正数")
    cutoff = _iso_date(completed_through, "completed_through")
    configured_exchanges = _expected_product_exchanges(universe_path)
    expected = (
        {str(value).strip().upper() for value in expected_products}
        if expected_products is not None
        else set(configured_exchanges)
    )
    if not expected or "" in expected:
        raise DailyCacheVerificationError("预期品种范围为空或非法")
    # Passing the configured set explicitly must not downgrade provenance into
    # the looser research-fixture path.  Scope is a property of the actual set,
    # not of which optional argument spelling the caller used.
    production_scope = expected == set(configured_exchanges)
    product = matched.group(1)
    if product not in expected:
        raise DailyCacheVerificationError(f"实际合约不在缓存品种范围：{normalized_contract}")

    if manifest_bytes is None:
        try:
            manifest_content = manifest_path.read_bytes()
        except OSError as exc:
            raise DailyCacheVerificationError(
                f"无法读取日线 manifest：{manifest_path}"
            ) from exc
    else:
        if not isinstance(manifest_bytes, bytes) or not manifest_bytes:
            raise DailyCacheVerificationError("归档日线 manifest 字节为空或非法")
        manifest_content = manifest_bytes
    manifest = _parse_manifest_bytes(manifest_content, manifest_path)
    schema_version = manifest.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DailyCacheVerificationError(f"不支持的日线 manifest schema：{schema_version}")
    resolved_daily_path = _resolve_manifest_daily_path(daily_path, manifest)
    declared_hash = manifest.get("sha256")
    if not isinstance(declared_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
        raise DailyCacheVerificationError("manifest.sha256 缺失或格式错误")
    try:
        actual_hash = _sha256_file(resolved_daily_path)
    except OSError as exc:
        raise DailyCacheVerificationError(f"无法读取日线缓存：{resolved_daily_path}") from exc
    if actual_hash != declared_hash:
        raise DailyCacheVerificationError("日线缓存 SHA-256 与 manifest 不一致")

    try:
        frame = pd.read_csv(
            resolved_daily_path,
            compression="gzip",
            dtype={"date": str, "product": str, "exchange": str, "contract": str},
        )
    except (OSError, ValueError) as exc:
        raise DailyCacheVerificationError(f"无法解析日线缓存：{daily_path}") from exc
    missing = set(CSV_FIELDS) - set(frame.columns)
    if frame.empty or missing:
        raise DailyCacheVerificationError(f"日线缓存为空或缺少字段：{sorted(missing)}")
    frame = frame.loc[:, CSV_FIELDS].copy()
    _validate_prices(frame)
    if isinstance(manifest.get("rows"), bool) or manifest.get("rows") != len(frame):
        raise DailyCacheVerificationError("manifest.rows 与日线缓存行数不一致")
    if manifest.get("coverage_sha256") != _coverage_sha256(frame):
        raise DailyCacheVerificationError(
            "manifest.coverage_sha256 与日期品种覆盖不一致"
        )
    if manifest.get("product_count") != len(expected):
        raise DailyCacheVerificationError("manifest.product_count 与交易范围不一致")
    if production_scope:
        expected_universe_sha256 = _sha256_file(universe_path)
        if manifest.get("universe_sha256") != expected_universe_sha256:
            raise DailyCacheVerificationError("manifest.universe_sha256 与当前交易范围不一致")
    claim_boundary = manifest.get("claim_boundary")
    if not isinstance(claim_boundary, dict) or any(
        claim_boundary.get(field) is not True
        for field in ("completed_daily_bars_only", "actual_month_contracts_only")
    ):
        raise DailyCacheVerificationError("manifest 未声明完成日线与实际月份合约边界")

    parsed_dates = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="coerce")
    if parsed_dates.isna().any():
        raise DailyCacheVerificationError("日线缓存含不可解析日期")
    frame["date"] = parsed_dates.dt.strftime("%Y-%m-%d")
    if frame.duplicated(["date", "product", "contract"]).any():
        raise DailyCacheVerificationError("日线缓存含重复 actual-contract key")
    observed_products = set(frame["product"].astype(str).str.upper())
    if observed_products != expected:
        raise DailyCacheVerificationError(
            "日线缓存品种范围与配置不一致；"
            f"缺失={sorted(expected - observed_products)}，越界={sorted(observed_products - expected)}"
        )
    normalized_products = frame["product"].astype(str).str.upper()
    normalized_exchanges = frame["exchange"].astype(str).str.upper()
    configured_rows = normalized_products.isin(configured_exchanges)
    expected_exchange_rows = normalized_products.map(configured_exchanges)
    if bool(
        configured_rows.any()
        and not (
            normalized_exchanges[configured_rows]
            == expected_exchange_rows[configured_rows]
        ).all()
    ):
        raise DailyCacheVerificationError("日线缓存交易所与品种配置不一致")
    contract_parts = frame["contract"].astype(str).str.extract(
        r"^([A-Z]+)(\d{4})$", expand=True
    )
    contract_products = contract_parts[0]
    contract_months = pd.to_numeric(contract_parts[1].str[-2:], errors="coerce")
    if (
        contract_products.isna().any()
        or contract_months.isna().any()
        or not bool(contract_months.between(1, 12).all())
        or not bool(
        (contract_products == frame["product"].astype(str).str.upper()).all()
        )
    ):
        raise DailyCacheVerificationError("日线缓存实际合约代码与品种字段不一致")
    coverage_by_date = frame.groupby("date", sort=False)["product"].agg(
        lambda values: set(str(value).upper() for value in values)
    )
    first_seen = (
        frame.assign(_product=frame["product"].astype(str).str.upper())
        .groupby("_product")["date"]
        .min()
    )
    derived_full_scope_start = str(first_seen.max())
    coverage_scope = manifest.get("coverage_scope")
    if production_scope:
        pinned_full_scope_start, anchor_hash = _verify_immutable_anchor(
            frame,
            manifest=manifest,
            universe_path=universe_path,
        )
        if not isinstance(coverage_scope, dict) or coverage_scope.get(
            "full_product_scope_start"
        ) != pinned_full_scope_start:
            raise DailyCacheVerificationError(
                "manifest.coverage_scope 未绑定完整品种覆盖起点"
            )
        if derived_full_scope_start > pinned_full_scope_start:
            raise DailyCacheVerificationError("缓存完整品种覆盖起点晚于独立策略")
        full_scope_start = pinned_full_scope_start
    else:
        # Explicit fixture/research scopes predate the production manifest
        # binding; derive their equivalent boundary without claiming universe
        # provenance.
        full_scope_start = derived_full_scope_start
        anchor_hash = None
    incomplete_dates = [
        str(trade_day)
        for trade_day, products_on_day in coverage_by_date.items()
        if str(trade_day) >= full_scope_start and products_on_day != expected
    ]
    if incomplete_dates:
        raise DailyCacheVerificationError(
            "日线缓存存在未覆盖完整品种范围的发布日："
            + ",".join(incomplete_dates[:10])
        )

    date_range = manifest.get("date_range")
    if not isinstance(date_range, dict):
        raise DailyCacheVerificationError("manifest.date_range 缺失")
    declared_start = _iso_date(date_range.get("start"), "manifest.date_range.start")
    declared_end = _iso_date(date_range.get("end"), "manifest.date_range.end")
    actual_start = date.fromisoformat(str(frame["date"].min()))
    actual_end = date.fromisoformat(str(frame["date"].max()))
    if (declared_start, declared_end) != (actual_start, actual_end):
        raise DailyCacheVerificationError("manifest.date_range 与日线缓存实际范围不一致")
    if not declared_start <= cutoff <= declared_end:
        raise DailyCacheVerificationError(
            f"绑定完成交易日 {cutoff.isoformat()} 超出缓存范围 "
            f"{declared_start.isoformat()}..{declared_end.isoformat()}"
        )
    terminal_products = set(
        frame.loc[frame["date"] == declared_end.isoformat(), "product"]
        .astype(str)
        .str.upper()
    )
    if terminal_products != expected:
        raise DailyCacheVerificationError(
            "缓存终端完成交易日未覆盖完整交易范围；"
            f"缺失={sorted(expected - terminal_products)}"
        )

    selected = frame[
        (frame["contract"].astype(str).str.upper() == normalized_contract)
        & (frame["date"] <= cutoff.isoformat())
    ].sort_values("date", kind="stable")
    if selected.empty:
        raise DailyCacheVerificationError(f"缓存没有 {normalized_contract} 的已完成日线")
    if str(selected.iloc[-1]["date"]) != cutoff.isoformat():
        raise DailyCacheVerificationError(
            f"缓存中 {normalized_contract} 在绑定完成交易日没有有效 bar"
        )
    selected = selected.tail(limit).copy()
    selected_exchange = str(selected.iloc[-1]["exchange"]).strip().upper()
    dce_identity_verified = False
    source_contract_identity_status = "not_applicable_non_dce"
    source_contract_identity_gap: str | None = None
    if selected_exchange == "DCE":
        try:
            identity_rows, identity_inventory_status = load_manifest_identity_rows(
                manifest,
                artifact_directory=manifest_path.parent,
            )
            if not identity_rows:
                source_contract_identity_status = identity_inventory_status
                source_contract_identity_gap = identity_inventory_status
            else:
                verify_identity_rows_cover_cache_rows(
                    identity_rows,
                    selected.to_dict("records"),
                )
                dce_identity_verified = True
                source_contract_identity_status = (
                    "verified_sina_cross_endpoint_response_archives"
                )
        except DceDailyIdentityError as exc:
            source_contract_identity_status = (
                "unverified_selected_rows_not_bound_to_response_archives"
            )
            source_contract_identity_gap = str(exc)
    evidence_status = (
        "degraded"
        if selected_exchange == "DCE" and not dce_identity_verified
        else "verified"
    )
    rows = [
        {
            "date": str(row["date"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "hold": float(row["open_interest"]),
            "open_interest": float(row["open_interest"]),
            "completed": True,
        }
        for row in selected.to_dict("records")
    ]
    evidence = {
        "status": evidence_status,
        "quality_tier": (
            "artifact_integrity_verified_source_contract_identity_unverified"
            if evidence_status == "degraded"
            else "manifest_anchor_content_and_cross_endpoint_identity_verified_cache"
            if selected_exchange == "DCE"
            else "manifest_anchor_and_content_verified_cache"
            if production_scope
            else "manifest_and_content_verified_explicit_scope"
        ),
        "source_kind": (
            "published_configured_product_actual_contract_daily_cache"
            if production_scope
            else "nonproduction_explicit_scope_actual_contract_daily_cache"
        ),
        "scope_kind": (
            "configured_product_universe"
            if production_scope
            else "explicit_nonproduction_scope"
        ),
        "expected_products": sorted(expected),
        "contract": normalized_contract,
        "schema_version": schema_version,
        "daily_path": str(resolved_daily_path),
        "manifest_path": str(manifest_path),
        "manifest_archive_encoding": "base64",
        "manifest_content_base64": base64.b64encode(manifest_content).decode("ascii"),
        "manifest_content_sha256": hashlib.sha256(manifest_content).hexdigest(),
        "sha256": actual_hash,
        "manifest_updated_at": manifest.get("updated_at"),
        "dataset_date_range": {
            "start": declared_start.isoformat(),
            "end": declared_end.isoformat(),
        },
        "bound_completed_trade_date": cutoff.isoformat(),
        "slice_date_range": {
            "start": rows[0]["date"],
            "end": rows[-1]["date"],
        },
        "slice_row_count": len(rows),
        "slice_sha256": _slice_sha256(rows),
        "source_lineage": manifest.get("source_lineage"),
        "artifact_integrity_status": "verified",
        "source_contract_identity_status": source_contract_identity_status,
        "source_contract_identity_gap": source_contract_identity_gap,
        "source_contract_identity_method": (
            "sina_identity_bearing_cross_endpoint_terminal_ohlcv_oi_exact_match"
            if dce_identity_verified
            else None
        ),
        "verification": {
            "artifact_integrity": True,
            "manifest_schema": True,
            "manifest_content_archived": True,
            "content_sha256": True,
            "row_count": True,
            "date_product_coverage_sha256": True,
            "configured_product_scope": True,
            "configured_product_exchange_mapping": True,
            "every_date_after_full_scope_start": True,
            "full_product_scope_start": True,
            **({"immutable_history_anchor": True} if anchor_hash is not None else {}),
            "terminal_product_scope": True,
            "declared_date_range": True,
            "inclusive_cutoff": True,
            "slice_content_sha256": True,
            **(
                {"dce_source_contract_identity": dce_identity_verified}
                if selected_exchange == "DCE"
                else {}
            ),
        },
    }
    return rows, evidence


def replay_daily_artifact_evidence(
    technical: Any,
    *,
    contract: str,
    require_source_identity: bool = False,
) -> dict[str, Any]:
    """Reopen the archived manifest/object and replay a raw slice independently.

    ``degraded`` DCE evidence may still prove that the bytes are the ones bound
    by the immutable cache anchor, even though the historical symbol-less
    upstream response cannot prove contract identity.  That distinction is
    useful for tamper detection but must never be promoted to source identity.
    """

    if not isinstance(technical, dict):
        raise DailyCacheVerificationError("technical 日线证据缺失")
    evidence = technical.get("daily_data_evidence")
    if not isinstance(evidence, dict) or evidence.get("status") not in {
        "verified",
        "degraded",
    }:
        raise DailyCacheVerificationError("日线证据未声明可重放的 verified/degraded 状态")
    if (
        evidence.get("artifact_integrity_status") != "verified"
        or not isinstance(evidence.get("verification"), dict)
        or evidence["verification"].get("artifact_integrity") is not True
    ):
        raise DailyCacheVerificationError("日线证据未声明 artifact integrity verified")
    if require_source_identity and evidence.get("status") != "verified":
        raise DailyCacheVerificationError("日线证据未声明 verified")
    if evidence.get("contract") != str(contract).strip().upper():
        raise DailyCacheVerificationError("日线证据 contract 与分析合约不一致")
    configured_products = set(_expected_product_exchanges(UNIVERSE_PATH))
    if evidence.get("scope_kind") != "configured_product_universe" or evidence.get(
        "expected_products"
    ) != sorted(configured_products):
        raise DailyCacheVerificationError("verified 日线证据未绑定当前完整配置品种范围")
    if evidence.get("manifest_archive_encoding") != "base64":
        raise DailyCacheVerificationError("日线 manifest 归档编码不受支持")
    encoded = evidence.get("manifest_content_base64")
    if not isinstance(encoded, str) or not encoded:
        raise DailyCacheVerificationError("日线 manifest 归档缺失")
    try:
        archived_manifest = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DailyCacheVerificationError("日线 manifest 归档不是严格 base64") from exc
    declared_manifest_hash = evidence.get("manifest_content_sha256")
    if (
        not isinstance(declared_manifest_hash, str)
        or hashlib.sha256(archived_manifest).hexdigest() != declared_manifest_hash
    ):
        raise DailyCacheVerificationError("日线 manifest 归档 SHA-256 不一致")
    daily_path = Path(str(evidence.get("daily_path") or ""))
    manifest_path = Path(str(evidence.get("manifest_path") or ""))
    if not daily_path.is_absolute() or not manifest_path.is_absolute():
        raise DailyCacheVerificationError("verified 日线证据必须绑定绝对 artifact 路径")
    row_count = evidence.get("slice_row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 1:
        raise DailyCacheVerificationError("日线证据 slice_row_count 非法")
    cutoff = evidence.get("bound_completed_trade_date")
    rows, regenerated = load_verified_contract_history(
        contract,
        completed_through=cutoff,
        limit=row_count,
        daily_path=daily_path,
        manifest_path=manifest_path,
        manifest_bytes=archived_manifest,
    )
    raw_rows = technical.get("completed_daily_rows")
    if not isinstance(raw_rows, list):
        raise DailyCacheVerificationError("technical.completed_daily_rows 缺失")
    expected_fields = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "hold",
        "completed",
    }
    if any(not isinstance(row, dict) or set(row) != expected_fields for row in raw_rows):
        raise DailyCacheVerificationError("completed_daily_rows 字段不受支持")
    normalized_rows = [
        {field: row.get(field) for field in expected_fields}
        for row in rows
    ]
    if raw_rows != normalized_rows:
        raise DailyCacheVerificationError("completed_daily_rows 与已验缓存切片不一致")
    if evidence.get("slice_sha256") != _slice_sha256(raw_rows):
        raise DailyCacheVerificationError("日线证据 slice_sha256 不一致")
    ignored = {"completion_basis", "completion_rule"}
    if {
        key: value for key, value in evidence.items() if key not in ignored
    } != regenerated:
        raise DailyCacheVerificationError("日线证据字段与归档 manifest 重放结果不一致")
    return regenerated


def replay_verified_daily_evidence(
    technical: Any,
    *,
    contract: str,
) -> dict[str, Any]:
    """Replay evidence that additionally has verified source-contract identity."""

    return replay_daily_artifact_evidence(
        technical,
        contract=contract,
        require_source_identity=True,
    )


__all__ = [
    "DAILY_PATH",
    "MANIFEST_PATH",
    "DailyCacheVerificationError",
    "load_verified_contract_history",
    "replay_daily_artifact_evidence",
    "replay_verified_daily_evidence",
    "_post_anchor_calendar_sha256",
    "_post_anchor_trade_dates",
]

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts import runtime_universe
from scripts.refresh_daily_market_data import (
    CSV_FIELDS,
    _write_deterministic_gzip_csv,
)


PROJECT = Path(__file__).resolve().parents[1]
EXPECTED_PRODUCTS = {
    "AP", "CF", "CJ", "CU", "FG", "JD", "JM", "LC", "LH", "M", "MA",
    "OI", "PK", "PS", "RU", "SA", "SI", "SM", "SP", "SR", "UR", "V",
}


def _read_json(relative: str) -> dict:
    return json.loads((PROJECT / relative).read_text(encoding="utf-8"))


def test_current_catalogs_and_schedule_are_exactly_aligned() -> None:
    universe = _read_json("config/trading_universe.json")
    products = universe["permission_scope"]["allowed_products"]
    allowed = {row["symbol"] for row in products}
    assert universe["product_count"] == len(products) == len(allowed) == 22
    assert allowed == EXPECTED_PRODUCTS

    specs = _read_json("config/product_contract_specs.json")
    spec_products = {row["symbol"] for row in specs["products"]}
    assert specs["product_count"] == 22
    assert spec_products == allowed

    policy = _read_json("config/futures_main_report_policy.json")
    scheduled = [
        symbol
        for values in policy["market_data_freshness"]
        ["regular_session_schedule_bjt"]["night_end_groups"].values()
        for symbol in values
    ]
    assert policy["scan_manifest"]["all_market_expected_count"] == 22
    assert len(scheduled) == len(set(scheduled)) == 22
    assert set(scheduled) == allowed

    groups = _read_json("config/product_groups.json")
    grouped = [
        symbol
        for group in groups["primary_groups"]
        for symbol in group["products"]
    ]
    assert groups["universe_sha256"] == hashlib.sha256(
        (PROJECT / "config/trading_universe.json").read_bytes()
    ).hexdigest()
    assert len(grouped) == len(set(grouped)) == len(allowed)
    assert set(grouped) == allowed
    for cluster in groups["correlated_exposure_clusters"]:
        assert len(cluster["products"]) >= 2
        assert set(cluster["products"]) <= allowed
    assert groups["claim_boundary"] == {
        "group_is_not_correlation_estimate": True,
        "group_is_not_direction_signal": True,
        "group_does_not_auto_select_or_remove_product": True,
    }


def test_current_daily_cache_matches_the_configured_products() -> None:
    if not (PROJECT / "local-data/market-history/daily/latest_manifest.json").is_file():
        pytest.skip("当前检出未导入已发布日线缓存")
    manifest = _read_json("local-data/market-history/daily/latest_manifest.json")
    daily_path = PROJECT / "local-data/market-history/daily" / manifest["file"]
    frame = pd.read_csv(daily_path, compression="gzip", usecols=["product"])
    observed = set(frame["product"].astype(str).str.upper())
    assert manifest["product_count"] == 22
    assert observed == set(runtime_universe.allowed_product_symbols())


def test_daily_gzip_writer_is_byte_deterministic(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        [
            {
                "date": "2026-09-08",
                "product": "RB",
                "exchange": "SHFE",
                "contract": "RB2701",
                "open": 1.0,
                "high": 2.0,
                "low": 1.0,
                "close": 2.0,
                "settle": 2.0,
                "volume": 10,
                "open_interest": 20,
            }
        ],
        columns=CSV_FIELDS,
    )
    first = tmp_path / "first.csv.gz"
    second = tmp_path / "second.csv.gz"
    _write_deterministic_gzip_csv(frame, first)
    _write_deterministic_gzip_csv(frame, second)
    first_bytes = first.read_bytes()
    second_bytes = second.read_bytes()
    assert first_bytes == second_bytes
    assert hashlib.sha256(first_bytes).hexdigest() == hashlib.sha256(
        second_bytes
    ).hexdigest()

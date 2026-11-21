from __future__ import annotations

import base64
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import fetch_futures
from scripts import dce_daily_identity
from scripts import market_structure
from scripts import refresh_daily_market_data
from scripts import scan_market
from scripts import verified_daily_loader
from scripts.market_structure import Bar, Pivot
from scripts.tick_evidence import archived_http_response_fields
from scripts.verified_daily_loader import (
    DailyCacheVerificationError,
    load_verified_contract_history,
    replay_daily_artifact_evidence,
    replay_verified_daily_evidence,
)


EXCHANGE_DISPLAY_NAMES = {
    "DCE": "大连商品交易所",
    "SHFE": "上海期货交易所",
    "CZCE": "郑州商品交易所",
    "GFEX": "广州期货交易所",
    "INE": "上海国际能源交易中心",
}


def _fixture_market_time() -> datetime:
    return datetime.fromisoformat("2026-09-13T10:00:00+08:00")


def _sina_contract_page_html(
    contract: str,
    tick_size: int | float,
    *,
    related_contracts: tuple[str, ...] = (),
) -> bytes:
    product = fetch_futures.require_allowed_product(contract)
    exchange = fetch_futures.allowed_products()[product]["exchange"]
    related = ",".join(f'"{item}"' for item in related_contracts)
    return (
        "<html><head>"
        f"<title>fixture({contract})期货行情_新浪财经</title>"
        "</head><body><script>var futures_app = {};"
        f'futures_app.code = "{contract}";'
        f"futures_app.inner_futures_per_month = {{data:[{related}]}};"
        "</script>"
        '<div class="futures-title"><h1 class="title">fixture</h1>'
        f'<span class="code">({contract})</span></div>'
        '<table id="table-futures-basic-data"><tbody>'
        f"<tr><th>最小变动价位</th><td>{tick_size}</td></tr>"
        f"<tr><th>交易代码</th><td>{product}</td>"
        f"<th>上市交易所</th><td>{EXCHANGE_DISPLAY_NAMES[exchange]}</td></tr>"
        "</tbody></table></body></html>"
    ).encode("gb18030")


def _exact_tick_evidence(
    contract: str,
    tick_size: int | float,
    *,
    retrieved_at: str = "2026-09-13T10:00:00+08:00",
) -> dict:
    provider_symbol = fetch_futures.provider_symbol_for_sina(contract)
    source_url = fetch_futures.CONTRACT_DETAIL_URL.format(symbol=provider_symbol)
    body = _sina_contract_page_html(provider_symbol, tick_size)
    return {
        "kind": "actual_contract_http_detail",
        "contract": contract,
        "provider_symbol": provider_symbol,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        **archived_http_response_fields(
            body,
            final_url=source_url,
            status_code=200,
        ),
        "hash_scope": "http_response_body",
        "locator": "contract detail table item=最小变动价位",
        "raw_item": "最小变动价位",
        "raw_value": str(tick_size),
        "tick_size": tick_size,
        "exact": True,
    }


def _dce_daily_body(rows: list[dict[str, object]]) -> bytes:
    payload = [
        {
            "d": row["date"],
            "o": row["open"],
            "h": row["high"],
            "l": row["low"],
            "c": row["close"],
            "v": row["volume"],
            "p": row["open_interest"],
            "s": row["settle"],
        }
        for row in rows
    ]
    return (
        f"var {dce_daily_identity.DAILY_CALLBACK}=("
        + json.dumps(payload, separators=(",", ":"))
        + ");"
    ).encode("utf-8")


def _dce_quote_body(
    contract: str,
    row: dict[str, object],
    *,
    clock: str = "150000",
    overrides: dict[str, object] | None = None,
) -> bytes:
    values = dict(row)
    values.update(overrides or {})
    fields = [""] * 29
    fields[0] = contract
    fields[1] = clock
    fields[2] = str(values["open"])
    fields[3] = str(values["high"])
    fields[4] = str(values["low"])
    fields[8] = str(values["close"])
    fields[13] = str(values["open_interest"])
    fields[14] = str(values["volume"])
    fields[17] = str(values["date"])
    return f'var hq_str_nf_{contract}="{",".join(fields)}";'.encode("gb18030")


def _http_response(content: bytes, url: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        url=url,
        status_code=200,
        request=SimpleNamespace(url=url, method="GET"),
    )


def _dce_contract_evidence(
    contract: str,
    rows: list[dict[str, object]],
    *,
    quote_clock: str = "150000",
    quote_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    daily_url = dce_daily_identity._prepared_daily_url(contract)
    quote_url = dce_daily_identity.QUOTE_URL.format(symbol=contract)
    _, evidence = dce_daily_identity.bind_contract_http_responses(
        contract,
        completed_through=date.fromisoformat(str(rows[-1]["date"])),
        daily_response=_http_response(_dce_daily_body(rows), daily_url),
        quote_response=_http_response(
            _dce_quote_body(
                contract,
                rows[-1],
                clock=quote_clock,
                overrides=quote_overrides,
            ),
            quote_url,
        ),
    )
    return evidence


def _write_cache_fixture(tmp_path: Path) -> tuple[Path, Path]:
    daily_path = tmp_path / "contract_daily_latest.csv.gz"
    rows = [
        {
            "date": day,
            "product": product,
            "exchange": "TEST",
            "contract": contract,
            "open": price,
            "high": price + 2,
            "low": price - 2,
            "close": price + 1,
            "settle": price,
            "volume": 100,
            "open_interest": 200,
        }
        for day, price in (("2026-09-10", 100.0), ("2026-09-11", 101.0))
        for product, contract in (("AA", "AA2610"), ("BB", "BB2610"))
    ]
    pd.DataFrame(rows).to_csv(daily_path, index=False, compression="gzip")
    digest = hashlib.sha256(daily_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "latest_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "decision_agent_daily_history_latest.v1",
                "file": daily_path.name,
                "sha256": digest,
                "rows": len(rows),
                "coverage_sha256": refresh_daily_market_data.coverage_sha256(
                    pd.DataFrame(rows)
                ),
                "product_count": 2,
                "date_range": {"start": "2026-09-10", "end": "2026-09-11"},
                "updated_at": "2026-09-12T18:00:00+08:00",
                "source_lineage": {"TEST": "fixture"},
                "claim_boundary": {
                    "completed_daily_bars_only": True,
                    "actual_month_contracts_only": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return daily_path, manifest_path


def _copy_current_published_cache(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the current manifest and the immutable object it actually names."""

    source_manifest = verified_daily_loader.MANIFEST_PATH
    if not source_manifest.is_file():
        pytest.skip("当前检出未导入已发布日线缓存")
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    source_daily = source_manifest.parent / manifest["file"]
    daily_path = tmp_path / source_daily.name
    manifest_path = tmp_path / source_manifest.name
    shutil.copyfile(source_daily, daily_path)
    shutil.copyfile(source_manifest, manifest_path)
    return daily_path, manifest_path


def test_verified_loader_checks_hash_scope_range_and_cutoff(tmp_path: Path) -> None:
    daily_path, manifest_path = _write_cache_fixture(tmp_path)

    rows, evidence = load_verified_contract_history(
        "AA2610",
        completed_through="2026-09-11",
        daily_path=daily_path,
        manifest_path=manifest_path,
        expected_products={"AA", "BB"},
    )

    assert [row["date"] for row in rows] == ["2026-09-10", "2026-09-11"]
    assert evidence["status"] == "verified"
    assert all(evidence["verification"].values())

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DailyCacheVerificationError, match="SHA-256"):
        load_verified_contract_history(
            "AA2610",
            completed_through="2026-09-11",
            daily_path=daily_path,
            manifest_path=manifest_path,
            expected_products={"AA", "BB"},
        )


def test_verified_daily_evidence_reopens_manifest_dataset_and_exact_slice(
    tmp_path: Path,
) -> None:
    """A raw ``status=verified`` string cannot substitute for artifact replay."""

    daily_path, manifest_path = _copy_current_published_cache(tmp_path)

    rows, evidence = load_verified_contract_history(
        "CU2609",
        completed_through="2026-09-08",
        limit=14,
        daily_path=daily_path,
        manifest_path=manifest_path,
    )
    raw_rows = [
        {
            field: row[field]
            for field in (
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "hold",
                "completed",
            )
        }
        for row in rows
    ]
    technical = {
        "symbol": "CU2609",
        "completed_daily_rows": raw_rows,
        "daily_data_evidence": evidence,
    }
    replayed = replay_verified_daily_evidence(technical, contract="CU2609")
    assert replayed == evidence
    assert evidence["scope_kind"] == "configured_product_universe"
    assert len(evidence["expected_products"]) == len(fetch_futures.allowed_products())

    self_attested = json.loads(json.dumps(technical))
    self_attested["daily_data_evidence"] = {"status": "verified"}
    with pytest.raises(DailyCacheVerificationError, match="artifact integrity"):
        replay_verified_daily_evidence(self_attested, contract="CU2609")

    resigned_slice = json.loads(json.dumps(technical))
    resigned_slice["completed_daily_rows"][-1]["close"] += 1
    resigned_slice["daily_data_evidence"]["slice_sha256"] = (
        verified_daily_loader._slice_sha256(resigned_slice["completed_daily_rows"])
    )
    with pytest.raises(DailyCacheVerificationError, match="缓存切片"):
        replay_verified_daily_evidence(resigned_slice, contract="CU2609")

    resigned_manifest = json.loads(json.dumps(technical))
    manifest_bytes = base64.b64decode(
        resigned_manifest["daily_data_evidence"]["manifest_content_base64"],
        validate=True,
    )
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    manifest["rows"] += 1
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    resigned_manifest["daily_data_evidence"]["manifest_content_base64"] = (
        base64.b64encode(manifest_bytes).decode("ascii")
    )
    resigned_manifest["daily_data_evidence"]["manifest_content_sha256"] = (
        hashlib.sha256(manifest_bytes).hexdigest()
    )
    with pytest.raises(DailyCacheVerificationError, match="manifest.rows"):
        replay_verified_daily_evidence(resigned_manifest, contract="CU2609")

    relative_pointer = json.loads(json.dumps(technical))
    relative_pointer["daily_data_evidence"]["daily_path"] = daily_path.name
    with pytest.raises(DailyCacheVerificationError, match="绝对 artifact 路径"):
        replay_verified_daily_evidence(relative_pointer, contract="CU2609")

    original = daily_path.read_bytes()
    daily_path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(DailyCacheVerificationError, match="SHA-256"):
        replay_verified_daily_evidence(technical, contract="CU2609")


def test_legacy_dce_cache_integrity_is_not_source_contract_identity(
    tmp_path: Path,
) -> None:
    daily_path, manifest_path = _copy_current_published_cache(tmp_path)

    rows, evidence = load_verified_contract_history(
        "M2609",
        completed_through="2026-09-08",
        limit=5,
        daily_path=daily_path,
        manifest_path=manifest_path,
    )

    assert rows[-1]["date"] == "2026-09-08"
    assert evidence["status"] == "degraded"
    assert evidence["artifact_integrity_status"] == "verified"
    assert (
        evidence["source_contract_identity_status"]
        == "unverified_legacy_no_response_identity_artifact"
    )
    assert evidence["verification"]["content_sha256"] is True
    assert evidence["verification"]["dce_source_contract_identity"] is False
    assert (
        scan_market._daily_data_evidence_status(
            {"daily_data_evidence": evidence},
            {"technical": {"daily_data_evidence": evidence}},
        )
        == "degraded"
    )
    technical = {
        "completed_daily_rows": [
            {key: value for key, value in row.items() if key != "open_interest"}
            for row in rows
        ],
        "daily_data_evidence": evidence,
    }
    replayed = replay_daily_artifact_evidence(technical, contract="M2609")
    assert replayed["status"] == "degraded"
    assert replayed["artifact_integrity_status"] == "verified"

    tampered = json.loads(json.dumps(technical))
    tampered["completed_daily_rows"][0]["close"] += 1
    with pytest.raises(DailyCacheVerificationError, match="缓存切片不一致"):
        replay_daily_artifact_evidence(tampered, contract="M2609")

    with pytest.raises(DailyCacheVerificationError, match="未声明 verified"):
        replay_verified_daily_evidence(
            technical,
            contract="M2609",
        )


def test_dce_identity_bundle_verifies_exact_cache_rows_and_detects_resigned_drift(
    tmp_path: Path,
) -> None:
    source_rows = [
        {
            "date": "2026-09-10",
            "open": 5100,
            "high": 5150,
            "low": 5080,
            "close": 5120,
            "settle": 5110,
            "volume": 100,
            "open_interest": 200,
        },
        {
            "date": "2026-09-11",
            "open": 5200,
            "high": 5237,
            "low": 5075,
            "close": 5087,
            "settle": 5151,
            "volume": 183927,
            "open_interest": 170135,
        },
    ]
    cache_rows = [
        {
            **row,
            "product": "M",
            "exchange": "DCE",
            "contract": "M2701",
        }
        for row in source_rows
    ]
    identity = _dce_contract_evidence("M2701", source_rows)
    bundle = dce_daily_identity.build_identity_bundle(
        [identity],
        published_dates=[date(2026, 9, 10), date(2026, 9, 11)],
    )
    dce_daily_identity.verify_bundle_covers_cache_rows(bundle, cache_rows)
    identity_path, identity_hash = (
        refresh_daily_market_data._write_dce_identity_content_addressed(
            bundle, tmp_path
        )
    )
    descriptor = dce_daily_identity.descriptor_for_bundle(
        file=identity_path.name,
        sha256=identity_hash,
        bundle=bundle,
    )
    daily_path = tmp_path / "contract_daily_latest.csv.gz"
    frame = pd.DataFrame(cache_rows)
    frame.to_csv(daily_path, index=False, compression="gzip")
    manifest = {
        "schema_version": "decision_agent_daily_history_latest.v1",
        "file": daily_path.name,
        "sha256": hashlib.sha256(daily_path.read_bytes()).hexdigest(),
        "rows": len(frame),
        "coverage_sha256": refresh_daily_market_data.coverage_sha256(frame),
        "product_count": 1,
        "date_range": {"start": "2026-09-10", "end": "2026-09-11"},
        "updated_at": "2026-09-11T16:00:00+08:00",
        "source_lineage": {
            "DCE": "Sina non-official cross-endpoint identity fixture"
        },
        "claim_boundary": {
            "completed_daily_bars_only": True,
            "actual_month_contracts_only": True,
        },
        "dce_source_identity": {
            "schema_version": dce_daily_identity.INDEX_SCHEMA,
            "legacy_unverified_through": "2026-09-09",
            "evidence_objects": [descriptor],
        },
    }
    manifest_path = tmp_path / "latest_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rows, evidence = load_verified_contract_history(
        "M2701",
        completed_through="2026-09-11",
        daily_path=daily_path,
        manifest_path=manifest_path,
        expected_products={"M"},
    )
    assert len(rows) == 2
    assert evidence["status"] == "verified"
    assert (
        evidence["source_contract_identity_status"]
        == "verified_sina_cross_endpoint_response_archives"
    )
    assert evidence["verification"]["dce_source_contract_identity"] is True

    # Re-signing a changed cache proves only the changed artifact's integrity;
    # it cannot rewrite the archived provider responses into a contract match.
    frame.loc[frame["date"] == "2026-09-11", "close"] = 5088
    frame.to_csv(daily_path, index=False, compression="gzip")
    manifest["sha256"] = hashlib.sha256(daily_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _, drifted = load_verified_contract_history(
        "M2701",
        completed_through="2026-09-11",
        daily_path=daily_path,
        manifest_path=manifest_path,
        expected_products={"M"},
    )
    assert drifted["status"] == "degraded"
    assert drifted["artifact_integrity_status"] == "verified"
    assert (
        drifted["source_contract_identity_status"]
        == "unverified_selected_rows_not_bound_to_response_archives"
    )


def test_nonproduction_explicit_scope_cannot_claim_verified_agent_daily_evidence(
    tmp_path: Path,
) -> None:
    daily_path, manifest_path = _write_cache_fixture(tmp_path)
    rows, evidence = load_verified_contract_history(
        "AA2610",
        completed_through="2026-09-11",
        daily_path=daily_path,
        manifest_path=manifest_path,
        expected_products={"AA", "BB"},
    )
    technical = {
        "completed_daily_rows": [
            {key: value for key, value in row.items() if key != "open_interest"}
            for row in rows
        ],
        "daily_data_evidence": evidence,
    }
    assert evidence["scope_kind"] == "explicit_nonproduction_scope"
    with pytest.raises(DailyCacheVerificationError, match="完整配置品种范围"):
        replay_verified_daily_evidence(technical, contract="AA2610")


def test_verified_loader_rejects_stale_cutoff(tmp_path: Path) -> None:
    daily_path, manifest_path = _write_cache_fixture(tmp_path)
    with pytest.raises(DailyCacheVerificationError, match="超出缓存范围"):
        load_verified_contract_history(
            "AA2610",
            completed_through="2026-09-14",
            daily_path=daily_path,
            manifest_path=manifest_path,
            expected_products={"AA", "BB"},
        )


def _resign_cache_fixture(daily_path: Path, manifest_path: Path) -> None:
    frame = pd.read_csv(daily_path, compression="gzip")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(daily_path.read_bytes()).hexdigest()
    manifest["rows"] = len(frame)
    manifest["date_range"] = {
        "start": str(frame["date"].min()),
        "end": str(frame["date"].max()),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_refresh_verifies_old_manifest_before_resigning_cache(tmp_path: Path) -> None:
    daily_path, manifest_path = _write_cache_fixture(tmp_path)
    universe = [
        {"symbol": "AA", "exchange": "TEST"},
        {"symbol": "BB", "exchange": "TEST"},
    ]
    assert len(
        refresh_daily_market_data.verify_existing_baseline(
            daily_path, manifest_path, universe
        )
    ) == 4

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(refresh_daily_market_data.RefreshError, match="SHA-256"):
        refresh_daily_market_data.verify_existing_baseline(
            daily_path, manifest_path, universe
        )


def test_content_addressed_publish_keeps_old_manifest_readable_until_pointer_switch(
    tmp_path: Path,
) -> None:
    daily_path, manifest_path = _write_cache_fixture(tmp_path)
    frame = pd.read_csv(daily_path, compression="gzip")
    additions = frame[frame["date"] == "2026-09-11"].copy()
    additions["date"] = "2026-09-12"
    updated = pd.concat([frame, additions], ignore_index=True).sort_values(
        ["date", "product", "contract"], kind="stable"
    )

    published_path, published_hash = (
        refresh_daily_market_data._write_daily_content_addressed(updated, daily_path)
    )

    # This is the supervisor-kill boundary: the new immutable object exists, but
    # the old manifest still points at the old readable cache.
    old_rows, _ = load_verified_contract_history(
        "AA2610",
        completed_through="2026-09-11",
        daily_path=daily_path,
        manifest_path=manifest_path,
        expected_products={"AA", "BB"},
    )
    assert old_rows[-1]["date"] == "2026-09-11"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "file": published_path.name,
            "sha256": published_hash,
            "rows": len(updated),
            "coverage_sha256": refresh_daily_market_data.coverage_sha256(updated),
            "date_range": {"start": "2026-09-10", "end": "2026-09-12"},
        }
    )
    refresh_daily_market_data._write_json_atomic(manifest, manifest_path)

    new_rows, evidence = load_verified_contract_history(
        "AA2610",
        completed_through="2026-09-12",
        daily_path=daily_path,
        manifest_path=manifest_path,
        expected_products={"AA", "BB"},
    )
    assert new_rows[-1]["date"] == "2026-09-12"
    assert Path(evidence["daily_path"]).name == published_path.name


def test_content_addressed_daily_object_is_fsynced_before_pointer_can_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daily_path, _ = _write_cache_fixture(tmp_path)
    frame = pd.read_csv(daily_path, compression="gzip")
    frame.loc[0, "close"] = float(frame.loc[0, "close"]) + 0.5
    events: list[str] = []
    real_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("fsync_file" if stat.S_ISREG(mode) else "fsync_directory")

    def record_replace(source: str | Path, target: str | Path) -> None:
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(refresh_daily_market_data.os, "fsync", record_fsync)
    monkeypatch.setattr(refresh_daily_market_data.os, "replace", record_replace)

    refresh_daily_market_data._write_daily_content_addressed(frame, daily_path)

    assert events[:3] == ["fsync_file", "replace", "fsync_directory"]


def test_immutable_anchor_rejects_a_whole_missing_trade_day_after_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    anchor_path = tmp_path / "anchor.csv.gz"
    anchor_rows = [
        {
            "date": "2026-09-08",
            "product": product,
            "exchange": "TEST",
            "contract": f"{product}2610",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "settle": 100,
            "volume": 100,
            "open_interest": 500,
        }
        for product in ("AA", "BB")
    ]
    anchor = pd.DataFrame(anchor_rows)
    anchor.to_csv(anchor_path, index=False, compression="gzip")
    universe_path = tmp_path / "universe.json"
    universe_path.write_text("{}\n", encoding="utf-8")
    policy_path = tmp_path / "integrity.json"
    policy = {
        "schema_version": "daily_history_integrity.v1",
        "universe_sha256": hashlib.sha256(universe_path.read_bytes()).hexdigest(),
        "anchor": {
            "file": anchor_path.name,
            "sha256": hashlib.sha256(anchor_path.read_bytes()).hexdigest(),
            "rows": len(anchor),
            "coverage_sha256": verified_daily_loader._coverage_sha256(anchor),
            "date_range": {"start": "2026-09-08", "end": "2026-09-08"},
        },
        "full_product_scope_start": "2026-09-08",
    }
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest = {
        "integrity_policy_sha256": hashlib.sha256(
            policy_path.read_bytes()
        ).hexdigest(),
        "post_anchor_calendar_sha256": verified_daily_loader._post_anchor_calendar_sha256(
            ["2026-09-09", "2026-09-10"]
        ),
    }
    # A complete 9/10 cannot hide the fact that the known 9/9 trading day vanished.
    day_ten = anchor.copy()
    day_ten["date"] = "2026-09-10"
    tampered = pd.concat([anchor, day_ten], ignore_index=True)
    monkeypatch.setattr(verified_daily_loader, "PROJECT", tmp_path)
    monkeypatch.setattr(
        verified_daily_loader, "INTEGRITY_POLICY_PATH", policy_path
    )

    with pytest.raises(DailyCacheVerificationError, match="交易日不连续"):
        verified_daily_loader._verify_immutable_anchor(
            tampered,
            manifest=manifest,
            universe_path=universe_path,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda frame: frame.assign(exchange="WRONG"), "交易所"),
        (lambda frame: frame.assign(open=-1), "无效OHLC"),
        (lambda frame: frame.assign(settle=-1), "无效OHLC"),
        (lambda frame: frame.assign(volume=0), "无效OHLC"),
        (
            lambda frame: frame[
                ~((frame["date"] == "2026-09-11") & (frame["product"] == "BB"))
            ],
            "coverage_sha256",
        ),
        (
            lambda frame: frame[
                ~((frame["date"] == "2026-09-10") & (frame["product"] == "BB"))
            ],
            "coverage_sha256",
        ),
    ],
)
def test_refresh_rejects_corrupt_old_baseline_even_if_manifest_is_resigned(
    tmp_path: Path, mutation, message: str
) -> None:
    daily_path, manifest_path = _write_cache_fixture(tmp_path)
    frame = mutation(pd.read_csv(daily_path, compression="gzip"))
    frame.to_csv(daily_path, index=False, compression="gzip")
    _resign_cache_fixture(daily_path, manifest_path)
    universe = [
        {"symbol": "AA", "exchange": "TEST"},
        {"symbol": "BB", "exchange": "TEST"},
    ]

    with pytest.raises(refresh_daily_market_data.RefreshError, match=message):
        refresh_daily_market_data.verify_existing_baseline(
            daily_path, manifest_path, universe
        )


def test_cache_failure_is_explicit_degraded_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_futures, "now_beijing", _fixture_market_time)
    def reject_cache(*args, **kwargs):
        raise DailyCacheVerificationError("fixture hash mismatch")

    frame = pd.DataFrame(
        [
            {
                "date": "2026-09-10",
                "open": 100,
                "high": 105,
                "low": 98,
                "close": 103,
                "volume": 1000,
                "hold": 2000,
            },
            {
                "date": "2026-09-11",
                "open": 103,
                "high": 107,
                "low": 101,
                "close": 106,
                "volume": 1200,
                "hold": 2200,
            },
        ]
    )
    monkeypatch.setattr(fetch_futures, "load_verified_contract_history", reject_cache)
    monkeypatch.setattr(fetch_futures.ak, "futures_zh_daily_sina", lambda symbol: frame)

    result = fetch_futures.technical_snapshot(
        "CU2610",
        106,
        completed_through="2026-09-11",
        completion_basis="template_inferred",
    )

    evidence = result["daily_data_evidence"]
    assert result["last_daily_date"] == "2026-09-11"
    assert evidence["status"] == "degraded"
    assert evidence["quality_equivalence"] == "not_equivalent_to_verified_cache"
    assert evidence["cache_error"] == "fixture hash mismatch"
    assert evidence["completion_basis"] == "template_inferred"


def test_single_contract_fallback_rejects_settlement_only_zero_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_futures, "now_beijing", _fixture_market_time)
    def reject_cache(*args, **kwargs):
        raise DailyCacheVerificationError("fixture cache unavailable")

    frame = pd.DataFrame(
        [
            {
                "date": "2026-09-11",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 0,
                "hold": 500,
            }
        ]
    )
    monkeypatch.setattr(fetch_futures, "load_verified_contract_history", reject_cache)
    monkeypatch.setattr(fetch_futures.ak, "futures_zh_daily_sina", lambda symbol: frame)

    with pytest.raises(ValueError, match="零成交"):
        fetch_futures.technical_snapshot(
            "CU2610",
            100,
            completed_through="2026-09-11",
            completion_basis="fixture",
        )


def test_weekend_completed_date_includes_friday_and_discloses_template() -> None:
    evidence = fetch_futures.infer_completed_trade_date(
        datetime.fromisoformat("2026-09-13T10:00:00+08:00")
    )
    assert evidence["date"] == "2026-09-11"
    assert evidence["source"] == "template_inferred"
    assert evidence["official_calendar_verified"] is False


@pytest.mark.parametrize(
    ("observed_at", "expected"),
    [
        ("2026-09-14T10:00:00+08:00", date(2026, 9, 11)),
        ("2026-09-14T14:59:00+08:00", date(2026, 9, 11)),
        ("2026-09-14T15:06:00+08:00", date(2026, 9, 14)),
        ("2026-09-13T10:00:00+08:00", date(2026, 9, 11)),
    ],
)
def test_refresh_default_never_requests_an_incomplete_daily_bar(
    observed_at: str, expected: date
) -> None:
    assert refresh_daily_market_data.default_completed_through(
        datetime.fromisoformat(observed_at)
    ) == expected


def test_explicit_refresh_through_cannot_exceed_latest_completed_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        refresh_daily_market_data,
        "default_completed_through",
        lambda observed_at=None: date(2026, 9, 11),
    )
    with pytest.raises(refresh_daily_market_data.RefreshError, match="进行中日线"):
        refresh_daily_market_data._refresh_unlocked(
            through=date(2026, 9, 14),
            daily_path=tmp_path / "daily.csv.gz",
            manifest_path=tmp_path / "manifest.json",
        )


def test_dce_cross_endpoint_mismatch_preserves_old_cache_and_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daily_path = tmp_path / "daily.csv.gz"
    manifest_path = tmp_path / "latest_manifest.json"
    daily_path.write_bytes(b"old-cache-bytes")
    manifest_path.write_text("{}\n", encoding="utf-8")
    baseline = pd.DataFrame(
        [
            {
                "date": "2026-09-10",
                "product": "M",
                "exchange": "DCE",
                "contract": "M2701",
                "open": 5000,
                "high": 5050,
                "low": 4950,
                "close": 5010,
                "settle": 5005,
                "volume": 100,
                "open_interest": 200,
            }
        ]
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "default_completed_through",
        lambda observed_at=None: date(2026, 9, 11),
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "_metadata",
        lambda *args, **kwargs: [{"symbol": "M", "exchange": "DCE"}],
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "verify_existing_baseline",
        lambda *args, **kwargs: baseline.copy(),
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "_trading_days_after",
        lambda last_day, through: [date(2026, 9, 11)],
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "fetch_official_day",
        lambda *args, **kwargs: [],
    )

    def blocked(*args, **kwargs):
        raise refresh_daily_market_data.RefreshError(
            "cross_endpoint_anchor_status=mismatch_terminal_ohlcv_oi"
        )

    monkeypatch.setattr(refresh_daily_market_data, "fetch_dce_days", blocked)
    old_daily = daily_path.read_bytes()
    old_manifest = manifest_path.read_bytes()

    with pytest.raises(
        refresh_daily_market_data.RefreshError,
        match="cross_endpoint_anchor_status=mismatch_terminal_ohlcv_oi",
    ):
        refresh_daily_market_data._refresh_unlocked(
            through=date(2026, 9, 11),
            daily_path=daily_path,
            manifest_path=manifest_path,
            workers=1,
        )

    assert daily_path.read_bytes() == old_daily
    assert manifest_path.read_bytes() == old_manifest
    assert list(tmp_path.glob("dce_daily_identity.sha256-*.json")) == []


def test_official_calendar_cannot_be_self_attested_by_a_source_string() -> None:
    with pytest.raises(ValueError, match="不能证明官方交易日历"):
        fetch_futures.fetch(
            "CU",
            timeout=1,
            limit=2,
            completed_trade_date="2026-09-11",
            completed_trade_date_source="official_exchange_calendar",
        )


def test_zero_volume_open_interest_only_far_month_is_skipped() -> None:
    trade_day = date(2026, 9, 9)
    frame = pd.DataFrame(
        [
            {
                "symbol": "CU2704",
                "date": "2026-09-09",
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "settle": 17000,
                "volume": 0,
                "open_interest": 55,
            },
            {
                "symbol": "CU2610",
                "date": "2026-09-09",
                "open": 17100,
                "high": 17200,
                "low": 17000,
                "close": 17150,
                "settle": 17140,
                "volume": 100,
                "open_interest": 500,
            },
        ]
    )
    skipped: list[dict[str, object]] = []

    rows = refresh_daily_market_data.normalize_exchange_frame(
        frame,
        exchange="SHFE",
        trade_day=trade_day,
        expected_products={"CU"},
        skipped_rows=skipped,
    )

    assert [row["contract"] for row in rows] == ["CU2610"]
    assert len(skipped) == 1
    assert skipped[0] == {
        "date": "2026-09-09",
        "exchange": "SHFE",
        "product": "CU",
        "contract": "CU2704",
        "status": "settlement_only_no_trade",
        "volume": 0.0,
        "open_interest": 55.0,
    }


@pytest.mark.parametrize("settle", [None, -1])
def test_invalid_settlement_never_enters_publishable_rows(settle: object) -> None:
    row = {
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100,
        "settle": settle,
        "volume": 100,
        "open_interest": 500,
    }
    assert refresh_daily_market_data._valid_bar(row) is False
    assert refresh_daily_market_data.classify_bar(row) != "valid_trading_bar"


def test_zero_volume_ohlc_filled_with_settlement_cannot_become_main() -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "CU2704",
                "date": "2026-09-09",
                "open": 17000,
                "high": 17000,
                "low": 17000,
                "close": 17000,
                "settle": 17000,
                "volume": 0,
                "open_interest": 600,
            },
            {
                "symbol": "CU2610",
                "date": "2026-09-09",
                "open": 17100,
                "high": 17200,
                "low": 17000,
                "close": 17150,
                "settle": 17140,
                "volume": 100,
                "open_interest": 500,
            },
        ]
    )

    with pytest.raises(refresh_daily_market_data.RefreshError, match="可能主力"):
        refresh_daily_market_data.normalize_exchange_frame(
            frame,
            exchange="SHFE",
            trade_day=date(2026, 9, 9),
            expected_products={"CU"},
        )


def test_dce_symbol_less_history_archives_exact_http_and_cross_endpoint_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "date": "2026-09-10",
            "open": 5100,
            "high": 5150,
            "low": 5080,
            "close": 5120,
            "settle": 5110,
            "volume": 100,
            "open_interest": 200,
        },
        {
            "date": "2026-09-11",
            "open": 5200,
            "high": 5237,
            "low": 5075,
            "close": 5087,
            "settle": 5151,
            "volume": 183927,
            "open_interest": 170135,
        },
    ]
    daily_url = dce_daily_identity._prepared_daily_url("M2701")
    quote_url = dce_daily_identity.QUOTE_URL.format(symbol="M2701")
    calls: list[tuple[str, dict[str, object]]] = []

    def get(url: str, **kwargs):
        calls.append((url, kwargs))
        if url == dce_daily_identity.DAILY_BASE_URL:
            assert kwargs["params"] == {
                "symbol": "M2701",
                "type": dce_daily_identity.DAILY_TYPE,
            }
            return _http_response(_dce_daily_body(rows), daily_url)
        assert url == quote_url
        return _http_response(
            _dce_quote_body("M2701", rows[-1]),
            quote_url,
        )

    monkeypatch.setattr(dce_daily_identity.requests, "get", get)
    parsed, evidence = dce_daily_identity.fetch_bound_contract_history(
        "M2701",
        completed_through=date(2026, 9, 11),
    )

    assert parsed == dce_daily_identity.replay_contract_evidence(evidence)
    assert evidence["daily_response"]["requested_url"] == daily_url
    assert evidence["daily_response"]["request_method"] == "GET"
    assert evidence["daily_response"]["final_url"] == daily_url
    assert evidence["direct_quote_response"]["requested_url"] == quote_url
    assert evidence["terminal_binding"]["matched_fields"] == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    ]
    assert len(calls) == 2
    assert all(call[1]["allow_redirects"] is False for call in calls)


def test_dce_resigned_wrong_instrument_daily_payload_still_fails_cross_binding() -> None:
    target_rows = [
        {
            "date": "2026-09-11",
            "open": 5200,
            "high": 5237,
            "low": 5075,
            "close": 5087,
            "settle": 5151,
            "volume": 183927,
            "open_interest": 170135,
        }
    ]
    evidence = _dce_contract_evidence("M2701", target_rows)
    wrong_rows = [
        {
            "date": "2026-09-11",
            "open": 3000,
            "high": 3050,
            "low": 2950,
            "close": 3010,
            "settle": 3005,
            "volume": 999,
            "open_interest": 888,
        }
    ]
    wrong_body = _dce_daily_body(wrong_rows)
    daily_url = dce_daily_identity._prepared_daily_url("M2701")
    resigned = json.loads(json.dumps(evidence))
    resigned["daily_response"] = dce_daily_identity._archive_response(
        wrong_body,
        requested_url=daily_url,
        response=_http_response(wrong_body, daily_url),
    )
    parsed_wrong = dce_daily_identity.parse_daily_response(wrong_body)
    resigned["daily_row_count"] = len(parsed_wrong)
    resigned["daily_date_range"] = {
        "start": parsed_wrong[0]["date"],
        "end": parsed_wrong[-1]["date"],
    }
    resigned["daily_rows_sha256"] = dce_daily_identity._canonical_rows_sha256(
        parsed_wrong
    )
    resigned["terminal_binding"]["matched_values"] = {
        field: parsed_wrong[-1][field]
        for field in resigned["terminal_binding"]["matched_fields"]
    }

    with pytest.raises(
        dce_daily_identity.DceDailyIdentityError,
        match="cross_endpoint_anchor_status=mismatch_terminal_ohlcv_oi",
    ):
        dce_daily_identity.replay_contract_evidence(resigned)


def test_dce_night_quote_after_daily_close_is_explicitly_blocked() -> None:
    daily_rows = [
        {
            "date": "2026-09-11",
            "open": 5200,
            "high": 5237,
            "low": 5075,
            "close": 5087,
            "settle": 5151,
            "volume": 183927,
            "open_interest": 170135,
        }
    ]

    with pytest.raises(
        dce_daily_identity.DceDailyIdentityError,
        match="cross_endpoint_anchor_status=outside_daily_close_crosscheck_window",
    ):
        _dce_contract_evidence(
            "M2701",
            daily_rows,
            quote_clock="230000",
            quote_overrides={
                "open": 5106,
                "high": 5122,
                "low": 5086,
                "close": 5099,
                "volume": 30289,
                "open_interest": 168656,
            },
        )


@pytest.mark.parametrize("quote_clock", ["146059", "156099", "159960", "210000"])
def test_dce_invalid_or_out_of_window_quote_clock_cannot_verify(
    quote_clock: str,
) -> None:
    rows = [
        {
            "date": "2026-09-11",
            "open": 5200,
            "high": 5237,
            "low": 5075,
            "close": 5087,
            "settle": 5151,
            "volume": 183927,
            "open_interest": 170135,
        }
    ]

    with pytest.raises(dce_daily_identity.DceDailyIdentityError):
        _dce_contract_evidence("M2701", rows, quote_clock=quote_clock)


@pytest.mark.parametrize("quote_clock", ["150000", "205959"])
def test_dce_valid_close_crosscheck_clock_can_bind(quote_clock: str) -> None:
    rows = [
        {
            "date": "2026-09-11",
            "open": 5200,
            "high": 5237,
            "low": 5075,
            "close": 5087,
            "settle": 5151,
            "volume": 183927,
            "open_interest": 170135,
        }
    ]

    evidence = _dce_contract_evidence("M2701", rows, quote_clock=quote_clock)

    assert evidence["identity_status"] == "verified_by_direct_quote_terminal_bar"


def test_dce_cross_binding_compares_archived_decimals_before_float_conversion() -> None:
    rows = [
        {
            "date": "2026-09-11",
            "open": "5200.0000000000001",
            "high": 5237,
            "low": 5075,
            "close": 5087,
            "settle": 5151,
            "volume": 183927,
            "open_interest": 170135,
        }
    ]

    with pytest.raises(
        dce_daily_identity.DceDailyIdentityError,
        match="cross_endpoint_anchor_status=mismatch_terminal_ohlcv_oi",
    ):
        _dce_contract_evidence(
            "M2701",
            rows,
            quote_overrides={"open": "5200.0000000000002"},
        )


def test_dce_http_archive_status_must_be_a_strict_integer() -> None:
    url = dce_daily_identity.QUOTE_URL.format(symbol="M2701")
    body = b"test"
    response = _http_response(body, url)
    response.status_code = 200.0
    with pytest.raises(dce_daily_identity.DceDailyIdentityError, match="status"):
        dce_daily_identity._archive_response(
            body,
            requested_url=url,
            response=response,
        )

    archive = dce_daily_identity._archive_response(
        body,
        requested_url=url,
        response=_http_response(body, url),
    )
    archive["status_code"] = 200.0
    with pytest.raises(dce_daily_identity.DceDailyIdentityError, match="status"):
        dce_daily_identity._restore_response(archive, expected_url=url)


def test_dce_direct_quote_must_identify_the_exact_requested_contract() -> None:
    rows = [
        {
            "date": "2026-09-11",
            "open": 5200,
            "high": 5237,
            "low": 5075,
            "close": 5087,
            "settle": 5151,
            "volume": 183927,
            "open_interest": 170135,
        }
    ]
    daily_url = dce_daily_identity._prepared_daily_url("M2701")
    quote_url = dce_daily_identity.QUOTE_URL.format(symbol="M2701")
    with pytest.raises(
        dce_daily_identity.DceDailyIdentityError,
        match="does not identify contract",
    ):
        dce_daily_identity.bind_contract_http_responses(
            "M2701",
            completed_through=date(2026, 9, 11),
            daily_response=_http_response(_dce_daily_body(rows), daily_url),
            quote_response=_http_response(
                _dce_quote_body("M2705", rows[-1]), quote_url
            ),
        )


def test_dce_missing_contract_day_row_cannot_be_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = [date(2026, 9, 10), date(2026, 9, 11)]
    monkeypatch.setattr(
        refresh_daily_market_data,
        "direct_quote",
        lambda product, timeout: {"product_name": "豆一"},
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "actual_contract_rows",
        lambda product_name, product: [
            {
                "symbol": "M2701",
                "exchange": "DCE",
                "provider_exchange": "dce",
                "exchange_identity_status": "verified_exact",
                "open_interest": 50,
            },
            {
                "symbol": "M2705",
                "exchange": "DCE",
                "provider_exchange": "dce",
                "exchange_identity_status": "verified_exact",
                "open_interest": 500,
            },
        ],
    )

    source_rows = {
        "M2701": [
            {
                "date": day.isoformat(),
                "open": 4000,
                "high": 4020,
                "low": 3990,
                "close": 4010,
                "settle": 4005,
                "volume": 100,
                "open_interest": 50,
            }
            for day in days
        ],
        # The response's first-seen date does not prove that this high-OI
        # contract did not exist on the prior requested trading day.
        "M2705": [
            {
                "date": days[-1].isoformat(),
                "open": 4100,
                "high": 4120,
                "low": 4090,
                "close": 4110,
                "settle": 4105,
                "volume": 100,
                "open_interest": 500,
            }
        ],
    }

    def bound_history(contract: str, **kwargs):
        rows = source_rows[contract]
        return rows, _dce_contract_evidence(contract, rows)

    monkeypatch.setattr(
        refresh_daily_market_data, "fetch_bound_contract_history", bound_history
    )

    with pytest.raises(
        refresh_daily_market_data.RefreshError, match="可能主力"
    ):
        refresh_daily_market_data.fetch_dce_days(
            days,
            products=["M"],
            workers=1,
            baseline_contracts={"M": ["M2701", "M2705"]},
            skipped_rows=[],
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"exchange": "SHFE"}, "exchange必须为DCE"),
        (
            {"provider_exchange": "SHFE"},
            "provider_exchange身份不可验证",
        ),
        ({"provider_exchange": None}, "provider_exchange身份不可验证"),
        (
            {"exchange_identity_status": "provider_exchange_conflict"},
            "exchange_identity_status与重算不一致",
        ),
    ),
)
def test_dce_current_chain_exchange_identity_cannot_be_self_attested(
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
    message: str,
) -> None:
    trade_day = date(2026, 9, 11)
    monkeypatch.setattr(
        refresh_daily_market_data,
        "direct_quote",
        lambda product, timeout: {"product_name": "豆一"},
    )
    chain_row: dict[str, object] = {
        "symbol": "M2701",
        "exchange": "DCE",
        "provider_exchange": "dce",
        "exchange_identity_status": "verified_exact",
    }
    chain_row.update(mutation)
    monkeypatch.setattr(
        refresh_daily_market_data,
        "actual_contract_rows",
        lambda product_name, product: [chain_row],
    )
    history_called = False

    def history(*args, **kwargs):
        nonlocal history_called
        history_called = True
        raise AssertionError("identity failure must block before history fetch")

    monkeypatch.setattr(
        refresh_daily_market_data, "_fetch_sina_contract_history", history
    )

    with pytest.raises(refresh_daily_market_data.RefreshError, match=message):
        refresh_daily_market_data.fetch_dce_days(
            [trade_day],
            products=["M"],
            workers=1,
            baseline_contracts={"M": ["M2701"]},
            skipped_rows=[],
        )
    assert history_called is False


def test_dce_history_exchange_cannot_be_unconditionally_relabelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trade_day = date(2026, 9, 11)
    monkeypatch.setattr(
        refresh_daily_market_data,
        "direct_quote",
        lambda product, timeout: {"product_name": "豆一"},
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "actual_contract_rows",
        lambda product_name, product: [
            {
                "symbol": "M2701",
                "exchange": "DCE",
                "provider_exchange": "dce",
                "exchange_identity_status": "verified_exact",
            }
        ],
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "_fetch_sina_contract_history",
        lambda contract, wanted_days, currently_listed=True: (
            contract,
            [
                {
                    "date": trade_day.isoformat(),
                    "exchange": "SHFE",
                    "contract": contract,
                    "open": 4000,
                    "high": 4020,
                    "low": 3990,
                    "close": 4010,
                    "settle": 4005,
                    "volume": 100,
                    "open_interest": 50,
                }
            ],
            [],
            _dce_contract_evidence(
                contract,
                [
                    {
                        "date": trade_day.isoformat(),
                        "open": 4000,
                        "high": 4020,
                        "low": 3990,
                        "close": 4010,
                        "settle": 4005,
                        "volume": 100,
                        "open_interest": 50,
                    }
                ],
            ),
        ),
    )

    with pytest.raises(refresh_daily_market_data.RefreshError, match="不得重标发布"):
        refresh_daily_market_data.fetch_dce_days(
            [trade_day],
            products=["M"],
            workers=1,
            baseline_contracts={"M": ["M2701"]},
            skipped_rows=[],
        )


def test_dce_history_marks_each_requested_missing_day_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_rows = [
        {
            "date": "2026-09-10",
            "open": 4000,
            "high": 4020,
            "low": 3990,
            "close": 4010,
            "settle": 4005,
            "volume": 100,
            "open_interest": 50,
        }
    ]
    monkeypatch.setattr(
        refresh_daily_market_data,
        "fetch_bound_contract_history",
        lambda contract, **kwargs: (
            source_rows,
            _dce_contract_evidence(contract, source_rows),
        ),
    )

    _, values, skipped, evidence = (
        refresh_daily_market_data._fetch_sina_contract_history(
            "M2701", {date(2026, 9, 9), date(2026, 9, 10)}
        )
    )

    assert [row["date"] for row in values] == ["2026-09-10"]
    assert dce_daily_identity.replay_contract_evidence(evidence) == source_rows
    assert skipped == [
        {
            "date": "2026-09-09",
            "exchange": "DCE",
            "product": "M",
            "contract": "M2701",
            "status": "missing_contract_day_row",
            "volume": None,
            "open_interest": None,
            "endpoint_first_returned_date": "2026-09-10",
            "endpoint_last_returned_date": "2026-09-10",
            "currently_listed_in_latest_chain": True,
            "official_lifecycle_evidence": None,
        }
    ]


def test_dce_contract_lifecycle_is_not_inferred_from_endpoint_first_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = [date(2026, 9, 9), date(2026, 9, 10)]
    monkeypatch.setattr(
        refresh_daily_market_data,
        "direct_quote",
        lambda product, timeout: {"product_name": "豆一"},
    )
    monkeypatch.setattr(
        refresh_daily_market_data,
        "actual_contract_rows",
        lambda product_name, product: [
            {
                "symbol": "M2701",
                "exchange": "DCE",
                "provider_exchange": "dce",
                "exchange_identity_status": "verified_exact",
            }
        ],
    )

    def history(contract: str, **kwargs):
        rows = [
            {
                "date": "2026-09-10",
                "open": 4000,
                "high": 4020,
                "low": 3990,
                "close": 4010,
                "settle": 4005,
                "volume": 100,
                "open_interest": 500,
            }
        ]
        return rows, _dce_contract_evidence(contract, rows)

    monkeypatch.setattr(
        refresh_daily_market_data, "fetch_bound_contract_history", history
    )
    skipped: list[dict[str, object]] = []

    with pytest.raises(
        refresh_daily_market_data.RefreshError, match="品种覆盖不完整"
    ):
        refresh_daily_market_data.fetch_dce_days(
            days,
            products=["M"],
            workers=1,
            baseline_contracts={"M": ["M2609"]},
            skipped_rows=skipped,
        )

    assert {item["status"] for item in skipped} == {"missing_contract_day_row"}
    assert all(item["official_lifecycle_evidence"] is None for item in skipped)


@pytest.mark.parametrize("excluded_open_interest", [None, 600])
def test_invalid_bar_blocks_when_it_could_change_main_selection(
    excluded_open_interest: int | None,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "CU2704",
                "date": "2026-09-09",
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "settle": 17000,
                "volume": 0,
                "open_interest": excluded_open_interest,
            },
            {
                "symbol": "CU2610",
                "date": "2026-09-09",
                "open": 17100,
                "high": 17200,
                "low": 17000,
                "close": 17150,
                "settle": 17140,
                "volume": 100,
                "open_interest": 500,
            },
        ]
    )

    with pytest.raises(refresh_daily_market_data.RefreshError, match="可能主力"):
        refresh_daily_market_data.normalize_exchange_frame(
            frame,
            exchange="SHFE",
            trade_day=date(2026, 9, 9),
            expected_products={"CU"},
        )


def test_product_still_fails_when_it_has_no_valid_bar() -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "CU2704",
                "date": "2026-09-09",
                "open": None,
                "high": None,
                "low": None,
                "close": None,
                "volume": 0,
                "open_interest": 55,
            }
        ]
    )
    with pytest.raises(refresh_daily_market_data.RefreshError, match="覆盖不完整"):
        refresh_daily_market_data.normalize_exchange_frame(
            frame,
            exchange="SHFE",
            trade_day=date(2026, 9, 9),
            expected_products={"CU"},
        )


def _contract(symbol: str, oi: int, price: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "exchange": "SHFE",
        "provider_trade_date": "2026-09-14",
        "price": price,
        "open": price - 10,
        "high": price + 10,
        "low": price - 20,
        "bid": price - 5,
        "ask": price + 5,
        "previous_settlement": price - 10,
        "change_percent": 0.1,
        "volume": 100,
        "open_interest": oi,
        "quote_time": "2026-09-11 15:00:00",
        "data_source": "fixture",
        "source_url": "https://example.test",
    }


@pytest.mark.parametrize(
    ("request_symbol", "expected_analysis", "expected_mode"),
    [
        ("CU2610", "CU2610", "requested_specific_contract"),
        ("CU", "CU2611", "product_actual_main"),
    ],
)
def test_fetch_separates_actual_main_from_analysis_contract(
    monkeypatch: pytest.MonkeyPatch,
    request_symbol: str,
    expected_analysis: str,
    expected_mode: str,
) -> None:
    calls: list[str] = []
    chain = [_contract("CU2610", 500, 10000), _contract("CU2611", 1000, 10100)]
    monkeypatch.setattr(
        fetch_futures,
        "now_beijing",
        lambda: datetime.fromisoformat("2026-09-13T10:00:00+08:00"),
    )
    monkeypatch.setattr(
        fetch_futures,
        "direct_quote",
        lambda symbol, timeout: {
            "symbol": symbol,
            "product_name": "铜",
            "price": 10000,
            "quote_time": "2026-09-11 15:00:00",
        },
    )
    monkeypatch.setattr(fetch_futures, "actual_contract_rows", lambda *args: chain)
    monkeypatch.setattr(fetch_futures, "completed_chain_close_asof", lambda *args: None)

    def fake_technical(symbol, live_price, *args, **kwargs):
        calls.append(symbol)
        return {
            "symbol": symbol,
            "last_daily_date": "2026-09-11",
            "last_daily_close": live_price,
            "last_daily_open_interest": 400,
            "open_interest_change_1d": 1,
            "daily_data_evidence": {"status": "verified"},
        }

    monkeypatch.setattr(fetch_futures, "technical_snapshot", fake_technical)
    monkeypatch.setattr(
        fetch_futures,
        "contract_tick_size_evidence",
        lambda symbol, timeout=20: _exact_tick_evidence(symbol, 10.0),
    )

    result = fetch_futures.fetch(request_symbol, timeout=1, limit=12)
    compact = fetch_futures.compact_result(result)

    assert result["main_contract"]["symbol"] == "CU2611"
    assert result["actual_main"]["symbol"] == "CU2611"
    assert result["analysis_contract"]["symbol"] == expected_analysis
    assert result["analysis_mode"] == expected_mode
    assert result["technical"]["symbol"] == expected_analysis
    assert calls == [expected_analysis]
    assert [row["symbol"] for row in result["actual_contract_chain"]] == [
        "CU2610",
        "CU2611",
    ]
    assert result["main_selection"]["available_contract_count"] == 2
    assert result["main_selection"]["rankable_contract_count"] == 2
    assert result["main_selection"]["source_record_count"] == 2
    assert result["main_selection"]["chain_complete_for_product"] is False
    assert compact["actual_main"]["symbol"] == "CU2611"
    assert compact["analysis_contract"]["symbol"] == expected_analysis
    assert compact["analysis_mode"] == expected_mode


def test_configured_research_tick_policy_does_not_call_third_party_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_futures, "now_beijing", _fixture_market_time)
    def unexpected_call(*args, **kwargs):
        raise AssertionError("全市场宽筛不应请求第三方合约详情页")

    monkeypatch.setattr(
        fetch_futures, "contract_tick_size_evidence", unexpected_call
    )

    evidence, warning = fetch_futures.resolved_tick_size_evidence(
        "CU2610", timeout=1, policy="configured_research"
    )

    assert evidence["kind"] == "product_specs_snapshot_proxy"
    assert evidence["tick_size"] == 10
    assert evidence["exact"] is False
    assert warning is None


def test_third_party_tick_disagreement_downgrades_to_configured_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_futures, "now_beijing", _fixture_market_time)
    monkeypatch.setattr(
        fetch_futures,
        "contract_tick_size_evidence",
        lambda symbol, timeout=20: _exact_tick_evidence(symbol, 20),
    )

    evidence, warning = fetch_futures.resolved_tick_size_evidence(
        "CU2610", timeout=1, policy="third_party_cross_check"
    )

    assert evidence["kind"] == "product_specs_snapshot_proxy"
    assert evidence["tick_size"] == 10
    assert evidence["exact"] is False
    assert warning is not None
    assert "已忽略第三方值" in warning
    assert "不再标记为行情字段冲突" in warning


def test_fetch_does_not_hide_unknown_oi_when_naming_available_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = _contract("CU2701", 1, 17000)
    unknown["open_interest"] = None
    unknown["chain_row_status"] = "open_interest_unobservable"
    observed = _contract("CU2610", 500, 17100)
    observed["chain_row_status"] = "rankable_positive_open_interest"
    chain = [unknown, observed]
    monkeypatch.setattr(
        fetch_futures,
        "now_beijing",
        lambda: datetime.fromisoformat("2026-09-13T10:00:00+08:00"),
    )
    monkeypatch.setattr(
        fetch_futures,
        "direct_quote",
        lambda symbol, timeout: {
            "symbol": symbol,
            "product_name": "铜",
            "price": 17100,
            "quote_time": "2026-09-11 15:00:00",
        },
    )
    monkeypatch.setattr(fetch_futures, "actual_contract_rows", lambda *args: chain)
    monkeypatch.setattr(fetch_futures, "completed_chain_close_asof", lambda *args: None)
    monkeypatch.setattr(
        fetch_futures,
        "technical_snapshot",
        lambda symbol, live_price, *args, **kwargs: {
            "symbol": symbol,
            "last_daily_date": "2026-09-11",
            "last_daily_close": live_price,
            "last_daily_open_interest": 500,
            "open_interest_change_1d": 0,
            "daily_data_evidence": {"status": "verified"},
        },
    )
    monkeypatch.setattr(
        fetch_futures,
        "contract_tick_size_evidence",
        lambda symbol, timeout=20: _exact_tick_evidence(symbol, 5.0),
    )

    result = fetch_futures.fetch("CU", timeout=1, limit=12)

    assert result["actual_main"]["symbol"] == "CU2610"
    assert len(result["actual_contract_chain"]) == 2
    selection = result["main_selection"]
    assert selection["source_record_count"] == 2
    assert selection["available_contract_count"] == 1
    assert selection["unknown_open_interest_count"] == 1
    assert selection["selection_claim"] == "provisional_leader_of_observed_oi_rows"
    assert selection["selection_uncertainty"]["unknown_open_interest_contracts"] == [
        "CU2701"
    ]
    assert any("持仓量不可观测" in warning for warning in result["warnings"])


def test_explicit_contract_survives_when_actual_main_oi_is_unobservable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _contract("CU2610", 0, 10000)
    row["open_interest"] = None
    row["chain_row_status"] = "open_interest_unobservable"
    monkeypatch.setattr(
        fetch_futures,
        "now_beijing",
        lambda: datetime.fromisoformat("2026-09-13T10:00:00+08:00"),
    )
    monkeypatch.setattr(
        fetch_futures,
        "direct_quote",
        lambda symbol, timeout: {
            "symbol": symbol,
            "product_name": "铜",
            "price": 10000,
            "quote_time": "2026-09-11 15:00:00",
        },
    )
    monkeypatch.setattr(fetch_futures, "actual_contract_rows", lambda *args: [row])
    monkeypatch.setattr(fetch_futures, "completed_chain_close_asof", lambda *args: None)
    monkeypatch.setattr(
        fetch_futures,
        "technical_snapshot",
        lambda symbol, live_price, *args, **kwargs: {
            "symbol": symbol,
            "last_daily_date": "2026-09-11",
            "last_daily_close": live_price,
            "last_daily_open_interest": 100,
            "open_interest_change_1d": 0,
            "daily_data_evidence": {"status": "verified"},
        },
    )
    monkeypatch.setattr(
        fetch_futures,
        "contract_tick_size_evidence",
        lambda symbol, timeout=20: _exact_tick_evidence(symbol, 10),
    )

    result = fetch_futures.fetch("CU2610", timeout=1, limit=12)

    assert result["actual_main"] is None
    assert result["main_contract"] is None
    assert result["analysis_contract"]["symbol"] == "CU2610"
    assert result["analysis_mode"] == "requested_specific_contract"
    assert result["main_selection"]["rankable_contract_count"] == 0
    assert result["main_selection"]["selection_claim"] == "unobservable_no_rankable_row"


def test_unknown_open_interest_row_is_preserved_and_selection_is_provisional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "CU2701",
                "position": None,
                "volume": 999,
                "trade": 17000,
                "presettlement": 16900,
                "tradedate": "2026-09-11",
                "ticktime": "15:00:00",
                "exchange": "shfe",
            },
            {
                "symbol": "CU2610",
                "position": 500,
                "volume": 100,
                "trade": 17100,
                "presettlement": 17000,
                "tradedate": "2026-09-11",
                "ticktime": "15:00:00",
                "exchange": "shfe",
            },
        ]
    )
    monkeypatch.setattr(fetch_futures.ak, "futures_zh_realtime", lambda name: frame)

    rows = fetch_futures.actual_contract_rows("铜", "CU")
    ranked = fetch_futures.rank_main_contract_rows(rows)

    assert [row["symbol"] for row in rows] == ["CU2701", "CU2610"]
    assert rows[0]["open_interest"] is None
    assert rows[0]["chain_row_status"] == "open_interest_unobservable"
    assert ranked[0]["symbol"] == "CU2610"


def test_provider_exchange_conflict_is_preserved_and_not_rankable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "CU2610",
                "position": 500,
                "volume": 100,
                "trade": 17100,
                "presettlement": 17000,
                "tradedate": "2026-09-11",
                "ticktime": "15:00:00",
                "exchange": "dce",
            }
        ]
    )
    monkeypatch.setattr(fetch_futures.ak, "futures_zh_realtime", lambda name: frame)

    rows = fetch_futures.actual_contract_rows("铜", "CU")

    assert rows[0]["exchange"] == "SHFE"
    assert rows[0]["provider_exchange"] == "dce"
    assert rows[0]["exchange_identity_status"] == "provider_exchange_conflict"
    assert rows[0]["chain_row_status"] == "provider_exchange_conflict"
    assert fetch_futures.rank_main_contract_rows(rows) == []


def test_future_trade_date_is_only_normalized_for_explainable_night_session() -> None:
    stale_same_day = {
        "provider_quote_time": "2026-09-11 10:00:00",
        "quote_time": "2026-09-11 10:00:00",
    }
    fetch_futures.normalize_chain_calendar_observation(
        stale_same_day,
        direct_quote_time="2026-09-11 15:00:00",
        product="M",
    )
    assert stale_same_day["quote_time_normalization"] == (
        "unexplained_provider_direct_time_divergence"
    )

    night_row = {
        "provider_quote_time": "2026-09-14 23:00:00",
        "quote_time": "2026-09-14 23:00:00",
    }
    fetch_futures.normalize_chain_calendar_observation(
        night_row,
        direct_quote_time="2026-09-11 23:00:00",
        product="M",
    )
    assert night_row["provider_quote_time"] == "2026-09-14 23:00:00"
    assert night_row["quote_time"] == "2026-09-11 23:00:00"
    assert night_row["quote_time_normalization"].startswith("derived_calendar_time")

    no_night_row = {
        "provider_quote_time": "2026-09-14 23:00:00",
        "quote_time": "2026-09-14 23:00:00",
    }
    fetch_futures.normalize_chain_calendar_observation(
        no_night_row,
        direct_quote_time="2026-09-11 23:00:00",
        product="AP",
    )
    assert no_night_row["quote_time"] == "2026-09-14 23:00:00"
    assert no_night_row["quote_time_normalization"] == (
        "unexplained_future_provider_trade_date"
    )

    wrong_day_row = {
        "provider_quote_time": "2026-09-15 23:00:00",
        "quote_time": "2026-09-15 23:00:00",
    }
    fetch_futures.normalize_chain_calendar_observation(
        wrong_day_row,
        direct_quote_time="2026-09-11 23:00:00",
        product="M",
    )
    assert wrong_day_row["quote_time"] == "2026-09-15 23:00:00"
    assert wrong_day_row["quote_time_normalization"] == (
        "unexplained_future_provider_trade_date"
    )

    weekend_row = {
        "provider_quote_time": "2026-09-14 21:00:00",
        "quote_time": "2026-09-14 21:00:00",
    }
    fetch_futures.normalize_chain_calendar_observation(
        weekend_row,
        direct_quote_time="2026-09-12 21:00:00",
        product="M",
    )
    assert weekend_row["quote_time"] == "2026-09-14 21:00:00"
    assert weekend_row["quote_time_normalization"] == (
        "unexplained_future_provider_trade_date"
    )


@pytest.mark.parametrize(
    ("product", "clock", "expected_derived"),
    (
        ("M", "02:00:00", False),
        ("CU", "00:30:00", True),
        ("CU", "01:30:00", False),
        ("CU", "02:00:00", False),
    ),
)
def test_night_calendar_normalization_uses_product_specific_close(
    product: str, clock: str, expected_derived: bool
) -> None:
    row = {
        "provider_quote_time": f"2026-09-14 {clock}",
        "quote_time": f"2026-09-14 {clock}",
    }
    fetch_futures.normalize_chain_calendar_observation(
        row,
        direct_quote_time=f"2026-09-12 {clock}",
        product=product,
    )
    assert row["quote_time_normalization"].startswith("derived") is expected_derived
    assert row["quote_time"] == (
        f"2026-09-12 {clock}" if expected_derived else f"2026-09-14 {clock}"
    )


def test_synthetic_or_impossible_delivery_month_is_rejected() -> None:
    for symbol in ("M8888", "M9999", "M2713", "M0001", "CU9912"):
        with pytest.raises(ValueError):
            fetch_futures.normalize_symbol(symbol)


def test_provider_native_czce_code_is_canonicalized_at_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "MA701",
                "position": 500,
                "volume": 100,
                "trade": 5100,
                "presettlement": 5050,
                "tradedate": "2026-09-11",
                "ticktime": "15:00:00",
            }
        ]
    )
    monkeypatch.setattr(fetch_futures.ak, "futures_zh_realtime", lambda name: frame)

    rows = fetch_futures.actual_contract_rows("甲醇", "MA")

    assert rows[0]["symbol"] == "MA2701"


def test_czce_refresh_filters_out_of_scope_products_before_strict_canonicalization() -> None:
    frame = pd.DataFrame(
        [
            {
                "symbol": "CY609",
                "date": "2026-09-09",
                "open": 20000,
                "high": 20100,
                "low": 19900,
                "close": 20050,
                "settle": 20040,
                "volume": 50,
                "open_interest": 100,
            },
            {
                "symbol": "MA701",
                "date": "2026-09-09",
                "open": 5100,
                "high": 5120,
                "low": 5080,
                "close": 5110,
                "settle": 5105,
                "volume": 100,
                "open_interest": 500,
            },
        ]
    )

    rows = refresh_daily_market_data.normalize_exchange_frame(
        frame,
        exchange="CZCE",
        trade_day=date(2026, 9, 9),
        expected_products={"MA"},
    )

    assert [row["contract"] for row in rows] == ["MA2701"]


def test_czce_symbol_keeps_canonical_four_digits_at_sina_outbound_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []
    fields = [""] * 19
    fields[1] = "150000"
    fields[8] = "5100"
    fields[10] = "5050"
    fields[13] = "500"
    fields[14] = "100"
    fields[16] = "甲醇"
    fields[17] = "2026-09-11"

    class QuoteResponse:
        content = f'var hq_str_nf_MA2701="{",".join(fields)}";'.encode("gb18030")

        @staticmethod
        def raise_for_status() -> None:
            return None

    class DetailResponse:
        content = _sina_contract_page_html("MA2701", 2)
        url = fetch_futures.CONTRACT_DETAIL_URL.format(symbol="MA2701")
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

    def get(url: str, **kwargs):
        requested_urls.append(url)
        return QuoteResponse() if "hq.sinajs.cn" in url else DetailResponse()

    monkeypatch.setattr(fetch_futures.requests, "get", get)

    quote = fetch_futures.direct_quote("MA2701", timeout=1)
    tick = fetch_futures.contract_tick_size("MA2701")

    assert requested_urls == [
        fetch_futures.QUOTE_URL.format(symbol="MA2701"),
        fetch_futures.CONTRACT_DETAIL_URL.format(symbol="MA2701"),
    ]
    assert quote["symbol"] == "MA2701"
    assert quote["provider_symbol"] == "MA2701"
    assert tick == 2


def test_legacy_czce_three_digit_code_is_inbound_only_and_dated() -> None:
    assert fetch_futures.canonical_delivery_contract(
        "MA701",
        exchange="CZCE",
        reference_date=date(2026, 9, 11),
        allow_czce_three_digit=True,
    ) == "MA2701"
    with pytest.raises(ValueError):
        fetch_futures.provider_symbol_for_sina("MA701")
    with pytest.raises(ValueError, match="three-digit CZCE code"):
        fetch_futures.sina_provider_symbol_for_contract("CF701")
    assert fetch_futures.provider_symbol_for_sina("CF2701") == "CF2701"
    # The inbound compatibility path has the same inclusive 0..48-month
    # horizon as canonical four-digit contracts, including the year+4 edge.
    assert fetch_futures.canonical_delivery_contract(
        "MA008",
        exchange="CZCE",
        reference_date=date(2026, 9, 11),
        allow_czce_three_digit=True,
    ) == "MA3008"
    assert fetch_futures.canonical_delivery_contract(
        "MA009",
        exchange="CZCE",
        reference_date=date(2026, 9, 11),
        allow_czce_three_digit=True,
    ) == "MA3009"
    with pytest.raises(ValueError):
        fetch_futures.canonical_delivery_contract(
            "MA010",
            exchange="CZCE",
            reference_date=date(2026, 9, 11),
            allow_czce_three_digit=True,
        )


def _bars(count: int = 8) -> list[Bar]:
    start = date(2026, 8, 1)
    return [
        Bar(
            at=start + timedelta(days=index),
            open=50 + index * 0.2,
            high=52 + index * 0.2,
            low=48 + index * 0.2,
            close=51 + index * 0.2,
            volume=100,
            open_interest=200,
        )
        for index in range(count)
    ]


def _pivot(kind: str, price: float, index: int, bars: list[Bar]) -> Pivot:
    return Pivot(
        kind=kind,
        price=price,
        pivot_at=bars[index].at,
        confirmed_at=bars[index + 1].at,
        available_at=bars[index + 1].at,
        pivot_index=index,
        confirmed_index=index + 1,
    )


def test_trend_projection_is_directionally_tick_aligned() -> None:
    bars = _bars()
    support = market_structure.project_trend_line(
        bars,
        [
            _pivot("low", 10.2, 1, bars),
            _pivot("low", 11.3, 3, bars),
            _pivot("low", 12.7, 5, bars),
        ],
        tick_size=1,
    )
    resistance = market_structure.project_trend_line(
        bars,
        [
            _pivot("high", 20.8, 1, bars),
            _pivot("high", 19.5, 3, bars),
            _pivot("high", 18.1, 5, bars),
        ],
        tick_size=1,
    )

    assert support.side == "support"
    assert support.value <= support.raw_projection < support.value + 1
    assert support.value % 1 == 0
    assert resistance.side == "resistance"
    assert resistance.value - 1 < resistance.raw_projection <= resistance.value
    assert resistance.value % 1 == 0


@pytest.mark.parametrize(
    ("tick", "raw", "expected_down", "expected_up"),
    [
        ("0.02", 0.691, "0.68", "0.70"),
        ("0.05", 0.141, "0.10", "0.15"),
    ],
)
def test_decimal_ticks_remain_exactly_aligned(
    tick: str,
    raw: float,
    expected_down: str,
    expected_up: str,
) -> None:
    down = market_structure._round_down(raw, float(tick))
    up = market_structure._round_up(raw, float(tick))

    assert Decimal(str(down)) == Decimal(expected_down)
    assert Decimal(str(up)) == Decimal(expected_up)
    assert Decimal(str(down)) % Decimal(tick) == 0
    assert Decimal(str(up)) % Decimal(tick) == 0


def test_structure_uses_analysis_contract_and_preserves_data_precision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_futures, "now_beijing", _fixture_market_time)
    rows = [
        {
            "date": bar.at.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "hold": bar.open_interest,
            "completed": True,
        }
        for bar in _bars(12)
    ]
    payload = {
        "success": True,
        "request_started_at": "2026-09-13T09:59:59+08:00",
        "fetch_time": "2026-09-13T10:00:00+08:00",
        "main_contract": {"symbol": "CU2611", "price": 60},
        "actual_main": {"symbol": "CU2611", "price": 60},
        "analysis_contract": {"symbol": "CU2610", "price": 53},
        "analysis_mode": "requested_specific_contract",
        "completed_trade_date_evidence": {
            "date": rows[-1]["date"],
            "source": "template_inferred",
        },
        "technical": {
            "symbol": "CU2610",
            "tick_size_contract": "CU2610",
            "tick_size_source": "fixture same-product specification",
            "tick_size": 10,
            "tick_size_exact": False,
            "tick_size_precision": "approximate_same_product_proxy",
            "tick_size_evidence": {
                **fetch_futures.product_spec_tick_size_evidence("CU2610"),
                "retrieved_at": "2026-09-13T10:00:00+08:00",
            },
            "last_daily_close": rows[-1]["close"],
            "completed_daily_rows": rows,
            "daily_data_evidence": {
                "status": "degraded",
                "bound_completed_trade_date": rows[-1]["date"],
            },
        },
    }

    result = market_structure._compact_fetch_structure(payload)

    assert result["schema_version"] == "cn_futures_market_structure.v1"
    assert (
        result["algorithm_version"]
        == "causal_pivots_zones_theil_sen_tick_aligned.v1"
    )
    assert result["schema_version"] == market_structure.STRUCTURE_SCHEMA_VERSION
    assert result["algorithm_version"] == market_structure.STRUCTURE_ALGORITHM_VERSION
    assert result["actual_main"] == "CU2611"
    assert result["analysis_contract"] == "CU2610"
    assert result["analysis_matches_actual_main"] is False
    assert result["reference_price"] == 53
    assert result["daily_data_evidence"]["status"] == "degraded"
    assert result["tick_size_actionability"] == "research_only_not_for_exact_order"
    assert "raw_projection" in result["trend"]
    assert "tick_alignment" in result["trend"]

    forged = json.loads(json.dumps(payload))
    forged["technical"]["tick_size_evidence"]["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="proxy tick evidence provenance"):
        market_structure._compact_fetch_structure(forged)


def test_market_structure_cli_binds_exact_input_file_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch_futures, "now_beijing", _fixture_market_time)
    rows = [
        {
            "date": bar.at.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "hold": bar.open_interest,
            "completed": True,
        }
        for bar in _bars(12)
    ]
    payload = {
        "success": True,
        "request_started_at": "2026-09-13T09:59:59+08:00",
        "fetch_time": "2026-09-13T10:00:00+08:00",
        "actual_main": {"symbol": "CU2611", "price": 60},
        "analysis_contract": {"symbol": "CU2610", "price": 53},
        "analysis_mode": "requested_specific_contract",
        "completed_trade_date_evidence": {"date": rows[-1]["date"]},
        "technical": {
            "symbol": "CU2610",
            "tick_size_contract": "CU2610",
            "tick_size_source": "fixture actual-contract specification",
            "tick_size": 10,
            "tick_size_exact": True,
            "tick_size_precision": "exact_actual_contract",
            "tick_size_evidence": _exact_tick_evidence("CU2610", 10),
            "last_daily_close": rows[-1]["close"],
            "completed_daily_rows": rows,
            "daily_data_evidence": {
                "status": "verified",
                "bound_completed_trade_date": rows[-1]["date"],
            },
        },
    }
    snapshot = tmp_path / "raw.json"
    snapshot.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    assert market_structure.main([str(snapshot)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["input_snapshot_sha256"] == hashlib.sha256(
        snapshot.read_bytes()
    ).hexdigest()

    forged = json.loads(json.dumps(payload))
    forged["technical"]["tick_size_evidence"].update(
        {
            "provider_symbol": "CU9999",
            "source_url": "https://finance.sina.com.cn/futures/quotes/CU9999.shtml",
        }
    )
    with pytest.raises(ValueError, match="exact tick evidence provenance"):
        market_structure._compact_fetch_structure(forged)


def test_refresh_blocked_status_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    args = SimpleNamespace(
        through=date(2026, 9, 11),
        daily=Path("daily.csv.gz"),
        manifest=Path("manifest.json"),
        workers=1,
        dry_run=True,
    )
    monkeypatch.setattr(refresh_daily_market_data, "parse_args", lambda: args)
    monkeypatch.setattr(
        refresh_daily_market_data,
        "refresh",
        lambda **kwargs: {"status": "blocked", "unavailable_reason": "fixture"},
    )
    assert refresh_daily_market_data.main() == 2

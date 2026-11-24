import json
from pathlib import Path

from app.agent_replay import digest
from app.product_history import _frozen, detail, index

import pytest


def write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


def test_records_use_product_specific_evidence_and_explain_missing_quotes(tmp_path):
    write(tmp_path / "config/product_contract_specs.json", {"products": [
        {"name": "苹果", "symbol": "AP", "exchange": "CZCE"},
        {"name": "白糖", "symbol": "SR", "exchange": "CZCE"},
    ]})
    day = "2026-07-01"
    packet = {
        "trade_date": day, "daily_data_date": "2026-06-30", "products": {
            "AP": {"technical": {"contract": "AP2610", "close": 8000}}, "SR": {"technical": None},
        }, "documents": [
            {"products": ["AP"], "title": "苹果仓单", "excerpt": "苹果增加两张。", "url": "https://example.com/ap"}
        ],
    }
    row = {"packet": packet, "recorded_at": "2026-06-30T20:05:00+08:00", "context_sha256": digest(packet),
           "decisions": {"A": {"product": "AP", "action": "wait", "thesis": "等待苹果仓单更新。"}}}
    write(tmp_path / f"local-data/rolling-six/decisions/{day}.json", {**row, "sha256": digest(row)})
    assert [p["name"] for p in index(tmp_path)["products"]] == ["苹果", "白糖"]
    ap = detail(tmp_path, "ap")["records"][0]
    assert ap["day"] == day and ap["sources"][0]["url"].endswith("/ap")
    assert any(s["text"] == "等待苹果仓单更新。" for s in ap["sections"])
    sr = detail(tmp_path, "SR")["records"][0]
    assert not sr["sources"]
    assert any("未取得" in s["text"] for s in sr["sections"])
    assert "苹果" not in str(sr)
    with pytest.raises(KeyError):
        detail(tmp_path, "../")


def test_new_receipt_checks_context_instead_of_serving_changed_history(tmp_path):
    file = tmp_path / "2026-07-01.json"
    row = {"version": "fixed-10000-split-v2", "day": file.stem, "cutoff": "2026-06-30T20:00:00+08:00",
           "context_sha256": "invalid", "packet": {"trade_date": file.stem, "strategy_version": "fixed-10000-split-v2",
                                                   "cutoff": "2026-06-30T20:00:00+08:00"}}
    write(file, row)
    with pytest.raises(ValueError, match="摘要"):
        _frozen(file, file.stat().st_mtime_ns, "新版策略", file.stem)


def test_latest_fixed_prefetch_supersedes_rolling_snapshot_for_same_trade_day(tmp_path):
    write(tmp_path / "config/product_contract_specs.json", {"products": [
        {"name": "苹果", "symbol": "AP", "exchange": "CZCE"},
    ]})
    day = "2026-07-02"
    for folder, stamp, price in (("rolling-six", "1700", 4000), ("july-strategy", "1600", 4100), ("july-strategy", "1800", 4200)):
        row = {"recorded_at": f"2026-07-01T{stamp[:2]}:00:00+08:00",
               "packet": {"trade_date": day, "daily_data_date": "2026-07-01", "products": {
                   "AP": {"technical": {"contract": "AP2610", "close": price}},
               }}}
        write(tmp_path / f"local-data/{folder}/staged/{day}/{stamp}.json", {**row, "sha256": digest(row)})
    observed, = detail(tmp_path, "AP")["records"]
    assert observed["day"] == "2026-07-01"
    assert observed["kind"] == "万元策略 · 收盘资料采集"
    assert "4200" in str(observed["sections"])

"""核对固定预算曲线的日期连续性、收盘归档失败保护和当日判断边界。"""

import gzip
import json
from datetime import datetime
from hashlib import sha256

import pytest
from app import strategy_overview
from app.agent_replay import digest
from app.agent_replay_account import FIXED_STRATEGY_VERSION

from scripts import july_settlement
from scripts.agent_replay import atomic


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-11-24T18:00:00+08:00").astimezone(tz)

    monkeypatch.setattr(july_settlement, "datetime", Clock)
    monkeypatch.setattr(strategy_overview, "datetime", Clock)


def blank_study(root, days):
    folder = root / "local-data/agent-study" / july_settlement.BATCH / "market"
    folder.mkdir(parents=True)
    bars = {day: {"future": {}, "option": {}} for day in days}
    blob = gzip.compress(json.dumps(bars).encode())
    (folder / "market.json.gz").write_bytes(blob)
    manifest = {"data_sha256": sha256(blob).hexdigest(), "sources": []}
    atomic(folder / "source-manifest.json", manifest)
    atomic(
        folder.parent / "run-manifest.json",
        {
            "market_manifest_sha256": digest(manifest),
            "market_sha256": manifest["data_sha256"],
        },
    )


def test_gap_stops_three_curves_and_later_failure_preserves_published_day(
    tmp_path, monkeypatch
):
    days = ["2026-07-01", "2026-07-02", "2026-07-03"]
    blank_study(tmp_path, days)
    folder = tmp_path / "local-data/july-strategy"
    for day in (days[0], days[2]):
        atomic(folder / "decisions-v2" / f"{day}.json", {"day": day})
    monkeypatch.setattr(july_settlement, "trading_days", lambda _: days)
    monkeypatch.setattr(
        july_settlement,
        "read_receipt",
        lambda path: {
            "day": path.stem,
            "packet": {},
            "cutoff": "2026-06-30T20:00:00+08:00",
            "decisions": {g: {"action": "wait", "thesis": "观望"} for g in "ABC"},
            "decision_sha256": "a" * 64,
            "mode": "historical_replay",
        },
    )
    report = july_settlement.refresh(tmp_path)
    assert report["end"] == days[0]
    assert any(days[1] in issue for issue in report["issues"])
    for group in report["groups"].values():
        assert [p["day"] for p in group["points"]] == days[:1]
        assert group["points"][0]["equity"] == 200000

    original = (
        tmp_path
        / "local-data/agent-study"
        / july_settlement.BATCH
        / "market/market.json.gz"
    )
    original.write_bytes(b"invalid")
    failed = july_settlement.refresh(tmp_path)
    assert failed["groups"] == report["groups"] and failed["end"] == days[0]
    assert "摘要不符" in failed["issues"][0]


def test_overview_requires_same_day_decision_and_fixed_version(tmp_path):
    folder = tmp_path / "local-data/july-strategy"
    atomic(folder / "decisions-v2/2026-07-01.json", {"day": "2026-07-01"})
    report = strategy_overview.overview(tmp_path)
    assert report["end"] is None and len(report["strategies"]) == 3
    for group in report["strategies"]:
        assert group["decision"]["day"] == report["as_of"]
        assert group["decision"]["status"] == "暂无当日决策" and group["curve"] == []
        assert group["version"] == FIXED_STRATEGY_VERSION


def test_overview_exposes_current_collection_failure_and_latest_due_session(
    tmp_path, monkeypatch
):
    folder = tmp_path / "local-data/july-strategy"
    report = {
        "version": FIXED_STRATEGY_VERSION,
        "end": "2026-11-23",
        "issues": [],
        "groups": {},
    }
    atomic(folder / "settlement-v2.json", report)
    monkeypatch.setattr(
        strategy_overview,
        "trading_days",
        lambda _: ["2026-11-23", "2026-11-24", "2026-11-25"],
    )
    status = {
        "checked_at": "2026-11-24T17:00:00+08:00",
        "status": "pending",
        "reason": "AP 的委托行情待采集",
    }
    atomic(folder / "daily-status.json", status)
    issues = strategy_overview.overview(tmp_path)["issues"]
    assert any("应结算至 2026-11-24" in v and "截至 2026-11-23" in v for v in issues)
    assert any("17:00" in v and "AP 的委托行情待采集" in v for v in issues)
    status["checked_at"] = "2026-11-23T17:00:00+08:00"
    atomic(folder / "daily-status.json", status)
    assert not any(
        "AP 的委托行情待采集" in v
        for v in strategy_overview.overview(tmp_path)["issues"]
    )


def test_candidate_market_day_uses_preopen_receipt_and_never_publishes(
    tmp_path, monkeypatch
):
    days = ["2026-07-01", "2026-07-02", "2026-07-03"]
    blank_study(tmp_path, days[:1])
    folder = tmp_path / "local-data/july-strategy"
    monkeypatch.setattr(july_settlement, "trading_days", lambda _: days)
    atomic(folder / "decisions-v2/2026-07-01.json", {"day": days[0]})
    monkeypatch.setattr(
        july_settlement,
        "read_receipt",
        lambda path: {
            "day": path.stem,
            "packet": {},
            "cutoff": "2026-06-30T20:00:00+08:00",
            "decisions": {g: {"action": "wait", "thesis": "观望"} for g in "ABC"},
            "decision_sha256": "a" * 64,
            "mode": "historical_replay",
        },
    )
    path = tmp_path / "candidate.json"
    entry = {
        "day": days[1],
        "market_sources": [],
        "decision_status": "missed_preopen",
        "decision_sha256": None,
    }
    market = tmp_path / "local-data/rolling-six/market"
    market.mkdir(parents=True)
    for kind, title, symbol in (
        ("future", "期货", "FG701"),
        ("option", "期权", "FG701P890"),
    ):
        blob = (
            f"{days[1]} {title}交易日报\n"
            "合约代码|今开盘|最高价|最低价|今收盘|成交量(手)|持仓量\n"
            f"{symbol}|20|22|19|21|300|400"
        ).encode()
        archive = market / (kind + ".gz")
        archive.write_bytes(gzip.compress(blob))
        entry["market_sources"].append(
            {
                "exchange": "CZCE",
                "type": kind,
                "day": days[1],
                "sha256": sha256(blob).hexdigest(),
                "raw": archive.name,
                "retrieved_at": days[1] + "T16:00:00+08:00",
            }
        )
    atomic(path, entry | {"sha256": digest(entry)})
    report = july_settlement.build(tmp_path, candidate=(days[1], path))
    assert report["end"] == days[1]
    assert not (folder / "settlement-v2.json").exists()
    entry["decision_status"] = "sealed_preopen"
    atomic(path, entry | {"sha256": digest(entry)})
    with pytest.raises(ValueError, match="缺少对应的策略回执"):
        july_settlement.build(tmp_path, candidate=(days[1], path))
    with pytest.raises(ValueError, match="下一交易日"):
        july_settlement.build(tmp_path, candidate=(days[2], path))


def test_effective_order_is_visible_before_same_day_settlement_and_keeps_original():
    day = "2026-11-24"
    choice = dict(
        action="enter",
        product="FG",
        legs=["FG701C980"],
        evidence_ids=[],
        thesis="趋势确认",
        ownership="自营未知",
        technical="五日上涨",
        fundamentals="库存回落",
        news="调查待发布",
        invalidation="收盘跌破失效位",
        invalidation_price=950,
        take_profit_close=1000,
        max_entry_premium=10,
        max_lots=0,
        holding_days=5,
        scenario_high=0,
        scenario_low=0,
        scenario_target=1000,
    )
    original = {
        **choice,
        "action": "wait",
        "legs": [],
        "holding_days": 0,
        "thesis": "等待",
    }
    packet = {
        "documents": [],
        "market_sources": {},
        "fixed_paper_accounts": {"B": {"positions": []}},
        "products": {
            "FG": {
                "options": [{"id": "FG701C980", "side": "C"}],
                "technical": {"change5_pct": 2, "close_vs_sma20_pct": 1},
            }
        },
    }
    record = {
        "day": day,
        "cutoff": "2026-11-23T20:00:00+08:00",
        "mode": "preopen",
        "packet": packet,
        "decisions": {"A": choice, "B": original},
    }
    view = strategy_overview._today_decision(
        day, record, "B", {}, "2026-11-23", "short_rotation"
    )
    assert view["status"] == "等待" and view["execution"] is None
    assert view["effective_order"]["legs"] == ["FG701C980"]
    assert view["effective_order"]["holding_days"] == 2 and view["order_source"] == "A"

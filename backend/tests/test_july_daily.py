from datetime import datetime

import pytest

from app import july_daily as daily
from app import july_policy
from scripts.agent_replay import atomic


def test_incomplete_contract_can_be_fetched_again_before_advancing(tmp_path, monkeypatch):
    import gzip
    import json
    from hashlib import sha256

    import requests
    from app.agent_replay import digest
    from app.agent_replay_account import FIXED_STRATEGY_VERSION
    from scripts import collect_replay_market, july_settlement

    previous, day = "2026-07-01", "2026-07-02"
    folder = tmp_path / "local-data/july-strategy"
    study = tmp_path / "local-data/agent-study" / july_settlement.BATCH
    underlying, leg = "JD2609", "JD2609C3300"
    bar = [3300, 3400, 3200, 3350, 1000, 500, None]
    opt = [20, 22, 18, 21, 1000, 300, None]
    bars = {previous: {"future": {underlying: bar}, "option": {leg: opt}}}
    blob = gzip.compress(json.dumps(bars).encode())
    (study / "market").mkdir(parents=True)
    (study / "market/market.json.gz").write_bytes(blob)
    baseline_sources = [{"day": previous, "exchange": "DCE", "type": k} for k in ("option", "future")]
    manifest = {"data_sha256": sha256(blob).hexdigest(), "sources": baseline_sources}
    atomic(study / "market/source-manifest.json", manifest)
    atomic(study / "run-manifest.json", {"market_manifest_sha256": digest(manifest), "market_sha256": manifest["data_sha256"]})
    monkeypatch.setattr(july_settlement, "trading_days", lambda root: [previous, day])
    wait = {"action": "wait", "thesis": "等待"}
    enter = {"action": "enter", "legs": [leg], "product": "JD", "holding_days": 5, "max_lots": 0,
             "invalidation_price": 3000, "take_profit_close": 0, "max_entry_premium": 0, "risk_bps": 500, "evidence_ids": []}
    packet = {"strategy_version": FIXED_STRATEGY_VERSION, "documents": [], "products": {
        "JD": {"exchange": "DCE", "multiplier": 10, "technical": {"contract": underlying},
               "options": [{"id": leg, "side": "C", "volume": 10000, "expiry_at": "2026-08-01T15:00:00+08:00", "expiry_sha256": "expiry"}]}}}
    atomic(folder / "decisions-v2" / f"{previous}.json", {})
    monkeypatch.setattr(july_settlement, "read_receipt", lambda path: {
        "day": previous, "decisions": {"A": wait, "B": enter, "C": wait}, "packet": packet,
        "decision_sha256": "test", "mode": "historical_replay", "cutoff": "2026-06-30T20:00:00+08:00",
    })
    ledger = july_settlement.refresh(tmp_path)
    assert ledger["end"] == previous and ledger["groups"]["B"]["opened"] == 1
    last = {"day": previous, "sha256": "previous", "baseline_end": previous}
    archive = tmp_path / "local-data/rolling-six/market"
    corrected = False

    def response(method, url, **kwargs):
        if "dce.com.cn" not in url:
            raise OSError("测试仅提供大商所行情")
        is_future = kwargs["json"]["tradeType"] == "1"
        symbol = underlying if is_future and corrected else "JD2610" if is_future else leg
        row = {"contractId": symbol, "tradeDate": day, "open": 30, "high": 40, "low": 20,
               "close": 35, "volumn": 100, "openInterest": 50}
        reply = requests.Response()
        reply.status_code = 200
        reply.url = url
        reply._content = json.dumps({"data": [row]}).encode()
        return reply

    monkeypatch.setattr(collect_replay_market.requests, "request", response)
    # 既有首个观察版本代表此前已使用的资料，修订版不得覆盖。
    original = collect_replay_market.collect(archive, {"JD": "DCE"}, day, "DCE", "future")
    original_bytes = (archive / original["raw"]).read_bytes()
    with pytest.raises(daily.DataGap, match=underlying):
        daily.capture_day(tmp_path, folder, last, day)
    assert not (folder / "market-days" / f"{day}.json").exists()
    assert json.loads((folder / "settlement-v2.json").read_bytes())["end"] == previous

    corrected = True
    saved = daily.capture_day(tmp_path, folder, last, day)
    assert saved == daily.current_market(tmp_path, folder)
    final_source = next(s for s in saved["market_sources"] if s["type"] == "future")
    assert final_source["sha256"] in final_source["raw"] and final_source["sha256"] != original["sha256"]
    assert (archive / original["raw"]).read_bytes() == original_bytes
    assert sha256(gzip.decompress((archive / final_source["raw"]).read_bytes())).hexdigest() == final_source["sha256"]
    final = july_settlement.refresh(tmp_path)
    assert final["end"] == day and all(g["points"][-1]["day"] == day for g in final["groups"].values())
    # 次日研究按已封存的修订原件读取，不能重新取到旧缓存。
    from app.rolling_update import _market
    prices, _, _ = _market(tmp_path, day, frozen_sources=saved["market_sources"])
    assert underlying in prices["future"]
    def offline(*args, **kwargs):
        raise requests.ConnectionError("交易所暂不可达")

    monkeypatch.setattr(collect_replay_market.requests, "request", offline)
    restored = collect_replay_market.collect(archive, {"JD": "DCE"}, day, "DCE", "future", reobserve=True)
    assert restored == final_source


def test_preopen_uses_frozen_fixed_accounts_and_matching_version(tmp_path, monkeypatch):
    folder = tmp_path / "local-data/july-strategy"
    last = {"day": "2026-11-24", "sha256": "settled"}
    day = "2026-11-25"
    packet = {"trade_date": day, "previous_account_sha256": "settled", "fixed_paper_accounts": {"B": {"cash": 198000}},
              "risk_context": {"max_single_trade_bps": 100}, "paper_accounts": {"B": {"cash": 10}}}
    daily._seal(folder / "staged" / day / "1900.json", {"packet": packet, "recorded_at": "2026-11-24T19:00:00+08:00"})
    now = datetime.fromisoformat("2026-11-24T20:01:00+08:00")
    monkeypatch.setattr(daily, "record", lambda root, packet, *args, mode, deadline: (july_policy.fixed_packet(packet), mode, deadline))
    selected, mode, deadline = daily._prepare(tmp_path, folder, last, day, "model", "cli", now)
    assert mode == "preopen" and deadline.hour == 21
    assert "paper_accounts" not in selected and selected["fixed_paper_accounts"]["B"]["cash"] == 198000
    assert selected["risk_context"]["trade_budget_cny"] == 10000 and "max_single_trade_bps" not in selected["risk_context"]
    with pytest.raises(ValueError, match="账户不匹配"):
        daily._prepare(tmp_path, folder, last | {"sha256": "changed"}, day, "model", "cli", now)
    with pytest.raises(daily.DataGap, match="超出"):
        daily._prepare(tmp_path, folder, last, day, "model", "cli", now.replace(hour=21))


def test_ledger_with_identical_policy_but_old_sizing_needs_recalculation(tmp_path):
    from app.agent_replay_account import FIXED_STRATEGY_VERSION, SETTLEMENT_REVISION
    day = "2026-11-24"
    state = {"version": FIXED_STRATEGY_VERSION, "settlement_revision": SETTLEMENT_REVISION,
             "end": day, "execution_policy": daily.selected_policy(tmp_path)}
    assert daily._current_ledger(tmp_path, state, day)
    assert not daily._current_ledger(tmp_path, state | {"settlement_revision": "previous"}, day)

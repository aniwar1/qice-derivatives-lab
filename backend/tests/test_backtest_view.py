from hashlib import sha256
import gzip
import json

import pytest

from app.backtest_view import BacktestIntegrityError, read_backtest


def test_backtest_reconciles_equity_and_rejects_mismatched_inputs(tmp_path):
    data = tmp_path / "backtest"
    assert read_backtest(data)["status"] == "missing"
    (data / "days").mkdir(parents=True)

    def write(relative, value):
        (data / relative).write_text(json.dumps(value), encoding="utf-8")

    snapshot = gzip.compress(b"{}")
    (data / "market.json.gz").write_bytes(snapshot)
    write("source-manifest.json", {
        "data_sha256": sha256(snapshot).hexdigest(),
        "configured_universe": {"JM": "DCE", "CU": "SHFE"}, "known_complete_exchanges": ["DCE"],
    })
    groups = "ABC"
    day = "2026-10-30"
    stats = {key: {"equity": 200000, "realized": 0, "open_pnl": 0, "drawdown": 0,
                   "opened": 0, "closed": 0, "open_count": 0} for key in groups}
    write("summary.json", {"initial_cash_each": 200000, "daily_new_cost_cap_each": 10000,
                           "through": day, "sessions_processed": 1, "groups": stats})
    day_report = {"official_trading_day": day, "previous_information_date": "2026-10-29", "groups": {
        key: {"new_entry": {"status": "skipped"}, "exits": [], "closing_account": {
            "total_equity": 200000, "cash": 200000, "realized_net_pnl": 0, "open_mark_pnl": 0,
        }} for key in groups
    }}
    write(f"days/{day}.json", day_report)
    csv_file = data / "equity.csv"
    csv = "day,group,equity,cash,new_entry_cost,open_positions\n" + "".join(
        f"{day},{key},200000,200000,0,0\n" for key in groups
    )
    csv_file.write_text(csv, encoding="utf-8")

    view = read_backtest(data)
    assert [plan["id"] for plan in view["plans"]] == list(groups)
    assert all(plan["curve"][0]["return_pct"] == 0 for plan in view["plans"])
    assert view["covered_products"] == ["JM"]

    csv_file.write_text(csv.replace(",200000,200000", ",199000,200000", 1), encoding="utf-8")
    with pytest.raises(BacktestIntegrityError):
        read_backtest(data)

    csv_file.write_text(csv, encoding="utf-8")
    day_report["previous_information_date"] = day
    write(f"days/{day}.json", day_report)
    with pytest.raises(BacktestIntegrityError):
        read_backtest(data)

    day_report["previous_information_date"] = "2026-10-29"
    write(f"days/{day}.json", day_report)
    (data / "market.json.gz").write_bytes(b"changed")
    with pytest.raises(BacktestIntegrityError):
        read_backtest(data)

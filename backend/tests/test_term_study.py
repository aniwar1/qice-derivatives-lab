from datetime import date, datetime

from app.agent_replay import BJT
from app.term_study import execution, select_term_pair, summarize
from scripts.term_study import timings


def bar(price, volume=1000, oi=500):
    return [price, price, price, price, volume, oi, None]


def test_term_pair_matches_each_legs_own_underlying_and_frozen_budget():
    future = {"MA2609": bar(1000, oi=8000), "MA2610": bar(1100, oi=5000), "MA2611": bar(2000)}
    options = {"MA2609C1020": bar(10), "MA2609P980": bar(20),
               "MA2610C1120": bar(10, volume=99), "MA2610P1080": bar(9),
               "MA2611C2040": bar(15), "MA2611P1960": bar(5), "MA2611C2010": bar(2)}
    args = ("MA", future, options, date(2026, 6, 1), date(2026, 6, 4), 10)
    result, reason = select_term_pair(*args)
    assert reason is None
    assert result["main"]["contract"] == "MA2609"
    assert result["far"]["contract"] == "MA2611"
    assert result["earlier_far_rejections"] == [{"contract": "MA2610", "reason": "illiquid_or_missing_otm_leg"}]
    assert [leg["otm_pct"] for leg in result["far"]["legs"]] == [2, 2]
    assert result["main"]["planned_quantity"] == 6
    assert result["far"]["planned_quantity"] == 9
    assert result["main"]["expiry_buffer_boundary"] == "2026-08-01"
    assert select_term_pair(*args[:4], date(2026, 8, 1), 10)[1] == "main_expiry_buffer"
    assert select_term_pair(*args, risk_budget=100)[1] == "main_insufficient_prior_budget"


def test_structure_debits_fees_all_four_leg_trades_and_rejects_excess_or_unpaired_fills():
    structure = {"planned_quantity": 2, "legs": [{"id": "c"}, {"id": "p"}]}
    entry = {"c": bar(15), "p": bar(10)}
    exit_ = {"c": bar(30), "p": bar(5)}
    result = execution(structure, entry, exit_, 10)
    assert result["entry_cash"] == 517
    assert result["exit_cash"] == 681
    assert result["pnl_yuan"] == 164
    assert result["budget_return_pct"] == 8.2
    assert execution(structure, entry, exit_, 10, budget=525)["status"] == "entry_debit_exceeds_budget"
    assert execution(structure, entry, {"c": bar(3), "p": bar(5, volume=0)}, 10)["status"] == "exit_quote_or_volume_gap"


def test_weekend_event_uses_strictly_complete_sessions_and_clusters_share_one_vote():
    days = ["2026-08-06", "2026-08-07", "2026-08-10", "2026-08-11"]
    assert timings(datetime(2026, 8, 9, 9, tzinfo=BJT), days) == (days[0], days[1], days[3])
    def row(event, product, value):
        return {"event_at": event, "product": product, "main": {"execution": {"budget_return_pct": 0}},
                "far": {"execution": {"budget_return_pct": value}}}
    result = summarize([row("e1", "A", 4), row("e1", "B", 8), row("e2", "B", -10)])
    assert result["far_minus_main_pp"] == -2
    assert result["event_clusters"] == 2
    assert result["leave_one_event_out_range_pp"] == [-10, 6]

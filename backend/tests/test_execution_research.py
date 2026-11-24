import pytest

from app.execution_research import ExecutionAccount, select_order
from app.agent_replay import build_packet
from test_agent_replay import DAYS, PRODUCTS, SPECS, decision, examples


@pytest.mark.parametrize(("low", "future_open", "status"), [(8.8, 100, "simulated_open"), (9.0, 100, "blocked"), (8.8, 92, "blocked")])
def test_day_limit_requires_a_real_cross_and_valid_underlying_at_open(low, future_open, status):
    prices, sources = examples()
    day = DAYS[1]
    packet = build_packet(day, DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    quote = prices[day]["option"]["JD2701C105"]
    quote[2] = low
    prices[day]["future"]["JD2701"][0] = future_open
    order = decision() | {"max_entry_premium": 9}
    sim = ExecutionAccount("B", "day_limit")
    trade, _ = sim.process(day, order, packet, prices[day])
    assert trade["status"] == status
    assert quote[0] == 10
    if status == "simulated_open":
        assert trade["market_open_prices"] == [10]
        assert trade["filled_premium_including_slippage"] == 9
        assert trade["fill_method"] == "daily_limit_proxy"
        assert trade["premium_and_fees"] == pytest.approx((9 * 10 + 3) * trade["quantity"])


def test_backup_is_frozen_without_execution_prices():
    packet = {"products": {"JD": {"technical": {"change5_pct": 2, "close_vs_sma20_pct": 1}, "options": [{"id": "JD2701C105", "side": "C"}]}}}
    proposals = {"B": decision("wait"), "A": decision() | {"holding_days": 5}}
    order, source = select_order("B", proposals, packet, [], "short_rotation")
    assert order["action"] == "enter" and source == "A" and order["holding_days"] == 2
    assert proposals["B"]["action"] == "wait" and proposals["A"]["holding_days"] == 5
    held = [{"product": "JD", "exit_reason": ""}]
    assert select_order("B", proposals, packet, held, "short_rotation") == (proposals["B"], "B")


def test_event_exit_covers_last_relevant_event():
    packet = {"next_sessions": ["2026-09-07", "2026-09-08", "2026-09-09"], "documents": [
        {"id": "S1", "products": ["JD"], "scheduled_at": "2026-09-07T19:00:00+08:00"},
        {"id": "S2", "products": ["AP"], "scheduled_at": "2026-09-09T10:00:00+08:00"},
    ]}
    proposal = decision() | {"evidence_ids": ["S1", "S2"], "holding_days": 3}
    selected, source = select_order("C", {"C": proposal}, packet, [], "day_limit")
    assert source == "C" and selected["holding_days"] == 2


def test_second_entry_requires_a_new_signal_date_and_respects_total_exposure():
    from app.agent_replay_account import FIXED_STRATEGY_VERSION
    from app.second_entry import SecondEntryAccount

    prices, sources = examples()
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    packet["strategy_version"] = FIXED_STRATEGY_VERSION
    packet["products"]["JD"]["options"][0]["volume"] = 100000
    order = decision() | {"holding_days": 5, "risk_bps": 500}
    sim = SecondEntryAccount("B", "second_signal")
    opened, _ = sim.process(DAYS[1], order, packet, prices[DAYS[1]])
    assert opened["status"] == "simulated_open"
    assert not sim.product_entry_allowed(DAYS[1], order, packet)
    before = sim.previous_equity
    again, _ = sim.process(DAYS[2], order, packet, prices[DAYS[2]])
    assert again["status"] == "simulated_open"
    assert sum(p["cost"] for p in sim.positions) <= max(20000, before * 0.08)
    assert opened["quantity"] == again["quantity"]
    assert not sim.product_entry_allowed("2026-05-25", order, packet)
    for p in sim.positions:
        p["exit_reason"] = "到期退出"
    prices[DAYS[2]]["option"]["JD2701C105"][4] = max(p["qty"] for p in sim.positions)
    close, _ = sim.process("2026-05-25", decision("wait"), packet, prices[DAYS[2]])
    assert len(close["exits"]) == 1 and len(sim.positions) == 1

from copy import deepcopy

import pytest
from app.agent_replay import build_packet, validate_decision
from app.agent_replay_account import Account

DAYS = ["2026-05-20", "2026-05-21", "2026-05-22"]
PRODUCTS = {"JD": "DCE"}
SPECS = {"JD": {"multiplier": 10}}


def examples():
    prices = {
        d: {
            "future": {"JD2701": [100, 105, 95, 100, 2000, 1000, None]},
            "option": {"JD2701C105": [10, 12, 9, 11, 2000, 900, 27]},
        }
        for d in DAYS
    }
    sources = {
        (d, "DCE", kind): {
            "day": d,
            "exchange": "DCE",
            "type": kind,
            "https": "https://www.dce.com.cn",
            "sha256": "ab" * 32,
            "configured_product_rows": {"JD": 1},
        }
        for d in DAYS
        for kind in ("future", "option")
    }
    return prices, sources


def test_future_market_changes_and_late_sources_do_not_change_packet():
    prices, sources = examples()
    docs = [{"id": "later", "available_at": "2026-05-20T20:01:00+08:00"}]
    monthly = [
        {
            "product": "JD",
            "kind": "future",
            "month": "2026-04",
            "available_at": "2026-05-15T15:00:00+08:00",
            "volume_lots": 10000,
            "month_end_open_interest": 200,
            "source_url": "https://example.com",
            "sha256": "ab" * 32,
        }
    ]
    args = (DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, docs)
    expected = build_packet(*args, monthly=monthly)
    assert not expected["documents"]
    assert (
        expected["products"]["JD"]["monthly_aggregate"]["future"]["ownership"]
        == "市场汇总"
    )
    prices[DAYS[1]]["future"]["JD2701"][3] = 700
    assert expected == build_packet(*args, monthly=monthly)
    assert expected["products"]["JD"]["technical"]["close"] == 100


def decision(action="enter"):
    return {
        "product": "JD",
        "news": "缺少消息",
        "technical": "本月合约上行",
        "fundamentals": "供需未核验",
        "ownership": "自营方向未知",
        "thesis": "等待确认或限仓",
        "invalidation": "低于 95 失效",
        "action": action,
        "evidence_ids": ["DCE-future", "DCE-option"],
        "legs": ["JD2701C105"] if action == "enter" else [],
        "risk_bps": 50 if action == "enter" else 0,
        "holding_days": int(action == "enter"),
        "invalidation_price": 95 if action == "enter" else 0,
        "scenario_target": 0,
        **order_fields(),
    }


def test_replay_checks_evidence_and_event_timing():
    prices, sources = examples()
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    d = {"A": decision("wait"), "B": decision(), "C": decision("wait")}
    validate_decision(d, packet)
    d["B"]["evidence_ids"] += ["unknown"]
    with pytest.raises(ValueError, match="证据引用"):
        validate_decision(d, packet)
    d["B"] = decision("wait")
    packet["products"]["JD"]["options"].append({"id": "JD2701P95", "side": "P"})
    d["C"] = decision() | order_fields(scenario_low=80, scenario_high=125)
    packet["products"]["JD"]["technical"].update(low20=80, high20=125)
    d["C"]["legs"].append("JD2701P95")
    packet["documents"] = [
        {
            "id": "calendar",
            "products": ["JD"],
            "scheduled_at": "2026-05-21T08:00:00+08:00",
        }
    ]
    d["C"]["evidence_ids"].append("calendar")
    with pytest.raises(ValueError, match="冻结事件"):
        validate_decision(d, packet)


def test_next_open_and_pending_exit_do_not_use_close_as_fill():
    prices, sources = examples()
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    ledger = Account()
    fill, first = ledger.process(DAYS[1], decision(), packet, prices[DAYS[1]])
    assert fill["status"] == "simulated_open" and first["pending_exits"] == 1
    missing = deepcopy(prices[DAYS[2]])
    missing["option"]["JD2701C105"][0] = None
    blocked, later = ledger.process(DAYS[2], decision(), packet, missing)
    assert blocked["status"] == "blocked" and later["pending_exits"] == 1
    exit, end = ledger.process("2026-05-25", decision("wait"), packet, prices[DAYS[2]])
    assert end["open_positions"] == 0 and end["realized"] < 0
    assert end["equity"] == 200000 + end["realized"] and len(exit["exits"]) == 1


def test_a_can_enter_with_evidence_and_positive_stressed_intrinsic_value():
    prices, sources = examples()
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    packet["products"]["JD"]["technical"].update(high20=125.0, low20=90.0)
    packet["documents"] = [
        {
            "id": "S1",
            "dimension": "fundamentals",
            "products": ["JD"],
            "scope": "direct",
        },
        {"id": "S2", "dimension": "news", "products": ["JD"], "scope": "upstream"},
    ]
    proposal = decision() | {
        "scenario_target": 125.0,
        "evidence_ids": ["DCE-future", "DCE-option", "S1", "S2"],
    }
    decisions = {"A": proposal, "B": decision("wait"), "C": decision("wait")}
    validate_decision(decisions, packet)
    execution, _ = Account("A").process(DAYS[1], proposal, packet, prices[DAYS[1]])
    assert execution["stressed_target_pnl_per_lot"] > 0
    assert (
        0
        < execution["prior_stressed_target_pnl_per_lot"]
        < execution["stressed_target_pnl_per_lot"]
    )
    packet["documents"][0]["freshness_role"] = "low_frequency_background_only"
    with pytest.raises(ValueError, match="直接基本面"):
        validate_decision(decisions, packet)
    del packet["documents"][0]["freshness_role"]
    proposal["scenario_target"] = 107.0
    with pytest.raises(ValueError, match="压力成本"):
        validate_decision(decisions, packet)


def test_c_preannounced_matched_event_allows_a_budgeted_two_leg_simulation():
    prices, sources = examples()
    for bars in prices.values():
        bars["option"]["JD2701P95"] = [8, 10, 7, 9, 1500, 400, 25]
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    packet["products"]["JD"]["technical"].update(high20=140.0, low20=75.0)
    packet["documents"] = [
        {
            "id": "S1",
            "products": ["JD"],
            "dimension": "news",
            "scheduled_at": "2026-05-21T10:00:00+08:00",
        }
    ]
    proposal = decision() | {
        "legs": ["JD2701C105", "JD2701P95"],
        "invalidation_price": 0,
        "scenario_low": 75,
        "scenario_high": 140,
        "evidence_ids": ["DCE-future", "DCE-option", "S1"],
        "risk_bps": 30,
        "holding_days": 1,
    }
    validate_decision(
        {"A": decision("wait"), "B": decision("wait"), "C": proposal}, packet
    )
    sim = Account("C")
    execution, point = sim.process(DAYS[1], proposal, packet, prices[DAYS[1]])
    assert execution["status"] == "simulated_open" and execution["quantity"] == 3
    assert round(execution["premium_and_fees"], 2) == round((18 * 1.01 * 10 + 6) * 3, 2)
    assert point["pending_exits"] == 1
    assert not sim.positions[0]["exit_reason"].startswith("标的收盘")
    assert execution["opening_stress"]["upside_net_cny_per_pair"] > 0
    expensive = deepcopy(prices[DAYS[1]])
    for leg in proposal["legs"]:
        expensive["option"][leg][0] = 45.0
    rejected, _ = Account("C").process(DAYS[1], proposal, packet, expensive)
    assert rejected["status"] == "blocked"
    assert (
        rejected["prior_close_stress"]["eligible"]
        and not rejected["opening_stress"]["eligible"]
    )
    proposal.update(scenario_high=107, scenario_low=92)
    rejected, _ = Account("C").process(DAYS[1], proposal, packet, prices[DAYS[1]])
    assert (
        not rejected["prior_close_stress"]["eligible"]
        and "opening_stress" not in rejected
    )


def test_only_an_explicit_agent_profit_exit_is_enforced_and_at_close():
    prices, sources = examples()
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    d = decision() | {
        "scenario_target": 120.0,
        "take_profit_close": 105.0,
        "risk_bps": 91,
        "holding_days": 5,
        "invalidation": "收盘低于95失效。触及105或满4个交易日也退出。",
    }
    quote = deepcopy(prices[DAYS[1]])
    quote["future"]["JD2701"][1] = 106.0
    ledger = Account("B")
    executed, row = ledger.process(DAYS[1], d, packet, quote)
    assert executed["target_exit_close"] == 105 and row["pending_exits"] == 0
    assert executed["effective_risk_bps"] == 50 and executed["agent_risk_bps"] == 91
    quote["future"]["JD2701"][3] = 106
    _, row = ledger.process(DAYS[2], decision("wait"), packet, quote)
    assert row["pending_exits"] == 1 and ledger.closed == 0
    exit, row = ledger.process("2026-05-25", decision("wait"), packet, quote)
    assert (
        exit["exits"][0]["reason"] == "标的收盘达到 Agent 明示止盈价"
        and ledger.closed == 1
    )
    quote["future"]["JD2701"][0] = 106
    already_met, _ = Account("B").process(DAYS[1], d, packet, quote)
    assert already_met["status"] == "blocked" and "止盈价" in already_met["reason"]
    quote["future"]["JD2701"][0] = 100


def test_exchange_update_after_cutoff_excludes_revision_until_eligible():
    prices, sources = examples()
    for kind in ("future", "option"):
        sources[DAYS[0], "DCE", kind]["disclosed_available_at"] = (
            "2026-05-21T09:00:00+08:00"
        )
    early = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    assert early["products"]["JD"]["technical"] is None
    assert early["products"]["JD"]["options"] == [] and not early["market_sources"]
    later = build_packet(DAYS[2], DAYS[:2], prices, sources, PRODUCTS, SPECS, [])
    assert (
        later["products"]["JD"]["technical"]["close"] == 100
        and later["products"]["JD"]["options"]
    )


def test_delayed_exchange_close_cannot_trigger_next_open_exit():
    prices, sources = examples()
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    d = decision() | {"holding_days": 5}
    prices[DAYS[1]]["future"]["JD2701"][3] = 90
    account = Account()
    _, day = account.process(DAYS[1], d, packet, prices[DAYS[1]], {"DCE"})
    assert day["pending_exits"] == 0 and day["delayed_close_signal_exchanges"] == [
        "DCE"
    ]
    _, later = account.process(DAYS[2], decision("wait"), packet, prices[DAYS[1]])
    assert later["pending_exits"] == 1 and not later["delayed_close_signal_exchanges"]


def order_fields(**kwargs):
    return (
        dict.fromkeys(
            (
                "take_profit_close",
                "max_lots",
                "max_entry_premium",
                "scenario_low",
                "scenario_high",
            ),
            0,
        )
        | kwargs
    )


def test_structured_trade_terms_take_precedence_over_scenario_and_unstructured_text():
    prices, sources = examples()
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    wait = decision("wait") | order_fields()
    proposal = decision() | order_fields(
        take_profit_close=109.0, max_lots=1, max_entry_premium=12.0
    )
    legacy = {
        key: value for key, value in proposal.items() if key not in order_fields()
    }
    with pytest.raises(ValueError, match="结构化交易条件缺失"):
        validate_decision({"A": wait, "B": legacy, "C": wait}, packet)
    proposal.update(scenario_target=125, invalidation="触及125则退出，模拟买入一手。")
    validate_decision({"A": wait, "B": proposal, "C": wait}, packet)
    executed, _ = Account("B").process(DAYS[1], proposal, packet, prices[DAYS[1]])
    assert executed["target_exit_close"] == 109 and executed["quantity"] == 1
    assert executed["order_terms_source"] == "structured_agent_fields"
    proposal.update(take_profit_close=0, max_lots=0, max_entry_premium=9)
    rejected, _ = Account("B").process(DAYS[1], proposal, packet, prices[DAYS[1]])
    assert rejected["target_exit_close"] is None and rejected["size_limit_lots"] is None
    assert rejected["status"] == "blocked"
    proposal["take_profit_close"] = 98
    with pytest.raises(ValueError, match="止盈触发价"):
        validate_decision({"A": wait, "B": proposal, "C": wait}, packet)


def test_structured_strangle_scenarios_use_agent_targets_and_summed_premium():
    prices, sources = examples()
    for quotes in prices.values():
        quotes["option"]["JD2701P95"] = [8, 9, 8, 9, 1500, 900, 20]
    packet = build_packet(DAYS[1], DAYS[:1], prices, sources, PRODUCTS, SPECS, [])
    packet["products"]["JD"]["technical"].update(low20=70, high20=140)
    packet["documents"] = [
        {
            "id": "S1",
            "products": ["JD"],
            "dimension": "news",
            "scheduled_at": "2026-05-21T10:00:00+08:00",
        }
    ]
    wait = decision("wait") | order_fields()
    prop = decision() | order_fields(
        scenario_low=79, scenario_high=132, max_entry_premium=17
    )
    prop.update(
        legs=["JD2701C105", "JD2701P95"],
        invalidation_price=0,
        evidence_ids=["S1", "DCE-future", "DCE-option"],
    )
    validate_decision({"A": wait, "B": wait, "C": prop}, packet)
    clipped, _ = Account("C").process(DAYS[1], prop, packet, prices[DAYS[1]])
    assert clipped["status"] == "blocked" and clipped["opening_premium_limit"] == 17
    prop["max_entry_premium"] = 19
    opened, _ = Account("C").process(DAYS[1], prop, packet, prices[DAYS[1]])
    assert opened["status"] == "simulated_open"
    assert (
        opened["opening_stress"]["scenario_high"] == 132
        and opened["opening_stress"]["scenario_low"] == 79
    )
    prop["scenario_high"] = 111
    insufficient, _ = Account("C").process(DAYS[1], prop, packet, prices[DAYS[1]])
    assert (
        insufficient["status"] == "blocked"
        and not insufficient["prior_close_stress"]["eligible"]
    )
    prop["scenario_high"] = 141
    with pytest.raises(ValueError, match="20日范围"):
        validate_decision({"A": wait, "B": wait, "C": prop}, packet)

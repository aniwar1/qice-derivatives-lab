from datetime import datetime

from app.agent_replay_account import Account
from app.frequency_account import WAIT, prefer, segment, select_order
from app.frequency_policy import signal_input

from scripts.frequency_replay import asof_nav


def setup_market():
    products, orders = {}, {}
    for product in ("FG", "SA"):
        leg = product + "2701C1200"
        products[product] = {
            "exchange": "CZCE",
            "multiplier": 20,
            "source_ids": ["CZCE-option", "CZCE-future"],
            "technical": {
                "contract": product + "2701",
                "close": 1198,
                "high20": 1320,
                "low20": 1030,
            },
            "options": [
                {"id": leg, "side": "C", "strike": 1200, "volume": 2000, "premium": 30}
            ],
        }
        orders[product] = WAIT | {
            "action": "enter",
            "product": product,
            "legs": [leg],
            "risk_bps": 100,
            "holding_days": 3,
            "invalidation_price": 1150,
            "take_profit_close": 1280,
            "max_entry_premium": 35,
            "evidence_ids": ["CZCE-option", "CZCE-future"],
        }
    packet = {
        "products": products,
        "market_sources": {"CZCE-option": {}, "CZCE-future": {}},
        "documents": [],
        "cutoff": "2026-07-06T20:00:00+08:00",
    }
    return packet, {"primary": orders["FG"], "reserve": orders["SA"]}


def test_preselection_checks_same_sleeve_holdings_before_any_open_information():
    packet, pair = setup_market()
    account = Account("B")
    assert select_order("B", pair, packet, account, 2)[1] == 1
    account.positions = [
        {"product": "FG", "qty": 1, "cost": 609, "exit_reason": "待平仓"}
    ]
    chosen, rank, trace = select_order("B", pair, packet, account, 2)
    assert rank == 2 and chosen["product"] == "SA"
    assert trace[1]["effective_risk_bps"] == 50 and trace[1]["estimated_lots"] == 1
    assert select_order("B", pair, packet, account, 1)[1] is None
    account.cash = 100
    _, rank, checks = select_order("B", pair, packet, account, 2)
    assert rank is None and checks[-1]["estimated_lots"] == 0
    assert select_order("B", pair | {"primary": WAIT}, packet, account, 2)[2] == [
        {"rank": 1, "product": "", "eligible": False, "reason": "Agent 明确等待"}
    ]


def test_future_open_exceeding_limit_does_not_activate_reserve():
    packet, pair = setup_market()
    account = Account("B")
    chosen, rank, _ = select_order("B", pair, packet, account, 2)
    ledger = {
        "future": {"FG2701": [1201] * 7, "SA2701": [1201] * 7},
        "option": {"FG2701C1200": [47] * 7, "SA2701C1200": [20] * 7},
    }
    result, _ = account.process("2026-07-07", chosen, packet, ledger)
    assert rank == 1 and result["status"] == "blocked" and account.opened == 0


def test_valuation_excludes_late_close_without_using_future_marks():
    account = Account("B")
    account.cash = 199000
    account.positions = [
        {
            "product": "FG",
            "day": "2026-07-06",
            "legs": ["FG2701C1200"],
            "cost": 610,
            "qty": 1,
            "mult": 20,
        }
    ]
    history = ["2026-07-06", "2026-07-07"]
    market = {
        d: {"option": {"FG2701C1200": [30, 35, 25, 30 + i * 25, 200, 100, 12]}}
        for i, d in enumerate(history)
    }
    meta = {
        (d, "CZCE", "option"): {"disclosed_available_at": d + "T16:00:00+08:00"}
        for d in history
    }
    meta[history[-1], "CZCE", "option"]["disclosed_available_at"] = (
        "2026-07-08T12:00:00+08:00"
    )
    cutoff = datetime.fromisoformat("2026-07-07T20:00:00+08:00")
    assert asof_nav(account, market, history, meta, cutoff, {"FG": "CZCE"}) == 199600
    del market["2026-07-06"]["option"]["FG2701C1200"]
    assert asof_nav(account, market, history, meta, cutoff, {"FG": "CZCE"}) == 199610


def test_inventory_changes_only_use_distinct_dates_of_same_warehouse_series():
    packet, _ = setup_market()
    base = {
        "products": ["FG"],
        "dimension": "fundamentals",
        "available_at": "2026-07-04T16:00:00+00:00",
        "title": "FG 交易所仓单数量",
        "channel": "czce",
    }
    packet["documents"] = [
        base | {"id": "S1", "excerpt": "2026-07-04 郑商所公布FG仓单数量总计 150 张。"},
        base | {"id": "S2", "excerpt": "2026-07-03 郑商所公布FG仓单数量总计 125 张。"},
        base | {"id": "S3", "excerpt": "2026-07-04 郑商所公布FG预报数量总计 200 张。"},
    ]
    payload = signal_input(packet)
    assert len(payload["warehouse_changes"]) == 1
    assert payload["warehouse_changes"][0]["change"] == 25 and payload[
        "warehouse_changes"
    ][0]["sources"] == ["S1", "S2"]
    assert payload["products"]["FG"]["options"][0]["prior_cost_cny_per_lot"] == 609


def test_holdout_uses_starting_nav_and_preregistered_comparison():
    curve = [{"day": "first", "equity": 201000}, {"day": "next", "equity": 200000}]
    sessions = [{"execution": {"status": "wait", "exits": []}}] * 2
    hold = segment(curve, sessions, 1, 2)
    assert hold["net_pnl"] == -1000 and hold["drawdown_pct"] > 0
    old = {
        "stats": {"net_pnl": 100, "opened": 4, "drawdown_pct": 1},
        "segments": {"last42": {"net_pnl": 50}},
    }
    more = {
        "stats": {"net_pnl": 105, "opened": 5, "drawdown_pct": 1.5},
        "segments": {"last42": {"net_pnl": 51}},
    }
    assert prefer(more, old)
    more["segments"]["last42"]["net_pnl"] = 49
    assert not prefer(more, old)


def test_invalid_evidence_liquidity_and_maturity_cannot_promote_an_alternate():
    packet, pair = setup_market()
    account = Account("B")
    account.positions = [{"product": "FG", "qty": 1, "cost": 609, "exit_reason": ""}]
    for field, value in (("evidence_ids", ["invented"]), ("invalidation_price", 1500)):
        first = pair["primary"] | {field: value}
        _, rank, checked = select_order(
            "B", pair | {"primary": first}, packet, account, 2
        )
        assert rank is None and len(checked) == 1
    packet["products"]["FG"]["options"][0]["volume"] = 99
    _, rank, checked = select_order("B", pair, packet, account, 2)
    assert rank is None and len(checked) == 1 and "成交量" in checked[0]["reason"]
    packet["products"]["FG"]["options"][0]["volume"] = 2000
    packet.update(
        execution_sessions=["2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"],
        execution_expiries={"FG2701": "2026-07-09"},
    )
    _, rank, checked = select_order("B", pair, packet, account, 2)
    assert (
        rank is None
        and len(checked) == 1
        and checked[0]["verified_last_trade"] == "2026-07-09"
    )
    packet["execution_expiries"]["FG2701"] = "2026-07-10"
    _, rank, unknown = select_order("B", pair, packet, account, 2)
    assert rank is None and "无法核实" in unknown[-1]["reason"]
    packet["execution_expiries"]["SA2701"] = "2026-07-14"
    assert select_order("B", pair, packet, account, 2)[1] == 2
    packet["execution_leg_expiries"] = {
        "FG2701C1200": {"expiry": "2026-07-09", "sha256": "official"}
    }
    _, rank, checked = select_order("B", pair, packet, account, 2)
    assert rank is None and checked[0]["verified_last_trade"] == "2026-07-09"
    assert checked[0]["expiry_sources"]["FG2701C1200"]["sha256"] == "official"


def test_two_leg_cost_failure_blocks_backup_even_if_portfolio_holds_primary():
    packet, pair = setup_market()
    packet.update(
        trade_date="2026-07-07",
        next_sessions=["2026-07-07", "2026-07-08", "2026-07-09"],
    )
    for rank in ("primary", "reserve"):
        candidate = pair[rank]
        p = candidate["product"]
        leg = p + "2701P1150"
        packet["products"][p]["options"].append(
            {"id": leg, "side": "P", "strike": 1150, "volume": 2000, "premium": 25}
        )
        packet["documents"].append(
            {
                "id": p + "event",
                "dimension": "news",
                "products": [p],
                "scheduled_at": "2026-07-08T09:30:00+08:00",
            }
        )
        candidate["evidence_ids"].append(p + "event")
        candidate.update(
            invalidation_price=0, take_profit_close=0, max_entry_premium=57
        )
        candidate["legs"].append(leg)
    pair["primary"].update(scenario_low=1180, scenario_high=1210)
    pair["reserve"].update(scenario_low=1050, scenario_high=1300)
    acct = Account("C")
    acct.positions = [{"product": "FG", "qty": 1, "cost": 609, "exit_reason": ""}]
    _, rank, trace = select_order("C", pair, packet, acct, 2)
    assert rank is None and len(trace) == 1 and trace[0]["stress"]["eligible"] is False

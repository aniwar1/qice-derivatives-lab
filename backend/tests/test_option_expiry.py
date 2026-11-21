from datetime import datetime, date, timedelta, timezone
import json

from app.option_expiry import ExpiryIndex, historical_expiry
from scripts.expiry_settlement import ExpiryLedger


def test_known_product_schedules_and_calendar_truncation():
    start = date(2026, 8, 1)
    calendar = [(start + timedelta(days=i)).isoformat() for i in range(62) if (start + timedelta(days=i)).weekday() < 5]
    calendar.insert(0, "2026-07-31")
    assert historical_expiry("AP2610", calendar) == date(2026, 8, 27)
    assert historical_expiry("PK2610", calendar) == date(2026, 9, 11)
    assert historical_expiry("AP2610", calendar[:17]) is None
    assert historical_expiry("AP2711", calendar) is None


def test_changed_expiry_excludes_later_disclosure(tmp_path):
    def source(stamp, expiry, digest):
        return {"available_at": stamp, "sha256": digest, "series": [{"legs": ["AP2610P7400"], "expiry": expiry}]}
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"sources":[source("2026-04-01T12:00:00+08:00","2026-08-27","a"),
                                          source("2026-08-25T12:00:00+08:00","2026-08-28","b")]}))
    index = ExpiryIndex(path)
    at = lambda day: datetime.fromisoformat(day+"T20:00:00+08:00")
    assert index.lookup("AP2610P7400",at("2026-03-31")) is None
    assert index.lookup("AP2610P7400",at("2026-08-24"))["sha256"] == "a"
    assert index.lookup("AP2610P7400",at("2026-08-25"))["expiry"] == "2026-08-28"


def test_expired_unexercised_position_releases_capital_and_no_double_realization():
    from app.agent_replay_account import Account
    account = Account()
    account.positions = [{"day":"2026-08-25","product":"AP","underlying":"AP2610","legs":["AP2610P7400"],
                          "qty":2,"mult":10,"age":2,"side":"P","invalid":8000,"hold":3,"cost":915.0,
                          "exit_reason":"","target_exit":None,"value":800.0}]
    account.cash -= 915
    class VerifiedLedger:
        def outcome(self, pos, day):
            return {"expiry":"2026-08-27","future_settlement":7438}
    prices = {"future":{"AP2610":[7438]*6}, "option":{"AP2610P7400":[50,59.5,0.5,0.5,87649,0]}}
    packet = {"products":{"AP":{"exchange":"CZCE"}}}
    order, point = ExpiryLedger.process(VerifiedLedger(),account,"2026-08-27",{"action":"wait","thesis":""},packet,prices)
    assert order["exits"][0]["pnl"] == -915
    assert point["equity"] == 199085
    assert account.closed == 1 and account.positions == [] and account.realized == -915
    next_order, _ = ExpiryLedger.process(VerifiedLedger(),account,"2026-08-28",{"action":"wait","thesis":""},packet,prices)
    assert next_order["exits"] == [] and account.previous_equity==199085


def test_czce_needs_visible_zero_settlement_and_no_automatic_exercise():
    import pytest

    day = "2026-08-27"
    pos = {"legs":["AP2610P7400"], "underlying":"AP2610","product":"AP"}
    class Reference:
        def lookup(self, leg, cutoff):
            return {"expiry":day, "sha256":"reference"}
    class Ledger:
        index = Reference()
        universe = {"AP":"CZCE"}
        strike_settlement = 7438
        released = datetime.fromisoformat("2026-08-27T16:00:00+08:00")
        def _czce_table(self, _, kind):
            rows = ({"AP610":{"今结算":str(self.strike_settlement)}} if kind == "future"
                    else {"AP610P7400":{"今结算":"0","持仓量":"0","行权量":"0"}})
            return rows, {"sha256":kind}, self.released
    ledger = Ledger()
    assert ExpiryLedger.outcome(ledger,pos,"2026-08-26") is None
    proof = ExpiryLedger.outcome(ledger,pos,day)
    assert proof["future_settlement"] == 7438 and proof["options"][0]["option_settlement"] == 0
    ledger.strike_settlement = 7350
    with pytest.raises(ValueError,match="行权"):
        ExpiryLedger.outcome(ledger,pos,day)
    ledger.released = datetime.fromisoformat("2026-08-28T11:00:00+08:00")
    assert ExpiryLedger.outcome(ledger,pos,day) is None

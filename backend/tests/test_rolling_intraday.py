from datetime import datetime, timedelta

from app import rolling_intraday as live

DAYS = ("2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24")


def account(_):
    def holding(p, strike):
        return {
            "product": p,
            "legs": [f"{p}2701P{strike}"],
            "value": 40.0,
            "qty": 2,
            "mult": 10,
        }

    return {
        "day": DAYS[1],
        "groups": {
            g: {"account": {"positions": pos}}
            for g, pos in [
                ("A", [holding("FG", 890), holding("SA", 950)]),
                ("B", []),
                ("C", []),
            ]
        },
    }


def test_weekend_and_night_rollover_do_not_relabel_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "trading_days", lambda _: DAYS)
    sunday = datetime(2026, 11, 22, 11, tzinfo=live.BJT)

    def must_not_fetch(_):
        raise AssertionError("休市不能请求盘口")

    assert (
        live.read_intraday(
            tmp_path, now=sunday, snapshot_loader=account, quote_loader=must_not_fetch
        )["status"]
        == "closed"
    )
    friday = datetime(2026, 11, 20, 22, 59, tzinfo=live.BJT)
    got = live.read_intraday(
        tmp_path,
        now=friday,
        snapshot_loader=account,
        quote_loader=lambda _: {
            "FG2701P890": (friday, 3.0),
            "SA2701P950": (friday, 4.0),
        },
    )
    assert got["status"] == "live" and got["groups"][0]["pnl_change"] == 60
    assert got["groups"][1]["pnl_change"] == 0
    assert (
        live.active_date("CU", friday + timedelta(hours=1, minutes=10), DAYS) == DAYS[2]
    )
    assert live.active_date("FG", friday + timedelta(hours=1, minutes=10), DAYS) is None


def test_missing_leg_keeps_group_gain_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "trading_days", lambda _: DAYS)
    now = datetime(2026, 11, 23, 10, tzinfo=live.BJT)
    res = live.read_intraday(
        tmp_path,
        now=now,
        snapshot_loader=account,
        quote_loader=lambda _: {
            "FG2701P890": (now - timedelta(minutes=3), 3.0),
            "SA2701P950": (now, 4.0),
        },
    )
    assert res["status"] == "partial" and res["groups"][0] == {
        "id": "A",
        "marked_legs": 1,
        "total_legs": 2,
        "pnl_change": None,
    }
    tuesday = now + timedelta(days=1)
    assert (
        live.read_intraday(tmp_path, now=tuesday, snapshot_loader=account)["status"]
        == "unavailable"
    )


def test_all_flat_groups_do_not_report_a_live_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "trading_days", lambda _: DAYS)
    now = datetime(2026, 11, 23, 10, tzinfo=live.BJT)
    snapshot = account(None)
    snapshot["groups"]["A"]["account"]["positions"] = []

    def no_quotes(_):
        raise AssertionError("空仓时无需请求期权盘口")

    result = live.read_intraday(
        tmp_path, now=now, snapshot_loader=lambda _: snapshot, quote_loader=no_quotes
    )
    assert result["status"] == "flat" and result["observed_at"] is None
    assert all(group["pnl_change"] == 0 for group in result["groups"])

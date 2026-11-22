import json
from datetime import date, datetime
from hashlib import sha256

import pytest
from app.agent_replay import digest
from app.rolling_view import read_rolling_baseline, window_anchor
from app.study_view import BATCH

CALENDAR = tuple(
    date.fromisoformat(d)
    for d in (
        "2026-05-21",
        "2026-05-22",
        "2026-05-25",
        "2026-11-20",
        "2026-11-23",
        "2026-11-24",
        "2026-11-25",
        "2026-11-26",
    )
)


def write(path, value, signed=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if signed:
        value = value | {"sha256": digest(value)}
    path.write_text(json.dumps(value, ensure_ascii=False))
    return value


def history(root):
    plans = []
    for g in "ABC":
        curve, sessions = [], []
        for i, d in enumerate(CALENDAR[:4]):
            day = str(d)
            delta, realized, open_count = (
                ((-10, 0, 1), (100, 0, 1), (200, 200, 0), (200, 200, 0))[i]
                if g == "A"
                else (0, 0, 0)
            )
            curve.append(
                {
                    "day": day,
                    "equity": 200000 + delta,
                    "realized": realized,
                    "open_pnl": delta - realized,
                    "open_positions": open_count,
                    "stale_marks": [],
                }
            )
            order = {"status": "wait", "exits": []}
            if g == "A" and i == 0:
                order.update(
                    status="simulated_open",
                    legs=["FG609P900"],
                    quantity=1,
                    premium_and_fees=100,
                )
            if g == "A" and i == 2:
                order["exits"] = [
                    {"product": "FG", "pnl": 200, "entry_day": str(CALENDAR[0])}
                ]
            sessions.append(
                {
                    "day": day,
                    "cutoff": day + "T18:00:00+08:00",
                    "decision": {
                        "action": "enter"
                        if order["status"] == "simulated_open"
                        else "wait",
                        "product": "FG",
                    },
                    "execution": order,
                }
            )
        plans.append({"id": g, "curve": curve, "sessions": sessions})
    path = root / "local-data/agent-study" / BATCH / "results.json"
    write(
        path,
        {
            "schema_version": 1,
            "status": "complete",
            "plans": plans,
            "start": str(CALENDAR[0]),
            "end": str(CALENDAR[3]),
        },
    )
    return path


def entry(point):
    return {
        "variant": "baseline",
        "point": point,
        "account": {
            "cash": point["equity"],
            "previous_equity": point["equity"],
            "positions": [],
            "realized": point["realized"],
        },
    }


def seed(root, path):
    plans = json.loads(path.read_bytes())["plans"]
    manifest = path.parent / "run-manifest.json"
    write(manifest, {"market_sha256": "a" * 64})
    return write(
        root / "local-data/rolling-six/seed.json",
        {
            "day": str(CALENDAR[3]),
            "groups": {p["id"]: entry(p["curve"][-1]) for p in plans},
            "recorded_at": "2026-11-22T10:00:00+08:00",
            "source_sha256": {
                "results": sha256(path.read_bytes()).hexdigest(),
                "manifest": sha256(manifest.read_bytes()).hexdigest(),
                "market": "a" * 64,
            },
        },
        True,
    )


def settlement(root, prior, day, *, clock="20:15", wrong_hash=False):
    data = {
        g: entry(v["point"] | {"day": day})
        | {"decision": None, "execution": {"status": "no_sealed_signal", "exits": []}}
        for g, v in prior["groups"].items()
    }
    return write(
        root / "local-data/rolling-six/settlements" / (day + ".json"),
        {
            "day": day,
            "recorded_at": day + "T" + clock + ":00+08:00",
            "prev_sha256": "f" * 64 if wrong_hash else prior["sha256"],
            "baseline_end": str(CALENDAR[3]),
            "groups": data,
            "market_sources": [],
            "market_gaps": [],
            "decision_status": "missed_preopen",
        },
        True,
    )


def view(root, ts):
    return read_rolling_baseline(
        root, now=datetime.fromisoformat(ts), calendar=CALENDAR
    )


@pytest.mark.parametrize(
    ("end", "anchor"),
    [
        ("2024-08-31", "2024-02-29"),
        ("2023-08-31", "2023-02-28"),
        ("2026-03-31", "2025-09-30"),
        ("2026-11-22", "2026-05-22"),
    ],
)
def test_six_calendar_month_anchor(end, anchor):
    assert window_anchor(date.fromisoformat(end)) == date.fromisoformat(anchor)


def test_window_counts_only_later_trades_and_carries_incoming_position(tmp_path):
    history(tmp_path)
    r = view(tmp_path, "2026-11-22T10:00:00+08:00")
    a = r["plans"][0]
    assert (r["status"], r["freshness"], r["start"], r["latest_expected_day"]) == (
        "ready",
        "current",
        "2026-05-22",
        "2026-11-20",
    )
    assert (
        a["baseline_point"]["index_value"] == 100
        and a["baseline_point"]["open_positions"] == 1
    )
    assert a["baseline_point"]["raw_equity"] == 200100
    assert [p["day"] for p in a["curve"]] == ["2026-05-25", "2026-11-20"]
    assert (a["stats"]["opened"], a["stats"]["closed"], a["stats"]["raw_net_pnl"]) == (
        0,
        1,
        100,
    )
    assert a["stats"]["net_pnl"] == 99.95 and a["stats"]["raw_equity"] == 200200
    assert (
        a["stats"]["open_pnl_change"] == -100 and a["stats"]["realized_change"] == 200
    )
    assert a["positions"] == []


def test_natural_weekend_uses_last_trading_close_and_beijing_date(tmp_path):
    history(tmp_path)
    r = view(tmp_path, "2026-11-22T18:00:00+00:00")
    assert r["start"] == "2026-05-23" and r["end"] == "2026-11-23"
    assert (
        r["plans"][0]["baseline_day"] == "2026-05-22"
        and r["latest_expected_day"] == "2026-11-20"
    )
    assert r["latest_day"] == "2026-11-20" and r["freshness"] == "current"
    later = view(tmp_path, "2026-11-23T20:05:00+08:00")
    assert later["freshness"] == "behind" and later["missing_days"] == ["2026-11-23"]


def test_authenticated_forward_extension_never_fills_gaps_or_reads_later_record(
    tmp_path,
):
    path = history(tmp_path)
    prior = seed(tmp_path, path)
    first = settlement(tmp_path, prior, "2026-11-23")
    settlement(tmp_path, first, "2026-11-25")
    early = view(tmp_path, "2026-11-23T20:10:00+08:00")
    assert early["latest_day"] == "2026-11-20" and early["missing_days"] == [
        "2026-11-23"
    ]
    current = view(tmp_path, "2026-11-23T20:30:00+08:00")
    a = current["plans"][0]
    assert (
        current["latest_day"] == "2026-11-23"
        and current["latest_recorded_at"] == "2026-11-23T20:15:00+08:00"
    )
    assert (
        a["sessions"][-1]["decision"] is None and a["curve"][-1]["source"] == "forward"
    )
    assert a["stats"]["raw_equity"] == 200200 and a["curve"][-1]["equity"] == 200099.95
    gap = view(tmp_path, "2026-11-25T21:30:00+08:00")
    assert (
        gap["missing_days"] == ["2026-11-24", "2026-11-25"]
        and gap["latest_day"] == "2026-11-23"
    )
    settlement(tmp_path, first, "2026-11-24", wrong_hash=True)
    broken = view(tmp_path, "2026-11-25T21:30:00+08:00")
    assert broken["status"] == "partial" and "前一笔摘要" in broken["issues"][0]


def test_tampered_seed_does_not_append_standalone_live_cash_or_zero_curve(tmp_path):
    path = history(tmp_path)
    prior = seed(tmp_path, path)
    settlement(tmp_path, prior, "2026-11-23")
    damaged = tmp_path / "local-data/rolling-six/seed.json"
    write(damaged, prior | {"day": "2026-11-19"})
    result = view(tmp_path, "2026-11-23T20:30:00+08:00")
    assert result["latest_day"] == "2026-11-20" and "摘要" in result["issues"][0]
    assert result["plans"][0]["positions"] == []
    absent = read_rolling_baseline(
        tmp_path / "empty",
        now=datetime.fromisoformat("2026-11-23T20:30:00+08:00"),
        calendar=CALENDAR,
    )
    assert absent["status"] == "missing" and absent["plans"] == []


def test_market_marks_do_not_turn_stale_window_into_six_month_flat_history(tmp_path):
    history(tmp_path)
    covered = CALENDAR + (date(2027, 6, 1), date(2027, 6, 2))
    r = read_rolling_baseline(
        tmp_path,
        now=datetime.fromisoformat("2027-06-01T21:00:00+08:00"),
        calendar=covered,
    )
    assert r["latest_day"] == "2026-11-20" and r["freshness"] == "behind"
    assert r["plans"][0]["curve"] == [] and r["plans"][0]["stats"]["return_pct"] is None
    uncertain = view(tmp_path, "2027-06-01T21:00:00+08:00")
    assert (
        uncertain["freshness"] == "calendar_unknown"
        and uncertain["latest_expected_day"] is None
    )


def test_all_sleeves_stay_at_last_good_close_for_one_invalid_signed_daily_account(
    tmp_path,
):
    path = history(tmp_path)
    prior = seed(tmp_path, path)
    doc = settlement(tmp_path, prior, "2026-11-23")
    del doc["sha256"]
    doc["groups"]["C"]["account"]["cash"] -= 500
    write(tmp_path / "local-data/rolling-six/settlements/2026-11-23.json", doc, True)
    r = view(tmp_path, "2026-11-23T20:30:00+08:00")
    assert all(p["latest_day"] == "2026-11-20" for p in r["plans"])
    assert r["missing_days"] == ["2026-11-23"] and "净值无法核对" in r["issues"][0]

from datetime import date, timedelta
import json

import pytest

from app.experiment_view import (
    BATCH,
    compare,
    live_research_policy,
    read_session,
    select_mode,
)


def series(variant, pnl=0, tail=0, opened=0, dd=0):
    return dict(variant=variant, pnl=pnl, tail_pnl=tail, opened=opened, drawdown_pct=dd)


def test_promotion_needs_improvement_in_both_windows_and_risk_limit():
    old = series("baseline", 20, 4, 2, 1)
    better = series("primary", 80, 8, 3, 1.4)
    assert select_mode([old, better, series("ranked", 90, 10, 5, 1.51)]) == "primary"
    assert select_mode([old, better, series("ranked", 90, 10, 5, 1.5)]) == "ranked"
    assert (
        select_mode([old, series("primary", 99, 3, 8), series("ranked", 120, 4, 9)])
        == "baseline"
    )


def test_partial_and_misaligned_archives_never_promote(tmp_path):
    base = tmp_path / "local-data" / "agent-study" / BATCH
    output = base.with_name(BATCH + "-frequency")
    base.mkdir(parents=True)
    output.mkdir()
    (output / "ranking-progress.json").write_text('{"completed_days":17}')
    assert compare(tmp_path)["completed_days"] == 17
    days = [(date(2026, 5, 21) + timedelta(days=i)).isoformat() for i in range(125)]

    def plan(group):
        return {
            "id": group,
            "curve": [{"day": d, "equity": 200_000} for d in days],
            "stats": {"opened": 0, "drawdown_pct": 0},
            "sessions": [{"day": days[0], "id": group}],
        }

    groups = [plan(g) for g in "ABC"]
    (base / "results.json").write_text(
        json.dumps({"status": "complete", "plans": groups})
    )
    groups[1]["curve"][0]["day"] = "2026-05-19"
    (output / "replay.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "settlement_revision": "qualified-account-v2",
                "completed_days": 125,
                "variants": [{"id": v, "plans": groups} for v in ("primary", "ranked")],
            }
        )
    )
    with pytest.raises(ValueError, match="交易日不一致"):
        compare(tmp_path)
    assert live_research_policy(tmp_path) == {
        "per_group": dict.fromkeys("ABC", "baseline"),
        "term_mode": "compare_only",
    }
    assert (
        read_session(tmp_path, "ranked", "C", date.fromisoformat(days[0]))["id"] == "C"
    )
    assert (
        read_session(tmp_path, "../secrets", "C", date.fromisoformat(days[0])) is None
    )


@pytest.mark.parametrize(
    ("n", "tail_n", "advantage", "expected"),
    [
        (9, 5, 1, "compare_only"),
        (10, 4, 1, "compare_only"),
        (10, 5, -1, "compare_only"),
        (10, 5, 1, "consider_far"),
    ],
)
def test_far_requires_independent_events_and_positive_advantage(
    tmp_path, n, tail_n, advantage, expected
):
    folder = tmp_path / "local-data" / "agent-study" / (BATCH + "-frequency")
    folder.mkdir(parents=True)

    def row(count, difference):
        return {
            "event_clusters": count,
            "far_mean_return_pct": 1,
            "far_minus_main_pp": difference,
        }

    (folder / "term.json").write_text(
        json.dumps(
            {
                "summary": {"full": row(n, 1), "holdout": row(tail_n, advantage)},
                "pairs": [],
            }
        )
    )
    assert compare(tmp_path)["term_mode"] == expected
    assert live_research_policy(tmp_path)["term_mode"] == "compare_only"


def test_option_leader_sums_all_strikes_sides_and_does_not_substitute_future():
    from app.term_study import option_leader

    def bar(oi, volume):
        return [1, 1, 1, 1, volume, oi]

    quotes = {
        "MA2609C2000": bar(180, 0),
        "MA2611P1800": bar(90, 5),
        "MA2611C2100": bar(90, 5),
        "SR2611C6000": bar(1000, 100),
        "MA2701P1850": bar(170, 5000),
    }
    assert option_leader("MA", quotes) == "MA2611"
    assert option_leader("JD", quotes) is None

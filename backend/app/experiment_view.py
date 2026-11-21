"""从本机独立归档读取频率与期限对照，按事先约定的规则选择研究模式。"""

from datetime import date
import json
from math import isfinite
from pathlib import Path

from .study_view import BATCH

INITIAL = 200_000
TERM_MIN_EVENTS = 10
TERM_MIN_TAIL_EVENTS = 5


def read_file(path):
    if path.is_file():
        return json.loads(path.read_bytes())
    return None


def plan_metrics(plan):
    curve = plan["curve"]
    days = [date.fromisoformat(p["day"]) for p in curve]
    if (
        len(curve) != 125
        or days != sorted(set(days))
        or not all(isfinite(p["equity"]) and p["equity"] >= 0 for p in curve)
        or not isfinite(plan["stats"]["drawdown_pct"])
    ):
        raise ValueError("频率比较要求完整的125个交易日")
    return {
        "group": plan["id"],
        "open_days": [
            s["day"]
            for s in plan.get("sessions", [])
            if s.get("execution", {}).get("status") == "simulated_open"
        ],
        "opened": plan["stats"]["opened"],
        "pnl": round(curve[-1]["equity"] - INITIAL, 2),
        "tail_pnl": round(curve[-1]["equity"] - curve[-43]["equity"], 2),
        "drawdown_pct": plan["stats"]["drawdown_pct"],
        "curve": [{"day": x["day"], "equity": x["equity"]} for x in curve],
    }


def improves(candidate, reference):
    return (
        candidate["opened"] > reference["opened"]
        and candidate["pnl"] > reference["pnl"]
        and candidate["tail_pnl"] > reference["tail_pnl"]
        and candidate["drawdown_pct"] <= reference["drawdown_pct"] + 0.5
    )


def select_mode(rows):
    by_id = {r["variant"]: r for r in rows}
    selected = "baseline"
    if improves(by_id["primary"], by_id["baseline"]):
        selected = "primary"
    if improves(by_id["ranked"], by_id["primary"]) and improves(
        by_id["ranked"], by_id["baseline"]
    ):
        selected = "ranked"
    return selected


def compare(root: Path):
    base = root / "local-data" / "agent-study" / BATCH
    folder = base.with_name(BATCH + "-frequency")
    result = read_file(folder / "replay.json")
    term = read_file(folder / "term.json")
    option_term = read_file(folder / "term-option.json")
    control = read_file(folder / "control.json")
    original = read_file(base / "results.json")
    rows = []
    selected = {k: "baseline" for k in "ABC"}
    if (
        result
        and result["status"] == "complete"
        and result.get("settlement_revision") == "qualified-account-v2"
        and result.get("completed_days") == 125
        and original
        and original["status"] == "complete"
    ):
        variants = [{"id": "baseline", "plans": original["plans"]}, *result["variants"]]
        if [v["id"] for v in variants] != ["baseline", "primary", "ranked"]:
            raise ValueError("实验版本不完整")
        for v in variants:
            if sorted(p["id"] for p in v["plans"]) != list("ABC"):
                raise ValueError("实验策略不完整")
            rows.extend(plan_metrics(p) | {"variant": v["id"]} for p in v["plans"])
        days = [p["day"] for p in rows[0]["curve"]]
        if any([p["day"] for p in row["curve"]] != days for row in rows[1:]):
            raise ValueError("实验版本的交易日不一致")
        for k in selected:
            selected[k] = select_mode([r for r in rows if r["group"] == k])
    summary = term["summary"] if term else None
    mode = "compare_only"
    if summary:
        full, tail = summary["full"], summary["holdout"]
        if (
            full["event_clusters"] >= TERM_MIN_EVENTS
            and tail["event_clusters"] >= TERM_MIN_TAIL_EVENTS
            and all(
                (v["far_mean_return_pct"] or 0) > 0
                and (v["far_minus_main_pp"] or 0) > 0
                for v in [full, tail]
            )
        ):
            mode = "consider_far"
    progress = result or read_file(folder / "ranking-progress.json") or {}
    return {
        "status": "complete" if rows and term and control else "pending",
        "rows": rows,
        "per_group": selected,
        "term_mode": mode,
        "term": summary,
        "term_pairs": term["pairs"] if term else [],
        "option_anchor": {"term": option_term["summary"], "pairs": option_term["pairs"]}
        if option_term
        else None,
        "control": control["summary"] if control else [],
        "completed_days": progress.get("completed_days", 0),
    }


def live_research_policy(workspace_root: Path) -> dict:
    try:
        obj = compare(workspace_root)
        if obj["status"] == "complete":
            return {"per_group": obj["per_group"], "term_mode": obj["term_mode"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        pass
    return {"per_group": {k: "baseline" for k in "ABC"}, "term_mode": "compare_only"}


def read_session(root: Path, variant: str, group: str, day: date):
    folder = root / "local-data" / "agent-study" / BATCH
    if variant not in {"baseline", "ranked", "primary"} or group not in ("A", "B", "C"):
        return None
    if variant == "baseline":
        data = read_file(folder / "results.json")
        plans = (data or {}).get("plans", [])
    else:
        data = read_file(folder.with_name(BATCH + "-frequency") / "replay.json")
        version = next(
            (v for v in (data or {}).get("variants", []) if v["id"] == variant), {}
        )
        plans = version.get("plans", [])
    for plan in plans:
        if plan["id"] == group:
            return next(
                (s for s in plan["sessions"] if s["day"] == day.isoformat()), None
            )
    return None

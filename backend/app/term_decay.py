"""按成交前的权利金反解隐波，隔离一天时间流逝造成的理论价值变化。"""

from collections import defaultdict
from datetime import date
from math import erfc, log, sqrt
from statistics import mean


def black_price(forward, strike, width, side):
    sign = 1 if side == "C" else -1
    if width <= 0:
        return max(sign * (forward - strike), 0.0)
    d1 = log(forward / strike) / width + width / 2
    d2 = d1 - width

    def cdf(z):
        return erfc(-z / sqrt(2)) / 2

    return sign * (forward * cdf(sign * d1) - strike * cdf(sign * d2))


def daily_decay(forward, strike, premium, side, days):
    if not (side in {"C", "P"} and forward > 0 and strike > 0 and days >= 1):
        return None
    lower, upper = 0.0, 12.0
    if (
        not black_price(forward, strike, lower, side)
        < premium
        < black_price(forward, strike, upper, side)
    ):
        return None
    for _ in range(55):
        mid = (lower + upper) / 2
        if black_price(forward, strike, mid, side) < premium:
            lower = mid
        else:
            upper = mid
    next_width = (lower + upper) / 2 * sqrt((days - 1) / days)
    return max(0.0, premium - black_price(forward, strike, next_width, side))


def explain_decay(rows, budget):
    events = defaultdict(list)
    for pair in rows:
        as_of = date.fromisoformat(pair["selection_cutoff"][:10])
        results = {}
        for name in ("main", "far"):
            structure = pair[name]
            parts = []
            for leg in structure["legs"]:
                expiry = leg.get("expiry")
                if not expiry:
                    break
                days = (date.fromisoformat(expiry) - as_of).days
                loss = daily_decay(
                    structure["underlying_price"],
                    leg["strike"],
                    leg["premium"],
                    leg["side"],
                    days,
                )
                if loss is None:
                    break
                parts.append(loss)
            if len(parts) == 2:
                gross = sum(leg["premium"] for leg in structure["legs"])
                cost = sum(parts) * pair["multiplier"] * structure["planned_quantity"]
                results[name] = {
                    "yuan": round(cost, 4),
                    "pct_budget": round(100 * cost / budget, 5),
                    "pct_premium": round(100 * sum(parts) / gross, 5),
                }
        pair["theoretical_one_day_decay"] = results
        if len(results) == 2:
            events[pair["event_at"]].append(results)
    summary = {
        "paired_products": sum(map(len, events.values())),
        "event_clusters": len(events),
    }
    for name in ("main", "far"):
        for key in ("yuan", "pct_budget", "pct_premium"):
            values = [mean(x[name][key] for x in peers) for peers in events.values()]
            summary[name + "_" + key] = round(mean(values), 5) if values else None
    return summary

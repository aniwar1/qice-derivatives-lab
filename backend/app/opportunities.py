from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from typing import Any, Callable

from .models import AgentStrategyPayload, StrategyRunRequest
from .research_archive import ResearchCapture, read_run_artifact


def read_candidates(run_dir: Path) -> tuple[str, list]:
    try:
        payload = json.loads(read_run_artifact(run_dir, "opportunities.json", max_bytes=128_000))
    except FileNotFoundError:
        return "not_returned", []
    except (OSError, ValueError):
        return "invalid", []
    if not isinstance(payload, list) or len(payload) > 22:
        return "invalid", []
    return "returned", payload


def compile_opportunities(
    primary: AgentStrategyPayload,
    request: StrategyRunRequest,
    capture: ResearchCapture | None,
    run_dir: Path,
    validate: Callable[[AgentStrategyPayload, StrategyRunRequest], AgentStrategyPayload],
) -> dict[str, Any]:
    status, candidates = read_candidates(run_dir) if request.mode == "all_market_scan" and request.option_analysis_requested else ("not_requested", [])
    found = {item.product: item for item in capture.observations} if capture else {}
    accepted: dict[str, dict] = {}
    sleeves: dict[str, dict] = {}
    admitted_groups = set()
    rejected = 0
    for index, raw in enumerate([primary, *candidates]):
        try:
            item = primary if index == 0 else AgentStrategyPayload.model_validate(raw)
            plans = [p for p in item.agent_review.plans if p.action == "paper_entry"] if item.agent_review else []
            if index == 0 and not plans and item.decision == "wait":
                continue
            if not item.product or (item.decision not in ("long_call", "long_put") and not plans):
                raise ValueError("机会缺少合格的期权计划")
            if item.product in sleeves or (index > 0 and item.product == primary.product):
                raise ValueError("同批同品种重复")
            if abs(item.as_of - primary.as_of) > timedelta(minutes=5):
                raise ValueError("候选策略与本轮时点不一致")
            remaining = [p for p in plans if p.strategy not in admitted_groups]
            if not remaining:
                raise ValueError("同组本轮已有合格的首选方案")
            if index > 0:
                item = validate(item, StrategyRunRequest(mode="single_analysis", product=item.product))
            quote = found.get(item.product)
            if (quote is None or quote.contract != item.analysis_contract or quote.available_at > item.as_of
                or item.as_of - quote.observed_at > timedelta(minutes=30)
                or item.market_levels.quote_at != quote.observed_at
                or item.market_levels.current_price != quote.price):
                raise ValueError("买方机会缺少同月、同一决策窗口的本机行情证据")
        except ValueError:
            rejected += 1
            continue
        review = item.agent_review.model_dump(mode="json")
        for plan in review["plans"]:
            if plan["action"] == "paper_entry" and plan["strategy"] in admitted_groups:
                plan.update(action="wait", quantity=0, max_holding_days=0, legs=[],comparison_symbols=[],
                            event_source_id=None, event_at=None, scenario_high=None, scenario_low=None,
                            blocked_first_choice_legs=[], blocked_first_choice_report=None,
                            reason="该策略本轮已有更早通过校验的品种。", invalidation="下一轮重新评估。")
        admitted_groups.update(p.strategy for p in remaining)
        sleeves[item.product] = {"product": item.product, "analysis_contract": item.analysis_contract,
                                 "as_of": item.as_of.isoformat(), "agent_review": review}
        if item.option_expression is None or not any(
            p.strategy in {"A", "B"} and p.legs[0].upper().replace("-", "") == item.option_expression.symbol.upper().replace("-", "")
            for p in remaining
        ):
            continue
        accepted[item.product] = {
            "product": item.product, "product_name": item.product_name, "contract": item.analysis_contract,
            "option_symbol": item.option_expression.symbol, "decision": item.decision, "direction": item.direction,
            "entry_timing": item.entry_timing, "thesis": item.rationale.thesis, "why_now": item.rationale.why_now,
            "counterevidence": item.rationale.strongest_counterevidence,
            "trigger": item.market_levels.trigger, "invalidation": item.market_levels.invalidation,
            "target": item.market_levels.target, "reassess_by": item.reassess_by.isoformat(),
            "as_of": item.as_of.isoformat(), "evidence_gaps": item.evidence_gaps,
            "option_sources": [source.model_dump(mode="json") for source in item.option_market.sources],
            "option": item.option_expression.model_dump(mode="json"),
            "market_evidence_sha256": quote.artifact_sha256,
        }
    return {"candidate_status": status, "excluded_count": rejected, "opportunities": list(accepted.values()),
            "sleeve_plans": list(sleeves.values())}


def compare_opportunities(previous: list[dict], current: list[dict]) -> dict[str, list[str]]:
    before = {item["product"]: item for item in previous}
    after = {item["product"]: item for item in current}
    fields = ("decision", "contract", "option_symbol", "entry_timing", "trigger", "invalidation", "target", "evidence_gaps")
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(
            product for product in before.keys() & after.keys()
            if any(before[product][field] != after[product][field] for field in fields)
        ),
    }

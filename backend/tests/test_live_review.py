from datetime import timedelta
from hashlib import sha256

import pytest

from app.live_review import bind_public_domains, validate_agent_review
from app.member_positions import collect_member_positions
from app.models import AgentStrategyPayload
from conftest import strategy_payload
from test_research_archive import evidence


def _case():
    strategy = strategy_payload(decision="wait")
    clock = strategy.as_of
    quote, _ = evidence("JM", "JM2701", clock, clock, 3520)
    context = {
        "cutoff": clock.isoformat(), "fee_per_leg": 3, "entry_budget": 2000, "multipliers": {"JM": 10},
        "member_positions": {"items": []}, "sources": {"items": []}, "options": {"books": []},
    }
    for key in ["fundamentals", "news"]:
        context["sources"]["items"].append({
            "id": key, "products": ["JM"], "dimension": key, "authority": "primary_official",
            "scope": "direct", "published_at": clock.isoformat(), "available_at": clock.isoformat(),
            "retrieved_at": clock.isoformat(), "url": "https://www.stats.gov.cn/example",
            "sha256": "b" * 64, "scheduled_at": (clock + timedelta(hours=2)).isoformat() if key == "news" else None,
        })
    for side, strike in [("C", 3600), ("P", 3450)]:
        context["options"]["books"].append({"symbol": f"jm2701{side}{strike}", "contract": "JM2701", "side": side,
            "strike": strike, "source_url": "https://example.com/book", "sha256": "c" * 64, "bid": 19, "ask": 20, "ask_size": 4, "bid_size": 3,
            "observed_at": clock.isoformat(), "available_at": clock.isoformat()})
    raw = strategy.model_dump(mode="json")
    review = raw["agent_review"]
    review["technical"]["source_ids"] = [quote.artifact_sha256]
    for key in ["fundamentals", "news"]:
        review[key]["source_ids"] = [key]
    review["plans"][2].update(action="paper_entry", quantity=2, max_holding_days=1,
                              legs=[tick["symbol"] for tick in context["options"]["books"]], event_source_id="news",
                              event_at=(clock + timedelta(hours=2)).isoformat(), scenario_low=3320, scenario_high=3750)
    return raw, context, [quote]


def test_c_pair_and_backward_compatible_directional_projection():
    raw, context, rows = _case()
    strategy = AgentStrategyPayload.model_validate(raw)
    validate_agent_review(strategy, context, rows)
    public = bind_public_domains(strategy, context, rows)
    assert public.agent_review.plans[2].quantity == 2
    assert public.decision == "wait" and public.option_expression is None
    assert public.technical_structure.sources[0].content_sha256 == rows[0].artifact_sha256


@pytest.mark.parametrize("defect", ["other_product", "after_cutoff", "wrong_domain", "untrusted", "missing_leg",
                                    "depth", "event_time", "excess_cost", "unsupported_ownership", "stale_pair",
                                    "uneconomic_move", "wrong_clock"])
def test_invalid_evidence_or_unexecutable_pair_cannot_be_published(defect):
    raw, context, rows = _case()
    source = context["sources"]["items"][-1]
    plan = raw["agent_review"]["plans"][2]
    if defect == "other_product":
        source["products"] = ["M"]
    elif defect == "after_cutoff":
        source["available_at"] = plan["event_at"]
    elif defect == "wrong_clock":
        context["cutoff"] = (rows[0].observed_at + timedelta(minutes=2)).isoformat()
    elif defect == "wrong_domain":
        source["dimension"] = "fundamentals"
    elif defect == "untrusted":
        source["authority"] = "secondary"
    elif defect == "missing_leg":
        context["options"]["books"].pop()
    elif defect == "depth":
        plan["quantity"] = 4
    elif defect == "event_time":
        source["scheduled_at"] = raw["as_of"]
    elif defect == "excess_cost":
        context["entry_budget"] = 100
    elif defect == "unsupported_ownership":
        raw["agent_review"]["positioning"].update(classification="exchange_member_aggregate", source_ids=["seat"])
    elif defect == "stale_pair":
        context["options"]["books"][0]["observed_at"] = (rows[0].observed_at - timedelta(seconds=6)).isoformat()
    elif defect == "uneconomic_move":
        plan.update(scenario_low=3460, scenario_high=3570)
    with pytest.raises(ValueError):
        validate_agent_review(AgentStrategyPayload.model_validate(raw), context, rows)


def test_exchange_member_rank_is_scoped_to_contract_and_owner_remains_unknown(monkeypatch):
    from app import member_positions
    from datetime import datetime, timezone
    clock = datetime(2026, 1, 19, 9, tzinfo=timezone.utc)
    row, _ = evidence("CU", "CU2603", clock, clock)
    row.daily_bars = [{"date": "2026-01-19"}]
    stub = {"RANK": 1, "PARTICIPANTABBR2": "甲", "CJ2": 12, "PARTICIPANTABBR3": "乙", "CJ3": 23}
    class Response:
        content = b"archive"
        def raise_for_status(self):
            pass
        def json(self):
            return {"report_date": "20260119", "update_date": "20260119 15:25:00",
                    "o_cursor": [{**stub, "INSTRUMENTID": "cu2603"}, {**stub, "INSTRUMENTID": "cu2604"}]}
    monkeypatch.setattr(member_positions.requests, "get", lambda *args, **kw: Response())
    packet, blobs = collect_member_positions([row])
    item, = packet["items"]
    assert len(item["ranks"]) == 1 and item["ranks"][0]["short_member"] == "乙"
    assert item["ownership"] == "unknown" and item["basis"] == "exchange_member_aggregate"
    assert blobs[sha256(Response.content).hexdigest()] == Response.content


def test_a_needs_direct_fundamental_and_option_comparison():
    raw, context, rows = _case()
    directional = strategy_payload(decision="long_call").model_dump(mode="json")
    for key in ("decision", "direction", "entry_style", "entry_timing", "option_expression", "market_levels"):
        raw[key] = directional[key]
    call = context["options"]["books"][0]
    raw["market_levels"]["quote_at"] = raw["as_of"]
    raw["option_expression"].update(bid=call["bid"], ask=call["ask"], quote_at=call["observed_at"])
    peer = call | {"strike": 3650, "symbol": "jm2701C3650"}
    context["options"]["books"].append(peer)
    raw["agent_review"]["plans"][0].update(action="paper_entry", quantity=1, max_holding_days=2,
                                          legs=[call["symbol"]], comparison_symbols=[peer["symbol"]])
    strategy = AgentStrategyPayload.model_validate(raw)
    validate_agent_review(strategy, context, rows)
    fundamental = context["sources"]["items"][0]
    fundamental["scope"] = "supply_chain"
    with pytest.raises(ValueError, match="直接基本面"):
        validate_agent_review(strategy, context, rows)
    fundamental.update(scope="direct", freshness_role="low_frequency_background_only")
    with pytest.raises(ValueError, match="直接基本面"):
        validate_agent_review(strategy, context, rows)
    for plan in raw["agent_review"]["plans"]:
        if plan["strategy"] == "B":
            plan["strategy"] = "A"
        elif plan["strategy"] == "A":
            plan.update(strategy="B", quantity=6, comparison_symbols=[])
    call.update(ask_size=8, bid_size=8)
    with pytest.raises(ValueError, match="超过入场预算"):
        validate_agent_review(AgentStrategyPayload.model_validate(raw), context, rows)
    fundamental["freshness_role"] = "dated_national_statistics"
    validate_agent_review(AgentStrategyPayload.model_validate(raw), context, rows)


def test_single_product_c_requires_observable_requested_focus(settings):
    from app.main import create_app
    from app.models import StrategyRunRequest
    from app.service import StrategyValidationError
    from conftest import FakeDomain, FakeGateway

    raw, _, _ = _case()
    request = StrategyRunRequest(mode="single_analysis", product="JM")
    raw["scan_coverage"].update(scope="single_product", expected_count=1, attempted_count=1,
                                products=[raw["scan_coverage"]["products"][1]])
    service = create_app(settings=settings, gateway=FakeGateway()).state.strategy_service
    service.domain = FakeDomain(settings.workspace_root)
    service._validate_result(AgentStrategyPayload.model_validate(raw), request, ("M", "JM"))
    raw["scan_coverage"]["products"][0].update(status="unobservable", known_gap="缺少盘口")
    with pytest.raises(StrategyValidationError, match="unobservable scan row"):
        service._validate_result(AgentStrategyPayload.model_validate(raw), request, ("M", "JM"))

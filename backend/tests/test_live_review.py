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


def _live_pair_case():
    from app.live_options import event_pairs
    raw, ctx, rows = _case()
    row = rows[0]
    # 固定到两个月份均覆盖退出窗口的交易日，避免测试依赖本机当前月份。
    from datetime import datetime
    target = datetime(2026, 10, 22, 10, tzinfo=row.observed_at.tzinfo)
    offset = target - row.observed_at
    def shift(obj):
        if isinstance(obj, dict):
            return {k: shift(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [shift(v) for v in obj]
        if isinstance(obj, str) and 'T' in obj:
            try: return (datetime.fromisoformat(obj) + offset).isoformat()
            except ValueError: pass
        return obj
    raw, ctx = shift(raw), shift(ctx)
    row = row.model_copy(update={"observed_at": target, "available_at":target})
    ctx.update(holding_sessions=["2026-10-22", "2026-10-23"], research_policy={"term_mode":"consider_far", "per_group":dict.fromkeys("ABC","primary")},
               paper_account={"status":"unavailable"}, paper_equity_reference=200000)
    term = {"product":"JM", "price":3520,"observed_at":raw["as_of"],"available_at":raw["as_of"], "sha256":row.artifact_sha256}
    far = term | {"price":3540, "contract":"JM2703", "role":"far"}
    row.forward_quotes = [far]
    ctx["options"]["underlyings"] = [term | {"contract":"JM2701", "role":"focus"}, far]
    near = ctx["options"]["books"]
    fs = [t | {"symbol": t["symbol"].replace("2701","2703"), "contract":"JM2703"} for t in near]
    near.extend(fs)
    ctx["options"]["expiry_sources"] = [{"sha256":"d"*64, "retrieved_at":raw["as_of"]}]
    for b in near:
        b.update(expiry_sha256="d"*64, expiry_at="2026-12-01T15:00:00+08:00")
    ctx["event_pairs"] = event_pairs(ctx)
    raw["agent_review"]["plans"][2]["legs"] = [v["symbol"] for v in fs]
    return raw, ctx, [row]


def test_far_pair_is_admissible_only_with_executable_near_comparison():
    raw, ctx, rows = _live_pair_case()
    model = AgentStrategyPayload.model_validate(raw)
    validate_agent_review(model, ctx, rows)
    ctx["research_policy"]["term_mode"] = "compare_only"
    with pytest.raises(ValueError, match="近远月对照"):
        validate_agent_review(model, ctx, rows)
    ctx["research_policy"]["term_mode"] = "consider_far"
    ctx["event_pairs"] = [x for x in ctx["event_pairs"] if x["role"] == "far"]
    with pytest.raises(ValueError, match="近远月对照"):
        validate_agent_review(model, ctx, rows)


def test_c_near_expiry_and_stale_spot_are_filtered_before_and_after_selection():
    from app.live_options import event_pairs
    raw, ctx, rows = _live_pair_case()
    near = [b for b in ctx["options"]["books"] if b["contract"] == "JM2701"]
    raw["agent_review"]["plans"][2]["legs"] = [b["symbol"] for b in near]
    model = AgentStrategyPayload.model_validate(raw)
    ctx["holding_sessions"] = ["2026-12-01"]
    assert all(p["role"] != "focus" for p in event_pairs(ctx))
    with pytest.raises(ValueError, match="到期缓冲"):
        validate_agent_review(model, ctx, rows)
    ctx["holding_sessions"] = ["2026-10-22"]
    rows[0].observed_at -= timedelta(minutes=3)
    model.market_levels.quote_at = rows[0].observed_at
    ctx["options"]["underlyings"][0]["observed_at"] = rows[0].observed_at.isoformat()
    assert all(p["role"] != "focus" for p in event_pairs(ctx))
    with pytest.raises(ValueError, match="同期的标的"):
        validate_agent_review(model, ctx, rows)


def test_forward_ledger_budget_is_independent_of_historical_study(tmp_path):
    import json
    from datetime import datetime, timezone
    from app.live_paper import entry_budget, paper_account
    now = datetime.now(timezone.utc) + timedelta(seconds=2)
    sleeve = {"cash": 98000, "equity": 100000, "positions":[{"product":"JM", "contract":"JM2701", "quantity":1, "open_cost":7500}]}
    fixture = {"schema_version":"forward_paper_account.v1", "as_of":(now - timedelta(seconds=1)).isoformat(),
               "valid_until":(now + timedelta(hours=3)).isoformat(), "sleeves":dict.fromkeys("ABC", sleeve)}
    folder = tmp_path / 'local-data'
    folder.mkdir()
    prior_study = folder / 'agent-study'
    prior_study.mkdir()
    (prior_study/'results.json').write_text(json.dumps(fixture))
    assert paper_account(tmp_path, now)["sleeves"] is None
    source = folder/'forward-paper-account.json'
    source.write_text(json.dumps(fixture))
    snapshot = paper_account(tmp_path, now)
    assert snapshot['status'] == 'observed' and snapshot['sha256'] == sha256(source.read_bytes()).hexdigest()
    assert entry_budget({'paper_account':snapshot},'B','JM') == 0
    assert entry_budget({'paper_account':snapshot},'C','M') == 500
    assert paper_account(tmp_path, now+timedelta(days=1))['status'] == 'unavailable'
    fixture['as_of']=(now + timedelta(seconds=1)).isoformat()
    source.write_text(json.dumps(fixture))
    assert paper_account(tmp_path, now)['status'] == 'unavailable'


def test_ranked_replacement_requires_previously_blocked_order():
    raw, ctx, rows = _live_pair_case()
    plan = raw['agent_review']['plans'][2]
    plan['legs'] = [b['symbol'] for b in ctx['options']['books'] if b['contract']=='JM2701']
    alt = [b | {'contract':'M2701','symbol':'m2701'+b['side']+str(b['strike'])} for b in ctx['options']['books'][:2]]
    ctx['multipliers']['M']=10
    ctx['options']['books'].extend(alt)
    plan['blocked_first_choice_legs']=[b['symbol'] for b in alt]
    model=AgentStrategyPayload.model_validate(raw)
    with pytest.raises(ValueError,match='无已授权'):
        validate_agent_review(model,ctx,rows)
    ctx['research_policy']['per_group']['C']='ranked'
    with pytest.raises(ValueError, match='完整三面'):
        validate_agent_review(model,ctx,rows)
    import json
    original = json.loads(json.dumps(raw).replace('JM','M').replace('jm','m'))
    original['scan_coverage'].update(scope='single_product', expected_count=1, attempted_count=1,
                                     products=[original['scan_coverage']['products'][1]])
    other_quote, _ = evidence('M','M2701',rows[0].observed_at,rows[0].available_at,3520)
    rows.append(other_quote)
    original['agent_review']['technical']['source_ids'] = [other_quote.artifact_sha256]
    original['agent_review']['plans'][2].update(blocked_first_choice_legs=[], legs=[t['symbol'] for t in alt])
    plan['blocked_first_choice_report'] = original
    for f in ctx['sources']['items']:
        f['products'].append('M')
    model = AgentStrategyPayload.model_validate(raw)
    with pytest.raises(ValueError,match='通过账户'):
        validate_agent_review(model,ctx,rows)
    one = {'equity':100000,'cash':99000,'positions':[{'product':'M', 'contract':'M2701','quantity':1,'open_cost':1000}]}
    ctx['paper_account'] = {'status':'observed','sleeves':dict.fromkeys('ABC',one)}
    validate_agent_review(model,ctx,rows)


def test_czce_standard_contracts_use_exchange_expiry_not_delivery_month():
    from types import SimpleNamespace
    from datetime import date
    from app.live_expiry import expiry_rows
    def xml(symbol, day, kind='常规期权'):
        return f'<Contract><CtrCd>{symbol}</CtrCd><PrdTp>{kind}</PrdTp><ExchCd>XZCE</ExchCd><ExpiryTime>{day} 15:00 北京时间</ExpiryTime><LstTrdDt>{day}</LstTrdDt><ExpiryDt>{day}</ExpiryDt></Contract>'
    payload = '<Contracts>' + xml('AP610C10000','2026-08-27') + xml('PK610C7200','2026-09-11') + xml('AP610P8500','2026-08-17','短期期权') + '</Contracts>'
    rows = expiry_rows('CZCE', date(2026,8,26), SimpleNamespace(content=payload.encode()))
    assert rows == {'AP610C10000':'2026-08-27T15:00:00+08:00','PK610C7200':'2026-09-11T15:00:00+08:00'}


def test_ranked_first_choice_must_be_eligible_independent_of_budget():
    raw, ctx, rows = _live_pair_case()
    plan = raw['agent_review']['plans'][2]
    plan['blocked_first_choice_legs'] = [b['symbol'] for b in ctx['options']['books'] if b['contract']=='JM2701']
    ctx['research_policy']['per_group']['C'] = 'ranked'
    with pytest.raises(ValueError, match='不同品种'):
        validate_agent_review(AgentStrategyPayload.model_validate(raw), ctx, rows)


async def test_far_pair_is_published_by_the_single_product_api(settings, monkeypatch):
    import json
    from dataclasses import replace
    import httpx
    from app import service as runtime
    from app.main import create_app
    from app.research_archive import ResearchCapture
    from conftest import FakeDomain, FakeGateway
    from test_api import wait_for_terminal

    raw, ctx, rows = _case()
    row = rows[0]
    ctx['options']['books'] = [b | {'contract':'JM2703','symbol':b['symbol'].replace('2701','2703')} for b in ctx['options']['books']] + ctx['options']['books']
    raw['agent_review']['plans'][2]['legs'] = [b['symbol'] for b in ctx['options']['books'][:2]]
    raw['scan_coverage'].update(scope='single_product',expected_count=1,attempted_count=1,
                                products=[raw['scan_coverage']['products'][1]])
    term = {'product':'JM','price':row.price,'observed_at':raw['as_of'],'available_at':raw['as_of'],'sha256':row.artifact_sha256}
    far = term | {'role':'far','contract':'JM2703','price':3540}
    row.forward_quotes = [far]
    ctx['options']['underlyings'] = [term | {'contract':'JM2701','role':'focus'},far]
    original = b'official release bytes'
    digest = sha256(original).hexdigest()
    for doc in ctx['sources']['items']:
        doc['sha256'] = digest
    source = settings.workspace_root/'local-data'/'agent-sources'/'blobs'/digest
    source.parent.mkdir(parents=True)
    source.write_bytes(original)
    config = settings.workspace_root/'config'
    config.mkdir()
    (config/'product_contract_specs.json').write_text(json.dumps({'products':[{'symbol':'JM','multiplier':10}]}))
    book_blob = b'option ticks'
    book_hash = sha256(book_blob).hexdigest()
    expiry_sha = sha256(b'exchange expiry table').hexdigest()
    for b in ctx['options']['books']:
        b.update(sha256=book_hash,expiry_at='2029-01-01T15:00:00+08:00',expiry_sha256=expiry_sha)
    ctx['options']['expiry_sources']=[{'sha256':expiry_sha,'retrieved_at':raw['as_of']}]
    async def market(workspace, rid, request, expected):
        _, blob = evidence('JM','JM2701',row.observed_at,row.available_at,3520)
        return ResearchCapture(run_id=rid,captured_at=row.available_at,expected_products=['JM'],observations=rows,gaps={}), blob
    monkeypatch.setattr(runtime,'capture_market',market)
    monkeypatch.setattr(runtime,'collect_agent_sources',lambda *args,**kwargs:ctx['sources'])
    monkeypatch.setattr(runtime,'collect_option_books',lambda obs:(ctx['options'],{book_hash:book_blob,expiry_sha:b'exchange expiry table'}))
    monkeypatch.setattr(runtime,'holding_sessions',lambda stamp:[(stamp+timedelta(days=1)).date().isoformat()])
    monkeypatch.setattr(runtime,'live_research_policy',lambda root:{'per_group':dict.fromkeys('ABC','primary'),'term_mode':'consider_far'})
    app=create_app(settings=replace(settings,capture_market_evidence=True),gateway=FakeGateway(AgentStrategyPayload.model_validate(raw)))
    app.state.strategy_service.domain=FakeDomain(settings.workspace_root)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
        rid=(await client.post('/api/strategy-runs',json={'mode':'single_analysis','product':'JM'})).json()['run_id']
        assert (await wait_for_terminal(client,rid))['status']=='completed'
        research=(await client.get('/api/research/runs/'+rid)).json()
        review=research['sleeve_plans'][0]['agent_review']
        assert '条件计划' in review['sizing_basis']
        assert review['plans'][2]['action']=='paper_entry' and all('2703' in leg for leg in review['plans'][2]['legs'])
        assert (await client.get('/api/research/runs/'+rid+'/evidence/'+expiry_sha)).content==b'exchange expiry table'

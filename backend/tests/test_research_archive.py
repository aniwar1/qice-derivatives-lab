from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.opportunities import compare_opportunities
from app.research_archive import MarketObservation, ResearchArchive, ResearchCapture
from conftest import FakeDomain, FakeGateway, strategy_payload
from test_api import wait_for_terminal


def evidence(product, contract, observed, available, price=3520):
    blob = json.dumps({"contract": contract, "price": price, "observed": observed.isoformat()}).encode()
    digest = sha256(blob).hexdigest()
    item = MarketObservation(
        product=product, contract=contract, observed_at=observed, available_at=available,
        price=price, source="https://example.com/market", artifact_sha256=digest,
    )
    return item, {digest: blob}


def test_duckdb_freezes_evidence_and_uses_availability_and_same_contract(tmp_path):
    archive = ResearchArchive(tmp_path / "archive.duckdb")
    archive.initialize()
    start = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
    for index, contract, observed, available in (
        (1, "JM2701", 1, 2), (2, "JM2701", 2, 12), (3, "JM2705", 3, 4),
    ):
        row, objects = evidence("JM", contract, start + timedelta(minutes=observed), start + timedelta(minutes=available))
        capture = ResearchCapture(run_id=str(index), captured_at=row.available_at, expected_products=["JM"], observations=[row], gaps={})
        stamp = archive.save(capture, objects)
        assert archive.save(capture, objects) == stamp
        assert archive.evidence(str(index), row.artifact_sha256) == objects[row.artifact_sha256]
    assert [row.observed_at for row in archive.followup("JM2701", start, start + timedelta(minutes=6))] == [start + timedelta(minutes=1)]
    assert archive.evidence("1", row.artifact_sha256) is None
    with pytest.raises(ValueError, match="不能被替换"):
        archive.save(capture.model_copy(update={"captured_at": capture.captured_at + timedelta(seconds=1)}), objects)
    with pytest.raises(ValueError, match="原件与摘要"):
        archive.save(capture, {row.artifact_sha256: b"modified"})


async def test_market_batch_persists_multiple_candidates_and_replays_frozen_evidence(settings, monkeypatch):
    import app.service as service_module

    primary = strategy_payload(decision="long_call", product="JM")
    primary = primary.model_copy(update={"market_levels": primary.market_levels.model_copy(update={
        "quote_at": primary.as_of - timedelta(minutes=2),
    })})
    def with_plan(payload):
        record = payload.model_dump(mode="json")
        row, _ = evidence(payload.product, payload.analysis_contract, primary.as_of - timedelta(minutes=2),
                          primary.as_of - timedelta(minutes=1))
        record["agent_review"]["technical"]["source_ids"] = [row.artifact_sha256]
        record["agent_review"]["plans"][1].update(action="paper_entry", quantity=1, max_holding_days=1,
                                                 legs=[payload.option_expression.symbol])
        return type(payload).model_validate(record)

    primary = with_plan(primary)
    second = with_plan(strategy_payload(decision="long_call", product="M")).model_dump(mode="json")
    second["market_levels"]["quote_at"] = (primary.as_of - timedelta(minutes=2)).isoformat()
    second["scan_coverage"].update(scope="single_product", expected_count=1, attempted_count=1,
                                   products=[second["scan_coverage"]["products"][0]])
    invalid = json.loads(json.dumps(second))
    invalid["option_expression"]["ask"] = None
    price_mismatch = json.loads(json.dumps(second))
    price_mismatch["market_levels"]["current_price"] += 1

    async def collect(workspace, run_id, request, expected):
        rows, objects = [], {}
        for product in expected:
            observation, blob = evidence(product, f"{product}2701", primary.as_of - timedelta(minutes=2), primary.as_of - timedelta(minutes=1))
            rows.append(observation)
            objects.update(blob)
        return ResearchCapture(run_id=run_id, captured_at=primary.as_of, expected_products=list(expected), observations=rows, gaps={}), objects

    class BatchGateway(FakeGateway):
        async def run_strategy(self, prompt, *, workspace_root: Path | None = None):
            assert workspace_root is not None
            directory = next((workspace_root / "local-data" / "codex-runs").iterdir())
            (directory / "opportunities.json").write_text(json.dumps([invalid, price_mismatch, second]), encoding="utf-8")
            return await super().run_strategy(prompt, workspace_root=workspace_root)

    def books(rows):
        raw = b"paper quote data"
        digest = sha256(raw).hexdigest()
        return {"books": [{"contract": row.contract, "symbol": f"{row.contract}-C-3600",
                            "sha256": digest, "source_url": "https://example.com/options", "side": "C", "strike": 3600.0, "bid": 40.0, "ask": 41.0,
                            "ask_size": 2, "bid_size": 1, "observed_at": primary.as_of.isoformat(),
                            "available_at": primary.as_of.isoformat()} for row in rows], "gaps": {}}, {digest: raw}

    config = settings.workspace_root / "config"
    config.mkdir(exist_ok=True)
    (config / "product_contract_specs.json").write_text(json.dumps({
        "products": [{"symbol": symbol, "multiplier": 10} for symbol in ["JM", "M"]]
    }))
    supplemental = b"issuer report contents"
    original_digest = sha256(supplemental).hexdigest()
    rel_name = f"originals/{original_digest}.pdf"
    original = settings.workspace_root / "local-data/agent-sources/supplement" / rel_name
    original.parent.mkdir(parents=True)
    original.write_bytes(supplemental)
    now = primary.as_of.isoformat()
    official = {"id": "issuer", "authority": "primary_official", "published_at": now, "available_at": now,
                "retrieved_at": now, "raw": rel_name, "sha256": original_digest}
    monkeypatch.setattr(service_module, "collect_agent_sources", lambda *args, **kwargs: {"items": [official], "gaps": []})
    monkeypatch.setattr(service_module, "collect_option_books", books)
    monkeypatch.setattr(service_module, "capture_market", collect)
    gateway = BatchGateway(primary)
    app = create_app(settings=replace(settings, capture_market_evidence=True), gateway=gateway)
    service = app.state.strategy_service
    service.domain = FakeDomain(settings.workspace_root)
    service.dashboard.domain = service.domain
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            start = await client.post("/api/strategy-runs", json={})
            run_id = start.json()["run_id"]
            terminal = await wait_for_terminal(client, run_id)
            assert terminal["status"] == "completed"
            base = f"/api/research/runs/{run_id}"
            result = (await client.get(base)).json()
            frozen = result["archive"]["agent_context"]
            assert frozen["paper_account"]["status"] == "unavailable" and frozen["paper_account"]["sleeves"] is None
            assert frozen["research_policy"] == {"term_mode": "compare_only", "per_group": dict.fromkeys("ABC", "baseline")}
            assert frozen["directional_precheck"] == {"A": [], "B": []}
            assert result["candidate_status"] == "returned"
            assert result["excluded_count"] == 3
            assert {item["product"] for item in result["opportunities"]} == {"JM"}
            assert len(result["archive"]["observations"]) == 2
            digest = result["archive"]["observations"][0]["artifact_sha256"]
            raw = await client.get(f"{base}/evidence/{digest}")
            assert sha256(raw.content).hexdigest() == digest
            original.unlink()
            response = await client.get(f"{base}/evidence/{original_digest}")
            assert response.status_code == 200 and response.content == supplemental
            assert not gateway.workspace_roots[0].exists()

            product = (await client.get("/api/workbench/products/jm")).json()
            assert product["observation"]["contract"] == "JM2701"
            assert product["evidence_run_id"] == run_id
            assert product["analysis"]["result"]["strategy"]["product"] == "JM"
            other = (await client.get("/api/workbench/products/M")).json()
            assert other["observation"]["contract"] == "M2701"
            assert other["analysis"] is None
            assert (await client.get("/api/workbench/products/ABSENT")).status_code == 404

            past = primary.as_of - timedelta(minutes=1, seconds=30)
            historical = (await client.get(f"{base}/replay", params={"at": past.isoformat()})).json()
            assert historical["validation"] == "not_published"
            assert historical["observations"] == []
            assert historical["excluded_after_cutoff"] == 2

            timestamp = datetime.fromisoformat(result["archive"]["agent_context"]["cutoff"]) + timedelta(milliseconds=2)
            observation, blob = evidence("JM", "JM2701", timestamp, timestamp + timedelta(milliseconds=1), price=3445)
            later = ResearchCapture(run_id="later", captured_at=observation.available_at, expected_products=["JM"], observations=[observation], gaps={})
            service.research_archive.save(later, blob)
            before_crossing = (await client.get(f"{base}/replay", params={"at": primary.as_of.isoformat()})).json()
            assert before_crossing["invalidation_observed"] is None
            await asyncio.sleep(max(0, (observation.available_at - datetime.now(timezone.utc)).total_seconds()) + .01)
            replay = (await client.get(f"{base}/replay")).json()
            assert replay["validation"] == "passed"
            assert replay["invalidation_observed"]["price"] == 3445
            assert (await client.get(f"{base}/replay", params={"at": "2100-01-01T00:00:00Z"})).status_code == 422


def test_compare_reports_changed_conditions_without_using_observation_time():
    common = dict(decision="long_call", contract="JM2701", option_symbol="JM2701-C-3600",
                  entry_timing="conditional", trigger=3550, invalidation=3470, target=3650, evidence_gaps=[])
    before = [dict(common, product="JM"), dict(common, product="M")]
    after = [dict(common, product="JM", invalidation=3480), dict(common, product="JD")]
    assert compare_opportunities(before, after) == {"added": ["JD"], "removed": ["M"], "changed": ["JM"]}


@pytest.mark.parametrize("issue", [None, "missing", "invalid_hash", "future_published", "future_available", "future_retrieved", "unsafe_path"])
def test_supplementary_originals_and_release_times_are_frozen_atomically(tmp_path, issue):
    archive = ResearchArchive(tmp_path / "research.duckdb")
    archive.initialize()
    clock = datetime.now(timezone.utc)
    stamp = clock.isoformat()
    raw = b"official release"
    digest = sha256(raw).hexdigest()
    directory = tmp_path / "agent-sources/supplement/originals"
    directory.mkdir(parents=True)
    original = directory / (digest + ".html")
    if issue != "missing":
        original.write_bytes(raw if issue != "invalid_hash" else b"different bytes")
    detail = b"original filing metadata"
    detail_hash = sha256(detail).hexdigest()
    index = directory / (detail_hash + ".json")
    index.write_bytes(detail)
    doc = {"sha256": digest, "raw": f"originals/{digest}.html", "authority": "primary_official",
           "published_at": stamp, "available_at": stamp, "retrieved_at": stamp,
           "disclosure_index_raw": f"originals/{detail_hash}.json", "disclosure_index_sha256": detail_hash}
    if issue and issue.startswith("future_"):
        doc[issue.removeprefix("future_") + "_at"] = (clock + timedelta(seconds=1)).isoformat()
    if issue == "unsafe_path":
        doc["raw"] = "../originals/" + digest + ".html"
    capture = ResearchCapture(run_id="frozen", captured_at=clock, expected_products=["LC"], observations=[],
                              gaps={"LC": "行情未取得"}, agent_context={"cutoff": stamp, "sources": {"items": [doc]}})
    if issue:
        with pytest.raises(ValueError):
            archive.save(capture, {})
        assert archive.get("frozen") is None
        return
    archive.save(capture, {})
    original.unlink()
    index.unlink()
    assert archive.evidence("frozen", digest) == raw
    assert archive.evidence("frozen", detail_hash) == detail
    assert archive.evidence("other-run", detail_hash) is None


def test_sleeve_dedup_keeps_independent_c_from_later_b_and_c(tmp_path):
    from app.models import AgentStrategyPayload, StrategyRunRequest
    from app.opportunities import compile_opportunities

    def proposal(product, groups):
        raw = strategy_payload(decision='long_call', product=product).model_dump(mode='json')
        for plan in raw['agent_review']['plans']:
            if plan['strategy'] in groups:
                legs = [raw['option_expression']['symbol']]
                if plan['strategy']=='C':
                    legs.append(raw['analysis_contract']+'-P-3300')
                plan.update(action='paper_entry',legs=legs,max_holding_days=1,quantity=1)
        return AgentStrategyPayload.model_validate(raw)
    first, second = proposal('JM',{'A','B'}), proposal('M',{'B','C'})
    (tmp_path/'opportunities.json').write_text(json.dumps([second.model_dump(mode='json')]))
    rows = [evidence(p.product,p.analysis_contract,p.market_levels.quote_at,p.as_of)[0] for p in (first,second)]
    capture = ResearchCapture(run_id='one',captured_at=max(r.available_at for r in rows),expected_products=['JM','M'],observations=rows,gaps={})
    group = compile_opportunities(first,StrategyRunRequest(),capture,tmp_path,lambda item,_:item)
    assert group['excluded_count']==0 and [o['product'] for o in group['opportunities']]==['JM']
    late = group['sleeve_plans'][1]['agent_review']['plans']
    assert [(p['strategy'],p['action'],p['quantity']) for p in late] == [('A','wait',0),('B','wait',0),('C','paper_entry',1)]

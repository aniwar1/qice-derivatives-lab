from __future__ import annotations

from datetime import datetime
import json

from scripts import evidence_adapter, run_decision


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=run_decision.BJT)


def test_adapter_binds_artifact_bytes_without_direction(tmp_path) -> None:
    artifact = tmp_path / "facts.json"
    artifact.write_text(json.dumps({"price": 2410, "volume": 10}), encoding="utf-8")
    facts, sources = evidence_adapter._load_facts(artifact)
    packet = evidence_adapter.create_packet(
        run_id="adapter-run",
        packet_id="market-contract-1",
        domain="market_contract",
        product="MA",
        analysis_contract="MA2701",
        observed_at=NOW.isoformat(),
        valid_until=None,
        availability="complete",
        freshness="fresh",
        integrity="verified",
        identity="verified",
        executability="executable",
        facts=facts,
        source_refs=sources,
        known_gaps=[],
    )
    assert packet["facts"] == {"price": 2410, "volume": 10}
    assert packet["does_not_select_action"] is True
    assert "action" not in packet
    assert packet["source_refs"][0]["content_sha256"]


def test_unobservable_adapter_requires_no_fabricated_facts() -> None:
    packet = evidence_adapter.create_packet(
        run_id="adapter-run",
        packet_id="member-flow-1",
        domain="member_flow",
        product="MA",
        analysis_contract="MA2701",
        observed_at=NOW.isoformat(),
        valid_until=None,
        availability="unobservable",
        freshness="not_applicable",
        integrity="unverified",
        identity="unverified",
        executability="not_applicable",
        facts={},
        source_refs=[],
        known_gaps=["exchange_member_source_unavailable"],
    )
    assert packet["facts"] == {}
    assert packet["known_gaps"]

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from datetime import timedelta
import builtins
import hashlib
import json
from pathlib import Path

import pytest

from scripts import build_dashboard_snapshot, run_decision, trade_learning


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=run_decision.BJT)


def _packet(run: dict, domain: str = "market_contract") -> dict:
    observed = NOW - timedelta(minutes=1)
    return {
        "schema_version": "evidence_packet.v1",
        "packet_id": f"dashboard-{domain}",
        "run_id": run["run_id"],
        "domain": domain,
        "product": run["request"]["product"],
        "analysis_contract": run["request"]["requested_contract"],
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(hours=1)).isoformat(),
        "status": {
            "availability": "complete",
            "freshness": "fresh",
            "integrity": "verified",
            "identity": "verified",
            "executability": "executable" if domain == "market_contract" else "not_applicable",
        },
        "source_refs": [
            {
                "kind": "file",
                "locator": f"evidence/{domain}.json",
                "retrieved_at": (observed - timedelta(seconds=1)).isoformat(),
                "content_sha256": "a" * 64,
            }
        ],
        "facts": {"summary": f"verified {domain}"},
        "known_gaps": [],
        "does_not_select_action": True,
    }


def _thesis() -> dict:
    return {
        "schema_version": "underlying_thesis.v1",
        "product": "MA",
        "analysis_contract": "MA2701",
        "directional_view": "long",
        "path": "anticipatory_breakout",
        "trigger": "完成K线突破已定义压力后生效",
        "target": "到达报告内唯一现实压力目标",
        "futures_structure_invalidation": "完成K线收于结构失效位下方",
        "latest_realization_time": (NOW + timedelta(days=7)).isoformat(),
        "why_now": "驱动窗口和结构蓄势同时存在。",
        "why_not_opposite": "反向假设不能解释持仓与结构组合。",
        "why_not_wait": "等待会放弃突破前凸性，但并非自动入场。",
        "strongest_counterevidence": "需求证据仍可能转弱。",
        "falsifiers": ["库存拐点与主假设相反", "完成K线触发结构失效"],
        "selected_by_agent": True,
    }


def test_empty_dashboard_combines_architecture_and_current_universe() -> None:
    snapshot = build_dashboard_snapshot.build_snapshot(now=NOW)
    assert snapshot["schema_version"] == "skill_dashboard_snapshot.v1"
    assert snapshot["current_run"] is None
    assert snapshot["universe"]["product_count"] == 22
    assert len(snapshot["universe"]["products"]) == 22
    nodes = snapshot["architecture"]["nodes"]
    ids = {node["id"] for node in nodes}
    assert ids == set(build_dashboard_snapshot.CANONICAL_NODE_IDS)
    assert len(nodes) == 14
    assert all(set(node) == {"id", "label", "owner", "status", "summary"} for node in nodes)
    assert snapshot["boundaries"]["order_placement_authorized"] is False
    assert snapshot["boundaries"]["learning_auto_modifies_skill"] is False
    build_dashboard_snapshot.validate_snapshot(snapshot)


def test_dashboard_reflects_decision_run_node_state() -> None:
    run = run_decision.create_run(
        mode="option_entry",
        run_id="dashboard-run",
        product="MA",
        requested_contract="MA2701",
        now=NOW,
    )
    snapshot = build_dashboard_snapshot.build_snapshot(run=run, now=NOW)
    assert snapshot["current_run"]["task_mode"] == "option_entry"
    assert snapshot["current_run"]["scope"] == run["scope"]
    assert snapshot["current_run"]["quality_review"] == run["quality_review"]
    assert snapshot["current_run"]["permissions"] == run["permissions"]
    option = next(
        node for node in snapshot["architecture"]["nodes"] if node["id"] == "option_expression"
    )
    assert option["status"] == "waiting"
    assert snapshot["underlying_thesis"] is None
    assert snapshot["option_expression"] is None


def test_product_groups_cover_each_product_once() -> None:
    snapshot = build_dashboard_snapshot.build_snapshot(now=NOW)
    grouped = [
        symbol
        for group in snapshot["universe"]["primary_groups"]
        for symbol in group["products"]
    ]
    assert len(grouped) == len(set(grouped)) == snapshot["universe"]["product_count"]
    assert set(grouped) == set(snapshot["universe"]["products"])


@pytest.mark.parametrize("mode", build_dashboard_snapshot.TASK_MODES)
def test_all_eight_task_modes_are_accepted(mode: str) -> None:
    requires_product = mode in {
        "single_analysis",
        "option_entry",
        "position_review",
    }
    run = run_decision.create_run(
        mode=mode,
        run_id=f"dashboard-{mode}",
        product="MA" if requires_product else None,
        requested_contract="MA2701" if requires_product else None,
        now=NOW,
    )
    snapshot = build_dashboard_snapshot.build_snapshot(run=run, now=NOW)
    assert snapshot["current_run"]["task_mode"] == mode


def test_dashboard_preserves_complete_typed_evidence_packet() -> None:
    run = run_decision.create_run(
        mode="single_analysis",
        run_id="dashboard-evidence",
        product="MA",
        requested_contract="MA2701",
        now=NOW,
    )
    packet = _packet(run)
    run = run_decision.attach_evidence(run, packet, now=NOW)
    snapshot = build_dashboard_snapshot.build_snapshot(run=run, now=NOW)
    assert snapshot["evidence"] == [packet]
    assert snapshot["evidence"][0]["schema_version"] == "evidence_packet.v1"
    assert snapshot["evidence"][0]["run_id"] == run["run_id"]
    assert snapshot["evidence"][0]["source_refs"] == packet["source_refs"]
    assert snapshot["evidence"][0]["facts"] == packet["facts"]


def test_learning_requires_summary_schema_and_verified_hash(tmp_path: Path) -> None:
    summary = trade_learning.build_summary([])
    snapshot = build_dashboard_snapshot.build_snapshot(learning=summary, now=NOW)
    assert snapshot["learning"] == summary

    changed = deepcopy(summary)
    changed["statistics"]["wins"] = 1
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="summary_sha256 mismatch"):
        build_dashboard_snapshot.build_snapshot(learning=changed, now=NOW)

    rendered_document = deepcopy(summary)
    rendered_document["schema_version"] = "trade_learning_document.v1"
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="must be trade_learning_summary"):
        build_dashboard_snapshot.build_snapshot(learning=rendered_document, now=NOW)

    malformed = deepcopy(summary)
    del malformed["statistics"]["wins"]
    unsigned = deepcopy(malformed)
    unsigned.pop("summary_sha256")
    malformed["summary_sha256"] = hashlib.sha256(
        build_dashboard_snapshot._canonical_bytes(unsigned)
    ).hexdigest()
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="JSON schema"):
        build_dashboard_snapshot.build_snapshot(learning=malformed, now=NOW)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"trade_learning_summary.v1","schema_version":"trade_learning_summary.v1"}', encoding="utf-8")
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="duplicate JSON key"):
        build_dashboard_snapshot._learning_payload(duplicate)


def test_snapshot_contract_rejects_boundary_or_architecture_weakening() -> None:
    snapshot = build_dashboard_snapshot.build_snapshot(now=NOW)
    weakened = deepcopy(snapshot)
    weakened["boundaries"]["order_placement_authorized"] = True
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="JSON schema"):
        build_dashboard_snapshot.validate_snapshot(weakened)

    duplicate_node = deepcopy(snapshot)
    duplicate_node["architecture"]["nodes"].append(
        {
            "id": "intent_router",
            "label": "重复路由",
            "owner": "deterministic",
            "status": "ready",
            "summary": "不允许重复节点标识",
        }
    )
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="JSON schema"):
        build_dashboard_snapshot.validate_snapshot(duplicate_node)

    duplicate_edge = deepcopy(snapshot)
    duplicate_edge["architecture"]["edges"].append(
        deepcopy(duplicate_edge["architecture"]["edges"][0])
    )
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="JSON schema"):
        build_dashboard_snapshot.validate_snapshot(duplicate_edge)


def test_snapshot_contract_rejects_partial_option_decision() -> None:
    run = run_decision.create_run(
        mode="option_entry",
        run_id="dashboard-partial-option",
        product="MA",
        requested_contract="MA2701",
        now=NOW,
    )
    run["option_expression"] = {"schema_version": "option_decision.v1"}
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="JSON schema"):
        build_dashboard_snapshot.build_snapshot(run=run, now=NOW)


def test_snapshot_contract_accepts_complete_option_decision(tmp_path: Path) -> None:
    from tests.test_option_decision import base_decision

    run = run_decision.create_run(
        mode="option_entry",
        run_id="dashboard-complete-option",
        product="MA",
        requested_contract="MA2701",
        now=NOW,
    )
    run["underlying_thesis"] = _thesis()
    run["option_expression"] = base_decision(tmp_path)
    snapshot = build_dashboard_snapshot.build_snapshot(run=run, now=NOW)
    assert snapshot["option_expression"]["schema_version"] == "option_decision.v1"
    assert snapshot["option_expression"]["claim_boundary"]["order_authorized"] is False


def test_native_validation_remains_active_without_jsonschema(monkeypatch) -> None:
    snapshot = build_dashboard_snapshot.build_snapshot(now=NOW)
    real_import = builtins.__import__

    def import_without_jsonschema(name, *args, **kwargs):
        if name == "jsonschema":
            raise ModuleNotFoundError("simulated optional dependency absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_jsonschema)
    build_dashboard_snapshot.validate_snapshot(snapshot)
    snapshot["boundaries"]["machine_selected_action"] = True
    with pytest.raises(build_dashboard_snapshot.DashboardSnapshotError, match="JSON schema"):
        build_dashboard_snapshot.validate_snapshot(snapshot)


def test_declared_schema_links_typed_contracts_and_constant_boundaries() -> None:
    schema = json.loads(
        Path("schemas/skill_dashboard_snapshot.v1.schema.json").read_text(encoding="utf-8")
    )
    assert schema["properties"]["evidence"]["items"]["$ref"] == "evidence_packet.v1.schema.json"
    assert schema["properties"]["option_expression"]["oneOf"][1]["$ref"] == "option_decision.v1.schema.json"
    assert schema["properties"]["learning"]["oneOf"][1]["$ref"] == "trade_learning_summary.v1.schema.json"
    boundary_properties = schema["$defs"]["boundaries"]["properties"]
    assert all(item == {"const": False} for item in boundary_properties.values())

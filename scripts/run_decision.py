#!/usr/bin/env python3
"""Deterministic control plane for one Agent-owned futures/options decision.

The runner freezes scope, routes evidence, validates identity and state, and
packages one decision run.  It never scores products, selects a direction, or
authorizes an order.  Agent-authored theses and option expressions enter only
through explicit attach steps and remain distinguishable from machine facts.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

try:
    from . import runtime_universe
except ImportError:  # pragma: no cover - direct CLI execution
    import runtime_universe


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "decision_orchestration_policy.json"
UNIVERSE_PATH = ROOT / "config" / "trading_universe.json"
SPECS_PATH = ROOT / "config" / "product_contract_specs.json"
REPORT_POLICY_PATH = ROOT / "config" / "futures_main_report_policy.json"
BJT = ZoneInfo("Asia/Shanghai")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PACKET_FIELDS = {
    "schema_version",
    "packet_id",
    "run_id",
    "domain",
    "product",
    "analysis_contract",
    "observed_at",
    "valid_until",
    "status",
    "source_refs",
    "facts",
    "known_gaps",
    "does_not_select_action",
}
STATUS_FIELDS = {"availability", "freshness", "integrity", "identity", "executability"}
THESIS_REQUIRED_FIELDS = {
    "schema_version",
    "product",
    "analysis_contract",
    "directional_view",
    "path",
    "trigger",
    "target",
    "futures_structure_invalidation",
    "latest_realization_time",
    "why_now",
    "why_not_opposite",
    "why_not_wait",
    "strongest_counterevidence",
    "falsifiers",
    "selected_by_agent",
}
THESIS_OPTIONAL_FIELDS = {"daily_bar_basis"}
DAILY_BAR_BASIS_FIELDS = {
    "evidence_packet_id",
    "bar_state",
    "as_of",
    "decision_semantics",
}


class DecisionRunError(ValueError):
    """Raised when a decision run cannot be trusted or advanced."""


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DecisionRunError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(
            resolved.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_object
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionRunError(f"cannot read JSON {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise DecisionRunError(f"JSON root must be an object: {resolved}")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DecisionRunError(f"value is not canonical JSON: {exc}") from exc


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DecisionRunError(f"cannot hash {path}: {exc}") from exc


def _atomic_write(path: str | Path, value: dict[str, Any], *, replace: bool) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not replace:
        raise DecisionRunError(f"refusing to replace existing output: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise DecisionRunError(f"{field} must be a timezone-aware ISO timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DecisionRunError(f"{field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DecisionRunError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _now_text(now: datetime | None = None) -> str:
    value = now or datetime.now(tz=BJT)
    if value.tzinfo is None or value.utcoffset() is None:
        raise DecisionRunError("runner clock must be timezone-aware")
    return value.astimezone(BJT).isoformat(timespec="seconds")


def _require_text(value: Any, field: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionRunError(f"{field} must be non-empty text")
    if len(value) > maximum:
        raise DecisionRunError(f"{field} exceeds {maximum} characters")
    return value


def _load_policy() -> dict[str, Any]:
    policy = _read_json(POLICY_PATH)
    if policy.get("schema_version") != "decision_orchestration_policy.v1":
        raise DecisionRunError("unsupported orchestration policy")
    if (
        policy.get("decision_owner") != "agent"
        or policy.get("machine_may_select_direction") is not False
        or policy.get("majority_vote_allowed") is not False
        or policy.get("maximum_critic_revisions") != 1
        or policy.get("order_placement_authorized") is not False
        or policy.get("short_option_open_allowed") is not False
    ):
        raise DecisionRunError("orchestration policy weakens Agent or execution boundaries")
    return policy


def _node(node_id: str, owner: str, status: str, purpose: str) -> dict[str, str]:
    return {"id": node_id, "owner": owner, "status": status, "purpose": purpose}


def _initial_nodes(mode: str, option_requested: bool) -> list[dict[str, str]]:
    all_market = mode in {"all_market_scan", "option_efficiency_scan"}
    market_reasoning = mode in {"all_market_scan", "option_efficiency_scan", "single_analysis", "option_entry", "position_review"}
    option_branch = mode in {"option_entry", "position_review"} or option_requested
    learning = mode == "trade_review"
    return [
        _node("intent_router", "deterministic", "complete", "select one explicit task mode"),
        _node("scope_gate", "deterministic", "complete", "freeze universe and permissions"),
        _node("market_scan", "deterministic", "planned" if all_market else "skipped", "scan the complete universe once"),
        _node("contract_binding", "deterministic", "planned" if market_reasoning else "skipped", "bind actual main and analysis month"),
        _node("evidence_fanout", "specialists", "planned" if market_reasoning or learning else "skipped", "collect typed evidence packets"),
        _node("evidence_validation", "deterministic", "waiting" if market_reasoning or learning else "skipped", "validate evidence facts and provenance"),
        _node("agent_hypothesis", "agent", "waiting" if market_reasoning else "skipped", "select one falsifiable underlying thesis"),
        _node("entry_path_comparison", "agent", "waiting" if option_branch else "skipped", "compare only the currently plausible entry paths and wait"),
        _node("option_expression", "agent", "waiting" if option_branch else "skipped", "compare plausible long-option expressions"),
        _node("bounded_critic", "agent", "waiting" if market_reasoning else "skipped", "challenge contradictions once without voting"),
        _node("final_validation", "deterministic", "waiting" if market_reasoning else "skipped", "check final facts and coherence"),
        _node("position_lifecycle", "state_machine", "optional", "track one entry, one full exit and reassessment states"),
        _node("learning_review", "agent", "planned" if learning else "optional", "derive candidate lessons from verified outcomes"),
        _node("unified_dashboard", "renderer", "waiting", "combine live decision, architecture and learning state"),
    ]


def _set_node_status(run: dict[str, Any], node_id: str, status: str) -> None:
    for node in run["nodes"]:
        if node.get("id") == node_id:
            node["status"] = status
            return
    raise DecisionRunError(f"decision run has no node {node_id}")


def create_run(
    *,
    mode: str,
    run_id: str,
    product: str | None = None,
    requested_contract: str | None = None,
    option_analysis_requested: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy = _load_policy()
    modes = policy["task_modes"]
    if mode not in modes:
        raise DecisionRunError(f"unsupported task mode: {mode}")
    if ID_RE.fullmatch(run_id) is None:
        raise DecisionRunError("run_id must be a compact stable identifier")
    normalized_product = product.strip().upper() if isinstance(product, str) else None
    if modes[mode]["requires_product"] and not normalized_product:
        raise DecisionRunError(f"task mode {mode} requires --product")
    if normalized_product:
        runtime_universe.require_allowed_product(normalized_product)
    normalized_contract = None
    if requested_contract:
        normalized_contract = runtime_universe.canonical_delivery_contract(requested_contract)
        if normalized_product and runtime_universe.product_prefix(normalized_contract) != normalized_product:
            raise DecisionRunError("requested contract conflicts with selected product")
        normalized_product = normalized_product or runtime_universe.product_prefix(normalized_contract)
    if mode in {"option_entry", "position_review"}:
        option_analysis_requested = True
    timestamp = _now_text(now)
    return {
        "schema_version": "decision_run.v1",
        "run_id": run_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "task_mode": mode,
        "request": {
            "product": normalized_product,
            "requested_contract": normalized_contract,
            "option_analysis_requested": bool(option_analysis_requested),
        },
        "scope": {
            "product_count": runtime_universe.configured_product_count(),
            "universe_sha256": _file_sha256(UNIVERSE_PATH),
            "product_specs_sha256": _file_sha256(SPECS_PATH),
            "report_policy_sha256": _file_sha256(REPORT_POLICY_PATH),
            "orchestration_policy_sha256": _file_sha256(POLICY_PATH),
        },
        "phase": "planned",
        "lifecycle_state": None,
        "nodes": _initial_nodes(mode, option_analysis_requested),
        "evidence_packets": [],
        "underlying_thesis": None,
        "option_expression": None,
        "quality_review": {"critic_revisions": 0, "unresolved_material_issue": None},
        "permissions": {
            "order_placement_authorized": False,
            "short_option_open_allowed": False,
        },
    }


def validate_evidence_packet(
    packet: dict[str, Any], run: dict[str, Any], *, now: datetime | None = None
) -> None:
    if set(packet) != PACKET_FIELDS:
        raise DecisionRunError(
            f"evidence packet fields mismatch; missing={sorted(PACKET_FIELDS - set(packet))}, "
            f"extra={sorted(set(packet) - PACKET_FIELDS)}"
        )
    if packet["schema_version"] != "evidence_packet.v1":
        raise DecisionRunError("unsupported evidence packet version")
    if ID_RE.fullmatch(str(packet["packet_id"])) is None:
        raise DecisionRunError("packet_id is invalid")
    if packet["run_id"] != run["run_id"]:
        raise DecisionRunError("evidence packet belongs to another decision run")
    policy = _load_policy()
    if packet["domain"] not in policy["evidence_domains"]:
        raise DecisionRunError("unknown evidence domain")
    product = packet["product"]
    if product is not None:
        if not isinstance(product, str):
            raise DecisionRunError("evidence product must be text or null")
        normalized_product = runtime_universe.require_allowed_product(product)
        requested_product = run["request"]["product"]
        if requested_product and normalized_product != requested_product:
            raise DecisionRunError("evidence product conflicts with decision run")
    contract = packet["analysis_contract"]
    if contract is not None:
        canonical = runtime_universe.canonical_delivery_contract(contract)
        if product and runtime_universe.product_prefix(canonical) != str(product).upper():
            raise DecisionRunError("evidence contract conflicts with evidence product")
        requested_contract = run["request"]["requested_contract"]
        if requested_contract and canonical != requested_contract:
            raise DecisionRunError("evidence contract conflicts with requested contract")
    observed = _parse_time(packet["observed_at"], "evidence.observed_at")
    current = (now or datetime.now(tz=BJT)).astimezone(timezone.utc)
    if observed > current:
        raise DecisionRunError("future-dated evidence is not allowed")
    valid_until_value = packet["valid_until"]
    if valid_until_value is not None and _parse_time(valid_until_value, "evidence.valid_until") < observed:
        raise DecisionRunError("evidence valid_until precedes observed_at")
    status = packet["status"]
    if not isinstance(status, dict) or set(status) != STATUS_FIELDS:
        raise DecisionRunError("evidence status must contain exactly five axes")
    for axis, allowed in policy["evidence_status_axes"].items():
        if status.get(axis) not in allowed:
            raise DecisionRunError(f"invalid evidence status {axis}={status.get(axis)!r}")
    if packet["does_not_select_action"] is not True:
        raise DecisionRunError("evidence packet must not select a trading action")
    if not isinstance(packet["facts"], dict):
        raise DecisionRunError("evidence facts must be an object")
    gaps = packet["known_gaps"]
    if not isinstance(gaps, list) or any(not isinstance(item, str) or not item for item in gaps):
        raise DecisionRunError("known_gaps must be a list of non-empty strings")
    sources = packet["source_refs"]
    if not isinstance(sources, list):
        raise DecisionRunError("source_refs must be a list")
    if status["availability"] == "complete" and not sources:
        raise DecisionRunError("complete evidence requires at least one source reference")
    for index, source in enumerate(sources):
        if not isinstance(source, dict) or set(source) != {
            "kind", "locator", "retrieved_at", "content_sha256"
        }:
            raise DecisionRunError(f"source_refs[{index}] has invalid fields")
        if source["kind"] not in {"url", "file"}:
            raise DecisionRunError(f"source_refs[{index}].kind is invalid")
        _require_text(source["locator"], f"source_refs[{index}].locator")
        retrieved = _parse_time(source["retrieved_at"], f"source_refs[{index}].retrieved_at")
        if retrieved > observed:
            raise DecisionRunError("source retrieval time is after packet observation time")
        if not isinstance(source["content_sha256"], str) or HEX64_RE.fullmatch(source["content_sha256"]) is None:
            raise DecisionRunError(f"source_refs[{index}].content_sha256 is invalid")
    session_bar = packet["facts"].get("session_daily_bar")
    if session_bar is not None:
        if packet["domain"] != "technical_structure":
            raise DecisionRunError(
                "session_daily_bar is allowed only in technical_structure evidence"
            )
        try:
            try:
                from .session_aware_bar import (
                    SessionAwareBarError,
                    validate_session_aware_daily_bar,
                )
            except ImportError:  # pragma: no cover - direct CLI execution
                from session_aware_bar import (
                    SessionAwareBarError,
                    validate_session_aware_daily_bar,
                )
            bar_validation = validate_session_aware_daily_bar(
                session_bar,
                expected_product=product,
                expected_contract=contract,
                packet_observed_at=packet["observed_at"],
                now=now,
            )
        except (SessionAwareBarError, ValueError) as exc:
            raise DecisionRunError(f"invalid session-aware daily bar: {exc}") from exc
        if session_bar["source"] not in sources:
            raise DecisionRunError(
                "session-aware daily-bar source must be included in packet source_refs"
            )
        if bar_validation["bar_state"] == "session_partial":
            if status["availability"] != "partial":
                raise DecisionRunError(
                    "session_partial daily bar must remain partial evidence"
                )
            if "session_partial_daily_bar_not_completed" not in gaps:
                raise DecisionRunError(
                    "session_partial daily bar must disclose its incomplete-bar gap"
                )
    if packet["domain"] == "option_chain" and status["executability"] == "executable":
        if status["freshness"] != "fresh" or status["availability"] != "complete":
            raise DecisionRunError("executable option evidence must be fresh and complete")
        if status["identity"] != "verified":
            raise DecisionRunError("executable option evidence requires verified identity")


def attach_evidence(
    run: dict[str, Any], packet: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    result = deepcopy(run)
    _validate_run_shape(result, verify_current_scope=True, now=now)
    validate_evidence_packet(packet, result, now=now)
    if any(item.get("packet_id") == packet["packet_id"] for item in result["evidence_packets"]):
        raise DecisionRunError(f"duplicate evidence packet id: {packet['packet_id']}")
    result["evidence_packets"].append(packet)
    result["updated_at"] = _now_text(now)
    result["phase"] = "collecting_evidence"
    _set_node_status(result, "evidence_fanout", "in_progress")
    required = set(_load_policy()["task_modes"][result["task_mode"]]["required_evidence_domains"])
    observed = {item["domain"] for item in result["evidence_packets"]}
    if required <= observed:
        result["phase"] = "ready_for_agent"
        _set_node_status(result, "evidence_fanout", "complete")
        _set_node_status(result, "evidence_validation", "complete")
        if result["task_mode"] in {"all_market_scan", "option_efficiency_scan"}:
            _set_node_status(result, "market_scan", "complete")
        if result["task_mode"] in {"single_analysis", "option_entry", "position_review"}:
            _set_node_status(result, "contract_binding", "complete")
    return result


def validate_underlying_thesis(thesis: dict[str, Any], run: dict[str, Any]) -> None:
    fields = set(thesis)
    if not THESIS_REQUIRED_FIELDS <= fields or not fields <= (
        THESIS_REQUIRED_FIELDS | THESIS_OPTIONAL_FIELDS
    ):
        raise DecisionRunError(
            "underlying thesis fields mismatch; "
            f"missing={sorted(THESIS_REQUIRED_FIELDS - fields)}, "
            f"extra={sorted(fields - THESIS_REQUIRED_FIELDS - THESIS_OPTIONAL_FIELDS)}"
        )
    if thesis["schema_version"] != "underlying_thesis.v1":
        raise DecisionRunError("unsupported underlying thesis version")
    product = runtime_universe.require_allowed_product(thesis["product"])
    requested_product = run["request"]["product"]
    if requested_product and product != requested_product:
        raise DecisionRunError("underlying thesis product conflicts with run")
    contract = runtime_universe.canonical_delivery_contract(thesis["analysis_contract"])
    if runtime_universe.product_prefix(contract) != product:
        raise DecisionRunError("underlying thesis contract conflicts with product")
    requested_contract = run["request"]["requested_contract"]
    if requested_contract and contract != requested_contract:
        raise DecisionRunError("underlying thesis contract conflicts with run")
    if thesis["directional_view"] not in {"long", "short", "neutral", "uncertain"}:
        raise DecisionRunError("invalid directional_view")
    for field in (
        "path", "trigger", "target", "futures_structure_invalidation", "why_now",
        "why_not_opposite", "why_not_wait", "strongest_counterevidence",
    ):
        _require_text(thesis[field], f"underlying_thesis.{field}")
    _parse_time(thesis["latest_realization_time"], "underlying_thesis.latest_realization_time")
    falsifiers = thesis["falsifiers"]
    if not isinstance(falsifiers, list) or not falsifiers or any(
        not isinstance(item, str) or not item for item in falsifiers
    ):
        raise DecisionRunError("underlying_thesis.falsifiers must be non-empty")
    if thesis["selected_by_agent"] is not True:
        raise DecisionRunError("underlying thesis must be explicitly selected by the Agent")

    bars_by_packet: dict[str, dict[str, Any]] = {}
    for packet in run["evidence_packets"]:
        if packet.get("domain") != "technical_structure":
            continue
        facts = packet.get("facts")
        bar = facts.get("session_daily_bar") if isinstance(facts, dict) else None
        if isinstance(bar, dict):
            bars_by_packet[str(packet["packet_id"])] = bar
    partial_packet_ids = {
        packet_id
        for packet_id, bar in bars_by_packet.items()
        if bar.get("bar_state") == "session_partial"
    }
    basis = thesis.get("daily_bar_basis")
    if partial_packet_ids and basis is None:
        raise DecisionRunError(
            "a thesis using current technical evidence must disclose daily_bar_basis for the session_partial bar"
        )
    if basis is not None:
        if not isinstance(basis, dict) or set(basis) != DAILY_BAR_BASIS_FIELDS:
            raise DecisionRunError("underlying_thesis.daily_bar_basis has an invalid shape")
        packet_id = _require_text(
            basis.get("evidence_packet_id"),
            "underlying_thesis.daily_bar_basis.evidence_packet_id",
            maximum=128,
        )
        bar = bars_by_packet.get(packet_id)
        if bar is None:
            raise DecisionRunError(
                "underlying_thesis.daily_bar_basis must reference technical evidence in this run"
            )
        expected_basis = {
            "evidence_packet_id": packet_id,
            "bar_state": bar["bar_state"],
            "as_of": bar["as_of"],
            "decision_semantics": bar["decision_semantics"],
        }
        if basis != expected_basis:
            raise DecisionRunError(
                "underlying_thesis.daily_bar_basis must exactly match its session-aware bar"
            )
        if bar["bar_state"] == "session_partial" and bar["decision_semantics"] not in {
            "anticipatory",
            "conditional",
        }:
            raise DecisionRunError(
                "session_partial evidence cannot support a confirmed daily breakout"
            )
    confirmed_path = str(thesis["path"]).strip().lower() == "confirmed_breakout"
    confirmed_semantics = (
        isinstance(basis, dict)
        and basis.get("decision_semantics") == "confirmed_daily_breakout"
    )
    if confirmed_path or confirmed_semantics:
        if not isinstance(basis, dict):
            raise DecisionRunError(
                "confirmed daily breakout requires an explicit completed daily-bar basis"
            )
        referenced = bars_by_packet.get(str(basis["evidence_packet_id"]))
        if (
            not isinstance(referenced, dict)
            or referenced.get("bar_state") != "completed"
            or referenced.get("claim_boundary", {}).get(
                "confirmed_daily_breakout_claim_eligible"
            )
            is not True
        ):
            raise DecisionRunError(
                "confirmed daily breakout requires a completed daily bar"
            )


def attach_thesis(run: dict[str, Any], thesis: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    result = deepcopy(run)
    _validate_run_shape(result, verify_current_scope=True, now=now)
    required = set(_load_policy()["task_modes"][result["task_mode"]]["required_evidence_domains"])
    observed = {item["domain"] for item in result["evidence_packets"]}
    if not required <= observed:
        raise DecisionRunError(f"missing required evidence domains: {sorted(required - observed)}")
    validate_underlying_thesis(thesis, result)
    replacing_after_critic = result["underlying_thesis"] is not None
    if replacing_after_critic and not (
        result["quality_review"]["critic_revisions"] == 1
        and result["quality_review"]["unresolved_material_issue"] is not None
    ):
        raise DecisionRunError("thesis replacement is allowed only for the one bounded critic revision")
    result["underlying_thesis"] = thesis
    result["phase"] = "agent_decided"
    result["updated_at"] = _now_text(now)
    _set_node_status(result, "agent_hypothesis", "complete")
    path_node = next(item for item in result["nodes"] if item["id"] == "entry_path_comparison")
    if path_node["status"] != "skipped":
        _set_node_status(result, "entry_path_comparison", "ready")
    if replacing_after_critic:
        result["quality_review"]["unresolved_material_issue"] = None
        _set_node_status(result, "bounded_critic", "complete")
    else:
        _set_node_status(result, "bounded_critic", "ready")
    return result


def attach_option_decision(
    run: dict[str, Any],
    option_decision: dict[str, Any],
    *,
    artifact_root: str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Attach a separately validated option expression without selecting it."""

    result = deepcopy(run)
    _validate_run_shape(result, verify_current_scope=True, now=now)
    if result["underlying_thesis"] is None:
        raise DecisionRunError("option expression requires an Agent-authored underlying thesis")
    try:
        try:
            from .validate_option_decision import (
                OptionDecisionValidationError,
                validate_option_decision,
            )
        except ImportError:  # pragma: no cover - direct CLI execution
            from validate_option_decision import (
                OptionDecisionValidationError,
                validate_option_decision,
            )
        validation = validate_option_decision(
            option_decision, artifact_root=Path(artifact_root).expanduser().resolve()
        )
    except OptionDecisionValidationError as exc:
        raise DecisionRunError(f"invalid option decision: {exc}") from exc
    if validation.get("valid") is not True:
        raise DecisionRunError("option decision validator did not return valid=true")
    if (
        validation.get("validator_selected_direction") is not False
        or validation.get("validation_is_trade_permission") is not False
        or validation.get("order_authorized") is not False
    ):
        raise DecisionRunError("option validator weakened the decision or order boundary")
    thesis = result["underlying_thesis"]
    if validation.get("analysis_contract") != thesis["analysis_contract"]:
        raise DecisionRunError("option decision analysis contract conflicts with decision run thesis")
    view = thesis["directional_view"]
    route = validation.get("option_route")
    if route == "long_call" and view != "long":
        raise DecisionRunError("long_call conflicts with decision run directional_view")
    if route == "long_put" and view != "short":
        raise DecisionRunError("long_put conflicts with decision run directional_view")
    result["option_expression"] = option_decision
    result["updated_at"] = _now_text(now)
    _set_node_status(result, "option_expression", "complete")
    _set_node_status(result, "entry_path_comparison", "complete")
    return result


def record_critic(
    run: dict[str, Any], *, unresolved_material_issue: str | None, now: datetime | None = None
) -> dict[str, Any]:
    result = deepcopy(run)
    _validate_run_shape(result, verify_current_scope=True, now=now)
    if result["underlying_thesis"] is None:
        raise DecisionRunError("critic review requires an Agent-authored thesis")
    if result["quality_review"]["critic_revisions"] >= _load_policy()["maximum_critic_revisions"]:
        raise DecisionRunError("bounded critic revision limit reached")
    result["quality_review"] = {
        "critic_revisions": result["quality_review"]["critic_revisions"] + 1,
        "unresolved_material_issue": (
            _require_text(unresolved_material_issue, "unresolved_material_issue")
            if unresolved_material_issue is not None
            else None
        ),
    }
    result["phase"] = "blocked" if unresolved_material_issue else "quality_review"
    result["updated_at"] = _now_text(now)
    _set_node_status(result, "bounded_critic", "blocked" if unresolved_material_issue else "complete")
    return result


def advance_lifecycle(
    run: dict[str, Any], *, state: str, now: datetime | None = None
) -> dict[str, Any]:
    """Advance the observable plan/position lifecycle without placing an order."""

    result = deepcopy(run)
    _validate_run_shape(result, verify_current_scope=True, now=now)
    policy = _load_policy()
    current = result["lifecycle_state"] or "unstarted"
    allowed = policy["lifecycle_transitions"].get(current)
    if not isinstance(allowed, list) or state not in allowed:
        raise DecisionRunError(f"invalid lifecycle transition: {current} -> {state}")
    domains = {packet["domain"] for packet in result["evidence_packets"]}
    if state == "entered" and "account_position" not in domains:
        raise DecisionRunError("entered requires a user-confirmed account_position evidence packet")
    if state == "closed" and "realized_trade" not in domains:
        raise DecisionRunError("closed requires a verified realized_trade evidence packet")
    result["lifecycle_state"] = state
    result["updated_at"] = _now_text(now)
    _set_node_status(result, "position_lifecycle", "complete" if state == "reviewed" else "active")
    return result


def _validate_run_shape(
    run: dict[str, Any], *, verify_current_scope: bool, now: datetime | None = None
) -> None:
    required = {
        "schema_version", "run_id", "created_at", "updated_at", "task_mode", "request",
        "scope", "phase", "lifecycle_state", "nodes", "evidence_packets",
        "underlying_thesis", "option_expression", "quality_review", "permissions",
    }
    if set(run) != required or run.get("schema_version") != "decision_run.v1":
        raise DecisionRunError("decision run shape or version is invalid")
    if ID_RE.fullmatch(str(run["run_id"])) is None:
        raise DecisionRunError("decision run id is invalid")
    created = _parse_time(run["created_at"], "run.created_at")
    updated = _parse_time(run["updated_at"], "run.updated_at")
    if updated < created:
        raise DecisionRunError("run.updated_at precedes run.created_at")
    current = (now or datetime.now(tz=BJT)).astimezone(timezone.utc)
    if updated > current:
        raise DecisionRunError("decision run is future-dated")
    policy = _load_policy()
    if run["task_mode"] not in policy["task_modes"]:
        raise DecisionRunError("decision run task mode is invalid")
    if run["phase"] not in {
        "planned", "collecting_evidence", "ready_for_agent", "agent_decided",
        "quality_review", "finalized", "blocked",
    }:
        raise DecisionRunError("decision run phase is invalid")
    permissions = run["permissions"]
    if permissions != {"order_placement_authorized": False, "short_option_open_allowed": False}:
        raise DecisionRunError("decision run weakens execution permissions")
    quality = run["quality_review"]
    if not isinstance(quality, dict) or set(quality) != {"critic_revisions", "unresolved_material_issue"}:
        raise DecisionRunError("decision run quality_review is invalid")
    revisions = quality["critic_revisions"]
    if isinstance(revisions, bool) or not isinstance(revisions, int) or not 0 <= revisions <= 1:
        raise DecisionRunError("critic revision count is invalid")
    if quality["unresolved_material_issue"] is not None:
        _require_text(quality["unresolved_material_issue"], "unresolved_material_issue")
    if run["lifecycle_state"] is not None and run["lifecycle_state"] not in policy["lifecycle_states"]:
        raise DecisionRunError("decision run lifecycle state is invalid")
    if not isinstance(run["nodes"], list) or len({node.get("id") for node in run["nodes"] if isinstance(node, dict)}) != len(run["nodes"]):
        raise DecisionRunError("decision run nodes must have unique ids")
    expected_nodes = {
        item["id"]: item
        for item in _initial_nodes(
            run["task_mode"], bool(run["request"].get("option_analysis_requested"))
        )
    }
    actual_nodes = {item.get("id"): item for item in run["nodes"] if isinstance(item, dict)}
    if set(actual_nodes) != set(expected_nodes):
        raise DecisionRunError("decision run nodes do not match the canonical architecture")
    allowed_node_statuses = {
        "active", "blocked", "complete", "in_progress", "optional", "planned",
        "ready", "skipped", "waiting",
    }
    for node_id, node in actual_nodes.items():
        expected_node = expected_nodes[node_id]
        if set(node) != {"id", "owner", "status", "purpose"}:
            raise DecisionRunError(f"decision run node {node_id} has an invalid shape")
        if node["owner"] != expected_node["owner"] or node["purpose"] != expected_node["purpose"]:
            raise DecisionRunError(f"decision run node {node_id} changed its owner or purpose")
        if node["status"] not in allowed_node_statuses:
            raise DecisionRunError(f"decision run node {node_id} has an invalid status")
    if not isinstance(run["evidence_packets"], list):
        raise DecisionRunError("decision run evidence_packets must be a list")
    packet_ids: set[str] = set()
    for packet in run["evidence_packets"]:
        if not isinstance(packet, dict):
            raise DecisionRunError("decision run evidence packet must be an object")
        validate_evidence_packet(packet, run, now=now)
        if packet["packet_id"] in packet_ids:
            raise DecisionRunError("duplicate evidence packet id")
        packet_ids.add(packet["packet_id"])
    if run["underlying_thesis"] is not None:
        if not isinstance(run["underlying_thesis"], dict):
            raise DecisionRunError("underlying thesis must be an object or null")
        validate_underlying_thesis(run["underlying_thesis"], run)
    if verify_current_scope:
        expected = {
            "product_count": runtime_universe.configured_product_count(),
            "universe_sha256": _file_sha256(UNIVERSE_PATH),
            "product_specs_sha256": _file_sha256(SPECS_PATH),
            "report_policy_sha256": _file_sha256(REPORT_POLICY_PATH),
            "orchestration_policy_sha256": _file_sha256(POLICY_PATH),
        }
        if run["scope"] != expected:
            raise DecisionRunError("decision run scope no longer matches current configuration")


def validate_run(run: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    try:
        _validate_run_shape(run, verify_current_scope=True, now=now)
    except DecisionRunError as exc:
        return {
            "schema_version": "decision_run_validation.v1",
            "valid": False,
            "machine_selected_action": False,
            "order_authorized": False,
            "errors": [str(exc)],
        }
    mode_policy = _load_policy()["task_modes"][run["task_mode"]]
    required = set(mode_policy["required_evidence_domains"])
    observed = {item["domain"] for item in run["evidence_packets"]}
    return {
        "schema_version": "decision_run_validation.v1",
        "valid": True,
        "phase": run["phase"],
        "required_evidence_domains": sorted(required),
        "missing_required_evidence_domains": sorted(required - observed),
        "ready_for_agent": required <= observed,
        "machine_selected_action": False,
        "order_authorized": False,
        "errors": [],
    }


def finalize_run(run: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    result = deepcopy(run)
    validation = validate_run(result, now=now)
    if not validation["valid"]:
        raise DecisionRunError(validation["errors"][0])
    if validation["missing_required_evidence_domains"]:
        raise DecisionRunError("cannot finalize before required evidence domains are represented")
    if result["task_mode"] in {"all_market_scan", "option_efficiency_scan", "single_analysis", "option_entry", "position_review"} and result["underlying_thesis"] is None:
        raise DecisionRunError("market decision modes require an Agent-authored underlying thesis")
    if result["task_mode"] in {"option_entry", "position_review"} and result["option_expression"] is None:
        raise DecisionRunError("option decision modes require an independently validated option expression")
    if result["task_mode"] in {"all_market_scan", "option_efficiency_scan", "single_analysis", "option_entry", "position_review"} and result["quality_review"]["critic_revisions"] != 1:
        raise DecisionRunError("market decision modes require exactly one bounded critic review")
    if result["quality_review"]["unresolved_material_issue"] is not None:
        raise DecisionRunError("unresolved material critic issue requires an explicit wait revision")
    result["phase"] = "finalized"
    result["updated_at"] = _now_text(now)
    _set_node_status(result, "final_validation", "complete")
    _set_node_status(result, "unified_dashboard", "ready")
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route and validate an Agent-owned futures/options decision without selecting trades."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="freeze task mode, scope and permissions")
    init_parser.add_argument("--mode", required=True)
    init_parser.add_argument("--run-id", required=True)
    init_parser.add_argument("--product")
    init_parser.add_argument("--requested-contract")
    init_parser.add_argument("--option-analysis", action="store_true")
    init_parser.add_argument("--output", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a complete decision run")
    validate_parser.add_argument("run")

    for command, item_label in (
        ("attach-evidence", "packet"),
        ("attach-thesis", "thesis"),
        ("attach-option", "option"),
    ):
        item = subparsers.add_parser(command)
        item.add_argument("run")
        item.add_argument(f"--{item_label}", required=True)
        item.add_argument("--output", required=True)
        item.add_argument("--replace", action="store_true")

    critic = subparsers.add_parser("critic", help="record one bounded contradiction review")
    critic.add_argument("run")
    critic.add_argument("--unresolved-material-issue")
    critic.add_argument("--output", required=True)
    critic.add_argument("--replace", action="store_true")

    lifecycle = subparsers.add_parser(
        "advance-lifecycle", help="record an observed plan or position state transition"
    )
    lifecycle.add_argument("run")
    lifecycle.add_argument("--state", required=True)
    lifecycle.add_argument("--output", required=True)
    lifecycle.add_argument("--replace", action="store_true")

    finalize = subparsers.add_parser("finalize", help="close the deterministic control-plane run")
    finalize.add_argument("run")
    finalize.add_argument("--output", required=True)
    finalize.add_argument("--replace", action="store_true")
    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            value = create_run(
                mode=args.mode,
                run_id=args.run_id,
                product=args.product,
                requested_contract=args.requested_contract,
                option_analysis_requested=args.option_analysis,
            )
            _atomic_write(args.output, value, replace=False)
        elif args.command == "validate":
            value = validate_run(_read_json(args.run))
            _print_json(value)
            return 0 if value["valid"] else 1
        elif args.command == "attach-evidence":
            value = attach_evidence(_read_json(args.run), _read_json(args.packet))
            _atomic_write(args.output, value, replace=args.replace)
        elif args.command == "attach-thesis":
            value = attach_thesis(_read_json(args.run), _read_json(args.thesis))
            _atomic_write(args.output, value, replace=args.replace)
        elif args.command == "attach-option":
            option_path = Path(args.option).expanduser().resolve()
            value = attach_option_decision(
                _read_json(args.run),
                _read_json(option_path),
                artifact_root=option_path.parent,
            )
            _atomic_write(args.output, value, replace=args.replace)
        elif args.command == "critic":
            value = record_critic(
                _read_json(args.run),
                unresolved_material_issue=args.unresolved_material_issue,
            )
            _atomic_write(args.output, value, replace=args.replace)
        elif args.command == "advance-lifecycle":
            value = advance_lifecycle(_read_json(args.run), state=args.state)
            _atomic_write(args.output, value, replace=args.replace)
        else:
            value = finalize_run(_read_json(args.run))
            _atomic_write(args.output, value, replace=args.replace)
        _print_json(value)
        return 0
    except DecisionRunError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

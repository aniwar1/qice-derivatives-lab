#!/usr/bin/env python3
"""Build one read-only data snapshot for the unified Skill application dashboard.

No network request is made and no trading action is inferred.  Optional run and
learning inputs must already be validated artifacts; invalid inputs fail closed.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urldefrag, urljoin

try:
    from . import run_decision, runtime_universe, session_aware_bar
except ImportError:  # pragma: no cover - direct CLI execution
    import run_decision
    import runtime_universe
    import session_aware_bar


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"
UNIVERSE_PATH = ROOT / "config" / "trading_universe.json"
GROUPS_PATH = ROOT / "config" / "product_groups.json"
ORCHESTRATION_POLICY_PATH = ROOT / "config" / "decision_orchestration_policy.json"
SCHEMA_PATH = ROOT / "schemas" / "skill_dashboard_snapshot.v1.schema.json"
EVIDENCE_SCHEMA_PATH = ROOT / "schemas" / "evidence_packet.v1.schema.json"
SESSION_BAR_SCHEMA_PATH = ROOT / "schemas" / "session_aware_daily_bar.v1.schema.json"
OPTION_SCHEMA_PATH = ROOT / "schemas" / "option_decision.v1.schema.json"
LEARNING_SCHEMA_PATH = ROOT / "schemas" / "trade_learning_summary.v1.schema.json"
TASK_MODES = (
    "education",
    "universe_maintenance",
    "all_market_scan",
    "option_efficiency_scan",
    "single_analysis",
    "option_entry",
    "position_review",
    "trade_review",
)
CANONICAL_NODE_IDS = (
    "intent_router",
    "scope_gate",
    "market_scan",
    "contract_binding",
    "evidence_fanout",
    "evidence_validation",
    "agent_hypothesis",
    "entry_path_comparison",
    "option_expression",
    "bounded_critic",
    "final_validation",
    "position_lifecycle",
    "unified_dashboard",
    "learning_review",
)
BOUNDARIES = {
    "machine_selected_action": False,
    "validation_is_trade_permission": False,
    "order_placement_authorized": False,
    "short_option_open_allowed": False,
    "learning_auto_modifies_skill": False,
    "hidden_reasoning_persisted": False,
}


class DashboardSnapshotError(ValueError):
    """Raised when the dashboard would otherwise display untrusted state."""


class _SchemaViolation(ValueError):
    """Internal error raised by the dependency-free JSON-Schema subset."""


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DashboardSnapshotError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(
            resolved.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_object
        )
    except (OSError, json.JSONDecodeError, DashboardSnapshotError) as exc:
        raise DashboardSnapshotError(f"cannot read JSON {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise DashboardSnapshotError(f"JSON root must be an object: {resolved}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        raise DashboardSnapshotError(f"value is not canonical JSON: {exc}") from exc


def _json_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical_bytes(left) == _canonical_bytes(right)
    except DashboardSnapshotError:
        return False


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise _SchemaViolation(f"unsupported schema type {expected!r}")


def _pointer_value(document: Any, fragment: str) -> Any:
    if fragment in {"", "/"}:
        return document
    if not fragment.startswith("/"):
        raise _SchemaViolation(f"unsupported JSON pointer fragment #{fragment}")
    value = document
    for raw in fragment[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        else:
            raise _SchemaViolation(f"unresolved JSON pointer token {token!r}")
    return value


def _schema_registry() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    root_schema = _read_json(SCHEMA_PATH)
    schemas = [
        root_schema,
        _read_json(EVIDENCE_SCHEMA_PATH),
        _read_json(SESSION_BAR_SCHEMA_PATH),
        _read_json(OPTION_SCHEMA_PATH),
        _read_json(LEARNING_SCHEMA_PATH),
    ]
    registry: dict[str, dict[str, Any]] = {}
    root_base = str(root_schema["$id"])
    for path, schema in zip(
        (
            SCHEMA_PATH,
            EVIDENCE_SCHEMA_PATH,
            SESSION_BAR_SCHEMA_PATH,
            OPTION_SCHEMA_PATH,
            LEARNING_SCHEMA_PATH,
        ),
        schemas,
        strict=True,
    ):
        file_uri = path.resolve().as_uri()
        schema_id = schema.get("$id")
        registry[file_uri] = schema
        registry[path.name] = schema
        registry[urljoin(root_base, path.name)] = schema
        if isinstance(schema_id, str):
            registry[urljoin(file_uri, schema_id)] = schema
            registry[urljoin(root_base, schema_id)] = schema
    return root_schema, registry


def _check_format(value: str, format_name: str, path: str) -> None:
    try:
        if format_name == "date-time":
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
        elif format_name == "date":
            date.fromisoformat(value)
    except ValueError as exc:
        raise _SchemaViolation(f"{path} is not a valid {format_name}") from exc


def _validate_schema_value(
    value: Any,
    schema: Any,
    *,
    document: dict[str, Any],
    base_uri: str,
    registry: dict[str, dict[str, Any]],
    path: str,
) -> None:
    """Validate the subset of Draft 2020-12 used by the local contracts."""

    if schema is True:
        return
    if schema is False:
        raise _SchemaViolation(f"{path} is forbidden by schema")
    if not isinstance(schema, dict):
        raise _SchemaViolation(f"{path} references a malformed schema")

    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str):
            raise _SchemaViolation(f"{path} has a non-string $ref")
        target_uri, fragment = urldefrag(urljoin(base_uri, reference))
        target_document = document if not target_uri or target_uri == base_uri else registry.get(target_uri)
        if target_document is None:
            raise _SchemaViolation(f"{path} has unresolved local $ref {reference!r}")
        target_base = str(target_document.get("$id") or target_uri or base_uri)
        _validate_schema_value(
            value,
            _pointer_value(target_document, fragment),
            document=target_document,
            base_uri=target_base,
            registry=registry,
            path=path,
        )

    if "allOf" in schema:
        for index, branch in enumerate(schema["allOf"]):
            _validate_schema_value(
                value,
                branch,
                document=document,
                base_uri=base_uri,
                registry=registry,
                path=f"{path}.allOf[{index}]",
            )
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _validate_schema_value(
                    value,
                    branch,
                    document=document,
                    base_uri=base_uri,
                    registry=registry,
                    path=path,
                )
            except _SchemaViolation:
                continue
            matches += 1
        if matches != 1:
            raise _SchemaViolation(f"{path} must match exactly one schema branch")

    if "const" in schema and not _json_equal(value, schema["const"]):
        raise _SchemaViolation(f"{path} must equal the schema constant")
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        raise _SchemaViolation(f"{path} is outside the allowed enum")

    expected_type = schema.get("type")
    if expected_type is not None:
        types = [expected_type] if isinstance(expected_type, str) else expected_type
        if not isinstance(types, list) or not all(isinstance(item, str) for item in types):
            raise _SchemaViolation(f"{path} has a malformed type declaration")
        if not any(_schema_type_matches(value, item) for item in types):
            raise _SchemaViolation(f"{path} has the wrong JSON type")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise _SchemaViolation(f"{path}.required is malformed")
        missing = [key for key in required if key not in value]
        if missing:
            raise _SchemaViolation(f"{path} is missing fields: {missing}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise _SchemaViolation(f"{path}.properties is malformed")
        for key, item in value.items():
            if key in properties:
                _validate_schema_value(
                    item,
                    properties[key],
                    document=document,
                    base_uri=base_uri,
                    registry=registry,
                    path=f"{path}.{key}",
                )
            elif schema.get("additionalProperties") is False:
                raise _SchemaViolation(f"{path} has unsupported field {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate_schema_value(
                    item,
                    schema["additionalProperties"],
                    document=document,
                    base_uri=base_uri,
                    registry=registry,
                    path=f"{path}.{key}",
                )

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise _SchemaViolation(f"{path} has too few items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise _SchemaViolation(f"{path} has too many items")
        if schema.get("uniqueItems") is True:
            canonical = [_canonical_bytes(item) for item in value]
            if len(canonical) != len(set(canonical)):
                raise _SchemaViolation(f"{path} contains duplicate items")
        prefix = schema.get("prefixItems", [])
        if prefix and not isinstance(prefix, list):
            raise _SchemaViolation(f"{path}.prefixItems is malformed")
        for index, item in enumerate(value):
            if index < len(prefix):
                item_schema = prefix[index]
            elif "items" in schema:
                item_schema = schema["items"]
            else:
                continue
            _validate_schema_value(
                item,
                item_schema,
                document=document,
                base_uri=base_uri,
                registry=registry,
                path=f"{path}[{index}]",
            )
        if "contains" in schema:
            matches = 0
            for item in value:
                try:
                    _validate_schema_value(
                        item,
                        schema["contains"],
                        document=document,
                        base_uri=base_uri,
                        registry=registry,
                        path=f"{path}[*]",
                    )
                except _SchemaViolation:
                    continue
                matches += 1
            if matches < schema.get("minContains", 1):
                raise _SchemaViolation(f"{path} does not contain a required item")
            if "maxContains" in schema and matches > schema["maxContains"]:
                raise _SchemaViolation(f"{path} contains too many matching items")

    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            raise _SchemaViolation(f"{path} is shorter than minLength")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            raise _SchemaViolation(f"{path} is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise _SchemaViolation(f"{path} does not match the required pattern")
        if isinstance(schema.get("format"), str):
            _check_format(value, schema["format"], path)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise _SchemaViolation(f"{path} must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise _SchemaViolation(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise _SchemaViolation(f"{path} is above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise _SchemaViolation(f"{path} is not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise _SchemaViolation(f"{path} is not below exclusiveMaximum")


def _validate_document_schema(value: Any, schema_path: Path, *, label: str) -> None:
    root_schema, registry = _schema_registry()
    schema = root_schema if schema_path == SCHEMA_PATH else _read_json(schema_path)
    base_uri = str(schema.get("$id") or schema_path.resolve().as_uri())
    try:
        _validate_schema_value(
            value,
            schema,
            document=schema,
            base_uri=base_uri,
            registry=registry,
            path="$",
        )
    except _SchemaViolation as exc:
        raise DashboardSnapshotError(f"{label} violates its JSON schema: {exc}") from exc


def _validate_with_optional_jsonschema(value: dict[str, Any]) -> None:
    """Run the reference implementation when the host already provides it."""

    try:
        import jsonschema
    except ModuleNotFoundError:
        return
    root_schema, registry = _schema_registry()
    try:
        from referencing import Registry, Resource

        resource_registry = Registry()
        root_base = str(root_schema["$id"])
        registered: set[str] = set()
        for uri, schema in registry.items():
            absolute_uri = urljoin(root_base, uri)
            if absolute_uri in registered:
                continue
            resource_registry = resource_registry.with_resource(
                absolute_uri, Resource.from_contents(schema)
            )
            registered.add(absolute_uri)
        jsonschema.Draft202012Validator.check_schema(root_schema)
        validator = jsonschema.Draft202012Validator(
            root_schema,
            registry=resource_registry,
            format_checker=jsonschema.FormatChecker(),
        )
        errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    except Exception as exc:  # pragma: no cover - dependency-specific failure
        raise DashboardSnapshotError(f"jsonschema validation could not run: {exc}") from exc
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "$"
        raise DashboardSnapshotError(
            f"dashboard snapshot violates JSON schema at {location}: {first.message}"
        )


def _atomic_write(path: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(
                json.dumps(
                    value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
                ).encode("utf-8")
                + b"\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _default_architecture_nodes(policy: dict[str, Any]) -> list[dict[str, str]]:
    modes = policy["task_modes"]
    if set(modes) != set(TASK_MODES) or len(modes) != len(TASK_MODES):
        raise DashboardSnapshotError(
            "orchestration policy must expose exactly the eight canonical task modes"
        )
    return [
        {"id": "intent_router", "label": "任务路由", "owner": "deterministic", "status": "ready", "summary": f"识别 {len(modes)} 种任务模式"},
        {"id": "scope_gate", "label": "范围与权限", "owner": "deterministic", "status": "ready", "summary": "冻结品种、合约、时间与交易边界"},
        {"id": "market_scan", "label": "全市场宽筛", "owner": "deterministic", "status": "idle", "summary": "完整扫描后再由 Agent 选择少量候选"},
        {"id": "contract_binding", "label": "实际主力绑定", "owner": "deterministic", "status": "idle", "summary": "先绑定 actual main 与 analysis contract"},
        {"id": "evidence_fanout", "label": "条件并行取证", "owner": "specialists", "status": "idle", "summary": "各专家只提交 EvidencePacket，不投票"},
        {"id": "evidence_validation", "label": "证据归一与校验", "owner": "deterministic", "status": "idle", "summary": "五轴状态、来源、时点和身份校验"},
        {"id": "agent_hypothesis", "label": "期货主假设", "owner": "agent", "status": "idle", "summary": "Agent 比较多空等待并选择可证伪假设"},
        {"id": "entry_path_comparison", "label": "入场路径比较", "owner": "agent", "status": "idle", "summary": "只比较当前合理路径与等待，不机械评分"},
        {"id": "option_expression", "label": "买方期权表达", "owner": "agent", "status": "idle", "summary": "仅在方向与兑现窗口成立时按需开启"},
        {"id": "bounded_critic", "label": "一次反证审查", "owner": "agent", "status": "idle", "summary": "只找矛盾和遗漏，不拥有第二票"},
        {"id": "final_validation", "label": "最终事实校验", "owner": "deterministic", "status": "idle", "summary": "校验事实与执行一致性，不授权下单"},
        {"id": "position_lifecycle", "label": "持仓生命周期", "owner": "state_machine", "status": "idle", "summary": "一次入场、持续重估、一次全平"},
        {"id": "unified_dashboard", "label": "统一应用看板", "owner": "renderer", "status": "ready", "summary": "同时呈现决策、架构、状态与学习"},
        {"id": "learning_review", "label": "真实交易复盘", "owner": "agent", "status": "idle", "summary": "从验证结果提炼候选经验，不自动改规则"},
    ]


def _validate_policy(policy: dict[str, Any]) -> None:
    modes = policy.get("task_modes")
    if not isinstance(modes, dict) or set(modes) != set(TASK_MODES) or len(modes) != len(TASK_MODES):
        raise DashboardSnapshotError(
            "orchestration policy must define exactly eight canonical task modes"
        )
    expected_controls = {
        "schema_version": "decision_orchestration_policy.v1",
        "decision_owner": "agent",
        "machine_may_select_direction": False,
        "majority_vote_allowed": False,
        "maximum_critic_revisions": 1,
        "order_placement_authorized": False,
        "short_option_open_allowed": False,
    }
    for field, expected in expected_controls.items():
        if not _json_equal(policy.get(field), expected):
            raise DashboardSnapshotError(
                f"orchestration policy weakens or omits control {field!r}"
            )
    lifecycle_states = policy.get("lifecycle_states")
    if not isinstance(lifecycle_states, list) or len(lifecycle_states) != 13:
        raise DashboardSnapshotError("orchestration policy lifecycle states are invalid")
    if len({_canonical_bytes(item) for item in lifecycle_states}) != len(lifecycle_states):
        raise DashboardSnapshotError("orchestration policy lifecycle states must be unique")


def _learning_payload(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = _read_json(path)
    _validate_learning_summary(value)
    return value


def _validate_learning_summary(value: dict[str, Any]) -> None:
    if value.get("schema_version") != "trade_learning_summary.v1":
        raise DashboardSnapshotError(
            "learning input must be trade_learning_summary.v1; rendered documents are not data inputs"
        )
    _validate_document_schema(
        value, LEARNING_SCHEMA_PATH, label="trade-learning summary"
    )
    unsigned = deepcopy(value)
    supplied = unsigned.pop("summary_sha256", None)
    expected = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    if supplied != expected:
        raise DashboardSnapshotError("trade-learning summary_sha256 mismatch")


def _validate_snapshot_semantics(
    snapshot: dict[str, Any], *, policy: dict[str, Any]
) -> None:
    modes = policy.get("task_modes", {})
    if not isinstance(modes, dict) or set(modes) != set(TASK_MODES) or len(modes) != len(TASK_MODES):
        raise DashboardSnapshotError("dashboard policy task-mode set is not canonical")

    nodes = snapshot["architecture"]["nodes"]
    node_ids = [item["id"] for item in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise DashboardSnapshotError("dashboard architecture node ids must be unique")
    if len(node_ids) != len(CANONICAL_NODE_IDS) or set(node_ids) != set(CANONICAL_NODE_IDS):
        missing_nodes = sorted(set(CANONICAL_NODE_IDS) - set(node_ids))
        extra_nodes = sorted(set(node_ids) - set(CANONICAL_NODE_IDS))
        raise DashboardSnapshotError(
            "dashboard architecture node set must equal the canonical 14 nodes; "
            f"missing={missing_nodes}, extra={extra_nodes}"
        )
    edges = snapshot["architecture"]["edges"]
    edge_keys = [tuple(edge) for edge in edges]
    if len(edge_keys) != len(set(edge_keys)):
        raise DashboardSnapshotError("dashboard architecture edges must be unique")
    for source, destination in edges:
        if source not in node_ids or destination not in node_ids:
            raise DashboardSnapshotError(
                f"dashboard architecture edge references an unknown node: {source} -> {destination}"
            )

    universe = snapshot["universe"]
    products = universe["products"]
    if universe["product_count"] != len(products):
        raise DashboardSnapshotError("dashboard universe product_count mismatch")
    grouped = [
        product
        for group in universe["primary_groups"]
        for product in group["products"]
    ]
    if len(grouped) != len(set(grouped)) or set(grouped) != set(products):
        raise DashboardSnapshotError(
            "dashboard primary groups must cover each universe product exactly once"
        )
    for cluster in universe["correlated_exposure_clusters"]:
        if not set(cluster["products"]) <= set(products):
            raise DashboardSnapshotError(
                f"dashboard exposure cluster {cluster['id']!r} includes an out-of-scope product"
            )

    current_run = snapshot["current_run"]
    if current_run is None:
        if snapshot["evidence"] or snapshot["underlying_thesis"] is not None:
            raise DashboardSnapshotError(
                "dashboard without current_run cannot carry evidence or an underlying thesis"
            )
        if snapshot["option_expression"] is not None or snapshot["lifecycle"]["current_state"] is not None:
            raise DashboardSnapshotError(
                "dashboard without current_run cannot carry option or lifecycle state"
            )
    else:
        run_id = current_run["run_id"]
        product = current_run["product"]
        requested_contract = current_run["requested_contract"]
        for packet in snapshot["evidence"]:
            if packet["run_id"] != run_id:
                raise DashboardSnapshotError("dashboard evidence packet run_id mismatch")
            if product is not None and packet["product"] not in {None, product}:
                raise DashboardSnapshotError("dashboard evidence packet product mismatch")
            if requested_contract is not None and packet["analysis_contract"] not in {
                None,
                requested_contract,
            }:
                raise DashboardSnapshotError("dashboard evidence packet contract mismatch")
            facts = packet.get("facts")
            session_bar = (
                facts.get("session_daily_bar") if isinstance(facts, dict) else None
            )
            if session_bar is not None:
                try:
                    bar_summary = session_aware_bar.validate_session_aware_daily_bar(
                        session_bar,
                        expected_product=packet.get("product"),
                        expected_contract=packet.get("analysis_contract"),
                        packet_observed_at=packet.get("observed_at"),
                        now=datetime.fromisoformat(
                            snapshot["generated_at"].replace("Z", "+00:00")
                        ),
                    )
                except (session_aware_bar.SessionAwareBarError, ValueError) as exc:
                    raise DashboardSnapshotError(
                        f"dashboard contains invalid session-aware daily bar: {exc}"
                    ) from exc
                if packet.get("domain") != "technical_structure":
                    raise DashboardSnapshotError(
                        "session-aware daily bar must remain technical_structure evidence"
                    )
                if session_bar.get("source") not in packet.get("source_refs", []):
                    raise DashboardSnapshotError(
                        "session-aware daily-bar source is not preserved in its evidence packet"
                    )
                if bar_summary["bar_state"] == "session_partial" and (
                    packet.get("status", {}).get("availability") != "partial"
                    or "session_partial_daily_bar_not_completed"
                    not in packet.get("known_gaps", [])
                ):
                    raise DashboardSnapshotError(
                        "session_partial daily bar must remain explicitly partial in the dashboard"
                    )
        thesis = snapshot["underlying_thesis"]
        if thesis is not None:
            if product is not None and thesis["product"] != product:
                raise DashboardSnapshotError("dashboard thesis product mismatch")
            if requested_contract is not None and thesis["analysis_contract"] != requested_contract:
                raise DashboardSnapshotError("dashboard thesis contract mismatch")
            bars_by_packet = {
                str(packet["packet_id"]): packet["facts"]["session_daily_bar"]
                for packet in snapshot["evidence"]
                if packet.get("domain") == "technical_structure"
                and isinstance(packet.get("facts"), dict)
                and isinstance(packet["facts"].get("session_daily_bar"), dict)
            }
            partial_ids = {
                packet_id
                for packet_id, bar in bars_by_packet.items()
                if bar.get("bar_state") == "session_partial"
            }
            basis = thesis.get("daily_bar_basis")
            if partial_ids and not isinstance(basis, dict):
                raise DashboardSnapshotError(
                    "dashboard thesis must disclose its session_partial daily-bar basis"
                )
            if isinstance(basis, dict):
                referenced = bars_by_packet.get(str(basis.get("evidence_packet_id")))
                if referenced is None or basis != {
                    "evidence_packet_id": basis.get("evidence_packet_id"),
                    "bar_state": referenced.get("bar_state"),
                    "as_of": referenced.get("as_of"),
                    "decision_semantics": referenced.get("decision_semantics"),
                }:
                    raise DashboardSnapshotError(
                        "dashboard thesis daily_bar_basis does not match its evidence"
                    )
                if (
                    str(thesis.get("path", "")).strip().lower()
                    == "confirmed_breakout"
                    and referenced.get("bar_state") != "completed"
                ):
                    raise DashboardSnapshotError(
                        "dashboard cannot relabel a partial bar as confirmed daily breakout"
                    )
        option = snapshot["option_expression"]
        if option is not None and thesis is None:
            raise DashboardSnapshotError(
                "dashboard option expression requires an underlying thesis"
            )
        if option is not None and option["underlying_thesis"]["analysis_contract"] != thesis["analysis_contract"]:
            raise DashboardSnapshotError(
                "dashboard option expression contract conflicts with its underlying thesis"
            )

    if snapshot["lifecycle"]["allowed_states"] != policy["lifecycle_states"]:
        raise DashboardSnapshotError(
            "dashboard lifecycle states do not match the frozen orchestration policy"
        )
    if snapshot["boundaries"] != BOUNDARIES:
        raise DashboardSnapshotError("dashboard execution boundaries are not fail-closed")
    if snapshot["learning"] is not None:
        _validate_learning_summary(snapshot["learning"])


def validate_snapshot(
    snapshot: dict[str, Any], *, policy: dict[str, Any] | None = None
) -> None:
    """Validate both the declared schema and invariants JSON Schema cannot express."""

    _canonical_bytes(snapshot)
    _validate_document_schema(snapshot, SCHEMA_PATH, label="dashboard snapshot")
    _validate_snapshot_semantics(snapshot, policy=policy or _read_json(ORCHESTRATION_POLICY_PATH))
    _validate_with_optional_jsonschema(snapshot)


def build_snapshot(
    *,
    run: dict[str, Any] | None = None,
    learning: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy = _read_json(ORCHESTRATION_POLICY_PATH)
    _validate_policy(policy)
    universe = _read_json(UNIVERSE_PATH)
    groups = _read_json(GROUPS_PATH)
    if groups.get("universe_sha256") != _sha256(UNIVERSE_PATH):
        raise DashboardSnapshotError("product groups are not bound to the current universe")
    allowed = set(runtime_universe.allowed_product_symbols())
    grouped = [
        symbol for group in groups["primary_groups"] for symbol in group["products"]
    ]
    if len(grouped) != len(set(grouped)) or set(grouped) != allowed:
        raise DashboardSnapshotError("primary product groups do not exactly cover the universe")

    if learning is not None:
        if not isinstance(learning, dict):
            raise DashboardSnapshotError("learning input must be an object or null")
        _validate_learning_summary(learning)

    current_run = None
    evidence: list[dict[str, Any]] = []
    thesis = None
    option = None
    architecture_nodes = _default_architecture_nodes(policy)
    lifecycle_state = None
    if run is not None:
        if not isinstance(run, dict):
            raise DashboardSnapshotError("decision run input must be an object or null")
        validation = run_decision.validate_run(run, now=now)
        if not validation["valid"]:
            raise DashboardSnapshotError(f"invalid decision run: {validation['errors'][0]}")
        current_run = {
            "run_id": run["run_id"],
            "task_mode": run["task_mode"],
            "phase": run["phase"],
            "product": run["request"]["product"],
            "requested_contract": run["request"]["requested_contract"],
            "updated_at": run["updated_at"],
            "missing_required_evidence_domains": validation["missing_required_evidence_domains"],
            "ready_for_agent": validation["ready_for_agent"],
            "scope": deepcopy(run["scope"]),
            "quality_review": deepcopy(run["quality_review"]),
            "permissions": deepcopy(run["permissions"]),
        }
        evidence = deepcopy(run["evidence_packets"])
        thesis = deepcopy(run["underlying_thesis"])
        option = deepcopy(run["option_expression"])
        lifecycle_state = run["lifecycle_state"]
        run_nodes = {item["id"]: item for item in run["nodes"]}
        for node in architecture_nodes:
            state = run_nodes.get(node["id"])
            if state is not None:
                node["status"] = state["status"]
                node["owner"] = state["owner"]
                node["summary"] = state["purpose"]

    clock = now or datetime.now(tz=run_decision.BJT)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise DashboardSnapshotError("dashboard clock must be timezone-aware")
    generated = clock.astimezone(run_decision.BJT).isoformat(timespec="seconds")
    snapshot = {
        "schema_version": "skill_dashboard_snapshot.v1",
        "generated_at": generated,
        "skill": {
            "name": "cn-futures-decision-agent",
            "display_name": "中国期货与买方期权决策 Agent",
            "architecture": "evidence_gated_adaptive_decision_graph.v1",
            "skill_sha256": _sha256(SKILL_PATH),
            "orchestration_policy_sha256": _sha256(ORCHESTRATION_POLICY_PATH),
        },
        "current_run": current_run,
        "architecture": {
            "nodes": architecture_nodes,
            "edges": [
                ["intent_router", "scope_gate"],
                ["scope_gate", "market_scan"],
                ["scope_gate", "contract_binding"],
                ["market_scan", "contract_binding"],
                ["contract_binding", "evidence_fanout"],
                ["evidence_fanout", "evidence_validation"],
                ["evidence_validation", "agent_hypothesis"],
                ["agent_hypothesis", "entry_path_comparison"],
                ["entry_path_comparison", "option_expression"],
                ["agent_hypothesis", "bounded_critic"],
                ["option_expression", "bounded_critic"],
                ["bounded_critic", "final_validation"],
                ["final_validation", "position_lifecycle"],
                ["position_lifecycle", "unified_dashboard"],
                ["unified_dashboard", "learning_review"],
                ["learning_review", "intent_router"]
            ],
            "decision_owner": "agent",
            "specialists_vote": False,
            "maximum_critic_revisions": policy["maximum_critic_revisions"]
        },
        "universe": {
            "product_count": universe["product_count"],
            "effective_since": universe["effective_since"],
            "products": sorted(allowed),
            "primary_groups": groups["primary_groups"],
            "correlated_exposure_clusters": groups["correlated_exposure_clusters"]
        },
        "evidence": evidence,
        "underlying_thesis": thesis,
        "option_expression": option,
        "lifecycle": {
            "current_state": lifecycle_state,
            "allowed_states": policy["lifecycle_states"],
            "monitor_required_for_immediate_stop": True
        },
        "learning": deepcopy(learning),
        "boundaries": deepcopy(BOUNDARIES),
    }
    validate_snapshot(snapshot, policy=policy)
    return snapshot


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build data for the unified Skill architecture and trading dashboard."
    )
    parser.add_argument("--run", help="validated decision_run.v1 JSON")
    parser.add_argument("--learning", help="validated trade-learning summary JSON")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        run = _read_json(args.run) if args.run else None
        learning = _learning_payload(args.learning)
        snapshot = build_snapshot(run=run, learning=learning)
        path = _atomic_write(args.output, snapshot)
        print(json.dumps({"success": True, "output": str(path)}, ensure_ascii=False))
        return 0
    except (DashboardSnapshotError, run_decision.DecisionRunError, OSError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

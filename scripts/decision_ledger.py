#!/usr/bin/env python3
"""Append-only forward decision ledger with replayable arithmetic.

This module records what the Agent knew and intended *before* an outcome, then
records the outcome as a second append-only event.  It validates local provenance,
ordering, single-entry/full-exit execution and PnL arithmetic.  It deliberately
never declares that a strategy is profitable: forward samples still require an
independent statistical and economic review.  The local ledger, artifacts and
head can all be rewritten by a privileged actor; this is not a WORM or signature.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "decision_ledger_policy.json"
BJT = ZoneInfo("Asia/Shanghai")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CONTRACT_RE = re.compile(r"^[A-Z]{1,3}[0-9]{4}$")
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")
PROFIT_OBJECTIVE = "long_term_verifiable_net_profit_after_costs"
AGENT_DOMAIN_IDS = (
    "technical_structure",
    "fundamentals_and_news",
    "member_flow",
)


class DecisionLedgerError(ValueError):
    """Base class for ledger input and persistence failures."""


class DecisionLedgerValidationError(DecisionLedgerError):
    """Raised when a ledger or event cannot be replayed safely."""


def _current_time() -> datetime:
    """Clock boundary kept tiny so tests can advance time without backdating CLI."""

    return datetime.now(tz=BJT)


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DecisionLedgerValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_loads(text: str, *, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_object)
    except (json.JSONDecodeError, DecisionLedgerValidationError) as exc:
        raise DecisionLedgerValidationError(f"invalid {label}: {exc}") from exc


def _canonical_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DecisionLedgerValidationError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_policy(policy_path: Path = POLICY_PATH) -> tuple[dict[str, Any], str]:
    try:
        raw = policy_path.read_bytes()
    except OSError as exc:
        raise DecisionLedgerValidationError(f"cannot read policy {policy_path}: {exc}") from exc
    policy = _json_loads(raw.decode("utf-8"), label="ledger policy JSON")
    if not isinstance(policy, dict) or policy.get("schema_version") != "decision_ledger_policy.v1":
        raise DecisionLedgerValidationError("unsupported decision ledger policy")
    if policy.get("hash_algorithm") != "sha256":
        raise DecisionLedgerValidationError("decision ledger policy must use sha256")
    try:
        if policy["genesis_previous_event_hash"] != "0" * 64:
            raise DecisionLedgerValidationError("ledger genesis must be the canonical zero hash")
        if set(policy["allowed_directional_views"]) != {"long", "short", "neutral", "uncertain"}:
            raise DecisionLedgerValidationError("directional_view policy is incompatible with report v3")
        if set(policy["allowed_actions"]) != {"long", "short", "wait"}:
            raise DecisionLedgerValidationError("action policy must be long, short or wait")
        execution = policy["execution_policy"]
        if (
            execution["entry_count"] != 1
            or execution["full_exit_count"] != 1
            or execution["allow_pyramiding"] is not False
            or execution["allow_averaging_down"] is not False
            or execution["allow_rollover"] is not False
        ):
            raise DecisionLedgerValidationError(
                "execution policy must preserve one entry, one full exit and no adding/rollover"
            )
        validation = policy["validation"]
        skew = int(validation["maximum_future_clock_skew_seconds"])
        if not 0 <= skew <= 300:
            raise DecisionLedgerValidationError("future clock skew cannot exceed five minutes")
        for evidence_gate in (
            "require_report_artifact_replay",
            "require_report_policy_artifact_replay",
            "require_execution_artifact_replay",
            "require_wait_market_bars_replay",
            "require_wait_point_plan_bidirectional_binding",
        ):
            if validation[evidence_gate] is not True:
                raise DecisionLedgerValidationError(
                    f"ledger policy cannot disable {evidence_gate}"
                )
        profit = policy["profit_summary"]
        if int(profit["minimum_closed_actual_trades"]) < 20:
            raise DecisionLedgerValidationError("profit review floor cannot be below 20 actual closures")
        if int(profit["minimum_observation_span_days"]) < 730:
            raise DecisionLedgerValidationError("profit review span cannot be below 730 days")
        if profit["require_all_due_decisions_closed"] is not True:
            raise DecisionLedgerValidationError("all due decisions must have outcomes")
        if profit["automatic_profit_verified"] is not False:
            raise DecisionLedgerValidationError("ledger can never auto-verify profit")
        if policy["claim_boundary"][
            "privileged_rewrite_of_ledger_and_head_together_is_not_detectable"
        ] is not True:
            raise DecisionLedgerValidationError(
                "policy must disclose privileged ledger+head rewrite limitation"
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise DecisionLedgerValidationError(f"malformed decision ledger policy: {exc}") from exc
    return policy, _sha256_bytes(raw)


def _head_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(f"{ledger_path.name}.head.json")


def _lock_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(f".{ledger_path.name}.lock")


@contextmanager
def _exclusive_lock(ledger_path: Path) -> Iterator[None]:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(ledger_path)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _shared_lock(ledger_path: Path) -> Iterator[None]:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(_lock_path(ledger_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(path: Path, content: bytes) -> None:
    """Publish one complete generation, then fsync the containing directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DecisionLedgerValidationError("recording clock must be timezone-aware")
    return value.astimezone(BJT).isoformat(timespec="seconds")


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise DecisionLedgerValidationError(f"{field} must be a timezone-aware ISO timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DecisionLedgerValidationError(f"{field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DecisionLedgerValidationError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _require_exact_fields(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DecisionLedgerValidationError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise DecisionLedgerValidationError(
            f"{label} fields mismatch; missing={missing}, extra={extra}"
        )
    return value


def _require_text(value: Any, field: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionLedgerValidationError(f"{field} must be non-empty text")
    if len(value) > maximum:
        raise DecisionLedgerValidationError(f"{field} exceeds {maximum} characters")
    return value


def _decimal(value: Any, field: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, str) or DECIMAL_RE.fullmatch(value) is None:
        raise DecisionLedgerValidationError(
            f"{field} must be a plain decimal string (JSON floats are not accepted)"
        )
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise DecisionLedgerValidationError(f"{field} is not a finite decimal") from exc
    if not result.is_finite():
        raise DecisionLedgerValidationError(f"{field} is not finite")
    if nonnegative and result < 0:
        raise DecisionLedgerValidationError(f"{field} must be non-negative")
    return result


def _positive_decimal(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)
    if result <= 0:
        raise DecisionLedgerValidationError(f"{field} must be positive")
    return result


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, str) or POSITIVE_INTEGER_RE.fullmatch(value) is None:
        raise DecisionLedgerValidationError(f"{field} must be a positive integer string")
    return int(value)


def _money_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def _resolve_artifact_path(path_text: Any, *, artifact_root: Path, field: str) -> Path:
    if not isinstance(path_text, str) or not path_text.strip():
        raise DecisionLedgerValidationError(f"{field}.path must be non-empty text")
    candidate = Path(path_text).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        root = artifact_root.resolve()
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise DecisionLedgerValidationError(
                f"{field}.path escapes the ledger artifact directory"
            ) from exc
    if not resolved.is_file():
        raise DecisionLedgerValidationError(f"{field}.path is not a readable file: {resolved}")
    return resolved


def _read_artifact_bytes(path: Path, *, field: str) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DecisionLedgerValidationError(f"cannot reopen {field} artifact {path}: {exc}") from exc
    if len(raw) > 4 * 1024 * 1024:
        raise DecisionLedgerValidationError(f"{field} artifact exceeds the 4 MiB replay bound")
    return raw


def _load_artifact(
    descriptor_value: Any,
    *,
    artifact_root: Path,
    expected_schema: str | set[str],
    field: str,
) -> tuple[dict[str, Any], Path]:
    descriptor = _require_exact_fields(
        descriptor_value,
        {"path", "sha256", "schema_version"},
        field,
    )
    schemas = {expected_schema} if isinstance(expected_schema, str) else expected_schema
    if descriptor["schema_version"] not in schemas:
        raise DecisionLedgerValidationError(f"{field}.schema_version is unsupported")
    if not isinstance(descriptor["sha256"], str) or HEX64_RE.fullmatch(descriptor["sha256"]) is None:
        raise DecisionLedgerValidationError(f"{field}.sha256 must be lowercase SHA-256")
    path = _resolve_artifact_path(descriptor["path"], artifact_root=artifact_root, field=field)
    raw = _read_artifact_bytes(path, field=field)
    actual_sha = _sha256_bytes(raw)
    if actual_sha != descriptor["sha256"]:
        raise DecisionLedgerValidationError(
            f"{field} artifact SHA-256 mismatch: "
            f"expected={descriptor['sha256']}, actual={actual_sha}"
        )
    try:
        artifact = _json_loads(raw.decode("utf-8"), label=f"{field} artifact JSON")
    except UnicodeDecodeError as exc:
        raise DecisionLedgerValidationError(f"{field} artifact is not UTF-8") from exc
    if not isinstance(artifact, dict) or artifact.get("schema_version") != descriptor["schema_version"]:
        raise DecisionLedgerValidationError(f"{field} artifact schema does not match descriptor")
    return artifact, path


def _report_decimal(value: Any, field: str) -> Decimal:
    """Read an already JSON-validated report number without float arithmetic."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise DecisionLedgerValidationError(f"{field} must be a finite JSON number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise DecisionLedgerValidationError(f"{field} must be a finite JSON number") from exc
    if not result.is_finite():
        raise DecisionLedgerValidationError(f"{field} must be a finite JSON number")
    return result


def _resolve_report_gap_ref(report: dict[str, Any], ref: Any, field: str) -> Any:
    allowed = {
        "structure_evidence.status",
        "market_data.status",
        "agent_judgment.coverage_uncertainty.known_gaps",
        "agent_judgment.costs_and_slippage_considered",
    }
    if not isinstance(ref, str) or ref not in allowed:
        raise DecisionLedgerValidationError(
            f"{field} must reference a report-local structure, market, coverage or cost gap"
        )
    cursor: Any = report
    for component in ref.split("."):
        if not isinstance(cursor, dict) or component not in cursor:
            raise DecisionLedgerValidationError(
                f"{field} does not resolve inside report_evidence: {ref}"
            )
        cursor = cursor[component]
    if cursor is None or cursor == "" or cursor == [] or cursor == {}:
        raise DecisionLedgerValidationError(
            f"{field} resolves to an empty report value: {ref}"
        )
    return cursor


def _replay_futures_report(
    report: dict[str, Any],
    report_policy: dict[str, Any],
    *,
    recorded: datetime,
    report_path: Path,
    policy_path: Path,
) -> dict[str, Any]:
    """Replay the real report validator at the original ledger freeze clock."""

    try:
        try:
            from scripts import futures_main_report
        except ModuleNotFoundError:  # Direct script execution puts scripts/ on sys.path.
            import futures_main_report  # type: ignore[no-redef]

        assessment = futures_main_report._assess_report_at(
            report,
            report_policy,
            validated_at=recorded,
            artifact_root=report_path.parent,
            policy_path=policy_path,
        )
    except Exception as exc:
        # Keep the public ledger error stable while retaining the production
        # validator's reason.  SystemExit/KeyboardInterrupt remain untouched.
        raise DecisionLedgerValidationError(
            f"report_evidence failed futures_main_report.v3 production replay: {exc}"
        ) from exc
    if not isinstance(assessment, dict):
        raise DecisionLedgerValidationError(
            "report_evidence production replay did not return an assessment object"
        )
    return assessment


def _bind_decision_to_report(
    decision: dict[str, Any],
    report: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    """Bind the compact forward record to the complete three-domain report."""

    if _parse_time(report.get("observed_at"), "report_evidence.observed_at") != _parse_time(
        decision["observed_at"], "observed_at"
    ):
        raise DecisionLedgerValidationError(
            "report_evidence.observed_at does not exactly bind the frozen decision instant"
        )
    target = report.get("target")
    if not isinstance(target, dict):
        raise DecisionLedgerValidationError("report_evidence.target must be an object")
    if (
        target.get("analysis_contract") != decision["analysis_contract"]
        or assessment.get("analysis_contract") != decision["analysis_contract"]
    ):
        raise DecisionLedgerValidationError(
            "report_evidence analysis_contract does not exactly bind the frozen decision"
        )
    if (
        report.get("action") != decision["action"]
        or assessment.get("action") != decision["action"]
    ):
        raise DecisionLedgerValidationError(
            "report_evidence action does not exactly bind the frozen decision"
        )

    judgment = report.get("agent_judgment")
    if not isinstance(judgment, dict):
        raise DecisionLedgerValidationError(
            "report_evidence.agent_judgment must be an object"
        )
    if judgment.get("objective") != PROFIT_OBJECTIVE or decision["objective"] != PROFIT_OBJECTIVE:
        raise DecisionLedgerValidationError(
            "decision and report objective must be long-term verifiable net profit after costs"
        )
    if (
        judgment.get("directional_view") != decision["directional_view"]
        or assessment.get("directional_view") != decision["directional_view"]
    ):
        raise DecisionLedgerValidationError(
            "report_evidence directional_view does not exactly bind the frozen decision"
        )
    if (
        judgment.get("expected_net_value") != decision["expected_net_value"]
        or assessment.get("expected_net_value") != decision["expected_net_value"]
    ):
        raise DecisionLedgerValidationError(
            "report_evidence expected_net_value does not exactly bind the frozen decision"
        )
    if decision["action"] in {"long", "short"} and decision["expected_net_value"] != "positive":
        raise DecisionLedgerValidationError(
            "a capital action requires the Agent's frozen after-cost expected_net_value=positive"
        )
    if judgment.get("synthesis_thesis") != decision["main_hypothesis"]:
        raise DecisionLedgerValidationError(
            "report_evidence synthesis_thesis does not exactly bind main_hypothesis"
        )
    expected_judgment_sha = _sha256_bytes(_canonical_bytes(judgment))
    if decision["agent_judgment_sha256"] != expected_judgment_sha:
        raise DecisionLedgerValidationError(
            "agent_judgment_sha256 does not bind the complete three-domain Agent judgment"
        )

    domains = judgment.get("domains")
    if not isinstance(domains, dict) or set(domains) != set(AGENT_DOMAIN_IDS):
        raise DecisionLedgerValidationError(
            "report_evidence must contain technical, fundamental/news and member-flow judgments"
        )
    domain_confidences: list[Decimal] = []
    for domain_id in AGENT_DOMAIN_IDS:
        domain = domains.get(domain_id)
        if not isinstance(domain, dict) or not isinstance(domain.get("confidence"), dict):
            raise DecisionLedgerValidationError(
                f"report_evidence.agent_judgment.domains.{domain_id}.confidence is missing"
            )
        domain_confidences.append(
            _report_decimal(
                domain["confidence"].get("value"),
                f"report_evidence.agent_judgment.domains.{domain_id}.confidence.value",
            )
        )
    confidence = decision["confidence_range"]
    if (
        _decimal(confidence["low"], "confidence_range.low") != min(domain_confidences)
        or _decimal(confidence["high"], "confidence_range.high") != max(domain_confidences)
    ):
        raise DecisionLedgerValidationError(
            "confidence_range must equal the min/max of the three frozen domain judgments"
        )

    report_plan = report.get("point_plan")
    if decision["action"] == "wait":
        wait_plan = decision["wait_counterfactual"]
        wait_status = wait_plan["status"]
        if report_plan is None and wait_status != "unavailable":
            raise DecisionLedgerValidationError(
                "report_evidence point_plan=null requires wait_counterfactual.status=unavailable"
            )
        if report_plan is not None and wait_status != "defined":
            raise DecisionLedgerValidationError(
                "report_evidence with a point_plan requires wait_counterfactual.status=defined"
            )
        if wait_status == "unavailable":
            for index, ref in enumerate(wait_plan["report_gap_refs"]):
                _resolve_report_gap_ref(
                    report,
                    ref,
                    f"wait_counterfactual.report_gap_refs[{index}]",
                )
    ledger_plan: dict[str, Any] | None
    if decision["action"] in {"long", "short"}:
        ledger_plan = decision["trade_plan"]
    elif decision["wait_counterfactual"]["status"] == "defined":
        ledger_plan = decision["wait_counterfactual"]
    else:
        ledger_plan = None
    if ledger_plan is None:
        return
    if not isinstance(report_plan, dict):
        raise DecisionLedgerValidationError(
            "a defined trade/wait plan must bind report_evidence.point_plan"
        )
    for field in ("trigger", "entry_zone", "invalidation", "target"):
        if not isinstance(report_plan.get(field), dict):
            raise DecisionLedgerValidationError(
                f"report_evidence.point_plan.{field} must be an object"
            )
    if report_plan.get("direction") != ledger_plan.get("direction", decision["action"]):
        raise DecisionLedgerValidationError(
            "report_evidence point-plan direction does not exactly bind the ledger plan"
        )
    if report_plan["trigger"].get("text") != ledger_plan["trigger"]:
        raise DecisionLedgerValidationError(
            "report_evidence point-plan trigger does not exactly bind the ledger plan"
        )
    invalidation = _report_decimal(
        report_plan["invalidation"].get("price"),
        "report_evidence.point_plan.invalidation.price",
    )
    target_price = _report_decimal(
        report_plan["target"].get("price"),
        "report_evidence.point_plan.target.price",
    )
    if invalidation != _decimal(ledger_plan["invalidation_price"], "ledger invalidation_price"):
        raise DecisionLedgerValidationError(
            "report_evidence invalidation price does not exactly bind the ledger plan"
        )
    if target_price != _decimal(ledger_plan["target_price"], "ledger target_price"):
        raise DecisionLedgerValidationError(
            "report_evidence target price does not exactly bind the ledger plan"
        )
    entry_zone = report_plan["entry_zone"]
    entry_lower = _report_decimal(
        entry_zone.get("lower"), "report_evidence.point_plan.entry_zone.lower"
    )
    entry_upper = _report_decimal(
        entry_zone.get("upper"), "report_evidence.point_plan.entry_zone.upper"
    )
    if decision["action"] in {"long", "short"}:
        entry_price = _decimal(ledger_plan["entry_price"], "trade_plan.entry_price")
        if not entry_lower <= entry_price <= entry_upper:
            raise DecisionLedgerValidationError(
                "trade_plan.entry_price must lie inside report_evidence.point_plan.entry_zone"
            )
    else:
        trigger_price = _report_decimal(
            report_plan["trigger"].get("price"),
            "report_evidence.point_plan.trigger.price",
        )
        if trigger_price != _decimal(
            ledger_plan["trigger_price"], "wait_counterfactual.trigger_price"
        ):
            raise DecisionLedgerValidationError(
                "report_evidence trigger price does not exactly bind the wait counterfactual"
            )
        if _parse_time(
            judgment.get("reassess_by"), "report_evidence.agent_judgment.reassess_by"
        ) != _parse_time(decision["reassess_by"], "reassess_by"):
            raise DecisionLedgerValidationError(
                "report_evidence reassess_by does not exactly bind the wait decision"
            )


def _validate_cost_assumptions(
    value: Any,
    *,
    field: str,
    require_complete: bool,
) -> dict[str, Decimal] | None:
    item = _require_exact_fields(
        value,
        {
            "complete",
            "commission_cny",
            "slippage_cny",
            "roll_cny",
            "basis",
            "missing_fields",
            "reason",
        },
        field,
    )
    if not isinstance(item["complete"], bool):
        raise DecisionLedgerValidationError(f"{field}.complete must be boolean")
    if not isinstance(item["missing_fields"], list) or any(
        not isinstance(entry, str) or not entry.strip() for entry in item["missing_fields"]
    ):
        raise DecisionLedgerValidationError(f"{field}.missing_fields must be a text list")
    if item["complete"]:
        if item["missing_fields"] or item["reason"] is not None:
            raise DecisionLedgerValidationError(
                f"{field} complete=true requires empty missing_fields and null reason"
            )
        _require_text(item["basis"], f"{field}.basis", maximum=1000)
    else:
        if require_complete:
            raise DecisionLedgerValidationError(
                f"{field}.complete must be true for a capital action (fail closed)"
            )
        if not item["missing_fields"]:
            raise DecisionLedgerValidationError(
                f"{field}.missing_fields must explain incomplete wait costs"
            )
        _require_text(item["reason"], f"{field}.reason", maximum=1000)
        if item["basis"] is not None and (
            not isinstance(item["basis"], str) or not item["basis"].strip()
        ):
            raise DecisionLedgerValidationError(f"{field}.basis must be text or null")
        for amount_field in ("commission_cny", "slippage_cny", "roll_cny"):
            amount = item[amount_field]
            if amount is not None:
                _decimal(amount, f"{field}.{amount_field}", nonnegative=True)
        return None
    costs = {
        "commission": _decimal(item["commission_cny"], f"{field}.commission_cny", nonnegative=True),
        "slippage": _decimal(item["slippage_cny"], f"{field}.slippage_cny", nonnegative=True),
        "roll": _decimal(item["roll_cny"], f"{field}.roll_cny", nonnegative=True),
    }
    return costs


def _validate_price_geometry(direction: str, entry: Decimal, invalidation: Decimal, target: Decimal, label: str) -> None:
    if direction == "long" and not invalidation < entry < target:
        raise DecisionLedgerValidationError(
            f"{label} long requires invalidation < entry < target"
        )
    if direction == "short" and not target < entry < invalidation:
        raise DecisionLedgerValidationError(
            f"{label} short requires target < entry < invalidation"
        )


def _validate_trade_plan(value: Any, direction: str, recorded: datetime) -> None:
    plan = _require_exact_fields(
        value,
        {
            "trigger",
            "entry_price",
            "invalidation_price",
            "target_price",
            "quantity",
            "contract_multiplier",
            "latest_entry_at",
        },
        "trade_plan",
    )
    _require_text(plan["trigger"], "trade_plan.trigger", maximum=1000)
    entry = _positive_decimal(plan["entry_price"], "trade_plan.entry_price")
    invalidation = _positive_decimal(plan["invalidation_price"], "trade_plan.invalidation_price")
    target = _positive_decimal(plan["target_price"], "trade_plan.target_price")
    _positive_integer(plan["quantity"], "trade_plan.quantity")
    _positive_decimal(plan["contract_multiplier"], "trade_plan.contract_multiplier")
    latest_entry = _parse_time(plan["latest_entry_at"], "trade_plan.latest_entry_at")
    if latest_entry <= recorded:
        raise DecisionLedgerValidationError("trade_plan.latest_entry_at must be after freeze time")
    _validate_price_geometry(direction, entry, invalidation, target, "trade_plan")


def _validate_wait_plan(value: Any, recorded: datetime) -> None:
    if not isinstance(value, dict):
        raise DecisionLedgerValidationError("wait_counterfactual must be an object")
    status = value.get("status")
    if status == "unavailable":
        plan = _require_exact_fields(
            value,
            {"status", "reason", "missing_evidence", "report_gap_refs"},
            "wait_counterfactual",
        )
        _require_text(plan["reason"], "wait_counterfactual.reason", maximum=1000)
        if not isinstance(plan["missing_evidence"], list) or not plan["missing_evidence"]:
            raise DecisionLedgerValidationError(
                "wait_counterfactual.missing_evidence must be a non-empty text list"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in plan["missing_evidence"]
        ):
            raise DecisionLedgerValidationError(
                "wait_counterfactual.missing_evidence must contain non-empty text"
            )
        if not isinstance(plan["report_gap_refs"], list) or not plan["report_gap_refs"]:
            raise DecisionLedgerValidationError(
                "wait_counterfactual.report_gap_refs must be a non-empty text list"
            )
        if any(
            not isinstance(item, str) or not item.strip()
            for item in plan["report_gap_refs"]
        ):
            raise DecisionLedgerValidationError(
                "wait_counterfactual.report_gap_refs must contain non-empty text"
            )
        return
    if status != "defined":
        raise DecisionLedgerValidationError(
            "wait_counterfactual.status must be defined or unavailable"
        )
    plan = _require_exact_fields(
        value,
        {
            "status",
            "direction",
            "trigger",
            "trigger_price",
            "entry_rule",
            "invalidation_price",
            "target_price",
            "quantity",
            "contract_multiplier",
            "evaluation_start_at",
            "evaluation_end_at",
            "exit_rule",
            "bar_interval",
        },
        "wait_counterfactual",
    )
    if plan["direction"] not in {"long", "short"}:
        raise DecisionLedgerValidationError("wait_counterfactual.direction must be long or short")
    _require_text(plan["trigger"], "wait_counterfactual.trigger", maximum=1000)
    trigger_price = _positive_decimal(
        plan["trigger_price"], "wait_counterfactual.trigger_price"
    )
    invalidation = _positive_decimal(
        plan["invalidation_price"], "wait_counterfactual.invalidation_price"
    )
    target = _positive_decimal(plan["target_price"], "wait_counterfactual.target_price")
    _positive_integer(plan["quantity"], "wait_counterfactual.quantity")
    _positive_decimal(
        plan["contract_multiplier"], "wait_counterfactual.contract_multiplier"
    )
    evaluation_start = _parse_time(
        plan["evaluation_start_at"], "wait_counterfactual.evaluation_start_at"
    )
    evaluation_end = _parse_time(
        plan["evaluation_end_at"], "wait_counterfactual.evaluation_end_at"
    )
    if evaluation_start < recorded or evaluation_end <= evaluation_start:
        raise DecisionLedgerValidationError(
            "wait counterfactual must freeze a forward start and later evaluation end"
        )
    if plan["entry_rule"] != "next_bar_open_after_close_trigger":
        raise DecisionLedgerValidationError(
            "wait_counterfactual.entry_rule must be next_bar_open_after_close_trigger"
        )
    if plan["exit_rule"] != "first_touch_invalidation_or_target_else_evaluation_end":
        raise DecisionLedgerValidationError(
            "wait_counterfactual.exit_rule must be the deterministic first-touch rule"
        )
    if plan["bar_interval"] not in {"1m", "5m", "15m", "30m", "60m"}:
        raise DecisionLedgerValidationError("wait_counterfactual.bar_interval is unsupported")
    _validate_price_geometry(
        plan["direction"], trigger_price, invalidation, target, "wait_counterfactual"
    )


def _validate_decision_payload(
    payload: Any,
    *,
    recorded: datetime,
    policy: dict[str, Any],
    artifact_root: Path,
) -> None:
    decision = _require_exact_fields(
        payload,
        {
            "decision_id",
            "observed_at",
            "report_evidence",
            "report_policy_evidence",
            "analysis_contract",
            "main_hypothesis",
            "objective",
            "expected_net_value",
            "agent_judgment_sha256",
            "directional_view",
            "action",
            "confidence_range",
            "cost_assumptions",
            "trade_plan",
            "wait_counterfactual",
            "reassess_by",
        },
        "decision payload",
    )
    if not isinstance(decision["decision_id"], str) or ID_RE.fullmatch(decision["decision_id"]) is None:
        raise DecisionLedgerValidationError("decision_id has an invalid format")
    observed = _parse_time(decision["observed_at"], "observed_at")
    skew = timedelta(seconds=int(policy["validation"]["maximum_future_clock_skew_seconds"]))
    if observed > recorded + skew:
        raise DecisionLedgerValidationError("observed_at cannot be after decision freeze time")
    if not isinstance(decision["analysis_contract"], str) or CONTRACT_RE.fullmatch(decision["analysis_contract"]) is None:
        raise DecisionLedgerValidationError("analysis_contract must be a canonical month contract")
    _require_text(decision["main_hypothesis"], "main_hypothesis")
    if decision["objective"] != PROFIT_OBJECTIVE:
        raise DecisionLedgerValidationError(
            "objective must be long_term_verifiable_net_profit_after_costs"
        )
    if decision["expected_net_value"] not in {"positive", "negative", "uncertain"}:
        raise DecisionLedgerValidationError("expected_net_value is not allowed")
    if (
        not isinstance(decision["agent_judgment_sha256"], str)
        or HEX64_RE.fullmatch(decision["agent_judgment_sha256"]) is None
    ):
        raise DecisionLedgerValidationError(
            "agent_judgment_sha256 must be lowercase SHA-256"
        )
    if decision["directional_view"] not in set(policy["allowed_directional_views"]):
        raise DecisionLedgerValidationError("directional_view is not allowed")
    if decision["action"] not in set(policy["allowed_actions"]):
        raise DecisionLedgerValidationError("action is not allowed")
    confidence = _require_exact_fields(
        decision["confidence_range"], {"low", "high"}, "confidence_range"
    )
    low = _decimal(confidence["low"], "confidence_range.low")
    high = _decimal(confidence["high"], "confidence_range.high")
    minimum = Decimal(policy["confidence_range"]["minimum"])
    maximum = Decimal(policy["confidence_range"]["maximum"])
    if not minimum <= low <= high <= maximum:
        raise DecisionLedgerValidationError("confidence_range must satisfy 0 <= low <= high <= 1")
    costs = _validate_cost_assumptions(
        decision["cost_assumptions"],
        field="cost_assumptions",
        require_complete=decision["action"] in {"long", "short"},
    )
    if costs is not None and not policy["execution_policy"]["allow_rollover"] and costs["roll"] != 0:
        raise DecisionLedgerValidationError("roll cost assumption must be zero: rollover is disabled")
    reassess_by = _parse_time(decision["reassess_by"], "reassess_by")
    if reassess_by <= recorded:
        raise DecisionLedgerValidationError("reassess_by must be after decision freeze time")

    if decision["action"] in {"long", "short"}:
        if decision["wait_counterfactual"] is not None:
            raise DecisionLedgerValidationError("trade decision cannot contain wait_counterfactual")
        _validate_trade_plan(decision["trade_plan"], decision["action"], recorded)
        latest_entry = _parse_time(
            decision["trade_plan"]["latest_entry_at"], "trade_plan.latest_entry_at"
        )
        if latest_entry > reassess_by:
            raise DecisionLedgerValidationError("latest_entry_at cannot exceed reassess_by")
    else:
        if decision["trade_plan"] is not None:
            raise DecisionLedgerValidationError("wait decision cannot contain trade_plan")
        _validate_wait_plan(decision["wait_counterfactual"], recorded)
        if decision["wait_counterfactual"]["status"] == "defined":
            if costs is None:
                raise DecisionLedgerValidationError(
                    "defined wait counterfactual requires complete frozen costs"
                )
            evaluation_end = _parse_time(
                decision["wait_counterfactual"]["evaluation_end_at"],
                "wait_counterfactual.evaluation_end_at",
            )
            if evaluation_end > reassess_by:
                raise DecisionLedgerValidationError(
                    "wait counterfactual evaluation cannot exceed reassess_by"
                )
        elif costs is not None:
            # A wait may be unavailable because structure is missing even when
            # costs happen to be known; that remains an honest unscorable wait.
            pass

    report_artifact, report_path = _load_artifact(
        decision["report_evidence"],
        artifact_root=artifact_root,
        expected_schema="futures_main_report.v3",
        field="report_evidence",
    )
    report_policy, report_policy_path = _load_artifact(
        decision["report_policy_evidence"],
        artifact_root=artifact_root,
        expected_schema="futures_main_report_policy.v3",
        field="report_policy_evidence",
    )
    assessment = _replay_futures_report(
        report_artifact,
        report_policy,
        recorded=recorded,
        report_path=report_path,
        policy_path=report_policy_path,
    )
    _bind_decision_to_report(decision, report_artifact, assessment)


def _validate_roll(value: Any, policy: dict[str, Any], field: str) -> Decimal:
    roll = _require_exact_fields(value, {"occurred", "cost_cny", "details"}, field)
    if not isinstance(roll["occurred"], bool):
        raise DecisionLedgerValidationError(f"{field}.occurred must be boolean")
    cost = _decimal(roll["cost_cny"], f"{field}.cost_cny", nonnegative=True)
    if not isinstance(roll["details"], list):
        raise DecisionLedgerValidationError(f"{field}.details must be a list")
    if not policy["execution_policy"]["allow_rollover"]:
        if roll["occurred"] or cost != 0 or roll["details"]:
            raise DecisionLedgerValidationError(
                f"{field} must record no rollover under the one-entry/full-exit policy"
            )
    return cost


def _validate_fill(value: Any, field: str) -> tuple[datetime, Decimal, int, str]:
    fill = _require_exact_fields(value, {"at", "price", "quantity", "side"}, field)
    when = _parse_time(fill["at"], f"{field}.at")
    price = _positive_decimal(fill["price"], f"{field}.price")
    quantity = _positive_integer(fill["quantity"], f"{field}.quantity")
    if fill["side"] not in {"buy", "sell"}:
        raise DecisionLedgerValidationError(f"{field}.side must be buy or sell")
    return when, price, quantity, fill["side"]


def _require_equal_decimal(reported: Any, expected: Decimal, field: str) -> None:
    actual = _decimal(reported, field)
    if actual != expected:
        raise DecisionLedgerValidationError(
            f"{field} arithmetic mismatch: reported={reported}, expected={_money_text(expected)}"
        )


def _pnl(direction: str, entry: Decimal, exit_price: Decimal, quantity: int, multiplier: Decimal) -> Decimal:
    difference = exit_price - entry if direction == "long" else entry - exit_price
    return difference * Decimal(quantity) * multiplier


def _decision_due_at(decision_event: dict[str, Any]) -> datetime:
    decision = decision_event["payload"]
    reassess = _parse_time(decision["reassess_by"], "reassess_by")
    if decision["action"] in {"long", "short"}:
        latest_entry = _parse_time(
            decision["trade_plan"]["latest_entry_at"], "trade_plan.latest_entry_at"
        )
        return min(latest_entry, reassess)
    counterfactual = decision["wait_counterfactual"]
    if counterfactual["status"] == "defined":
        evaluation_end = _parse_time(
            counterfactual["evaluation_end_at"], "wait_counterfactual.evaluation_end_at"
        )
        return min(evaluation_end, reassess)
    return reassess


def _validate_actual_outcome(
    outcome: dict[str, Any],
    *,
    decision_event: dict[str, Any],
    recorded: datetime,
    observed: datetime,
    policy: dict[str, Any],
    artifact_root: Path,
) -> None:
    expected_fields = {
        "decision_id",
        "decision_hash",
        "outcome_observed_at",
        "result_kind",
        "entry",
        "exit",
        "contract_multiplier",
        "costs_complete",
        "commission_cny",
        "slippage_cny",
        "slippage_accounting",
        "roll",
        "gross_pnl_cny",
        "net_pnl_cny",
        "mfe_cny",
        "mae_cny",
        "execution_evidence",
    }
    _require_exact_fields(outcome, expected_fields, "actual outcome payload")
    decision = decision_event["payload"]
    if decision["action"] not in {"long", "short"}:
        raise DecisionLedgerValidationError("actual_trade can only close a long or short decision")
    if outcome["costs_complete"] is not True:
        raise DecisionLedgerValidationError("actual outcome costs_complete must be true (fail closed)")
    entry_at, entry_price, entry_quantity, entry_side = _validate_fill(outcome["entry"], "entry")
    exit_at, exit_price, exit_quantity, exit_side = _validate_fill(outcome["exit"], "exit")
    expected_entry_side = "buy" if decision["action"] == "long" else "sell"
    expected_exit_side = "sell" if decision["action"] == "long" else "buy"
    if (entry_side, exit_side) != (expected_entry_side, expected_exit_side):
        raise DecisionLedgerValidationError("entry/exit sides do not match frozen action")
    if entry_at < _parse_time(decision_event["recorded_at"], "decision.recorded_at"):
        raise DecisionLedgerValidationError("entry cannot predate the frozen decision")
    if entry_at > _decision_due_at(decision_event):
        raise DecisionLedgerValidationError(
            "entry occurred after the earlier of latest_entry_at and reassess_by"
        )
    if not entry_at <= exit_at <= observed <= recorded:
        raise DecisionLedgerValidationError("actual outcome timestamps are not forward ordered")
    planned_quantity = _positive_integer(decision["trade_plan"]["quantity"], "trade_plan.quantity")
    if entry_quantity != planned_quantity or exit_quantity != entry_quantity:
        raise DecisionLedgerValidationError("execution must be one planned entry and one full exit")
    multiplier = _positive_decimal(outcome["contract_multiplier"], "contract_multiplier")
    planned_multiplier = _positive_decimal(
        decision["trade_plan"]["contract_multiplier"], "trade_plan.contract_multiplier"
    )
    if multiplier != planned_multiplier:
        raise DecisionLedgerValidationError("contract_multiplier differs from frozen plan")
    commission = _decimal(outcome["commission_cny"], "commission_cny", nonnegative=True)
    slippage = _decimal(outcome["slippage_cny"], "slippage_cny", nonnegative=True)
    if outcome["slippage_accounting"] != "embedded_in_actual_fill_prices":
        raise DecisionLedgerValidationError(
            "actual slippage_accounting must be embedded_in_actual_fill_prices"
        )
    roll = _validate_roll(outcome["roll"], policy, "roll")
    gross = _pnl(decision["action"], entry_price, exit_price, entry_quantity, multiplier)
    # Actual fill prices already include execution slippage.  Subtracting the
    # diagnostic slippage amount again would understate realized PnL.
    net = gross - commission - roll
    _require_equal_decimal(outcome["gross_pnl_cny"], gross, "gross_pnl_cny")
    _require_equal_decimal(outcome["net_pnl_cny"], net, "net_pnl_cny")
    _decimal(outcome["mfe_cny"], "mfe_cny", nonnegative=True)
    _decimal(outcome["mae_cny"], "mae_cny", nonnegative=True)
    execution_artifact, _ = _load_artifact(
        outcome["execution_evidence"],
        artifact_root=artifact_root,
        expected_schema="decision_ledger_execution_evidence.v1",
        field="execution_evidence",
    )
    _require_exact_fields(
        execution_artifact,
        {"schema_version", "outcome"},
        "execution evidence artifact",
    )
    outcome_core = {
        **{key: value for key, value in outcome.items() if key != "execution_evidence"},
        "analysis_contract": decision["analysis_contract"],
    }
    _require_exact_fields(
        execution_artifact["outcome"],
        set(outcome_core),
        "execution evidence artifact.outcome",
    )
    if execution_artifact["outcome"] != outcome_core:
        raise DecisionLedgerValidationError(
            "execution evidence artifact does not exactly bind fills, costs and PnL"
        )


def _load_wait_bars(
    descriptor: Any,
    *,
    artifact_root: Path,
    decision_event: dict[str, Any],
    observed: datetime,
) -> list[dict[str, Any]]:
    artifact, _ = _load_artifact(
        descriptor,
        artifact_root=artifact_root,
        expected_schema="decision_ledger_wait_bars_evidence.v1",
        field="market_evidence",
    )
    _require_exact_fields(
        artifact,
        {"schema_version", "decision_id", "analysis_contract", "bar_interval", "bars"},
        "wait bars evidence artifact",
    )
    decision = decision_event["payload"]
    plan = decision["wait_counterfactual"]
    if artifact["decision_id"] != decision["decision_id"]:
        raise DecisionLedgerValidationError("wait bars decision_id mismatch")
    if artifact["analysis_contract"] != decision["analysis_contract"]:
        raise DecisionLedgerValidationError("wait bars analysis_contract mismatch")
    if artifact["bar_interval"] != plan["bar_interval"]:
        raise DecisionLedgerValidationError("wait bars interval differs from frozen plan")
    bars_value = artifact["bars"]
    if not isinstance(bars_value, list) or not bars_value:
        raise DecisionLedgerValidationError("wait bars evidence must contain at least one bar")
    decision_recorded = _parse_time(decision_event["recorded_at"], "decision.recorded_at")
    planned_start = _parse_time(
        plan["evaluation_start_at"], "wait_counterfactual.evaluation_start_at"
    )
    planned_end = _parse_time(plan["evaluation_end_at"], "wait_counterfactual.evaluation_end_at")
    parsed: list[dict[str, Any]] = []
    previous_end: datetime | None = None
    interval_seconds = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "60m": 3600,
    }.get(plan["bar_interval"])
    for index, value in enumerate(bars_value):
        bar = _require_exact_fields(
            value,
            {"start_at", "end_at", "open", "high", "low", "close"},
            f"wait bars[{index}]",
        )
        start = _parse_time(bar["start_at"], f"wait bars[{index}].start_at")
        end = _parse_time(bar["end_at"], f"wait bars[{index}].end_at")
        if not start < end:
            raise DecisionLedgerValidationError("wait bar start must precede end")
        if interval_seconds is not None and (end - start).total_seconds() != interval_seconds:
            raise DecisionLedgerValidationError("wait bar duration differs from declared interval")
        if start < decision_recorded:
            raise DecisionLedgerValidationError("wait bars cannot include pre-decision price paths")
        if previous_end is not None and start != previous_end:
            raise DecisionLedgerValidationError(
                "wait bars must be contiguous without overlap or omitted intervals"
            )
        if end > observed:
            raise DecisionLedgerValidationError("wait bars contain future data")
        open_price = _positive_decimal(bar["open"], f"wait bars[{index}].open")
        high = _positive_decimal(bar["high"], f"wait bars[{index}].high")
        low = _positive_decimal(bar["low"], f"wait bars[{index}].low")
        close = _positive_decimal(bar["close"], f"wait bars[{index}].close")
        if low > min(open_price, close) or high < max(open_price, close) or low > high:
            raise DecisionLedgerValidationError("wait bar OHLC ordering is impossible")
        parsed.append(
            {
                "start": start,
                "end": end,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
            }
        )
        previous_end = end
    if parsed[0]["start"] != planned_start or parsed[-1]["end"] != planned_end:
        raise DecisionLedgerValidationError(
            "wait bars must cover the exact frozen evaluation interval"
        )
    return parsed


def _replay_wait_path(
    bars: list[dict[str, Any]],
    *,
    plan: dict[str, Any],
) -> dict[str, Any]:
    direction = plan["direction"]
    trigger_price = _positive_decimal(plan["trigger_price"], "wait_counterfactual.trigger_price")
    invalidation = _positive_decimal(
        plan["invalidation_price"], "wait_counterfactual.invalidation_price"
    )
    target = _positive_decimal(plan["target_price"], "wait_counterfactual.target_price")
    trigger_index: int | None = None
    for index, bar in enumerate(bars):
        triggered = bar["close"] >= trigger_price if direction == "long" else bar["close"] <= trigger_price
        if triggered:
            trigger_index = index
            break
    if trigger_index is None or trigger_index + 1 >= len(bars):
        return {
            "status": "scorable",
            "triggered": False,
            "evaluation_at": bars[-1]["end"],
            "evaluation_price": bars[-1]["close"],
            "exit_reason": "not_triggered_evaluation_mark",
            "mfe": Decimal(0),
            "mae": Decimal(0),
        }

    entry_index = trigger_index + 1
    entry_bar = bars[entry_index]
    entry_price = entry_bar["open"]
    entry_inside_bounds = (
        invalidation < entry_price < target
        if direction == "long"
        else target < entry_price < invalidation
    )
    if not entry_inside_bounds:
        return {
            "status": "ambiguous",
            "reason": "next_bar_open_gapped_outside_frozen_invalidation_target_bounds",
        }

    exit_index = len(bars) - 1
    exit_reason = "evaluation_end"
    exit_price = bars[-1]["close"]
    for index in range(entry_index, len(bars)):
        bar = bars[index]
        open_inside_bounds = (
            invalidation < bar["open"] < target
            if direction == "long"
            else target < bar["open"] < invalidation
        )
        if not open_inside_bounds:
            return {
                "status": "ambiguous",
                "reason": "active_bar_open_gapped_outside_frozen_invalidation_target_bounds",
            }
        if direction == "long":
            target_touched = bar["high"] >= target
            invalidation_touched = bar["low"] <= invalidation
        else:
            target_touched = bar["low"] <= target
            invalidation_touched = bar["high"] >= invalidation
        if target_touched and invalidation_touched:
            return {
                "status": "ambiguous",
                "reason": "same_bar_target_and_invalidation_order_unobservable",
            }
        if target_touched or invalidation_touched:
            exit_index = index
            exit_reason = "target" if target_touched else "invalidation"
            exit_price = target if target_touched else invalidation
            break

    active = bars[entry_index : exit_index + 1]
    if direction == "long":
        favorable_points = min(max(bar["high"] for bar in active), target) - entry_price
        adverse_points = entry_price - max(min(bar["low"] for bar in active), invalidation)
    else:
        favorable_points = entry_price - max(min(bar["low"] for bar in active), target)
        adverse_points = min(max(bar["high"] for bar in active), invalidation) - entry_price
    favorable_points = max(Decimal(0), favorable_points)
    adverse_points = max(Decimal(0), adverse_points)
    quantity = _positive_integer(plan["quantity"], "wait_counterfactual.quantity")
    multiplier = _positive_decimal(
        plan["contract_multiplier"], "wait_counterfactual.contract_multiplier"
    )
    return {
        "status": "scorable",
        "triggered": True,
        "entry_at": entry_bar["start"],
        "entry_price": entry_price,
        "evaluation_at": bars[exit_index]["end"],
        "evaluation_price": exit_price,
        "exit_reason": exit_reason,
        "mfe": favorable_points * Decimal(quantity) * multiplier,
        "mae": adverse_points * Decimal(quantity) * multiplier,
    }


def _validate_wait_outcome(
    outcome: dict[str, Any],
    *,
    decision_event: dict[str, Any],
    recorded: datetime,
    observed: datetime,
    policy: dict[str, Any],
    artifact_root: Path,
) -> None:
    expected_fields = {
        "decision_id",
        "decision_hash",
        "outcome_observed_at",
        "result_kind",
        "triggered",
        "entry",
        "evaluation_exit",
        "commission_cny",
        "slippage_cny",
        "roll",
        "gross_pnl_cny",
        "net_pnl_cny",
        "mfe_cny",
        "mae_cny",
        "market_evidence",
    }
    _require_exact_fields(outcome, expected_fields, "wait outcome payload")
    decision = decision_event["payload"]
    if decision["action"] != "wait" or decision["wait_counterfactual"]["status"] != "defined":
        raise DecisionLedgerValidationError(
            "wait_counterfactual outcome requires a frozen defined wait"
        )
    bars = _load_wait_bars(
        outcome["market_evidence"],
        artifact_root=artifact_root,
        decision_event=decision_event,
        observed=observed,
    )
    replay = _replay_wait_path(bars, plan=decision["wait_counterfactual"])
    if replay["status"] == "ambiguous":
        raise DecisionLedgerValidationError(
            f"wait path is unscorable: {replay['reason']}; append wait_unscorable"
        )
    if outcome["triggered"] is not replay["triggered"]:
        raise DecisionLedgerValidationError("wait triggered flag differs from replayed bars")
    evaluation = _require_exact_fields(
        outcome["evaluation_exit"], {"at", "price", "reason"}, "evaluation_exit"
    )
    evaluation_at = _parse_time(evaluation["at"], "evaluation_exit.at")
    evaluation_price = _positive_decimal(evaluation["price"], "evaluation_exit.price")
    if (
        evaluation_at != replay["evaluation_at"]
        or evaluation_price != replay["evaluation_price"]
        or evaluation["reason"] != replay["exit_reason"]
    ):
        raise DecisionLedgerValidationError("wait exit differs from replayed market bars")
    if not evaluation_at <= observed <= recorded:
        raise DecisionLedgerValidationError("wait outcome timestamps are not forward ordered")
    roll = _validate_roll(outcome["roll"], policy, "roll")
    commission = _decimal(outcome["commission_cny"], "commission_cny", nonnegative=True)
    slippage = _decimal(outcome["slippage_cny"], "slippage_cny", nonnegative=True)
    mfe = _decimal(outcome["mfe_cny"], "mfe_cny", nonnegative=True)
    mae = _decimal(outcome["mae_cny"], "mae_cny", nonnegative=True)
    if mfe != replay["mfe"] or mae != replay["mae"]:
        raise DecisionLedgerValidationError("wait MFE/MAE differs from replayed market bars")

    if not replay["triggered"]:
        if outcome["entry"] is not None:
            raise DecisionLedgerValidationError("untriggered wait counterfactual cannot have entry")
        if any(value != 0 for value in (commission, slippage, roll, mfe, mae)):
            raise DecisionLedgerValidationError(
                "untriggered wait counterfactual must have zero costs/MFE/MAE"
            )
        _require_equal_decimal(outcome["gross_pnl_cny"], Decimal(0), "gross_pnl_cny")
        _require_equal_decimal(outcome["net_pnl_cny"], Decimal(0), "net_pnl_cny")
        return

    entry_at, entry_price, quantity, side = _validate_fill(outcome["entry"], "entry")
    direction = decision["wait_counterfactual"]["direction"]
    expected_side = "buy" if direction == "long" else "sell"
    if (
        entry_at != replay["entry_at"]
        or entry_price != replay["entry_price"]
        or side != expected_side
    ):
        raise DecisionLedgerValidationError("wait entry differs from replayed next-bar open")
    planned_quantity = _positive_integer(
        decision["wait_counterfactual"]["quantity"], "wait_counterfactual.quantity"
    )
    if quantity != planned_quantity:
        raise DecisionLedgerValidationError("wait quantity differs from frozen counterfactual")
    assumed = _validate_cost_assumptions(
        decision["cost_assumptions"],
        field="cost_assumptions",
        require_complete=True,
    )
    assert assumed is not None
    if commission != assumed["commission"] or slippage != assumed["slippage"] or roll != assumed["roll"]:
        raise DecisionLedgerValidationError("wait costs differ from frozen assumptions")
    multiplier = _positive_decimal(
        decision["wait_counterfactual"]["contract_multiplier"],
        "wait_counterfactual.contract_multiplier",
    )
    gross = _pnl(direction, entry_price, evaluation_price, quantity, multiplier)
    net = gross - commission - slippage - roll
    _require_equal_decimal(outcome["gross_pnl_cny"], gross, "gross_pnl_cny")
    _require_equal_decimal(outcome["net_pnl_cny"], net, "net_pnl_cny")


def _validate_unscorable_wait_outcome(
    outcome: dict[str, Any],
    *,
    decision_event: dict[str, Any],
    observed: datetime,
    artifact_root: Path,
) -> None:
    _require_exact_fields(
        outcome,
        {
            "decision_id",
            "decision_hash",
            "outcome_observed_at",
            "result_kind",
            "reason",
            "missing_evidence",
            "market_evidence",
        },
        "unscorable wait outcome payload",
    )
    decision = decision_event["payload"]
    if decision["action"] != "wait":
        raise DecisionLedgerValidationError("wait_unscorable can only close a wait decision")
    _require_text(outcome["reason"], "reason", maximum=1000)
    if not isinstance(outcome["missing_evidence"], list) or not outcome["missing_evidence"]:
        raise DecisionLedgerValidationError("missing_evidence must be a non-empty text list")
    if any(
        not isinstance(item, str) or not item.strip()
        for item in outcome["missing_evidence"]
    ):
        raise DecisionLedgerValidationError("missing_evidence entries must be non-empty text")
    if decision["wait_counterfactual"]["status"] == "defined":
        bars = _load_wait_bars(
            outcome["market_evidence"],
            artifact_root=artifact_root,
            decision_event=decision_event,
            observed=observed,
        )
        replay = _replay_wait_path(bars, plan=decision["wait_counterfactual"])
        if replay["status"] != "ambiguous":
            raise DecisionLedgerValidationError(
                "defined wait may be unscorable only when market-bar replay is ambiguous"
            )
        expected_missing = (
            ["intrabar_trade_path"]
            if replay["reason"] == "same_bar_target_and_invalidation_order_unobservable"
            else ["gap_execution_price"]
        )
        if outcome["reason"] != replay["reason"] or outcome["missing_evidence"] != expected_missing:
            raise DecisionLedgerValidationError(
                "wait_unscorable reason must equal the replay-derived ambiguity"
            )
        return

    artifact, _ = _load_artifact(
        outcome["market_evidence"],
        artifact_root=artifact_root,
        expected_schema="decision_ledger_unscorable_evidence.v1",
        field="market_evidence",
    )
    _require_exact_fields(
        artifact,
        {"schema_version", "outcome"},
        "unscorable evidence artifact",
    )
    outcome_core = {
        **{key: value for key, value in outcome.items() if key != "market_evidence"},
        "analysis_contract": decision["analysis_contract"],
    }
    _require_exact_fields(
        artifact["outcome"],
        set(outcome_core),
        "unscorable evidence artifact.outcome",
    )
    if artifact["outcome"] != outcome_core:
        raise DecisionLedgerValidationError(
            "unscorable evidence artifact does not bind the outcome"
        )


def _validate_trade_not_entered_outcome(
    outcome: dict[str, Any],
    *,
    decision_event: dict[str, Any],
    observed: datetime,
    artifact_root: Path,
) -> None:
    _require_exact_fields(
        outcome,
        {
            "decision_id",
            "decision_hash",
            "outcome_observed_at",
            "result_kind",
            "reason",
            "no_fill_evidence",
        },
        "trade-not-entered outcome payload",
    )
    decision = decision_event["payload"]
    if decision["action"] not in {"long", "short"}:
        raise DecisionLedgerValidationError(
            "trade_not_entered can only close a long or short decision"
        )
    if observed < _decision_due_at(decision_event):
        raise DecisionLedgerValidationError(
            "trade_not_entered cannot close before latest_entry_at/reassess_by is due"
        )
    _require_text(outcome["reason"], "reason", maximum=1000)
    artifact, _ = _load_artifact(
        outcome["no_fill_evidence"],
        artifact_root=artifact_root,
        expected_schema="decision_ledger_no_fill_evidence.v1",
        field="no_fill_evidence",
    )
    _require_exact_fields(
        artifact,
        {"schema_version", "outcome", "order_status", "entry_fills"},
        "no-fill evidence artifact",
    )
    if artifact["order_status"] != "not_filled" or artifact["entry_fills"] != []:
        raise DecisionLedgerValidationError(
            "no-fill evidence must contain order_status=not_filled and zero entry fills"
        )
    outcome_core = {
        **{key: value for key, value in outcome.items() if key != "no_fill_evidence"},
        "analysis_contract": decision["analysis_contract"],
    }
    _require_exact_fields(
        artifact["outcome"],
        set(outcome_core),
        "no-fill evidence artifact.outcome",
    )
    if artifact["outcome"] != outcome_core:
        raise DecisionLedgerValidationError("no-fill evidence artifact does not bind outcome")


def _validate_outcome_payload(
    payload: Any,
    *,
    decision_event: dict[str, Any],
    recorded: datetime,
    policy: dict[str, Any],
    artifact_root: Path,
) -> None:
    if not isinstance(payload, dict):
        raise DecisionLedgerValidationError("outcome payload must be an object")
    if payload.get("decision_id") != decision_event["payload"]["decision_id"]:
        raise DecisionLedgerValidationError("outcome decision_id does not match referenced decision")
    if payload.get("decision_hash") != decision_event["event_hash"]:
        raise DecisionLedgerValidationError("outcome must reference the frozen decision hash")
    observed = _parse_time(payload.get("outcome_observed_at"), "outcome_observed_at")
    decision_recorded = _parse_time(decision_event["recorded_at"], "decision.recorded_at")
    if observed < decision_recorded or observed > recorded:
        raise DecisionLedgerValidationError("outcome_observed_at is not forward ordered")
    result_kind = payload.get("result_kind")
    if result_kind == "actual_trade":
        _validate_actual_outcome(
            payload,
            decision_event=decision_event,
            recorded=recorded,
            observed=observed,
            policy=policy,
            artifact_root=artifact_root,
        )
    elif result_kind == "wait_counterfactual":
        _validate_wait_outcome(
            payload,
            decision_event=decision_event,
            recorded=recorded,
            observed=observed,
            policy=policy,
            artifact_root=artifact_root,
        )
    elif result_kind == "wait_unscorable":
        _validate_unscorable_wait_outcome(
            payload,
            decision_event=decision_event,
            observed=observed,
            artifact_root=artifact_root,
        )
    elif result_kind == "trade_not_entered":
        _validate_trade_not_entered_outcome(
            payload,
            decision_event=decision_event,
            observed=observed,
            artifact_root=artifact_root,
        )
    else:
        raise DecisionLedgerValidationError(
            "result_kind must be actual_trade, trade_not_entered, wait_counterfactual or wait_unscorable"
        )


def _event_hash(event_without_hash: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_bytes(event_without_hash))


def _head_document(
    ledger_bytes: bytes,
    *,
    event_count: int,
    last_event_hash: str,
    policy_sha256: str,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": "decision_ledger_head.v1",
        "event_count": event_count,
        "last_event_hash": last_event_hash,
        "ledger_sha256": _sha256_bytes(ledger_bytes),
        "policy_sha256": policy_sha256,
        "updated_at": updated_at,
    }


def _write_head(
    ledger_path: Path,
    ledger_bytes: bytes,
    *,
    event_count: int,
    last_event_hash: str,
    policy_sha256: str,
    updated_at: str,
) -> None:
    head = _head_document(
        ledger_bytes,
        event_count=event_count,
        last_event_hash=last_event_hash,
        policy_sha256=policy_sha256,
        updated_at=updated_at,
    )
    _atomic_replace(_head_path(ledger_path), _canonical_bytes(head) + b"\n")


def _initialize_unlocked(ledger_path: Path, policy_sha256: str, now: datetime) -> None:
    head_path = _head_path(ledger_path)
    if ledger_path.exists() or head_path.exists():
        raise DecisionLedgerError(f"ledger or head checkpoint already exists for {ledger_path}")
    _atomic_replace(ledger_path, b"")
    _write_head(
        ledger_path,
        b"",
        event_count=0,
        last_event_hash="0" * 64,
        policy_sha256=policy_sha256,
        updated_at=_iso(now),
    )


def init_ledger(ledger_path: str | Path) -> dict[str, Any]:
    path = Path(ledger_path).expanduser().resolve()
    _, policy_sha256 = _load_policy()
    with _exclusive_lock(path):
        _initialize_unlocked(path, policy_sha256, _current_time())
    return {
        "status": "initialized",
        "ledger_path": str(path),
        "head_path": str(_head_path(path)),
        "event_count": 0,
        "last_event_hash": "0" * 64,
    }


def _read_ledger_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DecisionLedgerValidationError(f"cannot read ledger {path}: {exc}") from exc


def _parse_events(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecisionLedgerValidationError("ledger is not UTF-8") from exc
    if text and not text.endswith("\n"):
        raise DecisionLedgerValidationError("ledger ends with a partial event")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise DecisionLedgerValidationError(f"blank ledger line at {line_number}")
        event = _json_loads(line, label=f"event JSON at line {line_number}")
        if not isinstance(event, dict):
            raise DecisionLedgerValidationError(f"event at line {line_number} is not an object")
        events.append(event)
    return events


def _validate_head(
    ledger_path: Path,
    ledger_bytes: bytes,
    events: list[dict[str, Any]],
    policy_sha256: str,
) -> None:
    head_path = _head_path(ledger_path)
    try:
        head = _json_loads(head_path.read_text(encoding="utf-8"), label="head checkpoint")
    except OSError as exc:
        raise DecisionLedgerValidationError(f"missing head checkpoint {head_path}") from exc
    expected_fields = {
        "schema_version",
        "event_count",
        "last_event_hash",
        "ledger_sha256",
        "policy_sha256",
        "updated_at",
    }
    _require_exact_fields(head, expected_fields, "head checkpoint")
    if head["schema_version"] != "decision_ledger_head.v1":
        raise DecisionLedgerValidationError("unsupported head checkpoint schema")
    expected_last = events[-1].get("event_hash") if events else "0" * 64
    if type(head["event_count"]) is not int or head["event_count"] != len(events):
        raise DecisionLedgerValidationError("head checkpoint detects event deletion/addition")
    if head["last_event_hash"] != expected_last:
        raise DecisionLedgerValidationError("head checkpoint does not match ledger head")
    if head["ledger_sha256"] != _sha256_bytes(ledger_bytes):
        raise DecisionLedgerValidationError("head checkpoint detects ledger byte mutation/reordering")
    if head["policy_sha256"] != policy_sha256:
        raise DecisionLedgerValidationError("head checkpoint policy hash differs from current policy")
    _parse_time(head["updated_at"], "head.updated_at")


def _load_and_validate(
    ledger_path: Path,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]], bytes, str]:
    policy, policy_sha256 = _load_policy()
    raw = _read_ledger_bytes(ledger_path)
    events = _parse_events(raw)
    _validate_head(ledger_path, raw, events, policy_sha256)
    current = (now or _current_time()).astimezone(timezone.utc)
    future_skew = timedelta(seconds=int(policy["validation"]["maximum_future_clock_skew_seconds"]))
    expected_previous = "0" * 64
    decisions: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    previous_recorded: datetime | None = None

    event_fields = {
        "schema_version",
        "event_type",
        "sequence",
        "recorded_at",
        "policy_sha256",
        "previous_event_hash",
        "payload",
        "event_hash",
    }
    for index, event in enumerate(events, start=1):
        _require_exact_fields(event, event_fields, f"event {index}")
        if event["schema_version"] != policy["event_schema_version"]:
            raise DecisionLedgerValidationError(f"event {index} schema is unsupported")
        if event["event_type"] not in {"decision", "outcome"}:
            raise DecisionLedgerValidationError(f"event {index} type is unsupported")
        if type(event["sequence"]) is not int or event["sequence"] != index:
            raise DecisionLedgerValidationError(f"event {index} sequence is not contiguous")
        if event["policy_sha256"] != policy_sha256:
            raise DecisionLedgerValidationError(f"event {index} policy hash mismatch")
        if event["previous_event_hash"] != expected_previous:
            raise DecisionLedgerValidationError(f"event {index} hash chain is broken")
        if not isinstance(event["event_hash"], str) or HEX64_RE.fullmatch(event["event_hash"]) is None:
            raise DecisionLedgerValidationError(f"event {index} hash is invalid")
        without_hash = {key: value for key, value in event.items() if key != "event_hash"}
        calculated = _event_hash(without_hash)
        if event["event_hash"] != calculated:
            raise DecisionLedgerValidationError(f"event {index} content hash mismatch")
        recorded = _parse_time(event["recorded_at"], f"event {index}.recorded_at")
        if recorded > current + future_skew:
            raise DecisionLedgerValidationError(f"event {index} is recorded in the future")
        if previous_recorded is not None and recorded < previous_recorded:
            raise DecisionLedgerValidationError(f"event {index} recorded_at moves backward")

        if event["event_type"] == "decision":
            _validate_decision_payload(
                event["payload"],
                recorded=recorded,
                policy=policy,
                artifact_root=ledger_path.parent,
            )
            decision_id = event["payload"]["decision_id"]
            if decision_id in decisions:
                raise DecisionLedgerValidationError(f"duplicate decision_id: {decision_id}")
            decisions[decision_id] = event
        else:
            payload = event["payload"]
            if not isinstance(payload, dict) or not isinstance(payload.get("decision_id"), str):
                raise DecisionLedgerValidationError(f"event {index} outcome lacks decision_id")
            decision_id = payload["decision_id"]
            if decision_id not in decisions:
                raise DecisionLedgerValidationError(
                    f"outcome references unknown or later decision: {decision_id}"
                )
            if decision_id in outcomes:
                raise DecisionLedgerValidationError(f"decision closed more than once: {decision_id}")
            _validate_outcome_payload(
                payload,
                decision_event=decisions[decision_id],
                recorded=recorded,
                policy=policy,
                artifact_root=ledger_path.parent,
            )
            outcomes[decision_id] = event

        expected_previous = event["event_hash"]
        previous_recorded = recorded
    return events, decisions, outcomes, raw, policy_sha256


def _validated_snapshot(
    ledger_path: str | Path,
    *,
    now: datetime | None = None,
) -> tuple[
    dict[str, Any],
    tuple[
        list[dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        bytes,
        str,
    ] | None,
]:
    path = Path(ledger_path).expanduser().resolve()
    try:
        with _shared_lock(path):
            snapshot = _load_and_validate(path, now=now)
    except DecisionLedgerError as exc:
        return ({
            "schema_version": "decision_ledger_validation.v1",
            "valid": False,
            "validation_status": "invalid",
            "profit_verified": False,
            "errors": [str(exc)],
            "ledger_path": str(path),
        }, None)
    events, decisions, outcomes, raw, _ = snapshot
    return ({
        "schema_version": "decision_ledger_validation.v1",
        "valid": True,
        "validation_status": "valid_local_accounting_artifacts_replayed",
        "profit_verified": False,
        "evidence_assurance": "local_accounting_only_not_broker_exchange_or_worm_authenticated",
        "head_assurance": (
            "detects accidental change relative to the current separately stored local head; "
            "privileged rewrite of ledger and head together is not detectable"
        ),
        "claim_boundary": "valid local accounting does not establish positive expectancy or tamper-proof history",
        "ledger_path": str(path),
        "event_count": len(events),
        "decision_count": len(decisions),
        "outcome_count": len(outcomes),
        "replayed_local_artifact_count": (2 * len(decisions)) + len(outcomes),
        "artifact_replay_status": (
            "production_futures_v3_report_and_policy_plus_outcome_artifacts_reopened_and_bound"
        ),
        "open_decision_count": len(decisions) - len(outcomes),
        "last_event_hash": events[-1]["event_hash"] if events else "0" * 64,
        "ledger_sha256": _sha256_bytes(raw),
        "errors": [],
    }, snapshot)


def validate_ledger(
    ledger_path: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    validation, _ = _validated_snapshot(ledger_path, now=now)
    return validation


def _append_event(ledger_path: Path, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    policy, policy_sha256 = _load_policy()
    with _exclusive_lock(ledger_path):
        if not ledger_path.exists() or not _head_path(ledger_path).exists():
            raise DecisionLedgerValidationError(
                "ledger and head checkpoint must already exist; run init explicitly"
            )
        events, decisions, outcomes, raw, _ = _load_and_validate(ledger_path)
        recorded_now = _current_time()
        recorded = _parse_time(_iso(recorded_now), "recorded_at")
        if event_type == "decision":
            _validate_decision_payload(
                payload,
                recorded=recorded,
                policy=policy,
                artifact_root=ledger_path.parent,
            )
            decision_id = payload["decision_id"]
            if decision_id in decisions:
                raise DecisionLedgerValidationError(f"duplicate decision_id: {decision_id}")
        elif event_type == "outcome":
            decision_id = payload.get("decision_id") if isinstance(payload, dict) else None
            if decision_id not in decisions:
                raise DecisionLedgerValidationError("outcome references an unknown decision")
            if decision_id in outcomes:
                raise DecisionLedgerValidationError(f"decision already closed: {decision_id}")
            _validate_outcome_payload(
                payload,
                decision_event=decisions[decision_id],
                recorded=recorded,
                policy=policy,
                artifact_root=ledger_path.parent,
            )
        else:
            raise DecisionLedgerValidationError("event_type must be decision or outcome")

        event_without_hash = {
            "schema_version": policy["event_schema_version"],
            "event_type": event_type,
            "sequence": len(events) + 1,
            "recorded_at": _iso(recorded_now),
            "policy_sha256": policy_sha256,
            "previous_event_hash": events[-1]["event_hash"] if events else "0" * 64,
            "payload": payload,
        }
        event = {**event_without_hash, "event_hash": _event_hash(event_without_hash)}
        new_raw = raw + _canonical_bytes(event) + b"\n"
        _atomic_replace(ledger_path, new_raw)
        _write_head(
            ledger_path,
            new_raw,
            event_count=len(events) + 1,
            last_event_hash=event["event_hash"],
            policy_sha256=policy_sha256,
            updated_at=event["recorded_at"],
        )
    return event


def append_decision(ledger_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return _append_event(Path(ledger_path).expanduser().resolve(), "decision", payload)


def append_outcome(ledger_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return _append_event(Path(ledger_path).expanduser().resolve(), "outcome", payload)


def _section_summary(
    values: list[Decimal],
    *,
    decision_count: int,
    closed_count: int,
    minimum: int | None,
    label: str,
    resolved_without_sample_count: int = 0,
) -> dict[str, Any]:
    total = sum(values, Decimal(0))
    result = {
        "kind": label,
        "decision_count": decision_count,
        "closed_count": closed_count,
        "resolved_without_sample_count": resolved_without_sample_count,
        "open_count": decision_count - closed_count - resolved_without_sample_count,
        "total_net_pnl_cny": _money_text(total),
        "mean_net_pnl_cny": _money_text(total / Decimal(closed_count)) if closed_count else None,
        "positive_count": sum(value > 0 for value in values),
        "zero_count": sum(value == 0 for value in values),
        "negative_count": sum(value < 0 for value in values),
    }
    if minimum is None:
        result.update(
            {
                "minimum_closed_sample": None,
                "count_threshold_met": None,
                "sample_status": "descriptive_counterfactual_only",
            }
        )
    else:
        result.update(
            {
                "minimum_closed_sample": minimum,
                "count_threshold_met": closed_count >= minimum,
                "sample_status": (
                    "count_threshold_met" if closed_count >= minimum else "insufficient_sample"
                ),
            }
        )
    return result


def summarize_ledger(ledger_path: str | Path) -> dict[str, Any]:
    path = Path(ledger_path).expanduser().resolve()
    validation, snapshot = _validated_snapshot(path)
    if not validation["valid"]:
        return {
            "schema_version": "decision_ledger_summary.v1",
            "validation_status": "invalid_fail_closed",
            "profit_verified": False,
            "ledger_validation": validation,
            "actual": None,
            "wait_counterfactual": None,
        }

    assert snapshot is not None
    _, decisions, outcomes, _, _ = snapshot
    policy, _ = _load_policy()
    actual_decisions = [
        event for event in decisions.values() if event["payload"]["action"] in {"long", "short"}
    ]
    wait_decisions = [event for event in decisions.values() if event["payload"]["action"] == "wait"]
    defined_wait_decisions = [
        event
        for event in wait_decisions
        if event["payload"]["wait_counterfactual"]["status"] == "defined"
    ]
    unavailable_wait_decisions = [
        event
        for event in wait_decisions
        if event["payload"]["wait_counterfactual"]["status"] == "unavailable"
    ]
    actual_values: list[Decimal] = []
    wait_values: list[Decimal] = []
    wait_triggered = 0
    unscorable_defined_wait_closed = 0
    unscorable_unavailable_wait_closed = 0
    trade_not_entered_count = 0
    closed_actual_ids: list[str] = []
    for decision_id, outcome_event in outcomes.items():
        payload = outcome_event["payload"]
        if payload["result_kind"] == "wait_unscorable":
            if decisions[decision_id]["payload"]["wait_counterfactual"]["status"] == "defined":
                unscorable_defined_wait_closed += 1
            else:
                unscorable_unavailable_wait_closed += 1
            continue
        if payload["result_kind"] == "trade_not_entered":
            trade_not_entered_count += 1
            continue
        net = _decimal(payload["net_pnl_cny"], "net_pnl_cny")
        if payload["result_kind"] == "wait_counterfactual":
            wait_values.append(net)
            wait_triggered += int(payload["triggered"])
        else:
            actual_values.append(net)
            closed_actual_ids.append(decision_id)
    actual_minimum = int(policy["profit_summary"]["minimum_closed_actual_trades"])
    minimum_span = int(policy["profit_summary"]["minimum_observation_span_days"])
    actual = _section_summary(
        actual_values,
        decision_count=len(actual_decisions),
        closed_count=len(actual_values),
        minimum=actual_minimum,
        label="realized_actual_trades",
        resolved_without_sample_count=trade_not_entered_count,
    )
    wait = _section_summary(
        wait_values,
        decision_count=len(defined_wait_decisions),
        closed_count=len(wait_values),
        minimum=None,
        label="hypothetical_wait_counterfactuals_not_realized_profit",
        resolved_without_sample_count=unscorable_defined_wait_closed,
    )
    actual["trade_not_entered_count"] = trade_not_entered_count
    actual["trade_not_entered_profit_sample_inclusion"] = False
    wait["triggered_count"] = wait_triggered
    wait["all_wait_decision_count"] = len(wait_decisions)
    wait["unscorable_wait"] = {
        "decision_count": len(unavailable_wait_decisions),
        "closed_count": unscorable_unavailable_wait_closed,
        "open_count": len(unavailable_wait_decisions) - unscorable_unavailable_wait_closed,
        "defined_but_intrabar_ambiguous_count": unscorable_defined_wait_closed,
        "profit_sample_inclusion": False,
        "reason": "counterfactual or cost evidence was unavailable when frozen",
    }

    if closed_actual_ids:
        start = min(
            _parse_time(decisions[decision_id]["recorded_at"], "decision.recorded_at")
            for decision_id in closed_actual_ids
        )
        end = max(
            _parse_time(outcomes[decision_id]["payload"]["outcome_observed_at"], "outcome_observed_at")
            for decision_id in closed_actual_ids
        )
        observation_span_days = max(0, (end - start).days)
    else:
        observation_span_days = 0
    current = _current_time().astimezone(timezone.utc)
    due_ids = [
        decision_id
        for decision_id, event in decisions.items()
        if _decision_due_at(event) <= current
    ]
    missing_due_outcomes = sorted(decision_id for decision_id in due_ids if decision_id not in outcomes)
    all_due_complete = not missing_due_outcomes
    meets_minimum = (
        bool(actual["count_threshold_met"])
        and observation_span_days >= minimum_span
        and all_due_complete
    )
    return {
        "schema_version": "decision_ledger_summary.v1",
        "validation_status": (
            "meets_minimum_accounting_data_for_external_review"
            if meets_minimum
            else "insufficient_forward_accounting_data"
        ),
        "profit_verified": False,
        "profit_claim": "not established by this ledger, regardless of observed sign",
        "meets_minimum_accounting_data_for_external_review": meets_minimum,
        "minimum_observation_span_days": minimum_span,
        "observation_span_days": observation_span_days,
        "all_due_decisions_have_outcomes": all_due_complete,
        "due_decision_count": len(due_ids),
        "missing_due_outcome_decision_ids": missing_due_outcomes,
        "ledger_validation": validation,
        "actual": actual,
        "wait_counterfactual": wait,
    }


def _read_payload(path_text: str) -> dict[str, Any]:
    if path_text == "-":
        import sys

        text = sys.stdin.read()
        label = "stdin payload"
    else:
        path = Path(path_text).expanduser().resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DecisionLedgerError(f"cannot read payload {path}: {exc}") from exc
        label = f"payload {path}"
    value = _json_loads(text, label=label)
    if not isinstance(value, dict):
        raise DecisionLedgerValidationError(f"{label} must contain an object")
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze Agent decisions and append forward outcomes without asserting profitability."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create an empty ledger and head checkpoint")
    init_parser.add_argument("ledger")
    for command, help_text in (
        ("append-decision", "append a frozen decision payload"),
        ("append-outcome", "append an outcome referencing a frozen decision hash"),
    ):
        item = subparsers.add_parser(command, help=help_text)
        item.add_argument("ledger")
        item.add_argument("--input", required=True, help="payload JSON path, or - for stdin")
    validate_parser = subparsers.add_parser(
        "validate",
        help="reopen artifacts and replay local hash chain, checkpoint and arithmetic",
    )
    validate_parser.add_argument("ledger")
    summary_parser = subparsers.add_parser(
        "summary",
        help="separate actual, no-fill and wait local-accounting results",
    )
    summary_parser.add_argument("ledger")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = init_ledger(args.ledger)
        elif args.command == "append-decision":
            result = append_decision(args.ledger, _read_payload(args.input))
        elif args.command == "append-outcome":
            result = append_outcome(args.ledger, _read_payload(args.input))
        elif args.command == "validate":
            result = validate_ledger(args.ledger)
        else:
            result = summarize_ledger(args.ledger)
        _print_json(result)
        if args.command in {"validate", "summary"}:
            valid = result.get("valid", result.get("ledger_validation", {}).get("valid", False))
            return 0 if valid else 1
        return 0
    except DecisionLedgerError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

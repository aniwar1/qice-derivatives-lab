#!/usr/bin/env python3
"""Build the latest fail-closed strategy projection for an application backend.

This module does not fetch markets, select a direction, place an order or turn
validation into permission.  It accepts only a finalized ``decision_run.v1``
whose Agent-authored thesis points to a validated session-aware daily bar.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

try:
    from . import run_decision, runtime_universe, session_aware_bar
except ImportError:  # pragma: no cover - direct CLI execution
    import run_decision
    import runtime_universe
    import session_aware_bar


BJT = run_decision.BJT
MARKET_MODES = {"all_market_scan", "single_analysis", "option_entry", "position_review"}
BOUNDARIES = {
    "machine_selected_action": False,
    "validation_is_trade_permission": False,
    "order_placement_authorized": False,
    "automatic_ordering": False,
    "short_option_open_allowed": False,
}
LATEST_FIELDS = {
    "schema_version",
    "generated_at",
    "strategy_status",
    "source_run",
    "task_mode",
    "product",
    "analysis_contract",
    "technical_basis",
    "underlying_thesis",
    "option_expression",
    "lifecycle_state",
    "valid_until",
    "boundaries",
    "strategy_sha256",
}


class LatestStrategyError(ValueError):
    """Raised when no trustworthy current strategy can be published."""


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
        raise LatestStrategyError(f"strategy input is not canonical JSON: {exc}") from exc


def _read_json(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LatestStrategyError(f"cannot read JSON {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise LatestStrategyError("decision run root must be an object")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise LatestStrategyError(f"{label} must be a timezone-aware timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LatestStrategyError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LatestStrategyError(f"{label} must include a UTC offset")
    return parsed


def validate_latest_strategy(value: dict[str, Any], *, now: datetime | None = None) -> None:
    """Validate the derived projection and its canonical content hash."""

    if not isinstance(value, dict) or set(value) != LATEST_FIELDS:
        raise LatestStrategyError("latest strategy shape is invalid")
    if (
        value.get("schema_version") != "latest_strategy.v1"
        or value.get("strategy_status") != "current_research_decision"
        or value.get("task_mode") not in MARKET_MODES
        or value.get("boundaries") != BOUNDARIES
    ):
        raise LatestStrategyError("latest strategy weakens its contract or boundaries")
    product = runtime_universe.require_allowed_product(str(value.get("product", "")))
    contract = runtime_universe.canonical_delivery_contract(
        str(value.get("analysis_contract", ""))
    )
    if runtime_universe.product_prefix(contract) != product:
        raise LatestStrategyError("latest strategy product and contract conflict")
    generated = _timestamp(value.get("generated_at"), "generated_at")
    valid_until = _timestamp(value.get("valid_until"), "valid_until")
    current = now or datetime.now(tz=BJT)
    if current.tzinfo is None or current.utcoffset() is None:
        raise LatestStrategyError("validator clock must be timezone-aware")
    if generated > current.astimezone(generated.tzinfo):
        raise LatestStrategyError("latest strategy is future-dated")
    if valid_until <= generated:
        raise LatestStrategyError("latest strategy validity window is empty")
    if valid_until <= current.astimezone(valid_until.tzinfo):
        raise LatestStrategyError("latest strategy has expired")
    source_run = value.get("source_run")
    if (
        not isinstance(source_run, dict)
        or source_run.get("phase") != "finalized"
        or not isinstance(source_run.get("run_id"), str)
        or not source_run["run_id"]
        or not isinstance(source_run.get("decision_run_sha256"), str)
        or session_aware_bar.SHA256_RE.fullmatch(source_run["decision_run_sha256"])
        is None
    ):
        raise LatestStrategyError("latest strategy source_run is invalid")
    basis = value.get("technical_basis")
    if not isinstance(basis, dict):
        raise LatestStrategyError("latest strategy technical_basis is missing")
    if basis.get("bar_state") == "session_partial":
        if (
            basis.get("decision_semantics") not in {"anticipatory", "conditional"}
            or basis.get("confirmed_daily_breakout_claim_eligible") is not False
        ):
            raise LatestStrategyError("latest strategy relabels partial evidence as confirmed")
    elif basis.get("bar_state") == "completed":
        if basis.get("confirmed_daily_breakout_claim_eligible") is not True:
            raise LatestStrategyError("completed strategy basis has a false claim boundary")
    else:
        raise LatestStrategyError("latest strategy has an unknown daily-bar state")
    unsigned = deepcopy(value)
    supplied = unsigned.pop("strategy_sha256", None)
    if supplied != hashlib.sha256(_canonical_bytes(unsigned)).hexdigest():
        raise LatestStrategyError("latest strategy content hash mismatch")


def build_latest_strategy(
    run: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Return a signed-by-content current projection or fail without output."""

    current = now or datetime.now(tz=BJT)
    if current.tzinfo is None or current.utcoffset() is None:
        raise LatestStrategyError("builder clock must be timezone-aware")
    current = current.astimezone(BJT)
    validation = run_decision.validate_run(run, now=current)
    if validation.get("valid") is not True:
        errors = validation.get("errors") or ["unknown decision-run validation failure"]
        raise LatestStrategyError(f"invalid decision run: {errors[0]}")
    if run.get("phase") != "finalized":
        raise LatestStrategyError("latest strategy requires phase=finalized")
    if run.get("task_mode") not in MARKET_MODES:
        raise LatestStrategyError("latest strategy accepts only market decision modes")
    if validation.get("missing_required_evidence_domains"):
        raise LatestStrategyError("latest strategy has missing required evidence domains")
    if run.get("quality_review") != {
        "critic_revisions": 1,
        "unresolved_material_issue": None,
    }:
        raise LatestStrategyError("latest strategy requires one resolved bounded critic review")
    if run.get("permissions") != {
        "order_placement_authorized": False,
        "short_option_open_allowed": False,
    }:
        raise LatestStrategyError("decision run weakens execution boundaries")

    thesis = run.get("underlying_thesis")
    if not isinstance(thesis, dict):
        raise LatestStrategyError("latest strategy requires an Agent-authored underlying thesis")
    basis = thesis.get("daily_bar_basis")
    if not isinstance(basis, dict):
        raise LatestStrategyError(
            "latest strategy requires an explicit daily_bar_basis; legacy implicit bars fail closed"
        )
    packet_matches = [
        packet
        for packet in run.get("evidence_packets", [])
        if isinstance(packet, dict)
        and packet.get("packet_id") == basis.get("evidence_packet_id")
        and packet.get("domain") == "technical_structure"
    ]
    if len(packet_matches) != 1:
        raise LatestStrategyError("daily_bar_basis must identify one technical evidence packet")
    packet = packet_matches[0]
    facts = packet.get("facts")
    bar = facts.get("session_daily_bar") if isinstance(facts, dict) else None
    if not isinstance(bar, dict):
        raise LatestStrategyError("technical evidence does not contain session_daily_bar")
    try:
        bar_validation = session_aware_bar.validate_session_aware_daily_bar(
            bar,
            expected_product=thesis.get("product"),
            expected_contract=thesis.get("analysis_contract"),
            packet_observed_at=packet.get("observed_at"),
            now=current,
        )
    except (session_aware_bar.SessionAwareBarError, ValueError) as exc:
        raise LatestStrategyError(f"daily-bar basis is invalid: {exc}") from exc
    expected_basis = {
        "evidence_packet_id": packet["packet_id"],
        "bar_state": bar["bar_state"],
        "as_of": bar["as_of"],
        "decision_semantics": bar["decision_semantics"],
    }
    if basis != expected_basis:
        raise LatestStrategyError("daily_bar_basis does not exactly match its evidence")
    if bar["bar_state"] == "session_partial":
        expected_close = _timestamp(
            bar["coverage"]["expected_daily_close_at"],
            "technical bar expected_daily_close_at",
        ).astimezone(BJT)
        if current >= expected_close:
            raise LatestStrategyError(
                "session_partial bar expired at the daily close; refresh with a completed bar"
            )
        if bar["decision_semantics"] not in {"anticipatory", "conditional"}:
            raise LatestStrategyError("partial bar cannot publish confirmed daily breakout")
    packet_valid_until = packet.get("valid_until")
    if packet_valid_until is not None and _timestamp(
        packet_valid_until, "technical evidence valid_until"
    ).astimezone(BJT) <= current:
        raise LatestStrategyError("technical daily-bar evidence has expired")
    if (
        str(thesis.get("path", "")).strip().lower() == "confirmed_breakout"
        and bar_validation["confirmed_daily_breakout_claim_eligible"] is not True
    ):
        raise LatestStrategyError("confirmed daily breakout requires a completed daily bar")

    thesis_valid_until = _timestamp(
        thesis.get("latest_realization_time"), "underlying_thesis.latest_realization_time"
    ).astimezone(BJT)
    if thesis_valid_until <= current:
        raise LatestStrategyError("underlying thesis realization window has expired")
    option = run.get("option_expression")
    if run["task_mode"] in {"option_entry", "position_review"} and not isinstance(option, dict):
        raise LatestStrategyError("option decision mode requires a validated option expression")

    validity_candidates = [thesis_valid_until]
    for evidence in run.get("evidence_packets", []):
        evidence_valid_until = evidence.get("valid_until")
        if evidence_valid_until is not None:
            validity_candidates.append(
                _timestamp(
                    evidence_valid_until,
                    f"evidence {evidence.get('packet_id')} valid_until",
                ).astimezone(BJT)
            )
    if bar["bar_state"] == "session_partial":
        validity_candidates.append(
            _timestamp(
                bar["coverage"]["expected_daily_close_at"],
                "technical bar expected_daily_close_at",
            ).astimezone(BJT)
        )
    strategy_valid_until = min(validity_candidates)
    if strategy_valid_until <= current:
        raise LatestStrategyError("latest strategy has no current evidence-validity window")

    payload = {
        "schema_version": "latest_strategy.v1",
        "generated_at": current.isoformat(timespec="seconds"),
        "strategy_status": "current_research_decision",
        "source_run": {
            "run_id": run["run_id"],
            "phase": "finalized",
            "updated_at": run["updated_at"],
            "decision_run_sha256": hashlib.sha256(_canonical_bytes(run)).hexdigest(),
            "scope": deepcopy(run["scope"]),
        },
        "task_mode": run["task_mode"],
        "product": thesis["product"],
        "analysis_contract": thesis["analysis_contract"],
        "technical_basis": {
            "evidence_packet_id": packet["packet_id"],
            "bar_state": bar["bar_state"],
            "trade_date": bar["trade_date"],
            "as_of": bar["as_of"],
            "decision_semantics": bar["decision_semantics"],
            "coverage": deepcopy(bar["coverage"]),
            "source": deepcopy(bar["source"]),
            "confirmed_daily_breakout_claim_eligible": bar_validation[
                "confirmed_daily_breakout_claim_eligible"
            ],
        },
        "underlying_thesis": deepcopy(thesis),
        "option_expression": deepcopy(option),
        "lifecycle_state": run.get("lifecycle_state"),
        "valid_until": strategy_valid_until.isoformat(timespec="seconds"),
        "boundaries": deepcopy(BOUNDARIES),
    }
    payload["strategy_sha256"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    validate_latest_strategy(payload, now=current)
    return payload


def _atomic_write(path: str | Path, value: dict[str, Any], *, replace: bool) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not replace:
        raise LatestStrategyError(f"refusing to replace existing output: {destination}")
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
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomically update latest-strategy JSON from one finalized decision run."
    )
    parser.add_argument("--run", required=True, help="validated decision_run.v1 JSON")
    parser.add_argument("--output", required=True, help="latest_strategy.v1 JSON destination")
    parser.add_argument("--replace", action="store_true", help="atomically replace an existing projection")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_latest_strategy(_read_json(args.run))
        output = _atomic_write(args.output, result, replace=args.replace)
    except (LatestStrategyError, run_decision.DecisionRunError, OSError) as exc:
        print(
            json.dumps(
                {
                    "success": False,
                    "published": False,
                    "order_authorized": False,
                    "errors": [str(exc)],
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "success": True,
                "published": True,
                "output": str(output),
                "strategy_sha256": result["strategy_sha256"],
                "order_authorized": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

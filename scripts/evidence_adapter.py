#!/usr/bin/env python3
"""Normalize one validated domain artifact into an action-free EvidencePacket.

This adapter does not decide whether source facts are economically bullish or
bearish.  Domain-specific fetchers/validators produce the artifact; this module
only binds its bytes, scope, time and five-axis status for orchestration.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from . import run_decision
except ImportError:  # pragma: no cover - direct CLI execution
    import run_decision


def _load_facts(path: str | Path | None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if path is None:
        return {}, []
    resolved = Path(path).expanduser().resolve()
    raw = resolved.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise run_decision.DecisionRunError("evidence facts artifact must contain a JSON object")
    return value, [
        {
            "kind": "file",
            "locator": str(resolved),
            "retrieved_at": "",
            "content_sha256": hashlib.sha256(raw).hexdigest(),
        }
    ]


def create_packet(
    *,
    run_id: str,
    packet_id: str,
    domain: str,
    product: str | None,
    analysis_contract: str | None,
    observed_at: str,
    valid_until: str | None,
    availability: str,
    freshness: str,
    integrity: str,
    identity: str,
    executability: str,
    facts: dict[str, Any],
    source_refs: list[dict[str, str]],
    known_gaps: list[str],
) -> dict[str, Any]:
    for source in source_refs:
        if not source["retrieved_at"]:
            source["retrieved_at"] = observed_at
    return {
        "schema_version": "evidence_packet.v1",
        "packet_id": packet_id,
        "run_id": run_id,
        "domain": domain,
        "product": product.upper() if product else None,
        "analysis_contract": analysis_contract.upper() if analysis_contract else None,
        "observed_at": observed_at,
        "valid_until": valid_until,
        "status": {
            "availability": availability,
            "freshness": freshness,
            "integrity": integrity,
            "identity": identity,
            "executability": executability,
        },
        "source_refs": source_refs,
        "facts": facts,
        "known_gaps": known_gaps,
        "does_not_select_action": True,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wrap a domain artifact as evidence_packet.v1 without selecting a trade."
    )
    parser.add_argument("--run", required=True, help="decision_run.v1 used for scope validation")
    parser.add_argument("--packet-id", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--product")
    parser.add_argument("--analysis-contract")
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--valid-until")
    parser.add_argument("--availability", required=True)
    parser.add_argument("--freshness", required=True)
    parser.add_argument("--integrity", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--executability", required=True)
    parser.add_argument("--facts", help="validated JSON artifact; omit only for unobservable evidence")
    parser.add_argument("--gap", action="append", default=[])
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        run = run_decision._read_json(args.run)
        facts, sources = _load_facts(args.facts)
        packet = create_packet(
            run_id=run["run_id"],
            packet_id=args.packet_id,
            domain=args.domain,
            product=args.product,
            analysis_contract=args.analysis_contract,
            observed_at=args.observed_at,
            valid_until=args.valid_until,
            availability=args.availability,
            freshness=args.freshness,
            integrity=args.integrity,
            identity=args.identity,
            executability=args.executability,
            facts=facts,
            source_refs=sources,
            known_gaps=args.gap,
        )
        run_decision.validate_evidence_packet(packet, run)
        output = run_decision._atomic_write(args.output, packet, replace=False)
        print(json.dumps({"success": True, "output": str(output)}, ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, run_decision.DecisionRunError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

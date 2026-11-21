from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

from .codex_gateway import CodexGateway
from .db import StrategyStore
from .freshness import resolve_latest_strategy
from .models import StrategyRunView


BJT = ZoneInfo("Asia/Shanghai")


class DomainSnapshotProvider:
    """Read the existing Skill contracts without making them an HTTP boundary."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def allowed_products(self) -> tuple[str, ...]:
        path = self.workspace_root / "config" / "trading_universe.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        products = document["permission_scope"]["allowed_products"]
        symbols = tuple(sorted(str(item["symbol"]).upper() for item in products))
        if len(symbols) != document["product_count"] or len(symbols) != len(set(symbols)):
            raise ValueError("configured universe is internally inconsistent")
        return symbols

    def build(self) -> dict[str, Any]:
        root_text = str(self.workspace_root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from scripts import build_dashboard_snapshot

        learning_path = self.workspace_root / "local-data" / "trade-learning" / "summary.json"
        learning: dict[str, Any] | None = None
        if learning_path.is_file():
            candidate = json.loads(learning_path.read_text(encoding="utf-8"))
            if candidate:
                learning = candidate
        snapshot = build_dashboard_snapshot.build_snapshot(learning=learning)
        specs_path = self.workspace_root / "config" / "product_contract_specs.json"
        specs = json.loads(specs_path.read_text(encoding="utf-8"))
        allowed = set(snapshot["universe"]["products"])
        snapshot["product_catalog"] = [
            {key: spec[key] for key in ("symbol", "name", "exchange")}
            for spec in specs["products"]
            if spec["symbol"] in allowed
        ]
        return snapshot

    def validate_session_bar(
        self,
        bar: dict[str, Any],
        *,
        expected_product: str | None,
        expected_contract: str | None,
        packet_observed_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        root_text = str(self.workspace_root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from scripts import session_aware_bar

        return session_aware_bar.validate_session_aware_daily_bar(
            bar,
            expected_product=expected_product,
            expected_contract=expected_contract,
            packet_observed_at=packet_observed_at,
            now=now,
        )


def public_run_view(row: dict[str, Any]) -> StrategyRunView:
    return StrategyRunView(
        run_id=row["id"],
        status=row["status"],
        request=row["request"],
        result=row["result"],
        error_code=row["public_error_code"],
        error_message=row["public_error_message"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        updated_at=row["updated_at"],
    )


class DashboardProjector:
    def __init__(
        self,
        store: StrategyStore,
        gateway: CodexGateway,
        domain: DomainSnapshotProvider,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.domain = domain

    async def project(self) -> dict[str, Any]:
        projected_at = datetime.now(tz=BJT)
        latest_row, active_row, database_ok, codex = await asyncio.gather(
            self.store.get_latest_completed(),
            self.store.get_active_run(),
            self.store.ping(),
            self.gateway.status(),
        )
        projection_state = "ready"
        projection_error: str | None = None
        try:
            domain_snapshot = await asyncio.to_thread(self.domain.build)
        except Exception:
            domain_snapshot = None
            projection_state = "degraded"
            projection_error = "domain_snapshot_unavailable"

        resolution = resolve_latest_strategy(
            latest_row["result"] if latest_row else None,
            now=projected_at,
        )
        latest_record = (
            resolution.record.model_dump(mode="json")
            if resolution.record is not None
            else None
        )
        latest = None
        evidence: list[dict[str, Any]] | None = None
        underlying_thesis: dict[str, Any] | None = None
        option_expression: dict[str, Any] | None = None
        if latest_record is not None:
            strategy = latest_record["strategy"]
            levels = strategy["market_levels"]
            rationale = strategy["rationale"]
            option = strategy["option_expression"]
            session_bar = strategy["session_daily_bar"]
            display_bar = None
            if session_bar is not None:
                display_bar = {
                    **session_bar,
                    "state": session_bar["bar_state"],
                    "observed_at": session_bar["as_of"],
                    "coverage_label": (
                        "夜盘检查点后部分日线"
                        if session_bar["bar_state"] == "session_partial"
                        else "完成日线"
                    ),
                }
            latest = {
                **strategy,
                # Explicit read-model aliases keep the UI independent from the
                # richer storage envelope without weakening the source schema.
                "directional_view": strategy["direction"],
                "path": strategy["entry_style"],
                "trigger": levels["trigger"],
                "target": levels["target"],
                "invalidation": levels["invalidation"],
                "latest_realization_time": strategy["reassess_by"],
                "why_now": rationale["why_now"],
                "strongest_counterevidence": rationale["strongest_counterevidence"],
                "falsifiers": strategy["risks"],
                "option_action": strategy["decision"] if option else "wait",
                "option_route": option["route"] if option else "",
                "option_contract": option["symbol"] if option else "",
                "option_reason": strategy["option_market"]["conclusion"],
                "session_daily_bar": display_bar,
            }
            evidence = self._strategy_evidence(strategy, display_bar)
            underlying_thesis = {
                "product": strategy["product"],
                "actual_main_contract": strategy["actual_main_contract"],
                "analysis_contract": strategy["analysis_contract"],
                "directional_view": strategy["direction"],
                "path": strategy["entry_style"],
                "trigger": levels["trigger"],
                "target": levels["target"],
                "futures_structure_invalidation": levels["invalidation"],
                "latest_realization_time": strategy["reassess_by"],
                "why_now": rationale["why_now"],
                "strongest_counterevidence": rationale["strongest_counterevidence"],
                "falsifiers": strategy["risks"],
            }
            option_expression = option
        active = public_run_view(active_row).model_dump(mode="json") if active_row else None
        codex_payload = codex.model_dump(mode="json")
        codex_payload["status"] = "ready" if codex.authenticated else "authentication_required"
        public_domain_snapshot = dict(domain_snapshot or {})
        product_catalog = public_domain_snapshot.pop("product_catalog", [])
        # A domain snapshot can contain an older derived strategy.  The
        # database freshness decision is authoritative for public current-state
        # fields, so replace those aliases in both the flattened projection and
        # its nested snapshot instead of leaking a second stale read path.
        public_domain_snapshot["evidence"] = evidence or []
        public_domain_snapshot["underlying_thesis"] = underlying_thesis
        public_domain_snapshot["option_expression"] = option_expression
        base = dict(public_domain_snapshot)
        base.update({
            "schema_version": "dashboard_api.v1",
            # The existing UI treats generated_at as the strategy timestamp.
            # Keep that compatibility field, but source it from the immutable
            # strategy record rather than from this GET request.
            "generated_at": (
                latest_record["updated_at"] if latest_record is not None else None
            ),
            "strategy_updated_at": (
                latest_record["updated_at"] if latest_record is not None else None
            ),
            "strategy_as_of": (
                latest_record["strategy"]["as_of"] if latest_record is not None else None
            ),
            "projected_at": projected_at.isoformat(timespec="seconds"),
            "latest_strategy_status": resolution.freshness.model_dump(),
            "latest_strategy": latest,
            "latest_strategy_record": latest_record,
            "active_run": active,
            "product_catalog": product_catalog,
            "codex": codex_payload,
            "system": {
                "status": "ready" if database_ok and projection_state == "ready" else "degraded",
                "database": "ready" if database_ok else "unavailable",
                "projection": projection_state,
                "projection_error": projection_error,
                "decision_owner": "codex",
                "browser_direct_to_codex": False,
                "order_placement_authorized": False,
                "short_option_open_allowed": False,
                "hidden_reasoning_persisted": False,
            },
            "domain_snapshot": public_domain_snapshot or None,
        })
        return base

    @staticmethod
    def _strategy_evidence(
        strategy: dict[str, Any], display_bar: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for domain in (
            "technical_structure",
            "fundamentals_news",
            "member_flow",
            "option_market",
        ):
            item = strategy[domain]
            facts: dict[str, Any] = {"sources": item["sources"]}
            if domain == "technical_structure":
                facts["session_daily_bar"] = display_bar
                facts["market_levels"] = strategy["market_levels"]
            evidence.append(
                {
                    "domain": domain,
                    "observed_at": strategy["as_of"],
                    "status": {
                        "availability": item["status"],
                        "freshness": item["freshness"],
                        # A syntactically valid source hash is not independent
                        # evidence verification; the UI must not imply otherwise.
                        "integrity": "unverified",
                    },
                    "summary": item["conclusion"],
                    "facts": facts,
                }
            )
        return evidence

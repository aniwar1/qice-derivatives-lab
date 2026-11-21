from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.codex_gateway import CodexRunResult
from app.config import Settings
from app.dashboard import DomainSnapshotProvider
from app.models import AgentStrategyPayload, CodexRuntimeMetadata


BJT = ZoneInfo("Asia/Shanghai")


def strategy_payload(
    *,
    decision: str = "futures",
    bar_state: str = "session_partial",
    entry_style: str = "anticipatory_breakout",
    product: str | None = "JM",
) -> AgentStrategyPayload:
    now = datetime.now(tz=BJT).replace(microsecond=0)
    is_action = decision != "wait"
    direction = "long" if decision in {"futures", "long_call"} else "short"
    if decision == "wait":
        direction = "uncertain"
    contract = f"{product}2701" if product else None
    option = None
    if decision in {"long_call", "long_put"}:
        option = {
            "route": decision,
            "symbol": f"{contract}-C-3600" if decision == "long_call" else f"{contract}-P-3400",
            "underlying_contract": contract,
            "expiration_date": (now + timedelta(days=60)).date(),
            "strike": 3600 if decision == "long_call" else 3400,
            "bid": 40,
            "ask": 41,
            "quote_at": now,
            "implied_volatility": 0.22,
            "delta": 0.48 if decision == "long_call" else -0.48,
            "theta": -0.8,
            "max_premium_loss": 410,
        }
    source = {
        "url": "https://example.com/market",
        "title": "市场数据",
        "retrieved_at": now,
        "locator": "JM2701 quote",
        "content_sha256": "a" * 64,
    }
    domain = {
        "status": "partial",
        "freshness": "fresh",
        "conclusion": "当前证据可用于条件决策，但保留时段不完整性。",
        "sources": [source],
    }
    return AgentStrategyPayload.model_validate(
        {
            "schema_version": "codex_strategy_decision.v1",
            "as_of": now,
            "decision": decision,
            "product": product,
            "product_name": "螺纹钢" if product else None,
            "actual_main_contract": contract,
            "analysis_contract": contract,
            "direction": direction,
            "entry_style": entry_style if is_action else "none",
            "entry_timing": "conditional" if is_action else "wait",
            "scan_coverage": {
                "scope": "all_market",
                "expected_count": 2,
                "attempted_count": 2,
                "products": [
                    {
                        "product": "M",
                        "status": "complete",
                        "actual_main_contract": "M2701",
                        "observed_at": now,
                        "known_gap": None,
                    },
                    {
                        "product": "JM",
                        "status": "complete",
                        "actual_main_contract": "JM2701",
                        "observed_at": now,
                        "known_gap": None,
                    },
                ],
            },
            "session_daily_bar": {
                "schema_version": "session_aware_daily_bar.v1",
                "product": product,
                "analysis_contract": contract,
                "trade_date": now.date(),
                "bar_state": bar_state,
                "as_of": now,
                "decision_semantics": "confirmed_daily_breakout" if bar_state == "completed" else "anticipatory",
                "ohlcv": {
                    "open": 3500,
                    "high": 3530,
                    "low": 3490,
                    "close": 3520,
                    "volume": 100,
                    "open_interest": 200,
                },
                "coverage": {
                    "session_kind": "night",
                    "schedule_group": "23:00",
                    "session_started_at": now - timedelta(hours=2),
                    "checkpoint_at": now - timedelta(hours=1),
                    "observed_through": now - timedelta(minutes=1),
                    "expected_daily_close_at": now + timedelta(hours=12),
                    "covered_trading_seconds": 7200,
                    "required_checkpoint_seconds": 7200,
                },
                "source": {
                    "kind": "url",
                    "locator": "https://example.com/market#JM2701",
                    "retrieved_at": now - timedelta(minutes=1),
                    "content_sha256": "b" * 64,
                },
                "calendar_evidence": {
                    "exchange": "DCE",
                    "trade_date": now.date(),
                    "regular_session_confirmed": True,
                    "url": "https://www.dce.com.cn/dalianshangpin/calendar/",
                    "retrieved_at": now - timedelta(minutes=1),
                    "locator": f"{now.date().isoformat()} regular session calendar entry",
                    "content_sha256": "c" * 64,
                } if bar_state == "session_partial" else None,
                "claim_boundary": {
                    "completed_bar_claim_allowed": bar_state == "completed",
                    "confirmed_daily_breakout_claim_eligible": bar_state == "completed",
                },
            } if product else None,
            "market_levels": {
                "current_price": 3520,
                "quote_at": now,
                "strongest_support": 3480,
                "strongest_resistance": 3560,
                "trigger": 3550 if direction == "long" else 3490,
                "invalidation": 3470 if direction == "long" else 3570,
                "target": 3650 if direction == "long" else 3390,
                "tick_size": 1,
            },
            "rationale": {
                "thesis": "供需边际改善并由价格结构验证。",
                "why_now": "价格接近唯一触发位且失效边界明确。",
                "why_not_wait": "等待完整日线可能损失突破前赔率，但仅使用条件触发。",
                "why_not_opposite": "反向缺少新增持仓与结构共振。",
                "strongest_counterevidence": "夜盘尚未收盘，结构仍可能回落。",
            },
            "technical_structure": domain,
            "fundamentals_news": domain,
            "member_flow": domain,
            "option_market": domain,
            "option_expression": option,
            "risks": ["部分 K 线可能反转"],
            "evidence_gaps": ["尚无完成日线确认"] if bar_state != "completed" else [],
            "quality_review": {
                "critic_revisions": 1,
                "strongest_challenge": "部分日线可能在日盘反转，使当前突破候选失效。",
                "resolution": "保留该反证并将入场限制为条件触发，跌破失效位则放弃假设。",
                "unresolved_material_issue": None,
            },
            "reassess_by": now + timedelta(days=1),
            "order_placement_authorized": False,
            "short_option_open_allowed": False,
        }
    )


class FakeGateway:
    def __init__(self, payload: AgentStrategyPayload | None = None) -> None:
        self.payload = payload or strategy_payload()
        self.calls: list[str] = []
        self.workspace_roots: list[Path | None] = []
        self.release = None

    async def status(self, *, refresh: bool = False) -> CodexRuntimeMetadata:
        return CodexRuntimeMetadata(
            transport="cli_exec_json",
            auth_mode="chatgpt_managed",
            authenticated=True,
            model="configured_default",
            model_resolution="inherited",
            version="0.154.0",
        )

    async def run_strategy(
        self, prompt: str, *, workspace_root: Path | None = None
    ) -> CodexRunResult:
        self.calls.append(prompt)
        self.workspace_roots.append(workspace_root)
        if self.release is not None:
            await self.release.wait()
        return CodexRunResult(payload=self.payload, runtime=await self.status())


class FakeDomain(DomainSnapshotProvider):
    def __init__(self, root: Path) -> None:
        super().__init__(root)

    def allowed_products(self) -> tuple[str, ...]:
        return ("M", "JM")

    def build(self) -> dict[str, Any]:
        return {
            "schema_version": "skill_dashboard_snapshot.v1",
            "universe": {"product_count": 2, "products": ["M", "JM"]},
        }

    def validate_session_bar(self, bar: dict[str, Any], **_: Any) -> dict[str, Any]:
        return {
            "valid": True,
            "bar_state": bar["bar_state"],
            "confirmed_daily_breakout_claim_eligible": bar["claim_boundary"][
                "confirmed_daily_breakout_claim_eligible"
            ],
        }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        codex_bin="codex",
        auto_refresh_if_empty=False,
        sse_poll_milliseconds=100,
    )

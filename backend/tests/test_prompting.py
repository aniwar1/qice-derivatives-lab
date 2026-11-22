from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import StrategyRunRequest
from app.prompting import build_strategy_prompt


def test_prompt_freezes_scope_and_partial_bar_semantics() -> None:
    prompt = build_strategy_prompt(
        StrategyRunRequest(),
        run_id="run-1",
        now=datetime(2026, 9, 14, 21, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        allowed_products=("M", "JM"),
    )
    assert '"allowed_products": ["M", "JM"]' not in prompt
    assert '"allowed_products":["M","JM"]' in prompt
    assert "session_partial" in prompt
    assert "Never relabel a partial bar" in prompt
    assert "order" in prompt.lower()
    assert "Return exactly one" in prompt
    assert "原始事实、首次可得时间" in prompt
    assert "检验是否已计价" in prompt
    assert "资金意图缺少直接证据时只列为假设" in prompt
    assert "untrusted evidence" in prompt
    assert "scripts/refresh_daily_market_data.py --dry-run" in prompt
    assert "scripts/scan_market.py" in prompt
    assert "disposable staging workspace" in prompt
    assert "local-data/codex-runs/run-1/scan.json" in prompt
    assert "canonical `local-data/market-history` files" in prompt
    assert "small Agent-selected shortlist" in prompt
    assert "separately traverse live news" in prompt
    assert "one explicit `scan_coverage` row per product" in prompt
    assert "preceding 30 minutes" in prompt
    assert "do not start open-ended per-product fetching" in prompt
    assert "2 rows in the current configuration" in prompt
    assert "`status=unobservable` with a non-empty `known_gap`" in prompt
    assert "public official HTTPS domain" in prompt
    assert "`completed` bar must set `calendar_evidence=null`" in prompt
    assert "`critic_revisions=1`" in prompt
    assert "`unresolved_material_issue=null`" in prompt

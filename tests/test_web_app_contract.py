from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
RAIL = (ROOT / "frontend" / "src" / "components" / "DecisionRail.tsx").read_text(
    encoding="utf-8"
)
HELP = (ROOT / "frontend" / "src" / "components" / "HelpTip.tsx").read_text(
    encoding="utf-8"
)
API = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")


def test_frontend_keeps_research_workflows_separately_accessible() -> None:
    rolling = (ROOT / "frontend/src/components/RollingBaseline.tsx").read_text(encoding="utf-8")
    assert "<RollingWorkbench" in APP
    assert "<PlanDetail" in rolling and "<Curve" in rolling
    assert "<AgentStudy" in APP and "<FrequencyStudy" in APP
    assert "<ProductWorkbench" in APP
    assert "<ResearchPanel" in APP
    assert "<DecisionRail" in APP
    assert "dashboard.nodes" in APP
    assert "脉冲决策轨道" in RAIL


def test_explanations_use_accessible_question_tooltips() -> None:
    assert "<HelpTip" in APP
    assert 'className="help-tip__trigger"' in HELP
    assert 'aria-label={label}' in HELP
    assert 'role="tooltip"' in HELP
    assert "dangerouslySetInnerHTML" not in APP + RAIL + HELP


def test_frontend_reattaches_to_backend_started_run() -> None:
    assert 'dashboard?.runId' in APP
    assert '`/api/runs/${runId}/events`' in APP
    assert "策略更新中…" in APP or "策略更新中…" in (
        ROOT / "frontend" / "src" / "components" / "AsyncState.tsx"
    ).read_text(encoding="utf-8")


def test_api_routes_are_declared_before_same_origin_frontend_mount() -> None:
    assert API.index('@app.get("/api/health")') < API.index('app.mount("/"')
    assert API.index('@app.get("/api/workbench/rolling-baseline")') < API.index('app.mount("/"')
    assert API.index('@app.get("/api/workbench/backtest")') < API.index('app.mount("/"')
    assert API.index('@app.get("/api/workbench/products/{product}")') < API.index('app.mount("/"')
    assert API.index('@app.post(') < API.index('app.mount("/"')
    assert "StaticFiles" in API

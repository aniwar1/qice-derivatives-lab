from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")


def test_api_routes_are_declared_before_same_origin_frontend_mount() -> None:
    # 浏览器同源访问接口时，业务路由须先于静态资源兜底匹配。
    mount = API.index('app.mount("/"')
    for path in [
        "/api/health",
        "/api/workbench/rolling-baseline",
        "/api/workbench/backtest",
        "/api/workbench/products/{product}",
        "/api/workbench/strategies",
        "/api/workbench/history",
        "/api/workbench/history/{product}",
    ]:
        assert API.index(f'@app.get("{path}"') < mount
    assert API.index('@app.post(') < mount
    assert "StaticFiles" in API

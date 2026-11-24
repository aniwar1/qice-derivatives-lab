import httpx
import pytest

from app.main import create_app


@pytest.mark.asyncio
async def test_workbench_entry_revalidates_cached_html(settings):
    dist = settings.workspace_root / "frontend/dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<title>期策</title>", encoding="utf-8")
    app = create_app(settings=settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        for path in ("/", "/index.html"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache, must-revalidate"
            cached = await client.get(path, headers={"If-None-Match": response.headers["etag"]})
            assert cached.status_code == 304
            assert cached.headers["cache-control"] == "no-cache, must-revalidate"

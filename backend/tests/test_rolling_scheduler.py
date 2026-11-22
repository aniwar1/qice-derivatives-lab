import asyncio
import os
import sys

from app import rolling_scheduler as worker
from app.config import Settings


def test_pending_result_is_visible_and_successful_retry_clears_reason(
    tmp_path, monkeypatch
):
    base = tmp_path / "local-data" / "agent-study" / worker.BATCH
    base.mkdir(parents=True)
    (base / "results.json").write_text("{}")
    scheduler = worker.RollingScheduler(Settings(workspace_root=tmp_path))
    calls = 0

    async def run():
        completed = asyncio.Event()

        async def check():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "status": "pending",
                    "last_settled": "2026-11-20",
                    "reason": "缺少原件",
                }
            return {"status": "settled", "last_settled": "2026-11-23"}

        async def wait(_):
            if calls == 1:
                assert scheduler.state["state"] == "retrying"
                assert scheduler.state["reason"] == "缺少原件"
                assert scheduler.state["last_success_at"] is None
            else:
                completed.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(scheduler, "check", check)
        monkeypatch.setattr(worker, "refresh_calendar", lambda *args: None)
        monkeypatch.setattr(worker.asyncio, "sleep", wait)
        scheduler.start()
        await asyncio.wait_for(completed.wait(), timeout=3)
        await scheduler.stop()

    asyncio.run(run())
    assert scheduler.state["worker_status"] == "settled"
    assert scheduler.state["reason"] is None
    assert scheduler.state["last_settled"] == "2026-11-23"


def test_stopping_worker_reaps_separate_model_group(tmp_path, monkeypatch):
    marker = tmp_path / "child"
    runner = tmp_path / "simulation.py"
    runner.write_text(
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n"
        "child.wait()\n"
    )
    scheduler = worker.RollingScheduler(Settings(workspace_root=tmp_path))
    original = asyncio.create_subprocess_exec

    async def substitute(*args, **kw):
        return await original(sys.executable, str(runner), str(marker), **kw)

    async def run():
        monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", substitute)
        task = asyncio.create_task(scheduler.check())
        try:
            for _ in range(150):
                if marker.exists() and marker.read_text():
                    break
                await asyncio.sleep(0.02)
            assert marker.is_file()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        child = int(marker.read_text())
        for _ in range(100):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("独立模型进程在更新器停止后没有退出")

    asyncio.run(run())

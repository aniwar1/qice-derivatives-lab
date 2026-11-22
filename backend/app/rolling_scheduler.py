"""随本地服务运行的半年基线更新器，进程重启后沿已落盘的交易日接续。"""

import asyncio
from datetime import datetime, time
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
from zoneinfo import ZoneInfo

from .config import Settings
from .rolling_calendar import refresh_calendar
from .study_view import BATCH


BJT = ZoneInfo("Asia/Shanghai")
LOG = logging.getLogger(__name__)


def _worker_groups(pid):
    """模型运行器会另建进程组，按当前父子树限定本轮更新拥有的组。"""
    rows = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,pgid="], text=True, timeout=3
    )
    children = {}
    for row in rows.splitlines():
        child, parent, group = map(int, row.split())
        children.setdefault(parent, []).append((child, group))
    groups = {pid}
    todo = [pid]
    while todo:
        for child, group in children.get(todo.pop(), ()):
            todo.append(child)
            if child == group:
                groups.add(group)
    return groups


def _signal_groups(groups, sig):
    for group in groups:
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            pass


class RollingScheduler:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = {
            "state": "starting",
            "last_checked_at": None,
            "last_success_at": None,
        }
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run(), name="rolling-baseline-updater")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def run(self):
        base = self.settings.workspace_root / "local-data" / "agent-study" / BATCH
        while True:
            if not (base / "results.json").is_file():
                self.state["state"] = "waiting_for_history"
            else:
                self.state["last_checked_at"] = datetime.now(BJT).isoformat()
                try:
                    await asyncio.to_thread(
                        refresh_calendar,
                        self.settings.workspace_root,
                        datetime.now(BJT).date(),
                    )
                    result = await self.check()
                    pending = result["status"] in ("pending", "calendar_exhausted")
                    self.state.update(
                        state="retrying" if pending else "ready",
                        worker_status=result["status"],
                        reason=result.get("reason")
                        or (
                            "等待交易日历更新。"
                            if result["status"] == "calendar_exhausted"
                            else None
                        ),
                        next_session=result.get("next_session"),
                        last_settled=result.get("last_settled"),
                    )
                    if not pending:
                        self.state["last_success_at"] = datetime.now(BJT).isoformat()
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    TimeoutError,
                ):
                    self.state.update(
                        state="retrying", reason="更新器执行失败，等待下轮重试。"
                    )
                    LOG.exception("半年基线更新检查失败，下轮接续已有记录")
            clock = datetime.now(BJT).time()
            await asyncio.sleep(60 if time(15) <= clock < time(21) else 300)

    async def check(self):
        root = self.settings.workspace_root
        self.state["state"] = "checking"
        cmd = [
            sys.executable,
            "-m",
            "scripts.rolling_update",
            "--workspace",
            str(root),
            "--cli",
            self.settings.codex_bin,
        ]
        # 用独立进程隔开模型调用和网络耗时，网页查询仍可读取上次完整结算。
        code = Path(__file__).resolve().parents[2]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=code,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join((str(code / "backend"), str(code))),
            },
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1500)
            if proc.returncode:
                raise RuntimeError(f"更新器退出码 {proc.returncode}")
            return json.loads(stdout)
        finally:
            if proc.returncode is None:
                try:
                    groups = await asyncio.to_thread(_worker_groups, proc.pid)
                except (OSError, ValueError, subprocess.SubprocessError):
                    groups = {proc.pid}
                _signal_groups(groups, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=4)
                except TimeoutError:
                    pass
                # 父进程可比模型先退出，仍须回收属于这次请求的独立子进程组。
                _signal_groups(groups, signal.SIGKILL)
                await proc.wait()

"""盘后核对中期协已披露的最近月报，保留首次取得时间及原始统计口径。"""

from contextlib import redirect_stdout
from datetime import datetime, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from zipfile import BadZipFile

from scripts.agent_replay import atomic, load
from scripts.collect_monthly_statistics import collect

from .agent_replay import digest
from .study_view import BATCH


def monthly_context(root: Path, cutoff, clock, store_blob):
    observed = clock()
    folder = root / "local-data/rolling-six/monthly"
    day = min(cutoff, observed).date()
    current = folder / day.isoformat()
    progress = current / "check.json"
    status = load(progress) if progress.is_file() else {}
    if not status or (
        status["state"] == "unavailable"
        and (observed - datetime.fromisoformat(status["checked_at"])).total_seconds()
        >= 1800
    ):
        try:
            # 保留真实原件和首次观察时刻，避免命令行的采集计数混入生产者回执。
            with redirect_stdout(StringIO()):
                collect(current, day - timedelta(days=65), day)
            status = {
                "state": "observed",
                "index_sha256": digest(load(current / "index.json")),
            }
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            StopIteration,
            BadZipFile,
        ) as exc:
            status = {"state": "unavailable", "error": type(exc).__name__}
        status["checked_at"] = clock().isoformat()
        atomic(progress, status)

    base = root / "local-data/agent-study" / BATCH
    old = base / "monthly/index.json"
    inputs = []
    if old.is_file():
        baseline = load(old)
        if digest(baseline) != load(base / "run-manifest.json")["monthly_sha256"]:
            raise ValueError("基线中期协月报索引摘要错误")
        inputs.append((old.parent, baseline))
    # 最近一份有效月报即使当天中期协接口暂时不可用，仍应显示其真实月份和时点。
    for entry in sorted(folder.glob("*/check.json"), reverse=True):
        check = load(entry)
        if check["state"] != "observed" or entry.parent.name > day.isoformat():
            continue
        records = load(entry.parent / "index.json")
        if digest(records) != check["index_sha256"]:
            raise ValueError("新采中期协月报索引摘要错误")
        inputs.append((entry.parent, records))
        break
    limit = min(cutoff, clock())
    newest = {}
    for location, records in inputs:
        for item in records:
            if (
                max(
                    datetime.fromisoformat(item[k])
                    for k in ("retrieved_at", "available_at")
                )
                > limit
            ):
                continue
            key = (item["product"], item["kind"])
            if key not in newest or (item["month"], item["available_at"]) >= (
                newest[key][1]["month"],
                newest[key][1]["available_at"],
            ):
                newest[key] = (location, item)
    verified = set()
    for location, item in newest.values():
        for k, suffix in (("page_sha256", ".html"), ("sha256", ".xlsx")):
            h = item[k]
            if h not in verified:
                raw = (location / "originals" / (h + suffix)).read_bytes()
                if sha256(raw).hexdigest() != h:
                    raise ValueError("中期协正文或统计附件摘要错误")
                store_blob(root / "local-data/rolling-six", h, raw)
                verified.add(h)
    return [item for _, item in newest.values()], status

"""将本地保存的模型决策与当时的证据关联展示。"""

from datetime import date
import json
from pathlib import Path
import re

from .agent_replay import digest


BATCH = "2026-05-21_2026-11-20"


def read_results(directory: Path) -> dict:
    path = directory / "results.json"
    if not path.is_file():
        return {"status": "missing", "plans": []}
    report = json.loads(path.read_bytes())
    if (
        report["schema_version"] != 1
        or report["status"] not in {"partial", "complete"}
        or [plan["id"] for plan in report["plans"]] != ["A", "B", "C"]
    ):
        raise ValueError("回测报告版本或策略集合错误")
    return report


def read_context(directory: Path, day: date) -> dict | None:
    report = read_results(directory)
    if not report["plans"]:
        return None
    rows = report["plans"][0]["sessions"]
    session = next((row for row in rows if row["day"] == day.isoformat()), None)
    if not session:
        return None
    key = session["receipt_key"]
    if not re.fullmatch("[a-f0-9]{64}", key):
        raise ValueError("回执索引错误")
    context = json.loads((directory / "receipts" / key / "context.json").read_bytes())
    if (
        context["trade_date"] != session["day"]
        or context["cutoff"] != session["cutoff"]
        or digest(context) != session["context_sha256"]
    ):
        raise ValueError("证据摘要与报告不符")
    return context


def read_document(
    directory: Path, day: date, checksum: str
) -> tuple[bytes, str] | None:
    from hashlib import sha256
    from urllib.parse import urlsplit

    if not re.fullmatch("[a-f0-9]{64}", checksum):
        return None
    packet = read_context(directory, day)
    doc = next(
        (
            row
            for row in (packet or {}).get("documents", [])
            if row.get("sha256") == checksum
        ),
        None,
    )
    if not doc:
        return None
    extension = Path(urlsplit(doc["url"]).path).suffix.lower()
    if extension not in {".htm", ".html", ".xlsx", ".pdf"}:
        extension = ".bin"
    snapshot = directory / "source-snapshot"
    archived = snapshot / "blobs" / checksum
    if not archived.is_file():
        archived = snapshot / "supplement" / "originals" / (checksum + extension)
    raw = archived.read_bytes()
    if sha256(raw).hexdigest() != checksum:
        raise ValueError("历史原件摘要与决策引用不一致")
    return raw, extension

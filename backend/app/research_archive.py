from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import sys
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
from pydantic import Field, HttpUrl, field_validator, model_validator

from .codex_workspace import CodexWorkspace
from .models import StrategyRunRequest, StrictModel, _url_contains_credentials


BJT = ZoneInfo("Asia/Shanghai")
CONTRACT = re.compile(r"[A-Z]{1,3}(?:[0-9]{2})(?:0[1-9]|1[0-2])\Z")
BAR_COLUMNS = ("date", "open", "high", "low", "close", "volume", "hold", "completed")


class MarketObservation(StrictModel):
    product: str
    contract: str
    observed_at: datetime
    available_at: datetime
    source: HttpUrl
    price: float = Field(gt=0, allow_inf_nan=False)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chain_status: str | None = None
    daily_status: str | None = None
    daily_bars: list[dict[str, Any]] = Field(default_factory=list, max_length=120)

    @model_validator(mode="after")
    def validate_observation(self) -> MarketObservation:
        if not CONTRACT.fullmatch(self.contract) or self.contract.rstrip("0123456789") != self.product:
            raise ValueError("行情实际合约与品种不符")
        if self.observed_at.utcoffset() is None or self.available_at.utcoffset() is None:
            raise ValueError("行情时点必须包含时区")
        if self.observed_at > self.available_at:
            raise ValueError("行情时点晚于采集时点")
        if _url_contains_credentials(str(self.source)):
            raise ValueError("行情来源包含凭据")
        return self


class ResearchCapture(StrictModel):
    run_id: str
    captured_at: datetime
    expected_products: list[str]
    observations: list[MarketObservation]
    gaps: dict[str, str]
    policy_hashes: dict[str, str] = Field(default_factory=dict)

    @field_validator("captured_at")
    @classmethod
    def captured_at_has_zone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("采集时点必须包含时区")
        return value

    @model_validator(mode="after")
    def validate_coverage(self) -> ResearchCapture:
        observed = {row.product for row in self.observations}
        if len(observed) != len(self.observations) or observed & self.gaps.keys():
            raise ValueError("研究行情存在重复或冲突记录")
        if observed | self.gaps.keys() != set(self.expected_products):
            raise ValueError("研究行情未解释全部品种")
        if any(row.available_at > self.captured_at for row in self.observations):
            raise ValueError("取证完成时点早于行情可用时点")
        return self


class ResearchArchive:
    """保存已冻结的采集证据，支持按可用时点查询。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = Lock()

    def _connect(self):
        return duckdb.connect(str(self.path), config={"threads": "2", "TimeZone": "UTC"})

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS captures (
                    run_id VARCHAR PRIMARY KEY, body JSON NOT NULL, sha256 VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_objects (
                    sha256 VARCHAR PRIMARY KEY, content BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    run_id VARCHAR NOT NULL, product VARCHAR NOT NULL, contract VARCHAR NOT NULL,
                    observed_at TIMESTAMPTZ NOT NULL, available_at TIMESTAMPTZ NOT NULL,
                    body JSON NOT NULL, PRIMARY KEY (run_id, product)
                );
                CREATE INDEX IF NOT EXISTS observation_contract_time
                    ON observations (contract, available_at);
            """)

    def save(self, capture: ResearchCapture, artifacts: dict[str, bytes]) -> str:
        expected = {row.artifact_sha256 for row in capture.observations}
        if set(artifacts) != expected or any(sha256(blob).hexdigest() != key for key, blob in artifacts.items()):
            raise ValueError("归档行情原件与摘要不符")
        body = capture.model_dump_json()
        digest = sha256(body.encode()).hexdigest()
        with self.lock, self._connect() as db:
            db.execute("BEGIN")
            try:
                existing = db.execute("SELECT sha256 FROM captures WHERE run_id = ?", [capture.run_id]).fetchone()
                if existing:
                    if existing[0] != digest:
                        raise ValueError("已冻结的研究证据不能被替换")
                else:
                    db.execute("INSERT INTO captures VALUES (?, ?, ?)", [capture.run_id, body, digest])
                    if capture.observations:
                        db.executemany(
                            "INSERT INTO evidence_objects VALUES (?, ?) ON CONFLICT DO NOTHING", list(artifacts.items()),
                        )
                        db.executemany(
                            "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                (capture.run_id, row.product, row.contract, row.observed_at,
                                 row.available_at, row.model_dump_json())
                                for row in capture.observations
                            ],
                        )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return digest

    def get(self, run_id: str) -> tuple[ResearchCapture, str] | None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT body, sha256 FROM captures WHERE run_id = ?", [run_id]).fetchone()
        if row is None:
            return None
        body, digest = row
        if sha256(body.encode()).hexdigest() != digest:
            raise ValueError("研究证据摘要不匹配")
        return ResearchCapture.model_validate_json(body), digest

    def evidence(self, run_id: str, digest: str) -> bytes | None:
        capture = self.get(run_id)
        if capture is None or not any(row.artifact_sha256 == digest for row in capture[0].observations):
            return None
        with self.lock, self._connect() as db:
            row = db.execute("SELECT content FROM evidence_objects WHERE sha256 = ?", [digest]).fetchone()
        if row is None or sha256(row[0]).hexdigest() != digest:
            raise ValueError("归档行情原件未通过摘要校验")
        return row[0]

    def latest_for_product(self, product: str) -> tuple[str, MarketObservation] | None:
        with self.lock, self._connect() as db:
            row = db.execute("""
                SELECT run_id, body FROM observations WHERE product = ?
                ORDER BY available_at DESC, observed_at DESC, run_id DESC LIMIT 1
            """, [product]).fetchone()
        return (row[0], MarketObservation.model_validate_json(row[1])) if row else None

    def followup(self, contract: str, start: datetime, cutoff: datetime) -> list[MarketObservation]:
        with self.lock, self._connect() as db:
            rows = db.execute("""
                SELECT body FROM observations
                WHERE contract = ? AND observed_at > ? AND available_at <= ?
                QUALIFY row_number() OVER (PARTITION BY observed_at ORDER BY available_at, run_id) = 1
                ORDER BY observed_at, available_at LIMIT 501
            """, [contract, start, cutoff]).fetchall()
        return [MarketObservation.model_validate_json(row[0]) for row in rows]


def _source_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=BJT)


def _observation(product: str, blob: bytes, captured_at: datetime, contract: str | None) -> MarketObservation | None:
    raw = json.loads(blob)
    if raw.get("success") is not True:
        return None
    quote = raw["analysis_contract"]
    if contract is not None and quote.get("symbol") != contract:
        return None
    available_at = _source_time(raw["fetch_time"])
    if available_at > captured_at:
        return None
    technical = raw.get("technical") or {}
    chain = raw.get("main_selection") or {}
    daily = technical.get("daily_data_evidence") or {}
    return MarketObservation(
        product=product, contract=quote["symbol"], observed_at=_source_time(quote["quote_time"]),
        available_at=captured_at, price=quote["price"], source=quote["source_url"],
        artifact_sha256=sha256(blob).hexdigest(), chain_status=chain.get("selection_claim"),
        daily_status=daily.get("status"),
        daily_bars=[
            {key: bar[key] for key in BAR_COLUMNS if key in bar}
            for bar in (technical.get("completed_daily_rows") or [])[-120:]
            if isinstance(bar, dict)
        ],
    )


def read_run_artifact(run_dir: Path, name: str, max_bytes: int = 2_000_000) -> bytes:
    path = (run_dir / name).resolve(strict=True)
    if run_dir.resolve() != run_dir or not path.is_relative_to(run_dir) or not path.is_file():
        raise ValueError("研究工件路径越出任务目录")
    with path.open("rb") as stream:
        blob = stream.read(max_bytes + 1)
    if len(blob) > max_bytes:
        raise ValueError("研究工件超出限制")
    return blob


async def capture_market(
    workspace: CodexWorkspace, run_id: str, request: StrategyRunRequest, expected: tuple[str, ...],
) -> tuple[ResearchCapture, dict[str, bytes]]:
    scan = request.mode == "all_market_scan"
    if scan:
        command = [
            "scan_market.py", "--output", str(workspace.run_dir / "scan.json"),
            "--workers", "4", "--timeout", "15",
        ]
    else:
        command = [
            "fetch_futures.py", request.requested_contract or request.product,
            "--output", str(workspace.run_dir / "market.json"), "--timeout", "20",
        ]
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(workspace.root / "scripts" / command[0]), *command[1:],
        cwd=workspace.root, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), 110 if scan else 25)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        timed_out = True
    captured_at = datetime.now(timezone.utc)
    observations: list[MarketObservation] = []
    artifacts: dict[str, bytes] = {}
    gaps = dict.fromkeys(expected, "取证超时" if timed_out else "未获得可绑定到实际合约的行情")
    selected: dict[str, dict] = {}
    if scan and not timed_out:
        try:
            manifest = json.loads(read_run_artifact(workspace.run_dir, "scan.json"))
            for entry in manifest["products"]:
                symbol = entry["symbol"]
                if symbol in selected or symbol not in expected:
                    raise ValueError("扫描品种与研究范围不符")
                selected[symbol] = entry
            if set(selected) != set(expected):
                raise ValueError("扫描品种覆盖不完整")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            selected.clear()
    for product in expected:
        if timed_out or (scan and product not in selected):
            continue
        entry = selected.get(product)
        try:
            name = entry["raw_snapshot_path"] if entry else "market.json"
            blob = read_run_artifact(workspace.run_dir, name)
            if entry and sha256(blob).hexdigest() != entry["raw_snapshot_sha256"]:
                raise ValueError("扫描原件摘要不符")
            if row := _observation(product, blob, captured_at, request.requested_contract):
                observations.append(row)
                artifacts[row.artifact_sha256] = blob
                del gaps[product]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    policy_hashes = {
        name: sha256((workspace.root / "config" / name).read_bytes()).hexdigest()
        for name in ("trading_universe.json", "futures_main_report_policy.json", "product_contract_specs.json")
    }
    return ResearchCapture(
        run_id=run_id, captured_at=captured_at, expected_products=list(expected),
        observations=observations, gaps=gaps, policy_hashes=policy_hashes,
    ), artifacts

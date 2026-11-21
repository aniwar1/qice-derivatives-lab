from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Column,
    and_,
    desc,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


SCHEMA_VERSION = 1
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "failed")

metadata = MetaData()

schema_versions = Table(
    "app_schema_versions",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

strategy_runs = Table(
    "strategy_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("request", JSON, nullable=False),
    Column("runtime", JSON, nullable=False),
    Column("result", JSON, nullable=True),
    Column("public_error_code", String(80), nullable=True),
    Column("public_error_message", String(500), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('queued','running','completed','failed')",
        name="ck_strategy_runs_status",
    ),
)

# A partial unique constant-expression index is the cross-process mutex. It
# prevents two API workers from accepting overlapping strategy refreshes.
Index(
    "uq_strategy_runs_single_active",
    strategy_runs.c.id.is_not(None),
    unique=True,
    postgresql_where=strategy_runs.c.status.in_(ACTIVE_STATUSES),
    sqlite_where=strategy_runs.c.status.in_(ACTIVE_STATUSES),
)

run_events = Table(
    "run_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), ForeignKey("strategy_runs.id", ondelete="CASCADE"), nullable=False),
    Column("event_type", String(20), nullable=False),
    Column("stage", String(80), nullable=False),
    Column("message", String(500), nullable=False),
    Column("payload", JSON, nullable=False, default=dict),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "event_type IN ('snapshot','progress','completed','failed','heartbeat')",
        name="ck_run_events_type",
    ),
)
Index("ix_run_events_run_id_id", run_events.c.run_id, run_events.c.id)

dashboard_snapshots = Table(
    "dashboard_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), ForeignKey("strategy_runs.id", ondelete="CASCADE"), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
Index("ix_dashboard_snapshots_created", desc(dashboard_snapshots.c.created_at))


research_publications = Table(
    "research_publications",
    metadata,
    Column("run_id", String(36), ForeignKey("strategy_runs.id"), primary_key=True),
    Column("mode", String(24), nullable=False),
    Column("product", String(3), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)
Index("ix_research_publications_scope", research_publications.c.mode,
      research_publications.c.product, research_publications.c.published_at)


class ActiveRunConflict(RuntimeError):
    def __init__(self, active_run_id: str | None) -> None:
        super().__init__("a strategy refresh is already active")
        self.active_run_id = active_run_id


class RunNotFound(LookupError):
    pass


class InvalidRunTransition(RuntimeError):
    pass


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class StrategyStore:
    """异步保存策略、进度和看板快照。"""

    def __init__(self, database_url: str | URL, *, echo: bool = False) -> None:
        self.database_url = make_url(database_url)
        self.engine: AsyncEngine = create_async_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
        )

    async def initialize(self) -> list[str]:
        """Create schema and fail closed on work interrupted by a prior process."""

        database = self.database_url.database
        if self.database_url.get_backend_name() == "sqlite" and database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)

        async with self.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            known = await connection.scalar(
                select(schema_versions.c.version).where(schema_versions.c.version == SCHEMA_VERSION)
            )
            if known is None:
                await connection.execute(insert(schema_versions).values(version=SCHEMA_VERSION))

            interrupted_result = await connection.execute(
                update(strategy_runs)
                .where(strategy_runs.c.status.in_(ACTIVE_STATUSES))
                .values(
                    status="failed",
                    public_error_code="service_restarted",
                    public_error_message="策略更新因本地服务重启而中止，请重新触发。",
                    completed_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(strategy_runs.c.id)
            )
            interrupted = list(interrupted_result.scalars())
            for run_id in interrupted:
                await connection.execute(
                    insert(run_events).values(
                        run_id=run_id,
                        event_type="failed",
                        stage="recovery",
                        message="本地服务重启，未完成任务已安全终止。",
                        payload={"error_code": "service_restarted"},
                    )
                )
        return interrupted

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(select(1))
            return True
        except Exception:
            return False

    async def create_run(
        self,
        *,
        run_id: str,
        request: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    insert(strategy_runs).values(
                        id=run_id,
                        status="queued",
                        request=request,
                        runtime=runtime,
                    )
                )
                event_result = await connection.execute(
                    insert(run_events)
                    .values(
                        run_id=run_id,
                        event_type="snapshot",
                        stage="queued",
                        message="策略更新已进入队列。",
                        payload={"status": "queued"},
                    )
                    .returning(run_events.c.id)
                )
                event_id = event_result.scalar_one()
        except IntegrityError as exc:
            active = await self.get_active_run()
            raise ActiveRunConflict(active["id"] if active else None) from exc
        row = await self.get_run(run_id)
        assert row is not None
        row["initial_event_id"] = event_id
        return row

    async def mark_running(self, run_id: str) -> None:
        async with self.engine.begin() as connection:
            result = await connection.execute(
                update(strategy_runs)
                .where(and_(strategy_runs.c.id == run_id, strategy_runs.c.status == "queued"))
                .values(status="running", started_at=func.now(), updated_at=func.now())
            )
            if result.rowcount != 1:
                raise InvalidRunTransition(f"run {run_id} is not queued")
            await connection.execute(
                insert(run_events).values(
                    run_id=run_id,
                    event_type="progress",
                    stage="starting",
                    message="正在启动 Codex 决策任务。",
                    payload={"status": "running"},
                )
            )

    async def append_event(
        self,
        run_id: str,
        *,
        event_type: str = "progress",
        stage: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        async with self.engine.begin() as connection:
            exists = await connection.scalar(select(strategy_runs.c.id).where(strategy_runs.c.id == run_id))
            if exists is None:
                raise RunNotFound(run_id)
            result = await connection.execute(
                insert(run_events)
                .values(
                    run_id=run_id,
                    event_type=event_type,
                    stage=stage,
                    message=message,
                    payload=payload or {},
                )
                .returning(run_events.c.id)
            )
            return int(result.scalar_one())

    async def complete_run(
        self, run_id: str, result_payload: dict[str, Any], *, research: dict[str, Any] | None = None,
    ) -> None:
        async with self.engine.begin() as connection:
            changed = await connection.execute(
                update(strategy_runs)
                .where(and_(strategy_runs.c.id == run_id, strategy_runs.c.status == "running"))
                .values(
                    status="completed",
                    result=result_payload,
                    completed_at=func.now(),
                    updated_at=func.now(),
                )
            )
            if changed.rowcount != 1:
                raise InvalidRunTransition(f"run {run_id} is not running")
            if research is not None:
                await connection.execute(insert(research_publications).values(
                    run_id=run_id,
                    mode=research["mode"],
                    product=research["product"] or "",
                    published_at=datetime.fromisoformat(result_payload["updated_at"]).astimezone(timezone.utc),
                    payload=research,
                ))
            await connection.execute(
                insert(run_events).values(
                    run_id=run_id,
                    event_type="completed",
                    stage="completed",
                    message="最新策略已更新。",
                    payload={"status": "completed"},
                )
            )

    async def fail_run(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        async with self.engine.begin() as connection:
            changed = await connection.execute(
                update(strategy_runs)
                .where(
                    and_(
                        strategy_runs.c.id == run_id,
                        strategy_runs.c.status.in_(ACTIVE_STATUSES),
                    )
                )
                .values(
                    status="failed",
                    public_error_code=error_code[:80],
                    public_error_message=error_message[:500],
                    completed_at=func.now(),
                    updated_at=func.now(),
                )
            )
            if changed.rowcount != 1:
                return False
            await connection.execute(
                insert(run_events).values(
                    run_id=run_id,
                    event_type="failed",
                    stage="failed",
                    message=error_message[:500],
                    payload={
                        "status": "failed",
                        "error_code": error_code[:80],
                        **(payload or {}),
                    },
                )
            )
            return True

    async def save_dashboard_snapshot(self, run_id: str, snapshot: dict[str, Any]) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(dashboard_snapshots).values(run_id=run_id, snapshot=snapshot)
            )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(select(strategy_runs).where(strategy_runs.c.id == run_id))
            ).mappings().first()
        return self._run_mapping(row) if row else None

    async def get_active_run(self) -> dict[str, Any] | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(strategy_runs)
                    .where(strategy_runs.c.status.in_(ACTIVE_STATUSES))
                    .order_by(desc(strategy_runs.c.created_at))
                    .limit(1)
                )
            ).mappings().first()
        return self._run_mapping(row) if row else None

    async def get_latest_completed(self) -> dict[str, Any] | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(strategy_runs)
                    .where(strategy_runs.c.status == "completed")
                    .order_by(desc(strategy_runs.c.completed_at), desc(strategy_runs.c.created_at))
                    .limit(1)
                )
            ).mappings().first()
        return self._run_mapping(row) if row else None

    async def latest_product_strategy(self, product: str) -> dict[str, Any] | None:
        async with self.engine.connect() as connection:
            row = (await connection.execute(
                select(strategy_runs).where(
                    strategy_runs.c.status == "completed",
                    strategy_runs.c.result["strategy"]["product"].as_string() == product,
                ).order_by(desc(strategy_runs.c.completed_at), desc(strategy_runs.c.created_at)).limit(1)
            )).mappings().first()
        return self._run_mapping(row) if row else None

    async def list_recent_runs(self, limit: int) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(
                select(strategy_runs)
                .order_by(desc(strategy_runs.c.created_at), desc(strategy_runs.c.id))
                .limit(limit)
            )).mappings().all()
        return [self._run_mapping(row) for row in rows]

    async def research_publication(self, run_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as connection:
            return await connection.scalar(select(research_publications.c.payload).where(
                research_publications.c.run_id == run_id,
            ))

    async def previous_research_publication(self, run_id: str) -> tuple[str, dict[str, Any]] | None:
        current = research_publications.alias("current_publication")
        async with self.engine.connect() as connection:
            row = (await connection.execute(
                select(research_publications.c.run_id, research_publications.c.payload)
                .join(current, and_(current.c.mode == research_publications.c.mode,
                                    current.c.product == research_publications.c.product))
                .where(current.c.run_id == run_id,
                       research_publications.c.published_at < current.c.published_at)
                .order_by(desc(research_publications.c.published_at))
                .limit(1)
            )).first()
        return (row.run_id, row.payload) if row else None

    async def get_events_after(self, run_id: str, event_id: int) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            exists = await connection.scalar(select(strategy_runs.c.id).where(strategy_runs.c.id == run_id))
            if exists is None:
                raise RunNotFound(run_id)
            rows = (
                await connection.execute(
                    select(run_events)
                    .where(and_(run_events.c.run_id == run_id, run_events.c.id > event_id))
                    .order_by(run_events.c.id)
                )
            ).mappings().all()
        return [
            {
                "event_id": int(row["id"]),
                "run_id": row["run_id"],
                "type": row["event_type"],
                "stage": row["stage"],
                "message": row["message"],
                "occurred_at": _ensure_aware(row["occurred_at"]),
                "payload": row["payload"] or {},
            }
            for row in rows
        ]

    async def get_latest_saved_dashboard(self) -> dict[str, Any] | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(dashboard_snapshots.c.snapshot)
                    .order_by(desc(dashboard_snapshots.c.created_at), desc(dashboard_snapshots.c.id))
                    .limit(1)
                )
            ).first()
        return row[0] if row else None

    @staticmethod
    def _run_mapping(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "status": row["status"],
            "request": row["request"],
            "runtime": row["runtime"],
            "result": row["result"],
            "public_error_code": row["public_error_code"],
            "public_error_message": row["public_error_message"],
            "created_at": _ensure_aware(row["created_at"]),
            "started_at": _ensure_aware(row["started_at"]),
            "completed_at": _ensure_aware(row["completed_at"]),
            "updated_at": _ensure_aware(row["updated_at"]),
        }

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from .codex_gateway import CodexGateway, CodexGatewayError
from .codex_workspace import CodexWorkspace
from .config import Settings
from .dashboard import DashboardProjector, DomainSnapshotProvider, public_run_view
from .db import StrategyStore
from .freshness import LatestStrategyResolution, resolve_latest_strategy
from .models import AgentStrategyPayload, LatestStrategyRecord, StrategyRunAccepted, StrategyRunRequest
from .prompting import build_strategy_prompt


BJT = ZoneInfo("Asia/Shanghai")
# A Codex turn can legitimately take a few minutes while it gathers live
# evidence.  Persist a small, user-visible liveness event during that wait so
# the SSE client does not look frozen.  This is deliberately shorter than the
# gateway's hard deadline and contains no hidden model reasoning.
_CODEX_PROGRESS_INTERVAL_SECONDS = 10.0


class InvalidStrategyScope(ValueError):
    pass


class StrategyValidationError(ValueError):
    pass


class StrategyService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: StrategyStore,
        gateway: CodexGateway,
        domain: DomainSnapshotProvider | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.gateway = gateway
        self.domain = domain or DomainSnapshotProvider(settings.workspace_root)
        self.dashboard = DashboardProjector(store, gateway, self.domain)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    async def start(self) -> None:
        await self.store.initialize()
        if self.settings.auto_refresh_if_empty:
            latest, active = await asyncio.gather(
                self.store.get_latest_completed(), self.store.get_active_run()
            )
            latest_result = latest.get("result") if latest is not None else None
            publishable = resolve_latest_strategy(latest_result).record
            if publishable is None and active is None:
                await self.trigger(StrategyRunRequest())

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()

    async def trigger(self, request: StrategyRunRequest) -> StrategyRunAccepted:
        allowed = self.domain.allowed_products()
        if request.product and request.product not in allowed:
            raise InvalidStrategyScope(f"product {request.product} is outside the configured universe")

        run_id = str(uuid4())
        runtime = {
            "provider": "codex",
            "model": self.settings.model_label,
            "model_resolution": "explicit" if self.settings.codex_model else "inherited",
            "browser_direct_access": False,
        }
        await self.store.create_run(
            run_id=run_id,
            request=request.model_dump(mode="json"),
            runtime=runtime,
        )
        task = asyncio.create_task(self._execute(run_id, request, allowed), name=f"strategy-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task, rid=run_id: self._tasks.pop(rid, None))
        return StrategyRunAccepted(
            run_id=run_id,
            status="queued",
            events_url=f"/api/runs/{run_id}/events",
        )

    async def _execute(
        self,
        run_id: str,
        request: StrategyRunRequest,
        allowed_products: tuple[str, ...],
    ) -> None:
        progress_task: asyncio.Task[None] | None = None
        codex_workspace: CodexWorkspace | None = None
        codex_callback_received = False
        try:
            await self.store.mark_running(run_id)
            await self.store.append_event(
                run_id,
                stage="evidence",
                message=f"已装载 {len(allowed_products)} 品种范围、时点与证据门槛。",
                payload={
                    "partial_daily_bar_allowed": True,
                    "codex_workspace_isolated": True,
                },
            )
            try:
                # Evidence collectors write manifests and content-addressed
                # files. Run Codex in a disposable copy so workspace-write
                # cannot mutate the live source tree or persistent state.
                codex_workspace = await asyncio.to_thread(
                    CodexWorkspace.create,
                    self.settings.workspace_root,
                    run_id,
                )
            except Exception as exc:
                raise CodexGatewayError(
                    "codex_workspace_unavailable",
                    "无法建立 Codex 隔离工作区，旧策略保持不变。",
                ) from exc
            decision_clock = datetime.now(tz=BJT)
            prompt = build_strategy_prompt(
                request,
                run_id=run_id,
                now=decision_clock,
                allowed_products=allowed_products,
                run_dir=codex_workspace.relative_run_dir,
            )
            await self.store.append_event(
                run_id,
                stage="agent",
                message="已向 Codex 发出决策请求，等待结构化返回。",
                payload={
                    "codex_request_sent": True,
                    "provider": "codex",
                },
            )
            # Keep the run observable while the single Codex call is in
            # flight.  ``run_event_stream`` heartbeats are SSE comments and
            # therefore are intentionally invisible to EventSource
            # ``onmessage`` handlers; these persisted heartbeat events are
            # delivered as normal named SSE events instead.
            progress_task = asyncio.create_task(
                self._codex_progress_heartbeat(run_id),
                name=f"strategy-progress-{run_id}",
            )
            try:
                gateway_result = await self.gateway.run_strategy(
                    prompt,
                    workspace_root=codex_workspace.root,
                )
            finally:
                await self._stop_progress_heartbeat(progress_task)
                progress_task = None
            codex_callback_received = True
            await self.store.append_event(
                run_id,
                stage="agent",
                message="Codex 已回调并返回结构化策略，正在进入事实校验。",
                payload={
                    "codex_callback": True,
                    "provider": gateway_result.runtime.provider,
                    "transport": gateway_result.runtime.transport,
                    "model": gateway_result.runtime.model,
                },
            )
            await self.store.append_event(
                run_id,
                stage="validation",
                message="正在校验时点、半日线语义与执行边界。",
                payload={
                    "codex_callback": True,
                    "transport": gateway_result.runtime.transport,
                    "model": gateway_result.runtime.model,
                },
            )
            # Re-validate the complete public payload at the persistence
            # boundary even though transports already parse it.  This guards
            # against mutated/custom gateway objects and makes the two gates
            # explicit: Pydantic shape first, then service-level facts.
            validated_payload = AgentStrategyPayload.model_validate(
                gateway_result.payload.model_dump(mode="python")
            )
            self._validate_result(validated_payload, request, allowed_products)
            record = LatestStrategyRecord(
                run_id=run_id,
                updated_at=datetime.now(tz=BJT),
                source=gateway_result.runtime,
                strategy=validated_payload,
            )
            record_payload = record.model_dump(mode="json")
            await self.store.complete_run(run_id, record_payload)
            try:
                snapshot = await self.dashboard.project()
                await self.store.save_dashboard_snapshot(run_id, snapshot)
            except Exception:
                # A stored final strategy remains authoritative even if its
                # disposable dashboard projection cannot be materialized.
                pass
        except asyncio.CancelledError:
            if progress_task is not None:
                await self._stop_progress_heartbeat(progress_task)
                progress_task = None
            if not self._stopping:
                await self.store.fail_run(
                    run_id,
                    error_code="run_cancelled",
                    error_message="策略任务已取消，未保存不完整结果。",
                    payload={"codex_callback": codex_callback_received},
                )
            else:
                await self.store.fail_run(
                    run_id,
                    error_code="service_stopped",
                    error_message="本地服务停止，未完成策略已安全终止。",
                    payload={"codex_callback": codex_callback_received},
                )
            raise
        except CodexGatewayError as exc:
            message = exc.public_message
            if not codex_callback_received:
                message = f"{message} 未收到可校验的 Codex 结构化回调，旧策略保持不变。"
            await self.store.fail_run(
                run_id,
                error_code=exc.code,
                error_message=message,
                payload={"codex_callback": codex_callback_received},
            )
        except (StrategyValidationError, ValueError) as exc:
            await self.store.fail_run(
                run_id,
                error_code="strategy_validation_failed",
                error_message="Codex 返回的策略未通过事实与边界校验，旧策略保持不变。",
                payload={"codex_callback": codex_callback_received},
            )
        except Exception:
            await self.store.fail_run(
                run_id,
                error_code="strategy_run_failed",
                error_message="策略更新失败，旧策略保持不变。",
                payload={"codex_callback": codex_callback_received},
            )
        finally:
            if progress_task is not None:
                await self._stop_progress_heartbeat(progress_task)
            if codex_workspace is not None:
                # The gateway closes its child before returning or raising;
                # cleanup is therefore bounded to this exact staging tree.
                codex_workspace.cleanup()

    async def _codex_progress_heartbeat(self, run_id: str) -> None:
        """Persist bounded liveness updates while the Codex turn is running.

        A heartbeat is best-effort: a transient database error must never turn
        a valid Codex result into a failed strategy run.  Cancellation is
        propagated so the parent can deterministically clean up this task.
        """

        started = asyncio.get_running_loop().time()
        try:
            while True:
                await asyncio.sleep(_CODEX_PROGRESS_INTERVAL_SECONDS)
                elapsed = max(
                    0, int(asyncio.get_running_loop().time() - started)
                )
                try:
                    await self.store.append_event(
                        run_id,
                        event_type="heartbeat",
                        stage="agent",
                        message=f"Codex 正在生成策略（已运行 {elapsed} 秒）。",
                        payload={
                            "status": "running",
                            "codex_call_active": True,
                            "elapsed_seconds": elapsed,
                        },
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Liveness is advisory; the gateway result and terminal
                    # run state remain authoritative if this write fails.
                    return
        except asyncio.CancelledError:
            raise

    @staticmethod
    async def _stop_progress_heartbeat(task: asyncio.Task[None]) -> None:
        if task.done():
            # Retrieve the result so an unexpected task exception does not
            # become an unhandled warning during normal run completion.
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def _validate_result(
        self,
        result: AgentStrategyPayload,
        request: StrategyRunRequest,
        allowed_products: tuple[str, ...],
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        as_of = result.as_of.astimezone(timezone.utc)
        quality_review = result.quality_review
        if quality_review.critic_revisions != 1:
            raise StrategyValidationError("strategy requires exactly one bounded critic revision")
        if quality_review.unresolved_material_issue is not None:
            raise StrategyValidationError("strategy has an unresolved material critic issue")
        if not quality_review.strongest_challenge.strip() or not quality_review.resolution.strip():
            raise StrategyValidationError("strategy critic review is not explicit")
        if result.order_placement_authorized is not False or result.short_option_open_allowed is not False:
            raise StrategyValidationError("strategy weakens execution permissions")
        if self.domain.allowed_products() != allowed_products:
            raise StrategyValidationError("configured universe changed while the strategy was running")
        if as_of > now + timedelta(minutes=5):
            raise StrategyValidationError("strategy as_of is future-dated")
        if as_of < now - timedelta(hours=2):
            raise StrategyValidationError("strategy as_of is not current")
        if result.product and result.product not in allowed_products:
            raise StrategyValidationError("strategy product is outside configured universe")
        scanned = tuple(item.product for item in result.scan_coverage.products)
        if request.mode == "all_market_scan":
            if result.scan_coverage.scope != "all_market" or set(scanned) != set(allowed_products):
                raise StrategyValidationError("all-market scan does not exactly cover configured universe")
        else:
            if (
                result.scan_coverage.scope != "single_product"
                or scanned != (request.product,)
            ):
                raise StrategyValidationError("single-product scan coverage conflicts with request")
        if request.product and result.product != request.product:
            raise StrategyValidationError("strategy product differs from requested product")
        if request.requested_contract and result.analysis_contract != request.requested_contract:
            raise StrategyValidationError("analysis contract differs from requested contract")
        if result.product:
            focus_rows = [item for item in result.scan_coverage.products if item.product == result.product]
            if len(focus_rows) != 1:
                raise StrategyValidationError("selected product has no unique scan row")
            focus = focus_rows[0]
            if result.actual_main_contract != focus.actual_main_contract:
                raise StrategyValidationError("selected actual-main contract differs from scan row")
            if result.decision != "wait" and focus.status == "unobservable":
                raise StrategyValidationError("an unobservable scan row cannot support a capital action")
        if (
            result.decision != "wait"
            and request.requested_contract is None
            and result.analysis_contract != result.actual_main_contract
        ):
            raise StrategyValidationError("analysis must use the actual main unless explicitly requested")
        if not request.option_analysis_requested and result.decision in {"long_call", "long_put"}:
            raise StrategyValidationError("option action was not requested")
        evidence_domains = (
            result.technical_structure,
            result.fundamentals_news,
            result.member_flow,
            result.option_market,
        )
        for domain in evidence_domains:
            retrieval_times = [source.retrieved_at.astimezone(timezone.utc) for source in domain.sources]
            if any(retrieved_at > as_of + timedelta(minutes=5) for retrieved_at in retrieval_times):
                raise StrategyValidationError("evidence source retrieval is future-dated")
            if (
                domain.freshness == "fresh"
                and retrieval_times
                and max(retrieval_times) < as_of - timedelta(hours=2)
            ):
                raise StrategyValidationError("fresh evidence has no current retrieval")
        if result.session_daily_bar is not None:
            try:
                self.domain.validate_session_bar(
                    result.session_daily_bar.model_dump(mode="json"),
                    expected_product=result.product,
                    expected_contract=result.analysis_contract,
                    packet_observed_at=result.as_of,
                    now=datetime.now(tz=BJT),
                )
            except Exception as exc:
                raise StrategyValidationError("session-aware daily bar failed domain validation") from exc
        elif result.decision != "wait":
            raise StrategyValidationError("capital action requires session-aware daily-bar evidence")
        if result.decision != "wait":
            quote_at = result.market_levels.quote_at
            assert quote_at is not None
            quote_at_utc = quote_at.astimezone(timezone.utc)
            if not as_of - timedelta(minutes=30) <= quote_at_utc <= as_of + timedelta(minutes=5):
                raise StrategyValidationError("underlying quote is not current for this decision")
            if result.technical_structure.freshness != "fresh":
                raise StrategyValidationError("a capital action requires fresh technical evidence")
        if result.decision in {"long_call", "long_put"}:
            option = result.option_expression
            assert option is not None
            if result.option_market.freshness != "fresh":
                raise StrategyValidationError("long option entry requires a fresh option market")
            if option.bid is None or option.ask is None:
                raise StrategyValidationError("long option entry requires executable bid and ask")
            option_quote_at = option.quote_at.astimezone(timezone.utc)
            if not as_of - timedelta(minutes=30) <= option_quote_at <= as_of + timedelta(minutes=5):
                raise StrategyValidationError("option quote is not current for this decision")
            if option.expiration_date <= result.as_of.date():
                raise StrategyValidationError("long option must expire after the decision date")

    async def get_run(self, run_id: str):
        row = await self.store.get_run(run_id)
        return public_run_view(row) if row else None

    async def latest_strategy(self) -> LatestStrategyRecord | None:
        return (await self.latest_strategy_resolution()).record

    async def latest_strategy_resolution(self) -> LatestStrategyResolution:
        row = await self.store.get_latest_completed()
        return resolve_latest_strategy(row["result"] if row is not None else None)

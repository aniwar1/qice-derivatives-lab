from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import signal
from typing import Any, Protocol

from .config import Settings
from .models import AgentStrategyPayload, CodexRuntimeMetadata


# ``asyncio.wait_for`` waits for a cancelled coroutine to acknowledge
# cancellation, so it is not a hard deadline when an SDK transport is stuck.
# These small, fixed cleanup budgets keep the request bounded after its
# configured decision deadline has elapsed.
_SDK_CLOSE_GRACE_SECONDS = 1.0
_SDK_PROCESS_TERM_GRACE_SECONDS = 0.25
_SDK_PROCESS_KILL_GRACE_SECONDS = 0.25
_SDK_RUN_SETTLE_SECONDS = 0.25
_SDK_PROCESS_POLL_SECONDS = 0.02

# The strategy runtime needs the local skill, shell, and public web evidence,
# not the user's general-purpose plugin/app surface.  Keep this transport-
# neutral tuple as the single source of truth for both SDK app-server and CLI
# fallback launches.  The local network path cannot establish Codex's optional
# WebSocket transport, so the custom provider keeps the same ChatGPT-managed
# Codex endpoint and Responses wire protocol while explicitly selecting HTTP
# streaming.  This remains a Codex app-server/CLI concern, not a direct API
# integration owned by the application.  Native live web search replaces the
# disabled browser plugin without enumerating or mutating the user's installed
# plugin list.
# Shell snapshots are also disabled for this disposable decision run: stale
# snapshot cleanup can otherwise recursively scan unrelated rollout history
# before ``thread/start`` returns.
_STRATEGY_CODEX_CONFIG_OVERRIDES = (
    'model_provider="chatgpt-http"',
    'model_providers.chatgpt-http.name="ChatGPT HTTP"',
    'model_providers.chatgpt-http.base_url="https://chatgpt.com/backend-api/codex"',
    'model_providers.chatgpt-http.wire_api="responses"',
    "model_providers.chatgpt-http.requires_openai_auth=true",
    "model_providers.chatgpt-http.supports_websockets=false",
    "model_providers.chatgpt-http.supports_standalone_web_search=true",
    "features.memories=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.apps=false",
    "features.shell_snapshot=false",
    "features.unbounded_connection_retries=false",
    # Codex's workspace-write preset disables shell network by default.  The
    # Skill's public quote/calendar collectors need outbound HTTPS; the model
    # still runs in a disposable staging workspace with no approval or order
    # tools, so enabling this does not expose the live application checkout.
    "sandbox_workspace_write.network_access=true",
    'web_search="live"',
)


def _agent_schema_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "codex_strategy_decision.agent.schema.json"
    )


def _structured_output_wire_schema() -> dict[str, Any]:
    """Return the strict-output subset; full Pydantic validation remains final.

    The canonical public schema intentionally contains conditional ``allOf`` /
    ``if`` rules that the Responses structured-output dialect does not accept.
    Those same semantics and scalar constraints are enforced by
    ``AgentStrategyPayload`` and the service boundary after generation.  Keep
    the model-facing contract to the portable structural subset instead of
    depending on provider-specific format or range keywords.
    """

    schema_path = _agent_schema_path()
    try:
        canonical = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexGatewayError(
            "schema_missing", "策略输出契约缺失或损坏，任务未启动。"
        ) from exc

    def project(value: Any) -> Any:
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, dict):
            return value
        projected: dict[str, Any] = {}
        union_types: list[Any] | None = None
        for key, item in value.items():
            if key in {
                "$schema",
                "$id",
                "allOf",
                "if",
                "then",
                "else",
                "format",
                "pattern",
                "minLength",
                "maxLength",
                "minimum",
                "maximum",
                "exclusiveMinimum",
                "exclusiveMaximum",
                "minItems",
                "maxItems",
                "uniqueItems",
            }:
                continue
            if key == "const":
                projected["enum"] = [project(item)]
            elif key == "type" and isinstance(item, list):
                union_types = item
            else:
                projected["anyOf" if key == "oneOf" else key] = project(item)
        if union_types is not None:
            projected["anyOf"] = [
                {"type": type_name} for type_name in union_types
            ]
        return projected

    return project(canonical)


class CodexGatewayError(RuntimeError):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class CodexRunResult:
    payload: AgentStrategyPayload
    runtime: CodexRuntimeMetadata


class CodexGateway(Protocol):
    async def status(self, *, refresh: bool = False) -> CodexRuntimeMetadata: ...

    async def run_strategy(
        self, prompt: str, *, workspace_root: Path | None = None
    ) -> CodexRunResult: ...


def _extract_version(text: str) -> str | None:
    match = re.search(r"\b(?:codex-cli\s+)?([0-9]+(?:\.[0-9A-Za-z-]+)+)\b", text)
    return match.group(1) if match else None


async def _run_metadata_command(*args: str, timeout: float = 6.0) -> tuple[int, str]:
    """Run a bounded local command and retain output only in process memory."""

    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process(process)
        raise
    except asyncio.TimeoutError:
        if process is not None:
            await _terminate_process(process)
        return 127, ""
    except OSError:
        return 127, ""
    return process.returncode or 0, stdout.decode("utf-8", errors="replace")[:4096]


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate the Codex process group so app-server children cannot outlive a run."""

    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    except OSError:
        try:
            process.terminate()
        except ProcessLookupError:
            await process.wait()
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        await process.wait()
        return
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            await process.wait()
            return
    await process.wait()


def _sdk_owned_app_server(codex: Any) -> Any | None:
    """Return only the app-server process owned by this SDK client.

    The current SDK exposes no public hard-abort method. Its documented
    ``close`` path ultimately owns this exact ``Popen`` instance at
    ``AsyncCodex -> AsyncCodexClient -> CodexClient``. Keeping the lookup
    narrow is deliberate: never discover by process name or signal a process
    group, either of which could include the desktop Codex runtime.
    """

    async_client = getattr(codex, "_client", None)
    sync_client = getattr(async_client, "_sync", None)
    process = getattr(sync_client, "_proc", None)
    if process is None or not callable(getattr(process, "poll", None)):
        return None
    return process


def _sdk_process_is_running(process: Any) -> bool:
    try:
        return process.poll() is None
    except (OSError, ValueError):
        return False


async def _wait_for_sdk_process_exit(process: Any, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    while _sdk_process_is_running(process):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_SDK_PROCESS_POLL_SECONDS, remaining))
    return True


async def _terminate_sdk_app_server(process: Any | None) -> None:
    """Boundedly stop one SDK-owned app-server without touching its process group."""

    if process is None or not _sdk_process_is_running(process):
        return
    try:
        process.terminate()
    except (OSError, ProcessLookupError, ValueError):
        return
    if await _wait_for_sdk_process_exit(process, _SDK_PROCESS_TERM_GRACE_SECONDS):
        return
    try:
        process.kill()
    except (OSError, ProcessLookupError, ValueError):
        return
    await _wait_for_sdk_process_exit(process, _SDK_PROCESS_KILL_GRACE_SECONDS)


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a detached cleanup result so it cannot emit warning noise."""

    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def _settle_cancelled_task(task: asyncio.Task[Any]) -> None:
    if task.done():
        _consume_background_task(task)
        return
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=_SDK_RUN_SETTLE_SECONDS)
    if task in done:
        _consume_background_task(task)
    else:
        task.add_done_callback(_consume_background_task)


async def _task_finished_by(
    task: asyncio.Task[Any], *, deadline: float
) -> bool:
    """Wait for one SDK phase without extending the shared lifecycle deadline."""

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return task.done()
    done, _ = await asyncio.wait({task}, timeout=remaining)
    return task in done


async def _close_sdk_client(
    codex: Any, *, force: bool, timeout: float | None = None
) -> bool:
    """Close one SDK client within a hard bound.

    ``timeout`` lets a successful run spend only the time left in the shared
    SDK lifecycle deadline.  The boolean reports whether either the public
    close path or the exact-process fallback left no owned app-server running.
    """

    process = _sdk_owned_app_server(codex)
    close_task: asyncio.Task[Any] | None = None
    close = getattr(codex, "close", None)
    if callable(close):
        try:
            close_result = close()
        except Exception:
            close_result = None
        if inspect.isawaitable(close_result):
            close_task = asyncio.create_task(close_result, name="codex-sdk-close")

    # At a decision timeout/cancellation, request termination immediately.
    # On the normal path, first give the SDK's public close method a brief
    # opportunity to release pipes, router subscriptions, and reader threads.
    if force:
        await _terminate_sdk_app_server(process)

    if close_task is not None:
        close_budget = _SDK_CLOSE_GRACE_SECONDS
        if timeout is not None:
            close_budget = min(close_budget, max(0.0, timeout))
        done, _ = await asyncio.wait({close_task}, timeout=close_budget)
        if close_task in done:
            try:
                close_task.result()
            except (asyncio.CancelledError, Exception):
                await _terminate_sdk_app_server(process)
            return process is None or not _sdk_process_is_running(process)

    if not force:
        await _terminate_sdk_app_server(process)
    if close_task is not None and not close_task.done():
        close_task.cancel()
        close_task.add_done_callback(_consume_background_task)
    return process is None or not _sdk_process_is_running(process)


class LocalCodexGateway:
    """Codex-only decision adapter.

    It prefers the stable Python SDK, which owns a local app-server over stdio.
    If the package is unavailable, it uses a bounded, ephemeral `codex exec`
    process. Neither path calls the Responses API or accepts browser access.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sdk_module: Any | None = None
        if settings.prefer_codex_sdk:
            try:
                candidate = importlib.import_module("openai_codex")
            except (ImportError, ModuleNotFoundError):
                candidate = None
            if (
                candidate is not None
                and getattr(candidate, "AsyncCodex", None) is not None
                and getattr(candidate, "CodexConfig", None) is not None
                and getattr(getattr(candidate, "Sandbox", None), "workspace_write", None) is not None
                and getattr(getattr(candidate, "ApprovalMode", None), "deny_all", None) is not None
            ):
                self._sdk_module = candidate
        self._status_cache: CodexRuntimeMetadata | None = None
        self._status_cache_time = 0.0

    @property
    def transport(self) -> str:
        return "python_sdk_stdio" if self._sdk_module is not None else "cli_exec_json"

    async def status(self, *, refresh: bool = False) -> CodexRuntimeMetadata:
        loop_time = asyncio.get_running_loop().time()
        if not refresh and self._status_cache and loop_time - self._status_cache_time < 30:
            return self._status_cache

        login_code, login_text = await _run_metadata_command(
            self.settings.codex_bin, "login", "status"
        )
        lowered = login_text.lower()
        if login_code != 0:
            auth_mode = "unauthenticated"
            authenticated = False
        elif "chatgpt" in lowered or "chat gpt" in lowered:
            auth_mode = "chatgpt_managed"
            authenticated = True
        elif "api key" in lowered or "openai_api_key" in lowered:
            auth_mode = "api_key"
            authenticated = True
        else:
            auth_mode = "unknown"
            authenticated = True

        version_code, version_text = await _run_metadata_command(
            self.settings.codex_bin, "--version"
        )
        metadata = CodexRuntimeMetadata(
            transport=self.transport,
            auth_mode=auth_mode,
            authenticated=authenticated,
            model=self.settings.model_label,
            model_resolution="explicit" if self.settings.codex_model else "inherited",
            version=_extract_version(version_text) if version_code == 0 else None,
        )
        self._status_cache = metadata
        self._status_cache_time = loop_time
        return metadata

    async def run_strategy(
        self, prompt: str, *, workspace_root: Path | None = None
    ) -> CodexRunResult:
        decision_workspace = (
            workspace_root.expanduser().resolve()
            if workspace_root is not None
            else self.settings.workspace_root.resolve()
        )
        if not decision_workspace.is_dir():
            raise CodexGatewayError(
                "codex_workspace_unavailable",
                "Codex 隔离工作区不可用，未更新策略。",
            )
        runtime = await self.status(refresh=True)
        if not runtime.authenticated:
            raise CodexGatewayError(
                "codex_auth_required",
                "Codex 尚未登录，请先在本机完成 ChatGPT 登录。",
            )
        if self.settings.require_chatgpt_auth and runtime.auth_mode != "chatgpt_managed":
            raise CodexGatewayError(
                "chatgpt_auth_required",
                "当前服务只接受 Codex 的 ChatGPT 托管登录，请切换登录方式后重试。",
            )

        if self._sdk_module is not None:
            try:
                if workspace_root is None:
                    payload = await self._run_sdk(prompt)
                else:
                    payload = await self._run_sdk(
                        prompt, workspace_root=decision_workspace
                    )
            except CodexGatewayError as exc:
                if exc.code != "codex_sdk_failed":
                    raise
                # The fallback is ephemeral and runs in the same disposable
                # workspace, so a broken SDK transport cannot place an order or
                # mutate the live application checkout.
                if workspace_root is None:
                    payload = await self._run_cli(prompt)
                else:
                    payload = await self._run_cli(
                        prompt, workspace_root=decision_workspace
                    )
                runtime = runtime.model_copy(update={"transport": "cli_exec_json"})
        else:
            if workspace_root is None:
                payload = await self._run_cli(prompt)
            else:
                payload = await self._run_cli(
                    prompt, workspace_root=decision_workspace
                )
        return CodexRunResult(payload=payload, runtime=runtime)

    async def _run_sdk(
        self, prompt: str, *, workspace_root: Path | None = None
    ) -> AgentStrategyPayload:
        module = self._sdk_module
        assert module is not None
        AsyncCodex = getattr(module, "AsyncCodex", None)
        CodexConfig = getattr(module, "CodexConfig", None)
        Sandbox = getattr(module, "Sandbox", None)
        ApprovalMode = getattr(module, "ApprovalMode", None)
        if (
            AsyncCodex is None
            or CodexConfig is None
            or Sandbox is None
            or getattr(Sandbox, "workspace_write", None) is None
            or ApprovalMode is None
            or getattr(ApprovalMode, "deny_all", None) is None
        ):
            raise CodexGatewayError(
                "codex_sdk_incompatible",
                "已安装的 Codex Python SDK 缺少安全异步入口，请升级 SDK 或切换 CLI 后备。",
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.codex_timeout_seconds
        codex: Any | None = None
        phase_task: asyncio.Task[Any] | None = None
        client_closed = False
        try:
            config_kwargs: dict[str, Any] = {
                "config_overrides": _STRATEGY_CODEX_CONFIG_OVERRIDES,
            }
            if self.settings.codex_bin_explicit:
                config_kwargs["codex_bin"] = self.settings.codex_bin
            client_kwargs: dict[str, Any] = {
                "config": CodexConfig(**config_kwargs),
            }
            # The SDK constructor is local and lazy; external initialization
            # begins in thread_start below.  Start the shared clock before even
            # this synchronous construction so it cannot receive a fresh
            # timeout budget afterward.
            codex = AsyncCodex(**client_kwargs)
            if loop.time() >= deadline:
                await _close_sdk_client(codex, force=True)
                client_closed = True
                raise CodexGatewayError(
                    "codex_timeout", "Codex 决策超时，任务已安全终止。"
                )
            start_kwargs: dict[str, Any] = {}
            parameters = inspect.signature(codex.thread_start).parameters
            required_safety_parameters = {"cwd", "sandbox", "approval_mode", "ephemeral"}
            if not required_safety_parameters.issubset(parameters):
                raise CodexGatewayError(
                    "codex_sdk_failed",
                    "Codex Python SDK 缺少所需的隔离参数，正在切换安全后备。",
                )
            start_kwargs["cwd"] = str(
                (workspace_root or self.settings.workspace_root).resolve()
            )
            start_kwargs["sandbox"] = Sandbox.workspace_write
            start_kwargs["approval_mode"] = ApprovalMode.deny_all
            start_kwargs["ephemeral"] = True
            if self.settings.codex_model:
                start_kwargs["model"] = self.settings.codex_model
            start_deadline = min(
                deadline,
                loop.time() + max(0.0, self.settings.codex_start_timeout_seconds),
            )
            phase_task = asyncio.create_task(
                codex.thread_start(**start_kwargs), name="codex-sdk-thread-start"
            )
            if not await _task_finished_by(phase_task, deadline=start_deadline):
                await _close_sdk_client(codex, force=True)
                client_closed = True
                await _settle_cancelled_task(phase_task)
                raise CodexGatewayError(
                    "codex_sdk_failed",
                    "Codex Python SDK 启动超时，正在切换安全后备。",
                )
            thread = phase_task.result()
            phase_task = None
            if (
                _sdk_owned_app_server(codex) is None
                or not callable(getattr(codex, "close", None))
            ):
                raise CodexGatewayError(
                    "codex_sdk_failed",
                    "Codex Python SDK 缺少独立 app-server 关闭能力，正在切换安全后备。",
                )
            run_kwargs: dict[str, Any] = {}
            run_parameters = inspect.signature(thread.run).parameters
            if "output_schema" in run_parameters:
                run_kwargs["output_schema"] = _structured_output_wire_schema()
            # Keep the workspace policy on ``thread/start`` only.  Passing
            # ``Sandbox.workspace_write`` again to ``turn.run`` creates a
            # turn-level policy whose default ``networkAccess`` is false and
            # therefore overrides the thread config below.  The result is a
            # misleading, reproducible DNS failure for the public quote
            # collectors even though the disposable thread was configured
            # with ``sandbox_workspace_write.network_access=true``.  The
            # thread-level workspace-write policy is inherited by the turn;
            # omitting this override preserves that explicit network setting.
            if "approval_mode" in run_parameters:
                run_kwargs["approval_mode"] = ApprovalMode.deny_all

            phase_task = asyncio.create_task(
                thread.run(prompt, **run_kwargs), name="codex-sdk-turn"
            )
            if not await _task_finished_by(phase_task, deadline=deadline):
                # Unlike wait_for(), asyncio.wait() returns at the deadline
                # without waiting for an uncooperative cancellation. Closing
                # and, if needed, killing this client's exact app-server makes
                # the request bounded without touching desktop Codex.
                await _close_sdk_client(codex, force=True)
                client_closed = True
                await _settle_cancelled_task(phase_task)
                raise CodexGatewayError(
                    "codex_timeout", "Codex 决策超时，任务已安全终止。"
                )
            result = phase_task.result()
            phase_task = None

            # A successful response is not accepted until the SDK transport is
            # closed.  Closing therefore consumes the same lifecycle budget as
            # client initialization, thread_start, and thread.run.
            close_completed = await _close_sdk_client(
                codex,
                force=False,
                timeout=max(0.0, deadline - loop.time()),
            )
            client_closed = True
            if not close_completed or loop.time() > deadline:
                raise CodexGatewayError(
                    "codex_timeout", "Codex 决策超时，任务已安全终止。"
                )
        except asyncio.CancelledError:
            if codex is not None and not client_closed:
                await _close_sdk_client(codex, force=True)
                client_closed = True
            if phase_task is not None:
                await _settle_cancelled_task(phase_task)
            raise
        except CodexGatewayError:
            raise
        except Exception as exc:
            raise CodexGatewayError(
                "codex_sdk_failed",
                "Codex Python SDK 运行失败；未生成或保存不完整策略。",
            ) from exc
        finally:
            if codex is not None and not client_closed:
                await _close_sdk_client(codex, force=False)

        final_response = getattr(result, "final_response", None)
        return self._parse_final_response(final_response)

    async def _run_cli(
        self, prompt: str, *, workspace_root: Path | None = None
    ) -> AgentStrategyPayload:
        wire_schema = _structured_output_wire_schema()

        import tempfile

        with tempfile.TemporaryDirectory(prefix="cfda-codex-") as temporary_directory:
            output_path = Path(temporary_directory) / "final.json"
            schema_path = Path(temporary_directory) / "wire-schema.json"
            schema_path.write_text(
                json.dumps(wire_schema, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            command = [
                self.settings.codex_bin,
                "--ask-for-approval",
                "never",
            ]
            for override in _STRATEGY_CODEX_CONFIG_OVERRIDES:
                command.extend(["--config", override])
            command.extend([
                "exec",
                "--json",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "--cd",
                str((workspace_root or self.settings.workspace_root).resolve()),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ])
            if self.settings.codex_model:
                command.extend(["--model", self.settings.codex_model])
            command.append("-")
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                _, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode("utf-8")),
                    timeout=self.settings.codex_timeout_seconds,
                )
            except asyncio.CancelledError:
                if process is not None:
                    await _terminate_process(process)
                raise
            except asyncio.TimeoutError as exc:
                if process is not None:
                    await _terminate_process(process)
                raise CodexGatewayError("codex_timeout", "Codex 决策超时，任务已安全终止。") from exc
            except OSError as exc:
                raise CodexGatewayError("codex_unavailable", "无法启动本机 Codex 运行时。") from exc

            assert process is not None
            if process.returncode != 0:
                lowered = stderr.decode("utf-8", errors="replace")[:8192].lower()
                if "login" in lowered or "auth" in lowered or "unauthorized" in lowered:
                    code = "codex_auth_required"
                    message = "Codex 登录已失效，请在本机重新登录后重试。"
                else:
                    code = "codex_run_failed"
                    message = "Codex 决策任务失败；未保存不完整策略。"
                raise CodexGatewayError(code, message)
            try:
                final_response = output_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise CodexGatewayError(
                    "codex_output_missing",
                    "Codex 未返回结构化最终策略。",
                ) from exc
        return self._parse_final_response(final_response)

    @staticmethod
    def _parse_final_response(value: Any) -> AgentStrategyPayload:
        if not isinstance(value, str) or not value.strip():
            raise CodexGatewayError("codex_output_empty", "Codex 未返回最终策略。")
        try:
            parsed = json.loads(value)
            return AgentStrategyPayload.model_validate(parsed)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CodexGatewayError(
                "codex_output_invalid",
                "Codex 最终输出未通过策略契约校验，未更新看板。",
            ) from exc

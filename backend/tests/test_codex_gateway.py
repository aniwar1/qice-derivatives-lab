from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.codex_gateway as gateway_module
from app.codex_gateway import CodexGatewayError, LocalCodexGateway
from app.config import Settings
from app.models import CodexRuntimeMetadata
from conftest import strategy_payload


class FakeCodexConfig:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


@pytest.mark.asyncio
async def test_sdk_timeout_is_bounded_and_kills_only_its_owned_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9
            release.set()

    class FakeThread:
        def __init__(self, owner: "FakeAsyncCodex") -> None:
            self.owner = owner

        async def run(
            self,
            prompt: str,
            *,
            output_schema=None,
            sandbox=None,
            approval_mode=None,
        ):
            self.owner.run_kwargs = {
                "prompt": prompt,
                "output_schema": output_schema,
                "sandbox": sandbox,
                "approval_mode": approval_mode,
            }
            # This is the cancellation-resistant shape that makes wait_for()
            # exceed its timeout: only app-server shutdown releases the turn.
            try:
                await release.wait()
            except asyncio.CancelledError:
                self.owner.ignored_cancellation = True
                await release.wait()
            return SimpleNamespace(final_response=None)

    class FakeAsyncCodex:
        instance: "FakeAsyncCodex | None" = None

        def __init__(self, **_kwargs) -> None:
            type(self).instance = self
            self.client_kwargs = _kwargs
            self.process = FakeProcess()
            self._client = SimpleNamespace(_sync=SimpleNamespace(_proc=self.process))
            self.start_kwargs: dict = {}
            self.run_kwargs: dict = {}
            self.close_calls = 0
            self.ignored_cancellation = False

        async def thread_start(
            self,
            *,
            cwd=None,
            sandbox=None,
            approval_mode=None,
            ephemeral=None,
            model=None,
        ) -> FakeThread:
            self.start_kwargs = {
                "cwd": cwd,
                "sandbox": sandbox,
                "approval_mode": approval_mode,
                "ephemeral": ephemeral,
                "model": model,
            }
            return FakeThread(self)

        async def close(self) -> None:
            self.close_calls += 1
            await release.wait()

    class FakeSandbox:
        workspace_write = "workspace_write"

    class FakeApprovalMode:
        deny_all = "deny_all"

    settings = Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        auto_refresh_if_empty=False,
        codex_timeout_seconds=0.01,  # type: ignore[arg-type]
    )
    gateway = LocalCodexGateway(settings)
    gateway._sdk_module = SimpleNamespace(
        AsyncCodex=FakeAsyncCodex,
        CodexConfig=FakeCodexConfig,
        Sandbox=FakeSandbox,
        ApprovalMode=FakeApprovalMode,
    )
    monkeypatch.setattr(gateway_module, "_SDK_CLOSE_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(gateway_module, "_SDK_PROCESS_TERM_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(gateway_module, "_SDK_PROCESS_KILL_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(gateway_module, "_SDK_RUN_SETTLE_SECONDS", 0.01)

    def forbid_process_group_signal(*_args) -> None:
        raise AssertionError("SDK timeout must not signal a process group")

    monkeypatch.setattr(gateway_module.os, "killpg", forbid_process_group_signal)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    with pytest.raises(CodexGatewayError) as raised:
        await gateway._run_sdk("decision")
    elapsed = loop.time() - started_at

    assert raised.value.code == "codex_timeout"
    assert elapsed < 0.25
    client = FakeAsyncCodex.instance
    assert client is not None
    assert client.process.terminate_calls == 1
    assert client.process.kill_calls == 1
    assert client.close_calls == 1
    assert client.client_kwargs["config"].config_overrides == (
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
        "sandbox_workspace_write.network_access=true",
        'web_search="live"',
    )
    assert client.start_kwargs == {
        "cwd": str(tmp_path),
        "sandbox": FakeSandbox.workspace_write,
        "approval_mode": FakeApprovalMode.deny_all,
        "ephemeral": True,
        "model": None,
    }
    assert client.run_kwargs["prompt"] == "decision"
    # The workspace policy is set on thread/start.  A repeated turn-level
    # workspace-write preset would reset networkAccess to false and block the
    # live quote collectors, so the turn must inherit the thread policy.
    assert client.run_kwargs["sandbox"] is None
    assert client.run_kwargs["approval_mode"] == FakeApprovalMode.deny_all
    assert client.run_kwargs["output_schema"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_sdk_thread_start_timeout_kills_owned_server_then_uses_cli_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9
            release.set()

    class FakeAsyncCodex:
        instance: "FakeAsyncCodex | None" = None

        def __init__(self, **_kwargs) -> None:
            type(self).instance = self
            self.process = FakeProcess()
            self._client = SimpleNamespace(_sync=SimpleNamespace(_proc=self.process))
            self.close_calls = 0
            self.run_calls = 0

        async def thread_start(
            self,
            *,
            cwd=None,
            sandbox=None,
            approval_mode=None,
            ephemeral=None,
            model=None,
        ):
            await release.wait()
            return SimpleNamespace(run=self._run)

        async def _run(self, prompt: str, **_kwargs):
            self.run_calls += 1
            return SimpleNamespace(final_response=None)

        async def close(self) -> None:
            self.close_calls += 1
            await release.wait()

    class FakeSandbox:
        workspace_write = "workspace_write"

    class FakeApprovalMode:
        deny_all = "deny_all"

    settings = Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        auto_refresh_if_empty=False,
        codex_timeout_seconds=1,
        codex_start_timeout_seconds=0.01,  # type: ignore[arg-type]
    )
    gateway = LocalCodexGateway(settings)
    gateway._sdk_module = SimpleNamespace(
        AsyncCodex=FakeAsyncCodex,
        CodexConfig=FakeCodexConfig,
        Sandbox=FakeSandbox,
        ApprovalMode=FakeApprovalMode,
    )
    monkeypatch.setattr(gateway_module, "_SDK_CLOSE_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(gateway_module, "_SDK_PROCESS_TERM_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(gateway_module, "_SDK_PROCESS_KILL_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(gateway_module, "_SDK_RUN_SETTLE_SECONDS", 0.01)

    runtime = CodexRuntimeMetadata(
        transport="python_sdk_stdio",
        auth_mode="chatgpt_managed",
        authenticated=True,
        model="configured_default",
        model_resolution="inherited",
    )
    fallback_calls = 0

    async def status(*, refresh: bool = False) -> CodexRuntimeMetadata:
        return runtime

    async def cli_fallback(prompt: str):
        nonlocal fallback_calls
        fallback_calls += 1
        assert prompt == "decision"
        return strategy_payload()

    monkeypatch.setattr(gateway, "status", status)
    monkeypatch.setattr(gateway, "_run_cli", cli_fallback)

    started_at = asyncio.get_running_loop().time()
    result = await gateway.run_strategy("decision")
    elapsed = asyncio.get_running_loop().time() - started_at

    assert result.runtime.transport == "cli_exec_json"
    assert result.payload.schema_version == "codex_strategy_decision.v1"
    assert fallback_calls == 1
    assert elapsed < 0.25
    client = FakeAsyncCodex.instance
    assert client is not None
    assert client.run_calls == 0
    assert client.close_calls == 1
    assert client.process.terminate_calls == 1
    assert client.process.kill_calls == 1


@pytest.mark.asyncio
async def test_sdk_deadline_starts_before_lazy_client_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_loop = asyncio.get_running_loop()

    class FakeClock:
        def __init__(self) -> None:
            self.now = real_loop.time()

        def time(self) -> float:
            return self.now

    clock = FakeClock()

    class FakeProcess:
        returncode = 0

        def poll(self) -> int:
            return self.returncode

    class FakeAsyncCodex:
        instance: "FakeAsyncCodex | None" = None

        def __init__(self, **_kwargs) -> None:
            type(self).instance = self
            self.process = FakeProcess()
            self._client = SimpleNamespace(_sync=SimpleNamespace(_proc=self.process))
            self.close_calls = 0
            self.thread_start_calls = 0
            clock.now += 31

        async def thread_start(
            self,
            *,
            cwd=None,
            sandbox=None,
            approval_mode=None,
            ephemeral=None,
            model=None,
        ):
            self.thread_start_calls += 1
            raise AssertionError("expired construction must not start a thread")

        async def close(self) -> None:
            self.close_calls += 1

    class FakeSandbox:
        workspace_write = "workspace_write"

    class FakeApprovalMode:
        deny_all = "deny_all"

    settings = Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        auto_refresh_if_empty=False,
        codex_timeout_seconds=30,
    )
    gateway = LocalCodexGateway(settings)
    gateway._sdk_module = SimpleNamespace(
        AsyncCodex=FakeAsyncCodex,
        CodexConfig=FakeCodexConfig,
        Sandbox=FakeSandbox,
        ApprovalMode=FakeApprovalMode,
    )
    monkeypatch.setattr(gateway_module.asyncio, "get_running_loop", lambda: clock)

    with pytest.raises(CodexGatewayError) as raised:
        await gateway._run_sdk("decision")

    assert raised.value.code == "codex_timeout"
    client = FakeAsyncCodex.instance
    assert client is not None
    assert client.thread_start_calls == 0
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_sdk_start_cap_and_run_use_phase_and_total_deadlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadlines: list[float] = []
    original_wait = gateway_module._task_finished_by

    async def record_deadline(task: asyncio.Task, *, deadline: float) -> bool:
        deadlines.append(deadline)
        return await original_wait(task, deadline=deadline)

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    class FakeThread:
        async def run(
            self,
            prompt: str,
            *,
            output_schema=None,
            sandbox=None,
            approval_mode=None,
        ):
            return SimpleNamespace(
                final_response=json.dumps(strategy_payload().model_dump(mode="json"))
            )

    class FakeAsyncCodex:
        def __init__(self, **_kwargs) -> None:
            self.process = FakeProcess()
            self._client = SimpleNamespace(_sync=SimpleNamespace(_proc=self.process))

        async def thread_start(
            self,
            *,
            cwd=None,
            sandbox=None,
            approval_mode=None,
            ephemeral=None,
            model=None,
        ) -> FakeThread:
            return FakeThread()

        async def close(self) -> None:
            self.process.returncode = 0

    class FakeSandbox:
        workspace_write = "workspace_write"

    class FakeApprovalMode:
        deny_all = "deny_all"

    settings = Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        auto_refresh_if_empty=False,
        codex_timeout_seconds=90,
        codex_start_timeout_seconds=30,
    )
    gateway = LocalCodexGateway(settings)
    gateway._sdk_module = SimpleNamespace(
        AsyncCodex=FakeAsyncCodex,
        CodexConfig=FakeCodexConfig,
        Sandbox=FakeSandbox,
        ApprovalMode=FakeApprovalMode,
    )
    monkeypatch.setattr(gateway_module, "_task_finished_by", record_deadline)

    result = await gateway._run_sdk("decision")

    assert result.schema_version == "codex_strategy_decision.v1"
    assert len(deadlines) == 2
    assert 59 < deadlines[1] - deadlines[0] < 61


@pytest.mark.asyncio
async def test_sdk_close_must_finish_inside_its_bounded_lifecycle_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminate_calls = 0

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.returncode = -15
            release.set()

        def kill(self) -> None:
            self.returncode = -9
            release.set()

    class FakeThread:
        async def run(
            self,
            prompt: str,
            *,
            output_schema=None,
            sandbox=None,
            approval_mode=None,
        ):
            return SimpleNamespace(
                final_response=json.dumps(strategy_payload().model_dump(mode="json"))
            )

    class FakeAsyncCodex:
        instance: "FakeAsyncCodex | None" = None

        def __init__(self, **_kwargs) -> None:
            type(self).instance = self
            self.process = FakeProcess()
            self._client = SimpleNamespace(_sync=SimpleNamespace(_proc=self.process))
            self.close_calls = 0

        async def thread_start(
            self,
            *,
            cwd=None,
            sandbox=None,
            approval_mode=None,
            ephemeral=None,
            model=None,
        ) -> FakeThread:
            return FakeThread()

        async def close(self) -> None:
            self.close_calls += 1
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    class FakeSandbox:
        workspace_write = "workspace_write"

    class FakeApprovalMode:
        deny_all = "deny_all"

    settings = Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        auto_refresh_if_empty=False,
        codex_timeout_seconds=0.03,  # type: ignore[arg-type]
    )
    gateway = LocalCodexGateway(settings)
    gateway._sdk_module = SimpleNamespace(
        AsyncCodex=FakeAsyncCodex,
        CodexConfig=FakeCodexConfig,
        Sandbox=FakeSandbox,
        ApprovalMode=FakeApprovalMode,
    )
    monkeypatch.setattr(gateway_module, "_SDK_PROCESS_TERM_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(gateway_module, "_SDK_PROCESS_KILL_GRACE_SECONDS", 0.01)

    started_at = asyncio.get_running_loop().time()
    with pytest.raises(CodexGatewayError) as raised:
        await gateway._run_sdk("decision")
    elapsed = asyncio.get_running_loop().time() - started_at

    assert raised.value.code == "codex_timeout"
    assert elapsed < 0.25
    client = FakeAsyncCodex.instance
    assert client is not None
    assert client.close_calls == 1
    assert client.process.terminate_calls == 1


def test_cli_output_schema_requires_one_resolved_bounded_review() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "codex_strategy_decision.agent.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "quality_review" in schema["required"]
    assert schema["properties"]["quality_review"] == {"$ref": "#/$defs/qualityReview"}
    review = schema["$defs"]["qualityReview"]
    assert review["additionalProperties"] is False
    assert set(review["required"]) == {
        "critic_revisions",
        "strongest_challenge",
        "resolution",
        "unresolved_material_issue",
    }
    assert review["properties"]["critic_revisions"] == {"const": 1}
    assert review["properties"]["unresolved_material_issue"] == {"type": "null"}
    assert review["properties"]["strongest_challenge"]["minLength"] == 1
    assert review["properties"]["strongest_challenge"]["maxLength"] == 1000
    assert review["properties"]["resolution"]["minLength"] == 1
    assert review["properties"]["resolution"]["maxLength"] == 1000


def test_structured_output_wire_schema_uses_supported_combinators_only() -> None:
    schema = gateway_module._structured_output_wire_schema()

    def walk(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    objects = tuple(walk(schema))
    assert schema["additionalProperties"] is False
    assert all("allOf" not in item for item in objects)
    assert all("if" not in item and "then" not in item and "else" not in item for item in objects)
    assert all("oneOf" not in item for item in objects)
    assert all("format" not in item and "pattern" not in item for item in objects)
    assert all("const" not in item for item in objects)
    assert schema["properties"]["schema_version"]["enum"] == [
        "codex_strategy_decision.v1"
    ]
    assert schema["properties"]["product"]["anyOf"][0] == {"type": "null"}


@pytest.mark.asyncio
async def test_cli_gateway_uses_ephemeral_stdin_and_returns_only_validated_final(tmp_path: Path) -> None:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(strategy_payload().model_dump(mode="json")), encoding="utf-8"
    )
    stub = tmp_path / "codex-stub"
    stub.write_text(
        """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

args = sys.argv[1:]
root = Path(__file__).resolve().parent
if args == ["login", "status"]:
    print("Logged in using ChatGPT as private@example.test")
    raise SystemExit(0)
if args == ["--version"]:
    print("codex-cli 9.8.7-test")
    raise SystemExit(0)
if "exec" in args:
    (root / "argv.json").write_text(json.dumps(args), encoding="utf-8")
    (root / "stdin.txt").write_text(sys.stdin.read(), encoding="utf-8")
    output = Path(args[args.index("--output-last-message") + 1])
    output.write_text((root / "payload.json").read_text(encoding="utf-8"), encoding="utf-8")
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    stub.chmod(0o700)
    settings = Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        codex_bin=str(stub),
        prefer_codex_sdk=False,
        require_chatgpt_auth=True,
        auto_refresh_if_empty=False,
        codex_timeout_seconds=30,
    )
    gateway = LocalCodexGateway(settings)

    result = await gateway.run_strategy("PRIVATE PROMPT BODY")

    assert result.payload.session_daily_bar is not None
    assert result.payload.session_daily_bar.bar_state == "session_partial"
    assert result.runtime.auth_mode == "chatgpt_managed"
    assert result.runtime.credentials_exposed is False
    assert result.runtime.version == "9.8.7-test"
    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    cli_overrides = tuple(
        argv[index + 1]
        for index, argument in enumerate(argv)
        if argument == "--config"
    )
    assert cli_overrides == gateway_module._STRATEGY_CODEX_CONFIG_OVERRIDES
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--sandbox" in argv and "workspace-write" in argv
    assert "--ask-for-approval" in argv and "never" in argv
    assert "PRIVATE PROMPT BODY" not in argv
    assert (tmp_path / "stdin.txt").read_text(encoding="utf-8") == "PRIVATE PROMPT BODY"


@pytest.mark.asyncio
async def test_broken_sdk_transport_falls_back_to_disposable_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        codex_bin="codex",
        auto_refresh_if_empty=False,
    )
    gateway = LocalCodexGateway(settings)
    gateway._sdk_module = object()
    runtime = CodexRuntimeMetadata(
        transport="python_sdk_stdio",
        auth_mode="chatgpt_managed",
        authenticated=True,
        model="configured_default",
        model_resolution="inherited",
    )

    async def status(*, refresh: bool = False) -> CodexRuntimeMetadata:
        return runtime

    async def broken_sdk(prompt: str):
        raise CodexGatewayError("codex_sdk_failed", "sdk unavailable")

    async def cli_fallback(prompt: str):
        assert prompt == "decision"
        return strategy_payload()

    monkeypatch.setattr(gateway, "status", status)
    monkeypatch.setattr(gateway, "_run_sdk", broken_sdk)
    monkeypatch.setattr(gateway, "_run_cli", cli_fallback)

    result = await gateway.run_strategy("decision")

    assert result.runtime.transport == "cli_exec_json"
    assert result.payload.schema_version == "codex_strategy_decision.v1"


@pytest.mark.asyncio
async def test_sdk_timeout_is_propagated_without_starting_a_second_codex_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        workspace_root=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        auto_refresh_if_empty=False,
    )
    gateway = LocalCodexGateway(settings)
    gateway._sdk_module = object()
    runtime = CodexRuntimeMetadata(
        transport="python_sdk_stdio",
        auth_mode="chatgpt_managed",
        authenticated=True,
        model="configured_default",
        model_resolution="inherited",
    )

    async def status(*, refresh: bool = False) -> CodexRuntimeMetadata:
        return runtime

    async def timed_out_sdk(prompt: str):
        raise CodexGatewayError("codex_timeout", "timed out")

    async def forbidden_cli_fallback(prompt: str):
        raise AssertionError("a timed-out strategy must not start a duplicate CLI run")

    monkeypatch.setattr(gateway, "status", status)
    monkeypatch.setattr(gateway, "_run_sdk", timed_out_sdk)
    monkeypatch.setattr(gateway, "_run_cli", forbidden_cli_fallback)

    with pytest.raises(CodexGatewayError) as raised:
        await gateway.run_strategy("decision")

    assert raised.value.code == "codex_timeout"


def test_agent_schema_is_strict_json() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "codex_strategy_decision.agent.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["additionalProperties"] is False
    assert "scan_coverage" in schema["required"]
    assert schema["properties"]["scan_coverage"] == {"$ref": "#/$defs/scanCoverage"}
    assert schema["properties"]["order_placement_authorized"] == {"const": False}
    assert set(schema["properties"]["decision"]["enum"]) == {
        "futures",
        "long_call",
        "long_put",
        "wait",
    }
    session_bar = schema["$defs"]["sessionDailyBar"]
    assert "calendar_evidence" in session_bar["required"]
    assert session_bar["properties"]["calendar_evidence"] == {
        "oneOf": [
            {"type": "null"},
            {"$ref": "#/$defs/calendarEvidence"},
        ]
    }
    calendar = schema["$defs"]["calendarEvidence"]
    assert calendar["additionalProperties"] is False
    assert set(calendar["required"]) == {
        "exchange",
        "trade_date",
        "regular_session_confirmed",
        "url",
        "retrieved_at",
        "locator",
        "content_sha256",
    }
    assert calendar["properties"]["regular_session_confirmed"] == {"const": True}
    assert session_bar["allOf"][0]["then"]["properties"]["calendar_evidence"] == {
        "$ref": "#/$defs/calendarEvidence"
    }
    assert session_bar["allOf"][1]["then"]["properties"]["calendar_evidence"] == {
        "type": "null"
    }

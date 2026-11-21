"""独立、无工具的模型会话，保留实际请求摘要与调用回执。"""

import json
import os
import signal
import subprocess
import tempfile
import time
from hashlib import sha256
from pathlib import Path


def infer(model, cli, prompt, output_schema):
    flags = [
        'model_provider="chatgpt-http"',
        'model_providers.chatgpt-http.name="ChatGPT HTTP"',
        'model_providers.chatgpt-http.base_url="https://chatgpt.com/backend-api/codex"',
        'model_providers.chatgpt-http.wire_api="responses"',
        "model_providers.chatgpt-http.requires_openai_auth=true",
        "model_providers.chatgpt-http.supports_websockets=false",
        'web_search="disabled"',
        'model_reasoning_effort="medium"',
        "project_doc_max_bytes=0",
        "mcp_servers={}",
    ]
    flags.extend(
        f"features.{feature}=false"
        for feature in (
            "shell_tool",
            "unified_exec",
            "multi_agent",
            "apps",
            "plugins",
            "memories",
            "remote_plugin",
            "shell_snapshot",
            "unbounded_connection_retries",
        )
    )
    with tempfile.TemporaryDirectory(prefix="qice-frequency-agent-") as folder:
        schema_file = Path(folder) / "schema.json"
        result_file = Path(folder) / "result.json"
        schema_file.write_text(json.dumps(output_schema))
        command = [cli, "--ask-for-approval", "never"]
        for flag in flags:
            command.extend(["-c", flag])
        command.extend(
            [
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--cd",
                folder,
                "--model",
                model,
                "--output-schema",
                str(schema_file),
                "-o",
                str(result_file),
                "--json",
                "-",
            ]
        )
        started = time.monotonic()
        p = subprocess.Popen(
            command,
            cwd=folder,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            out, _ = p.communicate(prompt.encode(), timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            raise TimeoutError("条件委托模型会话超时")
        events = [
            json.loads(line)
            for line in out.decode().splitlines()
            if line.startswith("{")
        ]
        if p.returncode or not result_file.exists():
            failure = next(
                (
                    x.get("message", x.get("error", {}).get("message", ""))
                    for x in reversed(events)
                    if x.get("type") in ("turn.failed", "error")
                ),
                "",
            )
            raise RuntimeError(f"模型运行失败 {p.returncode} {str(failure)[:200]}")
        if any(
            x.get("type", "").startswith("item.")
            and x.get("item", {}).get("type")
            not in ("reasoning", "agent_message", "plan")
            for x in events
        ):
            raise ValueError("模型调用了冻结资料以外的工具")
        run_id = next(
            (x.get("thread_id") for x in events if x.get("type") == "thread.started"),
            None,
        )
        usage = next(
            (x.get("usage") for x in events if x.get("type") == "turn.completed"), None
        )
        if not run_id or not usage or usage.get("input_tokens", 0) <= 0:
            raise ValueError("缺少实际模型运行回执")
        return json.loads(result_file.read_bytes()), {
            "run_id": run_id,
            "model": model,
            "usage": usage,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "prompt_sha256": sha256(prompt.encode()).hexdigest(),
        }

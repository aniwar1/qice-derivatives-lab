"""独立无工具 Codex 会话，只接受带运行回执的模型判断。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from hashlib import sha256
from pathlib import Path

from .agent_replay import POLICY, validate_decision, wire_schema


def decide(packet, model, cli, timeout=300, feedback=None, *, policy=POLICY, retained=None):
    with tempfile.TemporaryDirectory(prefix="qice-frozen-agent-") as tmp:
        folder = Path(tmp)
        schema = folder / "output-schema.json"
        contract = wire_schema(packet["products"])
        if retained is not None:
            contract["properties"] = {g: spec for g, spec in contract["properties"].items() if g not in retained}
            contract["required"] = list(contract["properties"])
        schema.write_text(json.dumps(contract), encoding="utf-8")
        final = folder / "result.json"
        config = [
            'model_provider="chatgpt-http"',
            'model_providers.chatgpt-http.name="ChatGPT HTTP"',
            'model_providers.chatgpt-http.base_url="https://chatgpt.com/backend-api/codex"',
            'model_providers.chatgpt-http.wire_api="responses"',
            "model_providers.chatgpt-http.requires_openai_auth=true",
            "model_providers.chatgpt-http.supports_websockets=false",
            'web_search="disabled"',
            'model_reasoning_effort="medium"',
            "project_doc_max_bytes=0",
            "features.shell_tool=false",
            "features.unified_exec=false",
            "features.multi_agent=false",
            "features.apps=false",
            "features.plugins=false",
            "features.memories=false",
            "features.remote_plugin=false",
            "features.shell_snapshot=false",
            "features.unbounded_connection_retries=false",
            "mcp_servers={}",
        ]
        command = [cli, "--ask-for-approval", "never"]
        for option in config:
            command.extend(["-c", option])
        command += [
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--cd",
            tmp,
            "--output-schema",
            str(schema),
            "-o",
            str(final),
            "--model",
            model,
            "--json",
            "-",
        ]
        instruction = (
            policy + "\n等待时必须设置 legs=[]、risk_bps=0、holding_days=0。"
            "C 的事件必须具备引用资料内的 scheduled_at 并且晚于预定交易日09:05北京时间。"
            "未提供有效 scheduled_at 则只能等待。引用证据请使用 market_sources 的键或 documents 的 id。\n"
        )
        if retained is not None:
            instruction += "本次只评估输出契约要求的组。其他组的模型判断已独立封存。\n"
        if feedback:
            instruction += (
                f"上一次结果未通过规则校验：{feedback}。按同一份冻结资料重新判断。\n"
            )
        model_context = packet | {
            "documents": [
                {
                    k: doc[k]
                    for k in (
                        "id",
                        "products",
                        "dimension",
                        "scope",
                        "coverage_scope",
                        "freshness_role",
                        "period",
                        "title",
                        "excerpt",
                        "publisher",
                        "authority",
                        "available_at",
                        "scheduled_at",
                    )
                    if k in doc
                }
                for doc in packet["documents"]
            ],
            "market_sources": list(packet["market_sources"]),
        }
        prompt = instruction + json.dumps(
            model_context, ensure_ascii=False, separators=(",", ":")
        )
        started = time.time()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            cwd=tmp,
        )
        try:
            out, _ = process.communicate(prompt.encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise TimeoutError("冻结模型调用超时")
        events = [
            json.loads(line)
            for line in out.decode().splitlines()
            if line.startswith("{")
        ]
        failure = next(
            (
                str(x.get("message") or x.get("error", {}).get("message", ""))
                for x in reversed(events)
                if x.get("type") in ("error", "turn.failed")
                and (x.get("message") or x.get("error"))
            ),
            "",
        )
        if process.returncode or not final.exists():
            raise RuntimeError(
                f"Codex 返回 {process.returncode}: {failure[:320] or '未返回模型结果'}"
            )
        if any(
            e.get("type", "").startswith("item.")
            and e.get("item", {}).get("type")
            not in ("reasoning", "agent_message", "plan")
            for e in events
        ):
            raise RuntimeError("冻结回放检测到工具调用，拒绝收录")
        complete = next((e for e in events if e.get("type") == "turn.completed"), None)
        if not complete:
            raise RuntimeError("模型回执缺少 turn.completed")
        result = json.loads(final.read_text(encoding="utf-8"))
        if retained is not None:
            if set(result) != set(contract["required"]):
                raise ValueError("模型返回了本轮范围外的策略")
            result = retained | result
        validate_decision(result, packet)
        receipt = {
            "model": model,
            "provider": "ChatGPT Codex",
            "usage": complete.get("usage", {}),
            "prompt_sha256": sha256(prompt.encode()).hexdigest(),
            "run_id": next(
                (
                    e.get("thread_id")
                    for e in events
                    if e.get("type") == "thread.started"
                ),
                None,
            ),
            "elapsed_seconds": round(time.time() - started, 2),
        }
        return result, receipt

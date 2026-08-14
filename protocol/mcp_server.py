#!/usr/bin/env python3
"""Protocol MCP Server — 协作协议超越版桥（替换 openclaw-bridge）。

同工具名（call_openclaw / delegate_to_servant）+ 兼容结果格式 → Hermes 侧调用点零改动，
只需把 config.yaml 的 mcp_servers.openclaw-bridge.url 端口换成 8766。

内部从"简单队列 + 外部 60s cron worker"升级为完整 protocol 内核：
- 结构化参数：context_fields / tools / result_schema / priority / request_summary
- 三态反馈 + meta（summary / validation / tool_rounds）
- 白名单工具执行 + Schema 校验反馈重试 + 分块摘要 + 崩溃恢复 + 健康检查
- worker 常驻线程 2s 轮询（秒级响应，超越现役 cron 轮询延迟）

运行: python -m protocol.mcp_server --port 8766
Hermes 配置 (config.yaml):
  mcp_servers:
    openclaw-bridge:
      url: http://127.0.0.1:8766/mcp
      timeout: 360
      connect_timeout: 30

回滚：改回 8765 并重启 Hermes（旧 bridge 保留）。
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from protocol.queue import FileQueue
from protocol.task import Task
from protocol.worker import process_one

RUNTIME = os.path.join(os.environ.get("LOCALAPPDATA",
                                       os.path.expanduser("~")),
                       "hermes", "protocol-bridge", "runtime")
Q = FileQueue(RUNTIME)

mcp = FastMCP(
    "Protocol Bridge",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


def _worker_loop(interval: float = 2.0):
    """常驻 worker：恢复卡死任务 → 处理 pending（2s 轮询，秒级响应）。"""
    while True:
        try:
            Q.recover_stale()
            for t in Q.scan():
                try:
                    process_one(t, Q)
                except Exception as e:
                    print(f"[worker] 任务处理异常 {t.task_id}: {e}")
        except Exception:
            pass
        time.sleep(interval)


def _format_result(task: Task) -> str:
    """兼容现役 bridge 结果格式（[TIMEOUT]/[FAILED]/[NEEDS CONFIRMATION]/原文）+ meta。"""
    if task.status == "timeout":
        return f"[TIMEOUT] {task.result}"
    if task.status == "failed":
        return f"[FAILED] {task.result}"
    if task.status == "need_confirm":
        return f"[NEEDS CONFIRMATION] {task.result}"
    meta = task.result_meta or {}
    lines = [task.result or ""]
    if meta.get("summary"):
        lines.append(f"[SUMMARY] {meta['summary']}")
    if meta.get("validation") == "ok":
        lines.append("[VALIDATED] schema ok")
    if meta.get("tool_rounds"):
        lines.append(f"[TOOLS] {meta['tool_rounds']} round(s)")
    return "\n".join(lines)


def _submit_and_wait(task_text: str, timeout: int, context_fields=None,
                     tools=None, result_schema=None, priority: str = "normal",
                     request_summary: bool = False) -> str:
    t = Task(goal=task_text, context_fields=context_fields, tools=tools,
             result_schema=result_schema, priority=priority,
             request_summary=request_summary, timeout=timeout,
             target_agent="task-servant")
    Q.submit(t)
    done = Q.wait_result(t.task_id, timeout=timeout + 30)
    return _format_result(done)


@mcp.tool()
async def call_openclaw(task: str, timeout: int = 300,
                        context_fields: dict = None, tools: list = None,
                        result_schema: dict = None, priority: str = "normal",
                        request_summary: bool = False) -> str:
    """Delegate a task to the collaboration execution layer (protocol kernel).

    Backward-compatible superset of the legacy bridge tool: same name, same
    first two args — Hermes call sites keep working unchanged.

    Args:
        task: Task description (4-element spec: goal + inline context + constraints + acceptance).
        timeout: Max seconds to wait (default 300).
        context_fields: Structured data block (field -> value); LLM parses fields directly.
        tools: Whitelisted tools the task may use (e.g. ["calc", "read_file"]); must be
               a subset of the worker's ALLOWED_TOOLS env whitelist.
        result_schema: JSON Schema; worker validates the result (with one feedback retry).
        priority: "high" | "normal" | "low".
        request_summary: Ask worker to also emit a one-line [摘要] (for chunked chains).

    Returns:
        Done: raw answer + optional [SUMMARY]/[VALIDATED]/[TOOLS] lines.
        Failed: [FAILED] <reason>. Timeout: [TIMEOUT] <msg>.
    """
    return _submit_and_wait(task, timeout, context_fields, tools,
                            result_schema, priority, request_summary)


@mcp.tool()
async def delegate_to_servant(task: str, timeout: int = 300) -> str:
    """Delegate a simple task to the execution unit (lightweight semantics).

    Args:
        task: Task description.
        timeout: Max seconds to wait (default 300).

    Returns:
        Done: raw answer. Failed: [FAILED] <reason>. Timeout: [TIMEOUT] <msg>.
    """
    return _submit_and_wait(task, timeout)


def _ensure_api_key():
    """确保 LLM key 可用：env → WSL 智谱 key 文件 → ~/.hermes/.env。

    后台进程（Hermes terminal background）用干净环境启动，不继承会话 export，
    因此 server 必须自包含加载 key。
    """
    if os.environ.get("LLM_API_KEY") or os.environ.get("ZHIPUAI_API_KEY"):
        return
    candidates = [
        r"//wsl.localhost/PCMClawUbuntu/root/.zhipu_key",   # WSL 智谱 key 权威位置
        os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                     ".hermes", ".env"),
    ]
    for p in candidates:
        try:
            if not os.path.exists(p):
                continue
            content = open(p, encoding="utf-8", errors="ignore").read()
            key = None
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("ZHIPUAI_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
            if key is None and "=" not in content.strip():   # 纯 key 文件（WSL）
                key = content.strip()
            if key and len(key) > 20:
                os.environ["ZHIPUAI_API_KEY"] = key
                print(f"[protocol-bridge] 已从 {p} 加载 API key")
                return
        except Exception:
            continue
    print("[protocol-bridge] 警告: 未找到 LLM API key"
          "（设 LLM_API_KEY 或 ZHIPUAI_API_KEY 或 WSL /root/.zhipu_key）")


def main():
    ap = argparse.ArgumentParser(description="Protocol MCP Server (超越版协作桥)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-worker", action="store_true",
                    help="不起常驻 worker（调试用）")
    args = ap.parse_args()

    _ensure_api_key()
    if not args.no_worker:
        threading.Thread(target=_worker_loop, daemon=True).start()
        print(f"[protocol-bridge] 常驻 worker 已启动 (2s 轮询, queue={RUNTIME})")

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    print(f"[protocol-bridge] MCP 端点: http://{args.host}:{args.port}/mcp")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

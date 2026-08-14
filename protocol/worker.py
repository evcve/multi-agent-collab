"""Worker：从队列取任务 → 调 LLM API（OpenAI 兼容）→ 写三态结果。

零框架、纯 stdlib。任何 OpenAI 兼容端点可用（OpenAI / DeepSeek / Zhipu / 本地 vLLM）。

环境变量：
    LLM_BASE_URL  默认 https://api.openai.com/v1
    LLM_API_KEY   必填
    LLM_MODEL     默认 gpt-4o-mini
    LLM_MAX_TOKENS 默认 2000（大输出任务调大！）

用法：
    python -m protocol.worker [--queue-dir ./runtime] [--once]
"""
import argparse
import json
import os
import sys
import time
import urllib.request

from .queue import FileQueue
from .task import Task


def call_llm(prompt: str, timeout: int = 120) -> str:
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    if not key:
        raise RuntimeError("LLM_API_KEY 未设置")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "2000"))
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是多智能体协作中的任务执行单元（worker）。"
                                          "严格按任务要求执行，输出遵循指定反馈格式。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(f"{base}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def parse_three_state(raw: str) -> tuple[str, str]:
    """解析 [状态] done/failed/need_confirm + [结果] 内容。

    防误报：未找到合法的 [状态] 标记 → 返回 failed（格式偏差绝不能静默降级为 done，
    否则"防幻觉"协议自己会幻觉成功）。
    """
    status, result = None, None
    for line in raw.splitlines():
        if line.startswith("[状态]"):
            status = line.split("]", 1)[1].strip().lower()
        elif line.startswith("[结果]"):
            result = line.split("]", 1)[1].strip()
    if status not in ("done", "failed", "need_confirm"):
        return "failed", f"LLM 输出未遵循三态格式（缺 [状态] 标记）: {raw[:200]}"
    if result is None:
        result = raw  # 有状态但缺 [结果] 行：退回原文（状态仍有效）
    return status, result


def process_one(task: Task, q: FileQueue) -> None:
    q.mark_processing(task)
    try:
        raw = call_llm(task.prompt, timeout=task.timeout)
        status, result = parse_three_state(raw)
    except Exception as e:  # LLM 调用失败 → failed（诚实汇报，不掩盖）
        status, result = "failed", f"LLM call error: {e}"
    task.status = status
    task.result = result
    task.result_meta = {"finished_at": time.time()}
    q.complete(task)
    print(f"[{status}] {task.task_id}: {str(result)[:80]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-dir", default=os.path.join(os.path.dirname(__file__), "..", "runtime"))
    ap.add_argument("--once", action="store_true", help="只处理当前队列后退出")
    ap.add_argument("--interval", type=float, default=10.0)
    args = ap.parse_args()

    q = FileQueue(args.queue_dir)
    if args.once:
        for t in q.scan():
            process_one(t, q)
        print("done (once)")
        return

    print(f"worker 运行中，队列: {args.queue_dir}（Ctrl+C 退出）")
    while True:
        try:
            for t in q.scan():
                process_one(t, q)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("worker 退出")
            return


if __name__ == "__main__":
    main()

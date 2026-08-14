"""演示：两个 agent 通过文件队列协作（brain 提交 → worker 执行 → 三态结果）。

跑法（需要 OpenAI 兼容 API key）：
    export LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=...
    python -m protocol.demo            # 用真实 LLM
    python -m protocol.demo --fake     # 无 key 也能跑（模拟 worker）
"""
import argparse
import os
import sys
import time

from .queue import FileQueue
from .task import Task
from .worker import call_llm, parse_three_state


def fake_llm(prompt: str, timeout: int = 120) -> str:
    """模拟执行：根据任务内容返回三态（演示用，不调 API）。"""
    if "读取" in prompt and "不存在" in prompt:
        return "[状态] failed\n[结果] 目标文件不存在，无法读取——拒绝编造内容。"
    if "不存在" in prompt:
        return "[状态] need_confirm\n[结果] 数据不足，无法判断，请补充上下文。"
    return "[状态] done\n[结果] 任务完成（模拟）：已按四要素规范执行。"


def run_demo(fake: bool):
    queue_dir = os.path.join(os.path.dirname(__file__), "..", "runtime")
    q = FileQueue(queue_dir)

    # brain 提交一个四要素任务
    task = Task(
        goal="计算 5 件设备的总质量并给出平均值",
        context="设备质量(kg): 3.5, 4.4, 5.0, 9.0, 13.0",
        constraints="输出保留 1 位小数；不超过 3 行",
        acceptance="总质量=35.9kg 且平均=7.2kg（可复核）",
    )
    q.submit(task)
    print(f"[brain] 提交任务 {task.task_id}")

    # worker 执行（真实 LLM 或模拟）
    if fake:
        raw = fake_llm(task.prompt)
    else:
        raw = call_llm(task.prompt)
    status, result = parse_three_state(raw)
    task.status = status
    task.result = result
    q.complete(task)
    print(f"[worker] 完成: [{status}] {result}")

    # brain 读结果
    done = q.wait_result(task.task_id, timeout=10)
    print(f"[brain] 收到: [{done.status}] {done.result}")

    # 演示 failed / need_confirm 三态
    for bad in ("请读取 /secret/配置.json 并汇报（文件不存在）",
                "目标：评估一台不存在的设备的散热方案"):
        t2 = Task(goal=bad[:40], context="", constraints="", acceptance="")
        q.submit(t2)
        raw = fake_llm(t2.prompt) if fake else call_llm(t2.prompt)
        st, res = parse_three_state(raw)
        t2.status, t2.result = st, res
        q.complete(t2)
        done2 = q.wait_result(t2.task_id, timeout=10)
        print(f"[brain] 三态演示: [{done2.status}] {done2.result}")

    print("\n演示完成。队列目录:", os.path.abspath(queue_dir))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="模拟 worker（无需 API key）")
    args = ap.parse_args()
    run_demo(args.fake)

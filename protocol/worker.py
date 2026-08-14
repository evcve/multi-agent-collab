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


def parse_three_state(raw: str) -> tuple[str, str, str]:
    """解析 [状态] / [结果] / [备注]（备注不参与状态判断，供提交方参考）。

    备注支持多行：`[备注]` 之后直到下一个 `[标记]` 或结尾的所有行都归入备注。
    防误报：未找到合法的 [状态] 标记 → 返回 failed（格式偏差绝不能静默降级为 done，
    否则"防幻觉"协议自己会幻觉成功）。
    """
    status, result, note = None, None, ""
    lines = raw.splitlines()
    in_note = False
    note_parts = []

    def split_val(line: str) -> str:
        """'[状态] xxx' -> 'xxx'；'[状态]'（无内容）-> ''（防 IndexError）。"""
        return line.split("]", 1)[1].strip() if "]" in line else ""

    for line in lines:
        if line.startswith("[状态]"):
            in_note = False
            status = split_val(line).lower()
        elif line.startswith("[结果]"):
            in_note = False
            result = split_val(line)
        elif line.startswith("[备注]"):
            in_note = True
            note_parts.append(split_val(line))
        elif in_note:
            note_parts.append(line.strip())
    if note_parts:
        note = "\n".join(p for p in note_parts if p)
    if status not in ("done", "failed", "need_confirm"):
        return "failed", f"LLM 输出未遵循三态格式（缺 [状态] 标记）: {raw[:200]}", note
    if result is None:
        result = raw  # 有状态但缺 [结果] 行：退回原文（状态仍有效）
    return status, result, note


def validate_schema(data, schema: dict, max_depth: int = 20) -> list:
    """迷你 JSON Schema 校验（stdlib only）：type/required/properties/items。

    返回错误列表（空 = 通过）。带递归深度上限（防 DoS）。
    支持类型：object/array/string/number/integer/boolean/null。
    """
    errors = []

    def check(value, sch, path, depth):
        if depth > max_depth:
            errors.append(f"{path}: schema 嵌套超过深度上限 {max_depth}")
            return
        t = sch.get("type")
        if t == "object":
            if not isinstance(value, dict):
                errors.append(f"{path}: 期望 object，实际 {type(value).__name__}")
                return
            for k in sch.get("required", []):
                if k not in value:
                    errors.append(f"{path}.{k}: 缺少必填字段")
            for k, sub in sch.get("properties", {}).items():
                if k in value:
                    check(value[k], sub, f"{path}.{k}", depth + 1)
        elif t == "array":
            if not isinstance(value, list):
                errors.append(f"{path}: 期望 array，实际 {type(value).__name__}")
                return
            for i, item in enumerate(value):
                check(item, sch.get("items", {}), f"{path}[{i}]", depth + 1)
        elif t == "string":
            if not isinstance(value, str):
                errors.append(f"{path}: 期望 string，实际 {type(value).__name__}")
        elif t == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{path}: 期望 number，实际 {type(value).__name__}")
        elif t == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{path}: 期望 integer，实际 {type(value).__name__}")
        elif t == "boolean":
            if not isinstance(value, bool):
                errors.append(f"{path}: 期望 boolean，实际 {type(value).__name__}")
        elif t == "null":
            if value is not None:
                errors.append(f"{path}: 期望 null，实际 {type(value).__name__}")
        else:
            # 不支持的 type：显式报错而不是静默通过（防"以为校验了其实没有"）
            errors.append(f"{path}: 不支持的 schema type: {t}")

    check(data, schema, "$", 0)
    return errors


def process_one(task: Task, q: FileQueue) -> None:
    q.mark_processing(task)
    if q.is_cancelled(task.task_id):
        task.status = "failed"
        task.result = "Task cancelled by submitter."
        task.result_meta = {"retryable": False, "cancelled": True,
                            "finished_at": time.time()}
        q.complete(task)
        print(f"[cancelled] {task.task_id}")
        return
    try:
        raw = call_llm(task.prompt, timeout=task.timeout)
        status, result, note = parse_three_state(raw)
    except Exception as e:  # LLM 调用失败 → failed（诚实汇报，可重试）
        status, result, note = "failed", f"LLM call error: {e}", ""
    task.status = status
    task.result = result
    meta = {"finished_at": time.time(), "retryable": status == "failed"}
    if note:
        meta["note"] = note
    # 结果 Schema 自动核验（done 且提交方给了 schema）
    if status == "done" and task.result_schema:
        try:
            data = json.loads(result)
            errs = validate_schema(data, task.result_schema)
            if errs:
                task.status = "failed"
                meta["retryable"] = False
                meta["validation"] = errs
                task.result = f"结果未通过 schema 校验: {'; '.join(errs)}"
            else:
                meta["validation"] = "ok"
        except json.JSONDecodeError:
            task.status = "failed"
            meta["retryable"] = False
            meta["validation"] = ["结果不是合法 JSON"]
            task.result = "结果不是合法 JSON，无法通过 schema 校验"
    task.result_meta = meta
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

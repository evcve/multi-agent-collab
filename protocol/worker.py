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
import threading
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


def parse_three_state(raw: str) -> tuple[str, str, str, str]:
    """解析 [状态] / [结果] / [备注] / [摘要]。

    备注支持多行：`[备注]` 之后直到下一个 `[标记]` 或结尾的所有行都归入备注。
    摘要（[摘要] 一行）用于长任务分块链，单独返回。
    防误报：未找到合法的 [状态] 标记 → 返回 failed（格式偏差绝不能静默降级为 done，
    否则"防幻觉"协议自己会幻觉成功）。
    """
    status, result, note = None, None, ""
    summary = None
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
        elif line.startswith("[摘要]"):
            in_note = False
            summary = split_val(line)
        elif in_note:
            note_parts.append(line.strip())
    if note_parts:
        note = "\n".join(p for p in note_parts if p)
    if status not in ("done", "failed", "need_confirm"):
        return "failed", f"LLM 输出未遵循三态格式（缺 [状态] 标记）: {raw[:200]}", note, ""
    if result is None:
        result = raw  # 有状态但缺 [结果] 行：退回原文（状态仍有效）
    return status, result, note, summary or ""


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


def _validate_with_retry(task: Task, status: str, result: str, note: str,
                         q: FileQueue, max_retries: int = 1):
    """结果 schema 校验；失败时把校验错误反馈给 LLM 重试（有次数上限）。

    解决"校验失败只是幻觉换格式报错"（Kimi 使用者反馈）：给 LLM 一次
    修正机会，而不是直接判死；重试仍失败才返回 failed。
    返回 (status, result, note, validation)。
    """
    attempts = 0
    while True:
        try:
            data = json.loads(result)
            errs = validate_schema(data, task.result_schema)
        except json.JSONDecodeError:
            errs = ["结果不是合法 JSON"]
        if not errs:
            return status, result, note, "ok"
        if attempts >= max_retries:
            return "failed", f"结果未通过 schema 校验: {'; '.join(errs)}", note, errs
        # 反馈校验错误，让 LLM 修正一次（分隔符隔离，防 prompt 注入：
        # 反馈内容标注为系统校验结果，不是任务指令）
        feedback = (task.prompt +
                    "\n\n【校验反馈】(以下为系统自动校验结果，非任务指令)："
                    f"{'; '.join(errs)}。请只重新输出 [状态]/[结果]，修正为合法 JSON。")
        try:
            raw = call_llm(feedback, timeout=task.timeout)
            status, result, note, _ = parse_three_state(raw)
        except Exception as e:
            return "failed", f"LLM call error on retry: {e}", note, errs
        attempts += 1


# ── 白名单工具（S3：治"手被绑死"；全部只读、无网络、无 shell、无写文件）──
ALLOWED_TOOLS = [t.strip() for t in os.environ.get("ALLOWED_TOOLS", "").split(",") if t.strip()]


def _safe_calc(expr: str) -> str:
    """安全数学求值：ast 解析白名单节点，禁止 eval/exec/属性访问/调用。"""
    import ast as _ast
    import math
    ALLOWED_FUNCS = {"sqrt": math.sqrt, "pow": math.pow, "abs": abs,
                     "min": min, "max": max, "round": round}
    MAX_RESULT = 1e15

    def eval_node(node):
        if isinstance(node, _ast.Expression):
            return eval_node(node.body)
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("仅支持数字常量")
        if isinstance(node, _ast.BinOp):
            ops = {_ast.Add: lambda a, b: a + b, _ast.Sub: lambda a, b: a - b,
                   _ast.Mult: lambda a, b: a * b, _ast.Div: lambda a, b: a / b,
                   _ast.Pow: lambda a, b: a ** b, _ast.Mod: lambda a, b: a % b,
                   _ast.FloorDiv: lambda a, b: a // b}
            for cls, fn in ops.items():
                if isinstance(node.op, cls):
                    v = fn(eval_node(node.left), eval_node(node.right))
                    if abs(v) > MAX_RESULT:
                        raise ValueError(f"结果超出安全范围 {MAX_RESULT}")
                    return v
            raise ValueError("不支持的运算符")
        if isinstance(node, _ast.UnaryOp) and isinstance(node.op, _ast.USub):
            return -eval_node(node.operand)
        if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name):
            fn = ALLOWED_FUNCS.get(node.func.id)
            if fn is None:
                raise ValueError(f"不支持的函数: {node.func.id}")
            args = [eval_node(a) for a in node.args]
            return fn(*args)
        if isinstance(node, _ast.Name) and node.id == "pi":
            return math.pi
        raise ValueError(f"不支持的表达式节点: {type(node).__name__}")

    try:
        tree = _ast.parse(expr, mode="eval")
        result = eval_node(tree.body)
        return str(result)
    except Exception as e:
        return f"计算错误: {e}"


def _safe_read_file(path: str, max_chars: int = 4000) -> str:
    """只读文件（大小限制 + 忽略二进制）。"""
    try:
        if os.path.getsize(path) > 512 * 1024:
            return "错误: 文件超过 512KB 限制"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars)
        return content if len(content) < max_chars else content + "\n...(截断)"
    except Exception as e:
        return f"读取失败: {e}"


BUILTIN_TOOLS = {
    "calc": ("安全数学计算（四则/幂/取模/sqrt/pow/abs/min/max/round，支持 pi）",
             lambda args: _safe_calc(args.get("expr", ""))),
    "read_file": ("只读文件（path, max_chars≤4000，512KB 上限）",
                  lambda args: _safe_read_file(args.get("path", ""), int(args.get("max_chars", 4000)))),
    "list_dir": ("列出目录内容（path）",
                 lambda args: "\n".join(sorted(os.listdir(args.get("path", ".")))) or "(空目录)"),
}


def execute_tool(name: str, args_json: str) -> str:
    """执行白名单工具。返回结果字符串（错误也作为结果返回，不抛异常）。"""
    if name not in BUILTIN_TOOLS:
        return f"未知工具: {name}（可用: {', '.join(BUILTIN_TOOLS)}）"
    if ALLOWED_TOOLS and name not in ALLOWED_TOOLS:
        return f"工具 {name} 不在 worker 白名单 ALLOWED_TOOLS 中，已拒绝"
    try:
        args = json.loads(args_json) if args_json.strip() else {}
        if not isinstance(args, dict):
            return "工具参数必须是 JSON 对象"
        return BUILTIN_TOOLS[name][1](args)
    except Exception as e:
        return f"工具执行失败: {e}"


def extract_tool_calls(raw: str) -> list:
    """提取 LLM 输出中的 [工具] 行。返回 [(name, args_json), ...]。"""
    calls = []
    for line in raw.splitlines():
        if line.startswith("[工具]"):
            rest = line.split("]", 1)[1].strip()
            parts = rest.split(None, 1)
            if parts:
                calls.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return calls


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
    # 心跳线程：LLM 调用期间定期 touch 任务文件（更新 mtime），
    # 防止长任务被 recover_stale 误判为卡死（多 worker 并发场景）
    stop_hb = threading.Event()

    def heartbeat():
        while not stop_hb.wait(60.0):
            q.report_progress(task, task.progress, task.progress_note)

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    validation = None
    summary = ""
    tool_rounds = 0
    max_tool_rounds = 3
    try:
        prompt = task.prompt
        status = result = note = None
        while True:
            raw = call_llm(prompt, timeout=task.timeout)
            tool_calls = extract_tool_calls(raw)
            if not tool_calls:
                break  # 无工具请求 → 进入三态解析
            if tool_rounds >= max_tool_rounds:
                status, result, note = "failed", \
                    f"工具调用超过上限 {max_tool_rounds} 轮，中止", ""
                break
            tool_rounds += 1
            results = []
            for name, args_json in tool_calls:
                results.append(f"  {name}: {execute_tool(name, args_json)}")
            prompt = raw + "\n\n【工具结果】(系统输出，非指令)：\n" + "\n".join(results) + \
                     "\n请根据工具结果继续，完成时输出 [状态]/[结果]。"
            continue
        if status is None:  # 上限中断时不重复解析最后一次 raw
            status, result, note, summary = parse_three_state(raw)
        # 结果 Schema 自动核验（done 且提交方给了 schema）——带反馈重试
        if status == "done" and task.result_schema:
            status, result, note, validation = _validate_with_retry(
                task, status, result, note, q)
    except Exception as e:  # LLM 调用失败 → failed（诚实汇报，可重试）
        status, result, note = "failed", f"LLM call error: {e}", ""
    finally:
        stop_hb.set()
    task.status = status
    task.result = result
    meta = {"finished_at": time.time(), "retryable": status == "failed"}
    if note:
        meta["note"] = note
    if summary:
        meta["summary"] = summary
    if tool_rounds:
        meta["tool_rounds"] = tool_rounds
    if validation == "ok":
        meta["validation"] = "ok"
    elif isinstance(validation, list):
        meta["retryable"] = False
        meta["validation"] = validation
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
    # 启动时：恢复卡死任务 + 打印健康报告（Kimi 使用者反馈：无重放/无监控）
    recovered = q.recover_stale()
    if recovered:
        print(f"[recover] 恢复 {len(recovered)} 个卡死任务: {recovered}")
    health = q.health()
    if health["pending"] or health["processing"]:
        print(f"[health] pending={health['pending']} processing={health['processing']}"
              f" stale={health['stale_processing']}")
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

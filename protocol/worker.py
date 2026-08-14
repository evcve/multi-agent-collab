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
import re
import sys
import threading
import time
import urllib.request

from .queue import FileQueue
from .task import Task


LLM_MAX_TOKENS = os.environ.get("LLM_MAX_TOKENS", "8000")

SYSTEM_PROMPT = (
    "你是多智能体协作中的任务执行单元（worker）。"
    "严格按任务要求执行，输出遵循任务中指定的反馈格式（三态/JSON等）。"
    "不确定就说不知道，绝不编造。"
)


def call_llm(prompt: str, timeout: int = 120) -> str:
    """调 OpenAI 兼容端点；默认智谱 glm-5.2，可用环境变量覆盖。

    LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_MAX_TOKENS
    key 按 base_url 联动选择：bigmodel→ZHIPUAI_API_KEY、moonshot→KIMI_API_KEY、
    其他→LLM_API_KEY（避免把错误 key 发给目标端点）。
    """
    base_url = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    model = os.environ.get("LLM_MODEL", "glm-5.2")
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8000"))
    if "bigmodel" in base_url:
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("ZHIPUAI_API_KEY", "")
    elif "moonshot" in base_url:
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("KIMI_API_KEY", "")
    else:
        api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        raise RuntimeError(f"未找到 {base_url} 对应的 API key"
                           "（智谱: ZHIPUAI_API_KEY / Kimi: KIMI_API_KEY / 通用: LLM_API_KEY）")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(f"{base_url}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        raise RuntimeError("模型返回空 content（thinking 吃光 max_tokens？请调大 LLM_MAX_TOKENS）")
    return content


STATUS_ALIASES = {
    "完成": "done", "成功": "done", "ok": "done", "success": "done",
    "失败": "failed", "错误": "failed", "error": "failed",
    "需要确认": "need_confirm", "确认": "need_confirm", "不确定": "need_confirm",
}


def _split_val(line: str) -> str:
    """'[状态] xxx' -> 'xxx'；去常见分隔符（/、:）；空 -> ''（防 IndexError）。"""
    rest = line.split("]", 1)[1].strip() if "]" in line else ""
    if rest.startswith("/"):      # '[完成]/56088'
        rest = rest[1:].strip()
    if rest.startswith(":"):      # '[完成]: 内容'
        rest = rest[1:].strip()
    return rest


def _try_json(raw: str):
    """JSON 兜底：LLM 直接输出 {"status": ..., "result": ...} 或 {"result": ...}。"""
    try:
        d = json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    status = str(d.get("status", "")).strip().lower()
    if status not in ("done", "failed", "need_confirm"):
        status = STATUS_ALIASES.get(status, "")
    result = d.get("result")
    if status and result is not None:
        return status, str(result), "", str(d.get("summary", "") or "")
    return None


def parse_three_state(raw: str) -> tuple[str, str, str, str]:
    """解析 [状态] / [结果] / [备注] / [摘要]（含中文别名与 JSON 兜底）。

    备注支持多行：`[备注]` 之后直到下一个 `[标记]` 或结尾的所有行都归入备注。
    摘要（[摘要] 一行）用于长任务分块链，单独返回。
    防误报：完全无标记 → failed（格式偏差绝不静默降级为 done——除非有非空
    [结果]/[完成] 类内容，那是真实 LLM 的格式漂移，宽容接受）。
    """
    status, result, note = None, None, ""
    summary = None
    lines = raw.splitlines()
    in_note = False
    note_parts = []
    for line in lines:
        if line.startswith("[状态]"):
            in_note = False
            status = _split_val(line).lower()
            status = STATUS_ALIASES.get(status, status)   # 中文别名映射（[状态] 完成 → done）
        elif line.startswith("[结果]"):
            in_note = False
            result = _split_val(line)
        elif line.startswith("[完成]") or line.startswith("[成功]") or line.startswith("[ok]") \
                or line.startswith("[OK]"):
            in_note = False
            status = "done"
            result = _split_val(line)
        elif line.startswith("[失败]") or line.startswith("[错误]"):
            in_note = False
            status = "failed"
            result = _split_val(line)
        elif line.startswith("[需要确认]") or line.startswith("[确认]"):
            in_note = False
            status = "need_confirm"
            result = _split_val(line)
        elif line.startswith("[备注]"):
            in_note = True
            note_parts.append(_split_val(line))
        elif line.startswith("[摘要]"):
            in_note = False
            summary = _split_val(line)
        elif in_note:
            note_parts.append(line.strip())
    if note_parts:
        note = "\n".join(p for p in note_parts if p)
    if status not in ("done", "failed", "need_confirm"):
        # JSON 兜底：LLM 直接输出 {"status": ..., "result": ...}
        j = _try_json(raw)
        if j:
            return j
        # 宽容降级仅限"缺失状态"：有非空 [结果] 视为 done（真实 LLM 常漏状态行），
        # 但降级会被调用方标记 format_loose（见 process_one）供提交方判断；
        # "未知状态"（明确输出了非标准值）仍 failed——更值得警惕。
        if not status and result and result.strip():
            status = "done"
            note = (note + "\n" if note else "") + "[loose]"  # 降级标记，调用方提取
        else:
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


def _coerce_json(result: str):
    """把 LLM 结果清洗为可校验数据：严格 JSON → 提取 JSON 片段 → 裸数字/布尔。

    真实 LLM 常输出 '56088（通过工具计算）' 这类带杂质的字符串，
    直接 json.loads 会失败——先提取核心值再校验。
    """
    result = (result or "").strip()
    if not result:
        return None
    # 错误文本黑名单：'Error code 404'、'请求失败' 等不能清洗成数字误判通过
    if any(m in result.lower() for m in ("error", "fail", "exception", "timeout",
                                         "错误", "失败", "拒绝", "超时")):
        return None
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        s, e = result.find(opener), result.rfind(closer)
        if s >= 0 and e > s:
            try:
                return json.loads(result[s:e + 1])
            except json.JSONDecodeError:
                continue
    if any(c.isdigit() for c in result):      # 裸数字（含 '56088（...）'）
        try:
            return float(result.replace(",", ""))
        except ValueError:
            try:
                return float("".join(c for c in result if c.isdigit() or c in ".-"))
            except ValueError:
                return None
    return None


def _validate_with_retry(task: Task, status: str, result: str, note: str,
                         q: FileQueue, max_retries: int = 1):
    """结果 schema 校验；失败时把校验错误反馈给 LLM 重试（有次数上限）。

    解决"校验失败只是幻觉换格式报错"（Kimi 使用者反馈）：给 LLM 一次
    修正机会，而不是直接判死；重试仍失败才返回 failed。
    返回 (status, result, note, validation)。
    """
    attempts = 0
    while True:
        data = _coerce_json(result)
        if data is None:
            errs = ["结果不是合法 JSON"]
        else:
            errs = validate_schema(data, task.result_schema)
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


def _resolve_path(path: str) -> str:
    """跨平台路径解析：server 运行在 WSL 时，Windows 路径（D:/...）转 /mnt/d/...。

    协议桥可能跑在 Windows 或 WSL——提交方习惯 Windows 路径，
    工具必须兼容（本次实测发现的真问题）。
    """
    if os.path.exists(path):
        return path
    is_wsl = os.path.exists("/proc/version") and "microsoft" in open("/proc/version").read().lower()
    if is_wsl and re.match(r"^[A-Za-z]:[/\\]", path):
        drive, rest = path[0].lower(), path[2:].replace("\\", "/")
        mapped = f"/mnt/{drive}{rest}"
        if os.path.exists(mapped):
            return mapped
    return path


def _safe_read_file(path: str, max_chars: int = 4000) -> str:
    """只读文件前缀（大文件也允许读开头——只 seek 读 N 字符，不加载整个文件）。"""
    path = _resolve_path(path)
    max_chars = min(int(max_chars), 8000)   # 单次读取上限（防超量）
    try:
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
    if "[loose]" in note:               # 宽容降级标记 → 提交方可判断结果可信度
        meta["format_loose"] = True
        meta["note"] = note.replace("[loose]", "").strip()
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

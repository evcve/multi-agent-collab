"""protocol 单元测试：Task 结构、三态解析、文件队列闭环。"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.queue import FileQueue
from protocol.task import Task
from protocol.worker import parse_three_state


# ── Task ────────────────────────────────────────────────────────────
def test_task_prompt_contains_four_elements():
    t = Task(goal="算平均值", context="1,2,3", constraints="保留1位小数", acceptance="=2.0")
    p = t.prompt
    assert "【目标】算平均值" in p
    assert "【上下文】1,2,3" in p
    assert "【约束】保留1位小数" in p
    assert "【验收标准】=2.0" in p


def test_task_prompt_embeds_context_no_file_ref():
    """防幻觉纪律：数据内嵌，prompt 不包含任何文件路径引用。"""
    t = Task(goal="g", context="数据: x=1,y=2")
    assert "读取" not in t.prompt


def test_task_roundtrip_dict():
    t = Task(goal="g", context="c")
    t2 = Task.from_dict(t.to_dict())
    assert t2.goal == "g" and t2.task_id == t.task_id


# ── 三态解析 ────────────────────────────────────────────────────────
def test_parse_three_state_done():
    s, r, _, _ = parse_three_state("[状态] done\n[结果] 完成，文件 /tmp/a.json")
    assert s == "done" and "/tmp/a.json" in r


def test_parse_three_state_failed_and_confirm():
    s, r, _, _ = parse_three_state("[状态] failed\n[结果] 权限不足")
    assert s == "failed"
    s2, _, _, _ = parse_three_state("[状态] need_confirm\n[结果] 选项A/选项B")
    assert s2 == "need_confirm"


def test_parse_three_state_missing_status_is_failed():
    """防误报：完全无标记（无 [状态] 无 [结果]）→ failed，绝不静默当 done。"""
    s, r, _, _ = parse_three_state("好的，我完成了任务！")   # LLM 自由文本
    assert s == "failed"
    assert "三态格式" in r


def test_parse_missing_status_but_has_result_is_done():
    """宽容降级：真实 LLM 常漏 [状态]——有明确 [结果] 视为 done。"""
    s, r, _, _ = parse_three_state("[结果] 56088")
    assert s == "done" and r == "56088"


def test_parse_three_state_unknown_status_is_failed():
    s, _, _, _ = parse_three_state("[状态] maybe\n[结果] 不确定")
    assert s == "failed"


def test_parse_three_state_status_without_result_keeps_status():
    s, r, _, _ = parse_three_state("[状态] done\n输出了一些内容")
    assert s == "done"


# ── 文件队列 ────────────────────────────────────────────────────────
def test_queue_roundtrip(tmp_path):
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    assert len(q.scan()) == 1

    t.status = "done"
    t.result = "42"
    q.complete(t)          # worker 写结果 + 删队列文件
    assert len(q.scan()) == 0

    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "done" and got.result == "42"


def test_wait_result_after_worker_deleted_queue_file(tmp_path):
    """回归测试：complete() 已删队列文件时，wait_result 仍必须读到结果
    （历史 bug：unlink 已删文件抛 OSError 被 except 吞掉，return 未执行 → 假超时）。"""
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    t.status = "done"
    t.result = "ok"
    q.complete(t)          # 队列文件已被 worker 删掉
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "done" and got.result == "ok"


def test_queue_timeout_returns_timeout_task(tmp_path):
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    got = q.wait_result(t.task_id, timeout=1)
    assert got.status == "timeout"


def test_scan_only_pending(tmp_path):
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    q.mark_processing(t)
    assert q.scan() == []   # processing 不算 pending


# ── V0.2 新特性：备注 / 优先级 / 取消 / Schema / 进度 ─────────────
def test_parse_note_channel():
    s, r, note, _ = parse_three_state("[状态] done\n[结果] 完成\n[备注] 用了方法X")
    assert s == "done" and note == "用了方法X"


def test_scan_priority_order(tmp_path):
    q = FileQueue(str(tmp_path))
    q.submit(Task(goal="low", priority="low"))
    q.submit(Task(goal="high", priority="high"))
    q.submit(Task(goal="normal", priority="normal"))
    order = [t.goal for t in q.scan()]
    assert order == ["high", "normal", "low"]


def test_cancel_skips_task(tmp_path):
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    q.cancel(t.task_id)
    assert q.scan() == []          # 取消的任务不再派发
    assert q.is_cancelled(t.task_id)


def test_worker_marks_cancelled_task(tmp_path):
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    q.cancel(t.task_id)
    process_one(t, q)               # worker 处理
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "failed" and "cancelled" in got.result


def test_schema_validation_ok(tmp_path, monkeypatch):
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="g", result_schema={
        "type": "object", "required": ["name", "mass"],
        "properties": {"name": {"type": "string"}, "mass": {"type": "number"}}})
    q.submit(t)
    monkeypatch.setattr("protocol.worker.call_llm",
                        lambda prompt, **kw: '[状态] done\n[结果] {"name": "X", "mass": 3.5}')
    process_one(t, q)
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "done"
    assert got.result_meta.get("validation") == "ok"


def test_schema_validation_fails_wrong_type(tmp_path, monkeypatch):
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="g", result_schema={
        "type": "object", "required": ["mass"],
        "properties": {"mass": {"type": "number"}}})
    q.submit(t)
    monkeypatch.setattr("protocol.worker.call_llm",
                        lambda prompt, **kw: '[状态] done\n[结果] {"mass": "not-a-number"}')
    process_one(t, q)
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "failed"
    assert got.result_meta.get("retryable") is False
    assert "schema" in got.result


def test_progress_callback_receives_heartbeat(tmp_path):
    q = FileQueue(str(tmp_path))
    t = Task(goal="long")
    q.submit(t)
    # 模拟 worker 心跳：report_progress 更新任务文件
    q.mark_processing(t)
    q.report_progress(t, 50, "半程")
    seen = []
    q.wait_result(t.task_id, timeout=2,
                  progress_callback=lambda p, n: seen.append((p, n)))
    assert (50, "半程") in seen


def test_retryable_flag_on_llm_error(tmp_path, monkeypatch):
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("protocol.worker.call_llm", boom)
    process_one(t, q)
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "failed"
    assert got.result_meta.get("retryable") is True   # LLM 调用错误 → 可重试


# ── 第三方评审(Kimi)意见修复的回归测试 ──────────────────────────
def test_schema_announced_in_prompt():
    """Kimi P1：result_schema 必须进 prompt，LLM 才知道输出 JSON。"""
    t = Task(goal="g", result_schema={"type": "object", "required": ["x"]})
    assert "【输出要求】" in t.prompt
    assert '"required"' in t.prompt


def test_validate_schema_integer_and_null():
    from protocol.worker import validate_schema
    assert validate_schema(3, {"type": "integer"}) == []
    assert validate_schema(3.5, {"type": "integer"}) != []     # float 不是 integer
    assert validate_schema(True, {"type": "integer"}) != []    # bool 不是 integer
    assert validate_schema(None, {"type": "null"}) == []
    assert validate_schema(0, {"type": "null"}) != []


def test_validate_schema_recursion_limit():
    """Kimi P1：深嵌套 schema+数据必须被深度上限拦截（防 DoS）。"""
    from protocol.worker import validate_schema
    # 构造 30 层深的 schema 和 30 层深的数据
    deep_schema = {"type": "object", "properties": {}}
    deep_data = {}
    cur_s, cur_d = deep_schema, deep_data
    for _ in range(30):
        cur_s["properties"] = {"n": {"type": "object", "properties": {}}}
        cur_d["n"] = {}
        cur_s, cur_d = cur_s["properties"]["n"], cur_d["n"]
    errs = validate_schema(deep_data, deep_schema)
    assert any("深度上限" in e for e in errs)


def test_validate_schema_unknown_type_reports_error():
    """Kimi P1：不支持的 type 显式报错，不静默通过。"""
    from protocol.worker import validate_schema
    assert validate_schema("x", {"type": "anyOf"}) != []


def test_cancel_marker_cleaned_on_complete(tmp_path):
    """Kimi P1：任务完成后 cancel marker 必须清理，防目录无限增长。"""
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    q.cancel(t.task_id)
    assert q.is_cancelled(t.task_id)
    t.status = "failed"
    t.result = "cancelled"
    q.complete(t)
    assert not q.is_cancelled(t.task_id)


def test_multiline_note_supported():
    """Kimi P1：备注应支持多行（自由文本语义）。"""
    s, r, note, _ = parse_three_state(
        "[状态] done\n[结果] 完成\n[备注] 第一行\n第二行\n第三行")
    assert s == "done"
    assert note == "第一行\n第二行\n第三行"


def test_parse_no_indexerror_on_bare_marker():
    """智谱 P0：`[状态]` 无内容行不能 IndexError 崩溃，应安全解析为 failed。"""
    s, r, _, _ = parse_three_state("[状态]\n[结果]")
    assert s == "failed"            # 空状态 → failed
    s2, _, _, _ = parse_three_state("[状态] done\n[结果]")   # [结果] 无内容
    assert s2 == "done"             # 状态有效，结果回退原文


# ── Kimi 使用者反馈优化：崩溃恢复 / schema 重试 / 健康检查 ──────
def test_recover_stale_resets_processing(tmp_path):
    """崩溃恢复：卡死 processing 任务应能重放。"""
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    q.mark_processing(t)                       # 模拟 worker 取走
    # stale_after=0：任何 processing 都视为卡死（无需改 mtime，避开 Windows utime 锁）
    recovered = q.recover_stale(stale_after=0)
    assert t.task_id in recovered
    tasks = q.scan()
    assert len(tasks) == 1 and tasks[0].status == "pending"


def test_recover_stale_keeps_fresh_processing(tmp_path):
    q = FileQueue(str(tmp_path))
    t = Task(goal="g")
    q.submit(t)
    q.mark_processing(t)                       # mtime 是新的 → 不算卡死
    assert q.recover_stale(stale_after=300) == []


def test_health_report(tmp_path):
    q = FileQueue(str(tmp_path))
    t1 = Task(goal="a")
    t2 = Task(goal="b")
    q.submit(t1)
    q.submit(t2)
    q.mark_processing(t2)
    h = q.health(stale_after=300)
    assert h["pending"] == 1 and h["processing"] == 1
    assert h["stale_processing"] == []


def test_schema_retry_fixes_output(tmp_path, monkeypatch):
    """Kimi：校验失败不是直接判死——反馈给 LLM 重试，修正后应 done。"""
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="g", result_schema={
        "type": "object", "required": ["x"],
        "properties": {"x": {"type": "integer"}}})
    q.submit(t)
    calls = []
    def fake(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:      # 第一次：错误输出
            return '[状态] done\n[结果] {"x": "not-int"}'
        return '[状态] done\n[结果] {"x": 42}'   # 反馈后修正
    monkeypatch.setattr("protocol.worker.call_llm", fake)
    process_one(t, q)
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "done"
    assert got.result_meta.get("validation") == "ok"
    assert len(calls) == 2       # 确认发生了一次反馈重试


def test_schema_retry_gives_up_after_limit(tmp_path, monkeypatch):
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="g", result_schema={
        "type": "object", "required": ["x"],
        "properties": {"x": {"type": "integer"}}})
    q.submit(t)
    def fake(prompt, **kw):
        return '[状态] done\n[结果] {"x": "still-bad"}'
    monkeypatch.setattr("protocol.worker.call_llm", fake)
    process_one(t, q)
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "failed"
    assert got.result_meta.get("retryable") is False
    assert "schema" in got.result


# ── 作战计划：S1 结构化字段 / B7 TTL 清理 / 真实用例 ───────────
def test_context_fields_rendered_structured():
    """S1：结构化字段以 key: value 块渲染，LLM 不用从散文里找。"""
    t = Task(goal="g", context_fields={"温度": "45°C", "质量": "2.5kg"})
    assert "【数据】" in t.prompt
    assert "  温度: 45°C" in t.prompt
    assert "  质量: 2.5kg" in t.prompt


def test_context_fields_roundtrip_dict():
    t = Task(goal="g", context_fields={"a": 1, "b": [2, 3]})
    t2 = Task.from_dict(t.to_dict())
    assert t2.context_fields == {"a": 1, "b": [2, 3]}


def test_gc_removes_old_results(tmp_path):
    """B7：TTL 清理删除过期结果文件。"""
    q = FileQueue(str(tmp_path))
    import time as _t
    # 写两个结果文件，一个改老
    r1 = os.path.join(q.results_dir, "old.json")
    r2 = os.path.join(q.results_dir, "new.json")
    with open(r1, "w") as f: f.write("{}")
    with open(r2, "w") as f: f.write("{}")
    old = _t.time() - 999999
    os.utime(r1, (old, old))
    removed = q.gc(ttl_days=1)
    assert removed == 1
    assert not os.path.exists(r1) and os.path.exists(r2)


def test_layout_case_fake_engine_finds_conflicts():
    """真实用例：fake 几何引擎正确检出冲突（6 个，含双命中）。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "lc", os.path.join(os.path.dirname(__file__), "..", "examples", "layout_check_case.py"))
    lc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lc)
    raw = lc.fake_engine(lc.build_task().context_fields)
    assert "[状态] done" in raw
    data = json.loads(raw.split("[结果] ")[1].splitlines()[0])
    assert data["total_holes"] == 7
    assert len(data["conflicts"]) == 6   # EQ-A 孔0 同时撞 H2 和 V3 → 双命中


# ── S3 白名单工具 ────────────────────────────────────────────────
def test_safe_calc_basic():
    from protocol.worker import _safe_calc
    assert _safe_calc("40*30") == "1200"
    assert _safe_calc("sqrt(16)") == "4.0"
    assert _safe_calc("(2+3)*4") == "20"


def test_safe_calc_rejects_unsafe():
    from protocol.worker import _safe_calc
    assert "错误" in _safe_calc("__import__('os').system('dir')")
    assert "错误" in _safe_calc("'a'*1000")
    assert "错误" in _safe_calc("open('x').read()")


def test_execute_tool_whitelist_rejection(monkeypatch):
    from protocol.worker import execute_tool
    monkeypatch.setenv("ALLOWED_TOOLS", "calc")   # 只允许 calc
    import protocol.worker as W
    W.ALLOWED_TOOLS = ["calc"]
    assert "不在 worker 白名单" in execute_tool("read_file", '{"path": "/etc/passwd"}')
    assert execute_tool("calc", '{"expr": "1+1"}') == "2"


def test_extract_tool_calls():
    from protocol.worker import extract_tool_calls
    raw = "[工具] calc {\"expr\": \"1+1\"}\n[工具] list_dir {\"path\": \".\"}\n[状态] done"
    calls = extract_tool_calls(raw)
    assert calls == [("calc", '{"expr": "1+1"}'), ("list_dir", '{"path": "."}')]


def test_tool_loop_executes_and_finishes(tmp_path, monkeypatch):
    """工具循环：LLM 先请求工具 → 拿到结果 → 输出三态。"""
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="计算 40*30", tools=["calc"])
    q.submit(t)
    calls = []
    def fake(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return '[工具] calc {"expr": "40*30"}'
        assert "【工具结果】" in prompt and "1200" in prompt  # 工具结果已反馈
        return '[状态] done\n[结果] 结果是 1200'
    monkeypatch.setattr("protocol.worker.call_llm", fake)
    process_one(t, q)
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "done"
    assert got.result_meta.get("tool_rounds") == 1
    assert len(calls) == 2


def test_tool_loop_hits_limit(tmp_path, monkeypatch):
    """工具循环超过上限 → failed。"""
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="g", tools=["calc"])
    q.submit(t)
    def fake(prompt, **kw):
        return '[工具] calc {"expr": "1+1"}'
    monkeypatch.setattr("protocol.worker.call_llm", fake)
    process_one(t, q)
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "failed"
    assert "上限" in got.result


# ── S4 分块摘要 ──────────────────────────────────────────────────
def test_parse_summary():
    s, r, n, sm = parse_three_state(
        "[状态] done\n[结果] 完成\n[摘要] 布局分析完成，5 处冲突")
    assert s == "done" and sm == "布局分析完成，5 处冲突"


def test_summary_stored_in_meta(tmp_path, monkeypatch):
    """worker 把 [摘要] 存入 result_meta.summary（分块链用）。"""
    from protocol.worker import process_one
    q = FileQueue(str(tmp_path))
    t = Task(goal="g", request_summary=True)
    q.submit(t)
    monkeypatch.setattr("protocol.worker.call_llm",
                        lambda prompt, **kw: "[状态] done\n[结果] ok\n[摘要] 第一块完成")
    process_one(t, q)
    got = q.wait_result(t.task_id, timeout=5)
    assert got.status == "done"
    assert got.result_meta.get("summary") == "第一块完成"


def test_summary_requested_in_prompt():
    t = Task(goal="g", request_summary=True)
    assert "[摘要]" in t.prompt
    t2 = Task(goal="g")
    assert "[摘要]" not in t2.prompt


# ── 鲁棒解析：中文别名 / JSON 兜底（真实 LLM 格式漂移）──
def test_parse_chinese_alias_done():
    s, r, _, _ = parse_three_state("[完成]/56088")
    assert s == "done" and r == "56088"


def test_parse_chinese_alias_failed():
    s, r, _, _ = parse_three_state("[失败]: 网络错误")
    assert s == "failed" and "网络错误" in r


def test_parse_json_fallback():
    s, r, _, _ = parse_three_state('{"status": "done", "result": "完成"}')
    assert s == "done" and r == "完成"


# ── 协议桥实战测试发现的修复：跨平台路径 + 大文件前缀读 ──
def test_resolve_path_windows_on_windows():
    """Windows 侧：存在的 Windows 路径原样返回（不误转）。"""
    from protocol.worker import _resolve_path
    p = os.path.abspath(__file__)
    assert _resolve_path(p) == p


def test_read_file_prefix_of_large_file(tmp_path):
    """大文件允许读前缀（只读 N 字符，不加载整个文件）。"""
    from protocol.worker import _safe_read_file
    big = tmp_path / "big.bin"
    big.write_text("A" * 2000000)   # 2MB，超过旧 512KB 限制
    out = _safe_read_file(str(big), max_chars=100)
    assert "A" * 100 in out and "(截断)" in out

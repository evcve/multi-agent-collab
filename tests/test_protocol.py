"""protocol 单元测试：Task 结构、三态解析、文件队列闭环。"""
import os
import sys

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
    s, r, _ = parse_three_state("[状态] done\n[结果] 完成，文件 /tmp/a.json")
    assert s == "done" and "/tmp/a.json" in r


def test_parse_three_state_failed_and_confirm():
    s, r, _ = parse_three_state("[状态] failed\n[结果] 权限不足")
    assert s == "failed"
    s2, _, _ = parse_three_state("[状态] need_confirm\n[结果] 选项A/选项B")
    assert s2 == "need_confirm"


def test_parse_three_state_missing_status_is_failed():
    """防误报：无 [状态] 标记时绝不能静默当 done。"""
    s, r, _ = parse_three_state("好的，我完成了任务！")   # LLM 自由文本
    assert s == "failed"
    assert "三态格式" in r


def test_parse_three_state_unknown_status_is_failed():
    s, _, _ = parse_three_state("[状态] maybe\n[结果] 不确定")
    assert s == "failed"


def test_parse_three_state_status_without_result_keeps_status():
    s, r, _ = parse_three_state("[状态] done\n输出了一些内容")
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
    s, r, note = parse_three_state("[状态] done\n[结果] 完成\n[备注] 用了方法X")
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

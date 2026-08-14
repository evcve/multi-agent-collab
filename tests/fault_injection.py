"""故障注入测试套件：把"基本功能能跑"推进到"复杂场景稳定"。

覆盖架构评审 #4 的真实事故场景：
  crash 恢复 / 并发重复消费 / invalid JSON / partial result / 幻觉 /
  网络错误重试 / quota 耗尽 / duplicate task_id / 并发 worker
"""
import os
import sys
import threading
import time
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protocol.queue import FileQueue
from protocol.task import Task
from protocol.worker import process_one


def _mkq(tmp_path):
    return FileQueue(str(tmp_path / "rt"))


# ── 1. worker crash → recover_stale 重放 ─────────────────────
def test_worker_crash_then_recover(tmp_path, monkeypatch):
    """worker 处理中崩溃（锁残留 + processing 卡死）→ recover 后重放成功。"""
    from protocol import worker as W
    q = _mkq(tmp_path)
    t = Task(goal="g")
    q.submit(t)

    calls = {"n": 0}
    def fake(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker 崩溃（模拟）")   # 第一次处理崩了
        return "[状态] done\n[结果] 恢复成功"

    monkeypatch.setattr(W, "call_llm", fake)
    process_one(t, q)                                   # 崩溃：failed + 锁残留
    assert t.status == "failed"

    # 模拟超时：recover 把卡死任务放回 queue（stale_after=-1 保证 mtime 判定）
    q.recover_stale(stale_after=-1)
    tasks = q.scan()
    assert len(tasks) == 1 and tasks[0].status == "pending"

    process_one(tasks[0], q)                            # 重放成功
    assert tasks[0].status == "done"
    assert "恢复成功" in tasks[0].result
    assert calls["n"] == 2


# ── 2. 并发 worker → 不重复消费 ──────────────────────────────
def test_concurrent_workers_no_duplicate(tmp_path, monkeypatch):
    """两个 worker 同时抢同一任务 → 只有一次 LLM 调用（原子认领）。"""
    from protocol import worker as W
    q = _mkq(tmp_path)
    t = Task(goal="g")
    q.submit(t)

    calls = {"n": 0, "lock": threading.Lock()}
    def fake(prompt, **kw):
        with calls["lock"]:
            calls["n"] += 1
        time.sleep(0.3)                                  # 拉长处理时间制造竞态
        return "[状态] done\n[结果] ok"

    monkeypatch.setattr(W, "call_llm", fake)
    results = []

    def run():
        try:
            for task in q.scan():
                process_one(task, q)
                results.append(task.status)
        except Exception as e:
            results.append(f"err:{e}")

    ths = [threading.Thread(target=run) for _ in range(2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()

    assert calls["n"] == 1, f"重复消费! LLM 调用了 {calls['n']} 次"
    assert results.count("done") == 1


# ── 3. invalid JSON → 诚实 failed ────────────────────────────
def test_invalid_json_fails_honestly(tmp_path, monkeypatch):
    from protocol import worker as W
    q = _mkq(tmp_path)
    t = Task(goal="g", result_schema={"type": "object",
                                      "required": ["result"]})
    q.submit(t)

    monkeypatch.setattr(W, "call_llm",
                        lambda p, **k: "[状态] done\n[结果] 这不是JSON{{{")
    process_one(t, q)
    assert t.status == "failed"
    assert "schema" in t.result


# ── 4. partial result → 校验拦截 ─────────────────────────────
def test_partial_result_blocked(tmp_path, monkeypatch):
    """半截 JSON（缺必填字段）→ 反馈重试 → 仍不满足 → failed。"""
    from protocol import worker as W
    q = _mkq(tmp_path)
    t = Task(goal="g", result_schema={"type": "object",
                                      "required": ["result", "mass"]})
    q.submit(t)

    monkeypatch.setattr(W, "call_llm",
                        lambda p, **k: '[状态] done\n[结果] {"result": 1}')
    process_one(t, q)
    assert t.status == "failed"
    assert "mass" in t.result   # 缺失字段被点名


# ── 5. 幻觉内容 → 校验拦截 ───────────────────────────────────
def test_hallucination_blocked(tmp_path, monkeypatch):
    """LLM 编造结果（值类型不对）→ schema 拦截 → 不谎报成功。"""
    from protocol import worker as W
    q = _mkq(tmp_path)
    t = Task(goal="g", result_schema={"type": "object",
                                      "properties": {"temperature": {"type": "number"}},
                                      "required": ["temperature"]})
    q.submit(t)

    monkeypatch.setattr(W, "call_llm",
                        lambda p, **k: '[状态] done\n[结果] {"temperature": "很高"}')
    process_one(t, q)
    assert t.status == "failed"          # 字符串 ≠ number，不能通过
    assert t.result_meta.get("retryable") is False  # 业务失败，非可重试


# ── 6. 网络错误 → call_llm 自动重试 ──────────────────────────
def test_llm_network_error_retries(monkeypatch):
    """429 后重试成功（3 次上限内）。"""
    from protocol import worker as W
    real = W.urllib_request_urlopen if hasattr(W, "urllib_request_urlopen") else None
    calls = {"n": 0}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"choices":[{"message":{"content":"retry-ok"}}]}'

    def fake_urlopen(req, timeout=120):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)
        return FakeResp()

    # 直接 patch urllib.request.urlopen（call_llm 内部用）
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(W, "SYSTEM_PROMPT", "x")
    out = W.call_llm("ping")
    assert out == "retry-ok"
    assert calls["n"] == 2               # 1 次失败 + 1 次成功


# ── 7. quota 耗尽 → failed + retryable=True ──────────────────
def test_quota_exhaustion_retryable(tmp_path, monkeypatch):
    """LLM 持续 429 → 最终 failed，标记可重试（调度方可重放）。"""
    from protocol import worker as W
    q = _mkq(tmp_path)
    t = Task(goal="g")
    q.submit(t)

    import urllib.request
    def always_429(req, timeout=120):
        raise urllib.error.HTTPError(req.full_url, 429, "Quota", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", always_429)
    monkeypatch.setattr(W, "SYSTEM_PROMPT", "x")

    process_one(t, q)
    assert t.status == "failed"
    assert t.result_meta.get("retryable") is True    # 429 = 可重试


# ── 8. duplicate task_id → 幂等 ──────────────────────────────
def test_duplicate_task_id_idempotent(tmp_path):
    """同一 task_id 提交两次 → 队列只有一个任务。"""
    q = _mkq(tmp_path)
    t1 = Task(goal="g", task_id="dup1")
    t2 = Task(goal="g2", task_id="dup1")
    q.submit(t1)
    q.submit(t2)                       # 覆盖写（原子替换），不产生两个
    tasks = q.scan()
    assert len(tasks) == 1
    assert tasks[0].goal == "g2"       # 后提交的生效


# ── 9. 并发提交 10 任务 → 全部被处理 ─────────────────────────
def test_concurrent_batch(tmp_path, monkeypatch):
    """10 个任务 2 个 worker 并发消费 → 全部 done 且不重不漏。"""
    from protocol import worker as W
    q = _mkq(tmp_path)
    for i in range(10):
        q.submit(Task(goal=f"g{i}"))

    calls = {"n": 0, "lock": threading.Lock()}
    def fake(prompt, **kw):
        with calls["lock"]:
            calls["n"] += 1
        time.sleep(0.05)
        return "[状态] done\n[结果] ok"

    monkeypatch.setattr(W, "call_llm", fake)
    done_ids = []

    def run():
        while True:
            tasks = q.scan()
            if not tasks:
                return
            for task in tasks:
                process_one(task, q)
                if task.status == "done":
                    done_ids.append(task.task_id)

    ths = [threading.Thread(target=run) for _ in range(2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()

    assert calls["n"] == 10, f"LLM 调用 {calls['n']} 次（应恰好 10 次）"
    assert len(done_ids) == 10 and len(set(done_ids)) == 10   # 不重不漏

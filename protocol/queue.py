"""文件队列：agent 间协作通讯通道（零依赖，任何进程/语言可互操作）。

目录布局：
    <queue_dir>/
        queue/      # pending 任务 JSON（{task_id}.json）
        results/    # 完成结果 JSON（{task_id}.json）

协议：
    提交方  submit(task)            -> queue/<id>.json (status=pending)
    worker  扫描 queue/ -> 置 processing -> 执行 -> 写 results/<id>.json -> 删 queue 文件
    提交方  wait_result(task_id)    -> 轮询 results/，读到即返回
    三态结果：done(附句柄) / failed(附原因) / need_confirm(附选项)
"""
import json
import os
import time
from typing import Optional

from .task import Task


class FileQueue:
    PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}

    def __init__(self, base_dir: str):
        self.queue_dir = os.path.join(base_dir, "queue")
        self.results_dir = os.path.join(base_dir, "results")
        self.cancel_dir = os.path.join(base_dir, "cancel")
        os.makedirs(self.queue_dir, exist_ok=True)
        os.makedirs(self.results_dir, exist_ok=True)
        os.makedirs(self.cancel_dir, exist_ok=True)

    def _atomic_write(self, path: str, data: dict):
        """原子写：临时文件 + os.replace；Windows 上目标文件可能被短暂锁定
        （杀毒/索引），replace 失败小退避重试。"""
        tmp_path = path + ".tmp"
        for attempt in range(3):
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1)

    # ── 提交方 ─────────────────────────────────────────────
    def submit(self, task: Task) -> str:
        """入队并返回 task_id。"""
        self._atomic_write(os.path.join(self.queue_dir, f"{task.task_id}.json"),
                           task.to_dict())
        return task.task_id

    def cancel(self, task_id: str) -> None:
        """请求取消：写 cancel/<id>.marker（worker 执行前检查，幂等）。"""
        marker = os.path.join(self.cancel_dir, f"{task_id}.marker")
        open(marker, "w").close()

    def is_cancelled(self, task_id: str) -> bool:
        return os.path.exists(os.path.join(self.cancel_dir, f"{task_id}.marker"))

    def wait_result(self, task_id: str, timeout: float = 300.0,
                    poll_interval: float = 1.0,
                    progress_callback=None) -> Optional[Task]:
        """轮询等待结果（指数退避 1s→5s）。超时返回 status=timeout 的 Task。

        progress_callback: 可选 `callable(progress:int, note:str)`，轮询时读取
        任务文件的进度心跳（长任务中间态）。
        """
        result_path = os.path.join(self.results_dir, f"{task_id}.json")
        queue_path = os.path.join(self.queue_dir, f"{task_id}.json")
        deadline = time.time() + timeout
        interval = poll_interval
        while time.time() < deadline:
            if os.path.exists(result_path):
                with open(result_path, encoding="utf-8") as f:
                    t = Task.from_dict(json.load(f))
                os.unlink(result_path)
                try:
                    os.unlink(queue_path)
                except OSError:
                    pass  # worker 可能已删
                return t
            if progress_callback is not None and os.path.exists(queue_path):
                try:
                    with open(queue_path, encoding="utf-8") as f:
                        cur = json.load(f)
                    progress_callback(cur.get("progress", 0), cur.get("progress_note", ""))
                except (json.JSONDecodeError, OSError):
                    pass
            time.sleep(interval)
            interval = min(interval * 1.2, 5.0)
        # 超时：清理队列文件（任务可能已被 worker 取走，幂等处理）
        try:
            os.unlink(queue_path)
        except OSError:
            pass
        return Task(task_id=task_id, goal="", status="timeout",
                    result=f"No worker responded within {timeout}s.")

    # ── worker 侧 ──────────────────────────────────────────
    def scan(self) -> list[Task]:
        """扫描 pending 任务，按优先级排序（high > normal > low），跳过已取消。"""
        tasks = []
        for fn in sorted(os.listdir(self.queue_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.queue_dir, fn)
            task_id = fn[:-5]
            if self.is_cancelled(task_id):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    t = Task.from_dict(json.load(f))
                if t.status == "pending":
                    tasks.append(t)
            except (json.JSONDecodeError, OSError):
                continue
        tasks.sort(key=lambda t: self.PRIORITY_ORDER.get(t.priority, 1))
        return tasks

    def report_progress(self, task: Task, progress: int, note: str = ""):
        """心跳：更新任务文件的进度字段（worker 长任务中间态回报）。"""
        task.progress = max(0, min(100, progress))
        task.progress_note = note
        self._write_queue(task)

    def _iter_tasks(self):
        """遍历队列目录的任务文件，产出 (task_id, Task, path)。"""
        for fn in sorted(os.listdir(self.queue_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.queue_dir, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    yield fn[:-5], Task.from_dict(json.load(f)), path
            except (json.JSONDecodeError, OSError):
                continue

    def recover_stale(self, stale_after: float = 300.0) -> list:
        """崩溃恢复：把卡死的 processing 任务重置为 pending（重放）。

        判据：任务文件 mtime（= 最后一次心跳/写入时间）超过 stale_after 秒
        且状态为 processing → 视为 worker 已崩溃，重置 pending 让其他 worker 接手。
        worker 侧有心跳线程定期更新 mtime，长任务不会被误判。
        注意：不重置 progress 字段（业务层自行决定是否断点续算）。
        """
        recovered = []
        now = time.time()
        for task_id, t, path in self._iter_tasks():
            if t.status == "processing" and (now - os.path.getmtime(path)) > stale_after:
                t.status = "pending"
                self._write_queue(t)
                recovered.append(task_id)
        return recovered

    def health(self, stale_after: float = 300.0) -> dict:
        """队列健康报告：pending/processing 计数 + 卡死任务清单。"""
        pending = processing = 0
        stale = []
        now = time.time()
        for task_id, t, path in self._iter_tasks():
            if t.status == "processing":
                processing += 1
                if (now - os.path.getmtime(path)) > stale_after:
                    stale.append(task_id)
            elif t.status == "pending":
                pending += 1
        return {"pending": pending, "processing": processing,
                "stale_processing": stale}

    def mark_processing(self, task: Task):
        task.status = "processing"
        self._write_queue(task)

    def complete(self, task: Task):
        """写结果文件并删队列文件（原子写，防并发读半截）。

        同时清理该任务的取消标记（避免 cancel 目录无限增长）。
        """
        self._atomic_write(os.path.join(self.results_dir, f"{task.task_id}.json"),
                           task.to_dict())
        try:
            os.unlink(os.path.join(self.queue_dir, f"{task.task_id}.json"))
        except OSError:
            pass
        try:
            os.unlink(os.path.join(self.cancel_dir, f"{task.task_id}.marker"))
        except OSError:
            pass

    def _write_queue(self, task: Task):
        self._atomic_write(os.path.join(self.queue_dir, f"{task.task_id}.json"),
                           task.to_dict())

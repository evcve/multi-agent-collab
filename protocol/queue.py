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

    # ── 提交方 ─────────────────────────────────────────────
    def submit(self, task: Task) -> str:
        """入队并返回 task_id。"""
        path = os.path.join(self.queue_dir, f"{task.task_id}.json")
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(task.to_dict(), f, ensure_ascii=False)
        os.replace(tmp_path, path)
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

    def mark_processing(self, task: Task):
        task.status = "processing"
        self._write_queue(task)

    def complete(self, task: Task):
        """写结果文件并删队列文件（原子写：临时文件 + os.replace，防并发读半截）。"""
        result_path = os.path.join(self.results_dir, f"{task.task_id}.json")
        tmp_path = result_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(task.to_dict(), f, ensure_ascii=False)
        os.replace(tmp_path, result_path)   # 原子替换
        try:
            os.unlink(os.path.join(self.queue_dir, f"{task.task_id}.json"))
        except OSError:
            pass

    def _write_queue(self, task: Task):
        path = os.path.join(self.queue_dir, f"{task.task_id}.json")
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(task.to_dict(), f, ensure_ascii=False)
        os.replace(tmp_path, path)

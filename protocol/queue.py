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
import re
import time
from typing import Optional

from .task import Task


class FileQueue:
    PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}

    def __init__(self, base_dir: str):
        self.queue_dir = os.path.join(base_dir, "queue")
        self.processing_dir = os.path.join(base_dir, "processing")
        self.results_dir = os.path.join(base_dir, "results")
        self.cancel_dir = os.path.join(base_dir, "cancel")
        os.makedirs(self.queue_dir, exist_ok=True)
        os.makedirs(self.processing_dir, exist_ok=True)
        os.makedirs(self.results_dir, exist_ok=True)
        os.makedirs(self.cancel_dir, exist_ok=True)

    def _atomic_write(self, path: str, data: dict):
        """原子写：临时文件 + os.replace；Windows 上目标文件可能被短暂锁定
        （杀毒/索引），replace 失败小退避重试。"""
        tmp_path = path + ".tmp"
        for attempt in range(5):
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.3)

    # ── 提交方 ─────────────────────────────────────────────
    def submit(self, task: Task) -> str:
        """入队并返回 task_id；同名任务已在 processing/ 时拒绝（防双实例）。"""
        if not self._valid_task_id(task.task_id):
            raise ValueError(f"非法 task_id: {task.task_id!r}（仅允许字母数字-_，≤64）")
        if os.path.exists(os.path.join(self.processing_dir, f"{task.task_id}.json")):
            raise ValueError(f"task_id {task.task_id} 正在处理中，拒绝重复提交")
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
    _TASK_ID_RE = None

    @classmethod
    def _valid_task_id(cls, task_id: str) -> bool:
        """task_id 路径安全校验（防路径穿越：../ 等注入）。"""
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id or ""))

    def claim(self, task_id: str) -> bool:
        """原子认领：rename 任务文件 queue/ → processing/（原子互斥）。

        并发下只有一个 worker 能成功 rename（其他得到 OSError）——
        彻底消除"处理完成释放锁后另一 worker 又认领"的竞态。
        认领后 touch mtime：刚认领的任务不能被 recover_stale 误判卡死。
        """
        if not self._valid_task_id(task_id):
            return False
        src = os.path.join(self.queue_dir, f"{task_id}.json")
        dst = os.path.join(self.processing_dir, f"{task_id}.json")
        try:
            now = time.time()
            os.utime(src, (now, now))   # 先刷新源 mtime（rename 保留 mtime）
            os.rename(src, dst)         # 再原子移动——刚认领的不会被误判 stale
            return True
        except OSError:
            return False

    def release(self, task_id: str):
        """处理完成：删除 processing 文件（结果已写 results/）。"""
        if not self._valid_task_id(task_id):
            return
        try:
            os.unlink(os.path.join(self.processing_dir, f"{task_id}.json"))
        except OSError as e:
            print(f"[queue] WARNING: release {task_id} 失败: {e}")

    def scan(self) -> list[Task]:
        """扫描待办任务（queue/ 目录存在即 pending——目录即状态，不读 status 字段）。

        queue/ = 待办、processing/ = 处理中、results/ = 完成。
        """
        tasks = []
        for fn in sorted(os.listdir(self.queue_dir)):
            if not fn.endswith(".json"):
                continue
            task_id = fn[:-5]
            if not self._valid_task_id(task_id) or self.is_cancelled(task_id):
                continue
            path = os.path.join(self.queue_dir, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    t = Task.from_dict(json.load(f))
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
        """遍历队列目录的任务文件，产出 (task_id, Task, path)。

        注意：必须在 yield 前关闭文件（with 块内完成读取）——Windows 上
        replace 打开中的文件会 PermissionError；生成器挂起时句柄不能残留。
        """
        for fn in sorted(os.listdir(self.queue_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.queue_dir, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    task = Task.from_dict(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
            yield fn[:-5], task, path

    def recover_stale(self, stale_after: float = 300.0) -> list:
        """崩溃恢复：卡死任务原子 rename 回 queue/（真 rename，无读写窗口）。

        判据：processing/ 下文件 mtime（= 最后一次心跳/写入时间）超过 stale_after
        秒 → 视为 worker 崩溃。rename 是原子的——不存在"读了再写"的竞态窗口。
        损坏文件移到 .corrupt/ 不中断恢复；status 字段是运行时信息，
        目录位置才是权威状态（scan 不依赖 status）。
        """
        recovered = []
        now = time.time()
        corrupt_dir = os.path.join(os.path.dirname(self.queue_dir), "corrupt")
        for fn in sorted(os.listdir(self.processing_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.processing_dir, fn)
            task_id = fn[:-5]
            if (now - os.path.getmtime(path)) <= stale_after:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    Task.from_dict(json.load(f))     # 验证可解析（损坏→corrupt）
            except Exception as e:
                os.makedirs(corrupt_dir, exist_ok=True)
                try:
                    os.rename(path, os.path.join(corrupt_dir, fn))
                except OSError:
                    pass
                print(f"[queue] recover_stale: {fn} 损坏，移到 corrupt/ ({e})")
                continue
            try:
                os.rename(path, os.path.join(self.queue_dir, fn))   # 原子放回
                recovered.append(task_id)
            except OSError:
                continue   # 窗口内被 complete 删了（结果已在 results/）→ 安全跳过
        return recovered

    def health(self, stale_after: float = 300.0) -> dict:
        """队列健康报告：pending/processing 计数 + 卡死任务清单。"""
        pending = len([f for f in os.listdir(self.queue_dir) if f.endswith(".json")])
        processing = 0
        stale = []
        now = time.time()
        for fn in sorted(os.listdir(self.processing_dir)):
            if not fn.endswith(".json"):
                continue
            processing += 1
            path = os.path.join(self.processing_dir, fn)
            if (now - os.path.getmtime(path)) > stale_after:
                stale.append(fn[:-5])
        return {"pending": pending, "processing": processing,
                "stale_processing": stale}

    def gc(self, ttl_days: float = 7.0) -> int:
        """TTL 清理：删除 results/ 下超过 ttl_days 未动的结果文件（防堆积）。

        返回清理数量。语义：结果文件是"已消费即删"的，残留说明提交方没读——
        gc 清的是这些孤儿结果，不含 queue/（pending 任务不能删、processing 由
        recover_stale 管）。注意与 wait_result 的竞态窗口极小（正在读的结果
        mtime 通常是新的），如遇误删可将 ttl_days 调大。
        """
        removed = 0
        cutoff = time.time() - ttl_days * 86400
        for fn in os.listdir(self.results_dir):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.results_dir, fn)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError:
                continue
        return removed

    def mark_processing(self, task: Task):
        task.status = "processing"
        self._write_task_file(task)

    def complete(self, task: Task):
        """写结果文件并删队列文件（原子写，防并发读半截）。

        同时清理该任务的取消标记（避免 cancel 目录无限增长）。
        """
        self._atomic_write(os.path.join(self.results_dir, f"{task.task_id}.json"),
                           task.to_dict())
        self.release(task.task_id)   # 删 processing 文件（结果已落盘）
        try:
            os.unlink(os.path.join(self.cancel_dir, f"{task.task_id}.marker"))
        except OSError:
            pass

    def report_progress(self, task: Task, progress: int, note: str = ""):
        """心跳：更新任务文件的进度字段（worker 长任务中间态回报）。"""
        task.progress = max(0, min(100, progress))
        task.progress_note = note or task.progress_note
        self._write_task_file(task)

    def _write_task_file(self, task: Task):
        """写任务文件到其当前所在目录（processing/ 优先，否则 queue/）。

        claim 后任务在 processing/，写错目录会留幽灵文件。
        文件都不存在 = 任务已完成（结果已落盘）→ 不复活（防 completed 任务复活）。
        """
        for d in (self.processing_dir, self.queue_dir):
            p = os.path.join(d, f"{task.task_id}.json")
            if os.path.exists(p):
                self._atomic_write(p, task.to_dict())
                return

    def _write_queue(self, task: Task):
        self._atomic_write(os.path.join(self.queue_dir, f"{task.task_id}.json"),
                           task.to_dict())

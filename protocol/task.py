"""Task 结构：任务四要素 + 三态反馈（协作协议的核心数据结构）。

四要素：goal（目标）/ context（上下文，内嵌数据）/ constraints（约束）/ acceptance（验收标准）
三态反馈：done（附可验证句柄）/ failed（附原因）/ need_confirm（附选项）
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import time
import uuid


@dataclass
class Task:
    goal: str                      # 目标：一句话说清要什么
    context: str = ""              # 上下文：关键数据内嵌（勿引用外部文件路径）
    constraints: str = ""          # 约束：边界/禁止事项/输出格式
    acceptance: str = ""           # 验收标准：可检查的交付物
    # 运行时字段
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    target_agent: str = "worker"
    timeout: int = 120
    status: str = "pending"        # pending / processing / done / failed / need_confirm / timeout
    created_at: float = field(default_factory=time.time)
    result: Optional[Any] = None   # done 时：可验证句柄（路径/数值）；failed：原因；need_confirm：选项
    result_meta: dict = field(default_factory=dict)

    @property
    def prompt(self) -> str:
        """把四要素渲染成发送给执行 LLM 的提示词（防幻觉：数据全内嵌）。"""
        parts = [f"【目标】{self.goal}"]
        if self.context:
            parts.append(f"【上下文】{self.context}")
        if self.constraints:
            parts.append(f"【约束】{self.constraints}")
        if self.acceptance:
            parts.append(f"【验收标准】{self.acceptance}")
        parts.append("【反馈格式】只输出三态之一：\n"
                     "[状态] done / failed / need_confirm\n"
                     "[结果] <内容>。不确定就说不知道，绝不编造。")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

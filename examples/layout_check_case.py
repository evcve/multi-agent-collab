#!/usr/bin/env python3
"""真实用例：卫星单机安装孔 × 热管条带 干涉检查（走 protocol 全流程）。

演示协议完整闭环：
  结构化输入 (context_fields) → worker 执行 → 结果 Schema 校验 → 三态反馈
数据为合成示例（不代表任何真实项目）。

运行：
    python examples/layout_check_case.py --fake    # 免 API key（内置计算引擎）
    python examples/layout_check_case.py           # 真实 LLM（OpenAI 兼容端点）
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.queue import FileQueue
from protocol.task import Task
from protocol.worker import process_one

# ── 合成数据：热管条带（40mm 宽）+ 安装孔 ──────────────────────────
BANDS = {
    "X向热管H1": [336.2, 376.2], "X向热管H2": [175.6, 215.6],
    "X向热管H3": [-210.4, -170.4], "X向热管H4": [-342.3, -302.3],
    "Y向热管V1": [-603.4, -563.4], "Y向热管V2": [-198.4, -158.4],
    "Y向热管V3": [195.2, 235.2], "Y向热管V4": [558.1, 598.1],
}
HOLES = [
    ("EQ-A", 230.9, 194.8), ("EQ-A", 248.8, 194.8),   # 在 H2 内 → 冲突
    ("EQ-B", 222.0, 390.0),                            # 在 V3 内 → 冲突
    ("EQ-C", -667.5, -309.0),                          # 在 H4 内 → 冲突
    ("EQ-D", 560.0, 72.5),                             # 在 V4 内 → 冲突
    ("EQ-E", 481.3, -50.7),                            # 无冲突
    ("EQ-F", 0.2, 6.5),                                # 无冲突
]

RESULT_SCHEMA = {
    "type": "object",
    "required": ["conflicts", "total_holes"],
    "properties": {
        "conflicts": {
            "type": "array",
            "items": {"type": "object",
                      "required": ["equipment", "hole", "band", "x", "y"],
                      "properties": {
                          "equipment": {"type": "string"},
                          "hole": {"type": "integer"},
                          "band": {"type": "string"},
                          "x": {"type": "number"},
                          "y": {"type": "number"}}}},
        "total_holes": {"type": "integer"},
    },
}


def build_task() -> Task:
    """四要素 + 结构化字段 + 结果 Schema（协议标准姿势）。"""
    fields = {
        "热管条带(40mm宽, [下界,上界])": json.dumps(BANDS, ensure_ascii=False),
        "安装孔坐标": json.dumps(HOLES, ensure_ascii=False),
        "判定规则": "孔坐标落在任一条带范围内即冲突；X向条带看Y坐标，Y向条带看X坐标",
    }
    return Task(
        goal="检查所有安装孔是否与热管条带干涉，输出冲突清单 JSON",
        context="板 1400×1000mm，原点中心。",
        context_fields=fields,
        constraints="每条冲突记录包含 equipment/hole/band/x/y 字段；无冲突时 conflicts 为空数组",
        acceptance="conflicts 中每条记录字段完整；total_holes = 输入孔数",
        result_schema=RESULT_SCHEMA,
        timeout=120,
    )


def fake_engine(fields: dict) -> str:
    """内置计算引擎（--fake 用，纯几何计算，不调 LLM）：返回三态格式 JSON。"""
    bands = json.loads(fields["热管条带(40mm宽, [下界,上界])"])
    holes = json.loads(fields["安装孔坐标"])
    conflicts = []
    for idx, (eq, x, y) in enumerate(holes):
        hits = []
        for name, (lo, hi) in bands.items():
            if name.startswith("X") and lo <= y <= hi:
                hits.append(name)
            elif name.startswith("Y") and lo <= x <= hi:
                hits.append(name)
        for band in hits:   # 一个孔可撞多条带——全部报告，不漏检
            conflicts.append({"equipment": eq, "hole": idx, "band": band, "x": x, "y": y})
    return ("[状态] done\n[结果] " + json.dumps(
        {"conflicts": conflicts, "total_holes": len(holes)}, ensure_ascii=False) +
        "\n[备注] 内置几何引擎计算，非 LLM 推理")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="免 API key（内置计算引擎）")
    args = ap.parse_args()

    q = FileQueue(os.path.expanduser("~/.protocol-case/runtime"))  # 私有目录，避免 /tmp 公共权限问题
    task = build_task()
    q.submit(task)
    print(f"[brain] 提交任务 {task.task_id}: {task.goal}")
    print(f"[brain] prompt 含结构化数据块: {'【数据】' in task.prompt}\n")

    if args.fake:
        import protocol.worker as W
        raw = fake_engine(task.context_fields)
        status, result, note = W.parse_three_state(raw)
        task.status, task.result, note = status, result, note
        task.result_meta = {"note": note}
        # 走一次 schema 校验（演示校验环节）
        errs = W.validate_schema(json.loads(result), task.result_schema)
        task.result_meta["validation"] = "ok" if not errs else errs
        q.complete(task)
        print(f"[worker] (fake 引擎) 完成: [{status}]")
    else:
        process_one(task, q)

    done = q.wait_result(task.task_id, timeout=60)
    print(f"\n[brain] 收到: [{done.status}]")
    if done.status == "done":
        data = json.loads(done.result)
        print(f"[brain] Schema 校验: {done.result_meta.get('validation')}")
        print(f"[brain] 总孔数: {data['total_holes']}, 冲突: {len(data['conflicts'])}")
        for c in data["conflicts"]:
            print(f"        ⚠️  {c['equipment']} @ ({c['x']}, {c['y']}) 撞 {c['band']}")
        if done.result_meta.get("note"):
            print(f"[brain] 备注: {done.result_meta['note']}")
    else:
        print(f"[brain] 失败原因: {done.result[:200]}")


if __name__ == "__main__":
    main()

# Roadmap — V0.2 痛点驱动的特性计划

> 每个特性都来自**实战痛点**（brain 侧 + 执行层 servant 的原话吐槽），
> 不是拍脑袋的功能清单。实现顺序按 P 优先级。

## 痛点来源（2026-08-14 收集）

### 执行层（servant）吐槽原文摘录

| # | 吐槽 | 翻译成需求 |
|---|---|---|
| S1 | "数据全塞文本里，解析像在垃圾堆找吃的，格式稍微不对我就瞎" | 结构化数据字段（key-value 块），不要全拼在自然语言段落里 |
| S2 | "只能回个死板的状态词，想解释报错原因或给建议都憋死，像个只会点头的哑巴" | 三态之外的**补充说明通道**（`[备注]` 自由文本，不参与状态判断） |
| S3 | "手被绑死！只能写代码让你们跑，自己不能执行，出错还得等下一轮" | **白名单工具扩展**（只读查询/计算类，默认关闭，显式授权） |
| S4 | "上下文一长就断片，前脚说的后脚忘" | 长任务分块 + 关键摘要回传（每轮结束返回 1 行状态摘要） |
| S5 | "指令模棱两可，猜错了就是我的锅" | 验收标准必须可自动核验（数值/格式），不可核验的标注 `[需人工]` |

### 大脑（brain）侧痛点

| # | 痛点 | 翻译成需求 |
|---|---|---|
| B1 | 紧急任务在 FIFO 队列里排队，插不了队 | `priority: high/normal/low` 字段 + worker 按优先级取任务 |
| B2 | 大任务 pending→done 中间无任何状态，不知道卡住还是在进行 | `progress` 字段 + 可选事件文件（心跳），提交方可轮询 |
| B3 | 任务发错/不需要了，撤不回 | 取消协议：提交方写 `cancel/<id>` 标记，worker 检查后放弃 |
| B4 | failed 和 timeout 不区分，重试策略没法写 | 结果加 `retryable: bool`（timeout=可重试，failed=需人工/改指令） |
| B5 | 大上下文任务，数据内嵌 token 成本高 | 可选 `context_ref`（共享存储引用 + 内嵌摘要），由**提交方**负责展开 |
| B6 | 自由文本结果没法自动校验 | `acceptance` 支持可选 JSON Schema，结果自动校验并回传验证报告 |
| B7 | runtime 队列目录长期堆积 | 结果文件 TTL 清理（默认 7 天）+ 归档策略 |
| B8 | worker 纯文本，复杂任务利用率低 | 与 S3 合并：白名单工具 + 只读 shell 查询 |

## V0.2 特性清单

| 优先级 | 特性 | 解决痛点 | 方案要点 |
|---|---|---|---|
| P0 | `[备注]` 补充通道 | S2 | parse_three_state 忽略备注行，原样存入 result_meta.note |
| P0 | 验收标准自动核验（JSON Schema） | S5/B6 | acceptance 支持 `schema` 子字段；worker 侧内置轻量校验器 |
| P1 | 任务优先级 | B1 | Task.priority；FileQueue.scan 按 priority 排序；worker 抢占式消费 |
| P1 | 心跳/进度 | B2 | Task.progress(int) + progress_note；提交方 wait_result 轮询时顺带读进度 |
| P1 | 取消协议 | B3 | queue/cancel/<id>.marker；worker 每任务前检查 |
| P1 | 重试语义 | B4 | 结果 meta 加 retryable；demo 展示 timeout→自动重试 |
| P2 | 结构化数据字段 | S1 | context 支持 `字段名: 值` 块渲染（YAML-lite），非纯散文 |
| P2 | 白名单工具 | S3/B8 | worker 可配置 `ALLOWED_TOOLS`（如只读文件 glob/数学），默认空=纯文本 |
| P2 | 上下文分块+摘要回传 | S4 | 长任务协议：subtask 列表，每完成一块回传 1 行摘要 |
| P2 | 队列 TTL 清理 | B7 | FileQueue 提供 gc(ttl_days)；worker 启动时可选执行 |

## 不做（明确 out of scope）

- 通用 Agent 框架（CrewAI/AutoGen 已做，不重复造轮子）
- 复杂状态机/持久化编排（文件队列保持最小，高并发交给真实 broker）
- 自动执行任意 shell（安全边界：默认零工具）

---
*维护方式：每个实战踩坑 → 加一条 → 实现后移到 CHANGELOG。*

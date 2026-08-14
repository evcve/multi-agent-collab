# protocol/ — 协作通讯主程序（自研，可运行）

**多智能体协作通讯的最小可运行实现**：文件队列协议 + worker + 三态反馈。
纯 Python 标准库，零依赖，任何 OpenAI 兼容 LLM 端点可用。

> ⚠️ 这是独立原创实现，不依赖也不包含任何第三方 bridge 代码——协议通用，
> 可与任意 agent 对接（OpenClaw / CrewAI / 自研 agent 均可通过队列文件互操作）。

## 架构

```
┌───────────┐  submit()  ┌────────────────────┐  scan()   ┌──────────┐
│   BRAIN   │ ─────────> │  queue/<id>.json    │ ────────> │  WORKER  │
│ (提交方)   │            │  (pending任务)      │           │ (执行LLM) │
└───────────┘ <───────── │  results/<id>.json  │ <──────── └──────────┘
   wait_result()         └────────────────────┘  complete()
```

- **提交方**（brain）：`Task(四要素)` → `queue.submit()` → `queue.wait_result()`
- **执行方**（worker）：`queue.scan()` → 调 LLM → `parse_three_state()` → `queue.complete()`
- 三态结果：`done`（附可验证句柄）/ `failed`（附原因）/ `need_confirm`（附选项）

## 快速开始

```bash
# 1. 无 key 演示（模拟 worker，10 秒看完协议怎么跑）
python -m protocol.demo --fake

# 2. 真实 LLM（OpenAI 兼容端点）
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_API_KEY=sk-xxx
export LLM_MODEL=deepseek-chat
python -m protocol.demo

# 3. 常驻 worker（独立进程）
python -m protocol.worker --queue-dir ./runtime
```

## 为什么值得看（设计要点）

1. **四要素进 prompt**（`Task.prompt`）：目标/上下文/约束/验收标准渲染成提示词，
   关键数据**内嵌**——执行 LLM 无文件能力时，"读文件"指令必然被幻觉填补，
   数据内嵌是唯一可靠传递方式（实战教训）。
2. **三态强制解析**（`parse_three_state`）：LLM 输出必须是
   `[状态] done/failed/need_confirm + [结果]`，防止自由文本汇报掩盖失败。
3. **诚实失败**：LLM 调用异常 → `failed` + 原因，绝不假装成功。
4. **文件队列可互操作**：JSON 文件即协议，任何语言/进程都能读写，
   不需要共享内存或特定框架。
5. **超时幂等**：提交方超时会删自己的队列文件，worker 侧幂等处理。

## 文件

| 文件 | 职责 |
|---|---|
| `task.py` | 任务四要素结构 + 三态反馈解析 |
| `queue.py` | 文件队列（submit / scan / complete / wait_result） |
| `worker.py` | worker 循环（LLM 调用 + 三态解析），`--once` 单次模式 |
| `demo.py` | 端到端演示（`--fake` 免 key） |

## 对接真实系统

- **OpenClaw**：把 `queue.submit()` 换成其 bridge 的入队 API 即可（协议一致）
- **任何 HTTP agent**：任务 JSON 就是协议，POST/文件/消息队列都行
- 生产建议：队列目录放共享盘/对象存储，worker 多实例消费（天然并行）

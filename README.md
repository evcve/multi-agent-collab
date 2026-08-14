# multi-agent-collab

**Multi-agent collaboration playbook & toolkit** — 一套经过实战验证的多智能体协作模式与配套工程工具。

> 来源：一个真实项目（卫星总体设计 + 多 AI 协作者）中，Hermes（大脑）与 OpenClaw 主代理（手脚）通过 MCP bridge 长期协作沉淀的**实战经验记录**。已去除所有内部数据，保留通用模式与可复用工具。
>
> 免责声明：本仓库为独立维护的个人项目，与 OpenClaw、CrewAI 等任何第三方产品或项目无隶属关系。文档中提及的产品名称仅用于事实性描述。

## 核心思想：三层分工

```
┌─────────────────────────────────────────────┐
│  BRAIN  (Hermes / 主控 agent)                │
│  意图拆解 · 记忆/知识管理 · 任务派发          │
│  结果核验 · 人类接口                         │
├─────────────────────────────────────────────┤
│  HANDS  (Orchestrator agent, e.g. OpenClaw)  │
│  多步推理 · 工具编排 · 复杂任务执行           │
├─────────────────────────────────────────────┤
│  WORKERS (子模型 / servant)                  │
│  单轮生成 · 简单验证 · 格式化输出             │
└─────────────────────────────────────────────┘
        通道: MCP bridge / 队列 / 消息群
```

## 仓库内容

| 路径 | 内容 |
|---|---|
| [`COLLABORATION.md`](COLLABORATION.md) | 协作模式协议：任务四要素、三态反馈、心跳/中断、成本分级、纪律 |
| [`tools/`](tools/) | 工程工具：DXF 几何解析、FEM 安装孔提取、干涉检测（通用，含示例数据） |
| `examples/` | 示例数据（非真实项目数据） |

## 快速开始

```bash
# 工具示例：从 DXF 提取横竖条带（热管/槽道等 40mm 宽带）
python tools/dxf_rect_extract.py examples/sample_bands.dxf

# FEM RBE2 安装孔提取（质量点 rigid 连接的板节点 = 安装孔位置）
python tools/fem_rbe2_holes.py examples/sample_model.fem

# 干涉检测：孔 vs 条带
python tools/interference_check.py --holes holes.csv --bands bands.csv
```

## 为什么值得看

- **原创的协作协议**：任务四要素（目标/上下文内嵌/约束/验收）解决了"子代理编造文件"的经典问题（执行层无文件能力时，指令"读文件"必然被幻觉填补 → 关键数据必须内嵌任务文本）
- **结构化反馈**：done/failed/need_confirm 三态 + 可验证路径，取代自然语言汇报
- **成本治理**：免费模型层兜底简单任务，主力模型执行，视觉模型专项——配额耗尽时自动降级
- **实战教训固化**：文件操作归脚本（LLM 只做推理）、数值必须回源核验、长任务分块防上下文溢出

## License

MIT

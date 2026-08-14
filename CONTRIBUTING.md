# 贡献指南：三方评审工作流

> **规则：任何上传到 main 的变更，必须先过三方评审。** 单方自评会漏掉"我们都习以为常"的问题——评审要独立。

## 评审三方（三个不同模型家族，保证视角独立）

| 方 | 模型家族 | 视角 | 工具 |
|---|---|---|---|
| ① 作者自评（Hermes） | deepseek | 变更意图、实现完整性 | 变更摘要 + 自查清单 |
| ② 编排层（智谱 glm-4.7） | 智谱（与 deepseek 不同源） | 架构一致性、执行层可行性 | `scripts/review.py` + `REVIEW_BASE_URL=https://open.bigmodel.cn/api/paas/v4` `REVIEW_MODEL=glm-4.7` |
| ③ 独立第三方（Kimi） | Moonshot（不同源） | 挑刺、找盲区、安全/兼容性 | `scripts/review.py`（默认配置） |

> **为什么不直接用启明？** 启明主模型也是 deepseek——与作者同源，评审视角不够独立。
> 编排层评审改走智谱 glm-4.7（不同模型家族）。
> **Fallback**：若智谱直连不可用（key 失效/网络），可走启明渠道 `call_openclaw`，
> 明确要求"用子模型（glm-4.7）评审"。

## 流程（每次 commit 前）

```bash
# 1. 生成变更摘要 + diff
git diff origin/main..HEAD --stat
git diff origin/main..HEAD > /tmp/change.diff

# 2. 第三方独立评审（Kimi）
export KIMI_API_KEY=sk-xxx            # 或 REVIEW_API_KEY
python scripts/review.py --diff-file /tmp/change.diff --strict

# 3. 编排层评审（启明）——发 call_openclaw：变更摘要 + 请求架构/执行层视角评审

# 4. 作者自评对照三方意见
# 5. 分歧裁决：P0 问题必须解决；P1 争议由维护者定；结论"通过"才 push
```

## 评审输出模板

评审必须输出结构化结论，含严重度分级：

```
【优点】≤3 条
【问题】P0/P1/P2 + 具体位置
【风险】安全 / 法律 / 兼容性 / 舆论
【建议】可执行改进项
【结论】通过 / 需修改（理由）
```

- **P0**（必须修）：功能错误、凭据泄露、破坏兼容性、安全漏洞
- **P1**（建议修）：边界情况、文档与实现不一致、可维护性
- **P2**（可忽略）：风格、优化建议

## 自查清单（作者提交前先过一遍）

- [ ] 凭据/密钥/内部数据未进代码（`.env` 在 gitignore，diff 无 `sk-`/token/密码）
- [ ] 文档与实现一致（README/API.md 描述的接口真的存在）
- [ ] 测试通过（`python -m pytest tests/ -q`）
- [ ] commit message 规范：短标题 + Why/What 分层
- [ ] 协议字段变更评估兼容性（新增=向后兼容，改名/删字段=破坏）
- [ ] LLM 输出处理无注入风险（不 eval、不拼 shell）

## 逃生通道（谨慎使用）
- 测试门禁可绕过：`git push --no-verify`（仅在明确知道在做什么时用——绕过 = 责任自负）

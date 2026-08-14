#!/usr/bin/env python3
"""第三方独立评审工具（Kimi / 任意 OpenAI 兼容端点）。

用法：
    export KIMI_API_KEY=sk-...            # 或 REVIEW_BASE_URL/REVIEW_API_KEY/REVIEW_MODEL 覆盖
    python scripts/review.py --summary "变更摘要..."          # 文本评审
    python scripts/review.py --diff-file changes.diff        # 喂 diff
    python scripts/review.py --summary "..." --strict        # 严格模式（更狠的挑刺）

输出：结构化评审（✅ 优点 / ⚠️ 问题 / 🔴 风险 / 💡 建议 / 结论：通过 or 需修改）
"""
import argparse
import json
import os
import sys
import urllib.request

BASE_URL = os.environ.get("REVIEW_BASE_URL", "https://api.moonshot.cn/v1")
MODEL = os.environ.get("REVIEW_MODEL", "kimi-k2.7-code-highspeed")


def resolve_api_key(base_url: str) -> str:
    """按端点自动选择对应 provider 的 key（REVIEW_API_KEY 显式覆盖优先）。"""
    explicit = os.environ.get("REVIEW_API_KEY")
    if explicit:
        return explicit
    if "bigmodel" in base_url:          # 智谱
        return os.environ.get("ZHIPUAI_API_KEY", "")
    if "moonshot" in base_url:          # Kimi
        return os.environ.get("KIMI_API_KEY", "")
    return os.environ.get("OPENAI_API_KEY", "")

SYSTEM_PROMPT = (
    "你是一名严格的独立代码评审员（第三方视角，与项目作者无关）。"
    "对给定的项目变更进行批判性评审，重点找问题而不是夸："
    "功能正确性、边界情况、安全性（凭据/注入/权限）、可维护性、"
    "文档与代码一致性、协议兼容性、并发/原子性、回归风险。"
    "必须输出结构化格式：\n"
    "【优点】最多3条\n"
    "【问题】每条含严重度(P0/P1/P2)与具体位置\n"
    "【风险】法律/安全/舆论/兼容性风险\n"
    "【建议】可执行的改进项\n"
    "【结论】通过 / 需修改（附理由）。\n"
    "不要客套，不要复述变更内容，直接挑刺。"
)


def call_llm(prompt: str, timeout: int = 180) -> str:
    api_key = resolve_api_key(BASE_URL)
    if not api_key:
        print("错误: 未设置对应 provider 的 API key"
              "（智谱: ZHIPUAI_API_KEY / Kimi: KIMI_API_KEY / 或 REVIEW_API_KEY 覆盖）",
              file=sys.stderr)
        sys.exit(1)
    # temperature 按 provider：Kimi 推理模型只允许 1.0；智谱结构化输出用 0.3 更稳
    temperature = 1.0 if "moonshot" in BASE_URL else 0.3
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": int(os.environ.get("REVIEW_MAX_TOKENS", "8000")),  # 推理模型 thinking 会吃 token，大 diff 调大
    }).encode("utf-8")
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        # 不回退 reasoning_content（那是思维链，不是结构化输出）；诚实报错
        raise RuntimeError(
            "模型返回空 content（reasoning 吃光 max_tokens？）——请调大 REVIEW_MAX_TOKENS 或换模型")
    return content


def main():
    ap = argparse.ArgumentParser(description="Kimi 独立评审")
    ap.add_argument("--summary", help="变更摘要文本")
    ap.add_argument("--diff-file", help="diff 文件路径（自动截断到 30k 字符）")
    ap.add_argument("--strict", action="store_true", help="严格模式：附加挑刺指令")
    args = ap.parse_args()

    if not args.summary and not args.diff_file:
        ap.error("需要 --summary 或 --diff-file")

    parts = ["# 项目变更评审请求\n"]
    if args.summary:
        parts.append(f"## 变更摘要\n{args.summary}\n")
    if args.diff_file:
        with open(args.diff_file, encoding="utf-8", errors="replace") as f:
            diff = f.read()
        if len(diff) > 30000:
            diff = diff[:30000] + "\n...(截断)"
        parts.append(f"## 代码变更(diff)\n```diff\n{diff}\n```\n")
    if args.strict:
        parts.append("## 严格要求\n请额外关注：协议兼容性（字段变更是否破坏存量）、"
                     "安全边界（LLM 输出注入/凭据泄露）、以及\"文档与实现不一致\"问题。\n")
    parts.append("请按系统提示的格式输出评审。")

    print(f"评审中... (模型: {MODEL}, 端点: {BASE_URL})", file=sys.stderr)
    try:
        result = call_llm("\n".join(parts), timeout=300)
    except Exception as e:
        print(f"评审调用失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(result)


if __name__ == "__main__":
    main()

# multi-agent-collab

**A battle-tested multi-agent collaboration playbook + runnable protocol + engineering tools.**

> Source: a real production project (satellite bus design with multiple AI collaborators) where a
> brain agent (Hermes) and an orchestrator agent (OpenClaw) worked together over an MCP bridge.
> All internal data removed; only the general patterns and reusable tooling remain.
>
> Disclaimer: this is an independent personal project, not affiliated with OpenClaw, CrewAI or any
> third-party product. Product names are mentioned for factual description only.

## Core idea: three-tier division of labor

```
┌─────────────────────────────────────────────┐
│  BRAIN  (main controller agent)             │
│  intent decomposition · memory/knowledge    │
│  task dispatch · result verification · UI   │
├─────────────────────────────────────────────┤
│  HANDS  (orchestrator agent, e.g. OpenClaw) │
│  multi-step reasoning · tool orchestration  │
├─────────────────────────────────────────────┤
│  WORKERS (sub-models / servants)            │
│  single-turn generation · simple validation │
└─────────────────────────────────────────────┘
        channel: MCP bridge / file queue / messaging
```

## Repository layout

| Path | What |
|---|---|
| [`COLLABORATION.md`](COLLABORATION.md) | Collaboration protocol: 4-element task spec, 3-state feedback, heartbeat/interrupt, cost tiers, discipline |
| [`protocol/`](protocol/) | **Runnable communication program**: file queue + worker + 3-state feedback. Pure stdlib, works with any OpenAI-compatible LLM |
| [`tools/`](tools/) | Engineering tools: DXF band extraction, FEM RBE2 mount-hole extraction, interference checking (domain-agnostic, sample data included) |
| `examples/` | Synthetic sample data (no real project data) |

## Quick start

```bash
# 1. Run the collaboration protocol (no API key needed, simulated worker)
python -m protocol.demo --fake

# 2. With a real LLM (OpenAI-compatible endpoint)
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_API_KEY=sk-xxx
python -m protocol.demo

# 3. Persistent worker (separate process consuming the queue)
python -m protocol.worker --queue-dir ./runtime

# 4. Layout tools
python tools/dxf_rect_extract.py examples/sample_bands.dxf
python tools/fem_rbe2_holes.py examples/sample_model.fem --nodes 13,31,45
python tools/interference_check.py --holes examples/sample_holes.csv --bands examples/sample_bands.csv
```

## Why this is worth a look

- **A collaboration protocol born from real failures**, not a marketing playbook:
  - **Task 4 elements** (goal / context / constraints / acceptance) with **data embedded in the prompt** —
    when the executing LLM has no file access, "please read file X" is *guaranteed* to be
    hallucinated. Embedding data is the only reliable channel (field-tested).
  - **3-state structured feedback** (`done` with verifiable handle / `failed` with reason /
    `need_confirm` with options) replaces free-text reporting that hides failures.
  - **Anti-hallucination discipline**: uncertain → say so; never fabricate files, numbers, or results.
- **Runnable minimal kernel** (`protocol/`): file queue + worker in pure stdlib — zero framework
  weight, interoperable across languages/processes (JSON files are the protocol).
- **Cost governance**: free model tier for simple tasks, main model for execution, vision model for
  image work — with automatic downgrade when quotas run out (also field-tested).
- **Honest failure**: LLM call errors → `failed`, never a faked success; output that doesn't follow
  the 3-state format is rejected as `failed`, not silently treated as done.
- **Engineering tools with the footguns documented**: Nastran fixed-column parsing (adjacent numbers
  glued together), RBE2 continuation lines, tonne-vs-kg mass units.

## Tests & CI

```bash
pip install pytest
python -m pytest tests/ -v    # 19 tests: protocol (task/queue/3-state) + tools (DXF/FEM/interference)
```

GitHub Actions CI runs the suite on Python 3.9–3.12 (`.github/workflows/ci.yml`).

## License

MIT — see [LICENSE](LICENSE).

---
*中文版：[README.zh-CN.md](README.zh-CN.md) · Protocol 文档：[protocol/README.md](protocol/README.md)*

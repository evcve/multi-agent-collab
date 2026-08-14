# protocol API Reference

Stable public interface of the collaboration protocol. Version: 0.1

## `protocol.task.Task`

Task structure with the 4-element spec (goal / context / constraints / acceptance).

### Constructor

```python
Task(goal: str,
     context: str = "",
     constraints: str = "",
     acceptance: str = "",
     task_id: str = None,       # auto uuid4().hex[:12]
     target_agent: str = "worker",
     timeout: int = 120,
     status: str = "pending")   # pending|processing|done|failed|need_confirm|timeout
```

### Fields

| Field | Type | Meaning |
|---|---|---|
| `goal` | str | What result is wanted (one sentence, verifiable) |
| `context` | str | Key data **embedded inline** (never a file path — executing LLMs have no file access) |
| `constraints` | str | Boundaries / prohibitions / output format |
| `acceptance` | str | Checkable acceptance criteria |
| `task_id` | str | Unique id (generated if omitted) |
| `status` | str | One of the 6 states above |
| `result` | Any | done→verifiable handle (path/value); failed→reason; need_confirm→options |
| `result_meta` | dict | Extra metadata (e.g. finished_at) |

### Methods

- `prompt` (property) → str: renders the 4 elements + feedback-format instruction into the LLM prompt.
- `to_dict() -> dict` / `from_dict(d) -> Task`: JSON round-trip.

## `protocol.queue.FileQueue`

File-based queue; JSON files are the wire protocol — interoperable across processes and languages.

```python
FileQueue(base_dir: str)   # creates base_dir/queue and base_dir/results
```

### Submitter side

| Method | Signature | Behavior |
|---|---|---|
| `submit` | `(task: Task) -> task_id` | Writes `queue/<id>.json` (atomic), returns id |
| `wait_result` | `(task_id, timeout=300.0, poll_interval=1.0, progress_callback=None) -> Task` | Polls `results/<id>.json` (backoff 1s→5s); consumes file on read; returns `status="timeout"` Task on expiry; idempotent cleanup. `progress_callback(progress, note)` called on each poll while running |

### Worker side

| Method | Signature | Behavior |
|---|---|---|
| `scan` | `() -> list[Task]` | Returns all `pending` tasks (processing ones excluded) |
| `mark_processing` | `(task) -> None` | Sets status and rewrites queue file |
| `report_progress` | `(task, progress, note="") -> None` | Heartbeat: updates progress fields in the queue file |
| `cancel` / `is_cancelled` | `(task_id) -> None` / `-> bool` | Cancellation protocol: worker skips cancelled tasks |
| `complete` | `(task) -> None` | **Atomically** writes `results/<id>.json` (tmp + `os.replace`), deletes queue file |

## `protocol.worker`

Worker loop + LLM integration.

### Functions

| Function | Signature | Behavior |
|---|---|---|
| `call_llm` | `(prompt, timeout=120) -> str` | POST to `LLM_BASE_URL/chat/completions` with `LLM_API_KEY`, `LLM_MODEL`, `LLM_MAX_TOKENS` |
| `parse_three_state` | `(raw: str) -> (status, result, note)` | Parses `[状态]`/`[结果]`/`[备注]` lines; **missing/invalid status → `failed`** (never silent `done`); note stored in `result_meta.note` |
| `validate_schema` | `(data, schema) -> list` | Minimal JSON-Schema subset (type/required/properties); returns errors (empty = pass) |

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `LLM_API_KEY` | — (required) | Auth token |
| `LLM_MODEL` | `gpt-4o-mini` | Model name |
| `LLM_MAX_TOKENS` | `2000` | Raise for large outputs (HTML generation etc.) |

### CLI

```bash
python -m protocol.worker --queue-dir ./runtime        # daemon loop
python -m protocol.worker --queue-dir ./runtime --once # drain once
python -m protocol.worker --interval 10               # poll interval
```

## Status machine

```
        submit
pending ────────► processing ────────► done / failed / need_confirm
   ▲                  │
   └── timeout (submitter-side, idempotent cleanup)
```

## Stability notes

- JSON schema of the task/result files is the compatibility contract — keep `to_dict`/`from_dict`
  in sync when adding fields (additive only).
- `FileQueue` is safe for one-writer/one-reader; multiple workers need a lock or a real broker
  (the file layer is the *protocol*, not a high-concurrency transport).

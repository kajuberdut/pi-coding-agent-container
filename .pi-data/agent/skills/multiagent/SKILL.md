---
name: multiagent
description: Delegate complex coding, algorithm, or architecture problems to the GVS5H ledger-based multi-agent workflow (Primary Manager + Ideation & Task Workers sharing a plan, notes, and task ledger). Supports running synchronously or asynchronously in the background.
---

# Ledger-Based Multi-Agent Orchestrator (`multiagent`)

This skill runs the **GVS5H ledger-based zero-shot self-orchestration** workflow (`scripts/multiagent.py`). Fresh instances of a model coordinate through a shared filesystem ledger (`plan.md`, `notes.md`, `tasks.json`, `solution.py`/`answer.md`, and `transcript.jsonl`):

1. **Primary Planner**: Writes a 3–6 sentence strategy (`plan.md`) and initial task breakdown (`tasks.json`).
2. **Ideation Worker**: Brainstorms multiple genuinely distinct candidate approaches and pitfalls in prose (`notes.md`).
3. **Primary Manager**: Curates `tasks.json`, checks ground-truth test results, pivots approaches when workers get stuck, and selects the single highest-value next task.
4. **Task Workers**: Execute one task in a fresh context window, update `solution.py` / `answer.md`, and rewrite/compact `notes.md` so context never bloats.

---

## Endpoint & Model Resolution

By default, `scripts/multiagent.py` connects to the container's active model endpoint (`http://local-model-runner:8080/v1`, which routes to either the local `llama.cpp` runner or the primary `model-proxy`) and queries `/v1/models` to auto-detect the served model ID.

Check which endpoint and model will be used:
```bash
python3 ~/.pi/agent/skills/multiagent/scripts/multiagent.py --probe
```

---

## Usage

### 1. Asynchronous Background Mode (Recommended for Long Tasks)

Launch a multi-agent workflow in the background so you can continue working in Pi while the orchestrator iterates on the problem:

```bash
python3 ~/.pi/agent/skills/multiagent/scripts/multiagent.py \
  --bg \
  --run-id my-feature \
  --test-cmd "pytest -q" \
  --out /workspace/solution.py \
  "Design and implement a lock-free ring buffer in Python with property tests."
```

Check live progress (current phase, iteration, curated `tasks.json`, and `plan.md`):
```bash
# List all runs
python3 ~/.pi/agent/skills/multiagent/scripts/multiagent.py --status

# Detailed JSON status for a specific run
python3 ~/.pi/agent/skills/multiagent/scripts/multiagent.py --status my-feature
```

Inspect the live ledger files directly in `/workspace/.multiagent/<run-id>/`:
- `plan.md` — High-level strategy
- `notes.md` — Compacted findings & candidate approaches
- `tasks.json` — Curated task list (`pending` / `done`)
- `solution.py` or `answer.md` — Latest candidate artifact
- `driver.log` — Live step-by-step progress log

Wait for completion or stop a background run:
```bash
python3 ~/.pi/agent/skills/multiagent/scripts/multiagent.py --wait my-feature
python3 ~/.pi/agent/skills/multiagent/scripts/multiagent.py --stop my-feature
```

### 2. Synchronous Foreground Mode

Run and wait for the final output in one step:
```bash
python3 ~/.pi/agent/skills/multiagent/scripts/multiagent.py \
  --kind code \
  --test-cmd "python3 -m unittest" \
  "Implement an exact solver for..."
```

### Key Flags

| Flag | Description |
|---|---|
| `--bg` | Run detached in the background and return `run_id` immediately |
| `--status [RUN_ID]` | Show summary table of all runs, or detailed JSON ledger status of `RUN_ID` |
| `--wait RUN_ID` | Block until `RUN_ID` finishes and print the final artifact |
| `--stop RUN_ID` | Terminate a running background job |
| `--kind {code,text}` | `code` writes `solution.py`; `text` writes `answer.md` |
| `--test-cmd CMD` | Verification shell command run after each worker write (`{solution}` and `{ws}` are substituted) |
| `--api-base URL` | Override endpoint URL (default: `http://local-model-runner:8080/v1`) |
| `--model ID` | Override model name |
| `--max-iters N` | Maximum manager/worker iterations (default: `10`) |
| `--out PATH` | Copy the final artifact to `PATH` when complete |

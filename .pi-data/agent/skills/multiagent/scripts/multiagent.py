#!/usr/bin/env python3
"""
Standalone GVS5H Ledger-Based Multi-Agent Orchestrator for Pi Sandbox.

Coordinates a single LLM across fresh contexts (Primary Planner, Ideation Worker,
Primary Manager, Task Workers, and Cutoff Summarizer) via a shared filesystem
ledger:
  - plan.md          : Overarching 3-6 sentence strategy
  - notes.md         : Living analysis & findings, rewritten/compacted by workers
  - tasks.json       : Curated task list ([todo] / [done]) capped at MAX_TASKS
  - solution.py      : Current candidate code artifact (when --kind code)
  - answer.md        : Current candidate prose/math artifact (when --kind text)
  - transcript.jsonl : Full per-call audit log (prompts, responses, reasoning, tokens)
  - status.json      : Live state for asynchronous (--bg) monitoring
"""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- Configuration & Endpoint Resolution -------------------------------------

PRIMARY_DEFAULT_URL = "http://local-model-runner:8080/v1"

MAX_ITERS = int(os.environ.get("MULTIAGENT_MAX_ITERS", "10"))
MAX_TASKS = int(os.environ.get("MULTIAGENT_MAX_TASKS", "12"))
REQUEST_TIMEOUT = int(os.environ.get("MULTIAGENT_TIMEOUT", "1800"))
MAX_TOKENS = int(os.environ.get("MULTIAGENT_MAX_TOKENS", "32768"))
STRICT_FORMAT = os.environ.get("MULTIAGENT_STRICT_FORMAT", "auto")
REASONING_EFFORT = os.environ.get("MULTIAGENT_REASONING", "")

DEFAULT_WS_ROOT = os.environ.get("MULTIAGENT_WS", "/workspace/.multiagent")


def _normalize_chat_url(base_or_url: str) -> str:
    url = base_or_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def _normalize_models_url(base_or_url: str) -> str:
    url = base_or_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if url.endswith("/v1"):
        return f"{url}/models"
    return f"{url}/v1/models"


def probe_endpoint(base_or_url: str, api_key: str = "none", timeout: float = 2.0):
    """Probe an OpenAI-compatible /v1/models endpoint and return available model IDs."""
    models_url = _normalize_models_url(base_or_url)
    headers = {"User-Agent": "pi-multiagent/1.0"}
    if api_key and api_key != "none":
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(models_url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data.get("data"), list):
            return [m["id"] for m in data["data"] if isinstance(m, dict) and "id" in m]
        if isinstance(data.get("models"), list):
            return [m.get("id") or m.get("name") for m in data["models"] if isinstance(m, dict)]
        return []
    except Exception:
        return None


def resolve_endpoint_and_model(cli_url=None, cli_model=None, cli_key=None):
    """
    Resolve the LLM endpoint and model ID for the multi-agent workflow.
    Uses the primary container model endpoint (local-model-runner:8080, which
    routes to either the local llama.cpp runner or the primary model-proxy).
    """
    api_key = cli_key or os.environ.get("MULTIAGENT_API_KEY", "none")
    explicit_url = cli_url or os.environ.get("MULTIAGENT_API_BASE", "").strip()
    explicit_model = cli_model or os.environ.get("MULTIAGENT_MODEL", "").strip()

    chosen_base = explicit_url or PRIMARY_DEFAULT_URL
    discovered_models = []

    probed = probe_endpoint(chosen_base, api_key=api_key, timeout=2.0)
    if probed:
        discovered_models = probed

    # Determine model name
    model_id = explicit_model
    if not model_id and discovered_models:
        model_id = discovered_models[0]
    if not model_id:
        # Check Pi's settings.json as final fallback
        settings_path = Path(os.environ.get("HOME", "/home/node")) / ".pi" / "agent" / "settings.json"
        if settings_path.exists():
            try:
                sdata = json.loads(settings_path.read_text(encoding="utf-8"))
                model_id = sdata.get("defaultModel", "")
            except Exception:
                pass
    if not model_id:
        model_id = "default"

    return _normalize_chat_url(chosen_base), model_id, api_key


# --- OpenAI-Compatible Chat Client -------------------------------------------

def chat_completion(url, api_key, model, messages, temperature=0.2, meta=None):
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    if MAX_TOKENS > 0:
        body["max_tokens"] = MAX_TOKENS
    if REASONING_EFFORT:
        body["reasoning_effort"] = REASONING_EFFORT

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or 'none'}",
        "User-Agent": "pi-multiagent/1.0",
    }

    def _do_request(box, payload_bytes):
        try:
            req = urllib.request.Request(url, data=payload_bytes, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                box["out"] = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            box["err"] = e
            box["code"] = e.code
            try:
                box["body"] = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                box["body"] = ""
        except Exception as e:
            box["err"] = e

    attempts = 6
    cur_max = body.get("max_tokens")
    last_content = ""

    for attempt in range(attempts):
        req_body = dict(body)
        if cur_max:
            req_body["max_tokens"] = cur_max

        box = {}
        th = threading.Thread(
            target=_do_request, args=(box, json.dumps(req_body).encode("utf-8")), daemon=True
        )
        th.start()
        th.join(REQUEST_TIMEOUT + 5)

        if th.is_alive():
            if meta is not None:
                meta.setdefault("discarded", []).append(
                    {"attempt": attempt + 1, "why": f"timeout >{REQUEST_TIMEOUT}s"}
                )
            continue

        out = box.get("out")
        if out is None:
            e = box.get("err")
            body_txt = (box.get("body") or "").lower()
            ctx_err = any(
                s in body_txt
                for s in ("context", "maximum context length", "too long", "exceeds", "max_model_len")
            )
            if box.get("code") == 400 and ctx_err and cur_max and cur_max > 4096:
                cur_max = max(4096, cur_max // 2)
                continue
            if box.get("code") in (400, 401, 403, 404) and not ctx_err:
                if meta is not None:
                    meta["error"] = f"HTTP {box.get('code')}: {box.get('body')}"
                break
            time.sleep(min(15, 2 * (attempt + 1)))
            continue

        choices = out.get("choices") or []
        if not choices:
            time.sleep(2)
            continue

        choice = choices[0]
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason")
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        if not content and reasoning:
            content = reasoning
        usage = out.get("usage") or {}

        if meta is not None:
            meta["finish_reason"] = finish
            meta["completion_tokens"] = usage.get("completion_tokens")
            meta["prompt_tokens"] = usage.get("prompt_tokens")
            meta["reasoning"] = reasoning
            meta["attempts"] = attempt + 1

        if content.strip():
            return content
        last_content = content or last_content
        time.sleep(1)

    if meta is not None and not last_content.strip():
        meta["infra_exhausted"] = True
    return last_content


# --- Workspace & Ledger Helpers ----------------------------------------------

def _slug(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:12]


def _read(ws: str, name: str) -> str:
    p = os.path.join(ws, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _write(ws: str, name: str, content: str):
    with open(os.path.join(ws, name), "w", encoding="utf-8") as f:
        f.write(content)


def _append(ws: str, name: str, content: str):
    with open(os.path.join(ws, name), "a", encoding="utf-8") as f:
        f.write(content)


def _record(ws: str, rec: dict):
    with open(os.path.join(ws, "transcript.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _update_status(ws: str, **updates):
    status_path = os.path.join(ws, "status.json")
    cur = {}
    if os.path.exists(status_path):
        try:
            cur = json.loads(open(status_path, encoding="utf-8").read())
        except Exception:
            cur = {}
    cur.update(updates)
    cur["updated_at"] = time.time()
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2)


# --- Parsing Helpers ---------------------------------------------------------

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _strip_code(text: str) -> str:
    return re.sub(r"```.*?```", "[code omitted -- approach only]", text, flags=re.DOTALL).strip()


def _sections(text: str) -> dict:
    out, cur, buf = {}, None, []
    for line in _strip_think(text).splitlines():
        s = line.strip()
        m = (
            re.match(r"^#{1,3}\s*([A-Za-z_]+)\s*$", s)
            or re.match(r"^\*\*([A-Za-z_]+)\*\*\s*:?\s*$", s)
            or re.match(r"^([A-Z_]{3,})\s*:\s*$", s)
        )
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).upper(), []
        else:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def _bullets(text: str) -> list:
    items = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)", line)
        if m:
            items.append(m.group(1).strip())
    return items


def _extract_py(text: str) -> str:
    m = re.search(r"```(?:python|\w+)?\s*\n(.*?)```", _strip_think(text), re.DOTALL)
    return m.group(1).strip() if m else ""


def _parse_tasks(text: str, max_tasks: int = MAX_TASKS) -> list:
    out = []
    for b in _bullets(text):
        m = re.match(r"\[\s*([a-z_ ]+?)\s*\]\s*(.*)", b, re.I)
        if m:
            raw = m.group(1).lower().replace(" ", "").replace("_", "")
            desc = m.group(2).strip()
            status = (
                "done"
                if raw == "done"
                else ("in_progress" if raw in ("wip", "inprogress") else "pending")
            )
        else:
            desc, status = b, "pending"
        if desc:
            out.append({"id": len(out) + 1, "desc": desc, "status": status, "result": ""})
    return out[:max_tasks]


def _strict(model: str) -> bool:
    return STRICT_FORMAT == "1" or (
        STRICT_FORMAT == "auto" and any(s in model.lower() for s in ("muse", "glimmer"))
    )


def _format_rule(first: str, *rest: str) -> str:
    others = "".join(f", then the literal line '### {r}'" for r in rest)
    return (
        "\n\nTHE FORMAT IS MANDATORY AND YOUR REPLY IS PARSED BY A PROGRAM. "
        f"Begin your reply with the literal line '### {first}'{others}. "
        "Write nothing before the first header and nothing outside these sections. "
        "Do NOT solve the problem and do NOT write code -- a later worker does that. "
        "A reply without these exact header lines is discarded."
    )


MAX_PLAN_CHARS = 4000
MAX_ANSWER_CHARS = 30000


# --- Verification / Test Runner ----------------------------------------------

def run_verification(ws: str, test_cmd: str, artifact_name: str) -> dict:
    """
    Execute an optional verification command (e.g., pytest or a test script)
    against the workspace or candidate artifact so the Manager gets ground-truth
    pass/fail feedback.
    """
    if not test_cmd:
        return {"ran": False}
    artifact_path = os.path.join(ws, artifact_name)
    if not _read(ws, artifact_name).strip():
        return {"ran": False}

    cmd = test_cmd.replace("{solution}", artifact_path).replace("{ws}", ws)
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=30,
        )
        passed = r.returncode == 0
        out_snip = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()[:800]
        return {"ran": True, "passed": passed, "returncode": r.returncode, "output": out_snip}
    except subprocess.TimeoutExpired:
        return {"ran": True, "passed": False, "returncode": -1, "output": "<timed out (>30s)>"}
    except Exception as e:
        return {"ran": True, "passed": False, "returncode": -1, "output": f"<error: {e}>"}


def format_verification_feedback(res: dict) -> str:
    if not res or not res.get("ran"):
        return ""
    if res["passed"]:
        return "[TESTS: PASSED -- verification command succeeded (exit 0).] "
    return (
        f"[TESTS: FAILED (exit {res.get('returncode')}) -- current solution is WRONG. "
        f"Output: {res.get('output', '')!r}. Fix the issue or switch to a DIFFERENT approach.] "
    )


# --- Multi-Agent Roles -------------------------------------------------------

class MultiAgentEngine:
    def __init__(self, url: str, model: str, api_key: str, kind: str = "code",
                 max_iters: int = MAX_ITERS, max_tasks: int = MAX_TASKS,
                 test_cmd: str = "", log_fn=None):
        self.url = url
        self.model = model
        self.api_key = api_key
        self.kind = kind
        self.max_iters = max_iters
        self.max_tasks = max_tasks
        self.test_cmd = test_cmd
        self.log = log_fn or (lambda msg: print(msg, flush=True))
        self.artifact_file = "solution.py" if kind == "code" else "answer.md"

    def _chat(self, ws: str, role: str, messages: list, temperature: float, meta=None) -> str:
        m = meta if meta is not None else {}
        resp = chat_completion(
            self.url, self.api_key, self.model, messages, temperature=temperature, meta=m
        )
        _record(
            ws,
            {
                "t": time.time(),
                "role": role,
                "request": messages,
                "response": resp,
                "reasoning": m.get("reasoning"),
                "finish_reason": m.get("finish_reason"),
                "completion_tokens": m.get("completion_tokens"),
                "prompt_tokens": m.get("prompt_tokens"),
                "attempts": m.get("attempts"),
                "discarded": m.get("discarded"),
                "infra_exhausted": m.get("infra_exhausted"),
            },
        )
        return resp

    def primary_plan(self, problem: str, ws: str) -> list:
        domain = "software engineering and algorithms" if self.kind == "code" else "analytical problem solving"
        sys_prompt = (
            f"You are the PRIMARY orchestrator (manager) of a small team of workers, all "
            f"expert at {domain}. Given a problem, produce a short overarching plan to solve "
            f"it, then a task list the workers can pick up. Respond with EXACTLY these sections:\n"
            f"### PLAN\n<3-6 sentence strategy>\n"
            f"### TASKS\n<3-6 bullet tasks, each a concrete unit of work>"
        )
        if _strict(self.model):
            sys_prompt += _format_rule("PLAN", "TASKS")

        reply = self._chat(
            ws,
            "primary_plan",
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": problem}],
            temperature=0.3,
        )
        sec = _sections(reply)
        _write(ws, "plan.md", (sec.get("PLAN") or reply.strip())[:MAX_PLAN_CHARS])
        tasks = []
        have = set()
        for d in _bullets(sec.get("TASKS", "")):
            d = d.strip()
            if d and d.lower() not in have and len(tasks) < self.max_tasks:
                tasks.append({"id": len(tasks) + 1, "desc": d, "status": "pending", "result": ""})
                have.add(d.lower())
        self.log(f"  [primary:plan] created plan + {len(tasks)} initial tasks")
        return tasks

    def ideation_worker(self, problem: str, ws: str) -> list:
        sys_prompt = (
            "You are the FIRST WORKER. Do NOT solve the problem and do NOT write any code. "
            "Just think about it: identify the core difficulty, then list SEVERAL DISTINCT "
            "candidate approaches (genuinely different algorithms / architectures / problem "
            "reductions, not variations of one idea), and note pitfalls for each. Describe each "
            "approach in prose only -- absolutely no code blocks; a later worker will implement. "
            "Respond with EXACTLY:\n### NOTES\n<your analysis>\n"
            "### NEXT\n<bullet list of distinct approaches to try next>"
        )
        if _strict(self.model):
            sys_prompt += _format_rule("NOTES", "NEXT")

        reply = self._chat(
            ws,
            "ideation",
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"PROBLEM:\n{problem}\n\nPLAN:\n{_read(ws, 'plan.md')}"},
            ],
            temperature=0.4,
        )
        sec = _sections(reply)
        _append(
            ws,
            "notes.md",
            f"\n## ideation\n{_strip_code(sec.get('NOTES') or reply.strip())[:MAX_PLAN_CHARS * 2]}\n",
        )
        proposals = [_strip_code(p) for p in _bullets(sec.get("NEXT", ""))]
        self.log(f"  [worker:ideate] proposed {len(proposals)} distinct approaches")
        return proposals

    def primary_manage(self, problem: str, ws: str, tasks: list, proposals: list, last_summary: str):
        cur = _read(ws, self.artifact_file).strip()
        task_lines = (
            "\n".join(f"- [{'done' if t['status'] == 'done' else 'todo'}] {t['desc']}" for t in tasks)
            or "(none)"
        )
        prop_lines = "\n".join(f"- {p}" for p in proposals) or "(none)"
        sys_prompt = (
            "You are the PRIMARY orchestrator and manager. You OWN the task list and decide "
            "when the problem is solved. Review the current progress and the latest worker's "
            "result, then:\n"
            "- The LATEST WORKER RESULT may include a TESTS verdict from actually running "
            "verification. Treat it as ground truth: only set STATUS 'done' if verification "
            "PASSED; if it FAILED, you MUST set STATUS 'continue' and choose a task that "
            "fixes the failing case or switches to a different approach.\n"
            "- If the current solution/answer is complete and correct, set STATUS to 'done'.\n"
            "- Otherwise CURATE the task list: merge duplicates, drop finished or irrelevant "
            "items, mark completed ones [done], and fold in ONLY genuinely new sub-tasks from "
            "the proposals. Then choose the single most valuable next task.\n"
            "- IMPORTANT: if the current solution keeps failing, or the last worker made no real "
            "progress, do NOT keep refining the same idea. Switch to a DIFFERENT approach "
            "from the notes, or ask for a new one.\n"
            "Respond with EXACTLY these sections:\n"
            "### STATUS\n<done|continue>\n"
            "### NEXT\n<exact text of the ONE task to do next; omit if done>\n"
            "### TASKS\n<curated list, one per line, each '- [done] ...' or '- [todo] ...'>"
        )
        if _strict(self.model):
            sys_prompt += _format_rule("STATUS", "NEXT", "TASKS")

        reply = self._chat(
            ws,
            "primary_manage",
            [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": (
                        f"PROBLEM:\n{problem}\n\n"
                        f"CURRENT {'SOLUTION' if self.kind == 'code' else 'ANSWER'}:\n"
                        f"{cur or '(none yet)'}\n\n"
                        f"NOTES:\n{_read(ws, 'notes.md')}\n\n"
                        f"CURRENT TASK LIST:\n{task_lines}\n\n"
                        f"LATEST WORKER RESULT: {last_summary}\n\n"
                        f"PROPOSED NEW STEPS:\n{prop_lines}"
                    ),
                },
            ],
            temperature=0.2,
        )
        sec = _sections(reply)
        curated = _parse_tasks(sec.get("TASKS", ""), max_tasks=self.max_tasks) or tasks
        status = "done" if sec.get("STATUS", "").strip().lower().startswith("done") else "continue"
        if status == "done" and not cur:
            status = "continue"
        nxt = (_bullets(sec.get("NEXT", ""))[:1] or [sec.get("NEXT", "").strip()])[0]
        nxt = re.sub(r"^[-*]\s*", "", nxt).strip()
        if status == "continue" and not nxt:
            todo = [t for t in curated if t["status"] != "done"]
            nxt = todo[0]["desc"] if todo else ""
        if not cur and not nxt:
            status = "continue"
            nxt = "Implement the full working solution for the most promising approach in the notes."
        self.log(f"  [primary:manage] {status}" + (f" -> next: {nxt[:70]}" if status == "continue" else ""))
        return status, nxt, curated

    def summarize_cutoff(self, ws: str, reply: str, task_desc: str) -> str:
        txt = _strip_think(reply) or reply
        snippet = txt if len(txt) <= 9000 else txt[:3500] + "\n...[middle omitted]...\n" + txt[-5500:]
        sys_prompt = (
            "A worker's solution attempt was CUT OFF when it hit the token limit. Summarize its "
            "partial attempt in 3-5 sentences: which approach it was pursuing, what it established "
            "or ruled out, how far it got, and what remained unfinished. Be concrete so another "
            "worker can resume or judge it. Do NOT try to finish the solution yourself."
        )
        s = self._chat(
            ws,
            "cutoff_summary",
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"TASK: {task_desc}\n\nCUT-OFF ATTEMPT:\n{snippet}"},
            ],
            temperature=0.2,
        )
        return _strip_think(s).strip() or "(cut-off attempt could not be summarized)"

    def worker(self, problem: str, ws: str, task: dict, finalize: bool = False):
        cur = _read(ws, self.artifact_file)
        notes_fmt = (
            "### NOTES\n<the COMPLETE notes file, rewritten. You are shown the current NOTES "
            "above: fold your findings into them, keep what still matters, and DELETE anything "
            "superseded, disproven, or now obvious. This REPLACES the file, so whatever you omit "
            "is gone. Organise it as '- **Topic:** ...' bullets, under ~800 words. Do NOT use "
            "markdown headings (#, ##), bold-only lines, or ALLCAPS: lines anywhere inside this "
            "section -- the reply is split on those, so they would truncate your notes.>\n"
        )
        if self.kind == "code":
            solver_role = (
                "You are an elite software engineer and algorithmist. Write clean, complete, "
                "self-contained code."
            )
            out_fmt = (
                "### CODE\n```python\n<the FULL updated self-contained program>\n```\n"
                + notes_fmt
                + "### NEXT\n<bullet list of remaining steps, or 'none'>\n### STATUS\n<solved|continue>"
            )
        else:
            solver_role = "You are an expert problem solver and analyst."
            out_fmt = (
                "### ANSWER\n<full worked solution / final deliverable>\n"
                + notes_fmt
                + "### NEXT\n<bullet list of remaining checks, or 'none'>\n### STATUS\n<solved|continue>"
            )

        goal = (
            "Produce the DEFINITIVE final solution now, using all notes and current work."
            if finalize
            else f"Complete this task: {task['desc']}"
        )
        sys_prompt = (
            f"You are a WORKER subagent. {solver_role} "
            "You share a workspace with the team. Build on the current work and notes where "
            "useful -- but if your task is to try a different approach, write a FRESH solution "
            "for that approach instead of patching the stuck one. "
            f"Respond with EXACTLY these sections:\n{out_fmt}"
        )
        wmeta = {}
        reply = self._chat(
            ws,
            "finalize" if finalize else f"worker:{task['id']}",
            [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": (
                        f"PROBLEM:\n{problem}\n\nPLAN:\n{_read(ws, 'plan.md')}\n\n"
                        f"NOTES:\n{_read(ws, 'notes.md')}\n\nCURRENT WORK:\n{cur or '(none yet)'}\n\n"
                        f"YOUR TASK: {goal}"
                    ),
                },
            ],
            temperature=0.2,
            meta=wmeta,
        )
        sec = _sections(reply)
        wrote = False
        if self.kind == "code":
            code = _extract_py(sec.get("CODE", "")) or _extract_py(reply)
            if code:
                _write(ws, self.artifact_file, code)
                wrote = True
        else:
            ans = sec.get("ANSWER", "").strip()
            if not ans and len(reply) < MAX_ANSWER_CHARS:
                ans = _strip_think(reply).strip()
            if ans:
                _write(ws, self.artifact_file, ans)
                wrote = True

        if sec.get("NOTES") and wmeta.get("finish_reason") != "length":
            _write(ws, "notes.md", sec["NOTES"].strip()[: MAX_PLAN_CHARS * 2] + "\n")

        nexts = [b for b in _bullets(sec.get("NEXT", "")) if b.lower() != "none"]
        status = sec.get("STATUS", "continue").strip().lower()

        if wmeta.get("finish_reason") == "length" and not finalize:
            digest = self.summarize_cutoff(ws, reply, task["desc"])
            summary = (
                f"worker EXCEEDED THE TOKEN LIMIT and was cut off before finishing task: "
                f"{task['desc'][:80]}. Prefer a simpler or modular approach next. "
                f"Summary of partial thinking: {digest[:500]}"
            )
            self.log(f"  [worker:{task['id']}] CUT OFF at token limit; summarized thinking")
            return "continue", nexts, summary, wrote

        summary = f"worker reported '{status}' on: {task['desc'][:80]}. " + (sec.get("NOTES", "")[:200])
        self.log(f"  [worker:{task['id']}] -> {status} (+{len(nexts)} next steps)")
        return status, nexts, summary, wrote

    def run(self, problem_text: str, ws: str) -> str:
        os.makedirs(ws, exist_ok=True)
        for fname in ("task.md", "notes.md", "transcript.jsonl", "plan.md", self.artifact_file, "tasks.json"):
            if not os.path.exists(os.path.join(ws, fname)):
                _write(ws, fname, "")
        _write(ws, "task.md", problem_text)
        _record(
            ws,
            {
                "_meta": True,
                "t": time.time(),
                "endpoint": self.url,
                "model": self.model,
                "kind": self.kind,
                "max_iters": self.max_iters,
                "problem": problem_text,
            },
        )
        _update_status(
            ws,
            state="running",
            phase="planning",
            endpoint=self.url,
            model=self.model,
            iteration=0,
            max_iters=self.max_iters,
        )

        self.log(f"[multiagent] workspace: {ws}")
        self.log(f"[multiagent] endpoint : {self.url} (model={self.model})")

        tasks = self.primary_plan(problem_text, ws)
        _write(ws, "tasks.json", json.dumps(tasks, indent=2))

        _update_status(ws, phase="ideation")
        proposals = self.ideation_worker(problem_text, ws)

        _update_status(ws, phase="managing")
        status, next_desc, tasks = self.primary_manage(
            problem_text, ws, tasks, proposals, "ideation complete"
        )
        _write(ws, "tasks.json", json.dumps(tasks, indent=2))

        iters, prev_desc = 0, None
        while status == "continue" and next_desc and iters < self.max_iters:
            if prev_desc is not None and next_desc.strip().lower() == prev_desc.strip().lower():
                self.log(f"  [primary] reissued identical task; stopping after {iters} iterations")
                break
            prev_desc = next_desc
            iters += 1
            _update_status(ws, phase="worker", iteration=iters, active_task=next_desc)

            task = {"id": iters, "desc": next_desc, "status": "in_progress", "result": ""}
            _, nexts, summary, wrote = self.worker(problem_text, ws, task)

            vres = None
            if self.test_cmd and wrote:
                vres = run_verification(ws, self.test_cmd, self.artifact_file)
                if vres.get("ran"):
                    summary = format_verification_feedback(vres) + summary
                    self.log(f"  [verify] passed={vres['passed']}")

            _update_status(ws, phase="managing", iteration=iters)
            status, next_desc, tasks = self.primary_manage(problem_text, ws, tasks, nexts, summary)
            if vres and vres.get("ran") and not vres["passed"] and status == "done":
                status = "continue"
                if not next_desc:
                    next_desc = "Verification failed; fix the failing case or switch approaches."
                self.log("  [primary:manage] overriding 'done' -- verification still failing")
            _write(ws, "tasks.json", json.dumps(tasks, indent=2))

        has_ans = bool(_read(ws, self.artifact_file).strip())
        if status == "done" and has_ans:
            self.log("  [finalize] skipped (manager marked done with artifact present)")
        else:
            _update_status(ws, phase="finalize", iteration=iters)
            self.worker(problem_text, ws, {"id": 0, "desc": "finalize"}, finalize=True)

        final_artifact = _read(ws, self.artifact_file)
        _update_status(
            ws,
            state="done",
            phase="complete",
            iteration=iters,
            artifact_file= os.path.join(ws, self.artifact_file),
            has_artifact=bool(final_artifact.strip()),
        )
        return final_artifact


# --- CLI & Async Background Management ---------------------------------------

def _resolve_ws(ws_root: str, run_id: str = None, problem_text: str = None) -> str:
    if run_id:
        if os.path.isabs(run_id) and os.path.isdir(run_id):
            return run_id
        return os.path.join(ws_root, run_id)
    slug = _slug(problem_text or f"run-{time.time()}")
    return os.path.join(ws_root, slug)


def cmd_status(ws_root: str, run_id: str = None):
    if not run_id:
        # List all runs in ws_root
        if not os.path.exists(ws_root):
            print("No multiagent runs found.")
            return
        runs = []
        for entry in sorted(os.listdir(ws_root)):
            st_file = os.path.join(ws_root, entry, "status.json")
            if os.path.exists(st_file):
                try:
                    st = json.loads(open(st_file, encoding="utf-8").read())
                    runs.append((entry, st))
                except Exception:
                    pass
        if not runs:
            print("No multiagent runs found.")
            return
        print(f"{'RUN_ID':<14} {'STATE':<10} {'PHASE':<12} {'ITER':<6} {'MODEL'}")
        print("-" * 68)
        for rid, st in runs:
            it = f"{st.get('iteration', 0)}/{st.get('max_iters', '?')}"
            print(f"{rid:<14} {st.get('state', '?'):<10} {st.get('phase', '?'):<12} {it:<6} {st.get('model', '?')}")
        return

    ws = _resolve_ws(ws_root, run_id=run_id)
    st_file = os.path.join(ws, "status.json")
    if not os.path.exists(st_file):
        print(f"Run '{run_id}' not found in {ws}", file=sys.stderr)
        sys.exit(1)
    st = json.loads(open(st_file, encoding="utf-8").read())
    tasks_raw = _read(ws, "tasks.json")
    tasks = json.loads(tasks_raw) if tasks_raw.strip() else []
    st["tasks"] = tasks
    st["plan"] = _read(ws, "plan.md").strip()
    st["workspace"] = ws
    print(json.dumps(st, indent=2))


def cmd_stop(ws_root: str, run_id: str):
    ws = _resolve_ws(ws_root, run_id=run_id)
    pid_file = os.path.join(ws, "driver.pid")
    if not os.path.exists(pid_file):
        print(f"No active PID file for run '{run_id}'.", file=sys.stderr)
        sys.exit(1)
    pid = int(open(pid_file).read().strip())
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    _update_status(ws, state="stopped", phase="stopped_by_user")
    print(f"Stopped run {run_id} (pid {pid}).")


def cmd_wait(ws_root: str, run_id: str):
    ws = _resolve_ws(ws_root, run_id=run_id)
    st_file = os.path.join(ws, "status.json")
    if not os.path.exists(st_file):
        print(f"Run '{run_id}' not found.", file=sys.stderr)
        sys.exit(1)
    while True:
        try:
            st = json.loads(open(st_file, encoding="utf-8").read())
            if st.get("state") in ("done", "failed", "stopped"):
                artifact_path = st.get("artifact_file")
                if artifact_path and os.path.exists(artifact_path):
                    print(open(artifact_path, encoding="utf-8").read())
                else:
                    print(json.dumps(st, indent=2))
                return
        except Exception:
            pass
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser(
        description="GVS5H Ledger-Based Multi-Agent Solver (Synchronous & Async Background)"
    )
    ap.add_argument("prompt", nargs="?", help="Problem statement or task description")
    ap.add_argument("-f", "--file", help="Read problem statement from a file")
    ap.add_argument("--kind", choices=["code", "text"], default="code", help="Artifact mode: code (solution.py) or text (answer.md)")
    ap.add_argument("--api-base", help="OpenAI-compatible API base URL (overrides MULTIAGENT_API_BASE)")
    ap.add_argument("--model", help="Model ID to use (overrides MULTIAGENT_MODEL)")
    ap.add_argument("--api-key", help="API key if required by endpoint")
    ap.add_argument("--max-iters", type=int, default=MAX_ITERS, help=f"Max worker iterations (default: {MAX_ITERS})")
    ap.add_argument("--max-tasks", type=int, default=MAX_TASKS, help=f"Max tasks in ledger (default: {MAX_TASKS})")
    ap.add_argument("--test-cmd", default="", help="Shell command to verify candidate work (use {solution} or {ws})")
    ap.add_argument("--ws-root", default=DEFAULT_WS_ROOT, help=f"Workspace root directory (default: {DEFAULT_WS_ROOT})")
    ap.add_argument("--run-id", help="Explicit run ID / folder name inside ws-root")
    ap.add_argument("--out", help="Copy final solution/answer to this file path upon completion")

    # Async / background lifecycle flags
    ap.add_argument("--bg", action="store_true", help="Launch asynchronously in the background and return run_id immediately")
    ap.add_argument("--status", nargs="?", const="__all__", metavar="RUN_ID", help="Check status of a background run (or list all runs)")
    ap.add_argument("--wait", metavar="RUN_ID", help="Wait for a background run to finish and print its artifact")
    ap.add_argument("--stop", metavar="RUN_ID", help="Stop a running background job")
    ap.add_argument("--probe", action="store_true", help="Probe and print the resolved endpoint and model without running")
    ap.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)

    args = ap.parse_args()

    if args.probe:
        url, model, _ = resolve_endpoint_and_model(args.api_base, args.model, args.api_key)
        print(json.dumps({"endpoint": url, "model": model}, indent=2))
        return

    if args.status is not None:
        cmd_status(args.ws_root, None if args.status == "__all__" else args.status)
        return

    if args.stop:
        cmd_stop(args.ws_root, args.stop)
        return

    if args.wait:
        cmd_wait(args.ws_root, args.wait)
        return

    problem_text = ""
    if args.file:
        problem_text = open(args.file, encoding="utf-8").read().strip()
    elif args.prompt:
        problem_text = args.prompt.strip()
    elif not sys.stdin.isatty() and not args.internal_worker:
        problem_text = sys.stdin.read().strip()

    if not problem_text:
        ap.error("Provide a prompt argument, --file, or --status/--wait/--stop.")

    run_id = args.run_id or _slug(problem_text)
    ws = _resolve_ws(args.ws_root, run_id=run_id, problem_text=problem_text)
    os.makedirs(ws, exist_ok=True)
    _write(ws, "task.md", problem_text)

    if args.bg and not args.internal_worker:
        log_path = os.path.join(ws, "driver.log")
        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "--internal-worker",
            "--file",
            os.path.join(ws, "task.md"),
            "--kind",
            args.kind,
            "--max-iters",
            str(args.max_iters),
            "--max-tasks",
            str(args.max_tasks),
            "--ws-root",
            args.ws_root,
            "--run-id",
            run_id,
        ]
        if args.api_base:
            cmd += ["--api-base", args.api_base]
        if args.model:
            cmd += ["--model", args.model]
        if args.api_key:
            cmd += ["--api-key", args.api_key]
        if args.test_cmd:
            cmd += ["--test-cmd", args.test_cmd]
        if args.out:
            cmd += ["--out", args.out]

        with open(log_path, "a", encoding="utf-8") as log_f:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
            )
        _write(ws, "driver.pid", str(proc.pid))
        _update_status(ws, state="running", phase="starting", pid=proc.pid, run_id=run_id)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "pid": proc.pid,
                    "workspace": ws,
                    "log": log_path,
                    "status_cmd": f"python3 {os.path.abspath(__file__)} --status {run_id}",
                },
                indent=2,
            )
        )
        return

    url, model, api_key = resolve_endpoint_and_model(args.api_base, args.model, args.api_key)
    engine = MultiAgentEngine(
        url=url,
        model=model,
        api_key=api_key,
        kind=args.kind,
        max_iters=args.max_iters,
        max_tasks=args.max_tasks,
        test_cmd=args.test_cmd,
    )
    try:
        artifact = engine.run(problem_text, ws)
        if args.out and artifact:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(artifact)
        print("\n=== FINAL ARTIFACT ===")
        print(artifact)
    except Exception as e:
        _update_status(ws, state="failed", error=str(e))
        raise


if __name__ == "__main__":
    main()

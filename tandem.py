#!/usr/bin/env python3
"""Claude Code and Codex take turns on one repository until they agree the goal is done.

A run works on one goal in its own git worktree and branch, so the owner's checkout is never
touched. A campaign chains runs on one branch: when a goal ends, the agents pick the next one
from the project's plan and keep going until the plan is exhausted or the owner stops them.
The orchestrator owns git: after every turn it commits what the agent changed.
"""

import argparse
import concurrent.futures
import contextlib
import datetime
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

__version__ = "0.1.0"  # single source of truth; package.json must match (a test checks)

ROOT = pathlib.Path(__file__).resolve().parent
TURN_SCHEMA = ROOT / "schema" / "turn.json"
PLAN_SCHEMA = ROOT / "schema" / "plan.json"
TURN_PROMPT = ROOT / "prompts" / "turn.md"
PLAN_PROMPT = ROOT / "prompts" / "plan.md"


def resolve_runs():
    # Run directories must stay outside the target repository and tandem's own files because
    # Claude Code is not sandboxed. Only a source checkout keeps runs next to tandem.
    override = os.environ.get("TANDEM_RUNS")
    if override:
        path = pathlib.Path(override)
    elif (ROOT / ".git").exists():  # A worktree has a .git file instead of a directory.
        path = ROOT.parent / "tandem-runs"
    else:
        path = pathlib.Path.home() / ".tandem" / "runs"
    return path.expanduser().resolve()


RUNS = resolve_runs()

NAMES = {"claude": "Claude Code", "codex": "Codex"}
EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]
OTHER = {"claude": "codex", "codex": "claude"}

CLAUDE_READ = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(ls:*)", "Bash(which:*)",
]
CLAUDE_ALLOWED = CLAUDE_READ + [
    "Edit", "Write",
    "Bash(make:*)", "Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)",
    "Bash(python3:*)", "Bash(python:*)", "Bash(pip:*)", "Bash(pip3:*)", "Bash(uv:*)",
    "Bash(pytest:*)", "Bash(ruff:*)", "Bash(mypy:*)", "Bash(alembic:*)", "Bash(cd:*)",
    "Bash(.venv/bin/python:*)", "Bash(.venv/bin/pip:*)", "Bash(.venv/bin/pytest:*)",
    "Bash(docker compose:*)", "Bash(docker ps:*)", "Bash(docker logs:*)",
    "Bash(docker run:*)", "Bash(docker build:*)", "Bash(docker exec:*)", "Bash(docker stop:*)",
    "Bash(docker rm:*)", "Bash(docker inspect:*)", "Bash(docker port:*)", "Bash(docker images:*)",
    "Bash(docker pull:*)", "Bash(docker image rm:*)",
    "Bash(psql:*)", "Bash(curl:*)", "Bash(sleep:*)", "Bash(lsof:*)", "Bash(mkdir:*)",
]
CLAUDE_DISALLOWED = [
    "Bash(git commit:*)", "Bash(git push:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
    "Bash(git reset:*)", "Bash(git rebase:*)", "Bash(rm -rf:*)",
    "Bash(docker system prune:*)", "Bash(docker volume rm:*)", "Bash(docker volume prune:*)",
]

FULL_TURNS_IN_TRANSCRIPT = 8
MAX_FAILURES_PER_AGENT = 3
FAILED_RUN_WAIT = 1800          # seconds to wait when both agents fail for unknown reasons
MAX_FAILED_WAITS = 6
MAX_GOALS_WITHOUT_PROGRESS = 3  # blocked or out of turns in a row: the campaign pauses
SHORT_WAIT_MIN = 20             # an agent back within this many minutes is waited for, not skipped
REVIEW_MAX_TURNS = 20           # backlog reviews cover several goals at once
TRANSIENT_WAIT = 150            # network or API hiccup: retry the same turn after this
MAX_TRANSIENT_RETRIES = 6
LIMIT_FALLBACK_WAIT = 3600      # usage limit without a readable reset time
PLAN_RETRY_WAIT = 600

# Accounts are shared by every run on this machine, so availability is too: it lives beside the
# user's config, not in RUNS, so a source checkout and an npm install see the same usage limits.
AVAILABILITY = pathlib.Path.home() / ".tandem" / "availability.json"
# Pasted and dropped images are copied outside every repo and worktree, so no turn's commit can pick
# one up, and a screenshot deleted from the desktop stays readable for the rest of the run.
IMAGES = pathlib.Path.home() / ".tandem" / "images"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
IMAGE_MARK = re.compile(r"\[image: ([^\]\n]+)\]")
LIMIT_MARKERS = ("usage limit", "hit your limit", "limit reached", "rate limit", "quota exceeded",
                 "credit balance is too low")
TRANSIENT_MARKERS = ("enotfound", "econnreset", "etimedout", "econnrefused", "can't reach the api",
                     "overloaded", "stream disconnected", "error sending request", "internal server error",
                     " 502", " 503", " 504", " 529")


def run_path(arg):
    """A run or campaign directory given as a path, `runs/<id>` or a bare id."""
    p = pathlib.Path(arg).expanduser()
    return p.resolve() if p.exists() else (RUNS / p.name).resolve()


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class GitError(Exception):
    pass


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          **CHILD_POPEN)
    if check and proc.returncode != 0:
        raise GitError(f"`git {' '.join(args[:2])}` in {cwd} failed: {tail(proc.stderr or proc.stdout, 400)}")
    return proc.stdout.strip()


# The interactive session sets start_new_session here, so a Ctrl+C at its prompt reaches neither a
# turn in flight nor the check and git commands around it. Those children never get the terminal
# on stdin either: a detached shell holding it hangs up the session's terminal when it exits.
CHILD_POPEN = {}
# Agents and checks being waited on, so a session that is going away can stop them: an agent left
# behind keeps editing the worktree with nobody to time it out or commit what it did.
CHILDREN = set()
CHILDREN_LOCK = threading.Lock()
KILL_GRACE = 5  # seconds between asking those processes to stop and killing them


def run_child(cmd, input=None, timeout=None, **kwargs):
    """subprocess.run for the long calls (agents, the check), tracked in CHILDREN while they run."""
    with subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          **CHILD_POPEN, **kwargs) as proc:
        # In its own session the process leads a group, and killing the group takes its tools along.
        proc.tandem_group = bool(CHILD_POPEN.get("start_new_session")) and hasattr(os, "killpg")
        with CHILDREN_LOCK:
            CHILDREN.add(proc)
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except BaseException:
            signal_child(proc, hard=True)
            proc.wait()
            raise
        finally:
            with CHILDREN_LOCK:
                CHILDREN.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def signal_child(proc, hard=False):
    if proc.returncode is not None:
        return
    try:
        if proc.tandem_group:
            os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
        elif hard:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        pass


def signal_children(hard=False):
    """Stop every tracked agent and check; returns how many there were."""
    with CHILDREN_LOCK:
        procs = list(CHILDREN)
    for proc in procs:
        signal_child(proc, hard)
    return len(procs)


OUT_LOCK = threading.RLock()  # every line the orchestrator prints goes through here
CONSOLE = None                # the interactive session's Console, when one is running


def log(msg):
    emit(f"[{now()}] {msg}")


def emit(text):
    """Print one line; in an interactive session redraw the prompt so log and input do not interleave."""
    with OUT_LOCK:
        try:
            if CONSOLE:
                CONSOLE.write(text)
            else:
                print(text, flush=True)
        except OSError:
            pass  # the terminal is gone; the run must still record its turn and stop cleanly


def prog_name():
    """How the user invoked tandem, for advice we print: `tandem` via the npm wrapper, else the script path."""
    return os.environ.get("TANDEM_PROG") or (sys.argv[0] if sys.argv and sys.argv[0] else "./tandem.py")


def require_repo(path):
    """Resolve --repo and fail early, with the fix, unless it is a git repo with a commit."""
    repo = pathlib.Path(path).expanduser().resolve()
    if not repo.is_dir():
        sys.exit(f"{repo} is not a directory; pass --repo with a git repository")
    if git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        sys.exit(f"{repo} is not a git repository; run `git init` there or pass --repo")
    if not git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False):
        sys.exit(f"{repo} has no commits yet; make one (git add -A && git commit -m init) so tandem can branch from it")
    if RUNS == repo or repo in RUNS.parents:
        # Claude Code is not sandboxed, and run directories inside the target would be edited and
        # committed as part of it (a repo at $HOME holds ~/.tandem/runs, for one).
        sys.exit(f"the runs directory {RUNS} is inside {repo}; set TANDEM_RUNS to a directory outside it")
    return repo


def slug(text):
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")[:40]


def tail(text, n=600):
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


# ---------- agent availability ----------

def classify(err):
    low = (err or "").lower()
    if any(m in low for m in LIMIT_MARKERS):
        return "limit"
    if any(m in low for m in TRANSIENT_MARKERS):
        return "transient"
    return "other"


def parse_reset(err):
    """'try again at 5:45 PM' / 'resets 5pm' -> the next such local time; else an hour from now."""
    m = re.search(r"(?:again at|resets?(?: at)?)\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", err or "", re.I)
    now_dt = datetime.datetime.now()
    if not m:
        return now_dt + datetime.timedelta(seconds=LIMIT_FALLBACK_WAIT)
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    at = now_dt.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
    if at <= now_dt:
        at += datetime.timedelta(days=1)
    return at + datetime.timedelta(minutes=2)


def unavailable_until(agent):
    # fromisoformat is narrow before 3.11: it only reads what mark_unavailable wrote with
    # isoformat(timespec="seconds"). Do not feed it foreign ISO strings.
    try:
        until = datetime.datetime.fromisoformat(json.loads(AVAILABILITY.read_text())[agent])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None
    return until if until > datetime.datetime.now() else None


def mark_unavailable(agent, until):
    try:
        data = json.loads(AVAILABILITY.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[agent] = until.isoformat(timespec="seconds")
    AVAILABILITY.parent.mkdir(parents=True, exist_ok=True)
    write_json(AVAILABILITY, data)
    log(f"{NAMES[agent]} hit a usage limit; paused until {until:%H:%M}")


def wait_until(when, stop_file=None):
    """Sleep in short steps so a stop request is noticed; returns False if stopped."""
    while datetime.datetime.now() < when:
        if stop_file and pathlib.Path(stop_file).exists():
            return False
        time.sleep(min(5, max(1, (when - datetime.datetime.now()).total_seconds())))
    return True


def call_agent(agent, cfg, worktree, prompt, out_base, schema, key, readonly, stop_file=None):
    """Run one agent call, retrying network hiccups; a usage limit pauses the agent and re-raises."""
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        try:
            return RUNNERS[agent](cfg, worktree, prompt, out_base, schema, key, readonly)
        except RuntimeError as e:
            kind = classify(str(e))
            if kind == "limit":
                mark_unavailable(agent, parse_reset(str(e)))
                raise
            if kind != "transient" or attempt == MAX_TRANSIENT_RETRIES:
                raise
            log(f"{NAMES[agent]}: {tail(str(e), 120)}; retrying in {TRANSIENT_WAIT}s ({attempt + 1}/{MAX_TRANSIENT_RETRIES})")
            if not wait_until(datetime.datetime.now() + datetime.timedelta(seconds=TRANSIENT_WAIT), stop_file):
                raise


# ---------- state ----------

def write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def save(run_dir, state):
    write_json(run_dir / "state.json", state)


def load(run_dir):
    return json.loads((run_dir / "state.json").read_text())


def append_transcript(run_dir, text):
    with open(run_dir / "transcript.md", "a") as f:
        f.write(text + "\n")


def read_goal_file(path):
    """A goal file is plain text, optionally headed by `---` lines of `key: value` (name, first, check)."""
    text = pathlib.Path(path).expanduser().read_text()
    meta = {}
    if text.startswith("---\n"):
        head, _, text = text[4:].partition("\n---\n")
        for line in head.splitlines():
            key, _, value = line.partition(":")
            if value.strip():
                meta[key.strip()] = value.strip()
    return {"goal": text.strip(), "name": meta.get("name") or pathlib.Path(path).stem,
            "first": meta.get("first", "claude"), "check": meta.get("check", "")}


# ---------- prompts ----------

def render_history(history):
    if not history:
        return "(no turns yet - you start)"
    lines = []
    older, recent = history[:-FULL_TURNS_IN_TRANSCRIPT], history[-FULL_TURNS_IN_TRANSCRIPT:]
    for h in older:
        lines.append(f"- Turn {h['turn']} {NAMES[h['agent']]} ({h['status']}): {h['summary']}")
    for h in recent:
        head = f"### Turn {h['turn']} - {NAMES[h['agent']]} - {h['status']}"
        if h.get("commit"):
            head += f" - commit {h['commit']}"
        lines.append(head)
        if h.get("error"):
            lines.append(f"The agent failed: {h['error']}")
        for key, label in (("summary", "Summary"), ("could_not_do", "Could not do"),
                           ("checks", "Checks"), ("handoff", "Handoff")):
            if h.get(key):
                lines.append(f"{label}: {h[key]}")
        lines.append("")
    return "\n".join(lines)


def owner_block(messages, fresh):
    """Every owner message stays in every later prompt; the newest one is flagged."""
    if not messages:
        return ""
    parts = []
    for i, m in enumerate(messages):
        label = "NEW, read first" if fresh and i == len(messages) - 1 else f"from {m['at']}"
        parts.append(f"### Owner message ({label})\n\n{m['text'].strip()}")
    return ("\n## Messages from the repository owner (they take priority and apply to every turn)\n\n"
            + "\n\n".join(parts) + "\n")


def reviewer_mode(cfg):
    return cfg.get("codex_role") == "reviewer"


def away_for_long(agent):
    """Away beyond a short wait: the other agent should not sit idle for it."""
    until = unavailable_until(agent)
    return bool(until) and until - datetime.datetime.now() >= datetime.timedelta(minutes=SHORT_WAIT_MIN)


def role_block(state, agent):
    if not reviewer_mode(state["config"]):
        return ""
    base = state.get("review_from") or state["base"][:10]
    if agent == "codex":
        return (f"\n## Your role: reviewer\n\nYou review; Claude Code builds. Review `git diff {base}..HEAD` "
                f"against CLAUDE.md, the project's docs and the goal. Look hardest at correctness, security, "
                f"data integrity and races. For each finding give the rule it breaks, the "
                f"location and a concrete failing case. Fix only trivial things yourself (lint, typos); leave real "
                f"fixes to Claude Code in the handoff. Say done only when you approve the work as it stands; say "
                f"continue when there are findings to fix.\n")
    return (f"\n## Your role: builder\n\nYou build; Codex reviews. When the goal is met and you have checked it "
            f"yourself, say done: Codex then reviews the goal's diff. When Codex reports findings, confirm each one "
            f"first (a failing test or a live reproduction), then fix it with a regression test, or reject it with "
            f"the reason. Say done again when all are handled.\n")


def build_prompt(state, agent, human_msgs, check_feedback):
    work = state["worktree"]
    commits = git(work, "log", "--oneline", f"{state['base']}..HEAD") or "(none yet)"
    feedback = ("\n## Orchestrator check\n\n" + check_feedback.strip() + "\n") if check_feedback else ""
    feedback += role_block(state, agent)
    away = unavailable_until(OTHER[agent]) if away_for_long(OTHER[agent]) else None
    if away:
        feedback += (f"\n## {NAMES[OTHER[agent]]} is away\n\n{NAMES[OTHER[agent]]} hit its usage limit and is out "
                     f"until {away:%H:%M}. Work alone. Review your own last commit with fresh eyes as {NAMES[OTHER[agent]]} "
                     f"would. Done counts only from a turn that changes nothing, so build in one turn and confirm "
                     f"in the next; do not keep taking turns just to wait. Record anything that still needs an independent "
                     f"review in the repository (an open item in the relevant README or review notes), so the next "
                     f"goal picks it up.\n")
    return TURN_PROMPT.read_text().format(
        me=NAMES[agent], other=NAMES[OTHER[agent]], goal=state["goal"].strip(),
        turn=state["turn"], max_turns=state["max_turns"], branch=state["branch"],
        base=state["base"][:10], log=commits, transcript=render_history(state["history"]),
        human=owner_block(state.get("owner_messages", []), bool(human_msgs)), check_feedback=feedback,
    )


# ---------- agents ----------

def parse_result(text, key):
    """Accept a bare JSON object or the last fenced JSON block in free text."""
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and key in obj:
            return obj
    except json.JSONDecodeError:
        pass
    for block in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)):
        try:
            obj = json.loads(block)
            if key in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def prompt_images(prompt):
    """Image files the prompt names as [image: PATH] that still exist, once each, in order."""
    found = []
    for m in IMAGE_MARK.finditer(prompt):
        p = pathlib.Path(m.group(1))
        if p.suffix.lower() in IMAGE_EXTS and p.is_file() and p not in found:
            found.append(p)
    return found


def run_claude(cfg, worktree, prompt, out_base, schema, key, readonly):
    cmd = ["claude", "-p"]
    images = prompt_images(prompt)
    if images:
        # Read reaches files outside the worktree only through --add-dir. It takes several values,
        # so it goes before the next flag, never last.
        cmd += ["--add-dir", *dict.fromkeys(str(p.parent) for p in images)]
        prompt += "\n\nEach [image: PATH] above is an image from the owner. Open it with Read before you start.\n"
    cmd += ["--output-format", "json", "--json-schema", schema.read_text(),
            "--setting-sources", "project,local"]
    if readonly:
        cmd += ["--permission-mode", "dontAsk", "--allowedTools", *CLAUDE_READ]
    else:
        cmd += ["--permission-mode", cfg["claude_permission_mode"],
                "--allowedTools", *CLAUDE_ALLOWED, "--disallowedTools", *CLAUDE_DISALLOWED]
    if cfg.get("claude_model"):
        cmd += ["--model", cfg["claude_model"]]
    if cfg.get("claude_budget"):
        cmd += ["--max-budget-usd", str(cfg["claude_budget"])]
    proc = run_child(cmd, input=prompt, cwd=worktree, timeout=cfg["timeout"])
    out_base.with_suffix(".json").write_text(proc.stdout)
    out_base.with_suffix(".stderr").write_text(proc.stderr)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"exit {proc.returncode}, no JSON output: {tail(proc.stderr or proc.stdout)}")
    if payload.get("is_error"):
        raise RuntimeError(f"{payload.get('subtype')}: {tail(str(payload.get('result')))}")
    result = payload.get("structured_output") or parse_result(payload.get("result"), key)
    return result, payload.get("total_cost_usd") or 0.0


def run_codex(cfg, worktree, prompt, out_base, schema, key, readonly):
    last = out_base.with_suffix(".last.json")
    cmd = ["codex", "exec"]
    images = prompt_images(prompt)
    if images:
        # --image takes several values and would swallow the trailing "-" (prompt on stdin), so it
        # goes first, closed off by -C.
        cmd += ["--image", *map(str, images)]
        prompt += "\n\nEach [image: PATH] above is an image from the owner, attached to this message in that order.\n"
    cmd += ["-C", worktree, "-s", "read-only" if readonly else "workspace-write",
           "-c", 'approval_policy="never"', "--output-schema", str(schema), "-o", str(last),
           "--color", "never"]
    if cfg.get("codex_network") and not readonly:
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    if cfg.get("codex_model"):
        cmd += ["-m", cfg["codex_model"]]
    if cfg.get("codex_effort"):
        cmd += ["-c", f'model_reasoning_effort="{cfg["codex_effort"]}"']
    cmd.append("-")
    proc = run_child(cmd, input=prompt, cwd=worktree, timeout=cfg["timeout"])
    out_base.with_suffix(".log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    if proc.returncode != 0:
        errors = [l for l in (proc.stderr + "\n" + proc.stdout).splitlines() if l.startswith("ERROR")]
        raise RuntimeError(f"exit {proc.returncode}: {tail(errors[-1] if errors else proc.stderr or proc.stdout)}")
    return parse_result(last.read_text() if last.exists() else proc.stdout, key), 0.0


RUNNERS = {"claude": run_claude, "codex": run_codex}


# ---------- one run: git per turn ----------

def commit_turn(state, agent, turn, summary, failed):
    work = state["worktree"]
    if not git(work, "status", "--porcelain"):
        return None
    git(work, "add", "-A")
    title = (summary or "no summary").splitlines()[0][:72]
    label = f"{state['name']} " if state.get("name") else ""
    msg = f"tandem({agent}): {label}turn {turn}{' (failed)' if failed else ''} - {title}"
    git(work, "commit", "-q", "--no-verify", "-m", msg)
    return git(work, "rev-parse", "--short", "HEAD")


def run_check(state):
    cmd = state["config"].get("check")
    if not cmd:
        return True, ""
    proc = run_child(cmd, shell=True, cwd=state["worktree"], timeout=state["config"]["timeout"])
    return proc.returncode == 0, tail(proc.stdout + proc.stderr, 2000)


# ---------- one run: the loop ----------

# /say and the worker share this lock so a message cannot arrive between reading and clearing.
INBOX_LOCK = threading.Lock()


def take_inbox(paths):
    msgs = []
    with INBOX_LOCK:
        for p in map(pathlib.Path, paths):
            text = p.read_text().strip() if p.exists() else ""
            if text:
                msgs.append(text)
                p.write_text("")
    return "\n\n".join(msgs)


def stop_requested(state):
    return bool(state.get("stop_file")) and pathlib.Path(state["stop_file"]).exists()


def finish(run_dir, state, outcome, message):
    state["outcome"] = outcome
    state["outcome_message"] = message
    save(run_dir, state)
    append_transcript(run_dir, f"\n## Finished: {outcome}\n\n{message}\n")
    log(f"FINISHED {state.get('name') or run_dir.name}: {outcome} - {message}")
    log(f"transcript {run_dir / 'transcript.md'}")


PROGRESS = {}  # turn, agent, since: what the interactive status line shows
CURRENT = {}   # target: the run or campaign directory the session's /say, /stop and /status act on


def loop(run_dir):
    state = load(run_dir)
    state.pop("outcome", None)
    cfg = state["config"]
    inboxes = state.get("inboxes") or [str(run_dir / "inbox.md")]
    while True:
        if stop_requested(state):
            return finish(run_dir, state, "stopped", "The owner asked to stop.")
        if state["turn"] > state["max_turns"]:
            return finish(run_dir, state, "max_turns", "Turn limit reached before both agents agreed.")

        healthy = [a for a in ("claude", "codex") if state["failures"][a] < MAX_FAILURES_PER_AGENT]
        if not healthy:
            return finish(run_dir, state, "failed", "Both agents failed repeatedly; see the .stderr/.log files.")
        ready = [a for a in healthy if not unavailable_until(a)]
        if not ready:
            wake = min(unavailable_until(a) for a in healthy)
            log(f"no agent available (usage limits); waiting until {wake:%H:%M}")
            wait_until(wake, state.get("stop_file"))
            continue
        if state.get("review_goal") and "codex" in healthy and away_for_long("codex"):
            # Claude must not review its own work: park the review until Codex is back.
            return finish(run_dir, state, "parked", f"Codex is away until {unavailable_until('codex'):%H:%M}; "
                                                    "the review goes back on the backlog.")
        wanted = state["next"]
        away = unavailable_until(wanted) if wanted in healthy else None
        must_wait = not state["history"] or (reviewer_mode(cfg) and wanted == "codex")
        if must_wait and away and not away_for_long(wanted):
            # A short wait beats the author reviewing itself.
            log(f"{NAMES[wanted]} is needed next and is back at {away:%H:%M}; waiting")
            wait_until(away, state.get("stop_file"))
            continue
        agent = wanted if wanted in ready else ready[0]

        human = take_inbox(inboxes)
        if human:
            state.setdefault("owner_messages", []).append({"at": now(), "text": human})
            save(run_dir, state)
            append_transcript(run_dir, f"\n### Owner, before turn {state['turn']}\n\n{human}\n")
        prompt = build_prompt(state, agent, human, state.pop("check_feedback", ""))
        turn = state["turn"]
        out_base = run_dir / f"turn-{turn:02d}-{agent}"
        out_base.with_suffix(".prompt.md").write_text(prompt)

        log(f"turn {turn}: {NAMES[agent]} working...")
        started = time.time()
        with OUT_LOCK:
            PROGRESS.update(turn=turn, agent=agent, since=started)
        entry = {"turn": turn, "agent": agent, "started": now()}
        try:
            result, cost = call_agent(agent, cfg, state["worktree"], prompt, out_base, TURN_SCHEMA, "status",
                                      False, state.get("stop_file"))
            if not result:
                raise RuntimeError("no structured result in the agent's reply")
            entry.update({k: str(result.get(k, "")).strip() for k in
                          ("status", "summary", "handoff", "could_not_do", "checks")})
            state["cost_usd"] += cost
            state["failures"][agent] = 0
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            err = f"timed out after {cfg['timeout']}s" if isinstance(e, subprocess.TimeoutExpired) else str(e)
            limited = unavailable_until(agent)
            if limited:
                handoff = (f"{NAMES[agent]} hit its usage limit and is out until {limited:%H:%M}. "
                           f"Work alone until then; you may finish the goal alone if the check passes.")
            else:
                state["failures"][agent] += 1
                handoff = f"{NAMES[agent]} failed this turn; continue the goal yourself."
            entry.update({"status": "error", "error": err, "summary": "", "handoff": handoff})
        entry["seconds"] = round(time.time() - started)
        entry["commit"] = commit_turn(state, agent, turn, entry.get("summary") or entry.get("error"),
                                      entry["status"] == "error")
        state["history"].append(entry)
        state["turn"] += 1
        if reviewer_mode(cfg) and entry["status"] != "error":
            # Claude builds until it says done; then Codex reviews; findings go back to Claude.
            state["next"] = ("codex" if entry["status"] == "done" else "claude") if agent == "claude" else "claude"
        else:
            state["next"] = OTHER[agent]
        save(run_dir, state)

        append_transcript(run_dir, render_history([entry]) + f"_{entry['seconds']}s_\n")
        log(f"turn {turn}: {NAMES[agent]} -> {entry['status']} ({entry['seconds']}s"
            f"{', commit ' + entry['commit'] if entry['commit'] else ''}) {entry.get('summary') or entry.get('error', '')}")

        status = entry["status"]
        prev = state["history"][-2] if len(state["history"]) > 1 else None
        other = OTHER[agent]
        solo = state["failures"][other] >= MAX_FAILURES_PER_AGENT or away_for_long(other)

        if status == "done":
            peer_agreed = prev and prev["agent"] != agent and prev["status"] == "done" and not entry["commit"]
            if reviewer_mode(cfg) and agent == "codex" and not entry["commit"]:
                peer_agreed = True  # the reviewer approving unchanged work closes the goal
            # Alone, the agent still needs one fresh-eyes turn that changes nothing before done counts.
            if peer_agreed or (solo and not entry["commit"]):
                ok, output = run_check(state)
                if ok:
                    if not peer_agreed:
                        state["needs_review"] = True
                    who = "Both agents agree" if peer_agreed else f"{NAMES[agent]} alone ({NAMES[other]} away) says"
                    return finish(run_dir, state, "done",
                                  f"{who} the goal is met" + (" and the check passed." if cfg.get("check") else "."))
                state["check_feedback"] = (f"The agents said done, but `{cfg['check']}` failed. "
                                           f"Fix it before saying done again.\n\n```\n{output}\n```")
                log("check failed, continuing")
        elif status == "blocked" and prev and prev["status"] == "blocked":
            return finish(run_dir, state, "blocked", "Both agents need the owner:\n\n" + entry["handoff"])


def create_run(run_dir, repo, goal, name, cfg, max_turns, first, worktree=None, branch=None, extra=None):
    """New run in run_dir; reuses worktree and branch when given, otherwise makes a fresh pair."""
    run_dir.mkdir(parents=True)
    if worktree is None:
        branch = f"tandem/{run_dir.name}"
        worktree = str(run_dir / "work")
        git(repo, "worktree", "add", "-q", "-b", branch, worktree, git(repo, "rev-parse", cfg.pop("base", "HEAD")))
    state = {
        "name": name, "goal": goal, "repo": str(repo), "worktree": str(worktree), "branch": branch,
        "base": git(worktree, "rev-parse", "HEAD"), "turn": 1, "max_turns": max_turns, "next": first,
        "failures": {"claude": 0, "codex": 0}, "cost_usd": 0.0, "history": [], "config": cfg,
        "inboxes": [str(run_dir / "inbox.md")],
    }
    state.update(extra or {})
    save(run_dir, state)
    (run_dir / "inbox.md").write_text("")
    append_transcript(run_dir, f"# Tandem run {run_dir.name}\n\nRepo: {repo}\nBranch: {branch}\n\n"
                               f"## Goal\n\n{goal.strip()}\n\n## Turns\n")
    log(f"run {run_dir.name}: {name} on {branch}")
    return run_dir


def config_from(args):
    return {
        "check": getattr(args, "check", None), "timeout": args.timeout,
        "claude_model": args.claude_model, "claude_budget": args.claude_budget,
        "claude_permission_mode": args.claude_permission_mode,
        "codex_model": args.codex_model, "codex_effort": args.codex_effort,
        "codex_network": args.codex_network, "codex_role": args.codex_role,
    }


# ---------- campaign ----------

def plan_next(camp_dir, camp):
    """Ask one agent (read-only) for the next goal; the other agent tries if it fails."""
    outcome_text = {"done": "done, both agents agreed", "max_turns": "unfinished, ran out of turns",
                    "blocked": "blocked, see below", "failed": "failed, agents kept erroring"}
    done = "\n".join(f"- {r['name']}: {outcome_text.get(r['outcome'], r['outcome'])}"
                     for r in camp["runs"]) or "(nothing yet)"
    blockers = "\n".join(f"- {b}" for b in camp["blockers"]) or "(none)"
    human = take_inbox([camp_dir / "inbox.md"])
    if human:
        camp.setdefault("owner_notes", []).append(human)
    lasting = camp_dir / "notes.md"
    notes = "\n\n".join(camp.get("owner_notes", []) + ([lasting.read_text().strip()] if lasting.exists() else [])) or "(none)"
    for planner in (camp["next_planner"], OTHER[camp["next_planner"]]):
        if unavailable_until(planner):
            continue
        prompt = PLAN_PROMPT.read_text().format(me=NAMES[planner], done=done, blockers=blockers, notes=notes)
        n = len(list(camp_dir.glob("plan-*.prompt.md"))) + 1
        out_base = camp_dir / f"plan-{n:02d}-{planner}"
        out_base.with_suffix(".prompt.md").write_text(prompt)
        log(f"planning the next goal: {NAMES[planner]}")
        try:
            result, cost = call_agent(planner, camp["config"], camp["worktree"], prompt, out_base,
                                      PLAN_SCHEMA, "finished", True, camp_dir / "STOP")
            camp["cost_usd"] += cost
            if result:
                camp["next_planner"] = OTHER[planner]
                return result
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log(f"planner {NAMES[planner]} failed: {tail(str(e), 200)}")
    return None


def review_item(camp):
    """A goal for Codex to review everything Claude finished alone while Codex was away."""
    items = camp["review_backlog"]
    start = items[0]["from"]
    lines = "\n".join(f"- {i['name']}: `{i['from'][:10]}..{i['to'][:10]}`" + (f" ({i['note']})" if i.get("note") else "")
                      for i in items)
    goal = (f"Independent review by Codex of work Claude Code finished while Codex was away:\n\n{lines}\n\n"
            f"Codex reviews first: the diffs above (`git diff {start[:10]}..HEAD` covers all of it), CLAUDE.md, the "
            f"relevant parts of the project's docs, and any open review items in the READMEs or review notes. "
            f"Record each finding with the rule it breaks, the location "
            f"and a concrete failing case, in the review notes for that part of the code.\n"
            f"Claude Code confirms each finding (failing test or live reproduction), fixes it with a regression test or "
            f"rejects it with the reason, and Codex re-reviews the fixes.\n\n"
            f"Done means: every finding is fixed or rejected with a reason, Codex approves, and the open "
            f"'pending Codex review' items covered here are marked reviewed.")
    return {"goal": goal, "name": "codex-review-backlog", "first": "codex", "check": "",
            "review": True, "review_from": start, "max_turns": REVIEW_MAX_TURNS}


def push_branch(camp):
    """Publish the campaign branch after a goal; a failed push is logged, never fatal."""
    branch = camp["branch"]
    if branch in ("main", "master"):
        log(f"not pushing {branch}: campaigns never push the default branch")
        return
    proc = subprocess.run(["git", "push", "origin", f"{branch}:{branch}"], cwd=camp["repo"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, **CHILD_POPEN)
    if proc.returncode == 0:
        log(f"pushed {branch} to origin at {git(camp['worktree'], 'rev-parse', '--short', 'HEAD')}")
    else:
        log(f"push failed, continuing: {tail(proc.stderr, 300)}")


def refuse_if_running(camp_dir):
    lock = camp_dir / ".lock"
    if not lock.exists():
        return
    try:
        pid = int(lock.read_text())
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return
    sys.exit(f"campaign already running (pid {pid}); stop it first")


def campaign_loop(camp_dir):
    """One process per campaign: a second one would plan and commit over the first."""
    refuse_if_running(camp_dir)
    lock = camp_dir / ".lock"
    lock.write_text(str(os.getpid()))
    try:
        _campaign_loop(camp_dir)
    finally:
        lock.unlink(missing_ok=True)


def _campaign_loop(camp_dir):
    path = camp_dir / "campaign.json"
    camp = json.loads(path.read_text())
    stop_file = camp_dir / "STOP"
    while True:
        if stop_file.exists():
            log("campaign stopped by the owner")
            return
        current = camp.get("current")
        if current:
            run_dir = pathlib.Path(current)
            state = load(run_dir)
            waits = 0
            while True:
                loop(run_dir)
                state = load(run_dir)
                if state["outcome"] != "failed" or waits >= MAX_FAILED_WAITS or stop_file.exists():
                    break
                waits += 1
                log(f"both agents failing, probably a usage limit; waiting {FAILED_RUN_WAIT // 60} min ({waits}/{MAX_FAILED_WAITS})")
                if not wait_until(datetime.datetime.now() + datetime.timedelta(seconds=FAILED_RUN_WAIT), stop_file):
                    break
                state["failures"] = {"claude": 0, "codex": 0}
                save(run_dir, state)
            outcome = state["outcome"]
            if outcome == "stopped":
                return
            camp["runs"].append({"dir": str(run_dir), "name": state.get("name") or run_dir.name,
                                 "outcome": outcome, "turns": state["turn"] - 1})
            camp["cost_usd"] += state["cost_usd"]
            head = git(camp["worktree"], "rev-parse", "HEAD")
            backlog = camp.setdefault("review_backlog", [])
            if state.get("needs_review"):
                backlog.append({"name": state.get("name") or run_dir.name, "from": state["base"], "to": head})
                log(f"{state.get('name')} closed without Codex; added to the review backlog ({len(backlog)} items)")
            if outcome == "parked" or (state.get("review_goal") and outcome in ("max_turns", "failed")):
                backlog.append({"name": state.get("name") or run_dir.name, "from": state.get("review_from") or state["base"],
                                "to": head, "note": f"review unfinished ({outcome}); continue from the review notes"})
            elif outcome == "done":
                camp["no_progress"] = 0
            else:
                camp["no_progress"] += 1
                if outcome == "blocked":
                    camp["blockers"].append(f"{state.get('name')}: {tail(state.get('outcome_message'), 800)}")
                with open(camp_dir / "BLOCKERS.md", "a") as f:
                    f.write(f"## {state.get('name')} ({outcome}, {now()})\n\n{state.get('outcome_message')}\n\n")
            camp["current"] = None
            write_json(path, camp)
            if camp.get("push"):
                push_branch(camp)
            if outcome == "failed":
                log("campaign paused: both agents kept failing after waiting; resume later")
                return
            if camp["no_progress"] >= MAX_GOALS_WITHOUT_PROGRESS:
                log(f"campaign paused: {camp['no_progress']} goals in a row ended without progress; see BLOCKERS.md")
                return
            if stop_file.exists():
                log("campaign stopped by the owner")
                return

        if camp["queue"]:
            item = camp["queue"].pop(0)
        elif camp.get("review_backlog") and not away_for_long("codex"):
            item = review_item(camp)
            camp["review_backlog"] = []
            log(f"Codex is available: reviewing the backlog before building ({item['review_from'][:10]}..HEAD)")
        else:
            plan = plan_next(camp_dir, camp)
            write_json(path, camp)
            if plan is None:
                waits = [w for w in map(unavailable_until, NAMES) if w]
                wake = min(waits) if len(waits) == len(NAMES) else \
                    datetime.datetime.now() + datetime.timedelta(seconds=PLAN_RETRY_WAIT)
                log(f"neither agent could plan the next goal; retrying at {wake:%H:%M}")
                wait_until(wake, stop_file)
                continue
            if plan.get("finished"):
                log(f"campaign finished: {plan.get('reason')}")
                return
            item = {"goal": plan["goal"], "name": slug(plan["name"]) or "goal",
                    "first": plan.get("first") if plan.get("first") in NAMES else "claude",
                    "check": plan.get("check", "")}
            log(f"next goal: {item['name']} - {plan.get('reason')}")
        n = len(camp["runs"]) + 1
        cfg = dict(camp["config"], check=item.get("check") or None)
        run_dir = create_run(camp_dir / f"{n:02d}-{slug(item['name'])}", camp["repo"], item["goal"],
                             item["name"], cfg, item.get("max_turns", camp["max_turns"]), item.get("first", "claude"),
                             worktree=camp["worktree"], branch=camp["branch"],
                             extra={"stop_file": str(stop_file),
                                    **({"review_goal": True, "review_from": item["review_from"]} if item.get("review") else {}),
                                    "inboxes": [str(camp_dir / f"{n:02d}-{slug(item['name'])}" / "inbox.md"),
                                                str(camp_dir / "inbox.md")]})
        camp["current"] = str(run_dir)
        write_json(path, camp)


def prepare_campaign_start(args):
    camp_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{slug(args.name)}" if args.name else "")
    camp_dir = RUNS / f"campaign-{camp_id}"
    camp_dir.mkdir(parents=True)
    (camp_dir / "inbox.md").write_text("")
    cfg = config_from(args)
    camp = {"id": camp_id, "config": cfg, "max_turns": args.max_turns, "push": args.push, "queue": [], "runs": [],
            "blockers": [], "no_progress": 0, "next_planner": "claude", "cost_usd": 0.0, "current": None}
    if args.adopt:
        run_dir = run_path(args.adopt)
        state = load(run_dir)
        camp.update(repo=state["repo"], worktree=state["worktree"], branch=state["branch"])
        state.setdefault("name", run_dir.name)
        state["inboxes"] = [str(run_dir / "inbox.md"), str(camp_dir / "inbox.md")]
        state["stop_file"] = str(camp_dir / "STOP")
        state["max_turns"] = state["turn"] - 1 + args.max_turns
        state["failures"] = {"claude": 0, "codex": 0}
        state["config"].update({k: v for k, v in cfg.items() if v is not None and k != "check"})
        save(run_dir, state)
        camp["current"] = str(run_dir)
    else:
        repo = require_repo(args.repo)
        branch = f"tandem/campaign-{camp_id}"
        worktree = str(camp_dir / "work")
        git(repo, "worktree", "add", "-q", "-b", branch, worktree, git(repo, "rev-parse", args.base))
        camp.update(repo=str(repo), worktree=worktree, branch=branch)
    camp["queue"] = [read_goal_file(p) for p in args.goal_file or []]
    write_json(camp_dir / "campaign.json", camp)
    CURRENT["target"] = camp_dir
    log(f"campaign {camp_dir.name} on {camp['branch']} ({camp['worktree']})")
    log(f"talk: {prog_name()} campaign say {camp_dir} \"...\"   stop: {prog_name()} campaign stop {camp_dir}")
    return camp_dir


def cmd_campaign_start(args):
    campaign_loop(prepare_campaign_start(args))


def prepare_campaign_resume(args):
    camp_dir = run_path(args.dir)
    refuse_if_running(camp_dir)
    (camp_dir / "STOP").unlink(missing_ok=True)
    path = camp_dir / "campaign.json"
    camp = json.loads(path.read_text())
    camp["no_progress"] = 0
    if args.push is not None:
        camp["push"] = args.push
    changes = {k: v for k, v in (("codex_effort", args.codex_effort), ("codex_role", args.codex_role)) if v}
    camp["config"].update(changes)
    if args.seed_review:
        sha, _, note = args.seed_review.partition(":")
        camp.setdefault("review_backlog", []).append({"name": "seeded", "from": git(camp["worktree"], "rev-parse", sha),
                                                      "to": git(camp["worktree"], "rev-parse", "HEAD"), "note": note})
    if camp.get("current"):
        state = load(pathlib.Path(camp["current"]))
        state["config"].update(changes)
        if state.get("outcome"):
            state["max_turns"] = max(state["max_turns"], state["turn"] - 1 + camp["max_turns"] // 2)
        state["failures"] = {"claude": 0, "codex": 0}
        save(pathlib.Path(camp["current"]), state)
    write_json(path, camp)
    CURRENT["target"] = camp_dir
    return camp_dir


def cmd_campaign_resume(args):
    campaign_loop(prepare_campaign_resume(args))


def cmd_campaign_stop(args):
    (run_path(args.dir) / "STOP").write_text(now())
    emit("the campaign stops after the current turn")


def cmd_campaign_status(args):
    camp_dir = run_path(args.dir)
    camp = json.loads((camp_dir / "campaign.json").read_text())
    emit(f"branch {camp['branch']}  goals {len(camp['runs'])}  queued {len(camp['queue'])}  "
          f"claude cost ${camp['cost_usd']:.2f} (without the current goal)")
    for r in camp["runs"]:
        emit(f"  {r['outcome']:<9} {r['name']} ({r['turns']} turns)")
    if camp.get("current"):
        emit(f"  current: {camp['current']}")
        cmd_status(argparse.Namespace(run=camp["current"]))


# ---------- single-run commands ----------

def prepare_run(args):
    """Create a single run shared by the start command and the session entry points."""
    repo = require_repo(args.repo)
    item = read_goal_file(args.goal_file) if args.goal_file else {"goal": args.goal, "name": args.name}
    if not item["goal"]:
        sys.exit("give a goal: --goal TEXT or --goal-file PATH")
    name = args.name or item.get("name")
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{slug(name)}" if name else "")
    cfg = dict(config_from(args), base=args.base)
    cfg["check"] = args.check or item.get("check") or None
    run_dir = create_run(RUNS / run_id, repo, item["goal"], name or run_id,
                         cfg, args.max_turns, args.first or item.get("first") or "claude")
    CURRENT["target"] = run_dir
    log(f"talk to the agents: {prog_name()} say {run_dir} \"...\"")
    return run_dir


def cmd_start(args):
    run_dir = prepare_run(args)
    loop(run_dir)


def outcome_exit_code(outcome):
    return {"done": 0, "max_turns": 2, "blocked": 3, "stopped": 4, "failed": 5}.get(outcome, 1)


def headless_result(run_dir, state):
    history = state.get("history", [])
    return {
        "run_id": run_dir.name if run_dir else None,
        "name": state.get("name"), "repo": state.get("repo"), "branch": state.get("branch"),
        "base": state.get("base"), "outcome": state.get("outcome"),
        "outcome_message": state.get("outcome_message", ""), "turn_count": len(history),
        "cost_usd": state.get("cost_usd", 0.0),
        "transcript": str(run_dir / "transcript.md") if run_dir else None,
        "turns": [{k: h.get(k) for k in ("turn", "agent", "status", "commit", "seconds", "summary")}
                  for h in history],
    }


def cmd_headless(args):
    run_dir, state = None, {}
    # Keep stdout machine-readable even when setup fails. Only this synchronous entry point
    # redirects stdout; the interactive session must be able to keep reading and writing.
    with contextlib.ExitStack() as stack:
        if args.quiet:
            stream = stack.enter_context(open(os.devnull, "w"))
            stack.enter_context(contextlib.redirect_stdout(stream))
        elif args.output_format == "json":
            stack.enter_context(contextlib.redirect_stdout(sys.stderr))
        try:
            args.repo = require_repo(args.repo)
            if args.goal is not None:
                sys.exit("with -p, give GOAL directly, pipe it on stdin, or use --goal-file instead of --goal")
            if args.goal_file:
                if args.prompt != "-":
                    sys.exit("give one goal source: -p GOAL or -p --goal-file PATH")
            else:
                if args.prompt == "-":
                    if sys.stdin.isatty():
                        sys.exit("give a goal: -p GOAL, -p --goal-file PATH, or pipe the goal to -p")
                    args.goal = sys.stdin.read().strip()
                else:
                    args.goal = args.prompt.strip()
                if not args.goal:
                    sys.exit("goal is empty; give -p GOAL, --goal-file PATH, or a non-empty goal on stdin")
            run_dir = prepare_run(args)
            try:
                loop(run_dir)
            finally:
                # The saved state includes every completed turn, including after Ctrl+C.
                state = load(run_dir)
        except KeyboardInterrupt:
            message = "Interrupted by the owner."
            if run_dir:
                message += f" Continue with `{prog_name()} resume {run_dir}`."
                finish(run_dir, state, "stopped", message)
            else:
                state.update(outcome="stopped", outcome_message=message)
        except (GitError, OSError, ValueError, SystemExit) as exc:
            if isinstance(exc, SystemExit) and not isinstance(exc.code, str):
                raise
            state.update(outcome="error", outcome_message=str(exc))
            print(str(exc), file=sys.stderr)
        except Exception as exc:  # a bug must still leave one JSON object on stdout
            traceback.print_exc()
            state.update(outcome="error", outcome_message=f"{type(exc).__name__}: {exc}")

    result = headless_result(run_dir, state)
    if args.output_format == "json":
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"outcome: {result['outcome']} - {result['outcome_message']}")
        if run_dir:
            print(f"branch: {result['branch']}\ntranscript: {result['transcript']}")
    sys.exit(outcome_exit_code(result["outcome"]))


def cmd_resume(args):
    run_dir = run_path(args.run)
    state = load(run_dir)
    if args.more_turns:
        state["max_turns"] = state["turn"] - 1 + args.more_turns
    state["failures"] = {"claude": 0, "codex": 0}
    save(run_dir, state)
    # A STOP left by the interactive /stop would end the resumed run at once. Only clear the run's own
    # file; a run inside a campaign points at the campaign's STOP, which the owner controls.
    stop = state.get("stop_file")
    if stop and pathlib.Path(stop).resolve().parent == run_dir.resolve():
        pathlib.Path(stop).unlink(missing_ok=True)
    loop(run_dir)


def cmd_say(args):
    target = run_path(args.run)
    with INBOX_LOCK:
        with open(target / "inbox.md", "a") as f:
            f.write(args.message.strip() + "\n\n")
        if (target / "campaign.json").exists():
            # The next turn consumes the inbox; the planner keeps reading notes.md for every later goal.
            with open(target / "notes.md", "a") as f:
                f.write(f"- ({now()}) {args.message.strip()}\n")
    emit("queued for the next turn")


def cmd_status(args):
    run_dir = run_path(args.run)
    state = load(run_dir)
    emit(f"{state.get('name') or run_dir.name}: branch {state['branch']}  turn {state['turn'] - 1}/{state['max_turns']}  "
          f"outcome {state.get('outcome', 'running or interrupted')}  claude cost ${state['cost_usd']:.2f}")
    for h in state["history"]:
        emit(f"  {h['turn']:>2} {h['agent']:<6} {h['status']:<8} {h.get('commit') or '-':<8} "
              f"{(h.get('summary') or h.get('error') or '')[:100]}")


def list_runs(limit=None):
    """Runs and campaigns under RUNS, newest first. Run directories nested in a campaign are not listed."""
    found = []
    if RUNS.is_dir():
        for d in RUNS.iterdir():
            for fname, kind in (("state.json", "run"), ("campaign.json", "campaign")):
                f = d / fname
                if f.is_file():
                    try:
                        data = json.loads(f.read_text())
                        mtime = f.stat().st_mtime
                    except (OSError, ValueError):
                        continue
                    found.append((mtime, kind, d, data))
                    break
    found.sort(key=lambda x: x[0], reverse=True)
    rows = []
    for _, kind, d, data in found[:limit]:
        if kind == "run":
            outcome = data.get("outcome") or "running or interrupted"
            turns = max(data.get("turn", 1) - 1, 0)
            name = data.get("name") or d.name
        else:
            # A campaign has no outcome of its own; show how far it got and how the last goal ended.
            done = data.get("runs", [])
            outcome = f"campaign, {len(done)} goals" + (", last " + done[-1]["outcome"] if done and done[-1].get("outcome") else "")
            turns = sum(r.get("turns", 0) for r in data.get("runs", []))
            name = d.name
        rows.append({"id": d.name, "name": name, "branch": data.get("branch", "-"), "outcome": outcome,
                     "turns": turns, "path": str(d), "repo": data.get("repo")})
    return rows


def cmd_runs(args):
    rows = list_runs(args.limit)
    if not rows:
        emit(f"no runs yet in {RUNS}")
        return
    for r in rows:
        emit(f"{r['id']}  {r['name']}  {r['branch']}  {r['outcome']}  {r['turns']} turns  {r['path']}")


def check_runs_directory():
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        # Probe an actual write: permission bits alone miss ACLs and read-only mounts.
        with tempfile.TemporaryFile(dir=RUNS) as probe:
            probe.write(b"tandem\n")
            probe.flush()
    except OSError as exc:
        return False, f"{RUNS}: {exc}; set TANDEM_RUNS to a writable directory"
    return True, str(RUNS)


def cwd_repo_status(path):
    """(ok, detail) for doctor: is `path` a git repo with a commit? Same test as require_repo, without exiting."""
    try:
        if git(path, "rev-parse", "--is-inside-work-tree", check=False) != "true":
            return False, f"{path} is not a git repository; cd into one or run `git init`"
        if not git(path, "rev-parse", "--verify", "-q", "HEAD", check=False):
            return False, f"{path} has no commits yet; run git add -A && git commit -m init"
        branch = git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "detached HEAD"
    except OSError as exc:
        # Doctor must still print all check results when Git cannot even start.
        return False, f"cannot check {path}: {exc}; install git and ensure it is on PATH"
    return True, f"{path} on {branch}"


def cmd_doctor(args):
    """Check that this machine can run tandem: tools on PATH and both agents logged in."""
    def probe(cmd):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return proc.returncode, (proc.stdout + proc.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)

    checks = [("tandem", True, __version__)]
    checks.append(("python 3.9+", sys.version_info >= (3, 9), f"{sys.version.split()[0]} {sys.executable}"))
    if os.environ.get("TANDEM_NODE"):  # set by bin/tandem.js, absent from a source checkout
        checks.append(("node", True, os.environ["TANDEM_NODE"]))
    runs_ok, runs_detail = check_runs_directory()
    checks.append(("runs directory", runs_ok, runs_detail))
    code, out = probe(["git", "--version"])
    checks.append(("git", code == 0, out))
    if code == 0:
        who = [probe(["git", "config", key])[1] for key in ("user.name", "user.email")]
        checks.append(("git identity", all(who), " <".join(who) + ">" if all(who)
                       else "set git config --global user.name and user.email (turns are committed)"))
    code, out = probe(["claude", "--version"])
    checks.append(("claude on PATH", code == 0, out.splitlines()[0] if out else ""))
    if code == 0:
        code, out = probe(["claude", "auth", "status"])
        try:
            auth = json.loads(out)
            ok = bool(auth.get("loggedIn"))
            detail = f"{auth.get('authMethod')} {auth.get('email') or ''}".strip()
        except json.JSONDecodeError:
            ok, detail = code == 0, tail(out)
        checks.append(("claude logged in", ok, detail if ok else "run `claude auth login` or set ANTHROPIC_API_KEY"))
    code, out = probe(["codex", "--version"])
    checks.append(("codex on PATH", code == 0, out.splitlines()[0] if out else ""))
    if code == 0:
        code, out = probe(["codex", "login", "status"])
        checks.append(("codex logged in", code == 0, out if code == 0 else "run `codex login`"))
    checks.append(("current repo",) + cwd_repo_status(os.getcwd()))
    for name, ok, detail in checks:
        emit(f"  {'ok  ' if ok else 'FAIL'}  {name:<18} {detail}")
    if not all(ok for _, ok, _ in checks):
        sys.exit(1)
    emit("ready")


def add_agent_options(p, max_turns):
    p.add_argument("--max-turns", type=int, default=max_turns, help="turns per goal")
    p.add_argument("--timeout", type=int, default=1800, help="seconds per turn")
    p.add_argument("--claude-model")
    p.add_argument("--claude-budget", type=float, help="max USD per Claude turn")
    p.add_argument("--claude-permission-mode", default="acceptEdits")
    p.add_argument("--codex-model")
    p.add_argument("--codex-effort", choices=EFFORTS, help="Codex reasoning effort (default: ~/.codex/config.toml)")
    p.add_argument("--codex-role", choices=["peer", "reviewer"], default="peer",
                   help="peer: agents alternate; reviewer: Claude builds, Codex reviews at the end of each goal")
    p.add_argument("--codex-network", action="store_true", help="let Codex's sandbox reach the network")


def add_start_options(p):
    p.add_argument("--repo", default=os.getcwd(), help="git repository to work on (default: current directory)")
    p.add_argument("--goal")
    p.add_argument("--goal-file")
    p.add_argument("--name", help="short label added to the run id and branch")
    p.add_argument("--base", default="HEAD", help="commit or branch to start from")
    p.add_argument("--first", choices=["claude", "codex"], help="who starts (default: the goal file's first:, else claude)")
    p.add_argument("--check", help="shell command that must pass before the run counts as done")
    add_agent_options(p, 12)


HELP_DESCRIPTION = """\
Claude Code and Codex take turns on your git repository until they agree the goal is done.

usage shapes:
  %(prog)s                 interactive session in the current repo (needs a terminal)
  %(prog)s -p "GOAL"       one run, then exit; the exit code says how it ended
  %(prog)s COMMAND ...     one of the commands below
"""

HELP_EPILOG = """\
-p takes the options of `start`: --repo --check --max-turns --first --name --base,
--claude-model --codex-model --codex-effort --codex-role --codex-network --claude-budget --timeout.
"""


class TandemParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if CONSOLE and message:
            emit(message.rstrip())
        else:
            super()._print_message(message, file)

    def error(self, message):
        # Exit 2 belongs to a completed headless run that exhausted its turn budget.
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")

    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if getattr(parsed, "cmd", None) and parsed.prompt is not None:
            self.error("-p cannot be combined with a command")
        return parsed


def build_parser():
    p = TandemParser(prog=prog_name(), formatter_class=argparse.RawDescriptionHelpFormatter,
                     description=HELP_DESCRIPTION, epilog=HELP_EPILOG)
    p.add_argument("-V", "--version", action="version", version=f"tandem {__version__}")
    p.add_argument("-p", "--print", dest="prompt", nargs="?", const="-", metavar="GOAL",
                   help="headless: run GOAL (or the goal on stdin, or --goal-file) and exit")
    p.add_argument("--output-format", choices=["text", "json"], default="text", help="with -p")
    p.add_argument("--quiet", action="store_true", help="with -p: no turn stream")
    # Options for -p sit at the top level; `start` repeats them after the command.
    shown = len(p._actions)
    add_start_options(p)
    for action in p._actions[shown:]:  # keep the front-door help short; the epilog names them
        action.help = argparse.SUPPRESS
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("start", help="start a single-goal run")
    add_start_options(s)
    s.set_defaults(fn=cmd_start)

    r = sub.add_parser("resume", help="continue an interrupted or finished run")
    r.add_argument("run")
    r.add_argument("--more-turns", type=int, help="allow this many more turns")
    r.set_defaults(fn=cmd_resume)

    m = sub.add_parser("say", help="leave a message for the next turn (run or campaign directory)")
    m.add_argument("run")
    m.add_argument("message")
    m.set_defaults(fn=cmd_say)

    t = sub.add_parser("status", help="one line per turn")
    t.add_argument("run")
    t.set_defaults(fn=cmd_status)

    d = sub.add_parser("doctor", help="check tools and logins before the first run")
    d.set_defaults(fn=cmd_doctor)

    u = sub.add_parser("runs", help="recent runs and campaigns, newest first")
    u.add_argument("--limit", type=int, default=10)
    u.set_defaults(fn=cmd_runs)

    c = sub.add_parser("campaign", help="chain goals on one branch until the plan is done")
    csub = c.add_subparsers(dest="ccmd", required=True)
    cs = csub.add_parser("start")
    cs.add_argument("--repo", default=os.getcwd(), help="default: current directory; ignored with --adopt")
    cs.add_argument("--adopt", help="continue an existing run directory as the first goal")
    cs.add_argument("--goal-file", action="append", help="queue a goal before planning (repeatable)")
    cs.add_argument("--name")
    cs.add_argument("--base", default="HEAD")
    cs.add_argument("--push", action="store_true", help="push the branch to origin after every goal")
    add_agent_options(cs, 12)
    cs.set_defaults(fn=cmd_campaign_start)
    for name, fn in (("resume", cmd_campaign_resume), ("stop", cmd_campaign_stop), ("status", cmd_campaign_status)):
        x = csub.add_parser(name)
        x.add_argument("dir")
        if name == "resume":
            x.add_argument("--push", action=argparse.BooleanOptionalAction, default=None,
                           help="turn pushing after every goal on or off")
            x.add_argument("--codex-effort", choices=EFFORTS, help="change Codex reasoning effort from now on")
            x.add_argument("--codex-role", choices=["peer", "reviewer"], help="change how Codex takes part from now on")
            x.add_argument("--seed-review", metavar="FROM_SHA:NOTE", help="add a review backlog item from FROM_SHA to HEAD")
        x.set_defaults(fn=fn)
    x = csub.add_parser("say")
    x.add_argument("run")
    x.add_argument("message")
    x.set_defaults(fn=cmd_say)

    return p


# ---------- interactive session ----------
#
# Implemented: the background-thread design. loop() runs in a worker thread while the main thread
# reads input, so `/say` reaches the next turn while the agents work. All output goes through
# emit() and OUT_LOCK; on a real terminal the Console clears the prompt line, prints, and redraws
# the prompt (plus a one-line "turn N: Agent working... 2m14s" status above it). I chose it over
# the Ctrl+C-interrupts-the-run fallback because the redraw is a few escape codes, and the worker's
# children run in their own session (CHILD_POPEN) so a Ctrl+C at the prompt cannot kill a turn in
# flight. The price is that the session must stop those children itself when it goes away.

SLASH_COMMANDS = ["help", "status", "say", "paste", "stop", "resume", "goal", "campaign", "runs",
                  "transcript", "doctor", "quit"]

SLASH_HELP = """\
type a goal to start a run in this repo; while it runs, type /say to talk to the agents
  /help                show this list
  /status              one line per turn of the current run
  /say TEXT            leave a message the next turn reads
  Ctrl+V               paste the clipboard's image into the line as [image #N]
  /paste               attach the clipboard's image to your next goal or /say
                       (or drag image files onto the terminal; /paste clear drops them)
  /stop                stop after the current turn
  /resume [TURNS]      continue the last run, optionally allowing TURNS more
  /goal FILE           start a run from a goal file
  /campaign ARGS...    same as `tandem campaign ARGS...` (start, resume, stop, status, say)
  /runs                recent runs and campaigns
  /transcript          print the transcript path
  /doctor              check tools and logins
  /quit                leave (also Ctrl+D)"""


def parse_input(line):
    """Classify a typed line: ("empty", "", ""), ("goal", text, ""), ("command", name, rest) or ("unknown", name, "")."""
    text = line.strip()
    if not text:
        return "empty", "", ""
    if not text.startswith("/"):
        return "goal", text, ""
    parts = text[1:].split(None, 1)
    name = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    if name in SLASH_COMMANDS:
        return "command", name, rest.strip()
    return "unknown", name, ""


def new_image_path(suffix):
    IMAGES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while (IMAGES / f"{stamp}-{n}{suffix}").exists():
        n += 1
    return IMAGES / f"{stamp}-{n}{suffix}"


# AppleScript writes the clipboard's PNG to the path given as its argument; it fails when there is none.
CLIPBOARD_SCRIPT = [
    "on run argv",
    "set f to open for access (POSIX file (item 1 of argv)) with write permission",
    "try",
    "write (the clipboard as «class PNGf») to f",
    "on error",
    "close access f",
    "error number 1",
    "end try",
    "close access f",
    "end run",
]


def paste_image():
    """Save the clipboard's image under IMAGES. Raises ValueError saying why there is none."""
    path = new_image_path(".png")
    if sys.platform == "darwin":
        cmd = ["osascript", *[a for line in CLIPBOARD_SCRIPT for a in ("-e", line)], str(path)]
        out = None
    elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        cmd, out = ["wl-paste", "--no-newline", "--type", "image/png"], path
    elif shutil.which("xclip"):
        cmd, out = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], path
    else:
        raise ValueError("no clipboard tool (macOS, wl-paste or xclip); drag the image file in instead")
    try:
        with open(out, "wb") if out else contextlib.nullcontext() as f:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=f or subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=10, **CHILD_POPEN)
        ok = proc.returncode == 0 and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        path.unlink(missing_ok=True)
        raise ValueError("no image on the clipboard")
    return path


INLINE_IMAGE = re.compile(r"\[image #(\d+)\]")


def bind_paste_key(complete):
    """Make Ctrl+V paste an image at the prompt, with GNU readline or macOS libedit.

    readline cannot run Python on a key press, but it runs the completer, so Ctrl+V is bound to
    completion (Tab stays a plain insert, as Python sets it). A macro that types a word and then
    Tab does not work: in Python's callback mode libedit plays one macro key per real key press."""
    try:
        import readline
    except ImportError:
        return
    readline.set_completer(complete)
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^V rl_complete")
    else:
        # GNU readline rebinds the tty's literal-next key (Ctrl+V) to quoted-insert on every prompt
        # unless told not to; erase, kill and word-rubout keep their default bindings.
        readline.parse_and_bind("set bind-tty-special-chars off")
        readline.parse_and_bind('"\\C-v": complete')


# A file dragged onto the terminal arrives as its path: quoted, or with spaces escaped by backslashes.
DROPPED = re.compile(r"(?<![^ \t])(?:'([^']+)'|\"([^\"]+)\"|((?:~|/)(?:\\.|[^ \t\\'\"])+))")


def take_dropped_images(line):
    """Split image paths dropped into the line from the rest. Returns (rest, [path, ...])."""
    found, rest, last = [], [], 0
    for m in DROPPED.finditer(line):
        raw = m.group(1) or m.group(2) or re.sub(r"\\(.)", r"\1", m.group(3))
        p = pathlib.Path(raw).expanduser()
        if p.suffix.lower() in IMAGE_EXTS and p.is_file():
            found.append(p)
            rest.append(line[last:m.start()])
            last = m.end()
    if not found:
        return line, []
    rest.append(line[last:])
    return re.sub(r"[ \t]{2,}", " ", "".join(rest)).strip(), found


def config_path():
    return pathlib.Path.home() / ".tandem" / "config.json"


def consent_recorded():
    try:
        return bool(json.loads(config_path().read_text()).get("consent"))
    except (OSError, ValueError, AttributeError):
        return False


def record_consent():
    path = config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["consent"] = {"version": __version__, "date": datetime.date.today().isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


CONSENT_TEXT = """\
Before the first run, one thing to know. The agents work on your machine as you:
  - Claude Code's allow-list is a guard rail, not a sandbox. `python3` and `npm` are on it and can
    run anything as this user.
  - Only Codex runs in a sandbox.
  - Each run works in its own git worktree and branch, but nothing stops an agent from touching
    files outside it.
Use a repository you can afford to have changed, and review the branch before merging."""


def ask_consent(read=input):
    """First interactive run on this machine: require 'yes' and remember it. Returns False on refusal."""
    if consent_recorded():
        return True
    print(CONSENT_TEXT)
    try:
        answer = read("Type yes to continue: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "yes":
        return False
    record_consent()
    return True


def agent_login_status(name):
    """Cheap banner probe: PATH presence plus the login command, each with a short timeout."""
    if not shutil.which(name):
        return "not found"
    cmd = ["claude", "auth", "status"] if name == "claude" else ["codex", "login", "status"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "login unknown"
    if name == "claude":
        try:
            return "logged in" if json.loads(proc.stdout).get("loggedIn") else "not logged in"
        except ValueError:
            return "login unknown"
    return "logged in" if proc.returncode == 0 else "not logged in"


# One letter per pair; colored from Claude orange to Codex blue on a terminal.
LOGO = [("▀█▀", " █ "), ("▄▀█", "█▀█"), ("█▄ █", "█ ▀█"), ("█▀▄", "█▄▀"), ("█▀▀", "██▄"), ("█▀▄▀█", "█ ▀ █")]
LOGO_COLORS = [208, 209, 215, 117, 111, 75]


def banner(repo, fancy=False):
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "?"
    # Both probes at once: a hung CLI costs one timeout before the prompt, not two.
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        logins = list(zip(("claude", "codex"), pool.map(agent_login_status, ("claude", "codex"))))
    if not fancy:
        lines = [f"tandem {__version__}", f"repo    {repo} ({branch})",
                 f"python  {sys.executable} {sys.version.split()[0]}"]
        lines += [f"{name:<7} {status}" for name, status in logins]
        lines.append("type a goal, Ctrl+V to paste an image, or /help")
        return "\n".join(lines)

    def paint(code, text):
        return f"\x1b[{code}m{text}\x1b[0m"

    home = str(pathlib.Path.home())
    where = "~" + str(repo)[len(home):] if str(repo) == home or str(repo).startswith(home + os.sep) else str(repo)
    rows = [" ".join(paint(f"1;38;5;{c}", letter[i]) for letter, c in zip(LOGO, LOGO_COLORS)) for i in (0, 1)]
    lines = ["", "  " + rows[0], "  " + rows[1] + "   " + paint("2", f"v{__version__}"), "",
             f"  {paint('2', 'repo   ')} {where} ({branch})"]
    for name, status in logins:
        color = "32" if status == "logged in" else "33" if status == "login unknown" else "31"
        lines.append(f"  {paint('2', f'{name:<7}')} {paint(color, '●')} {status}")
    lines += ["", "  " + paint("2", "type a goal · Ctrl+V pastes an image · /help"), ""]
    return "\n".join(lines)


def fancy_terminal():
    """Escape codes and the status line only on a real terminal, and never with NO_COLOR."""
    return (sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


class Console:
    """Prompt-aware output for the interactive session. write() is only called holding OUT_LOCK."""

    def __init__(self, fancy):
        self.fancy = fancy
        self.prompt = ""
        self.prompt_active = False
        self.status_shown = False
        self.running = False

    def status_text(self):
        if not PROGRESS:
            return "working..."
        secs = int(time.time() - PROGRESS["since"])
        return f"turn {PROGRESS['turn']}: {NAMES[PROGRESS['agent']]} working... {secs // 60}m{secs % 60:02d}s"

    def _buffer(self):
        try:
            import readline
        except ImportError:
            return ""
        # macOS libedit hands back the line just submitted while the new one is still empty, which
        # would redraw stale text after every log line. Redrawing nothing there is the lesser evil.
        if getattr(readline, "backend", "") == "editline" or "libedit" in (readline.__doc__ or ""):
            return ""
        return readline.get_line_buffer()

    def write(self, text):
        if not self.prompt_active:
            print(text, flush=True)
        elif not self.fancy:
            print("\n" + text, flush=True)
        else:
            out = "\r\x1b[K" + ("\x1b[1A\r\x1b[K" if self.status_shown else "") + text + "\n"
            self.status_shown = self.running
            if self.running:
                out += self.status_text() + "\n"
            sys.stdout.write(out + self.prompt + self._buffer())
            sys.stdout.flush()

    def tick(self):
        """Redraw the status line in place, without moving the cursor the user is typing at."""
        with OUT_LOCK:
            if self.fancy and self.running and self.prompt_active and self.status_shown:
                sys.stdout.write("\x1b7\x1b[1A\r\x1b[K" + self.status_text() + "\x1b8")
                sys.stdout.flush()

    def ask(self, prompt):
        # Print the prompt under the same lock as logs. input(prompt) prints outside Python's
        # lock, and macOS libedit does not call readline's pre-input hook to release it safely.
        # input() still provides line editing; history redraws may omit the prefix until a log.
        with OUT_LOCK:
            self.prompt = prompt
            self.status_shown = self.fancy and self.running
            if self.status_shown:
                print(self.status_text(), flush=True)
            sys.stdout.write(prompt)
            sys.stdout.flush()
            self.prompt_active = True
        try:
            return input()
        finally:
            with OUT_LOCK:
                self.prompt_active = False


class Session:
    def __init__(self, args, console):
        self.args = args
        self.console = console
        self.thread = None
        self.quitting = False
        self.images = []  # pasted or dropped, waiting for the next goal or /say
        self.inline = {}  # N -> path for each [image #N] Ctrl+V put in the line
        self.image_count = 0

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def refuse_if_busy(self):
        if self.busy():
            emit("a run is already going; /say talks to it, /stop ends it")
            return True
        return False

    def launch(self, label, fn):
        if self.refuse_if_busy():
            return
        with OUT_LOCK:
            PROGRESS.clear()

        def work():
            try:
                fn()
            except KeyboardInterrupt:
                pass
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    emit(f"{label}: {exc.code}")
            except Exception as exc:  # the session must survive a failing run
                emit(f"{label} failed: {exc}")
            finally:
                with OUT_LOCK:
                    self.console.running = False
                if not self.quitting:
                    emit(f"{label} ended; type a goal, /resume or /quit")

        with OUT_LOCK:
            self.console.running = True
        # A daemon, so a session ended by a signal is never held open by a turn it could not stop.
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def wait_for_worker(self, forced, pause):
        """Before leaving, let the turn in flight end; a signal, or Ctrl+C here, stops the agent instead."""
        if not self.busy():
            return
        if CURRENT.get("target"):
            (CURRENT["target"] / "STOP").write_text(now())
        if not forced:
            emit("waiting for the current turn to end; Ctrl+C stops the agent now (the turn is recorded as failed)")
        stopped_at = None
        while self.busy():
            if forced and stopped_at is None:
                signal_children()
                stopped_at = time.monotonic()
            try:
                pause(0.1)
            except KeyboardInterrupt:
                if stopped_at is None:
                    emit("stopping the agent")
                    forced = True
            except SessionEnd:
                forced = True
            if stopped_at is not None and time.monotonic() - stopped_at > KILL_GRACE:
                signal_children(hard=True)
                if time.monotonic() - stopped_at > 3 * KILL_GRACE:
                    break  # the worker is stuck outside an agent; its state is saved after each turn

    def attach(self, paths, copy=True):
        for src in paths:
            try:
                path = shutil.copyfile(src, new_image_path(src.suffix.lower())) if copy else src
            except OSError as exc:
                emit(f"cannot attach {src}: {exc}")
                continue
            self.images.append(pathlib.Path(path))
            self.image_count += 1
            emit(f"image #{self.image_count} attached ({pathlib.Path(src).name}); "
                 f"it goes with your next goal or /say")

    def complete(self, text, state):
        """readline completer, called for Ctrl+V (see bind_paste_key). text is the word before the cursor."""
        if state:
            return None
        before = text + " " if text else ""
        try:
            path = paste_image()
        except (ValueError, OSError):
            sys.stdout.write("\a")  # mid-line, the bell is the only thing that does not garble the prompt
            sys.stdout.flush()
            return before
        self.image_count += 1
        self.inline[self.image_count] = path
        return f"{before}[image #{self.image_count}] "

    def expand_inline(self, line):
        """Turn each [image #N] into [image: PATH]. A line of nothing but images waits for the next goal or /say."""
        used = []

        def swap(m):
            path = self.inline.get(int(m.group(1)))
            if path:
                used.append(path)
            return f"[image: {path}]" if path else m.group(0)

        line = INLINE_IMAGE.sub(swap, line)
        if used and not IMAGE_MARK.sub("", line).strip():
            self.images.extend(used)
            emit(f"{len(self.images)} image(s) waiting for your next goal or /say")
            return ""
        return line

    def with_images(self, text):
        """Name the waiting images in the message; prompt_images finds them there on every later turn."""
        return "\n\n".join([text, *(f"[image: {p}]" for p in self.images)]).strip()

    def paste(self, rest):
        if rest == "clear":
            emit(f"dropped {len(self.images)} image(s)")
            self.images.clear()
        elif rest:
            emit("usage: /paste [clear]")
        else:
            try:
                self.attach([paste_image()], copy=False)
            except (ValueError, OSError) as exc:
                emit(str(exc))

    def start_goal(self, text=None, goal_file=None):
        # Refuse before creating files or changing CURRENT: /say and /stop must keep targeting
        # the live run even when the user accidentally types another goal.
        if self.refuse_if_busy():
            return
        args = argparse.Namespace(**vars(self.args))
        args.goal, args.goal_file = (self.with_images(text) if text else text), goal_file
        try:
            run_dir = prepare_run(args)
        except (SystemExit, GitError, OSError, ValueError) as exc:
            emit(f"cannot start: {exc}")
            return
        state = load(run_dir)
        state["stop_file"] = str(run_dir / "STOP")
        save(run_dir, state)
        if text:
            self.images.clear()
        self.launch("run", lambda: loop(run_dir))

    def target(self, latest=False):
        target = CURRENT.get("target")
        repo = getattr(self.args, "repo", None)
        if not target and latest and repo:
            # A new session has no run of its own yet; /resume, /status and /transcript then mean the
            # newest run or campaign in this repo, so work from an earlier session is reachable.
            row = next((r for r in list_runs() if r.get("repo") == str(repo)), None)
            if row:
                target = CURRENT["target"] = pathlib.Path(row["path"])
                emit(f"using {row['id']}, the latest run in this repo")
        if not target:
            emit("no run yet; type a goal first")
        return target

    def request_stop(self):
        target = self.target()
        if not target:
            return
        # A campaign watches <dir>/STOP; a run watches the stop_file in its state (set in start_goal).
        stop = target / "STOP"
        stop.write_text(now())
        emit("stopping after the current turn")

    def handle(self, name, rest):
        """Run one slash command. Returns False to leave the session."""
        if name == "quit":
            return False
        if name == "help":
            emit(SLASH_HELP)
        elif name == "doctor":
            self.guarded(cmd_doctor, argparse.Namespace())
        elif name == "runs":
            cmd_runs(argparse.Namespace(limit=10))
        elif name == "goal":
            if not rest:
                emit("usage: /goal FILE")
            else:
                self.start_goal(goal_file=rest)
        elif name == "campaign":
            self.campaign(rest)
        elif name == "paste":
            self.paste(rest)
        elif name == "stop":
            self.guarded(lambda _: self.request_stop(), None)
        else:
            target = self.target(latest=name in ("resume", "status", "transcript"))
            if not target:
                return True
            if name == "say":
                if not rest and not self.images:
                    emit("usage: /say TEXT")
                elif self.guarded(cmd_say, argparse.Namespace(run=str(target), message=self.with_images(rest))):
                    self.images.clear()
            elif name == "status":
                fn = cmd_campaign_status if (target / "campaign.json").exists() else cmd_status
                self.guarded(fn, argparse.Namespace(run=str(target), dir=str(target)))
            elif name == "transcript":
                emit(str(self.transcript_path(target)))
            elif name == "resume":
                self.resume(target, rest)
        return True

    @staticmethod
    def transcript_path(target):
        """A run has its own transcript; a campaign has one per goal, so point at the running (or last) one."""
        if (target / "state.json").exists():
            return target / "transcript.md"
        try:
            camp = json.loads((target / "campaign.json").read_text())
        except (OSError, ValueError):
            return target
        run = camp.get("current") or (camp["runs"][-1]["dir"] if camp.get("runs") else None)
        return pathlib.Path(run) / "transcript.md" if run else target

    def guarded(self, fn, args):
        """Run a command, reporting its failure instead of ending the session. True when it succeeded."""
        try:
            fn(args)
            return True
        except SystemExit as exc:
            if exc.code not in (None, 0):
                emit(str(exc.code))
                return False
            return True
        except (GitError, OSError, ValueError) as exc:
            emit(str(exc))
        return False

    def resume(self, target, rest):
        if self.refuse_if_busy():
            return
        try:
            more = int(rest) if rest else None
            if more is not None and more <= 0:
                raise ValueError
        except ValueError:
            emit("usage: /resume [TURNS] (TURNS must be positive)")
            return
        if (target / "campaign.json").exists():
            if more is not None:
                emit("/resume TURNS applies to a single run; use /resume for a campaign")
                return
            self.campaign("resume " + shlex.quote(str(target)))
        else:
            try:
                (target / "STOP").unlink(missing_ok=True)
            except OSError as exc:
                emit(f"cannot resume: {exc}")
                return
            self.launch("run", lambda: cmd_resume(argparse.Namespace(run=str(target), more_turns=more)))

    def campaign(self, rest):
        try:
            argv = shlex.split(rest)
        except ValueError as exc:
            emit(f"cannot parse arguments: {exc}")
            return
        if not argv or argv[0] not in ("start", "resume", "stop", "status", "say"):
            emit("usage: /campaign start|resume|stop|status|say ...")
            return
        if argv[0] in ("start", "resume") and self.refuse_if_busy():
            return
        if argv[0] == "start":
            argv[1:1] = ["--repo", str(self.args.repo)]
        try:
            parsed = build_parser().parse_args(["campaign", *argv])
        except SystemExit:
            return
        if argv[0] in ("start", "resume"):
            # Establish the target before reading the next command, so an immediate /stop or
            # /quit cannot miss a campaign that is still being created in the worker.
            prepare = prepare_campaign_start if argv[0] == "start" else prepare_campaign_resume
            try:
                target = prepare(parsed)
            except (SystemExit, GitError, OSError, ValueError) as exc:
                emit(f"cannot {argv[0]} campaign: {exc}")
                return
            self.launch("campaign", lambda: campaign_loop(target))
        else:
            self.guarded(parsed.fn, parsed)


class SessionEnd(BaseException):
    """SIGHUP or SIGTERM reached the interactive session: leave now and take the agents along."""


def cmd_interactive(args):
    global CONSOLE
    args.repo = require_repo(args.repo)
    if not ask_consent():
        sys.exit("not confirmed; nothing started")
    try:
        import readline  # noqa: F401  # line editing and history for input(); absent on some builds
    except ImportError:
        pass
    # The logo and dots need escape codes and UTF-8; anything less gets the plain lines.
    print(banner(args.repo, fancy_terminal() and "utf" in (sys.stdout.encoding or "").lower()))
    console = Console(fancy_terminal())
    session = Session(args, console)
    bind_paste_key(session.complete)
    CHILD_POPEN["start_new_session"] = True
    CONSOLE = console

    # Agents run in their own session, out of reach of the terminal's hangup, so the session has to
    # stop them itself when the window closes or someone sends SIGTERM.
    ended_by = []

    def on_signal(signum, frame):
        if ended_by:
            return  # the npm launcher forwards what the terminal already sent; once is enough
        ended_by.append(signum)
        if signum == getattr(signal, "SIGHUP", None):
            sys.stdout = sys.stderr = open(os.devnull, "w")  # the terminal is gone
        raise SessionEnd()

    previous = {}
    for name in ("SIGHUP", "SIGTERM"):
        if hasattr(signal, name):
            previous[name] = signal.signal(getattr(signal, name), on_signal)

    ticker_stop = threading.Event()

    def ticker():
        while not ticker_stop.wait(1):
            console.tick()

    if console.fancy:
        threading.Thread(target=ticker, daemon=True).start()
    try:
        while True:
            try:
                line = console.ask("tandem> ")
            except EOFError:
                emit("")
                break
            except KeyboardInterrupt:
                emit("")  # Ctrl+C at the prompt clears the line; /quit or Ctrl+D leaves
                continue
            try:
                line, dropped = take_dropped_images(session.expand_inline(line))
                session.attach(dropped)
                kind, name, rest = parse_input(line)
                if kind == "goal":
                    session.start_goal(name)
                elif kind == "unknown":
                    emit(f"unknown command /{name}; valid: " + " ".join("/" + c for c in SLASH_COMMANDS))
                elif kind == "command" and not session.handle(name, rest):
                    break
            except KeyboardInterrupt:
                emit("interrupted")  # Ctrl+C cancels a slow command, never the session or its run
    except SessionEnd:
        pass
    finally:
        session.quitting = True
        # A detached agent left running would keep editing the worktree, and a later resume could
        # start a second agent there. Event waits also avoid interrupting Thread.join on 3.9.
        try:
            session.wait_for_worker(bool(ended_by), ticker_stop.wait)
        except SessionEnd:
            session.wait_for_worker(True, ticker_stop.wait)
        ticker_stop.set()
        CONSOLE = None
        CHILD_POPEN.clear()
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)
    if ended_by:
        sys.exit(128 + ended_by[0])


def pick_command(p, args):
    """Choose what to run: a subcommand, headless -p, or the interactive session."""
    if args.cmd:
        return args.fn
    if args.prompt is not None:
        return cmd_headless
    if not sys.stdin.isatty():
        sys.exit(f'no command and no terminal: use `{prog_name()} -p "GOAL"` for a one-shot run')
    return cmd_interactive


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    fn = pick_command(p, args)
    previous = None
    if fn is not cmd_interactive and threading.current_thread() is threading.main_thread():
        # SIGTERM (a supervisor, or the npm launcher passing one on) ends a run the way Ctrl+C does,
        # so run_child kills the agent instead of leaving it running in the worktree.
        previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        fn(args)
    except KeyboardInterrupt:
        print("\ninterrupted; continue with resume")
    except GitError as e:
        sys.exit(f"{e}\nfix it, then continue with resume")
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()

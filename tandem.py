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
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import unicodedata

try:
    import termios  # raw keys for the prompt and the /models picker; Windows has none and gets readline
except ImportError:
    termios = None

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
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
# The settings a saved default and a mid-run change can both touch; nothing else is stored or tuned.
TUNABLE = ("claude_model", "claude_effort", "codex_model", "codex_effort")
# What tuning.json may change mid-run: the models, plus the spending cap that /budget sets.
TUNING_KEYS = TUNABLE + ("max_cost",)
BUDGET_WARN = 0.8  # share of --max-cost after which the log says, once, that the cap is near
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
MAX_QUEUED = 5                  # owner messages waiting for one turn; the agent reads them all at once

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
PIPE_GRACE = 2  # seconds an exited agent's pipes may stay open while the rest of its output drains


def run_child(cmd, input=None, timeout=None, kind="agent", on_line=None, **kwargs):
    """subprocess.run for the long calls (agents, the check), tracked in CHILDREN while they run.

    on_line(stream, line), when given, sees each line of stdout and stderr as it arrives."""
    with subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          **CHILD_POPEN, **kwargs) as proc:
        # In its own session the process leads a group, and killing the group takes its tools along.
        proc.tandem_group = bool(CHILD_POPEN.get("start_new_session")) and hasattr(os, "killpg")
        proc.tandem_kind = kind
        proc.tandem_done = False
        with CHILDREN_LOCK:
            CHILDREN.add(proc)
        try:
            if on_line is None:
                out, err = proc.communicate(input, timeout=timeout)
            else:
                out, err = follow_output(proc, input, timeout, on_line)
        except BaseException:
            signal_child(proc, hard=True)
            proc.wait()
            raise
        finally:
            # A late signal_child (the interrupt's second try) must not reach a group id reused by now.
            proc.tandem_done = True
            with CHILDREN_LOCK:
                CHILDREN.discard(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def follow_output(proc, input, timeout, on_line):
    """communicate() that also hands every line to on_line while the process runs.

    Bounded by timeout like communicate(), even when a tool the agent started keeps its pipes open."""
    deadline = None if timeout is None else time.monotonic() + timeout
    lines = {"stdout": [], "stderr": []}
    pipes = {"stdout": proc.stdout, "stderr": proc.stderr}
    stdin = proc.stdin
    # The threads own the pipes from here. Popen's exit would close a pipe a reader is blocked on,
    # and that close waits for the read: forever, while a leftover tool still holds the other end.
    proc.stdin = proc.stdout = proc.stderr = None
    failed = []

    def left(limit=None):
        ends = [t for t in (deadline, limit) if t is not None]
        return max(min(ends) - time.monotonic(), 0) if ends else None

    def pump(name):
        # A killed agent's pipe can close under the reader; a traceback here would garble the session.
        with contextlib.suppress(ValueError, OSError):
            with pipes[name]:
                for line in pipes[name]:
                    lines[name].append(line)
                    try:
                        on_line(name, line.rstrip("\n"))
                    except Exception:  # a broken activity log must never cost the agent's output
                        pass

    def feed():
        # Off the calling thread: a CLI that stalls before reading a prompt bigger than the pipe
        # buffer would otherwise block the call before the timeout is ever looked at.
        try:
            with stdin:
                stdin.write(input)
        except OSError:
            pass  # the agent exited or was stopped without reading it all
        except Exception as exc:
            failed.append(exc)

    readers = [threading.Thread(target=pump, args=(name,), daemon=True) for name in lines]
    if stdin:
        readers.append(threading.Thread(target=feed, daemon=True))
    for reader in readers:
        reader.start()
    proc.wait(left())  # TimeoutExpired here: run_child stops the agent and its group
    # The agent has exited, so its reply is in the pipes. Anything still holding them is a tool it
    # left running; in its own group it goes now, outside one it is left with the pipe it holds.
    grace = time.monotonic() + PIPE_GRACE
    for reader in readers:
        reader.join(left(grace))
    if any(reader.is_alive() for reader in readers):
        signal_child(proc, hard=True)
        for reader in readers:
            reader.join(left(time.monotonic() + 1))
    if failed:
        raise failed[0]
    return "".join(lines["stdout"]), "".join(lines["stderr"])


def signal_child(proc, hard=False):
    if getattr(proc, "tandem_done", False):
        return
    # An exited leader's group can still hold its tools, and killpg reaches them while any is alive.
    if proc.returncode is not None and not proc.tandem_group:
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


def tracked(kind=None):
    with CHILDREN_LOCK:
        return [p for p in CHILDREN if kind is None or p.tandem_kind == kind]


def signal_children(hard=False, kind=None, procs=None):
    """Stop every tracked agent and check (or only those of one kind, or only procs); returns how many."""
    procs = tracked(kind) if procs is None else procs
    for proc in procs:
        signal_child(proc, hard)
    return len(procs)


OUT_LOCK = threading.RLock()  # every line the orchestrator prints goes through here
CONSOLE = None                # the interactive session's Console, when one is running


def log(msg, kind=None, brief=None):
    """One line of the orchestrator's log. Scripts and log files parse the dated form, so it never
    changes outside the terminal session; there the date is noise, and the line gets a short stamp,
    a symbol by kind (LOG_MARKS) and brief instead of msg when given ("" prints nothing)."""
    if in_session():
        if brief != "":
            emit(CONSOLE.log_line(msg if brief is None else brief, kind))
        return
    emit(f"[{now()}] {msg}")


def in_session():
    """The interactive session on a terminal, with or without color: the only place output is restyled."""
    return bool(CONSOLE and CONSOLE.styled)


def show_path(path, base=None):
    """A path as the session shows it: relative to base when inside it, else ~ for the home directory.
    Whole everywhere else, because scripts copy it; /transcript prints it whole in the session too."""
    text = str(path)
    if not in_session():
        return text
    for root, prefix in ((base, ""), (pathlib.Path.home(), "~" + os.sep)):
        root = str(root).rstrip(os.sep) if root else ""
        if root and text.startswith(root + os.sep):
            return prefix + text[len(root) + 1:]
    return text


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


# A run takes long enough that the owner looks away; main() turns this on for the commands that
# start or resume one in front of them. Headless -p stays quiet unless asked: scripts and CI run it.
NOTIFY = {"on": False}
NOTIFY_TIMEOUT = 2  # seconds: a hung notifier must not hold up the loop
# The title and body arrive as arguments, so no quote or backslash in a run name can break the script.
NOTIFY_SCRIPT = ["on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run"]


def notifier():
    """The desktop notifier notify() uses on this machine, or None when the bell is all there is."""
    if sys.platform == "darwin":
        return "osascript" if shutil.which("osascript") else None
    return "notify-send" if shutil.which("notify-send") else None


def notify(title, body):
    """Tell the owner something happened, best effort: a desktop notification, OSC 9 in iTerm2 and
    the bell. Never raises; the loop must go on whether or not anyone heard it."""
    body = CONTROL_CHARS.sub(" ", str(body))[:200]
    try:
        tool = notifier()
        if tool == "osascript":
            cmd = ["osascript", *[a for line in NOTIFY_SCRIPT for a in ("-e", line)], title, body]
        elif tool == "notify-send":
            cmd = ["notify-send", "--", title, body]
        if tool:
            subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=NOTIFY_TIMEOUT, **CHILD_POPEN)
    except Exception:  # missing, hung (TimeoutExpired) or refused: the bell below still rings
        pass
    # OSC 9 ends with BEL, which iTerm2 takes as the terminator, so the bell is a second BEL.
    seq = (f"\x1b]9;{title}: {body}\a" if os.environ.get("TERM_PROGRAM") == "iTerm.app" else "") + "\a"
    with OUT_LOCK:
        try:
            if CONSOLE:
                CONSOLE.alert(seq)
            else:
                for stream in (sys.stdout, sys.stderr):
                    if stream.isatty():
                        stream.write(seq)
                        stream.flush()
                        break
        except Exception:
            pass


def notifier_report():
    """What doctor says notify() will use here."""
    parts = [notifier()] if notifier() else []
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        parts.append("OSC 9")
    return " + ".join(parts + ["bell"]) if parts else "bell only"


def wants_notify(fn, args):
    """Whether this command tells the owner when a run ends: --no-notify and a saved "notify": false
    silence it; headless -p only with --notify."""
    if getattr(args, "no_notify", False):
        return False
    if fn is cmd_headless:
        return bool(getattr(args, "notify", False))
    if fn not in (cmd_interactive, cmd_start, cmd_resume, cmd_campaign_start, cmd_campaign_resume):
        return False
    return read_config().get("notify", True) is not False


def run_notice(run_dir, state):
    name = state.get("name") or run_dir.name
    history = state.get("history") or []
    if state.get("outcome") == "blocked" and history:
        return f"{name}: blocked, {NAMES[history[-1]['agent']]} needs you"
    n = len(history)
    words = {"max_turns": "out of turns", "over_budget": "over budget"}
    return (f"{name}: {words.get(state.get('outcome'), state.get('outcome'))} in {n} turn{'s' * (n != 1)}, "
            f"${state.get('cost_usd', 0.0):.2f}")


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
    log(f"{NAMES[agent]} hit a usage limit; paused until {until:%H:%M}", "warn")


def wait_until(when, stop_file=None):
    """Sleep in short steps so a stop request is noticed; returns False if stopped."""
    while datetime.datetime.now() < when:
        if stop_file and pathlib.Path(stop_file).exists():
            return False
        time.sleep(min(5, max(1, (when - datetime.datetime.now()).total_seconds())))
    return True


class TurnInterrupted(RuntimeError):
    """The owner stopped the turn to hand the agent a correction."""


def interrupted(run_dir):
    return bool(run_dir) and (pathlib.Path(run_dir) / "INTERRUPT").exists()


def call_agent(agent, cfg, worktree, prompt, out_base, schema, key, readonly, stop_file=None, run_dir=None):
    """Run one agent call, retrying network hiccups; a usage limit pauses the agent and re-raises."""
    spent = 0.0  # reported by attempts that failed; --max-cost counts every attempt, not just the last
    budget = cfg.get("claude_budget")
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        if attempt and interrupted(run_dir):
            stop = TurnInterrupted("interrupted by the owner")
            stop.cost = spent
            raise stop
        if spent and budget:
            # A retry is the same turn: it gets what the failed attempts left of the turn's budget.
            cfg = dict(cfg, claude_budget=max(round(budget - spent, 2), 0.01))
        try:
            result, cost, *more = RUNNERS[agent](cfg, worktree, prompt, out_base, schema, key, readonly)
            return (result, cost + spent, *more)
        except RuntimeError as e:
            spent += getattr(e, "cost", 0.0)
            e.cost = spent
            # Stopping the agent makes it exit like a crash; that must not count as a failure or a
            # network hiccup worth waiting for.
            if interrupted(run_dir):
                stop = TurnInterrupted("interrupted by the owner")
                stop.cost = spent
                raise stop from e
            kind = classify(str(e))
            if kind == "limit":
                mark_unavailable(agent, parse_reset(str(e)))
                raise
            if kind != "transient" or attempt == MAX_TRANSIENT_RETRIES:
                raise
            log(f"{NAMES[agent]}: {tail(str(e), 120)}; retrying in {TRANSIENT_WAIT}s ({attempt + 1}/{MAX_TRANSIENT_RETRIES})", "warn")
            if not wait_until(datetime.datetime.now() + datetime.timedelta(seconds=TRANSIENT_WAIT), stop_file):
                raise


# ---------- state ----------

def write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def write_tuning(target, values):
    """Merge values into target/tuning.json. The loop re-reads it every turn, so a session never has to
    touch state.json, which the loop owns and rewrites. None means "back to the default"."""
    path = target / "tuning.json"
    current = read_tuning(path)
    current.update({k: v for k, v in values.items() if k in TUNING_KEYS})
    write_json(path, current)


def read_tuning(path):
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}  # missing, or caught mid-edit by hand: keep the current settings
    return {k: v for k, v in data.items() if k in TUNING_KEYS} if isinstance(data, dict) else {}


def apply_tuning(state, paths):
    """Fold tuning files (later wins) into state["config"] in place; returns [(key, old, new)] for changes."""
    wanted = {}
    for path in paths:
        wanted.update(read_tuning(path))
    cfg = state["config"]
    changes = [(k, cfg.get(k), v) for k, v in wanted.items() if cfg.get(k) != v]
    for k, _, v in changes:
        cfg[k] = v
    return changes


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
        owner = h["status"] == "interrupted"
        head = f"### Turn {h['turn']} - {NAMES[h['agent']]} - {h['summary'] if owner else h['status']}"
        if h.get("commit"):
            head += f" - commit {h['commit']}"
        lines.append(head)
        if h.get("error"):
            lines.append(f"The agent failed: {h['error']}")
        for key, label in (("summary", "Summary"), ("could_not_do", "Could not do"),
                           ("checks", "Checks"), ("handoff", "Handoff")):
            if h.get(key) and not (owner and key == "summary"):
                lines.append(f"{label}: {h[key]}")
        lines.append("")
    return "\n".join(lines)


# ---------- turn cards and the board ----------

AGENT_COLORS = {"claude": 208, "codex": 75}  # the banner logo's two ends
STATUS_COLORS = {"continue": "36", "done": "32", "blocked": "33", "error": "31", "interrupted": "35"}
OUTCOME_COLORS = {"done": "32", "max_turns": "33", "blocked": "33", "stopped": "35", "parked": "36", "failed": "31",
                  "over_budget": "33"}
# The session's log lines by kind: (symbol, ASCII symbol, style). Warnings and errors keep their
# color on the text too, so dimming the rest of the log never hides them.
LOG_MARKS = {"run": ("▸", ">", "1"), "turn": ("·", ".", "2"), "ok": ("✓", "+", "32"),
             "warn": ("!", "!", "33"), "error": ("✗", "x", "31")}
OWNER_STYLE = "1;48;5;236"  # bold on a dark band: no tandem line uses a background
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
MIN_CARD_WIDTH = 20  # the narrowest box that still closes with a title and a few words


def term_width():
    # Python before 3.11 reports 0 columns for a terminal whose size was never set.
    return shutil.get_terminal_size().columns or 80


def card_width():
    return max(MIN_CARD_WIDTH, min(100, term_width()))


def wrap_field(text, width, label="", max_lines=3):
    """Wrap label+text to at most max_lines lines of at most width; the last one ends in an ellipsis if cut.

    Agent text goes through the terminal, so control characters (an escape sequence in a summary) become spaces.
    """
    lines = textwrap.wrap(CONTROL_CHARS.sub(" ", f"{label}{text}"), width, subsequent_indent="  " if label else "",
                          break_long_words=True)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        lines[-1] = (last if len(last) < width else last[:width - 1]) + "\u2026"
    return lines


def draw_box(title, rows, color, fancy, width):
    """A closed box of exactly `width` columns. rows are (text, style) pairs, each already at most width-4 wide."""
    width = max(width, MIN_CARD_WIDTH)
    tl, tr, bl, br, h, v = "\u256d\u256e\u2570\u256f\u2500\u2502" if fancy else "++++-|"
    edge = f"38;5;{color}"

    def paint(code, text):
        return f"\x1b[{code}m{text}\x1b[0m" if fancy and code else text

    title = title if len(title) <= width - 6 else title[:width - 7] + "\u2026"
    out = [paint(edge, tl + h + " ") + paint("1;" + edge, title) + paint(edge, " " + h * (width - 5 - len(title)) + tr)]
    for text, style in rows:
        out.append(paint(edge, v) + " " + paint(style, text) + " " * (width - 4 - len(text)) + " " + paint(edge, v))
    out.append(paint(edge, bl + h * (width - 2) + br))
    return "\n".join(out)


def rule_line(title, width, style, fancy, utf):
    """`── title ─────` across width columns: opens and closes each run in the session. The dashes
    separate runs without color too; the title is cut with an ellipsis, never wrapped."""
    h, more = ("─", "…") if utf else ("-", "...")
    room = max(width - 5, 1)  # at least one dash after the title
    if display_width(title) > room:
        title = fit_width(title, max(room - len(more), 0)) + more
    fill = h * max(width - 4 - display_width(title), 1)
    if not fancy:
        return f"{h * 2} {title} {fill}"
    return f"\x1b[2m{h * 2}\x1b[0m \x1b[{style}m{title}\x1b[0m \x1b[2m{fill}\x1b[0m"


def turn_card(entry, fancy, width):
    agent, inner = entry["agent"], max(width, MIN_CARD_WIDTH) - 4
    status, seconds = entry.get("status", ""), entry.get("seconds")
    head = [status]
    if seconds is not None:
        head.append(f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s")
    if entry.get("commit"):
        head.append(f"commit {entry['commit']}")
    rows = [(line, STATUS_COLORS.get(status)) for line in wrap_field(" \u00b7 ".join(head), inner, max_lines=2)]
    for key, label, style in (("summary", "", None), ("error", "error: ", "31"),
                              ("handoff", f"\u2192 {NAMES[OTHER[agent]]}: ", None),
                              ("could_not_do", "\u2717 could not: ", "33"), ("checks", "\u2713 checks: ", "32")):
        if entry.get(key):
            rows += [(line, style) for line in wrap_field(entry[key], inner, label)]
    return draw_box(f"turn {entry['turn']} \u00b7 {NAMES[agent]}", rows, AGENT_COLORS[agent], fancy, width)


def board(state, fancy, width, campaign=None):
    """Where the run stands: what the agents agree on, what is still open, the models and the cost."""
    inner, history, cfg = max(width, MIN_CARD_WIDTH) - 4, state["history"], state.get("config", {})
    outcome, last = state.get("outcome"), (state["history"][-1] if state["history"] else None)
    rows = [(line, None) for line in wrap_field(state.get("goal", ""), inner, "goal: ", 2)]
    if outcome:
        rows.append((f"outcome: {outcome}", None))
    if campaign:
        rows += [(line, None) for line in wrap_field(
            f"campaign: {campaign.get('branch', '?')} \u00b7 {len(campaign.get('runs', []))} done \u00b7 "
            f"{len(campaign.get('queue', []))} queued", inner, max_lines=2)]

    rows.append(("agreed", "1"))
    if outcome == "done" and last:
        who = f"{NAMES[last['agent']]} alone" if state.get("needs_review") else "both agents agreed"
        agreed = [f"\u2713 {who}"]
    elif last and last["status"] == "done" and not outcome:
        agreed = [f"\u2713 {NAMES[last['agent']]} says done (turn {last['turn']}) \u00b7 "
                  f"waiting for {NAMES[OTHER[last['agent']]]}"]
    else:
        agreed = ["nothing agreed yet"]
    rows += [(line, "32" if agreed[0][0] == "\u2713" else None) for text in agreed
             for line in wrap_field(text, inner, max_lines=2)]
    if cfg.get("check"):
        verdict = ("passed" if outcome == "done" else "failed last time" if state.get("check_feedback")
                   else "not run yet")
        rows += [(line, None) for line in wrap_field(f"check: {cfg['check']} \u00b7 {verdict}", inner)]
    else:
        rows.append(("no check", None))

    open_items = []
    if last and last.get("handoff"):
        open_items.append(f"\u2022 {NAMES[OTHER[last['agent']]]}: {last['handoff']}")
    open_items += [f"\u2022 could not (turn {h['turn']}): {h['could_not_do']}" for h in history[-2:]
                   if h.get("could_not_do")]
    if state.get("owner_messages"):
        open_items.append(f"owner messages: {len(state['owner_messages'])}")
    if open_items:
        rows.append(("open", "1"))
        rows += [(line, None) for text in open_items for line in wrap_field(text, inner, max_lines=2)]

    def model(agent):
        return f"{agent} {cfg.get(agent + '_model') or 'default'}/{cfg.get(agent + '_effort') or 'default'}"
    rows += [(line, "2") for line in wrap_field(
        f"models: {model('claude')} \u00b7 {model('codex')} \u00b7 ${state.get('cost_usd', 0):.2f}", inner)]
    title = f"{state.get('name') or 'run'} \u00b7 turn {state['turn'] - 1}/{state['max_turns']}"
    return draw_box(title, rows, AGENT_COLORS[last["agent"]] if last else AGENT_COLORS["claude"], fancy, width)


def usage_tokens(usage):
    """Tokens one turn used: Codex reports a total, Claude Code input and output apart."""
    return usage.get("total", usage.get("input", 0) + usage.get("output", 0))


def cost_text(state):
    """The run's reported cost, or spent/cap when --max-cost is set (a campaign's total, inside one)."""
    cap = (state.get("config") or {}).get("max_cost")
    return f"${budget_spent(state):.2f}/${cap:.2f}" if cap else f"${state.get('cost_usd', 0):.2f}"


def short_count(n):
    return f"{n}" if n < 1000 else f"{n / 1000:.0f}k" if n < 999500 else f"{n / 1e6:.1f}M"


def short_span(secs):
    secs = int(secs)
    return f"{secs // 3600}h{secs // 60 % 60:02d}m" if secs >= 3600 else f"{secs // 60}m{secs % 60:02d}s"


def fit_segments(segments, width, fancy):
    """Join (text, style) pairs cut to width columns with an ellipsis, colored only after the cut:
    escape codes take no columns, so fitting the colored string would cut it short."""
    out, used, total = [], 0, sum(display_width(t) for t, _ in segments)
    room = width if total <= width else max(width - 1, 0)
    for text, style in segments:
        text = fit_width(text, room - used)
        used += display_width(text)
        if text:
            out.append(f"\x1b[{style}m{text}\x1b[0m" if fancy and style else text)
        if used >= room:
            break
    return "".join(out) + ("\u2026" if total > width and width else "")


def status_bar(state, now, width, fancy, progress=None, session_start=None, defaults=None,
               editing=None, hint=None):
    """The rows under the prompt: what each agent runs and does, then the run's numbers.

    state is the run's state.json, or None before the first run (defaults then name the models).
    progress is PROGRESS while a turn runs, else empty; session_start is when the session began.
    editing is (values, focused key) while the owner changes the models on the bar itself.
    hint ends the bar; with a run it is left out unless given, because the run's numbers come first."""
    cfg = dict(defaults or {}, **(state or {}).get("config", {})) if state else dict(defaults or {})
    history = (state or {}).get("history", [])
    progress = PROGRESS if progress is None else progress
    dim, sep = "2", (" \u00b7 ", "2")

    def value(key, text):
        if editing and key == editing[1]:
            # Brackets keep focus visible under NO_COLOR too.
            return (text, "7") if fancy else (f"[{text}]", None)
        return text, None

    def model(agent):
        if editing:
            shown = [(k, editing[0].get(k) or "default") for k in (agent + "_model", agent + "_effort")]
        else:
            ran = next((h["model"] for h in reversed(history) if h["agent"] == agent and h.get("model")), None)
            shown = [(agent + "_model", cfg.get(agent + "_model") or ran or "default"),
                     (agent + "_effort", cfg.get(agent + "_effort") or "default")]
        out = []
        for key, text in shown:
            out.append(("/" if out else " ", None))
            out.append(value(key, text))
        return out

    def doing(agent):
        if progress.get("agent") == agent and progress.get("since"):
            return f"working {short_span(now - progress['since'])}", STATUS_COLORS["continue"]
        if state and state.get("failures", {}).get(agent, 0) >= MAX_FAILURES_PER_AGENT:
            return "benched", STATUS_COLORS["error"]
        away = unavailable_until(agent)
        if away:
            return f"away until {away:%H:%M}", STATUS_COLORS["blocked"]
        return ("next", None) if state.get("next") == agent and not state.get("outcome") else ("waiting", dim)

    first = []
    for agent in NAMES:
        if first:
            first.append(("   ", None))
        first += [(agent, f"38;5;{AGENT_COLORS[agent]}")] + model(agent)
        if state:
            first += [sep, doing(agent)]
    # The keys of the mode would not fit beside the models on 80 columns: they get a row of their own.
    keys = [fit_segments([(hint or BAR_EDIT_HINT, dim)], width, fancy)] if editing else []
    models = [fit_segments(first, width, fancy)]
    if editing and sum(display_width(text) for text, _ in first) > width:
        # A clipped bar can hide the selected Codex field entirely, especially during a turn.
        # Give each field a row when the combined bar no longer fits after a resize.
        models = []
        for key in TUNABLE:
            row = [(key.replace("_", " ") + " ", dim), value(key, editing[0].get(key) or "default")]
            if state and key.endswith("_effort"):
                row += [sep, doing(key.split("_")[0])]
            models.append(fit_segments(row, width, fancy))
    if not state:
        return models + keys if editing else \
            [fit_segments(first + [sep, (hint or "/models to change", dim)], width, fancy)]

    second = [("turn ", dim), (f"{min(state['turn'], state['max_turns'])}/{state['max_turns']}", None),
              sep, (cost_text(state), None)]
    tokens = sum(usage_tokens(h["usage"]) for h in history if h.get("usage"))  # only what was reported
    if tokens:
        second += [sep, (short_count(tokens), None), (" tok", dim)]
    if session_start is not None:
        second += [sep, ("session ", dim), (short_span(now - session_start), None)]
    stat = state.get("diffstat")
    if stat:
        second += [sep, (f"+{stat['added']}", "32"), (" ", None), (f"\u2212{stat['removed']}", "31")]
    if cfg.get("check"):
        passed = state.get("check_passed")
        mark = ("\u2013", None) if passed is None else ("\u2713", "32") if passed else ("\u2717", "31")
    else:
        mark = ("\u2013", None)
    second += [sep, ("check ", dim), mark]
    if hint and not editing:
        second += [sep, (hint, dim)]  # last, so a narrow terminal cuts it before the numbers
    return models + [fit_segments(second, width, fancy)] + keys


def report_turn(entry):
    """One finished turn: a card in a fancy session, the single log line everywhere else (logs, pipes, NO_COLOR)."""
    if CONSOLE and CONSOLE.fancy:
        return emit(turn_card(entry, True, card_width()))
    log(f"turn {entry['turn']}: {NAMES[entry['agent']]} -> {entry['status']} ({entry['seconds']}s"
        f"{', commit ' + entry['commit'] if entry['commit'] else ''}) {entry.get('summary') or entry.get('error', '')}",
        "error" if entry["status"] == "error" else "warn" if entry["status"] == "blocked" else None)


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
    human = owner_block(state.get("owner_messages", []), bool(human_msgs))
    last = state["history"][-1] if state["history"] else None
    if last and last["status"] == "interrupted" and last["agent"] == agent:
        edits = f"its partial edits are in commit {last['commit']}" if last.get("commit") else "it changed nothing"
        # Only a message read for this turn came with the interruption; an older one above is not
        # what the owner stopped the turn for (Ctrl+C leaving the session stops it with none).
        if human_msgs:
            human += (f"\nYour previous turn was interrupted by the owner; {edits}. Check them against the "
                      f"message above before you go on.\n")
        else:
            human += (f"\nYour previous turn was interrupted by the owner without a message; {edits}. "
                      f"Check them before you go on.\n")
    return TURN_PROMPT.read_text().format(
        me=NAMES[agent], other=NAMES[OTHER[agent]], goal=state["goal"].strip(),
        turn=state["turn"], max_turns=state["max_turns"], branch=state["branch"],
        base=state["base"][:10], log=commits, transcript=render_history(state["history"]),
        human=human, check_feedback=feedback,
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


# ---------- what the agents are doing ----------

# A turn prints nothing until it ends, often many minutes later. While an agent works, each action
# it takes becomes one line of <run dir>/activity.log, which `tandem watch` follows in a pane of its
# own: the session's prompt keeps the screen it has, and the log outlives the session.
ACTIVITY = "activity.log"
# The headers of codex exec's plain log, each alone on its line before what it introduces.
CODEX_SECTIONS = ("user", "codex", "thinking", "exec", "apply patch", "tokens used")
TOOL_VERBS = {"Read": "read", "Edit": "edit", "MultiEdit": "edit", "Write": "write", "NotebookEdit": "edit",
              "Grep": "grep", "Glob": "glob", "WebFetch": "fetch", "WebSearch": "search"}


def note_activity(run_dir, who, text):
    text = " ".join(str(text).split())[:300]  # one line, whatever the agent wrote
    if text:
        with open(pathlib.Path(run_dir) / ACTIVITY, "a") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S}\t{who}\t{text}\n")


def start_activity(out_base, agent):
    """Head the call's lines with what it is (turn 3, plan 2) and return a writer for the rest."""
    name = re.match(r"([a-z]+)-0*(\d+)-", out_base.name)
    note_activity(out_base.parent, agent, f"── {' '.join(name.groups()) if name else out_base.name} ──")
    return lambda text: note_activity(out_base.parent, agent, text)


def first_line(text):
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def short_path(path, worktree):
    try:
        return str(pathlib.Path(path).relative_to(worktree))
    except ValueError:
        return str(path)


def describe_tool(name, args, worktree):
    if name == "StructuredOutput":
        return ""  # the reply itself; the turn card shows it
    if name == "Bash":
        return "$ " + first_line(args.get("command"))
    if name in ("Task", "Agent"):
        return "agent " + first_line(args.get("description"))
    if name in TOOL_VERBS:
        target = args.get("file_path") or args.get("notebook_path")
        target = short_path(target, worktree) if target else args.get("pattern") or args.get("url") or args.get("query")
        return f"{TOOL_VERBS[name]} {target or ''}".strip()
    return name


def claude_actions(event, worktree):
    """The lines worth showing from one event of Claude's stream-json output."""
    message = event.get("message") if event.get("type") in ("assistant", "user") else None
    content = message.get("content") if isinstance(message, dict) else None
    found = []
    for part in content if isinstance(content, list) else []:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            found.append("» " + first_line(part.get("text")) if first_line(part.get("text")) else "")
        elif kind == "tool_use":
            found.append(describe_tool(part.get("name", ""), part.get("input") or {}, worktree))
        elif kind == "tool_result" and part.get("is_error"):
            body = part.get("content")
            if isinstance(body, list):
                body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
            found.append("✗ " + first_line(body))
    return [line for line in found if line]


class CodexActivity:
    """Reads codex exec's plain log (its stderr) a line at a time; feed() returns a line to show, or None."""

    def __init__(self, worktree):
        self.worktree, self.section, self.fresh = worktree, None, False

    def feed(self, line):
        if line in CODEX_SECTIONS:
            self.section, self.fresh = line, True
            return None
        if not line.strip():
            return None
        fresh, self.fresh = self.fresh, False
        if self.section == "codex" and fresh:
            return "» " + line.strip()
        if self.section == "exec" and fresh:
            # "/bin/zsh -lc 'pytest' in /work/dir"; a command of several lines shows its first only,
            # so the directory and the closing quote never arrive.
            end = line.rfind(" in /")
            command = re.sub(r"^\S*sh -lc ", "", line[:end] if end > 0 else line)
            if command[:1] in ("'", '"'):
                command = command[1:-1] if len(command) > 1 and command[-1] == command[0] else command[1:]
            return "$ " + command
        exited = re.match(r"\s*exited (-?\d+) in ", line) if self.section == "exec" else None
        if exited:
            return f"✗ exited {exited.group(1)}"
        if self.section == "apply patch" and line.startswith("/"):
            return "edit " + short_path(line.strip(), self.worktree)
        return None


def claude_payload(stdout):
    """Claude's result object: the last "result" event of stream-json, or a whole --output-format json reply."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return event
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def run_claude(cfg, worktree, prompt, out_base, schema, key, readonly):
    cmd = ["claude", "-p"]
    images = prompt_images(prompt)
    if images:
        # Read reaches files outside the worktree only through --add-dir. It takes several values,
        # so it goes before the next flag, never last.
        cmd += ["--add-dir", *dict.fromkeys(str(p.parent) for p in images)]
        prompt += "\n\nEach [image: PATH] above is an image from the owner. Open it with Read before you start.\n"
    cmd += ["--output-format", "stream-json", "--verbose", "--json-schema", schema.read_text(),
            "--setting-sources", "project,local"]
    if readonly:
        cmd += ["--permission-mode", "dontAsk", "--allowedTools", *CLAUDE_READ]
    else:
        cmd += ["--permission-mode", cfg["claude_permission_mode"],
                "--allowedTools", *CLAUDE_ALLOWED, "--disallowedTools", *CLAUDE_DISALLOWED]
    if cfg.get("claude_model"):
        cmd += ["--model", cfg["claude_model"]]
    if cfg.get("claude_effort"):
        cmd += ["--effort", cfg["claude_effort"]]
    if cfg.get("claude_budget"):
        cmd += ["--max-budget-usd", str(cfg["claude_budget"])]
    say = start_activity(out_base, "claude")

    def on_line(stream, line):
        try:
            event = json.loads(line) if stream == "stdout" else None
        except ValueError:
            return  # not an event: a warning, or output from a CLI without stream-json
        for text in claude_actions(event, worktree) if isinstance(event, dict) else []:
            say(text)

    proc = run_child(cmd, input=prompt, cwd=worktree, timeout=cfg["timeout"], on_line=on_line)
    payload = claude_payload(proc.stdout)
    out_base.with_suffix(".json").write_text(json.dumps(payload) if payload else proc.stdout)
    out_base.with_suffix(".stderr").write_text(proc.stderr)
    if payload is None:
        raise RuntimeError(f"exit {proc.returncode}, no JSON output: {tail(proc.stderr or proc.stdout)}")
    if payload.get("is_error"):
        err = RuntimeError(f"{payload.get('subtype')}: {tail(str(payload.get('result')))}")
        # A turn stopped by --max-budget-usd still spent money; --max-cost must count it.
        err.cost = payload.get("total_cost_usd") or 0.0
        raise err
    result = payload.get("structured_output") or parse_result(payload.get("result"), key)
    return result, payload.get("total_cost_usd") or 0.0, claude_usage(payload)


def claude_usage(payload):
    """Tokens and the model the turn actually ran on, from what the payload reports; nothing is guessed."""
    found, usage = {}, payload.get("usage")
    if isinstance(usage, dict):
        counts = [usage.get(k) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
        if any(isinstance(n, int) for n in counts):
            found["input"] = sum(n for n in counts if isinstance(n, int))
        if isinstance(usage.get("output_tokens"), int):
            found["output"] = usage["output_tokens"]
    models = payload.get("modelUsage")
    if isinstance(models, dict) and models:
        # Claude Code calls a small model on the side; the one that cost the most did the turn.
        found["model"] = max(models, key=lambda m: (models[m].get("costUSD") or 0)
                             if isinstance(models[m], dict) else 0)
    return found


def codex_usage(text):
    """codex exec prints `model: NAME` in its header and `tokens used` (with or without a colon, the
    count on the same line or the next) at the end; only the total is known."""
    found = {}
    # The log also holds the output of commands the agent ran, which may say the same words;
    # codex's own count is the last one.
    counts = re.findall(r"^tokens used:?\s*([\d,]+)\s*$", text, re.M | re.I)
    if counts:
        found["total"] = int(counts[-1].replace(",", ""))
    m = re.search(r"^model:\s*(\S+)\s*$", text, re.M)
    if m:
        found["model"] = m.group(1)
    return found


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
    say, reader = start_activity(out_base, "codex"), CodexActivity(worktree)

    def on_line(stream, line):
        text = reader.feed(line) if stream == "stderr" else None
        if text:
            say(text)

    proc = run_child(cmd, input=prompt, cwd=worktree, timeout=cfg["timeout"], on_line=on_line)
    out_base.with_suffix(".log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    if proc.returncode != 0:
        errors = [l for l in (proc.stderr + "\n" + proc.stdout).splitlines() if l.startswith("ERROR")]
        raise RuntimeError(f"exit {proc.returncode}: {tail(errors[-1] if errors else proc.stderr or proc.stdout)}")
    return (parse_result(last.read_text() if last.exists() else proc.stdout, key), 0.0,
            codex_usage(proc.stdout + "\n" + proc.stderr))


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


def diffstat(worktree, base, head="HEAD"):
    """{"files": N, "added": N, "removed": N} for base..head, from git's own summary line."""
    try:
        line = git(worktree, "diff", "--shortstat", f"{base}..{head}")
    except (GitError, OSError):
        return None
    counts = {kind: int(n) for n, kind in re.findall(r"(\d+) (file|insertion|deletion)", line)}
    return {"files": counts.get("file", 0), "added": counts.get("insertion", 0), "removed": counts.get("deletion", 0)}


# Outcomes whose work is not finished: the summary offers resume before the merge.
UNFINISHED = ("blocked", "failed", "max_turns", "stopped", "over_budget")


def branch_facts(repo, branch, base, worktree=None):
    """What branch holds over base, read in the owner's repo so a removed worktree still answers.
    Never raises: the end of a run must not crash on a branch the owner already merged and deleted."""
    facts = {"commits": None, "diffstat": None, "note": None}
    notes = []
    try:
        facts["commits"] = int(git(repo, "rev-list", "--count", f"{base}..{branch}"))
        facts["diffstat"] = diffstat(repo, base, branch)
    except (GitError, OSError, ValueError, TypeError):
        notes.append(f"cannot read {branch} in {repo}; merged and deleted already?")
    else:
        if facts["diffstat"] is None:
            notes.append(f"cannot read the diffstat of {base[:12]}..{branch}")
    if worktree and not pathlib.Path(worktree).is_dir():
        notes.append("the worktree is gone")
    facts["note"] = "; ".join(notes) or None
    return facts


def run_handoff(run_dir, state):
    """The branch a run left behind, as handoff_lines() takes it."""
    camp = state.get("campaign")
    again = f"campaign resume {shlex.quote(camp)}" if camp else f"resume {shlex.quote(str(run_dir))}"
    return {"repo": state.get("repo"), "worktree": state.get("worktree"), "branch": state.get("branch"),
            "base": state.get("base"),
            "resume": f"{prog_name()} {again}" if state.get("outcome") in UNFINISHED else None}


def campaign_base(camp):
    """Where the campaign's branch started. Older campaigns did not record it; their first run did."""
    if camp.get("base"):
        return camp["base"]
    for d in [r.get("dir") for r in camp.get("runs", [])] + [camp.get("current")]:
        try:
            return load(pathlib.Path(d))["base"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def campaign_handoff(camp_dir, camp):
    # A running campaign holds its lock; offering resume then would only meet "already running".
    unfinished = camp.get("outcome") != "finished" and not (camp_dir / ".lock").exists()
    return {"repo": camp.get("repo"), "worktree": camp.get("worktree"), "branch": camp.get("branch"),
            "base": campaign_base(camp),
            "resume": f"{prog_name()} campaign resume {shlex.quote(str(camp_dir))}" if unfinished else None}


def handoff_lines(h):
    """The end-of-run summary: what the branch holds, where it is, and the commands that read, take
    and clean it up. The commands are printed whole and unstyled so they copy cleanly."""
    paint = CONSOLE.paint if in_session() else (lambda style, text: text)
    branch, base, repo, work = h.get("branch"), h.get("base"), h.get("repo"), h.get("worktree")
    if repo and branch and base:
        facts = branch_facts(repo, branch, base, work)
    else:
        facts = {"commits": None, "diffstat": None, "note": "no branch recorded for this run"}
    n, stat = facts["commits"], facts["diffstat"] or {}
    short = (base or "?")[:12]
    if n is None:
        what = f"{branch or '?'} over {short}: unknown"
    elif n == 0:
        what = f"{branch}: no changes over {short}"
    else:
        what = f"{branch}: {n} commit{'' if n == 1 else 's'} over {short}, "
        if facts["diffstat"] is None:
            what += "files changed unknown"
        else:
            what += (f"{stat['files']} file{'' if stat['files'] == 1 else 's'} changed, "
                     f"+{stat['added']} -{stat['removed']}")
    rows = [("branch", what, False), ("worktree", str(work or "?"), True)]
    if h.get("resume"):
        rows.append(("resume", h["resume"], True))
    if n:
        q = shlex.quote
        r = q(str(repo))
        rows += [("read", f"git -C {r} diff {short}..{q(branch)}", True),
                 ("merge", f"git -C {r} merge {q(branch)}", True),
                 ("clean up", f"git -C {r} worktree remove {q(str(work))} && git -C {r} branch -d {q(branch)}", True)]
    if facts["note"]:
        rows.append(("note", facts["note"], False))
    width = max(term_width() - 1, 20)
    lines = []
    for label, text, whole in rows:
        head = paint("2", f"  {label:<9} ")
        lines.append(head + (text if whole or not in_session() else fit_width(text, width - 12)))
    return lines


def diff_stat_lines(repo, base, branch, limit=20):
    """`git diff --stat` of the branch, cut to its first limit files plus git's summary line."""
    try:
        files = git(repo, "diff", "--stat", f"{base}..{branch}").splitlines()
    except (GitError, OSError, TypeError):
        return []
    total = files[-1:]
    files = files[:-1]
    if len(files) > limit:
        files = files[:limit] + [f" ... {len(files) - limit} more files"]
    return files + total


def show_handoff(h, stat=False):
    for line in handoff_lines(h):
        emit(line)
    if stat and h.get("repo") and h.get("branch") and h.get("base"):
        for line in diff_stat_lines(h["repo"], h["base"], h["branch"]):
            emit(line)


def handoff_of(target):
    """The handoff for a run or campaign directory; None when target is neither."""
    if (target / "campaign.json").is_file():
        return campaign_handoff(target, json.loads((target / "campaign.json").read_text()))
    if (target / "state.json").is_file():
        return run_handoff(target, load(target))
    return None


def show_diff(target):
    """`tandem diff` and /diff: the end-of-run summary at any time, plus the files it touched."""
    try:
        h = handoff_of(target)
    except (OSError, ValueError) as exc:
        emit(f"cannot read {target}: {exc}")
        return
    if h is None:
        emit(f"no run or campaign at {target}")
        return
    show_handoff(h, stat=True)


def run_check(state):
    cmd = state["config"].get("check")
    if not cmd:
        return True, ""
    proc = run_child(cmd, shell=True, cwd=state["worktree"], timeout=state["config"]["timeout"], kind="check")
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


def interrupt_turn(run_dir, quiet=False):
    """Stop the agent working on run_dir now; loop records the turn as interrupted, not failed.

    Callable at any time. Only agents are stopped, never the check: the turn it checks has already
    ended, so it finishes and the next turn reads the owner's message.
    """
    procs = [p for p in tracked("agent") if p.returncode is None]
    if not procs:
        # No flag then: nothing would handle it, and a real error later in the turn (a usage limit)
        # would pass for an interruption.
        if not quiet:
            emit("no agent is working right now; the next turn reads it")
        return
    flag = pathlib.Path(run_dir) / "INTERRUPT"
    flag.write_text(now())  # before the signal: the agent's exit must already read as interrupted
    signal_children(procs=procs)
    if not quiet:
        emit("stopping the turn; its edits are kept and the same agent goes on with your message")

    def insist():
        # Only the agents stopped here: by now a planner or the next goal's agent may be running.
        signal_children(hard=True, procs=procs)
    timer = threading.Timer(KILL_GRACE, insist)
    timer.daemon = True
    timer.start()


def stop_requested(state):
    return bool(state.get("stop_file")) and pathlib.Path(state["stop_file"]).exists()


def finish(run_dir, state, outcome, message):
    state["outcome"] = outcome
    state["outcome_message"] = message
    save(run_dir, state)
    append_transcript(run_dir, f"\n## Finished: {outcome}\n\n{message}\n")
    log(f"FINISHED {state.get('name') or run_dir.name}: {outcome} - {message}",
        "error" if outcome == "failed" else "ok" if outcome == "done" else None, brief=message)
    show_handoff(run_handoff(run_dir, state))
    log(f"transcript {run_dir / 'transcript.md'}", brief=f"transcript {show_path(run_dir / 'transcript.md', run_dir)}")
    if in_session():
        emit(CONSOLE.rule(f"{outcome} \u00b7 {state.get('name') or run_dir.name}", OUTCOME_COLORS.get(outcome, "1")))
    # A campaign goes on to its next goal, so only the campaign's own end is worth a notification.
    if NOTIFY["on"] and not state.get("campaign"):
        notify("tandem", run_notice(run_dir, state))


def budget_spent(state):
    """What counts against --max-cost: the cost the CLIs reported. A campaign's cap covers all its runs,
    so a run inside one also carries what the campaign had spent when the run began."""
    return state.get("prior_cost", 0.0) + state.get("cost_usd", 0.0)


def turn_budget(cfg, spent):
    """The --max-budget-usd for one Claude turn: what is left of the cap, or --claude-budget if smaller,
    so a single turn cannot run far past the cap."""
    caps = [b for b in (cfg.get("claude_budget"),) if b]
    if cfg.get("max_cost"):
        caps.append(max(round(cfg["max_cost"] - spent, 2), 0.01))
    return min(caps) if caps else None


def over_budget_message(run_dir, state):
    cap = state["config"]["max_cost"]
    camp = state.get("campaign")
    again = f"campaign resume {camp}" if camp else f"resume {run_dir}"
    return (f"spent ${budget_spent(state):.2f} of the ${cap:.2f} cap; "
            f"`{prog_name()} {again} --max-cost {cap * 2:g}` continues")


PROGRESS = {}  # turn, agent, since: what the interactive status line shows
CURRENT = {}   # target: the run or campaign directory the session's /say, /stop and /status act on


def loop(run_dir):
    try:
        return _loop(run_dir)
    except Exception as exc:
        # A crash is a run end too, and the owner who looked away would otherwise never hear of it.
        # Ctrl+C is not: whoever pressed it is looking.
        if NOTIFY["on"]:
            try:
                state = load(run_dir)
            except Exception:
                state = {}
            if not state.get("campaign"):
                notify("tandem", f"{state.get('name') or pathlib.Path(run_dir).name}: crashed, {exc}")
        raise


def _loop(run_dir):
    state = load(run_dir)
    state.pop("outcome", None)
    cfg = state["config"]
    inboxes = state.get("inboxes") or [str(run_dir / "inbox.md")]
    tuning_files = state.get("tuning_files") or [str(run_dir / "tuning.json")]
    if in_session():
        resumed = f" \u00b7 turn {state['turn']}" if state["history"] else ""
        emit(CONSOLE.rule(f"{state.get('name') or run_dir.name} \u00b7 {state['branch']}{resumed}"))
    while True:
        if stop_requested(state):
            return finish(run_dir, state, "stopped", "The owner asked to stop.")
        changes = apply_tuning(state, tuning_files)
        for key, old, new in changes:
            log(f"config: {key} {old or 'default'} -> {new or 'default'}")
        if changes:
            save(run_dir, state)
        # Checked only between turns: a running turn is never killed for the cap, so a run can
        # overshoot it by at most one turn. Checked before the turn limit and the availability waits:
        # "over budget" is the more useful answer, and waiting hours for an agent only to stop is not.
        if cfg.get("max_cost") and budget_spent(state) >= cfg["max_cost"]:
            return finish(run_dir, state, "over_budget", over_budget_message(run_dir, state))
        if state["turn"] > state["max_turns"]:
            return finish(run_dir, state, "max_turns", "Turn limit reached before both agents agreed.")

        healthy = [a for a in ("claude", "codex") if state["failures"][a] < MAX_FAILURES_PER_AGENT]
        if not healthy:
            return finish(run_dir, state, "failed", "Both agents failed repeatedly; see the .stderr/.log files.")
        ready = [a for a in healthy if not unavailable_until(a)]
        if not ready:
            wake = min(unavailable_until(a) for a in healthy)
            log(f"no agent available (usage limits); waiting until {wake:%H:%M}", "warn")
            if NOTIFY["on"]:
                notify("tandem", f"{state.get('name') or run_dir.name}: both agents at usage limits, "
                                 f"waiting until {wake:%H:%M}")
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

        # Cleared before the inbox is read: an interruption from a crash, or one that arrived while
        # nothing could be stopped (the check), must not kill this turn; its message is read now.
        (run_dir / "INTERRUPT").unlink(missing_ok=True)
        human = take_inbox(inboxes)
        if human:
            state.setdefault("owner_messages", []).append({"at": now(), "text": human})
            save(run_dir, state)
            append_transcript(run_dir, f"\n### Owner, before turn {state['turn']}\n\n{human}\n")
        prompt = build_prompt(state, agent, human, state.pop("check_feedback", ""))
        turn = state["turn"]
        out_base = run_dir / f"turn-{turn:02d}-{agent}"
        out_base.with_suffix(".prompt.md").write_text(prompt)

        log(f"turn {turn}: {NAMES[agent]} working...", "turn", brief=f"turn {turn} \u00b7 {NAMES[agent]}")
        started = time.time()
        with OUT_LOCK:
            PROGRESS.update(turn=turn, agent=agent, since=started)
        entry = {"turn": turn, "agent": agent, "started": now()}
        try:
            turn_cfg = dict(cfg, claude_budget=turn_budget(cfg, budget_spent(state)))
            result, cost, *more = call_agent(agent, turn_cfg, state["worktree"], prompt, out_base, TURN_SCHEMA,
                                             "status", False, state.get("stop_file"), run_dir)
            usage = dict(more[0]) if more and more[0] else {}
            if usage.get("model"):
                entry["model"] = usage.pop("model")
            if usage:
                entry["usage"] = usage
                totals = state.setdefault("tokens", {"claude": 0, "codex": 0})
                totals[agent] = totals.get(agent, 0) + usage_tokens(usage)
            if not result:
                raise RuntimeError("no structured result in the agent's reply")
            entry.update({k: str(result.get(k, "")).strip() for k in
                          ("status", "summary", "handoff", "could_not_do", "checks")})
            state["cost_usd"] += cost
            state["failures"][agent] = 0
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            state["cost_usd"] += getattr(e, "cost", 0.0)
            if isinstance(e, TurnInterrupted) or interrupted(run_dir):
                # The owner's call, not the agent's failure, and not a turn of the budget: the same
                # agent goes on with the correction and its partial edits.
                state["max_turns"] += 1
                entry.update({"status": "interrupted", "summary": "interrupted by the owner", "handoff": ""})
            else:
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
        if entry["commit"]:
            # Once per commit, here, so the status bar never runs git on its own every second.
            state["diffstat"] = diffstat(state["worktree"], state["base"])
        state["history"].append(entry)
        state["turn"] += 1
        if entry["status"] == "interrupted":
            state["next"] = agent
        elif reviewer_mode(cfg) and entry["status"] != "error":
            # Claude builds until it says done; then Codex reviews; findings go back to Claude.
            state["next"] = ("codex" if entry["status"] == "done" else "claude") if agent == "claude" else "claude"
        else:
            state["next"] = OTHER[agent]
        save(run_dir, state)
        if entry["status"] == "interrupted":
            (run_dir / "INTERRUPT").unlink(missing_ok=True)

        append_transcript(run_dir, render_history([entry]) + f"_{entry['seconds']}s_\n")
        report_turn(entry)
        cap = cfg.get("max_cost")
        if cap and budget_spent(state) >= BUDGET_WARN * cap and state.get("budget_warned") != cap:
            state["budget_warned"] = cap  # once per cap: raising it with /budget earns a new warning
            save(run_dir, state)
            log(f"spent ${budget_spent(state):.2f} of the ${cap:.2f} cap", "warn")
        note_activity(run_dir, agent, f"= {entry['status']} ({entry['seconds']}s) "
                                      f"{first_line(entry.get('summary') or entry.get('error'))}")

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
                if cfg.get("check"):
                    note_activity(run_dir, "check", "$ " + cfg["check"])
                ok, output = run_check(state)
                if cfg.get("check"):
                    state["check_passed"] = ok
                    note_activity(run_dir, "check", "= passed" if ok else "\u2717 failed")
                if ok:
                    if not peer_agreed:
                        state["needs_review"] = True
                    who = "Both agents agree" if peer_agreed else f"{NAMES[agent]} alone ({NAMES[other]} away) says"
                    return finish(run_dir, state, "done",
                                  f"{who} the goal is met" + (" and the check passed." if cfg.get("check") else "."))
                state["check_feedback"] = (f"The agents said done, but `{cfg['check']}` failed. "
                                           f"Fix it before saying done again.\n\n```\n{output}\n```")
                save(run_dir, state)  # the status bar reads the check's verdict from disk
                log("check failed, continuing", "warn")
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
    log(f"run {run_dir.name}: {name} on {branch}", brief="")  # the session's run rule says it
    return run_dir


def config_from(args):
    saved = load_defaults()
    # A flag given on the command line beats what /models saved; absent flags fall back to it.
    chosen = {k: getattr(args, k) if getattr(args, k) is not None else saved.get(k) for k in TUNABLE}
    return {
        "check": getattr(args, "check", None), "timeout": args.timeout,
        "claude_budget": args.claude_budget, "max_cost": getattr(args, "max_cost", None),
        "claude_permission_mode": args.claude_permission_mode,
        "codex_network": args.codex_network, "codex_role": args.codex_role,
        **chosen,
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
        cap = camp["config"].get("max_cost")
        if cap and camp["cost_usd"] >= cap:
            break
        if unavailable_until(planner):
            continue
        prompt = PLAN_PROMPT.read_text().format(me=NAMES[planner], done=done, blockers=blockers, notes=notes)
        n = len(list(camp_dir.glob("plan-*.prompt.md"))) + 1
        out_base = camp_dir / f"plan-{n:02d}-{planner}"
        out_base.with_suffix(".prompt.md").write_text(prompt)
        log(f"planning the next goal: {NAMES[planner]}")
        try:
            # The campaign's cap covers planner calls too, so a planner gets only what is left of it.
            plan_cfg = dict(camp["config"], claude_budget=turn_budget(camp["config"], camp["cost_usd"]))
            result, cost, *_ = call_agent(planner, plan_cfg, camp["worktree"], prompt, out_base,
                                          PLAN_SCHEMA, "finished", True, camp_dir / "STOP")
            camp["cost_usd"] += cost
            if result:
                camp["next_planner"] = OTHER[planner]
                return result
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            camp["cost_usd"] += getattr(e, "cost", 0.0)
            log(f"planner {NAMES[planner]} failed: {tail(str(e), 200)}", "error")
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
        log(f"not pushing {branch}: campaigns never push the default branch", "warn")
        return
    proc = subprocess.run(["git", "push", "origin", f"{branch}:{branch}"], cwd=camp["repo"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, **CHILD_POPEN)
    if proc.returncode == 0:
        log(f"pushed {branch} to origin at {git(camp['worktree'], 'rev-parse', '--short', 'HEAD')}", "ok")
    else:
        log(f"push failed, continuing: {tail(proc.stderr, 300)}", "warn")


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
    ended = crash = None
    try:
        _campaign_loop(camp_dir)
        ended = True
    except Exception as exc:
        ended, crash = True, exc
        raise
    finally:
        lock.unlink(missing_ok=True)
        # Interrupts still leave work to take or resume; release the lock before offering resume.
        camp = None
        try:
            camp = json.loads((camp_dir / "campaign.json").read_text())
            show_handoff(campaign_handoff(camp_dir, camp))
        except (OSError, ValueError) as exc:
            log(f"cannot sum up the campaign's branch: {exc}", "warn")
        # Not after Ctrl+C: whoever pressed it is looking.
        if ended and NOTIFY["on"]:
            notify("tandem", campaign_notice(camp_dir, camp or {}, crash))


def campaign_notice(camp_dir, camp, crash=None):
    outcome = ("crashed" if crash else camp.get("outcome")
               or ("stopped" if (camp_dir / "STOP").exists() else "paused"))
    runs = camp.get("runs") or []
    n = len(runs)
    turns = sum(r.get("turns") or 0 for r in runs)
    cost = camp.get("cost_usd", 0.0)
    if camp.get("current"):
        # A stopped or over-budget run stays current, so its turns and cost are not in the totals yet.
        try:
            state = load(pathlib.Path(camp["current"]))
            turns += max(state.get("turn", 1) - 1, 0)
            cost += state.get("cost_usd", 0.0)
        except Exception:
            pass
    text = (f"campaign {camp_dir.name}: {outcome.replace('_', ' ')} after {n} goal{'s' * (n != 1)}, "
            f"{turns} turn{'s' * (turns != 1)}, ${cost:.2f}")
    return f"{text}: {crash}" if crash else text


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
                log(f"both agents failing, probably a usage limit; waiting {FAILED_RUN_WAIT // 60} min ({waits}/{MAX_FAILED_WAITS})", "warn")
                wake = datetime.datetime.now() + datetime.timedelta(seconds=FAILED_RUN_WAIT)
                if NOTIFY["on"]:
                    notify("tandem", f"campaign {camp_dir.name}: both agents failing, waiting until {wake:%H:%M}")
                if not wait_until(wake, stop_file):
                    break
                state["failures"] = {"claude": 0, "codex": 0}
                save(run_dir, state)
            outcome = state["outcome"]
            if outcome == "over_budget":
                # Not a finished goal: `campaign resume --max-cost` carries on with this same run.
                camp["outcome"] = "over_budget"
                write_json(path, camp)
                log(f"campaign over_budget: {state['outcome_message']}", "warn")
                return
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
                log("campaign paused: both agents kept failing after waiting; resume later", "warn")
                return
            if camp["no_progress"] >= MAX_GOALS_WITHOUT_PROGRESS:
                log(f"campaign paused: {camp['no_progress']} goals in a row ended without progress; see BLOCKERS.md", "warn")
                return
            if stop_file.exists():
                log("campaign stopped by the owner")
                return

        # The cap covers every run and planner call together; /budget may have changed it since.
        apply_tuning(camp, [camp_dir / "tuning.json"])
        cap = camp["config"].get("max_cost")
        if cap and camp["cost_usd"] >= cap:
            camp["outcome"] = "over_budget"
            write_json(path, camp)
            log(f"campaign over_budget: spent ${camp['cost_usd']:.2f} of the ${cap:.2f} cap; "
                f"`{prog_name()} campaign resume {camp_dir} --max-cost {cap * 2:g}` continues", "warn")
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
            if cap and camp["cost_usd"] >= cap and not (plan and plan.get("finished")):
                # Keep a paid-for goal for resume, but stop before another planner or run starts.
                if plan:
                    camp["queue"].append({"goal": plan["goal"], "name": slug(plan["name"]) or "goal",
                                          "first": plan.get("first") if plan.get("first") in NAMES else "claude",
                                          "check": plan.get("check", "")})
                    write_json(path, camp)
                continue
            if plan is None:
                waits = [w for w in map(unavailable_until, NAMES) if w]
                wake = min(waits) if len(waits) == len(NAMES) else \
                    datetime.datetime.now() + datetime.timedelta(seconds=PLAN_RETRY_WAIT)
                log(f"neither agent could plan the next goal; retrying at {wake:%H:%M}", "warn")
                if NOTIFY["on"] and len(waits) == len(NAMES):
                    notify("tandem", f"campaign {camp_dir.name}: both agents at usage limits, "
                                     f"waiting until {wake:%H:%M}")
                wait_until(wake, stop_file)
                continue
            if plan.get("finished"):
                camp["outcome"] = "finished"
                write_json(path, camp)
                log(f"campaign finished: {plan.get('reason')}", "ok")
                return
            item = {"goal": plan["goal"], "name": slug(plan["name"]) or "goal",
                    "first": plan.get("first") if plan.get("first") in NAMES else "claude",
                    "check": plan.get("check", "")}
            log(f"next goal: {item['name']} - {plan.get('reason')}", "run")
        n = len(camp["runs"]) + 1
        # The next run starts from whatever /models set since the last one began.
        apply_tuning(camp, [camp_dir / "tuning.json"])
        cfg = dict(camp["config"], check=item.get("check") or None)
        run_dir = create_run(camp_dir / f"{n:02d}-{slug(item['name'])}", camp["repo"], item["goal"],
                             item["name"], cfg, item.get("max_turns", camp["max_turns"]), item.get("first", "claude"),
                             worktree=camp["worktree"], branch=camp["branch"],
                             extra={"stop_file": str(stop_file), "tuning_files": [str(camp_dir / "tuning.json")],
                                    "campaign": str(camp_dir), "prior_cost": camp["cost_usd"],
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
        camp.update(repo=state["repo"], worktree=state["worktree"], branch=state["branch"], base=state["base"])
        state.setdefault("name", run_dir.name)
        state["inboxes"] = [str(run_dir / "inbox.md"), str(camp_dir / "inbox.md")]
        state["stop_file"] = str(camp_dir / "STOP")
        # The campaign adds the whole run's cost when it ends, so the cap counts it whole too.
        state.update(campaign=str(camp_dir), prior_cost=0.0)
        state["tuning_files"] = [str(run_dir / "tuning.json"), str(camp_dir / "tuning.json")]
        state["max_turns"] = state["turn"] - 1 + args.max_turns
        state["failures"] = {"claude": 0, "codex": 0}
        state["config"].update({k: v for k, v in cfg.items() if v is not None and k != "check"})
        save(run_dir, state)
        camp["current"] = str(run_dir)
    else:
        repo = require_repo(args.repo)
        branch = f"tandem/campaign-{camp_id}"
        worktree = str(camp_dir / "work")
        base = git(repo, "rev-parse", args.base)
        git(repo, "worktree", "add", "-q", "-b", branch, worktree, base)
        camp.update(repo=str(repo), worktree=worktree, branch=branch, base=base)
    camp["queue"] = [read_goal_file(p) for p in args.goal_file or []]
    write_json(camp_dir / "campaign.json", camp)
    CURRENT["target"] = camp_dir
    log(f"campaign {camp_dir.name} on {camp['branch']} ({camp['worktree']})", brief="")
    log(f"talk: {prog_name()} campaign say {camp_dir} \"...\"   stop: {prog_name()} campaign stop {camp_dir}", brief="")
    if in_session():
        emit(CONSOLE.rule(f"campaign {camp_dir.name} \u00b7 {camp['branch']}"))
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
    camp.pop("outcome", None)
    if getattr(args, "max_cost", None):
        # Later wins in tuning.json, so an older /budget there would undo the new cap.
        write_tuning(camp_dir, {"max_cost": args.max_cost})
    if args.push is not None:
        camp["push"] = args.push
    changes = {k: v for k, v in (("codex_effort", args.codex_effort), ("codex_role", args.codex_role),
                                 ("max_cost", getattr(args, "max_cost", None))) if v}
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
    if in_session():
        emit(CONSOLE.rule(f"campaign {camp_dir.name} · {camp['branch']} · resumed"))
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
    # The only hint for start; in the session typing and /say already talk to the agents.
    log(f"talk to the agents: {prog_name()} say {run_dir} \"...\"", brief="")
    return run_dir


def cmd_start(args):
    run_dir = prepare_run(args)
    loop(run_dir)


def outcome_exit_code(outcome):
    return {"done": 0, "max_turns": 2, "blocked": 3, "stopped": 4, "failed": 5, "over_budget": 6}.get(outcome, 1)


def headless_result(run_dir, state):
    history = state.get("history", [])
    facts = {"commits": None, "diffstat": None}
    if state.get("repo") and state.get("branch") and state.get("base"):
        facts = branch_facts(state["repo"], state["branch"], state["base"])
    return {
        "run_id": run_dir.name if run_dir else None,
        "name": state.get("name"), "repo": state.get("repo"), "branch": state.get("branch"),
        "base": state.get("base"), "worktree": state.get("worktree"),
        "commits": facts["commits"], "diffstat": facts["diffstat"], "outcome": state.get("outcome"),
        "outcome_message": state.get("outcome_message", ""), "turn_count": len(history),
        "cost_usd": state.get("cost_usd", 0.0), "max_cost": (state.get("config") or {}).get("max_cost"),
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
    if getattr(args, "max_cost", None):
        state["config"]["max_cost"] = args.max_cost
        # The loop re-reads tuning.json every turn, so an older /budget there would undo the new cap.
        tuning = state.get("tuning_files") or [str(run_dir / "tuning.json")]
        write_tuning(pathlib.Path(tuning[-1]).parent, {"max_cost": args.max_cost})
    state["failures"] = {"claude": 0, "codex": 0}
    save(run_dir, state)
    # A STOP left by the interactive /stop would end the resumed run at once. Only clear the run's own
    # file; a run inside a campaign points at the campaign's STOP, which the owner controls.
    stop = state.get("stop_file")
    if stop and pathlib.Path(stop).resolve().parent == run_dir.resolve():
        pathlib.Path(stop).unlink(missing_ok=True)
    loop(run_dir)


def queued(target):
    """The messages waiting in target's inbox for the next turn, oldest first."""
    try:
        text = (target / "inbox.md").read_text()
    except OSError:
        return []
    return [m.strip() for m in text.split("\n\n") if m.strip()]


def leave_message(target, message, force=False):
    """Queue a message for the next turn. Past MAX_QUEUED waiting ones it refuses, unless forced: the
    agent reads them all at once, and a long backlog of notes it cannot answer one by one is more
    likely to contradict itself than to help. /now (forced) ends the wait instead."""
    # Blank lines separate the messages in the inbox, so a message keeps none of its own.
    message = re.sub(r"\n\s*\n", "\n", message.strip())
    with INBOX_LOCK:
        if not force and len(queued(target)) >= MAX_QUEUED:
            raise ValueError(f"{MAX_QUEUED} messages already wait for the next turn; /queue shows them, "
                             f"/queue clear drops them, /now hands them over now (\u2191 brings your line back)")
        with open(target / "inbox.md", "a") as f:
            f.write(message + "\n\n")
        if (target / "campaign.json").exists():
            # The next turn consumes the inbox; the planner keeps reading notes.md for every later goal.
            with open(target / "notes.md", "a") as f:
                f.write(f"- ({now()}) {message}\n")
        return len(queued(target))


def clear_queue(target):
    with INBOX_LOCK:
        dropped = len(queued(target))
        if dropped:
            (target / "inbox.md").write_text("")
    return dropped


def cmd_say(args):
    waiting = leave_message(run_path(args.run), args.message)
    emit(f"queued for the next turn ({waiting} waiting)")


def activity_files(root):
    """The activity logs under root: a run's own, a campaign's and its runs', or every run's in RUNS."""
    return [*root.glob(ACTIVITY), *root.glob("*/" + ACTIVITY), *root.glob("*/*/" + ACTIVITY)]


def watch_line(line, width, fancy):
    stamp, _, rest = line.partition("\t")
    who, _, text = rest.partition("\t")
    text = fit_width(text, max(width - 17, 10))
    if not fancy:
        return f"{stamp} {who:<6} {text}"
    color = f"38;5;{AGENT_COLORS[who]}" if who in AGENT_COLORS else "2"
    if text.startswith("\u2500"):
        return f"\x1b[2m{stamp}\x1b[0m \x1b[1;{color}m{who:<6} {text}\x1b[0m"
    if text.startswith(("=", "\u2717")):
        text = f"\x1b[1m{text}\x1b[0m"
    return f"\x1b[2m{stamp}\x1b[0m \x1b[{color}m{who:<6}\x1b[0m {text}"


def cmd_watch(args):
    """Follow what the agents do, as it happens. Meant for a pane beside the session (/watch opens one):
    without a run it follows whichever run in RUNS acted last, so it keeps up with every new goal."""
    root = run_path(args.run) if args.run else RUNS
    fancy = fancy_terminal()
    print(f"tandem watch \u00b7 {root} \u00b7 Ctrl+C closes", flush=True)
    offsets, current, scanned = {}, None, float("-inf")  # monotonic() may start near 0
    try:
        while True:
            if time.monotonic() - scanned >= 1:
                scanned = time.monotonic()
                try:
                    newest = max(activity_files(root), key=lambda f: f.stat().st_mtime, default=None)
                except OSError:
                    newest = None  # a run directory removed mid-scan; the next scan settles it
                if newest and newest != current:
                    current = newest
                    print(f"\n{current.parent.name}", flush=True)
                    if current not in offsets:  # first sight: the last few lines, then everything new
                        size = current.stat().st_size
                        start = max(size - 4096, 0)
                        with open(current, "rb") as f:
                            f.seek(start)
                            head = f.read(size - start)
                        offsets[current] = start + (head.find(b"\n") + 1 if start else 0)
                elif newest is None and current is None and not offsets:
                    offsets[None] = 0
                    print("waiting for a turn to start", flush=True)
            if current:
                try:
                    with open(current, "rb") as f:
                        f.seek(offsets[current])
                        chunk = f.read()
                except OSError:
                    current, chunk = None, b""  # its run was removed; the next scan finds another
                complete = chunk[:chunk.rfind(b"\n") + 1]  # a line still being written waits
                offsets[current] += len(complete)
                width = term_width()
                for line in complete.decode("utf-8", "replace").splitlines()[-200:]:
                    print(watch_line(line, width, fancy), flush=True)
            time.sleep(0.3)
    except KeyboardInterrupt:
        print()


def cmd_status(args):
    run_dir = run_path(args.run)
    state = load(run_dir)
    emit(f"{state.get('name') or run_dir.name}: branch {state['branch']}  turn {state['turn'] - 1}/{state['max_turns']}  "
          f"outcome {state.get('outcome', 'running or interrupted')}  claude cost ${state['cost_usd']:.2f}")
    for h in state["history"]:
        emit(f"  {h['turn']:>2} {h['agent']:<6} {h['status']:<8} {h.get('commit') or '-':<8} "
              f"{(h.get('summary') or h.get('error') or '')[:100]}")


def cmd_diff(args):
    if args.run:
        target = run_path(args.run)
    else:
        rows = list_runs(1)
        if not rows:
            emit(f"no runs yet in {RUNS}")
            return
        target = pathlib.Path(rows[0]["path"])
    show_diff(target)


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
    off = read_config().get("notify", True) is False
    checks.append(("notifications", True, "off (\"notify\": false in ~/.tandem/config.json)" if off
                   else notifier_report()))
    for name, ok, detail in checks:
        emit(f"  {'ok  ' if ok else 'FAIL'}  {name:<18} {detail}")
    if not all(ok for _, ok, _ in checks):
        sys.exit(1)
    emit("ready")


MAX_COST_HELP = ("stop before the next turn once this much is spent; counts the cost the CLIs report, "
                 "so Claude only (Codex bills through its subscription)")


def positive_usd(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an amount: {text!r}")
    if not 0 < value < float("inf"):  # also refuses nan
        raise argparse.ArgumentTypeError("must be more than 0 and finite")
    return value


def add_agent_options(p, max_turns):
    p.add_argument("--max-turns", type=int, default=max_turns, help="turns per goal")
    p.add_argument("--timeout", type=int, default=1800, help="seconds per turn")
    p.add_argument("--claude-model")
    p.add_argument("--claude-effort", choices=CLAUDE_EFFORTS, help="Claude effort (default: Claude Code's own)")
    p.add_argument("--claude-budget", type=float, help="max USD per Claude turn")
    p.add_argument("--max-cost", type=positive_usd, metavar="USD", help=MAX_COST_HELP)
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
    add_notify_option(p)


NO_NOTIFY_HELP = "no desktop notification or bell when the run ends or waits for usage limits"


def add_notify_option(p):
    p.add_argument("--no-notify", action="store_true", help=NO_NOTIFY_HELP)


HELP_DESCRIPTION = """\
Claude Code and Codex take turns on your git repository until they agree the goal is done.

usage shapes:
  %(prog)s                 interactive session in the current repo (needs a terminal)
  %(prog)s -p "GOAL"       one run, then exit; the exit code says how it ended
  %(prog)s COMMAND ...     one of the commands below
"""

HELP_EPILOG = """\
-p takes the options of `start`: --repo --check --max-turns --first --name --base,
--claude-model --claude-effort --codex-model --codex-effort --codex-role --codex-network --claude-budget --timeout,
--max-cost USD (checked before each turn; counts reported cost only, which is Claude's: Codex reports none).
The session, start, resume and campaigns notify when a run ends; --no-notify silences that, -p --notify asks for it.
-p exit codes: 0 done, 1 error, 2 max_turns, 3 blocked, 4 stopped, 5 failed, 6 over_budget.
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
    p.add_argument("--notify", action="store_true", help="with -p: notify when the run ends, as start does")
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
    r.add_argument("--max-cost", type=positive_usd, metavar="USD", help="set or raise the run's spending cap")
    add_notify_option(r)
    r.set_defaults(fn=cmd_resume)

    m = sub.add_parser("say", help="leave a message for the next turn (run or campaign directory)")
    m.add_argument("run")
    m.add_argument("message")
    m.set_defaults(fn=cmd_say)

    w = sub.add_parser("watch", help="follow what the agents do as they work (a pane beside the session)")
    w.add_argument("run", nargs="?", help="a run or campaign; default: whichever run acted last")
    w.set_defaults(fn=cmd_watch)

    t = sub.add_parser("status", help="one line per turn")
    t.add_argument("run")
    t.set_defaults(fn=cmd_status)

    f = sub.add_parser("diff", help="what a run changed and the commands to read, merge and clean it up")
    f.add_argument("run", nargs="?", help="a run or campaign; default: the newest")
    f.set_defaults(fn=cmd_diff)

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
    add_notify_option(cs)
    cs.set_defaults(fn=cmd_campaign_start)
    for name, fn in (("resume", cmd_campaign_resume), ("stop", cmd_campaign_stop), ("status", cmd_campaign_status)):
        x = csub.add_parser(name)
        x.add_argument("dir")
        if name == "resume":
            x.add_argument("--push", action=argparse.BooleanOptionalAction, default=None,
                           help="turn pushing after every goal on or off")
            x.add_argument("--codex-effort", choices=EFFORTS, help="change Codex reasoning effort from now on")
            x.add_argument("--codex-role", choices=["peer", "reviewer"], help="change how Codex takes part from now on")
            x.add_argument("--max-cost", type=positive_usd, metavar="USD",
                           help="set or raise the spending cap for all the campaign's goals together")
            x.add_argument("--seed-review", metavar="FROM_SHA:NOTE", help="add a review backlog item from FROM_SHA to HEAD")
            add_notify_option(x)
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

# The single list of slash commands: the parser, /help and the menu under the prompt all read it.
SLASH = [
    ("help", "", "show this list"),
    ("status", "", "one line per turn of the current run"),
    ("board", "", "what the agents agree on, what is open, models and cost"),
    ("models", "", "pick Claude and Codex model and effort"),
    ("budget", "[USD|off]", "show or change the reported-cost cap from the next turn"),
    ("say", "TEXT", "leave a message the next turn reads"),
    ("paste", "", "attach the clipboard's image to your next goal or /say"),
    ("stop", "", "stop after the current turn"),
    ("now", "TEXT", "stop the current turn and hand it your message"),
    ("queue", "[clear]", "the messages waiting for the next turn; clear drops them"),
    ("watch", "", "follow what the agents do, in a pane on the right"),
    ("resume", "[TURNS]", "continue the last run, optionally allowing TURNS more"),
    ("goal", "FILE", "start a run from a goal file"),
    ("campaign", "ARGS...", "same as `tandem campaign ARGS...` (start, resume, stop, status, say)"),
    ("runs", "", "recent runs and campaigns"),
    ("diff", "", "what the run changed and the commands to read, merge and clean it up"),
    ("transcript", "", "print the transcript path"),
    ("doctor", "", "check tools and logins"),
    ("quit", "", "leave (also Ctrl+D, or Ctrl+C twice)"),
]
SLASH_COMMANDS = [name for name, _, _ in SLASH]
# /help has room for more than one menu line.
HELP_MORE = {"models": " (saved as the default;\na running run takes it from its next turn)",
             "paste": "\n(or drag image files onto the terminal; /paste clear drops them)"}


def slash_help():
    lines = ["type a goal to start a run in this repo; while it runs, type a correction to step in"]
    for name, args, text in SLASH:
        if name == "paste":
            lines.append(f"  {'Ctrl+V':<21}paste the clipboard's image into the line as [image #N]")
        first, *more = (text + HELP_MORE.get(name, "")).split("\n")
        lines.append(f"  {('/' + name + ' ' + args).strip():<21}{first}")
        lines += [" " * 23 + line for line in more]
        if name == "models":
            lines.append(f"  {'Tab':<21}on an empty line: change them on the status bar, ↑↓ and Enter")
    return "\n".join(lines)


SLASH_HELP = slash_help()


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
    data = read_config()
    data["consent"] = {"version": __version__, "date": datetime.date.today().isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


def read_config():
    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_defaults():
    saved = read_config().get("defaults")
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items() if k in TUNABLE}


def save_defaults(values):
    """Merge into the saved defaults; None forgets a key. Other config keys (consent) stay."""
    data = read_config()
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    for k, v in values.items():
        if k not in TUNABLE:
            continue
        if v is None:
            defaults.pop(k, None)
        else:
            defaults[k] = v
    data["defaults"] = defaults
    config_path().parent.mkdir(parents=True, exist_ok=True)
    write_json(config_path(), data)


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
        lines.append("type a goal, Ctrl+V to paste an image, /models, or /help")
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
    lines += ["", "  " + paint("2", "type a goal · Ctrl+V pastes an image · /models · /help"), ""]
    return "\n".join(lines)


def fancy_terminal():
    """Escape codes and the status line only on a real terminal, and never with NO_COLOR."""
    return (sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


# Codex keeps the models an account can use in its own cache; tandem only reads it.
CODEX_MODELS_CACHE = pathlib.Path.home() / ".codex" / "models_cache.json"
CLAUDE_MODELS = [("fable", "Newest model"), ("opus", "Most capable; slower, pricier"),
                 ("sonnet", "Fast and strong; the everyday choice"), ("haiku", "Fastest and cheapest")]
DEFAULT_NOTE = "the CLI's own setting"
EFFORT_NOTES = {"low": "quick, light reasoning", "medium": "balanced speed and depth",
                "high": "deeper reasoning for hard problems", "xhigh": "extra deep reasoning",
                "max": "the deepest reasoning; slowest", "ultra": "max reasoning with task delegation"}
PICKER_HINT = "↑↓ move · ←→ change · Tab next · Enter save · Esc cancel"


def codex_models():
    """[(slug, description, [(effort, description), ...])] from Codex's cache; [] when it is unreadable."""
    try:
        data = json.loads(CODEX_MODELS_CACHE.read_text())
    except (OSError, ValueError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    found = []
    for m in models if isinstance(models, list) else []:
        if not isinstance(m, dict) or not isinstance(m.get("slug"), str) or m.get("visibility", "list") != "list":
            continue
        levels = m.get("supported_reasoning_levels")
        levels = [(lv["effort"], lv.get("description") or EFFORT_NOTES.get(lv["effort"], ""))
                  for lv in (levels if isinstance(levels, list) else [])
                  if isinstance(lv, dict) and isinstance(lv.get("effort"), str)]
        found.append((m["slug"], m.get("description") or "", levels))
    return found


def choice_row(key, label, choices, current):
    """One picker row from [(value, note)]. A saved value the list lacks stays, right after default."""
    values = ["default"] + [v for v, _ in choices]
    if current is not None and current not in values:
        values.insert(1, current)
    return {"key": key, "label": label, "values": values, "notes": {"default": DEFAULT_NOTE, **dict(choices)},
            "index": values.index(current) if current is not None else 0}


def codex_effort_row(levels, model, current):
    # Codex rejects an effort its model does not support, so offer only that model's levels.
    choices = levels.get(model) or [(e, EFFORT_NOTES[e]) for e in EFFORTS]
    return choice_row("codex_effort", "Codex effort", choices, current)


def model_choices(current):
    """The /models picker's four rows, each selecting current[key] (None is default)."""
    codex = codex_models()
    levels = {slug: lv for slug, _, lv in codex}
    rows = [choice_row("claude_model", "Claude model", CLAUDE_MODELS, current.get("claude_model")),
            choice_row("claude_effort", "Claude effort", [(e, EFFORT_NOTES[e]) for e in CLAUDE_EFFORTS],
                       current.get("claude_effort")),
            choice_row("codex_model", "Codex model", [(slug, note) for slug, note, _ in codex],
                       current.get("codex_model")),
            codex_effort_row(levels, current.get("codex_model"), current.get("codex_effort"))]
    rows[2]["levels"] = levels
    return rows


PICKER_MOVES = {"up": -1, "k": -1, "shift-tab": -1, "down": 1, "j": 1, "tab": 1}
PICKER_CHANGES = {"left": -1, "h": -1, "right": 1, "l": 1}


def picker_step(rows, focus, key):
    """Apply one key to the picker. Returns (focus, action); action is None, "save" or "cancel"."""
    if not isinstance(key, str):
        return focus, None  # a paste or another event the picker has no use for
    if key == "enter":
        return focus, "save"
    if key in ("esc", "q", "ctrl-c"):
        return focus, "cancel"
    if key in PICKER_MOVES:
        return (focus + PICKER_MOVES[key]) % len(rows), None
    if key in PICKER_CHANGES:
        step_choice(rows, focus, PICKER_CHANGES[key])
    return focus, None


def step_choice(rows, focus, step):
    """Move rows[focus] step values on, wrapping around. Shared by the picker and the status bar."""
    row = rows[focus]
    row["index"] = (row["index"] + step) % len(row["values"])
    if row["key"] == "codex_model":
        efforts = next(r for r in rows if r["key"] == "codex_effort")
        kept = efforts["values"][efforts["index"]]
        fresh = codex_effort_row(row.get("levels", {}), row["values"][row["index"]], None)
        if kept in fresh["values"]:
            fresh["index"] = fresh["values"].index(kept)
        efforts.update(fresh)


def chosen(rows):
    """{key: value} the picker rows select, None for default."""
    return {r["key"]: (None if r["values"][r["index"]] == "default" else r["values"][r["index"]]) for r in rows}


def render_picker(rows, focus, width):
    """The picker's frame. Every line stays narrower than the terminal: a wrapped line would throw off
    the line count the in-place redraw moves up by."""
    def fit(text):
        return text if len(text) < width else text[:max(width - 2, 0)] + "…"

    lines = ["\x1b[1m" + fit("Models and effort") + "\x1b[0m", ""]
    for i, row in enumerate(rows):
        text = fit(f"{'▸' if i == focus else ' '} {row['label']:<16}‹ {row['values'][row['index']]} ›")
        lines.append(f"\x1b[1m{text}\x1b[0m" if i == focus else text)
    row = rows[focus]
    note = row["notes"].get(row["values"][row["index"]], "")
    lines += ["", fit("  " + note), "", "\x1b[2m" + fit(PICKER_HINT) + "\x1b[0m"]
    return "\n".join(lines)


KEYS = {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left", "\x1b[Z": "shift-tab",
        "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
        "\x1b[H": "home", "\x1bOH": "home", "\x1b[1~": "home", "\x1b[7~": "home",
        "\x1b[F": "end", "\x1bOF": "end", "\x1b[4~": "end", "\x1b[8~": "end", "\x1b[3~": "delete",
        "\t": "tab", "\r": "enter", "\n": "enter", "\x1b": "esc", "\x7f": "backspace", "\x08": "backspace",
        "\x01": "ctrl-a", "\x03": "ctrl-c", "\x04": "ctrl-d", "\x05": "ctrl-e", "\x0b": "ctrl-k",
        "\x15": "ctrl-u", "\x16": "ctrl-v", "\x17": "ctrl-w"}
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


def read_key(fd):
    """One key press from a terminal in cbreak mode: a name from KEYS, else the character typed
    (an unknown escape sequence comes back whole), or ("paste", text) for a bracketed paste.
    "" means stdin is closed."""
    data = os.read(fd, 1)
    if data == b"\x1b":
        # A lone Esc and the first byte of an arrow key are the same; only a pause tells them apart.
        if select.select([fd], [], [], 0.05)[0]:
            data += os.read(fd, 1)
            if data[1:] in (b"[", b"O"):
                while True:  # a CSI sequence ends with a byte from @ to ~
                    c = os.read(fd, 1)
                    data += c
                    if not c or 0x40 <= c[0] <= 0x7e:
                        break
        if data == PASTE_START:
            pasted = b""
            while not pasted.endswith(PASTE_END):
                c = os.read(fd, 1)
                if not c:
                    break
                pasted += c
            return ("paste", pasted[:-len(PASTE_END)].decode(errors="replace"))
    elif data and data[0] >= 0xc0:
        data += os.read(fd, 3 if data[0] >= 0xf0 else 2 if data[0] >= 0xe0 else 1)  # rest of a UTF-8 char
    text = data.decode(errors="replace")
    return KEYS.get(text, text)


def raw_keys(fd):
    """Put the terminal in cbreak mode without signals and return the settings to restore.

    Without ISIG, Ctrl+C comes in as a key, never as a KeyboardInterrupt in the middle of a redraw, and
    the terminal does not echo ^C where the cursor is. Without IEXTEN, Ctrl+V reaches us instead of
    quoting the next key. TCSANOW, unlike tty.setcbreak, keeps keys typed ahead."""
    saved = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
    mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
    termios.tcsetattr(fd, termios.TCSANOW, mode)
    return saved


def run_picker(rows):
    """Let the owner choose on the terminal. Returns {key: value, None for default} on Enter, None on cancel."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    focus, drawn, action = 0, 0, None
    try:
        raw_keys(fd)
        while action is None:
            frame = render_picker(rows, focus, term_width())
            with OUT_LOCK:
                sys.stdout.write((f"\x1b[{drawn}A" if drawn else "\x1b[?25l") + "\r\x1b[J" + frame + "\n")
                sys.stdout.flush()
            drawn = frame.count("\n") + 1
            key = read_key(fd)
            focus, action = picker_step(rows, focus, key) if key else (focus, "cancel")
    except KeyboardInterrupt:
        action = "cancel"  # a SIGINT from outside the terminal
    finally:
        # Even on SIGHUP or an error: the prompt after the picker needs line input and a cursor.
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        with contextlib.suppress(OSError), OUT_LOCK:
            sys.stdout.write((f"\x1b[{drawn}A\r\x1b[J" if drawn else "") + "\x1b[?25h")
            sys.stdout.flush()
    return chosen(rows) if action == "save" else None


def models_line(values):
    v = {k: values.get(k) or "default" for k in TUNABLE}
    return f"claude {v['claude_model']}/{v['claude_effort']} · codex {v['codex_model']}/{v['codex_effort']}"


# ---------- the prompt line ----------

MENU_SIZE = 8
MENU_HINT = "↑↓ select · Tab or → complete · Enter run · Esc close"
BAR_HINT = "Tab or /models to change"
BAR_EDIT_HINT = "Tab next · ↑↓ change · Enter save · Esc cancel"
HISTORY_SIZE = 500
EXIT_ARM_SECONDS = 2  # a second Ctrl+C on an empty prompt within this many seconds leaves, as in Claude Code
EXIT_WORDS = ("exit", "quit", "q")  # typed alone, they leave like /quit instead of becoming a goal
NAMED_KEYS = set(KEYS.values())


def history_path():
    return pathlib.Path.home() / ".tandem" / "history"


ESCAPE_CODE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def char_width(c):
    return 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def display_width(text):
    """Columns text takes on a terminal: wide East Asian characters take two, combining marks and
    escape codes none."""
    return sum(map(char_width, ESCAPE_CODE.sub("", text)))


def fit_width(text, width):
    """The longest start of text that takes at most width columns. Escape codes are kept whole and
    take no room; a colored line cut short gets a reset, so its color cannot run into the next row."""
    used, i = 0, 0
    while i < len(text):
        code = ESCAPE_CODE.match(text, i)
        if code:
            i = code.end()
            continue
        used += char_width(text[i])
        if used > width:
            return text[:i] + ("\x1b[0m" if "\x1b[" in text[:i] else "")
        i += 1
    return text


def slash_matches(buffer):
    """The commands the menu offers for what is typed: names that start with it, then names that contain it."""
    if not buffer.startswith("/") or any(c.isspace() for c in buffer):
        return []
    typed = buffer[1:].lower()
    first = [c for c in SLASH if c[0].startswith(typed)]
    return (first + [c for c in SLASH if typed in c[0] and c not in first])[:MENU_SIZE]


def editor_terminal():
    """Our own line editor needs raw keys and cursor movement; anything less keeps readline's input()."""
    return (termios is not None and sys.stdin.isatty() and sys.stdout.isatty()
            and os.environ.get("TERM") != "dumb")


class LineEditor:
    """The prompt on a POSIX terminal. input() has no hook for each key, so the menu of slash commands
    could not follow the typing; this reads the keys itself.

    While it reads, it owns the rows from the status line (header) down to the menu: anything else
    printed then goes through Console.write, which erases those rows, prints, and draws them again.
    The prompt is one row that scrolls sideways, so the row count it moves up by is always right."""

    def __init__(self, fd_in, out, history_path, keys=None, paste=None, color=False):
        self.fd, self._out, self.history_path = fd_in, out, pathlib.Path(history_path)
        self.keys = keys  # a script of keys for tests, instead of the terminal
        self.paste = paste or (lambda: None)
        self.color = color
        self.header = lambda: []
        self.prompt, self.buffer, self.cursor, self.scroll = "", "", 0, 0
        self.selected, self.menu_closed = 0, False
        self.reading = False
        self.touched = False   # a key other than Ctrl+C came during the last read
        self.mid_line = False  # the last read left its line without a newline (Ctrl+C, Ctrl+D)
        self.kept = False      # the last read was an empty Enter: its rows are still on screen, as drawn
        self.resized = False  # SIGWINCH only sets this; output stays under OUT_LOCK
        self.above = 0         # rows drawn above the prompt row, where the cursor is
        # Tab on an empty line edits the models on the status bar: models() gives the picker's rows
        # (None turns the mode off), save_models(changed, values) gets the choice after Enter.
        self.models = lambda: None
        self.save_models = lambda changed, values: None
        self.echo = lambda line: self.prompt + line  # how a submitted line stays in the scrollback
        self.fields, self.field, self.fields_before, self.saving = None, 0, {}, None
        self.history = self.load_history()
        self.hist_pos, self.draft = len(self.history), ""

    @property
    def out(self):
        return self._out or sys.stdout  # looked up on each write: SIGHUP swaps sys.stdout for /dev/null

    def write(self, text):
        self.out.write(text)
        self.out.flush()

    def footer_lines(self, width):
        return []  # rows under the menu, drawn on every redraw

    def load_history(self):
        try:
            lines = self.history_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [line for line in lines if line.strip()][-HISTORY_SIZE:]

    def remember(self, line):
        if not line.strip() or (self.history and self.history[-1] == line):
            return
        self.history = (self.history + [line])[-HISTORY_SIZE:]
        with contextlib.suppress(OSError):
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text("".join(h + "\n" for h in self.history), encoding="utf-8")

    def next_key(self):
        return next(self.keys, "") if self.keys is not None else read_key(self.fd)

    def pending(self):
        """More keys already waiting (typed fast, or a paste without brackets): draw once after them."""
        return self.keys is None and bool(select.select([self.fd], [], [], 0)[0])

    def read(self, prompt, history=True):
        """One line, like input(): EOFError on Ctrl+D on an empty line, KeyboardInterrupt on Ctrl+C."""
        # Saved outside the try and changed inside it, as in run_picker: a SIGINT landing right after
        # the switch to raw mode must still put the owner's terminal back.
        saved = termios.tcgetattr(self.fd) if self.keys is None else None
        line = None
        try:
            if saved is not None:
                raw_keys(self.fd)
            with OUT_LOCK:
                self.prompt, self.buffer, self.cursor, self.scroll = prompt, "", 0, 0
                self.selected, self.menu_closed = 0, False
                self.hist_pos, self.draft = len(self.history), ""
                self.fields, self.saving = None, None
                fresh = "\n" if self.mid_line else ""
                if not self.kept:
                    self.above = 0
                self.mid_line, self.kept, self.reading, self.touched = False, False, True, False
                self.write(fresh + "\x1b[?2004h" + self.redraw())
            while line is None:
                key = self.next_key()
                self.touched = self.touched or key != "ctrl-c"
                if key == "ctrl-v":
                    key = ("image", self.paste())  # outside OUT_LOCK: the clipboard tool may take seconds
                with OUT_LOCK:
                    line = self.apply(key)
                    saving, self.saving = self.saving, None
                    if line is None and not self.pending():
                        self.write(self.redraw())
                if saving:
                    self.save_models(*saving)  # outside OUT_LOCK: it writes files and prints
        finally:
            with OUT_LOCK:
                self.reading = False
                # An empty Enter changes nothing on screen: a new prompt row for each one would push the
                # status line up. The next read draws over the same rows.
                self.kept = line is not None and not line.strip()
                # Otherwise the line stays as typed, without the menu. After Enter the cursor goes below
                # it; after Ctrl+C or Ctrl+D it stays at the end, and the next output starts a fresh line.
                with contextlib.suppress(OSError):
                    self.write("\x1b[?2004l" if self.kept else self.erase()
                               + (self.echo(line) + "\n" if line is not None else self.prompt + self.buffer)
                               + "\x1b[?2004l")
                self.mid_line = line is None
            if saved is not None:
                with contextlib.suppress(OSError, termios.error):
                    termios.tcsetattr(self.fd, termios.TCSADRAIN, saved)
        if history:
            self.remember(line)
        return line

    def matches(self):
        found = [] if self.menu_closed else slash_matches(self.buffer)
        self.selected = min(self.selected, max(len(found) - 1, 0))
        return found

    def edit(self, buffer, cursor):
        if buffer != self.buffer:
            self.selected, self.menu_closed = 0, False
        self.buffer, self.cursor = buffer, cursor

    def insert(self, text):
        self.edit(self.buffer[:self.cursor] + text + self.buffer[self.cursor:], self.cursor + len(text))

    def recall(self, step):
        pos = min(max(self.hist_pos + step, 0), len(self.history))
        if pos == self.hist_pos:
            return  # already at the oldest line, or back at the one being typed
        if self.hist_pos == len(self.history):
            self.draft = self.buffer
        self.hist_pos = pos
        text = self.history[pos] if pos < len(self.history) else self.draft
        self.edit(text, len(text))
        self.menu_closed = True  # ↑ again goes further back, not into a menu for the recalled command

    def apply(self, key):
        """Apply one key. Returns the line when it is submitted, else None."""
        if self.fields is not None and self.field_key(key):
            return None
        b, c, matches = self.buffer, self.cursor, self.matches()
        if isinstance(key, tuple):
            kind, text = key
            if kind == "image" and text is None:
                self.write("\a")  # no image on the clipboard
            elif kind == "image":
                self.insert(text if not c or b[c - 1].isspace() else " " + text)
            else:  # a paste is one line: its newlines become spaces, and no escape code reaches the screen
                self.insert(CONTROL_CHARS.sub(" ", text.replace("\r\n", "\n")))
        elif key == "":
            raise EOFError
        elif key == "ctrl-c":
            raise KeyboardInterrupt
        elif key == "ctrl-d":
            if not b:
                raise EOFError
            self.edit(b[:c] + b[c + 1:], c)
        elif key == "enter":
            if matches and b[1:].lower() not in SLASH_COMMANDS:
                self.edit("/" + matches[self.selected][0], len(matches[self.selected][0]) + 1)
            return self.buffer
        elif key == "tab" or (key == "right" and matches and c == len(b)):
            if matches:  # → at the end of the line accepts the suggestion, as in fish and zsh
                name = matches[self.selected][0]
                self.edit(f"/{name} ", len(name) + 2)
            elif key == "tab" and not b:
                rows = self.models()
                if rows:
                    self.fields, self.field, self.fields_before = rows, 0, chosen(rows)
        elif key == "esc":
            self.menu_closed = True
        elif key in ("up", "down"):
            if matches:
                self.selected = (self.selected + (1 if key == "down" else -1)) % len(matches)
            else:
                self.recall(1 if key == "down" else -1)
        elif key == "left":
            self.cursor = max(c - 1, 0)
        elif key == "right":
            self.cursor = min(c + 1, len(b))
        elif key in ("home", "ctrl-a"):
            self.cursor = 0
        elif key in ("end", "ctrl-e"):
            self.cursor = len(b)
        elif key == "backspace":
            if c:
                self.edit(b[:c - 1] + b[c:], c - 1)
        elif key == "delete":
            self.edit(b[:c] + b[c + 1:], c)
        elif key == "ctrl-u":
            self.edit(b[c:], 0)
        elif key == "ctrl-k":
            self.edit(b[:c], c)
        elif key == "ctrl-w":
            start = re.search(r"\S*\s*$", b[:c]).start()
            self.edit(b[:start] + b[c:], start)
        elif key not in NAMED_KEYS and key.isprintable():
            self.insert(key)  # escape sequences and control keys we do not use type nothing
        return None

    def field_key(self, key):
        """A key while the status bar's models are being edited. False when it left the mode and
        still has to do what it does on the prompt, so typing right after never loses a character."""
        if key in ("tab", "shift-tab"):
            self.field = (self.field + (1 if key == "tab" else -1)) % len(self.fields)
        elif key in ("up", "down"):
            step_choice(self.fields, self.field, 1 if key == "up" else -1)
        else:
            values, self.fields = chosen(self.fields), None
            if key == "enter":
                # Only what the owner changed: the values shown may be the run's own, not defaults.
                changed = {k: v for k, v in values.items() if v != self.fields_before.get(k)}
                self.saving = (changed, values) if changed else None
            return key in ("enter", "esc")
        return True

    def editing(self):
        """(values, focused key) for the status bar while its models are being edited, else None."""
        return (chosen(self.fields), self.fields[self.field]["key"]) if self.fields else None

    def prompt_line(self, width):
        """(text, cursor column) of the prompt row. The text scrolls sideways so the cursor stays in view
        and the row never wraps."""
        prompt = fit_width(self.prompt, max(width - 2, 0))
        room = max(width - 1 - display_width(prompt), 1)
        start, used = self.cursor, 0  # the furthest left the row can start with the cursor still in view
        while start and used + display_width(self.buffer[start - 1]) <= room:
            start -= 1
            used += display_width(self.buffer[start])
        self.scroll = 0 if not start else min(max(self.scroll, start), self.cursor)
        shown = fit_width(self.buffer[self.scroll:], room)
        return prompt + shown, display_width(prompt) + display_width(self.buffer[self.scroll:self.cursor])

    def menu_lines(self, width):
        matches = self.matches()
        if not matches:
            return []
        pad = max(len(name) for name, _, _ in matches) + 1
        lines = []
        for i, (name, _, text) in enumerate(matches):
            mark = "▸ " if i == self.selected and not self.color else "  "
            line = fit_width(f"{mark}{'/' + name:<{pad}}   {text}", width - 1)
            lines.append(f"\x1b[7m{line}\x1b[0m" if i == self.selected and self.color else line)
        hint = fit_width("  " + MENU_HINT, width - 1)
        return lines + [f"\x1b[2m{hint}\x1b[0m" if self.color else hint]

    def to_top(self):
        if self.resized:
            self.resized = False
            self.above = 0
            # Terminals reflow and clamp the cursor differently on resize. Relative movement can
            # leave wrapped rows behind, so rebuild the visible screen from a known position.
            return "\x1b[H\x1b[J"
        return "\r" + (f"\x1b[{self.above}A" if self.above else "")

    def erase(self):
        """Clear every row the editor drew, leaving the cursor where the first one was."""
        out, self.above = self.to_top() + "\x1b[J", 0
        return out

    def render(self):
        """Draw the rows from where the cursor is, each line overwritten in place, and put the cursor back."""
        width = term_width()
        text, col = self.prompt_line(width)
        head = [fit_width(line, width - 1) for line in self.header()]
        below = self.menu_lines(width) + [fit_width(line, width - 1) for line in self.footer_lines(width)]
        self.above = len(head)
        return ("\x1b[K\n".join(head + [text] + below) + "\x1b[K\x1b[J"
                + (f"\x1b[{len(below)}A" if below else "") + "\r" + (f"\x1b[{col}C" if col else ""))

    def redraw(self):
        return self.to_top() + self.render()


class Console:
    """Prompt-aware output for the interactive session. write() is only called holding OUT_LOCK."""

    def __init__(self, fancy, styled=None, utf=None):
        self.fancy = fancy
        # styled: restyle the log, the owner's lines and the run rules, which a terminal without color
        # (NO_COLOR) still gets as symbols and dashes; fancy adds the colors.
        self.styled = fancy if styled is None else styled
        self.utf = "utf" in (sys.stdout.encoding or "").lower() if utf is None else utf
        self.prompt = ""
        self.prompt_active = False
        self.status_shown = False
        self.running = False
        self.held = None  # log lines kept back while the /models picker owns the screen
        self.editor = None  # the LineEditor when the terminal allows one, else input() with readline
        self.started = time.time()  # the bar's session time counts from here, not from the run
        self.cached = (None, None, None)  # (state.json, its mtime, its contents): read again only when it changes

    def attach(self, editor):
        self.editor = editor
        # The bar already says who is working and for how long; the line above the prompt is only
        # for work the bar cannot show yet, such as a campaign planning its first goal.
        editor.header = lambda: [self.status_text()] if self.running and not self.bar_state() else []
        editor.footer_lines = self.bar
        editor.models = lambda: model_choices(Session.current_models())
        if self.styled:
            editor.echo = self.owner_line

    def paint(self, style, text):
        return f"\x1b[{style}m{text}\x1b[0m" if self.fancy and style else text

    def owner_line(self, line):
        """A submitted line as it stays in the scrollback: the owner's marker on a band across the row
        (erasing to the end of the row paints the band there), so it is the first thing to find."""
        text = ("› " if self.utf else "> ") + line
        return f"\x1b[{OWNER_STYLE}m{text}\x1b[K\x1b[0m" if self.fancy else text

    def log_line(self, msg, kind=None):
        """Keep each session log entry on one row, with an explicit mark when text is clipped."""
        symbol, plain, style = LOG_MARKS.get(kind, (" ", " ", None))
        symbol = symbol if self.utf else plain
        width = max(term_width() - 1, 20)
        text_style = style if kind in ("turn", "warn", "error") else None
        head = self.paint("2", datetime.datetime.now().strftime("%H:%M")) + " " + self.paint(style, symbol) + " "
        text = CONTROL_CHARS.sub(" ", msg)
        room = width - display_width(head)
        if display_width(text) > room:
            more = "…" if self.utf else "..."
            text = fit_width(text, room - len(more)) + more
        return head + self.paint(text_style, text)

    def rule(self, title, style="1"):
        return rule_line(title, max(term_width() - 1, MIN_CARD_WIDTH), style, self.fancy, self.utf)

    def bar_state(self):
        """The state of the run the session works on, as last saved; None before there is one."""
        target = CURRENT.get("target")
        run = Session.current_run(target)[0] if target else None
        path = run / "state.json" if run else None
        try:
            mtime = path.stat().st_mtime_ns if path else None
            if path and self.cached[:2] != (path, mtime):
                self.cached = (path, mtime, json.loads(path.read_text()))
        except (OSError, ValueError):
            pass  # gone, or caught mid-write: the last copy read stays good enough for a second
        return self.cached[2] if path and self.cached[0] == path else None

    def bar(self, width):
        """The status bar under the prompt. Fit one column short: a full row would wrap on some terminals."""
        editing = self.editor.editing() if self.editor else None
        try:
            state = self.bar_state()
            # Typing hides the Tab hint, since Tab then completes; a run's bar has no room for the rest.
            hint = BAR_EDIT_HINT if editing else BAR_HINT if self.editor and not self.editor.buffer \
                else None if state else "/models to change"
            return status_bar(state, time.time(), width - 1, self.fancy, PROGRESS if self.running else {},
                              self.started, None if state else load_defaults(), editing, hint)
        except (KeyError, TypeError, ValueError, AttributeError):
            return []  # a state.json from an older tandem, or edited by hand: no bar rather than no prompt

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
        if self.held is not None:
            self.held.append(text)
        elif self.editor and self.editor.reading:
            self.editor.write(self.editor.erase() + text + "\n" + self.editor.render())
        elif self.editor:
            # A fresh line: after Ctrl+C the editor left its line open (the empty emit that follows only
            # closes it), and keys typed while a command runs, or the ^C the terminal echoes then, sit
            # where the cursor is.
            if self.editor.mid_line:
                out = "\n" + (text + "\n" if text else "")
            else:
                out = (self.editor.erase() if self.editor.kept else "\r\x1b[K") + text + "\n"
            self.editor.write(out)
            self.editor.mid_line = self.editor.kept = False
        elif not self.prompt_active:
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

    def alert(self, seq):
        """The bell and OSC 9 move no cursor, so they go out as they are between two redraws, even
        while the picker holds the log back."""
        if self.editor:
            self.editor.write(seq)
        else:
            sys.stdout.write(seq)
            sys.stdout.flush()

    @contextlib.contextmanager
    def hold(self):
        """Keep log lines back until the block ends, then print them. OUT_LOCK guards only the list:
        a worker logging during a turn must never wait for the owner's key press."""
        with OUT_LOCK:
            self.held = []
        try:
            yield
        finally:
            with OUT_LOCK, contextlib.suppress(OSError):
                lines, self.held = self.held, None
                for text in lines:
                    self.write(text)

    def interrupted_line(self):
        """After Ctrl+C at the prompt: (the line had text, a key was pressed before Ctrl+C)."""
        if self.editor:
            return bool(self.editor.buffer), self.editor.touched
        text = bool(self._buffer())  # readline does not tell keys from text, and libedit gives no text
        return text, text

    def tick(self):
        """Redraw the status line in place, without moving the cursor the user is typing at."""
        with OUT_LOCK:
            if self.editor:
                if self.editor.reading:  # the bar's clocks move even with no run going
                    self.editor.write(self.editor.redraw())
            elif self.fancy and self.running and self.prompt_active and self.status_shown:
                sys.stdout.write("\x1b7\x1b[1A\r\x1b[K" + self.status_text() + "\x1b8")
                sys.stdout.flush()

    def ask(self, prompt, history=True):
        if self.editor:
            self.prompt = prompt
            return self.editor.read(prompt, history)
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
        self.declined = None  # the turn (its start time) the owner chose not to stop; asked once per turn

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def note(self, text, kind=None):
        """A session message in the log's look, so it does not read like the owner's own line."""
        emit(self.console.log_line(text, kind) if in_session() else text)

    def refuse_if_busy(self):
        if self.busy():
            emit("a run is already going; /say talks to it, /stop ends it")
            return True
        return False

    def launch(self, label, fn, no_notify=False):
        if self.refuse_if_busy():
            return
        with OUT_LOCK:
            PROGRESS.clear()

        def work():
            previous_notify = NOTIFY["on"]
            NOTIFY["on"] = previous_notify and not no_notify
            try:
                fn()
            except KeyboardInterrupt:
                pass
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    self.note(f"{label}: {exc.code}", "error")
            except Exception as exc:  # the session must survive a failing run
                self.note(f"{label} failed: {exc}", "error")
            finally:
                NOTIFY["on"] = previous_notify
                with OUT_LOCK:
                    self.console.running = False
                if not self.quitting:
                    self.note(f"{label} ended; type a goal, /resume or /quit")

        with OUT_LOCK:
            self.console.running = True
        # A daemon, so a session ended by a signal is never held open by a turn it could not stop.
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def wait_for_worker(self, forced, pause, pressed, interrupt=False):
        """Before leaving, let the turn in flight end; a signal, or Ctrl+C here, stops the agent instead.

        pressed collects the Ctrl+C presses noted while waiting. interrupt (Ctrl+C twice at the prompt)
        stops the agent at once and records its turn as interrupted, not failed."""
        if not self.busy():
            return
        target = CURRENT.get("target")
        if target:
            (target / "STOP").write_text(now())
        if interrupt:
            # After STOP: once the loop has recorded the interrupted turn it must stop, not start the next.
            run, camp = self.current_run(target) if target else (None, None)
            if run and (camp is None or camp.get("current")):
                interrupt_turn(run, quiet=True)
            forced = True
        if not forced:
            emit("waiting for the current turn to end; Ctrl+C stops the agent now (the turn is recorded as failed)")
        stopped_at = None
        while self.busy():
            if forced and stopped_at is None:
                signal_children()
                stopped_at = time.monotonic()
            try:
                pause(0.1)
            except SessionEnd:
                forced = True
            if pressed and not forced:
                emit("stopping the agent")
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

    def paste_inline(self):
        """Ctrl+V: save the clipboard's image and return "[image #N] " for the line, None when there is none."""
        try:
            path = paste_image()
        except (ValueError, OSError):
            return None
        self.image_count += 1
        self.inline[self.image_count] = path
        return f"[image #{self.image_count}] "

    def complete(self, text, state):
        """readline completer, called for Ctrl+V (see bind_paste_key). text is the word before the cursor."""
        if state:
            return None
        before = text + " " if text else ""
        mark = self.paste_inline()
        if mark is None:
            sys.stdout.write("\a")  # mid-line, the bell is the only thing that does not garble the prompt
            sys.stdout.flush()
            return before
        return before + mark

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

    def typed(self, text):
        """A plain line: a goal when nothing runs, a correction to the run when one does."""
        if not self.busy():
            return self.start_goal(text)
        target = self.target()
        if target:
            self.correct(target, text, ask=True)

    def correct(self, target, text, ask):
        """Save the message first, so nothing the owner answers next (or a crash) can lose it.

        ask: offer to stop the turn now (once per turn: after a no, later lines just queue); without
        it the turn stops at once (/now)."""
        waiting = []
        if not self.guarded(lambda _: waiting.append(leave_message(target, self.with_images(text), force=not ask)),
                            None):
            return
        self.images.clear()
        emit(f"\u2713 queued for the next turn ({waiting[0]} waiting)")
        if not self.busy():
            return emit("no turn is running; the next turn reads it")
        if ask and self.declined is not None and self.declined == PROGRESS.get("since"):
            return  # already asked during this turn; /now still steps in
        if ask:
            # The worker keeps going while the owner decides, so the turn asked about may end meanwhile.
            since = PROGRESS.get("since")
            try:
                # A y/N answer is not worth recalling with the up arrow.
                answer = self.console.ask("  stop the current turn now and hand it over? its edits are kept [y/N] ",
                                          history=False)
            except (KeyboardInterrupt, EOFError):
                answer = ""
            answer = answer.strip()
            if answer.lower() not in ("y", "yes", "t", "tak"):
                if answer.lower() not in ("", "n", "no", "nie"):
                    # More of the correction, typed where the question was: kept, never dropped.
                    self.guarded(lambda _: leave_message(target, answer, force=True), None)
                self.declined = since
                return emit("the next turn reads it; until then more lines queue without asking, /now steps in")
            if PROGRESS.get("since") != since:
                return emit("that turn has ended; the next turn reads it")
        run, camp = self.current_run(target)
        if not run or (camp is not None and not camp.get("current")):
            return emit("nothing to interrupt; the next goal reads it")
        self.guarded(lambda _: interrupt_turn(run), None)

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
        elif name == "models":
            self.models()
        elif name == "watch":
            self.watch()
        else:
            target = self.target(latest=name in ("resume", "status", "transcript", "board", "diff"))
            if not target:
                return True
            if name == "say":
                if not rest and not self.images:
                    emit("usage: /say TEXT")
                elif self.guarded(cmd_say, argparse.Namespace(run=str(target), message=self.with_images(rest))):
                    self.images.clear()
            elif name == "queue":
                self.queue(target, rest)
            elif name == "budget":
                self.guarded(lambda _: self.budget(target, rest), None)
            elif name == "now":
                if not rest and not self.images:
                    emit("usage: /now TEXT")
                else:
                    self.correct(target, rest, ask=False)
            elif name == "status":
                fn = cmd_campaign_status if (target / "campaign.json").exists() else cmd_status
                self.guarded(fn, argparse.Namespace(run=str(target), dir=str(target)))
            elif name == "board":
                self.guarded(lambda _: self.show_board(target), None)
            elif name == "transcript":
                emit(str(self.transcript_path(target)))
            elif name == "diff":
                self.guarded(lambda _: show_diff(target), None)
            elif name == "resume":
                self.resume(target, rest)
        return True

    @staticmethod
    def transcript_path(target):
        """A run has its own transcript; a campaign has one per goal, so point at the running (or last) one."""
        run, _ = Session.current_run(target)
        return run / "transcript.md" if run else target

    @staticmethod
    def current_run(target):
        """(run dir, campaign or None): a campaign stands for the run it is on, or the last one it finished."""
        if (target / "state.json").exists():
            return target, None
        try:
            camp = json.loads((target / "campaign.json").read_text())
        except (OSError, ValueError):
            return None, None
        run = camp.get("current") or (camp["runs"][-1]["dir"] if camp.get("runs") else None)
        return (pathlib.Path(run) if run else None), camp

    @staticmethod
    def current_models():
        """The models and effort in force: the saved defaults, then the current run's own, then a
        choice made earlier that its next turn has not applied yet."""
        current = load_defaults()
        target = CURRENT.get("target")
        if target:
            run, _ = Session.current_run(target)
            try:
                cfg = load(run)["config"] if run else {}
            except (OSError, ValueError, KeyError, TypeError):
                cfg = {}
            current.update({k: cfg[k] for k in TUNABLE if k in cfg})
            current.update({k: v for k, v in read_tuning(target / "tuning.json").items() if k in TUNABLE})
        return current

    def models(self):
        """Pick models and effort: saved as the default, and handed to the current run or campaign."""
        current = self.current_models()
        if not (self.console.fancy and termios):
            emit("models: " + models_line(current))
            emit("the picker needs a terminal; use --claude-model, --claude-effort, --codex-model, --codex-effort")
            return
        with self.console.hold():
            choice = run_picker(model_choices(current))
        if choice is None:
            emit("models unchanged")
            return
        self.save_models(choice, choice)

    def save_models(self, changed, values):
        """Save changed as the default and hand it to the current run or campaign; values is the
        whole choice, for the line that confirms it."""
        target = CURRENT.get("target")
        try:
            save_defaults(changed)
            if target:
                write_tuning(target, changed)
        except OSError as exc:
            return emit(f"could not save the models: {exc}")
        emit("saved: " + models_line(values) + (" (applies from the next turn)" if self.busy() else ""))

    def budget(self, target, rest):
        if rest:
            try:
                cap = None if rest == "off" else positive_usd(rest)
            except argparse.ArgumentTypeError as exc:
                return emit(f"usage: /budget [USD|off] ({exc})")
            write_tuning(target, {"max_cost": cap})
            label = "off" if cap is None else f"${cap:.2f}"
            return emit(f"budget: {label} (applies from the next turn)")
        run, camp = self.current_run(target)
        state = camp if camp is not None else load(run)
        spent = state.get("cost_usd", 0.0)
        if camp is not None and camp.get("current"):
            spent += load(pathlib.Path(camp["current"])).get("cost_usd", 0.0)
        elif camp is None:
            spent = budget_spent(state)
        cfg = dict(state.get("config") or {})
        cfg.update(read_tuning(target / "tuning.json"))
        cap = cfg.get("max_cost")
        label = f"${cap:.2f} cap" if cap is not None else "no cap"
        emit(f"spent ${spent:.2f}; {label} (reported cost only)")

    def queue(self, target, rest):
        if rest == "clear":
            dropped = []
            if self.guarded(lambda _: dropped.append(clear_queue(target)), None):
                emit(f"dropped {dropped[0]} message(s)")
            return
        if rest:
            return emit("usage: /queue [clear]")
        waiting = queued(target)
        if not waiting:
            return emit("nothing waits for the next turn")
        width = term_width()
        emit("\n".join([f"{len(waiting)} of {MAX_QUEUED} waiting for the next turn:"] +
                        [fit_width(f"  {i}. {' '.join(m.split())}", width - 1) for i, m in enumerate(waiting, 1)]))

    def watch(self):
        cmd = watch_command()
        if open_watch_pane(cmd):
            return emit("watching in the pane on the right; Ctrl+C there closes it")
        emit("to see what the agents do, run this in a pane beside this one:\n  " + shlex.join(cmd))

    def show_board(self, target):
        run, camp = self.current_run(target)
        if not run:
            return emit("no run to show yet")
        emit(board(load(run), self.console.fancy, card_width(), camp))

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
            words = shlex.split(rest)
            no_notify = "--no-notify" in words
            words = [word for word in words if word != "--no-notify"]
            more = int(" ".join(words)) if words else None
            if more is not None and more <= 0:
                raise ValueError
        except ValueError:
            emit("usage: /resume [TURNS] [--no-notify] (TURNS must be positive)")
            return
        if (target / "campaign.json").exists():
            if more is not None:
                emit("/resume TURNS applies to a single run; use /resume for a campaign")
                return
            self.campaign("resume " + shlex.quote(str(target)) + (" --no-notify" if no_notify else ""))
        else:
            try:
                (target / "STOP").unlink(missing_ok=True)
            except OSError as exc:
                emit(f"cannot resume: {exc}")
                return
            self.launch("run", lambda: cmd_resume(argparse.Namespace(run=str(target), more_turns=more)),
                        no_notify=no_notify)

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
            self.launch("campaign", lambda: campaign_loop(target), no_notify=parsed.no_notify)
        else:
            self.guarded(parsed.fn, parsed)


# AppleScript for iTerm2: split the session typing /watch, then give it back the keyboard.
ITERM_SPLIT = """tell application "iTerm2"
    set here to current session of current window
    tell here to split vertically with default profile command "%s"
    tell here to select
end tell"""


def watch_command():
    """`tandem watch` for another pane: same Python, same tandem.py, same RUNS as this session."""
    return [shutil.which("env") or "/usr/bin/env", f"TANDEM_RUNS={RUNS}", sys.executable,
            str(ROOT / "tandem.py"), "watch"]


def open_watch_pane(cmd):
    """Run cmd in a new pane to the right, in tmux or iTerm2. False when neither is here or it failed."""
    line = shlex.join(cmd)
    try:
        if os.environ.get("TMUX") and shutil.which("tmux"):
            done = subprocess.run(["tmux", "split-window", "-h", "-d", "-l", "40%", line],
                                  capture_output=True, timeout=10)
        elif os.environ.get("TERM_PROGRAM") == "iTerm.app" and shutil.which("osascript"):
            quoted = line.replace("\\", "\\\\").replace('"', '\\"')
            done = subprocess.run(["osascript", "-e", ITERM_SPLIT % quoted], capture_output=True, timeout=10)
        else:
            return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


class SessionEnd(BaseException):
    """SIGHUP or SIGTERM reached the interactive session: leave now and take the agents along."""


def cmd_interactive(args):
    global CONSOLE
    args.repo = require_repo(args.repo)
    if not ask_consent():
        sys.exit("not confirmed; nothing started")
    # The logo and dots need escape codes and UTF-8; anything less gets the plain lines.
    print(banner(args.repo, fancy_terminal() and "utf" in (sys.stdout.encoding or "").lower()))
    console = Console(fancy_terminal(), styled=sys.stdout.isatty())
    session = Session(args, console)
    if editor_terminal():
        console.attach(LineEditor(sys.stdin.fileno(), None, history_path(), paste=session.paste_inline,
                                  color=console.fancy))
        console.editor.save_models = session.save_models
    else:
        try:
            import readline  # noqa: F401  # line editing and history for input(); absent on some builds
        except ImportError:
            pass
        bind_paste_key(session.complete)
    CHILD_POPEN["start_new_session"] = True
    CONSOLE = console
    # The terminal as the owner's shell left it, put back on the way out whatever happened in
    # between (the line editor and the picker restore their own changes; this is the last resort).
    terminal = None
    if termios is not None and sys.stdin.isatty():
        with contextlib.suppress(OSError, termios.error):
            terminal = termios.tcgetattr(sys.stdin.fileno())

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

    if console.editor and hasattr(signal, "SIGWINCH"):
        def on_resize(signum, frame):
            console.editor.resized = True
        previous["SIGWINCH"] = signal.signal(signal.SIGWINCH, on_resize)

    ticker_stop = threading.Event()

    def ticker():
        while not ticker_stop.wait(1):
            console.tick()

    if console.fancy or console.editor:
        threading.Thread(target=ticker, daemon=True).start()
    armed = None        # when a Ctrl+C on the empty prompt asked for a second one
    by_ctrl_c = False   # left by Ctrl+C twice: the turn in flight is stopped, not waited for
    try:
        while True:
            try:
                line = console.ask("tandem> ")
            except EOFError:
                emit("")
                break
            except KeyboardInterrupt:
                text, touched = console.interrupted_line()
                if armed is not None and not touched and time.monotonic() - armed <= EXIT_ARM_SECONDS:
                    emit("")
                    by_ctrl_c = True
                    break
                armed = None
                if text:
                    emit("")  # Ctrl+C on a line with text clears it, nothing more
                    continue
                armed = time.monotonic()
                emit("press Ctrl+C again to exit")
                continue
            armed = None
            if line.strip().lower() in EXIT_WORDS:
                break  # owner report: typing `exit` started a run whose goal was "exit"
            try:
                line, dropped = take_dropped_images(session.expand_inline(line))
                session.attach(dropped)
                kind, name, rest = parse_input(line)
                if kind == "goal":
                    session.typed(name)
                elif kind == "unknown":
                    emit(f"unknown command /{name}; valid: " + " ".join("/" + c for c in SLASH_COMMANDS))
                elif kind == "command" and not session.handle(name, rest):
                    break
            except KeyboardInterrupt:
                emit("interrupted")  # Ctrl+C cancels a slow command, never the session or its run
    except KeyboardInterrupt:
        # Ctrl+C as a signal while the one before it was still being handled, between two reads of
        # the prompt: the second press of a pair.
        by_ctrl_c = True
    except SessionEnd:
        pass
    finally:
        # From here a Ctrl+C is only noted: raised as KeyboardInterrupt, a second press could land
        # anywhere on the way out and kill tandem by SIGINT halfway through (owner report). Left in
        # place to the end of the process for the same reason.
        pressed = []
        signal.signal(signal.SIGINT, lambda signum, frame: pressed.append(signum))
        session.quitting = True
        stopped_turn = by_ctrl_c and session.busy()
        # A detached agent left running would keep editing the worktree, and a later resume could
        # start a second agent there. Event waits also avoid interrupting Thread.join on 3.9.
        try:
            session.wait_for_worker(bool(ended_by), ticker_stop.wait, pressed, interrupt=by_ctrl_c)
        except SessionEnd:
            session.wait_for_worker(True, ticker_stop.wait, pressed)
        if stopped_turn:
            emit("stopped the current turn; its edits are kept. /resume or `tandem resume` continues")
        ticker_stop.set()
        CONSOLE = None
        CHILD_POPEN.clear()
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)
        if terminal is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal)
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
    NOTIFY["on"] = wants_notify(fn, args)
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
        NOTIFY["on"] = False
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Claude Code and Codex take turns on one repository until they agree the goal is done.

A run works on one goal in its own git worktree and branch, so the owner's checkout is never
touched. A campaign chains runs on one branch: when a goal ends, the agents pick the next one
from the project's plan and keep going until the plan is exhausted or the owner stops them.
The orchestrator owns git: after every turn it commits what the agent changed.
"""

import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
TURN_SCHEMA = ROOT / "schema" / "turn.json"
PLAN_SCHEMA = ROOT / "schema" / "plan.json"
TURN_PROMPT = ROOT / "prompts" / "turn.md"
PLAN_PROMPT = ROOT / "prompts" / "plan.md"
# Runs live next to the repo, not inside it: Claude is not sandboxed, and an agent that writes
# outside its worktree must not land in this checkout. TANDEM_RUNS overrides the location.
RUNS = pathlib.Path(os.environ.get("TANDEM_RUNS") or ROOT.parent / f"{ROOT.name}-runs").expanduser()

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

# Accounts are shared by every run on this machine, so availability is too.
AVAILABILITY = RUNS / ".availability.json"
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
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"`git {' '.join(args[:2])}` in {cwd} failed: {tail(proc.stderr or proc.stdout, 400)}")
    return proc.stdout.strip()


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


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
        time.sleep(min(60, max(1, (when - datetime.datetime.now()).total_seconds())))
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


def run_claude(cfg, worktree, prompt, out_base, schema, key, readonly):
    cmd = ["claude", "-p", "--output-format", "json", "--json-schema", schema.read_text(),
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
    proc = subprocess.run(cmd, input=prompt, cwd=worktree, capture_output=True, text=True,
                          timeout=cfg["timeout"])
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
    cmd = ["codex", "exec", "-C", worktree, "-s", "read-only" if readonly else "workspace-write",
           "-c", 'approval_policy="never"', "--output-schema", str(schema), "-o", str(last),
           "--color", "never"]
    if cfg.get("codex_network") and not readonly:
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    if cfg.get("codex_model"):
        cmd += ["-m", cfg["codex_model"]]
    if cfg.get("codex_effort"):
        cmd += ["-c", f'model_reasoning_effort="{cfg["codex_effort"]}"']
    cmd.append("-")
    proc = subprocess.run(cmd, input=prompt, cwd=worktree, capture_output=True, text=True,
                          timeout=cfg["timeout"])
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
    proc = subprocess.run(cmd, shell=True, cwd=state["worktree"], capture_output=True, text=True,
                          timeout=state["config"]["timeout"])
    return proc.returncode == 0, tail(proc.stdout + proc.stderr, 2000)


# ---------- one run: the loop ----------

def take_inbox(paths):
    msgs = []
    for p in map(pathlib.Path, paths):
        if p.exists() and p.read_text().strip():
            msgs.append(p.read_text().strip())
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


def loop(run_dir):
    state = load(run_dir)
    state.pop("outcome", None)
    cfg = state["config"]
    inboxes = state.get("inboxes") or [str(run_dir / "inbox.md")]
    while True:
        if stop_requested(state):
            return finish(run_dir, state, "stopped", "The owner asked the campaign to stop.")
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
                          capture_output=True, text=True)
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


def cmd_campaign_start(args):
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
        repo = pathlib.Path(args.repo).expanduser().resolve()
        branch = f"tandem/campaign-{camp_id}"
        worktree = str(camp_dir / "work")
        git(repo, "worktree", "add", "-q", "-b", branch, worktree, git(repo, "rev-parse", args.base))
        camp.update(repo=str(repo), worktree=worktree, branch=branch)
    camp["queue"] = [read_goal_file(p) for p in args.goal_file or []]
    write_json(camp_dir / "campaign.json", camp)
    log(f"campaign {camp_dir.name} on {camp['branch']} ({camp['worktree']})")
    log(f"talk: {sys.argv[0]} campaign say {camp_dir} \"...\"   stop: {sys.argv[0]} campaign stop {camp_dir}")
    campaign_loop(camp_dir)


def cmd_campaign_resume(args):
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
    campaign_loop(camp_dir)


def cmd_campaign_stop(args):
    (run_path(args.dir) / "STOP").write_text(now())
    print("the campaign stops after the current turn")


def cmd_campaign_status(args):
    camp_dir = run_path(args.dir)
    camp = json.loads((camp_dir / "campaign.json").read_text())
    print(f"branch {camp['branch']}  goals {len(camp['runs'])}  queued {len(camp['queue'])}  "
          f"claude cost ${camp['cost_usd']:.2f} (without the current goal)")
    for r in camp["runs"]:
        print(f"  {r['outcome']:<9} {r['name']} ({r['turns']} turns)")
    if camp.get("current"):
        print(f"  current: {camp['current']}")
        cmd_status(argparse.Namespace(run=camp["current"]))


# ---------- single-run commands ----------

def cmd_start(args):
    repo = pathlib.Path(args.repo).expanduser().resolve()
    item = read_goal_file(args.goal_file) if args.goal_file else {"goal": args.goal, "name": args.name}
    if not item["goal"]:
        sys.exit("give a goal: --goal TEXT or --goal-file PATH")
    name = args.name or item.get("name")
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{slug(name)}" if name else "")
    cfg = dict(config_from(args), base=args.base)
    cfg["check"] = args.check or item.get("check") or None
    run_dir = create_run(RUNS / run_id, repo, item["goal"], name or run_id,
                         cfg, args.max_turns, args.first or item.get("first") or "claude")
    log(f"talk to the agents: {sys.argv[0]} say {run_dir} \"...\"")
    loop(run_dir)


def cmd_resume(args):
    run_dir = run_path(args.run)
    state = load(run_dir)
    if args.more_turns:
        state["max_turns"] = state["turn"] - 1 + args.more_turns
    state["failures"] = {"claude": 0, "codex": 0}
    save(run_dir, state)
    loop(run_dir)


def cmd_say(args):
    target = run_path(args.run)
    with open(target / "inbox.md", "a") as f:
        f.write(args.message.strip() + "\n\n")
    if (target / "campaign.json").exists():
        # The next turn consumes the inbox; the planner keeps reading notes.md for every later goal.
        with open(target / "notes.md", "a") as f:
            f.write(f"- ({now()}) {args.message.strip()}\n")
    print("queued for the next turn")


def cmd_status(args):
    run_dir = run_path(args.run)
    state = load(run_dir)
    print(f"{state.get('name') or run_dir.name}: branch {state['branch']}  turn {state['turn'] - 1}/{state['max_turns']}  "
          f"outcome {state.get('outcome', 'running or interrupted')}  claude cost ${state['cost_usd']:.2f}")
    for h in state["history"]:
        print(f"  {h['turn']:>2} {h['agent']:<6} {h['status']:<8} {h.get('commit') or '-':<8} "
              f"{(h.get('summary') or h.get('error') or '')[:100]}")


def cmd_doctor(args):
    """Check that this machine can run tandem: tools on PATH and both agents logged in."""
    def probe(cmd):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return proc.returncode, (proc.stdout + proc.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)

    checks = [("python 3.10+", sys.version_info >= (3, 10), sys.version.split()[0])]
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
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<18} {detail}")
    if not all(ok for _, ok, _ in checks):
        sys.exit(1)
    print("ready")


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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="start a single-goal run")
    s.add_argument("--repo", required=True)
    s.add_argument("--goal")
    s.add_argument("--goal-file")
    s.add_argument("--name", help="short label added to the run id and branch")
    s.add_argument("--base", default="HEAD", help="commit or branch to start from")
    s.add_argument("--first", choices=["claude", "codex"], help="who starts (default: the goal file's first:, else claude)")
    s.add_argument("--check", help="shell command that must pass before the run counts as done")
    add_agent_options(s, 12)
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

    c = sub.add_parser("campaign", help="chain goals on one branch until the plan is done")
    csub = c.add_subparsers(dest="ccmd", required=True)
    cs = csub.add_parser("start")
    cs.add_argument("--repo", help="required unless --adopt")
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

    args = p.parse_args()
    if getattr(args, "ccmd", None) == "start" and not (args.repo or args.adopt):
        p.error("campaign start needs --repo or --adopt")
    try:
        args.fn(args)
    except KeyboardInterrupt:
        print("\ninterrupted; continue with resume")
    except GitError as e:
        sys.exit(f"{e}\nfix it, then continue with resume")


if __name__ == "__main__":
    main()

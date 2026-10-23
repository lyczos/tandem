# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An orchestrator that makes two external coding-agent CLIs (`claude` and `codex`) take turns on a
third-party repository. Everything lives in one file, `tandem.py`, Python 3.9+ (stock macOS ships 3.9.6; CI enforces the floor, so no `match`, no `X | Y` annotations, no 3.10-only stdlib), standard library
only — no Python dependencies, no Python package, no build step. Keep it that way: a new import from PyPI is a
design change, not a detail.

The npm package is `@lyczos/tandem` and installs the `tandem` command through `bin/tandem.js`.
That launcher uses `TANDEM_PYTHON` when set (and fails if it is not 3.9+), otherwise tries `python3`,
`python3.15` through `python3.9`, `python`, then `py -3` on Windows. It requires Python 3.9+, forwards arguments and signals, and runs the same `tandem.py`
used from a source checkout. The npm package must have no runtime dependencies and no install
scripts; users install and log in to Claude Code and Codex themselves. Keep `package.json`'s
version equal to `__version__` in `tandem.py`, and ship `prompts/` and `schema/` alongside it.
The launcher sets `TANDEM_PROG=tandem` for command hints and `TANDEM_NODE` for `doctor`.

## Commands

```bash
python3 -m unittest discover -s tests            # the whole suite
python3 -m unittest tests.test_tandem.Git        # one class
python3 -m unittest tests.test_tandem.ParseResult.test_bare_json   # one test
./tandem.py --repo ~/code/my-project              # interactive session (stdin must be a terminal)
./tandem.py doctor                               # tools, logins, storage, and cwd repo with a commit
./tandem.py -p "GOAL" --output-format json        # one run; progress on stderr, result on stdout
./tandem.py -p --goal-file goals/my-goal.md        # also accepts a piped goal with bare -p
./tandem.py runs --limit 10                       # recent runs and campaigns in RUNS
```

An npm install exposes these same entry points as `tandem`. With no subcommand, a terminal
on stdin starts the session, `-p` starts a headless run, and non-terminal stdin without `-p`
fails with a hint. Keep every explicit subcommand working from the source checkout too.

Run tests from the repo root: `tests/test_tandem.py` loads `tandem.py` by path with
`importlib.util.spec_from_file_location`, so there is no importable package. CI (`.github/workflows/ci.yml`)
runs the same discover command on Python 3.9, 3.10 and 3.13, ubuntu and macos. There is no linter or
formatter configured.
The npm CI job uses Node 20, tests the launcher, and installs an `npm pack` tarball into a
temporary prefix to check `tandem --version` and `tandem --help`. Launcher tests skip when Node
is absent, so the Python suite still runs without Node.

The interactive session (`cmd_interactive`, `Session`, `Console`) runs `loop()` in a worker thread
while the main thread reads input. All output must go through `emit()`/`log()` (one `OUT_LOCK`),
never bare `print` from code that can run during a run, or the prompt redraw breaks. On a POSIX
terminal `LineEditor` owns the prompt row and the rows around it (status line above, slash-command
menu and status bar below); `Console.write` must erase and redraw them around every line it prints.
Tab on an empty line edits the models on that bar: the unsaved values live in the editor
(`LineEditor.fields`), so redraws keep them, and Enter saves outside `OUT_LOCK` through
`Session.save_models()`, the same path `/models` takes, with `step_choice()` shared by both. While it
runs, every child the worker starts (agents, the check, git) gets its own session and `/dev/null`
on stdin (`CHILD_POPEN`) so Ctrl+C at the prompt cannot kill a turn or fail a check. Being out of
the terminal's reach, agents and checks go through `run_child()`, which tracks them in `CHILDREN`:
SIGHUP, SIGTERM or Ctrl+C during `/quit` stop them with `signal_children()`, because an agent left
behind keeps editing the worktree with nobody to commit or time it out. Long subprocesses started
from the worker must use `run_child()`. First-run consent lives in `~/.tandem/config.json`.
Check `Session.refuse_if_busy()` before creating a run or changing a stop file. Campaign setup
finishes before launching the worker so `/say` and `/stop` always have the right target.
`INBOX_LOCK` protects messages sent within the session from a simultaneous inbox read and clear.
Quitting waits for the worker; leaving an agent running could race with a later resume.

`build_parser()` builds the CLI without dispatching it. `prepare_run()` shares run creation
between `start` and the session entry points. Headless `-p` uses the same `loop()` and done rule;
`outcome_exit_code()` maps done/error/max_turns/blocked/stopped/failed/over_budget to 0/1/2/3/4/5/6. Usage
errors exit 1 so they cannot be confused with the turn limit. `--output-format json` emits one
result object on stdout and sends logs to stderr; `--quiet` hides progress, not the result.

Exercising the real loop costs money and needs both CLIs logged in. To try out a change to the loop
itself, use a throwaway git repo as `--repo` (see README "First run on a throwaway repo"). Running
tandem on this repository with a real goal is fine: the agents work
in their own worktree and branch, while the orchestrator keeps running from this checkout.

## Architecture

Two nested loops over state that lives entirely on disk, so every command is resumable:

- `loop(run_dir)` — **one run, one goal.** Picks the agent, builds the prompt, calls the agent,
  commits, appends to the transcript, decides whether the goal is closed. Exits by calling
  `finish()`, which writes `outcome` into `state.json`.
- `_campaign_loop(camp_dir)` — **many goals on one branch.** Calls `loop()` for the current run,
  records the outcome, then takes the next goal from the queue, from the review backlog, or from
  `plan_next()` (one agent, read-only, picks the next goal from the project's own plan).

Nothing is held in memory between turns. A run is `state.json` in its directory; a campaign is
`campaign.json` plus one subdirectory per run. `resume` just reloads and re-enters the loop. Agent
availability is shared across all runs on the machine in `~/.tandem/availability.json` (not in
`RUNS`, so a source checkout and an npm install agree), because the accounts are shared.

`RUNS` uses `TANDEM_RUNS` when set, otherwise `ROOT.parent / "tandem-runs"` when `ROOT` has its
own `.git` directory or worktree file, otherwise `~/.tandem/runs` for an installed copy. Resolve
the path without creating it at import time; run creation and `doctor` create it on demand.
`doctor` reports the resolved path and probes an actual write. Run directories must stay outside
the target repository and tandem's own files because Claude Code is not sandboxed;
`require_repo()` refuses a target that contains `RUNS`.
Users moving existing runs from `../tandem-runs` to an npm install can set `TANDEM_RUNS` to
the old directory's absolute path to keep their run ids reachable. Interactive consent stays
in `~/.tandem/config.json`, independently of run storage.

While an agent works, `run_child(on_line=...)` hands its output over line by line: Claude runs with
`--output-format stream-json` (`claude_payload()` picks the final result event, the same object the
plain `json` format returns) and Codex's plain log goes through `CodexActivity`. Each action becomes
one line of `activity.log` in the directory of the call's `out_base` (`note_activity()`), and the
loop adds the turn's end and the check. `tandem watch` follows the newest of those files; the
session's `/watch` opens it in a tmux or iTerm2 pane, because the session's own screen belongs to
the prompt. The log is for people only: nothing reads it back.

Owner messages wait in `inbox.md`, one per blank-line-separated block (`queued()`). `leave_message()`
refuses past `MAX_QUEUED` unless forced (`/now` and the answer to the stop question are), because
the next agent reads the whole backlog at once.

`tuning.json` (in a run or campaign directory) is how the session changes the models and effort of
a running run, plus its reported-cost cap (`/budget`, `max_cost`; `null` removes the cap): `write_tuning()` merges into it, and `loop` re-reads it at the start of every turn
(`apply_tuning()`, later file wins, `null` means default). A campaign run reads the campaign's file.
Never write `state.json` from the session: the loop holds a copy in memory and would overwrite it.

### Invariants worth protecting

- **The orchestrator owns git.** Agents never commit, push or switch branches: `commit_turn()` does
  it after every turn with `--no-verify` (a lint hook failing on half-done work must not stop a
  turn), and `CLAUDE_DISALLOWED` plus Codex's sandbox block the agents from trying. Each run or
  campaign works in its own `git worktree`, so the owner's checkout is never touched.
- **Neither agent closes a goal alone.** In peer mode, `done` counts only when the previous turn
  was also `done`, by the *other* agent, and the current turn produced no commit (`entry["commit"]`
  is None) — a fresh-eyes turn that changes nothing. Reviewer mode (`--codex-role reviewer`) and
  solo mode (the other agent benched or away long) relax this in specific, commented ways; solo
  closure sets `needs_review`, which the campaign turns into a `codex-review-backlog` goal later.
- **Failures are classified, not just raised.** `classify()` sorts an agent error into `limit`
  (pause that agent until `parse_reset()`'s time, let the other carry on), `transient` (retry the
  same turn in `call_agent`) or `other` (count against `MAX_FAILURES_PER_AGENT`, bench the agent at
  three). The tuning constants are the block near the top of the file; change them there, not inline.
- **An owner interruption is not a failure.** `interrupt_turn()` writes `INTERRUPT` in the run dir
  and stops only agents, never the check. `call_agent` and `loop` look for that file before
  classifying an error, so the killed agent is recorded as `interrupted`: no failure counted, no
  transient retry, `max_turns` raised by one, and the same agent goes next with the correction and
  its partial edits committed. `loop` clears the file before each turn reads the inbox, so a stale
  one never kills a fresh turn.

### Agents, prompts and schemas

`run_claude` and `run_codex` are the only places that know a CLI's flags; `RUNNERS` dispatches by
agent name, and both return `(result_dict, cost_usd, usage)` — Codex cost is always 0.0, and `usage`
holds only the tokens and model the CLI reported (`claude_usage()`, `codex_usage()`), never a guess.
Callers unpack `result, cost, *more` so test stubs returning two values still work. Both pass a JSON
schema to the CLI and fall back to `parse_result()`, which accepts a bare JSON object or the last
fenced ```json block in free text.

Images travel inside the prompt text as `[image: PATH]` (pasted or dropped in the session, stored in
`~/.tandem/images`). Each runner finds them with `prompt_images()` and hands them to its CLI
(`--image` for Codex, `--add-dir` for Claude). Both flags take several values, so they go before
another flag, never at the end of the command.

`prompts/turn.md` and `prompts/plan.md` are `str.format` templates, so **every literal brace in them
must be doubled** and the placeholders must match the kwargs in `build_prompt()` and `plan_next()`
exactly. A new result field means editing three places together: the schema in `schema/`, the field
list in the prompt's "Fields" section, and the code that reads it (`loop`'s `entry.update`, plus
`render_history` if it should show in the transcript).

The read-only path matters: planners and reviewers get `CLAUDE_READ` / Codex's `read-only` sandbox
via the `readonly` argument. Widening `CLAUDE_ALLOWED` widens what unsandboxed agents can run on the
user's machine — it is a guard rail, not a sandbox, and the README says so.

## Conventions

- Prose in the README, prompts and commit messages is plain, concrete and unhyped; the agents'
  structured replies are deliberately terse ("caveman style", defined in `prompts/turn.md`).
- Comments explain *why* a rule exists (usually "an agent must not review its own work" or "the
  accounts are shared"), not what the line does. Match that density.
- Helpers to reuse rather than reinvent: `git()` (raises `GitError` with context), `log()`,
  `tail()`, `slug()`, `write_json()` (atomic via `.tmp` + replace), `run_path()` (accepts a path,
  `runs/<id>` or a bare id — new commands taking a run or campaign should too).

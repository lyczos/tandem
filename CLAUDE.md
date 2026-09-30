# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An orchestrator that makes two external coding-agent CLIs (`claude` and `codex`) take turns on a
third-party repository. Everything lives in one file, `tandem.py`, Python 3.10+, standard library
only — no dependencies, no package, no build step. Keep it that way: a new import from PyPI is a
design change, not a detail.

## Commands

```bash
python3 -m unittest discover -s tests            # the whole suite
python3 -m unittest tests.test_tandem.Git        # one class
python3 -m unittest tests.test_tandem.ParseResult.test_bare_json   # one test
./tandem.py doctor                               # tools on PATH and both agents logged in
```

Run tests from the repo root: `tests/test_tandem.py` loads `tandem.py` by path with
`importlib.util.spec_from_file_location`, so there is no importable package. CI (`.github/workflows/ci.yml`)
runs the same discover command on Python 3.10 and 3.13, ubuntu and macos. There is no linter or
formatter configured.

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
availability is shared across all runs on the machine in `RUNS/.availability.json`, because the
accounts are shared.

`RUNS` defaults to `../tandem-runs`, a sibling of this checkout, not a subdirectory — Claude Code
is not sandboxed, so an agent that writes outside its worktree must not be able to land in tandem's
own source. `TANDEM_RUNS` overrides it. Do not move run directories inside the repo.

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

### Agents, prompts and schemas

`run_claude` and `run_codex` are the only places that know a CLI's flags; `RUNNERS` dispatches by
agent name, and both return `(result_dict, cost_usd)` — Codex cost is always 0.0. Both pass a JSON
schema to the CLI and fall back to `parse_result()`, which accepts a bare JSON object or the last
fenced ```json block in free text.

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

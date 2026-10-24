# tandem

Claude Code and Codex work on your repository in turns. One agent takes a step, tandem commits
it, and the other reviews that commit and carries on. Type a goal, talk to the agents while
they work, and review the resulting branch before merging it.

**The Claude allow-list is a guard rail, not a sandbox.** `python3` and `npm` can run any code
with your user's permissions. Only Codex's side is sandboxed. Use repositories you trust,
ideally in a VM or container if the code is not yours. A separate worktree keeps the work on
its own branch; it does not restrict what Claude can access elsewhere on your machine.

## Getting started

Install the command, then open a repository with at least one commit:

```bash
npm install -g @lyczos/tandem
cd ~/code/my-project
tandem doctor
tandem
```

At the prompt, type a goal such as `Add tests for the parser`. The first interactive session
asks you to type `yes` after reading the security warning. For scripts, use
`tandem -p "Add tests for the parser"`.

`doctor` reports the tandem version, Python version and interpreter path, Node version when
launched through npm, the writable runs directory, Git and its author identity, both agent
CLIs and logins, the current repository and branch, and which notifier tandem will use. Every line should say `ok`; a `FAIL`
line identifies what needs fixing. It exits 1 if any check fails, including when you run it
outside a Git repository or before that repository has its first commit.

## Requirements

- **Python 3.9+** (the interpreter that ships with macOS Command Line Tools is enough).
  Tandem uses only the Python standard library.
- **Node 18+** for the npm install and launcher.
- **Git**, with `user.name` and `user.email` configured so tandem can commit each turn.
- **Claude Code and Codex CLI** on `PATH`, both logged in. Install them separately; tandem
  does not bundle or log in to them.
- A target Git repository with at least one commit. Its `CLAUDE.md` is the rulebook for both
  agents: describe the stack, how to run tests, and what must not change.

macOS and Linux are supported; Windows is untested. From a source checkout, Python and Git
are enough to run tandem itself; Node is only needed if your agent installation or project
requires it.

Install the peer tools and log in if you have not already:

```bash
npm install -g @anthropic-ai/claude-code @openai/codex
claude auth login
codex login
```

Everything runs on your machine with your own accounts. Claude turns use your Claude
subscription or API key; Codex turns use your ChatGPT plan or OpenAI API key. For Claude API
access, export `ANTHROPIC_API_KEY` in the shell that starts tandem. Put it in your shell
profile, not in `~/.claude/settings.json`: the agents run with your user settings switched
off. For Codex API access, use `printenv OPENAI_API_KEY | codex login --with-api-key`.

## First run on a throwaway repo

Try a small goal before pointing tandem at real code:

```bash
mkdir -p ~/code/tandem-test
cd ~/code/tandem-test
git init -q
echo "# tandem test" > README.md
git add README.md
git commit -qm init
tandem doctor
tandem -p "Write a Python greeting CLI with a --name flag and unittest tests" \
  --name greeting --claude-model sonnet --max-turns 6
```

Each run works in its own worktree and branch. The final output names the branch and
transcript path; `tandem runs` finds them again. Your original checkout stays on its branch.

## How turns work

When an agent cannot do something (sandbox, missing tool, no idea), it says so in
`could_not_do` and the other agent tries first. In the default peer mode, the run ends when
both agents say `done` in a row, the second agent makes no changes, and the optional check
command passes. You can read the transcript and leave messages for the next turn throughout.

## Run

```bash
tandem start \
  --repo ~/code/my-project \
  --goal-file goals/my-goal.md \
  --name my-goal \
  --check "npm test" \
  --max-turns 12
```

`--repo` defaults to the current directory. `--goal "..."` works instead of `--goal-file` for
short goals; without `--name` the run is named after the goal's first words, so
`--goal "Add a --json flag"` gives `add-a-json-flag`. A good goal says what "done" means in checkable terms;
[`examples/todo-cli.md`](examples/todo-cli.md) in the source repository shows the
shape. A goal file may start
with a header: `---`, then `name:`, `first: codex|claude`, `check:`, then `---`.

## Find and resume runs

```bash
tandem runs --limit 10
```

Lists recent runs and campaigns, newest first: id, name, branch, outcome, turns, and path.
Run storage uses the precedence described in [Configuration](#configuration), with
`~/.tandem/runs` as the default for npm installs.

Each run lives in `<runs directory>/<id>/`. Commands accept the directory as a path, as
`runs/<id>` or as the bare `<id>`:

- `work/` - a git worktree on branch `tandem/<id>`; your own checkout is never touched
- `transcript.md` - the conversation, readable while it runs (`tail -f`)
- `activity.log` - one line per thing an agent does during its turn: a command, a file read or
  edited, a line of what it says; `tandem watch` follows it
- `turn-NN-<agent>.*` - the exact prompt and raw output of every turn
- `state.json` - what `resume` continues from

Talk to them while they work; the next agent reads it first:

```bash
tandem say <id> "Use SQLAlchemy 2.0 async, not psycopg directly."
tandem status <id>
tandem watch                          # follow what the agents do; Ctrl+C closes it
tandem resume <id> --more-turns 6     # after Ctrl+C, a limit, or a blocked run
```

`tandem watch` with no id follows whichever run acted last, so one left open in a side pane keeps
up with every new goal and every run of a campaign.

## After a run

The work sits on the run's branch, in its own worktree; tandem never merges, pushes or deletes it.
Right after the `FINISHED` line every run prints what changed and how to take it, with your real
paths filled in:

```text
  branch    tandem/20261024-120000-parser: 3 commits over f89ce5c3f217, 4 files changed, +120 -7
  worktree  /home/me/.tandem/runs/20261024-120000-parser/work
  read      git -C /home/me/project diff f89ce5c3f217..tandem/20261024-120000-parser
  merge     git -C /home/me/project merge tandem/20261024-120000-parser
  clean up  git -C /home/me/project worktree remove /home/me/.tandem/runs/20261024-120000-parser/work
            git -C /home/me/project branch -d tandem/20261024-120000-parser
```

Each command is on a line of its own and is never cut, so it copies whole; in the session the
paths start with `~`.

The merge is a plain one, so each turn's commit stays in your history; squash it yourself if you
prefer. Clean up only after merging: `branch -d` refuses a branch that is not merged. A run that
ended `blocked`, `failed`, `max_turns`, `stopped` or `over_budget` shows a `resume` line first,
because its work is not finished. After the turn limit it carries `--more-turns 6`, and after the
cap a `--max-cost` of what was spent plus one more cap, so copying it really continues; in the
session `/resume` alone gives a run out of turns six more, and `/budget USD` then `/resume`
continues past the cap. A run with no commits says `no changes` and prints no commands.
A campaign prints the same summary for its branch when it ends. Each of its goals prints only a
`goal` line with what it added, because the next goal goes on in the same worktree.

`tandem diff [RUN]` (default: the newest run or campaign) and `/diff` in the session print it again
at any time, followed by `git diff --stat` of the branch (its first 20 files). If the worktree or
the branch is already gone, they say so in a `note` line instead of failing.

## Interactive session

Run `tandem` with no arguments in a terminal. It prints the version, repo and branch, and whether
`claude` and `codex` are installed and logged in (with `NO_COLOR` or no terminal, also the Python in
use, as plain lines). The first time on a machine
it shows the security warning and asks you to type `yes`; the answer is saved in
`~/.tandem/config.json`.

Plain text is a goal and starts a run in the current repo. The run goes on in the background, so
you can keep typing and talk to the agents while they work. Typing `/` opens a menu of the slash
commands under the prompt, narrowed with every key, each with a short description: ↑↓ select,
Tab or → at the end of the line completes, Enter runs the highlighted one, Esc closes the menu.
Tab on an empty line edits the models on the status bar instead (see below).
When the menu is closed, ↑↓ go through earlier lines, kept in `~/.tandem/history` (the last 500). The usual editing keys work:
←→, Home/End or Ctrl+A/Ctrl+E, Ctrl+U and Ctrl+K delete to the start or end, Ctrl+W the word
before the cursor. A paste of several lines arrives as one line. With `TERM=dumb` or without a
terminal the session uses Python's `input()` with readline instead, without the menu. Slash commands:

- `/help`, `/status`, `/runs`, `/transcript` (prints the transcript path), `/doctor`
- `/diff` shows what the current or last run changed and the commands to read, merge and clean
  it up (see [After a run](#after-a-run))
- `/board` shows where the run stands: what both agents agree on, what is still open, the models in use
  and the cost. Each finished turn appears in the session as a small card with its summary, handoff and
  checks, drawn in plain characters with `NO_COLOR`.
- `/budget [USD|off]` shows spent cost and the cap, sets a positive dollar cap, or removes it.
  Changes apply from the next turn of the current run or campaign.
- `/models` picks the Claude and Codex model and effort: ↑↓ move, ←→ change the value, Tab goes
  to the next row, Enter saves, Esc cancels. The choice is saved as the default in
  `~/.tandem/config.json`, and a running run takes it from its next turn. Codex models and the
  efforts each supports come from `~/.codex/models_cache.json`; `default` leaves the setting to
  the CLI's own config. Without a terminal, or with `NO_COLOR`, it prints the current settings instead
- `/say TEXT` leaves a message the next turn reads; `/now TEXT` also stops the current turn
  (see below). `/queue` lists the messages waiting for the next turn, `/queue clear` drops them
- `/watch` opens `tandem watch` in a pane to the right of the session, in tmux or iTerm2, to see what
  the agents do while a turn runs. Elsewhere it prints the command to run in a pane of your own
- Ctrl+V pastes the image on the clipboard (a screenshot) into the line as `[image #1]`, as in
  Claude Code. `/paste` attaches it to your next goal or `/say` instead, and so does dragging
  image files onto the terminal; `/paste clear` drops what is waiting. Cmd+V stays the
  terminal's own text paste
- `/stop` stops after the current turn; `/resume [TURNS]` continues the last run (in a new
  session, the latest run in this repo)
- `/goal FILE` starts a run from a goal file; `/campaign ARGS...` is `tandem campaign ARGS...`
- `/quit`, Ctrl+D, or `exit`, `quit` or `q` alone on the line leaves after the current turn finishes;
  Ctrl+C twice leaves at once (see below)

Scrolling back, three kinds of lines are easy to tell apart. What you typed stays as
`› your line`, bold on a dark band across the row. tandem's own lines are quiet: a dim `HH:MM`
stamp, a symbol where it helps (`▸` a new goal, `·` a turn starting, `✓` done, `!` a warning,
`✗` an error, the last two in yellow and red) and paths shortened with `~` or relative to the
run directory. Ordinary log entries fit on one row with an ellipsis; warnings, errors and the
end of a run (a blocked run's question, the way past a cap, the transcript path) wrap and are
never cut. Each run opens with a rule such as
`── bar-controls · tandem/20261024-071612-bar-controls ───` and closes with one carrying the
outcome in its color, and the turn cards sit in between. With `NO_COLOR` the marker, symbols and
rules stay, without color. Only the session looks like this: `start`, `resume`, `-p` and any
output that is not a terminal keep the dated `[YYYY-MM-DD HH:MM:SS]` lines, which also print
the `say` command to talk to the agents.

Only one run or campaign runs in a session at a time. `/goal FILE` or `/resume` while it is busy
leaves the current run and any pending `/stop` unchanged. Ctrl+C works as in Claude Code: on a
line with text it clears the line; on an empty line it says `press Ctrl+C again to exit`, and a
second Ctrl+C within two seconds leaves. If a turn is running then, its agent is stopped and the
turn is recorded as interrupted, with its edits committed; it is not a failure and does not use up
a turn, and `/resume` or `tandem resume` continues from there. During a slow command (such as
`/doctor`) Ctrl+C cancels that command and leaves the run alone.
While `/quit` waits for the turn to finish, Ctrl+C stops the agent instead: the turn is recorded
as failed and `/resume` or `tandem resume` continues from there; more presses change nothing.
Closing the terminal or sending the session SIGTERM does the same without asking, so no agent
keeps working unsupervised.

### Correcting a turn in progress

When you see a turn going wrong, type the correction at the prompt. It is saved for the next turn
before anything else happens, so whatever you answer next, it is not lost. Then tandem asks whether
to stop the current turn now: `y` stops the agent, commits what it changed so far, and gives the
same agent the turn again with your message and a note to check its partial edits; Enter, `n` (or
Ctrl+C) lets the turn finish and the next one reads it. Anything else typed there is taken as more
of the correction and saved with it. An interrupted turn is not a failure and does not use up a
turn; `/now TEXT` does the same without asking.

After a no, tandem does not ask again during that turn: more lines just join the queue, and each
shows how many wait. The next agent reads them all at once, in order, so at most five wait at a
time; a sixth is refused rather than buried (↑ brings it back). Look at them with `/queue`, drop
them with `/queue clear`, or use `/now`, which always goes through and hands them over at once.

Images are copied to `~/.tandem/images` and named in the message as `[image: PATH]`, so every
later turn sees them too. Codex gets them attached with `--image`; Claude Code gets the folder
with `--add-dir` and is told to open them. `/paste` reads the clipboard with `osascript` on
macOS and `wl-paste` or `xclip` on Linux; elsewhere, drag the file in.

The line editor shows a live status bar under the prompt and slash menu. It lists each agent's
model, effort and activity: working with elapsed time, next, waiting, away until a reset time,
or benched after repeated failures. During a run, a second row shows the turn, cost, tokens,
session time, diff size and check. On an empty line the bar ends with the hint `Tab or /models to
change`; while you type, Tab completes instead, so the hint drops the Tab (and during a run,
goes away).

Tab on an empty line (no slash menu open) changes the models and effort right on the bar, without
opening `/models`. One of the four values (Claude model, Claude effort, Codex model, Codex effort)
is highlighted, or shown in brackets like `[high]` with `NO_COLOR`. Tab and Shift+Tab move to the
next or previous one, ↑↓ go through the same values `/models` offers, including `default`, and a
Codex model keeps its effort only if it supports it. Enter saves like `/models` does; if nothing
changed, it saves nothing. Esc puts back what was shown before. Any other key leaves the mode
without saving and then does what it would normally do, so typing a goal right away loses nothing.
This works while a turn runs, and the change applies from the next turn. If the bar is too wide
for the terminal, each editable field gets its own row so the selected value stays visible.

A second row shows the turn budget, reported dollar cost and tokens, elapsed session time,
committed branch additions and deletions, and the check result (✓ passed, ✗ failed, – not run).
Cost and tokens update after turns; Codex reports tokens but no dollar cost. Missing usage is
not estimated. Branch counts refresh after commits, so uncommitted edits are not included.
Session time starts when the interactive session opens, including time between runs.

The bar truncates to the terminal width. Resizing clears the visible screen and redraws the
prompt and bar, keeping the text being edited. `NO_COLOR` removes colors; the line editor still
uses cursor movement to draw the plain bar. The readline fallback and redirected output have
no bar.

## Headless runs

The npm command accepts a one-shot goal with `-p`. From a source checkout, use `./tandem.py`
in place of `tandem`. `--repo` defaults to the current directory, which must be a git repository
with at least one commit.

```bash
tandem -p "Add tests for the parser" --max-turns 6
printf '%s\n' "Add tests for the parser" | tandem -p
tandem -p --goal-file goals/parser-tests.md
tandem -p "Review the parser" --output-format json > result.json
```

All `start` tuning flags work with `-p`, including `--repo`, `--check`, `--first`, model and
budget flags, `--timeout`, `--name`, and `--base`. Give one goal source; `-p` cannot accompany
a subcommand. Headless mode does not ask for consent: read the Claude allow-list warning in
[How the agents are allowed to act](#how-the-agents-are-allowed-to-act) before running it.

Text output streams progress and ends with the outcome, branch, and transcript path. JSON
output writes one object to stdout and sends progress to stderr. `--quiet` suppresses progress
in either format; the final result and errors remain. For example, `result.json` contains:

```json
{
  "run_id": "20261023-120000-parser",
  "name": "parser",
  "repo": "/home/me/project",
  "branch": "tandem/20261023-120000-parser",
  "base": "f89ce5c3f217b8d35b0d8e804ba98a10ab78f6a1",
  "worktree": "/home/me/.tandem/runs/20261023-120000-parser/work",
  "commits": 1,
  "diffstat": {"files": 2, "added": 40, "removed": 3},
  "outcome": "max_turns",
  "outcome_message": "Turn limit reached before both agents agreed.",
  "turn_count": 1,
  "cost_usd": 0.12,
  "max_cost": null,
  "transcript": "/home/me/.tandem/runs/20261023-120000-parser/transcript.md",
  "turns": [
    {"turn": 1, "agent": "claude", "status": "continue", "commit": "a12bc34", "seconds": 14, "summary": "Added parser tests."}
  ]
}
```

`commits` and `diffstat` are what the branch holds over `base`, so a script can tell an empty run
from one worth merging without running git; both are `null` when the branch cannot be read.
`cost_usd` counts Claude costs only; Codex cost is not tracked. Setup errors after argument
parsing produce the same JSON shape with `outcome: "error"` and null fields for a run that was
not created. Invalid arguments print usage on stderr. Ctrl+C records `stopped`; `resume` can
continue the run.

| Exit code | Meaning |
| --- | --- |
| 0 | Done |
| 1 | Usage or setup error |
| 2 | Out of turns (`max_turns`) |
| 3 | Blocked |
| 4 | Stopped |
| 5 | Both agents failed |
| 6 | Reported-cost cap reached (`over_budget`) |

These outcome exit codes apply to `-p`; the existing `start` and `resume` commands keep their
previous exit behavior.

## Limit reported spending

Pass `--max-cost 5` to `start`, headless `-p`, or the interactive session to give its runs a
$5 cap. `campaign start --max-cost 5` shares one cap across all goals in that campaign.
The cap is saved in the run or campaign config. Amounts must be greater than zero; invalid
amounts are usage errors (exit 1).

Only cost reported by the CLIs counts: Claude reports dollar cost; Codex reports none and
bills through its subscription. Tandem does not estimate Codex cost. Before each turn,
tandem checks the total and stops with `over_budget` if the cap has been reached. It never
kills a running turn for this cap, so spending can overshoot by at most one turn. Claude
receives the remaining amount as `--max-budget-usd`, or the smaller of that amount and
`--claude-budget` when both are set. Turns that fail or are retried after a network error
still count what they spent, and in a campaign so do the calls that plan the next goal.

Raise the cap and continue with `tandem resume RUN --max-cost 10` or
`tandem campaign resume CAMPAIGN --max-cost 10`. In a session, `/budget 10` changes the
current run or campaign's cap from the next turn; `/budget off` removes it and `/budget`
shows spent cost and the cap. The status bar shows `$2.83/$5.00` when capped, and a log
warning appears when 80% is spent. Headless JSON includes `max_cost` (`null` when unset);
`over_budget` exits with code 6 in headless mode.

## Campaign: keep going after one goal

A campaign chains goals on one branch and one worktree. Queued goal files go first; after that,
one agent (read-only, alternating) reads the project's plan and code and picks the next goal,
with its own check command. It keeps going until the planner says the plan is done, you stop
it, or three goals in a row end blocked or out of turns (then read `BLOCKERS.md` and resume).
This works best when the repository has a written plan or roadmap for the planner to follow.

```bash
tandem campaign start --repo ~/code/my-project --name v1 --goal-file goals/my-goal.md
tandem campaign start --adopt <run-id> ...     # continue an existing run as the first goal
tandem campaign status campaign-<id>
tandem campaign say campaign-<id> "Skip the admin screens for now."
tandem campaign stop campaign-<id>             # after the current turn
tandem campaign resume campaign-<id>
```

When both agents fail (usually a usage limit), the campaign waits 30 minutes and retries, up to
six times. An agent that hits its usage limit is paused until the reset time it reports; the
other one carries on alone. In a campaign, work closed by one agent alone is reviewed by the
other once it is back.

Runs and campaigns are independent (own worktree, branch and run directory), so several can run
at once in separate terminals, on one project or many. They share your usage limits, and Docker
ports, which the agents are told to pick freely.

## How the agents are allowed to act

- **Claude Code**: `claude -p` with `--permission-mode acceptEdits` and an allow-list of tools
  (`make`, `npm`, `python3`, `pytest`, `docker run/build/exec/stop/rm`, `curl`, read-only git,
  ...). Volume deletion and `docker system prune` are denied. Anything else is denied, not
  prompted. Your user settings and hooks are not loaded (`--setting-sources project,local`);
  the target repository's own `.claude/settings.json` is. Edit `CLAUDE_ALLOWED` in `tandem.py`
  to widen the list (for example `Bash(cargo:*)` or `Bash(go:*)`).
- **Codex**: `codex exec` in the `workspace-write` sandbox with approvals off. No network
  unless `--codex-network`, which is exactly the kind of gap the other agent covers.
- Neither agent commits, pushes or switches branches; the orchestrator owns git. Its commits
  skip the target repository's git hooks, since a lint hook failing on half-done work must not
  stop a turn.

**The Claude allow-list is a guard rail, not a sandbox**: `python3` or `npm` can run any code
with your user's permissions. Only Codex's side is sandboxed. Run it on repositories you trust,
ideally in a VM or container if the code is not yours.

## Cost and auth

A smoke test (two trivial Claude turns on the default model) cost $1.57 on API pricing, so use
`--claude-model sonnet` or `--claude-budget` (USD cap per Claude turn) for routine goals. On a
Claude subscription the turns count against your plan's usage limits instead. Codex cost is not
tracked.

`codex login status` can say "Logged in" while the refresh token is dead; if a Codex turn fails
with `Failed to refresh token`, run `codex logout && codex login` and `tandem resume <id>`. If
`~/.codex/config.toml` names a model your account does not have, pass `--codex-model`; the
available ones are in `~/.codex/models_cache.json`.

## Options worth knowing

`--first codex` to let Codex start, `--claude-model` / `--codex-model`, `--claude-effort` /
`--codex-effort`, `--timeout` (seconds per turn, default 1800), `--codex-role reviewer` (Claude builds, Codex
reviews each goal instead of alternating), `--base` (start from another branch), `--push`
(campaigns: push the branch after every goal). An agent that fails three turns in a row (crash,
bad output) is benched and the other one finishes alone. `/models` choices are saved in
`~/.tandem/config.json` and used when a flag is not given.

## Notifications

A run takes long enough that you will look away, so tandem tells you when it needs you: when a
run ends (any outcome, including a crash) in the interactive session, `start` or `resume`; when
a campaign ends or crashes; and once each time both agents are on usage limits and the loop
starts waiting, with the time it resumes. Runs inside a campaign do not notify on their own, only
the campaign's end does. Ctrl+C does not notify, since whoever pressed it is looking. The
message is short, such as `status-bar: done in 5 turns, $2.83`,
`status-bar: blocked, Codex needs you` or `campaign 20261024-0848: stopped after 3 goals, 14 turns, $9.10`.

It uses what the machine already has: `osascript` on macOS, `notify-send` on Linux when it is on
PATH, the OSC 9 escape in iTerm2, and always the terminal bell. With none of those, the bell is
all you get. A notifier that hangs is given up after 2 seconds and never stops the run.
`doctor` says which one will be used.

Headless `-p` stays quiet unless you pass `--notify`, so scripts and CI do not ring. Pass
`--no-notify` to the session, `start`, `resume`, `campaign start` or `campaign resume` to turn it
off once, or set `"notify": false` in `~/.tandem/config.json` to turn it off by default.
Inside the session, `/resume --no-notify`, `/campaign start --no-notify` and
`/campaign resume DIR --no-notify` silence only that run or campaign; the next command uses
the session default again.

## Configuration

| Variable | Effect |
| --- | --- |
| `TANDEM_PYTHON` | The interpreter the npm launcher uses; give a Python executable name or path, without flags. If it is not Python 3.9+, the launcher stops with an error instead of searching further. |
| `TANDEM_RUNS` | Run storage directory. Relative paths resolve from the directory where tandem starts; use an absolute path to find the same runs from any repo. |
| `NO_COLOR` | When present, disables colors. The line editor keeps its plain status bar. |

Without `TANDEM_PYTHON`, the launcher tries `python3`, then `python3.15` through `python3.9`,
then `python` (and `py -3` on Windows). Each candidate must pass a version check. If none
qualifies, the launcher lists what it tried and how to install Python.

Run storage uses the first applicable path:

1. `TANDEM_RUNS`, when set.
2. `../tandem-runs` beside tandem's own Git checkout, including a worktree checkout.
3. `~/.tandem/runs` for an installed copy.

The directory is created when needed. `doctor` prints its resolved path and checks an actual
write. Run directories must stay outside the target repository and tandem's own files because
Claude Code is not sandboxed; tandem refuses to start a run when the runs directory is inside the
target repository (a repository at your home directory contains `~/.tandem/runs`, for example).
Usage limits seen by either agent are kept in `~/.tandem/availability.json`, shared by every copy
of tandem on the machine. If you already have runs under `../tandem-runs`, set
`TANDEM_RUNS` to that directory's absolute path to keep those run ids reachable after the npm
install. For example:

```bash
export TANDEM_RUNS="$HOME/code/tandem-runs"
export TANDEM_PYTHON=/usr/bin/python3
```

Interactive consent is stored separately in `~/.tandem/config.json`, with the version and
date when you accepted. Headless `-p` never prompts for consent.

## From source and development

The source entry point still works directly, without installing the npm package:

```bash
git clone https://github.com/lyczos/tandem.git
cd tandem
./tandem.py doctor
./tandem.py start --repo ~/code/my-project --goal-file examples/todo-cli.md
python3 -m unittest discover -s tests
```

Use `./tandem.py` in place of `tandem` for any command, including interactive mode, `-p`,
`runs`, and `--version`. For an interactive session in another repository, use
`./tandem.py --repo ~/code/my-project`.

The orchestrator remains one standard-library Python file. The npm launcher has no runtime
dependencies or install scripts; it only finds Python and forwards arguments, signals, and
exit codes. There is no telemetry or auto-update. The prompts live in `prompts/`, and the
result schemas in `schema/`; all four runtime files ship in the npm tarball.

CI runs the suite on Python 3.9, 3.10, and 3.13 on macOS and Ubuntu. A separate Node 20 job
packs and installs the tarball, then checks `tandem --version` and `tandem --help`. The
Python suite can also run without Node; launcher-specific tests skip in that case.

## License

MIT, see [LICENSE](LICENSE).

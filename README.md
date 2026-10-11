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
CLIs and logins, and the current repository and branch. Every line should say `ok`; a `FAIL`
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
short goals. A good goal says what "done" means in checkable terms;
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
- `turn-NN-<agent>.*` - the exact prompt and raw output of every turn
- `state.json` - what `resume` continues from

Talk to them while they work; the next agent reads it first:

```bash
tandem say <id> "Use SQLAlchemy 2.0 async, not psycopg directly."
tandem status <id>
tandem resume <id> --more-turns 6     # after Ctrl+C, a limit, or a blocked run
```

When a run is done, review and take the branch:

```bash
cd ~/code/my-project
git log --oneline main..tandem/<id>
git diff main...tandem/<id>
git merge --squash tandem/<id>              # or open a PR from the branch
git worktree remove <runs-directory>/<id>/work
```

## Interactive session

Run `tandem` with no arguments in a terminal. It prints the version, repo and branch, and whether
`claude` and `codex` are installed and logged in (with `NO_COLOR` or no terminal, also the Python in
use, as plain lines). The first time on a machine
it shows the security warning and asks you to type `yes`; the answer is saved in
`~/.tandem/config.json`.

Plain text is a goal and starts a run in the current repo. The run goes on in the background, so
you can keep typing and talk to the agents while they work. Slash commands:

- `/help`, `/status`, `/runs`, `/transcript` (prints the transcript path), `/doctor`
- `/say TEXT` leaves a message the next turn reads
- Ctrl+V pastes the image on the clipboard (a screenshot) into the line as `[image #1]`, as in
  Claude Code. `/paste` attaches it to your next goal or `/say` instead, and so does dragging
  image files onto the terminal; `/paste clear` drops what is waiting. Cmd+V stays the
  terminal's own text paste
- `/stop` stops after the current turn; `/resume [TURNS]` continues the last run (in a new
  session, the latest run in this repo)
- `/goal FILE` starts a run from a goal file; `/campaign ARGS...` is `tandem campaign ARGS...`
- `/quit` or Ctrl+D leaves after the current turn finishes

Only one run or campaign runs in a session at a time. Another goal or `/resume` while it is busy
leaves the current run and any pending `/stop` unchanged. Ctrl+C at the prompt clears the line
and during a slow command (such as `/doctor`) cancels that command; neither touches the run.
While `/quit` waits for the turn to finish, Ctrl+C stops the agent instead: the turn is recorded
as failed and `/resume` or `tandem resume` continues from there. Closing the terminal or sending
the session SIGTERM does the same without asking, so no agent keeps working unsupervised.

Images are copied to `~/.tandem/images` and named in the message as `[image: PATH]`, so every
later turn sees them too. Codex gets them attached with `--image`; Claude Code gets the folder
with `--add-dir` and is told to open them. `/paste` reads the clipboard with `osascript` on
macOS and `wl-paste` or `xclip` on Linux; elsewhere, drag the file in.

On a terminal without `NO_COLOR`, an updating
"turn 3: Claude Code working... 2m14s" line shows above the prompt. Set `NO_COLOR` to disable
terminal escape codes and the status line; redirected stdout uses plain text too.

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
  "outcome": "max_turns",
  "outcome_message": "Turn limit reached before both agents agreed.",
  "turn_count": 1,
  "cost_usd": 0.12,
  "transcript": "/home/me/.tandem/runs/20261023-120000-parser/transcript.md",
  "turns": [
    {"turn": 1, "agent": "claude", "status": "continue", "commit": "a12bc34", "seconds": 14, "summary": "Added parser tests."}
  ]
}
```

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

These outcome exit codes apply to `-p`; the existing `start` and `resume` commands keep their
previous exit behavior.

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

## Configuration

| Variable | Effect |
| --- | --- |
| `TANDEM_PYTHON` | The interpreter the npm launcher uses; give a Python executable name or path, without flags. If it is not Python 3.9+, the launcher stops with an error instead of searching further. |
| `TANDEM_RUNS` | Run storage directory. Relative paths resolve from the directory where tandem starts; use an absolute path to find the same runs from any repo. |
| `NO_COLOR` | When present, disables escape codes and the updating status line. |

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

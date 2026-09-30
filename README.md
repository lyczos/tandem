# tandem

Claude Code and Codex work on one repository in turns, as peers. Each turn one agent does a
step, the orchestrator commits it, and the other agent reviews that commit and carries on.
When an agent cannot do something (sandbox, missing tool, no idea), it says so in
`could_not_do` and the other agent tries first. The run ends when both agents say `done` in a
row and the optional check command passes.

Two different models catch different mistakes: one writes, the other reviews with fresh eyes,
and neither can close the goal alone. You watch the transcript, leave messages between turns
and merge the branch when you like it.

One Python file, standard library only. macOS and Linux; Windows is untested.

## Getting started

Everything runs on your machine with your own accounts: your Claude subscription or API key
pays for the Claude turns, your ChatGPT plan or OpenAI API key for the Codex turns.

1. **Python 3.10+ and git**, with `git config --global user.name` and `user.email` set (every
   turn is committed).

2. **Claude Code**, then log in:

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash     # or: npm install -g @anthropic-ai/claude-code
   claude auth login                                   # Claude Pro/Max, or a Console account
   ```

   With an API key instead, export it in the shell you start `tandem.py` from:
   `export ANTHROPIC_API_KEY=sk-ant-...`. Put it in your shell profile, not in
   `~/.claude/settings.json`: the agents run with your user settings switched off (see below),
   so a key there is not seen.

3. **Codex CLI**, then log in:

   ```bash
   npm install -g @openai/codex                        # or: brew install --cask codex
   codex login                                         # ChatGPT Plus/Pro/Team in the browser
   ```

   With an API key instead: `printenv OPENAI_API_KEY | codex login --with-api-key`.

4. **Check the setup:**

   ```bash
   git clone https://github.com/lyczos/tandem.git && cd tandem
   ./tandem.py doctor
   ```

   Every line should say `ok`. A `FAIL` line tells you what to run.

5. **First run on a throwaway repo**, to see it work before pointing it at real code:

   ```bash
   mkdir -p ~/code/tandem-test && cd ~/code/tandem-test && git init -q
   echo "# tandem test" > README.md && git add . && git commit -qm init
   cd -
   ./tandem.py start --repo ~/code/tandem-test --goal-file goals/example-todo-cli.md \
     --claude-model sonnet --max-turns 6
   ```

   Follow it with `tail -f ../tandem-runs/*-todo-cli/transcript.md` in a second terminal. The
   result sits on branch `tandem/<id>-todo-cli` in `~/code/tandem-test`; `main` is untouched.

## Requirements

- `claude` (Claude Code, logged in) and `codex` (Codex CLI, `codex login`) on `PATH`
- Python 3.10+, standard library only
- the target repository is a git repo with at least one commit; its `CLAUDE.md` is the rulebook
  for both agents (create one if it has none: stack, how to run tests, what not to touch)

## Run

```bash
./tandem.py start \
  --repo ~/code/my-project \
  --goal-file goals/my-goal.md \
  --name my-goal \
  --check "npm test" \
  --max-turns 12
```

`--goal "..."` works instead of `--goal-file` for short goals. A good goal says what "done"
means in checkable terms; `goals/example-todo-cli.md` shows the shape. A goal file may start
with a header: `---`, then `name:`, `first: codex|claude`, `check:`, then `---`.

Each run gets a directory in `../tandem-runs/<id>/`, next to this repo and not inside it, so an
agent that writes outside its worktree cannot change tandem itself (Claude is not sandboxed).
Set `TANDEM_RUNS` to put it elsewhere. Commands accept the directory as a path, as `runs/<id>`
or as the bare `<id>`:

- `work/` - a git worktree on branch `tandem/<id>`; your own checkout is never touched
- `transcript.md` - the conversation, readable while it runs (`tail -f`)
- `turn-NN-<agent>.*` - the exact prompt and raw output of every turn
- `state.json` - what `resume` continues from

Talk to them while they work; the next agent reads it first:

```bash
./tandem.py say <id> "Use SQLAlchemy 2.0 async, not psycopg directly."
./tandem.py status <id>
./tandem.py resume <id> --more-turns 6     # after Ctrl+C, a limit, or a blocked run
```

When a run is done, review and take the branch:

```bash
cd ~/code/my-project
git log --oneline main..tandem/<id>
git diff main...tandem/<id>
git merge --squash tandem/<id>              # or open a PR from the branch
git worktree remove <tandem checkout>/../tandem-runs/<id>/work
```

## Campaign: keep going after one goal

A campaign chains goals on one branch and one worktree. Queued goal files go first; after that,
one agent (read-only, alternating) reads the project's plan and code and picks the next goal,
with its own check command. It keeps going until the planner says the plan is done, you stop
it, or three goals in a row end blocked or out of turns (then read `BLOCKERS.md` and resume).
This works best when the repository has a written plan or roadmap for the planner to follow.

```bash
./tandem.py campaign start --repo ~/code/my-project --name v1 --goal-file goals/my-goal.md
./tandem.py campaign start --adopt <run-id> ...     # continue an existing run as the first goal
./tandem.py campaign status campaign-<id>
./tandem.py campaign say campaign-<id> "Skip the admin screens for now."
./tandem.py campaign stop campaign-<id>             # after the current turn
./tandem.py campaign resume campaign-<id>
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
with `Failed to refresh token`, run `codex logout && codex login` and `tandem.py resume`. If
`~/.codex/config.toml` names a model your account does not have, pass `--codex-model`; the
available ones are in `~/.codex/models_cache.json`.

## Options worth knowing

`--first codex` to let Codex start, `--claude-model` / `--codex-model`, `--codex-effort`,
`--timeout` (seconds per turn, default 1800), `--codex-role reviewer` (Claude builds, Codex
reviews each goal instead of alternating), `--base` (start from another branch), `--push`
(campaigns: push the branch after every goal). An agent that fails three turns in a row (crash,
bad output) is benched and the other one finishes alone.

## Development

```bash
python3 -m unittest discover -s tests
```

The prompts the agents get are in `prompts/`, the JSON they must answer with in `schema/`.

## License

MIT, see [LICENSE](LICENSE).

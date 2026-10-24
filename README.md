# tandem

[![ci](https://github.com/lyczos/tandem/actions/workflows/ci.yml/badge.svg)](https://github.com/lyczos/tandem/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/@lyczos/tandem)](https://www.npmjs.com/package/@lyczos/tandem)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Claude Code and Codex take turns on your repository until both agree the goal is done.

One agent takes a step, tandem commits it, and the other reviews that commit and carries on.
A goal closes only when both agents say `done` in a row, the second one changes nothing, and
your check command passes. You get a branch to review and merge; your own checkout is never
touched.

> **Security:** Claude Code runs with an allow-list of commands. It is a guard rail, not a
> sandbox: `python3` or `npm` can run any code as your user. Only Codex is sandboxed. Use
> tandem on repositories you trust, ideally in a VM or container if the code is not yours.

## Install

You need Python 3.9+, Node 18+, Git, and both agent CLIs logged in.

```bash
npm install -g @anthropic-ai/claude-code @openai/codex   # skip if you have them
claude auth login && codex login
npm install -g @lyczos/tandem
tandem doctor                                            # every line should say ok
```

## Quick start

```bash
cd ~/code/my-project        # any git repository with at least one commit
tandem
```

Type a goal at the prompt, such as `Add tests for the parser`, and watch the turns arrive as
cards. When the run ends, tandem says what changed and how to take it:

```text
  branch    tandem/20261024-120000-add-tests: 3 commits over f89ce5c3f217, 4 files changed, +120 -7
  read      git -C ~/code/my-project diff f89ce5c3f217..tandem/20261024-120000-add-tests
  merge     git -C ~/code/my-project merge tandem/20261024-120000-add-tests
```

Try it on a [throwaway repository](https://github.com/lyczos/tandem/blob/main/docs/usage.md#first-run-on-a-throwaway-repo)
before pointing it at real code.

## How a run works

- Each run gets its own git worktree and branch, `tandem/<id>`.
- The agents alternate. tandem commits every turn; the agents never commit, push or switch
  branches. Each turn hands the other agent a note: what is left, and what it could not do.
- A run ends `done`, `blocked` (the agents need you), out of turns, over the cost cap, or when
  you stop it. Every run can be resumed.
- An agent that hits its usage limit pauses until the reset; the other carries on alone.

## In the session

The run goes on in the background, so you can keep typing while the agents work.

| Key or command | What it does |
| --- | --- |
| `/` | menu of commands, narrowed as you type |
| text during a turn | a message for the next turn; tandem asks once whether to stop the current one |
| `/now TEXT` | stop the current turn and hand it your message |
| `/board` | what the agents agree on, what is open, models and cost |
| `/diff` | what the run changed and the commands to merge it |
| `/watch` | follow what the agents do in a pane on the right (tmux or iTerm2) |
| `/models` or Tab on an empty line | change models and effort, from the next turn |
| `/budget USD` | cap the reported cost of the run |
| `/resume`, `/stop`, `/quit` | continue the last run, stop after this turn, leave |
| Ctrl+V | paste a screenshot into your message |
| Ctrl+C twice | leave now; the turn is kept and can be resumed |

A status bar under the prompt shows each agent's model and what it is doing, the turn, cost,
tokens, the branch's diff size and the last check. A desktop notification tells you when a run
ends or both agents are waiting for their limits.

## Goal files

A goal longer than a sentence belongs in a file: what to build, then what "done" means in
checkable terms. The agents decide against that list.

```markdown
---
name: todo-cli
first: claude
check: python3 -m unittest discover -s tests
---
Add a command-line todo list: `todo.py add "buy milk"`, `todo.py list`, `todo.py done 2`.

Done means:
- the three commands work, and a bad number exits non-zero with a clear message;
- tests cover each command and the error cases;
- `python3 -m unittest discover -s tests` passes.
```

The header is optional: `name` names the run and its branch, `first` picks the agent that
starts, and `check` must pass before the run can end. Run it with `/goal todo.md` in the
session or `tandem start --goal-file todo.md`. The full version is in
[`examples/todo-cli.md`](examples/todo-cli.md).

## Scripts and CI

```bash
tandem -p "Add tests for the parser" --max-turns 6 --max-cost 5
tandem -p --goal-file goal.md --output-format json > result.json
```

Progress goes to stderr; the result goes to stdout, as one JSON object with
`--output-format json`. The exit code says how it ended: 0 done, 1 usage or setup error,
2 out of turns, 3 blocked, 4 stopped, 5 both agents failed, 6 over the cost cap.

## Options worth knowing

| Option | Effect |
| --- | --- |
| `--check CMD` | a command that must pass before a goal is done |
| `--max-turns N` | turns per goal (default 12) |
| `--max-cost USD` | stop before a turn that would start past this reported cost; Codex reports none |
| `--claude-model`, `--codex-model`, `--claude-effort`, `--codex-effort` | models and effort; `/models` saves your defaults |
| `--first codex` | let Codex take the first turn |
| `--codex-role reviewer` | Claude builds, Codex only reviews |
| `--base BRANCH` | start from another branch |

`tandem campaign start` chains goals on one branch and lets an agent pick the next one from the
project's own plan. `tandem runs` lists past runs, and `tandem resume <id>` continues one.

## More

- [Full guide](https://github.com/lyczos/tandem/blob/main/docs/usage.md): every command and
  key, run storage, campaigns, notifications, configuration, auth and costs.
- [Changelog](CHANGELOG.md).
- Development: tandem is one standard-library Python file, `tandem.py`, with a thin Node
  launcher. Run the tests with `python3 -m unittest discover -s tests`; [`CLAUDE.md`](CLAUDE.md)
  describes the architecture and the rules it keeps.

## License

MIT, see [LICENSE](LICENSE).

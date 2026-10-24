# Changelog

## 0.1.0

- tandem tells you when a run needs you: a desktop notification (`osascript` on macOS,
  `notify-send` on Linux when present), OSC 9 in iTerm2 and the terminal bell when a run ends
  or crashes in the session, `start` or `resume`, when a campaign ends or crashes, and once per wait when both agents
  are on usage limits. Headless `-p` notifies only with `--notify`; `--no-notify` or
  `"notify": false` in `~/.tandem/config.json` turns it off. `doctor` names the notifier.

- Every run ends with what changed and how to take it: the branch, its commits and diffstat
  over the base, the worktree, and ready-to-copy `git -C REPO diff`, `merge` and cleanup
  commands (a `resume` line first when the run is unfinished). Campaigns print the same for
  their branch at the end. `tandem diff [RUN]` and `/diff` show it at any time with
  `git diff --stat`. Headless JSON gains `worktree`, `commits` and `diffstat`, and the
  diffstat now includes `files`. tandem still never merges, pushes or deletes anything itself.

- Add `--max-cost USD` for runs and campaigns, with a shared campaign cap, remaining-budget
  limits for Claude turns, and `over_budget` (headless exit 6). The cap counts reported cost
  only; Codex reports none. `/budget [USD|off]` changes the cap from the next turn. Show
  spent/cap in the status bar, warn at 80%, and include `max_cost` in headless JSON.
  Reported costs from failed attempts, retries, and campaign planners also count; retries
  and planners receive only the remaining budget.

- The interactive session is easier to read back: submitted lines stay as `› line` on a band,
  log lines carry a short `HH:MM` stamp and a symbol by kind instead of the full date, paths
  show with `~` or relative to the run and long log entries fit on one row. Each run opens and
  closes with a rule across the width (the closing one in the outcome's color), and the "talk to the agents"
  hint is gone there. `start`, `resume`, `-p` and non-terminal output are unchanged.
- Tab on an empty prompt line edits the models and effort on the status bar: Tab and Shift+Tab
  pick a field, ↑↓ change it through the values `/models` offers, Enter saves only what changed,
  Esc cancels, and any other key leaves the mode and is then handled as usual.
- `tandem watch` follows what the agents do during a turn, one line per command, file or
  message, from the run's new `activity.log`; `/watch` opens it in a pane on the right in tmux
  or iTerm2. Claude Code now runs with `--output-format stream-json`.
- Messages typed during a turn ask about stopping it once per turn and then queue, showing how
  many wait. At most five wait at a time; `/queue` lists them and `/queue clear` drops them.
- → at the end of the line accepts the highlighted slash command, like Tab.

- Add a live bar below the interactive prompt with agent models, effort and activity, reported
  cost and tokens, session time, committed branch changes and check state. Record reported usage
  and model on each turn. Narrow terminals truncate colored rows correctly; resizing redraws
  the visible screen without losing the text being edited.

- Ctrl+C twice on an empty prompt leaves the session, as in Claude Code; a turn in flight is
  stopped and recorded as interrupted, with its edits committed. `exit`, `quit` or `q` alone on
  the line leaves like `/quit` instead of starting a run with that goal. Further Ctrl+C presses
  while `/quit` waits no longer kill tandem, and the terminal's settings are put back on the way out.
- Step in during a turn: plain text typed while a run is going is saved for the next turn, and
  `y` to the question that follows stops the turn, keeps its edits in a commit and hands the same
  agent your correction; any other answer than `n` or Enter is saved as more of the correction.
  `/now TEXT` does it without the question. Interrupted turns show as
  `interrupted` and neither count as a failure nor use up a turn.
- Each turn of an interactive session shows as a card, and `/board` summarizes what the agents agree
  on, what is open, the models and the cost. Plain output (pipes, `NO_COLOR`) keeps the one-line log.
- The session's prompt has its own line editor: typing `/` shows the matching slash commands with
  a short description under the prompt, and earlier lines come back with ↑ from `~/.tandem/history`.
  Log lines and the status line no longer garble what is being typed, and an empty Enter prints nothing.
- `/models` picks Claude and Codex model and effort with the arrow keys; a running run or campaign
  takes the choice from its next turn.
- Add `--claude-effort`. Model and effort choices made with `/models` are saved in
  `~/.tandem/config.json` and used when the matching flag is not given.
- Add the public npm package `@lyczos/tandem`, installing the `tandem` command. The Node
  launcher has no runtime dependencies or install scripts; source execution still works.
- Lower the supported Python floor to 3.9, including macOS Command Line Tools Python 3.9.6.
  CI enforces the floor alongside Python 3.10 and 3.13 on macOS and Ubuntu.
- Add an interactive session with first-run consent, live progress, and slash commands for
  sending messages, stopping and resuming work, campaigns, status, and transcripts.
- Add headless `-p` goals from arguments, stdin, or a file, with text or JSON output,
  `--quiet`, and outcome exit codes 0 through 5.
- Store installed runs in `~/.tandem/runs`. `TANDEM_RUNS` takes precedence; source checkouts
  use `../tandem-runs`. Set `TANDEM_RUNS` to an existing directory to keep old run ids.
- A source checkout in a directory not named `tandem` used to keep runs in `../<name>-runs`;
  it now uses `../tandem-runs`. Set `TANDEM_RUNS` to the old directory to keep those runs.
- Usage limits are remembered in `~/.tandem/availability.json` instead of the runs directory,
  so every copy of tandem on a machine shares them.
- Refuse to start a run when the runs directory is inside the target repository.
- Closing the terminal or sending SIGTERM stops the agents along with tandem, in the interactive
  session and in `start`/`-p`. A `TANDEM_PYTHON` that is not Python 3.9+ is now an error.
- The interactive session takes screenshots: Ctrl+V pastes the clipboard's image into the line,
  `/paste` attaches it, and image files dragged onto the terminal are attached too. Both agents see them on every later turn.
- The interactive session opens with a colored logo and login status on a terminal.
- Add `tandem runs`, `--version`, current-directory repo defaults, and doctor checks for
  the interpreter, Node launcher, writable runs directory, and repository readiness.

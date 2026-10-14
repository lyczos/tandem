# Changelog

## 0.1.0

- Each turn of an interactive session shows as a card, and `/board` summarizes what the agents agree
  on, what is open, the models and the cost. Plain output (pipes, `NO_COLOR`) keeps the one-line log.
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

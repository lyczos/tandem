# Changelog

## 0.1.0

- Add the public npm package `@lyczos/tandem`, installing the `tandem` command. The Node
  launcher has no runtime dependencies or install scripts; source execution still works.
- Lower the supported Python floor to 3.9, including macOS Command Line Tools Python 3.9.6.
  CI enforces the floor alongside Python 3.10 and 3.13 on macOS and Ubuntu.
- Add an interactive session with first-run consent, live progress, and slash commands for
  sending messages, stopping and resuming work, campaigns, status, and transcripts.
- Add headless `-p` goals from arguments, stdin, or a file, with text or JSON output,
  `--quiet`, and outcome exit codes 0 through 5.
- Store installed runs in `~/.tandem/runs`. `TANDEM_RUNS` takes precedence; source checkouts
  retain `../tandem-runs`. Set `TANDEM_RUNS` to an existing directory to keep old run ids.
- Add `tandem runs`, `--version`, current-directory repo defaults, and doctor checks for
  the interpreter, Node launcher, writable runs directory, and repository readiness.

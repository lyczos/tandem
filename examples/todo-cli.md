---
name: todo-cli
first: claude
check: python3 -m unittest discover -s tests
---
Add a small command-line todo list to this repository, in Python 3.10+ with the standard library
only.

`todo.py add "buy milk"` adds an item, `todo.py list` prints the open items numbered from 1,
`todo.py done 2` marks the second one done. Items are kept in `todo.json` next to the script,
created on first use.

Done means:
- `todo.py` does the three commands above and exits non-zero with a clear message on a bad
  number or an unknown command;
- `tests/test_todo.py` covers each command and the error cases, using a temporary directory
  instead of the real `todo.json`;
- `python3 -m unittest discover -s tests` passes;
- `README.md` has a short usage section.

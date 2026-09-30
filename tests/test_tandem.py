import datetime
import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tandem", ROOT / "tandem.py")
tandem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tandem)


class ParseResult(unittest.TestCase):
    def test_bare_json(self):
        self.assertEqual(tandem.parse_result('{"status": "done"}', "status"), {"status": "done"})

    def test_last_fenced_block_wins(self):
        text = 'first\n```json\n{"status": "continue"}\n```\nthen\n```json\n{"status": "done"}\n```'
        self.assertEqual(tandem.parse_result(text, "status"), {"status": "done"})

    def test_missing_key_or_garbage(self):
        self.assertIsNone(tandem.parse_result('{"other": 1}', "status"))
        self.assertIsNone(tandem.parse_result("no json here", "status"))
        self.assertIsNone(tandem.parse_result(None, "status"))


class Availability(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(tandem.classify("You've hit your limit, resets 5pm"), "limit")
        self.assertEqual(tandem.classify("API Error: 529 overloaded"), "transient")
        self.assertEqual(tandem.classify("syntax error in prompt"), "other")

    def test_parse_reset_reads_the_time(self):
        at = tandem.parse_reset("Usage limit reached, try again at 5:45 PM.")
        self.assertEqual((at.hour, at.minute), (17, 47))  # two minutes of slack
        self.assertGreater(at, datetime.datetime.now())

    def test_parse_reset_falls_back_to_an_hour(self):
        at = tandem.parse_reset("limit reached")
        expected = datetime.datetime.now() + datetime.timedelta(seconds=tandem.LIMIT_FALLBACK_WAIT)
        self.assertLess(abs((at - expected).total_seconds()), 5)


class GoalFile(unittest.TestCase):
    def read(self, text, name="my-goal.md"):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / name
            path.write_text(text)
            return tandem.read_goal_file(path)

    def test_header(self):
        goal = self.read("---\nname: api\nfirst: codex\ncheck: make test\n---\nBuild the API.\n")
        self.assertEqual(goal, {"goal": "Build the API.", "name": "api", "first": "codex", "check": "make test"})

    def test_plain_text_uses_the_file_name(self):
        goal = self.read("Build the API.\n")
        self.assertEqual(goal, {"goal": "Build the API.", "name": "my-goal", "first": "claude", "check": ""})


class Prompts(unittest.TestCase):
    def test_old_turns_are_one_line_each(self):
        history = [{"turn": i, "agent": "claude" if i % 2 else "codex", "status": "continue",
                    "summary": f"step {i}", "handoff": f"next {i}"} for i in range(1, 12)]
        text = tandem.render_history(history)
        self.assertIn("- Turn 1 Claude Code (continue): step 1", text)
        self.assertNotIn("Handoff: next 1\n", text)
        self.assertIn("### Turn 11 - Claude Code - continue", text)
        self.assertIn("Handoff: next 11", text)

    def test_owner_messages_stay_and_newest_is_flagged(self):
        msgs = [{"at": "2026-01-01 10:00:00", "text": "use sqlite"}, {"at": "2026-01-01 11:00:00", "text": "no docker"}]
        text = tandem.owner_block(msgs, fresh=True)
        self.assertIn("use sqlite", text)
        self.assertIn("NEW, read first", text)
        self.assertEqual(tandem.owner_block([], fresh=False), "")


class Git(unittest.TestCase):
    def test_turn_commits_despite_a_failing_hook(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
            run = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True, env=env)
            run("init", "-q")
            run("commit", "-q", "--allow-empty", "-m", "init")
            hook = pathlib.Path(d) / ".git" / "hooks" / "pre-commit"
            hook.write_text("#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
            (pathlib.Path(d) / "a.txt").write_text("a")
            old = {k: os.environ.get(k) for k in env if k.startswith("GIT_")}
            os.environ.update({k: v for k, v in env.items() if k.startswith("GIT_")})
            try:
                sha = tandem.commit_turn({"worktree": d, "name": "t"}, "claude", 1, "add a.txt", False)
            finally:
                for k, v in old.items():
                    os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
            self.assertTrue(sha)
            self.assertEqual(tandem.git(d, "log", "-1", "--format=%s"), "tandem(claude): t turn 1 - add a.txt")

    def test_git_error_says_what_failed(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(tandem.GitError) as ctx:
                tandem.git(d, "rev-parse", "HEAD")
            self.assertIn("git rev-parse", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

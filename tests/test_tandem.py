import datetime
import importlib.util
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tandem", ROOT / "tandem.py")
tandem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tandem)


class RunsDirectory(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.base = pathlib.Path(temp.name)
        self.root = self.base / "renamed-checkout"
        self.root.mkdir()
        for patch in (mock.patch.object(tandem, "ROOT", self.root),
                      mock.patch.dict(os.environ, {"TANDEM_RUNS": ""}),
                      mock.patch.object(pathlib.Path, "home", return_value=self.base / "home")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_env_override_wins_for_source_and_installed_copy(self):
        override = self.base / "custom" / ".." / "runs"
        with mock.patch.dict(os.environ, {"TANDEM_RUNS": str(override)}):
            self.assertEqual(tandem.resolve_runs(), self.base / "runs")
            (self.root / ".git").mkdir()
            self.assertEqual(tandem.resolve_runs(), self.base / "runs")
        self.assertFalse((self.base / "runs").exists())

    def test_source_checkout_uses_fixed_sibling_name(self):
        (self.root / ".git").mkdir()
        self.assertEqual(tandem.resolve_runs(), self.base / "tandem-runs")
        self.assertFalse((self.base / "tandem-runs").exists())

    def test_worktree_uses_same_sibling_directory(self):
        (self.root / ".git").write_text("gitdir: /another/checkout/.git/worktrees/tandem\n")
        self.assertEqual(tandem.resolve_runs(), self.base / "tandem-runs")

    def test_installed_copy_ignores_parent_checkout(self):
        (self.base / ".git").mkdir()
        expected = self.base / "home" / ".tandem" / "runs"
        self.assertEqual(tandem.resolve_runs(), expected)
        self.assertFalse(expected.exists())

    def test_doctor_creates_directory_and_removes_write_probe(self):
        runs = self.base / "storage" / "runs"
        with mock.patch.object(tandem, "RUNS", runs):
            self.assertEqual(tandem.check_runs_directory(), (True, str(runs)))
        self.assertTrue(runs.is_dir())
        self.assertEqual(list(runs.iterdir()), [])

    def test_doctor_reports_failed_write_with_fix(self):
        runs = self.base / "runs"
        with mock.patch.object(tandem, "RUNS", runs), \
                mock.patch.object(tempfile, "TemporaryFile", side_effect=PermissionError("write denied")):
            ok, detail = tandem.check_runs_directory()
        self.assertFalse(ok)
        self.assertIn(str(runs), detail)
        self.assertIn("write denied", detail)
        self.assertIn("set TANDEM_RUNS to a writable directory", detail)

    def test_doctor_reports_file_instead_of_directory(self):
        runs = self.base / "runs"
        runs.write_text("not a directory")
        with mock.patch.object(tandem, "RUNS", runs):
            ok, detail = tandem.check_runs_directory()
        self.assertFalse(ok)
        self.assertIn(str(runs), detail)
        self.assertIn("set TANDEM_RUNS", detail)


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


@unittest.skipUnless(shutil.which("node") and os.name == "posix", "launcher checks need Node and POSIX")
class NpmLauncher(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.temp = pathlib.Path(temp.name)
        self.node = shutil.which("node")
        self.env = dict(os.environ, PATH=str(self.temp), TANDEM_PYTHON="")

    def interpreter(self, name, version="3.9", body="print('fallback')"):
        path = self.temp / name
        path.write_text(
            f"#!{sys.executable}\nimport json, os, signal, sys, time\n"
            f"if sys.argv[1] == '-c':\n    print({version!r})\n    sys.exit()\n{body}\n")
        path.chmod(0o755)
        return path

    def launch(self, *args):
        return subprocess.run([self.node, str(ROOT / "bin/tandem.js"), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_override_preserves_arguments_environment_and_exit_code(self):
        python = self.interpreter("custom-python", body=(
            "print(json.dumps([sys.argv[1:], os.environ['TANDEM_PROG'], os.environ['TANDEM_NODE']]))\n"
            "sys.exit(3)"))
        self.interpreter("python3")
        self.env["TANDEM_PYTHON"] = str(python)
        result = self.launch("-p", "goal with spaces", "--max-turns", "2")
        self.assertEqual(result.returncode, 3, result.stderr)
        argv, program, node = json.loads(result.stdout)
        self.assertEqual(argv, [str(ROOT / "tandem.py"), "-p", "goal with spaces", "--max-turns", "2"])
        self.assertEqual(program, "tandem")
        self.assertTrue(node.startswith("v"))

    def test_unsupported_versions_fall_back_in_order(self):
        self.env["TANDEM_PYTHON"] = str(self.interpreter("old-python", "3.8"))
        self.interpreter("python3", "2.7")
        self.interpreter("python3.13", "3.13", "print('python3.13')")
        self.interpreter("python3.9", "3.9", "print('python3.9')")
        result = self.launch("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "python3.13")

    def test_no_python_gives_actionable_error(self):
        self.env["TANDEM_PYTHON"] = str(self.temp / "missing-python")
        result = self.launch("--version")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("tandem needs Python 3.9", result.stderr)
        self.assertIn("missing-python, python3, python3.13", result.stderr)
        self.assertIn("python3.9, python.", result.stderr)
        self.assertIn("Install Python", result.stderr)
        self.assertNotIn("Error:", result.stderr)

    def check_signal(self, sig, group=False):
        self.env["TANDEM_PYTHON"] = str(self.interpreter("signal-python", body=(
            "def stop(signum, frame):\n    print('interrupted', flush=True)\n    sys.exit(4)\n"
            "signal.signal(signal.SIGINT, stop)\nprint('ready', flush=True)\ntime.sleep(30)")))
        stdin = subprocess.DEVNULL
        if group:
            import pty
            master, stdin = pty.openpty()
            self.addCleanup(os.close, master)
            self.addCleanup(os.close, stdin)
        with tempfile.TemporaryFile(dir=self.temp, mode="w+") as output:
            proc = subprocess.Popen([self.node, str(ROOT / "bin/tandem.js")], env=self.env,
                                    stdin=stdin, stdout=output, stderr=output, start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while True:
                    output.seek(0)
                    if "ready" in output.read():
                        break
                    self.assertIsNone(proc.poll(), "launcher exited before Python was ready")
                    self.assertLess(time.monotonic(), deadline, "Python never became ready")
                    time.sleep(.01)
                if group:
                    os.killpg(proc.pid, sig)
                else:
                    proc.send_signal(sig)
                proc.wait(timeout=5)
                output.seek(0)
                text = output.read()
                self.assertEqual(proc.returncode, -signal.SIGTERM if sig == signal.SIGTERM else 4, text)
                if sig == signal.SIGINT:
                    self.assertIn("interrupted", text)
            finally:
                # Also reap the fake Python if a broken launcher exits without forwarding.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()

    def test_sigterm_reaches_child_and_preserves_signal_exit(self):
        self.check_signal(signal.SIGTERM)

    def test_sigint_reaches_child_and_preserves_handled_exit(self):
        self.check_signal(signal.SIGINT)

    def test_tty_group_sigint_allows_python_cleanup(self):
        self.check_signal(signal.SIGINT, group=True)


class Packaging(unittest.TestCase):
    def setUp(self):
        self.pkg = json.loads((ROOT / "package.json").read_text())

    def test_version_matches_module(self):
        self.assertEqual(self.pkg["version"], tandem.__version__)

    def test_files_cover_runtime_assets(self):
        listed = self.pkg["files"]
        for asset in (tandem.TURN_PROMPT, tandem.PLAN_PROMPT, tandem.TURN_SCHEMA, tandem.PLAN_SCHEMA,
                      ROOT / "tandem.py", ROOT / "bin" / "tandem.js", ROOT / "README.md", ROOT / "LICENSE"):
            rel = asset.relative_to(ROOT).as_posix()
            self.assertTrue(asset.exists(), rel)
            self.assertTrue(any(rel == f or (f.endswith("/") and rel.startswith(f)) for f in listed),
                            f"{rel} is not shipped")

    def test_name_bin_and_no_install_scripts(self):
        self.assertEqual(self.pkg["name"], "@lyczos/tandem")
        self.assertEqual(self.pkg["bin"], {"tandem": "bin/tandem.js"})
        self.assertEqual(self.pkg["publishConfig"], {"access": "public"})
        self.assertFalse(self.pkg.get("dependencies"))
        for hook in ("preinstall", "install", "postinstall"):
            self.assertNotIn(hook, self.pkg.get("scripts", {}))


if __name__ == "__main__":
    unittest.main()

import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tandem", ROOT / "tandem.py")
tandem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tandem)
REAL_NOTIFY = tandem.notify


def setUpModule():
    # `start` and the session notify by default; no test may pop a real notification or ring the bell.
    patch = mock.patch.object(tandem, "notify", lambda title, body: None)
    patch.start()
    unittest.addModuleCleanup(patch.stop)


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
        self.interpreter("python3", "2.7")
        self.interpreter("python3.15", "3.8")
        self.interpreter("python3.14", "3.14", "print('python3.14')")
        self.interpreter("python3.9", "3.9", "print('python3.9')")
        result = self.launch("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "python3.14")

    def test_unusable_override_fails_instead_of_falling_through(self):
        self.interpreter("python3", "3.13", "print('python3')")
        for override, message in ((self.interpreter("old-python", "3.8"), "is Python 3.8"),
                                  (self.temp / "missing-python", "could not be run")):
            with self.subTest(override=override.name):
                self.env["TANDEM_PYTHON"] = str(override)
                result = self.launch("--version")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(f"TANDEM_PYTHON={override} {message}", result.stderr)
                self.assertNotIn("Error:", result.stderr)

    def test_no_python_gives_actionable_error(self):
        result = self.launch("--version")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("tandem needs Python 3.9", result.stderr)
        self.assertIn("Tried: python3, python3.15, python3.14", result.stderr)
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
                self.assertEqual(proc.returncode, 4 if sig == signal.SIGINT else -sig, text)
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

    def test_sighup_reaches_child(self):
        self.check_signal(signal.SIGHUP)

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


class Cli(unittest.TestCase):
    def parse(self, *argv):
        p = tandem.build_parser()
        args = p.parse_args(list(argv))
        return p, args

    def pick(self, *argv, tty=True):
        p, args = self.parse(*argv)
        with mock.patch.object(sys.stdin, "isatty", return_value=tty):
            return tandem.pick_command(p, args)

    def test_three_usage_shapes(self):
        self.assertIs(self.pick(), tandem.cmd_interactive)
        self.assertIs(self.pick("-p", "fix it", tty=False), tandem.cmd_headless)
        self.assertIs(self.pick("-p", "--goal-file", "g.md"), tandem.cmd_headless)
        self.assertIs(self.pick("doctor"), tandem.cmd_doctor)
        _, args = self.parse("-p", "fix it", "--max-turns", "3")
        self.assertEqual((args.prompt, args.max_turns), ("fix it", 3))
        _, args = self.parse("-p")
        self.assertEqual(args.prompt, "-")  # goal comes from stdin

    def test_no_command_without_terminal_exits_with_hint(self):
        with self.assertRaises(SystemExit) as cm:
            self.pick(tty=False)
        self.assertIn("-p", str(cm.exception.code))

    def test_prompt_with_command_is_rejected(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as cm:
            self.parse("-p", "goal", "doctor")
        self.assertEqual(cm.exception.code, 1)

    def test_usage_errors_exit_one(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as cm:
            self.parse("-p", "goal", "--max-turns", "nope")
        self.assertEqual(cm.exception.code, 1)

    def test_repo_defaults_to_cwd(self):
        self.assertEqual(self.parse("start")[1].repo, os.getcwd())
        self.assertEqual(self.parse("campaign", "start")[1].repo, os.getcwd())
        self.assertEqual(self.parse("-p", "x")[1].repo, os.getcwd())

    def test_runs_subcommand_and_prog_name(self):
        self.assertEqual(self.parse("runs", "--limit", "3")[1].limit, 3)
        with mock.patch.dict(os.environ, {"TANDEM_PROG": "tandem"}):
            self.assertEqual(tandem.prog_name(), "tandem")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(sys, "argv", ["tools/tandem.py"]):
                self.assertEqual(tandem.prog_name(), "tools/tandem.py")
            with mock.patch.object(sys, "argv", [""]):
                self.assertEqual(tandem.prog_name(), "./tandem.py")


class RequireRepo(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = pathlib.Path(temp.name)

    def test_not_a_repo_and_no_commits_fail_with_fix(self):
        with self.assertRaises(SystemExit) as cm:
            tandem.require_repo(self.dir)
        self.assertIn("git init", str(cm.exception.code))
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)
        with self.assertRaises(SystemExit) as cm:
            tandem.require_repo(self.dir)
        self.assertIn("no commits", str(cm.exception.code))

    def test_missing_directory(self):
        with self.assertRaises(SystemExit):
            tandem.require_repo(self.dir / "nope")

    def test_runs_directory_inside_the_repo_is_refused(self):
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "init"], cwd=self.dir, check=True)
        repo = self.dir.resolve()
        for runs in (repo, repo / ".tandem" / "runs"):
            with self.subTest(runs=runs), mock.patch.object(tandem, "RUNS", runs), \
                    self.assertRaises(SystemExit) as cm:
                tandem.require_repo(self.dir)
            self.assertIn("set TANDEM_RUNS", str(cm.exception.code))
        with mock.patch.object(tandem, "RUNS", repo.parent / "elsewhere"):
            self.assertEqual(tandem.require_repo(self.dir), repo)


class DoctorRepoAndVersions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = pathlib.Path(temp.name)

    def test_cwd_repo_status_steps(self):
        ok, detail = tandem.cwd_repo_status(self.dir)
        self.assertFalse(ok)
        self.assertIn("git init", detail)
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)
        ok, detail = tandem.cwd_repo_status(self.dir)
        self.assertFalse(ok)
        self.assertIn("no commits", detail)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "init"], cwd=self.dir, check=True)
        ok, detail = tandem.cwd_repo_status(self.dir)
        self.assertTrue(ok)
        self.assertIn(str(self.dir), detail)

    def test_doctor_prints_version_python_and_node(self):
        lines = []
        env = {"TANDEM_NODE": "v20.1.0"}
        fail = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(subprocess, "run", return_value=fail), \
                mock.patch.object(tandem, "RUNS", self.dir / "runs"), \
                mock.patch.object(tandem, "emit", side_effect=lines.append), \
                self.assertRaises(SystemExit):
            tandem.cmd_doctor(argparse.Namespace())
        text = "\n".join(lines)
        self.assertIn(tandem.__version__, text)
        self.assertIn("python 3.9+", text)
        self.assertIn("v20.1.0", text)
        self.assertIn("current repo", text)

    def test_doctor_without_git_reports_failures_instead_of_crashing(self):
        lines = []
        with mock.patch.dict(os.environ, {"PATH": ""}), \
                mock.patch.object(tandem, "RUNS", self.dir / "runs"), \
                mock.patch.object(tandem, "emit", side_effect=lines.append), \
                self.assertRaises(SystemExit) as exit:
            tandem.cmd_doctor(argparse.Namespace())
        self.assertEqual(exit.exception.code, 1)
        self.assertTrue(any("FAIL" in line and "git " in line for line in lines))
        self.assertTrue(any("FAIL" in line and "current repo" in line for line in lines))
        self.assertTrue(any("install git" in line for line in lines))
        self.assertTrue(any("python 3.9+" in line for line in lines))


class ListRuns(unittest.TestCase):
    def test_newest_first_with_runs_and_campaigns(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = pathlib.Path(tmp)
            for name, fname, data, mtime in (
                    ("a", "state.json", {"name": "old", "branch": "b1", "outcome": "done", "turn": 4}, 100),
                    ("campaign-b", "campaign.json", {"branch": "b2", "runs": [{"turns": 2}, {"turns": 3}]}, 300),
                    ("c", "state.json", {"branch": "b3", "turn": 1}, 200)):
                d = runs / name
                d.mkdir()
                (d / fname).write_text(json.dumps(data))
                os.utime(d / fname, (mtime, mtime))
            (runs / "empty").mkdir()
            with mock.patch.object(tandem, "RUNS", runs):
                rows = tandem.list_runs()
                self.assertEqual([r["id"] for r in rows], ["campaign-b", "c", "a"])
                self.assertEqual((rows[0]["outcome"], rows[0]["turns"]), ("campaign, 2 goals", 5))
                self.assertEqual((rows[2]["outcome"], rows[2]["turns"]), ("done", 3))
                self.assertEqual(rows[1]["outcome"], "running or interrupted")
                self.assertEqual(len(tandem.list_runs(1)), 1)


class Headless(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.base = pathlib.Path(temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.runs = self.base / "runs"
        for patch in (mock.patch.object(tandem, "RUNS", self.runs),
                      mock.patch.object(tandem, "AVAILABILITY", self.runs / ".availability.json"),
                      mock.patch.dict(os.environ, {
                          "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})):
            patch.start()
            self.addCleanup(patch.stop)
        tandem.git(self.repo, "init", "-q")
        tandem.git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")

    def invoke(self, *args, stdin="", tty=False):
        stdout, stderr, source = io.StringIO(), io.StringIO(), io.StringIO(stdin)
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr), \
                mock.patch("sys.stdin", source), mock.patch.object(source, "isatty", return_value=tty):
            try:
                tandem.main([*args, "--repo", str(self.repo)])
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_outcome_exit_codes(self):
        for outcome, expected in (("done", 0), ("error", 1), (None, 1), ("unexpected", 1),
                                  ("max_turns", 2), ("blocked", 3), ("stopped", 4), ("failed", 5),
                                  ("over_budget", 6)):
            with self.subTest(outcome=outcome):
                self.assertEqual(tandem.outcome_exit_code(outcome), expected)

    def test_json_real_loop_reports_completed_turns_and_tuning(self):
        with mock.patch.object(tandem, "call_agent", return_value=({"status": "done", "summary": "Checked café"}, .25)):
            code, out, err = self.invoke(
                "-p", "Review the repo", "--output-format", "json", "--name", "json-check",
                "--first", "codex", "--base", "HEAD", "--max-turns", "3", "--check", "git status --porcelain",
                "--claude-model", "model-c", "--codex-model", "model-o", "--codex-effort", "high",
                "--codex-role", "peer", "--codex-network", "--claude-budget", "2.5", "--timeout", "60")
        self.assertEqual(code, 0, err)
        result = json.loads(out)  # Extra log lines or JSON objects make this fail.
        self.assertEqual(set(result), {"run_id", "name", "repo", "branch", "base", "worktree", "commits", "diffstat",
                                      "outcome", "outcome_message", "turn_count", "cost_usd", "max_cost",
                                      "transcript", "turns"})
        self.assertIsNone(result["max_cost"])
        self.assertEqual(result["worktree"], str(self.runs / result["run_id"] / "work"))
        self.assertEqual((result["commits"], result["diffstat"]), (0, {"files": 0, "added": 0, "removed": 0}))
        self.assertIn("no changes over", err)
        self.assertEqual((result["name"], result["outcome"], result["turn_count"], result["cost_usd"]),
                         ("json-check", "done", 2, .5))
        self.assertEqual(result["repo"], str(self.repo))
        self.assertEqual(result["branch"], "tandem/" + result["run_id"])
        self.assertEqual(result["base"], tandem.git(self.repo, "rev-parse", "HEAD"))
        self.assertTrue(pathlib.Path(result["transcript"]).is_file())
        self.assertEqual([t["agent"] for t in result["turns"]], ["codex", "claude"])
        for turn in result["turns"]:
            self.assertEqual(set(turn), {"turn", "agent", "status", "commit", "seconds", "summary"})
            self.assertIsNone(turn["commit"])
            self.assertIsInstance(turn["seconds"], int)
            self.assertEqual(turn["summary"], "Checked café")
        cfg = tandem.load(self.runs / result["run_id"])["config"]
        for key, value in {"claude_model": "model-c", "codex_model": "model-o", "codex_effort": "high",
                           "codex_role": "peer", "codex_network": True, "claude_budget": 2.5,
                           "timeout": 60, "check": "git status --porcelain"}.items():
            self.assertEqual(cfg[key], value)
        self.assertIn("turn 1: Codex working...", err)
        self.assertIn("FINISHED", err)
        self.assertNotIn("\x1b", out + err)

    def test_stdin_and_goal_file_with_metadata(self):
        goal_file = self.base / "goal.md"
        goal_file.write_text("---\nname: from-file\nfirst: codex\ncheck: git status --porcelain\n---\nFile goal\n")
        with mock.patch.object(tandem, "call_agent", return_value=({"status": "continue"}, 0)):
            for args, stdin, goal, first in (
                    (("--name", "from-stdin"), "  Goal from stdin\n", "Goal from stdin", "claude"),
                    (("--goal-file", str(goal_file)), "", "File goal", "codex")):
                with self.subTest(args=args):
                    code, out, err = self.invoke("-p", *args, "--max-turns", "1", "--output-format", "json", stdin=stdin)
                    self.assertEqual(code, 2, err)
                    result = json.loads(out)
                    state = tandem.load(self.runs / result["run_id"])
                    self.assertEqual(state["goal"], goal)
                    self.assertEqual(result["turns"][0]["agent"], first)
                    if first == "codex":
                        self.assertEqual(state["config"]["check"], "git status --porcelain")

    def test_text_stream_and_quiet_final_result(self):
        with mock.patch.object(tandem, "call_agent", return_value=({"status": "continue"}, 0)):
            for quiet in (False, True):
                code, out, err = self.invoke("-p", "Work", "--name", str(quiet), "--max-turns", "1",
                                             *(["--quiet"] if quiet else []))
                self.assertEqual(code, 2, err)
                self.assertEqual(err, "")
                self.assertEqual("working..." in out, not quiet)
                self.assertIn("outcome: max_turns", out)
                self.assertIn("branch: tandem/", out)
                self.assertIn("transcript: ", out)
                self.assertNotIn("\x1b", out)

    def test_quiet_json_and_blocked_exit(self):
        with mock.patch.object(tandem, "call_agent", return_value=({"status": "blocked", "handoff": "Need answer"}, 0)):
            code, out, err = self.invoke("-p", "Work", "--quiet", "--output-format", "json")
        self.assertEqual(code, 3, err)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["outcome"], "blocked")
        self.assertEqual(json.loads(out)["turn_count"], 2)

    def test_both_agents_failed_exit(self):
        with mock.patch.object(tandem, "call_agent", side_effect=RuntimeError("bad fixture reply")):
            code, out, err = self.invoke("-p", "Work", "--output-format", "json")
        self.assertEqual(code, 5, err)
        result = json.loads(out)
        self.assertEqual((result["outcome"], result["turn_count"]), ("failed", 6))
        self.assertTrue(all(t["status"] == "error" for t in result["turns"]))

    def test_interrupt_saves_stopped_and_keeps_json_valid(self):
        with mock.patch.object(tandem, "call_agent", side_effect=[({"status": "continue"}, .1), KeyboardInterrupt()]):
            code, out, err = self.invoke("-p", "Work", "--output-format", "json")
        self.assertEqual(code, 4, err)
        result = json.loads(out)
        self.assertEqual((result["outcome"], result["turn_count"]), ("stopped", 1))
        self.assertIn("resume", result["outcome_message"])
        self.assertEqual(tandem.load(self.runs / result["run_id"])["outcome"], "stopped")

    def test_setup_errors_are_exit_one_and_json_without_tracebacks(self):
        for args, stdin, tty, message in (
                (("-p", ""), "", False, "goal is empty"),
                (("-p",), "  \n", False, "goal is empty"),
                (("-p",), "", True, "give a goal"),
                (("-p", "--goal", "wrong"), "", False, "instead of --goal"),
                (("-p", "text", "--goal-file", "x"), "", False, "give one goal source"),
                (("-p", "--goal-file", str(self.base / "missing")), "", False, "No such file"),
                (("-p", "text", "--base", "missing-ref"), "", False, "git rev-parse")):
            with self.subTest(args=args):
                code, out, err = self.invoke(*args, "--output-format", "json", "--quiet", stdin=stdin, tty=tty)
                self.assertEqual(code, 1, err)
                self.assertIn(message, err)
                self.assertNotIn("Traceback", err)
                result = json.loads(out)
                self.assertEqual(result["outcome"], "error")
                self.assertIn(message, result["outcome_message"])
                self.assertIsNone(result["run_id"])
                self.assertEqual(result["turn_count"], 0)

    def test_unexpected_error_still_prints_one_json_object(self):
        with mock.patch.object(tandem, "call_agent", side_effect=TypeError("a bug")):
            code, out, err = self.invoke("-p", "Work", "--output-format", "json")
        self.assertEqual(code, 1)
        self.assertIn("Traceback", err)
        result = json.loads(out)
        self.assertEqual((result["outcome"], result["outcome_message"]), ("error", "TypeError: a bug"))
        self.assertIsNotNone(result["run_id"])

    def test_start_still_uses_the_same_loop(self):
        with mock.patch.object(tandem, "call_agent", return_value=({"status": "continue"}, 0)):
            code, out, err = self.invoke("start", "--goal", "Legacy goal", "--max-turns", "1")
        self.assertEqual(code, 0, err)  # Legacy commands keep their successful exit status.
        self.assertIn("working...", out)
        state = tandem.load(next(self.runs.iterdir()))
        self.assertEqual((state["goal"], state["outcome"]), ("Legacy goal", "max_turns"))


class Handoff(unittest.TestCase):
    setUp = Headless.setUp  # same throwaway repo and RUNS patches
    invoke = Headless.invoke

    def run_with(self, status, edit=True):
        def agent(name, cfg, worktree, *args, **kwargs):
            if edit and name == "claude" and not (pathlib.Path(worktree) / "new.txt").exists():
                (pathlib.Path(worktree) / "new.txt").write_text("one\ntwo\n")
            return {"status": status, "summary": "s", "handoff": "Need answer"}, 0
        with mock.patch.object(tandem, "call_agent", side_effect=agent):
            code, out, err = self.invoke("-p", "Work", "--max-turns", "4")
        run_dir = next(self.runs.iterdir())
        return out, run_dir, tandem.load(run_dir)

    def command(self, *argv):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            tandem.main(list(argv))
        return out.getvalue()

    def test_summary_after_a_run_with_commits(self):
        out, run_dir, state = self.run_with("done")
        self.assertEqual(state["outcome"], "done")
        repo, work, branch = shlex.quote(str(self.repo)), shlex.quote(state["worktree"]), state["branch"]
        after = out[out.index("FINISHED"):]
        self.assertIn(f"{branch}: 1 commit over {state['base'][:12]}, 1 file changed, +2 -0", after)
        self.assertIn(f"worktree  {state['worktree']}\n", after)
        self.assertIn(f"git -C {repo} diff {state['base'][:12]}..{branch}\n", after)
        self.assertIn(f"git -C {repo} merge {branch}\n", after)
        self.assertIn(f"git -C {repo} worktree remove {work} && git -C {repo} branch -d {branch}\n", after)
        self.assertNotIn("resume", after.split("transcript")[0])
        # The commands really work: the merge brings the run's file into the owner's checkout.
        tandem.git(self.repo, "merge", "-q", branch)
        self.assertTrue((self.repo / "new.txt").is_file())

    def test_no_commits_says_no_changes_and_skips_the_commands(self):
        out, run_dir, state = self.run_with("done", edit=False)
        self.assertIn(f"{state['branch']}: no changes over", out)
        self.assertNotIn(" merge ", out)
        self.assertNotIn("worktree remove", out)

    def test_blocked_run_offers_resume_before_the_merge(self):
        out, run_dir, state = self.run_with("blocked")
        self.assertEqual(state["outcome"], "blocked")
        resume = out.index(f"resume    {tandem.prog_name()} resume {run_dir}")
        self.assertLess(resume, out.index(f"merge {state['branch']}"))

    def test_diff_command_with_bare_id_and_newest(self):
        _, run_dir, state = self.run_with("done")
        for argv in (("diff", run_dir.name), ("diff",)):
            with self.subTest(argv=argv):
                text = self.command(*argv)
                self.assertIn(f"{state['branch']}: 1 commit over", text)
                self.assertIn("new.txt", text)  # the --stat file list
                self.assertIn("1 file changed, 2 insertions(+)", text)

    def test_diff_with_no_runs(self):
        self.assertIn("no runs yet", self.command("diff"))

    def test_session_diff_without_a_run_says_so(self):
        session = tandem.Session(argparse.Namespace(repo=self.repo), tandem.Console(fancy=False))
        with mock.patch.dict(tandem.CURRENT, {}, clear=True), mock.patch.object(tandem, "emit") as emit:
            self.assertTrue(session.handle("diff", ""))
        emit.assert_called_once_with("no run yet; type a goal first")
        self.assertIn("/diff", tandem.SLASH_HELP)

    def test_removed_worktree_and_deleted_branch_do_not_crash(self):
        _, run_dir, state = self.run_with("done")
        tandem.git(self.repo, "worktree", "remove", "--force", state["worktree"])
        text = self.command("diff", run_dir.name)
        self.assertIn("1 commit over", text)  # read from the owner's repo, not the worktree
        self.assertIn("the worktree is gone", text)
        tandem.git(self.repo, "branch", "-D", state["branch"])
        text = self.command("diff", run_dir.name)
        self.assertIn(f"cannot read {state['branch']}", text)
        self.assertNotIn(" merge ", text)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            tandem.finish(run_dir, state, "done", "again")
        self.assertIn("the worktree is gone", out.getvalue())
        result = tandem.headless_result(run_dir, state)
        self.assertEqual((result["commits"], result["diffstat"]), (None, None))

    def test_unreadable_diffstat_is_unknown_not_zero(self):
        _, run_dir, state = self.run_with("done")
        with mock.patch.object(tandem, "diffstat", return_value=None):
            text = self.command("diff", run_dir.name)
        self.assertIn("1 commit over", text)
        self.assertIn("files changed unknown", text)
        self.assertNotIn("0 files changed", text)
        self.assertIn("cannot read the diffstat", text)
        self.assertIn(f" merge {state['branch']}", text)  # the commits are known, so the commands stay

    def test_campaign_summary_uses_its_own_base(self):
        base = tandem.git(self.repo, "rev-parse", "HEAD")
        camp_dir = self.runs / "campaign-x"
        work = camp_dir / "work"
        camp_dir.mkdir(parents=True)
        tandem.git(self.repo, "worktree", "add", "-q", "-b", "tandem/campaign-x", str(work), base)
        (work / "a.txt").write_text("a\n")
        tandem.git(work, "add", "-A")
        tandem.git(work, "commit", "-qm", "goal one")
        tandem.write_json(camp_dir / "campaign.json", {"repo": str(self.repo), "worktree": str(work),
                                                       "branch": "tandem/campaign-x", "base": base, "runs": []})
        text = self.command("diff", "campaign-x")
        self.assertIn("tandem/campaign-x: 1 commit over", text)
        self.assertIn("campaign resume", text)
        self.assertIn("merge tandem/campaign-x", text)

    def test_campaign_run_finish_prints_summary_after_finished(self):
        _, run_dir, state = self.run_with("done")
        state["campaign"] = str(self.runs / "campaign-x")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            tandem.finish(run_dir, state, "blocked", "Need owner input")
        text = out.getvalue()
        lines = text.splitlines()
        finished = next(i for i, line in enumerate(lines) if "FINISHED" in line)
        self.assertIn(f"{state['branch']}: 1 commit over", lines[finished + 1])
        self.assertIn(f"campaign resume {state['campaign']}", text)

    def test_campaign_exit_prints_summary_even_on_interrupt(self):
        _, run_dir, state = self.run_with("done")
        camp_dir = self.runs / "campaign-x"
        camp_dir.mkdir()
        tandem.write_json(camp_dir / "campaign.json", {
            **{key: state[key] for key in ("repo", "worktree", "branch", "base")},
            "runs": []})
        for error in (None, KeyboardInterrupt(), tandem.GitError("git failed")):
            with self.subTest(error=error):
                out = io.StringIO()
                with mock.patch("sys.stdout", out), \
                        mock.patch.object(tandem, "_campaign_loop", side_effect=error):
                    if error is None:
                        tandem.campaign_loop(camp_dir)
                    else:
                        with self.assertRaises(type(error)):
                            tandem.campaign_loop(camp_dir)
                self.assertFalse((camp_dir / ".lock").exists())
                self.assertIn(f"{state['branch']}: 1 commit over", out.getvalue())
                self.assertIn(f"campaign resume {camp_dir}", out.getvalue())


class Tuning(unittest.TestCase):
    setUp = Headless.setUp  # same throwaway repo and RUNS patches

    def run_dir_with(self, **cfg):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patch = mock.patch.dict(os.environ, {"HOME": home.name})
        patch.start()
        self.addCleanup(patch.stop)
        base = dict(timeout=5, claude_permission_mode="acceptEdits", codex_role="peer", check=None,
                    **{k: None for k in tandem.TUNABLE})
        with mock.patch.object(tandem, "log"):
            return tandem.create_run(self.runs / "r1", self.repo, "goal", "r1", dict(base, **cfg), 3, "codex")

    def test_tuning_applies_next_turn_and_is_logged(self):
        run_dir = self.run_dir_with()
        seen, lines = [], []

        def agent(name, cfg, *rest):
            if name == "codex":
                seen.append(cfg["codex_model"])
                if len(seen) == 1:
                    tandem.write_tuning(run_dir, {"codex_model": "gpt-y"})
            return {"status": "continue"}, 0

        with mock.patch.object(tandem, "call_agent", side_effect=agent), \
                mock.patch.object(tandem, "log", side_effect=lambda msg, *kind, **brief: lines.append(msg)):
            tandem.loop(run_dir)
        self.assertEqual(seen, [None, "gpt-y"])
        self.assertIn("config: codex_model default -> gpt-y", lines)
        self.assertEqual(tandem.load(run_dir)["config"]["codex_model"], "gpt-y")
        self.assertEqual(sum("config: " in l for l in lines), 1)

    def test_unset_and_broken_files(self):
        d = self.base / "t"
        d.mkdir()
        state = {"config": {"claude_model": "sonnet"}}
        tandem.write_tuning(d, {"claude_model": "opus", "bogus": "x"})
        self.assertEqual(tandem.apply_tuning(state, [d / "tuning.json"]), [("claude_model", "sonnet", "opus")])
        self.assertNotIn("bogus", json.loads((d / "tuning.json").read_text()))
        tandem.write_tuning(d, {"claude_model": None})
        self.assertEqual(tandem.apply_tuning(state, [d / "tuning.json"]), [("claude_model", "opus", None)])
        self.assertIsNone(state["config"]["claude_model"])
        (d / "tuning.json").write_text("{")
        self.assertEqual(tandem.apply_tuning(state, [d / "tuning.json", d / "missing.json"]), [])

    def test_later_file_wins(self):
        a, b = self.base / "a", self.base / "b"
        for d, model in ((a, "one"), (b, "two")):
            d.mkdir()
            tandem.write_tuning(d, {"codex_model": model})
        state = {"config": {}}
        tandem.apply_tuning(state, [a / "tuning.json", b / "tuning.json"])
        self.assertEqual(state["config"]["codex_model"], "two")

    def test_campaign_runs_read_the_campaign_file(self):
        self.run_dir_with()  # patches HOME
        camp_dir = self.runs / "campaign-x"
        camp_dir.mkdir(parents=True)
        worktree = self.base / "camp-work"
        tandem.git(self.repo, "worktree", "add", "-q", "-b", "camp", str(worktree), "HEAD")
        cfg = {"timeout": 5, "claude_permission_mode": "acceptEdits", "codex_role": "peer", "check": None,
               **{k: None for k in tandem.TUNABLE}}
        tandem.write_json(camp_dir / "campaign.json", {
            "id": "x", "config": cfg, "max_turns": 2, "push": False, "runs": [], "blockers": [], "no_progress": 0,
            "cost_usd": 0.0, "current": None, "repo": str(self.repo), "worktree": str(worktree), "branch": "camp",
            "queue": [{"goal": "g", "name": "first", "first": "claude"}]})
        tandem.write_tuning(camp_dir, {"codex_model": "gpt-z"})

        def agent(name, cfg, *rest):
            (camp_dir / "STOP").write_text("")  # one turn is enough to inspect the run
            return {"status": "continue"}, 0

        with mock.patch.object(tandem, "call_agent", side_effect=agent), mock.patch.object(tandem, "log"):
            tandem._campaign_loop(camp_dir)
        camp = json.loads((camp_dir / "campaign.json").read_text())
        state = tandem.load(pathlib.Path(camp["current"]))
        self.assertEqual(state["tuning_files"], [str(camp_dir / "tuning.json")])
        self.assertEqual(state["config"]["codex_model"], "gpt-z")
        self.assertEqual(camp["config"]["codex_model"], "gpt-z")


class Budget(unittest.TestCase):
    setUp = Headless.setUp
    invoke = Headless.invoke
    run_dir_with = Tuning.run_dir_with

    def agent(self, cost, seen, on_call=None):
        def call(name, cfg, *rest):
            seen.append(cfg.get("claude_budget"))
            if on_call:
                on_call(len(seen), rest[-1])  # rest[-1] is call_agent's run_dir
            return {"status": "continue"}, cost
        return call

    def test_headless_stops_before_the_turn_past_the_cap(self):
        seen = []
        with mock.patch.object(tandem, "call_agent", side_effect=self.agent(.4, seen)):
            code, out, err = self.invoke("-p", "Work", "--output-format", "json", "--max-cost", "1")
        self.assertEqual(code, 6, err)
        result = json.loads(out)
        # .4, .8 and 1.2 spent: the third turn ran to the end, the fourth never started.
        self.assertEqual((result["outcome"], result["turn_count"], result["max_cost"]), ("over_budget", 3, 1.0))
        self.assertTrue(all(t["status"] == "continue" for t in result["turns"]))
        self.assertIn("spent $1.20 of the $1.00 cap", result["outcome_message"])
        self.assertIn(f"resume {self.runs / result['run_id']} --max-cost 2", result["outcome_message"])
        # Each Claude turn may spend at most what is left of the cap.
        self.assertEqual(seen, [1.0, .6, .2])

    def test_one_expensive_turn_finishes_then_the_run_stops(self):
        seen = []
        with mock.patch.object(tandem, "call_agent", side_effect=self.agent(5, seen)):
            code, out, err = self.invoke("-p", "Work", "--output-format", "json", "--max-cost", "1")
        self.assertEqual(code, 6, err)
        self.assertEqual(json.loads(out)["turn_count"], 1)

    def test_turn_budget_is_the_smaller_of_cap_left_and_claude_budget(self):
        seen = []
        with mock.patch.object(tandem, "call_agent", side_effect=self.agent(.4, seen)):
            self.invoke("-p", "Work", "--quiet", "--max-cost", "1", "--claude-budget", ".5")
        self.assertEqual(seen, [.5, .5, .2])
        self.assertEqual(tandem.turn_budget({"claude_budget": 3}, 0), 3)
        self.assertIsNone(tandem.turn_budget({}, 0))

    def test_cost_of_a_turn_that_errored_still_counts(self):
        err = RuntimeError("error_max_budget_usd: over")
        err.cost = 2.0
        with mock.patch.object(tandem, "call_agent", side_effect=err):
            code, out, _ = self.invoke("-p", "Work", "--output-format", "json", "--max-cost", "1")
        self.assertEqual((code, json.loads(out)["turn_count"], json.loads(out)["cost_usd"]), (6, 1, 2.0))

    def test_zero_negative_or_bad_cap_is_a_usage_error(self):
        for value in ("0", "-1", "abc", "nan", "inf"):
            with self.subTest(value=value):
                code, _, err = self.invoke("-p", "Work", "--max-cost", value)
                self.assertEqual(code, 1)
                self.assertIn("--max-cost", err)
        for argv in (["resume", "r1", "--max-cost", "0"], ["campaign", "resume", "c", "--max-cost", "-2"],
                     ["campaign", "start", "--max-cost", "0"], ["start", "--goal", "g", "--max-cost", "0"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as exit, \
                    mock.patch("sys.stderr", io.StringIO()):
                tandem.build_parser().parse_args(argv)
            self.assertEqual(exit.exception.code, 1)

    def test_tuning_raises_the_cap_from_the_next_turn_and_null_removes_it(self):
        cfg = tandem.load(self.run_dir_with(max_cost=1.0))["config"]
        for i, (new, outcome) in enumerate(((10, "max_turns"), (None, "max_turns"), (.5, "over_budget"))):
            with self.subTest(new=new), mock.patch.object(tandem, "log"):
                run_dir = tandem.create_run(self.runs / f"t{i}", self.repo, "goal", "t", dict(cfg), 3, "codex")
                seen = []
                on_call = lambda n, rd: n == 1 and tandem.write_tuning(rd, {"max_cost": new})
                with mock.patch.object(tandem, "call_agent", side_effect=self.agent(.6, seen, on_call)), \
                        mock.patch.object(tandem, "log"):
                    tandem.loop(run_dir)
                state = tandem.load(run_dir)
                self.assertEqual(state["outcome"], outcome)
                self.assertEqual(state["config"]["max_cost"], new)
                self.assertEqual(len(state["history"]), 3 if outcome == "max_turns" else 1)

    def test_resume_raises_the_cap_over_an_older_tuning_value(self):
        run_dir = self.run_dir_with(max_cost=1.0)
        tandem.write_tuning(run_dir, {"max_cost": 1.0})
        with mock.patch.object(tandem, "call_agent", side_effect=self.agent(.6, [])), mock.patch.object(tandem, "log"):
            tandem.loop(run_dir)
            self.assertEqual(tandem.load(run_dir)["outcome"], "over_budget")
            tandem.cmd_resume(argparse.Namespace(run=str(run_dir), more_turns=None, max_cost=10.0))
        state = tandem.load(run_dir)
        self.assertEqual((state["outcome"], state["config"]["max_cost"], len(state["history"])), ("max_turns", 10.0, 3))

    def test_session_budget_changes_apply_from_next_turn(self):
        run_dir = self.run_dir_with(max_cost=1.0)
        session = tandem.Session.__new__(tandem.Session)
        session.args = argparse.Namespace(repo=self.repo)
        seen = []

        def change(n, rd):
            session.handle("budget", "10" if n == 1 else "off")
            # The session writes tuning only; the loop still owns the saved config.
            self.assertEqual(tandem.load(rd)["config"]["max_cost"], 1.0 if n == 1 else 10.0)

        with mock.patch.dict(tandem.CURRENT, target=run_dir), mock.patch.object(tandem, "emit"), \
                mock.patch.object(tandem, "log"), \
                mock.patch.object(tandem, "call_agent", side_effect=self.agent(.6, seen, lambda n, rd: change(n, rd) if n < 3 else None)):
            tandem.loop(run_dir)
        self.assertEqual(seen, [1.0, 9.4, None])
        self.assertIsNone(tandem.load(run_dir)["config"]["max_cost"])

    def test_session_budget_reports_pending_cap_and_rejects_invalid_amounts(self):
        run_dir = self.run_dir_with(max_cost=1.0)
        session = tandem.Session.__new__(tandem.Session)
        with mock.patch.object(tandem, "emit") as emit:
            session.budget(run_dir, "10")
            session.budget(run_dir, "")
            emit.assert_called_with("spent $0.00; $10.00 cap (reported cost only)")
            for value in ("0", "-1", "nan", "bad"):
                session.budget(run_dir, value)
                self.assertIn("usage: /budget", emit.call_args[0][0])
            self.assertEqual(tandem.read_tuning(run_dir / "tuning.json"), {"max_cost": 10.0})
            session.budget(run_dir, "off")
            session.budget(run_dir, "")
            emit.assert_called_with("spent $0.00; no cap (reported cost only)")

    def test_session_budget_campaign_total_excludes_already_counted_last_run(self):
        run_dir = self.run_dir_with(max_cost=1)
        state = tandem.load(run_dir)
        state["cost_usd"] = .75
        tandem.save(run_dir, state)
        camp_dir = self.runs / "camp"
        camp_dir.mkdir()
        camp = {"config": {"max_cost": 5}, "cost_usd": 2,
                "current": str(run_dir), "runs": [{"dir": str(run_dir)}]}
        session = tandem.Session.__new__(tandem.Session)
        with mock.patch.object(tandem, "emit") as emit:
            tandem.write_json(camp_dir / "campaign.json", camp)
            session.budget(camp_dir, "")
            emit.assert_called_with("spent $2.75; $5.00 cap (reported cost only)")
            camp["current"] = None
            tandem.write_json(camp_dir / "campaign.json", camp)
            session.budget(camp_dir, "")
            emit.assert_called_with("spent $2.00; $5.00 cap (reported cost only)")

    def test_eighty_percent_is_said_once(self):
        run_dir = self.run_dir_with(max_cost=1.0)
        lines = []
        with mock.patch.object(tandem, "call_agent", side_effect=self.agent(.3, [])), \
                mock.patch.object(tandem, "log", side_effect=lambda msg, *kind, **brief: lines.append(msg)):
            tandem.loop(run_dir)
        self.assertEqual([l for l in lines if "cap" in l and "spent" in l][:1], ["spent $0.90 of the $1.00 cap"])
        self.assertEqual(sum(l.startswith("spent $") for l in lines), 1)

    def test_campaign_stops_on_the_sum_of_its_runs_and_resume_raises_it(self):
        self.run_dir_with()  # patches HOME
        camp_dir = self.runs / "campaign-x"
        camp_dir.mkdir(parents=True)
        worktree = self.base / "camp-work"
        tandem.git(self.repo, "worktree", "add", "-q", "-b", "camp", str(worktree), "HEAD")
        cfg = {"timeout": 5, "claude_permission_mode": "acceptEdits", "codex_role": "peer", "check": None,
               "max_cost": 1.0, **{k: None for k in tandem.TUNABLE}}
        tandem.write_json(camp_dir / "campaign.json", {
            "id": "x", "config": cfg, "max_turns": 3, "push": False, "runs": [], "blockers": [], "no_progress": 0,
            "cost_usd": 0.0, "current": None, "repo": str(self.repo), "worktree": str(worktree), "branch": "camp",
            "queue": [{"goal": "g", "name": n, "first": "claude"} for n in ("one", "two", "three")]})
        seen = []
        with mock.patch.object(tandem, "call_agent", side_effect=self.agent(.3, seen)), \
                mock.patch.object(tandem, "log"):
            tandem._campaign_loop(camp_dir)
            camp = json.loads((camp_dir / "campaign.json").read_text())
            # Goal one runs out of turns at .9; goal two takes one turn to 1.2 and stops there.
            self.assertEqual(camp["outcome"], "over_budget")
            self.assertEqual([r["name"] for r in camp["runs"]], ["one"])
            state = tandem.load(pathlib.Path(camp["current"]))
            self.assertEqual((state["name"], state["outcome"], len(state["history"])), ("two", "over_budget", 1))
            self.assertIn(f"campaign resume {camp_dir} --max-cost 2", state["outcome_message"])
            self.assertEqual([round(b, 2) for b in seen], [1.0, .7, .4, .1])
            tandem.prepare_campaign_resume(argparse.Namespace(
                dir=str(camp_dir), push=None, codex_effort=None, codex_role=None, seed_review=None, max_cost=2.0))
            self.assertNotIn("outcome", json.loads((camp_dir / "campaign.json").read_text()))
            tandem._campaign_loop(camp_dir)
        camp = json.loads((camp_dir / "campaign.json").read_text())
        # Goal two finishes its turns at 1.8; goal three's first turn reaches 2.1, past the raised cap.
        self.assertEqual([r["name"] for r in camp["runs"]], ["one", "two"])
        self.assertEqual(camp["outcome"], "over_budget")
        self.assertAlmostEqual(camp["cost_usd"], 1.8)
        state = tandem.load(pathlib.Path(camp["current"]))
        self.assertEqual((state["name"], len(state["history"])), ("three", 1))
        self.assertAlmostEqual(tandem.budget_spent(state), 2.1)

    def test_claude_cli_gets_what_is_left_of_the_cap(self):
        reply = json.dumps({"type": "result", "structured_output": {"status": "continue"}, "total_cost_usd": .4})
        done = subprocess.CompletedProcess([], 0, reply + "\n", "")
        with mock.patch.object(tandem, "run_child", return_value=done) as run:
            code, out, err = self.invoke("-p", "Work", "--output-format", "json", "--max-turns", "1",
                                         "--max-cost", "1.5", "--claude-budget", "2")
        self.assertEqual((code, json.loads(out)["cost_usd"]), (2, .4), err)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "1.5")

    def test_spent_cap_wins_over_the_turn_limit_and_skips_availability_waits(self):
        run_dir = self.run_dir_with(max_cost=1.0)
        state = tandem.load(run_dir)
        state.update(cost_usd=1.0, turn=state["max_turns"] + 1)
        tandem.save(run_dir, state)
        later = datetime.datetime.now() + datetime.timedelta(hours=3)
        with mock.patch.object(tandem, "unavailable_until", return_value=later), \
                mock.patch.object(tandem, "wait_until") as wait, mock.patch.object(tandem, "log"):
            tandem.loop(run_dir)
        wait.assert_not_called()
        self.assertEqual(tandem.load(run_dir)["outcome"], "over_budget")

    def test_transient_retries_keep_the_cost_of_failed_attempts(self):
        calls = []

        def runner(cfg, *rest):
            calls.append(cfg.get("claude_budget"))
            if len(calls) <= 2:
                err = RuntimeError("API Error: 529 overloaded")
                err.cost = .3
                raise err
            return {"status": "continue"}, .2, {}

        with mock.patch.dict(tandem.RUNNERS, claude=runner), mock.patch.object(tandem, "log"), \
                mock.patch.object(tandem, "wait_until", return_value=True):
            result, cost, _ = tandem.call_agent("claude", {"claude_budget": 1.0}, None, "p", None, None, "status", False)
            self.assertAlmostEqual(cost, .8)
            self.assertEqual(calls, [1.0, .7, .4])
            calls.clear()
            failing = lambda *a: (_ for _ in ()).throw(type("E", (RuntimeError,), {"cost": .3})("x 503"))
            with mock.patch.dict(tandem.RUNNERS, claude=failing), self.assertRaises(RuntimeError) as caught:
                tandem.call_agent("claude", {}, None, "p", None, None, "status", False)
        self.assertAlmostEqual(caught.exception.cost, .3 * (tandem.MAX_TRANSIENT_RETRIES + 1))

    def test_planner_gets_what_is_left_and_its_failed_cost_counts(self):
        camp_dir = self.runs / "camp-plan"
        camp_dir.mkdir(parents=True)
        camp = {"config": {"max_cost": 1.0, "claude_budget": None}, "cost_usd": .7, "runs": [], "blockers": [],
                "next_planner": "claude", "worktree": str(self.repo)}
        seen = []

        def planner(agent, cfg, *rest):
            seen.append(cfg["claude_budget"])
            err = RuntimeError("planner crashed")
            err.cost = .1
            raise err

        with mock.patch.object(tandem, "call_agent", side_effect=planner), mock.patch.object(tandem, "log"), \
                mock.patch.object(tandem, "unavailable_until", return_value=None):
            self.assertIsNone(tandem.plan_next(camp_dir, camp))
        self.assertEqual([round(b, 2) for b in seen], [.3, .2])
        self.assertAlmostEqual(camp["cost_usd"], .9)

    def test_campaign_planner_exhausts_cap_without_fallback_or_wait(self):
        for result in (None, {"finished": True, "reason": "done"},
                       {"finished": False, "goal": "Next goal", "name": "next"}):
            with self.subTest(result=result):
                camp_dir = self.runs / "camp-plan-cap"
                camp_dir.mkdir(parents=True, exist_ok=True)
                tandem.write_json(camp_dir / "campaign.json", {
                    "config": {"max_cost": 1.0}, "cost_usd": .7, "runs": [], "blockers": [],
                    "queue": [], "current": None, "next_planner": "claude", "worktree": str(self.repo)})

                def planner(*args):
                    if result is None:
                        err = RuntimeError("planner crashed")
                        err.cost = .4
                        raise err
                    return result, .4

                with mock.patch.object(tandem, "call_agent", side_effect=planner) as call, \
                        mock.patch.object(tandem, "log"), \
                        mock.patch.object(tandem, "unavailable_until", return_value=None), \
                        mock.patch.object(tandem, "wait_until") as wait, \
                        mock.patch.object(tandem, "create_run") as create:
                    tandem._campaign_loop(camp_dir)
                camp = json.loads((camp_dir / "campaign.json").read_text())
                # A planner that says the plan is finished ends the campaign as finished, cap or not.
                finished = bool(result and result["finished"])
                self.assertEqual(camp.get("outcome"), "finished" if finished else "over_budget")
                self.assertAlmostEqual(camp["cost_usd"], 1.1)
                self.assertEqual(call.call_count, 1)
                wait.assert_not_called()
                create.assert_not_called()
                self.assertEqual([item["goal"] for item in camp["queue"]],
                                 ["Next goal"] if result and not result["finished"] else [])

    def test_bar_shows_spent_over_cap(self):
        state = {"turn": 2, "max_turns": 4, "next": "claude", "cost_usd": 2.83, "history": [],
                 "failures": {"claude": 0, "codex": 0}, "config": {"max_cost": 5}}
        self.assertIn("$2.83/$5.00", tandem.status_bar(state, 0, 200, False, progress={})[1])
        self.assertEqual(tandem.cost_text(dict(state, prior_cost=1)), "$3.83/$5.00")
        self.assertEqual(tandem.cost_text(dict(state, config={})), "$2.83")


class Notify(unittest.TestCase):
    setUp = Headless.setUp
    invoke = Headless.invoke
    run_dir_with = Tuning.run_dir_with
    run_with = Handoff.run_with

    def home(self, config=None):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patch = mock.patch.dict(os.environ, {"HOME": home.name})
        patch.start()
        self.addCleanup(patch.stop)
        if config is not None:
            tandem.config_path().parent.mkdir(parents=True)
            tandem.config_path().write_text(json.dumps(config))

    def real(self, title, body, platform, which, run=None, term=""):
        """The real notify() with the platform, PATH and subprocess.run stubbed; returns (run mock, alerts)."""
        alerts = []
        console = mock.Mock(alert=alerts.append)
        run = run or mock.Mock()
        with mock.patch.object(tandem.sys, "platform", platform), \
                mock.patch.object(tandem.shutil, "which", side_effect=lambda name: which.get(name)), \
                mock.patch.object(tandem.subprocess, "run", run), \
                mock.patch.object(tandem, "CONSOLE", console), \
                mock.patch.dict(os.environ, {"TERM_PROGRAM": term}):
            REAL_NOTIFY(title, body)
        return run, alerts

    def test_macos_passes_title_and_body_as_arguments(self):
        body = 'say "hi" \\ and \'bye\''
        run, alerts = self.real("tandem", body, "darwin", {"osascript": "/usr/bin/osascript"})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[-2:], ["tandem", body])
        script = [cmd[i + 1] for i, a in enumerate(cmd[:-2]) if a == "-e"]
        self.assertEqual(script, tandem.NOTIFY_SCRIPT)
        self.assertFalse(any("hi" in line or "tandem" in line for line in script))
        self.assertEqual(run.call_args[1]["timeout"], tandem.NOTIFY_TIMEOUT)
        self.assertEqual(alerts, ["\a"])

    def test_notify_send_only_when_present(self):
        run, alerts = self.real("tandem", "r: done", "linux", {})
        run.assert_not_called()
        self.assertEqual(alerts, ["\a"])  # the bell alone is fine
        run, _ = self.real("tandem", "r: done", "linux", {"notify-send": "/usr/bin/notify-send"})
        self.assertEqual(run.call_args[0][0], ["notify-send", "--", "tandem", "r: done"])

    def test_failing_or_hung_notifier_does_not_propagate(self):
        for error in (OSError("gone"), subprocess.TimeoutExpired("osascript", 2), ValueError("odd")):
            with self.subTest(error=error):
                _, alerts = self.real("tandem", "x", "darwin", {"osascript": "/x"}, mock.Mock(side_effect=error))
                self.assertEqual(alerts, ["\a"])
        with mock.patch.object(tandem, "CONSOLE", mock.Mock(alert=mock.Mock(side_effect=OSError))), \
                mock.patch.object(tandem, "notifier", return_value=None):
            REAL_NOTIFY("tandem", "x")  # a terminal gone away is no reason to stop the loop

    def test_iterm_gets_osc9_then_the_bell_and_control_characters_go(self):
        _, alerts = self.real("tandem", "a\x1b]evil\nb", "linux", {}, term="iTerm.app")
        self.assertEqual(alerts, ["\x1b]9;tandem: a ]evil b\a\a"])

    def test_outside_the_session_the_bell_goes_to_a_terminal_only(self):
        out = io.StringIO()
        with mock.patch.object(tandem, "notifier", return_value=None), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", io.StringIO()), mock.patch.dict(os.environ, {"TERM_PROGRAM": ""}):
            REAL_NOTIFY("tandem", "x")
            self.assertEqual(out.getvalue(), "")
            out.isatty = lambda: True
            REAL_NOTIFY("tandem", "x")
        self.assertEqual(out.getvalue(), "\a")

    def test_start_notifies_once_with_outcome_turns_and_cost(self):
        self.home()
        with mock.patch.object(tandem, "call_agent", return_value=({"status": "continue"}, .5)), \
                mock.patch.object(tandem, "notify") as notify:
            code, out, err = self.invoke("start", "--goal", "g", "--name", "bar", "--max-turns", "2")
        self.assertEqual(code, 0, err)
        notify.assert_called_once_with("tandem", "bar: out of turns in 2 turns, $1.00")
        self.assertFalse(tandem.NOTIFY["on"])  # main() leaves no switch on for whatever runs next

    def test_blocked_names_who_needs_the_owner(self):
        self.home()
        reply = {"status": "blocked", "summary": "s", "handoff": "Need answer"}
        with mock.patch.object(tandem, "notify") as notify, \
                mock.patch.object(tandem, "call_agent", return_value=(reply, 0)):
            self.invoke("-p", "Work", "--notify", "--name", "nb")
        notify.assert_called_once_with("tandem", "nb: blocked, Codex needs you")

    def test_headless_only_with_notify_and_silenced_by_flag_or_config(self):
        self.home()
        cases = ((("-p", "g"), 0), (("-p", "g", "--notify"), 1), (("start", "--goal", "g", "--no-notify"), 0))
        for n, (args, calls) in enumerate(cases):
            with self.subTest(args=args), mock.patch.object(tandem, "notify") as notify, \
                    mock.patch.object(tandem, "call_agent", return_value=({"status": "continue"}, 0)):
                self.invoke(*args, "--max-turns", "1", "--name", f"case{n}")
                self.assertEqual(notify.call_count, calls)
        self.home({"notify": False})
        with mock.patch.object(tandem, "notify") as notify, \
                mock.patch.object(tandem, "call_agent", return_value=({"status": "continue"}, 0)):
            self.invoke("start", "--goal", "g", "--max-turns", "1", "--name", "saved-off")
        notify.assert_not_called()

    def test_entry_points_that_notify(self):
        self.home()
        quiet, off = argparse.Namespace(no_notify=False), argparse.Namespace(no_notify=True)
        for fn in (tandem.cmd_interactive, tandem.cmd_start, tandem.cmd_resume,
                   tandem.cmd_campaign_start, tandem.cmd_campaign_resume):
            with self.subTest(fn=fn.__name__):
                self.assertTrue(tandem.wants_notify(fn, quiet))
                self.assertFalse(tandem.wants_notify(fn, off))
        self.assertFalse(tandem.wants_notify(tandem.cmd_status, quiet))
        parser = tandem.build_parser()
        for argv in (["--no-notify"], ["resume", "r", "--no-notify"], ["campaign", "start", "--no-notify"],
                     ["campaign", "resume", "d", "--no-notify"]):
            self.assertTrue(parser.parse_args(argv).no_notify, argv)
        self.home({"notify": False})
        self.assertFalse(tandem.wants_notify(tandem.cmd_interactive, quiet))
        self.assertTrue(tandem.wants_notify(tandem.cmd_headless, argparse.Namespace(notify=True)))

    def test_session_run_end_notifies_once(self):
        run_dir = self.run_dir_with()
        console = tandem.Console(fancy=False, styled=True)
        console.write = lambda text: None
        with mock.patch.object(tandem, "CONSOLE", console), mock.patch.object(tandem, "NOTIFY", {"on": True}), \
                mock.patch.object(tandem, "notify") as notify, \
                mock.patch.object(tandem, "call_agent", return_value=({"status": "continue"}, .25)):
            tandem.loop(run_dir)
        notify.assert_called_once_with("tandem", "r1: out of turns in 3 turns, $0.75")

    def test_session_command_silence_lasts_only_for_its_worker(self):
        target = self.runs / "session-target"
        target.mkdir(parents=True)
        session = tandem.Session(argparse.Namespace(repo="."), tandem.Console(fancy=False))
        seen = []

        def check_switch(*args):
            seen.append(tandem.NOTIFY["on"])

        with mock.patch.object(tandem, "NOTIFY", {"on": True}), mock.patch.object(tandem, "emit"), \
                mock.patch.object(tandem, "prepare_campaign_start", return_value=target), \
                mock.patch.object(tandem, "prepare_campaign_resume", return_value=target), \
                mock.patch.object(tandem, "campaign_loop", side_effect=check_switch), \
                mock.patch.object(tandem, "cmd_resume", side_effect=check_switch):
            for command in ("start --no-notify", "resume x --no-notify", "start"):
                session.campaign(command)
                session.thread.join(timeout=2)
                self.assertFalse(session.busy())
                self.assertTrue(tandem.NOTIFY["on"])
            for rest in ("2 --no-notify", "2"):
                session.resume(target, rest)
                session.thread.join(timeout=2)
                self.assertFalse(session.busy())
                self.assertTrue(tandem.NOTIFY["on"])
            session.launch("run", lambda: (_ for _ in ()).throw(ValueError("failed")), no_notify=True)
            session.thread.join(timeout=2)
            self.assertTrue(tandem.NOTIFY["on"])
        self.assertEqual(seen, [False, False, True, False, True])

    def test_campaign_end_notifies_once_and_its_runs_do_not(self):
        out, run_dir, state = self.run_with("done")
        camp_dir = self.runs / "campaign-x"
        camp_dir.mkdir()
        tandem.write_json(camp_dir / "campaign.json", {
            **{key: state[key] for key in ("repo", "worktree", "branch", "base")},
            "runs": [{"name": "a", "turns": 3}, {"name": "b", "turns": 1}], "outcome": "finished", "cost_usd": 2.5})
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.object(tandem, "NOTIFY", {"on": True}), \
                mock.patch.object(tandem, "notify") as notify:
            with mock.patch.object(tandem, "_campaign_loop"):
                tandem.campaign_loop(camp_dir)
            notify.assert_called_once_with("tandem", "campaign campaign-x: finished after 2 goals, 4 turns, $2.50")
            notify.reset_mock()
            with mock.patch.object(tandem, "_campaign_loop", side_effect=KeyboardInterrupt), \
                    self.assertRaises(KeyboardInterrupt):
                tandem.campaign_loop(camp_dir)
            notify.assert_not_called()  # the owner pressed Ctrl+C; they are looking already
            with mock.patch.object(tandem, "_campaign_loop", side_effect=RuntimeError("disk full")), \
                    self.assertRaises(RuntimeError):
                tandem.campaign_loop(camp_dir)
            notify.assert_called_once_with(
                "tandem", "campaign campaign-x: crashed after 2 goals, 4 turns, $2.50: disk full")
            notify.reset_mock()
            tandem.finish(run_dir, dict(state, campaign=str(camp_dir)), "done", "ok")
            notify.assert_not_called()

    def test_stopped_campaign_counts_its_current_run(self):
        run_dir = self.run_dir_with()
        state = tandem.load(run_dir)
        tandem.save(run_dir, dict(state, turn=3, cost_usd=1.25))
        camp_dir = self.runs / "campaign-y"
        camp_dir.mkdir()
        (camp_dir / "STOP").write_text("")
        camp = {"runs": [{"name": "a", "turns": 2}], "cost_usd": 1.0, "current": str(run_dir)}
        self.assertEqual(tandem.campaign_notice(camp_dir, camp),
                         "campaign campaign-y: stopped after 1 goal, 4 turns, $2.25")
        camp["outcome"] = "over_budget"
        self.assertTrue(tandem.campaign_notice(camp_dir, camp).startswith("campaign campaign-y: over budget"))

    def test_run_crash_notifies_outside_a_campaign_only(self):
        run_dir = self.run_dir_with()
        with mock.patch.object(tandem, "NOTIFY", {"on": True}), mock.patch.object(tandem, "log"), \
                mock.patch.object(tandem, "notify") as notify, \
                mock.patch.object(tandem, "_loop", side_effect=tandem.GitError("commit failed")):
            with self.assertRaises(tandem.GitError):
                tandem.loop(run_dir)
            notify.assert_called_once_with("tandem", "r1: crashed, commit failed")
            notify.reset_mock()
            tandem.save(run_dir, dict(tandem.load(run_dir), campaign="c"))
            with self.assertRaises(tandem.GitError):
                tandem.loop(run_dir)
            notify.assert_not_called()  # the campaign's own end says it

    def test_both_away_wait_notifies_once(self):
        run_dir = self.run_dir_with()
        state = tandem.load(run_dir)
        state["stop_file"] = str(run_dir / "STOP")
        tandem.save(run_dir, state)
        later = datetime.datetime.now() + datetime.timedelta(hours=2)
        stop = lambda when, stop_file: pathlib.Path(stop_file).write_text("")
        with mock.patch.object(tandem, "unavailable_until", return_value=later), \
                mock.patch.object(tandem, "wait_until", side_effect=stop) as wait, \
                mock.patch.object(tandem, "NOTIFY", {"on": True}), mock.patch.object(tandem, "log"), \
                mock.patch.object(tandem, "notify") as notify:
            tandem.loop(run_dir)
        wait.assert_called_once()
        bodies = [c[0][1] for c in notify.call_args_list]
        self.assertEqual(bodies, [f"r1: both agents at usage limits, waiting until {later:%H:%M}",
                                  "r1: stopped in 0 turns, $0.00"])

    def test_doctor_names_the_notifier(self):
        self.home()
        for platform, which, term, expected in (
                ("darwin", {"osascript": "/x"}, "iTerm.app", "osascript + OSC 9 + bell"),
                ("linux", {"notify-send": "/x"}, "", "notify-send + bell"),
                ("linux", {}, "", "bell only")):
            with self.subTest(expected=expected), mock.patch.object(tandem.sys, "platform", platform), \
                    mock.patch.object(tandem.shutil, "which", side_effect=lambda name: which.get(name)), \
                    mock.patch.dict(os.environ, {"TERM_PROGRAM": term}):
                self.assertEqual(tandem.notifier_report(), expected)


ANSI =re.compile(r"\x1b\[[0-9;]*m")


def plain(text):
    return ANSI.sub("", text)


class Cards(unittest.TestCase):
    ENTRY = {"turn": 3, "agent": "claude", "status": "continue", "seconds": 134, "commit": "abc1234",
             "summary": "Moved the parser into its own module.", "handoff": "review",
             "could_not_do": "run the integration suite", "checks": "unit tests pass", "error": ""}

    def test_card_has_every_field_and_skips_empty(self):
        card = plain(tandem.turn_card(self.ENTRY, True, 80))
        for text in ("Moved the parser", "\u2192 Codex: review", "\u2717 could not:", "\u2713 checks:", "2m14s", "abc1234"):
            self.assertIn(text, card)
        bare = plain(tandem.turn_card(dict(self.ENTRY, could_not_do="", checks="", handoff=""), True, 80))
        self.assertNotIn("could not", bare)
        self.assertNotIn("checks", bare)

    def test_card_fits_a_narrow_terminal(self):
        entry = dict(self.ENTRY, summary="word " * 40 + "x" * 90 + " tail")
        for fancy in (True, False):
            lines = plain(tandem.turn_card(entry, fancy, 24)).split("\n")
            self.assertTrue(all(len(line) == 24 for line in lines), lines)
            self.assertEqual(lines[0][0], "\u256d" if fancy else "+")
            self.assertEqual(lines[-1][0], "\u2570" if fancy else "+")
            self.assertIn("\u2026", "\n".join(lines))
            summary = [line for line in lines if "word" in line or "xxx" in line]
            self.assertLessEqual(len(summary), 3)

    def test_card_stays_closed_at_the_minimum_width(self):
        lines = plain(tandem.turn_card(self.ENTRY, True, 20)).split("\n")
        self.assertTrue(all(len(line) == 20 for line in lines), lines)

    def test_card_width_is_clamped(self):
        for columns, expected in ((10, 20), (30, 30), (300, 100)):
            with mock.patch.object(tandem.shutil, "get_terminal_size", return_value=os.terminal_size((columns, 24))):
                self.assertEqual(tandem.card_width(), expected)

    def test_status_colors(self):
        for status, code in (("continue", "36"), ("done", "32"), ("blocked", "33"), ("error", "31")):
            card = tandem.turn_card(dict(self.ENTRY, status=status), True, 60)
            self.assertIn(f"\x1b[{code}m{status}", card)

    def test_plain_card_has_no_escape_codes(self):
        card = tandem.turn_card(self.ENTRY, False, 60)
        self.assertNotIn("\x1b", card)
        self.assertTrue(card.startswith("+"))

    def test_error_turn_shows_the_error_in_red(self):
        entry = dict(self.ENTRY, status="error", error="codex exited 1: boom", summary="")
        card = tandem.turn_card(entry, True, 60)
        self.assertIn("\x1b[31m", card)
        self.assertIn("codex exited 1: boom", plain(card))

    def test_agent_text_cannot_inject_escape_codes(self):
        card = tandem.turn_card(dict(self.ENTRY, summary="hi \x1b[2J there"), True, 60)
        self.assertNotIn("\x1b[2J", card)

    def state(self, history, **extra):
        state = {"name": "parser", "goal": "Split the parser", "turn": len(history) + 1, "max_turns": 20,
                 "history": history, "cost_usd": 1.5, "config": {"claude_model": "opus", "check": "make test"}}
        state.update(extra)
        return state

    def test_board_agreement_states(self):
        done = lambda agent, n: dict(self.ENTRY, agent=agent, turn=n, status="done")
        waiting = plain(tandem.board(self.state([done("claude", 1)]), True, 80))
        self.assertIn("Claude Code says done (turn 1) \u00b7 waiting for Codex", waiting)
        agreed = plain(tandem.board(self.state([done("claude", 1), done("codex", 2)], outcome="done"), True, 80))
        self.assertIn("both agents agreed", agreed)
        self.assertIn("passed", agreed)
        alone = plain(tandem.board(self.state([done("claude", 1)], outcome="done", needs_review=True), True, 80))
        self.assertIn("Claude Code alone", alone)
        none = plain(tandem.board(self.state([self.ENTRY]), True, 80))
        self.assertIn("nothing agreed yet", none)
        self.assertIn("not run yet", none)
        failed = plain(tandem.board(self.state([self.ENTRY], check_feedback="it failed"), True, 80))
        self.assertIn("failed last time", failed)
        self.assertIn("no check", plain(tandem.board(self.state([self.ENTRY], config={}), True, 80)))

    def test_board_lists_what_is_open_and_the_models(self):
        history = [dict(self.ENTRY, turn=1, could_not_do="start the server"), dict(self.ENTRY, turn=2)]
        text = plain(tandem.board(self.state(history, owner_messages=[{"text": "hi"}]), True, 100))
        for expected in ("\u2022 Codex: review", "run the integration suite", "start the server", "owner messages: 1",
                         "models: claude opus/default \u00b7 codex default/default \u00b7 $1.50", "Split the parser"):
            self.assertIn(expected, text)

    def test_board_fits_its_width_and_has_no_codes_when_plain(self):
        state = self.state([self.ENTRY], goal="g " * 100)
        camp = {"branch": "tandem/x", "runs": [{}], "queue": ["a", "b"]}
        for fancy in (True, False):
            text = tandem.board(state, fancy, 30, camp)
            self.assertTrue(all(len(line) == 30 for line in plain(text).split("\n")))
        self.assertNotIn("\x1b", tandem.board(state, False, 30))

    def test_board_command_uses_latest_run(self):
        with tempfile.TemporaryDirectory() as d:
            run = pathlib.Path(d)
            (run / "state.json").write_text(json.dumps(self.state([self.ENTRY])))
            rows = [{"id": "mine", "path": str(run), "repo": "/repo"}]
            session = tandem.Session(argparse.Namespace(repo=pathlib.Path("/repo")), tandem.Console(fancy=False))
            with mock.patch.dict(tandem.CURRENT, {}, clear=True), mock.patch.object(tandem, "list_runs", return_value=rows), \
                    mock.patch.object(tandem, "emit") as emit:
                session.handle("board", "")
            self.assertIn("Split the parser", "\n".join(c.args[0] for c in emit.call_args_list))

    def test_board_command_follows_a_campaign_to_its_current_run(self):
        with tempfile.TemporaryDirectory() as d:
            camp, run = pathlib.Path(d), pathlib.Path(d) / "run1"
            run.mkdir()
            (run / "state.json").write_text(json.dumps(self.state([self.ENTRY])))
            (camp / "campaign.json").write_text(json.dumps(
                {"branch": "b", "runs": [], "queue": [], "current": str(run)}))
            session = tandem.Session(argparse.Namespace(repo=None), tandem.Console(fancy=False))
            with mock.patch.dict(tandem.CURRENT, {"target": camp}), mock.patch.object(tandem, "emit") as emit:
                session.handle("board", "")
            self.assertIn("Split the parser", "\n".join(c.args[0] for c in emit.call_args_list))

    def test_loop_prints_a_card_only_in_a_fancy_session(self):
        for fancy, expect_card in ((True, True), (False, False)):
            console = tandem.Console(fancy=fancy)
            with mock.patch.object(tandem, "CONSOLE", console), mock.patch.object(tandem, "emit") as emit:
                tandem.report_turn(dict(self.ENTRY))
            text = "\n".join(c.args[0] for c in emit.call_args_list)
            self.assertEqual("\u256d" in text, expect_card)
            self.assertIn("turn 3", plain(text))


class StatusBar(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = pathlib.Path(temp.name)
        patcher = mock.patch.object(tandem, "AVAILABILITY", self.dir / "availability.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self, **extra):
        state = {"turn": 3, "max_turns": 12, "next": "claude", "cost_usd": 0.84,
                 "failures": {"claude": 0, "codex": 0}, "diffstat": {"added": 477, "removed": 7},
                 "config": {"claude_model": "opus", "claude_effort": "high", "codex_model": None,
                            "codex_effort": "low", "check": "make test"},
                 "history": [{"turn": 1, "agent": "claude", "status": "continue",
                              "usage": {"input": 30000, "output": 1000}},
                             {"turn": 2, "agent": "codex", "status": "continue", "model": "gpt-5-codex",
                              "usage": {"total": 10000}}]}
        state.update(extra)
        return state

    def test_bar_shows_models_doing_cost_and_session(self):
        now = 1000000.0
        first, second = tandem.status_bar(self.state(), now, 200, False,
                                          progress={"turn": 3, "agent": "codex", "since": now - 5},
                                          session_start=now - 4320)
        self.assertEqual(first, "claude opus/high · next   codex gpt-5-codex/low · working 0m05s")
        self.assertEqual(second, "turn 3/12 · $0.84 · 41k tok · session 1h12m · "
                                 "+477 −7 · check –")
        _, second = tandem.status_bar(self.state(check_passed=False), now, 200, False, progress={})
        self.assertTrue(second.endswith("check ✗"))
        self.assertNotIn("session", second)
        self.assertNotIn("tok", tandem.status_bar(self.state(history=[]), now, 200, False, progress={})[1])
        idle = tandem.status_bar(None, now, 200, False, progress={}, defaults={"codex_model": "o3"})
        self.assertEqual(idle, ["claude default/default   codex o3/default · /models to change"])

    def test_bar_fits_and_degrades(self):
        state = self.state()
        for fancy in (True, False):
            lines = tandem.status_bar(state, 0, 30, fancy, progress={}, session_start=0)
            self.assertEqual(len(lines), 2)
            for line in lines:
                self.assertLessEqual(tandem.display_width(line), 30, line)
                self.assertTrue(plain(line).endswith("…"), line)
                self.assertEqual(len(plain(line)), 30)  # colored after the cut, so the codes cost no room
            self.assertEqual("\x1b" in "".join(lines), fancy)
        self.assertEqual([plain(line) for line in tandem.status_bar(state, 0, 30, True, progress={})],
                         tandem.status_bar(state, 0, 30, False, progress={}))

    def test_fit_width_skips_escape_codes(self):
        colored = "\x1b[32mgreen\x1b[0m and more"
        self.assertEqual(tandem.display_width(colored), 14)
        self.assertEqual(tandem.fit_width(colored, 14), colored)
        self.assertEqual(tandem.fit_width(colored, 3), "\x1b[32mgre\x1b[0m")  # the color cannot run on

    def test_away_and_benched_agents(self):
        until = datetime.datetime.now().replace(second=0, microsecond=0) + datetime.timedelta(hours=1)
        tandem.write_json(tandem.AVAILABILITY, {"codex": until.isoformat(timespec="seconds")})
        state = self.state(failures={"claude": tandem.MAX_FAILURES_PER_AGENT, "codex": 0})
        first = tandem.status_bar(state, 0, 200, False, progress={})[0]
        self.assertIn(f"codex gpt-5-codex/low · away until {until:%H:%M}", first)
        self.assertIn("claude opus/high · benched", first)
        state = self.state(next="codex", outcome="done", failures={"claude": 0, "codex": 0})
        tandem.AVAILABILITY.unlink()
        self.assertNotIn("next", tandem.status_bar(state, 0, 200, False, progress={})[0])

    def test_console_bar_follows_the_saved_state(self):
        console = tandem.Console(fancy=False)
        with mock.patch.dict(tandem.CURRENT, clear=True), \
                mock.patch.object(tandem, "load_defaults", return_value={"claude_model": "sonnet"}):
            self.assertEqual(console.bar(80), ["claude sonnet/default   codex default/default · /models to change"])
            self.assertTrue(console.bar(40)[0].endswith("…"))
            self.assertEqual(tandem.display_width(console.bar(40)[0]), 39)  # a column short of a wrap
            tandem.save(self.dir, self.state())
            tandem.CURRENT["target"] = self.dir
            first, second = console.bar(200)
            self.assertIn("claude opus/high · next", first)
            self.assertIn("session 0m0", second)
            (self.dir / "state.json").write_text("{broken")  # caught mid-write: the last copy read stays
            self.assertEqual(console.bar(200)[0], first)

    def test_usage_is_recorded_from_claude_json_and_codex_log(self):
        schema = self.dir / "schema.json"
        schema.write_text("{}")
        cfg = {"timeout": 5, "claude_permission_mode": "acceptEdits"}
        payload = {"structured_output": {"status": "done"}, "total_cost_usd": 0.5,
                   "usage": {"input_tokens": 10, "cache_read_input_tokens": 2000,
                             "cache_creation_input_tokens": 300, "output_tokens": 45},
                   "modelUsage": {"claude-haiku-4-5": {"costUSD": 0.01}, "claude-opus-5-5": {"costUSD": 0.49}}}
        reply = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.object(tandem, "run_child", return_value=reply):
            self.assertEqual(tandem.run_claude(cfg, self.dir, "goal", self.dir / "c", schema, "status", False),
                             ({"status": "done"}, 0.5, {"input": 2310, "output": 45, "model": "claude-opus-5-5"}))
        log = "OpenAI Codex v0.50\n--------\nmodel: gpt-5-codex\nprovider: openai\n--------\ndone\ntokens used: 12,345\n"
        (self.dir / "x.last.json").write_text('{"status": "continue"}')
        with mock.patch.object(tandem, "run_child", return_value=subprocess.CompletedProcess([], 0, "", log)):
            self.assertEqual(tandem.run_codex(cfg, self.dir, "goal", self.dir / "x", schema, "status", False),
                             ({"status": "continue"}, 0.0, {"total": 12345, "model": "gpt-5-codex"}))
        self.assertEqual(tandem.codex_usage("tokens used\n9,876\n"), {"total": 9876})
        self.assertEqual(tandem.codex_usage("no numbers here"), {})
        # A command the agent ran may print the same words; codex's own count comes last.
        self.assertEqual(tandem.codex_usage("exec\ntokens used: 5\nexited 0\ntokens used\n50,881\n"), {"total": 50881})
        self.assertEqual(tandem.claude_usage({"result": "x"}), {})

    def test_loop_keeps_usage_on_entries_and_totals_in_state(self):
        repo = self.dir / "repo"
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        with mock.patch.dict(os.environ, env), mock.patch.object(tandem, "log"):
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.txt").write_text("one\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
            cfg = {"check": None, "timeout": 5, "claude_permission_mode": "acceptEdits"}
            run_dir = tandem.create_run(self.dir / "run", repo, "goal", "g", cfg, 2, "claude")
            work = pathlib.Path(tandem.load(run_dir)["worktree"])

            def agent(name, *args, **kwargs):
                if name == "claude":
                    (work / "a.txt").write_text("two\nthree\n")
                    return {"status": "done"}, 0.1, {"input": 100, "output": 5, "model": "claude-opus-5-5"}
                return {"status": "done"}, 0.0, {}

            with mock.patch.object(tandem, "call_agent", side_effect=agent):
                tandem.loop(run_dir)
        state = tandem.load(run_dir)
        self.assertEqual(state["history"][0]["usage"], {"input": 100, "output": 5})
        self.assertEqual(state["history"][0]["model"], "claude-opus-5-5")
        self.assertNotIn("usage", state["history"][1])  # Codex reported nothing: nothing is guessed
        self.assertEqual(state["tokens"], {"claude": 105, "codex": 0})
        self.assertEqual(state["diffstat"], {"files": 1, "added": 2, "removed": 1})


class Interactive(unittest.TestCase):
    def test_slash_parser(self):
        parse = tandem.parse_input
        self.assertEqual(parse("   ")[0], "empty")
        self.assertEqual(parse("fix the bug"), ("goal", "fix the bug", ""))
        self.assertEqual(parse("/help"), ("command", "help", ""))
        self.assertEqual(parse("/say  use the   v2 API please "), ("command", "say", "use the   v2 API please"))
        self.assertEqual(parse("/say\tuse the v2 API"), ("command", "say", "use the v2 API"))
        self.assertEqual(parse("/RESUME 5"), ("command", "resume", "5"))
        self.assertEqual(parse("/campaign start --goal-file a.md"),
                         ("command", "campaign", "start --goal-file a.md"))
        self.assertEqual(parse("/nope arg"), ("unknown", "nope", ""))
        self.assertEqual(parse("/"), ("unknown", "", ""))

    def test_every_documented_command_is_known(self):
        for name in ("help", "status", "say", "now", "paste", "stop", "resume", "goal", "campaign", "runs", "transcript",
                     "doctor", "board", "models", "quit"):
            self.assertEqual(tandem.parse_input("/" + name)[0], "command")
            self.assertIn("/" + name, tandem.SLASH_HELP)

    def test_consent_asked_once_and_recorded(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            with mock.patch("builtins.print"):
                self.assertFalse(tandem.ask_consent(lambda _: "no"))
            self.assertFalse(tandem.consent_recorded())
            with mock.patch("builtins.print"):
                self.assertTrue(tandem.ask_consent(lambda _: "YES"))
            data = json.loads((pathlib.Path(home) / ".tandem" / "config.json").read_text())
            self.assertEqual(data["consent"]["version"], tandem.__version__)
            self.assertTrue(tandem.ask_consent(lambda _: self.fail("asked twice")))

    def test_console_redraws_prompt_and_status_on_a_terminal(self):
        console = tandem.Console(fancy=True)
        console.prompt, console.prompt_active, console.running, console.status_shown = "tandem> ", True, True, True
        out = io.StringIO()
        progress = dict(turn=3, agent="claude", since=time.time() - 134)
        with mock.patch("sys.stdout", out), mock.patch.dict(tandem.PROGRESS, progress):
            console.write("hello")
        text = out.getvalue()
        self.assertIn("hello\n", text)
        self.assertIn("turn 3: Claude Code working... 2m1", text)
        self.assertTrue(text.endswith("tandem> "))

    def test_plain_console_has_no_escape_codes(self):
        console = tandem.Console(fancy=False)
        console.prompt_active = True
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            console.write("hello")
        self.assertNotIn("\x1b", out.getvalue())
        tty = mock.Mock()
        tty.isatty.return_value = True
        for value in ("1", ""):
            with mock.patch.dict(os.environ, {"NO_COLOR": value}), mock.patch("sys.stdout", tty):
                self.assertFalse(tandem.fancy_terminal())
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(tandem.fancy_terminal())

    def test_second_goal_does_not_create_run_or_change_target(self):
        session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
        session.thread = mock.Mock()
        session.thread.is_alive.return_value = True
        target = pathlib.Path("already-running")
        with mock.patch.dict(tandem.CURRENT, {"target": target}, clear=True), \
                mock.patch.object(tandem, "prepare_run") as prepare, mock.patch.object(tandem, "emit") as emit:
            session.start_goal("another goal")
            session.start_goal(goal_file="another.md")
            self.assertEqual(tandem.CURRENT["target"], target)
        prepare.assert_not_called()
        self.assertIn("already going", emit.call_args[0][0])

    def test_resume_while_busy_keeps_pending_stop(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d)
            stop = target / "STOP"
            stop.write_text("stop")
            session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
            session.thread = mock.Mock()
            session.thread.is_alive.return_value = True
            with mock.patch.object(tandem, "emit"), mock.patch.object(session, "launch") as launch:
                session.resume(target, "5")
            self.assertEqual(stop.read_text(), "stop")
            launch.assert_not_called()

    def test_resume_rejects_nonpositive_turn_count_without_removing_stop(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d)
            stop = target / "STOP"
            stop.write_text("stop")
            session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
            with mock.patch.object(tandem, "emit"), mock.patch.object(session, "launch") as launch:
                for value in ("0", "-1", "bad"):
                    session.resume(target, value)
            self.assertTrue(stop.exists())
            launch.assert_not_called()

    def test_cli_resume_clears_own_stop_but_not_campaign_stop(self):
        with tempfile.TemporaryDirectory() as d:
            camp = pathlib.Path(d) / "camp"
            run = camp / "01-x"
            run.mkdir(parents=True)
            (camp / "STOP").write_text("stop")
            (run / "STOP").write_text("stop")
            for stop, kept in ((run / "STOP", False), (camp / "STOP", True)):
                (run / "state.json").write_text(json.dumps({"turn": 3, "max_turns": 9, "stop_file": str(stop)}))
                with mock.patch.object(tandem, "loop") as loop:
                    tandem.cmd_resume(argparse.Namespace(run=str(run), more_turns=None))
                loop.assert_called_once()
                self.assertEqual(stop.exists(), kept)
                stop.write_text("stop")

    def test_transcript_path_for_run_and_campaign(self):
        with tempfile.TemporaryDirectory() as d:
            camp = pathlib.Path(d)
            (camp / "campaign.json").write_text(json.dumps({"runs": [{"dir": "/r/01"}], "current": None}))
            self.assertEqual(tandem.Session.transcript_path(camp), pathlib.Path("/r/01/transcript.md"))
            (camp / "campaign.json").write_text(json.dumps({"runs": [{"dir": "/r/01"}], "current": "/r/02"}))
            self.assertEqual(tandem.Session.transcript_path(camp), pathlib.Path("/r/02/transcript.md"))
            (camp / "campaign.json").write_text(json.dumps({"runs": [], "current": None}))
            self.assertEqual(tandem.Session.transcript_path(camp), camp)
            (camp / "state.json").write_text("{}")
            self.assertEqual(tandem.Session.transcript_path(camp), camp / "transcript.md")

    def test_campaign_prepares_target_before_returning_to_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            target = pathlib.Path(d)
            session = tandem.Session(argparse.Namespace(repo="session-repo"), tandem.Console(fancy=False))

            def prepare(args):
                self.assertEqual(args.repo, "session-repo")
                tandem.CURRENT["target"] = target
                return target

            with mock.patch.dict(tandem.CURRENT, {}, clear=True), \
                    mock.patch.object(tandem, "prepare_campaign_start", side_effect=prepare), \
                    mock.patch.object(tandem, "campaign_loop") as loop, \
                    mock.patch.object(session, "launch") as launch, mock.patch.object(tandem, "emit"):
                session.campaign("start")
                session.request_stop()
                self.assertTrue((target / "STOP").exists())
                launch.call_args[0][1]()
                loop.assert_called_once_with(target)

    def test_campaign_help_uses_console_and_does_not_report_bad_arguments(self):
        session = tandem.Session(argparse.Namespace(repo="."), tandem.Console(fancy=False))
        console = mock.Mock()
        with mock.patch.object(tandem, "CONSOLE", console):
            session.campaign("start --help")
        text = "\n".join(call.args[0] for call in console.write.call_args_list)
        self.assertIn("--goal-file", text)
        self.assertNotIn("bad arguments", text)

    def test_inbox_message_arriving_during_consumption_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = pathlib.Path(d) / "inbox.md"
            inbox.write_text("first message")
            clearing, allow_clear, writer_started, writer_done = [threading.Event() for _ in range(4)]
            consumed, errors = [], []
            write_text = pathlib.Path.write_text

            def pause_clear(path, text, *args, **kwargs):
                if path == inbox and text == "":
                    clearing.set()
                    if not allow_clear.wait(3):
                        raise RuntimeError("test did not release inbox consumer")
                return write_text(path, text, *args, **kwargs)

            def consume():
                try:
                    consumed.append(tandem.take_inbox([inbox]))
                except Exception as exc:
                    errors.append(exc)

            def say():
                writer_started.set()
                try:
                    tandem.cmd_say(argparse.Namespace(run=d, message="next message"))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    writer_done.set()

            reader, writer = threading.Thread(target=consume), threading.Thread(target=say)
            with mock.patch.object(pathlib.Path, "write_text", pause_clear), mock.patch.object(tandem, "emit"):
                reader.start()
                try:
                    self.assertTrue(clearing.wait(3))
                    writer.start()
                    self.assertTrue(writer_started.wait(3))
                    self.assertFalse(writer_done.wait(.05))
                finally:
                    allow_clear.set()
                    reader.join(3)
                    if writer.ident is not None:
                        writer.join(3)
            self.assertFalse(errors)
            self.assertEqual(consumed, ["first message"])
            self.assertEqual(tandem.take_inbox([inbox]), "next message")

    def test_say_reaches_the_inbox_while_a_run_is_going(self):
        with tempfile.TemporaryDirectory() as d:
            run = pathlib.Path(d)
            session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
            with mock.patch.dict(tandem.CURRENT, {"target": run}), mock.patch("builtins.print"):
                self.assertTrue(session.handle("say", "use the v2 API"))
            self.assertEqual((run / "inbox.md").read_text(), "use the v2 API\n\n")

    def test_stop_writes_the_stop_file_and_quit_leaves(self):
        with tempfile.TemporaryDirectory() as d:
            session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
            with mock.patch.dict(tandem.CURRENT, {"target": pathlib.Path(d)}), mock.patch("builtins.print"):
                session.handle("stop", "")
            self.assertTrue((pathlib.Path(d) / "STOP").exists())
            self.assertFalse(session.handle("quit", ""))

    def test_new_session_resumes_the_latest_run_in_its_repo(self):
        rows = [{"id": "other", "path": "/runs/other", "repo": "/elsewhere"},
                {"id": "mine", "path": "/runs/mine", "repo": "/repo"}]
        session = tandem.Session(argparse.Namespace(repo=pathlib.Path("/repo")), tandem.Console(fancy=False))
        with mock.patch.dict(tandem.CURRENT, {}, clear=True), mock.patch.object(tandem, "list_runs", return_value=rows), \
                mock.patch.object(tandem, "emit"), mock.patch.object(session, "resume") as resume:
            session.handle("resume", "4")
            self.assertEqual(tandem.CURRENT["target"], pathlib.Path("/runs/mine"))
        resume.assert_called_once_with(pathlib.Path("/runs/mine"), "4")
        with mock.patch.dict(tandem.CURRENT, {}, clear=True), mock.patch.object(tandem, "list_runs", return_value=rows), \
                mock.patch.object(tandem, "emit"):
            self.assertIsNone(session.target())  # /say does not guess

    def test_no_resume_hint_after_quit(self):
        session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
        session.quitting = True
        with mock.patch.object(tandem, "emit") as emit:
            session.launch("run", lambda: None)
            session.thread.join(3)
        self.assertFalse(any("type a goal" in c.args[0] for c in emit.call_args_list))

    def test_libedit_buffer_is_not_redrawn(self):
        console = tandem.Console(fancy=True)
        for backend, doc, expected in (("editline", "", ""), ("", "Importing this module enables libedit", ""),
                                       ("readline", "GNU readline", "typed")):
            fake = mock.Mock(backend=backend, __doc__=doc, get_line_buffer=lambda: "typed")
            with self.subTest(backend=backend), mock.patch.dict(sys.modules, {"readline": fake}):
                self.assertEqual(console._buffer(), expected)


class Correction(unittest.TestCase):
    setUp = Headless.setUp  # same throwaway repo and RUNS patches
    run_dir_with = Tuning.run_dir_with  # first agent codex, 3 turns

    def runners(self, reply):
        """Stub RUNNERS that record (agent, prompt) and answer with reply(call number, worktree).

        Each call is tracked like a real agent process, so interrupt_turn has something to stop."""
        calls = []

        def make(name):
            def run(cfg, worktree, prompt, *rest):
                calls.append((name, prompt))
                with self.agent_running():
                    return reply(len(calls), worktree)
            return run
        return calls, mock.patch.dict(tandem.RUNNERS, {n: make(n) for n in tandem.NAMES})

    @contextlib.contextmanager
    def agent_running(self):
        """A fake agent process in CHILDREN for the block; stopping it only records the call."""
        proc = mock.Mock(returncode=None, tandem_group=False, tandem_kind="agent", tandem_done=False)
        with tandem.CHILDREN_LOCK:
            tandem.CHILDREN.add(proc)
        try:
            yield proc
        finally:
            with tandem.CHILDREN_LOCK:
                tandem.CHILDREN.discard(proc)

    def test_interrupted_turn_keeps_edits_and_turn_budget(self):
        run_dir = self.run_dir_with()

        def reply(n, worktree):
            if n == 1:
                (pathlib.Path(worktree) / "half.txt").write_text("half done")
                tandem.cmd_say(argparse.Namespace(run=str(run_dir), message="use tabs not spaces"))
                tandem.interrupt_turn(run_dir)
                raise RuntimeError("exit -15, no JSON output: ")  # what a killed agent looks like
            return {"status": "continue", "summary": "went on"}, 0

        calls, patch = self.runners(reply)
        with patch, mock.patch.object(tandem, "log"), mock.patch.object(tandem, "emit"):
            tandem.loop(run_dir)
        state = tandem.load(run_dir)
        first = state["history"][0]
        self.assertEqual((first["status"], first["summary"], first["agent"]),
                         ("interrupted", "interrupted by the owner", "codex"))
        self.assertIn("half.txt", tandem.git(self.repo, "show", "--name-only", "--format=%s", first["commit"]))
        self.assertIn("interrupted", tandem.git(self.repo, "log", "-1", "--format=%s", first["commit"]))
        self.assertEqual(state["failures"], {"claude": 0, "codex": 0})
        self.assertEqual(state["max_turns"], 4)
        self.assertEqual(len(calls), 4)
        agent, prompt = calls[1]
        self.assertEqual(agent, "codex")
        self.assertIn("use tabs not spaces", prompt)
        self.assertIn("NEW, read first", prompt)
        self.assertIn(f"Your previous turn was interrupted by the owner; its partial edits are in commit "
                      f"{first['commit']}. Check them against the message above before you go on.", prompt)
        self.assertNotIn("interrupted by the owner;", calls[2][1])  # only the turn right after
        self.assertIn(f"### Turn 1 - Codex - interrupted by the owner - commit {first['commit']}",
                      (run_dir / "transcript.md").read_text())
        self.assertFalse((run_dir / "INTERRUPT").exists())
        activity = (run_dir / "activity.log").read_text()
        self.assertIn("\tcodex\t= interrupted (", activity)  # `tandem watch` sees each turn end
        self.assertIn("\tcodex\t= continue (", activity)

    def test_interrupt_is_never_retried_or_counted(self):
        run_dir = self.run_dir_with()

        def flaky(*args):
            (run_dir / "INTERRUPT").write_text("now")
            raise RuntimeError("ECONNRESET")  # would otherwise be a transient error and wait

        with mock.patch.dict(tandem.RUNNERS, {"codex": flaky}), \
                mock.patch.object(tandem, "wait_until", side_effect=AssertionError("waited")), \
                self.assertRaises(tandem.TurnInterrupted):
            tandem.call_agent("codex", {}, ".", "p", run_dir / "t", None, "status", False, None, run_dir)

    def test_nothing_changed_is_said_so(self):
        run_dir = self.run_dir_with()

        def reply(n, worktree):
            if n == 1:
                tandem.interrupt_turn(run_dir)
                raise RuntimeError("exit -15, no JSON output: ")
            return {"status": "continue"}, 0

        calls, patch = self.runners(reply)
        with patch, mock.patch.object(tandem, "log"), mock.patch.object(tandem, "emit"):
            tandem.loop(run_dir)
        self.assertIsNone(tandem.load(run_dir)["history"][0]["commit"])
        self.assertIn("interrupted by the owner without a message; it changed nothing. Check", calls[1][1])

    def test_interrupt_without_a_message_does_not_point_at_an_old_one(self):
        run_dir = self.run_dir_with()
        tandem.cmd_say(argparse.Namespace(run=str(run_dir), message="use tabs not spaces"))

        def reply(n, worktree):
            if n == 2:
                tandem.interrupt_turn(run_dir)
                raise RuntimeError("exit -15, no JSON output: ")
            return {"status": "continue"}, 0

        calls, patch = self.runners(reply)
        with patch, mock.patch.object(tandem, "log"), mock.patch.object(tandem, "emit"):
            tandem.loop(run_dir)
        prompt = calls[2][1]
        self.assertIn("use tabs not spaces", prompt)  # the old message is still there, as every owner message is
        self.assertIn("Your previous turn was interrupted by the owner without a message; it changed nothing. "
                      "Check them before you go on.", prompt)
        self.assertNotIn("message above", prompt)

    def test_interrupt_without_an_agent_leaves_no_flag(self):
        # A flag nobody handles would turn a real error later in the same turn, even a usage limit,
        # into an interruption.
        run_dir = self.run_dir_with()
        with self.agent_running() as done, mock.patch.object(tandem, "emit") as emit:
            done.returncode = 0  # exited, not yet collected by run_child
            tandem.interrupt_turn(run_dir)
        self.assertFalse((run_dir / "INTERRUPT").exists())
        self.assertEqual([c.args[0] for c in emit.call_args_list], ["no agent is working right now; the next turn reads it"])

        def limited(*args):
            raise RuntimeError("You've hit your usage limit. Try again at 5:45 PM")

        with mock.patch.dict(tandem.RUNNERS, {"codex": limited}), mock.patch.object(tandem, "log"), \
                self.assertRaises(RuntimeError) as raised:
            tandem.call_agent("codex", {}, ".", "p", run_dir / "t", None, "status", False, None, run_dir)
        self.assertNotIsInstance(raised.exception, tandem.TurnInterrupted)
        self.assertIsNotNone(tandem.unavailable_until("codex"))

    def test_hard_kill_reaches_only_the_interrupted_agent(self):
        # The agent that ignored the polite request, not a planner or the next goal's agent that
        # started in the meantime.
        run_dir = self.run_dir_with()
        with mock.patch.object(tandem, "KILL_GRACE", 0.2), mock.patch.object(tandem, "emit"):
            with self.agent_running() as first:
                tandem.interrupt_turn(run_dir)
            first.terminate.assert_called_once_with()
            with self.agent_running() as later:
                time.sleep(0.6)
        first.kill.assert_called_once_with()
        later.kill.assert_not_called()
        later.terminate.assert_not_called()

    def test_stale_interrupt_is_ignored(self):
        run_dir = self.run_dir_with()
        (run_dir / "INTERRUPT").write_text("left by a crash")
        calls, patch = self.runners(lambda n, w: ({"status": "continue"}, 0))
        with patch, mock.patch.object(tandem, "log"), mock.patch.object(tandem, "emit"):
            tandem.loop(run_dir)
        state = tandem.load(run_dir)
        self.assertEqual([h["status"] for h in state["history"]], ["continue"] * 3)
        self.assertEqual(state["max_turns"], 3)
        self.assertFalse((run_dir / "INTERRUPT").exists())

    def test_interrupt_during_the_check_lets_it_finish(self):
        # The turn has already ended when the check runs: the check is not an agent and is not
        # stopped, and the next turn starts normally with the correction.
        marker, script = self.base / "check-done", self.base / "check.py"
        script.write_text(f"import pathlib, time\ntime.sleep(1)\npathlib.Path({str(marker)!r}).write_text('x')\n"
                          f"raise SystemExit(1)\n")
        run_dir = self.run_dir_with(check=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")

        def interrupt_when_the_check_runs():
            deadline = time.monotonic() + 10
            while not tandem.CHILDREN and time.monotonic() < deadline:
                time.sleep(.01)
            tandem.cmd_say(argparse.Namespace(run=str(run_dir), message="rename it"))
            tandem.interrupt_turn(run_dir)

        watcher = threading.Thread(target=interrupt_when_the_check_runs)
        calls, patch = self.runners(lambda n, w: ({"status": "done" if n < 3 else "continue"}, 0))
        with patch, mock.patch.object(tandem, "log"), mock.patch.object(tandem, "emit") as emit:
            watcher.start()
            tandem.loop(run_dir)
            watcher.join(10)
        state = tandem.load(run_dir)
        self.assertTrue(marker.exists())
        self.assertEqual([h["status"] for h in state["history"]], ["done", "done", "continue"])
        self.assertEqual(state["max_turns"], 3)
        self.assertIn("failed", calls[2][1])  # the check's verdict still counts
        self.assertIn("rename it", calls[2][1])
        self.assertNotIn("Your previous turn was interrupted", calls[2][1])
        self.assertIn("no agent is working right now; the next turn reads it", [c.args[0] for c in emit.call_args_list])
        self.assertFalse((run_dir / "INTERRUPT").exists())

    def busy_session(self):
        session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
        session.thread = mock.Mock()
        session.thread.is_alive.return_value = True
        return session

    def run_target(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = pathlib.Path(temp.name)
        (run / "state.json").write_text("{}")
        return run

    def test_busy_plain_text_is_saved_before_asking(self):
        run = self.run_target()
        session = self.busy_session()
        session.images = [pathlib.Path("/tmp/shot.png")]
        seen = []
        for error in (KeyboardInterrupt, EOFError, "", "n", " No ", "nie"):
            def ask(prompt, history=True):
                self.assertEqual(prompt, "  stop the current turn now and hand it over? its edits are kept [y/N] ")
                self.assertFalse(history)
                seen.append((run / "inbox.md").read_text())
                self.assertIn("use tabs not spaces", seen[-1])
                if isinstance(error, str):
                    return error
                raise error
            (run / "inbox.md").write_text("")
            session.declined = None  # asked once per turn; each answer here stands for a fresh turn
            with self.subTest(answer=error), mock.patch.dict(tandem.CURRENT, {"target": run}), \
                    mock.patch.object(session.console, "ask", side_effect=ask) as asked, \
                    mock.patch.object(tandem, "signal_children") as signal_children, \
                    mock.patch.object(tandem, "emit") as emit:
                session.typed("use tabs not spaces")
            asked.assert_called_once()
            signal_children.assert_not_called()
            self.assertFalse((run / "INTERRUPT").exists())
            lines = [c.args[0] for c in emit.call_args_list]
            self.assertEqual(lines, ["✓ queued for the next turn (1 waiting)",
                                     "the next turn reads it; until then more lines queue without asking, /now steps in"])
        self.assertIn("[image: /tmp/shot.png]", seen[0])  # pending images go with it, as with /say
        self.assertEqual(session.images, [])

    def test_any_other_answer_is_kept_as_another_message(self):
        # The owner may answer the question with more of the correction; nothing typed is dropped.
        run = self.run_target()
        session = self.busy_session()
        with mock.patch.dict(tandem.CURRENT, {"target": run}), \
                mock.patch.object(session.console, "ask", return_value=" and rename the file "), \
                mock.patch.object(tandem, "signal_children") as signal_children, \
                mock.patch.object(tandem, "emit") as emit:
            session.typed("use tabs not spaces")
        signal_children.assert_not_called()
        self.assertFalse((run / "INTERRUPT").exists())
        self.assertEqual((run / "inbox.md").read_text(), "use tabs not spaces\n\nand rename the file\n\n")
        self.assertEqual([c.args[0] for c in emit.call_args_list],
                         ["✓ queued for the next turn (1 waiting)", "the next turn reads it; until then more lines queue without asking, /now steps in"])

    def test_yes_interrupts(self):
        run = self.run_target()
        session = self.busy_session()
        for answer in ("y", "Y", "yes", "t", " TAK "):
            (run / "INTERRUPT").unlink(missing_ok=True)
            with self.subTest(answer=answer), mock.patch.dict(tandem.CURRENT, {"target": run}), \
                    mock.patch.object(session.console, "ask", return_value=answer), \
                    self.agent_running() as agent, mock.patch.object(tandem, "emit"):
                session.typed("use tabs not spaces")
            self.assertTrue((run / "INTERRUPT").exists())
            agent.terminate.assert_called_once_with()

    def test_turn_that_ended_while_asking_is_left_alone(self):
        run = self.run_target()
        session = self.busy_session()

        def ask(prompt, history=True):
            tandem.PROGRESS.update(turn=2, agent="codex", since=2.0)  # the next turn began meanwhile
            return "y"

        with mock.patch.dict(tandem.CURRENT, {"target": run}), mock.patch.dict(tandem.PROGRESS, since=1.0), \
                mock.patch.object(session.console, "ask", side_effect=ask), \
                mock.patch.object(tandem, "signal_children") as signal_children, \
                mock.patch.object(tandem, "emit") as emit:
            session.typed("use tabs not spaces")
        signal_children.assert_not_called()
        self.assertFalse((run / "INTERRUPT").exists())
        self.assertEqual(emit.call_args[0][0], "that turn has ended; the next turn reads it")

    def test_now_skips_the_question(self):
        run = self.run_target()
        session = self.busy_session()
        with mock.patch.dict(tandem.CURRENT, {"target": run}), \
                mock.patch.object(session.console, "ask") as asked, \
                self.agent_running() as agent, mock.patch.object(tandem, "emit") as emit:
            session.handle("now", "use tabs not spaces")
            session.handle("now", "")
        asked.assert_not_called()
        agent.terminate.assert_called_once_with()
        self.assertTrue((run / "INTERRUPT").exists())
        self.assertEqual((run / "inbox.md").read_text(), "use tabs not spaces\n\n")
        self.assertEqual(emit.call_args[0][0], "usage: /now TEXT")

    def test_now_in_a_campaign_interrupts_its_current_run(self):
        camp, run = self.run_target(), self.run_target()
        (camp / "state.json").unlink()
        session = self.busy_session()
        for current, expect in ((None, "nothing to interrupt; the next goal reads it"), (str(run), None)):
            (camp / "campaign.json").write_text(json.dumps({"current": current, "runs": [{"dir": str(run)}]}))
            with self.subTest(current=current), mock.patch.dict(tandem.CURRENT, {"target": camp}), \
                    self.agent_running() as agent, mock.patch.object(tandem, "emit") as emit:
                session.handle("now", "rename it")
            self.assertEqual((run / "INTERRUPT").exists(), current is not None)
            self.assertFalse((camp / "INTERRUPT").exists())
            self.assertEqual(agent.terminate.called, current is not None)
            if expect:
                self.assertEqual(emit.call_args[0][0], expect)
        self.assertIn("rename it", (camp / "notes.md").read_text())

    def test_not_busy_now_only_saves(self):
        run = self.run_target()
        session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
        with mock.patch.dict(tandem.CURRENT, {"target": run}), \
                mock.patch.object(tandem, "signal_children") as signal_children, \
                mock.patch.object(tandem, "emit") as emit:
            session.handle("now", "rename it")
        signal_children.assert_not_called()
        self.assertFalse((run / "INTERRUPT").exists())
        self.assertEqual(emit.call_args[0][0], "no turn is running; the next turn reads it")

    def test_interrupted_card_is_magenta(self):
        entry = dict(Cards.ENTRY, status="interrupted", summary="interrupted by the owner", handoff="")
        card = tandem.turn_card(entry, True, 60)
        self.assertIn("\x1b[35minterrupted", card)
        self.assertIn("interrupted by the owner", plain(card))


CODEX_CACHE = {"models": [
    {"slug": "gpt-big", "description": "Frontier model", "visibility": "list",
     "supported_reasoning_levels": [{"effort": e, "description": f"{e} effort"} for e in ("low", "high", "ultra")]},
    {"slug": "gpt-hidden", "visibility": "hide", "supported_reasoning_levels": [{"effort": "low"}]},
    {"slug": "gpt-small", "description": "Fast model",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
]}


class Picker(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = pathlib.Path(temp.name) / "models_cache.json"
        self.cache.write_text(json.dumps(CODEX_CACHE))
        for patch in (mock.patch.object(tandem, "CODEX_MODELS_CACHE", self.cache),
                      mock.patch.dict(os.environ, {"HOME": temp.name})):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def selected(row):
        return row["values"][row["index"]]

    def test_keys_move_change_wrap_and_finish(self):
        rows = tandem.model_choices({})
        self.assertEqual([r["key"] for r in rows], list(tandem.TUNABLE))
        self.assertEqual(rows[0]["values"], ["default", "fable", "opus", "sonnet", "haiku"])
        self.assertEqual(rows[1]["values"], ["default", *tandem.CLAUDE_EFFORTS])
        self.assertEqual(rows[2]["values"], ["default", "gpt-big", "gpt-small"])
        self.assertEqual(rows[0]["notes"]["default"], "the CLI's own setting")
        focus = 0
        for key in ("right", "right"):
            focus, action = tandem.picker_step(rows, focus, key)
            self.assertIsNone(action)
        self.assertEqual(self.selected(rows[0]), "opus")
        tandem.picker_step(rows, 0, "h")
        tandem.picker_step(rows, 0, "left")
        tandem.picker_step(rows, 0, "left")
        self.assertEqual(self.selected(rows[0]), "haiku")  # left wraps around the values
        tandem.picker_step(rows, 0, "l")
        self.assertEqual(self.selected(rows[0]), "default")
        self.assertEqual(tandem.picker_step(rows, 0, "up"), (3, None))
        self.assertEqual(tandem.picker_step(rows, 3, "tab"), (0, None))
        self.assertEqual(tandem.picker_step(rows, 0, "shift-tab"), (3, None))
        self.assertEqual(tandem.picker_step(rows, 1, "j"), (2, None))
        self.assertEqual(tandem.picker_step(rows, 1, "k"), (0, None))
        self.assertEqual(tandem.picker_step(rows, 3, "down"), (0, None))
        self.assertEqual(tandem.picker_step(rows, 2, "enter"), (2, "save"))
        for key in ("esc", "q", "ctrl-c"):
            self.assertEqual(tandem.picker_step(rows, 1, key), (1, "cancel"))
        for key in ("x", ("paste", "text"), None, "\x1b[5~"):
            self.assertEqual(tandem.picker_step(rows, 1, key), (1, None))

    def test_codex_effort_follows_the_codex_model(self):
        rows = tandem.model_choices({"codex_model": "gpt-big", "codex_effort": "high"})
        self.assertEqual(self.selected(rows[2]), "gpt-big")
        self.assertEqual(rows[3]["values"], ["default", "low", "high", "ultra"])
        self.assertEqual(self.selected(rows[3]), "high")
        self.assertEqual(rows[3]["notes"]["ultra"], "ultra effort")
        tandem.picker_step(rows, 2, "right")
        self.assertEqual(self.selected(rows[2]), "gpt-small")
        self.assertEqual(rows[3]["values"], ["default", "low", "medium"])
        self.assertEqual(self.selected(rows[3]), "default")  # high is not offered by gpt-small
        tandem.picker_step(rows, 3, "right")
        tandem.picker_step(rows, 2, "right")  # back to default: every effort Codex knows
        self.assertEqual(rows[3]["values"], ["default", *tandem.EFFORTS])
        self.assertEqual(self.selected(rows[3]), "low")

    def test_codex_choices_survive_a_bad_cache(self):
        for content in (None, "{", "{}", '{"models": 3}', '{"models": [1, {"no": "slug"}]}'):
            with self.subTest(content=content):
                if content is None:
                    self.cache.unlink(missing_ok=True)
                else:
                    self.cache.write_text(content)
                rows = tandem.model_choices({})
                self.assertEqual(rows[2]["values"], ["default"])
                self.assertEqual(rows[3]["values"], ["default", *tandem.EFFORTS])
                self.assertIn("Codex model", tandem.render_picker(rows, 2, 80))

    def test_saved_value_missing_from_the_list_is_kept(self):
        rows = tandem.model_choices({"codex_model": "old-model", "claude_model": None, "claude_effort": "max"})
        self.assertEqual(rows[2]["values"][:2], ["default", "old-model"])
        self.assertEqual(self.selected(rows[2]), "old-model")
        self.assertEqual(self.selected(rows[0]), "default")
        self.assertEqual(self.selected(rows[1]), "max")

    def test_render_shows_rows_focus_note_and_hint(self):
        rows = tandem.model_choices({"claude_model": "opus"})
        text = tandem.render_picker(rows, 0, 80)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        lines = plain.split("\n")
        self.assertEqual(lines[0], "Models and effort")
        self.assertIn("▸ Claude model    ‹ opus ›", lines)
        self.assertIn("  Codex model     ‹ default ›", lines)
        self.assertIn(rows[0]["notes"]["opus"], plain)
        self.assertEqual(lines[-1], "↑↓ move · ←→ change · Tab next · Enter save · Esc cancel")
        self.assertIn("\x1b[2m↑↓ move", text)
        self.assertIn("\x1b[1m▸ Claude model", text)
        narrow = re.sub(r"\x1b\[[0-9;]*m", "", tandem.render_picker(rows, 2, 24))
        self.assertTrue(all(len(line) < 24 for line in narrow.split("\n")), narrow)

    def test_read_key_maps_escape_sequences(self):
        for raw, key in ((b"\x1b[A", "up"), (b"\x1b[B", "down"), (b"\x1b[C", "right"), (b"\x1b[D", "left"),
                         (b"\x1bOA", "up"), (b"\x1b[Z", "shift-tab"), (b"\t", "tab"), (b"\r", "enter"),
                         (b"\n", "enter"), (b"\x1b", "esc"), (b"\x03", "ctrl-c"), (b"q", "q"), ("ż".encode(), "ż")):
            r, w = os.pipe()
            self.addCleanup(os.close, r)
            os.write(w, raw)
            os.close(w)
            with self.subTest(raw=raw):
                self.assertEqual(tandem.read_key(r), key)

    @unittest.skipUnless(os.name == "posix", "raw keys are POSIX")
    def test_interrupt_or_error_restores_the_terminal(self):
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        termios = tandem.termios

        def attrs():
            # The kernel sets PENDIN by itself when canonical mode comes back; it is not ours to restore.
            a = termios.tcgetattr(slave)
            a[3] &= ~getattr(termios, "PENDIN", 0)
            return a

        before = attrs()
        stdin = mock.Mock(fileno=lambda: slave)
        for failure, expected in ((KeyboardInterrupt, None), (RuntimeError("boom"), RuntimeError)):
            out = io.StringIO()
            keys = iter(["down", failure])

            def key(fd):
                k = next(keys)
                if not isinstance(k, str):
                    self.assertFalse(termios.tcgetattr(fd)[3] & termios.ICANON)  # raw while open
                    raise k
                return k

            with self.subTest(failure=failure), mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", out), \
                    mock.patch.object(tandem, "read_key", key):
                if expected is None:
                    self.assertIsNone(tandem.run_picker(tandem.model_choices({})))
                else:
                    with self.assertRaises(expected):
                        tandem.run_picker(tandem.model_choices({}))
                self.assertEqual(attrs(), before)
                self.assertTrue(out.getvalue().startswith("\x1b[?25l"))
                self.assertTrue(out.getvalue().endswith("\x1b[10A\r\x1b[J\x1b[?25h"))  # frame cleared, cursor back

    def test_logs_wait_while_the_picker_is_open(self):
        console = tandem.Console(fancy=True)
        out = io.StringIO()
        with mock.patch.object(tandem, "CONSOLE", console), mock.patch("sys.stdout", out):
            with console.hold():
                tandem.emit("x")
                worker = threading.Thread(target=tandem.emit, args=("from a worker",))
                worker.start()
                worker.join(3)
                self.assertFalse(worker.is_alive())  # a worker never waits for the picker
                self.assertEqual(out.getvalue(), "")
            tandem.emit("after")
        self.assertEqual(out.getvalue(), "x\nfrom a worker\nafter\n")

    def test_picker_without_terminal_explains(self):
        tandem.save_defaults({"claude_model": "opus"})
        session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
        with mock.patch.dict(tandem.CURRENT, {}, clear=True), mock.patch.object(tandem, "emit") as emit, \
                mock.patch.object(tandem, "run_picker") as picker:
            self.assertTrue(session.handle("models", ""))
        picker.assert_not_called()
        text = "\n".join(c.args[0] for c in emit.call_args_list)
        self.assertIn("claude opus/default · codex default/default", text)
        self.assertIn("the picker needs a terminal; use --claude-model, --claude-effort, --codex-model, "
                      "--codex-effort", text)
        self.assertNotIn("\x1b", text)

    def test_saving_writes_defaults_and_the_running_target(self):
        with tempfile.TemporaryDirectory() as d:
            run = pathlib.Path(d)
            tandem.save(run, {"config": {"claude_model": "sonnet", "claude_effort": None,
                                         "codex_model": None, "codex_effort": "low"}})
            session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=True))
            session.thread = mock.Mock()
            session.thread.is_alive.return_value = True
            seen = []

            def pick(rows):
                seen.append({r["key"]: r["values"][r["index"]] for r in rows})
                return {"claude_model": "opus", "claude_effort": "high", "codex_model": "gpt-big",
                        "codex_effort": None}

            with mock.patch.dict(tandem.CURRENT, {"target": run}, clear=True), \
                    mock.patch.object(tandem, "emit") as emit, mock.patch.object(tandem, "run_picker", pick):
                session.handle("models", "")
            self.assertEqual(seen, [{"claude_model": "sonnet", "claude_effort": "default",
                                     "codex_model": "default", "codex_effort": "low"}])
            self.assertEqual(tandem.load_defaults(), {"claude_model": "opus", "claude_effort": "high",
                                                      "codex_model": "gpt-big"})
            self.assertEqual(tandem.read_tuning(run / "tuning.json"),
                             {"claude_model": "opus", "claude_effort": "high", "codex_model": "gpt-big",
                              "codex_effort": None})
            emit.assert_called_with("saved: claude opus/high · codex gpt-big/default (applies from the next turn)")
            with mock.patch.dict(tandem.CURRENT, {}, clear=True), mock.patch.object(tandem, "emit"), \
                    mock.patch.object(tandem, "run_picker", return_value=None) as cancel:
                session.handle("models", "")
            cancel.assert_called_once()
            self.assertEqual(tandem.load_defaults()["claude_model"], "opus")  # cancel saves nothing


SCREEN_TOKEN = re.compile(r"\x1b\[\??[0-9;]*[A-Za-z~]|\x1b.|.", re.S)


def screen(text):
    """The rows a terminal shows after text, with enough of VT100 for the editor's redraws (no wrapping)."""
    rows, r, c = [""], 0, 0
    for tok in SCREEN_TOKEN.findall(text):
        if tok == "\r":
            c = 0
        elif tok == "\n":  # a pty sends \r\n; StringIO output only \n
            r, c = r + 1, 0
            rows += [""] * (r + 1 - len(rows))
        elif tok == "\x08":
            c = max(c - 1, 0)
        elif tok.startswith("\x1b["):
            arg, final = tok[2:-1].lstrip("?"), tok[-1]
            n = int(arg) if arg.isdigit() else 1
            if final == "A":
                r = max(r - n, 0)
            elif final == "B":
                r += n
                rows += [""] * (r + 1 - len(rows))
            elif final == "C":
                c += n
            elif final == "D":
                c = max(c - n, 0)
            elif final == "H":
                r, c = 0, 0
            elif final == "G":
                c = n - 1
            elif final in "KJ":
                rows[r] = rows[r][:c]
                if final == "J":
                    del rows[r + 1:]
        elif tok.startswith("\x1b") or ord(tok) < 32:
            pass
        else:
            line = rows[r].ljust(c)
            rows[r] = line[:c] + tok + line[c + 1:]
            c += 1
    return rows


BAR_ROW = re.compile(r"^(claude \S+/\S+ |turn \d+/\d+ \u00b7 )")


def without_bar(rows):
    """The rows above the status bar, which the session draws under the prompt and its menu."""
    end = len(rows)
    while end and BAR_ROW.match(rows[end - 1]):
        end -= 1
    return rows[:end]


class BarModels(unittest.TestCase):
    """Tab on an empty prompt edits the models on the status bar."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = pathlib.Path(temp.name)
        cache = self.dir / "models_cache.json"
        cache.write_text(json.dumps(CODEX_CACHE))
        for patch in (mock.patch.object(tandem, "CODEX_MODELS_CACHE", cache),
                      mock.patch.object(tandem, "AVAILABILITY", self.dir / "availability.json")):
            patch.start()
            self.addCleanup(patch.stop)
        self.saved = []

    def editor(self, current=None, keys=()):
        editor = tandem.LineEditor(None, io.StringIO(), self.dir / "history", keys=iter(keys))
        editor.models = lambda: tandem.model_choices(dict(current or {}))
        editor.save_models = lambda changed, values: self.saved.append((changed, values))
        return editor

    def keys(self, editor, *keys):
        for key in keys:
            self.assertIsNone(editor.apply(key))

    def test_tab_on_an_empty_line_enters_and_tab_with_text_completes(self):
        editor = self.editor()
        self.keys(editor, "tab")
        self.assertEqual(editor.editing(), (dict.fromkeys(tandem.TUNABLE), "claude_model"))
        editor = self.editor()
        self.keys(editor, "/mo", "tab")
        self.assertEqual((editor.buffer, editor.fields), ("/models ", None))
        editor = self.editor()
        self.keys(editor, "fix", "tab")
        self.assertEqual((editor.buffer, editor.fields), ("fix", None))
        editor = self.editor()
        editor.models = lambda: None  # no bar to edit: Tab stays a no-op
        self.keys(editor, "tab")
        self.assertIsNone(editor.editing())

    def test_tab_and_shift_tab_wrap_through_the_fields(self):
        editor = self.editor()
        self.keys(editor, "tab")
        seen = []
        for _ in range(5):
            seen.append(editor.editing()[1])
            self.keys(editor, "tab")
        self.assertEqual(seen, list(tandem.TUNABLE) + ["claude_model"])
        self.keys(editor, "shift-tab", "shift-tab")
        self.assertEqual(editor.editing()[1], "codex_effort")

    def test_up_and_down_wrap_through_the_values(self):
        editor = self.editor({"claude_model": "opus"})
        self.keys(editor, "tab")

        def shown():
            return editor.editing()[0]["claude_model"]
        self.keys(editor, "up")
        self.assertEqual(shown(), "sonnet")
        self.keys(editor, "up", "up")
        self.assertIsNone(shown())  # past haiku back to default
        self.keys(editor, "down")
        self.assertEqual(shown(), "haiku")
        self.keys(editor, "tab", "down")
        self.assertEqual(editor.editing()[0]["claude_effort"], tandem.CLAUDE_EFFORTS[-1])

    def test_codex_model_keeps_a_supported_effort_and_resets_another(self):
        editor = self.editor({"codex_model": "gpt-big", "codex_effort": "low"})
        self.keys(editor, "tab", "tab", "tab", "up")
        self.assertEqual(editor.editing(), (dict(claude_model=None, claude_effort=None, codex_model="gpt-small",
                                                 codex_effort="low"), "codex_model"))
        editor = self.editor({"codex_model": "gpt-big", "codex_effort": "high"})
        self.keys(editor, "tab", "tab", "tab", "up")
        self.assertEqual(editor.editing()[0]["codex_effort"], None)  # gpt-small has no high
        self.keys(editor, "tab", "up")
        self.assertEqual(editor.editing()[0]["codex_effort"], "low")  # only gpt-small's levels on offer

    def test_enter_saves_only_what_changed(self):
        current = {"claude_model": "opus", "codex_effort": "high"}  # say the run's own, not the defaults
        editor = self.editor(current, ["tab", "enter", "enter"])
        self.assertEqual(editor.read("tandem> "), "")  # the second Enter submits the empty line
        self.assertEqual(self.saved, [])
        editor = self.editor(current, ["tab", "tab", "up", "enter", "enter"])
        editor.read("tandem> ")
        values = dict(current, claude_effort=tandem.CLAUDE_EFFORTS[0], codex_model=None)
        self.assertEqual(self.saved, [({"claude_effort": tandem.CLAUDE_EFFORTS[0]}, values)])
        self.assertIsNone(editor.editing())

    def test_session_save_passes_the_change_on(self):
        session = mock.Mock(busy=lambda: True)
        changed, values = {"claude_effort": "high"}, dict.fromkeys(tandem.TUNABLE, None)
        values["claude_effort"] = "high"
        with mock.patch.object(tandem, "save_defaults") as defaults, \
                mock.patch.object(tandem, "write_tuning") as tuning, \
                mock.patch.object(tandem, "emit") as emit, \
                mock.patch.dict(tandem.CURRENT, {"target": self.dir}):
            tandem.Session.save_models(session, changed, values)
        defaults.assert_called_once_with({"claude_effort": "high"})
        tuning.assert_called_once_with(self.dir, {"claude_effort": "high"})
        emit.assert_called_once_with("saved: claude default/high · codex default/default (applies from the next turn)")

    def test_esc_restores_and_other_keys_leave_then_act(self):
        editor = self.editor({"claude_model": "opus"}, ["tab", "up", "esc", "enter"])
        self.assertEqual(editor.read("tandem> "), "")
        self.assertEqual(self.saved, [])
        self.assertIsNone(editor.editing())
        editor = self.editor({}, ["tab", "up", "f", "i", "x", "enter"])
        self.assertEqual(editor.read("tandem> "), "fix")  # the first character is not lost
        self.assertEqual(self.saved, [])
        editor = self.editor({}, ["tab", "up", ("paste", "a goal"), "enter"])
        self.assertEqual(editor.read("tandem> "), "a goal")
        for key, error in (("ctrl-c", KeyboardInterrupt), ("ctrl-d", EOFError)):
            with self.assertRaises(error):
                self.editor({}, ["tab", "up", key]).read("tandem> ")
        self.assertEqual(self.saved, [])

    def test_bar_shows_the_focus_without_color(self):
        values = {"claude_model": "opus", "claude_effort": "high", "codex_model": None, "codex_effort": None}
        idle = tandem.status_bar(None, 0, 200, False, progress={}, editing=(values, "claude_effort"),
                                 hint=tandem.BAR_EDIT_HINT)
        self.assertEqual(idle, ["claude opus/[high]   codex default/default", tandem.BAR_EDIT_HINT])
        colored = tandem.status_bar(None, 0, 200, True, progress={}, editing=(values, "codex_model"))[0]
        self.assertIn("\x1b[7mdefault\x1b[0m", colored)
        self.assertNotIn("[", plain(colored).replace("\x1b", ""))
        state = StatusBar.state(self)
        rows = tandem.status_bar(state, 0, 200, False, progress={}, editing=(values, "codex_model"),
                                 hint=tandem.BAR_EDIT_HINT)
        self.assertEqual(len(rows), 3)  # the run's two rows, and the keys on their own
        self.assertIn("codex [default]/default", rows[0])
        self.assertEqual(rows[2], tandem.BAR_EDIT_HINT)
        self.assertEqual(len(tandem.status_bar(state, 0, 200, False, progress={})), 2)

    def test_narrow_bar_keeps_each_edit_field_visible_during_a_turn(self):
        values = dict(claude_model="opus", claude_effort="medium",
                      codex_model="gpt-6-astra", codex_effort="medium")
        state = StatusBar.state(self)
        for width in (79, 39):
            for key in tandem.TUNABLE:
                with self.subTest(width=width, key=key):
                    rows = tandem.status_bar(state, 10, width, False,
                                             progress={"agent": "claude", "since": 1},
                                             editing=(values, key), hint=tandem.BAR_EDIT_HINT)
                    self.assertTrue(any("[" + values[key] + "]" in row for row in rows))
                    self.assertTrue(all(tandem.display_width(row) <= width for row in rows))
                    self.assertTrue(any("working" in row for row in rows))

    def test_console_bar_keeps_the_mode_across_redraws(self):
        console = tandem.Console(fancy=False)
        editor = self.editor({"claude_model": "opus"})
        console.attach(editor)
        editor.models = lambda: tandem.model_choices({"claude_model": "opus"})
        with mock.patch.dict(tandem.CURRENT, clear=True), \
                mock.patch.object(tandem, "load_defaults", return_value={"claude_model": "opus"}), \
                mock.patch.object(tandem, "term_width", return_value=120):
            self.assertTrue(console.bar(120)[0].endswith("· Tab or /models to change"))
            self.keys(editor, "tab", "tab", "up")
            for _ in range(2):  # the ticker redraws the bar; the unsaved value and focus stay
                editor.resized = True
                rows = screen(editor.redraw())
                self.assertIn("claude opus/[low]", rows[-2])
                self.assertEqual(rows[-1], tandem.BAR_EDIT_HINT)
            self.keys(editor, "esc")
            self.assertTrue(console.bar(120)[0].startswith("claude opus/default "))
            self.keys(editor, "x")
            self.assertTrue(console.bar(120)[0].endswith("· /models to change"))
            tandem.save(self.dir, StatusBar.state(self))
            tandem.CURRENT["target"] = self.dir
            self.assertNotIn("change", "".join(console.bar(200)))  # typing: Tab completes, no hint
            editor.buffer = ""
            first, second = console.bar(200)
            self.assertTrue(second.endswith("check – · Tab or /models to change"), second)
            self.assertNotIn("change", first)


class Editor(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.history = pathlib.Path(temp.name) / "history"

    def editor(self, *keys, **kw):
        self.out = io.StringIO()
        return tandem.LineEditor(None, self.out, self.history, keys=iter(keys), **kw)

    def read(self, *keys, **kw):
        return self.editor(*keys, **kw).read("tandem> ")

    def test_resize_clears_reflowed_rows_before_redrawing(self):
        editor = self.editor(color=True)
        editor.prompt = "tandem> "
        editor.buffer, editor.cursor = "x" * 65, 65
        editor.header = lambda: ["turn 3: Codex working... 0m05s"]
        editor.footer_lines = lambda width: tandem.status_bar(None, 0, width - 1, True)
        with mock.patch.object(tandem, "term_width", return_value=100):
            editor.redraw()
        # Reflow differs between terminals. Start from arbitrary wrapped rows and cursor position.
        wrapped = "old header\nwrapped header\nold prompt\nwrapped prompt\nold bar\n"
        editor.resized = True
        with mock.patch.object(tandem, "term_width", return_value=30):
            update = editor.redraw()
            self.assertTrue(update.startswith("\x1b[H\x1b[J"))
            rows = screen(wrapped + update)
            self.assertEqual(len(rows), 3)
            self.assertTrue(rows[1].startswith("tandem> "))
            self.assertTrue(rows[2].startswith("claude "))
            self.assertTrue(all(tandem.display_width(row) < 30 for row in rows))
            self.assertFalse(editor.resized)
            self.assertFalse(editor.redraw().startswith("\x1b[H"))

    def test_slash_matches(self):
        def names(buffer):
            return [m[0] for m in tandem.slash_matches(buffer)]
        self.assertEqual(names("/m")[0], "models")
        self.assertEqual(names("/"), [c[0] for c in tandem.SLASH][:8])
        self.assertEqual(names("/s")[:3], ["status", "say", "stop"])  # prefix matches first, in table order
        self.assertEqual(names("/say hi"), [])
        self.assertEqual(names("hello"), [])
        self.assertEqual(names("/oar"), ["board"])  # no prefix match: the names containing it

    def test_slash_table_is_the_single_source(self):
        self.assertEqual(tandem.SLASH_COMMANDS, [c[0] for c in tandem.SLASH])
        self.assertIn(("models", "", "pick Claude and Codex model and effort"), tandem.SLASH)
        help_text = tandem.SLASH_HELP
        self.assertTrue(help_text.startswith("type a goal to start a run in this repo; while it runs"))
        for line in ("  /help                show this list",
                     "  /models              pick Claude and Codex model and effort (saved as the default;\n"
                     "                       a running run takes it from its next turn)\n"
                     "  Tab                  on an empty line: change them on the status bar, ↑↓ and Enter",
                     "  /say TEXT            leave a message the next turn reads\n"
                     "  Ctrl+V               paste the clipboard's image into the line as [image #N]\n"
                     "  /paste               attach the clipboard's image to your next goal or /say\n"
                     "                       (or drag image files onto the terminal; /paste clear drops them)",
                     "  /resume [TURNS]      continue the last run, optionally allowing TURNS more",
                     "  /quit                leave (also Ctrl+D, or Ctrl+C twice)"):
            self.assertIn(line, help_text)
        self.assertTrue(help_text.endswith("(also Ctrl+D, or Ctrl+C twice)"))

    def test_display_width(self):
        for text, width in (("abc", 3), ("ż", 1), ("漢字", 4), ("é", 1), ("", 0)):
            self.assertEqual(tandem.display_width(text), width, text)

    def test_editor_keys(self):
        self.assertEqual(self.read("fix", "left", "left", "X", "end", "enter"), "fXix")
        self.assertEqual(self.read("one two", "ctrl-w", "enter"), "one ")
        self.assertEqual(self.read("abc", "ctrl-a", "ctrl-k", "enter"), "")
        self.assertEqual(self.read("abc", "home", "delete", "end", "backspace", "enter"), "b")
        self.assertEqual(self.read("abcd", "left", "ctrl-u", "ctrl-e", "x", "enter"), "dx")
        self.assertEqual(self.read("a", "shift-tab", "\x1b[5~", "\x01", "enter"), "a")  # unused keys type nothing
        with self.assertRaises(EOFError):
            self.read("ctrl-d")
        with self.assertRaises(KeyboardInterrupt):
            self.read("ab", "ctrl-c")
        self.assertTrue(screen(self.out.getvalue())[-1].startswith("tandem> ab"))  # the line stays, no menu

    def test_answers_can_stay_out_of_history(self):
        editor = self.editor("y", "enter")
        self.assertEqual(editor.read("[y/N] ", history=False), "y")
        self.assertEqual(editor.history, [])

    def test_empty_enter_does_nothing(self):
        editor = self.editor("enter", " ", "enter", "x", "enter")
        self.assertEqual([editor.read("tandem> ") for _ in range(3)], ["", " ", "x"])
        self.assertEqual(screen(self.out.getvalue()), ["tandem> x", ""])
        self.assertEqual(self.history.read_text(), "x\n")  # blank lines are not history

    def test_menu_select_and_run(self):
        self.assertEqual(self.read("/", "m", "enter"), "/models")
        second = tandem.slash_matches("/")[1][0]
        self.assertEqual(self.read("/", "down", "tab", "enter").strip(), "/" + second)
        self.assertEqual(self.read("/", "up", "enter"), "/" + tandem.slash_matches("/")[-1][0])  # wraps
        self.assertEqual(self.read("/m", "esc", "enter"), "/m")  # Esc closed the menu
        self.assertEqual(self.read("/say", "enter"), "/say")  # an exact command runs as typed
        self.assertEqual(self.read("/st", "tab", "hi", "enter"), "/status hi")
        self.assertEqual(self.read("/go", "right", "x", "enter"), "/goal x")  # → at the end completes too
        self.assertEqual(self.read("/go", "left", "right", "enter"), "/goal")  # mid-line → only moves

    def test_menu_lines(self):
        editor = self.editor()
        editor.prompt, editor.buffer, editor.cursor = "tandem> ", "/mo", 3
        lines = editor.menu_lines(80)
        self.assertEqual(lines, ["▸ /models   pick Claude and Codex model and effort",
                                 "  ↑↓ select · Tab or → complete · Enter run · Esc close"])
        self.assertEqual(lines[-1], "  ↑↓ select · Tab or → complete · Enter run · Esc close")
        self.assertNotIn("\x1b", "".join(lines))
        editor.buffer, editor.cursor = "/", 1
        lines = editor.menu_lines(30)
        self.assertEqual(len(lines), 9)  # 8 commands and the hint
        self.assertTrue(all(tandem.display_width(line) < 30 for line in lines))
        self.assertTrue(lines[1].startswith("  /status"))
        color = self.editor(color=True)
        color.prompt, color.buffer, color.cursor = "tandem> ", "/mo", 3
        lines = color.menu_lines(80)
        self.assertEqual(lines[0], "\x1b[7m  /models   pick Claude and Codex model and effort\x1b[0m")
        self.assertTrue(lines[-1].startswith("\x1b[2m"))
        editor.buffer = "/say hi"
        self.assertEqual(editor.menu_lines(80), [])

    def test_history_up_down_and_file(self):
        editor = self.editor("a", "enter", "b", "enter", "up", "up", "up", "enter", "x", "up", "down", "enter")
        self.assertEqual([editor.read("> ") for _ in range(4)], ["a", "b", "a", "x"])
        self.assertEqual(self.history.read_text(), "a\nb\na\nx\n")
        again = self.editor("up", "enter", "up", "enter")
        self.assertEqual([again.read("> ") for _ in range(2)], ["x", "x"])  # same as the last: not stored twice
        self.assertEqual(self.history.read_text(), "a\nb\na\nx\n")
        self.history.write_text("".join(f"{i}\n" for i in range(600)))
        many = self.editor("new", "enter")
        many.read("> ")
        lines = self.history.read_text().splitlines()
        self.assertEqual((len(lines), lines[0], lines[-1]), (500, "101", "new"))

    def test_recalled_command_does_not_open_the_menu(self):
        self.history.write_text("/st\n")
        self.assertEqual(self.read("up", "enter"), "/st")  # an open menu would have made it /status
        self.assertEqual(self.read("up", "down", "x", "enter"), "x")

    def test_wide_chars_and_long_line_keep_cursor_visible(self):
        editor = self.editor()
        editor.prompt, editor.buffer = "tandem> ", "ż漢" * 25
        for cursor in (50, 25, 0, 49):
            editor.cursor = cursor
            text, col = editor.prompt_line(20)
            self.assertLessEqual(tandem.display_width(text), 20, cursor)
            self.assertLessEqual(col, 20, cursor)
            self.assertTrue(text.startswith("tandem> "))
            before = editor.buffer[editor.scroll:cursor]
            self.assertEqual(col, 8 + tandem.display_width(before))
            self.assertTrue(text[8:].startswith(before))  # the cursor's place is on screen
        with mock.patch.object(tandem, "term_width", return_value=20):
            editor.buffer, editor.cursor = "/" + "ż" * 40, 41
            rows = plain(editor.render()).split("\x1b[K")
        self.assertTrue(all(tandem.display_width(re.sub(r"\x1b\[[0-9;]*[A-Za-z]|[\r\n]", "", r)) < 20
                            for r in rows))

    def test_ctrl_v_inserts_or_rings(self):
        paste = mock.Mock(side_effect=["[image #1] ", None])
        self.assertEqual(self.read("fix", "ctrl-v", "ctrl-v", "enter", paste=paste), "fix [image #1] ")
        self.assertEqual(paste.call_count, 2)
        self.assertIn("\a", self.out.getvalue())

    def test_bracketed_paste(self):
        self.assertEqual(self.read("a ", ("paste", "line1\nline2\r\nline3\x1b[31m"), "enter"),
                         "a line1 line2 line3 [31m")
        self.assertTrue(self.out.getvalue().startswith("\x1b[?2004h"))
        self.assertIn("\x1b[?2004l", self.out.getvalue())

    def test_read_key_maps_editing_keys(self):
        for raw, key in ((b"\x7f", "backspace"), (b"\x08", "backspace"), (b"\x1b[3~", "delete"),
                         (b"\x1b[H", "home"), (b"\x1b[F", "end"), (b"\x1bOH", "home"), (b"\x01", "ctrl-a"),
                         (b"\x05", "ctrl-e"), (b"\x15", "ctrl-u"), (b"\x0b", "ctrl-k"), (b"\x17", "ctrl-w"),
                         (b"\x04", "ctrl-d"), (b"\x16", "ctrl-v"),
                         (b"\x1b[200~one\nz\xc5\xbc\x1b[201~", ("paste", "one\nzż"))):
            r, w = os.pipe()
            self.addCleanup(os.close, r)
            os.write(w, raw)
            os.close(w)
            with self.subTest(raw=raw):
                self.assertEqual(tandem.read_key(r), key)

    def test_console_redraws_the_line_around_a_log(self):
        console = tandem.Console(fancy=False)
        editor = self.editor()
        console.attach(editor)
        keys, seen = iter(["a", "b", "log", "look"]), []

        def key():
            k = next(keys)
            if k == "log":  # a worker logs and the status ticks while the owner types
                tandem.emit("a log line")
                console.tick()
                return "c"
            if k == "look":
                seen.append(screen(self.out.getvalue()))
                return ""  # stdin closed: EOFError
            return k

        editor.next_key = key
        progress = dict(turn=2, agent="codex", since=time.time() - 5)
        with mock.patch.object(tandem, "CONSOLE", console), mock.patch.dict(tandem.PROGRESS, progress):
            console.running = True
            with self.assertRaises(EOFError):
                console.ask("tandem> ")
            self.assertNotIn("\x1b7", self.out.getvalue())  # never save/restore cursor
            self.assertEqual(without_bar(seen[0])[-3:], ["a log line", "turn 2: Codex working... 0m05s", "tandem> abc"])
            self.assertEqual(sum("working..." in r for r in seen[0]), 1)
            tandem.emit("")  # what the session prints after Ctrl+C or Ctrl+D: it only ends the line
            tandem.emit("after")
        self.assertEqual(screen(self.out.getvalue())[-4:], ["a log line", "tandem> abc", "after", ""])

    @unittest.skipUnless(os.name == "posix", "raw keys are POSIX")
    def test_raw_mode_is_restored(self):
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        termios = tandem.termios

        def attrs():
            a = termios.tcgetattr(slave)
            a[3] &= ~getattr(termios, "PENDIN", 0)
            return a

        before = attrs()
        for failure in (None, KeyboardInterrupt, RuntimeError):
            keys = iter(["a", failure or "enter"])

            def key(fd):
                lflag = termios.tcgetattr(fd)[3]
                self.assertFalse(lflag & (termios.ICANON | termios.ECHO | termios.ISIG))
                k = next(keys)
                if not isinstance(k, str):
                    raise k
                return k

            editor = tandem.LineEditor(slave, io.StringIO(), self.history)
            with self.subTest(failure=failure), mock.patch.object(tandem, "read_key", key):
                if failure is None:
                    self.assertEqual(editor.read("> "), "a")
                else:
                    with self.assertRaises(failure):
                        editor.read("> ")
                self.assertEqual(attrs(), before)

    def test_signal_right_after_raw_mode_restores_the_terminal(self):
        # A Ctrl+C sent as a signal (kill, or the terminal before raw mode is on) can land the moment
        # raw mode is set; the owner's shell must not be left without echo.
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        termios = tandem.termios

        def attrs():
            a = termios.tcgetattr(slave)
            a[3] &= ~getattr(termios, "PENDIN", 0)
            return a

        before = attrs()
        real = tandem.raw_keys

        def raw_then_sigint(fd):
            saved = real(fd)
            os.kill(os.getpid(), signal.SIGINT)
            return saved

        editor = tandem.LineEditor(slave, io.StringIO(), self.history)
        with mock.patch.object(tandem, "raw_keys", raw_then_sigint), self.assertRaises(KeyboardInterrupt):
            editor.read("> ")
            time.sleep(1)  # where the signal lands without the fix: after read returned
        self.assertEqual(attrs(), before)


class SessionLook(unittest.TestCase):
    """The session restyles its own output; everything else keeps the dated lines scripts parse."""

    def console(self, fancy=True, styled=True):
        return tandem.Console(fancy=fancy, styled=styled, utf=True)

    def history(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return pathlib.Path(temp.name) / "history"

    def lines(self, console, fn, width=80):
        out = []
        with mock.patch.object(tandem, "CONSOLE", console), mock.patch.object(tandem, "term_width", return_value=width), \
                mock.patch.object(tandem, "emit", side_effect=out.append):
            fn()
        return "\n".join(out).split("\n")

    def test_session_log_has_a_short_stamp_and_no_date(self):
        rows = self.lines(self.console(), lambda: tandem.log("hello", "warn"))
        self.assertEqual(len(rows), 1)
        self.assertRegex(plain(rows[0]), r"^\d\d:\d\d ! hello$")
        self.assertIn("\x1b[33mhello", rows[0])  # a warning keeps its color, it is not dimmed
        self.assertNotRegex(rows[0], r"\d{4}-\d\d-\d\d")

    def test_log_without_a_session_is_unchanged(self):
        for console in (None, self.console(fancy=False, styled=False)):
            out = io.StringIO()
            with mock.patch.object(tandem, "CONSOLE", console), mock.patch("sys.stdout", out), \
                    mock.patch.object(tandem, "now", return_value="2026-10-24 07:16:12"):
                tandem.log("turn 1: Claude Code working...", "turn", brief="turn 1 · Claude Code")
                tandem.log("talk to the agents: x", brief="")
            self.assertEqual(out.getvalue(), "[2026-10-24 07:16:12] turn 1: Claude Code working...\n"
                                             "[2026-10-24 07:16:12] talk to the agents: x\n")

    def test_brief_replaces_or_hides_the_line_in_the_session(self):
        rows = self.lines(self.console(), lambda: (tandem.log("long", "turn", brief="turn 1 · Codex"),
                                                   tandem.log("talk to the agents: x", brief="")))
        self.assertEqual(len(rows), 1)
        self.assertTrue(plain(rows[0]).endswith("· turn 1 · Codex"))

    def test_submitted_line_is_redrawn_as_the_owners(self):
        for fancy, marked in ((True, f"\x1b[{tandem.OWNER_STYLE}m\u203a /goal x.md\x1b[K\x1b[0m\n"),
                              (False, "\u203a /goal x.md\n")):
            console = self.console(fancy=fancy)
            out = io.StringIO()
            editor = tandem.LineEditor(None, out, self.history(), keys=iter(list("/goal x.md") + ["enter"]),
                                       color=fancy)
            console.attach(editor)
            editor.footer_lines = lambda width: []
            with mock.patch.object(tandem, "CONSOLE", console):
                self.assertEqual(editor.read("tandem> "), "/goal x.md")
            self.assertIn(marked, out.getvalue())
            self.assertNotIn("tandem> /goal", out.getvalue().split("\x1b[J")[-1])

    def test_plain_editor_keeps_the_prompt(self):
        out = io.StringIO()
        editor = tandem.LineEditor(None, out, self.history(), keys=iter(list("abc") + ["enter"]))
        self.assertEqual(editor.read("tandem> "), "abc")
        self.assertTrue(out.getvalue().endswith("tandem> abc\n\x1b[?2004l"))

    def test_run_rules_fit_the_width_and_close_in_the_outcome_color(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = pathlib.Path(d)
            state = {"name": "bar-controls-" + "x" * 80, "history": []}
            for width in (40, 120):
                with mock.patch.object(tandem, "save"), mock.patch.object(tandem, "append_transcript"):
                    rows = self.lines(self.console(), lambda: tandem.finish(run_dir, state, "failed", "Both failed."),
                                      width)
                self.assertTrue(plain(rows[-1]).startswith("\u2500\u2500 failed \u00b7 bar-controls"))
                self.assertEqual(tandem.display_width(plain(rows[-1])), width - 1)
                self.assertIn("\x1b[31mfailed", rows[-1])
                self.assertIn("transcript transcript.md", plain(rows[-2]))
                self.assertNotIn("FINISHED", "".join(rows))

    def test_long_path_is_shortened_and_never_split(self):
        home = str(pathlib.Path.home())
        path = home + "/" + "/".join(["a-rather-long-directory-name"] * 4) + "/transcript.md"
        console = self.console()
        rows = self.lines(console, lambda: tandem.log("transcript " + tandem.show_path(path)), 60)
        short = "~/" + path[len(home) + 1:]
        self.assertEqual(len(rows), 1)
        self.assertEqual(tandem.display_width(rows[0]), 59)
        self.assertEqual(plain(rows[0])[8:], ("transcript " + short)[:59 - 8 - 1] + "\u2026")
        with mock.patch.object(tandem, "CONSOLE", None):
            self.assertEqual(tandem.show_path(path), path)  # whole for scripts
        run = pathlib.Path(home) / "runs" / "r1"
        with mock.patch.object(tandem, "CONSOLE", console):
            self.assertEqual(tandem.show_path(run / "transcript.md", run), "transcript.md")
            self.assertEqual(tandem.show_path("/elsewhere/x"), "/elsewhere/x")

    def test_long_logs_fit_one_row_and_keep_error_color(self):
        for utf, more in ((True, "…"), (False, "...")):
            console = tandem.Console(fancy=True, styled=True, utf=utf)
            rows = self.lines(console, lambda: tandem.log("界 word " * 30, "error"), 40)
            self.assertEqual(len(rows), 1)
            self.assertLessEqual(tandem.display_width(rows[0]), 39)
            self.assertTrue(plain(rows[0]).endswith(more))
            self.assertIn("\x1b[31m界", rows[0])
            self.assertTrue(rows[0].endswith(more + "\x1b[0m"))

    def test_talk_hint_only_outside_the_session(self):
        args = argparse.Namespace(repo=".", goal_file=None, goal="g", name="n", base="HEAD", check=None,
                                  max_turns=3, first=None)
        for console, expected in ((None, True), (self.console(), False)):
            with tempfile.TemporaryDirectory() as d, mock.patch.object(tandem, "RUNS", pathlib.Path(d)), \
                    mock.patch.object(tandem, "require_repo", return_value=pathlib.Path(d)), \
                    mock.patch.object(tandem, "config_from", return_value={}), \
                    mock.patch.object(tandem, "create_run", side_effect=lambda path, *a: path), \
                    mock.patch.dict(tandem.CURRENT):
                rows = self.lines(console, lambda: tandem.prepare_run(args))
            self.assertEqual(any("talk to the agents" in r for r in rows), expected)

    def test_no_color_keeps_the_marker_and_rules(self):
        console = self.console(fancy=False)
        rows = self.lines(console, lambda: (tandem.log("pushed", "ok"), emit_rule(console)))
        self.assertNotIn("\x1b", "".join(rows))
        self.assertRegex(rows[0], r"^\d\d:\d\d \u2713 pushed$")
        self.assertTrue(rows[1].startswith("\u2500\u2500 done \u00b7 x \u2500"))
        self.assertEqual(console.owner_line("hi"), "\u203a hi")
        ascii_console = tandem.Console(fancy=False, styled=True, utf=False)
        self.assertEqual(ascii_console.owner_line("hi"), "> hi")
        with mock.patch.object(tandem, "term_width", return_value=30):
            self.assertEqual(ascii_console.rule("done"), "-- done " + "-" * 21)

    def test_no_color_turn_line_marks_an_error(self):
        entry = {"turn": 2, "agent": "codex", "status": "error", "seconds": 5, "commit": None, "error": "boom"}
        rows = self.lines(self.console(fancy=False), lambda: tandem.report_turn(entry))
        self.assertRegex(rows[0], r"^\d\d:\d\d \u2717 turn 2: Codex -> error")

    def test_campaign_resume_opens_with_a_rule(self):
        args = argparse.Namespace(push=None, codex_effort=None, codex_role=None, seed_review=None)
        for console, expected in ((None, 0), (self.console(), 1)):
            with tempfile.TemporaryDirectory() as d, mock.patch.object(tandem, "refuse_if_running"), \
                    mock.patch.dict(tandem.CURRENT):
                args.dir = d
                tandem.write_json(pathlib.Path(d) / "campaign.json",
                                  {"config": {}, "branch": "tandem/campaign-x", "current": None})
                rows = self.lines(console, lambda: tandem.prepare_campaign_resume(args))
            self.assertEqual(sum("tandem/campaign-x \u00b7 resumed" in plain(r) for r in rows), expected)


def emit_rule(console):
    tandem.emit(console.rule("done \u00b7 x", tandem.OUTCOME_COLORS["done"]))


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


class Defaults(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        p = mock.patch.dict(os.environ, {"HOME": self.home.name})
        p.start()
        self.addCleanup(p.stop)

    def test_saved_defaults_fill_only_missing_flags(self):
        tandem.record_consent()
        tandem.save_defaults({"claude_model": "opus", "codex_effort": "high", "bogus": 1})
        args = tandem.build_parser().parse_args(["start", "--goal", "g", "--claude-model", "sonnet"])
        cfg = tandem.config_from(args)
        self.assertEqual((cfg["claude_model"], cfg["codex_effort"], cfg["claude_effort"]), ("sonnet", "high", None))
        self.assertTrue(tandem.consent_recorded())  # other keys survive
        tandem.save_defaults({"claude_model": None})
        self.assertEqual(tandem.load_defaults(), {"codex_effort": "high"})

    def test_broken_config_means_no_defaults(self):
        tandem.config_path().parent.mkdir(parents=True)
        tandem.config_path().write_text("[1, 2")
        self.assertEqual(tandem.load_defaults(), {})

    def test_claude_gets_effort(self):
        tmp = pathlib.Path(self.home.name)
        schema = tmp / "schema.json"
        schema.write_text("{}")
        done = subprocess.CompletedProcess([], 0, json.dumps({"structured_output": {"status": "done"}}), "")
        for effort, expected in (("max", True), (None, False)):
            cfg = {"timeout": 5, "claude_permission_mode": "acceptEdits", "claude_effort": effort}
            with mock.patch.object(tandem, "run_child", return_value=done) as run:
                tandem.run_claude(cfg, tmp, "goal", tmp / "c", schema, "status", False)
            cmd = run.call_args[0][0]
            self.assertEqual("--effort" in cmd and cmd[cmd.index("--effort") + 1] == "max", expected)


class Images(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        patcher = mock.patch.object(tandem, "IMAGES", self.dir / "images")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.shot = self.dir / "Screen Shot 1.png"
        self.shot.write_bytes(PNG)

    def session(self):
        return tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))

    def test_dropped_paths_are_cut_from_the_line(self):
        other = self.dir / "it's.jpg"
        other.write_bytes(PNG)
        escaped = str(self.shot).replace(" ", "\\ ")
        self.assertEqual(tandem.take_dropped_images(f"fix this {escaped} now"), ("fix this now", [self.shot]))
        self.assertEqual(tandem.take_dropped_images(f"'{self.shot}' \"{other}\""), ("", [self.shot, other]))
        self.assertEqual(tandem.take_dropped_images(f"/say look {escaped}"), ("/say look", [self.shot]))
        notes = self.dir / "notes.md"
        notes.write_text("x")
        for line in (f"read {notes}", "/say  keep   spacing", f"x{escaped}", "/nope/missing.png"):
            self.assertEqual(tandem.take_dropped_images(line), (line, []))

    def test_prompt_images_are_existing_images_once_each(self):
        notes = self.dir / "notes.md"
        notes.write_text("x")
        prompt = f"[image: {self.shot}] [image: /gone.png] [image: {notes}] again [image: {self.shot}]"
        self.assertEqual(tandem.prompt_images(prompt), [self.shot])

    def test_agents_get_the_images_before_their_variadic_flags_end(self):
        cfg = {"timeout": 5, "claude_permission_mode": "acceptEdits"}
        schema = self.dir / "schema.json"
        schema.write_text("{}")
        prompt = f"goal\n\n[image: {self.shot}]"
        done = subprocess.CompletedProcess([], 0, json.dumps({"structured_output": {"status": "done"}}), "")
        with mock.patch.object(tandem, "run_child", return_value=done) as run:
            tandem.run_claude(cfg, self.dir, prompt, self.dir / "c", schema, "status", False)
        cmd, given = run.call_args[0][0], run.call_args[1]["input"]
        self.assertEqual(cmd[2:5], ["--add-dir", str(self.dir), "--output-format"])
        self.assertIn("Open it with Read", given)
        with mock.patch.object(tandem, "run_child", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            tandem.run_codex(cfg, self.dir, prompt, self.dir / "x", schema, "status", False)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[2:5], ["--image", str(self.shot), "-C"])
        self.assertEqual(cmd[-1], "-")
        with mock.patch.object(tandem, "run_child", return_value=done) as run:
            tandem.run_claude(cfg, self.dir, "no images", self.dir / "c", schema, "status", False)
        self.assertNotIn("--add-dir", run.call_args[0][0])

    def test_images_wait_for_the_next_goal(self):
        session = self.session()
        with mock.patch.object(tandem, "emit"):
            session.attach([self.shot])
        copy = session.images[0]
        self.assertEqual((copy.parent, copy.read_bytes()), (tandem.IMAGES, PNG))
        session.thread = mock.Mock()
        session.thread.is_alive.return_value = True
        with mock.patch.object(tandem, "prepare_run") as prepare, mock.patch.object(tandem, "emit"):
            session.start_goal("fix the layout")
        prepare.assert_not_called()
        self.assertEqual(session.images, [copy])  # refused while busy: still waiting
        session.thread = None
        run_dir = self.dir / "run"
        run_dir.mkdir()
        (run_dir / "state.json").write_text("{}")
        with mock.patch.object(tandem, "prepare_run", return_value=run_dir) as prepare, \
                mock.patch.object(session, "launch"):
            session.start_goal("fix the layout")
        self.assertEqual(prepare.call_args[0][0].goal, f"fix the layout\n\n[image: {copy}]")
        self.assertEqual(session.images, [])

    def test_say_carries_images_and_paste_clear_drops_them(self):
        session = self.session()
        run = self.dir / "run"
        run.mkdir()
        with mock.patch.dict(tandem.CURRENT, {"target": run}), mock.patch.object(tandem, "emit"):
            session.attach([self.shot])
            session.handle("say", "")
            self.assertIn("[image: ", (run / "inbox.md").read_text())
            self.assertEqual(session.images, [])
            session.attach([self.shot])
            session.handle("paste", "clear")
            self.assertEqual(session.images, [])
            session.handle("say", "")
        self.assertEqual((run / "inbox.md").read_text().count("[image: "), 1)

    def test_paste_saves_the_clipboard_or_says_there_is_none(self):
        def osascript(cmd, **kw):
            pathlib.Path(cmd[-1]).write_bytes(PNG)
            return subprocess.CompletedProcess(cmd, 0)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(subprocess, "run", side_effect=osascript):
            path = tandem.paste_image()
        self.assertEqual((path.parent, path.read_bytes()), (tandem.IMAGES, PNG))
        failed = subprocess.CompletedProcess([], 1)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(ValueError, "no image"):
                tandem.paste_image()
        self.assertEqual(list(tandem.IMAGES.iterdir()), [path])  # nothing half-written left behind
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "drag the image"):
                tandem.paste_image()

    def test_banner_logo_only_on_a_fancy_terminal(self):
        with mock.patch.object(tandem, "agent_login_status", return_value="logged in"), \
                mock.patch.object(tandem, "git", return_value="main"):
            plain = tandem.banner(pathlib.Path("/repo"))
            fancy = tandem.banner(pathlib.Path("/repo"), fancy=True)
        self.assertNotIn("\x1b", plain)
        self.assertIn("claude  logged in", plain)
        self.assertIn("type a goal, Ctrl+V to paste an image, /models, or /help", plain)
        self.assertIn("▀█▀", fancy)
        self.assertIn("type a goal · Ctrl+V pastes an image · /models · /help", fancy)
        self.assertIn("\x1b[32m●\x1b[0m logged in", fancy)


class Children(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.dir = pathlib.Path(temp.name)

    def wait_gone(self, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(.05)
        return False

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
    def test_timeout_kills_a_detached_agent_with_its_tools(self):
        pid_file = self.dir / "grandchild"
        script = (f"import pathlib, subprocess, time\n"
                  f"p = subprocess.Popen(['sleep', '30'])\n"
                  f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\ntime.sleep(30)")
        with mock.patch.dict(tandem.CHILD_POPEN, {"start_new_session": True}), \
                self.assertRaises(subprocess.TimeoutExpired):
            tandem.run_child([sys.executable, "-c", script], timeout=1)
        self.assertTrue(self.wait_gone(int(pid_file.read_text())))
        self.assertFalse(tandem.CHILDREN)

    @unittest.skipUnless(os.name == "posix", "needs sh")
    def test_timeout_holds_while_following_a_wrapper_that_keeps_the_pipe(self):
        # Outside the session there is no group: killing the wrapper leaves its program on the pipe.
        pid_file = self.dir / "program"
        cmd = ["sh", "-c", f"sleep 6 & echo $! > {shlex.quote(str(pid_file))}; wait"]
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            tandem.run_child(cmd, input="prompt", timeout=1, on_line=lambda stream, line: None)
        self.assertLess(time.monotonic() - start, 4)
        self.assertFalse(tandem.CHILDREN)
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(pid_file.read_text()), signal.SIGKILL)

    @unittest.skipUnless(hasattr(os, "killpg"), "process groups are POSIX")
    def test_an_agent_that_exits_is_done_although_a_tool_it_left_holds_the_pipe(self):
        pid_file = self.dir / "tool"
        cmd = ["sh", "-c", f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}; echo reply"]
        seen = []
        start = time.monotonic()
        with mock.patch.dict(tandem.CHILD_POPEN, {"start_new_session": True}):
            proc = tandem.run_child(cmd, input="prompt", timeout=20, on_line=lambda s, line: seen.append(line))
        self.assertLess(time.monotonic() - start, tandem.PIPE_GRACE + 3)
        self.assertEqual((proc.returncode, proc.stdout, seen), (0, "reply\n", ["reply"]))
        self.assertTrue(self.wait_gone(int(pid_file.read_text())))  # the leftover tool went with its group

    def test_timeout_bounds_writing_a_prompt_nobody_reads(self):
        # Bigger than any pipe buffer, to a process that never reads stdin.
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            tandem.run_child(cmd, input="x" * 2_000_000, timeout=1, on_line=lambda stream, line: None)
        self.assertLess(time.monotonic() - start, 5)

    def test_signal_children_stops_what_the_worker_waits_on(self):
        results = []
        worker = threading.Thread(target=lambda: results.append(
            tandem.run_child([sys.executable, "-c", "import time; time.sleep(30)"], input="")))
        with mock.patch.dict(tandem.CHILD_POPEN, {"start_new_session": hasattr(os, "killpg")}):
            worker.start()
            deadline = time.monotonic() + 5
            while not tandem.CHILDREN and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(tandem.signal_children(), 1)
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertNotEqual(results[0].returncode, 0)
        self.assertFalse(tandem.CHILDREN)

    @unittest.skipUnless(hasattr(os, "getpgrp"), "process groups are POSIX")
    def test_check_and_git_leave_the_terminal_group_in_a_session(self):
        state = {"worktree": str(self.dir), "config": {"timeout": 30,
                 "check": f"{shlex.quote(sys.executable)} -c 'import os; print(os.getpgrp())'"}}
        with mock.patch.dict(tandem.CHILD_POPEN, {"start_new_session": True}):
            ok, output = tandem.run_check(state)
            with mock.patch.object(subprocess, "run", wraps=subprocess.run) as run:
                tandem.git(self.dir, "--version")
        self.assertTrue(ok, output)
        self.assertNotEqual(int(output), os.getpgrp())
        self.assertTrue(run.call_args.kwargs.get("start_new_session"))


STUB_AGENT = """\
import json, os, pathlib, signal, sys, time
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
if args[:2] in (["auth", "status"], ["login", "status"]):
    print(json.dumps({"loggedIn": True}))
    sys.exit()
if args == ["--version"]:
    time.sleep(float(os.environ.get("STUB_VERSION_SLEEP", "0")))
    print(name, "stub")
    sys.exit()
sys.stdin.read()
if os.environ.get("STUB_ARGS"):
    with open(os.environ["STUB_ARGS"], "a") as f:
        f.write(json.dumps([name, args]) + "\\n")
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(f"{name} start {os.getpid()}\\n")
if os.environ.get("STUB_TOUCH"):
    pathlib.Path(os.environ["STUB_TOUCH"]).write_text("half done")
if os.environ.get("STUB_TERM_DELAY"):  # slow to stop, like a CLI cleaning up
    signal.signal(signal.SIGTERM, lambda *a: (time.sleep(float(os.environ["STUB_TERM_DELAY"])), sys.exit(143)))
time.sleep(float(os.environ.get("STUB_TURN_SLEEP", "0")))
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(f"{name} end\\n")
reply = {"status": "done", "summary": "stub"}
if name == "codex":
    pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(reply))
else:
    print(json.dumps({"structured_output": reply, "total_cost_usd": 0}))
"""


# Starts the command in a new session with the pty on stdin as its controlling terminal, as a shell's
# job would have it: Ctrl+C typed while the session is not reading keys then arrives as SIGINT.
WITH_TERMINAL = ("import fcntl, os, sys, termios\nos.setsid()\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
                 "os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n")


@unittest.skipUnless(os.name == "posix", "the session's signal handling is POSIX")
class SessionProcess(unittest.TestCase):
    """tandem.py as a user runs it, on a pseudo-terminal, with stub agents on PATH."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.base = pathlib.Path(temp.name).resolve()
        self.repo, self.runs, home, bin_dir = (self.base / n for n in ("repo", "runs", "home", "bin"))
        for d in (self.repo, home / ".tandem", bin_dir):
            d.mkdir(parents=True)
        (home / ".tandem" / "config.json").write_text(json.dumps({"consent": {"version": "test"}}))
        for name in ("claude", "codex"):
            (bin_dir / name).write_text(f"#!{sys.executable}\n{STUB_AGENT}")
            (bin_dir / name).chmod(0o755)
        # The clipboard holds an image: osascript writes it to its last argument, xclip to stdout.
        clip = {"osascript": "open(sys.argv[-1], 'wb').write(PNG)", "xclip": "sys.stdout.buffer.write(PNG)"}
        for name, body in clip.items():
            (bin_dir / name).write_text(f"#!{sys.executable}\nimport sys\nPNG = {PNG!r}\n{body}\n")
            (bin_dir / name).chmod(0o755)
        self.log = self.base / "agents.log"
        self.output = []
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOME=str(home),
                        TANDEM_RUNS=str(self.runs), NO_COLOR="1", TERM="xterm-256color", STUB_LOG=str(self.log),
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        self.env.pop("TANDEM_PROG", None)
        self.env.pop("WAYLAND_DISPLAY", None)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True, env=self.env)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=self.repo, check=True, env=self.env)

    def start(self, *args, **env):
        """env overrides the test environment; None removes a variable."""
        import pty
        master, slave = pty.openpty()
        self.output, lock = [], threading.Lock()
        # Its own session, as a terminal's job would be: signals to its group are the terminal's.
        self.proc = subprocess.Popen([sys.executable, "-c", WITH_TERMINAL, "-B", str(ROOT / "tandem.py"),
                                      "--repo", str(self.repo), *args],
                                     stdin=slave, stdout=slave, stderr=slave, cwd=self.repo,
                                     env={k: v for k, v in dict(self.env, **env).items() if v is not None})
        os.close(slave)

        def read():
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                with lock:
                    self.output.append(chunk.decode(errors="replace"))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def cleanup(proc=self.proc):  # this session's process: a test may start several
            for pid in self.agent_pids():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            os.close(master)

        self.addCleanup(cleanup)
        self.master = master

    def text(self):
        return "".join(self.output)

    def wait_for(self, predicate, what, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail(f"never saw {what}; output:\n{self.text()}")
            time.sleep(.05)

    def type(self, line):
        os.write(self.master, (line + "\n").encode())

    def log_text(self):
        return self.log.read_text() if self.log.exists() else ""

    def agent_pids(self):
        return [int(l.split()[2]) for l in self.log_text().splitlines() if " start " in l]

    def state(self):
        return tandem.load(next(d for d in self.runs.iterdir() if (d / "state.json").exists()))

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def start_slow_turn(self, **env):
        self.start(STUB_TURN_SLEEP="60", **env)
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        self.type("make a file")
        self.wait_for(lambda: "claude start" in self.log_text(), "the agent starting")
        return self.agent_pids()[0]

    def check_signal_ends_session(self, sig):
        agent = self.start_slow_turn()
        os.kill(self.proc.pid, sig)  # the terminal closing, or a supervisor; not the agent's group
        self.assertEqual(self.proc.wait(timeout=20), 128 + sig, self.text())
        self.wait_for(lambda: not self.alive(agent), "the agent stopping", timeout=5)
        self.assertNotIn("claude end", self.log_text())
        self.assertEqual(self.state()["outcome"], "stopped")

    def test_hangup_stops_the_agent(self):
        self.check_signal_ends_session(signal.SIGHUP)

    def test_sigterm_stops_the_agent(self):
        self.check_signal_ends_session(signal.SIGTERM)

    def test_ctrl_c_while_quitting_stops_the_agent(self):
        agent = self.start_slow_turn()
        self.type("/quit")
        self.wait_for(lambda: "waiting for the current turn" in self.text(), "the quit wait")
        os.killpg(self.proc.pid, signal.SIGINT)  # Ctrl+C reaches the terminal's foreground group
        self.assertEqual(self.proc.wait(timeout=20), 0, self.text())
        self.assertFalse(self.alive(agent))
        self.assertEqual(self.state()["outcome"], "stopped")
        self.assertNotIn("type a goal, /resume", self.text())

    def test_ctrl_c_at_the_prompt_spares_the_check(self):
        started, done = self.base / "check-started", self.base / "check-done"
        self.start("--check", f"touch {started} && sleep 2 && touch {done}")
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        self.type("make a file")
        self.wait_for(started.exists, "the check starting")
        os.killpg(self.proc.pid, signal.SIGINT)
        self.wait_for(lambda: "press Ctrl+C again to exit" in self.text(), "the exit hint")
        self.wait_for(lambda: "transcript transcript.md" in self.text(), "the run finishing")
        self.assertTrue(done.exists())
        self.assertEqual((self.state()["outcome"], self.state()["turn"]), ("done", 3))
        self.type("/quit")
        self.assertEqual(self.proc.wait(timeout=10), 0, self.text())

    def test_ctrl_c_during_a_command_keeps_the_session(self):
        self.start(STUB_VERSION_SLEEP="5")
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        self.type("/doctor")
        time.sleep(1)
        os.killpg(self.proc.pid, signal.SIGINT)
        self.wait_for(lambda: "interrupted" in self.text(), "the command being interrupted")
        self.type("/help")
        self.wait_for(lambda: "/transcript" in self.text(), "the session answering")
        self.type("/quit")
        self.assertEqual(self.proc.wait(timeout=10), 0, self.text())

    def hints(self):
        return self.text().count("press Ctrl+C again to exit")

    def ctrl_c_at_the_prompt(self):
        """Ctrl+C as a key, once the prompt is drawn, so it reaches the line editor and not the terminal."""
        self.wait_for(self.screen_ends_with_prompt(), "the prompt")
        os.write(self.master, b"\x03")

    def test_double_ctrl_c_on_empty_line_exits(self):
        self.start()
        self.ctrl_c_at_the_prompt()
        self.wait_for(lambda: self.hints() == 1, "the exit hint")
        self.ctrl_c_at_the_prompt()
        self.assertEqual(self.proc.wait(timeout=5), 0, self.text())
        self.assertNotIn("Traceback", self.text())

    def test_single_ctrl_c_clears_and_waits(self):
        self.start()
        self.wait_for(self.screen_ends_with_prompt(), "the prompt")
        os.write(self.master, b"abc")
        self.wait_for(self.screen_ends_with_prompt("tandem> abc"), "the typed text")
        os.write(self.master, b"\x03")  # clears the line, asks nothing
        self.wait_for(self.screen_ends_with_prompt(), "the empty prompt")
        self.assertEqual(self.hints(), 0)
        self.ctrl_c_at_the_prompt()   # so this one, on the empty line, only asks
        self.wait_for(lambda: self.hints() == 1, "the exit hint")
        time.sleep(2.5)                # past the window
        self.ctrl_c_at_the_prompt()
        self.wait_for(lambda: self.hints() == 2, "the exit hint again")
        self.wait_for(self.screen_ends_with_prompt(), "the prompt")
        os.write(self.master, b"x\x7f\x03")  # a key in between disarms it too
        self.wait_for(lambda: self.hints() == 3, "the exit hint a third time")
        self.assertIsNone(self.proc.poll(), self.text())
        self.type("/quit")
        self.assertEqual(self.proc.wait(timeout=10), 0, self.text())

    def test_repeated_ctrl_c_while_quitting_exits_cleanly(self):
        # Owner report: a second Ctrl+C while /quit waited killed tandem by SIGINT.
        agent = self.start_slow_turn(STUB_TERM_DELAY="1")
        self.type("/quit")
        self.wait_for(lambda: "waiting for the current turn" in self.text(), "the quit wait")
        presses = 0
        while self.proc.poll() is None and presses < 100:  # on through the way out, not just the wait
            with contextlib.suppress(OSError):  # the session may have gone since the check
                os.write(self.master, b"\x03")  # the terminal turns each into a SIGINT now
            presses += 1
            time.sleep(.1)
        self.assertGreaterEqual(presses, 3)
        self.assertEqual(self.proc.wait(timeout=20), 0, self.text())
        self.assertNotIn("Traceback", self.text())
        self.assertNotIn("continue with resume", self.text())  # main's own KeyboardInterrupt message
        self.assertFalse(self.alive(agent))
        self.assertEqual(self.text().count("stopping the agent"), 1)

    def run_dirs(self):
        return [d for d in self.runs.iterdir() if d.is_dir()] if self.runs.exists() else []

    def test_exit_word_leaves(self):
        # Owner report: typing `exit` started a run whose goal was "exit".
        for word in ("exit", "quit", " Q "):
            with self.subTest(word=word):
                self.start()
                self.wait_for(self.screen_ends_with_prompt(), "the prompt")
                self.type(word)
                self.assertEqual(self.proc.wait(timeout=10), 0, self.text())
                self.assertEqual(self.run_dirs(), [])
        self.start()
        self.wait_for(self.screen_ends_with_prompt(), "the prompt")
        self.type("exit now please")
        self.wait_for(lambda: "transcript transcript.md" in self.text(), "the run finishing")
        self.assertEqual(self.state()["goal"], "exit now please")
        self.type("/quit")
        self.assertEqual(self.proc.wait(timeout=10), 0, self.text())

    def test_double_ctrl_c_during_a_turn_keeps_the_edits(self):
        agent = self.start_slow_turn(STUB_TOUCH="half.txt")
        self.wait_for(self.run_dirs, "the run")
        self.wait_for(self.screen_ends_with_prompt(), "the prompt during the turn")
        os.write(self.master, b"\x03\x03")
        self.assertEqual(self.proc.wait(timeout=20), 0, self.text())
        self.assertFalse(self.alive(agent))
        self.assertNotIn("claude end", self.log_text())
        state = self.state()
        last = state["history"][-1]
        self.assertEqual(last["status"], "interrupted")
        self.assertEqual(state["failures"], {"claude": 0, "codex": 0})
        self.assertEqual(state["outcome"], "stopped")
        run = self.run_dirs()[0]
        budget = int(re.search(r"Turn 1 of at most (\d+)", (run / "turn-01-claude.prompt.md").read_text()).group(1))
        self.assertEqual(state["max_turns"], budget + 1)  # no turn used up
        self.assertIn("half.txt", tandem.git(self.repo, "show", "--name-only", "--format=%s", last["commit"]))
        self.assertIn("stopped the current turn; its edits are kept. /resume or `tandem resume` continues",
                      self.text())
        self.assertNotIn("Traceback", self.text())

    def test_ctrl_v_puts_the_clipboard_image_in_the_goal(self):
        self.check_ctrl_v()

    def test_ctrl_v_with_readline_on_a_dumb_terminal(self):
        self.check_ctrl_v(TERM="dumb")  # no line editor of our own: readline's completer does it

    def check_ctrl_v(self, **env):
        argv_log = self.base / "argv.log"
        self.start(STUB_ARGS=str(argv_log), **env)
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        os.write(self.master, b"fix the layout in")
        os.write(self.master, b"\x16")
        self.wait_for(lambda: "[image #1]" in self.text(), "the image in the line")
        self.type("please")
        self.wait_for(lambda: "transcript transcript.md" in self.text(), "the run finishing")
        goal = self.state()["goal"]
        image = tandem.IMAGE_MARK.search(goal).group(1)
        self.assertEqual(goal, f"fix the layout in [image: {image}] please")
        self.assertEqual(pathlib.Path(image).parent, self.base / "home" / ".tandem" / "images")
        self.assertEqual(pathlib.Path(image).read_bytes(), PNG)
        calls = dict(json.loads(l) for l in argv_log.read_text().splitlines())
        self.assertEqual(calls["codex"][:3], ["exec", "--image", image])
        self.assertEqual(calls["claude"][:3], ["-p", "--add-dir", str(pathlib.Path(image).parent)])
        self.type("/quit")
        self.assertEqual(self.proc.wait(timeout=10), 0, self.text())

    def test_models_picker_saves_defaults(self):
        codex = self.base / "home" / ".codex"
        codex.mkdir()
        (codex / "models_cache.json").write_text(json.dumps(CODEX_CACHE))
        self.start(NO_COLOR=None, TERM="xterm-256color")
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        self.type("/models")
        self.wait_for(lambda: "Enter save" in self.text(), "the picker")
        self.assertIn("\x1b[?25l", self.text())  # cursor hidden while it is open
        os.write(self.master, b"\x1b[C")
        os.write(self.master, b"\r")
        self.wait_for(lambda: "saved:" in self.text(), "the choice being saved")
        config = json.loads((self.base / "home" / ".tandem" / "config.json").read_text())
        self.assertEqual(config["defaults"]["claude_model"], "fable")
        self.assertEqual(config["consent"], {"version": "test"})
        # Ctrl+C cancels the picker, not the session, and the terminal is back to line input.
        frames = self.text().count("Enter save")
        self.type("/models")
        self.wait_for(lambda: self.text().count("Enter save") > frames, "the picker again")
        prompts = self.text().count("tandem> ")
        os.write(self.master, b"\x03")
        self.wait_for(lambda: self.text().count("tandem> ") > prompts, "the prompt after Ctrl+C")
        self.assertGreater(self.text().rfind("\x1b[?25h"), self.text().rfind("\x1b[?25l"))
        seen = len(self.text())
        self.type("/help")
        self.wait_for(lambda: "/transcript" in self.text()[seen:], "the session answering")
        self.type("/quit")
        self.assertEqual(self.proc.wait(timeout=10), 0, self.text())

    def test_tab_changes_models_on_the_bar(self):
        self.start(NO_COLOR="1")
        self.wait_for(lambda: "Tab or /models to change" in self.text(), "the bar's hint")
        os.write(self.master, b"\t")
        self.wait_for(lambda: screen(self.text())[-1] == tandem.BAR_EDIT_HINT, "the mode's keys")
        self.assertIn("claude [default]/default", screen(self.text())[-2])
        os.write(self.master, b"\x1b[A")
        self.wait_for(lambda: "claude [fable]/default" in screen(self.text())[-2], "the changed value")
        os.write(self.master, b"\r")
        self.wait_for(lambda: "saved: claude fable/default" in self.text(), "the choice being saved")
        config = json.loads((self.base / "home" / ".tandem" / "config.json").read_text())
        self.assertEqual(config["defaults"], {"claude_model": "fable"})
        self.type("/quit")
        self.assertEqual(self.proc.wait(timeout=10), 0, self.text())

    def test_bar_edit_survives_running_turn_and_resize(self):
        self.start_slow_turn(NO_COLOR="1")
        self.wait_for(self.screen_ends_with_prompt(), "the prompt during the turn")
        os.write(self.master, b"\t\x1b[Z\x1b[A")  # Codex effort, default to low
        self.wait_for(lambda: "[low]" in "\n".join(screen(self.text())), "the unsaved effort")
        frames = self.text().count(tandem.BAR_EDIT_HINT)
        self.wait_for(lambda: self.text().count(tandem.BAR_EDIT_HINT) > frames, "ticker redraw")
        self.assertIn("[low]", "\n".join(screen(self.text())))
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 40, 0, 0))
        seen = len(self.text())
        os.kill(self.proc.pid, signal.SIGWINCH)
        self.wait_for(lambda: "\x1b[H\x1b[J" in self.text()[seen:], "resize redraw")
        self.wait_for(lambda: any("codex effort [low]" in row for row in screen(self.text())),
                      "focus after resize")
        self.assertTrue(all(tandem.display_width(row) < 40 for row in screen(self.text())))
        os.write(self.master, b"\r")
        self.wait_for(lambda: "(applies from the next turn)" in self.text(), "saved tuning")
        run = self.run_dirs()[0]
        self.assertEqual(tandem.read_tuning(run / "tuning.json"), {"codex_effort": "low"})
        config = json.loads((self.base / "home" / ".tandem" / "config.json").read_text())
        self.assertEqual(config["defaults"], {"codex_effort": "low"})
        os.write(self.master, b"\x03\x03\x03")
        self.assertEqual(self.proc.wait(timeout=20), 0, self.text())

    def screen_ends_with_prompt(self, text="tandem> "):
        return lambda: without_bar(screen(self.text()))[-1:] == [text]

    def test_status_bar_is_drawn_below_the_prompt(self):
        self.start_slow_turn(NO_COLOR=None)
        self.wait_for(lambda: any("claude " in row and "working" in row
                                 for row in screen(self.text())[-2:]), "the live status bar")
        os.write(self.master, b"draft")
        self.wait_for(self.screen_ends_with_prompt("tandem> draft"), "text above the bar")
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 40, 0, 0))
        seen = len(self.text())
        os.kill(self.proc.pid, signal.SIGWINCH)
        self.wait_for(lambda: "\x1b[H\x1b[J" in self.text()[seen:], "resize clearing old rows")
        self.wait_for(self.screen_ends_with_prompt("tandem> draft"), "draft kept after resize")
        rows = screen(self.text())
        self.assertTrue(all(tandem.display_width(row) < 40 for row in rows))
        self.assertIn("working", rows[-2])
        os.write(self.master, b"\x7f!")
        self.wait_for(self.screen_ends_with_prompt("tandem> draf!"), "editing after resize")
        os.write(self.master, b"\x03\x03\x03")
        self.assertEqual(self.proc.wait(timeout=20), 0, self.text())

    def test_slash_menu_shows_descriptions(self):
        self.start(NO_COLOR=None)
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        os.write(self.master, b"/m")
        self.wait_for(lambda: "pick Claude and Codex model" in self.text(), "the menu")
        rows = without_bar(screen(self.text()))
        at = rows.index("tandem> /m")
        self.assertRegex(rows[at + 1], r"/models +pick Claude and Codex model and effort")
        self.assertIn("/campaign", rows[-2])  # it has an m too, after the names that start with one
        self.assertIn("Esc close", rows[-1])
        os.write(self.master, b"\r")  # Enter takes the highlighted command
        self.wait_for(lambda: "Enter save" in self.text(), "the picker")
        os.write(self.master, b"\x1b")
        self.wait_for(lambda: "models unchanged" in self.text(), "the picker closing")
        self.type("/quit")
        self.assertEqual(self.proc.wait(timeout=10), 0, self.text())

    def test_prompt_comes_back_after_the_picker(self):
        # Owner report: after Esc in /models the next line had no prompt.
        self.start(NO_COLOR=None)
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        self.type("/models")
        self.wait_for(lambda: "Enter save" in self.text(), "the picker")
        os.write(self.master, b"\x1b")
        self.wait_for(lambda: "models unchanged" in self.text(), "the picker closing")
        self.wait_for(self.screen_ends_with_prompt(), "a fresh prompt")
        self.assertEqual(without_bar(screen(self.text()))[-2], "models unchanged")

    def test_prompt_cannot_be_erased(self):
        # Owner report: Backspace on macOS libedit erased `tandem> ` and it never came back after /board.
        self.start()
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        for _ in range(5):
            os.write(self.master, b"\x7f")
        os.write(self.master, b"x")
        self.wait_for(self.screen_ends_with_prompt("tandem> x"), "the prompt with x")
        os.write(self.master, b"\x15")
        self.type("/board")
        self.wait_for(lambda: "no run yet" in self.text(), "the board's answer")
        self.wait_for(self.screen_ends_with_prompt(), "a fresh prompt")

    def test_empty_enter_during_a_turn_prints_nothing(self):
        # Owner report (iTerm2): each empty Enter during a turn printed another status line and prompt.
        self.start_slow_turn(NO_COLOR=None)
        self.wait_for(lambda: "working 0m" in plain(self.text()), "the status bar")

        def count():
            rows = [plain(r) for r in screen(self.text())]
            return sum(r.startswith(("tandem> ", "\u203a ")) for r in rows), sum(r.startswith("claude ") and "working" in r for r in rows)

        self.wait_for(lambda: count() == (2, 1), "the goal line, the prompt and the bar")
        for _ in range(3):
            os.write(self.master, b"\r")
            time.sleep(.2)
        os.write(self.master, b"/status")
        self.wait_for(lambda: any(r.startswith("tandem> /status") for r in screen(self.text())), "the typed command")
        self.wait_for(lambda: count() == (2, 1), "no new prompt or bar", timeout=3)
        os.write(self.master, b"\r")
        self.wait_for(lambda: "outcome running" in self.text(), "the status answer")

    def check_typing_survives_a_log_line(self, **env):
        self.start(STUB_TURN_SLEEP="60", **env)
        self.wait_for(lambda: "tandem> " in self.text(), "the prompt")
        self.type("make a file")
        self.wait_for(lambda: "claude start" in self.log_text(), "the agent starting")
        self.wait_for(lambda: "working 0m" in plain(self.text()), "the status bar")
        os.write(self.master, b"abc")
        seen = len(self.text())
        self.wait_for(lambda: plain(self.text()[seen:]).count("working 0m") >= 2, "the bar ticking while typing")
        os.write(self.master, b"\r")
        self.wait_for(lambda: "queued for the next turn" in self.text(), "the typed line arriving")
        self.assertIn("\u203a abc", [plain(r) for r in screen(self.text())])  # kept as the owner's line

    def test_typing_survives_a_log_line(self):
        self.check_typing_survives_a_log_line()

    def test_typing_survives_a_log_line_in_color(self):
        self.check_typing_survives_a_log_line(NO_COLOR=None)

    def test_correction_interrupts_a_slow_turn(self):
        agent = self.start_slow_turn(NO_COLOR=None)
        self.type("use tabs not spaces")
        self.wait_for(lambda: "[y/N]" in self.text(), "the question")
        self.assertTrue(self.alive(agent))  # asking stops nothing
        self.type("y")
        self.wait_for(lambda: not self.alive(agent), "the agent stopping", timeout=10)
        self.wait_for(lambda: self.log_text().count("claude start") == 2, "the same agent taking the turn again")
        run = next(d for d in self.runs.iterdir() if (d / "state.json").exists())
        self.wait_for((run / "turn-02-claude.prompt.md").exists, "the next prompt")
        prompt = (run / "turn-02-claude.prompt.md").read_text()
        self.assertIn("use tabs not spaces", prompt)
        self.assertIn("Your previous turn was interrupted by the owner; it changed nothing.", prompt)
        self.wait_for(lambda: "interrupted by the owner" in plain(self.text()), "the interrupted card")
        self.assertNotIn("claude end", self.log_text())
        first = self.state()["history"][0]
        self.assertEqual((first["status"], self.state()["failures"]["claude"]), ("interrupted", 0))
        if os.environ.get("TANDEM_SHOW_SCREEN"):
            print("\n".join(plain(r) for r in screen(self.text())))

    def test_sigterm_in_headless_mode_stops_the_agent(self):
        log = self.base / "headless.txt"
        with open(log, "w") as out:
            proc = subprocess.Popen([sys.executable, "-B", str(ROOT / "tandem.py"), "-p", "goal",
                                     "--repo", str(self.repo), "--output-format", "json"],
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL,
                                    env=dict(self.env, STUB_TURN_SLEEP="60"), start_new_session=True)
            self.addCleanup(lambda: proc.poll() is None and os.killpg(proc.pid, signal.SIGKILL))
            self.wait_for(lambda: "claude start" in self.log_text(), "the agent starting")
            proc.send_signal(signal.SIGTERM)  # only tandem, as the npm launcher forwards it
            self.assertEqual(proc.wait(timeout=20), 4)
        self.assertFalse(self.alive(self.agent_pids()[0]))
        self.assertEqual(json.loads(log.read_text())["outcome"], "stopped")


class Queue(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.run = pathlib.Path(temp.name)
        (self.run / "state.json").write_text("{}")

    def test_messages_queue_up_to_the_limit(self):
        for n in range(1, tandem.MAX_QUEUED + 1):
            self.assertEqual(tandem.leave_message(self.run, f"note {n}\n\n\nsecond line"), n)
        self.assertEqual(tandem.queued(self.run)[0], "note 1\nsecond line")  # blank lines separate messages
        with self.assertRaisesRegex(ValueError, "already wait for the next turn"):
            tandem.leave_message(self.run, "one too many")
        self.assertEqual(len(tandem.queued(self.run)), tandem.MAX_QUEUED)
        self.assertEqual(tandem.leave_message(self.run, "now", force=True), tandem.MAX_QUEUED + 1)
        self.assertEqual(tandem.clear_queue(self.run), tandem.MAX_QUEUED + 1)
        self.assertEqual(tandem.queued(self.run), [])
        self.assertEqual(tandem.take_inbox([self.run / "inbox.md"]), "")

    def session(self):
        session = tandem.Session(argparse.Namespace(), tandem.Console(fancy=False))
        session.thread = mock.Mock()
        session.thread.is_alive.return_value = True
        return session

    def test_asked_once_per_turn(self):
        session = self.session()
        with mock.patch.dict(tandem.CURRENT, {"target": self.run}), \
                mock.patch.dict(tandem.PROGRESS, {"since": 1.0}), \
                mock.patch.object(session.console, "ask", return_value="") as ask, \
                mock.patch.object(tandem, "emit") as emit:
            session.typed("first")
            session.typed("second")
            self.assertEqual(ask.call_count, 1)
            self.assertEqual(emit.call_args[0][0], "\u2713 queued for the next turn (2 waiting)")
            tandem.PROGRESS["since"] = 2.0  # the next turn asks again
            session.typed("third")
            self.assertEqual(ask.call_count, 2)

    def test_full_queue_refuses_but_now_goes_through(self):
        session = self.session()
        for n in range(tandem.MAX_QUEUED):
            tandem.leave_message(self.run, f"note {n}")
        with mock.patch.dict(tandem.CURRENT, {"target": self.run}), \
                mock.patch.object(session.console, "ask") as ask, \
                mock.patch.object(tandem, "interrupt_turn") as interrupt, \
                mock.patch.object(tandem, "emit") as emit:
            session.typed("more")
            ask.assert_not_called()
            self.assertIn("/queue clear drops them", emit.call_args[0][0])
            session.handle("now", "stop and read these")
            interrupt.assert_called_once_with(self.run)
        self.assertEqual(tandem.queued(self.run)[-1], "stop and read these")

    def test_queue_command_lists_and_clears(self):
        session = self.session()
        tandem.leave_message(self.run, "use tabs\nnot spaces")
        with mock.patch.dict(tandem.CURRENT, {"target": self.run}), mock.patch.object(tandem, "emit") as emit:
            session.handle("queue", "")
            self.assertEqual(emit.call_args[0][0].splitlines(),
                             [f"1 of {tandem.MAX_QUEUED} waiting for the next turn:", "  1. use tabs not spaces"])
            session.handle("queue", "clear")
            self.assertEqual(emit.call_args[0][0], "dropped 1 message(s)")
            session.handle("queue", "")
            self.assertEqual(emit.call_args[0][0], "nothing waits for the next turn")


class Activity(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = pathlib.Path(temp.name)
        self.work = self.dir / "work"

    def test_claude_events_become_short_lines(self):
        def event(kind, *parts):
            return {"type": kind, "message": {"content": list(parts)}}
        tool = lambda name, **args: {"type": "tool_use", "name": name, "input": args}
        seen = [
            event("assistant", {"type": "text", "text": "\nI'll read the tests first.\nThen fix."}),
            event("assistant", tool("Bash", command="python3 -m unittest\necho done", description="x")),
            event("assistant", tool("Edit", file_path=str(self.work / "tandem.py")), tool("Grep", pattern="def loop")),
            event("user", {"type": "tool_result", "is_error": True, "content": "Exit code 1\nboom"}),
            event("user", {"type": "tool_result", "content": "fine"}),
            event("assistant", tool("StructuredOutput", status="done"), {"type": "thinking", "thinking": "hm"}),
            {"type": "system", "subtype": "init"},
            event("assistant", tool("mcp__x__y")),
        ]
        lines = [line for e in seen for line in tandem.claude_actions(e, self.work)]
        self.assertEqual(lines, ["\u00bb I'll read the tests first.", "$ python3 -m unittest", "edit tandem.py",
                                 "grep def loop", "\u2717 Exit code 1", "mcp__x__y"])

    def test_codex_log_becomes_short_lines(self):
        log = ["OpenAI Codex v0.163.0", "user", "Goal: codex", "exec", "the prompt", "codex",
               "I'll review the last commit.", "more of that message", "",
               "exec", f"/bin/zsh -lc 'git show HEAD --stat' in {self.work}", " succeeded in 12ms:",
               "exec", "exec", f'/bin/zsh -lc "rg -n \'x\' tandem.py" in {self.work}', " exited 1 in 30ms:",
               "exec", "/bin/zsh -lc 'cat CLAUDE.md", "if [ -f AGENTS.md ]; then cat AGENTS.md; fi' in /w",
               "apply patch", "patch: completed", f"{self.work}/README.md", f"{self.work}/tandem.py",
               "tokens used", "51,292"]
        reader = tandem.CodexActivity(str(self.work))
        lines = [text for text in map(reader.feed, log) if text]
        self.assertEqual(lines, ["$ the prompt", "\u00bb I'll review the last commit.", "$ git show HEAD --stat",
                                 "$ rg -n 'x' tandem.py", "\u2717 exited 1", "$ cat CLAUDE.md",
                                 "edit README.md", "edit tandem.py"])

    def test_claude_payload_from_stream_or_plain_json(self):
        result = {"type": "result", "structured_output": {"status": "done"}, "total_cost_usd": .5}
        stream = "\n".join([json.dumps({"type": "system"}), "not json", json.dumps(result), ""])
        self.assertEqual(tandem.claude_payload(stream), result)
        self.assertEqual(tandem.claude_payload('{\n "result": "x"\n}'), {"result": "x"})
        self.assertIsNone(tandem.claude_payload("[1]"))
        self.assertIsNone(tandem.claude_payload("crashed"))

    def test_run_claude_streams_into_the_activity_log(self):
        schema = self.dir / "schema.json"
        schema.write_text("{}")
        result = {"type": "result", "structured_output": {"status": "done"}, "total_cost_usd": .5}
        stream = [json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(self.work / "a.py")}}]}}),
            "{broken", json.dumps(result)]

        def fake(cmd, **kwargs):
            for line in stream:
                kwargs["on_line"]("stdout", line)
            kwargs["on_line"]("stderr", "a warning")
            return subprocess.CompletedProcess(cmd, 0, "\n".join(stream) + "\n", "a warning\n")

        cfg = {"timeout": 5, "claude_permission_mode": "acceptEdits"}
        with mock.patch.object(tandem, "run_child", side_effect=fake) as run:
            self.assertEqual(tandem.run_claude(cfg, self.work, "goal", self.dir / "turn-03-claude", schema,
                                               "status", False), ({"status": "done"}, .5, {}))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--output-format") + 1:cmd.index("--output-format") + 3],
                         ["stream-json", "--verbose"])
        lines = [line.split("\t", 1)[1] for line in (self.dir / "activity.log").read_text().splitlines()]
        self.assertEqual(lines, ["claude\t\u2500\u2500 turn 3 \u2500\u2500", "claude\tread a.py"])
        self.assertEqual(json.loads((self.dir / "turn-03-claude.json").read_text()), result)

    def test_follow_output_hands_over_lines_as_they_come(self):
        seen = []
        script = "import sys, time\nprint('one', flush=True)\nprint('oops', file=sys.stderr)\nprint(sys.stdin.read())"
        proc = tandem.run_child([sys.executable, "-c", script], input="from stdin",
                                on_line=lambda stream, line: seen.append((stream, line)))
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "one\nfrom stdin\n", "oops\n"))
        self.assertEqual(sorted(seen), [("stderr", "oops"), ("stdout", "from stdin"), ("stdout", "one")])
        with self.assertRaises(subprocess.TimeoutExpired):
            tandem.run_child([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.5,
                             on_line=lambda *_: None)
        self.assertFalse(tandem.CHILDREN)

    def test_watch_follows_the_run_that_acted_last(self):
        old, new = self.dir / "20261001-old", self.dir / "20261002-camp" / "01-goal"
        for run in (old, new):
            run.mkdir(parents=True)
        tandem.note_activity(old, "claude", "$ ls")
        os.utime(old / tandem.ACTIVITY, (1, 1))
        tandem.note_activity(new, "codex", "\u2500\u2500 turn 1 \u2500\u2500")
        out = io.StringIO()
        ticks = iter([None, KeyboardInterrupt])

        def sleep(_):
            step = next(ticks)
            if step:
                raise step
            tandem.note_activity(new, "check", "= passed")  # arrives while watching

        with mock.patch.object(tandem, "RUNS", self.dir), mock.patch("sys.stdout", out), \
                mock.patch.object(tandem.time, "sleep", side_effect=sleep), \
                mock.patch.object(tandem, "term_width", return_value=60):
            tandem.cmd_watch(argparse.Namespace(run=None))
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[2], "01-goal")
        self.assertRegex(lines[3], r"^\d\d:\d\d:\d\d codex  \u2500\u2500 turn 1 \u2500\u2500$")
        self.assertRegex(lines[4], r"^\d\d:\d\d:\d\d check  = passed$")
        self.assertNotIn("$ ls", out.getvalue())

    def test_watch_pane_opens_in_tmux_or_iterm_or_not_at_all(self):
        cmd = tandem.watch_command()
        self.assertEqual(cmd[1:], [f"TANDEM_RUNS={tandem.RUNS}", sys.executable, str(ROOT / "tandem.py"), "watch"])
        done = subprocess.CompletedProcess([], 0)
        for env, program in (({"TMUX": "/tmp/tmux-1/default"}, "tmux"), ({"TERM_PROGRAM": "iTerm.app"}, "osascript")):
            with self.subTest(program=program), mock.patch.dict(os.environ, env), \
                    mock.patch.object(tandem.shutil, "which", return_value="/bin/x"), \
                    mock.patch.object(tandem.subprocess, "run", return_value=done) as run:
                os.environ.pop("TMUX", None) if program == "osascript" else None
                self.assertTrue(tandem.open_watch_pane(['/a b/python3', 'say "hi"']))
            args = run.call_args[0][0]
            self.assertEqual(args[0], program)
            if program == "osascript":
                self.assertIn('command "\'/a b/python3\' \'say \\"hi\\"\'"', args[2])
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}), \
                mock.patch.object(tandem.subprocess, "run") as run:
            os.environ.pop("TMUX", None)
            self.assertFalse(tandem.open_watch_pane(cmd))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

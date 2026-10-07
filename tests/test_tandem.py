import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
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
                                  ("max_turns", 2), ("blocked", 3), ("stopped", 4), ("failed", 5)):
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
        self.assertEqual(set(result), {"run_id", "name", "repo", "branch", "base", "outcome",
                                      "outcome_message", "turn_count", "cost_usd", "transcript", "turns"})
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
        for name in ("help", "status", "say", "stop", "resume", "goal", "campaign", "runs", "transcript",
                     "doctor", "quit"):
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
import json, os, pathlib, sys, time
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
if args[:2] in (["auth", "status"], ["login", "status"]):
    print(json.dumps({"loggedIn": True}))
    sys.exit()
if args == ["--version"]:
    time.sleep(float(os.environ.get("STUB_VERSION_SLEEP", "0")))
    print(name, "stub")
    sys.exit()
sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(f"{name} start {os.getpid()}\\n")
time.sleep(float(os.environ.get("STUB_TURN_SLEEP", "0")))
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(f"{name} end\\n")
reply = {"status": "done", "summary": "stub"}
if name == "codex":
    pathlib.Path(args[args.index("-o") + 1]).write_text(json.dumps(reply))
else:
    print(json.dumps({"structured_output": reply, "total_cost_usd": 0}))
"""


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
        self.log = self.base / "agents.log"
        self.output = []
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOME=str(home),
                        TANDEM_RUNS=str(self.runs), NO_COLOR="1", STUB_LOG=str(self.log),
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        self.env.pop("TANDEM_PROG", None)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True, env=self.env)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=self.repo, check=True, env=self.env)

    def start(self, *args, **env):
        import pty
        master, slave = pty.openpty()
        self.output, lock = [], threading.Lock()
        # Its own session, as a terminal's job would be: signals to its group are the terminal's.
        self.proc = subprocess.Popen([sys.executable, "-B", str(ROOT / "tandem.py"), "--repo", str(self.repo), *args],
                                     stdin=slave, stdout=slave, stderr=slave, cwd=self.repo,
                                     env=dict(self.env, **env), start_new_session=True)
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

        def cleanup():
            for pid in self.agent_pids():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
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

    def start_slow_turn(self):
        self.start(STUB_TURN_SLEEP="60")
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
        self.wait_for(lambda: "FINISHED" in self.text(), "the run finishing")
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


if __name__ == "__main__":
    unittest.main()

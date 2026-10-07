#!/usr/bin/env node
// Launcher for the npm install: finds a Python >= 3.9 and runs tandem.py with it.
// No dependencies on purpose; all the logic lives in tandem.py.
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const path = require("node:path");

const MIN = [3, 9];
const PROBE = "import sys; print('%d.%d' % sys.version_info[:2])";
const script = path.join(__dirname, "..", "tandem.py");

function candidates() {
  const list = [["python3"]];
  for (let minor = 15; minor >= MIN[1]; minor--) list.push([`python3.${minor}`]);
  list.push(["python"]);
  if (process.platform === "win32") list.push(["py", "-3"]);
  return list;
}

function version(cmd) {
  const r = spawnSync(cmd[0], [...cmd.slice(1), "-c", PROBE], { encoding: "utf8" });
  if (r.error || r.status !== 0) return null;
  const m = /^(\d+)\.(\d+)/.exec(r.stdout.trim());
  return m ? [Number(m[1]), Number(m[2])] : null;
}

function supported(v) {
  return v && (v[0] > MIN[0] || (v[0] === MIN[0] && v[1] >= MIN[1]));
}

function find() {
  const override = process.env.TANDEM_PYTHON;
  if (override) {
    // An explicit choice that does not work is an error, not a hint: falling through would run
    // some other Python without saying so.
    const v = version([override]);
    if (supported(v)) return { cmd: [override], version: v.join(".") };
    console.error(
      `TANDEM_PYTHON=${override} ` +
        (v ? `is Python ${v.join(".")}; tandem needs ${MIN.join(".")} or newer.` : "could not be run as Python.") +
        "\nPoint it at a Python 3.9+ executable, or unset it to search PATH."
    );
    process.exit(1);
  }
  const tried = [];
  for (const cmd of candidates()) {
    const v = version(cmd);
    if (supported(v)) return { cmd, version: v.join(".") };
    tried.push(cmd.join(" ") + (v ? ` (${v.join(".")})` : ""));
  }
  console.error(
    `tandem needs Python ${MIN.join(".")} or newer and found none.\n` +
      `Tried: ${tried.join(", ")}.\n` +
      "Install Python (macOS: xcode-select --install, or brew install python; " +
      "Debian/Ubuntu: apt install python3), or point TANDEM_PYTHON at an interpreter."
  );
  process.exit(1);
}

const python = find();
const child = spawn(python.cmd[0], [...python.cmd.slice(1), script, ...process.argv.slice(2)], {
  stdio: "inherit",
  env: {
    ...process.env,
    TANDEM_PROG: "tandem",
    TANDEM_NODE: process.version,
  },
});

child.on("error", (e) => {
  console.error(`tandem: could not start ${python.cmd.join(" ")}: ${e.message}`);
  process.exit(1);
});

// On a tty Ctrl+C reaches the whole foreground group. Forwarding it again can interrupt
// Python's KeyboardInterrupt cleanup. Non-tty parents need explicit SIGINT forwarding.
// SIGHUP is passed on too, so a closed terminal reaches Python even if only Node was told.
const signalHandlers = new Map();
for (const sig of process.platform === "win32" ? ["SIGINT", "SIGTERM"] : ["SIGINT", "SIGTERM", "SIGHUP"]) {
  const forward = () => {
    if (sig === "SIGINT" && process.stdin.isTTY) return;
    if (child.exitCode === null && child.signalCode === null) child.kill(sig);
  };
  signalHandlers.set(sig, forward);
  process.on(sig, forward);
}

child.on("exit", (code, signal) => {
  // Restore the default handlers before re-raising: otherwise our own listener swallows the
  // signal and Node exits successfully, hiding a terminated run from scripts and supervisors.
  for (const [sig, forward] of signalHandlers) process.removeListener(sig, forward);
  if (signal) process.kill(process.pid, signal);
  else process.exit(code === null ? 1 : code);
});

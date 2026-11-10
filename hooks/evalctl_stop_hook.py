#!/usr/bin/env python3
"""Claude Code Stop hook: no valid evalctl receipt, no stopping.

Input and output follow https://code.claude.com/docs/en/hooks (Stop):
  stdin   JSON with `cwd`, `stop_hook_active`, ...
  stdout  nothing                                -> Claude may stop
          {"decision": "block", "reason": "..."} -> Claude continues, `reason` is shown to it
          {"systemMessage": "..."}               -> Claude may stop, the user sees a warning

Behaviour:
  * No .evalctl.yaml in the session directory or its parents: allow. Sessions
    that have nothing to do with evalctl are never affected.
  * `evalctl verify` passes: allow.
  * Otherwise run `evalctl check`. PASS writes the receipt: allow. FAIL: block
    and hand the failure list to Claude.
  * Once the round limit is reached (or evalctl cannot run at all), Claude is
    blocked one more time so it reports the situation to the user, then allowed
    to stop. The user gets a warning that there is no receipt.

Only the standard library is used. Set EVALCTL_HOOK=off in the environment that
launches Claude Code to disable the hook for a session.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

POINTER = ".evalctl.yaml"
MAX_REASON = 9000  # stay under Claude Code's 10,000 character cap for hook text


def find_pointer(start):
    try:
        d = Path(start).resolve()
    except OSError:
        return None
    for p in [d, *d.parents]:
        if (p / POINTER).is_file():
            return p / POINTER
    return None


def find_evalctl():
    env = os.environ.get("EVALCTL_BIN")
    if env:
        return [env]
    exe = shutil.which("evalctl")
    if exe:
        return [exe]
    for cand in (Path.home() / ".local/bin/evalctl", Path("/opt/homebrew/bin/evalctl"),
                 Path("/usr/local/bin/evalctl")):
        if cand.is_file():
            return [str(cand)]
    return None


def allow(message=None):
    if message:
        print(json.dumps({"systemMessage": message}))
    sys.exit(0)


def block(reason):
    if len(reason) > MAX_REASON:
        reason = reason[:MAX_REASON] + "\n[... truncated; run `evalctl check` for the full list]"
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if os.environ.get("EVALCTL_HOOK", "").lower() == "off" or os.environ.get("EVALCTL_IN_JUDGE"):
        allow()

    pointer = None
    for start in (data.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if start:
            pointer = find_pointer(start)
            if pointer:
                break
    if pointer is None:
        allow()
    root = str(pointer.parent)
    again = bool(data.get("stop_hook_active"))  # Claude is already continuing because of a Stop hook

    def give_up(problem):
        """Block once so Claude tells the user; after that let it stop, with a warning."""
        if again:
            allow(f"evalctl: {root} has NO valid receipt. {problem}")
        block(f"evalctl: {problem}\nYou cannot fix this by yourself. Tell the user exactly this, "
              "state clearly that the output has not passed its checks, and then stop.")

    # The hook only ever runs inside Claude Code, so the worker is a Claude model unless told otherwise.
    os.environ.setdefault("EVALCTL_WORKER", "anthropic")
    evalctl = find_evalctl()
    if evalctl is None:
        give_up(f"this directory is gated by {pointer} but the `evalctl` command was not found "
                "(install it or set EVALCTL_BIN).")

    try:
        verify = subprocess.run(evalctl + ["verify", "--dir", root], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=120)
        if verify.returncode == 0:
            allow()
        check = subprocess.run(evalctl + ["check", "--dir", root, "--json", "--source", "hook"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, timeout=540)
    except (subprocess.TimeoutExpired, OSError) as e:
        give_up(f"could not run evalctl: {e}")

    if check.returncode == 0:
        allow()  # output was fine; the check just wrote the receipt
    try:
        result = json.loads(check.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        result = {}
    text = result.get("text") or (check.stdout + check.stderr).decode("utf-8", errors="replace").strip()

    if check.returncode != 1:
        give_up("the checks could not be run (setup error):\n" + text)

    round_no, max_rounds = result.get("round") or 0, result.get("max_rounds") or 3
    if again and round_no >= max_rounds:
        allow(f"evalctl: Claude stopped WITHOUT a valid receipt in {root}: checks still fail after "
              f"{round_no} round(s). Run `evalctl check --dir {root}` to see what is left.")
    block("evalctl: you may not finish yet. This directory is quality-gated and its output has no "
          "valid receipt.\n\n" + text)


if __name__ == "__main__":
    main()

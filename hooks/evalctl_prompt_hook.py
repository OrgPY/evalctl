#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: write the acceptance contract from the user's own prompt.

Input and output follow https://code.claude.com/docs/en/hooks (UserPromptSubmit), confirmed against
Claude Code 2.1.322:
  stdin   JSON with `prompt`, `session_id`, `cwd`, ...
  stdout  {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}
          -> the text is added to what Claude sees next to the prompt. Nothing printed: no effect.
The hook never blocks a prompt. Claude Code gives this event 30 seconds by default, so the model
call that writes the contract runs in a detached process; `evalctl check` waits for it.

When it acts:
  * the session directory (or a parent) has a .evalctl.yaml, or
  * the prompt contains a trigger word from ~/.evalctl/settings.yaml (default: 重要 / important),
    which also puts the task under the `high_stakes` profile, or
  * an earlier prompt of the same session was already gated.
A short prompt (a follow-up such as "yes, go on") adds no contract. Everything else is untouched.

Only the standard library is used. EVALCTL_HOOK=off disables it for a session.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_evalctl():
    env = os.environ.get("EVALCTL_BIN")
    if env:
        return [env]
    exe = shutil.which("evalctl")
    if exe:
        return [exe]
    for cand in (Path.home() / ".local/bin/evalctl", Path("/opt/homebrew/bin/evalctl"), Path("/usr/local/bin/evalctl")):
        if cand.is_file():
            return [str(cand)]
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    if os.environ.get("EVALCTL_HOOK", "").lower() == "off" or any(
            os.environ.get(v) for v in ("EVALCTL_IN_JUDGE", "EVALCTL_CHILD", "EVALCTL_OUTER")):
        return
    prompt, cwd = data.get("prompt") or "", data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
    evalctl = find_evalctl()
    if not prompt.strip() or not cwd or evalctl is None:
        return
    os.environ.setdefault("EVALCTL_WORKER", "anthropic")  # this hook only ever runs inside Claude Code
    try:
        p = subprocess.run(evalctl + ["contract", "--hook", "--task", "-", "--dir", cwd, "--session",
                                      str(data.get("session_id") or ""), "--json"],
                           input=prompt.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
        info = json.loads(p.stdout.decode("utf-8", errors="replace"))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return
    if not info.get("gated"):
        return
    root, profile = info.get("dir") or cwd, info.get("profile")
    lines = [f"evalctl: this task is quality-gated in {root}" + (f" (profile: {profile})" if profile else "") + "."]
    if info.get("contract"):
        lines.append("An acceptance contract is being written from the user's request above, by a separate model, "
                     "outside this directory. You do not need to run `evalctl contract`.")
    lines.append("When you believe the work is done, run `evalctl check` there. Fix only the FAIL items and re-check, "
                 "at most 3 rounds.")
    if info.get("repeat"):
        lines.append(f"This profile requires {info['repeat']} independent repeats: when `evalctl check` asks for it, run "
                     "`evalctl repeat` (several minutes: use the longest command timeout or run it in the background and "
                     "wait for it), settle every difference it reports in .eval/reconcile.md from the original inputs, "
                     "and check again.")
    lines.append("Deliver only with a receipt. In your final message report: how many contract items passed, each "
                 "difference between the repeat runs and how you settled it, and every item left for the user to judge.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}}))


if __name__ == "__main__":
    main()

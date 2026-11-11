#!/usr/bin/env python3
"""A scripted stand-in for an agent CLI, used by the tests of `evalctl run` and `evalctl bench`.

    fake_agent.py --plan PLAN.json [--resume SESSION] [--format claude|codex|text] PROMPT

PLAN.json: {"runs": [[call, call, ...], ...]}. A "run" is one working directory (in order of first
use; the last run is reused when there are more directories than runs). Each call is
{"write": {path: content}, "bash": [command], "delete": [path], "exit": 0, "sleep": 0}. Calls beyond the list do nothing.
Every call is logged to PLAN.json.calls (one JSON object per line) with the prompt it received.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plan", required=True)
ap.add_argument("--resume")
ap.add_argument("--format", default="claude")
ap.add_argument("prompt")
a = ap.parse_args()

plan = json.loads(Path(a.plan).read_text())
state_file = Path(a.plan + ".state")
state = json.loads(state_file.read_text()) if state_file.exists() else {"dirs": {}}
cwd = os.getcwd()
entry = state["dirs"].setdefault(cwd, {"run": len(state["dirs"]), "calls": 0})
run = plan["runs"][min(entry["run"], len(plan["runs"]) - 1)]
call = run[entry["calls"]] if entry["calls"] < len(run) else {}
entry["calls"] += 1
state_file.write_text(json.dumps(state))
session = a.resume or f"sess-{entry['run']}-{os.getpid()}"
with open(a.plan + ".calls", "a") as f:
    f.write(json.dumps({"cwd": cwd, "resume": a.resume, "prompt": a.prompt, "session": session,
                        "env": {k: os.environ.get(k) for k in ("EVALCTL_HOOK", "EVALCTL_OUTER", "EVALCTL_WORKER", "EVALCTL_CHILD")}}) + "\n")

time.sleep(call.get("sleep", 0))
events = []
if a.format == "claude":
    events.append({"type": "system", "subtype": "init", "session_id": session, "cwd": cwd})
elif a.format == "codex":
    events.append({"type": "thread.started", "thread_id": session})
for i, (rel, content) in enumerate((call.get("write") or {}).items()):
    p = Path(cwd) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    if a.format == "claude":
        events.append({"type": "assistant", "session_id": session, "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Write", "input": {"file_path": str(p), "content": content}}]}})
        events.append({"type": "user", "session_id": session, "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": "File created successfully"}]}})
    elif a.format == "codex":
        events.append({"type": "item.completed", "item": {"id": f"item_{i}", "type": "file_change",
                                                         "changes": [{"path": str(p), "kind": "add"}], "status": "completed"}})
for i, cmd in enumerate(call.get("bash") or []):   # commands the agent "ran" (only recorded, never executed)
    if a.format == "claude":
        events.append({"type": "assistant", "session_id": session, "message": {"content": [
            {"type": "tool_use", "id": f"b{i}", "name": "Bash", "input": {"command": cmd}}]}})
        events.append({"type": "user", "session_id": session, "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"b{i}", "content": "ok"}]}})
    elif a.format == "codex":
        events.append({"type": "item.completed", "item": {"id": f"cmd_{i}", "type": "command_execution", "command": cmd,
                                                         "aggregated_output": "ok", "exit_code": 0, "status": "completed"}})
for rel in call.get("delete") or []:
    (Path(cwd) / rel).unlink()
if a.format == "claude":
    events.append({"type": "result", "subtype": "success", "result": "done", "session_id": session})
elif a.format == "codex":
    events.append({"type": "item.completed", "item": {"id": "item_last", "type": "agent_message", "text": "done"}})
    events.append({"type": "turn.completed", "usage": {}})
if a.format == "text":
    print("done")
else:
    for e in events:
        print(json.dumps(e))
sys.exit(call.get("exit", 0))

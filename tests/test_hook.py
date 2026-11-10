"""The Stop hook script, driven the way Claude Code drives it: JSON on stdin, JSON on stdout."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "evalctl_stop_hook.py"
EVALCTL = Path(sys.executable).parent / "evalctl"
CHECKS = [{"id": "no_todo", "type": "contains", "file": "report.md", "must_not": ["TODO"]}]


def stop(cwd, active=False, **env):
    payload = {"session_id": "s1", "transcript_path": "/tmp/t.jsonl", "cwd": str(cwd),
               "permission_mode": "default", "hook_event_name": "Stop",
               "stop_hook_active": active, "last_assistant_message": "Done."}
    full_env = {**os.environ, "EVALCTL_BIN": str(EVALCTL), **env}
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full_env, timeout=60)
    assert p.returncode == 0, p.stderr.decode()
    out = p.stdout.decode().strip()
    return json.loads(out) if out else None


def test_sessions_without_pointer_are_untouched(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert stop(plain) is None


def test_blocks_with_failure_list_then_allows_once_fixed(proj):
    proj.config(CHECKS)
    proj.write("report.md", "intro\nTODO finish\n")
    out = stop(proj.dir)
    assert out["decision"] == "block"
    assert "you may not finish yet" in out["reason"]
    assert "report.md:2: forbidden text 'TODO' found" in out["reason"]
    assert "Fix only the FAIL items" in out["reason"]
    assert stop(proj.dir, active=True)["decision"] == "block"   # still not fixed

    proj.write("report.md", "intro\nfinished\n")
    assert stop(proj.dir, active=True) is None                  # hook ran check, receipt written
    assert (proj.dir / ".eval" / "receipt.json").is_file()
    assert stop(proj.dir) is None                               # valid receipt: verify is enough

    proj.write("report.md", "intro\nfinished, then TODO again\n")
    assert stop(proj.dir)["decision"] == "block"                # edited after the receipt


def test_works_from_a_subdirectory(proj):
    proj.config(CHECKS)
    proj.write("report.md", "TODO")
    sub = proj.dir / "src"
    sub.mkdir()
    assert stop(sub)["decision"] == "block"


def test_round_limit_block_once_more_then_release_with_warning(proj):
    proj.config(CHECKS, max_rounds=2)
    proj.write("report.md", "TODO 1")
    assert stop(proj.dir)["decision"] == "block"
    proj.write("report.md", "TODO 2")
    out = stop(proj.dir, active=True)           # round 2 of 2, already continuing: let go
    assert "decision" not in out and "WITHOUT a valid receipt" in out["systemMessage"]
    out = stop(proj.dir, active=False)          # a fresh stop is always told first
    assert out["decision"] == "block" and "round limit reached" in out["reason"]


def test_setup_error_is_reported_then_released(proj):
    proj.config(CHECKS)
    (proj.dir / ".evalctl.yaml").write_text("config: does_not_exist\n")
    out = stop(proj.dir)
    assert out["decision"] == "block" and "Tell the user" in out["reason"] and "not found" in out["reason"]
    out = stop(proj.dir, active=True)
    assert "decision" not in out and "NO valid receipt" in out["systemMessage"]


def test_missing_evalctl_binary(proj):
    proj.config(CHECKS)
    proj.write("report.md", "ok")
    out = stop(proj.dir, EVALCTL_BIN="/nonexistent/evalctl")
    assert out["decision"] == "block" and "could not run evalctl" in out["reason"]


@pytest.mark.parametrize("env", [{"EVALCTL_HOOK": "off"}, {"EVALCTL_IN_JUDGE": "1"}])
def test_switches_that_disable_the_hook(proj, env):
    proj.config(CHECKS)
    proj.write("report.md", "TODO")
    assert stop(proj.dir, **env) is None


def test_hook_runs_are_marked_in_stats(proj):
    proj.config(CHECKS)
    proj.write("report.md", "TODO")
    stop(proj.dir)
    s = json.loads(proj.run("stats", "--json")[1])["configs"]["t"]
    assert s["tasks_first_checked_by_hook"] == 1

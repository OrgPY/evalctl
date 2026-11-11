"""`evalctl run`: the outer loop around an agent, with a scripted agent standing in for the real one."""
from __future__ import annotations

import json

import pytest
import yaml

from evalctl import agents, store

CHECKS = [{"id": "has_report", "type": "file", "path": "report.md", "min_size": 1},
          {"id": "no_todo", "type": "contains", "file": "report.md", "must_not": ["TODO"]},
          {"id": "has_summary", "type": "contains", "file": "report.md", "must": ["## Summary"]}]


def run(proj, *extra, task="Write the report"):
    code, out, err = proj.run("run", "--agent", "fake", "--dir", str(proj.dir), "--json", task, *extra)
    return code, json.loads(out)


def test_fail_then_feedback_then_pass(proj, fake_agent):
    proj.config(CHECKS)
    fake_agent.script([{"write": {"report.md": "intro\nTODO finish\n"}},
                       {"write": {"report.md": "## Summary\nall done\n"}}])
    code, data = run(proj)
    assert code == 0 and data["status"] == "pass"
    assert [r["status"] for r in data["rounds"]] == ["fail", "pass"]
    assert data["rounds"][0]["failed_checks"] == ["no_todo", "has_summary"]
    assert proj.verify()[0] == 0

    first, second = fake_agent.calls()
    assert first["resume"] is None and first["prompt"].startswith("Write the report")
    # the failure list went back into the same session
    assert second["resume"] == first["session"]
    assert "[FAIL] no_todo" in second["prompt"] and "TODO" in second["prompt"]
    assert "do not run evalctl yourself" in second["prompt"]
    # the Stop hook and a skill-driven check are kept out of the way while the outer loop drives
    assert first["env"] == {"EVALCTL_HOOK": "off", "EVALCTL_OUTER": "1", "EVALCTL_WORKER": "anthropic"}


def test_every_round_is_kept_on_disk(proj, fake_agent):
    proj.config(CHECKS)
    proj.write("notes.txt", "given")
    fake_agent.script([{"write": {"report.md": "TODO"}}, {"write": {"report.md": "## Summary\nok\n"}}])
    run(proj)
    r1, r2 = proj.dir / ".eval/rounds/1", proj.dir / ".eval/rounds/2"
    assert "Write the report" in (r1 / "input.md").read_text()
    assert "[FAIL] no_todo" in (r2 / "input.md").read_text()
    # the output is found by diffing the directory, not by asking the agent
    changes = json.loads((r1 / "changes.json").read_text())
    assert changes["this_round"] == {"added": ["report.md"], "changed": [], "deleted": []}
    assert json.loads((r2 / "changes.json").read_text())["this_round"]["changed"] == ["report.md"]
    assert (r1 / "output/report.md").read_text() == "TODO"
    assert not (r1 / "output/notes.txt").exists()
    assert json.loads((r1 / "check.json").read_text())["status"] == "fail"
    assert "[FAIL] no_todo" in (r1 / "check.txt").read_text()
    assert '"tool_use"' in (r1 / "trajectory.jsonl").read_text()


def test_stops_at_max_rounds_without_a_receipt(proj, fake_agent):
    proj.config(CHECKS, max_rounds=2)
    fake_agent.script([{"write": {"report.md": "TODO 1"}}, {"write": {"report.md": "TODO 2 ## Summary"}},
                       {"write": {"report.md": "## Summary"}}])
    code, data = run(proj)
    assert code == 1 and data["status"] == "fail" and len(data["rounds"]) == 2
    assert "round limit reached (2)" in data["reason"]
    assert "[FAIL] no_todo" in data["text"]
    assert not (proj.dir / ".eval/receipt.json").exists()
    assert len(fake_agent.calls()) == 2


def test_stops_when_failures_do_not_go_down_for_two_rounds(proj, fake_agent):
    proj.config(CHECKS, max_rounds=6)
    fake_agent.script([{"write": {"report.md": f"TODO {i}"}} for i in range(6)])
    code, data = run(proj)
    assert code == 1 and len(data["rounds"]) == 3  # round 2 and round 3 were no better than the one before
    assert "did not go down for 2 rounds" in data["reason"]


def test_progress_resets_the_stall_counter(proj, fake_agent):
    proj.config(CHECKS, max_rounds=6)
    fake_agent.script([{"write": {"report.md": "TODO"}}, {"write": {"report.md": "TODO again"}},
                       {"write": {"report.md": "TODO ## Summary"}}, {"write": {"report.md": "TODO  ## Summary"}},
                       {"write": {"report.md": "## Summary"}}])
    code, data = run(proj)
    assert code == 0 and [r["left_to_fix"] for r in data["rounds"]] == [2, 2, 1, 1, 0]


def test_agent_without_resume_gets_a_new_call_with_the_previous_output_and_failures(proj, fake_agent):
    proj.config(CHECKS)
    fake_agent.script([{"write": {"report.md": "TODO"}}, {"write": {"report.md": "## Summary"}}], fmt="text")
    code, data = run(proj)
    assert code == 0
    first, second = fake_agent.calls()
    assert second["resume"] is None
    assert second["prompt"].startswith("Write the report")           # the task again
    assert str(proj.dir) in second["prompt"] and "report.md" in second["prompt"]  # where the last output is
    assert "[FAIL] no_todo" in second["prompt"]                      # and what failed
    assert (proj.dir / ".eval/rounds/1/trajectory.txt").read_text().strip() == "done"


def test_codex_style_output_gives_the_session_id(proj, fake_agent):
    proj.config(CHECKS)
    fake_agent.script([{"write": {"report.md": "TODO"}}, {"write": {"report.md": "## Summary"}}], fmt="codex")
    code, data = run(proj)
    first, second = fake_agent.calls()
    assert code == 0 and second["resume"] == first["session"] == data["session_id"]


def test_agent_crash_is_reported_and_the_output_is_still_checked(proj, fake_agent):
    proj.config(CHECKS, max_rounds=1)
    fake_agent.script([{"write": {"report.md": "TODO"}, "exit": 3}])
    code, data = run(proj)
    assert code == 1 and "exited with code 3" in data["rounds"][0]["agent_problem"]
    assert data["rounds"][0]["failed_checks"] == ["no_todo", "has_summary"]


def test_agent_timeout(proj, fake_agent):
    proj.config(CHECKS, max_rounds=1)
    fake_agent.script([{"sleep": 20}], timeout=1)
    code, data = run(proj)
    assert code == 1 and "did not finish within the timeout" in data["rounds"][0]["agent_problem"]


def test_setup_errors_are_found_before_the_agent_is_called(proj, fake_agent):
    fake_agent.script([{"write": {"report.md": "x"}}])
    code, _, err = proj.run("run", "--agent", "fake", "--dir", str(proj.dir), "task")
    assert code == 2 and "No check config selected" in err
    proj.config(CHECKS)
    code, _, err = proj.run("run", "--agent", "nope", "--dir", str(proj.dir), "task")
    assert code == 2 and "agent 'nope' is not defined" in err and "fake" in err
    assert fake_agent.calls() == []


def test_runs_are_logged_with_agent_trajectory_and_round(proj, fake_agent):
    proj.config(CHECKS)
    fake_agent.script([{"write": {"report.md": "TODO"}}, {"write": {"report.md": "## Summary"}}])
    _, data = run(proj)
    rows = store.runs_for("t")
    assert [(r["round"], r["status"], r["agent"], r["mode"]) for r in reversed(rows)] == \
        [(1, "fail", "fake", "run"), (2, "pass", "fake", "run")]
    assert rows[0]["task_id"] == rows[1]["task_id"]
    assert rows[1]["trajectory"].endswith(".eval/rounds/1/trajectory.jsonl")
    assert rows[0]["extra"]["loop_result"] == "pass"
    s = store.stats()["configs"]["t"]
    assert s["tasks"] == 1 and s["avg_rounds"] == 2 and s["compliance_rate"] == 1


def test_check_called_by_the_agent_inside_a_run_writes_nothing(proj, monkeypatch):
    proj.config(CHECKS)
    proj.write("report.md", "## Summary")
    monkeypatch.setenv("EVALCTL_OUTER", "1")
    code, data = proj.check()
    assert code == 0 and data["receipt"] is None
    assert any("outer `evalctl run`" in n for n in data["notes"])
    assert store.runs_for("t") == []


def test_builtin_agents_file_is_created_and_matches_the_example(home):
    from pathlib import Path
    names = set(agents.load_agents())
    assert names == {"claude", "codex"}
    assert (home / "agents.yaml").read_text() == Path(__file__).parents[1].joinpath("agents.example.yaml").read_text()
    a = agents.get_agent("codex")
    assert a.can_resume and a.session_id_field == "thread_id" and "resume" in a.resume


@pytest.mark.parametrize("agent, problem", [
    ({"start": "x --go"}, "no {prompt} placeholder"),
    ({"start": "x {prompt}", "resume": "x {prompt}", "output": "jsonl", "session_id_field": "id"}, "no {session_id} placeholder"),
    ({"start": "x {prompt}", "resume": "x {session_id} {prompt}"}, "`resume` needs `output: json`"),
    ({"start": "x {prompt}", "output": "xml"}, "`output` must be one of"),
    ({"start": "x {prompt}", "colour": "red"}, "unknown key(s): colour"),
])
def test_bad_agent_definitions_are_rejected(home, agent, problem):
    (home / "agents.yaml").write_text(yaml.safe_dump({"agents": {"a": agent}}))
    with pytest.raises(Exception) as e:
        agents.load_agents()
    assert problem in str(e.value)


def test_session_id_is_read_from_json_and_jsonl():
    a = agents.parse_agent("a", {"start": "x {prompt}", "output": "json", "session_id_field": "meta.session"}, "t")
    assert agents.find_session_id(a, '{"meta": {"session": "abc"}}') == "abc"
    a = agents.parse_agent("a", {"start": "x {prompt}", "output": "jsonl", "session_id_field": "session_id"}, "t")
    assert agents.find_session_id(a, 'noise\n{"session_id": "one"}\n{"other": 1}\n{"session_id": "two"}\n') == "two"
    assert agents.find_session_id(a, "not json at all") is None

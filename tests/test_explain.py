"""`evalctl explain`: trajectories of both agents, the rule-based fallback, the model path, clustering."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalctl import explain as ex, store, trajectory as tj

FX = Path(__file__).resolve().parents[1] / "fixtures" / "explain"
CASES = ["missed_material", "reasoning_error", "format_mismatch", "tool_error", "environment", "checker_false_positive"]
CHECKS = [{"id": "has_report", "type": "file", "path": "report.md", "min_size": 1},
          {"id": "no_todo", "type": "contains", "file": "report.md", "must_not": ["TODO"]}]


def expected(case):
    return json.loads((FX / case / "expected.json").read_text())


def explain_round(proj, case, *extra):
    code, out, err = proj.run("explain", "--round", str(FX / case), "--json", *extra)
    assert code == 0, err
    return json.loads(out)


# ---------------------------------------------------------------- trajectories

def test_claude_stream_json_becomes_numbered_steps():
    steps = tj.load([FX / "missed_material" / "trajectory.jsonl"])
    assert [(s.n, s.kind, s.tool) for s in steps] == [
        (1, "message", ""), (2, "tool", "Read"), (3, "tool", "Glob"), (4, "message", ""), (5, "tool", "Write"), (6, "message", "")]
    assert steps[1].input == {"file_path": "/work/task/brief.md"} and "finance lead" in steps[1].output
    assert steps[4].render().startswith("[5] TOOL Write: ")


def test_codex_json_becomes_numbered_steps():
    steps = tj.load([FX / "tool_error" / "trajectory.jsonl"])
    assert [(s.n, s.kind, s.tool, s.failed) for s in steps] == [
        (1, "message", "", False), (2, "tool", "shell", False), (3, "tool", "shell", True),
        (4, "tool", "shell", False), (5, "message", "", False)]
    assert "python3 extract.py" in steps[2].command() and "AttributeError" in steps[2].output
    assert "-> ERROR: Traceback" in steps[2].render()


def test_several_files_are_numbered_continuously_and_other_formats_are_skipped(tmp_path):
    junk = tmp_path / "plain.txt"
    junk.write_text("done\n")
    steps = tj.load([FX / "tool_error" / "trajectory.jsonl", junk, FX / "environment" / "trajectory.jsonl"])
    assert [s.n for s in steps] == list(range(1, 11)) and steps[5].source.endswith("environment/trajectory.jsonl")
    assert tj.readable([junk]) == []


def test_real_event_shapes(tmp_path):
    """Lines as captured from claude 2.1.322 and codex-cli 0.166.0, with the fields this reader does not use."""
    c = tmp_path / "c.jsonl"
    c.write_text("\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "hook_started", "hook_event": "SessionStart", "session_id": "s"},
        {"type": "system", "subtype": "init", "session_id": "s", "tools": ["Read"], "cwd": "/w"},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "thinking", "thinking": "", "signature": "abc"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls", "description": "List"}, "caller": {"type": "direct"}}]}},
        {"type": "rate_limit_event", "rate_limit_info": {}},
        {"type": "user", "message": {"content": [{"tool_use_id": "toolu_1", "type": "tool_result", "content": "in.txt", "is_error": False}]}},
        {"type": "user", "message": {"content": [{"tool_use_id": "toolu_x", "type": "tool_result", "content": [{"type": "text", "text": "orphan"}]}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        {"type": "result", "subtype": "success", "result": "Done.", "session_id": "s", "permission_denials": []}]))
    steps = tj.load([c])
    assert [(s.kind, s.tool, s.output or s.text) for s in steps] == [("tool", "Bash", "in.txt"), ("message", "", "Done.")]
    x = tmp_path / "x.jsonl"
    x.write_text("\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "ls", "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "ls", "aggregated_output": "in.txt\n", "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "changes": [{"path": "/w/out.txt", "kind": "add"}], "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "mcp_tool_call", "server": "slack", "tool": "send_message", "arguments": {"text": "hi"}, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_4", "type": "web_search", "query": "zfs scrub"}},
        {"type": "item.completed", "item": {"id": "item_5", "type": "reasoning", "text": "thinking"}},
        {"type": "turn.failed", "error": {"message": "stream disconnected"}}]))
    steps = tj.load([x])
    assert [(s.kind, s.tool) for s in steps] == [("tool", "shell"), ("tool", "file_change"), ("tool", "mcp__slack__send_message"),
                                                 ("tool", "web_search"), ("error", "")]
    assert steps[-1].failed and "stream disconnected" in steps[-1].text


# ---------------------------------------------------------------- without a model

@pytest.mark.parametrize("case", ["missed_material", "format_mismatch", "tool_error", "environment"])
def test_rules_get_category_and_step_right_where_the_trajectory_alone_shows_it(proj, case):
    d = explain_round(proj, case, "--no-model", "--lang", "en")
    want = expected(case)
    assert d["category"] == want["category"] and d["step"] in want["steps"] and d["method"] == "rules"
    assert d["sentence"].startswith("Because ") and d["sentence"].endswith(f"(step {d['step']}).")


def test_rules_see_the_file_that_was_never_opened_and_the_failed_command(proj):
    d = explain_round(proj, "missed_material", "--no-model", "--lang", "en")
    assert d["signals"]["never_opened"] == ["expenses.csv"]        # brief.md was read; report.md is the output
    assert d["sentence"] == "Because no step ever opened expenses.csv, the output was written without that material (step 5)."
    d = explain_round(proj, "environment", "--no-model", "--lang", "zh")
    assert d["signals"]["errors"][0] == {"step": 2, "environment": True, "what": "curl: (6) Could not resolve host: forum.homelab.example"}
    assert d["sentence"].startswith("因为") and "，所以" in d["sentence"] and d["sentence"].endswith("（步骤 2）。")
    assert d["label"] == "环境问题"


def test_rules_fall_back_to_reasoning_error_and_say_what_they_are(proj):
    d = explain_round(proj, "reasoning_error", "--no-model")
    assert d["category"] == "reasoning_error" and d["method"] == "rules"
    code, text, _ = proj.run("explain", "--round", str(FX / "reasoning_error"))     # no model configured at all
    assert "method: rules" in text and "no model was usable, so this is a rule-based guess" in text


# ---------------------------------------------------------------- with a model

def answer(category, step, missing="the agent never opened expenses.csv", wrong="The figures in report.md are invented"):
    return json.dumps({"category": category, "step": step, "missing": missing, "wrong": wrong,
                       "evidence": f"Step {step} writes the report; no step reads expenses.csv."})


def test_model_gets_task_failures_numbered_steps_and_facts(proj, grader):
    grader.reply = lambda prompt: answer("missed_material", 4)
    d = explain_round(proj, "missed_material", "--lang", "en")
    assert (d["category"], d["step"], d["method"], d["segments"]) == ("missed_material", 4, "model deepseek:m", 1)
    assert d["sentence"] == "Because the agent never opened expenses.csv, the figures in report.md are invented (step 4)."
    prompt = grader.requests[-1]["body"]["messages"][-1]["content"]
    assert "Write the weekly expense report" in prompt                                   # the task
    assert "report_md_numbers_from_inputs (numbers_in_inputs)" in prompt and "820.00 is not in the inputs" in prompt
    assert "[2] TOOL Read:" in prompt and "[5] TOOL Write:" in prompt                    # numbered steps
    assert "files named in the task that no step ever opened: expenses.csv" in prompt    # facts
    for cat in ex.CATEGORIES:
        assert f"- {cat}:" in prompt
    assert "Steps are numbered 1 to 6" in prompt
    code, text, _ = proj.run("explain", "--round", str(FX / "missed_material"), "--lang", "zh")
    assert "cause: missed_material (漏读材料)" in text and "earliest step that went wrong: 4 of 6" in text
    assert "（步骤 4）。" in text and "Chinese" in grader.requests[-1]["body"]["messages"][-1]["content"]


def test_bad_model_answers_are_retried_then_reported(proj, grader):
    replies = iter([answer("laziness", 4), answer("tool_error", 3)])
    grader.reply = lambda prompt: next(replies)
    assert explain_round(proj, "tool_error")["category"] == "tool_error"
    grader.reply = lambda prompt: answer("tool_error", 99)              # a step that does not exist
    code, _, err = proj.run("explain", "--round", str(FX / "tool_error"))
    assert code == 2 and "step 99 is not one of 1..5" in err


def test_long_trajectory_is_read_in_segments(grader, tmp_path):
    """200 steps, far over the per-call budget: every segment is summarised, then one verdict over the notes."""
    events = [{"type": "thread.started", "thread_id": "t"}]
    for i in range(1, 201):
        out = "x" * 300 if i != 137 else "jq: error: Cannot index number with string"
        events.append({"type": "item.completed", "item": {"id": f"i{i}", "type": "command_execution",
                       "command": f"step-{i} --do-something", "aggregated_output": out, "exit_code": 5 if i == 137 else 0, "status": "completed"}})
    path = tmp_path / "long.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events))
    seen = []

    def reply(prompt):
        seen.append(prompt)
        if "Write notes for an analyst" in prompt:
            first = int(prompt.split("These are steps ")[1].split(" ")[0])
            return f"Routine commands from step {first}." + (" Step 137: jq failed, ignored." if "[137]" in prompt else "")
        return answer("tool_error", 137, "the jq command at step 137 failed", "the rest of the pipeline ran on partial data")
    grader.reply = reply
    checks = [{"id": "post_count", "type": "schema", "status": "fail", "summary": "too few", "failures": ["posts.jsonl: 5 record(s)"]}]
    d = ex.explain_data(checks, [path], task="Scrape the forum", segment_chars=20000)
    notes, verdict = [p for p in seen if "Write notes" in p], [p for p in seen if "Write notes" not in p]
    assert d["segments"] == len(notes) > 3 and len(verdict) == 1
    # the segments cover every step exactly once, in order
    ranges = [tuple(map(int, p.split("These are steps ")[1].split(" of an agent")[0].split(" to "))) for p in notes]
    assert ranges[0][0] == 1 and ranges[-1][1] == 200 and all(a[1] + 1 == b[0] for a, b in zip(ranges, ranges[1:]))
    assert all(len(p) < 30000 for p in seen)
    # the verdict is given over the notes, with their step numbers, not over the raw log
    assert "Step 137: jq failed, ignored." in verdict[0] and "x" * 100 not in verdict[0]
    assert d["step"] == 137 and "(step 137)" in d["sentence"] and "[137] TOOL shell" in d["step_shown"]


# ---------------------------------------------------------------- by run id, and clusters

@pytest.fixture
def failed_runs(proj, fake_agent):
    proj.config(CHECKS, max_rounds=1)
    ids = []
    for i in range(3):
        fake_agent.script([{"write": {"report.md": f"TODO {i}"}}])
        code, out, _ = proj.run("run", "--agent", "fake", "--dir", str(proj.dir), "--json", "Write report.md from notes.csv")
        ids.append(json.loads(out)["run_id"])
    return ids


def test_explain_by_run_id_uses_the_recorded_trajectory(proj, grader, failed_runs):
    grader.reply = lambda prompt: answer("missed_material", 1, "notes.csv was never read", "report.md still says TODO")
    code, out, err = proj.run("explain", str(failed_runs[0]), "--json", "--lang", "en")
    d = json.loads(out)
    assert code == 0 and d["run_id"] == failed_runs[0] and d["config"] == "t" and d["agent"] == "fake"
    assert d["failed_checks"] == ["no_todo"] and d["signals"]["never_opened"] == ["notes.csv"]
    prompt = grader.requests[-1]["body"]["messages"][-1]["content"]
    assert "Write report.md from notes.csv" in prompt and "[1] TOOL Write" in prompt
    n = len(grader.requests)
    code, text, _ = proj.run("explain", str(failed_runs[0]), "--lang", "en")           # stored: no second model call
    assert len(grader.requests) == n and "[cached]" in text and f"run #{failed_runs[0]}" in text
    proj.run("explain", str(failed_runs[0]), "--lang", "en", "--refresh")
    assert len(grader.requests) == n + 1


def test_explain_refuses_runs_that_did_not_fail_or_have_no_trajectory(proj, failed_runs):
    proj.config(CHECKS)
    proj.write("report.md", "fine")
    _, data = proj.check()
    code, _, err = proj.run("explain", str(data["run_id"]))
    assert code == 2 and "did not fail" in err
    proj.write("report.md", "TODO")
    _, data = proj.check()                                           # a plain check: nobody recorded a trajectory
    code, _, err = proj.run("explain", str(data["run_id"]))
    assert code == 2 and "no readable trajectory" in err and "--trajectory FILE" in err
    code, text, _ = proj.run("explain", str(data["run_id"]), "--no-model", "--trajectory", str(FX / "tool_error" / "trajectory.jsonl"))
    assert code == 0 and "cause: tool_error" in text
    assert proj.run("explain", "99999")[0] == 2


def test_cluster_groups_failed_tasks_by_cause(proj, grader, failed_runs):
    causes = iter([("missed_material", "notes.csv was never read"), ("missed_material", "notes.csv was skipped"),
                   ("environment", "the sandbox blocked the write")])

    def reply(prompt):
        cat, missing = next(causes)
        return answer(cat, 1, missing, "report.md still says TODO")
    grader.reply = reply
    code, out, _ = proj.run("explain", "--cluster", "t", "--json", "--lang", "en")
    c = json.loads(out)
    assert code == 0 and c["explained"] == 3
    assert [(g["category"], g["runs"]) for g in c["clusters"]] == [("missed_material", 2), ("environment", 1)]
    assert c["clusters"][0]["checks"] == {"no_todo": 2} and c["clusters"][0]["share"] == pytest.approx(2 / 3)
    code, text, _ = proj.run("explain", "--cluster", "t", "--lang", "en")               # all three are stored by now
    assert " 67%  missed_material (漏读材料): 2 task(s)" in text and "Because notes.csv was" in text
    code, _, err = proj.run("explain", "--cluster", "nothing_here")
    assert code == 2 and "no failed run recorded" in err

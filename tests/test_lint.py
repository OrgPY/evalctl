"""`evalctl lint`: reviewing the check config itself, standalone and as a gate at the start of check."""
from __future__ import annotations

import json

import pytest

TASK = ("Write report.md about the incident. It must have a Summary section, list every affected service, "
        "and state the root cause.")
# Three planted problems: (1) nothing checks the root cause or the affected services,
# (2) quality is "checked" by looking for exact words, (3) the reference output fails a check.
BAD = [
    {"id": "has_summary", "type": "contains", "file": "report.md", "must": ["## Summary"]},
    {"id": "sounds_thorough", "type": "contains", "file": "report.md", "must": ["thoroughly investigated"]},
    {"id": "long_enough", "type": "file", "path": "report.md", "min_size": 5000},
]
FINAL = "## Summary\nThe cache node ran out of memory.\n\n## Affected services\n- api\n- search\n\n## Root cause\nA leak in the session store.\n"


def model_says(*findings):
    return lambda prompt: json.dumps({"findings": list(findings)})


@pytest.fixture
def final(tmp_path):
    d = tmp_path / "final"
    d.mkdir()
    (d / "report.md").write_text(FINAL)
    return d


def lint(proj, *extra, name="t"):
    code, out, err = proj.run("lint", name, "--json", *extra)
    data = json.loads(out)
    return code, (data["error"] if data.get("status") == "error" else data)


def kinds(result):
    return sorted((f["severity"], f["kind"], f["check"]) for f in result["findings"])


def test_problem_config_all_three_problems_are_reported(proj, grader, final):
    proj.config(BAD, task=TASK, pointer=False)
    grader.reply = model_says(
        {"kind": "uncovered_requirement", "check": None, "requirement": "state the root cause",
         "message": "No check verifies that the report states the root cause; add a heading check or a rubric criterion."},
        {"kind": "uncovered_requirement", "check": None, "requirement": "list every affected service",
         "message": "No check verifies the list of affected services."},
        {"kind": "fragile_keyword", "check": "sounds_thorough", "requirement": None, "message": "dup of the static finding"})
    code, r = lint(proj, "--final", str(final))
    assert code == 1                                               # a critical finding
    assert kinds(r) == [
        ("critical", "final_fails", "long_enough"),                # the known-good output fails its own check
        ("critical", "final_fails", "sounds_thorough"),
        ("warning", "fragile_keyword", "sounds_thorough"),         # found by rule; the model's copy is dropped
        ("warning", "uncovered_requirement", None),
        ("warning", "uncovered_requirement", None),
    ]
    assert {f["requirement"] for f in r["findings"] if f["kind"] == "uncovered_requirement"} == \
        {"state the root cause", "list every affected service"}
    # the model saw the task and the checks
    prompt = grader.requests[-1]["body"]["messages"][-1]["content"]
    assert "state the root cause" in prompt and "sounds_thorough" in prompt and "thoroughly investigated" in prompt
    code, text, _ = proj.run("lint", "t", "--final", str(final))
    assert "2 critical, 3 warning" in text and "[CRITICAL] long_enough: reference output fails" in text
    assert "requirement: state the root cause" in text and "[cached]" in text


def test_static_rules_need_no_model(proj):
    proj.config([
        {"id": "a", "type": "contains", "file": "r.md", "must": ["DRAFT"]},
        {"id": "b", "type": "contains", "file": "r.md", "must_not": ["DRAFT"]},
        {"id": "a2", "type": "contains", "file": "r.md", "must": ["DRAFT"]},
        {"id": "exists", "type": "file", "path": "tmp.log"},
        {"id": "gone", "type": "file", "path": "tmp.log", "exists": False},
        {"id": "count", "type": "schema", "file": "x.jsonl", "min_records": 9, "max_records": 3},
        {"id": "short", "type": "contains", "file": "r.md", "must_not": ["no"]},
        {"id": "ok_structure", "type": "contains", "file": "r.md", "must": ["## Sources", "| Total |"], "must_not": ["TODO"]},
        {"id": "q", "type": "llm_rubric", "file": "r.md",
         "rubric": [{"id": "vague", "text": "The report is helpful and well written."},
                    {"id": "sharp", "text": "Every claim cites a source. A claim without a citation fails."}]},
    ], pointer=False)
    code, r = lint(proj, "--static")
    assert code == 1 and r["model"] is None
    assert kinds(r) == [
        ("critical", "conflict", "a"), ("critical", "conflict", "a2"), ("critical", "conflict", "count"),
        ("critical", "conflict", "exists"),
        ("warning", "duplicate", "a2"), ("warning", "fragile_keyword", "a"), ("warning", "fragile_keyword", "a2"),
        ("warning", "fragile_keyword", "short"), ("warning", "pass_only_rubric", "q.vague"),
    ]
    assert any("coverage of the task requirements was not reviewed" in n for n in r["notes"])


def test_clean_config_passes_and_final_is_taken_from_the_config(proj, grader, final):
    proj.config([{"id": "has_summary", "type": "contains", "file": "report.md", "must": ["## Summary", "## Root cause"]}],
                task=TASK, reference={"final": str(final)}, pointer=False)
    grader.reply = model_says()
    code, r = lint(proj)
    assert code == 0 and r["findings"] == [] and r["model"] == "deepseek:m" and r["notes"] == []


def test_result_is_cached_by_config_hash(proj, grader, final):
    path = proj.config(BAD[:1], task=TASK, pointer=False)
    grader.reply = model_says()
    _, first = lint(proj, "--final", str(final))
    n = len(grader.requests)
    _, again = lint(proj, "--final", str(final))
    assert again["cached"] and len(grader.requests) == n
    _, forced = lint(proj, "--final", str(final), "--refresh")
    assert not forced["cached"] and len(grader.requests) == n + 1
    path.write_text(path.read_text() + "\n# edited\n")                 # the config changed: reviewed again
    _, changed = lint(proj, "--final", str(final))
    assert not changed["cached"] and len(grader.requests) == n + 2
    (final / "report.md").write_text(FINAL + "more\n")                 # so did the reference output
    assert not lint(proj, "--final", str(final))[1]["cached"]


def test_without_a_model_lint_says_so_instead_of_passing(proj):
    proj.config(BAD[:1], pointer=False)
    code, err = lint(proj)
    assert code == 2 and "needs a model" in err and "--static" in err   # no silent "all fine"
    assert lint(proj, "--static")[0] == 0


def test_model_answer_that_is_not_json_is_an_error(proj, grader):
    proj.config(BAD[:1], task=TASK, pointer=False)
    grader.raw_answer = "Looks good to me!"
    code, err = lint(proj)
    assert code == 2 and "the model review failed" in err


def test_no_task_description_means_coverage_is_not_reviewed(proj, grader):
    proj.config(BAD[:1], pointer=False)
    grader.reply = model_says({"kind": "uncovered_requirement", "check": None, "requirement": "x", "message": "made up"})
    code, r = lint(proj)
    assert r["findings"] == [] and any("coverage" in n and "was not reviewed" in n for n in r["notes"])


# ---------------------------------------------------------------- inside the loop

def test_check_stops_with_exit_2_on_a_critical_finding(proj, grader, final):
    proj.config(BAD, task=TASK, reference={"final": str(final)})
    proj.pointer(loop={"lint": {"when": "config_changed", "block_on": "critical"}})
    proj.write("report.md", FINAL)
    grader.reply = model_says()
    code, out, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "lint found 2 blocking problem(s)" in err and "problem with the rules, not with the output" in err
    assert "[CRITICAL] long_enough" in err
    assert not (proj.dir / ".eval/receipt.json").exists()
    code, data = proj.check()
    assert code == 2 and data["status"] == "error" and data["lint"]["findings"]


def test_config_changed_reviews_once_per_config_version(proj, grader, final):
    good = [{"id": "has_summary", "type": "contains", "file": "report.md", "must": ["## Summary"]}]
    path = proj.config(good, task=TASK, reference={"final": str(final)})
    proj.pointer(loop={"lint": {"when": "config_changed"}})
    proj.write("report.md", FINAL)
    grader.reply = model_says({"kind": "uncovered_requirement", "check": None, "requirement": "root cause", "message": "No check."})
    code, data = proj.check()
    assert code == 0 and len(grader.requests) == 1                  # warnings do not block
    assert data["ran"]["lint"] == {"ran": True, "config_sha": data["config"]["sha256"], "cached": False, "critical": 0, "warning": 1}
    assert "lint: 1 warning(s)" in data["text"]
    proj.write("report.md", FINAL + "edit\n")
    code, data = proj.check()
    assert len(grader.requests) == 1 and data["ran"]["lint"]["cached"]   # same config: no second review
    assert proj.verify()[0] == 0
    path.write_text(path.read_text() + "\n# edited\n")
    proj.check()
    assert len(grader.requests) == 2


def test_always_reviews_every_time_and_never_skips(proj, grader):
    proj.config([{"id": "f", "type": "file", "path": "report.md"}])
    proj.write("report.md", "x")
    grader.reply = model_says()
    proj.pointer(loop={"lint": {"when": "always"}})
    proj.check(), proj.check()
    assert len(grader.requests) == 2
    proj.pointer(loop={"lint": {"when": "never"}})
    code, data = proj.check()
    assert len(grader.requests) == 2 and data["ran"]["lint"] is None


def test_block_on_warning_and_never(proj, grader):
    proj.config([{"id": "f", "type": "file", "path": "report.md"}], task=TASK)
    proj.write("report.md", "x")
    grader.reply = model_says({"kind": "uncovered_requirement", "check": None, "requirement": "r", "message": "m"})
    proj.pointer(loop={"lint": {"when": "always", "block_on": "warning"}})
    assert proj.check()[0] == 2
    proj.pointer(loop={"lint": {"when": "always", "block_on": "never"}})
    assert proj.check()[0] == 0


def test_required_lint_that_cannot_run_is_a_setup_error(proj):
    proj.config([{"id": "f", "type": "file", "path": "report.md"}])
    proj.pointer(loop={"lint": {"when": "config_changed"}})
    proj.write("report.md", "x")
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "lint is required by the loop settings" in err


def test_verify_fails_when_lint_is_required_but_the_receipt_has_none(proj, grader):
    proj.config([{"id": "f", "type": "file", "path": "report.md"}])
    proj.write("report.md", "x")
    assert proj.check()[0] == 0
    proj.pointer(loop={"lint": {"when": "config_changed"}})
    code, v = proj.verify()
    assert code == 1 and "requires lint" in v["reasons"][0]

"""`evalctl bench`: a suite of stored tasks, and the comparison of two bench runs."""
from __future__ import annotations

import json

import pytest
import yaml

from evalctl import bench

CHECKS = [{"id": "no_todo", "type": "contains", "file": "report.md", "must_not": ["TODO"]},
          {"id": "has_summary", "type": "contains", "file": "report.md", "must": ["## Summary"]}]
GOOD, BAD, HALF = "## Summary\nok\n", "TODO\n", "## Summary\nTODO\n"


@pytest.fixture
def suite(proj, tmp_path):
    proj.config(CHECKS, name="report", pointer=False, max_rounds=2)
    seed = tmp_path / "suite" / "seed_a"
    seed.mkdir(parents=True)
    (seed / "brief.md").write_text("write about a")
    path = tmp_path / "suite" / "reports.yaml"
    path.write_text(yaml.safe_dump({"name": "reports", "config": "report", "tasks": [
        {"id": "a", "prompt": "Write report a", "files": "seed_a"},
        {"id": "b", "prompt": "Write report b"},
        {"id": "c", "prompt": "Write report c"}]}))
    return path


def run_bench(proj, suite, *extra):
    code, out, _ = proj.run("bench", str(suite), "--agent", "fake", "--json", *extra)
    return code, json.loads(out)


def w(text):
    return {"write": {"report.md": text}}


def test_suite_reports_pass_rate_and_per_check_fail_rate(proj, suite, fake_agent, home):
    # a: passes at once; b: fails then passes; c: never passes
    fake_agent.script([w(GOOD)], [w(BAD), w(GOOD)], [w(BAD), w(HALF)])
    code, r = run_bench(proj, suite)
    assert code == 1
    assert {t: (v["passes"], v["attempts"][0]["rounds"]) for t, v in r["tasks"].items()} == \
        {"a": (1, 1), "b": (1, 2), "c": (0, 2)}
    s = r["summary"]
    assert s["pass_rate"] == pytest.approx(2 / 3) and s["pass_k"] == pytest.approx(2 / 3)
    assert s["checks"]["no_todo"]["fail_rate"] == pytest.approx(1 / 3)
    assert s["checks"]["no_todo"]["failed_first_round"] == 2
    assert s["checks"]["has_summary"]["fail_rate"] == 0 and s["checks"]["has_summary"]["passed"] == 3
    # each attempt has its own working directory with the seed files, the rounds and (on a pass) a receipt
    work = home / "bench" / r["run_id"] / "tasks" / "a" / "1"
    assert (work / "brief.md").read_text() == "write about a"
    assert (work / ".eval/rounds/1/check.json").is_file() and (work / ".eval/receipt.json").is_file()
    assert json.loads((home / "bench" / r["run_id"] / "result.json").read_text())["summary"] == s
    text = bench.render(r)
    assert "pass rate: 67%" in text and "c: 0/1" in text and "failing: no_todo" in text


def test_repeat_gives_pass_to_the_k(proj, suite, fake_agent):
    # 3 tasks x 2 attempts, in order a1 a2 b1 b2 c1 c2: a passes twice, b once, c twice
    fake_agent.script([w(GOOD)], [w(GOOD)], [w(GOOD)], [w(BAD), w(BAD)], [w(GOOD)], [w(GOOD)])
    code, r = run_bench(proj, suite, "--repeat", "2")
    s = r["summary"]
    assert s["attempts"] == 6 and s["pass_rate"] == pytest.approx(5 / 6)
    assert s["pass_k"] == pytest.approx(2 / 3)            # b did not pass all of its 2 attempts
    assert r["tasks"]["b"]["pass_all"] is False
    assert s["checks"]["no_todo"]["passed"] == 5          # how often each check passed, out of 6
    assert "pass^2: 67%" in bench.render(r)


def test_compare_lists_better_and_worse_tasks(proj, suite, fake_agent):
    fake_agent.script([w(GOOD)], [w(BAD), w(GOOD)], [w(BAD), w(HALF)])     # a pass(1r), b pass(2r), c fail
    _, before = run_bench(proj, suite)
    fake_agent.script([w(BAD), w(BAD)], [w(GOOD)], [w(GOOD)])              # a fail, b pass(1r), c pass
    _, after = run_bench(proj, suite)
    code, out, _ = proj.run("bench", "--compare", before["run_id"], after["run_id"], "--json")
    c = json.loads(out)
    assert code == 1                                                        # something got worse
    assert [x["task"] for x in c["worse"]] == ["a"] and c["worse"][0]["b_failing"] == ["has_summary", "no_todo"]
    assert [x["task"] for x in c["better"]] == ["c"] and c["better"][0]["a_failing"] == ["no_todo"]
    assert c["same"] == ["b"]
    assert c["rounds_changed"][0]["task"] == "b" and (c["rounds_changed"][0]["a_rounds"], c["rounds_changed"][0]["b_rounds"]) == (2, 1)
    assert {"check": "has_summary", "a_fail_rate": 0.0, "b_fail_rate": pytest.approx(1 / 3)} in c["checks"]
    code, text, _ = proj.run("bench", "--compare", before["run_id"], after["run_id"])
    assert "WORSE in B (1)" in text and "a: 1/1 -> 0/1" in text and "BETTER in B (1)" in text and "c: 0/1 -> 1/1" in text
    # identical runs: nothing better, nothing worse, exit 0
    code, out, _ = proj.run("bench", "--compare", before["run_id"], before["run_id"], "--json")
    assert code == 0 and json.loads(out)["worse"] == [] and json.loads(out)["better"] == []


def test_only_selected_tasks(proj, suite, fake_agent):
    fake_agent.script([w(GOOD)])
    code, r = run_bench(proj, suite, "--task", "b")
    assert code == 0 and list(r["tasks"]) == ["b"]


def test_bad_suite_and_unknown_run(proj, tmp_path, fake_agent):
    bad = tmp_path / "s.yaml"
    bad.write_text("tasks: [{id: a}]")
    code, _, err = proj.run("bench", str(bad), "--agent", "fake")
    assert code == 2 and "each task needs a `prompt`" in err
    code, _, err = proj.run("bench", "--compare", "nope", "nope2")
    assert code == 2 and "bench run 'nope' not found" in err

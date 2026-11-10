from __future__ import annotations

import json
import time

from evalctl import store

CHECKS = [{"id": "has_report", "type": "file", "path": "report.md", "min_size": 1},
          {"id": "no_todo", "type": "contains", "file": "report.md", "must_not": ["TODO"]}]


def test_exit_codes(proj):
    proj.config(CHECKS)
    proj.write("report.md", "TODO")
    assert proj.check()[0] == 1
    proj.write("report.md", "done")
    assert proj.check()[0] == 0
    proj.config([{"type": "nonsense"}])
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "unknown check type 'nonsense'" in err


def test_no_config_explains_how_to_set_one_up(proj):
    proj.config(CHECKS, name="report", pointer=False)
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2
    assert ".evalctl.yaml" in err and "config: <name>" in err and "--config <name>" in err
    assert "report" in err  # lists the configs that exist


def test_config_flag_wins_over_pointer(proj):
    proj.write("report.md", "TODO")
    proj.config(CHECKS, name="strict", pointer=False)
    proj.config([{"type": "file", "path": "report.md"}], name="loose", pointer=True)
    assert proj.check()[1]["config"]["name"] == "loose"
    code, data = proj.check("--config", "strict")
    assert code == 1 and data["config"]["name"] == "strict"
    code, _, err = proj.run("check", "--dir", str(proj.dir), "--config", "missing")
    assert code == 2 and "not found" in err


def test_pointer_is_found_in_parent_directories(proj, monkeypatch):
    proj.config(CHECKS)
    proj.write("report.md", "done")
    sub = proj.dir / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    code, out, _ = proj.run("check", "--json")
    data = json.loads(out)
    # Without --dir, the directory holding .evalctl.yaml is the one that gets checked.
    assert code == 0 and data["dir"] == str(proj.dir.resolve())
    assert proj.run("verify")[0] == 0


def test_config_inside_the_workdir_is_rejected(proj):
    inside = proj.write("my_rules.yaml", "checks:\n  - {type: file, path: '*'}\n")
    code, _, err = proj.run("check", "--dir", str(proj.dir), "--config", str(inside))
    assert code == 2 and "inside the working directory" in err


def test_bad_pointer_and_unknown_option_are_config_errors(proj):
    proj.config(CHECKS)
    (proj.dir / ".evalctl.yaml").write_text("something: else\n")
    assert proj.run("check", "--dir", str(proj.dir))[0] == 2
    proj.config([{"type": "file", "path": "x", "min_sise": 3}])
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "unknown option(s) for file: min_sise" in err


def test_text_report_is_an_actionable_checklist(proj):
    proj.config([dict(CHECKS[0]), dict(CHECKS[1], hint="Finish the section instead of leaving a marker.")])
    proj.write("report.md", "intro\nTODO write results\n")
    code, out, _ = proj.run("check", "--dir", str(proj.dir))
    assert code == 1
    assert out.splitlines()[0] == "evalctl check: FAIL  (1/2 checks passed, 1 failed)"
    assert "[FAIL] no_todo (contains)" in out and "[PASS] has_report (file)" in out
    assert "report.md:2: forbidden text 'TODO' found" in out
    assert "how to fix: Finish the section" in out
    assert "Round 1 of 3. Fix only the FAIL items above" in out
    assert out.index("[FAIL]") < out.index("[PASS]")


def test_json_report(proj):
    proj.config(CHECKS)
    proj.write("report.md", "TODO")
    code, data = proj.check()
    assert code == 1 and data["status"] == "fail" and data["passed"] is False
    assert data["round"] == 1 and data["max_rounds"] == 3
    assert [c["status"] for c in data["checks"]] == ["pass", "fail"]
    assert data["checks"][1]["failures"] and "evalctl check: FAIL" in data["text"]


def test_rounds_count_changed_output_only(proj):
    proj.config(CHECKS, max_rounds=2)
    proj.write("report.md", "TODO 1")
    assert proj.check()[1]["round"] == 1
    assert proj.check()[1]["round"] == 1          # same files: still round 1
    proj.write("report.md", "TODO 2")
    code, data = proj.check()
    assert data["round"] == 2 and "round limit reached" in data["next_step"]
    assert "Do not deliver this output as passing" in data["next_step"]
    proj.write("report.md", "done")
    code, data = proj.check()
    assert code == 0 and data["round"] == 3
    proj.write("report.md", "done, and edited later")   # new work after a pass: new task
    assert proj.check()[1]["round"] == 1


def test_stale_failing_task_starts_over(proj, monkeypatch):
    proj.config(CHECKS)
    proj.write("report.md", "TODO 1")
    proj.check()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + (store.TASK_GAP_HOURS + 1) * 3600)
    proj.write("report.md", "TODO 2")
    assert proj.check()[1]["round"] == 1


def test_stats(proj, tmp_path, home):
    proj.config(CHECKS, name="report")
    # task 1: fails twice on no_todo, then passes -> 3 rounds
    for body in ("TODO a", "TODO b", "done"):
        proj.write("report.md", body)
        proj.check()
    # task 2 in another directory: fails once and is left failing
    other = tmp_path / "other"
    other.mkdir()
    (other / ".evalctl.yaml").write_text("config: report\n")
    proj.run("check", "--dir", str(other))
    code, out, _ = proj.run("stats", "--json")
    s = json.loads(out)["configs"]["report"]
    assert s["tasks"] == 2 and s["runs"] == 4
    assert s["pass_rate"] == 0.5 and s["first_round_pass_rate"] == 0
    assert s["avg_rounds"] == 2.0
    assert s["compliance_rate"] == 0.5
    # no_todo: two rounds in task 1, plus task 2 where report.md does not exist at all
    assert s["top_failing_checks"][0] == {"id": "no_todo", "failed_rounds": 3}
    assert {"id": "has_report", "failed_rounds": 1} in s["top_failing_checks"]

    # Editing the output after the pass, without re-checking, breaks compliance.
    proj.write("report.md", "done, then quietly edited")
    s = json.loads(proj.run("stats", "--json")[1])["configs"]["report"]
    assert s["pass_rate"] == 0.5 and s["compliance_rate"] == 0.0

    code, out, _ = proj.run("stats")
    assert "compliance rate: 0%" in out and "average rounds: 2.0" in out and "no_todo" in out

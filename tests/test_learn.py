"""learn / review / feedback, driven by the shipped draft + final example (fixtures/expense_report)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from evalctl import learn as lrn, store

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "fixtures" / "expense_report"
INPUTS, DRAFT, FINAL = FX / "inputs", FX / "draft", FX / "final"


def run_learn(proj, draft=DRAFT, final=FINAL, inputs=INPUTS, name="expense_report"):
    args = ["learn", "--draft", str(draft), "--final", str(final), "--name", name, "--json"]
    if inputs:
        args += ["--inputs", str(inputs)]
    code, out, err = proj.run(*args)
    assert code == 0, err
    return json.loads(out)


def answers(monkeypatch, *replies):
    it = iter(replies)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)


def edit_id(doc, needle):
    hits = [e["id"] for e in doc["edits"] if needle in e["before"] + e["after"] or needle == e["file"]]
    assert len(hits) == 1, (needle, hits)
    return hits[0]


# ---------------------------------------------------------------- learn

# what the human changed (text that identifies the edit) -> the check that must capture it
EXPECTED = {
    "11/02/2026": "report_md_dates_iso",
    "TODO: confirm": "report_md_no_placeholders",
    "175.55": "report_md_numbers_from_inputs",
    "| Office |": "report_md_covers_category",
    "1,341.94": "report_md_totals_add_up",
    "## Sources": "report_md_sections",
    '"count": 10': "summary_json_structure",
    '"total": "1351.94"': "summary_json_structure",
    "notes.tmp": "notes_tmp_absent",
}


def test_learn_turns_each_edit_into_its_check(proj):
    doc = run_learn(proj)
    cands = {c["id"]: c for c in doc["candidates"]}
    for needle, cid in EXPECTED.items():
        eid = edit_id(doc, needle)
        assert cid in cands, f"no candidate {cid}"
        c = cands[cid]
        assert eid in c["edits"], f"{cid} is not attributed to {eid} ({needle})"
        assert c["source"] == "edit" and c["scope"] == "general" and c["enabled"]
        assert c["validation"] == {"final": "pass", "draft": "fail"}
    # every edit is accounted for: it has a check, or the report says why not
    explained = {u["id"] for u in doc["unconverted_edits"]}
    assert all(e["checks"] or e["id"] in explained for e in doc["edits"])


def test_no_candidate_fails_the_final_and_edit_checks_fail_the_draft(proj):
    doc = run_learn(proj)
    assert len(doc["candidates"]) >= 12
    for c in doc["candidates"]:
        assert lrn.run_check(c["check"], FINAL, INPUTS).status == "pass", c["id"]
        if c["source"] == "edit":
            assert lrn.run_check(c["check"], DRAFT, INPUTS).status == "fail", c["id"]
        assert c["kind"] in ("structure", "format", "relation", "content")
        assert c["scope"] in ("general", "case-specific") and c["basis"]


def test_features_of_the_final_become_checks_even_where_the_draft_was_right(proj):
    cands = {c["id"]: c for c in run_learn(proj)["candidates"]}
    for cid in ("report_md_exists", "report_md_two_decimals", "summary_json_numbers_from_inputs",
                "summary_json_covers_category"):
        assert cands[cid]["source"] == "feature" and cands[cid]["validation"]["draft"] == "pass"
    assert cands["report_md_totals_add_up"]["check"] == {
        "type": "table_sum", "file": "report.md", "hint": cands["report_md_totals_add_up"]["check"]["hint"]}
    assert cands["report_md_numbers_from_inputs"]["kind"] == "relation"


def test_concrete_numbers_never_end_up_in_a_check(proj):
    doc = run_learn(proj)
    for c in doc["candidates"]:
        body = yaml.safe_dump({k: v for k, v in c["check"].items() if k != "hint"})
        for figure in ("1351", "1,351", "157.55", "412.80", "796.20", "104.19", "356.00", "2026"):
            assert figure not in body, f"{c['id']} hard-codes {figure}"
    # the largest-expense edit swapped one input figure for another: no relation explains it
    eid = edit_id(doc, "Hotel Lumen")
    note = {u["id"]: u["reason"] for u in doc["unconverted_edits"]}[eid]
    assert "numeric part was not converted" in note


def test_case_specific_checks_are_disabled_by_default(proj):
    doc = run_learn(proj)
    specific = [c for c in doc["candidates"] if c["scope"] == "case-specific"]
    assert {c["id"] for c in specific} >= {"report_md_no_hotel_lumen", "report_md_no_biggest_spend_bucket_by"}
    assert all(not c["enabled"] for c in specific)
    assert all(c["enabled"] for c in doc["candidates"] if c["scope"] == "general")
    wording = edit_id(doc, "biggest spend bucket")
    assert all(wording in c["edits"] for c in specific if "bucket" in c["id"])


def test_candidates_that_fail_validation_are_discarded_and_listed(proj, tmp_path):
    draft, final = tmp_path / "d", tmp_path / "f"
    draft.mkdir(), final.mkdir()
    # "beta gamma" was removed from line 1 but still stands in line 2 of the final: a ban would fail the final.
    # "all good now" was added to line 3 but the draft already had it in line 4: requiring it does not fail the draft.
    (draft / "a.txt").write_text("alpha beta gamma delta\nkeep beta gamma here\nstatus pending review\nnote: all good now\n")
    (final / "a.txt").write_text("alpha delta\nkeep beta gamma here\nstatus all good now\nnote: all good now\n")
    doc = run_learn(proj, draft, final, inputs=None, name="t")
    reasons = {d["id"]: d["reason"] for d in doc["discarded"]}
    assert "fails on the final output" in reasons["a_txt_no_beta_gamma"]
    assert "draft does not fail it" in reasons["a_txt_has_all_good_now"]
    kept = {c["id"] for c in doc["candidates"]}
    assert "a_txt_no_pending_review" in kept and not kept & set(reasons)
    code, out, _ = proj.run("learn", "--draft", str(draft), "--final", str(final), "--name", "t")
    assert "Discarded (2)" in out and "a_txt_no_beta_gamma" in out


def test_strict_number_rule_falls_back_when_the_final_has_small_counts(proj, tmp_path):
    inputs, draft, final = tmp_path / "i", tmp_path / "d", tmp_path / "f"
    for d in (inputs, draft, final):
        d.mkdir()
    (inputs / "data.csv").write_text("item,price\npen,2.50\nink,7.25\n")
    (draft / "r.md").write_text("Top 5 picks. Pen costs 2.55.\n")
    (final / "r.md").write_text("Top 5 picks. Pen costs 2.50.\n")
    c = {c["id"]: c for c in run_learn(proj, draft, final, inputs, "t")["candidates"]}["r_md_numbers_from_inputs"]
    assert c["check"]["ignore_small_integers"] == 10 and "strict rule fails" in c["basis"]
    assert c["validation"] == {"final": "pass", "draft": "fail"} and c["edits"] == ["E1"]


def test_learn_rejects_bad_arguments(proj, tmp_path):
    code, _, err = proj.run("learn", "--draft", str(DRAFT), "--final", str(DRAFT), "--name", "x")
    assert code == 2 and "same directory" in err
    code, _, err = proj.run("learn", "--draft", str(DRAFT), "--final", str(tmp_path / "nope"), "--name", "x")
    assert code == 2 and "not a directory" in err
    code, _, err = proj.run("learn", "--draft", str(DRAFT), "--final", str(FINAL), "--name", "../x")
    assert code == 2 and "bad name" in err


# ---------------------------------------------------------------- review

def gate(proj, src, name="expense_report"):
    shutil.rmtree(proj.dir)
    shutil.copytree(src, proj.dir)
    (proj.dir / ".evalctl.yaml").write_text(f"config: {name}\ninputs: {INPUTS}\n")


def test_review_writes_the_enabled_checks_as_a_working_config(proj, home, monkeypatch):
    doc = run_learn(proj)
    ids = [c["id"] for c in doc["candidates"]]
    replies = {"report_md_two_decimals": "d", "report_md_no_hotel_lumen": "e"}
    answers(monkeypatch, *[replies.get(i, "") for i in ids], "y")
    code, out, _ = proj.run("review", "expense_report")
    assert code == 0, out
    for c in doc["candidates"]:  # every candidate is shown with where it comes from
        assert c["id"] in out and c["basis"] in out
    path = home / "checks" / "expense_report.yaml"
    text = path.read_text()
    written = [c["id"] for c in yaml.safe_load(text)["checks"]]
    expect = [c["id"] for c in doc["candidates"]
              if (c["enabled"] or c["id"] == "report_md_no_hotel_lumen") and c["id"] != "report_md_two_decimals"]
    assert written == expect
    assert "# basis: " in text and "general rule" in text

    gate(proj, FINAL)
    code, data = proj.check()
    assert code == 0, data["text"]
    gate(proj, DRAFT)
    code, data = proj.check()
    failed = {c["id"] for c in data["checks"] if c["status"] == "fail"}
    assert code == 1 and failed >= set(EXPECTED.values())


def test_review_quit_or_decline_writes_nothing(proj, home, monkeypatch):
    run_learn(proj)
    path = home / "checks" / "expense_report.yaml"
    answers(monkeypatch, "", "q")
    assert proj.run("review", "expense_report")[0] == 1 and not path.exists()
    answers(monkeypatch, *[""] * 17, "n")
    assert proj.run("review", "expense_report")[0] == 1 and not path.exists()
    answers(monkeypatch)  # input closed
    code, out, _ = proj.run("review", "expense_report")
    assert code == 1 and "--yes" in out and not path.exists()
    code, _, err = proj.run("review", "never_learned")
    assert code == 2 and "evalctl learn" in err


def test_review_edit_revalidates_against_the_example(proj, home, monkeypatch, tmp_path):
    run_learn(proj)
    editor = tmp_path / "ed.py"
    editor.write_text(
        "import sys, yaml\n"
        "p = sys.argv[1]\n"
        "c = yaml.safe_load(open(p))\n"
        "if c['type'] == 'contains' and 'must' in c:\n"
        "    c['must'] = ['## Summary', '## Sources']\n"
        "elif c['type'] == 'file':\n"
        "    c['min_size'] = '5GB'\n"          # valid, but the final output does not pass it
        "else:\n"
        "    c['type'] = 'nonsense'\n"         # not a check at all
        "open(p, 'w').write(yaml.safe_dump(c))\n")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {editor}")
    # 1 exists: edit -> fails on final -> decline, keep.  2 sections: edit, keep.  3 placeholders: bad edit, keep.
    answers(monkeypatch, "i", "n", "", "i", "", "i", "", *[""] * 14, "y")
    code, out, _ = proj.run("review", "expense_report")
    assert code == 0, out
    assert "does not pass on the final output" in out and "edit dropped" in out
    assert "edited: final pass, draft fail" in out
    assert "not a valid check: unknown check type 'nonsense'" in out
    checks = {c["id"]: c for c in yaml.safe_load((home / "checks" / "expense_report.yaml").read_text())["checks"]}
    assert checks["report_md_sections"]["must"] == ["## Summary", "## Sources"]
    assert checks["report_md_exists"]["min_size"] == 1
    assert checks["report_md_no_placeholders"]["type"] == "regex"


def test_review_keeps_hand_written_checks_and_can_retract_its_own(proj, home):
    path = home / "checks" / "expense_report.yaml"
    path.write_text("max_rounds: 5\nchecks:\n  - {id: mine, type: file, path: report.md}\n")
    run_learn(proj)
    code, out, _ = proj.run("review", "expense_report", "--yes")
    assert code == 0 and "1 existing check(s) kept" in out
    conf = yaml.safe_load(path.read_text())
    ids = [c["id"] for c in conf["checks"]]
    assert conf["max_rounds"] == 5 and ids[0] == "mine" and "report_md_dates_iso" in ids
    assert "mine" in (home / "checks" / "expense_report.yaml.bak").read_text()
    # disabling a check that review wrote earlier takes it out again; everything else stays
    code, out, _ = proj.run("review", "expense_report", "--yes", "--disable", "report_md_dates_iso")
    after = [c["id"] for c in yaml.safe_load(path.read_text())["checks"]]
    assert code == 0 and after == [i for i in ids if i != "report_md_dates_iso"]
    code, _, err = proj.run("review", "expense_report", "--yes", "--enable", "nope")
    assert code == 2 and "no such candidate" in err


# ---------------------------------------------------------------- feedback

def learned_config(proj):
    run_learn(proj)
    assert proj.run("review", "expense_report", "--yes")[0] == 0


def test_feedback_bad_on_passing_output_points_to_learn(proj, home):
    learned_config(proj)
    gate(proj, FINAL)
    code, out, _ = proj.run("feedback", "--dir", str(proj.dir), "--verdict", "bad", "--note", "wrong largest expense",
                            "--json")
    data = json.loads(out)
    assert code == 0 and data["check_status"] == "pass" and data["agrees"] is False and data["action"] == "learn"
    snap = Path(data["snapshot"])
    assert (snap / "report.md").read_text() == (FINAL / "report.md").read_text()
    assert f"evalctl learn --inputs {INPUTS} --draft {snap} --final {proj.dir.resolve()} --name expense_report" in data["text"]
    assert not (proj.dir / ".eval").exists()  # judging is not checking: no receipt appears

    # ... the human fixes what the checks missed, and learn picks the edit up from the snapshot
    report = proj.dir / "report.md"
    report.write_text(report.read_text().replace("## Sources", "## Method\n\nSummed per category.\n\n## Sources"))
    doc = run_learn(proj, draft=snap, final=proj.dir)
    c = {c["id"]: c for c in doc["candidates"]}["report_md_sections"]
    assert "## Method" in c["check"]["must"] and c["source"] == "edit" and c["validation"]["draft"] == "fail"
    assert any("already in config" in d["reason"] for d in doc["discarded"])
    assert proj.run("review", "expense_report", "--yes")[0] == 0
    assert proj.check()[0] == 0


def test_feedback_good_on_failing_output_names_the_checks_to_relax(proj, home):
    learned_config(proj)
    gate(proj, FINAL)
    report = proj.dir / "report.md"
    report.write_text(report.read_text().replace("Period: 2026-11-02 to 2026-11-06", "Period: 11/02/2026 to 11/06/2026"))
    assert proj.check()[0] == 1
    code, out, _ = proj.run("feedback", "--dir", str(proj.dir), "--verdict", "good")
    assert code == 0 and "DISAGREEMENT" in out and "stricter than you are" in out
    assert "report_md_dates_iso (regex)" in out and "report_md_totals_add_up" not in out
    assert "Relax or delete it" in out and str(home / "checks" / "expense_report.yaml") in out
    assert "evalctl review expense_report" in out
    code, out, _ = proj.run("feedback", "--dir", str(proj.dir), "--verdict", "good")
    assert "[overruled 2 times]" in out
    fb = store.stats()["configs"]["expense_report"]["feedback"]
    assert fb["checks_too_strict"] == 2 and fb["overruled"] == {"report_md_dates_iso": 2}


def test_feedback_that_agrees_is_just_recorded(proj):
    learned_config(proj)
    gate(proj, FINAL)
    code, out, _ = proj.run("feedback", "--dir", str(proj.dir), "--verdict", "good")
    assert code == 0 and "agree" in out and "DISAGREEMENT" not in out
    gate(proj, DRAFT)
    code, out, _ = proj.run("feedback", "--dir", str(proj.dir), "--verdict", "bad", "--note", "sloppy")
    assert code == 0 and "agree" in out and "report_md_totals_add_up" in out
    fb = store.feedback_summary()["expense_report"]
    assert fb["verdicts"] == 2 and fb["agree"] == 2


# ---------------------------------------------------------------- the relation check types

def test_table_sum(proj):
    table = "| Item | Qty | Cost |\n| --- | --- | --- |\n| a | 1 | $1,000.50 |\n| b | 2 | $20.25 |\n| **Total** | 3 | {} |\n"
    proj.write("r.md", table.format("$1,020.75"))
    assert proj.one({"type": "table_sum", "file": "r.md"})[1]["status"] == "pass"
    proj.write("r.md", table.format("$1,020.57"))
    code, c = proj.one({"type": "table_sum", "file": "r.md"})
    assert code == 1 and "r.md:5" in c["failures"][0] and "1,020.75" in c["failures"][0]
    assert proj.one({"type": "table_sum", "file": "r.md", "tolerance": 0.5})[1]["status"] == "pass"
    proj.write("r.md", "| Item | Cost |\n| --- | --- |\n| a | 1 |\n")
    assert "no table with a total row" in proj.one({"type": "table_sum", "file": "r.md"})[1]["failures"][0]
    assert proj.one({"type": "table_sum", "file": "r.md", "require_total": False})[1]["status"] == "pass"
    proj.write("t.csv", "item,cost\na,1.5\nb,2.5\n合计,5\n")
    assert proj.one({"type": "table_sum", "file": "t.csv"})[1]["status"] == "fail"
    proj.write("t.csv", "item,cost\na,1.5\nb,2.5\n合计,4\n")
    assert proj.one({"type": "table_sum", "file": "t.csv"})[1]["status"] == "pass"


def test_numbers_in_inputs(proj, tmp_path, monkeypatch):
    inputs = tmp_path / "in"
    inputs.mkdir()
    (inputs / "sales.csv").write_text("region,units,revenue\nnorth,3,1200.50\nsouth,5,800.00\nnorth,2,99.50\n")
    (inputs / "note.md").write_text("Target for the quarter: 2,500 units.\n")
    check = {"type": "numbers_in_inputs", "file": "out.md"}
    proj.write("out.md", "On 2026-11-10 at 09:30: 3 rows, 10 units, revenue 2,100.00.\n"
                         "North: 1,300.00 from 2 orders. Target 2,500. Largest order 1200.5.\n"
                         "1. first point\n2. second point\n")
    code, c = proj.one(check)
    assert code == 2 and "--inputs" in c["summary"]  # nobody said where the inputs are: setup error, not a verdict
    assert proj.run("check", "--dir", str(proj.dir), "--inputs", str(inputs))[0] == 0
    monkeypatch.setenv("EVALCTL_INPUTS", str(inputs))
    assert proj.check()[0] == 0
    monkeypatch.delenv("EVALCTL_INPUTS")
    (proj.dir / ".evalctl.yaml").write_text(f"config: t\ninputs: {inputs}\n")
    assert proj.check()[0] == 0

    proj.write("out.md", "Revenue 2,150.00 across 4 regions, 45% up.\n")
    code, data = proj.check()
    fails = data["checks"][0]["failures"]
    assert code == 1 and len(fails) == 3 and "out.md:1: 2,150.00 does not appear in the inputs" in fails[0]
    proj.config([dict(check, id="c", ignore=[r"\d+%"], ignore_small_integers=5)])
    (proj.dir / ".evalctl.yaml").write_text(f"config: t\ninputs: {inputs}\n")
    assert len(proj.check()[1]["checks"][0]["failures"]) == 1
    proj.write("out.md", "Revenue 2,100.00.\n")
    proj.config([dict(check, id="c", derived=False)])
    (proj.dir / ".evalctl.yaml").write_text(f"config: t\ninputs: {inputs}\n")
    assert proj.check()[0] == 1  # a sum is not accepted when derived figures are switched off
    (proj.dir / ".evalctl.yaml").write_text("config: t\ninputs: missing_dir\n")
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "inputs directory not found" in err


def test_covers_input_values(proj, tmp_path):
    inputs = tmp_path / "in"
    inputs.mkdir()
    (inputs / "tickets.jsonl").write_text('{"team": "Core", "n": 1}\n{"team": "Infra", "n": 2}\n{"team": "Core", "n": 3}\n')
    check = {"type": "covers_input_values", "id": "c", "file": "*.md", "input": "tickets.jsonl", "column": "team"}
    proj.config([check])
    (proj.dir / ".evalctl.yaml").write_text(f"config: t\ninputs: {inputs}\n")
    proj.write("a.md", "Core shipped.\n")
    code, data = proj.check()
    assert code == 1 and '"Infra" (tickets.jsonl, column team) is not mentioned' in data["checks"][0]["failures"][0]
    proj.write("b.md", "Infra too.\n")
    assert proj.check()[0] == 0
    proj.config([dict(check, column="squad")])
    (proj.dir / ".evalctl.yaml").write_text(f"config: t\ninputs: {inputs}\n")
    assert proj.check()[0] == 2

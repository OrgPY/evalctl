"""learn from several final outputs (--finals), drafts from git (--draft-rev), and lint after learn / review."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from evalctl import learn as lrn

REPO = Path(__file__).resolve().parents[1]
MULTI = REPO / "fixtures" / "expense_reports_multi"
WEEKS = ["week_45", "week_46", "week_47"]
FX = REPO / "fixtures" / "expense_report"


def learn_finals(proj, finals, inputs=(), name="weekly", *extra):
    args = ["learn", "--name", name, "--json", "--finals", *map(str, finals)]
    if inputs:
        args += ["--inputs", *map(str, inputs)]
    code, out, err = proj.run(*args, *extra)
    assert code == 0, err
    return json.loads(out)


@pytest.fixture
def weekly(proj):
    return learn_finals(proj, [MULTI / w / "final" for w in WEEKS], [MULTI / w / "inputs" for w in WEEKS])


def cand(doc, cid):
    return next(c for c in doc["candidates"] if c["id"] == cid)


def test_what_all_finals_share_becomes_general_rules(weekly):
    ids = {c["id"] for c in weekly["candidates"]}
    assert {"report_md_sections", "report_md_dates_iso", "report_md_two_decimals", "report_md_totals_add_up",
            "report_md_numbers_from_inputs", "report_md_covers_category", "summary_json_structure",
            "items_csv_structure"} <= ids
    for c in weekly["candidates"]:
        assert c["scope"] == "general" and c["enabled"] and c["validation"]["final"] == "pass"
        assert "final output" in c["basis"]
    # every candidate really passes on every final, with that final's own inputs
    for w in WEEKS:
        for c in weekly["candidates"]:
            assert lrn.run_check(c["check"], MULTI / w / "final", MULTI / w / "inputs").status == "pass", (w, c["id"])


def test_case_specific_features_are_not_general_rules(weekly):
    """The number of expenses (10, 7, 13) and the Notes section of one week are not rules."""
    by_id = {x["id"]: x for x in weekly["inconsistent"]}
    count = by_id["items_csv_record_count"]
    assert count["scope"] == "case-specific" and "(10, 7, 13)" in count["reason"]
    notes = by_id["report_md_sections:## Notes"]
    assert notes["scope"] == "case-specific" and notes["holds_in"] == [3] and notes["fails_in"] == [1, 2]
    # nothing about them reached the candidates: no exact count, no Notes heading, no literal amount
    dumped = json.dumps([c["check"] for c in weekly["candidates"]])
    assert "## Notes" not in dumped and "max_records" not in dumped
    for number in ("10", "13", "1351.94", "1,351.94", "412.80", "2,299.15"):
        assert f'"{number}"' not in dumped and f" {number}" not in dumped
    assert cand(weekly, "report_md_sections")["check"]["must"] == \
        ["# Weekly expense report", "## Summary", "## Breakdown", "## Largest expense", "## Sources"]


def test_same_exact_count_everywhere_is_still_case_specific_and_disabled(proj, tmp_path):
    """Three decks with exactly 3 pages and exactly 2 records each: plausible coincidence, not a rule."""
    finals = []
    for i, topic in enumerate(["alpha", "beta", "gamma"]):
        d = tmp_path / f"deck_{topic}"
        d.mkdir()
        for page in (1, 2, 3):
            (d / f"page_{page}.md").write_text(f"# {topic} {page}\n\n## Notes\n\nbody of {topic}\n")
        (d / "index.jsonl").write_text("".join(json.dumps({"title": f"{topic} {k}", "page_ref": f"p{k}"}) + "\n" for k in (1, 2)))
        finals.append(d)
    doc = learn_finals(proj, finals, name="deck")
    pages, records = cand(doc, "page_n_md_file_count"), cand(doc, "index_jsonl_record_count")
    for c in (pages, records):
        assert c["scope"] == "case-specific" and c["enabled"] is False
        assert "exact count is treated as case-specific" in c["basis"]
    assert pages["check"] == {"type": "file", "path": "page_[0-9].md", "min_count": 3, "max_count": 3}
    assert all(c["scope"] == "general" for c in doc["candidates"] if c not in (pages, records))
    # review --yes writes only what is enabled: the exact counts stay out of the config
    assert proj.run("review", "deck", "--yes", "--no-lint")[0] == 0
    written = yaml.safe_load((proj.home / "checks" / "deck.yaml").read_text())
    assert not any("count" in c["id"] for c in written["checks"])


def test_page_count_that_differs_is_reported_not_proposed(proj, tmp_path):
    finals = []
    for topic, n in (("alpha", 3), ("beta", 5)):
        d = tmp_path / topic
        d.mkdir()
        for page in range(1, n + 1):
            (d / f"page_{page}.md").write_text(f"# {topic}\n\ntext\n")
        finals.append(d)
    doc = learn_finals(proj, finals, name="deck")
    x = next(x for x in doc["inconsistent"] if x["id"] == "page_n_md_file_count")
    assert "(3, 5)" in x["reason"] and not any("file_count" in c["id"] for c in doc["candidates"])


def test_file_missing_from_one_final_is_not_required(proj, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        (d / "report.md").write_text("# Report\n\n## Summary\n\nfine\n")
    (a / "appendix.md").write_text("# Appendix\n\nextra\n")
    doc = learn_finals(proj, [a, b], name="r")
    assert "appendix_md_exists" not in {c["id"] for c in doc["candidates"]}
    x = next(x for x in doc["inconsistent"] if x["id"] == "appendix_md_exists")
    assert x["holds_in"] == [1] and x["fails_in"] == [2]


def test_schema_is_merged_so_optional_fields_are_not_required(proj, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "out.json").write_text(json.dumps({"title": "x", "tags": ["a"], "note": "only here"}))
    (b / "out.json").write_text(json.dumps({"title": "y", "tags": []}))
    doc = learn_finals(proj, [a, b], name="j")
    schema = cand(doc, "out_json_structure")["check"]["schema"]
    assert schema["required"] == ["title", "tags"] and "note" in schema["properties"]


def test_finals_mode_argument_errors(proj, tmp_path):
    one = MULTI / "week_45" / "final"
    code, _, err = proj.run("learn", "--name", "x", "--finals", str(one))
    assert code == 2 and "at least two different directories" in err
    code, _, err = proj.run("learn", "--name", "x", "--finals", str(one), str(MULTI / "week_46/final"), "--draft", str(one))
    assert code == 2 and "--finals is its own mode" in err
    code, _, err = proj.run("learn", "--name", "x", "--finals", str(one), str(MULTI / "week_46/final"),
                            "--inputs", str(one), str(one), str(one))
    assert code == 2 and "one per final (2)" in err
    code, _, err = proj.run("learn", "--name", "x", "--final", str(one))
    assert code == 2 and "--draft DIR or --draft-rev REV" in err


def test_review_revalidates_an_edited_candidate_on_every_final(weekly, proj, monkeypatch, tmp_path):
    text = proj.run("learn", "--name", "weekly", "--no-lint", "--finals", *[str(MULTI / w / "final") for w in WEEKS])[1]
    assert "from 3 final outputs" in text and "Not consistent across the final outputs" in text
    assert "Human edits" not in text and "validated: passes every final output" in text
    doc = lrn.load("weekly")
    c = cand(doc, "report_md_sections")
    c["check"]["must"].append("## Notes")           # holds in week 47 only
    status, why = lrn.revalidate(doc, c)
    assert status == "fail" and "week_45" in why


# ---------------------------------------------------------------- --draft-rev

@pytest.fixture
def repo(tmp_path):
    """A git repo whose first commit is the agent's draft and whose working tree is the corrected final."""
    if not shutil.which("git"):
        pytest.skip("git not installed")
    root = tmp_path / "repo"
    out = root / "reports" / "week"
    shutil.copytree(FX / "draft", out)
    git = lambda *a: subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                                    check=True, capture_output=True)
    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "agent draft")
    shutil.rmtree(out)
    shutil.copytree(FX / "final", out)
    return root, out, git


def test_draft_rev_gives_the_same_result_as_an_explicit_draft_directory(proj, repo):
    root, out, git = repo
    code, text, err = proj.run("learn", "--name", "expense_report", "--final", str(out), "--draft-rev", "HEAD",
                               "--inputs", str(FX / "inputs"), "--json", "--no-lint")
    assert code == 0, err
    from_git = json.loads(text)
    code, text, _ = proj.run("learn", "--name", "other", "--final", str(FX / "final"), "--draft", str(FX / "draft"),
                             "--inputs", str(FX / "inputs"), "--json", "--no-lint")
    explicit = json.loads(text)
    strip = lambda d: [(c["id"], c["scope"], c["enabled"], c["edits"], c["check"]) for c in d["candidates"]]
    assert strip(from_git) == strip(explicit)
    assert [e["summary"] for e in from_git["edits"]] == [e["summary"] for e in explicit["edits"]]
    assert from_git["draft_rev"] == "HEAD"
    # the extracted draft is kept, so `review` can re-validate an edited check later
    draft = Path(from_git["draft"])
    assert draft.parent == proj.home / "learned" and (draft / "notes.tmp").is_file()


def test_draft_rev_after_the_final_was_committed_too(proj, repo):
    root, out, git = repo
    git("add", "-A")
    git("commit", "-q", "-m", "human fixes")
    code, text, err = proj.run("learn", "--name", "e", "--final", str(out), "--draft-rev", "HEAD~1", "--json", "--no-lint")
    assert code == 0 and len(json.loads(text)["edits"]) > 3
    code, text, _ = proj.run("learn", "--name", "e", "--final", str(out), "--draft-rev", "HEAD", "--json", "--no-lint")
    assert json.loads(text)["edits"] == []


def test_draft_rev_errors_are_explained(proj, repo, tmp_path):
    root, out, git = repo
    code, _, err = proj.run("learn", "--name", "e", "--final", str(out), "--draft-rev", "no-such-rev")
    assert code == 2 and "--draft-rev no-such-rev" in err
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.md").write_text("x")
    code, _, err = proj.run("learn", "--name", "e", "--final", str(plain), "--draft-rev", "HEAD")
    assert code == 2 and "git" in err
    new = root / "brand_new"
    new.mkdir()
    (new / "a.md").write_text("x")
    code, _, err = proj.run("learn", "--name", "e", "--final", str(new), "--draft-rev", "HEAD")
    assert code == 2 and "--draft-rev HEAD" in err      # the directory did not exist in that revision


# ---------------------------------------------------------------- lint after learn / review

def test_learn_and_review_lint_what_they_produce(proj, grader):
    grader.reply = lambda prompt: json.dumps({"findings": [
        {"kind": "unrequested_check", "check": "report_md_two_decimals", "requirement": None,
         "message": "Dropped: there is no task description, so coverage findings are not accepted."},
        {"kind": "duplicate", "check": "report_md_exists", "requirement": None,
         "message": "report_md_exists is implied by report_md_sections."}]})
    args = ["learn", "--name", "expense_report", "--draft", str(FX / "draft"), "--final", str(FX / "final"),
            "--inputs", str(FX / "inputs")]
    code, text, _ = proj.run(*args)
    assert code == 0 and "Lint of the enabled candidates" in text and "report_md_exists: duplicated check" in text
    assert "check nobody asked for" not in text
    assert "reviewed by: deepseek:m" in text
    n = len(grader.requests)
    code, text, _ = proj.run("review", "expense_report", "--yes")
    assert code == 0 and "evalctl lint: expense_report  (0 critical, 1 warning)" in text
    assert len(grader.requests) == n + 1
    written = yaml.safe_load((proj.home / "checks" / "expense_report.yaml").read_text())
    assert written["reference"] == {"final": str(FX / "final"), "inputs": str(FX / "inputs")}
    # the written config passes its own reference output, so a later lint has nothing critical
    code, out, _ = proj.run("lint", "expense_report", "--json")
    assert code == 0 and json.loads(out)["cached"]


def test_learn_without_a_model_still_runs_the_static_lint(proj):
    code, text, _ = proj.run("learn", "--name", "expense_report", "--draft", str(FX / "draft"), "--final", str(FX / "final"))
    assert code == 0 and "reviewed by: static rules only" in text and "the model review did not run" in text

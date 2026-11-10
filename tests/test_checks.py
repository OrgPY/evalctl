"""One section per check type: a passing case, failing cases, and what the failure text says."""
from __future__ import annotations

import pytest

POST = {"title": "t", "url": "https://x.example/1", "author": "a",
        "posted_at": "2026-01-02T03:04:05Z", "body": "hello"}
RECORD_SCHEMA = {
    "type": "object",
    "required": ["title", "url", "author", "posted_at", "body"],
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "url": {"type": "string", "format": "uri"},
        "posted_at": {"type": "string", "format": "date-time"},
    },
}


# ---------------------------------------------------------------- shell
def test_shell_pass(proj):
    code, r = proj.one({"type": "shell", "command": "test -f out.txt"})
    assert code == 1 and r["status"] == "fail"
    proj.write("out.txt", "x")
    code, r = proj.one({"type": "shell", "command": "test -f out.txt"})
    assert code == 0 and r["status"] == "pass"


def test_shell_failure_shows_exit_code_and_output(proj):
    code, r = proj.one({"type": "shell", "command": "echo 'line 12: syntax error'; exit 3"})
    assert code == 1
    text = "\n".join(r["failures"])
    assert "exited 3 (expected 0)" in text and "line 12: syntax error" in text


def test_shell_expect_exit_and_timeout(proj):
    proj.write("a.txt", "x")
    _, r = proj.one({"type": "shell", "command": "exit 4", "expect_exit": 4})
    assert r["status"] == "pass"
    _, r = proj.one({"type": "shell", "command": "sleep 5", "timeout": 0.3})
    assert r["status"] == "fail" and "did not finish" in r["failures"][0]


def test_shell_runs_in_workdir(proj):
    proj.write("marker.txt", "x")
    _, r = proj.one({"type": "shell", "command": 'test "$(pwd -P)" = "$(cd "$EVALCTL_DIR" && pwd -P)" && ls marker.txt'})
    assert r["status"] == "pass"


# ---------------------------------------------------------------- schema
def test_schema_jsonl_pass(proj):
    proj.write("posts.jsonl", [POST, dict(POST, url="https://x.example/2")])
    code, r = proj.one({"type": "schema", "file": "posts.jsonl", "record_schema": RECORD_SCHEMA,
                        "unique": ["url"], "min_records": 2, "max_records": 5})
    assert code == 0 and r["status"] == "pass"


def test_schema_jsonl_reports_line_and_field(proj):
    bad = dict(POST)
    del bad["author"]
    proj.write("posts.jsonl", [POST, bad, dict(POST, title=""), dict(POST, url="not a url"),
                               dict(POST, posted_at="yesterday"), dict(POST, body=5)])
    _, r = proj.one({"type": "schema", "file": "posts.jsonl", "record_schema": dict(
        RECORD_SCHEMA, properties=dict(RECORD_SCHEMA["properties"], body={"type": "string"}))})
    f = r["failures"]
    assert "posts.jsonl:2: missing required field 'author'" in f
    assert "posts.jsonl:3: field 'title' is empty" in f
    assert any(x.startswith("posts.jsonl:4: field 'url' is not a valid uri") for x in f)
    assert any(x.startswith("posts.jsonl:5: field 'posted_at' is not a valid date-time") for x in f)
    assert any(x.startswith("posts.jsonl:6: field 'body' must be string, got integer") for x in f)
    assert len(f) == 5


def test_schema_jsonl_invalid_json_line(proj):
    proj.write("posts.jsonl", '{"title": "ok"}\n{"title": \n')
    _, r = proj.one({"type": "schema", "file": "posts.jsonl", "record_schema": {"type": "object"}})
    assert r["status"] == "fail" and r["failures"][0].startswith("posts.jsonl:2: line is not valid JSON")


def test_schema_duplicates_name_both_lines(proj):
    proj.write("posts.jsonl", [POST, dict(POST, url="https://x.example/2"), POST])
    _, r = proj.one({"type": "schema", "file": "posts.jsonl", "unique": "url"})
    assert r["failures"] == [
        'posts.jsonl:3: duplicate url "https://x.example/1" (first seen at posts.jsonl:1); '
        "keep one, remove or replace the other"
    ]


def test_schema_record_count_range(proj):
    proj.write("posts.jsonl", [POST] * 3)
    _, r = proj.one({"type": "schema", "file": "posts.jsonl", "min_records": 5})
    assert r["failures"] == ["posts.jsonl: has 3 record(s), need at least 5 (2 more)"]
    _, r = proj.one({"type": "schema", "file": "posts.jsonl", "max_records": 2})
    assert r["failures"] == ["posts.jsonl: has 3 record(s), at most 2 allowed (1 too many)"]


def test_schema_missing_file(proj):
    code, r = proj.one({"type": "schema", "file": "posts.jsonl", "min_records": 1})
    assert code == 1 and "file not found" in r["failures"][0]


def test_schema_json_document_and_array(proj):
    proj.write("meta.json", {"count": 3, "source": "forum"})
    doc = {"type": "object", "required": ["count", "pages"],
           "properties": {"count": {"type": "integer", "minimum": 5}}, "additionalProperties": False}
    _, r = proj.one({"type": "schema", "file": "meta.json", "schema": doc})
    assert set(r["failures"]) == {
        "meta.json: missing required field 'pages'",
        "meta.json: field 'count' is 3, minimum is 5",
        "meta.json: unexpected field 'source'",
    }
    proj.write("items.json", [{"id": 1}, {"id": "x"}, {"id": 1}])
    _, r = proj.one({"type": "schema", "file": "items.json", "unique": ["id"],
                     "record_schema": {"type": "object", "properties": {"id": {"type": "integer"}}}})
    assert any(x.startswith("items.json: $[1]: field 'id' must be integer") for x in r["failures"])
    assert any(x.startswith("items.json: $[2]: duplicate id 1") for x in r["failures"])
    proj.write("items.json", {"not": "a list"})
    _, r = proj.one({"type": "schema", "file": "items.json", "min_records": 1})
    assert r["failures"] == ["items.json: top level must be a JSON array of records"]
    proj.write("items.json", "{broken")
    _, r = proj.one({"type": "schema", "file": "items.json", "min_records": 1})
    assert "not valid JSON" in r["failures"][0]


def test_schema_csv(proj):
    proj.write("rows.csv", "name,price,qty\napple,1.5,3\npear,,2\nplum,abc,1\n")
    check = {"type": "schema", "file": "rows.csv", "columns": ["name", "price", "sku"],
             "record_schema": {"type": "object", "required": ["name", "price"],
                               "properties": {"price": {"type": "number"}, "qty": {"type": "integer"}}}}
    _, r = proj.one(check)
    assert set(r["failures"]) == {
        "rows.csv:1: header is missing column 'sku'",
        "rows.csv:3: missing required field 'price'",
        "rows.csv:4: field 'price' must be number, got string \"abc\"",
    }
    proj.write("rows.csv", "name,price,sku\napple,1.5,A1\n")
    code, r = proj.one(check)
    assert code == 0 and r["status"] == "pass"


def test_schema_unsupported_keyword_is_a_config_error(proj):
    proj.write("posts.jsonl", [POST])
    proj.config([{"type": "schema", "file": "posts.jsonl", "record_schema": {"oneOf": []}}])
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "unsupported keyword(s) oneOf" in err


# ---------------------------------------------------------------- contains / regex
def test_contains(proj):
    proj.write("report.md", "# Summary\nAll good.\nTODO: finish this\nTODO again\n")
    check = {"type": "contains", "file": "report.md", "must": ["# Summary", "## Sources"],
             "must_not": ["TODO"]}
    code, r = proj.one(check)
    assert code == 1
    assert r["failures"] == [
        "report.md: required text '## Sources' not found; add it",
        "report.md:3: forbidden text 'TODO' found: \"TODO: finish this\"; remove it",
        "report.md:4: forbidden text 'TODO' found: \"TODO again\"; remove it",
    ]
    proj.write("report.md", "# Summary\n## Sources\n")
    code, r = proj.one(check)
    assert code == 0 and r["status"] == "pass"


def test_contains_is_literal_and_ignore_case(proj):
    proj.write("a.txt", "price is 5.00 USD\n")
    _, r = proj.one({"type": "contains", "file": "a.txt", "must": ["5.00"], "must_not": ["5x00"]})
    assert r["status"] == "pass"
    _, r = proj.one({"type": "contains", "file": "a.txt", "must": ["usd"]})
    assert r["status"] == "fail"
    _, r = proj.one({"type": "contains", "file": "a.txt", "must": ["usd"], "ignore_case": True})
    assert r["status"] == "pass"


def test_regex(proj):
    proj.write("out.txt", "id=42\nkey=sk-abcdef123456\n")
    check = {"type": "regex", "file": "out.txt", "must": [r"^id=\d+$", r"^version="],
             "must_not": [r"sk-[a-z0-9]{8,}"]}
    _, r = proj.one(check)
    assert r["failures"][0] == "out.txt: required pattern '^version=' not found; add it"
    assert r["failures"][1].startswith("out.txt:2: forbidden pattern")
    proj.write("out.txt", "id=42\nversion=1\n")
    _, r = proj.one(check)
    assert r["status"] == "pass"


def test_text_check_on_missing_file_fails(proj):
    proj.write("other.txt", "x")
    _, r = proj.one({"type": "regex", "file": "out.txt", "must_not": ["x"]})
    assert r["status"] == "fail" and "no file matches" in r["failures"][0]


def test_bad_regex_is_a_config_error(proj):
    proj.config([{"type": "regex", "file": "a.txt", "must": ["("]}])
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "bad regex" in err


# ---------------------------------------------------------------- file
def test_file_exists_and_size(proj):
    check = {"type": "file", "path": "report.md", "min_size": 10, "max_size": "1KB"}
    _, r = proj.one(check)
    assert r["failures"] == ["report.md: no such file; create it"]
    proj.write("report.md", "")
    _, r = proj.one(check)
    assert r["failures"] == ["report.md: is empty"]
    proj.write("report.md", "x" * 2000)
    _, r = proj.one(check)
    assert r["failures"] == ["report.md: 2000 bytes, maximum is 1024"]
    proj.write("report.md", "x" * 100)
    code, r = proj.one(check)
    assert code == 0 and r["status"] == "pass"


def test_file_naming_and_count(proj):
    for n in ("shots/page-01.png", "shots/page-02.png", "shots/Final Copy.png"):
        proj.write(n, "png")
    _, r = proj.one({"type": "file", "path": "shots/*.png", "name_regex": r"page-\d{2}\.png",
                     "min_count": 4})
    assert set(r["failures"]) == {
        "shots/*.png: 3 file(s) match, need at least 4",
        "shots/Final Copy.png: file name does not match /page-\\d{2}\\.png/; rename it",
    }
    _, r = proj.one({"type": "file", "path": "shots/*.png", "max_count": 2})
    assert r["failures"] == ["shots/*.png: 3 file(s) match, at most 2 allowed"]


def test_file_must_not_exist(proj):
    proj.write("debug.log", "x")
    _, r = proj.one({"type": "file", "path": "*.log", "exists": False})
    assert r["failures"] == ["debug.log: must not exist; delete it"]
    (proj.dir / "debug.log").unlink()
    proj.write("keep.txt", "x")
    _, r = proj.one({"type": "file", "path": "*.log", "exists": False})
    assert r["status"] == "pass"


# ---------------------------------------------------------------- python
PY = '''
import os

def has_marker(dir, name="marker.txt", word=None):
    path = os.path.join(dir, name)
    if not os.path.exists(path):
        return False, f"{name}: missing"
    if word and word not in open(path).read():
        return False, f"{name}: does not mention {word!r}\\n{name}: second problem"
    return True, "marker present"

def crashes(dir):
    raise RuntimeError("boom")

def wrong_shape(dir):
    return "yes"
'''


def test_python_check(proj, home):
    (home / "checks" / "my_checks.py").write_text(PY)
    check = {"type": "python", "module": "my_checks.py", "function": "has_marker",
             "args": {"word": "done"}}
    code, r = proj.one(check)
    assert code == 1 and r["failures"] == ["marker.txt: missing"]
    proj.write("marker.txt", "wip")
    _, r = proj.one(check)
    assert r["failures"] == ["marker.txt: does not mention 'done'", "marker.txt: second problem"]
    proj.write("marker.txt", "done")
    code, r = proj.one(check)
    assert code == 0 and r["summary"] == "marker present"


def test_python_check_bugs_are_errors_not_verdicts(proj, home):
    (home / "checks" / "my_checks.py").write_text(PY)
    proj.write("a.txt", "x")
    for fn, needle in (("crashes", "RuntimeError: boom"), ("wrong_shape", "must return"),
                       ("nope", "cannot load")):
        code, r = proj.one({"type": "python", "module": "my_checks.py", "function": fn})
        assert code == 2 and r["status"] == "error" and needle in r["summary"]


def test_python_module_must_live_outside_workdir(proj):
    proj.write("sneaky.py", "def ok(dir):\n    return True, ''\n")
    proj.config([{"type": "python", "module": str(proj.dir / "sneaky.py"), "function": "ok"}])
    code, _, err = proj.run("check", "--dir", str(proj.dir))
    assert code == 2 and "inside the working directory" in err


# ---------------------------------------------------------------- llm_rubric
RUBRIC = {"type": "llm_rubric", "file": "posts.jsonl", "sample": 4, "fields": ["title", "body"],
          "rubric": [{"id": "real_body", "text": "Body is real post text."}]}


def posts(n, bad=()):
    return [dict(POST, url=f"https://x.example/{i}", body="NAV Home Login" if i in bad else f"real text {i}")
            for i in range(1, n + 1)]


@pytest.mark.parametrize("provider,path,auth", [
    ("anthropic", "/v1/messages", "x-api-key"),
    ("openai", "/chat/completions", "authorization"),
    ("deepseek", "/chat/completions", "authorization"),
])
def test_llm_rubric_providers(proj, llm, monkeypatch, provider, path, auth):
    env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}[provider]
    monkeypatch.setenv(env, "secret-key")
    proj.write("posts.jsonl", posts(10))
    judge = {"provider": provider, "model": "grader-1", "base_url": llm.url}
    code, r = proj.one(dict(RUBRIC, judge=judge))
    assert code == 0 and r["status"] == "pass", r
    assert r["meta"]["graded"] == 4 and r["meta"]["total"] == 10
    assert len(llm.requests) == 4
    req = llm.requests[0]
    assert req["path"] == path and "secret-key" in req["headers"][auth]
    assert req["body"]["model"] == "grader-1" and "temperature" not in req["body"]
    prompt = req["body"]["messages"][-1]["content"]
    assert '"body"' in prompt and '"url"' not in prompt.split("<item>")[1]  # only the chosen fields


def test_llm_rubric_failure_names_the_lines(proj, llm, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    llm.decide = lambda item: "NAV" not in item
    proj.write("posts.jsonl", posts(3, bad={2}))
    code, r = proj.one(dict(RUBRIC, sample=None, judge={"provider": "deepseek", "model": "m", "base_url": llm.url}))
    assert code == 1 and r["status"] == "fail"
    assert "criterion 'real_body'" in r["failures"][0] and "1 of 3" in r["failures"][0]
    assert r["failures"][1] == "  posts.jsonl:2: this is navigation text"


def test_llm_rubric_min_pass_rate(proj, llm, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    llm.decide = lambda item: "NAV" not in item
    proj.write("posts.jsonl", posts(4, bad={1}))
    judge = {"provider": "deepseek", "model": "m", "base_url": llm.url}
    _, r = proj.one(dict(RUBRIC, sample=None, min_pass_rate=0.75, judge=judge))
    assert r["status"] == "pass"
    _, r = proj.one(dict(RUBRIC, sample=None, min_pass_rate=0.9, judge=judge))
    assert r["status"] == "fail"


def test_llm_rubric_whole_text_file_is_one_item(proj, llm, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    proj.write("report.md", "# Findings\nIt works.\n")
    _, r = proj.one({"type": "llm_rubric", "file": "report.md", "rubric": ["States a finding."],
                     "judge": {"provider": "deepseek", "model": "m", "base_url": llm.url}})
    assert r["status"] == "pass" and len(llm.requests) == 1
    assert "It works." in llm.requests[0]["body"]["messages"][-1]["content"]


def test_llm_rubric_runs_only_after_hard_checks_pass(proj, llm, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    proj.write("posts.jsonl", posts(3))
    proj.config([
        dict(RUBRIC, id="soft", judge={"provider": "deepseek", "model": "m", "base_url": llm.url}),
        {"id": "hard", "type": "schema", "file": "posts.jsonl", "min_records": 5},
    ])
    code, data = proj.check()
    by = {c["id"]: c for c in data["checks"]}
    assert code == 1 and by["hard"]["status"] == "fail" and by["soft"]["status"] == "skipped"
    assert llm.requests == []
    proj.write("posts.jsonl", posts(6))
    code, data = proj.check()
    assert code == 0 and len(llm.requests) == 4


def test_judge_must_be_another_family_than_the_worker(proj, llm, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    proj.write("posts.jsonl", posts(4))
    claude = {"provider": "anthropic", "model": "claude-haiku-5-5", "base_url": llm.url}
    deepseek = {"provider": "deepseek", "model": "deepseek-chat", "base_url": llm.url}

    proj.config([dict(RUBRIC, id="c", judge=claude)])
    code, data = proj.check("--worker", "anthropic")
    assert code == 2 and data["checks"][0]["status"] == "error"
    assert "same model family as the worker" in data["checks"][0]["failures"][0]
    assert llm.requests == []

    # Claude Code is detected from its environment marker.
    monkeypatch.setenv("CLAUDECODE", "1")
    assert proj.check()[0] == 2
    monkeypatch.delenv("CLAUDECODE")

    # With a list, the first judge from another family is used.
    proj.config([dict(RUBRIC, id="c", judge=[claude, deepseek])])
    code, data = proj.check("--worker", "anthropic")
    assert code == 0 and data["checks"][0]["meta"]["judge_family"] == "deepseek"
    code, data = proj.check("--worker", "deepseek")
    assert code == 0 and data["checks"][0]["meta"]["judge_family"] == "anthropic"

    # Explicit opt-out.
    proj.config([dict(RUBRIC, id="c", judge=dict(claude, allow_same_family=True))])
    assert proj.check("--worker", "anthropic")[0] == 0

    # Unknown worker: graded, with a note that the rule could not be enforced.
    proj.config([dict(RUBRIC, id="c", judge=claude)])
    code, data = proj.check()
    assert code == 0 and "family unknown" in data["checks"][0]["meta"]["note"]


def test_judge_from_global_file_and_missing_key(proj, llm, home, monkeypatch):
    proj.write("posts.jsonl", posts(4))
    proj.config([dict(RUBRIC, id="c")])
    code, data = proj.check()
    assert code == 2 and "none is configured" in data["checks"][0]["failures"][0]

    (home / "judge.yaml").write_text(
        f"judges:\n  - {{provider: deepseek, model: m, base_url: '{llm.url}'}}\n")
    code, data = proj.check()
    assert code == 2 and "DEEPSEEK_API_KEY is not set" in data["checks"][0]["failures"][0]
    assert not (proj.dir / ".eval" / "receipt.json").exists()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    assert proj.check()[0] == 0


def test_unusable_grader_answer_is_an_error_not_a_pass(proj, llm, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    proj.write("posts.jsonl", posts(2))
    llm.raw_answer = "Sure! They all look great."
    code, r = proj.one(dict(RUBRIC, sample=None, judge={"provider": "deepseek", "model": "m", "base_url": llm.url}))
    assert code == 2 and r["status"] == "error" and "did not answer with JSON" in r["failures"][0]
    assert len(llm.requests) == 4  # each of the two items is retried once


def test_command_judge(proj, tmp_path):
    grader = tmp_path / "grader.py"
    grader.write_text(
        "import json, os, re, sys\n"
        "p = sys.stdin.read()\n"
        "assert os.environ['EVALCTL_IN_JUDGE'] == '1'\n"
        "ids = re.findall(r'^- id \"([^\"]+)\":', p, re.M)\n"
        "ok = 'NAV' not in p.split('<item>')[1]\n"
        "print('thinking...\\n' + json.dumps({'verdicts': [{'id': i, 'pass': ok, 'reason': 'nav'} for i in ids]}))\n")
    import sys
    judge = {"provider": "command", "command": f"{sys.executable} {grader}", "family": "local"}
    proj.write("posts.jsonl", posts(3, bad={3}))
    code, r = proj.one(dict(RUBRIC, sample=None, judge=judge))
    assert code == 1 and r["failures"][1] == "  posts.jsonl:3: nav"
    proj.write("posts.jsonl", posts(3))
    code, r = proj.one(dict(RUBRIC, sample=None, judge=judge))
    assert code == 0

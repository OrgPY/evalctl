from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from evalctl import cli


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own ~/.evalctl and a clean judge/worker environment."""
    h = tmp_path / "evalctl_home"
    (h / "checks").mkdir(parents=True)
    monkeypatch.setenv("EVALCTL_HOME", str(h))
    for var in ("EVALCTL_WORKER", "CLAUDECODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "DEEPSEEK_API_KEY", "EVALCTL_HOOK", "EVALCTL_IN_JUDGE", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


class Project:
    """A working directory wired to a named config in the test home."""

    def __init__(self, home: Path, work: Path, capsys):
        self.home, self.dir, self.capsys = home, work, capsys

    def config(self, checks, name="t", pointer=True, **top):
        path = self.home / "checks" / f"{name}.yaml"
        path.write_text(yaml.safe_dump({"checks": checks, **top}, sort_keys=False), encoding="utf-8")
        if pointer:
            (self.dir / ".evalctl.yaml").write_text(f"config: {name}\n", encoding="utf-8")
        return path

    def pointer(self, name="t", **more):
        """Write .evalctl.yaml with loop / profile / profiles settings."""
        (self.dir / ".evalctl.yaml").write_text(yaml.safe_dump({"config": name, **more}, sort_keys=False), encoding="utf-8")

    def write(self, rel, content):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (list, dict)) and rel.endswith(".jsonl"):
            content = "".join(json.dumps(r) + "\n" for r in content)
        elif isinstance(content, (list, dict)):
            content = json.dumps(content)
        p.write_text(content, encoding="utf-8")
        return p

    def run(self, *argv):
        """-> (exit code, stdout, stderr)"""
        self.capsys.readouterr()
        code = cli.main(list(argv))
        out = self.capsys.readouterr()
        return code, out.out, out.err

    def check(self, *extra):
        code, out, err = self.run("check", "--dir", str(self.dir), "--json", *extra)
        return code, json.loads(out)

    def one(self, check, **top):
        """Run a config holding a single check; -> that check's result dict."""
        self.config([dict(check, id=check.get("id", "c"))], **top)
        code, data = self.check()
        return code, data["checks"][0]

    def verify(self, *extra):
        code, out, _ = self.run("verify", "--dir", str(self.dir), "--json", *extra)
        return code, json.loads(out)


@pytest.fixture
def proj(home, work, capsys):
    return Project(home, work, capsys)


class FakeLLM:
    """Local HTTP server speaking both the Anthropic Messages and OpenAI chat protocols.

    `decide(item_text) -> bool` gives the verdict for every criterion of an item.
    """

    def __init__(self):
        self.requests = []
        self.decide = lambda item: True
        self.raw_answer = None  # set to a string to answer with exactly that text
        self.verdict = None     # (item_text, criterion_id, nth grading request) -> bool; wins over `decide`
        self.reply = None       # prompt -> answer text, for requests that are not gradings (lint, explain)
        self.gradings = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                prompt = body["messages"][-1]["content"]
                if "<item>" not in prompt:
                    answer = outer.raw_answer or (outer.reply(prompt) if outer.reply else "{}")
                else:
                    ids = re.findall(r'^- id "([^"]+)":', prompt, re.M)
                    item = prompt.split("<item>", 1)[1].split("</item>", 1)[0]
                    outer.gradings += 1
                    oks = {i: (outer.verdict(item, i, outer.gradings) if outer.verdict else outer.decide(item))
                           for i in ids}
                    answer = outer.raw_answer or json.dumps({"verdicts": [
                        {"id": i, "pass": oks[i], "reason": "looks fine" if oks[i] else "this is navigation text"}
                        for i in ids]})
                if self.path.endswith("/v1/messages"):
                    payload = {"content": [{"type": "text", "text": answer}], "stop_reason": "end_turn"}
                elif self.path.endswith("/chat/completions"):
                    payload = {"choices": [{"message": {"role": "assistant", "content": answer}}]}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def llm():
    s = FakeLLM()
    yield s
    s.close()


@pytest.fixture
def grader(llm, home, monkeypatch):
    """The fake model as the global grader (~/.evalctl/judge.yaml), usable by rubric checks, lint and explain."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    (home / "judge.yaml").write_text(f"judges:\n  - {{provider: deepseek, model: m, base_url: '{llm.url}'}}\n")
    return llm


class FakeAgent:
    """Registers tests/fake_agent.py as agent `fake` in the test home's agents.yaml."""

    def __init__(self, home: Path, tmp_path: Path):
        self.home, self.plan = home, tmp_path / "plan.json"

    def script(self, *runs, resume=True, fmt="claude", timeout=60):
        """Each run is a list of calls; see fake_agent.py."""
        import sys
        self.plan.write_text(json.dumps({"runs": [list(r) for r in runs]}))
        for extra in (".state", ".calls"):
            Path(str(self.plan) + extra).unlink(missing_ok=True)
        base = [sys.executable, str(Path(__file__).with_name("fake_agent.py")), "--plan", str(self.plan), "--format", fmt]
        agent = {"family": "anthropic", "start": base + ["{prompt}"], "timeout": timeout,
                 "output": "text" if fmt == "text" else "jsonl",
                 "trajectory": "none" if fmt == "text" else fmt}
        if resume and fmt != "text":
            agent["resume"] = base + ["--resume", "{session_id}", "{prompt}"]
            agent["session_id_field"] = "session_id" if fmt == "claude" else "thread_id"
        (self.home / "agents.yaml").write_text(yaml.safe_dump({"agents": {"fake": agent}}), encoding="utf-8")

    def calls(self):
        p = Path(str(self.plan) + ".calls")
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def fake_agent(home, tmp_path):
    return FakeAgent(home, tmp_path)


class TaskModel:
    """Drives the fake grader for acceptance contracts: writing them, grading them, comparing repeat runs."""

    def __init__(self, llm):
        self.llm = llm
        self.deliverables, self.requirements = [], []
        self.grade = lambda item_id, outputs: True       # outputs: the "Files the agent created or changed" block
        self.differences = []
        self.prompts = []
        llm.reply = self._reply

    def _reply(self, prompt):
        self.prompts.append(prompt)
        if "Write the checklist" in prompt:
            return json.dumps({"deliverables": self.deliverables, "requirements": self.requirements})
        if "Checklist items:" in prompt:
            ids = re.findall(r'^- id "([^"]+)":', prompt, re.M)
            outputs = prompt.split("Files the agent created or changed (its output):", 1)[1].split("Other files in the working directory", 1)[0]
            return json.dumps({"verdicts": [
                {"id": i, "pass": bool(self.grade(i, outputs)),
                 "reason": "found in the output" if self.grade(i, outputs) else f"nothing in the output satisfies {i}"} for i in ids]})
        if "Attempt MAIN" in prompt:
            return json.dumps({"differences": self.differences})
        return "{}"


@pytest.fixture
def task_model(grader):
    return TaskModel(grader)

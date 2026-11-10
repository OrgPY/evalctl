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
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                prompt = body["messages"][-1]["content"]
                ids = re.findall(r'^- id "([^"]+)":', prompt, re.M)
                item = prompt.split("<item>", 1)[1].split("</item>", 1)[0]
                ok = outer.decide(item)
                answer = outer.raw_answer or json.dumps({"verdicts": [
                    {"id": i, "pass": ok, "reason": "looks fine" if ok else "this is navigation text"}
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

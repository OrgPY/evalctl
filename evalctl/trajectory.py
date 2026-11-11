"""Read an agent's structured output as a numbered list of steps.

Two formats, both as the tools write them today:
  claude   `claude -p --output-format stream-json --verbose` (one JSON event per line), and the
           session transcripts Claude Code keeps (the Stop hook's `transcript_path`): same blocks.
  codex    `codex exec --json` (thread.started / item.completed / ... events).

A step is a message the agent wrote, or one tool call together with its result. Steps are
numbered from 1 across all files given, so "step 14" means the same thing in `evalctl explain`
and in an action_policy failure.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .checks.base import clip

MESSAGE, TOOL, USER, ERROR_EVENT = "message", "tool", "user", "error"


@dataclass
class Step:
    n: int
    kind: str                      # message | tool | user | error
    tool: str = ""                 # Claude: Bash, Read, Write, WebFetch, mcp__x__y ... Codex: shell, file_change, web_search, mcp__x__y
    input: Dict[str, Any] = field(default_factory=dict)
    output: str = ""
    failed: bool = False           # the tool reported an error / the command exited non-zero
    denied: bool = False           # the agent's own permission system refused the call: it never ran
    text: str = ""                 # for messages
    source: str = ""               # file the step came from

    def command(self) -> str:
        """The shell command of a Bash / shell step, else ''."""
        if self.kind == TOOL and self.tool in ("Bash", "shell"):
            return str(self.input.get("command") or "")
        return ""

    def render(self, max_in: int = 700, max_out: int = 500) -> str:
        if self.kind == TOOL:
            body = self.command() or json.dumps(self.input, ensure_ascii=False)
            out = clip(self.output, max_out) if self.output else ""
            mark = "DENIED, DID NOT RUN: " if self.denied else "ERROR: " if self.failed else ""
            return (f"[{self.n}] TOOL {self.tool}: {_shorten(body, max_in)}"
                    + (f"\n     -> {mark}{out}" if out or self.failed else ""))
        label = {MESSAGE: "AGENT", USER: "USER", ERROR_EVENT: "ERROR"}[self.kind]
        return f"[{self.n}] {label}: {_shorten(self.text, max_in)}"


def _shorten(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n * 2 // 3] + f" ...[{len(s) - n} chars cut]... " + s[-(n // 3):]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _events(path: Path) -> List[Dict[str, Any]]:
    out = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def detect(events: List[Dict[str, Any]]) -> str:
    for e in events[:50]:
        t = str(e.get("type", ""))
        if t in ("thread.started", "turn.started", "turn.completed") or t.startswith("item."):
            return "codex"
        if t in ("assistant", "user", "system", "result") or "message" in e:
            return "claude"
    return "unknown"


# Wording of Claude Code's refusals, for session transcripts, which carry no permission_denied events.
DENIAL = re.compile(r"requires approval|requested permissions to .{0,300}but you haven't granted|permission to use .{0,80}has been denied"
                    r"|was blocked by a hook|user doesn't want to proceed with this tool use", re.I | re.S)


def _claude(events: List[Dict[str, Any]], steps: List[Step], source: str) -> None:
    by_id: Dict[str, Step] = {}
    # stream-json reports every refused call twice: a system/permission_denied event, and the
    # `permission_denials` list of the final result. Either is enough.
    denied = {str(e.get("tool_use_id")) for e in events if e.get("type") == "system" and e.get("subtype") == "permission_denied"}
    for e in events:
        if e.get("type") == "result":
            denied.update(str(d.get("tool_use_id")) for d in e.get("permission_denials") or [] if isinstance(d, dict))
    for e in events:
        msg = e.get("message")
        if e.get("type") not in ("assistant", "user") or not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if e["type"] == "user" and isinstance(content, str):
            if content.strip():
                steps.append(Step(len(steps) + 1, USER, text=content, source=source))
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text" and str(b.get("text", "")).strip():
                steps.append(Step(len(steps) + 1, MESSAGE if e["type"] == "assistant" else USER,
                                  text=str(b["text"]), source=source))
            elif kind == "tool_use":
                s = Step(len(steps) + 1, TOOL, tool=str(b.get("name", "?")),
                         input=b.get("input") if isinstance(b.get("input"), dict) else {"input": b.get("input")},
                         source=source)
                steps.append(s)
                by_id[str(b.get("id"))] = s
            elif kind == "tool_result":
                s = by_id.get(str(b.get("tool_use_id")))
                if s is not None:
                    s.output = _text_of(b.get("content"))
                    s.failed = bool(b.get("is_error"))
                    s.denied = str(b.get("tool_use_id")) in denied or (s.failed and bool(DENIAL.search(s.output[:600])))


def _codex(events: List[Dict[str, Any]], steps: List[Step], source: str) -> None:
    for e in events:
        t = e.get("type")
        if t in ("error", "turn.failed"):
            detail = e.get("message") or (e.get("error") or {}).get("message") or json.dumps(e, ensure_ascii=False)
            steps.append(Step(len(steps) + 1, ERROR_EVENT, text=str(detail), failed=True, source=source))
            continue
        item = e.get("item")
        if t != "item.completed" or not isinstance(item, dict):
            continue
        kind = item.get("type")
        n = len(steps) + 1
        if kind == "agent_message":
            if str(item.get("text", "")).strip():
                steps.append(Step(n, MESSAGE, text=str(item["text"]), source=source))
        elif kind == "command_execution":
            code = item.get("exit_code")
            steps.append(Step(n, TOOL, tool="shell", input={"command": str(item.get("command", ""))},
                              output=str(item.get("aggregated_output") or ""),
                              failed=(code not in (0, None)) or item.get("status") == "failed", source=source))
        elif kind == "file_change":
            steps.append(Step(n, TOOL, tool="file_change", input={"changes": item.get("changes") or []},
                              failed=item.get("status") == "failed", source=source))
        elif kind == "mcp_tool_call":
            steps.append(Step(n, TOOL, tool=f"mcp__{item.get('server', '?')}__{item.get('tool', '?')}",
                              input=item.get("arguments") if isinstance(item.get("arguments"), dict) else {"arguments": item.get("arguments")},
                              output=_text_of(item.get("result") or item.get("error")),
                              failed=item.get("status") == "failed" or bool(item.get("error")), source=source))
        elif kind == "web_search":
            steps.append(Step(n, TOOL, tool="web_search", input={"query": item.get("query", "")}, source=source))
        elif kind == "error":
            steps.append(Step(n, ERROR_EVENT, text=str(item.get("message", "")), failed=True, source=source))


def load(paths: Iterable[Path]) -> List[Step]:
    """Steps of all files, in the order given, numbered continuously. Unknown formats give no steps."""
    steps: List[Step] = []
    for p in paths:
        events = _events(Path(p))
        fmt = detect(events)
        if fmt == "claude":
            _claude(events, steps, str(p))
        elif fmt == "codex":
            _codex(events, steps, str(p))
    return steps


def readable(paths: Iterable[Path]) -> List[Path]:
    """The files that are in a format we can read."""
    return [Path(p) for p in paths if detect(_events(Path(p))) != "unknown"]


def render(steps: List[Step], max_in: int = 700, max_out: int = 500) -> str:
    return "\n".join(s.render(max_in, max_out) for s in steps)

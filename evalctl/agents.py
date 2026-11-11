"""Agent adapters: how to start an agent on a task and how to continue its session.

Everything agent-specific is in ~/.evalctl/agents.yaml (written on first use from TEMPLATE below);
the code here only fills in a command template, runs it and reads a session id from the output.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import paths
from .config import ConfigError

# Flags confirmed against `claude --help` (2.1.322) and https://code.claude.com/docs/en/headless,
# and against `codex exec --help` / `codex exec resume --help` (codex-cli 0.166.0) and
# https://developers.openai.com/codex/noninteractive. Both were also run for real.
TEMPLATE = """\
# Agent adapters for `evalctl run` and `evalctl bench`.
#
# start    command for the first call          placeholders: {prompt} {dir}
# resume   command that continues the session  placeholders: {prompt} {dir} {session_id}
#          Leave it out for an agent that cannot continue a session: evalctl then starts a new
#          call and puts the location of the previous output and the failure list in the prompt.
# output   json | jsonl | text    how stdout is read to find the session id
# session_id_field   field holding the session id (dotted path; for jsonl the last line that has it)
# trajectory         claude | codex | none   format of stdout as a tool-call log
#                    (used by `evalctl explain` and by action_policy checks)
# family   model family doing the work; the grader for llm_rubric must be from another family
# timeout  seconds per call
# stdin    true: send the prompt on stdin instead of {prompt}
# env      extra environment variables for the agent
#
# A command is a string (split like a shell would, without running a shell) or a list.
agents:
  claude:
    family: anthropic
    # acceptEdits lets Claude write files but not run arbitrary shell commands or use the
    # network. Widen it for workflows that need more, e.g. add:  --allowedTools "Bash,WebFetch"
    start: claude -p {prompt} --output-format stream-json --verbose --permission-mode acceptEdits
    resume: claude -p {prompt} --resume {session_id} --output-format stream-json --verbose --permission-mode acceptEdits
    output: jsonl
    session_id_field: session_id
    trajectory: claude
    timeout: 1800

  codex:
    family: openai
    start: codex exec --json --skip-git-repo-check -s workspace-write {prompt}
    # `codex exec resume` has no -s flag; the sandbox is set with -c.
    resume: codex exec resume --json --skip-git-repo-check -c sandbox_mode="workspace-write" {session_id} {prompt}
    output: jsonl
    session_id_field: thread_id
    trajectory: codex
    timeout: 1800

  # An agent without session support (your own harness):
  # deepseek_scraper:
  #   family: deepseek
  #   start: python3 ~/harness/run.py --task {prompt} --out {dir}
  #   output: text
  #   trajectory: none
  #   timeout: 900
"""

KEYS = {"family", "start", "resume", "output", "session_id_field", "trajectory", "timeout", "stdin", "env"}
OUTPUTS = ("json", "jsonl", "text")
TRAJECTORIES = ("claude", "codex", "none")


def agents_path() -> Path:
    return paths.home() / "agents.yaml"


@dataclass
class Agent:
    name: str
    start: List[str]
    resume: Optional[List[str]] = None
    output: str = "text"
    session_id_field: Optional[str] = None
    trajectory: str = "none"
    family: Optional[str] = None
    timeout: float = 1800
    stdin: bool = False
    env: Dict[str, str] = field(default_factory=dict)

    @property
    def can_resume(self) -> bool:
        return bool(self.resume and self.session_id_field and self.output != "text")


@dataclass
class Call:
    command: List[str]
    exit_code: Optional[int]
    session_id: Optional[str]
    duration: float
    timed_out: bool
    stdout_path: Path
    stderr_path: Path

    def problem(self) -> Optional[str]:
        if self.timed_out:
            return f"the agent did not finish within the timeout ({self.duration:.0f}s) and was stopped"
        if self.exit_code != 0:
            tail = self.stderr_path.read_text(encoding="utf-8", errors="replace").strip()[-400:]
            return f"the agent exited with code {self.exit_code}" + (f": {tail}" if tail else "")
        return None


def _command(raw: Any, where: str) -> List[str]:
    if isinstance(raw, str):
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            raise ConfigError(f"{where}: cannot split the command: {e}")
    elif isinstance(raw, list) and all(isinstance(x, (str, int, float)) for x in raw):
        parts = [str(x) for x in raw]
    else:
        raise ConfigError(f"{where}: a command is a string or a list of arguments")
    if not parts:
        raise ConfigError(f"{where}: empty command")
    return parts


def parse_agent(name: str, raw: Any, where: str) -> Agent:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: an agent is a mapping with at least `start`")
    bad = set(raw) - KEYS
    if bad:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(bad))} (allowed: {', '.join(sorted(KEYS))})")
    if not raw.get("start"):
        raise ConfigError(f"{where}: `start` (the command for the first call) is required")
    start = _command(raw["start"], f"{where}.start")
    resume = _command(raw["resume"], f"{where}.resume") if raw.get("resume") else None
    stdin = bool(raw.get("stdin", False))
    for label, cmd in (("start", start), ("resume", resume)):
        if cmd is None:
            continue
        if not stdin and not any("{prompt}" in a for a in cmd):
            raise ConfigError(f"{where}.{label}: no {{prompt}} placeholder (or set `stdin: true`)")
    if resume and not any("{session_id}" in a for a in resume):
        raise ConfigError(f"{where}.resume: no {{session_id}} placeholder")
    output = raw.get("output", "text")
    if output not in OUTPUTS:
        raise ConfigError(f"{where}: `output` must be one of {', '.join(OUTPUTS)}")
    if resume and (output == "text" or not raw.get("session_id_field")):
        raise ConfigError(f"{where}: `resume` needs `output: json` or `jsonl` and a `session_id_field` "
                          "to read the session id from")
    trajectory = raw.get("trajectory", "none") or "none"
    if trajectory not in TRAJECTORIES:
        raise ConfigError(f"{where}: `trajectory` must be one of {', '.join(TRAJECTORIES)}")
    timeout = raw.get("timeout", 1800)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"{where}: `timeout` must be a positive number of seconds")
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ConfigError(f"{where}: `env` must be a mapping")
    return Agent(name=name, start=start, resume=resume, output=output,
                 session_id_field=raw.get("session_id_field"), trajectory=trajectory,
                 family=str(raw["family"]).lower() if raw.get("family") else None,
                 timeout=float(timeout), stdin=stdin, env={str(k): str(v) for k, v in env.items()})


def load_agents() -> Dict[str, Agent]:
    p = agents_path()
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TEMPLATE, encoding="utf-8")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}")
    raw = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{p}: expected `agents:` with at least one agent")
    return {str(n): parse_agent(str(n), a, f"{p}: agents.{n}") for n, a in raw.items()}


def get_agent(name: str) -> Agent:
    agents = load_agents()
    if name not in agents:
        raise ConfigError(f"agent '{name}' is not defined in {agents_path()} (defined: {', '.join(sorted(agents))})")
    return agents[name]


def _dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def find_session_id(agent: Agent, stdout: str) -> Optional[str]:
    if not agent.session_id_field or agent.output == "text":
        return None
    found = None
    if agent.output == "json":
        try:
            found = _dig(json.loads(stdout), agent.session_id_field)
        except ValueError:
            return None
    else:
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                v = _dig(json.loads(line), agent.session_id_field)
            except ValueError:
                continue
            if isinstance(v, (str, int)) and str(v):
                found = v
    return str(found) if isinstance(found, (str, int)) and str(found) else None


def invoke(agent: Agent, prompt: str, cwd: Path, stdout_path: Path, stderr_path: Path,
           session_id: Optional[str] = None, extra_env: Optional[Dict[str, str]] = None) -> Call:
    """Run the agent once in `cwd`. stdout goes to `stdout_path` untouched: it is the trajectory."""
    template = agent.resume if (session_id and agent.can_resume) else agent.start
    values = {"{prompt}": prompt, "{dir}": str(cwd), "{session_id}": session_id or ""}
    command = []
    for arg in template:
        for k, v in values.items():
            arg = arg.replace(k, v)
        command.append(os.path.expanduser(arg) if arg.startswith("~") else arg)
    if not shutil.which(command[0]):
        raise ConfigError(f"agent '{agent.name}': command '{command[0]}' not found on PATH "
                          f"(see {agents_path()})")
    env = dict(os.environ)
    # The outer loop is the gate here: keep the Stop hook and a skill-driven `evalctl check` out of it.
    env.update({"EVALCTL_HOOK": "off", "EVALCTL_OUTER": "1", "EVALCTL_AGENT": agent.name})
    if agent.family:
        env["EVALCTL_WORKER"] = agent.family
    env.update(agent.env)
    env.update(extra_env or {})
    started = time.time()
    timed_out = False
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=out, stderr=err,
                                stdin=subprocess.PIPE if agent.stdin else subprocess.DEVNULL,
                                start_new_session=True)
        try:
            proc.communicate(prompt.encode() if agent.stdin else None, timeout=agent.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    return Call(command=command, exit_code=proc.returncode, session_id=find_session_id(agent, stdout) or session_id,
                duration=time.time() - started, timed_out=timed_out, stdout_path=stdout_path,
                stderr_path=stderr_path)

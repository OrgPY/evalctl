"""Grader models for llm_rubric checks.

A judge is configured (first match wins) on the check, on the check config, or
globally in ~/.evalctl/judge.yaml. Each place may hold one judge or a list;
from a list evalctl takes the first usable judge whose model family differs
from the family of the agent that did the work.

Providers:
  anthropic           Anthropic Messages API           (ANTHROPIC_API_KEY)
  openai              OpenAI Chat Completions          (OPENAI_API_KEY)
  deepseek            DeepSeek, OpenAI-compatible      (DEEPSEEK_API_KEY)
  openai_compatible   any other /chat/completions endpoint: set base_url, api_key_env, family
  command             a shell command: prompt on stdin, answer on stdout (e.g. `codex exec -`)
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

from . import paths
from .config import ConfigError

PRESETS: Dict[str, Dict[str, Any]] = {
    "anthropic": {"api": "anthropic", "base_url": "https://api.anthropic.com",
                  "api_key_env": "ANTHROPIC_API_KEY", "family": "anthropic"},
    "openai": {"api": "openai", "base_url": "https://api.openai.com/v1",
               "api_key_env": "OPENAI_API_KEY", "family": "openai"},
    "deepseek": {"api": "openai", "base_url": "https://api.deepseek.com",
                 "api_key_env": "DEEPSEEK_API_KEY", "family": "deepseek"},
    "openai_compatible": {"api": "openai"},
    "command": {"api": "command"},
}
ALLOWED_KEYS = {"provider", "model", "base_url", "api_key_env", "family", "allow_same_family",
                "command", "timeout", "max_tokens", "anthropic_version"}
ANTHROPIC_VERSION = "2026-10-21"


class JudgeError(Exception):
    """The judge could not be reached or did not answer usably."""


@dataclass
class Judge:
    provider: str
    api: str
    family: str
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    command: Optional[str] = None
    allow_same_family: bool = False
    timeout: float = 120
    max_tokens: int = 4000
    anthropic_version: str = ANTHROPIC_VERSION

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model or shlex.split(self.command or '?')[0]}"

    def unavailable_reason(self) -> Optional[str]:
        if self.api == "command":
            exe = shlex.split(self.command or "")[0]
            return None if shutil.which(exe) else f"command '{exe}' not found on PATH"
        if not os.environ.get(self.api_key_env or ""):
            return f"environment variable {self.api_key_env} is not set"
        return None

    def ask(self, system: str, user: str) -> str:
        if self.api == "command":
            return self._ask_command(system, user)
        if self.api == "anthropic":
            return self._ask_anthropic(system, user)
        return self._ask_openai(system, user)

    # -- transports ---------------------------------------------------------
    def _post(self, url: str, headers: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json", "user-agent": "evalctl", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            raise JudgeError(f"{self.label}: HTTP {e.code} from {url}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise JudgeError(f"{self.label}: request to {url} failed: {e}")

    def _ask_anthropic(self, system: str, user: str) -> str:
        # No sampling parameters: current Claude models reject temperature/top_p.
        data = self._post(
            self.base_url.rstrip("/") + "/v1/messages",
            {"x-api-key": os.environ[self.api_key_env], "anthropic-version": self.anthropic_version},
            {"model": self.model, "max_tokens": self.max_tokens, "system": system,
             "messages": [{"role": "user", "content": user}]},
        )
        if data.get("stop_reason") == "refusal":
            raise JudgeError(f"{self.label}: the model refused to grade this item")
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        if not text.strip():
            raise JudgeError(f"{self.label}: empty answer (stop_reason={data.get('stop_reason')})")
        return text

    def _ask_openai(self, system: str, user: str) -> str:
        data = self._post(
            self.base_url.rstrip("/") + "/chat/completions",
            {"authorization": f"Bearer {os.environ[self.api_key_env]}"},
            {"model": self.model,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise JudgeError(f"{self.label}: unexpected response shape: {str(data)[:300]}")
        if not (text or "").strip():
            raise JudgeError(f"{self.label}: empty answer")
        return text

    def _ask_command(self, system: str, user: str) -> str:
        # Run outside the working directory so an agent CLI used as judge neither
        # touches the output nor triggers the evalctl Stop hook recursively.
        with tempfile.TemporaryDirectory(prefix="evalctl-judge-") as tmp:
            try:
                proc = subprocess.run(
                    self.command, shell=True, cwd=tmp, input=(system + "\n\n" + user).encode(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout,
                    env=dict(os.environ, EVALCTL_IN_JUDGE="1"),
                )
            except subprocess.TimeoutExpired:
                raise JudgeError(f"{self.label}: no answer within {self.timeout}s")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise JudgeError(f"{self.label}: exited {proc.returncode}: {err}")
        out = proc.stdout.decode("utf-8", errors="replace")
        if not out.strip():
            raise JudgeError(f"{self.label}: empty answer")
        return out


def _guess_family(model: Optional[str], base_url: Optional[str]) -> Optional[str]:
    m = (model or "").lower()
    for needle, fam in (("claude", "anthropic"), ("gpt", "openai"), ("deepseek", "deepseek"),
                        ("gemini", "google"), ("qwen", "alibaba"), ("glm", "zhipu"),
                        ("kimi", "moonshot"), ("moonshot", "moonshot"), ("grok", "xai")):
        if needle in m:
            return fam
    host = urlparse(base_url or "").hostname or ""
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else (host or None)


def parse_judge(raw: Any, where: str) -> Judge:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a judge must be a mapping with at least `provider`")
    bad = set(raw) - ALLOWED_KEYS
    if bad:
        raise ConfigError(f"{where}: unknown judge key(s): {', '.join(sorted(bad))}")
    provider = raw.get("provider")
    if provider not in PRESETS:
        raise ConfigError(f"{where}: `provider` must be one of {', '.join(PRESETS)}")
    merged = {**PRESETS[provider], **{k: v for k, v in raw.items() if v is not None}}
    api = merged["api"]
    if api == "command":
        if not merged.get("command"):
            raise ConfigError(f"{where}: provider command needs `command`")
        if not merged.get("family"):
            raise ConfigError(f"{where}: provider command needs `family` (which model family answers)")
    else:
        for k in ("model", "base_url", "api_key_env"):
            if not merged.get(k):
                raise ConfigError(f"{where}: provider {provider} needs `{k}`")
        merged.setdefault("family", _guess_family(merged["model"], merged["base_url"]))
        if not merged.get("family"):
            raise ConfigError(f"{where}: cannot infer the model family; set `family`")
    return Judge(
        provider=provider, api=api, family=str(merged["family"]).lower(),
        model=merged.get("model"), base_url=merged.get("base_url"),
        api_key_env=merged.get("api_key_env"), command=merged.get("command"),
        allow_same_family=bool(merged.get("allow_same_family", False)),
        timeout=float(merged.get("timeout", 120)), max_tokens=int(merged.get("max_tokens", 4000)),
        anthropic_version=str(merged.get("anthropic_version", ANTHROPIC_VERSION)),
    )


def parse_judges(raw: Any, where: str) -> List[Judge]:
    items = raw if isinstance(raw, list) else [raw]
    return [parse_judge(r, f"{where}[{i}]" if isinstance(raw, list) else where)
            for i, r in enumerate(items)]


def configured_judges(check_level: Any, config_level: Any) -> Tuple[List[Judge], str]:
    if check_level is not None:
        return parse_judges(check_level, "check `judge`"), "the check"
    if config_level is not None:
        return parse_judges(config_level, "config `judge`"), "the check config"
    gp = paths.judge_path()
    if gp.is_file():
        try:
            data = yaml.safe_load(gp.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ConfigError(f"{gp}: invalid YAML: {e}")
        if isinstance(data, dict) and "judges" in data:
            data = data["judges"]
        if data:
            return parse_judges(data, str(gp)), str(gp)
    raise ConfigError(
        "llm_rubric needs a grader model and none is configured.\n"
        f"Create {gp} (see judge.example.yaml in the evalctl repo), e.g.:\n"
        "  judges:\n"
        "    - {provider: deepseek, model: deepseek-chat}\n"
        "    - {provider: anthropic, model: claude-haiku-5-5}\n"
        "and export the matching API key (DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY)."
    )


def detect_worker(explicit: Optional[str], config_default: Optional[str]) -> Optional[str]:
    """Model family of the agent that produced the output, if we can tell."""
    w = explicit or os.environ.get("EVALCTL_WORKER")
    if not w and os.environ.get("CLAUDECODE"):
        w = "anthropic"  # set by Claude Code in the shells it spawns
    w = w or config_default
    return str(w).lower() if w else None


def pick_judge(judges: List[Judge], source: str, worker: Optional[str]) -> Tuple[Judge, Optional[str]]:
    """First judge that is from another family than the worker and is usable."""
    skipped = []
    for j in judges:
        if worker and j.family == worker and not j.allow_same_family:
            skipped.append(f"{j.label}: same model family as the worker ({worker})")
            continue
        why = j.unavailable_reason()
        if why:
            skipped.append(f"{j.label}: {why}")
            continue
        note = None
        if not worker:
            note = ("worker model family unknown, cross-family grading not enforced "
                    "(set EVALCTL_WORKER or pass --worker)")
        return j, note
    raise ConfigError(
        f"no usable grader model among the judges configured in {source}:\n  - "
        + "\n  - ".join(skipped)
        + "\nThe grader must be from a different model family than the worker. Add another judge, "
        "export its API key, or set `allow_same_family: true` on a judge to accept same-family grading."
    )

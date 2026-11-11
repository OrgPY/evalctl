"""Signed receipts: proof that the current files passed the current config."""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from .checks.base import Result, match_files
from .config import Config, ConfigError

VERSION = 2  # v2 adds profile, switches and what actually ran
# Never part of the output when `artifacts` is not set in the config.
DEFAULT_IGNORED_DIRS = {".eval", ".git", ".hg", ".svn", ".claude", ".codex", "__pycache__",
                        "node_modules", ".venv", "venv", ".pytest_cache", ".mypy_cache",
                        ".ruff_cache", ".idea", ".vscode"}
DEFAULT_IGNORED_FILES = {".DS_Store", "Thumbs.db", "*.pyc", paths.POINTER_NAME}


def load_key(create: bool = True) -> bytes:
    kp = paths.key_path()
    if kp.is_file():
        key = kp.read_bytes().strip()
        if len(key) < 32:
            raise ConfigError(f"{kp} is too short to be a signing key; delete it to generate a new one")
        return key
    if not create:
        raise ConfigError(f"signing key {kp} does not exist yet (no check has passed on this machine)")
    kp.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode()
    fd = os.open(str(kp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key + b"\n")
    os.chmod(str(kp), 0o600)
    return key


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_artifacts(workdir: Path, patterns: Optional[List[str]]) -> List[Path]:
    """Files the receipt covers: the config's `artifacts` globs, or every file in the directory."""
    if patterns:
        seen = {}
        for pat in patterns:
            for p in match_files(workdir, pat):
                if p.is_file():
                    seen[p] = True
        return sorted(seen)
    out = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = sorted(d for d in dirs if d not in DEFAULT_IGNORED_DIRS)
        for name in sorted(files):
            if any(fnmatch.fnmatch(name, pat) for pat in DEFAULT_IGNORED_FILES):
                continue
            p = Path(root) / name
            if p.is_file():
                out.append(p)
    return sorted(out)


def hash_artifacts(workdir: Path, patterns: Optional[List[str]]) -> List[Dict[str, Any]]:
    return [
        {"path": p.relative_to(workdir).as_posix(), "sha256": sha256_file(p), "size": p.stat().st_size}
        for p in list_artifacts(workdir, patterns)
    ]


def digest(artifacts: List[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for a in artifacts:
        h.update(f"{a['path']}\0{a['sha256']}\n".encode())
    return h.hexdigest()


def _sign(body: Dict[str, Any], key: bytes) -> str:
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return "hmac-sha256:" + hmac.new(key, payload, hashlib.sha256).hexdigest()


def write(workdir: Path, config: Config, results: List[Result], round_no: Optional[int],
          loop: Any = None, ran: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None) -> Path:
    """`loop` is the resolved loop.Loop; `ran` records what each switch actually did:
    {"lint": {...} | None, "judge_repeat": {...} | None, "repeat": {...} | None}."""
    artifacts = hash_artifacts(workdir, config.artifacts)
    if not artifacts:
        raise ConfigError(
            "all checks passed but there is no output file to put in the receipt"
            + (f" (`artifacts` patterns {config.artifacts} match nothing in {workdir})"
               if config.artifacts else f" ({workdir} has no files)")
        )
    body: Dict[str, Any] = {
        "version": VERSION,
        "dir": str(workdir),
        "config": {"name": config.name, "path": str(config.path), "sha256": config.sha256},
        "passed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "round": round_no,
        "artifact_patterns": config.artifacts,
        "artifacts": artifacts,
        "checks": [{"id": r.id, "type": r.type, "status": r.status, "summary": r.summary}
                   for r in results],
        "profile": loop.profile if loop else None,
        "switches": loop.switches() if loop else None,
        "ran": ran or {},
    }
    # contracts: [{hash, sha256, items}]; repeated: K; reconciled: bool; divergences / needs_human: counts
    body.update(extra or {})
    body["signature"] = _sign(body, load_key())
    rp = paths.receipt_path(workdir)
    rp.parent.mkdir(parents=True, exist_ok=True)
    tmp = rp.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, rp)
    return rp


def invalidate(workdir: Path) -> None:
    """A failing check removes any older receipt: it no longer describes this output."""
    try:
        paths.receipt_path(workdir).unlink()
    except FileNotFoundError:
        pass


def read(workdir: Path) -> Optional[Dict[str, Any]]:
    rp = paths.receipt_path(workdir)
    if not rp.is_file():
        return None
    try:
        data = json.loads(rp.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def missing_switches(rec: Dict[str, Any], config: Optional[Config], loop: Any) -> List[str]:
    """Switches the loop settings in force now require, which the receipt does not show as run."""
    if loop is None:
        return []
    ran = rec.get("ran") or {}
    who = f"profile '{loop.profile}'" if loop.profile else "the loop settings in .evalctl.yaml"
    out: List[str] = []
    if loop.lint_on:
        lint = ran.get("lint") or {}
        if not lint.get("ran") or (config is not None and lint.get("config_sha") != config.sha256):
            out.append(f"{who} requires lint (when: {loop.lint['when']}), but the receipt records no lint "
                       "of this check config")
    has_rubric = config is None or any(c.type == "llm_rubric" for c in config.checks)
    if loop.judge_repeat_on and has_rubric:
        jr, want = ran.get("judge_repeat") or {}, loop.judge_repeat
        if not jr.get("ran") or jr.get("n", 0) < want["n"] or jr.get("agree", 0) < want["agree"]:
            out.append(f"{who} requires judge_repeat (n={want['n']}, agree={want['agree']}), but the receipt "
                       + (f"records n={jr.get('n')}, agree={jr.get('agree')}" if jr.get("ran")
                          else "records no repeated grading"))
    if loop.repeat_on:
        rp, want = ran.get("repeat") or {}, loop.repeat
        settled = rp.get("agreed") or rp.get("reconcile_complete")
        if not rp.get("ran") or rp.get("k", 0) < want["k"] or not settled:
            out.append(f"{who} requires repeat (k={want['k']}, when: on_pass), but the receipt records "
                       + ("a repeat whose differences were not reconciled" if rp.get("ran") and rp.get("k", 0) >= want["k"]
                          else f"only k={rp.get('k')}" if rp.get("ran") else "no repeat run")
                       + ". Run `evalctl repeat` in the session (or use `evalctl run`), reconcile, and check again")
    return out


def verify(workdir: Path, config: Optional[Config], loop: Any = None,
           contracts: Optional[List[Dict[str, Any]]] = None, contract_problem: Optional[str] = None
           ) -> Tuple[bool, List[str], Optional[Dict[str, Any]]]:
    """-> (valid, reasons it is not valid, receipt). `config` and `loop` are what is in force now."""
    rec = read(workdir)
    rp = paths.receipt_path(workdir)
    if rec is None:
        return False, [f"no receipt at {rp}: `evalctl check` has not passed for this directory"], None
    if not rec or "signature" not in rec:
        return False, [f"{rp} is not a readable receipt"], rec
    body = {k: v for k, v in rec.items() if k != "signature"}
    try:
        key = load_key(create=False)
    except ConfigError as e:
        return False, [str(e)], rec
    if not hmac.compare_digest(_sign(body, key), str(rec["signature"])):
        return False, ["signature does not match: the receipt was edited or not written by evalctl "
                       "on this machine"], rec
    reasons: List[str] = []
    rc = rec.get("config") or {}
    if config is not None:
        if rc.get("name") != config.name or Path(str(rc.get("path"))) != config.path:
            reasons.append(f"receipt is for check config '{rc.get('name')}' but this directory now "
                           f"uses '{config.name}' ({config.path})")
        elif rc.get("sha256") != config.sha256:
            reasons.append(f"check config '{config.name}' changed since the receipt was written")
    if Path(str(rec.get("dir"))) != workdir:
        reasons.append(f"receipt was written for {rec.get('dir')}, not {workdir}")
    then = {a["path"]: a["sha256"] for a in rec.get("artifacts") or []}
    now = {a["path"]: a["sha256"] for a in hash_artifacts(workdir, rec.get("artifact_patterns"))}
    for path in sorted(set(then) | set(now)):
        if path not in now:
            reasons.append(f"{path}: deleted since the receipt was written")
        elif path not in then:
            reasons.append(f"{path}: new file since the receipt was written")
        elif then[path] != now[path]:
            reasons.append(f"{path}: content changed since the receipt was written")
    reasons += missing_switches(rec, config, loop)
    # the acceptance contracts in force now ([{hash, sha256}]) against the ones the receipt was written for
    if contract_problem:
        reasons.append(contract_problem)
    elif contracts is not None:
        then_c = {c["hash"]: c.get("sha256") for c in rec.get("contracts") or []}
        for c in contracts:
            if c["hash"] not in then_c:
                reasons.append(f"an acceptance contract ({c['hash']}) is active for this task but the receipt does not cover it")
            elif then_c[c["hash"]] != c["sha256"]:
                reasons.append(f"acceptance contract {c['hash']} changed since the receipt was written")
        for h in then_c:
            if h not in {c["hash"] for c in contracts}:
                reasons.append(f"the receipt was written for acceptance contract {h}, which is no longer the active one")
    return not reasons, reasons, rec

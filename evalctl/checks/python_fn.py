from __future__ import annotations

import importlib.util
import traceback
from pathlib import Path
from typing import List

from ..config import CheckSpec, ConfigError, _inside
from .base import Check, Context, Result, error, result


def _module_path(spec: CheckSpec, config_dir: Path) -> Path:
    p = Path(str(spec.params["module"])).expanduser()
    return (p if p.is_absolute() else config_dir / p).resolve()


class PythonCheck(Check):
    """Call a custom function: fn(dir, **args) -> (passed: bool, detail: str).

    `module` is a .py file, relative to the config file's directory. It is part
    of the config hash and may not live inside the working directory.
    """

    type = "python"
    allowed = ("module", "function", "args")
    required = ("module", "function")

    @classmethod
    def validate_extra(cls, spec: CheckSpec, config_dir: Path, workdir: Path) -> List[Path]:
        path = _module_path(spec, config_dir)
        if not path.is_file():
            raise ConfigError(f"python module not found: {path}")
        if _inside(path, workdir):
            raise ConfigError(f"python module {path} is inside the working directory; keep it next to the config")
        if not isinstance(spec.params.get("args", {}), dict):
            raise ConfigError("`args` must be a mapping")
        return [path]

    @classmethod
    def run(cls, spec: CheckSpec, ctx: Context) -> Result:
        path = _module_path(spec, ctx.config.path.parent)
        fn_name = spec.params["function"]
        try:
            mspec = importlib.util.spec_from_file_location(f"evalctl_user_{path.stem}", path)
            module = importlib.util.module_from_spec(mspec)  # type: ignore[arg-type]
            mspec.loader.exec_module(module)  # type: ignore[union-attr]
            fn = getattr(module, fn_name)
        except Exception as e:
            return error(spec, f"cannot load {fn_name} from {path}: {type(e).__name__}: {e}")
        try:
            out = fn(str(ctx.workdir), **(spec.params.get("args") or {}))
        except Exception:
            tb = traceback.format_exc().strip().splitlines()
            return error(spec, f"{fn_name}() raised an exception (bug in the check, not a verdict): "
                         + " | ".join(tb[-3:]))
        if not (isinstance(out, (tuple, list)) and len(out) == 2 and isinstance(out[0], bool)):
            return error(spec, f"{fn_name}() must return (passed: bool, detail: str), got {out!r}")
        passed, detail = out[0], str(out[1] or "")
        if passed:
            return result(spec, [], detail or "ok")
        lines = [ln for ln in detail.splitlines() if ln.strip()] or [f"{fn_name}() returned False without detail"]
        return result(spec, lines, "", lines[0] if len(lines) == 1 else f"{len(lines)} problem(s)")

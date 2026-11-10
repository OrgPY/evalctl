from __future__ import annotations

from typing import Dict, Type

from .base import ERROR, FAIL, PASS, SKIPPED, Check, Context, Result
from .file import FileCheck
from .llm_rubric import LlmRubricCheck
from .python_fn import PythonCheck
from .schema import SchemaCheck
from .shell import ShellCheck
from .text import ContainsCheck, RegexCheck

REGISTRY: Dict[str, Type[Check]] = {
    c.type: c
    for c in (ShellCheck, SchemaCheck, ContainsCheck, RegexCheck, FileCheck, PythonCheck, LlmRubricCheck)
}

# Checks that need a model. They only run once every other check has passed.
SOFT_TYPES = {"llm_rubric"}

__all__ = ["REGISTRY", "SOFT_TYPES", "Check", "Context", "Result", "PASS", "FAIL", "ERROR", "SKIPPED"]

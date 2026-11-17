"""Orchestration boundary.

The pipeline is deliberately thin: parse -> calculate -> report.  It is the
only layer that turns a validation failure into a stable error envelope, so
every entry point (library, CLI, QA harness) reports failures identically.
"""

from __future__ import annotations

from typing import Any, Dict

from .engine import calculate
from .models import InputError
from .parser import load_records, parse
from .report import build_error, build_result

PROJECT = "usage-contract-replay"


def run(payload: Any) -> Dict[str, Any]:
    try:
        parsed = parse(payload)
        data = calculate(parsed)
    except InputError as error:
        return build_error(PROJECT, error.code, str(error))
    return build_result(PROJECT, data)


__all__ = ["run", "load_records", "parse"]

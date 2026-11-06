"""Colored console logging; quiet under ``-q`` (``set_verbose(False)``)."""

from __future__ import annotations

import sys

_VERBOSE = True
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"


def set_verbose(verbose: bool) -> None:
    global _VERBOSE
    _VERBOSE = verbose


def _log(color: str, tag: str, message: str) -> None:
    if not _VERBOSE:
        return
    print(f"{color}[{tag}]{NC} {message}", file=sys.stderr)


def log_info(message: str) -> None:
    _log(BLUE, "INFO", message)


def log_ok(message: str) -> None:
    _log(GREEN, "OK", message)


def log_warn(message: str) -> None:
    _log(YELLOW, "WARN", message)


def log_error(message: str) -> None:
    # Errors are printed even in quiet mode; they explain a non-zero exit.
    print(f"{RED}[ERROR]{NC} {message}", file=sys.stderr)

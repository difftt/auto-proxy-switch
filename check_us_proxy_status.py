#!/usr/bin/env python3
"""
Compatibility wrapper for the original US-default entry point.

The real implementation lives in `check_proxy_status.py`, which exposes a
generic `--region` flag whose default is `us`. This thin wrapper preserves
the original US-default behavior for existing cron entries and scripts that
still invoke `check_us_proxy_status.py` directly: if the caller does not
pass `--region`, the wrapper injects `--region us` before delegating to
`check_proxy_status.main()`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sibling `check_proxy_status` importable regardless of the caller's cwd.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import check_proxy_status  # noqa: E402


def _inject_default_region(argv: list[str]) -> list[str]:
    """Insert ``--region us`` after ``argv[0]`` when ``--region`` is absent.

    Recognizes both ``--region VALUE`` and ``--region=VALUE`` forms. A
    caller-provided ``--region`` is always honored.
    """
    for arg in argv[1:]:
        if arg == "--region" or arg.startswith("--region="):
            return argv
    return [argv[0], "--region", "us", *argv[1:]]


def main() -> int:
    sys.argv = _inject_default_region(sys.argv)
    return check_proxy_status.main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1)

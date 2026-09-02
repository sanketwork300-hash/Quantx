#!/usr/bin/env python
"""Regenerate regression golden files.

Regenerating is a deliberate act: a golden diff must be reviewed and justified
in the pull request, and a formula change that moves one requires a model
version bump (docs/testing.md).

Usage:
    python scripts/regen_golden.py --accept all
    python scripts/regen_golden.py --accept options_chain_clean
    python scripts/regen_golden.py --diff          # show drift, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.regression.golden import (  # noqa: E402
    CASES,
    golden_path,
    load_golden,
    run_case,
    write_golden,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accept", help="case name, or 'all'")
    parser.add_argument("--diff", action="store_true", help="report drift only")
    args = parser.parse_args()

    if not args.accept and not args.diff:
        parser.error("pass --accept <case|all> or --diff")

    names = CASES if args.accept in (None, "all") else (args.accept,)
    drift = 0
    for name in names:
        current = run_case(name)
        if args.diff:
            if not golden_path(name).exists():
                print(f"{name}: no golden file yet")
                drift += 1
                continue
            expected = load_golden(name)
            if current != expected:
                drift += 1
                print(f"{name}: DRIFT")
                for key in sorted(set(current) | set(expected)):
                    if current.get(key) != expected.get(key):
                        print(f"  {key}:")
                        print(f"    expected: {json.dumps(expected.get(key))[:300]}")
                        print(f"    actual:   {json.dumps(current.get(key))[:300]}")
            else:
                print(f"{name}: unchanged")
        else:
            write_golden(name, current)
            print(f"wrote {golden_path(name).relative_to(ROOT)}")

    return 1 if (args.diff and drift) else 0


if __name__ == "__main__":
    raise SystemExit(main())

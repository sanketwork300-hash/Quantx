#!/usr/bin/env python
"""Enforce the layering rules from docs/architecture.md section 3.

The rules exist so that the numerical layer stays testable without
infrastructure, and so that extracting a domain into its own service later is
mechanical rather than archaeological. They are checked in CI because a layering
rule nobody enforces is a comment.

Usage:
    python scripts/check_layering.py [--quiet]
Exit code 1 if any rule is violated.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: package -> packages it may never import.
FORBIDDEN: dict[str, frozenset[str]] = {
    "quant": frozenset({"domains", "infrastructure", "api", "apps"}),
    "infrastructure": frozenset({"domains", "quant", "api", "apps"}),
    "domains": frozenset({"api", "apps"}),
    "api": frozenset({"apps"}),
}

#: Domains that may be imported freely by other domains: shared contracts and
#: value objects rather than engines.
SHARED_DOMAINS = frozenset({"reports"})


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path.relative_to(ROOT)}:{self.line}: {self.message}"


def iter_python_files(package: str) -> Iterator[Path]:
    for path in sorted((ROOT / package).rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def imported_modules(tree: ast.AST) -> Iterator[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno


def check_package_layering() -> list[Violation]:
    violations: list[Violation] = []
    for package, forbidden in FORBIDDEN.items():
        for path in iter_python_files(package):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for module, lineno in imported_modules(tree):
                top = module.split(".")[0]
                if top in forbidden:
                    violations.append(
                        Violation(
                            path,
                            lineno,
                            f"{package}/ must not import {top}/ (found {module!r})",
                        )
                    )
    return violations


def check_cross_domain_orm() -> list[Violation]:
    """A domain must not reach into another domain's persistence models.

    Cross-domain reads go through a service interface. Sharing ORM models is how
    two domains quietly become one.
    """
    violations: list[Violation] = []
    for path in iter_python_files("domains"):
        own_domain = path.relative_to(ROOT / "domains").parts[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, lineno in imported_modules(tree):
            parts = module.split(".")
            if len(parts) < 3 or parts[0] != "domains":
                continue
            other_domain, submodule = parts[1], parts[2]
            if other_domain in {own_domain, *SHARED_DOMAINS}:
                continue
            if submodule in {"orm", "repository"}:
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"domains/{own_domain} must not import "
                        f"domains/{other_domain}/{submodule}; use its service instead",
                    )
                )
    return violations


def check_no_math_in_routes() -> list[Violation]:
    """API routes must not import the numerical layer.

    Build spec: no financial mathematics inside API controllers.
    """
    violations: list[Violation] = []
    for path in iter_python_files("api"):
        if path.parent.name != "routes":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, lineno in imported_modules(tree):
            if module.split(".")[0] == "quant":
                violations.append(
                    Violation(
                        path,
                        lineno,
                        f"API routes must not import quant/ (found {module!r}); "
                        "put the calculation in a domain service",
                    )
                )
    return violations


def run() -> list[Violation]:
    return [
        *check_package_layering(),
        *check_cross_domain_orm(),
        *check_no_math_in_routes(),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    violations = run()
    if violations:
        print(f"{len(violations)} layering violation(s):", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1
    if not args.quiet:
        print("layering OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The layering rules are enforced, not merely documented."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def test_no_layering_violations():
    from scripts.check_layering import run

    violations = run()
    assert violations == [], "\n".join(str(v) for v in violations)


PROBE = """
import json, sys
import quant.pricing.black76
import quant.volatility.svi
import quant.statistics.scoring
import quant.numerical.tolerances
leaked = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"domains", "infrastructure", "api", "apps"}
)
print(json.dumps(leaked))
"""


def test_quant_is_importable_without_any_infrastructure():
    """``tests/quant_validation`` runs with no database, cache or object store.

    That is only possible because ``quant/`` is import-clean. Checked in a fresh
    interpreter: mutating ``sys.modules`` in-process would both give a false
    reading and break other tests' exception identities.
    """
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    leaked = json.loads(result.stdout)
    assert leaked == [], f"quant/ pulled in {leaked}"

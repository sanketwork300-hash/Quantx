#!/usr/bin/env python
"""Print a credential encryption key entry for QIP_CREDENTIAL_ENCRYPTION_KEYS.

Broker access tokens are encrypted before they are stored. This produces the
key that does it.

Usage:
    python scripts/generate_credential_key.py [key_id]

To rotate, generate a new entry and put it *first* in the setting, keeping the
old one after it:

    QIP_CREDENTIAL_ENCRYPTION_KEYS=v2:<new>,v1:<old>

New rows are then sealed with ``v2`` while rows written under ``v1`` remain
readable. Remove ``v1`` only once no row still names it — dropping a key that
rows still reference makes those credentials unrecoverable, and the users
concerned will have to reconnect their brokers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infrastructure.security.crypto import generate_key_spec  # noqa: E402


def main() -> int:
    key_id = sys.argv[1] if len(sys.argv) > 1 else "v1"
    print(f"QIP_CREDENTIAL_ENCRYPTION_KEYS={generate_key_spec(key_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

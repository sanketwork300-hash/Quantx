"""Fetching an instrument master file and getting rows out of it.

Two concerns, kept apart from the parsing: where the bytes come from, and what
container they arrived in. Both are sniffed from the bytes themselves — gzip by
its magic number, JSON by its first non-space character — rather than inferred
from the URL, because a filename is a claim and a magic number is a fact.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from collections.abc import Iterator, Mapping
from typing import Any

#: Two bytes that begin every gzip member (RFC 1952 section 2.3.1).
GZIP_MAGIC = b"\x1f\x8b"


class InstrumentMasterUnavailable(Exception):
    """The instrument file could not be fetched or read."""


def decode_rows(raw: bytes) -> Iterator[Mapping[str, Any]]:
    """Yield instrument rows from a downloaded file.

    Accepts gzipped or plain bytes, holding either a JSON array of objects or a
    CSV with a header row. A container this cannot identify raises rather than
    returning nothing, because "the file was empty" and "we could not read the
    file" call for very different responses.
    """
    if raw[:2] == GZIP_MAGIC:
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise InstrumentMasterUnavailable(f"the file is not readable gzip: {exc}") from exc

    stripped = raw.lstrip()
    if not stripped:
        raise InstrumentMasterUnavailable("the instrument file is empty")

    if stripped[:1] in (b"[", b"{"):
        try:
            payload = json.loads(stripped)
        except ValueError as exc:
            raise InstrumentMasterUnavailable(f"the file is not readable JSON: {exc}") from exc
        if isinstance(payload, Mapping):
            # Some publishers wrap the list; take the first list-valued member
            # rather than guessing at a key name.
            payload = next((value for value in payload.values() if isinstance(value, list)), None)
        if not isinstance(payload, list):
            raise InstrumentMasterUnavailable(
                "the file decoded to JSON but contains no list of instruments"
            )
        for row in payload:
            if isinstance(row, Mapping):
                yield row
        return

    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise InstrumentMasterUnavailable("the file has no header row and is not JSON")
    for row in reader:
        yield {(key or "").strip(): value for key, value in row.items()}


async def fetch(url: str, timeout_seconds: float = 120.0) -> bytes:
    """Download an instrument master file.

    Given a generous timeout because these files are large and are fetched
    rarely — once a session, typically — rather than on any hot path.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise InstrumentMasterUnavailable(f"could not fetch {url}: {exc}") from exc

    if response.status_code >= 400:
        raise InstrumentMasterUnavailable(f"{url} returned HTTP {response.status_code}")
    return response.content

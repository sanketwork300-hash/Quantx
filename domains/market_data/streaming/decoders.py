"""Turning a feed frame into entries this platform can normalise.

A decoder's only job is to get from bytes to ``{provider_key: {field: value}}``.
Everything after that — field mapping, quality scoring, identity — is shared
with the REST path, so a quote that arrives over a socket and the same quote
fetched over HTTP cannot disagree about what they mean.

There is no decoder here that guesses at a binary format. A frame this platform
cannot read is reported as unreadable, with the reason and what to do about it,
because a decoder that produces plausible numbers from a format it has misread
is the single worst failure mode a market-data system has.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any


class FeedDecodeError(Exception):
    """A frame could not be read."""


class DecoderUnavailable(FeedDecodeError):
    """The decoder needs something this deployment does not have.

    Carries instructions rather than just a failure, because the fix is almost
    always a one-line install or a generated module.
    """


class FeedDecoder(ABC):
    name: str = "abstract"

    @abstractmethod
    def decode(self, frame: bytes | str) -> Mapping[str, Any]:
        """Return the frame as a mapping, or raise :class:`FeedDecodeError`."""

    def entries(self, decoded: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
        """Per-instrument entries within a decoded frame.

        Overridden by decoders whose frames wrap the entries in an envelope.
        """
        return {key: value for key, value in decoded.items() if isinstance(value, Mapping)}


class JsonFeedDecoder(FeedDecoder):
    """Text frames carrying JSON.

    Used by feeds that publish JSON, and by the test suite as the transport for
    exercising everything downstream of decoding without a binary schema.
    """

    name = "json"

    def __init__(self, entries_path: str = "feeds") -> None:
        #: Where the per-instrument map sits inside a frame. Empty means the
        #: frame *is* the map.
        self._entries_path = entries_path

    def decode(self, frame: bytes | str) -> Mapping[str, Any]:
        text = frame.decode("utf-8") if isinstance(frame, bytes) else frame
        try:
            decoded = json.loads(text)
        except ValueError as exc:
            raise FeedDecodeError(f"frame is not valid JSON: {exc}") from exc
        if not isinstance(decoded, Mapping):
            raise FeedDecodeError("frame decoded to something other than an object")
        return decoded

    def entries(self, decoded: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
        if not self._entries_path:
            return super().entries(decoded)
        section = decoded.get(self._entries_path)
        if section is None:
            return {}
        if not isinstance(section, Mapping):
            raise FeedDecodeError(f"{self._entries_path!r} is not an object of entries")
        return {key: value for key, value in section.items() if isinstance(value, Mapping)}


class ProtobufFeedDecoder(FeedDecoder):
    """Binary frames described by a provider-published ``.proto``.

    The schema is **not** vendored here and is not reimplemented from
    observation. It is imported from a module the operator generates from the
    provider's own ``.proto`` file, which is the only way to decode a binary
    feed without asserting a wire format nobody has checked.

    Generate it with, for example::

        protoc --python_out=. MarketDataFeedV3.proto

    and point ``QIP_UPSTOX_FEED_PROTO_MODULE`` at the resulting module together
    with the top-level message name.
    """

    name = "protobuf"

    def __init__(self, module_path: str, message_name: str, entries_path: str = "feeds") -> None:
        self._module_path = module_path
        self._message_name = message_name
        self._entries_path = entries_path
        self._message_type: Any = None
        self._to_dict: Any = None

    def _load(self) -> None:
        if self._message_type is not None:
            return
        try:
            from google.protobuf.json_format import MessageToDict
        except ImportError as exc:
            raise DecoderUnavailable(
                "the protobuf runtime is not installed; `pip install protobuf` and generate "
                "the provider's message module before selecting the protobuf decoder"
            ) from exc

        import importlib

        try:
            module = importlib.import_module(self._module_path)
        except ImportError as exc:
            raise DecoderUnavailable(
                f"could not import {self._module_path!r}. Generate it from the provider's "
                "published .proto file (`protoc --python_out=. <file>.proto`) and put it on "
                "the Python path; this platform does not ship a reimplementation of a "
                "provider's wire format"
            ) from exc

        message_type = getattr(module, self._message_name, None)
        if message_type is None:
            raise DecoderUnavailable(
                f"{self._module_path!r} has no message named {self._message_name!r}; "
                f"available: {', '.join(sorted(name for name in dir(module) if name[0].isupper()))}"
            )
        self._message_type = message_type
        self._to_dict = MessageToDict

    def decode(self, frame: bytes | str) -> Mapping[str, Any]:
        self._load()
        if isinstance(frame, str):
            raise FeedDecodeError("a text frame arrived on a binary feed")
        try:
            message = self._message_type.FromString(frame)
        except Exception as exc:  # protobuf raises its own DecodeError type
            raise FeedDecodeError(f"frame did not parse as {self._message_name}: {exc}") from exc
        # ``preserving_proto_field_name`` keeps the provider's own field names,
        # so one normalisation spec describes both the REST and the feed shape.
        return self._to_dict(
            message, preserving_proto_field_name=True, always_print_fields_with_no_presence=False
        )

    def entries(self, decoded: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
        section = decoded.get(self._entries_path) if self._entries_path else decoded
        if not isinstance(section, Mapping):
            return {}
        return {key: value for key, value in section.items() if isinstance(value, Mapping)}

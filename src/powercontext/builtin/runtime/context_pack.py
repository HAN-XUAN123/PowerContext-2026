# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Content-addressed context packs and prefix-reusing wire deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512
# Incremental transport prefers finer chunks: unchanged leading bytes survive a
# change that shifts later boundaries, which keeps deltas small in practice.
INCREMENTAL_CHUNK_BYTES = 128

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})


class ContextPackError(Exception):
    """A context pack or delta violated the wire protocol or its checksums."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete context split into verified byte chunks."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: Iterable[bytes], /) -> ContextPack:
        """Build a complete pack from already split chunks."""

        chunks = tuple(_as_bytes(item) for item in items)
        digest = _pack_digest(chunks)
        byte_count = 0
        while True:
            payload = {
                "schema": CONTEXT_PACK_SCHEMA,
                "items": _encode_items(chunks),
                "digest": digest,
                "byte_count": byte_count,
            }
            size = len(_canonical(payload))
            if size == byte_count:
                break
            byte_count = size
        return cls(items=chunks, digest=digest, byte_count=byte_count)

    @classmethod
    def from_content(cls, content: str, /, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Split UTF-8 content into deterministic, bounded, valid-UTF-8 chunks."""

        return cls.build(_split_content(content, chunk_bytes))

    def to_bytes(self) -> bytes:
        """Serialize the complete pack into its canonical wire form."""

        return _canonical(
            {
                "schema": CONTEXT_PACK_SCHEMA,
                "items": _encode_items(self.items),
                "digest": self.digest,
                "byte_count": self.byte_count,
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes, /) -> ContextPack:
        """Decode and fully verify one complete pack."""

        payload = _load_object(raw, _PACK_FIELDS, CONTEXT_PACK_SCHEMA)
        items = _decode_items(payload["items"])
        digest = payload["digest"]
        byte_count = payload["byte_count"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ContextPackError("pack digest must be sha256:<hex>")  # noqa: TRY003
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            raise ContextPackError("pack byte_count must be a non-negative integer")  # noqa: TRY003
        if byte_count != len(raw):
            raise ContextPackError("pack byte_count does not match its serialized length")  # noqa: TRY003
        if digest != _pack_digest(items):
            raise ContextPackError("pack digest does not match its items")  # noqa: TRY003
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """The reusable prefix of a base pack plus the target's remaining chunks."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Carry the common chunk prefix and the whole target suffix."""

        prefix = 0
        for base_item, target_item in zip(base.items, target.items, strict=False):
            if base_item != target_item:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        """Wire size of this delta, excluding no field but reporting none."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Serialize the delta into its canonical wire form."""

        return _canonical(
            {
                "schema": CONTEXT_PACK_DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes, /) -> ContextPackDelta:
        """Decode and verify one delta envelope."""

        payload = _load_object(raw, _DELTA_FIELDS, CONTEXT_PACK_DELTA_SCHEMA)
        base_digest = payload["base_digest"]
        target_digest = payload["target_digest"]
        for name, value in (("base_digest", base_digest), ("target_digest", target_digest)):
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ContextPackError(f"delta {name} must be sha256:<hex>")  # noqa: TRY003
        prefix_items = payload["prefix_items"]
        if not isinstance(prefix_items, int) or isinstance(prefix_items, bool) or prefix_items < 0:
            raise ContextPackError("delta prefix_items must be a non-negative integer")  # noqa: TRY003
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild the target pack from one base pack and one delta."""

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("apply requires a ContextPack and a ContextPackDelta")  # noqa: TRY003
    if base.digest != delta.base_digest:
        raise ContextPackError("delta was built against a different base pack")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base pack")  # noqa: TRY003
    rebuilt = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match its payload")  # noqa: TRY003
    return rebuilt


def _split_content(content: str, chunk_bytes: int) -> tuple[bytes, ...]:
    if not isinstance(content, str):
        raise ContextPackError("content must be a string")  # noqa: TRY003
    if not isinstance(chunk_bytes, int) or isinstance(chunk_bytes, bool) or chunk_bytes < 1:
        raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ContextPackError("content must be valid UTF-8 text") from error
    if not encoded:
        return ()

    chunks: list[bytes] = []
    current = bytearray()
    for character in content:
        try:
            piece = character.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContextPackError("content must be valid UTF-8 text") from error
        if len(piece) > chunk_bytes:
            raise ContextPackError("a single character exceeds the chunk byte limit")  # noqa: TRY003
        if len(current) + len(piece) > chunk_bytes:
            chunks.append(bytes(current))
            current = bytearray()
        current.extend(piece)
    if current:
        chunks.append(bytes(current))
    return tuple(chunks)


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": CONTEXT_PACK_SCHEMA, "items": _encode_items(items)}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: Any) -> tuple[bytes, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContextPackError("wire items must be a list of Base64 strings")  # noqa: TRY003
    decoded: list[bytes] = []
    for item in value:
        try:
            decoded.append(base64.b64decode(item, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("wire items must be valid Base64") from error
    return tuple(decoded)


def _load_object(raw: bytes, fields: frozenset[str], schema: str) -> Mapping[str, Any]:
    if not isinstance(raw, (bytes, bytearray)):
        raise ContextPackError("wire payload must be bytes")  # noqa: TRY003
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ContextPackError("wire payload must be canonical JSON") from error
    if not isinstance(payload, dict) or set(payload) != set(fields):
        raise ContextPackError("wire payload has missing or extra fields")  # noqa: TRY003
    if payload.get("schema") != schema:
        raise ContextPackError("wire payload declares an unsupported schema")  # noqa: TRY003
    return payload


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _as_bytes(value: object) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ContextPackError("pack items must be bytes")  # noqa: TRY003
    return bytes(value)


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "DEFAULT_CHUNK_BYTES",
    "INCREMENTAL_CHUNK_BYTES",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]

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

"""Capture the original visible events of one Pi session branch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    TextEvidence,
)
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_EVENT_SOURCE_DEFINITION_VERSION = "1"
PI_SESSION_SUPPORTED_VERSION = 3

_MESSAGE_ROLE_TOOL_RESULT = "toolResult"


class PiSessionInput(BaseModel):
    """One complete Pi JSONL archive plus the branch leaf to capture."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, with its untouched record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


class PiEventSource(Source):
    """Captured Source preserving exactly one visible Pi event."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, Any]


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Return the visible events from root to the selected leaf, or raise."""

    document = _load_document(value.content)
    return _events_on_branch(document, value.leaf_id)


def pi_event_source_id(session_id: str, event_id: str, /) -> str:
    """Return the stable identity of one session event, delimiter-safe and bounded."""

    session = _require_identity_part("session_id", session_id)
    event = _require_identity_part("event_id", event_id)
    identity = f"pi:{quote(session, safe='')}:{quote(event, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Pi event identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


class PiEventSourceAdapter:
    """Resolve one parsed event into a captured Source and read it back."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one Pi event as canonical text evidence with its exact identity."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Import newly visible events from one Pi session branch through a lifecycle."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_EVENT_SOURCE_DEFINITION_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        document = _load_document(self.path.read_bytes())
        imported = _imported_digests(session.checkpoint)
        _reject_rewritten_records(document, imported)
        events = _events_on_branch(document, self.leaf_id)

        digests = dict(imported)
        for event in events:
            identity = pi_event_source_id(event.session_id, event.event_id)
            if identity in imported:
                continue
            await session.submit(identity, PI_EVENT_SOURCE_NAME, event)
            digests[identity] = _record_digest(event.record)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"events": {identity: digests[identity] for identity in sorted(digests)}},
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_SOURCE_DEFINITION_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


@dataclass(frozen=True, slots=True)
class _Document:
    session_id: str
    records: tuple[dict[str, Any], ...]
    nodes: Mapping[str, dict[str, Any]]


def _load_document(content: bytes) -> _Document:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session archive must be valid UTF-8") from error

    rows = [line for line in text.splitlines() if line.strip()]
    if not rows:
        raise ValueError("Pi session archive must start with a session record")  # noqa: TRY003
    decoded = [_decode_row(row) for row in rows]

    header = decoded[0]
    if header.get("type") != "session":
        raise ValueError("Pi session archive must start with a session record")  # noqa: TRY003
    if header.get("version") != PI_SESSION_SUPPORTED_VERSION:
        raise ValueError("Pi session archive must declare version 3")  # noqa: TRY003
    session_id = _require_identity_part("session_id", header.get("id"))

    records: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    for record in decoded[1:]:
        event_id = _require_identity_part("event_id", record.get("id"))
        if event_id in nodes:
            raise ValueError(f"duplicate Pi event id {event_id!r}")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError("Pi parentId must be a non-empty string or null")  # noqa: TRY003
            if parent_id not in nodes:
                raise ValueError(f"Pi parent {parent_id!r} was not seen before {event_id!r}")  # noqa: TRY003
        nodes[event_id] = record
        records.append(record)
    return _Document(session_id=session_id, records=tuple(records), nodes=nodes)


def _decode_row(row: str) -> dict[str, Any]:
    try:
        value = json.loads(row)
    except json.JSONDecodeError as error:
        raise ValueError("Pi session archive must contain valid JSON lines") from error
    if not isinstance(value, dict):
        raise ValueError("each Pi session record must be a JSON object")  # noqa: TRY003
    return value


def _events_on_branch(document: _Document, leaf_id: str | None) -> tuple[PiEventInput, ...]:
    if leaf_id is None:
        leaf_id = document.records[-1]["id"] if document.records else None
    if leaf_id is None:
        return ()
    if leaf_id not in document.nodes:
        raise ValueError(f"Pi leaf {leaf_id!r} is not present in the archive")  # noqa: TRY003

    path: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen:
            raise ValueError("Pi session graph must not contain a cycle")  # noqa: TRY003
        seen.add(current)
        node = document.nodes[current]
        path.append(node)
        parent = node.get("parentId")
        current = parent if isinstance(parent, str) else None
    path.reverse()

    events: list[PiEventInput] = []
    for record in path:
        event = _project_event(document.session_id, record)
        if event is not None:
            events.append(event)
    return tuple(events)


def _project_event(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    event_type = record.get("type")
    role: str
    body: str
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        raw_role = message.get("role")
        role = raw_role if isinstance(raw_role, str) and raw_role else "message"
        body = _message_body(role, message)
    elif event_type == "compaction" or event_type == "branch_summary":
        role = str(event_type)
        summary = record.get("summary")
        body = summary if isinstance(summary, str) else ""
    elif event_type == "custom_message":
        role = "custom"
        body = _blocks_text(record.get("content"))
    else:
        return None
    if not body:
        return None
    parent_id = record.get("parentId")
    return PiEventInput(
        session_id=session_id,
        event_id=str(record["id"]),
        parent_id=parent_id if isinstance(parent_id, str) else None,
        event_type=str(event_type),
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(role: str, message: Mapping[str, Any]) -> str:
    if role == _MESSAGE_ROLE_TOOL_RESULT:
        name = message.get("toolName")
        call_id = message.get("toolCallId")
        text = _blocks_text(message.get("content"))
        tool_name = name if isinstance(name, str) else ""
        tool_call_id = call_id if isinstance(call_id, str) else ""
        return f"[tool_result {tool_name} {tool_call_id}] {text}"
    if role == "bashExecution":
        command = message.get("command")
        output = message.get("output")
        return f"$ {command if isinstance(command, str) else ''}\n{output if isinstance(output, str) else ''}"
    return _blocks_text(message.get("content"))


def _blocks_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "toolCall":
            name = block.get("name")
            call_id = block.get("id")
            parts.append(
                f"[tool_call {name if isinstance(name, str) else ''} {call_id if isinstance(call_id, str) else ''}] "
                f"{_compact_json(block.get('arguments'))}"
            )
    return "\n".join(parts)


def _compact_json(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _record_digest(record: Mapping[str, Any]) -> str:
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _imported_digests(checkpoint: object) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, Mapping):
        raise ValueError("Pi checkpoint must be a JSON object")  # noqa: TRY003
    events = checkpoint.get("events", {})
    if not isinstance(events, Mapping):
        raise ValueError("Pi checkpoint events must be a JSON object")  # noqa: TRY003
    imported: dict[str, str] = {}
    for identity, digest in events.items():
        if not isinstance(identity, str) or not isinstance(digest, str):
            raise ValueError("Pi checkpoint entries must map identity to a digest")  # noqa: TRY003
        imported[identity] = digest
    return imported


def _reject_rewritten_records(document: _Document, imported: Mapping[str, str]) -> None:
    for record in document.records:
        identity = pi_event_source_id(document.session_id, str(record["id"]))
        expected = imported.get(identity)
        if expected is not None and _record_digest(record) != expected:
            raise ValueError(f"Pi event {record['id']!r} was modified after it was imported")  # noqa: TRY003


def _require_identity_part(field: str, value: object) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")  # noqa: TRY003
    return value


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_DEFINITION_VERSION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]

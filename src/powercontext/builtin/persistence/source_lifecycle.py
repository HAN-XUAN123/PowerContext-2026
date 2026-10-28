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

"""Durable evidence-dependency nodes, Source events, and Topic restrictions."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.errors import InvalidRepositoryArgumentError
from powercontext.builtin.persistence.supervision import database_utc_now
from powercontext.builtin.persistence.tables import (
    SOURCE_LIFECYCLE_EVENTS_TABLE,
    SOURCE_LIFECYCLE_NODES_TABLE,
    TOPIC_MEMORY_LIFECYCLE_TABLE,
)
from powercontext.limits import MAX_SCOPE_ID_LENGTH
from powercontext.sources import SourceRef

Reference = SourceRef | MemoryCitation | ArtifactRef
_KINDS = frozenset({"source", "memory", "topic"})
_POLICIES = frozenset({"all", "any"})


@dataclass(frozen=True, slots=True)
class LifecycleNode:
    """One registered dependency node and its authoritative availability."""

    scope_id: str
    node_id: str
    ordinal: int
    kind: str
    revision: int | None
    sequence: int
    available: bool
    policy: str
    parents: tuple[str, ...]
    reference: Reference


@dataclass(frozen=True, slots=True)
class LifecycleEventRecord:
    """One accepted Source lifecycle event."""

    node_id: str
    sequence: int
    revision: int
    available: bool
    reason: str


class SourceLifecycleRepository:
    """Persist nodes, events, and Topic restrictions with caller-owned transactions."""

    async def load_nodes(self, connection: AsyncConnection, scope_id: str, /) -> tuple[LifecycleNode, ...]:
        _require_scope(scope_id)
        table = SOURCE_LIFECYCLE_NODES_TABLE
        rows = (
            await connection.execute(
                select(table).where(table.c.scope_id == scope_id).order_by(table.c.ordinal)
            )
        ).mappings()
        return tuple(_node(row) for row in rows)

    async def load_node(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        for_update: bool = False,
    ) -> LifecycleNode | None:
        _require_scope(scope_id)
        _require_node(node_id)
        table = SOURCE_LIFECYCLE_NODES_TABLE
        statement = select(table).where(table.c.scope_id == scope_id, table.c.node_id == node_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).mappings().one_or_none()
        return None if row is None else _node(row)

    async def next_ordinal(self, connection: AsyncConnection, scope_id: str, /) -> int:
        _require_scope(scope_id)
        table = SOURCE_LIFECYCLE_NODES_TABLE
        highest = await connection.scalar(
            select(func.max(table.c.ordinal)).where(table.c.scope_id == scope_id)
        )
        return 0 if highest is None else int(highest) + 1

    async def insert_node(self, connection: AsyncConnection, node: LifecycleNode, /) -> None:
        await connection.execute(
            SOURCE_LIFECYCLE_NODES_TABLE.insert().values(
                scope_id=node.scope_id,
                node_id=node.node_id,
                ordinal=node.ordinal,
                kind=node.kind,
                revision=node.revision,
                sequence=node.sequence,
                available=node.available,
                policy=node.policy,
                parents=_dump_parents(node.parents),
                reference=_dump_reference(node.reference),
            )
        )

    async def set_availability(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        available: bool,
        sequence: int | None = None,
    ) -> None:
        _require_scope(scope_id)
        _require_node(node_id)
        table = SOURCE_LIFECYCLE_NODES_TABLE
        values: dict[str, object] = {"available": available}
        if sequence is not None:
            values["sequence"] = sequence
        await connection.execute(
            update(table)
            .where(table.c.scope_id == scope_id, table.c.node_id == node_id)
            .values(**values)
        )

    async def load_event(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        sequence: int,
        /,
    ) -> LifecycleEventRecord | None:
        _require_scope(scope_id)
        _require_node(node_id)
        table = SOURCE_LIFECYCLE_EVENTS_TABLE
        row = (
            (
                await connection.execute(
                    select(table).where(
                        table.c.scope_id == scope_id,
                        table.c.node_id == node_id,
                        table.c.sequence == sequence,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return LifecycleEventRecord(
            node_id=str(row["node_id"]),
            sequence=int(row["sequence"]),
            revision=int(row["revision"]),
            available=bool(row["available"]),
            reason=str(row["reason"]),
        )

    async def last_sequence(self, connection: AsyncConnection, scope_id: str, node_id: str, /) -> int:
        _require_scope(scope_id)
        _require_node(node_id)
        table = SOURCE_LIFECYCLE_EVENTS_TABLE
        highest = await connection.scalar(
            select(func.max(table.c.sequence)).where(table.c.scope_id == scope_id, table.c.node_id == node_id)
        )
        return 0 if highest is None else int(highest)

    async def insert_event(
        self,
        connection: AsyncConnection,
        scope_id: str,
        event: LifecycleEventRecord,
        /,
    ) -> None:
        _require_scope(scope_id)
        await connection.execute(
            SOURCE_LIFECYCLE_EVENTS_TABLE.insert().values(
                scope_id=scope_id,
                node_id=event.node_id,
                sequence=event.sequence,
                revision=event.revision,
                available=event.available,
                reason=event.reason,
                recorded_at=await database_utc_now(connection),
            )
        )

    async def suppressed_artifacts(self, connection: AsyncConnection, scope_id: str, /) -> frozenset[str]:
        _require_scope(scope_id)
        table = TOPIC_MEMORY_LIFECYCLE_TABLE
        rows = await connection.scalars(select(table.c.artifact_id).where(table.c.scope_id == scope_id))
        return frozenset(str(value) for value in rows)

    async def set_topic_restricted(
        self,
        connection: AsyncConnection,
        scope_id: str,
        artifact_id: str,
        node_id: str,
        /,
        *,
        restricted: bool,
    ) -> None:
        _require_scope(scope_id)
        _require_node(artifact_id)
        _require_node(node_id)
        table = TOPIC_MEMORY_LIFECYCLE_TABLE
        if not restricted:
            await connection.execute(
                delete(table).where(table.c.scope_id == scope_id, table.c.artifact_id == artifact_id)
            )
            return
        values = {"scope_id": scope_id, "artifact_id": artifact_id, "node_id": node_id}
        if connection.dialect.name == "sqlite":
            statement = sqlite_insert(table).values(**values).on_conflict_do_nothing()
        elif connection.dialect.name == "mysql":
            statement = mysql_insert(table).values(**values).prefix_with("IGNORE")
        else:
            raise InvalidRepositoryArgumentError("dialect", f"{connection.dialect.name!r} is not supported")
        await connection.execute(statement)


def _node(row: object) -> LifecycleNode:
    values = dict(row)  # type: ignore[arg-type]
    kind = str(values["kind"])
    return LifecycleNode(
        scope_id=str(values["scope_id"]),
        node_id=str(values["node_id"]),
        ordinal=int(values["ordinal"]),
        kind=kind,
        revision=None if values["revision"] is None else int(values["revision"]),
        sequence=int(values["sequence"]),
        available=bool(values["available"]),
        policy=str(values["policy"]),
        parents=_load_parents(values["parents"]),
        reference=_load_reference(kind, values["reference"]),
    )


def _dump_parents(parents: tuple[str, ...]) -> str:
    return json.dumps(list(parents), separators=(",", ":"), ensure_ascii=False)


def _load_parents(value: object) -> tuple[str, ...]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode()
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise InvalidRepositoryArgumentError("parents", "must be a JSON array of node ids")
    return tuple(decoded)


def _dump_reference(reference: Reference) -> bytes:
    payload = reference.model_dump(mode="json", by_alias=True)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _load_reference(kind: str, value: object) -> Reference:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    elif isinstance(value, str):
        raw = value.encode()
    else:
        raise InvalidRepositoryArgumentError("reference", "must store canonical JSON bytes")
    payload = json.loads(raw)
    if kind == "source":
        return SourceRef.model_validate(payload)
    if kind == "memory":
        return MemoryCitation.model_validate(payload)
    if kind == "topic":
        return ArtifactRef.model_validate(payload)
    raise InvalidRepositoryArgumentError("kind", "must be source, memory, or topic")


def validate_kind(kind: str) -> str:
    if kind not in _KINDS:
        raise InvalidRepositoryArgumentError("kind", "must be source, memory, or topic")
    return kind


def validate_policy(policy: str) -> str:
    if policy not in _POLICIES:
        raise InvalidRepositoryArgumentError("policy", "must be all or any")
    return policy


def _require_scope(scope_id: str) -> None:
    if not isinstance(scope_id, str) or not scope_id.strip() or scope_id != scope_id.strip():
        raise InvalidRepositoryArgumentError("scope_id", "must be a non-empty trimmed identifier")
    if len(scope_id) > MAX_SCOPE_ID_LENGTH:
        raise InvalidRepositoryArgumentError("scope_id", "must be within the identity limit")


def _require_node(node_id: str) -> None:
    if not isinstance(node_id, str) or not node_id.strip() or node_id != node_id.strip():
        raise InvalidRepositoryArgumentError("node_id", "must be a non-empty trimmed identifier")
    if len(node_id) > 256:
        raise InvalidRepositoryArgumentError("node_id", "must be within the identity limit")


__all__ = [
    "LifecycleEventRecord",
    "LifecycleNode",
    "Reference",
    "SourceLifecycleRepository",
    "validate_kind",
    "validate_policy",
]

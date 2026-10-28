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

"""Propagate Source withdrawal and restoration through evidence dependencies.

Every Scope owns one directed acyclic graph whose Sources carry a lifecycle
revision and event sequence and whose derived nodes (Memory entries and Topic
Memory revisions) declare an explicit ``all``/``any`` policy over their
registered parents. An accepted event changes exactly one Source and then
recomputes availability in registration order, which is a topological order
because a node can only name already registered parents. Native Memory state
and Topic search eligibility follow the propagated result; the original
references stay readable for audit even while a node is unavailable.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import Memory, MemoryEntryVersion, MemoryService
from powercontext.builtin.artifacts.memory.errors import (
    CapabilityNotSupportedError,
    InvalidMemoryCitationError,
    MemoryEntryNotFoundError,
)
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.persistence.source_lifecycle import (
    LifecycleEventRecord,
    LifecycleNode,
    Reference,
    SourceLifecycleRepository,
    validate_kind,
    validate_policy,
)
from powercontext.builtin.records import BaseAccessError
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.errors import ArtifactNotFoundError, RevisionConflictError
from powercontext.sources import SourceRef

_MAX_NODE_ID_LENGTH = 256
_PURPOSES = frozenset({"generation", "audit"})
_TOPIC_FAMILY = "topic-memory"


class SourceLifecycleError(ValueError):
    """Report an invalid dependency node, lifecycle event, or resolution."""


class SourceLifecycleEvent(BaseModel):
    """One strictly validated Source availability event."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str = Field(max_length=_MAX_NODE_ID_LENGTH)
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty, non-blank string")
        return value


class SourceLifecycleApplication:
    """Select the Source lifecycle service for one Scope."""

    def __init__(self, runtime: Any, database: AsyncDatabase | None) -> None:
        self._runtime = runtime
        self._database = database

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, self._database, scope_id)


class ScopedSourceLifecycle:
    """Register dependency nodes, apply events, and resolve node references."""

    def __init__(self, runtime: Any, database: AsyncDatabase | None, scope_id: str) -> None:
        self._runtime = runtime
        self._database = database
        self.scope_id = validate_scope_id(scope_id)
        self._nodes = SourceLifecycleRepository()

    async def register_source(
        self,
        node_id: str,
        reference: SourceRef,
        /,
        *,
        revision: int,
    ) -> None:
        """Register one captured Source at its lifecycle revision."""

        _require_node_id(node_id)
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")
        if not isinstance(reference, SourceRef):
            raise SourceLifecycleError("source registration requires a SourceRef")
        await self._require_source(reference)
        await self._register(
            node_id=node_id,
            kind="source",
            reference=reference,
            parents=(),
            policy="all",
            revision=revision,
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...],
        policy: Literal["all", "any"],
    ) -> None:
        """Register one Memory entry and the dependencies that support it."""

        _require_node_id(node_id)
        _require_policy(policy)
        _require_parents(parents)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory registration requires a MemoryCitation")
        await self._require_memory(citation)
        await self._register(
            node_id=node_id,
            kind="memory",
            reference=citation,
            parents=parents,
            policy=policy,
            revision=None,
        )

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: Literal["all", "any"],
    ) -> None:
        """Register one Topic Memory revision and the dependencies that support it."""

        _require_node_id(node_id)
        _require_policy(policy)
        _require_parents(parents)
        if not isinstance(reference, ArtifactRef) or reference.family != _TOPIC_FAMILY:
            raise SourceLifecycleError("topic registration requires a topic-memory ArtifactRef")
        await self._require_topic(reference)
        await self._register(
            node_id=node_id,
            kind="topic",
            reference=reference,
            parents=parents,
            policy=policy,
            revision=None,
        )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one Source event and return every currently available node id."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply requires a SourceLifecycleEvent")
        async with self._transaction() as connection:
            node = await self._nodes.load_node(connection, self.scope_id, event.node_id, for_update=True)
            if node is None or node.kind != "source":
                raise SourceLifecycleError("event requires a registered Source")
            if node.revision != event.revision:
                raise SourceLifecycleError("event revision does not match the registered revision")
            recorded = await self._nodes.load_event(
                connection, self.scope_id, event.node_id, event.sequence
            )
            if recorded is not None:
                if (
                    recorded.revision,
                    recorded.available,
                    recorded.reason,
                ) != (event.revision, event.available, event.reason):
                    raise SourceLifecycleError("event sequence was already recorded with different content")
            else:
                latest = await self._nodes.last_sequence(connection, self.scope_id, event.node_id)
                if event.sequence <= latest:
                    raise SourceLifecycleError("event sequence must increase for one Source")
                await self._nodes.insert_event(
                    connection,
                    self.scope_id,
                    LifecycleEventRecord(
                        node_id=event.node_id,
                        sequence=event.sequence,
                        revision=event.revision,
                        available=event.available,
                        reason=event.reason,
                    ),
                )
                await self._nodes.set_availability(
                    connection,
                    self.scope_id,
                    event.node_id,
                    available=event.available,
                    sequence=event.sequence,
                )
            nodes = await self._nodes.load_nodes(connection, self.scope_id)
            nodes = await self._persist_propagation(connection, nodes)
        for item in nodes:
            if item.kind == "source":
                continue
            await self._sync_native(item, reason=event.reason)
        return sorted(item.node_id for item in nodes if item.available)

    async def resolve(self, node_id: str, /, *, purpose: str) -> Reference:
        """Return the original reference when the node qualifies for one purpose."""

        _require_node_id(node_id)
        if purpose not in _PURPOSES:
            raise SourceLifecycleError("purpose must be generation or audit")
        async with self._transaction() as connection:
            node = await self._nodes.load_node(connection, self.scope_id, node_id)
        if node is None:
            raise SourceLifecycleError("unknown lifecycle node")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("node is not available for generation")
        return node.reference

    async def _register(
        self,
        *,
        node_id: str,
        kind: str,
        reference: Reference,
        parents: tuple[str, ...],
        policy: str,
        revision: int | None,
    ) -> None:
        validate_kind(kind)
        validate_policy(policy)
        created = False
        async with self._transaction() as connection:
            existing = await self._nodes.load_node(connection, self.scope_id, node_id, for_update=True)
            if existing is not None:
                if not _same_binding(existing, kind, reference, parents, policy, revision):
                    raise SourceLifecycleError("node id is already bound to a different dependency node")
                node = existing
            else:
                parent_nodes = []
                for parent_id in parents:
                    parent = await self._nodes.load_node(connection, self.scope_id, parent_id, for_update=True)
                    if parent is None:
                        raise SourceLifecycleError("every parent must be registered first")
                    parent_nodes.append(parent)
                available = True
                if kind != "source":
                    support = [parent.available for parent in parent_nodes]
                    available = all(support) if policy == "all" else any(support)
                node = LifecycleNode(
                    scope_id=self.scope_id,
                    node_id=node_id,
                    ordinal=await self._nodes.next_ordinal(connection, self.scope_id),
                    kind=kind,
                    revision=revision,
                    sequence=0,
                    available=available,
                    policy=policy,
                    parents=parents,
                    reference=reference,
                )
                await self._nodes.insert_node(connection, node)
                created = True
        if created and not node.available:
            await self._sync_native(node, reason="registered without support")

    async def _persist_propagation(
        self,
        connection: AsyncConnection,
        nodes: tuple[LifecycleNode, ...],
    ) -> tuple[LifecycleNode, ...]:
        effective: dict[str, bool] = {}
        updated: list[LifecycleNode] = []
        for node in nodes:
            if node.kind == "source":
                available = node.available
            else:
                support = [effective.get(parent, False) for parent in node.parents]
                available = all(support) if node.policy == "all" else any(support)
                if available != node.available:
                    await self._nodes.set_availability(
                        connection, self.scope_id, node.node_id, available=available
                    )
                    node = replace(node, available=available)
            effective[node.node_id] = available
            updated.append(node)
        return tuple(updated)

    async def _sync_native(self, node: LifecycleNode, *, reason: str) -> None:
        if node.kind == "memory":
            await self._sync_memory(node, reason)
        elif node.kind == "topic":
            await self._sync_topic(node)

    async def _sync_memory(self, node: LifecycleNode, reason: str) -> None:
        citation = node.reference
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory node lost its citation")
        async with self._runtime._context(self.scope_id) as context:
            service: MemoryService = context.artifacts.memory
            if await self._memory_matches(service, citation, node.available):
                return
            async with self._runtime._locked(self.scope_id):
                memory, entry, state = await self._memory_entry(service, citation)
                if entry is None or state == _state(node.available):
                    return
                if node.available:
                    await service.reactivate(memory, entries=(entry,), reason=reason)
                else:
                    await service.forget(memory, entries=(entry,), reason=reason)

    async def _sync_topic(self, node: LifecycleNode) -> None:
        if not isinstance(node.reference, ArtifactRef):
            raise SourceLifecycleError("topic node lost its artifact reference")
        async with self._transaction() as connection:
            await self._nodes.set_topic_restricted(
                connection,
                self.scope_id,
                node.reference.artifact_id,
                node.node_id,
                restricted=not node.available,
            )

    async def _memory_matches(self, service: MemoryService, citation: MemoryCitation, available: bool) -> bool:
        _memory, entry, state = await self._memory_entry(service, citation)
        return entry is not None and state == _state(available)

    async def _memory_entry(
        self,
        service: MemoryService,
        citation: MemoryCitation,
    ) -> tuple[Memory | None, MemoryEntryVersion | None, str | None]:
        try:
            memory = await service.head(citation.memory_ref.artifact_id)
        except ArtifactNotFoundError:
            return None, None, None
        state = next(
            (
                item.state
                for item in memory.content.manifest.entries
                if item.entry_id == citation.entry_id
            ),
            None,
        )
        if state is None:
            return memory, None, None
        entries = await service.entries(memory)
        entry = next((item for item in entries if item.entry_id == citation.entry_id), None)
        return memory, entry, state

    async def _require_source(self, reference: SourceRef) -> None:
        records = self._runtime.records
        if records is None:
            raise SourceLifecycleError("runtime has no record service")
        try:
            await records.for_scope(self.scope_id).get_source(reference.source_type, reference.source_id)
        except (BaseAccessError, RepositoryNotFoundError) as error:
            raise SourceLifecycleError("registered Source is not readable") from error

    async def _require_memory(self, citation: MemoryCitation) -> None:
        try:
            await self._runtime.memory.for_scope(self.scope_id).get(
                GetMemoryEntryRequest(citation=citation)
            )
        except (
            ArtifactNotFoundError,
            RevisionConflictError,
            MemoryEntryNotFoundError,
            InvalidMemoryCitationError,
            CapabilityNotSupportedError,
            RepositoryNotFoundError,
            BaseAccessError,
        ) as error:
            raise SourceLifecycleError("registered Memory citation is not readable") from error

    async def _require_topic(self, reference: ArtifactRef) -> None:
        records = self._runtime.records
        if records is None:
            raise SourceLifecycleError("runtime has no record service")
        try:
            await records.for_scope(self.scope_id).get_artifact_revision(
                reference.family, reference.artifact_id, reference.revision
            )
        except (BaseAccessError, RepositoryNotFoundError) as error:
            raise SourceLifecycleError("registered Topic revision is not readable") from error

    def _transaction(self):
        if self._database is None:
            raise SourceLifecycleError("runtime has no lifecycle database")
        return self._database.transaction()


def _state(available: bool) -> str:
    return "active" if available else "inactive"


def _same_binding(
    existing: LifecycleNode,
    kind: str,
    reference: Reference,
    parents: tuple[str, ...],
    policy: str,
    revision: int | None,
) -> bool:
    return (
        existing.kind == kind
        and existing.reference == reference
        and existing.parents == tuple(parents)
        and existing.policy == policy
        and existing.revision == revision
    )


def _require_node_id(node_id: str) -> None:
    if not isinstance(node_id, str) or not node_id.strip():
        raise SourceLifecycleError("node_id must be a non-empty, non-blank string")
    if len(node_id) > _MAX_NODE_ID_LENGTH:
        raise SourceLifecycleError("node_id exceeds the identity limit")


def _require_parents(parents: tuple[str, ...]) -> None:
    if not isinstance(parents, tuple) or not parents:
        raise SourceLifecycleError("a derived node requires at least one parent")
    if len(set(parents)) != len(parents):
        raise SourceLifecycleError("a derived node cannot name one parent twice")
    for parent in parents:
        _require_node_id(parent)


def _require_policy(policy: str) -> None:
    if policy not in {"all", "any"}:
        raise SourceLifecycleError("policy must be all or any")


__all__ = [
    "ScopedSourceLifecycle",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
]

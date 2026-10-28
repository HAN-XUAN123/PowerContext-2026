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

"""Share in-flight Embedding work and cache successful vectors across calls.

One wrapper coordinates every caller on the same event loop. Identical texts
registered by any call join the same pending future, so a late caller reuses a
request that is already executing, and a completed batch is cached for callers
that arrive afterwards. Each call keeps its own ordered, duplicated result and
its own attribution of the provider usage it actually waited on.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass(slots=True)
class _Batch:
    """One provider request shared by every pending text in it."""

    usage: InferenceUsage | None = None


@dataclass(slots=True)
class _Pending:
    """One un-embedded text identity and the future all of its callers await."""

    text: str
    future: asyncio.Future[EmbeddingVector]
    batch: _Batch | None = field(default=None)


class CoalescingEmbeddingModel:
    """Coalesce concurrent Embedding calls and cache successful vectors.

    ``max_batch_size`` bounds the deduplicated texts per provider request,
    ``coalesce_ms`` is the merge window before a provider request starts (0
    dispatches on the next loop step while still merging same-tick callers), and
    ``cache_size`` bounds the successful-vector LRU (0 disables cross-call
    caching without disabling in-flight sharing).
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, int | float) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self.profile: EmbeddingProfile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._inflight: dict[str, _Pending] = {}
        self._pending: list[_Pending] = []
        self._flush_handle: asyncio.Handle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one ordered vector per input text, sharing provider work."""

        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        resolved: dict[str, EmbeddingVector] = {}
        waits: list[_Pending] = []
        registered = False
        loop = asyncio.get_running_loop()
        for text in dict.fromkeys(texts):
            cached = self._cached(text)
            if cached is not None:
                resolved[text] = cached
                continue
            pending = self._inflight.get(text)
            if pending is None:
                pending = _Pending(text=text, future=loop.create_future())
                self._inflight[text] = pending
                self._pending.append(pending)
                registered = True
            waits.append(pending)
        if registered:
            self._schedule_flush()
        if waits:
            try:
                await asyncio.gather(*(asyncio.shield(item.future) for item in waits))
            except asyncio.CancelledError:
                # Only this caller stops waiting. The shared batch keeps running
                # for every other caller because its future was shielded.
                raise
            for item in waits:
                resolved[item.text] = item.future.result()
        return EmbeddingResult(
            vectors=tuple(resolved[text] for text in texts),
            usage=self._usage(waits),
        )

    def _cached(self, text: str) -> EmbeddingVector | None:
        if self._cache_size <= 0:
            return None
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _store(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size <= 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _schedule_flush(self) -> None:
        if self._flush_handle is not None or not self._pending:
            return
        loop = asyncio.get_running_loop()
        if self._coalesce_seconds <= 0:
            # call_soon still lets every same-tick caller register first.
            self._flush_handle = loop.call_soon(self._flush)
        else:
            self._flush_handle = loop.call_later(self._coalesce_seconds, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        pending, self._pending = self._pending, []
        if not pending:
            return
        loop = asyncio.get_running_loop()
        for start in range(0, len(pending), self._max_batch_size):
            items = pending[start : start + self._max_batch_size]
            batch = _Batch()
            for item in items:
                item.batch = batch
            task = loop.create_task(self._run_batch(items, batch), name="powercontext-embedding-batch")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_batch(self, items: list[_Pending], batch: _Batch) -> None:
        texts = tuple(item.text for item in items)
        try:
            result = await self._model.embed(texts)
        except asyncio.CancelledError:
            self._discard(items)
            for item in items:
                if not item.future.done():
                    item.future.cancel()
            raise
        except BaseException as error:
            self._discard(items)
            for item in items:
                if not item.future.done():
                    item.future.set_exception(error)
            return
        if len(result.vectors) != len(texts):
            error = ValueError("embedding provider returned an unexpected vector count")
            self._discard(items)
            for item in items:
                if not item.future.done():
                    item.future.set_exception(error)
            return
        batch.usage = result.usage
        for item, vector in zip(items, result.vectors, strict=True):
            self._inflight.pop(item.text, None)
            self._store(item.text, tuple(vector))
            if not item.future.done():
                item.future.set_result(tuple(vector))

    def _discard(self, items: list[_Pending]) -> None:
        for item in items:
            self._inflight.pop(item.text, None)

    def _usage(self, waits: list[_Pending]) -> InferenceUsage:
        """Sum each distinct provider batch this call actually waited on once."""

        batches: dict[int, _Batch] = {}
        for item in waits:
            if item.batch is not None:
                batches.setdefault(id(item.batch), item.batch)
        usages = [batch.usage for batch in batches.values() if batch.usage is not None]
        if not usages:
            return InferenceUsage(requests=0)
        input_tokens = None
        output_tokens = None
        if any(usage.input_tokens is not None for usage in usages):
            input_tokens = sum(usage.input_tokens or 0 for usage in usages)
        if any(usage.output_tokens is not None for usage in usages):
            output_tokens = sum(usage.output_tokens or 0 for usage in usages)
        return InferenceUsage(
            requests=sum(usage.requests for usage in usages),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


__all__ = ["CoalescingEmbeddingModel"]

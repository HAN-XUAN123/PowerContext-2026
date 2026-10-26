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

"""Score Memory candidates through one batched HTTP cross-encoder service."""

from __future__ import annotations

import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Rank one coarse candidate pool by scores returned from an HTTP service.

    Each batch carries the original one-based rank of every candidate, so the
    scores of separate responses are merged back into one global ordering.
    """

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("reranker endpoint must be a non-empty string")  # noqa: TRY003
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("reranker batch_size must be a positive integer")  # noqa: TRY003
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("reranker policy_id must be a non-empty string")  # noqa: TRY003
        self.endpoint = endpoint
        self.batch_size = batch_size
        self.policy_id = policy_id
        self._client = client

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Request every candidate once, then select the globally best ranks."""

        if len(candidates) < 1:
            raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
        if limit < 1 or limit > len(candidates):
            raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start : start + self.batch_size]
            ranks = tuple(range(start + 1, start + 1 + len(batch)))
            response = await self._client.post(
                self.endpoint,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": candidate.text} for rank, candidate in zip(ranks, batch)],
                },
            )
            response.raise_for_status()
            requests += 1
            scores.update(_parse_scores(response, expected=ranks))

        ordered = tuple(sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit])
        return MemoryRerankDecision(selected_ranks=ordered, usage=InferenceUsage(requests=requests))


def _parse_scores(response: httpx.Response, *, expected: tuple[int, ...]) -> dict[int, float]:
    """Validate one batch response and return its rank-to-score mapping."""

    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("reranker response must be valid JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("reranker response must contain exactly one 'scores' field")  # noqa: TRY003
    items = payload["scores"]
    if not isinstance(items, list):
        raise ValueError("reranker 'scores' must be a list")  # noqa: TRY003

    expected_ranks = set(expected)
    scores: dict[int, float] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"rank", "score"}:
            raise ValueError("each reranker score must contain exactly 'rank' and 'score'")  # noqa: TRY003
        rank = item["rank"]
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise ValueError("reranker rank must be an integer")  # noqa: TRY003
        if rank not in expected_ranks:
            raise ValueError(f"reranker returned rank {rank!r} outside its batch")  # noqa: TRY003
        if rank in scores:
            raise ValueError(f"reranker returned rank {rank!r} more than once")  # noqa: TRY003
        score = item["score"]
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
            raise ValueError("reranker score must be a finite number")  # noqa: TRY003
        scores[rank] = float(score)

    missing = expected_ranks.difference(scores)
    if missing:
        raise ValueError(f"reranker response is missing ranks {sorted(missing)!r}")  # noqa: TRY003
    return scores


__all__ = ["HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]

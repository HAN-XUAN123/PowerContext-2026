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

"""Parse every physical PDF page and retrieve citable page evidence."""

from __future__ import annotations

import hashlib
import io
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from pydantic import BaseModel
from pypdf import PdfReader

from powercontext.artifacts.models import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory import MemoryHit

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_SOURCE_DEFINITION_VERSION = "1"

_TOKEN = re.compile(r"[a-z]+|\d+")
_MAX_SINGLE_TERM_QUERIES = 10
_MAX_KEYWORD_TERMS = 12

_STOP_WORDS = frozenset(
    {        "a",
        "about",
        "after",
        "all",
        "also",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "both",
        "by",
        "can",
        "company",
        "did",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "more",
        "most",
        "not",
        "of",
        "on",
        "or",
        "per",
        "reported",
        "respond",
        "said",
        "say",
        "s",
        "state",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "under",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "within",
        "would",
        "year",
        "years",
    }
)

# Generic 10-K vocabulary that maps a question concept to the wording a filing uses.
_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "geograph": ("geographic", "geographies", "regions", "segments", "countries", "markets"),
    "battle": ("legal", "proceedings", "litigation", "contingencies", "claims"),
    "legal": ("legal", "proceedings", "litigation", "contingencies", "commitments"),
    "lawsuit": ("legal", "proceedings", "litigation", "contingencies"),
    "litigation": ("legal", "proceedings", "contingencies", "commitments"),
    "restructur": ("restructuring", "charges", "productivity", "severance", "plan"),
    "ebitda": ("operating", "income", "depreciation", "amortization", "ebit"),
    "capex": ("capital", "expenditures", "purchases", "property", "plant", "equipment"),
    "margin": ("margin", "operating", "profit", "revenue", "net"),
    "revenue": ("revenue", "sales", "net"),
    "sales": ("sales", "revenue", "net"),
    "segment": ("segments", "reportable", "geographic"),
    "debt": ("debt", "borrowings", "notes", "commercial", "paper"),
    "dividend": ("dividends", "share", "repurchase"),
    "pension": ("pension", "retirement", "benefits"),
    "tax": ("tax", "effective", "rate", "provision"),
    "goodwill": ("goodwill", "impairment", "intangible"),
    "employee": ("employees", "headcount", "workforce"),
    "lease": ("lease", "leases", "rent"),
    "inventory": ("inventory", "inventories"),
    "cash": ("cash", "flows", "operating", "investing", "financing"),
    "share": ("share", "repurchase", "dividends"),
}


class PDFDocumentInput(BaseModel):
    """One complete original PDF to parse page by page."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical PDF page with its extractable text."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """Captured Source for exactly one physical page of one document revision."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def parse_pdf_document(value: PDFDocumentInput, /) -> tuple[PDFPageInput, ...]:
    """Parse every physical page in order, or raise without returning partial pages."""

    document_id = value.document_id
    if not document_id or not document_id.strip():
        raise ValueError("document_id must not be empty or blank")  # noqa: TRY003
    content = value.content
    if not isinstance(content, bytes) or not content:
        raise ValueError("PDF content must be a non-empty byte string")  # noqa: TRY003
    digest = hashlib.sha256(content).hexdigest()

    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a user password")  # noqa: TRY003
        texts = [page.extract_text() or "" for page in reader.pages]
    except ValueError:
        raise
    except Exception as error:  # pypdf raises several unrelated parser exceptions
        raise ValueError("PDF document could not be fully parsed") from error
    if not texts and reader.get_num_pages() != 0:
        raise ValueError("PDF document could not be fully parsed")  # noqa: TRY003

    return tuple(
        PDFPageInput(
            document_id=document_id,
            document_sha256=digest,
            page_number=number,
            text=text,
        )
        for number, text in enumerate(texts, start=1)
    )


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int, /) -> str:
    """Return the stable identity of one page of one exact document revision."""

    if not document_id or not document_id.strip():
        raise ValueError("document_id must not be empty or blank")  # noqa: TRY003
    if not re.fullmatch(r"[0-9a-f]{64}", document_sha256):
        raise ValueError("document_sha256 must be a lowercase SHA-256 hex digest")  # noqa: TRY003
    if not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003
    encoded = quote(document_id, safe="")
    document_token = f"q{encoded}" if len(encoded) <= 100 else f"h{hashlib.sha256(document_id.encode()).hexdigest()}"
    identity = f"pdf:{document_token}:{document_sha256[:32]}:{page_number}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"PDF page identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


class PDFPageSourceAdapter:
    """Resolve one parsed page into a captured Source and read it back."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one physical page as citable text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_SOURCE_DEFINITION_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` citations to distinct pages that may answer ``question``."""

    if limit < 1:
        return ()
    variants = _query_variants(question)
    if not variants:
        return ()

    from powercontext.builtin.runtime import SearchMemoryRequest

    per_query = max(80, min(250, limit * 10))
    candidates: dict[str, _Candidate] = {}
    for query, weight in variants:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=per_query))
        for rank, hit in enumerate(page.hits, start=1):
            current = candidates.get(hit.entry_id)
            if current is None:
                current = _Candidate(hit=hit)
                candidates[hit.entry_id] = current
            current.contributions.append(weight / (60.0 + rank))

    if not candidates:
        return ()
    return tuple(_rank(candidates, question, limit))


@dataclass(slots=True)
class _Candidate:
    hit: MemoryHit
    contributions: list[float] = field(default_factory=list)


def _rank(candidates: dict[str, _Candidate], question: str, limit: int) -> list[MemoryCitation]:
    documents = {key: _tokenize(_body(item.hit.text)) for key, item in candidates.items()}
    lengths = {key: len(tokens) for key, tokens in documents.items()}
    average = (sum(lengths.values()) / len(lengths)) if lengths else 1.0
    frequencies: dict[str, int] = {}
    for tokens in documents.values():
        for term in set(tokens):
            frequencies[term] = frequencies.get(term, 0) + 1
    total = len(documents)

    weights: dict[str, float] = {}
    for term, term_weight in _scoring_terms(question):
        if term in weights:
            continue
        df = frequencies.get(term, 0)
        if not df or df >= total * 0.6:
            continue
        weights[term] = term_weight * math.log(1.0 + (total - df + 0.5) / (df + 0.5))

    scores: dict[str, float] = {}
    for key, tokens in documents.items():
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        length = lengths[key]
        score = 0.0
        strongest = 0.0
        for term, weight in weights.items():
            tf = counts.get(term, 0)
            if not tf:
                continue
            score += weight * (tf * 2.2) / (tf + 1.2 * (1.0 - 0.75 + 0.75 * length / average))
            strongest = max(strongest, weight)
        # A page carrying the rarest question concept is far more likely to be the source.
        score += 4.0 * strongest
        # Reciprocal-rank fusion rewards pages the backend ranked highly for several queries.
        score += 40.0 * sum(candidates[key].contributions)
        scores[key] = score

    ordered = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    return [
        MemoryCitation(
            memory_ref=candidates[key].hit.memory_ref,
            entry_id=candidates[key].hit.entry_id,
            entry_version_id=candidates[key].hit.entry_version_id,
        )
        for key in ordered
    ]


def _query_variants(question: str) -> tuple[tuple[str, float], ...]:
    text = " ".join(question.split())
    if not text:
        return ()
    tokens = _tokenize(text)
    content = [token for token in tokens if token not in _STOP_WORDS and len(token) > 1]
    numbers = _unique(token for token in content if token.isdigit())
    words = _unique(token for token in content if not token.isdigit())
    variants: list[tuple[str, float]] = [(text, 1.0)]
    if words:
        variants.append((" ".join(words[:_MAX_KEYWORD_TERMS]), 0.8))
    if numbers:
        variants.append((" ".join(numbers[:4]), 0.8))
    if numbers and words:
        variants.append((" ".join(numbers[:2] + words[:3]), 0.8))
    for word in sorted(words, key=lambda token: (-len(token), words.index(token)))[:_MAX_SINGLE_TERM_QUERIES]:
        variants.append((word, 0.6))
    for word in words:
        expansion = _expansion_for(word)
        if expansion:
            variants.append((" ".join(expansion), 0.5))
    unique: dict[str, float] = {}
    for query, weight in variants:
        if query:
            unique[query] = max(unique.get(query, 0.0), weight)
    return tuple(unique.items())


def _expansion_for(word: str) -> tuple[str, ...]:
    for prefix, expansion in _EXPANSIONS.items():
        if word.startswith(prefix):
            return expansion
    return ()


def _scoring_terms(question: str) -> tuple[tuple[str, float], ...]:
    """Question terms at full weight plus generic 10-K wording at reduced weight."""

    terms: dict[str, float] = {}
    for token in _tokenize(question):
        if token in _STOP_WORDS:
            continue
        terms[token] = max(terms.get(token, 0.0), 2.0 if token.isdigit() else 1.0)
    for token in tuple(terms):
        for expansion in _expansion_for(token):
            for part in _tokenize(expansion):
                if part not in _STOP_WORDS:
                    terms.setdefault(part, 0.75)
    return tuple(terms.items())


def _body(text: str) -> str:
    """Drop the transport document-id line before scoring page content."""

    return text.split("\n", 1)[1] if "\n" in text else text


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _unique(values) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_DEFINITION_VERSION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]

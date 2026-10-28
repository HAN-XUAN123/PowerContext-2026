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
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

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
_MAX_SINGLE_TERM_QUERIES = 20
_MAX_EXPANSION_QUERIES = 40
_MAX_VARIANTS = 75
_MAX_KEYWORD_TERMS = 12
_PAGE_INDEX_CACHE: dict[tuple[int, str, int], tuple[_Page, ...]] = {}
_PAGE_INDEX_CACHE_LIMIT = 2

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
# Multi-word entries are table labels: the query generator searches their distinctive
# terms and the ranker rewards a page that contains the whole label in order.
_EXPANSIONS: dict[str, tuple[str, ...]] = {
    # Balance sheet, liquidity and leverage.
    "quick": (
        "current assets",
        "current liabilities",
        "cash and cash equivalents",
        "accounts receivable",
        "inventories",
    ),
    "liquid": ("cash and cash equivalents", "current assets", "current liabilities", "working capital"),
    "ratio": ("current assets", "current liabilities", "total assets", "total liabilities"),
    "solven": ("total liabilities", "total assets", "stockholders equity", "long-term debt"),
    "liabil": ("total liabilities", "current liabilities", "customer deposits", "accounts payable", "long-term debt"),
    "asset": ("total assets", "current assets", "property and equipment", "goodwill", "intangible"),
    "balance": ("balance sheets", "total assets", "total liabilities"),
    # Income statement, margins and profitability.
    "margin": (
        "total revenues",
        "cost of products",
        "cost of services",
        "cost of sales",
        "operating income",
        "gross profit",
        "income from operations",
    ),
    "gross": ("gross profit", "total revenues", "cost of sales", "cost of products"),
    "operating": (
        "operating income",
        "loss from operations",
        "income from operations",
        "total revenues",
        "total expenses",
    ),
    "profit": ("net income", "gross profit", "operating income", "income before income taxes"),
    "ebitda": ("operating income", "depreciation", "amortization"),
    "expense": ("total expenses", "cost of products", "selling general and administrative"),
    "cost": ("cost of products", "cost of services", "cost of sales", "total costs and expenses"),
    # Taxes.
    "tax": (
        "income tax provision",
        "provision for income taxes",
        "effective tax rate",
        "income before income taxes",
        "deferred tax",
    ),
    "effective": ("effective tax rate", "income tax provision"),
    # Revenue and concentration.
    "revenue": ("total revenues", "net revenue", "net sales", "sales of products", "sales of services"),
    "sales": ("net sales", "total revenues", "sales of products", "sales of services"),
    "concentrat": ("accounted for", "net revenue", "major customer", "percent of"),
    "customer": ("accounted for", "net revenue", "major customer", "credit risk"),
    "product": ("sales of products", "products", "services", "total revenues"),
    "service": ("sales of services", "services", "total revenues"),
    # Segments and geography.
    "segment": ("reportable segments", "segment revenues", "geographic", "commercial airplanes", "global services"),
    "geograph": (
        "geographic",
        "regions",
        "north america",
        "europe",
        "asia pacific",
        "africa",
        "middle east",
        "latin america",
    ),
    "region": ("regions", "north america", "europe", "asia pacific", "africa", "middle east"),
    "countr": ("countries", "international", "foreign"),
    # Cash flow and capital spending.
    "cash": (
        "cash and cash equivalents",
        "cash flows",
        "operating activities",
        "investing activities",
        "financing activities",
    ),
    "capex": ("purchases of property", "plant and equipment", "capital expenditures"),
    "invest": ("investing activities", "capital expenditures", "purchases of property"),
    "financ": ("financing activities", "long-term debt", "repayments", "proceeds"),
    # Debt, equity and distributions.
    "debt": ("long-term debt", "commercial paper", "borrowings", "notes payable", "total debt"),
    "borrow": ("long-term debt", "commercial paper", "borrowings"),
    "dividend": ("dividends declared", "dividends per share", "share repurchase"),
    "repurchas": ("share repurchase", "treasury stock", "repurchases of common stock"),
    "equity": ("stockholders equity", "shareholders equity", "retained earnings"),
    "share": ("shares outstanding", "earnings per share", "share repurchase"),
    # Remaining recurring 10-K topics.
    "pension": ("pension", "retirement", "benefits"),
    "goodwill": ("goodwill", "impairment", "intangible"),
    "lease": ("operating lease", "right-of-use", "lease liabilities"),
    "inventor": ("inventories", "inventory"),
    "receivab": ("accounts receivable", "receivables"),
    "employee": ("employees", "headcount"),
    "legal": ("legal proceedings", "legal proceeding", "contingencies", "litigation", "claims"),
    "litigation": ("legal proceedings", "legal proceeding", "contingencies", "litigation"),
    "battle": ("legal proceedings", "legal proceeding", "litigation", "claims", "contingencies"),
    "dispute": ("legal proceedings", "legal proceeding", "litigation", "claims"),
    "claim": ("claims", "legal proceedings", "litigation"),
    "retain": ("retention", "retained", "renewal", "card member"),
    "renew": ("retention", "renewal"),
    "statement": ("statements of operations", "statements of income", "statements of cash flows", "balance sheets"),
    "sheet": ("balance sheets",),
    "flow": ("cash flows", "operating activities", "investing activities", "financing activities"),
    "operation": ("statements of operations", "income from operations", "operating income"),
    "regulat": ("regulatory", "compliance", "regulations"),
    "risk": ("risk factors", "risks", "uncertainties"),
    "acquisition": ("business combinations", "acquisitions", "purchase price"),
    "restructur": ("restructuring", "charges", "severance"),
    "impair": ("impairment", "goodwill", "intangible"),
    "contract": ("contract liabilities", "remaining performance obligations", "backlog"),
    "backlog": ("backlog", "remaining performance obligations"),
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
    """Return up to ``limit`` citations to distinct pages that may answer ``question``.

    Financial questions are answered by statement tables and note sections whose own
    labels ("total liabilities", "cost of products") rarely reuse the question's
    wording, and a per-query top-k full-text search truncates exactly those pages.
    The scope's current page entries are therefore ranked directly through the
    public Memory listing API, still returning only real stored entry versions.
    """

    if limit < 1:
        return ()
    pages = await _pdf_page_index(memory)
    if pages:
        return tuple(_select_pages(pages, question, limit))
    return await _search_pdf_pages(memory, question, limit)


@dataclass(slots=True)
class _Page:
    """One stored page entry prepared for lexical ranking."""

    record: Any
    location: tuple[str, int]
    tokens: list[str]
    term_set: set[str]
    counts: Counter[str]
    length: int


async def _pdf_page_index(memory: Any) -> tuple[_Page, ...]:
    """List the scope's current page entries, cached per exact Memory revision."""

    lister = getattr(memory, "list", None)
    if lister is None:
        return ()
    try:
        listing = await lister()
    except (AttributeError, TypeError):
        return ()
    memory_ref = getattr(listing, "memory_ref", None)
    if memory_ref is None:
        return ()
    key = (id(memory), memory_ref.artifact_id, memory_ref.revision)
    cached = _PAGE_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    pages: list[_Page] = []
    seen: set[tuple[str, int]] = set()
    for record in getattr(listing, "entries", ()):
        entry = getattr(record, "entry", None)
        if entry is None or getattr(record, "state", "active") != "active":
            continue
        location = None
        for reference in entry.sources:
            if getattr(reference, "source_type", PDF_PAGE_SOURCE_NAME) != PDF_PAGE_SOURCE_NAME:
                continue
            location = _page_location(reference.source_id)
            if location is not None:
                break
        if location is None or location in seen:
            continue
        seen.add(location)
        tokens = _tokenize(_body(entry.text))
        pages.append(
            _Page(
                record=record,
                location=location,
                tokens=tokens,
                term_set=set(tokens),
                counts=Counter(tokens),
                length=max(1, len(tokens)),
            )
        )
    index = tuple(pages)
    if len(_PAGE_INDEX_CACHE) >= _PAGE_INDEX_CACHE_LIMIT:
        _PAGE_INDEX_CACHE.clear()
    _PAGE_INDEX_CACHE[key] = index
    return index


def _page_location(source_id: str) -> tuple[str, int] | None:
    parts = source_id.split(":")
    if len(parts) < 4 or parts[0] != "pdf":
        return None
    token = parts[1]
    if token.startswith("q"):
        document = unquote(token[1:])
    elif token.startswith("h"):
        document = token
    else:
        return None
    try:
        page_number = int(parts[3])
    except ValueError:
        return None
    return document, page_number


def _select_pages(pages: tuple[_Page, ...], question: str, limit: int) -> list[MemoryCitation]:
    """Rank every stored page with idf-weighted table-label and content matching."""

    terms: dict[str, float] = {}
    for token in _tokenize(question):
        if token in _STOP_WORDS or len(token) < 2:
            continue
        terms[token] = 0.8 if token.isdigit() else 1.0
    for word in tuple(term for term in terms if not term.isdigit()):
        for expansion in _expansion_for(word):
            for part in _tokenize(expansion):
                if part not in _STOP_WORDS and len(part) > 1:
                    terms.setdefault(part, 0.55)
    if not terms:
        return []

    total = len(pages)
    frequencies = dict.fromkeys(terms, 0)
    for page in pages:
        present = page.term_set
        for term in terms:
            if term in present:
                frequencies[term] += 1
    weights: dict[str, float] = {}
    for term, weight in terms.items():
        document_frequency = frequencies[term]
        if not document_frequency or document_frequency >= total * 0.9:
            continue
        weights[term] = weight * math.log(
            1.0 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
        )
    phrases = _scoring_phrases(question)

    scores: dict[int, float] = {}
    for index, page in enumerate(pages):
        counts = page.counts
        length = page.length
        score = 0.0
        matched = 0
        for term, weight in weights.items():
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            matched += 1
            score += weight * (frequency * 2.2) / (
                frequency + 1.2 * (0.25 + 0.75 * length / 600.0)
            )
        for phrase in phrases:
            if all(part in page.term_set for part in phrase) and _contains_sequence(page.tokens, phrase):
                # The filing's own table label is a strong signal even when the
                # question never uses those words.
                score += 4.0
        score += 0.6 * matched
        scores[index] = score

    best_by_document: dict[str, float] = {}
    for index, page in enumerate(pages):
        document = page.location[0]
        best_by_document[document] = max(best_by_document.get(document, 0.0), scores[index])
    strongest = max(best_by_document.values()) or 1.0
    for index, page in enumerate(pages):
        scores[index] *= 0.7 + 0.3 * (best_by_document[page.location[0]] / strongest)

    ordered = sorted(scores, key=lambda index: (-scores[index], pages[index].location))[:limit]
    return [
        MemoryCitation(
            memory_ref=pages[index].record.memory_ref,
            entry_id=pages[index].record.entry.entry_id,
            entry_version_id=pages[index].record.entry.entry_version_id,
        )
        for index in ordered
    ]


async def _search_pdf_pages(memory: Any, question: str, limit: int) -> tuple[MemoryCitation, ...]:
    """Fallback retrieval through backend search when the listing API is unavailable."""

    variants = _query_variants(question)
    if not variants:
        return ()

    from powercontext.builtin.runtime import SearchMemoryRequest

    per_query = max(120, min(320, limit * 16))
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
    question_terms: dict[str, float] = {}
    for term, term_weight in _scoring_terms(question):
        if term in weights:
            continue
        df = frequencies.get(term, 0)
        if not df or df >= total * 0.6:
            continue
        weights[term] = term_weight * math.log(1.0 + (total - df + 0.5) / (df + 0.5))
        if term_weight >= 1.0:
            question_terms[term] = weights[term]
    phrases = _scoring_phrases(question)

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
        if phrases:
            present = set(tokens)
            for phrase in phrases:
                if all(part in present for part in phrase) and _contains_sequence(tokens, phrase):
                    # A filing's own table label ("total liabilities", "cost of products")
                    # is a strong signal even when the question never uses those words.
                    score += 2.5
        if question_terms:
            covered = sum(1 for term in question_terms if term in counts)
            score += 0.8 * covered
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
    for word in words[:_MAX_SINGLE_TERM_QUERIES]:
        variants.append((word, 0.6))
    expansion_queries: list[str] = []
    for word in words:
        for expansion in _expansion_for(word):
            expansion_queries.append(expansion)
            for part in _tokenize(expansion):
                if part not in _STOP_WORDS and len(part) > 1:
                    expansion_queries.append(part)
    for query in _unique(expansion_queries)[:_MAX_EXPANSION_QUERIES]:
        variants.append((query, 0.7))
    unique: dict[str, float] = {}
    for query, weight in variants:
        if query:
            unique[query] = max(unique.get(query, 0.0), weight)
    return tuple(list(unique.items())[:_MAX_VARIANTS])


def _expansion_for(word: str) -> tuple[str, ...]:
    matches: list[str] = []
    for prefix, expansion in _EXPANSIONS.items():
        if word.startswith(prefix):
            matches.extend(expansion)
    return tuple(dict.fromkeys(matches))


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


def _scoring_phrases(question: str) -> tuple[tuple[str, ...], ...]:
    """Table labels suggested by the question's concepts, as adjacent token runs."""

    phrases: dict[tuple[str, ...], None] = {}
    for word in _unique(token for token in _tokenize(question) if token not in _STOP_WORDS and len(token) > 1):
        for expansion in _expansion_for(word):
            parts = tuple(_tokenize(expansion))
            if len(parts) > 1:
                phrases.setdefault(parts, None)
    return tuple(phrases)


def _contains_sequence(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    for start in range(len(tokens) - width + 1):
        if tuple(tokens[start : start + width]) == phrase:
            return True
    return False


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

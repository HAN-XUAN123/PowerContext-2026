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

"""Row-level CSV Sources whose citations resolve to one complete original record."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_ROW_SOURCE_DEFINITION_VERSION = "1"


class CSVDocumentInput(BaseModel):
    """A caller-supplied offline CSV document to capture row by row."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One parsed CSV record bound to its document and primary key."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def require_aligned_columns(self) -> CSVRowInput:
        if len(self.columns) != len(self.values):
            raise ValueError("columns and values must have the same length")  # noqa: TRY003
        return self


class CSVRowSource(Source):
    """Captured Source for one CSV record that survived parsing validation."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def require_aligned_columns(self) -> CSVRowSource:
        if len(self.columns) != len(self.values):
            raise ValueError("columns and values must have the same length")  # noqa: TRY003
        return self


def parse_csv_document(value: CSVDocumentInput, /) -> tuple[CSVRowInput, ...]:
    """Parse and fully validate one CSV document before returning any row."""

    document_id = _require_document_id(value.document_id)
    primary_key = value.primary_key
    if not primary_key:
        raise ValueError("primary_key must not be empty")  # noqa: TRY003

    try:
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV document must be valid UTF-8") from error

    try:
        with io.StringIO(text, newline="") as stream:
            records = list(
                csv.reader(
                    stream,
                    delimiter=",",
                    quotechar='"',
                    doublequote=True,
                    strict=True,
                )
            )
    except csv.Error as error:
        raise ValueError("CSV document contains malformed quoting") from error

    if not records:
        raise ValueError("CSV document must start with a non-empty header record")  # noqa: TRY003
    header = tuple(records[0])
    if not header:
        raise ValueError("CSV header must not be empty")  # noqa: TRY003
    for column in header:
        if not column or not column.strip():
            raise ValueError("CSV header fields must be non-empty and non-blank")  # noqa: TRY003
    if len(set(header)) != len(header):
        raise ValueError("CSV header fields must be unique")  # noqa: TRY003
    if primary_key not in header:
        raise ValueError("primary_key must match a header field exactly")  # noqa: TRY003
    key_index = header.index(primary_key)

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records[1:]:
        if not record:
            continue
        if len(record) != len(header):
            raise ValueError("CSV record width must match the header width")  # noqa: TRY003
        key_value = record[key_index]
        if not key_value or not key_value.strip():
            raise ValueError("primary key values must be non-empty and non-blank")  # noqa: TRY003
        if key_value in seen_keys:
            raise ValueError("primary key values must be unique within a document")  # noqa: TRY003
        seen_keys.add(key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=key_value,
                columns=header,
                values=tuple(record),
            )
        )
    return tuple(rows)


def csv_row_source_id(document_id: str, primary_key_value: str, /) -> str:
    """Return the stable identity of one row, delimiter-safe and length-bounded."""

    identity = f"csv:{quote(_require_document_id(document_id), safe='')}:{quote(primary_key_value, safe='')}"
    if not primary_key_value or not primary_key_value.strip():
        raise ValueError("primary key values must be non-empty and non-blank")  # noqa: TRY003
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


class CSVRowSourceAdapter:
    """Resolve one parsed row into a captured Source and read it back."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one CSV row as canonical text evidence without losing column order."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_SOURCE_DEFINITION_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _require_document_id(document_id: str) -> str:
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")  # noqa: TRY003
    return document_id


__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_DEFINITION_VERSION",
    "CSV_ROW_SOURCE_NAME",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceAdapter",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]

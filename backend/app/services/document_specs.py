"""Typed JSON specifications for generated DOCX and XLSX files.

The agent may still send Markdown; these models are the deterministic path for
complex documents. Validation happens before any bytes are written, so a bad
spec returns a precise error instead of a half-created file.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class DocumentSpecError(ValueError):
    """A JSON document specification is structurally invalid."""


class TableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header: list[str] = Field(min_length=1)
    rows: list[list[Any]] = Field(default_factory=list)
    caption: str | None = Field(default=None, max_length=500)

    @field_validator("rows")
    @classmethod
    def _rows_have_cells(cls, rows: list[list[Any]]) -> list[list[Any]]:
        if any(not isinstance(row, list) for row in rows):
            raise ValueError("each table row must be a list")
        return rows


class ImageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: str | None = None
    base64: str | None = None
    width_mm: int | None = Field(default=None, ge=10, le=2000)
    caption: str | None = Field(default=None, max_length=500)
    alt: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _has_data(self) -> "ImageSpec":
        if not (self.data or self.base64):
            raise ValueError("image requires data/base64")
        return self


class DocxBlockSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["heading", "paragraph", "table", "list", "code", "quote", "pagebreak", "image"] = "paragraph"
    text: str | None = None
    level: int = Field(default=1, ge=1, le=9)
    ordered: bool = False
    items: list[str] = Field(default_factory=list)
    header: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    data: str | None = None
    base64: str | None = None
    width_mm: int | None = Field(default=None, ge=10, le=2000)
    caption: str | None = Field(default=None, max_length=500)
    alt: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _required_fields(self) -> "DocxBlockSpec":
        if self.type in {"heading", "paragraph", "code", "quote"} and not (self.text or "").strip():
            raise ValueError(f"{self.type} requires text")
        if self.type == "table" and not self.header:
            raise ValueError("table requires header")
        if self.type == "list" and not self.items:
            raise ValueError("list requires items")
        if self.type == "image" and not (self.data or self.base64):
            raise ValueError("image requires data/base64")
        return self

    def as_block(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class DocxSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    blocks: list[DocxBlockSpec] = Field(min_length=1)


class XlsxSheetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="Sheet1", min_length=1, max_length=31)
    rows: list[list[Any]] = Field(default_factory=list)
    header: bool = True
    freeze: str | None = None
    formats: dict[str, str] = Field(default_factory=dict)
    merges: list[str] = Field(default_factory=list)
    widths: dict[str, float | int] = Field(default_factory=dict)
    styles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    auto_filter: bool = True
    header_fill: str = "DDEBF7"

    @field_validator("rows")
    @classmethod
    def _rows_are_lists(cls, rows: list[list[Any]]) -> list[list[Any]]:
        if any(not isinstance(row, list) for row in rows):
            raise ValueError("each row must be a list")
        return rows


class XlsxSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheets: list[XlsxSheetSpec] = Field(min_length=1)


def _format_error(kind: str, exc: ValidationError) -> DocumentSpecError:
    errors: list[str] = []
    for item in exc.errors()[:6]:
        path = ".".join(str(part) for part in item.get("loc") or ())
        message = str(item.get("msg") or "invalid value")
        errors.append(f"{path or '<root>'}: {message}")
    return DocumentSpecError(f"Invalid {kind.upper()} specification: " + "; ".join(errors))


def parse_docx_text(text: str) -> DocxSpec | None:
    """Parse a DOCX JSON spec, or return ``None`` for Markdown content."""
    stripped = (text or "").lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError) as exc:
        raise DocumentSpecError(f"Invalid DOCX JSON: {exc}") from exc
    if not isinstance(payload, dict) or "blocks" not in payload:
        raise DocumentSpecError("DOCX JSON must contain a blocks array")
    try:
        return DocxSpec.model_validate(payload)
    except ValidationError as exc:
        raise _format_error("DOCX", exc) from exc


def parse_xlsx_text(text: str) -> XlsxSpec | None:
    """Parse an XLSX JSON spec, or return ``None`` for tabular text."""
    stripped = (text or "").lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError) as exc:
        raise DocumentSpecError(f"Invalid XLSX JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DocumentSpecError("XLSX JSON must be an object")
    if isinstance(payload.get("sheets"), list):
        try:
            return XlsxSpec.model_validate(payload)
        except ValidationError as exc:
            raise _format_error("XLSX", exc) from exc
    # Accept the convenient shorthand {"Sheet1": [[...], ...]}.
    if payload and all(isinstance(value, list) for value in payload.values()):
        try:
            return XlsxSpec.model_validate(
                {"sheets": [{"name": name, "rows": rows} for name, rows in payload.items()]}
            )
        except ValidationError as exc:
            raise _format_error("XLSX", exc) from exc
    raise DocumentSpecError("XLSX JSON must contain sheets[] or a sheet-name mapping")


def docx_json_schema() -> dict[str, Any]:
    return DocxSpec.model_json_schema()


def xlsx_json_schema() -> dict[str, Any]:
    return XlsxSpec.model_json_schema()

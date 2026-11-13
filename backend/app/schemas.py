"""Pydantic request/response schemas for the REST API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------- nodes
class NodeOut(ORMModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    owner: str | None = None
    name: str
    kind: Literal["project", "folder", "file"]
    mime: str | None = None
    size: int = 0
    index_status: str = "pending"
    index_error: str | None = None
    chunk_count: int = 0
    indexed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    view_kind: str = "binary"
    extension: str = ""
    created_by: str = "user"
    children: list["NodeOut"] = Field(default_factory=list)


class CreateProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CreateFolderIn(BaseModel):
    parent_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)


class UpdateNodeIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: uuid.UUID | None = None


class UploadItem(BaseModel):
    node: NodeOut
    replaced: bool = False


class UploadResult(BaseModel):
    items: list[UploadItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ExtractResult(BaseModel):
    created: list[NodeOut] = Field(default_factory=list)
    count: int = 0


class VersionOut(ORMModel):
    id: uuid.UUID
    version_no: int
    size: int
    created_by: str
    note: str | None = None
    created_at: datetime


class TextPreviewOut(BaseModel):
    node_id: uuid.UUID
    name: str
    view_kind: str
    text: str
    segments: list[dict[str, str]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False


# -------------------------------------------------------------------- chat
class SessionCreateIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    project_id: uuid.UUID | None = None
    provider: str | None = None
    model: str | None = None


class SessionOut(ORMModel):
    id: uuid.UUID
    title: str
    provider: str | None = None
    model: str | None = None
    project_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class MessageOut(ORMModel):
    id: uuid.UUID
    role: str
    content: str
    provider: str | None = None
    model: str | None = None
    tool_calls: list[Any] | None = None
    citations: list[Any] | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime


class SendMessageIn(BaseModel):
    content: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    attachments: list[uuid.UUID] = Field(default_factory=list)
    use_rag: bool = True


class OperationOut(ORMModel):
    id: uuid.UUID
    tool_name: str
    description: str
    node_id: uuid.UUID | None = None
    reverted: bool
    created_at: datetime
    reverted_at: datetime | None = None


# ---------------------------------------------------------- providers/settings
class ProviderTestOut(BaseModel):
    provider: str
    ok: bool
    message: str
    latency_ms: float = 0.0
    models: list[str] = Field(default_factory=list)


class RagQueryIn(BaseModel):
    query: str = Field(min_length=1)
    project_id: uuid.UUID | None = None
    node_ids: list[uuid.UUID] = Field(default_factory=list)
    top_k: int = Field(default=6, ge=1, le=20)


class RagQueryOut(BaseModel):
    citations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class HealthOut(BaseModel):
    status: str
    version: str
    checks: dict[str, Any] = Field(default_factory=dict)
    providers: dict[str, Any] = Field(default_factory=dict)
    banner: str | None = None
    warnings: list[str] = Field(default_factory=list)


# -------------------------------------------------------------------- auth
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class UserStatsOut(BaseModel):
    projects: int = 0
    files: int = 0
    sessions: int = 0
    chunks: int = 0


class ModelConnectionOut(BaseModel):
    name: str
    base_url: str
    key_masked: str = ""
    has_key: bool = False
    model: str


class ModelConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=128)
    # Empty/missing key keeps the stored one; clear_key removes it.
    api_key: str | None = Field(default=None, max_length=500)
    clear_key: bool = False


class UserOut(ORMModel):
    id: uuid.UUID
    username: str
    display_name: str = ""
    role: Literal["admin", "user"]
    is_admin: bool = False
    is_active: bool = True
    connections: list[ModelConnectionOut] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    stats: UserStatsOut | None = None


class LoginOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=200)
    display_name: str = Field(default="", max_length=120)
    role: Literal["admin", "user"] = "user"
    connections: list[ModelConnectionIn] | None = None


class UserUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    password: str | None = Field(default=None, min_length=6, max_length=200)
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None
    connections: list[ModelConnectionIn] | None = None



class AdminProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID | None = None
    owner: str | None = None
    files: int = 0
    chunks: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


NodeOut.model_rebuild()


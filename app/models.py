"""ORM models for batches, documents and extracted findings."""

import enum
import json
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    types,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class JSONType(types.TypeDecorator):
    """Store a Python list/dict as JSON text in SQLite."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return json.dumps(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return json.loads(value) if value is not None else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BatchStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    partial = "partial"
    failed = "failed"


class DocumentStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class FindingStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    variables: Mapped[list[str]] = mapped_column(JSONType)
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus), default=BatchStatus.queued, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    documents: Mapped[list["Document"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # Derived counters (computed on read from documents; kept for quick access)
    total_documents: Mapped[int] = mapped_column(Integer, default=0)
    completed_documents: Mapped[int] = mapped_column(Integer, default=0)
    failed_documents: Mapped[int] = mapped_column(Integer, default=0)
    pending_findings: Mapped[int] = mapped_column(Integer, default=0)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus), default=DocumentStatus.queued, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    batch: Mapped["Batch"] = relationship(back_populates="documents")
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    variable_name: Mapped[str] = mapped_column(String(255), index=True)
    extracted_value: Mapped[str] = mapped_column(Text)
    status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus), default=FindingStatus.pending, index=True
    )
    reviewer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped["Document"] = relationship(back_populates="findings")


def derive_batch_status(
    total: int, completed: int, failed: int
) -> BatchStatus:
    """Derive batch-level status purely from document counters.

    Using a derived (computed) status instead of a stored one guarantees the
    batch view always agrees with the individual documents.
    """
    if total == 0:
        return BatchStatus.failed
    done = completed + failed
    if done == 0:
        return BatchStatus.queued
    if done < total:
        return BatchStatus.processing
    if failed == 0:
        return BatchStatus.completed
    if completed == 0:
        return BatchStatus.failed
    return BatchStatus.partial

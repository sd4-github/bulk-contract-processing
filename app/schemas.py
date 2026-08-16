"""Pydantic request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import BatchStatus, DocumentStatus, FindingStatus


class ReviewDecision(BaseModel):
    decision: FindingStatus  # accepted | rejected
    reviewer: str = "reviewer"


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    variable_name: str
    extracted_value: str
    status: FindingStatus
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    size_bytes: int
    status: DocumentStatus
    error_message: str | None = None
    retry_count: int
    processed_at: datetime | None = None
    findings: list[FindingOut] = []


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    variables: list[str]
    status: BatchStatus
    created_at: datetime
    updated_at: datetime
    total_documents: int
    completed_documents: int
    failed_documents: int
    pending_findings: int


class BatchListOut(BaseModel):
    id: int
    name: str
    status: BatchStatus
    created_at: datetime
    total_documents: int
    completed_documents: int
    failed_documents: int
    pending_findings: int


class BatchDetailOut(BatchOut):
    documents: list[DocumentOut] = []


class UploadResponse(BaseModel):
    batch_id: int
    name: str
    status: BatchStatus
    total_documents: int
    variables: list[str]
    message: str = Field(
        default="Batch accepted. Processing is running in the background."
    )


class HealthOut(BaseModel):
    status: str
    worker_running: bool
    queued_batches: int

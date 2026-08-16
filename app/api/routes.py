"""HTTP API: upload, batch tracking, findings review, health."""

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import (
    Batch,
    BatchStatus,
    Document,
    Finding,
    FindingStatus,
    utcnow,
)
from ..schemas import (
    BatchDetailOut,
    BatchListOut,
    DocumentOut,
    FindingOut,
    HealthOut,
    ReviewDecision,
    UploadResponse,
)
from ..services.batch_service import ZipValidationError, create_batch
from ..services.worker import worker

router = APIRouter()


def _serialize_variables(raw: str) -> list[str]:
    try:
        variables = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail="variables must be a JSON array of strings, e.g. "
            '["effective_date", "governing_law"]',
        ) from exc
    if not isinstance(variables, list) or not variables:
        raise HTTPException(
            status_code=422, detail="variables must be a non-empty JSON array"
        )
    if not all(isinstance(v, str) and v.strip() for v in variables):
        raise HTTPException(
            status_code=422,
            detail="each variable must be a non-empty string",
        )
    return [v.strip() for v in variables]


@router.get("/health", response_model=HealthOut, tags=["system"])
def health(db: Session = Depends(get_db)) -> HealthOut:
    queued = (
        db.query(Batch)
        .filter(Batch.status.in_([BatchStatus.queued, BatchStatus.processing]))
        .count()
    )
    return HealthOut(
        status="ok",
        worker_running=worker.running,
        queued_batches=queued,
    )


@router.post(
    "/batches",
    response_model=UploadResponse,
    status_code=202,
    tags=["batches"],
)
async def upload_batch(
    zip_file: UploadFile = File(...),
    variables: str = Form(...),
    name: str = Form(""),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Accept a ZIP of contract documents plus the variables to extract.

    Returns immediately with a ``batch_id``; processing happens in the
    background worker.
    """
    parsed_variables = _serialize_variables(variables)

    payload = await zip_file.read()
    max_zip_bytes = settings.max_zip_size_mb * 1024 * 1024
    if len(payload) > max_zip_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"ZIP exceeds the {settings.max_zip_size_mb} MB limit",
        )

    try:
        batch = create_batch(
            db, name=name, variables=parsed_variables, zip_bytes=payload
        )
    except ZipValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not worker.running:
        worker.start()

    return UploadResponse(
        batch_id=batch.id,
        name=batch.name,
        status=batch.status,
        total_documents=batch.total_documents,
        variables=parsed_variables,
    )


@router.get("/batches", response_model=list[BatchListOut], tags=["batches"])
def list_batches(
    status: BatchStatus | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[BatchListOut]:
    q = db.query(Batch).order_by(Batch.id.desc())
    if status is not None:
        q = q.filter(Batch.status == status)
    return q.limit(limit).all()


@router.get("/batches/{batch_id}", response_model=BatchDetailOut, tags=["batches"])
def get_batch(batch_id: int, db: Session = Depends(get_db)) -> BatchDetailOut:
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@router.get(
    "/batches/{batch_id}/documents/{document_id}",
    response_model=DocumentOut,
    tags=["documents"],
)
def get_document(
    batch_id: int, document_id: int, db: Session = Depends(get_db)
) -> DocumentOut:
    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.batch_id == batch_id)
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.get("/findings", response_model=list[FindingOut], tags=["review"])
def list_findings(
    batch_id: int | None = Query(None),
    status: FindingStatus | None = Query(FindingStatus.pending),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[Finding]:
    """List findings, optionally filtered by batch and review status.

    Defaults to un-reviewed (pending) findings so reviewers see a work queue.
    """
    q = db.query(Finding)
    if status is not None:
        q = q.filter(Finding.status == status)
    if batch_id is not None:
        q = q.join(Document).filter(Document.batch_id == batch_id)
    return q.order_by(Finding.id.asc()).limit(limit).all()


@router.post(
    "/findings/{finding_id}/review",
    response_model=FindingOut,
    tags=["review"],
)
def review_finding(
    finding_id: int,
    decision: ReviewDecision,
    db: Session = Depends(get_db),
) -> Finding:
    """Accept or reject a single extracted finding."""
    if decision.decision not in (FindingStatus.accepted, FindingStatus.rejected):
        raise HTTPException(
            status_code=422,
            detail="decision must be 'accepted' or 'rejected'",
        )
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")

    finding.status = decision.decision
    finding.reviewer = decision.reviewer
    finding.reviewed_at = utcnow()
    db.commit()
    db.refresh(finding)

    from ..services.batch_service import refresh_batch_metrics

    refresh_batch_metrics(db, finding.document.batch_id)
    return finding
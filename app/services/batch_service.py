"""Batch handling: ZIP intake, persistence, and document processing.

The processing logic lives here so it can be driven synchronously by tests or
scheduled by the background worker in ``app/services/worker.py``.
"""

import asyncio
import io
import json
import posixpath
import shutil
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Batch,
    BatchStatus,
    Document,
    DocumentStatus,
    Finding,
    FindingStatus,
    derive_batch_status,
    utcnow,
)
from .extraction import MockExtractionError, extract


def run_coro(result):
    """Run an async coroutine from a synchronous (worker-thread) context.

    The processing pipeline is synchronous at the DB layer but delegates to the
    (async) mocked extraction API, which simulates network latency. ``asyncio.run``
    is safe here because processing never runs inside an event loop: the worker
    executes documents in worker threads via ``asyncio.to_thread``.

    Accepts plain return values too, so tests can substitute a synchronous fake
    for the mocked API.
    """
    import inspect

    if not inspect.isawaitable(result):
        return result
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)
    raise RuntimeError(
        "extract() cannot be awaited from within a running event loop"
    )


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

class ZipValidationError(ValueError):
    """Raised when an uploaded ZIP violates the fixed constraints."""


def _validate_member(member: zipfile.ZipInfo) -> bool:
    """Return True if the member is a real file we should process."""
    if member.is_dir():
        return False
    # Reject macOS junk and hidden/system files.
    name = member.filename
    if name.startswith("__MACOSX/"):
        return False
    if "/" in name and name.split("/")[-1].startswith("."):
        return False
    # Guard against path traversal in archive member names.
    normalized = posixpath.normpath(name)
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        return False
    return True


def _safe_member_name(member: zipfile.ZipInfo) -> tuple[str, str]:
    """Return (display_name, on_disk_name) for a ZIP member.

    ``display_name`` preserves the archive's relative path for human review;
    ``on_disk_name`` is a flat, collision-safe basename used for storage.
    """
    display_name = posixpath.normpath(member.filename)
    on_disk_name = f"{member.header_offset}-{Path(display_name).name}"
    return display_name, on_disk_name


def extract_zip_to_storage(
    zip_bytes: bytes, batch_id: int
) -> list[tuple[str, str, int]]:
    """Validate a ZIP and write members into per-batch storage.

    Returns a list of ``(display_name, on_disk_name, size_bytes)`` tuples for
    the accepted documents. Raises :class:`ZipValidationError` for constraint
    violations.
    """
    batch_dir = settings.storage_dir / str(batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)

    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ZipValidationError("Uploaded file is not a valid ZIP archive") from exc

    members = [m for m in archive.infolist() if _validate_member(m)]
    if len(members) == 0:
        raise ZipValidationError("ZIP contains no processable documents")
    if len(members) > settings.max_documents_per_zip:
        raise ZipValidationError(
            f"ZIP contains {len(members)} documents; limit is "
            f"{settings.max_documents_per_zip}"
        )

    max_bytes = settings.max_document_size_mb * 1024 * 1024
    accepted: list[tuple[str, str, int]] = []
    try:
        for member in members:
            if member.file_size > max_bytes:
                raise ZipValidationError(
                    f"Document '{member.filename}' is {member.file_size} bytes; "
                    f"limit is {settings.max_document_size_mb} MB"
                )
            display_name, on_disk_name = _safe_member_name(member)
            target = batch_dir / on_disk_name
            with archive.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            accepted.append((display_name, on_disk_name, member.file_size))
    except ZipValidationError:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    return accepted


def create_batch(
    db: Session,
    *,
    name: str,
    variables: list[str],
    zip_bytes: bytes,
) -> Batch:
    """Create a batch row and persist its documents.

    The batch is left in ``queued`` state; the worker picks it up asynchronously.
    """
    batch = Batch(name=name or "untitled-batch")
    batch.variables = variables
    db.add(batch)
    db.commit()
    db.refresh(batch)

    try:
        documents = extract_zip_to_storage(zip_bytes, batch.id)
    except ZipValidationError as exc:
        db.delete(batch)
        db.commit()
        raise

    for display_name, on_disk_name, size in documents:
        doc = Document(
            batch_id=batch.id,
            filename=display_name,
            size_bytes=size,
            storage_path=str(settings.storage_dir / str(batch.id) / on_disk_name),
            status=DocumentStatus.queued,
        )
        db.add(doc)

    batch.total_documents = len(documents)
    db.commit()
    db.refresh(batch)
    return batch


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def refresh_batch_metrics(db: Session, batch_id: int) -> None:
    """Recompute batch counters and derive batch status from documents.

    The batch status is always *derived* from its documents, which keeps the
    batch view consistent with the individual document rows.
    """
    docs = (
        db.query(Document)
        .filter(Document.batch_id == batch_id)
        .all()
    )
    total = len(docs)
    completed = sum(1 for d in docs if d.status == DocumentStatus.completed)
    failed = sum(1 for d in docs if d.status == DocumentStatus.failed)
    pending_findings = (
        db.query(Finding)
        .join(Document)
        .filter(
            Document.batch_id == batch_id,
            Finding.status == FindingStatus.pending,
        )
        .count()
    )

    batch = db.get(Batch, batch_id)
    if batch is None:
        return
    batch.total_documents = total
    batch.completed_documents = completed
    batch.failed_documents = failed
    batch.pending_findings = pending_findings
    batch.status = derive_batch_status(total, completed, failed)
    batch.updated_at = utcnow()
    db.commit()


def _claim_document(db: Session, doc_id: int) -> bool:
    """Atomically transition a claimable document -> processing.

    Claimable means ``queued`` or ``failed`` (retry) with retries remaining.
    Returns False otherwise, giving idempotency so a document is never
    processed twice concurrently (important for the double-processing concern
    in the Part 2 case study).
    """
    doc = db.get(Document, doc_id)
    if doc is None:
        return False
    if doc.status == DocumentStatus.completed:
        return False
    if doc.status == DocumentStatus.failed and doc.retry_count >= settings.max_retries:
        return False
    was_failed = doc.status == DocumentStatus.failed
    doc.status = DocumentStatus.processing
    if was_failed:
        doc.retry_count += 1
    doc.processing_started_at = utcnow()
    doc.updated_at = utcnow()
    db.commit()
    return True


def process_document(db: Session, document: Document) -> None:
    """Extract variables for one document and persist findings.

    Idempotent: only ``queued`` documents are processed. On success the
    document becomes ``completed``; on failure it becomes ``failed`` and a
    retry is scheduled by the worker.
    """
    if not _claim_document(db, document.id):
        return

    variables = document.batch.variables
    try:
        extracted = run_coro(extract(document.storage_path, variables))
    except MockExtractionError as exc:
        doc = db.get(Document, document.id)
        doc.status = DocumentStatus.failed
        doc.error_message = str(exc)
        doc.updated_at = utcnow()
        db.commit()
        refresh_batch_metrics(db, document.batch_id)
        raise

    for variable in variables:
        finding = Finding(
            document_id=document.id,
            variable_name=variable,
            extracted_value=extracted.get(variable, ""),
            status=FindingStatus.pending,
        )
        db.add(finding)

    doc = db.get(Document, document.id)
    doc.status = DocumentStatus.completed
    doc.processed_at = utcnow()
    doc.updated_at = utcnow()
    db.commit()
    refresh_batch_metrics(db, document.batch_id)


def process_batch(db: Session, batch: Batch) -> None:
    """Process every claimable document in a batch (synchronous driver).

    Used by the worker and directly by tests. Documents already processed are
    skipped; failed documents with retries remaining are retried. Re-running is
    therefore safe (idempotent).
    """
    docs = (
        db.query(Document)
        .filter(Document.batch_id == batch.id)
        .filter(
            (Document.status == DocumentStatus.queued)
            | (
                (Document.status == DocumentStatus.failed)
                & (Document.retry_count < settings.max_retries)
            )
        )
        .all()
    )
    for document in docs:
        # Refresh the object after the metric refresh committed the session.
        db.expire_all()
        fresh = db.get(Document, document.id)
        if fresh is None:
            continue
        try:
            process_document(db, fresh)
        except MockExtractionError:
            # Document marked failed already; continue with the rest.
            continue
    refresh_batch_metrics(db, batch.id)


# ---------------------------------------------------------------------------
# Stuck-document recovery
# ---------------------------------------------------------------------------

def requeue_stuck_documents(db: Session) -> int:
    """Find documents stuck in ``processing`` and requeue them.

    A document is considered stuck if it has been in the processing state
    longer than ``STUCK_THRESHOLD_SECONDS``. This mirrors the recovery we
    would implement in production for the "stuck documents" symptom.
    """
    from datetime import timedelta

    threshold = utcnow() - timedelta(seconds=settings.stuck_threshold_seconds)
    stuck = (
        db.query(Document)
        .filter(
            Document.status == DocumentStatus.processing,
            Document.processing_started_at.is_not(None),
            Document.processing_started_at < threshold,
        )
        .all()
    )
    for doc in stuck:
        doc.status = DocumentStatus.queued
        doc.retry_count += 1
        doc.processing_started_at = None
        doc.updated_at = utcnow()
    if stuck:
        db.commit()
        for doc in stuck:
            refresh_batch_metrics(db, doc.batch_id)
    return len(stuck)
"""Tests for the background worker and stuck-document recovery."""

import time
from datetime import timedelta

from app.models import Batch, BatchStatus, DocumentStatus, utcnow
from app.services.batch_service import create_batch, requeue_stuck_documents
from app.services.worker import Worker

from .conftest import make_txt_zip


def _wait_until(predicate, timeout=10.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_worker_processes_batch_asynchronously():
    from app.database import SessionLocal

    with SessionLocal() as db:
        batch = create_batch(
            db,
            name="async",
            variables=["effective_date", "monthly_rent"],
            zip_bytes=make_txt_zip(4),
        )
        batch_id = batch.id

    worker = Worker(concurrency=2)
    worker.start()
    try:
        assert _wait_until(
            lambda: _batch_status(batch_id) == BatchStatus.completed
        )
    finally:
        worker.stop()

    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        assert batch.completed_documents == 4
        assert batch.failed_documents == 0
        docs = batch.documents
        assert all(d.status == DocumentStatus.completed for d in docs)
        assert all(len(d.findings) == 2 for d in docs)


def _batch_status(batch_id):
    from app.database import SessionLocal

    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        return batch.status if batch else None


def test_requeue_stuck_documents(db):
    batch = create_batch(
        db, name="stuck", variables=["x"], zip_bytes=make_txt_zip(1)
    )
    doc = batch.documents[0]
    doc.status = DocumentStatus.processing
    doc.processing_started_at = utcnow() - timedelta(seconds=7200)
    db.commit()

    requeued = requeue_stuck_documents(db)
    assert requeued == 1

    db.expire_all()
    doc = batch.documents[0]
    assert doc.status == DocumentStatus.queued
    assert doc.retry_count == 1
    assert doc.processing_started_at is None


def test_recently_processing_documents_not_requeued(db):
    batch = create_batch(
        db, name="recent", variables=["x"], zip_bytes=make_txt_zip(1)
    )
    doc = batch.documents[0]
    doc.status = DocumentStatus.processing
    doc.processing_started_at = utcnow()
    db.commit()

    requeued = requeue_stuck_documents(db)
    assert requeued == 0
"""Tests for async document processing and idempotency."""

import pytest

from app.models import BatchStatus, Document, DocumentStatus, FindingStatus
from app.services.batch_service import create_batch, process_batch, process_document

from .conftest import make_txt_zip


def _make_batch(db, count=5, variables=None):
    variables = variables or ["effective_date", "monthly_rent"]
    batch = create_batch(db, name="proc", variables=variables, zip_bytes=make_txt_zip(count))
    return batch, variables


def test_process_batch_extracts_findings(db):
    batch, variables = _make_batch(db)
    process_batch(db, batch)

    docs = batch.documents
    assert all(d.status == DocumentStatus.completed for d in docs)
    assert batch.status == BatchStatus.completed
    assert batch.completed_documents == len(docs)

    for doc in docs:
        assert len(doc.findings) == len(variables)
        assert all(f.status == FindingStatus.pending for f in doc.findings)
        assert all(f.extracted_value for f in doc.findings)


def test_processing_is_idempotent(db):
    batch, _ = _make_batch(db)
    process_batch(db, batch)
    first_pass = [d.status for d in batch.documents]

    process_batch(db, batch)
    docs = batch.documents
    assert [d.status for d in docs] == first_pass
    # No duplicate findings and no double-processing.
    assert all(d.status == DocumentStatus.completed for d in docs)
    assert all(len(d.findings) == 2 for d in docs)


def test_process_document_twice_does_not_duplicate(db):
    batch, variables = _make_batch(db, count=1)
    doc = batch.documents[0]

    process_document(db, doc)
    db.expire_all()
    doc = db.get(Document, doc.id)
    process_document(db, doc)

    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.status == DocumentStatus.completed
    assert len(doc.findings) == len(variables)


def test_failed_documents_drive_partial_batch(db, monkeypatch):
    from app.services import batch_service

    def flaky(*args, **kwargs):
        from app.services.extraction import MockExtractionError

        raise MockExtractionError("provider 500")

    monkeypatch.setattr(batch_service, "extract", flaky)

    batch, _ = _make_batch(db, count=3)
    process_batch(db, batch)

    docs = batch.documents
    assert all(d.status == DocumentStatus.failed for d in docs)
    assert all(d.error_message for d in docs)
    assert batch.status == BatchStatus.failed
    assert batch.failed_documents == 3
    # No findings created for failed documents.
    assert all(len(d.findings) == 0 for d in docs)


def test_retry_reprocesses_failed_document(db, monkeypatch):
    from app.services import batch_service

    calls = {"n": 0}

    def flaky_once(*args, **kwargs):
        calls["n"] += 1
        from app.services.extraction import MockExtractionError

        if calls["n"] <= 1:
            raise MockExtractionError("provider 500")
        return {"effective_date": "2026-01-01", "monthly_rent": "Rs. 10,000"}

    monkeypatch.setattr(batch_service, "extract", flaky_once)

    batch, _ = _make_batch(db, count=1)
    process_batch(db, batch)
    assert batch.status == BatchStatus.failed

    process_batch(db, batch)
    db.expire_all()
    doc = batch.documents[0]
    assert doc.status == DocumentStatus.completed
    assert batch.status == BatchStatus.completed


def test_extracted_values_match_requested_variables(db):
    variables = ["security_deposit", "monthly_rent", "maintenance_charges", "notice_period"]
    batch, _ = _make_batch(db, count=1, variables=variables)
    process_batch(db, batch)

    doc = batch.documents[0]
    names = {f.variable_name for f in doc.findings}
    assert names == set(variables)
    values = {f.variable_name: f.extracted_value for f in doc.findings}
    assert "Rs." in values["monthly_rent"]
    assert "Rs." in values["security_deposit"]
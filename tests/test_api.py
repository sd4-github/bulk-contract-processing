"""Tests for the HTTP API: upload, status, findings review, health."""

import json

from app.database import SessionLocal
from app.models import Batch, FindingStatus
from app.services.batch_service import process_batch

from .conftest import make_txt_zip


def _upload(client, zip_bytes, variables, name="rent"):
    return client.post(
        "/batches",
        files={"zip_file": ("batch.zip", zip_bytes, "application/zip")},
        data={
            "variables": json.dumps(variables),
            "name": name,
        },
    )


def _process(client, batch_id):
    with SessionLocal() as db:
        process_batch(db, db.get(Batch, batch_id))


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_upload_returns_accepted_async(client, disable_worker, sample_zip, variables):
    resp = _upload(client, sample_zip, variables)
    assert resp.status_code == 202
    data = resp.json()
    assert data["batch_id"] == 1
    assert data["total_documents"] == 5
    assert data["status"] == "queued"
    assert data["variables"] == variables


def test_upload_validates_variables(client, sample_zip):
    resp = _upload(client, sample_zip, "not-a-list")
    assert resp.status_code == 422

    resp = _upload(client, sample_zip, [])
    assert resp.status_code == 422


def test_upload_rejects_bad_zip(client, disable_worker, variables):
    resp = _upload(client, b"not a zip", variables)
    assert resp.status_code == 422


def test_list_and_get_batch(client, disable_worker, sample_zip, variables):
    _upload(client, sample_zip, variables)

    listed = client.get("/batches")
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    assert body[0]["total_documents"] == 5

    detail = client.get("/batches/1")
    assert detail.status_code == 200
    assert detail.json()["total_documents"] == 5


def test_batch_status_reflects_documents(client, disable_worker, sample_zip, variables):
    _upload(client, sample_zip, variables)
    detail = client.get("/batches/1")
    assert detail.json()["status"] == "queued"


def test_get_document_after_processing(client, disable_worker, sample_zip, variables):
    _upload(client, sample_zip, variables)
    _process(client, 1)

    resp = client.get("/batches/1/documents/1")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["status"] == "completed"
    assert len(doc["findings"]) == len(variables)


def test_review_accept_and_reject(client, disable_worker, sample_zip, variables):
    _upload(client, sample_zip, variables)
    _process(client, 1)

    pending = client.get("/findings")
    findings = pending.json()
    assert len(findings) == 5 * len(variables)
    assert all(f["status"] == "pending" for f in findings)

    # Accept the first, reject the second.
    first, second = findings[0], findings[1]
    ok = client.post(
        f"/findings/{first['id']}/review",
        json={"decision": "accepted", "reviewer": "alice"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "accepted"
    assert ok.json()["reviewer"] == "alice"

    rej = client.post(
        f"/findings/{second['id']}/review", json={"decision": "rejected"}
    )
    assert rej.json()["status"] == "rejected"

    # Filter by status.
    accepted = client.get("/findings", params={"status": "accepted"})
    assert len(accepted.json()) == 1
    rejected = client.get("/findings", params={"status": "rejected"})
    assert len(rejected.json()) == 1


def test_review_invalid_decision(client, disable_worker, sample_zip, variables):
    _upload(client, sample_zip, variables)
    _process(client, 1)

    fid = client.get("/findings").json()[0]["id"]
    resp = client.post(f"/findings/{fid}/review", json={"decision": "maybe"})
    assert resp.status_code == 422


def test_batch_filter_by_status(client, disable_worker, sample_zip, variables):
    _upload(client, sample_zip, variables)
    resp = client.get("/batches", params={"status": "queued"})
    assert len(resp.json()) == 1
    resp = client.get("/batches", params={"status": "completed"})
    assert len(resp.json()) == 0
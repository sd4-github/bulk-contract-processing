"""Tests for ZIP intake and constraint validation."""

import io
import zipfile

import pytest

from app.services.batch_service import ZipValidationError, create_batch

from .conftest import make_txt_zip, make_zip


def test_create_batch_persists_documents(db, sample_zip, variables):
    batch = create_batch(
        db, name="rent", variables=variables, zip_bytes=sample_zip
    )
    assert batch.id is not None
    assert batch.total_documents == 5
    assert batch.status.value == "queued"

    docs = batch.documents
    assert len(docs) == 5
    assert {d.filename for d in docs} == {
        f"contracts/doc_{i}.txt" for i in range(5)
    }


def test_zip_must_contain_files(db, variables):
    empty = make_zip({})
    with pytest.raises(ZipValidationError):
        create_batch(db, name="empty", variables=variables, zip_bytes=empty)


def test_rejects_non_zip(db, variables):
    with pytest.raises(ZipValidationError):
        create_batch(db, name="bad", variables=variables, zip_bytes=b"not a zip")


def test_rejects_over_limit_documents(db, variables, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "max_documents_per_zip", 3)
    big = make_txt_zip(5)
    with pytest.raises(ZipValidationError, match="limit"):
        create_batch(db, name="big", variables=variables, zip_bytes=big)


def test_rejects_oversized_document(db, variables, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "max_document_size_mb", 1)
    payload = b"x" * (2 * 1024 * 1024)  # 2 MB
    big = make_zip({"contracts/huge.txt": payload})
    with pytest.raises(ZipValidationError, match="limit is 1 MB"):
        create_batch(db, name="huge", variables=variables, zip_bytes=big)


def test_skips_directory_and_macos_junk(db, variables):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("docs/a.txt", b"a")
        zf.writestr("docs/subdir/", b"")  # directory entry
        zf.writestr("__MACOSX/docs/._a.txt", b"junk")
    batch = create_batch(db, name="junk", variables=variables, zip_bytes=buf.getvalue())
    assert batch.total_documents == 1
    assert batch.documents[0].filename == "docs/a.txt"


def test_rolls_back_batch_on_invalid_zip(db, variables):
    with pytest.raises(ZipValidationError):
        create_batch(db, name="rollback", variables=variables, zip_bytes=b"nope")
    from app.models import Batch

    assert db.query(Batch).count() == 0
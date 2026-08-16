"""Test fixtures: isolated DB + storage, and a controllable worker."""

import os
import shutil
import tempfile

import pytest

TEST_DIR = tempfile.mkdtemp(prefix="contract-batch-tests-")

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DIR}/test.db"
os.environ["STORAGE_DIR"] = f"{TEST_DIR}/storage"
os.environ["MOCK_EXTRACTION_DELAY"] = "0.0"
os.environ["MOCK_FAILURE_RATE"] = "0.0"
os.environ["QUEUE_POLL_SECONDS"] = "0.1"

from fastapi.testclient import TestClient  # noqa: E402

from app.api import routes  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Batch, Document, Finding  # noqa: E402
from app.services.batch_service import create_batch, process_batch  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database():
    from app.database import Base

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    storage = os.environ["STORAGE_DIR"]
    if os.path.isdir(storage):
        shutil.rmtree(storage)
    yield
    Base.metadata.drop_all(bind=engine)


class _StubWorker:
    running = False

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def client(monkeypatch):
    # Keep the real background worker out of tests; processing is driven
    # synchronously via process_batch/process_document instead.
    monkeypatch.setattr(routes, "worker", _StubWorker())
    monkeypatch.setattr("app.main.worker", _StubWorker(), raising=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def disable_worker(monkeypatch):
    """Explicit alias; the client fixture already stubs the worker."""

    monkeypatch.setattr(routes, "worker", _StubWorker())
    monkeypatch.setattr("app.main.worker", _StubWorker(), raising=False)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def make_zip(payloads: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in payloads.items():
            zf.writestr(name, content)
    return buf.getvalue()


def make_txt_zip(count: int = 5, prefix: str = "contracts") -> bytes:
    return make_zip(
        {
            f"{prefix}/doc_{i}.txt": f"document number {i}".encode()
            for i in range(count)
        }
    )


@pytest.fixture
def sample_zip():
    return make_txt_zip(5)


@pytest.fixture
def variables():
    return ["effective_date", "monthly_rent", "governing_law"]
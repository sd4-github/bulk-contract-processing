"""In-process background worker.

Polls a DB-backed queue (batches in ``queued`` or ``processing`` state) and
processes their documents. Runs on an asyncio loop in a dedicated thread started
at application startup.

This intentionally mirrors a distributed worker (e.g. Celery/RQ workers) so the
design can be lifted to production: the "queue" is the database, and the worker
picks work with a claim (status transition) for idempotency.
"""

import asyncio
import logging
import threading

from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import Batch, BatchStatus, Document
from .batch_service import requeue_stuck_documents

logger = logging.getLogger("app.worker")


class Worker:
    """Runs until stopped, claiming batches and processing documents."""

    def __init__(self, concurrency: int = 4) -> None:
        self.concurrency = concurrency
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="batch-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._pump())

    # -- main loop ---------------------------------------------------------

    async def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                await self._sweep()
            except Exception:  # noqa: BLE001 - worker must keep running
                logger.exception("Worker sweep failed; continuing")
            await asyncio.sleep(settings.queue_poll_seconds)

    async def _sweep(self) -> None:
        with SessionLocal() as db:
            # Recover documents stuck in 'processing'.
            requeue_stuck_documents(db)

            # Claim queued batches and dispatch their documents.
            batches = (
                db.query(Batch)
                .filter(
                    Batch.status.in_([BatchStatus.queued, BatchStatus.processing])
                )
                .order_by(Batch.id.asc())
                .limit(10)
                .all()
            )
            for batch in batches:
                if self._stop.is_set():
                    break
                try:
                    await self._process_batch(batch.id)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to process batch %s", batch.id)

    async def _process_batch(self, batch_id: int) -> None:
        # Dispatch the whole batch (all queued + retryable documents) in a
        # worker thread. Documents within a batch are processed sequentially for
        # simple SQLite semantics; concurrency across batches is bounded by the
        # single worker thread.
        await asyncio.to_thread(self._process_batch_docs, batch_id)
        with SessionLocal() as db:
            refresh_batch_metrics(db, batch_id)

    def _process_batch_docs(self, batch_id: int) -> None:
        with SessionLocal() as db:
            from .batch_service import process_batch

            batch = db.get(Batch, batch_id)
            if batch is None:
                return
            try:
                process_batch(db, batch)
            except Exception:  # noqa: BLE001
                logger.exception("Batch %s processing failed", batch_id)

    def _process_doc(self, doc_id: int) -> None:
        with SessionLocal() as db:
            from .batch_service import process_document

            doc = db.get(Document, doc_id)
            if doc is None:
                return
            try:
                process_document(db, doc)
            except Exception:  # noqa: BLE001
                logger.exception("Document %s processing failed", doc_id)


def refresh_batch_metrics(db: Session, batch_id: int) -> None:
    from .batch_service import refresh_batch_metrics as _refresh

    _refresh(db, batch_id)


worker = Worker(concurrency=settings.worker_concurrency)
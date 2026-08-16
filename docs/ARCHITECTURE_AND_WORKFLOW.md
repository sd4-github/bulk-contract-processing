# Architecture & Workflow (FastAPI learning guide)

This document explains how the contract-bulk-processing app is built and how a
request flows through it, **in the context of learning FastAPI**. Each section
maps a real piece of the code to the FastAPI / Python concept it demonstrates.

Read this alongside the source. File:line references point at the exact code.

---

## 1. The big picture

```
                 ┌───────────────────────────────────────────────────────┐
                 │                     app/main.py                       │
                 │        FastAPI()  +  lifespan  +  include_router      │
                 └───────────────────────┬───────────────────────────────┘
                                         │ routes (app/api/routes.py)
                                         ▼
   upload  ──►  POST /batches     GET /batches/{id}      POST /findings/{id}/review
   (ZIP+      ──► multipart form       ──► read models       ──► update one row
    vars)        202 + batch_id
                                         │
                                         │ SQLAlchemy session (per-request dependency)
                                         ▼
                             ┌───────────────────────────┐
                             │   SQLite (data/contracts.db)  │
                             │   batches · documents · findings │
                             └─────────────┬─────────────┘
                                           ▲
                         worker claims work │ (status transitions)
                                           │
                             ┌─────────────┴─────────────┐
                             │   app/services/worker.py  │  background thread,
                             │   polls the DB queue      │  asyncio loop
                             └─────────────┬─────────────┘
                                           │
                             ┌─────────────┴─────────────┐
                             │   extraction.py (mock)    │  async, mimics a
                             │   returns random values   │  real OCR API
                             └───────────────────────────┘
```

Two sides of the system:

1. **Synchronous request side** — what you call with `curl`/the browser. It
   accepts uploads, reads status, and lets reviewers accept/reject findings.
2. **Asynchronous worker side** — a background thread that churns through
   batches. The request side never waits for extraction to finish.

That split (fast response now, slow work later) is the core "async processing"
requirement of the assignment and the most useful pattern to learn here.

---

## 2. Request flow, end to end

### 2.1 Upload: `POST /batches` → `app/api/routes.py:68`

This endpoint is the best single example of FastAPI's request handling.

```python
@router.post("/batches", response_model=UploadResponse, status_code=202)
async def upload_batch(
    zip_file: UploadFile = File(...),
    variables: str = Form(...),
    name: str = Form(""),
    db: Session = Depends(get_db),
) -> UploadResponse:
```

What it teaches:

- **`UploadFile = File(...)` and `str = Form(...)`** — FastAPI's way to receive
  a multipart form. `File` gives you the uploaded binary, `Form` gives a text
  field. The `...` means "required" (like a FastAPI/typing required marker).
- **`async def`** — the function is a coroutine. While `await zip_file.read()`
  runs, the event loop is free to serve other requests. FastAPI decides
  automatically whether to run a route in the event loop (async) or a thread
  pool (sync); only `async def` routes need `await` for I/O.
- **`status_code=202`** — "Accepted". We return this instead of `200` because
  the work isn't done yet; the client should poll `GET /batches/{id}`.
- **`response_model=UploadResponse`** — FastAPI validates the returned dict
  against the Pydantic schema `app/schemas.py:70` and filters out any extra
  fields. This is how your API guarantees a stable, documented contract.

The body of the handler:

```python
parsed_variables = _serialize_variables(variables)     # validate input
payload = await zip_file.read()                         # read whole file
batch = create_batch(db, ...)                           # unzip + persist
if not worker.running:
    worker.start()                                      # ensure worker is up
return UploadResponse(batch_id=batch.id, ...)           # "here's your id, bye"
```

The function does **validate → persist → start background work → return fast**.
It never processes documents itself.

### 2.2 The DB dependency: `get_db` → `app/database.py:22`

```python
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

`Depends(get_db)` in every route is a **dependency injection**. FastAPI calls
`get_db()`, hands the session to your route, and **guarantees `finally: close()`
runs after the response**, even if the route raised. You never leak connections,
and you never write `try/finally` yourself. This is the FastAPI idiomatic
pattern for "one session per request".

### 2.3 Pydantic schemas: `app/schemas.py`

Two roles of schemas here:

- **Request validation**: `ReviewDecision` (`app/schemas.py:10`) checks that a
  review body has a `decision` of `accepted`/`rejected` and a `reviewer` string
  before your code even runs.
- **Response shaping**: `BatchDetailOut` (`app/schemas.py:66`) inherits
  `BatchOut` and adds `documents`. It uses `ConfigDict(from_attributes=True)`,
  which lets FastAPI serialize **ORM objects directly** (that's why the routes
  can return `batch` — an SQLAlchemy model — as the response).

### 2.4 Tracking: `GET /batches/{id}` → `app/api/routes.py:126`

A read-only query wrapped in a `Depends(get_db)` session. It also demonstrates
the **404 pattern**:

```python
batch = db.get(Batch, batch_id)
if batch is None:
    raise HTTPException(status_code=404, detail="Batch not found")
```

FastAPI turns the `HTTPException` into a JSON error response automatically.

### 2.5 Review: `POST /findings/{id}/review` → `app/api/routes.py:171`

The review endpoint is a **write** with three steps: load the row → mutate it →
commit → refresh the derived counters. Note it calls
`refresh_batch_metrics(...)` afterward so the batch-level "pending findings"
count stays accurate — an example of keeping denormalised counters in sync
after a write.

---

## 3. The state machine (what "processing" actually means)

Statuses live in `app/models.py` as `str, enum.Enum` classes
(`app/models.py:38`), which makes them JSON-serializable AND typed.

**Document lifecycle:**

```
        POST /batches            worker claims            provider ok
 queued ────────────► queued ────────────► processing ─────────────► completed
                        (rows inserted              (findings written
                         at upload)                 + metrics refreshed)
                                                │ provider fails
                                                ▼
                                              failed ──(retry, ≤ MAX_RETRIES)──► queued
```

**Batch status is DERIVED, not stored** (`app/models.py:141`):

```python
def derive_batch_status(total, completed, failed) -> BatchStatus:
    if total == 0:            return BatchStatus.failed
    done = completed + failed
    if done == 0:             return BatchStatus.queued
    if done < total:          return BatchStatus.processing
    if failed == 0:           return BatchStatus.completed
    if completed == 0:        return BatchStatus.failed
    return BatchStatus.partial
```

Why this matters (and it's directly relevant to FastAPI beginners): a common
mistake is to store `batch.status` as its own column updated "somewhere else".
That column drifts out of sync with the documents. By **computing** it from the
documents on every read (`refresh_batch_metrics`, `app/services/batch_service.py:182`),
there is a single source of truth — the document rows.

---

## 4. The worker: background async processing

### 4.1 Lifecycle hook: `lifespan` → `app/main.py:19`

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()        # create tables on startup
    worker.start()   # start the background thread
    yield
    worker.stop()    # clean shutdown
```

`lifespan` is FastAPI's modern replacement for the old `on_event` startup
handlers. Code before `yield` runs on startup; code after runs on shutdown. This
is the right place for "warm up the app" logic.

### 4.2 What the worker does: `app/services/worker.py`

The worker is a **daemon thread** running its own asyncio event loop
(`app/services/worker.py:26`). Its `_pump` loop (`app/services/worker.py:62`):

```python
async def _pump(self):
    while not self._stop.is_set():
        await self._sweep()          # find work + do it
        await asyncio.sleep(settings.queue_poll_seconds)   # poll interval
```

Each sweep:
1. `requeue_stuck_documents` — flips documents stuck in `processing` back to
   `queued` (crash recovery).
2. Finds batches in `queued`/`processing`, and for each:
   `await asyncio.to_thread(self._process_batch_docs, batch_id)`.

`asyncio.to_thread` runs the blocking work in a thread-pool thread so the
worker's loop stays responsive. This is the FastAPI-adjacent pattern for "don't
block the loop with CPU/blocking work".

### 4.3 Claim semantics: `_claim_document` → `app/services/batch_service.py:218`

```python
def _claim_document(db, doc_id) -> bool:
    doc = db.get(Document, doc_id)
    if doc is None or doc.status == DocumentStatus.completed:
        return False
    if doc.status == DocumentStatus.failed and doc.retry_count >= settings.max_retries:
        return False
    was_failed = doc.status == DocumentStatus.failed
    doc.status = DocumentStatus.processing      # ← the claim
    if was_failed:
        doc.retry_count += 1
    ...
    db.commit()
    return True
```

`process_document` (`app/services/batch_service.py:243`) calls `_claim_document`
**first**. Only if the claim succeeds does it call the provider and insert
findings. Because the claim is a state transition, two workers can never both
"own" the same document → **no duplicate findings**. This is the idempotency
guarantee discussed in the Part 2 case study.

### 4.4 The mocked provider: `extract` → `app/services/extraction.py:76`

```python
async def extract(document_path, variables, *, seed=None) -> dict[str, str]:
    await asyncio.sleep(settings.mock_extraction_delay_seconds)  # fake latency
    if settings.mock_failure_rate > 0 and random.random() < settings.mock_failure_rate:
        raise MockExtractionError("Simulated provider failure")
    rng = random.Random(seed if seed is not None else document_path)
    return {variable: _generator_for(variable)(rng) for variable in variables}
```

Design points worth learning:

- It's `async` (fake network I/O), returning plausible values keyed by variable
  name (`monthly_rent` → `"Rs. 12,345"`, `effective_date` → `"2026-01-01"`).
- The seed is derived from the document path → **deterministic per document**,
  which makes tests reproducible.
- The whole worker depends only on the `extract(path, variables)` signature
  (bridged through `run_coro`, `app/services/batch_service.py:31`). Swapping in
  a real provider = write a new function with the same signature.

---

## 5. SQLAlchemy models: `app/models.py`

Each model maps to a table. Notable FastAPI-relevant details:

- `Mapped[type] = mapped_column(...)` — the SQLAlchemy 2.0 typed style. The
  type hints line up with the Pydantic schemas, so FastAPI serializes them
  cleanly.
- **Enums as columns** (`Enum(BatchStatus)`) — stored as strings; the Python
  enum value is exposed through the API.
- **Relationships** (`Batch.documents`, `Document.findings`) — so a route can
  return `batch` and FastAPI walks the ORM graph into nested JSON.
- `JSONType` (`app/models.py:21`) is a custom `TypeDecorator` that stores the
  `variables` list as JSON text. A small but useful example of extending
  SQLAlchemy column types.

---

## 6. One-request mental model (cheat sheet)

| You see this in the code | FastAPI concept | What it buys you |
|---|---|---|
| `response_model=...` | Response validation | Guaranteed API shape; no leaking ORM internals |
| `Depends(get_db)` | Dependency injection | Auto session lifecycle; testable |
| `UploadFile` / `Form` | Multipart parsing | No manual `request.files` handling |
| `HTTPException(404, ...)` | Error responses | Consistent JSON errors |
| `status_code=202` | HTTP semantics | Correct "accepted for later processing" contract |
| `lifespan` | App lifecycle | Startup/shutdown hook for worker + DB init |
| `async def` + `await` | Coroutines | Non-blocking I/O under load |
| `asyncio.to_thread` | Blocking-IO bridge | Don't stall the event loop on sync work |

---

## 7. Exercises to practice (if you're learning)

1. **Add an endpoint** `GET /batches/{id}/stats` that returns counts by finding
   status. You'll practice `Depends(get_db)`, a query with `group_by`, and a new
   Pydantic schema.
2. **Add pagination** to `GET /findings` (it already has `limit`; add `offset`
   and a `total` field) — classic FastAPI query-param practice.
3. **Replace the mock provider**: write a second `extract` implementation that
   returns `"NOT_IMPLEMENTED"` for every variable and swap it in — no other file
   changes. This proves the provider seam works.
4. **Add a retry endpoint** `POST /batches/{id}/retry` that flips failed
   documents back to `queued`. Watch the worker pick them up.
5. **Write a test** for one of your new endpoints following the pattern in
   `tests/test_api.py` (TestClient + `disable_worker` fixture).

Run the test suite to check your changes: `.venv/bin/pytest -q`

---

## 8. File map (learn-by-reading order)

| File | What it teaches |
| ---- | --------------- |
| `app/config.py` | Settings via env vars + Pydantic |
| `app/database.py` | Engine, session, `get_db` dependency |
| `app/models.py` | SQLAlchemy models, enums, JSON type |
| `app/schemas.py` | Pydantic request/response models |
| `app/api/routes.py` | Routes: multipart, query params, DI, errors |
| `app/main.py` | App assembly + `lifespan` |
| `app/services/extraction.py` | Mock provider (async seam) |
| `app/services/batch_service.py` | Business logic: intake, claim, process |
| `app/services/worker.py` | Background worker (thread + asyncio) |
| `tests/` | FastAPI `TestClient` patterns |
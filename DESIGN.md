# Design Document

## 1. Overview

`contract-batch` is a FastAPI service that ingests a ZIP of up to 1,000 contract
documents (each up to 15 MB), extracts user-defined variables from every
document in the background via a mocked extraction API, and exposes the results
for human review (accept/reject) with full batch/document status tracking.

The core architectural idea is **separation of concerns**:

```
                          ┌──────────────────────────────┐
                          │            HTTP API          │
                          │  POST /batches               │
                          │  GET  /batches/{id}          │
                          │  GET  /findings              │
                          │  POST /findings/{id}/review  │
                          └──────────────┬───────────────┘
                                         │ SQLAlchemy
                                         ▼
                             ┌─────────────────────────┐
                             │   SQL DB  (SQLite now,  │
                             │   Postgres in prod)     │
                             │   batches/documents/    │
                             │   findings              │
                             └────────────┬────────────┘
                                          ▲
                                          │ claim (status transition)
                         ┌────────────────┴────────────────┐
                         │        Background worker        │
                         │  polls queue, claims documents, │
                         │  calls the extraction provider, │
                         │  writes findings                │
                         └────────────────┬────────────────┘
                                          ▼
                              ┌──────────────────────────┐
                              │  Extraction provider     │
                              │  (mock now, real later)  │
                              └──────────────────────────┘
```

## 2. Data model

- **Batch** — a submitted ZIP. Stores the requested `variables`, derived
  counters (`total/completed/failed_documents`, `pending_findings`) and a
  *derived* `status`.
- **Document** — one file inside the batch. Holds `filename`, `size_bytes`,
  on-disk `storage_path`, per-document `status`, `error_message`,
  `retry_count`, timestamps.
- **Finding** — one extracted value for one document/variable pair. Holds
  `variable_name`, `extracted_value`, review `status`, `reviewer`,
  `reviewed_at`.

### Status model

```
Document: queued → processing → completed
                       └──(failure)──→ failed → (retry, ≤MAX_RETRIES) → queued
Batch:    derived from documents:
             all done & 0 failed → completed
             some failed, some done → partial
             all failed → failed
             otherwise (work remaining) → queued/processing
```

**Batch status is never stored independently; it is derived from document
counts.** This is a deliberate choice that eliminates the "batch status does not
match individual documents" failure mode (see Part 2 case study) by
construction.

## 3. Async processing model

- A dedicated **worker thread** runs an asyncio loop that *polls* the DB for
  batches in `queued`/`processing` state — a deliberate mirror of a distributed
  Celery/RQ worker so the design lifts cleanly to production.
- Each document is **claimed** via an atomic `queued → processing` transition.
  Because the claim is a row-level state transition, concurrent workers cannot
  process the same document twice (idempotency). Findings are only written
  after the claim succeeds.
- The mocked extraction API is `async` (simulating provider latency). Processing
  runs in worker threads; each document's provider call is awaited via
  `asyncio.run`/`to_thread` bridging.
- Failed documents are retried up to `MAX_RETRIES` (exponential backoff would be
  added in production).
- Documents stuck in `processing` beyond `STUCK_THRESHOLD_SECONDS` are
  automatically requeued on each sweep (recovery for worker crashes).

## 4. Reviewer workflow

- Reviewers list `pending` findings (optionally per batch), then issue
  `accept`/`reject` decisions. `reviewer` and `reviewed_at` are recorded for
  auditability. Batch-level `pending_findings` counters are recomputed on each
  decision.

## 5. Evolution to production (future scope)

Each future requirement maps onto a change that keeps the current seams intact:

| Future requirement | Design change |
| ------------------ | ------------- |
| **Real extraction/OCR providers** | The worker only calls `extract(path, variables)`. Introduce a provider interface with a registry (mock, commercial extraction engine, OCR vendor). Add idempotency keys per (document, provider, model-version) so re-runs never create duplicates. |
| **Editing extracted findings** | Findings get a `source` (extracted vs edited) and `edited_value`; audit-history events record before/after + reviewer. |
| **Audit history for all changes** | Add an `audit_events` table (entity, action, old/new payload, actor, ts). Written in the same transaction as the change. |
| **Multiple reviewers & user roles** | Add users/roles; findings get a `reviewer_id` FK; statuses `assigned → pending_review`. RBAC on endpoints. |
| **Persistent object storage** | `STORAGE_DIR` is the only place documents live on disk; swap the local filesystem adapter for S3/GCS (presigned upload + object keys stored on `Document`). The model already stores a `storage_path`, which becomes an object key. |
| **Higher processing volumes / distributed workers** | The DB-backed queue becomes a real broker (Celery/RabbitMQ, RQ/Redis, or AWS SQS + worker fleet). `concurrency` is scaled out per worker. Claim semantics are preserved via the broker's atomic dequeue. |
| **Additional document formats / extraction rules** | Extraction is already decoupled (path + variable list); add a format-detection step (PDF/DOCX/scan → text) before calling the provider. Variable schemas become declarative per contract type. |

### Hardening for production (non-functional)

- **Database**: move to Postgres; the JSON `variables` column maps naturally to
  `JSONB`; add indexes on status columns and `(batch_id, status)`.
- **Observability**: structured logs with `batch_id`/`document_id` correlation,
  OpenTelemetry traces across upload → worker → provider → review, and metrics
  (batch duration, throughput, failure rate, queue depth).
- **Concurrency**: today documents in a batch are processed sequentially (simple
  SQLite semantics); with Postgres, process documents within a batch in parallel
  up to `WORKER_CONCURRENCY`.
- **Rate limiting & retry/backoff** for the provider, plus a circuit breaker.

## 6. Assumptions and trade-offs

| Area | Assumption / trade-off | Why |
| ---- | ---------------------- | --- |
| SQLite in the assignment | Production would use Postgres | Zero-setup runnable deliverable; the SQLAlchemy layer is DB-agnostic. |
| In-process worker | A real broker is needed for scale/durability | Keeps the deliverable self-contained while mirroring the production pattern (claim/ack model). |
| Mocked provider | Random but plausible values, optional failure rate | Assignment requirement; seeded per-document RNG for reproducible tests. |
| Batch status derived | Re-computed on writes rather than stored | Eliminates drift; cheap at this volume, indexed counters keep reads fast. |
| Findings are separate rows | One row per (document, variable) | Natural fit for accept/reject per value and for review queues. |
| On-disk filenames are prefixed by header offset | Prevents collisions between same-named files in different ZIP dirs | Flat, collision-safe storage; original relative path preserved for display. |
| Stuck-recovery threshold | Static `STUCK_THRESHOLD_SECONDS` | Simple; production would derive it from the provider SLA. |

## 7. Known limitations

- Documents in a batch are processed sequentially; parallelism is bounded by the
  single worker thread (acceptable for the mock, not for 1,000-doc SLAs).
- No real authentication/RBAC (single implicit reviewer).
- No audit history or editing of findings yet.
- SQLite serialises writes; concurrent reviewers can contend on the DB.
- The worker queue is not durable across process restarts in this build (a
  restart just re-scans the DB, so no work is lost, but ordering is not
  guaranteed).
- ZIP contents are extracted to local disk; no object-storage backend yet.
- No exponential backoff on retries (fixed retry count only).
- The mock never returns per-variable confidence scores, which a real
  reviewer workflow would want for prioritisation.
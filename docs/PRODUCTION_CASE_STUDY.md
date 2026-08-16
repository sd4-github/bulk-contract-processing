# Part 2 — Production Issue Case Study

This document analyses the operational incidents reported against the
contract-document-processing application and proposes causes, investigation
steps, mitigations, remediations, risks and assumptions for each. A condensed
cause-and-remediation table is at the end.

---

## Working model (assumed architecture)

```
Upload → API → DB(queue) → Worker fleet → Extraction service → DB(findings) → Reviewer
```

Batches of up to 1,000 documents are enqueued; workers claim documents, call an
external extraction service, and persist findings. Status is derived from
per-document state; findings are reviewed by humans.

The incidents below are **highly correlated** — several share root causes. This
is important: fixing the shared root causes (lack of idempotency, lack of
claim semantics, batch-level status written independently of documents) resolves
multiple symptoms at once.

---

## Incident 1 — Large batches are taking much longer to complete

### Possible causes
- **C1.1 No/limited concurrency**: documents are processed sequentially per
  batch (one worker, one document at a time), so a 1,000-doc batch has a
  serialised wall-clock time of `N × (per-doc latency)`.
- **C1.2 Per-document overhead**: every document incurs a provider round-trip,
  a DB transaction, and possibly a new session/connection; no batching or
  connection pooling tuning.
- **C1.3 Thundering herd / hot queue**: many batches submitted at once; workers
  poll one shared queue and each batch is locked as a whole, so small batches
  starve behind huge ones.
- **C1.4 Slow extraction service** (see Incident 3): provider latency dominates;
  the queue backs up.
- **C1.5 Lock contention on the DB**: documents marked `processing` via an
  optimistic update that retries on conflict, or full-table scans to find
  work, slowing everything down as the table grows.

### Investigation / validation
- Instrument per-phase timings: enqueue → claim → provider-call → persist →
  review-ready. Correlate `completed_at - created_at` per batch with
  `batch_size` and `worker_count`.
- Add throughput/latency dashboards (docs/sec, p50/p95/p99 per document) and
  queue-depth metrics; look for a flat "serialisation" profile.
- Inspect DB slow-query logs / EXPLAIN on the claim and status-update queries;
  check for full scans and lock waits.
- Check provider latency histogram (Incident 3) and worker utilisation; if
  workers idle while queue is deep, the bottleneck is elsewhere.

### Immediate mitigation
- Increase `WORKER_CONCURRENCY` (more workers / higher concurrency per worker)
  and let documents within a batch be processed concurrently instead of
  batch-serialised.
- Raise provider client timeouts/retries only after confirming the provider is
  the bottleneck; add basic batching of DB writes (bulk insert findings).
- Rebalance: process documents, not whole batches, as the unit of work, so one
  slow batch doesn't block others.

### Permanent remediation
- Make **the document** the unit of work (not the batch) with per-document
  claim semantics (see Incident 4) and a bounded thread pool / async worker
  fleet.
- Introduce a real queue (RabbitMQ/SQS/Celery) with worker autoscaling and
  priority queues for small batches.
- Batch DB writes (commit findings per document, but use bulk inserts); use
  Postgres with proper indexes on `(status)`, `(batch_id, status)`.
- Add backpressure + bounded concurrency + circuit breaker toward the
  provider; cache provider responses keyed by document content hash when safe.

### Risks / side effects
- Higher concurrency raises provider and DB load (watch Incident 3/5).
- Retrying/bulk changes risk duplicate findings unless idempotency is in place
  (Incident 4) — **do concurrency changes with idempotency first**.
- Priority queues add operational complexity.

### Assumptions
- The provider is the primary latency driver and can absorb more concurrency;
- DB is not the first bottleneck; worker count is currently low.

---

## Incident 2 — Some documents remain in a processing state indefinitely

### Possible causes
- **C2.1 Worker crash/kill between claim and completion**: the document was
  marked `processing`, the worker died, and nothing ever re-claims it.
- **C2.2 No claim timeout**: there is no mechanism to detect a document stuck in
  `processing`; no liveness/heartbeat.
- **C2.3 Uncaught exception leaves state inconsistent**: the status update to
  `processing` commits, but a later step (provider call, finding insert) throws
  and the transaction that would set `completed`/`failed` never runs.
- **C2.4 Transaction boundary bug**: the `processing` mark and the completion
  mark are written in different transactions without a compensating action.
- **C2.5 Deploy/restart during long processing**: rolling deploys terminate
  in-flight work with no handoff.
- **C2.6 DB connection/session leaks**: a worker hangs on a stale connection and
  never finishes.

### Investigation / validation
- Query documents where `status='processing'` and `processing_started_at` is
  older than N× the p99 document duration; report age distribution.
- Correlate stuck documents with deploy windows, worker restarts, and
  exceptions in worker logs (`document_id` correlation IDs).
- Check the extraction provider for abandoned/in-flight requests from those
  document IDs.
- Add a heartbeat column (`last_heartbeat_at`) temporarily to distinguish
  "actively working but slow" from "abandoned".

### Immediate mitigation
- Manually requeue stuck documents (flip `processing` → `queued`, increment a
  retry counter) and retry processing.
- Restart the affected workers; raise the provider timeout/retry so long calls
  finish or fail fast.

### Permanent remediation
- **Claim timeout / stuck reaper**: a periodic job requeues any document in
  `processing` whose `processing_started_at` (or `last_heartbeat_at`) exceeds a
  threshold — bounded by `MAX_RETRIES`.
- Worker **heartbeats + graceful shutdown**: on shutdown, workers finish current
  work or release the claim (return to `queued`).
- **Outbox/SAGA-style completion**: persist a "completion intent" or use a
  single transaction per document (claim, extract via idempotency key, insert
  findings, mark done) so partial state cannot survive.
- Move to a queue broker where **visibility/ack timeouts** are a first-class
  concept (SQS visibility timeout, RabbitMQ dead-lettering) — the broker, not
  ad-hoc timestamps, drives recovery.

### Risks / side effects
- Aggressive reaping can double-process documents unless claims are
  **atomically exclusive** (Incident 4) — pair with idempotency.
- Requeuing in-flight-but-slow documents wastes provider work; use heartbeats to
  avoid false positives.
- Dead-letter queues can fill if the underlying cause is provider degradation.

### Assumptions
- Stuck state is due to abandoned work, not genuinely long-running documents
  (which would instead need a raised SLA-based threshold);
- the number of stuck docs is small enough that manual requeue is feasible now.

---

## Incident 3 — Extraction service is experiencing increased timeouts

### Possible causes
- **C3.1 Provider overload**: concurrency raised (Incident 1) or growing batch
  volume saturates the provider (no rate limiting / burst control).
- **C3.2 No timeout/retry with backoff**: clients wait too long, retry in a
  thundering herd, or retry without jitter, amplifying load.
- **C3.3 Document bloat**: near-15 MB documents take long to OCR; provider SLA
  exceeded; no size-based throttling or pre-processing.
- **C3.4 Connection pool exhaustion client-side**: too many open connections to
  the provider, or leaked connections from the stuck documents (Incident 2).
- **C3.5 Dependency degradation**: the provider itself depends on another
  service (OCR/GPU pipeline) that is degrading; our application is only a
  consumer.

### Investigation / validation
- Provider-side: request-rate, latency histogram, error codes (429/503/timeout),
  and per-tenant/per-file-size breakdowns.
- Client-side: connection pool stats, retry counts, and timeout configuration.
- Correlate timeouts with (a) concurrent document counts, (b) file-size
  distribution, (c) specific variable/format types.
- Check whether timeouts started when concurrency was last changed (Incident 1).

### Immediate mitigation
- Apply client-side **circuit breaker + bounded retries with exponential
  backoff and jitter**; reduce concurrency to relieve the provider.
- Raise per-request timeout moderately, but cap it and fail fast rather than
  hang.
- Reject or pre-screen oversized documents; consider a size-based queue
  priority.

### Permanent remediation
- Adopt provider rate limits / client throttling, request coalescing, and
  content-hash caching of repeated extractions.
- Move to an **asynchronous provider contract**: submit jobs, poll for results
  (webhooks/polling) instead of long synchronous calls — decouples worker
  availability from provider latency.
- Provider SLA + capacity planning; autoscaling on both sides.
- Add structured tracing (OpenTelemetry) so a timeout is traceable from document
  → queue → provider request.

### Risks / side effects
- Aggressive circuit-breaking may stall batches during provider blips; tune
  thresholds.
- Caching extraction results risks stale values if the document changed;
  invalidate on content-hash change.
- Async provider contract is a bigger change; requires a job-state store.

### Assumptions
- Provider overload is driven by our client behaviour (concurrency/retries), not
  a provider-side incident we can't see; we can influence load.

---

## Incident 4 — Some documents appear to have been processed more than once

### Possible causes
- **C4.1 No claim semantics / no idempotency key**: two workers (or two retries)
  pick up the same document and both extract + insert findings; there is no
  "claimed" state transition guarding the work.
- **C4.2 Stuck-document reaper double-fires**: the reaper requeues a document
  that was actually being processed (Incident 2), so the original worker and the
  re-run both complete it.
- **C4.3 At-least-once queue without dedup**: the queue redelivers messages
  (visibility timeout expiry, worker ack failure) and the consumer has no
  deduplication.
- **C4.4 Retry-after-failure logic is buggy**: a document marked `failed` was
  retried but its partial findings were never cleaned up, so it looks like it
  ran twice; or retry doesn't check whether it already succeeded.
- **C4.5 Findings inserted outside the completion transaction**: provider call
  succeeded, findings inserted, but completion-mark failed; a retry inserts
  findings again.

### Investigation / validation
- Count findings per (document, variable): duplicates are the smoking gun.
- Inspect worker logs for the same `document_id` claimed at overlapping times;
  correlate with reaper runs and redelivery/visibility-expiry events.
- Add a `processing_token`/lease (unique per claim) and check whether
  completion uses the same token.
- Query for documents whose `retry_count` > 0 AND `status='completed'` — were
  they legitimately retried or double-processed?

### Immediate mitigation
- Halt auto-retries of stuck documents until idempotency is in place; manually
  deduplicate affected findings (or roll back the affected batch and re-run).
- Pause the reaper; reduce queue redelivery by raising visibility timeouts.

### Permanent remediation
- **Atomic claim**: `UPDATE documents SET status='processing', lease_token=<uuid>
  WHERE id=? AND status IN ('queued')` — only one claim succeeds. Completion:
  `UPDATE ... SET status='completed' WHERE id=? AND lease_token=?`.
- **Idempotency key**: unique constraint on `(document_id, extraction_attempt)`
  or `(content_hash, provider, model_version, variable)` so re-running cannot
  duplicate findings (INSERT ... ON CONFLICT DO NOTHING).
- Queue-side **dedup** (message/Job IDs) and poison-letter handling.
- Make completion **transactional**: insert findings and mark `completed` in the
  same DB transaction; if it fails, the claim is released atomically.
- Reaper uses the same lease token so it never requeues a claim that is still
  valid.

### Risks / side effects
- Unique constraints will surface latent duplicates as errors on first deploy —
  need a backfill/dedup migration.
- Lease-based claims add complexity (lease expiry handling); combine with
  Incident 2's heartbeat so leases don't expire mid-work.

### Assumptions
- Duplicates originate from missing idempotency at the application/DB layer
  (not intentional re-processing of revised documents).

---

## Incident 5 — Database is showing increased load

### Possible causes
- **C5.1 N+1 writes**: per-document findings are inserted one-by-one; counters
  and statuses are recomputed per document (many small transactions).
- **C5.2 No indexes / full-table scans**: the worker's "find work" query scans
  the documents table; status filters aren't indexed; batch list queries are
  unscoped.
- **C5.3 Connection churn**: a new connection per document or per worker sweep
  without pooling; connection limit hit (SQLite locks; Postgres exhaustion).
- **C5.4 Lock contention / hot rows**: concurrent claims and counter updates
  on the same batch rows cause waits and retries (Incident 1/4).
- **C5.5 Unbounded review queries**: listing findings without pagination/filters
  scans huge tables.

### Investigation / validation
- DB metrics: QPS, lock waits, connection count, slow-query log, index usage
  (`pg_stat_user_indexes`).
- Count transactions per document; measure per-batch write amplification.
- Profile the claim query and the metrics-recompute query with EXPLAIN.

### Immediate mitigation
- Add the obvious indexes (document `status`, `(batch_id, status)`, finding
  `(status)`, finding `(document_id)`).
- Reduce write amplification: update batch counters on completion of the whole
  batch (or derived-on-read) rather than per document.
- Increase connection pool size; enable statement/connection reuse.

### Permanent remediation
- **Derived status**: stop storing/updating batch-level counters transactionally
  per document; compute batch status/counters from documents (aggregate query or
  incremental counters updated once per batch completion) — removes per-document
  counter contention.
- Batch inserts for findings; commit per document but bulk-create rows.
- Move to Postgres, use connection pooling (PGBouncer), and tune
  `max_connections` for the worker fleet.
- Paginate review queries and add covering indexes.
- Consider an analytics read replica for reporting queries.

### Risks / side effects
- Derived status needs an aggregate path that stays consistent (Incident 6);
  ensure the read path and the write path agree.
- Bulk inserts change error handling for duplicates (Incident 4).

### Assumptions
- The load is caused by write amplification and missing indexes, not a
  legitimate throughput ceiling; a read replica is acceptable for reporting.

---

## Incident 6 — Batch-level status does not always match the status of individual documents

### Possible causes
- **C6.1 Batch status stored separately**: `batches.status` is a stored column
  updated at different times / by different code paths than `documents.status`;
  the two can drift (a race or a missed update).
- **C6.2 Non-atomic updates**: counters on the batch are updated in a different
  transaction than the document, so an observer between the two sees
  inconsistency; on failure one side is left stale.
- **C6.3 Multiple writers**: the worker, the reaper, and the review endpoint all
  update batch state with no locking, so updates overwrite each other.
- **C6.4 Derivation logic duplicated**: document-status mapping to batch status
  is implemented in more than one place and disagrees (e.g. `partial` handling).

### Investigation / validation
- Find batches where `batches.status` != the mapping of their documents; sample
  the distribution.
- Diff `batches.updated_at` vs the newest `documents.updated_at` for those rows.
- Search the codebase for every place that writes `batches.status` or
  `batches.completed_documents` and confirm whether each is transactionally
  consistent with the document write.

### Immediate mitigation
- Recompute batch status/counters from documents on read (or a repair job) until
  the write path is fixed.
- Standardise on one derivation function used everywhere.

### Permanent remediation
- **Make batch status derived, never stored**: compute `completed/failed/total`
  and status from the documents table (aggregate query, or a materialised view
  refreshed by the queue), so a single source of truth exists.
- Ensure any cached counters are **written in the same transaction** as the
  document status change that they summarise.
- Add a consistency check job that alerts when derived vs cached counters diverge.

### Risks / side effects
- Aggregates on every read add DB load (Incident 5) — mitigate with a
  materialised view / incremental counters updated in-transaction.
- Removing stored batch status changes the read model; API consumers must not
  assume it's authoritative.

### Assumptions
- Document rows are the source of truth for processing progress; batch status is
  a projection of them.

---

## Incident 7 — Reviewers are seeing missing, duplicate, or inconsistent findings

### Possible causes
- **C7.1 Duplicate findings** (root cause: Incident 4) — same variable inserted
  twice.
- **C7.2 Missing findings**: a document failed mid-way (Incident 2) or the
  variable list changed between extraction and insert; or findings were written
  but the completion transaction rolled back, and the retry path doesn't
  recreate them.
- **C7.3 Inconsistent findings**: reviewer edits/accepts a finding while a retry
  or re-extraction rewrites it (no versioning); or two different provider/model
  versions produced different values for the same document, and both exist.
- **C7.4 Review queue drift**: the "pending" list and the per-batch counter
  disagree (Incident 6), so reviewers see findings that were already reviewed,
  or miss pending ones (pagination/scan order issues).
- **C7.5 No isolation on edits**: two reviewers (or a reviewer + reaper) update
  the same finding row, last-write-wins, corrupting `reviewer`/`reviewed_at`.

### Investigation / validation
- Run the duplicate check: group findings by (document_id, variable_name)
  having count > 1.
- Compare the set of expected findings `(documents × variables)` with what's
  stored to find missing rows.
- Check for findings whose `created_at` differs from the document's
  `processed_at` (re-written later) and for multiple `model_version`/`attempt`
  values per document.
- Audit `reviewed_at` changes on the same finding id (edits vs double-write).

### Immediate mitigation
- Stop review of affected batches; run a dedup + backfill job against a snapshot
  of expected findings.
- Freeze auto-retries so the review queue stops mutating mid-review.

### Permanent remediation
- Rely on the idempotency constraint (Incident 4) to guarantee exactly-once
  findings.
- **Make findings append-only / versioned**: `findings` gets a unique
  `(document_id, variable_name, attempt)` or a version column; reviews update
  the "head" revision while the audit trail preserves history.
- Derive the review queue and counters from a single query of findings
  (Incident 6) so the reviewer's view and the batch counters cannot diverge.
- Add row-level locking or optimistic versioning on finding review updates to
  avoid last-write-wins conflicts.

### Risks / side effects
- Dedup/backfill migrations can be destructive — take snapshots and run in dry
  run first.
- Append-only findings increase storage and complicate "current value" queries;
  introduce a canonical head pointer.

### Assumptions
- Inconsistency is a consequence of the processing pipeline bugs (4/2/6), not of
  malicious reviewer behaviour.

---

## Cause-and-remediation table

| # | Symptom | Likely root cause(s) | Immediate mitigation | Permanent remediation | Primary risk of remediation |
|---|---------|----------------------|----------------------|------------------------|------------------------------|
| 1 | Large batches slow | Serialised per-batch processing; per-doc overhead; queue starvation; provider latency | Raise concurrency; process docs (not batches); raise provider timeouts | Document-level work units; real queue + autoscaling; bulk DB writes; backpressure/circuit breaker | Higher provider/DB load; duplicates if done before idempotency |
| 2 | Docs stuck `processing` | Worker death w/o claim release; no claim timeout; partial-state exceptions; session leaks | Requeue stuck docs manually; restart workers | Stuck-document reaper with threshold; heartbeats + graceful shutdown; broker visibility timeouts; transactional per-doc completion | False-positive requeues → double processing (pair with #4) |
| 3 | Provider timeouts | Provider overload from our concurrency/retries; no timeout/backoff; doc bloat; conn-pool exhaustion | Circuit breaker + bounded backoff retries; lower concurrency; cap timeouts | Provider rate limiting, content-hash caching; async provider contract (submit+webhook); tracing; capacity planning | Circuit-break stalls batches; caching can go stale |
| 4 | Docs processed twice | No claim/idempotency; reaper double-fire; at-least-once redelivery; findings outside completion txn | Halt auto-retries/reaper; manual dedup; raise visibility timeouts | Atomic lease claim (`WHERE status='queued'`); unique idempotency key on findings; transactional completion; queue dedup | Migration to unique constraints needs backfill; lease expiry complexity |
| 5 | DB load high | Per-doc write amplification; missing indexes; conn churn; lock contention; unbounded queries | Add indexes; stop per-doc counter updates; pool connections | Derived batch status (single source of truth); bulk inserts; Postgres + pooling; pagination; read replica | Derived status must stay consistent (#6); bulk inserts interact with dedup |
| 6 | Batch vs doc status mismatch | Stored batch status updated separately/non-atomically; multiple writers; duplicated derivation | Recompute from documents; single derivation function | Never store batch status — derive from documents; update any cache in the same txn as the doc change; consistency alert | Aggregate reads add load (#5); read-model change for consumers |
| 7 | Missing/dup/inconsistent findings | Consequence of 4/2/6; no versioning; review-queue drift; last-write-wins edits | Freeze retries; dedup + backfill; stop review of affected batches | Idempotency (#4); append-only/versioned findings; single derived review queue; row-level locking on review | Dedup migrations risky; append-only increases storage |

---

## Cross-cutting recommendation

Most incidents share three root causes:

1. **No atomic claim / idempotency** → fixes 4, and directly unblocks 7.
2. **Batch status stored separately from documents** → fixes 6, and reduces 5.
3. **Missing claim-timeout/reaper + at-least-once semantics** → fixes 2, and
   prevents 4.

**Suggested order of work** (defence in depth):

1. Add the **atomic lease claim + idempotency key** (Incident 4) — everything
   else (retries, concurrency, reaper) becomes safe once claims are exclusive.
2. Make **batch status derived** from documents, written atomically (Incidents 6
   and 5).
3. Add the **stuck-document reaper** with heartbeat awareness and a bounded
   retry counter (Incident 2).
4. Tune **concurrency, retries/backoff and circuit breaking** toward the
   provider (Incidents 1 and 3).
5. Introduce a real queue broker + observability (all incidents, for scale).

### Assumptions made throughout
- Documents are immutable after upload (extraction is not expected to change
  unless re-run intentionally).
- `documents` rows are the authoritative record of processing progress.
- The extraction provider is external and can be throttled but not otherwise
  controlled by us.
- Reviewer volume is low; findings are not currently subject to concurrent
  high-contention edits in the current design.
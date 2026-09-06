# Bulk Contract Processing

A Python (FastAPI) application that processes contract documents in bulk:

- A user uploads a **ZIP** containing up to 1,000 contract documents and a
  **list of variables** to extract from each document (e.g.
  `effective_date`, `monthly_rent`, `governing_law`).
- Documents are processed **asynchronously in the background** against a
  **mocked extraction API** that returns plausible random values.
- Extracted **findings** are queued for **human review** — each finding can be
  **accepted** or **rejected**.
- Batch and per-document **status** is tracked end-to-end.

Also included:
- `docs/ARCHITECTURE_AND_WORKFLOW.md` — architecture + request/workflow walkthrough, written as a FastAPI learning guide.
- `DESIGN.md` — design decisions, future-scope evolution, assumptions and trade-offs.
- `docs/PRODUCTION_CASE_STUDY.md` — the Part 2 production-issue case study.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# run the API (auto-creates ./data/contracts.db and ./data/storage/)
uvicorn app.main:app --reload --port 8000
```

Interactive API docs: http://127.0.0.1:8000/docs

## Generate a sample ZIP

```bash
python scripts/generate_sample_zip.py sample_batch.zip 5
```

## Usage

### 1. Upload a batch

```bash
curl -X POST http://127.0.0.1:8000/batches \
  -F "zip_file=@sample_batch.zip" \
  -F 'variables=["effective_date","monthly_rent","governing_law","security_deposit"]' \
  -F "name=rent-batch"
```

Returns `202 Accepted` immediately with a `batch_id`. Processing runs in the
background (a worker polls the queue and extracts per-document values).

### 2. Track the batch

```bash
curl http://127.0.0.1:8000/batches/1          # batch detail + documents
curl "http://127.0.0.1:8000/batches?status=completed"
curl http://127.0.0.1:8000/batches/1/documents/1   # one document + its findings
curl http://127.0.0.1:8000/health             # worker liveness
```

Batch status (`queued → processing → completed | partial | failed`) is
**derived** from the individual documents, so the batch view always matches the
document rows.

### 3. Review findings

```bash
# pending (un-reviewed) findings form the reviewer work queue
curl "http://127.0.0.1:8000/findings?status=pending"

# accept / reject
curl -X POST http://127.0.0.1:8000/findings/1/review \
  -H "Content-Type: application/json" \
  -d '{"decision":"accepted","reviewer":"alice"}'

curl -X POST http://127.0.0.1:8000/findings/2/review \
  -H "Content-Type: application/json" \
  -d '{"decision":"rejected","reviewer":"bob"}'
```

## API overview

| Method | Path | Description |
| ------ | ---- | ----------- |
| `POST` | `/batches` | Upload ZIP + variables (multipart). Returns `202` + `batch_id`. |
| `GET` | `/batches` | List batches (optional `?status=&limit=`). |
| `GET` | `/batches/{id}` | Batch detail incl. per-document summaries & findings. |
| `GET` | `/batches/{id}/documents/{doc_id}` | One document + its findings. |
| `GET` | `/findings` | List findings (`?status=pending&batch_id=&limit=`). |
| `POST` | `/findings/{id}/review` | `{"decision": "accepted"\|"rejected", "reviewer"}` |
| `GET` | `/health` | Worker liveness + queued batch count. |

## Configuration (env vars, see `app/config.py`)

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `DATABASE_URL` | `sqlite:///./data/contracts.db` | SQLAlchemy URL |
| `STORAGE_DIR` | `./data/storage` | Where ZIP members are extracted |
| `MAX_DOCS_PER_ZIP` | `1000` | Hard limit per ZIP (assignment constraint) |
| `MAX_DOC_SIZE_MB` | `15` | Per-document limit (assignment constraint) |
| `MAX_ZIP_SIZE_MB` | `1024` | Uploaded ZIP size limit |
| `WORKER_CONCURRENCY` | `4` | Worker concurrency hint |
| `MOCK_FAILURE_RATE` | `0.0` | `0..1` chance a mock extraction call fails |
| `MOCK_EXTRACTION_DELAY` | `0.05` | Simulated provider latency (seconds) |
| `MAX_RETRIES` | `3` | Retries per failed document |
| `QUEUE_POLL_SECONDS` | `0.5` | Worker queue poll interval |
| `STUCK_THRESHOLD_SECONDS` | `3600` | Age at which `processing` docs are requeued |

## Design notes

- **Idempotency**: a document is only processed once (claim transition
  `queued → processing`); re-running the worker or a failed batch never
  double-processes or duplicates findings.
- **Batch status is derived** from document counters — no drift between the two.
- **Stuck recovery**: documents in `processing` longer than
  `STUCK_THRESHOLD_SECONDS` are automatically requeued with a retry counter.
- **Mocked API seam**: the worker depends only on `extract(path, variables)`,
  so a real OCR/extraction provider can replace the mock without touching the
  rest of the pipeline. See `DESIGN.md`.

## Tests

```bash
pytest -q
```

Covers: ZIP validation and the 1,000-doc / 15 MB constraints, path-traversal
guards, extraction + finding creation, idempotency and retries, partial/failed
batches, the review workflow, status consistency, the background worker, and
stuck-document recovery.

## Built With

Made with [OpenCode](https://opencode.ai).
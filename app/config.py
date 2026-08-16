"""Application configuration and settings."""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class Settings(BaseModel):
    """Runtime configuration, overridable via environment variables / .env."""

    app_name: str = "Contract Bulk Processor"
    database_url: str = os.getenv(
        "DATABASE_URL", "sqlite:///./data/contracts.db"
    )
    storage_dir: Path = Path(os.getenv("STORAGE_DIR", "./data/storage"))
    max_documents_per_zip: int = int(os.getenv("MAX_DOCS_PER_ZIP", "1000"))
    max_document_size_mb: int = int(os.getenv("MAX_DOC_SIZE_MB", "15"))
    max_zip_size_mb: int = int(os.getenv("MAX_ZIP_SIZE_MB", "1024"))
    worker_concurrency: int = int(os.getenv("WORKER_CONCURRENCY", "4"))
    mock_failure_rate: float = float(os.getenv("MOCK_FAILURE_RATE", "0.0"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))
    mock_extraction_delay_seconds: float = float(
        os.getenv("MOCK_EXTRACTION_DELAY", "0.05")
    )
    stuck_threshold_seconds: int = int(
        os.getenv("STUCK_THRESHOLD_SECONDS", "3600")
    )
    queue_poll_seconds: float = float(os.getenv("QUEUE_POLL_SECONDS", "0.5"))


settings = Settings()
"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.routes import router
from .config import settings
from .database import init_db
from .services.worker import worker

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker.start()
    yield
    worker.stop()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Bulk contract-document processing with background extraction "
        "and human-in-the-loop review of findings."
    ),
    lifespan=lifespan,
)

app.include_router(router)
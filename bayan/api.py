"""FastAPI app. Skeleton only: health plus read-only reference endpoints."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .config import Settings, load_settings
from .db import init_db
from .libraries import LIBRARIES
from .models import DOCUMENT_TYPES, LANGUAGES


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(settings.database_path)
        yield

    app = FastAPI(title="BAYAN", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/libraries")
    def libraries() -> list[dict]:
        return list(LIBRARIES)

    @app.get("/api/languages")
    def languages() -> dict:
        return LANGUAGES

    @app.get("/api/document-types")
    def document_types() -> list[str]:
        return list(DOCUMENT_TYPES)

    return app


app = create_app()

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audit import router as audit_router
from app.api.demo import router as demo_router
from app.api.incidents import router as incident_router
from app.api.schemas import HealthResponse
from app.api.sessions import router
from app.core.cors import (
    DEFAULT_ALLOW_HEADERS,
    DEFAULT_ALLOW_METHODS,
    get_allow_credentials,
    get_allow_origins,
)
from app.core.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Local-dev convenience: ensure SQLite tables exist on startup.
    init_db()
    yield


app = FastAPI(title="Pramaan Exam Ecosystem", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health_check():
    return {"status": "ok"}


# Cross-origin support for the local frontend dev servers. Origins come from
# CORS_ALLOW_ORIGINS; credentials are never combined with a wildcard origin.
_allowed_origins = get_allow_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=get_allow_credentials(_allowed_origins),
    allow_methods=list(DEFAULT_ALLOW_METHODS),
    allow_headers=list(DEFAULT_ALLOW_HEADERS),
)


app.include_router(router)
app.include_router(demo_router)
app.include_router(incident_router)
app.include_router(audit_router)

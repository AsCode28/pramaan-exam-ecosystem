from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.sessions import router
from app.core.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Local-dev convenience: ensure SQLite tables exist on startup.
    init_db()
    yield


app = FastAPI(title="Pramaan Exam Ecosystem", lifespan=lifespan)


@app.get("/health")
def health_check():
    return {"status": "ok"}


app.include_router(router)
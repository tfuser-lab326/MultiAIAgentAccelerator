import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.observability import setup_observability
from app.routers import review, decision, agents

# Configure logging — level controlled by LOG_LEVEL env var (default INFO for prod).
# Set LOG_LEVEL=DEBUG in .env or container env for verbose local development.
_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(getattr(logging, _log_level, logging.INFO))

# Enable Azure Application Insights observability
setup_observability()

app = FastAPI(
    title="Prior Authorization Review API",
    description="Prior auth review powered by Azure OpenAI gpt-5.4 via Microsoft Agent Framework",
    version="0.1.0",
)

# CORS: allow the configured frontend origin. Localhost dev origins are added
# only when FRONTEND_ORIGIN itself points at localhost (i.e. local development).
_allowed_origins = [settings.FRONTEND_ORIGIN]
if "localhost" in settings.FRONTEND_ORIGIN or "127.0.0.1" in settings.FRONTEND_ORIGIN:
    _allowed_origins.extend(["http://localhost:3000", "http://127.0.0.1:3000"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(_allowed_origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(review.router, prefix="/api")
app.include_router(decision.router, prefix="/api")
app.include_router(agents.router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}

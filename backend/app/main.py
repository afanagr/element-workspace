"""FastAPI application factory, middleware and error handling."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import chat, files, health, providers
from app.config import settings
from app.llm.schemas import NoProviderAvailable
from app.logging_config import configure_logging, get_logger
from app.services.nodes import NodeError
from app.services.operations import OperationError
from app.services.storage import storage

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    storage.ensure()
    logger.info(
        "Starting %s v%s | default provider=%s model=%s | workspace=%s",
        settings.app_name,
        __version__,
        settings.llm_provider,
        settings.llm_model,
        settings.workspace_dir,
    )
    if settings.secret_key_is_insecure:
        logger.warning(
            "APP_SECRET_KEY is insecure - provider keys saved from the UI are weakly encrypted"
        )
    yield
    from app.db import dispose_engine
    from app.llm.factory import registry
    from app.services.vectorstore import vector_store

    registry.invalidate()
    await vector_store.close()
    await dispose_engine()
    logger.info("Shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.exception_handler(NodeError)
async def node_error_handler(request: Request, exc: NodeError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(OperationError)
async def operation_error_handler(request: Request, exc: OperationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(NoProviderAvailable)
async def provider_error_handler(request: Request, exc: NoProviderAvailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": exc.message, "code": "no_provider", "details": exc.details},
    )


@app.get("/api")
async def api_root() -> dict:
    return {
        "name": settings.app_name,
        "version": __version__,
        "docs": "/api/docs",
        "websocket": "/api/ws/chat/{session_id}",
    }


app.include_router(health.router)
app.include_router(files.router)
app.include_router(chat.router)
app.include_router(providers.router)

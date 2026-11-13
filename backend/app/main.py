"""FastAPI application factory, middleware and error handling."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import admin, auth, chat, files, health, providers
from app.config import settings
from app.db import session_scope
from app.llm.factory import DEFAULT_PROVIDER, PROVIDER_NAMES
from app.llm.schemas import NoProviderAvailable
from app.logging_config import configure_logging, get_logger
from app.services import users as user_service
from app.services.nodes import NodeError
from app.services.operations import OperationError
from app.services.storage import storage

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    storage.ensure()
    logger.info(
        "Starting %s v%s | providers=%s default=%s/%s | workspace=%s",
        settings.app_name,
        __version__,
        ",".join(PROVIDER_NAMES),
        DEFAULT_PROVIDER,
        settings.llm_model,
        settings.workspace_dir,
    )
    if settings.secret_key_is_insecure:
        logger.warning(
            "APP_SECRET_KEY is insecure - provider keys stored in the database are weakly encrypted"
        )
    try:
        async with session_scope() as db:
            created = await user_service.ensure_admin(db)
        if created is not None:
            logger.warning(
                "Bootstrap administrator '%s' created with the password from ADMIN_PASSWORD - "
                "change it in the UI right after the first login",
                created.username,
            )
    except Exception:  # noqa: BLE001 - the API must still start if the DB is late
        logger.exception("Could not bootstrap the administrator account")
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


@app.exception_handler(user_service.UserError)
async def user_error_handler(request: Request, exc: user_service.UserError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.get("/api")
async def api_root() -> dict:
    return {
        "name": settings.app_name,
        "version": __version__,
        "docs": "/api/docs",
        "websocket": "/api/ws/chat/{session_id}",
        "auth": "/api/auth/login",
    }


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(files.router)
app.include_router(chat.router)
app.include_router(providers.router)

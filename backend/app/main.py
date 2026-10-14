from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.staticfiles import StaticFiles

from app import audit  # noqa: F401 -- registers ORM audit guards
from app.api import audit as audit_api
from app.api import auth, csl, dashboard, parties, review, screening, settings, sources, users
from app.config import PRODUCT_NAME, Config
from app.database import create_database_engine, migrate, session_factory
from app.security import ConfiguredTrustedHostMiddleware, validate_key_binding


def create_app(config: Config | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings = config or Config()
        engine = create_database_engine(settings)
        migrate(engine)
        application.state.config = settings
        application.state.engine = engine
        application.state.session_factory = session_factory(engine)
        try:
            with application.state.session_factory() as db:
                validate_key_binding(db, settings)
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title=PRODUCT_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    application.add_middleware(ConfiguredTrustedHostMiddleware)
    application.include_router(auth.router, prefix="/api/v1")
    application.include_router(dashboard.router, prefix="/api/v1")
    application.include_router(csl.router, prefix="/api/v1")
    application.include_router(screening.router, prefix="/api/v1")
    application.include_router(sources.router, prefix="/api/v1")
    for router in (review.router, parties.router, users.router, settings.router, audit_api.router):
        application.include_router(router, prefix="/api/v1")

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/api/v1/health")
    def health(request: Request):
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            raise HTTPException(503, "Database unavailable") from None
        return {"status": "ok"}

    @application.get("/{path:path}")
    async def frontend(path: str, request: Request):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        root = request.app.state.config.frontend_dist.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404, "Not found")
        if target.is_file():
            return await StaticFiles(directory=root).get_response(path, request.scope)
        if not (root / "index.html").is_file():
            raise HTTPException(503, "Build the frontend before serving this page")
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

    return application


app = create_app()

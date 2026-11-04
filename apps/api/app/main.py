"""NYXORA API entrypoint: app factory, security middleware, lifespan wiring."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import error_payload, router
from app.config import Settings, get_settings
from app.core.tools import ToolError
from app.deps import Container, build_container
from app.models.router import weights_from_yaml
from app.security.audit import redact
from app.security.path_guard import PathGuardError
from app.security.permissions import PermissionDenied

logger = logging.getLogger("nyxora.main")
VERSION = "0.1.0"

_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "too_large",
    415: "unsupported_type",
    422: "validation_failed",
    429: "rate_limited",
}


class RateLimiter:
    """Fixed-window limiter per client. Local-first does not mean unbounded."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        window = self._hits[key]
        now = time.monotonic()
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True


def create_app(
    settings: Settings | None = None,
    *,
    force_mock: bool = False,
    require_token: bool = True,
    container: Container | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        built = container or build_container(cfg, force_mock=force_mock)
        state["container"] = built
        app.state.container = built
        await built.registry.refresh()
        try:
            config_yaml = Path("config") / "models.yaml"
            if config_yaml.exists():
                built.registry.load_yaml_overrides(config_yaml)
                weights = weights_from_yaml(config_yaml)
                if weights is not None:
                    built.router.weights = weights
        except Exception:  # noqa: BLE001
            logger.warning("models_yaml_load_failed")
        models = built.registry.all()
        logger.info(
            "nyxora_ready",
            extra={
                "models": len(models),
                "privacy_level": cfg.privacy_level,
                "workspace": str(built.guard.root),
            },
        )
        if not models:
            logger.warning("no models available - run 'ollama pull qwen2.5:3b' or set a key")
        yield
        logger.info("nyxora_stopped")

    app = FastAPI(
        title="NYXORA Universal AI",
        version=VERSION,
        description="One Intelligence. Infinite Capabilities.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["X-NYXORA-Token", "Content-Type"],
    )

    limiter = RateLimiter(cfg.rate_limit_per_min)
    token = cfg.resolved_api_token() if require_token else ""

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Health is open so onboarding can probe it before a token exists.
        if request.url.path == "/health":
            return await call_next(request)
        if require_token and request.headers.get("X-NYXORA-Token", "") != token:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "unauthorized",
                        "message": "invalid X-NYXORA-Token",
                        "details": None,
                    }
                },
            )
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(f"{client}:{request.url.path}"):
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": f"{cfg.rate_limit_per_min}/min",
                        "details": None,
                    }
                },
            )
        response = await call_next(request)
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Log the redacted shape, never the contents."""
        logger.error("unhandled", extra={"path": request.url.path, "error": str(redact(str(exc)))})
        return JSONResponse(
            status_code=500,
            content=error_payload("internal_error", "unexpected failure"),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Every HTTP failure leaves as {error:{code,message,details}}.

        Routes already build that shape; FastAPI's default would bury it under
        "detail", and a client should not have to guess which of the two it got.
        """
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            content = detail
        else:
            content = error_payload(
                _CODE_BY_STATUS.get(exc.status_code, "request_failed"), str(detail)
            )
        return JSONResponse(
            status_code=exc.status_code, content=content, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_payload(
                "validation_failed",
                "request body did not validate",
                {"errors": jsonable_encoder(exc.errors())},
            ),
        )

    @app.exception_handler(PermissionDenied)
    async def denied(request: Request, exc: PermissionDenied) -> JSONResponse:
        """The permission controller answers 403 with its reason, never a 500."""
        return JSONResponse(
            status_code=403,
            content=error_payload("permission_denied", str(exc), {"outcome": exc.outcome.value}),
        )

    @app.exception_handler(PathGuardError)
    async def path_refused(request: Request, exc: PathGuardError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_payload("path_refused", str(exc), {"violation": exc.violation.value}),
        )

    @app.exception_handler(ToolError)
    async def tool_refused(request: Request, exc: ToolError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_payload("tool_error", str(exc)),
        )

    app.include_router(router)
    return app


app = create_app()

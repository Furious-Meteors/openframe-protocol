"""
openframe/protocol/http/server.py
=====================================
`bind_query`/`bind_command` — register a FastAPI route that calls into a
`UseCase`/`CommandHandler`/`QueryHandler` (`openframe.core.inbound`)
through the shared binder, instead of a hand-written route function
calling the handler and mapping `HTTPException`s itself.

Named `server.py`, paired with this package's own `client.py` — not
`binding.py` (its original name): "binding" described the mechanism,
while "client" described a role, an inconsistent naming style for two
files sitting right next to each other. `inbound.py`/`outbound.py` was
considered and rejected — those names are already owned by
`openframe.core.inbound`/`openframe.core.ports.outbound` at a different
level of granularity, and reusing them here for a narrower, HTTP-specific
concept would create two unrelated meanings of "inbound"/"outbound"
floating around the ecosystem.

Spiked first (see the repo's own build session, not reproduced here):
confirmed FastAPI 0.143 supports (a) a Pydantic `BaseModel` as a query
dependency via `Annotated[Model, Query()]`, with each field becoming its
own query param, and (b) a dynamically-constructed `inspect.Signature`
assigned to a plain `async def (**kwargs)` endpoint, so path/body/query
parameters can all be declared generically from data rather than one
hand-written function signature per route. Both confirmed against a real
`TestClient`, not assumed from FastAPI's docs.

Deliberately not fully generic beyond what `items-postgres` needs (per
the plan's own scope line) — `build_input` is the one place route-
specific shape lives: how a path param, a query model, and a body model
combine into the single argument a handler's `execute()` expects. Adding
a new combination later means adding to `build_input`, not changing this
module.

Dependency order:
    protocol/http/binding → protocol/base (errors, context)
"""
from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Sequence

from fastapi import APIRouter, Body, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from openframe.core.exceptions import AdapterConnectionError
from openframe.core.inbound import RequestContext
from openframe.core.middleware import TelemetryMiddleware
from openframe.core.telemetry import record_error, record_lifecycle_event, setup_telemetry, shutdown_telemetry
from openframe.protocol.base.context import build_request_context
from openframe.protocol.base.errors import ErrorCategory, build_envelope, categorize

__all__ = ["bind_query", "bind_command", "install_error_handlers", "create_app"]

_logger = logging.getLogger(__name__)

_CATEGORY_TO_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.INVALID: 422,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.UNAVAILABLE: 503,
    ErrorCategory.INTERNAL: 500,
}


def _build_signature(
    *,
    path_param: tuple[str, type] | None,
    body_model: type[BaseModel] | None,
    query_model: type[BaseModel] | None,
) -> inspect.Signature:
    """
    Build the `inspect.Signature` FastAPI introspects to extract path,
    body, and query parameters for a dynamically-constructed endpoint.

    Fixed kwarg names so `build_input` can address them by name
    regardless of the route: the path param keeps its own name (it's
    also the URL template variable), the body model is always bound to
    `body`, the query model to `query`.
    """
    params: list[inspect.Parameter] = [
        inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
        inspect.Parameter("response", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Response),
    ]
    if path_param is not None:
        name, annotation = path_param
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=annotation, default=Path(...)
            )
        )
    if body_model is not None:
        params.append(
            inspect.Parameter(
                "body", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=body_model, default=Body(...)
            )
        )
    if query_model is not None:
        params.append(
            inspect.Parameter(
                "query",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=query_model,
                default=Query(),
            )
        )
    return inspect.Signature(params)


def _resolve_input(build_input: Callable[..., Any] | None, kwargs: dict[str, Any]) -> Any:
    """
    Call `build_input` with exactly the kwargs it declares, by name — so
    a route with only a path param doesn't need a `build_input` that
    also accepts `body`/`query` it'll never receive.

    `build_input=None` is valid when exactly one of path/body/query is
    present — the handler's input is just that one value directly.
    """
    available = {k: v for k, v in kwargs.items() if k not in ("request", "response", "__handler__")}
    if build_input is None:
        if len(available) == 0:
            # No path param, body, or query model — the handler takes no
            # real input (e.g. "list everything", "run this check").
            return None
        if len(available) != 1:
            raise ValueError(
                "build_input is required when more than one of path_param/body_model/"
                "query_model is set — ambiguous which one is the handler's input."
            )
        return next(iter(available.values()))
    sig = inspect.signature(build_input)
    call_kwargs = {name: available[name] for name in sig.parameters if name in available}
    return build_input(**call_kwargs)


def _bind(
    router: APIRouter,
    path: str,
    *,
    method: str,
    handler_dep: Callable[..., Any],
    path_param: tuple[str, type] | None,
    body_model: type[BaseModel] | None,
    query_model: type[BaseModel] | None,
    build_input: Callable[..., Any] | None,
    response_model: type | None,
    status_code: int,
    not_found_when: Callable[[Any], bool] | None,
    not_found_detail: Callable[[Any], str] | None,
) -> None:
    from fastapi import Depends

    async def endpoint(**kwargs: Any) -> Any:
        request: Request = kwargs["request"]
        response: Response = kwargs["response"]
        handler = kwargs["__handler__"]
        handler_input = _resolve_input(build_input, kwargs)
        context: RequestContext = build_request_context(dict(request.headers))
        response.headers["X-Correlation-ID"] = context.correlation_id
        result = await handler.execute(handler_input, context)
        if not_found_when is not None and not_found_when(result):
            detail = not_found_detail(handler_input) if not_found_detail is not None else "not found"
            raise HTTPException(status_code=404, detail=detail)
        return result

    signature = _build_signature(path_param=path_param, body_model=body_model, query_model=query_model)
    # Append the handler dependency as its own parameter so FastAPI resolves
    # it per-request via Depends(), same lifecycle as every other dependency.
    extra = list(signature.parameters.values())
    extra.append(
        inspect.Parameter(
            "__handler__",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=Any,
            default=Depends(handler_dep),
        )
    )
    endpoint.__signature__ = inspect.Signature(extra)  # type: ignore[attr-defined]

    router.add_api_route(
        path,
        endpoint,
        methods=[method],
        response_model=response_model,
        status_code=status_code,
    )


def bind_query(
    router: APIRouter,
    path: str,
    *,
    handler_dep: Callable[..., Any],
    path_param: tuple[str, type] | None = None,
    query_model: type[BaseModel] | None = None,
    build_input: Callable[..., Any] | None = None,
    response_model: type | None = None,
    status_code: int = 200,
    not_found_when: Callable[[Any], bool] | None = None,
    not_found_detail: Callable[[Any], str] | None = None,
) -> None:
    """
    Register a GET route backed by a `QueryHandler`/`UseCase`.

    Args:
        router:         The `APIRouter` to register onto.
        path:           Route path, e.g. `"/items/{item_id}"`.
        handler_dep:    A FastAPI dependency (plain callable or
                        `Depends`-wrapped) resolving to the handler
                        instance for this request.
        path_param:     `(name, type)` if the route has a path parameter,
                        else `None`.
        query_model:    A `BaseModel` whose fields become query params
                        (via `Annotated[Model, Query()]`), bound to the
                        `query` kwarg — spiked and confirmed against a
                        real `TestClient` to work in FastAPI 0.143.
        build_input:    Combines whichever of `path_param`/`query_model`
                        are present into the handler's single `execute()`
                        argument. Required only when both are present —
                        with exactly one, that one value is passed as-is.
        response_model: FastAPI response model, as with a hand-written route.
        status_code:    Defaults to 200 (query semantics).
        not_found_when: If the result satisfies this, raise 404 instead
                        of returning it — e.g. `lambda item: item is None`.
        not_found_detail: Builds the 404 body's `detail` string from the
                        handler's own input (e.g. `lambda item_id: f"Item
                        {item_id!r} not found"`), for a route whose
                        existing behavior put the id in the message —
                        a real second service needed this (`items-cached`,
                        migrated from a hand-written route that did
                        exactly this); defaults to the generic `"not
                        found"` previous routes used when omitted.
    """
    _bind(
        router,
        path,
        method="GET",
        handler_dep=handler_dep,
        path_param=path_param,
        body_model=None,
        query_model=query_model,
        build_input=build_input,
        response_model=response_model,
        status_code=status_code,
        not_found_when=not_found_when,
        not_found_detail=not_found_detail,
    )


def bind_command(
    router: APIRouter,
    path: str,
    *,
    method: str,
    handler_dep: Callable[..., Any],
    path_param: tuple[str, type] | None = None,
    body_model: type[BaseModel] | None = None,
    query_model: type[BaseModel] | None = None,
    build_input: Callable[..., Any] | None = None,
    response_model: type | None = None,
    status_code: int = 200,
    not_found_when: Callable[[Any], bool] | None = None,
    not_found_detail: Callable[[Any], str] | None = None,
) -> None:
    """
    Register a POST/PUT/PATCH/DELETE route backed by a `UseCase`/`CommandHandler`.

    Same parameter shape as `bind_query`, plus `body_model` (bound to the
    `body` kwarg) and an explicit `method` since commands aren't always
    one HTTP verb. A `CommandHandler` always returns `None` — a route
    whose result the caller needs (the created entity, a deleted-count
    bool) needs a `UseCase`, not a `CommandHandler`; this binder doesn't
    care which protocol the concrete handler satisfies, both have the
    same `execute()` shape.

    `query_model` is supported here too, not just on `bind_query` — a
    real second service (not `items-postgres`) needed a command whose
    input is a path param plus a query param and no body at all (e.g.
    `PATCH /{id}/status?status=...`), which `_bind` already handled
    generically; only this public wrapper hadn't exposed it yet.

    Args: see `bind_query` — identical except `body_model`/`method`.
    """
    _bind(
        router,
        path,
        method=method,
        handler_dep=handler_dep,
        path_param=path_param,
        body_model=body_model,
        query_model=query_model,
        build_input=build_input,
        response_model=response_model,
        status_code=status_code,
        not_found_when=not_found_when,
        not_found_detail=not_found_detail,
    )


def install_error_handlers(app: Any) -> None:
    """
    Register FastAPI exception handlers mapping every error through the
    shared `categorize()`/`build_envelope()` — the one place this
    translation happens, instead of each route catching its own
    exceptions.

    Calls `record_error()` on every INTERNAL-category error (5xx) — the
    built-in `TelemetryMiddleware` only records errors that escape all
    the way past it unhandled; an exception handler, by design, catches
    the error *before* it would otherwise propagate that far, so without
    this call here an INTERNAL error would be correctly hidden from the
    caller but silently never recorded anywhere.
    """
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from openframe.core.exceptions import OpenFrameError

    app = app  # type: FastAPI

    def _respond(exc: BaseException, correlation_id: str | None) -> JSONResponse:
        envelope = build_envelope(exc, correlation_id=correlation_id)
        category = categorize(exc)
        status = _CATEGORY_TO_STATUS[category]
        if category is ErrorCategory.INTERNAL:
            record_error(exc if isinstance(exc, Exception) else Exception(str(exc)))
        headers = {}
        if envelope.retryable:
            headers["Retry-After"] = "1"
        return JSONResponse(
            status_code=status,
            headers=headers,
            content={
                "error": {
                    "code": envelope.code,
                    "message": envelope.message,
                    "retryable": envelope.retryable,
                    "correlation_id": envelope.correlation_id,
                }
            },
        )

    @app.exception_handler(OpenFrameError)
    async def _handle_openframe_error(request: Request, exc: OpenFrameError) -> JSONResponse:
        context = build_request_context(dict(request.headers))
        return _respond(exc, context.correlation_id)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        context = build_request_context(dict(request.headers))
        return _respond(exc, context.correlation_id)

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        context = build_request_context(dict(request.headers))
        return _respond(exc, context.correlation_id)


# ---------------------------------------------------------------------------
# Server/app creation — the piece M1/M1b deliberately left outside protocol
# ---------------------------------------------------------------------------
#
# M1's own design notes (see this module's own history) drew the boundary at
# "protocol configures routes and error handling on a server object the
# caller already created" — FastAPI() instantiation, lifespan, middleware,
# and the uvicorn process stayed hand-written per service. That produced the
# exact same ~15-line lifespan block duplicated across every
# openframe-local-validation-framework service: setup_telemetry(),
# record_lifecycle_event("cold_start"), a try/except around the service's
# own initialise() catching (AdapterConnectionError, ValidationError) for
# degraded-mode startup, yield, shutdown(), shutdown_telemetry(). That's
# real, demonstrated duplication across seven real services, not a
# hypothetical one worth abstracting pre-emptively — so `create_app` now
# owns exactly that lifespan shape, parameterised by the one thing that
# actually differs per service: the startup/shutdown hooks themselves.
#
# Deliberately NOT owning what on_startup/on_shutdown do internally (which
# plugins to register, how many adapters, whether a background consumer
# task needs to start) — that stays each service's own `dependencies.py`,
# passed in as a plain callable. A service with extra lifespan needs (e.g.
# events-kafka's/research-pipeline's background Kafka consumer task) just
# does that extra work inside its own on_startup/on_shutdown; create_app
# doesn't need to know a consumer task exists at all.


def create_app(
    *,
    title: str,
    description: str | None = None,
    on_startup: Callable[[], Awaitable[None]],
    on_shutdown: Callable[[], Awaitable[None]],
    routers: Sequence[APIRouter] = (),
    degraded_mode_exceptions: tuple[type[BaseException], ...] = (AdapterConnectionError, ValidationError),
) -> FastAPI:
    """
    Build a `FastAPI` app with the lifespan/middleware/error-handler shape
    every `openframe-local-validation-framework` service hand-wrote
    identically: telemetry setup on startup, degraded-mode tolerance for a
    not-yet-ready backend, telemetry flush on shutdown, `TelemetryMiddleware`
    installed, and `install_error_handlers` wired — all before the caller's
    own routers are included.

    Does NOT start uvicorn, does NOT decide what `on_startup`/`on_shutdown`
    actually do (that's still each service's own `dependencies.initialise`/
    `shutdown`, built from whichever `openframe-adapters` plugins it needs)
    — this function's job ends at handing back a configured `FastAPI`
    instance, same as it always has; the caller still does
    `uvicorn.run(app, ...)` or the Dockerfile's `CMD` does.

    Args:
        title:        Passed straight to `FastAPI(title=...)`.
        description:  Passed straight to `FastAPI(description=...)`.
        on_startup:   Awaited inside the lifespan's startup phase. Any
                      exception in `degraded_mode_exceptions` is caught and
                      logged as a warning (degraded mode, not a crash) —
                      the exact same tolerance every service's own lifespan
                      hand-wrote around its own `initialise()` call.
        on_shutdown:  Awaited on app shutdown, after the lifespan yields.
        routers:      Included via `app.include_router()` after creation —
                      a service with more than one router (rare, but not
                      impossible) passes all of them here rather than
                      calling `app.include_router()` itself afterward.
        degraded_mode_exceptions: Which exception types during `on_startup`
                      are tolerated (logged, not raised) rather than
                      crashing app startup. Defaults to the exact pair
                      every migrated service already used:
                      `AdapterConnectionError` (backend not reachable yet)
                      and pydantic's `ValidationError` (bad settings).

    Returns:
        A `FastAPI` instance with `TelemetryMiddleware` added,
        `install_error_handlers` wired, and every router in `routers`
        included — ready for the caller to add any extra non-domain routes
        (e.g. a `/health` or `/registry` endpoint) directly, exactly as
        every migrated service still does today.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_telemetry()
        record_lifecycle_event("cold_start")
        try:
            await on_startup()
        except degraded_mode_exceptions as exc:
            _logger.warning(
                "%s: dependency not ready at startup: %s — running in degraded mode", title, exc
            )
        yield
        await on_shutdown()
        shutdown_telemetry()

    app = FastAPI(title=title, description=description, lifespan=lifespan)
    app.add_middleware(TelemetryMiddleware)
    install_error_handlers(app)
    for router in routers:
        app.include_router(router)
    return app

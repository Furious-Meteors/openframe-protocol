"""
openframe/protocol/grpc/server.py
=====================================
`bind_service` — register a `grpc.aio` servicer method that calls into a
`UseCase`/`CommandHandler`/`QueryHandler` (`openframe.core.inbound`)
through the shared binder, the gRPC counterpart of `-http`'s
`bind_query`/`bind_command`.

One generic RPC (`GenericService.Call`) instead of one per route, mirroring
`-http`'s own `inspect.Signature` trick for avoiding a per-route function
signature: the wire schema (`GenericRequest`/`GenericResponse`, both just
`{json_payload: string}`) doesn't need to know the domain type, only that
a JSON string travels inside it. The actual handler is selected by
`invocation_metadata()`'s `"x-openframe-route"` key, decoded on the way
in — the gRPC equivalent of an HTTP path.

No global interceptor. A wrapping function around the generated
servicer's `Call` method achieves the same effect `install_error_handlers`
does for HTTP — catch, categorize, `context.abort()` — without depending
on `grpc.aio`'s own interceptor API, which was not spiked and is not
needed for what this binder does.

Spiked first (see `PLAN.md`'s "M4 — gRPC" section): confirmed `grpc.aio`
works end to end (async server + channel), `context.abort(code, details)`
propagates to the client as `AioRpcError.code()`/`.details()`, and
`context.invocation_metadata()` carries key-value pairs exactly like HTTP
headers — so `build_request_context` (built for a header-like `Mapping`)
works unchanged here, no gRPC-specific context builder needed.

Dependency order:
    protocol/grpc/server → protocol/base (errors, context), protocol/grpc (generic_pb2*)
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

import grpc
from pydantic import BaseModel, TypeAdapter, ValidationError

from openframe.core.exceptions import AdapterConnectionError
from openframe.core.inbound import RequestContext
from openframe.core.telemetry import record_error, record_lifecycle_event, setup_telemetry, shutdown_telemetry
from openframe.protocol.base.context import build_request_context
from openframe.protocol.base.errors import ErrorCategory, build_envelope, categorize
from openframe.protocol.grpc import generic_pb2, generic_pb2_grpc

__all__ = ["bind_service", "ROUTE_METADATA_KEY", "create_server"]

_logger = logging.getLogger(__name__)

_CATEGORY_TO_STATUS_CODE: dict[ErrorCategory, grpc.StatusCode] = {
    ErrorCategory.NOT_FOUND: grpc.StatusCode.NOT_FOUND,
    ErrorCategory.INVALID: grpc.StatusCode.INVALID_ARGUMENT,
    ErrorCategory.TIMEOUT: grpc.StatusCode.DEADLINE_EXCEEDED,
    ErrorCategory.UNAVAILABLE: grpc.StatusCode.UNAVAILABLE,
    ErrorCategory.INTERNAL: grpc.StatusCode.INTERNAL,
}

ROUTE_METADATA_KEY = "x-openframe-route"


class _RouteRegisteringServicer(generic_pb2_grpc.GenericServiceServicer):
    """
    One servicer instance per `grpc.aio.Server`, holding every route
    `bind_service` has registered on it so far — the gRPC equivalent of
    an `APIRouter` accumulating routes before `app.include_router()`.
    """

    def __init__(self) -> None:
        self._routes: dict[str, Callable[[Any, grpc.aio.ServicerContext], Any]] = {}

    def register(self, route: str, handler: Callable[[Any, grpc.aio.ServicerContext], Any]) -> None:
        self._routes[route] = handler

    async def Call(
        self, request: "generic_pb2.GenericRequest", context: grpc.aio.ServicerContext
    ) -> "generic_pb2.GenericResponse":
        metadata = dict(context.invocation_metadata())
        route = metadata.get(ROUTE_METADATA_KEY)
        handler = self._routes.get(route)
        if handler is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"no route bound for {route!r}")
            return  # unreachable: abort() always raises
        return await handler(request, context)


def _get_or_create_servicer(server: grpc.aio.Server) -> _RouteRegisteringServicer:
    """
    Reuse the single `_RouteRegisteringServicer` already added to *server*,
    if `bind_service` has been called on it before — otherwise create and
    register one. Mirrors how multiple `bind_query`/`bind_command` calls
    share one `APIRouter` without each creating its own route table.
    """
    existing = getattr(server, "_openframe_generic_servicer", None)
    if existing is not None:
        return existing
    servicer = _RouteRegisteringServicer()
    generic_pb2_grpc.add_GenericServiceServicer_to_server(servicer, server)
    server._openframe_generic_servicer = servicer  # type: ignore[attr-defined]
    return servicer


def _to_jsonable(value: Any) -> Any:
    """
    Recursively converts a handler's result into something `json.dumps`
    can actually serialise. Needed because handlers built for `-http`
    (where FastAPI's own response_model machinery already handles this)
    routinely return `BaseModel` instances or plain containers of them —
    e.g. `GetItem` returning an `Item`, or `ListItems` returning
    `{"items": [Item, ...], "total": N}` — and `json.dumps` has no idea
    what to do with a `BaseModel` on its own.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def bind_service(
    server: grpc.aio.Server,
    route: str,
    *,
    handler_factory: Callable[[], Any],
    input_model: Any = None,
    not_found_when: Callable[[Any], bool] | None = None,
) -> None:
    """
    Register one route's handler on *server*, callable by a client that
    sends `route` in its `x-openframe-route` metadata.

    Args:
        server:          The `grpc.aio.Server` to register onto. Safe to
                         call this more than once for the same server —
                         each call adds one route to a shared servicer,
                         the same relationship `bind_query`/`bind_command`
                         have to one `APIRouter`.
        route:           Logical route name, e.g. `"get_item"` — the
                         gRPC equivalent of an HTTP path. Not a URL
                         template; gRPC has no path params, so the
                         handler's own input carries whatever an HTTP
                         path param would have (see `client.py`).
        handler_factory: Builds a fresh handler instance for this call —
                         the gRPC equivalent of a FastAPI dependency.
                         Called once per request, no caching, matching
                         `handler_dep`'s own per-request lifecycle in
                         `-http`'s `server.py`.
        input_model:     The gRPC counterpart of `-http`'s `body_model` —
                         a real need, not a speculative one: handlers
                         built for `-http` (e.g. `CreateItem.execute(self,
                         item: Item, ...)`) expect a validated domain
                         object, but the decoded `json_payload` is just a
                         plain dict/list with no casting of its own
                         (unlike FastAPI, which already validates a
                         `body_model` before the handler ever sees it).
                         Any type `pydantic.TypeAdapter` accepts — a
                         `BaseModel` subclass, `list[Item]`, etc. `None`
                         (the default) passes the decoded value through
                         unchanged, for handlers whose input is already a
                         plain dict/str/int/list of primitives.
        not_found_when:  If the handler's result satisfies this, abort
                         with `NOT_FOUND` instead of returning it.
    """
    servicer = _get_or_create_servicer(server)
    adapter = TypeAdapter(input_model) if input_model is not None else None

    async def _handle(
        request: "generic_pb2.GenericRequest", context: grpc.aio.ServicerContext
    ) -> "generic_pb2.GenericResponse":
        handler = handler_factory()
        metadata = dict(context.invocation_metadata())
        request_context: RequestContext = build_request_context(metadata)
        context.set_trailing_metadata((("x-correlation-id", request_context.correlation_id),))

        try:
            raw_input = json.loads(request.json_payload) if request.json_payload else None
            # Validation errors from an invalid input_model cast go through
            # the same categorize()/build_envelope() path as a handler's
            # own exception — matching how FastAPI's RequestValidationError
            # (from an invalid body_model) categorizes as INVALID on -http,
            # rather than crashing uncaught before context.abort() runs.
            handler_input = (
                adapter.validate_python(raw_input) if adapter is not None and raw_input is not None else raw_input
            )
            result = await handler.execute(handler_input, request_context)
        except BaseException as exc:
            await _abort_for_exception(context, exc, request_context.correlation_id)
            return  # unreachable: _abort_for_exception always raises

        if not_found_when is not None and not_found_when(result):
            await context.abort(grpc.StatusCode.NOT_FOUND, "not found")
            return  # unreachable

        return generic_pb2.GenericResponse(json_payload=json.dumps(_to_jsonable(result)))

    servicer.register(route, _handle)


async def _abort_for_exception(
    context: grpc.aio.ServicerContext, exc: BaseException, correlation_id: str
) -> None:
    """
    Map *exc* through the shared `categorize()`/`build_envelope()` and
    `context.abort()` — the gRPC equivalent of `install_error_handlers`'
    exception handlers for HTTP. One function instead of per-exception-
    type handlers, since `context.abort()` is the one call site that
    needs to run regardless of which category the error mapped to.

    Calls `record_error()` for an INTERNAL-category error, for the same
    reason `install_error_handlers` does: `context.abort()` raises
    immediately and never reaches any outer recorder on its own.
    """
    category = categorize(exc)
    envelope = build_envelope(exc, correlation_id=correlation_id)
    if category is ErrorCategory.INTERNAL:
        record_error(exc if isinstance(exc, Exception) else Exception(str(exc)))
    status_code = _CATEGORY_TO_STATUS_CODE[category]
    details = json.dumps(
        {
            "error": {
                "code": envelope.code,
                "message": envelope.message,
                "retryable": envelope.retryable,
                "correlation_id": envelope.correlation_id,
            }
        }
    )
    await context.abort(status_code, details)


# ---------------------------------------------------------------------------
# Server creation/lifecycle — the gRPC counterpart of -http's create_app
# ---------------------------------------------------------------------------
#
# Named `create_server` for naming symmetry with `-http`'s `create_app`, but
# shaped differently on purpose, not by oversight: FastAPI's app object is
# just configuration handed off to an external process (uvicorn) that owns
# the actual accept loop, so `create_app` can hand back a plain `FastAPI`
# instance and be done. `grpc.aio.Server` has no external process to hand
# off to — THIS process owns start()/stop() directly, and routes
# (`bind_service` calls) must be registered before `start()`, not after.
# An async context manager is the shape that actually fits that lifecycle,
# not a plain factory function returning an unstarted server.


@asynccontextmanager
async def create_server(
    *,
    address: str,
    configure: Callable[["grpc.aio.Server"], None],
    on_startup: Callable[[], Awaitable[None]],
    on_shutdown: Callable[[], Awaitable[None]],
    degraded_mode_exceptions: tuple[type[BaseException], ...] = (AdapterConnectionError, ValidationError),
) -> AsyncIterator["grpc.aio.Server"]:
    """
    Async context manager yielding a started `grpc.aio.Server`, with the
    same telemetry-setup / degraded-mode-startup / telemetry-shutdown shape
    `-http`'s `create_app` gives FastAPI — the gRPC counterpart of the
    lifespan block every validation-framework HTTP service hand-wrote
    identically.

    Args:
        address:     Passed to `server.add_insecure_port()`, e.g. `"[::]:50051"`.
        configure:   Called with the bare `grpc.aio.server()` instance
                     before `on_startup`/`start()` — this is where the
                     caller's own `bind_service(server, ...)` calls belong,
                     since gRPC requires routes registered before start().
        on_startup:  Awaited after `configure`, before `add_insecure_port`/
                     `start()`. Any exception in `degraded_mode_exceptions`
                     is caught and logged as a warning rather than
                     preventing the server from starting — the same
                     tolerance `create_app` gives a not-yet-ready backend.
        on_shutdown: Awaited after the `with` block exits, before
                     `server.stop()`.
        degraded_mode_exceptions: Same default pair `create_app` uses.

    Usage::

        async with create_server(
            address="[::]:50051",
            configure=lambda server: bind_service(server, "get_item", handler_factory=get_get_item_handler),
            on_startup=dependencies.initialise,
            on_shutdown=dependencies.shutdown,
        ) as server:
            await server.wait_for_termination()
    """
    setup_telemetry()
    record_lifecycle_event("cold_start")

    server = grpc.aio.server()
    configure(server)

    try:
        await on_startup()
    except degraded_mode_exceptions as exc:
        _logger.warning("dependency not ready at startup: %s — running in degraded mode", exc)

    port = server.add_insecure_port(address)
    # Stashed the same way bind_service's own _get_or_create_servicer already
    # stashes _openframe_generic_servicer on the server — the real bound
    # port matters when address ends in ":0" (an OS-assigned port, the
    # pattern every other conformance test in this repo uses for an
    # isolated, parallel-safe test server). A fixed real address doesn't
    # need this at all; it's here for exactly that ephemeral-port case.
    server._openframe_bound_port = port  # type: ignore[attr-defined]
    await server.start()
    try:
        yield server
    finally:
        await on_shutdown()
        await server.stop(None)
        shutdown_telemetry()

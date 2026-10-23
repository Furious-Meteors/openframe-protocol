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

`bind_service_stream` + `GrpcSession` — the `SessionHandler` (ADR-008)
binding, `CallStream`'s bidirectional-streaming counterpart to `Call`.
Several real spikes against a live `grpc.aio` server/channel confirmed,
not assumed: (1) `context.abort()` before the first `yield` produces a
clean client-side error with zero messages, same as `Call`'s own reject
path; (2) `invocation_metadata()` is readable before consuming
`request_iterator`, so `build_request_context` still runs exactly once
per session; (3) a genuine abrupt disconnect reliably flips
`context.cancelled()` to `True` — but only after a real, confirmed
propagation delay (the underlying HTTP/2 GOAWAY frame being processed; a
single `asyncio.sleep(0)` tick was confirmed NOT enough, a ~100ms poll
was); (4) **a real race SSE/WebSocket never had**: `request_iterator`
(inbound) and the method's own `yield` loop (outbound) are two
independently-scheduled signals for the same disconnect, and
`request_iterator` reliably (not just occasionally) resolves via plain
`StopAsyncIteration` *before* `context.cancelled()` reflects the same
disconnect — confirmed consistent across multiple spike shapes, not a
one-off timing fluke. Because of (4), `bind_service_stream` does **not**
reuse `openframe.protocol.base.session.run_opened_session` wholesale —
it still uses `open_session` (confirmed correct for the pre-yield reject
timing), but the inbound dispatch task itself polls `context.cancelled()`
for a short window right after its own loop ends normally, before
trusting that as `CloseReason.COMPLETED` rather than
`CloseReason.CLIENT_DISCONNECT` (see finding 3) — then calls
`session.close()` itself either way, so the outer `yield`-loop's own
sentinel-wait unblocks. The outer loop's own `except asyncio.CancelledError`
remains as a second, independent path for a disconnect that reaches
*it* directly (reading `session.close_reason` back out afterward — the
same pattern already proven for SSE's own `bind_session`, for the same
reason: a self-closing handler's reason must survive being read back
after the fact). `SseSession`/`bind_session` already don't use
`run_opened_session` either (no inbound events at all) — gRPC is a
second, differently-shaped case where the shared primitive's ownership
model doesn't fit, confirmed by testing, not assumed.

Dependency order:
    protocol/grpc/server → protocol/base (errors, context, session), protocol/grpc (generic_pb2*)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from openframe.core.exceptions import AdapterConnectionError
from openframe.core.inbound import CloseReason, RequestContext, SessionHandler
from openframe.core.telemetry import (
    record_error,
    record_lifecycle_event,
    setup_telemetry,
    shutdown_telemetry,
)
from openframe.protocol.base.context import ContextEnricher, build_request_context
from openframe.protocol.base.errors import ErrorCategory, build_envelope, categorize
from openframe.protocol.base.session import open_session, send_error_event
from openframe.protocol.grpc import generic_pb2, generic_pb2_grpc
from pydantic import BaseModel, TypeAdapter, ValidationError

import grpc

__all__ = ["ROUTE_METADATA_KEY", "GrpcSession", "bind_service", "bind_service_stream", "create_server"]

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
    `bind_service`/`bind_service_stream` has registered on it so far —
    the gRPC equivalent of an `APIRouter` accumulating routes before
    `app.include_router()`.

    Two separate route tables — `Call` (unary) and `CallStream`
    (bidirectional streaming) are different RPC methods on the same
    `GenericService`, each with its own `x-openframe-route` namespace;
    nothing stops a service from binding the same route name to both
    (they're independent dispatch tables, not a shared one).
    """

    def __init__(self) -> None:
        self._routes: dict[str, Callable[[Any, grpc.aio.ServicerContext], Any]] = {}
        self._stream_routes: dict[str, Callable[[Any, grpc.aio.ServicerContext], Any]] = {}

    def register(self, route: str, handler: Callable[[Any, grpc.aio.ServicerContext], Any]) -> None:
        self._routes[route] = handler

    def register_stream(self, route: str, handler: Callable[[Any, grpc.aio.ServicerContext], Any]) -> None:
        self._stream_routes[route] = handler

    async def Call(
        self, request: generic_pb2.GenericRequest, context: grpc.aio.ServicerContext
    ) -> generic_pb2.GenericResponse:
        metadata = dict(context.invocation_metadata())
        route: str | None = metadata.get(ROUTE_METADATA_KEY)
        handler = self._routes.get(route) if route is not None else None
        if handler is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"no route bound for {route!r}")
            raise AssertionError("unreachable: context.abort() always raises")
        return await handler(request, context)

    async def CallStream(
        self, request_iterator: AsyncIterator[generic_pb2.GenericRequest], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[generic_pb2.GenericResponse]:
        metadata = dict(context.invocation_metadata())
        route: str | None = metadata.get(ROUTE_METADATA_KEY)
        handler = self._stream_routes.get(route) if route is not None else None
        if handler is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"no stream route bound for {route!r}")
            return  # unreachable: abort() always raises — bare return is correct here, this is an async generator (CallStream), not a function returning GenericResponse
        async for response in handler(request_iterator, context):
            yield response


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
    context_enricher: ContextEnricher | None = None,
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
        request: generic_pb2.GenericRequest, context: grpc.aio.ServicerContext
    ) -> generic_pb2.GenericResponse:
        handler = handler_factory()
        metadata = dict(context.invocation_metadata())
        request_context: RequestContext = build_request_context(metadata, context_enricher=context_enricher)
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
        except asyncio.CancelledError:
            # A cancellation (client disconnect, server shutdown) is not a
            # handler failure — let it propagate so grpc.aio's own
            # cancellation handling runs, instead of reporting it to the
            # client as an INTERNAL error via _abort_for_exception.
            raise
        except Exception as exc:
            await _abort_for_exception(context, exc, request_context.correlation_id)
            raise AssertionError("unreachable: _abort_for_exception always raises")

        if not_found_when is not None and not_found_when(result):
            await context.abort(grpc.StatusCode.NOT_FOUND, "not found")
            raise AssertionError("unreachable: context.abort() always raises")

        return generic_pb2.GenericResponse(json_payload=json.dumps(_to_jsonable(result)))

    servicer.register(route, _handle)


_STREAM_SENTINEL = object()


class GrpcSession:
    """
    `Session[TOut]` backed by an `asyncio.Queue` — `send()` enqueues, the
    `CallStream` servicer method's own generator loop (`bind_service_stream`)
    dequeues and yields a `GenericResponse`. Structurally identical to
    `openframe.protocol.http.sse.SseSession`'s own queue — needed for the
    same reason: the handler (via `on_open`/`on_event`, possibly from a
    background task) and the generator loop `grpc.aio` actually drives are
    two different coroutines that need to hand items between them.

    `close()` is idempotent and records the *first* reason it was called
    with, in :attr:`close_reason` — `bind_service_stream` reads this back
    after its own loop ends, rather than tracking a separate local
    variable, since more than one path (the handler itself, a dispatch
    error, or a confirmed client disconnect) can be the one that actually
    decided the session was over.
    """

    def __init__(self, *, context: RequestContext) -> None:
        self.context = context
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False
        self.close_reason: CloseReason | None = None

    async def send(self, event: Any) -> None:
        if self._closed:
            raise RuntimeError("cannot send on a closed session")
        await self._queue.put(event)

    async def close(self, reason: CloseReason = CloseReason.COMPLETED) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_reason = reason
        await self._queue.put(_STREAM_SENTINEL)


def bind_service_stream(
    server: grpc.aio.Server,
    route: str,
    *,
    handler_factory: Callable[[], SessionHandler],
    input_model: Any = None,
    context_enricher: ContextEnricher | None = None,
) -> None:
    """
    Register one bidirectional-streaming route's `SessionHandler` on
    *server*, callable by a client that sends `route` in its
    `x-openframe-route` metadata on a `CallStream` call — the streaming
    counterpart to `bind_service`.

    `on_open` runs — and can reject — before this method's first `yield`,
    mirroring `bind_service`'s own `_abort_for_exception` path exactly
    (a reject here is just as uncommitted as a unary `Call` failure,
    confirmed by spiking `context.abort()` before any `yield`). `on_event`
    is dispatched from a separate task than the one `yield`ing responses,
    since inbound (`request_iterator`) and outbound (this method's own
    `yield` loop) are driven independently by `grpc.aio` — see this
    module's own docstring for the real race that forced this shape
    rather than reusing `openframe.protocol.base.session.run_opened_session`
    wholesale.

    Args:
        server:          The `grpc.aio.Server` to register onto.
        route:           Logical route name — independent of `bind_service`'s
                         own route namespace (see `_RouteRegisteringServicer`'s
                         own docstring).
        handler_factory: Builds a fresh `SessionHandler` instance for this
                         call. Called once per call, no caching.
        input_model:     Same as `bind_service`'s own `input_model` — casts
                         each decoded inbound event before `on_event` sees
                         it. `None` passes the decoded value through
                         unchanged.
    """
    servicer = _get_or_create_servicer(server)
    adapter = TypeAdapter(input_model) if input_model is not None else None

    async def _handle_stream(
        request_iterator: AsyncIterator[generic_pb2.GenericRequest], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[generic_pb2.GenericResponse]:
        metadata = dict(context.invocation_metadata())
        session = GrpcSession(context=build_request_context(metadata, context_enricher=context_enricher))
        handler = handler_factory()

        try:
            await open_session(handler, session)
        except Exception as exc:
            await _abort_for_exception(context, exc, session.context.correlation_id)
            return  # unreachable: _abort_for_exception always raises

        async def inbound_events():
            async for request in request_iterator:
                raw = json.loads(request.json_payload) if request.json_payload else None
                yield adapter.validate_python(raw) if adapter is not None and raw is not None else raw

        async def _dispatch() -> None:
            """
            Consumes inbound events, dispatching to `on_event`. Does NOT
            call `on_close` — only the outer loop below does, since it's
            the one thing `grpc.aio` reliably cancels on a real disconnect
            (confirmed by spiking; `request_iterator` ending on its own is
            a separate, independently-scheduled signal that can race it).
            Calls `session.close()` itself at its own natural end points
            so the outer loop's sentinel-wait unblocks either way.
            """
            try:
                async for event in inbound_events():
                    await handler.on_event(session, event)
                # request_iterator ending (confirmed by spiking, not
                # assumed) is NOT on its own proof the client finished
                # gracefully — grpc.aio resolves it via plain
                # StopAsyncIteration *deterministically faster* than it
                # delivers context.cancelled() for the exact same
                # disconnect, confirmed consistent across multiple real
                # spikes, not a one-off race. A short poll (confirmed
                # needed: a single asyncio.sleep(0) tick was NOT enough —
                # context.cancelled() only flips after the underlying
                # HTTP/2 GOAWAY frame is fully processed, visibly a few ms
                # later in practice) gives that slower signal a real
                # chance to land before trusting "ended normally" as
                # actually meaning COMPLETED rather than CLIENT_DISCONNECT.
                for _ in range(20):
                    if context.cancelled():
                        break
                    await asyncio.sleep(0.005)
                if context.cancelled():
                    await session.close(CloseReason.CLIENT_DISCONNECT)
                else:
                    await session.close(CloseReason.COMPLETED)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await send_error_event(session, exc)
                await session.close(CloseReason.ERROR)

        dispatch_task = asyncio.create_task(_dispatch())
        try:
            while True:
                item = await session._queue.get()
                if item is _STREAM_SENTINEL:
                    break
                yield generic_pb2.GenericResponse(json_payload=json.dumps(_to_jsonable(item)))
        except asyncio.CancelledError:
            # The one place this binder learns about a real disconnect,
            # confirmed reliable by spiking. A no-op if the session was
            # already closed for some other reason (session.close() is
            # idempotent) — first signal wins.
            await session.close(CloseReason.CLIENT_DISCONNECT)
            raise
        finally:
            if not dispatch_task.done():
                dispatch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dispatch_task
            # session.close_reason, not a locally-tracked variable — the
            # same pattern already proven for SSE's own bind_session, for
            # the same reason: whoever actually closed the session first
            # (the handler itself, a dispatch error, or this disconnect
            # branch) decided the real reason, not whichever code path
            # happens to be reading it back out.
            await handler.on_close(session, session.close_reason or CloseReason.COMPLETED)

    servicer.register_stream(route, _handle_stream)


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
    configure: Callable[[grpc.aio.Server], None],
    on_startup: Callable[[], Awaitable[None]],
    on_shutdown: Callable[[], Awaitable[None]],
    degraded_mode_exceptions: tuple[type[BaseException], ...] = (AdapterConnectionError, ValidationError),
    stop_grace: float | None = 5.0,
    manage_telemetry: bool = True,
) -> AsyncIterator[grpc.aio.Server]:
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
        stop_grace:  Seconds `server.stop()` waits for in-flight RPCs to
                     finish before cancelling them outright. `None` stops
                     immediately (grpc.aio's own default meaning) — the
                     previous unconditional behaviour, now opt-in.
        manage_telemetry: `True` (the default) calls `setup_telemetry()`/
                     `shutdown_telemetry()` itself, same as always. Pass
                     `False` when a composition root runs HTTP and gRPC
                     concurrently in one process — see `create_app`'s own
                     `manage_telemetry` docstring for the full reasoning
                     (both functions otherwise tear down the same shared
                     telemetry providers independently).

    Usage::

        async with create_server(
            address="[::]:50051",
            configure=lambda server: bind_service(server, "get_item", handler_factory=get_get_item_handler),
            on_startup=dependencies.initialise,
            on_shutdown=dependencies.shutdown,
        ) as server:
            await server.wait_for_termination()
    """
    if manage_telemetry:
        setup_telemetry()
        record_lifecycle_event("cold_start")

    server = grpc.aio.server()
    try:
        configure(server)

        try:
            await on_startup()
        except degraded_mode_exceptions as exc:
            _logger.warning("dependency not ready at startup: %s — running in degraded mode", exc)

        port = server.add_insecure_port(address)
        # Stashed the same way bind_service's own _get_or_create_servicer
        # already stashes _openframe_generic_servicer on the server — the
        # real bound port matters when address ends in ":0" (an
        # OS-assigned port, the pattern every other conformance test in
        # this repo uses for an isolated, parallel-safe test server). A
        # fixed real address doesn't need this at all; it's here for
        # exactly that ephemeral-port case.
        server._openframe_bound_port = port  # type: ignore[attr-defined]
        await server.start()
    except BaseException:
        # configure() or a non-degraded on_startup() failed after
        # setup_telemetry() already ran — without this, the telemetry
        # providers it started are never torn down, since the `finally`
        # below (which owns shutdown_telemetry()) is never reached.
        if manage_telemetry:
            shutdown_telemetry()
        raise

    try:
        yield server
    finally:
        await on_shutdown()
        await server.stop(stop_grace)
        if manage_telemetry:
            shutdown_telemetry()

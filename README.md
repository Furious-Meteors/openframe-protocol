<p align="center">
  <img src="https://raw.githubusercontent.com/Furious-Meteors/openframe-protocol/production/docs/assets/images/banner.png" alt="openframe-core" width="100%" />
</p>

<p align="center">
  <a href="https://pypi.org/project/openframe-protocol/"><img src="https://img.shields.io/pypi/v/openframe-protocol?color=6DB33F&labelColor=1a1a1a&label=PyPI" alt="PyPI version"/></a>
  </a>
  <a href="https://pypi.org/project/openframe-core/"><img src="https://img.shields.io/pypi/pyversions/openframe-core?color=6DB33F&labelColor=1a1a1a" alt="Python versions"/></a>
  <a href="https://github.com/Furious-Meteors/openframe-protocol/actions/workflows/app-test.yml"><img src="https://img.shields.io/github/actions/workflow/status/Furious-Meteors/openframe-protocol/app-test.yml?branch=production&color=6DB33F&labelColor=1a1a1a&label=tests" alt="Tests"/></a>
  <a href="https://github.com/Furious-Meteors/openframe-protocol/blob/production/LICENSE"><img src="https://img.shields.io/badge/license-MIT-6DB33F?labelColor=1a1a1a" alt="License"/></a>
  <a href="https://furious-meteors.github.io/openframe-protocol/"><img src="https://img.shields.io/badge/docs-live-6DB33F?labelColor=1a1a1a" alt="Docs"/></a>
</p>

<p align="center">
  <a href="https://github.com/Furious-Meteors/openframe-protocol/blob/production/.github/CHANGELOG.md">Changelog</a> ·
  <a href="https://github.com/Furious-Meteors/openframe-protocol/blob/production/PLAN.md">Build plan</a> ·
  <a href="https://github.com/Furious-Meteors/openframe-protocol/blob/production/openframe-protocol-design.md">Design spec</a>
</p>

---

`openframe-protocol` is the communication-protocol family of the OpenFrame Microservice
Development Suite — HTTP (including HTTP/3 over QUIC), gRPC, SSE, WebSocket, and webhooks today,
with MCP and the rest of the roadmap gated on demonstrated need (see `openframe-protocol-design.md`
section 0). Every
request/response binding package follows the same two-file schema: `server.py` (bind a
`UseCase`/`CommandHandler`/`QueryHandler` from `openframe.core.inbound` to an inbound route) and
`client.py` (call another service built on this platform) — split by `[server]`/`[client]` extras
so installing one never drags in the other's dependency. Webhooks are a one-shot exchange, the
same `CommandHandler`/`UseCase` shape — `bind_webhook` just adds HMAC signature verification
against the raw body before the handler runs; see
[Webhooks — HMAC signature verification](#webhooks--hmac-signature-verification) below. Long-lived
connections (SSE, WebSocket, and gRPC's own `CallStream`) use a different contract —
`SessionHandler` (ADR-008, `openframe-core`) — since they aren't one-request-in/one-response-out;
see
[Session transports — SSE, WebSocket, gRPC streaming, and the SessionHandler contract](#session-transports--sse-websocket-grpc-streaming-and-the-sessionhandler-contract)
below.

Not yet published to PyPI — install from this repo directly (editable installs of the sibling
packages, or build local wheels) until a first release ships. `openframe-protocol-base` is at
**0.4.2**, `openframe-protocol-http` at **0.11.0**, `openframe-protocol-grpc` at **0.4.2**,
`openframe-protocol-media` at **0.4.0**. All four develop and test against
`openframe-core>=3.2.1,<4` (`openframe-core` itself is at **3.10.0** — `openframe-protocol-media`
specifically needs `>=3.10.0` for `openframe.core.media.MediaFrame`/`MediaKind`/`MediaSegment`,
the last of which is new in 3.10.0 and used by the HLS/DASH pull-ingest capabilities below).

Both transports can run **concurrently in one process, over the same business logic** — a real
service in `openframe-local-validation-framework` serves HTTP and gRPC side by side, sharing the
exact same handler classes, with both transports live-validated against real Postgres/Mongo/
Redis/Kafka backends in Docker. See
[Multiple entrypoints — HTTP and gRPC together](#multiple-entrypoints--http-and-grpc-together)
below.

Both transports ship `create_app`/`create_server` — a configured app/server with telemetry setup,
degraded-mode startup tolerance, and error-handler wiring already done, replacing the
lifespan/middleware block every consuming service used to hand-write itself. See
[Wiring with create_app / create_server](#wiring-with-create_app--create_server) below.

---

## Installation

### Granular — one specific transport

```bash
pip install "openframe-protocol[http]"   # server.py needs [server] extra too, or [client]
pip install "openframe-protocol[grpc]"
pip install "openframe-protocol[media]"  # RTMP ingest.py needs [ingest] (pyrtmp); HLS/DASH delivery.py needs [delivery] (fastapi)
```

Each transport package splits its own runtime dependency further by direction:

```bash
pip install "openframe-protocol-http[server]"   # FastAPI — binding a server
pip install "openframe-protocol-http[client]"    # httpx — calling another service
pip install "openframe-protocol-grpc[server]"    # grpcio
pip install "openframe-protocol-grpc[client]"    # grpcio too — gRPC has no separate client-only wire library
```

### Everything

```bash
pip install "openframe-protocol[all]"     # every transport package, base included either way
```

---

## Package inventory

| Extra | Package | Status | Depends on |
|---|---|---|---|
| (default) | `openframe-protocol-base` | **0.4.2** — error mapping, `RequestContext` construction, request/response + session conformance suites, `open_session`/`run_opened_session`/`send_error_event` | `openframe-core` |
| `[http]` | `openframe-protocol-http` | **0.11.0** — `bind_query`/`bind_command`/`create_app`/`bind_session`/`bind_websocket`/`bind_webhook` (`[server]`), `create_quic_config` (`[quic]`, HTTP/3 over QUIC — no `bind_quic`, every binder works unchanged), `HttpRemoteQueryClient`/`HttpRemoteCommandClient` (`[client]`) | `openframe-protocol-base` |
| `[grpc]` | `openframe-protocol-grpc` | **0.4.2** — `bind_service`/`bind_service_stream`/`create_server` (`[server]`), `GrpcRemoteQueryClient`/`GrpcRemoteCommandClient` (`[client]`) | `openframe-protocol-base` |
| `[media]` | `openframe-protocol-media` | **0.4.0** — `bind_ingest`/`create_server`/`RtmpRegistry`/`RtmpSession` (`[ingest]`, RTMP over `pyrtmp`), `pull_hls`/`HlsPullSession`/`pull_dash`/`DashPullSession` (`[pull]`, HLS + DASH pull ingest over `httpx` + `m3u8` + `mpegdash`), `bind_hls_delivery`/`bind_dash_delivery`/`bind_media_delivery` (`[delivery]`, HLS/DASH over fastapi, with Range support), `MediaFlowController`/`DropPolicy`/`FlowConfig`/`MediaRequest`/`MediaObject`/`parse_range_header` (no extra — zero third-party deps) | `openframe-protocol-base`, `openframe-core>=3.10.0` |

`TransportConformanceTests` and `RemoteClientConformanceTests` (`openframe-protocol-base`) both
pass for real against **two** transports — the rule-of-three bar the project's own design spec
sets before either suite would be promoted into `openframe.core.testing`.

---

## Quick start

### HTTP — server

```python
from fastapi import APIRouter
from openframe.protocol.http import bind_query, bind_command, create_app

router = APIRouter(prefix="/items")

bind_query(
    router, "/{item_id}",
    handler_dep=get_get_item_handler,
    path_param=("item_id", str),
    response_model=Item,
    not_found_when=lambda item: item is None,
    not_found_detail=lambda item_id: f"Item {item_id!r} not found",
)

bind_command(
    router, "", method="POST",
    handler_dep=get_create_item_handler,
    body_model=Item,
    status_code=201,
    response_model=Item,
)

app = create_app(
    title="items-service",
    on_startup=dependencies.initialise,
    on_shutdown=dependencies.shutdown,
    routers=[router],
)
```

### HTTP — client

```python
from openframe.protocol.http import HttpRemoteQueryClient, RequestSpec

get_item = HttpRemoteQueryClient(
    base_url="http://items-service:8000", path="/items/{item_id}",
    build_request=lambda item_id: RequestSpec(path_params={"item_id": item_id}),
)
item = await get_item.execute("abc-123")
```

### gRPC — server

```python
from openframe.protocol.grpc import bind_service, create_server

def configure(server):
    bind_service(
        server, "get_item", handler_factory=get_get_item_handler,
        not_found_when=lambda item: item is None,
    )
    bind_service(
        server, "create_item", handler_factory=get_create_item_handler,
        input_model=Item,   # casts the decoded dict into a validated Item before execute() —
    )                        # the gRPC counterpart of HTTP's body_model validation

async with create_server(
    address="[::]:50051",
    configure=configure,
    on_startup=dependencies.initialise,
    on_shutdown=dependencies.shutdown,
) as server:
    await server.wait_for_termination()
```

### gRPC — client

```python
from openframe.protocol.grpc import GrpcRemoteQueryClient

get_item = GrpcRemoteQueryClient(target="items-service:50051", route="get_item")
item = await get_item.execute("abc-123")
```

---

## Design philosophy

Protocol handles three things and nothing else:

1. **Request/call → handler input** — path, query, and body (HTTP) or a JSON-encoded payload
   (gRPC) become the single value a `UseCase`/`CommandHandler`/`QueryHandler`'s `execute()`
   expects. `build_input` (HTTP) is the one place route-specific shape lives; `input_model`
   (gRPC) is its counterpart — casting the decoded dict/list into a validated domain object via
   `pydantic.TypeAdapter`, since gRPC's generic payload has no schema layer of its own the way
   FastAPI's `body_model` already provides. Handler results are serialised back the same way on
   the way out (`BaseModel`/dict/list all handled), so the exact same handler class built for
   HTTP works unmodified over gRPC.
2. **Server/app creation and lifecycle** — `create_app`/`create_server` own telemetry setup,
   degraded-mode startup tolerance, and error-handler wiring, parameterised by the one thing that
   differs per service: its own `on_startup`/`on_shutdown` hooks.
3. **Error → wire-safe response mapping** — `categorize()`/`build_envelope()` turn any
   `AdapterError` subclass, a validation error, or a bare bug into the right status
   code/`grpc.StatusCode` and a safe, non-leaking envelope, the same way across every transport.

Everything else — business logic, persistence, adapter wiring, domain models — is explicitly not
protocol's concern. A handler calls a service; the service does the real work. Protocol never
sees a repository, a database query, or a message broker.

```python
# protocol handles this:
bind_query(router, "/{item_id}", handler_dep=get_get_item_handler, path_param=("item_id", str))
app = create_app(title="items-service", on_startup=init, on_shutdown=shutdown, routers=[router])

# you handle this directly:
class GetItem:
    def __init__(self, service: ItemService) -> None:
        self._service = service
    async def execute(self, item_id, context=None):
        return await self._service.get_item(item_id)   # the real work — protocol never sees it
```

---

## Error handling

Every binding maps through the same transport-neutral categories — `NOT_FOUND`, `INVALID`,
`TIMEOUT`, `UNAVAILABLE`, `INTERNAL` — regardless of which wire format produced the failure:

```python
from openframe.protocol.base.errors import ErrorCategory, categorize, build_envelope, raise_for_envelope

# Server side — any transport's own error handler
category = categorize(exc)                        # -> ErrorCategory.NOT_FOUND, etc.
envelope = build_envelope(exc, correlation_id=cid) # {"code", "message", "retryable", "correlation_id"}
                                                    # never the raw driver-cause message for INTERNAL

# Client side — the reverse: reconstruct the matching AdapterError subclass
raise_for_envelope(category, envelope, remote_name="http:GET /items/{item_id}")
```

A remote-call failure and a database failure share one catch clause in calling code —
`HttpRemoteQueryClient`/`GrpcRemoteQueryClient` both raise real `AdapterError` subclasses, never a
transport-specific exception type.

---

## Wiring with `create_app` / `create_server`

`create_app` (HTTP) and `create_server` (gRPC) replace the lifespan/middleware/error-handler block
every consuming service used to hand-write identically — `setup_telemetry()`,
`record_lifecycle_event("cold_start")`, a try/except around the service's own startup catching
`(AdapterConnectionError, ValidationError)` for degraded-mode tolerance, telemetry flush on
shutdown. Neither function decides what `on_startup`/`on_shutdown` actually do — that stays each
service's own composition root, built from whichever `openframe-adapters` plugins it needs:

```python
# src/main.py — HTTP-only service (no gRPC entrypoint needed)
from openframe.protocol.http import create_app
from src import pipeline
from src.api.http.routes import router

app = create_app(
    title="items-service",
    description="Items over Postgres",
    on_startup=pipeline.initialise,    # ApplicationBootstrap.start() underneath
    on_shutdown=pipeline.shutdown,
    routers=[router],
)

@app.get("/health")
async def health():
    return {"status": "ok"}
```

`create_app` is a plain factory — FastAPI's app object is configuration handed off to an external
process (uvicorn) that owns the actual accept loop. `create_server` is an async context manager
instead, not by inconsistency: `grpc.aio.Server` has no external process to hand off to, and
route registration (`bind_service`) must happen before `start()`, not after — `configure` is
where those calls belong. See the design spec's draft 11 for the full reasoning.

---

## Multiple entrypoints — HTTP and gRPC together

Both transports can serve the same business logic concurrently, in one process — a real,
Docker-validated pattern, not a toy. The same `core/handlers.py` classes a service already built
for HTTP are reused unmodified for gRPC; only the route binding (`api/http/routes.py` vs.
`api/rpc/routes.py`) differs. Which transports actually run is a runtime toggle
(`EntrypointSettings`'s `ENABLE_HTTP`/`ENABLE_GRPC`, both default `true`), not a code fork:

```python
# src/main.py — single process entrypoint
import asyncio
from contextlib import AsyncExitStack

import uvicorn
from openframe.core.telemetry import setup_telemetry, shutdown_telemetry
from openframe.protocol.grpc import create_server
from openframe.protocol.http import create_app

from src import pipeline
from src.api.http.routes import router as http_router
from src.api.rpc.routes import configure as configure_grpc
from src.utils.config import EntrypointSettings


async def _noop() -> None:
    pass


async def main() -> None:
    settings = EntrypointSettings()

    # Telemetry set up/torn down exactly ONCE here, shared by whichever
    # transports are enabled — create_app/create_server are both told
    # manage_telemetry=False below. Without that, each one calls
    # setup_telemetry()/shutdown_telemetry() independently, and whichever
    # transport's own lifespan exits first tears down the providers the
    # other is still using.
    setup_telemetry()

    # Startup/shutdown called exactly ONCE here, shared by whichever
    # transports are enabled — create_app/create_server each get a no-op
    # hook instead. Calling pipeline.initialise()/shutdown() per transport
    # would double-initialise the plugin registry and orphan the first
    # instance.
    await pipeline.initialise()
    try:
        async with AsyncExitStack() as stack:
            tasks: dict[str, asyncio.Task] = {}

            if settings.enable_http:
                app = create_app(
                    title="items-service", on_startup=_noop, on_shutdown=_noop,
                    routers=[http_router], manage_telemetry=False,
                )
                http_server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.http_port))
                tasks["http"] = asyncio.create_task(http_server.serve())

            if settings.enable_grpc:
                grpc_server = await stack.enter_async_context(
                    create_server(address=settings.grpc_address, configure=configure_grpc,
                                  on_startup=_noop, on_shutdown=_noop, manage_telemetry=False)
                )
                tasks["grpc"] = asyncio.create_task(grpc_server.wait_for_termination())

            # Not gather(): both coroutines run indefinitely, so if only one
            # receives a shutdown signal, asyncio.wait(FIRST_COMPLETED) lets
            # it explicitly stop the other — gather() would hang forever.
            _done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
            if "grpc" in tasks and tasks["grpc"] in pending:
                await grpc_server.stop(grace=5)
            if "http" in tasks and tasks["http"] in pending:
                http_server.should_exit = True
                await tasks["http"]
    finally:
        await pipeline.shutdown()
        shutdown_telemetry()


if __name__ == "__main__":
    asyncio.run(main())
```

A handler whose HTTP route combines a path param and a query/body into one argument (e.g.
`UpdateStatus(artifact_id, status)`, built by HTTP's own `build_input`) needs a small adapter on
the gRPC side instead, since gRPC has no path/query split — the one place a service's gRPC
surface isn't a line-for-line mirror of its HTTP one:

```python
class UpdateStatusInput(BaseModel):
    artifact_id: str
    status: EmbeddingStatus

class _UpdateStatusAdapter:
    def __init__(self, inner): self._inner = inner
    async def execute(self, value: UpdateStatusInput, context=None):
        return await self._inner.execute((value.artifact_id, value.status), context)

bind_service(server, "update_status", handler_factory=lambda: _UpdateStatusAdapter(...), input_model=UpdateStatusInput)
```

---

## Session transports — SSE, WebSocket, gRPC streaming, and the `SessionHandler` contract

HTTP and gRPC's unary `Call` are both request/response: one input in, one output out. A long-lived
connection (SSE, WebSocket, gRPC's own `CallStream`) doesn't fit that shape — a handler may send
zero or many events over a connection's lifetime, and (for WebSocket and gRPC streaming, both
genuinely bidirectional) receive zero or many inbound events too.
`openframe.core.inbound.SessionHandler[TIn, TOut]` (ADR-008) is the contract for that:

```python
class ArtifactUpdates:
    async def on_open(self, session: Session[dict]) -> None:
        # Accept by returning normally; reject by raising — the exception
        # flows through the same categorize()/build_envelope() path a
        # request/response failure does.
        async def _produce():
            async for artifact in watch_artifact_changes():
                await session.send(artifact.model_dump())
            await session.close()
        asyncio.create_task(_produce())

    async def on_event(self, session: Session[dict], event) -> None:
        ...  # never called for SSE (no client-to-server events) — actually
             # fires for WebSocket, the one transport where this matters

    async def on_close(self, session, reason: CloseReason) -> None:
        ...  # called exactly once, however the session ended
```

`openframe.protocol.http.bind_session` (SSE) and `bind_websocket` (WebSocket) are modules of
`-http`, not separate packages — neither needs a dependency beyond `[server]` (FastAPI), since
Starlette's own `StreamingResponse` and `WebSocket` support cover both.
`openframe.protocol.grpc.bind_service_stream` is gRPC's own counterpart, needing **no new
dependency at all** — `grpcio` already supports bidirectional streaming natively, via a second RPC
(`CallStream`) alongside the existing unary `Call`:

```python
from openframe.protocol.http import bind_session, bind_websocket
from openframe.protocol.grpc import bind_service_stream

bind_session(router, "/artifacts/stream", handler_factory=lambda: ArtifactUpdates())
bind_websocket(router, "/artifacts/ws", handler_factory=lambda: ArtifactUpdates())
bind_service_stream(grpc_server, "artifact_updates", handler_factory=lambda: ArtifactUpdates())
```

Real findings from building all three, confirmed by testing against actual framework/infrastructure
behaviour, not assumed:

- **Accept/reject must happen before the connection commits, not inside it.** For SSE, Starlette's
  `StreamingResponse` sends the HTTP status (200) before ever pulling the body generator's first
  item; for WebSocket, calling `websocket.close()` before `accept()` produces a real failed
  handshake rather than a post-connect message. Both confirmed by tracing/spiking directly. So
  `on_open` always runs — and can reject — before that commit point; a reject propagates into
  `install_error_handlers`'s normal `categorize`/`build_envelope` mapping for SSE, or a real closed
  handshake for WebSocket — never a connection that already looked successful before failing.
- **A failure *after* the connection has committed** (SSE's background production, or a
  WebSocket's `on_event`) has no status code or handshake left to change — it's sent **in-band**,
  via `session.send()`, as a plain `{"error": {...}}` dict, using the now-public
  `openframe.protocol.base.send_error_event`.
- **WebSocket's `WebSocketDisconnect` is translated to `asyncio.CancelledError`** so the
  transport-neutral `run_opened_session` (which only knows about `CancelledError`, not Starlette)
  can report a real client disconnect correctly without this package needing a Starlette
  dependency of its own.
- **`TestClient.websocket_connect` doesn't service an orphan `asyncio.create_task`** the way a
  real server does — found while testing a background-task-pushing WebSocket handler, confirmed
  harmless (and the pattern confirmed working) by verifying the same handler against a real
  `uvicorn.Server` instead. A test-harness limitation, not a `bind_websocket` bug.
- **gRPC's streaming has a real race SSE/WebSocket never had**: the inbound `request_iterator` and
  the servicer method's own outbound `yield` loop are two *independently-scheduled* `grpc.aio`
  signals for the same client disconnect. Confirmed across four live spikes that `request_iterator`
  reliably ends via plain `StopAsyncIteration` *before* `context.cancelled()` reflects the same
  disconnect — not a one-off timing fluke. Fixed by polling `context.cancelled()` for a short window
  (a single `asyncio.sleep(0)` tick was confirmed not enough — the underlying HTTP/2 GOAWAY frame
  takes a few real milliseconds to process) before trusting "inbound ended" as a graceful finish.

See ADR-008 in `openframe-core` for the full contract design (including why outbound events are
push-based — `session.send()` — rather than a pull-based `AsyncIterator` return value), and
`openframe-protocol-base`'s `SessionConformanceTests` for the suite all three session transports
prove themselves against (open/reject, ordered delivery, clean close, abrupt disconnect,
correlation id stability).

---

## Webhooks — HMAC signature verification

A webhook is a one-shot exchange — one HTTP POST in, one ack out — the same shape
`CommandHandler`/`UseCase` already fit, confirmed by this repo's own design spec (§5.5). It's
**not** a `SessionHandler` transport. The one thing a webhook receiver needs that `bind_command`
doesn't provide: proving the payload actually came from the claimed sender, which needs the raw
request bytes, not a re-serialized parse of them.

```python
from openframe.protocol.http import bind_webhook

bind_webhook(
    router,
    "/webhooks/orders",
    handler_dep=get_order_paid_handler,
    body_model=OrderPaid,
    signature_header="X-Hub-Signature-256",
    secret=settings.webhook_secret,
)
```

`verify_hmac_sha256` (the default `verify`) covers the common GitHub/Shopify-style scheme:
HMAC-SHA256 over the raw body, hex-encoded, optionally prefixed (`"sha256=..."`), compared with
`hmac.compare_digest` (constant-time, not a timing-attack-prone `==`). A provider with a different
scheme (Stripe's timestamp+payload concatenation, for instance) supplies its own `verify` callable.

A missing or invalid signature returns 401 **before** the body is parsed or the handler runs — a
sender that can't prove who it is doesn't get to cause a validation error or a handler side effect
either. This maps straight to 401, not through `categorize()`/`build_envelope()`: an auth failure
specific to this one binder doesn't warrant a new `ErrorCategory` member. Any error the *handler*
itself raises after the signature check passes still flows through `install_error_handlers`'s
normal path unchanged — proven with a real `AdapterConnectionError` → 503.

---

## HTTP/3 over QUIC

QUIC is a transport-layer change, not an application-layer one — every existing `-http` binder
(`bind_query`, `bind_command`, `bind_session`, `bind_websocket`, `bind_webhook`) already runs
unmodified over it. Confirmed by a real spike, not assumed: a real Hypercorn server running
`create_app()`'s completely unmodified output, driven by a real `aioquic`-based HTTP/3 client, round
trips a `bind_query` route with status 200, the correct JSON body, and `build_request_context`'s
correlation-id generation all intact. Because of that, there is **no `bind_quic`** — QUIC only needs
a different ASGI server (Hypercorn, which speaks HTTP/3) in place of uvicorn, configured via one
function:

```python
from openframe.protocol.http import create_app
from openframe.protocol.http.quic import create_quic_config
from hypercorn.asyncio import serve

app = create_app(title="items-service", on_startup=pipeline.initialise, on_shutdown=pipeline.shutdown, routers=[router])

config = create_quic_config(
    bind="0.0.0.0:443",
    certfile="/etc/tls/fullchain.pem",
    keyfile="/etc/tls/privkey.pem",
)

await serve(app, config)
```

`create_quic_config` just builds a `hypercorn.Config` with `alpn_protocols=("h3",)` and both `bind`
and `quic_bind` set to the same address (QUIC runs over UDP, uvicorn's TCP accept loop never comes
into it). This lives as a module in `-http` behind a `[quic]` extra (`hypercorn[h3]`), not a separate
package — same reasoning as SSE/WebSocket/webhooks: no new wire schema, no new binder, just another
optional server dependency.

---

## Ecosystem

| Package | Install | Description |
|---|---|---|
| [`openframe-core`](https://github.com/Furious-Meteors/openframe-core) | `pip install openframe-core` | Ports · tracing · telemetry · exceptions · config |
| [`openframe-adapters`](https://github.com/Furious-Meteors/openframe-adapters) | `pip install openframe-adapters[postgres]` | Database and queue adapters — Postgres, Mongo, Redis, Kafka, and more |
| `openframe-protocol` | `pip install "openframe-protocol[http]"` | HTTP (incl. HTTP/3 over QUIC) · gRPC · SSE · WebSocket · webhooks · RTMP ingest today — MCP, HLS/DASH delivery on demonstrated need |
| `openframe-infra` | — | Storage · auth · secrets · observability · feature flags (not yet built) |
| `openframe-ai` | — | LangChain · LlamaIndex · CrewAI · model serving · training (not yet built) |

---

## License

MIT — see [LICENSE](LICENSE).

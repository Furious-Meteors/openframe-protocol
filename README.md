<p align="center">
  <a href="https://pypi.org/project/openframe-protocol/"><img src="https://img.shields.io/pypi/v/openframe-protocol?color=6DB33F&labelColor=1a1a1a&label=PyPI" alt="PyPI version"/></a>
  <a href="https://github.com/amrit2356/openframe-protocol"><img src="https://img.shields.io/badge/python-3.11%2B-6DB33F?labelColor=1a1a1a" alt="Python versions"/></a>
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
Development Suite — HTTP and gRPC today, with MCP, SSE/WebSocket, and the rest of the roadmap
gated on demonstrated need (see `openframe-protocol-design.md` section 0). Every binding package
follows the same two-file schema: `server.py` (bind a `UseCase`/`CommandHandler`/`QueryHandler`
from `openframe.core.inbound` to an inbound route) and `client.py` (call another service built on
this platform) — split by `[server]`/`[client]` extras so installing one never drags in the
other's dependency.

Not yet published to PyPI — install from this repo directly (editable installs of the sibling
packages, or build local wheels) until a first release ships. `openframe-protocol-base` is at
**0.3.0**, `openframe-protocol-http` at **0.5.0**, `openframe-protocol-grpc` at **0.3.0**. All
three develop and test against `openframe-core>=3.2.1,<4`.

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
| (default) | `openframe-protocol-base` | **0.3.0** — error mapping, `RequestContext` construction, both conformance suites | `openframe-core` |
| `[http]` | `openframe-protocol-http` | **0.5.0** — `bind_query`/`bind_command`/`create_app` (`[server]`), `HttpRemoteQueryClient`/`HttpRemoteCommandClient` (`[client]`) | `openframe-protocol-base` |
| `[grpc]` | `openframe-protocol-grpc` | **0.3.0** — `bind_service`/`create_server` (`[server]`), `GrpcRemoteQueryClient`/`GrpcRemoteCommandClient` (`[client]`) | `openframe-protocol-base` |

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
# src/entrypoints/http/main.py
from openframe.protocol.http import create_app
from src.bootstrap import dependencies
from src.entrypoints.http.routes import router

app = create_app(
    title="items-service",
    description="Items over Postgres",
    on_startup=dependencies.initialise,    # ApplicationBootstrap.start() underneath
    on_shutdown=dependencies.shutdown,
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
Docker-validated pattern, not a toy. The same `application/handlers.py` classes a service already
built for HTTP are reused unmodified for gRPC; only the route binding differs:

```python
# src/entrypoints/main.py — combined process entrypoint
import asyncio
import uvicorn
from openframe.protocol.grpc import create_server
from openframe.protocol.http import create_app

from src.bootstrap import dependencies
from src.entrypoints.grpc.routes import configure as configure_grpc
from src.entrypoints.http.routes import router as http_router


async def _noop() -> None:
    pass


async def main() -> None:
    # Startup/shutdown called exactly ONCE here, shared by both transports —
    # create_app/create_server each get a no-op hook instead. Calling
    # dependencies.initialise()/shutdown() twice (once per transport) would
    # double-initialise the plugin registry and orphan the first instance.
    await dependencies.initialise()
    try:
        app = create_app(title="items-service", on_startup=_noop, on_shutdown=_noop, routers=[http_router])
        http_server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8001))

        async with create_server(
            address="[::]:50051", configure=configure_grpc, on_startup=_noop, on_shutdown=_noop,
        ) as grpc_server:
            # Not gather(): both coroutines run indefinitely, so if only one
            # receives a shutdown signal, asyncio.wait(FIRST_COMPLETED) lets
            # it explicitly stop the other — gather() would hang forever.
            http_task = asyncio.create_task(http_server.serve())
            grpc_task = asyncio.create_task(grpc_server.wait_for_termination())
            _done, pending = await asyncio.wait({http_task, grpc_task}, return_when=asyncio.FIRST_COMPLETED)
            if grpc_task in pending:
                await grpc_server.stop(grace=5)
            if http_task in pending:
                http_server.should_exit = True
                await http_task
    finally:
        await dependencies.shutdown()


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

## Ecosystem

| Package | Install | Description |
|---|---|---|
| [`openframe-core`](https://github.com/Furious-Meteors/openframe-core) | `pip install openframe-core` | Ports · tracing · telemetry · exceptions · config |
| [`openframe-adapters`](https://github.com/Furious-Meteors/openframe-adapters) | `pip install openframe-adapters[postgres]` | Database and queue adapters — Postgres, Mongo, Redis, Kafka, and more |
| `openframe-protocol` | `pip install "openframe-protocol[http]"` | HTTP · gRPC today — MCP, SSE/WebSocket, media protocols on demonstrated need |
| `openframe-infra` | — | Storage · auth · secrets · observability · feature flags (not yet built) |
| `openframe-ai` | — | LangChain · LlamaIndex · CrewAI · model serving · training (not yet built) |

---

## License

MIT — see [LICENSE](LICENSE).

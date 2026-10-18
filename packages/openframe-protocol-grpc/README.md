# openframe-protocol-grpc

gRPC server and client binding, the second transport (after HTTP) to prove
`openframe-protocol-base`'s conformance suites for real. Wires a
`QueryHandler`/`CommandHandler`/`UseCase` (`openframe.core.inbound`) to a
`grpc.aio.Server`, and provides `GrpcRemoteQueryClient`/
`GrpcRemoteCommandClient` for calling another service built on this
platform — through the same shared error mapper and `RequestContext`
builder `openframe-protocol-http` uses, not a parallel implementation.

One generic RPC (`GenericService.Call`, `{json_payload: string}` in both
directions) instead of one per route — the domain type travels as JSON
inside the payload, the gRPC equivalent of `-http`'s dynamic
`inspect.Signature` trick for avoiding a per-route message type.

Install the `[server]` extra for a server, `[client]` extra to call one —
both pull in `grpcio` (unlike HTTP, there's no separate client-only wire
library for gRPC, but the extras split is kept for symmetry with every
other `openframe-protocol-*` package).

## Server-side wiring example

```python
from openframe.protocol.grpc import bind_service

server = grpc.aio.server()

bind_service(
    server, "get_item",
    handler_factory=get_get_item_handler,
    not_found_when=lambda item: item is None,
)
bind_service(
    server, "create_item",
    handler_factory=get_create_item_handler,
)

server.add_insecure_port("[::]:50051")
await server.start()
```

## Client-side wiring example

```python
from openframe.protocol.grpc import GrpcRemoteQueryClient

get_item = GrpcRemoteQueryClient(target="items-service:50051", route="get_item")
item = await get_item.execute("abc-123")
```

## Streaming — `bind_service_stream`

`GenericService` also has `CallStream` (`stream GenericRequest` → `stream GenericResponse`), the
bidirectional-streaming counterpart to `Call` — the `SessionHandler` (ADR-008, `openframe-core`)
binding for long-lived connections, same route-multiplexing trick (`x-openframe-route` metadata).
Needed no new dependency — `grpcio` already supports streaming natively; this package simply hadn't
used it until now:

```python
from openframe.protocol.grpc import bind_service_stream

bind_service_stream(server, "artifact_updates", handler_factory=lambda: ArtifactUpdates())
```

`on_open` runs — and can reject — before the method's first `yield`, mirroring `bind_service`'s own
reject path exactly. A real race not present in `-http`'s own session transports (SSE, WebSocket):
the inbound `request_iterator` and the method's own outbound `yield` loop are two
independently-scheduled `grpc.aio` signals for the same disconnect, confirmed by spiking to resolve
consistently (not randomly) in one specific order — see `openframe.protocol.grpc.server`'s own
module docstring for the full finding and fix (polling `context.cancelled()` for a short window
rather than trusting the inbound side ending on its own).

## Status

Experimental, M4 of the project's own build plan. `TransportConformanceTests`,
`RemoteClientConformanceTests`, and `SessionConformanceTests` (all `openframe-protocol-base`) pass
against this package for real — the point of building it: proving the architecture generalizes past
HTTP, not just adding a second transport for its own sake.

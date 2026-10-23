# openframe-protocol-http

HTTP/ASGI server binding. Wires a `QueryHandler`/`CommandHandler`/`UseCase`
(`openframe.core.inbound`) to a FastAPI route through the shared error
mapper and `RequestContext` builder in `openframe-protocol-base`, instead
of each route hand-mapping `HTTPException`s itself.

Install the `[server]` extra for the FastAPI/ASGI dependencies —
`openframe-protocol-http[server]`.

## Wiring example

```python
from openframe.protocol.http import bind_query, bind_command, install_error_handlers

router = APIRouter(prefix="/items")

bind_query(
    router, "/{item_id}",
    handler_dep=get_get_item_handler,
    path_param=("item_id", str),
    response_model=Item,
    not_found_when=lambda item: item is None,
)

bind_command(
    router, "", method="POST",
    handler_dep=get_create_item_handler,
    body_model=Item,
    status_code=201,
    response_model=Item,
)

install_error_handlers(app)
```

## Session transports — `bind_session` (SSE) and `bind_websocket` (WebSocket)

Long-lived connections don't fit the one-request/one-response shape above — a handler may send zero
or many events over a connection's lifetime, and (WebSocket only) receive zero or many inbound
events too. Both bind `openframe.core.inbound.SessionHandler[TIn, TOut]` (ADR-008, `openframe-core`)
instead of a `QueryHandler`/`CommandHandler`:

```python
from openframe.protocol.http import bind_session, bind_websocket

bind_session(router, "/artifacts/stream", handler_factory=lambda: ArtifactUpdates())
bind_websocket(router, "/artifacts/ws", handler_factory=lambda: ArtifactUpdates())
```

Neither needs a dependency beyond `[server]` — Starlette's own `StreamingResponse` (SSE) and
`WebSocket` (WebSocket) cover both. `on_open` runs — and can reject — before the connection commits
(before `StreamingResponse` sends its 200, before `websocket.accept()`); a failure after that point
(SSE's background production, a WebSocket `on_event`) is sent in-band via `session.send()` as a
plain `{"error": {...}}` dict (`openframe.protocol.base.send_error_event`). See the top-level
README's "Session transports" section and ADR-008 for the full contract and the real findings
(disconnect translation, `TestClient` task-scheduling limitation, etc.) behind this binding.

## Webhooks — `bind_webhook`

A webhook is a one-shot exchange, the same `CommandHandler`/`UseCase` shape `bind_command` already
fits — `bind_webhook` adds HMAC signature verification against the raw request body before the
handler runs:

```python
from openframe.protocol.http import bind_webhook

bind_webhook(
    router, "/webhooks/orders",
    handler_dep=get_order_paid_handler,
    body_model=OrderPaid,
    signature_header="X-Hub-Signature-256",
    secret=settings.webhook_secret,
)
```

`verify_hmac_sha256` (the default `verify`) covers the common GitHub/Shopify-style scheme; a missing
or invalid signature returns 401 before the body is parsed or the handler runs. See the top-level
README's "Webhooks" section for the full reasoning.

## HTTP/3 over QUIC — `create_quic_config`

QUIC is a transport-layer change, not an application-layer one — every binder above already runs
unmodified over it, confirmed by a real Hypercorn + `aioquic` spike. There is no `bind_quic`; QUIC
only needs a different ASGI server (Hypercorn, via the `[quic]` extra) in place of uvicorn:

```python
from openframe.protocol.http.quic import create_quic_config
from hypercorn.asyncio import serve

config = create_quic_config(bind="0.0.0.0:443", certfile="fullchain.pem", keyfile="privkey.pem")
await serve(app, config)
```

See the top-level README's "HTTP/3 over QUIC" section for the full reasoning.

## Status

Experimental, M1-M7 of the project's own build plan. `TransportConformanceTests`,
`RemoteClientConformanceTests`, and `SessionConformanceTests` (all `openframe-protocol-base`) pass
against this package for real. HTTP was the first transport to prove the inbound handler contract
survives a real transport at all; it's since grown session support (SSE, WebSocket), webhooks, and
HTTP/3 over QUIC on top of that same foundation.

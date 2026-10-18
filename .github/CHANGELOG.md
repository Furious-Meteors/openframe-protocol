# Changelog

All notable changes to this project will be documented in this file.

---

## [openframe-protocol-grpc 0.4.0] - 2026-10-18

### Added — `bind_service_stream`, the third `SessionHandler` transport, via `grpc.aio`'s native streaming

`GenericService` gains a second RPC, `CallStream` (`stream GenericRequest` → `stream GenericResponse`),
alongside the existing unary `Call` — same route-multiplexing trick (`x-openframe-route` metadata),
regenerated `generic_pb2.py`/`generic_pb2_grpc.py` via `grpc_tools.protoc`, confirmed the existing
`Call` RPC's generated shape stayed byte-for-byte compatible (31 pre-existing tests still pass
unchanged). Unlike SSE/WebSocket, this needed **no new library or dependency** — `grpcio` already
supports all four RPC shapes; this codebase had simply never used the streaming ones until now.

- **`GrpcSession`** — `Session[TOut]` backed by an `asyncio.Queue`, structurally identical to
  `SseSession`'s own (the servicer method's `yield` loop and the handler are different coroutines
  that need to hand items between them, same reason SSE needed one).
- **`bind_service_stream(server, route, *, handler_factory, input_model=None)`** — `on_open` runs
  (and can reject) before the method's first `yield`, mirroring SSE's pre-`StreamingResponse` and
  WebSocket's pre-`accept()` commit-point rule, reusing `open_session` unchanged.
- **A real race SSE/WebSocket never had, found by spiking, not assumed**: `request_iterator`
  (inbound) and the servicer method's own `yield` loop (outbound) are two independently-scheduled
  `grpc.aio` signals for the same client disconnect — confirmed across four separate live
  server/channel spikes that `request_iterator` reliably resolves via plain `StopAsyncIteration`
  *before* `context.cancelled()` reflects the same disconnect, not a one-off timing fluke.
  `bind_service_stream` therefore does **not** reuse
  `openframe.protocol.base.session.run_opened_session` wholesale — inbound dispatch polls
  `context.cancelled()` for a short window (confirmed needed: a single `asyncio.sleep(0)` tick was
  NOT enough; the underlying HTTP/2 GOAWAY frame takes a few real milliseconds to process) right
  after its own loop ends normally, before trusting that as `COMPLETED` rather than
  `CLIENT_DISCONNECT`.
- Mid-session `on_event` failures sent in-band via the now-shared `send_error_event`, same as
  SSE/WebSocket; a reject propagates as a clean `AioRpcError` with zero messages, confirmed by a
  real spike, mirroring `Call`'s own existing reject path (`_abort_for_exception`, reused unchanged).
- `SessionConformanceTests` now proven against a **third** real transport (`tests/test_grpc_session_conformance.py`
  — named to avoid the cross-package `tests/` module-name collision this package's own `conftest.py`
  already documents, since `openframe-protocol-http` has its own `test_session_conformance.py`).
- 10 new tests. Full monorepo suite: 174 passing (up from 164).

---

## [openframe-protocol-http 0.8.0] - 2026-10-18

### Added — `bind_webhook`, a one-shot receiver with HMAC signature verification

A new `webhook.py` module inside `-http` (not its own package — per the design spec's own
placement decision, alongside `sse.py`/`websocket.py`). Deliberately **not** a `SessionHandler`
transport — a webhook is a one-shot exchange (confirmed by the design spec's own text, §5.5:
`CommandHandler`/`QueryHandler` already fit webhooks), so `bind_webhook` dispatches to a plain
`CommandHandler`/`UseCase`, the same contract `bind_command` uses.

- **`bind_webhook(router, path, *, handler_dep, body_model, signature_header, secret, verify, status_code=200)`**
  — reads the raw request body once, verifies *signature_header*'s value against it with *verify*
  **before** parsing the body or calling the handler, then parses `body_model` from those same raw
  bytes (never a re-serialized copy, which wouldn't byte-match what was actually signed).
- **`verify_hmac_sha256(secret, raw_body, signature, prefix="sha256=")`** — the default verifier,
  covering the common GitHub/Shopify-style scheme (HMAC-SHA256 over the raw body, hex-encoded,
  optionally prefixed), using `hmac.compare_digest` for constant-time comparison. Deliberately not
  universal — a provider with a different scheme (Stripe's timestamp+payload concatenation, for
  instance) supplies its own `verify` callable.
- **A missing or invalid signature returns 401 before the handler ever runs** — a sender that can't
  prove who it is doesn't get to cause a validation error or a handler side effect either. This
  maps straight to 401, not through `categorize()`/`build_envelope()`: an auth failure specific to
  this one binder doesn't warrant a new `ErrorCategory` member (none of the existing five draw an
  authenticated/unauthenticated distinction, and `-base`'s own rule-of-three discipline argues
  against adding one for a single binder's pre-handler check).
- **Any error the handler itself raises after signature verification passes still flows through
  `install_error_handlers`'s normal `categorize`/`build_envelope` path unchanged** — proven with a
  real `AdapterConnectionError` → 503, confirming the handler actually ran (not short-circuited by
  the signature check).
- **A real `TestClient` quirk found and fixed while spiking this, not new but newly hit**: a bare
  `pydantic.ValidationError` (raised directly by this binder's own `body_model.model_validate_json`
  call, not by FastAPI's internal body-extraction) is only caught by `install_error_handlers`'s
  bare `@app.exception_handler(Exception)` when `TestClient(app, raise_server_exceptions=False)` —
  otherwise it re-raises into the test itself. Same root cause as the conformance driver's own
  documented `httpx.ASGITransport(raise_app_exceptions=False)` requirement, hit again here in a new
  binder's own test.
- 7 new tests. Full monorepo suite: 164 passing (up from 157).

---

## [openframe-protocol-http 0.7.0] - 2026-10-17

### Added — `bind_websocket`, the second `SessionHandler` transport and the first to exercise `on_event`

A new `websocket.py` module inside `-http` (not its own package — Starlette's native WebSocket
support needs no dependency beyond `[server]`, same as SSE). Unlike SSE, WebSocket is genuinely
bidirectional: this is the first transport in this repo to actually dispatch inbound events
through `on_event`.

- **`WebSocketSession`** — `Session[TOut]` backed directly by the Starlette `WebSocket` object (no
  intermediate queue needed, unlike `SseSession` — `send()` calls the real socket's `send_text()`
  directly, accepting the connection on first call if `accept()` hasn't happened yet).
- **`bind_websocket(router, path, *, handler_factory)`** — `on_open` always runs, and can reject,
  *before* `accept()`, mirroring SSE's own commit-point timing rule but for a handshake instead of
  a response's headers. Confirmed with a real spike (`TestClient.websocket_connect`): calling
  `websocket.close()` before `accept()` produces a genuine failed handshake the client sees as
  `WebSocketDisconnect` on connect, not a message after connecting.
- **`WebSocketDisconnect` → `asyncio.CancelledError` translation**: `run_opened_session`
  (`openframe-protocol-base`) only special-cases `CancelledError` for disconnect handling — it has
  no Starlette dependency of its own. `bind_websocket`'s `inbound_events()` generator re-raises a
  real client disconnect as `CancelledError` instead of letting `WebSocketDisconnect` propagate
  unmapped, so the existing generic handling applies unchanged.
- **A real race found and fixed while spiking this**: a handler that closes the session itself
  from inside `on_event` (not a client disconnect) left the inbound loop's next `receive_text()`
  call hitting an already-closed socket, raising `WebSocketDisconnected` (a *different* exception
  than `WebSocketDisconnect`, confirmed by the actual traceback) — unmapped, this surfaced as
  `CloseReason.ERROR` instead of `COMPLETED`. Fixed by checking the session's closed state before
  each receive, ending the generator normally instead.
- **A test-harness-only limitation found and worked around, not a `bind_websocket` bug**:
  `TestClient.websocket_connect` does not service an orphan `asyncio.create_task` the way a real
  server does — a background-task-pushing handler hung forever under `TestClient` but worked
  correctly when verified against a real `uvicorn.Server` with a real `websockets`-library client
  (confirmed directly, not assumed). `SessionConformanceTests`'s own WebSocket driver uses a
  client-triggered close pattern instead, which `TestClient` can actually test.
- `SessionConformanceTests` (from `-base`) now proven against a *second* real transport — all 6
  pass, alongside SSE's own 6.
- 16 new tests. Full monorepo suite: 157 passing (up from 146).
- **Not done as part of this**: nothing else in the M3 milestone (webhooks, MCP) — WebSocket was
  the last transport this plan called for.

---

## [openframe-protocol-http 0.6.0] - 2026-10-17

### Added — `bind_session` (SSE), the first real `SessionHandler` transport

Built on `openframe-protocol-base` 0.4.0's `open_session`/`run_opened_session`/
`send_error_event`. A new `sse.py` module inside `-http` (not a new
package), per the design spec's own placement decision — SSE has no
handshake beyond a normal HTTP GET.

- **`SseSession`** — `Session[TOut]` backed by an `asyncio.Queue`; `send()`
  enqueues, `close()` enqueues a sentinel and records the *first* reason
  it was called with.
- **`bind_session(router, path, *, handler_factory)`** — calls
  `open_session` (accept/reject) **before** constructing the
  `StreamingResponse`, confirmed necessary by actually tracing Starlette's
  `StreamingResponse.stream_response`: it sends the ASGI
  `http.response.start` message (status 200) before ever pulling the body
  generator's first item, so a reject decided inside the generator would
  already be too late. A reject now propagates straight out of the route
  function into `install_error_handlers`'s normal `categorize`/
  `build_envelope` → status-code path — proven with a real
  `AdapterTimeoutError` → 504 response, not assumed. Sets
  `X-Correlation-ID` on the response, matching `bind_query`/
  `bind_command`'s own convention.
- **Deliberately does not use `run_opened_session`** — SSE has no inbound
  events at all (`on_event` is never called), so there's nothing for it
  to dispatch. An SSE handler drives its own background production after
  `on_open` returns and reports its own failures via the now-public
  `send_error_event` directly.
- **Spiked against real infrastructure, not just `httpx.ASGITransport`**:
  confirmed the hard way that `ASGITransport` does not simulate a real
  client disconnect at all (no parallel `http.disconnect`-watcher task
  the way a real server has) — an early version of the disconnect test
  hung forever. Rewrote it against a real `uvicorn.Server` and a real
  socket close instead; `uvicorn>=0.30` added as a `[dev]`-only
  dependency for this. Proven: ordered multi-event delivery over one
  connection, reject → mapped HTTP error (not a broken stream), mid-session
  failure → in-band error event with no message leak, and real client
  disconnect → `on_close(CLIENT_DISCONNECT)`.
- `SessionConformanceTests` (from `-base`) now also proven against this
  real transport, not just the in-process fake — all 6 pass.
- `openframe-protocol-base` floor bumped to `>=0.1,<1` (unchanged —
  already covers 0.4.0).
- 11 new tests. Full monorepo suite: 146 passing (up from 135).
- **Not done as part of this**: WebSocket — its own package-boundary
  question (still `[verify]` in the design spec) and genuinely
  bidirectional, unlike SSE. Next step once this is reviewed.

---

## [openframe-protocol-base 0.4.0] - 2026-10-17

### Added — `run_session` + `SessionConformanceTests`, the session-lifecycle counterpart to `TransportConformanceTests`

Built on `openframe-core` 3.8.0's new `SessionHandler`/`Session`/`CloseReason`
(ADR-008) — the driving-side contract for long-lived connections
(WebSocket, SSE), added directly to core rather than incubated here first
(see that ADR's "Why skip incubation" section).

- **`open_session(handler, session)` + `run_opened_session(handler, session,
  inbound_events)`** (`session.py`) — the open/dispatch/error/close state
  machine every session transport's `server.py` reuses, the
  session-lifecycle sibling of `create_app`/`create_server` (M1c).
  `run_session(...)` is a convenience wrapper combining both, correct
  only when there's no wire "commit point" to worry about. Two failure
  points, two different wire treatments — found while designing this,
  not assumed: an `on_open` rejection propagates out of `open_session`
  (nothing committed to the wire yet — the transport maps it through the
  usual `categorize`/`build_envelope` status-code path), while an
  `on_event` failure mid-session (`run_opened_session`) is sent
  **in-band** via `session.send()` (the stream already committed by
  then, no status code left to change) and does not propagate.
  `asyncio.CancelledError` is reported as `CloseReason.CLIENT_DISCONNECT`
  and always re-raised — standard cancellation hygiene, never swallowed.
  `on_close`/`session.close()` always fire exactly once for the path
  taken, including when `on_open` itself failed.
  **`open_session`/`run_opened_session` were split from one single
  `run_session` function mid-build**, confirmed by tracing Starlette's
  own `StreamingResponse.stream_response`: it sends the ASGI
  `http.response.start` message (status + headers) before ever pulling
  the body generator's first item — so a reject decided *inside* a
  generator would already be too late to turn into a real 4xx/5xx. A
  transport with a commit-point distinction (SSE, WebSocket) must call
  `open_session` before constructing its `StreamingResponse`/completing
  its handshake, then `run_opened_session` after.
- **`SessionResult`/`SessionDriver`/`SessionConformanceTests`**
  (`session_testing.py`, its own module, not added to the existing
  `testing.py` — the two suites share no scenarios) — covers exactly what
  the design spec's session section specifies: open/reject, ordered
  delivery, clean close, abrupt disconnect, and correlation id stability
  across the session's whole lifetime. Proven against a real in-process
  driver running real handlers through the real `run_session`/
  `InMemorySession` (`tests/test_session_testing.py`), not just hand-built
  result objects — doubles as an integration test of `run_session` itself.
  Promotion to `openframe.core.testing` gated on two session transports
  (SSE, WebSocket) passing this suite for real, same rule-of-three
  discipline as `TransportConformanceTests`.
- `openframe-core` floor bumped to `>=3.2.1,<4` (unchanged — already
  covers 3.8.0).
- 21 new tests. Full monorepo suite: 135 passing (up from 114).
- **Not done as part of this**: any real session transport implementing
  `SessionHandler`/calling `run_session` — this built and proved the
  generic plumbing, not yet a transport. SSE (inside
  `openframe-protocol-http`, per the design spec's own placement
  decision) is next.

---

## [openframe-protocol-grpc 0.3.0] - 2026-10-13

### Added — `bind_service(input_model=...)`, found while wiring a real HTTP-built handler over gRPC

Adding a parallel gRPC entrypoint to a real `openframe-local-validation-framework` service
(sharing the exact same `application/handlers.py` classes the HTTP entrypoint already uses, not
a gRPC-specific duplicate) surfaced two real gaps `bind_service` never needed before, since
nothing had actually called it with a handler built for `-http`'s own validated-input
assumption:

- **Input casting.** A handler like `CreateItem.execute(self, item: Item, ...)` expects a
  validated domain object. FastAPI's `body_model` already validates before the handler ever
  sees it; `bind_service`'s decoded `json_payload` was just a plain dict/list with no casting
  step of its own. `input_model` (any type `pydantic.TypeAdapter` accepts — a `BaseModel`
  subclass, `list[Item]`, etc.) fixes this; `None` (the default) keeps passing the raw decoded
  value through unchanged, so every existing call site is unaffected.
- **Output serialisation.** `json.dumps` has no idea what to do with a `BaseModel` a handler
  returns on its own (`GetItem` returning an `Item`, `ListItems` returning `{"items": [Item,
  ...], "total": N}`). Added `_to_jsonable()` — recursively converts `BaseModel`/dict/list
  results into something `json.dumps` can actually serialise, the gRPC counterpart of
  `response_model`'s implicit job on `-http`.
- A validation failure from an invalid `input_model` cast now categorises as
  `INVALID_ARGUMENT`/`ErrorCategory.INVALID` through the same `categorize()`/`build_envelope()`
  path a handler's own exception goes through — matching how FastAPI's own
  `RequestValidationError` categorises, not a separate uncaught crash.
- `pydantic` moved from `[dev]`-only to a real `[server]` extra runtime dependency —
  `TypeAdapter`/`BaseModel.model_dump` are no longer just a test-time concern.
- 5 new tests (`test_bind_service_input_model.py`). Full monorepo suite: 114 passing (up from
  109).

---

## [infra] - 2026-10-13

### Fixed — real CI failure: `app-test.yml` couldn't install `-http`/`-grpc`

A real GitHub Actions run failed `pip install -e ".[dev]"` for
`openframe-protocol-grpc` with `ERROR: No matching distribution found for
openframe-protocol-base` — confirmed from the actual CI log, not assumed.
Cause: `openframe-protocol-base` isn't published to PyPI (unlike
`openframe-core`/`openframe-adapters-*`), so a clean runner has no way to
resolve it as a dependency of `-http`/`-grpc`'s own `pyproject.toml`. The
same problem the Docker-based validation-framework build hit earlier
(worked around there with local wheels) — CI needed its own fix since
there's no Docker layer to inject a wheel into.

Fixed in `app-test.yml`: added a step installing `openframe-protocol-base`
from the same checkout (`pip install -e packages/openframe-protocol-base`)
before each matrix job's own `pip install -e ".[dev]"` — pip then finds
the requirement already locally satisfied and never reaches the index for
it. Harmless no-op duplication for the `openframe-protocol-base` matrix
entry itself. Verified locally end to end, not just reasoned about: a
from-scratch venv running the exact same two steps installed
`openframe-protocol-base` 0.3.0 + `openframe-protocol-grpc` 0.2.0 cleanly,
26/26 tests passing.

### Added — `.github/requirements/{test,docs}.txt`

Added the local-dev convenience files every sibling repo
(`openframe-core`/`openframe-adapters`/`openframe-tooling`) already has —
`test.txt` (aggregates every package's `[dev]` extras so `pytest
packages/` works from the repo root in one shared environment, not used
by CI itself) and `docs.txt` (mkdocs toolchain, same versions every other
repo pins). Verified `pip install -r .github/requirements/test.txt` into
a from-scratch venv, then `pip install -e` all three packages and
`pytest packages/` — 109/109 passing.

---

## [openframe-protocol-http 0.5.0 / openframe-protocol-grpc 0.2.0] - 2026-10-13

### Added — server/app creation, a boundary M1/M1b deliberately left out

M1/M1b's own design drew the boundary at "protocol configures routes and
error handling on a server object the caller already created" — `FastAPI()`
instantiation, lifespan, middleware registration, and the uvicorn process
stayed hand-written per service. That produced real, demonstrated
duplication: every one of `openframe-local-validation-framework`'s seven
migrated services hand-wrote the identical ~15-line lifespan block
(`setup_telemetry()`, `record_lifecycle_event("cold_start")`, a try/except
around the service's own `initialise()` catching
`(AdapterConnectionError, ValidationError)` for degraded-mode startup,
`yield`, `shutdown()`, `shutdown_telemetry()`). This release moves that
shape into the protocol packages themselves, parameterised by the one
thing that actually differs per service: the startup/shutdown hooks.

- **`openframe-protocol-http`: `create_app(title=, description=,
  on_startup=, on_shutdown=, routers=, degraded_mode_exceptions=)`** —
  returns a configured `FastAPI` instance with the lifespan above,
  `TelemetryMiddleware` added, `install_error_handlers` wired, and every
  router in `routers` included. Does NOT start uvicorn and does NOT decide
  what `on_startup`/`on_shutdown` do internally (still each service's own
  `dependencies.initialise`/`shutdown`) — a service with extra lifespan
  needs (e.g. a background Kafka consumer task) does that extra work
  inside its own hook; `create_app` doesn't need to know it exists.
- **`openframe-protocol-grpc`: `create_server(address=, configure=,
  on_startup=, on_shutdown=, degraded_mode_exceptions=)`** — an async
  context manager, not a plain factory like `create_app`, and deliberately
  so: `grpc.aio.Server` has no external process to hand off to the way
  FastAPI hands off to uvicorn, and `bind_service` calls must register
  before `start()`, not after — `configure` is where those calls belong.
  Yields a started server; stashes the real bound port on
  `server._openframe_bound_port` for the ephemeral-port (`"...:0"`) test
  pattern every conformance suite in this repo already uses.
- Both proven with dedicated tests (not folded into existing conformance
  suites, since this is server*creation*, a different concern from
  request binding): `on_startup`/`on_shutdown` actually fire, a
  degraded-mode exception doesn't crash startup, a non-degraded exception
  still propagates, routes/error-handlers are reachable on the result.
  9 new tests (5 HTTP, 4 gRPC). Full monorepo suite: 109 passing (up from
  99).

### Found and fixed while writing the HTTP test

- `httpx.ASGITransport` alone never runs the ASGI lifespan protocol — it
  only forwards HTTP-scope requests. An early version of the `create_app`
  startup/shutdown test used a raw `httpx.AsyncClient` over it and got an
  empty "nothing called" result that looked like a `create_app` bug but
  wasn't one. Fixed by switching those specific tests to
  `fastapi.testclient.TestClient`'s own context manager, which does drive
  lifespan on `__enter__`/`__exit__`.

---

## [openframe-protocol-http 0.4.0] - 2026-10-13

### Added

- **`bind_query`/`bind_command(not_found_detail=...)`** — a second real
  service (`items-cached`) needed its 404 response's `detail` string to
  include the handler's own input (`"Item 'abc' not found"`), matching
  its pre-migration hand-written route exactly; caught by that service's
  own pre-existing test, not invented for this package. `not_found_when`
  alone only controls whether a 404 is raised, not what it says — the
  generic `"not found"` default (still the fallback when omitted) lost
  that per-route detail during migration. Purely additive.

---

## [openframe-protocol-http 0.3.0] - 2026-10-13

### Added — migrating `openframe-local-validation-framework`'s remaining services

- **`bind_command(query_model=...)`** — a real second service
  (`artifacts-mongo`, later `research-pipeline`) needed a command whose
  input is a path param plus a query param and no body at all (`PATCH
  /{id}/status?status=...`). `_bind` already handled this generically;
  only the public `bind_command` wrapper hadn't exposed `query_model`
  yet, since `items-postgres` (M1) never needed that exact combination.
  Purely additive — every existing call site is unaffected.

### Found and fixed during the build

- **Cross-package `tests/` namespace collision, again**: adding a new
  test file to `openframe-protocol-http` changed test collection order
  enough that `test_client.py`'s own pre-existing `from
  tests.test_conformance import _build_app` (same pattern already fixed
  in `openframe-protocol-grpc`, see that package's `2026-10-13` entry
  below) started resolving to `openframe-protocol-grpc`'s
  `test_conformance.py` instead of its own. Fixed the same way: moved
  `_build_app` into `conftest.py` as a `fastapi_app` fixture, used by
  `test_conformance.py` and `test_client.py` alike instead of either
  importing it from the other.

---

## [openframe-protocol-base 0.3.0 / openframe-protocol-grpc 0.1.0] - 2026-10-13

### Added — M4 (gRPC server + client), per PLAN.md's "M4 — gRPC" section

- **New package, `openframe-protocol-grpc`**: `bind_service` (`server.py`)
  and `GrpcRemoteQueryClient`/`GrpcRemoteCommandClient` (`client.py`) —
  same two-file schema `openframe-protocol-http` established for M1/M1b,
  confirming the schema generalizes past HTTP rather than being an
  HTTP-specific convention.
- One generic RPC (`GenericService.Call`, `{json_payload: string}` both
  ways) instead of a per-route message type — the gRPC equivalent of
  `-http`'s dynamic `inspect.Signature` trick. The route is selected by
  an `x-openframe-route` metadata key, the gRPC equivalent of an HTTP
  path. Reuses `categorize()`/`build_envelope()`/`raise_for_envelope()`
  from `openframe-protocol-base` almost verbatim — only the status-code
  table changes (`grpc.StatusCode` instead of HTTP integers), confirmed
  during the M4 research spike to match `ErrorCategory`'s five members
  1:1, cleaner than HTTP's own mapping.
- `[server]`/`[client]` extras both depend on `grpcio` — unlike HTTP,
  gRPC has no separate client-only wire library; the split is kept
  anyway for schema consistency across every `openframe-protocol-*`
  package, not because the dependency differs here.
- **`openframe-protocol-base`**: `RemoteClientDriver` (Protocol) +
  `RemoteClientConformanceTests` — the reusable client-side conformance
  suite that was missing when M1b shipped (HTTP's client tests were
  ad hoc, not a reusable suite). Built and proven against HTTP first
  (`openframe-protocol-http`'s `test_client.py`, 76 tests total, up from
  71), *then* reused as-is for gRPC — two transports passing the same
  suite is what actually proves `raise_for_envelope()`'s reconstruction
  logic isn't accidentally HTTP-shaped.
- **Both `TransportConformanceTests` and `RemoteClientConformanceTests`
  now pass for a second real transport** (`grpc.aio`, a real loopback
  socket — confirmed `grpc.aio` has no true in-memory transport, same
  rigor as HTTP's `httpx.ASGITransport`). Per draft 8's rule-of-three
  discipline, this is what would justify promoting both suites into
  `openframe.core.testing` — not done automatically by this build; a
  separate, deliberate step.
- 22 new tests in `openframe-protocol-grpc`. Full monorepo suite: 98
  passing (up from 76).

### Found and fixed during the build

- **Cross-package `tests/` module-name collision**: `openframe-protocol-grpc`'s
  `test_client.py` originally did `from tests.test_conformance import
  _build_server`, mirroring `-http`'s own existing pattern — but once
  both packages' `tests/` directories (neither has `__init__.py`) are on
  `sys.path` in the same pytest session, Python merges them into one
  namespace package, and the plain import silently resolved to the
  *other* package's `test_conformance.py`. Fixed by moving the shared
  server-builder into `conftest.py` as a `grpc_server` fixture instead of
  a cross-module import — pytest loads each `conftest.py` by its own
  file path, which isn't subject to this ambiguity. Confirmed by running
  the full `pytest packages/` sweep (98 passed), not just each package
  in isolation.
- **`generic_pb2_grpc.py`'s generated import was a bare `import
  generic_pb2`**, the flat-layout default `grpc_tools.protoc` emits —
  breaks immediately once the stub lives inside a real package
  (`openframe.protocol.grpc`) rather than a standalone script directory.
  Fixed to `from openframe.protocol.grpc import generic_pb2`.

### Status

M0, M1, M1b, M4 complete. MCP, sessions, and every other media protocol
remain out of scope — gated on their own open design decisions per the
project's spec. The true cross-service Docker-based check remains
deferred until Docker is available in this environment.

---

## [openframe-protocol-base 0.2.0 / openframe-protocol-http 0.2.0] - 2026-10-11

### Added — M1b (HTTP client, service-to-service calls)

- **`openframe-core` v3.7.0 dependency**: `openframe.core.remote` —
  `RemoteCommandClient[TIn]`/`RemoteQueryClient[TIn, TOut]`, the outbound
  counterparts of `core.inbound`'s `CommandHandler`/`QueryHandler`.
  Structurally identical signature, deliberately separate classes (see
  that module's own docstring); no `BasePort`/`Capability` involvement
  per draft 8's D10 — no dynamic "which remote client fills this role"
  lookup use case exists, a caller always knows exactly which service
  it's calling.
- **`openframe-protocol-base`**: `raise_for_envelope()` — the client-side
  reverse of `build_envelope()`, reconstructing the right `AdapterError`
  subclass from a received error envelope. `INVALID`/`INTERNAL` both
  reconstruct as `AdapterQueryError` (core's `AdapterError` family has no
  dedicated validation-error subclass to mirror symmetrically) —
  documented as a deliberate choice. Proven as genuine inverses of
  `build_envelope`, not just independently-plausible mappings, via a
  round-trip test.
- **`openframe-protocol-http`**: `HttpRemoteQueryClient`/
  `HttpRemoteCommandClient` + `RequestSpec`. Request construction
  mirrors `server.py`'s own `build_input` philosophy — a `build_request`
  hook returns exactly which path params, query params, and JSON body a
  call needs, rather than guessing how to serialise an arbitrary input.
  No `PluginRegistry` lifecycle — each client owns (or borrows) a plain
  `httpx.AsyncClient`, closed via its own `aclose()`.
- Validated end-to-end against a real, in-process FastAPI app built with
  this same package's own server binder (`httpx.ASGITransport`, real
  routing/DI/exception-handlers, nothing mocked except the TCP socket) —
  including a genuine network-level connection refusal (not a simulated
  503) and a real OTel traceparent round trip (client injects, server's
  derived correlation id matches the exact trace id, not just "some
  non-empty value"). A true cross-service Docker-based check (this
  client calling a separately-running container) remains a further step
  once Docker is available in this environment.
- 14 new tests. Full suite: 71 passing (up from 57).

### Found and fixed during the build

- **`__init__.py` eagerly imported both `server.py` (needs FastAPI) and
  `client.py` (needs httpx) unconditionally** — silently defeating the
  `[server]`/`[client]` extras split this package's own `pyproject.toml`
  already promised; installing just `[client]` would still raise
  `ImportError` on the unrelated FastAPI dependency the moment anything
  imported `openframe.protocol.http` at all. Fixed with lazy
  `__getattr__`-based submodule imports (PEP 562) before this shipped,
  not after. Added the missing `[client]` extra (`httpx>=0.27`) itself,
  which had never been declared at all despite being planned from M1.

### Status

M1 and M1b complete. gRPC, MCP, sessions, and every media protocol
remain out of scope — gated on their own open design decisions per the
project's spec.

---

## [openframe-protocol-base 0.1.0 / openframe-protocol-http 0.1.0 / meta 0.1.0] - 2026-10-11

### Added — M1 (base package + HTTP server binding), per PLAN.md

- **`openframe-protocol-base`** — `errors.py` (`categorize()`/`build_envelope()`,
  table-tested against every real `AdapterError`/`PluginError` subclass
  in the installed `openframe-core`, not a hand-maintained list), `context.py`
  (`build_request_context()`), and `testing.py` (`TransportConformanceTests`
  + `TransportDriver`, the suite every binding proves itself against).
  Incubating here rather than in `openframe-core`, per draft 8's own
  rule-of-three discipline — promoted to core only once a second
  transport (gRPC) passes the same suite for real.
- **`openframe-protocol-http`** — `bind_query`/`bind_command` wiring a
  `QueryHandler`/`UseCase`/`CommandHandler` to a FastAPI route through
  the shared error mapper and `RequestContext` builder, plus
  `install_error_handlers`. Spiked and confirmed against a real
  `TestClient` before building: FastAPI 0.143 supports a Pydantic
  `BaseModel` as a query dependency via `Annotated[Model, Query()]`, and
  a dynamically-constructed `inspect.Signature` assigned to a plain
  `async def (**kwargs)` endpoint — both load-bearing for a binder that
  doesn't need one hand-written function signature per route.
- **Migrated `items-postgres`** (`openframe-local-validation-framework`,
  patch only, `ItemService` unchanged): new thin handler classes in
  `application/handlers.py` (all `UseCase`-shaped, not `CommandHandler`
  — every route here returns a value the caller needs, and
  `CommandHandler.execute()` always returns `None` by contract);
  `routes.py` rewritten onto the binder with identical paths and status
  codes; `main.py` wires `install_error_handlers`; new
  `test_architecture.py` rules enforcing the transport-import boundary
  (mirroring the existing `openframe.adapters` boundary rules). Core
  floor bumped to `>=3.6,<4` for guaranteed traceparent extraction.
  **57/57 relevant tests pass** (42 architecture/service/domain/bootstrap
  + 15 routes) — zero regressions. (7 `test_adapter.py` + 4
  `test_observability.py` failures are pre-existing, unrelated — real
  Postgres/OTel-collector connections this sandbox has neither for.)

### Found and fixed during the build (not left as follow-ups)

- **A real bug in `build_request_context`'s own design, not just an
  implementation slip**: checking `trace.get_current_span()` (ambient
  OTel state) cannot distinguish "a trace genuinely propagated in from
  an upstream caller" from "`TelemetryMiddleware` started its own fresh
  local span because there was no incoming trace at all" — the latter
  always has a valid span context too, so an explicit
  `X-Correlation-ID` header could never win once `TelemetryMiddleware`
  was wired into a real app, even though the original design's resolution
  order said it should. Caught only because the HTTP conformance driver
  was built with `TelemetryMiddleware` actually wired in, not a bare
  toy app — an earlier, unwired version of the same test suite passed
  on this exact bug by accident. Fixed by extracting directly from the
  request's own headers (`propagate.extract(headers)`) instead of
  ambient state.
- **`httpx.ASGITransport`'s `raise_app_exceptions=True` default** (its
  own, not this project's) re-raises an unhandled exception into the
  test itself instead of letting `install_error_handlers`'s own
  `@app.exception_handler(Exception)` catch it and return a real 500 —
  the opposite of what a real `uvicorn` deployment does. Set to `False`
  in the conformance driver to test the real production error path.
- **`BulkImport` handler dropped the original route's response shape**
  during migration — `ItemService.bulk_import()` returns a plain `int`;
  the original hand-written route wrapped it as `{"imported": N}`
  before returning it. Caught by `test_routes.py`'s own pre-existing
  assertion, not missed silently.
- **Handler-dependency functions initially called `get_item_service()`
  as a plain function** instead of through `Depends(get_item_service)`
  — bypassing FastAPI's dependency graph entirely, which silently broke
  `test_routes.py`'s own `app.dependency_overrides[get_item_service]`
  mock-override mechanism. Fixed before it shipped, not after.

### Status

M1 complete. M1b (HTTP client, service-to-service), gRPC, MCP, sessions,
and every media protocol remain out of scope for this release — gated on
their own open design decisions (D10 especially) per the project's spec.

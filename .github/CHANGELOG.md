# Changelog

All notable changes to this project will be documented in this file.

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

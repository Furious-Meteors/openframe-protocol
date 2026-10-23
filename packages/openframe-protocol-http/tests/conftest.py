"""
Shared fixtures for openframe-protocol-http tests.

`_build_app` lives here, not in test_conformance.py, so test_client.py
can get it without a plain `from tests.test_conformance import
_build_app` cross-module import. That import pattern collides once
another package also has a `tests/` directory with a same-named module
and both lack `__init__.py` — Python merges them into one namespace
package, and a plain `import tests.X` can silently resolve to the WRONG
package's module once the whole monorepo's `packages/` tree is tested in
one pytest session (confirmed: this broke exactly this way once
`openframe-protocol-grpc` also had a `tests/test_conformance.py` — see
that package's own conftest.py docstring for the original occurrence).
conftest.py fixtures don't have this problem — pytest loads each
conftest.py by its own file path, never through this ambiguous
dotted-name resolution.
"""
from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from openframe.core.exceptions import AdapterConnectionError, AdapterTimeoutError
from openframe.core.middleware import TelemetryMiddleware
from openframe.core.telemetry import setup_telemetry
from openframe.core.testing import EchoUseCase, SpyQueryHandler
from openframe.core.testing.fixtures import *
from openframe.protocol.base.testing import UNEXPECTED_ERROR_SECRET
from openframe.protocol.http import bind_command, bind_query, install_error_handlers
from pydantic import BaseModel


class _Query(BaseModel):
    limit: int = 20


def _build_app() -> FastAPI:
    setup_telemetry()
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware)
    install_error_handlers(app)
    router = APIRouter()

    bind_command(
        router, "/create", method="POST",
        handler_dep=lambda: EchoUseCase(),
        body_model=_Query,
    )
    bind_query(
        router, "/existing",
        handler_dep=lambda: SpyQueryHandler(response={"id": "1"}),
    )
    bind_query(
        router, "/missing",
        handler_dep=lambda: SpyQueryHandler(response=None),
        not_found_when=lambda r: r is None,
    )
    bind_query(
        router, "/list",
        handler_dep=lambda: SpyQueryHandler(response={"items": []}),
    )
    bind_command(
        router, "/invalid", method="POST",
        handler_dep=lambda: EchoUseCase(),
        body_model=_Query,
    )
    bind_query(
        router, "/timeout",
        handler_dep=lambda: SpyQueryHandler(
            raises=AdapterTimeoutError("slow", adapter="x", operation="get")
        ),
    )
    bind_query(
        router, "/connection-error",
        handler_dep=lambda: SpyQueryHandler(
            raises=AdapterConnectionError("down", adapter="x", operation="get")
        ),
    )
    bind_query(
        router, "/unexpected",
        handler_dep=lambda: SpyQueryHandler(raises=RuntimeError(UNEXPECTED_ERROR_SECRET)),
    )
    bind_query(
        router, "/trace-test",
        handler_dep=lambda: SpyQueryHandler(response={"ok": True}),
    )

    app.include_router(router)
    return app


@pytest.fixture
def fastapi_app() -> FastAPI:
    return _build_app()

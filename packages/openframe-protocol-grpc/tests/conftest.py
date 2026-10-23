"""
Shared fixtures for openframe-protocol-grpc tests.

`_build_server`/`grpc_server` live here, not in test_conformance.py, so
test_client.py can use the same route table via a pytest fixture instead
of a plain `from tests.test_conformance import _build_server` cross-
module import. That import pattern works fine within a single package's
own test run, but collides once another package also has a `tests/`
directory with a same-named module and both lack `__init__.py` — Python
merges them into one namespace package, and a plain `import tests.X`
can silently resolve to the WRONG package's module when the whole
monorepo's `packages/` tree is tested in one pytest session (confirmed:
this broke exactly this way when `openframe-protocol-http` already had
its own `tests/test_conformance.py`). conftest.py fixtures don't have
this problem — pytest loads each conftest.py by its own file path,
never through this ambiguous dotted-name resolution.
"""
from __future__ import annotations

import grpc
import pytest
from openframe.core.exceptions import AdapterConnectionError, AdapterTimeoutError
from openframe.core.testing import EchoUseCase, SpyQueryHandler
from openframe.core.testing.fixtures import *
from openframe.protocol.base.testing import UNEXPECTED_ERROR_SECRET
from openframe.protocol.grpc import bind_service
from pydantic import BaseModel


class _Query(BaseModel):
    limit: int = 20


def _validated_echo(payload):
    """
    Mirrors what FastAPI's body_model validation does automatically for
    HTTP — gRPC's generic proto has no schema layer of its own, so this
    is where the equivalent validation actually happens, raising a real
    pydantic.ValidationError that categorize() maps to INVALID exactly
    the same way it maps FastAPI's RequestValidationError.
    """
    _Query.model_validate(payload)
    return payload


class _ValidatingUseCase:
    async def execute(self, value, context=None):
        return _validated_echo(value)


async def _build_server() -> tuple[grpc.aio.Server, int]:
    server = grpc.aio.server()

    bind_service(server, "create", handler_factory=lambda: EchoUseCase())
    bind_service(server, "existing", handler_factory=lambda: SpyQueryHandler(response={"id": "1"}))
    bind_service(
        server,
        "missing",
        handler_factory=lambda: SpyQueryHandler(response=None),
        not_found_when=lambda r: r is None,
    )
    bind_service(server, "list", handler_factory=lambda: SpyQueryHandler(response={"items": []}))
    bind_service(server, "invalid", handler_factory=lambda: _ValidatingUseCase())
    bind_service(
        server,
        "timeout",
        handler_factory=lambda: SpyQueryHandler(
            raises=AdapterTimeoutError("slow", adapter="x", operation="get")
        ),
    )
    bind_service(
        server,
        "connection-error",
        handler_factory=lambda: SpyQueryHandler(
            raises=AdapterConnectionError("down", adapter="x", operation="get")
        ),
    )
    bind_service(
        server,
        "unexpected",
        handler_factory=lambda: SpyQueryHandler(raises=RuntimeError(UNEXPECTED_ERROR_SECRET)),
    )
    bind_service(server, "trace-test", handler_factory=lambda: SpyQueryHandler(response={"ok": True}))

    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


@pytest.fixture
async def grpc_server():
    server, port = await _build_server()
    yield server, port
    await server.stop(None)

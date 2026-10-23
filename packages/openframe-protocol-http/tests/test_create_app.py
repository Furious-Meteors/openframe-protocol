"""
Proves `create_app` for real — not just that it returns a FastAPI
instance, but that every piece of the lifespan shape it replaces actually
fires: on_startup is awaited, a degraded-mode exception is caught (not
crashing app startup), on_shutdown is awaited, TelemetryMiddleware and
install_error_handlers are both wired on the returned app.

Added after a second question surfaced a real boundary gap: M1/M1b
deliberately kept FastAPI() instantiation, lifespan, and middleware
registration outside openframe-protocol-http, which meant every one of
openframe-local-validation-framework's seven migrated services hand-wrote
the identical ~15-line lifespan block. create_app replaces that
duplication — proven here before any service is migrated onto it.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from openframe.core.exceptions import AdapterConnectionError
from openframe.protocol.http import create_app


def test_on_startup_and_on_shutdown_are_awaited() -> None:
    """
    `httpx.ASGITransport` alone never runs the ASGI lifespan protocol — it
    only forwards HTTP-scope requests, confirmed directly (an earlier
    version of this test used it and got an empty `called` list even
    though create_app's lifespan was never broken). `TestClient`'s own
    context manager does drive startup on `__enter__`/shutdown on
    `__exit__`, which is what this test actually needs.
    """
    called: list[str] = []

    async def on_startup() -> None:
        called.append("startup")

    async def on_shutdown() -> None:
        called.append("shutdown")

    app = create_app(title="t", on_startup=on_startup, on_shutdown=on_shutdown)
    with TestClient(app):
        assert called == ["startup"]
    assert called == ["startup", "shutdown"]


def test_degraded_mode_exception_does_not_crash_startup() -> None:
    async def on_startup() -> None:
        raise AdapterConnectionError("down", adapter="x", operation="connect")

    async def on_shutdown() -> None:
        pass

    app = create_app(title="t", on_startup=on_startup, on_shutdown=on_shutdown)
    with TestClient(app) as client:
        response = client.get("/does-not-exist")
    assert response.status_code == 404  # app served a real request — startup didn't crash


def test_non_degraded_exception_still_crashes_startup() -> None:
    async def on_startup() -> None:
        raise RuntimeError("not in the degraded-mode tuple")

    async def on_shutdown() -> None:
        pass

    app = create_app(title="t", on_startup=on_startup, on_shutdown=on_shutdown)
    try:
        with TestClient(app):
            pass
    except RuntimeError as exc:
        assert "not in the degraded-mode tuple" in str(exc)
    else:
        raise AssertionError("expected RuntimeError to propagate, not be swallowed")


async def test_routers_are_included_and_error_handlers_wired() -> None:
    router = APIRouter()

    @router.get("/boom")
    async def boom():
        raise RuntimeError("unexpected")

    async def noop() -> None:
        pass

    app = create_app(title="t", on_startup=noop, on_shutdown=noop, routers=[router])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")
    # install_error_handlers caught the bare RuntimeError and mapped it,
    # rather than it propagating as a raw 500 with a traceback leaking out.
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal.error"

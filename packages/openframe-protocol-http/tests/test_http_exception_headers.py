"""
Regression test for a real bug flagged twice during the
`openframe-protocol-media` build: `install_error_handlers`'s
`_handle_http_exception` rebuilt a raised `HTTPException` into the shared
`{"error": {...}}` envelope using only `status_code`/`content`, silently
dropping `exc.headers` — unlike FastAPI's own default `HTTPException`
handler, which does propagate them. No current binder in this repo
happened to raise an `HTTPException` with headers attached, so this went
unnoticed until `openframe-protocol-media`'s delivery binder specifically
had to route around it (returning a raw `Response` for a 416 instead of
raising `HTTPException`, to avoid losing the required `Content-Range`
header) rather than depend on it being fixed.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from openframe.protocol.http import install_error_handlers


async def test_http_exception_headers_propagate_through_envelope() -> None:
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()

    @router.get("/protected")
    async def protected() -> None:
        raise HTTPException(status_code=401, detail="missing credentials", headers={"WWW-Authenticate": "Bearer"})

    app.include_router(router)

    client = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
    response = await client.get("/protected")
    assert response.status_code == 401
    # The header is the whole point of this test — a bare envelope-shape
    # assertion wouldn't have caught the original bug.
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["message"] == "missing credentials"
    await client.aclose()


async def test_http_exception_without_headers_still_works() -> None:
    """exc.headers defaults to None — confirms JSONResponse(headers=None) is a no-op, not a crash."""
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()

    @router.get("/plain")
    async def plain() -> None:
        raise HTTPException(status_code=403, detail="forbidden")

    app.include_router(router)

    client = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
    response = await client.get("/plain")
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "forbidden"
    await client.aclose()

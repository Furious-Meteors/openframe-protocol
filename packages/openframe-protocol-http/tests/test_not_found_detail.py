"""
Proves bind_query(not_found_detail=...) — added when a second real
service (items-cached, in openframe-local-validation-framework) needed
its 404 response's `detail` string to include the handler's own input
(e.g. `"Item 'abc' not found"`), matching its pre-migration hand-written
route exactly. Without this hook, `not_found_when` only controls whether
a 404 is raised, not what its message says — the generic `"not found"`
default lost that per-route detail during migration (caught by that
service's own pre-existing test, not invented for this package).
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from openframe.protocol.http import bind_query, install_error_handlers


class _MissingHandler:
    async def execute(self, value, context=None):
        return None


async def test_not_found_detail_includes_handler_input() -> None:
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_query(
        router,
        "/items/{item_id}",
        handler_dep=lambda: _MissingHandler(),
        path_param=("item_id", str),
        not_found_when=lambda item: item is None,
        not_found_detail=lambda item_id: f"Item {item_id!r} not found",
    )
    app.include_router(router)

    client = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
    response = await client.get("/items/abc")
    assert response.status_code == 404
    assert response.json()["detail"] == "Item 'abc' not found"
    await client.aclose()


async def test_not_found_detail_defaults_to_generic_message() -> None:
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_query(
        router,
        "/items/{item_id}",
        handler_dep=lambda: _MissingHandler(),
        path_param=("item_id", str),
        not_found_when=lambda item: item is None,
    )
    app.include_router(router)

    client = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
    response = await client.get("/items/abc")
    assert response.status_code == 404
    assert response.json()["detail"] == "not found"
    await client.aclose()

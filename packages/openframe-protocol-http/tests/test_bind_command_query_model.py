"""
Proves bind_command(query_model=...) — added when a second real service
(artifacts-mongo, in openframe-local-validation-framework) needed a
command whose input is a path param plus a query param and no body at
all (`PATCH /{id}/status?status=...`), a combination items-postgres
never needed. `_bind` already handled this generically; only the public
`bind_command` wrapper hadn't exposed `query_model` yet.
"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from openframe.protocol.http import bind_command, install_error_handlers


class _StatusQuery(BaseModel):
    status: str


async def test_bind_command_with_path_param_and_query_model_and_no_body() -> None:
    received: dict = {}

    class _RecordingHandler:
        async def execute(self, value, context=None):
            received["value"] = value
            return None

    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_command(
        router,
        "/items/{item_id}/status",
        method="PATCH",
        handler_dep=lambda: _RecordingHandler(),
        path_param=("item_id", str),
        query_model=_StatusQuery,
        build_input=lambda item_id, query: (item_id, query.status),
    )
    app.include_router(router)

    client = AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
    response = await client.patch("/items/abc/status", params={"status": "done"})
    assert response.status_code == 200
    assert received["value"] == ("abc", "done")
    await client.aclose()

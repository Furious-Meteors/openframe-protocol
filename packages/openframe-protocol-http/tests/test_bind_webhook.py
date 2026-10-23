"""
Spikes bind_webhook for real — not assumed. Proves: (a) a valid
signature lets the handler run and its result comes back, (b) a missing
or invalid signature returns 401 and the handler is never called, (c) a
handler error after a valid signature flows through the normal
categorize/build_envelope path unchanged, (d) body validation failure
after a valid signature returns a normal 422 (never special-cased by
this binder), and (e) a custom verify callable (no prefix, matching a
provider with a bare hex signature) works.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from openframe.core.exceptions import AdapterConnectionError
from openframe.protocol.http import (
    bind_webhook,
    install_error_handlers,
    verify_hmac_sha256,
)
from pydantic import BaseModel

SECRET = "shh-its-a-secret"


class _OrderPaid(BaseModel):
    order_id: str
    amount: int


def _sign(body: bytes, *, prefix: str = "sha256=") -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"{prefix}{digest}"


class _RecordingHandler:
    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.received: list = []
        self._raises = raises

    async def execute(self, body, context=None):
        self.received.append(body)
        if self._raises is not None:
            raise self._raises
        return {"ok": True}


def _build_app(handler, *, verify=verify_hmac_sha256):
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_webhook(
        router,
        "/webhooks/orders",
        handler_dep=lambda: handler,
        body_model=_OrderPaid,
        signature_header="X-Signature",
        secret=SECRET,
        verify=verify,
    )
    app.include_router(router)
    return app


def test_valid_signature_calls_handler_and_returns_result() -> None:
    handler = _RecordingHandler()
    app = _build_app(handler)
    client = TestClient(app)
    payload = {"order_id": "o-1", "amount": 100}
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(handler.received) == 1
    assert handler.received[0].order_id == "o-1"


def test_missing_signature_returns_401_and_handler_not_called() -> None:
    handler = _RecordingHandler()
    app = _build_app(handler)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    resp = client.post("/webhooks/orders", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert handler.received == []


def test_invalid_signature_returns_401_and_handler_not_called() -> None:
    handler = _RecordingHandler()
    app = _build_app(handler)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": "sha256=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert handler.received == []


def test_tampered_body_fails_signature_even_with_signature_present() -> None:
    """The signature was computed over a DIFFERENT body — proves this
    actually verifies the payload, not just that a header is present."""
    handler = _RecordingHandler()
    app = _build_app(handler)
    client = TestClient(app)
    original = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    tampered = json.dumps({"order_id": "o-1", "amount": 999999}).encode()
    resp = client.post(
        "/webhooks/orders",
        content=tampered,
        headers={"X-Signature": _sign(original), "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert handler.received == []


def test_handler_error_after_valid_signature_uses_normal_error_mapping() -> None:
    handler = _RecordingHandler(raises=AdapterConnectionError("down", adapter="x", operation="pay"))
    app = _build_app(handler)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 503, "AdapterConnectionError must categorise UNAVAILABLE -> 503"
    assert len(handler.received) == 1, "the handler DID run — this is a post-verification failure"


def test_invalid_body_after_valid_signature_returns_422() -> None:
    """
    raise_server_exceptions=False, same reasoning as the conformance
    driver's own httpx.ASGITransport(raise_app_exceptions=False):
    pydantic.ValidationError is only caught by install_error_handlers'
    bare @app.exception_handler(Exception) when the test client doesn't
    re-raise it into the test itself first — confirmed directly, not
    assumed, after this test initially failed with the raw
    ValidationError surfacing in the test instead of a 422 response.
    """
    handler = _RecordingHandler()
    app = _build_app(handler)
    client = TestClient(app, raise_server_exceptions=False)
    body = json.dumps({"order_id": "o-1"}).encode()  # missing required "amount"
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert handler.received == []


def test_body_over_max_size_rejected_before_handler_runs() -> None:
    handler = _RecordingHandler()
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_webhook(
        router,
        "/webhooks/orders",
        handler_dep=lambda: handler,
        body_model=_OrderPaid,
        signature_header="X-Signature",
        secret=SECRET,
        max_body_size=10,
    )
    app.include_router(router)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert handler.received == []


def test_body_under_max_size_accepted() -> None:
    handler = _RecordingHandler()
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_webhook(
        router,
        "/webhooks/orders",
        handler_dep=lambda: handler,
        body_model=_OrderPaid,
        signature_header="X-Signature",
        secret=SECRET,
        max_body_size=10_000,
    )
    app.include_router(router)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert len(handler.received) == 1


def _build_app_with_timestamp(handler, *, replay_window_seconds: float = 300.0):
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_webhook(
        router,
        "/webhooks/orders",
        handler_dep=lambda: handler,
        body_model=_OrderPaid,
        signature_header="X-Signature",
        secret=SECRET,
        timestamp_header="X-Timestamp",
        replay_window_seconds=replay_window_seconds,
    )
    app.include_router(router)
    return app


def test_missing_timestamp_rejected_when_timestamp_header_configured() -> None:
    handler = _RecordingHandler()
    app = _build_app_with_timestamp(handler)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert handler.received == []


def test_stale_timestamp_outside_replay_window_rejected() -> None:
    import time

    handler = _RecordingHandler()
    app = _build_app_with_timestamp(handler, replay_window_seconds=60)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    stale = str(time.time() - 3600)  # one hour old, window is 60s
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": _sign(body), "Content-Type": "application/json", "X-Timestamp": stale},
    )
    assert resp.status_code == 401
    assert handler.received == []


def test_fresh_timestamp_within_replay_window_accepted() -> None:
    import time

    handler = _RecordingHandler()
    app = _build_app_with_timestamp(handler, replay_window_seconds=300)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={
            "X-Signature": _sign(body),
            "Content-Type": "application/json",
            "X-Timestamp": str(time.time()),
        },
    )
    assert resp.status_code == 200
    assert len(handler.received) == 1


def test_is_replay_hook_rejects_a_previously_seen_signature() -> None:
    seen: set[str] = set()

    async def _is_replay(signature: str) -> bool:
        if signature in seen:
            return True
        seen.add(signature)
        return False

    handler = _RecordingHandler()
    app = FastAPI()
    install_error_handlers(app)
    router = APIRouter()
    bind_webhook(
        router,
        "/webhooks/orders",
        handler_dep=lambda: handler,
        body_model=_OrderPaid,
        signature_header="X-Signature",
        secret=SECRET,
        is_replay=_is_replay,
    )
    app.include_router(router)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    headers = {"X-Signature": _sign(body), "Content-Type": "application/json"}

    first = client.post("/webhooks/orders", content=body, headers=headers)
    assert first.status_code == 200
    assert len(handler.received) == 1

    second = client.post("/webhooks/orders", content=body, headers=headers)
    assert second.status_code == 401
    assert len(handler.received) == 1, "replayed delivery must not reach the handler a second time"


def test_custom_verify_with_no_prefix() -> None:
    def _verify_no_prefix(*, secret: str, raw_body: bytes, signature: str) -> None:
        verify_hmac_sha256(secret=secret, raw_body=raw_body, signature=signature, prefix="")

    handler = _RecordingHandler()
    app = _build_app(handler, verify=_verify_no_prefix)
    client = TestClient(app)
    body = json.dumps({"order_id": "o-1", "amount": 100}).encode()
    bare_signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhooks/orders",
        content=body,
        headers={"X-Signature": bare_signature, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert len(handler.received) == 1

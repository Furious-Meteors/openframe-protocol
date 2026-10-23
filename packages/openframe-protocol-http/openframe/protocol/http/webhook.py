"""
openframe/protocol/http/webhook.py
=====================================
`bind_webhook` — register a webhook receiver backed by a
`CommandHandler`/`UseCase`, verifying an HMAC signature against the
**raw** request body before the handler ever runs.

Deliberately NOT a `SessionHandler` (ADR-008) — a webhook is a one-shot
exchange, one HTTP POST in, one ack out, exactly the shape
`CommandHandler`/`UseCase` already fit (confirmed by the design spec's
own text, §5.5: "`CommandHandler`/`QueryHandler` fit one-shot exchanges
— HTTP, gRPC, webhooks..."). The one thing a webhook receiver needs that
`bind_command` doesn't provide: proving the payload actually came from
the claimed sender, not just that it's well-formed JSON — which needs
the **raw** bytes, read once, verified, then parsed from those same
bytes (not re-serialized from an already-parsed body, which wouldn't
byte-match what was actually signed).

A module of `-http`, not its own package — per the design spec's own
placement decision ("SSE and webhook receivers as `server.py` modules"),
alongside `sse.py`/`websocket.py`.

**Signature verification is deliberately not universal.** Providers
differ: GitHub/Shopify sign the raw body directly with HMAC-SHA256,
hex-encoded, often prefixed (`"sha256=..."`); Stripe signs a
timestamp+payload concatenation instead. `verify_hmac_sha256` here
covers the common GitHub/Shopify-style case; a provider with a different
scheme supplies its own `verify` callable to `bind_webhook` rather than
this module trying to special-case every provider's own format.

A signature failure maps straight to a 401 here, not through
`categorize()`/`build_envelope()` — this is an authentication concern
specific to this one binder, not a generic error shape a
`CommandHandler`/`UseCase` would also produce, so it doesn't need a new
`ErrorCategory` member (the existing five don't draw an
authenticated/unauthenticated distinction, and adding one for a single
binder's own pre-handler check would be the kind of addition `-base`'s
own rule-of-three discipline warns against). Any error the *handler*
itself raises after signature verification passes still flows through
`install_error_handlers`'s normal path unchanged.

Dependency order:
    protocol/http/webhook → protocol/base/context, openframe.core.inbound
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from openframe.core.inbound import RequestContext
from openframe.protocol.base.context import ContextEnricher, build_request_context
from pydantic import BaseModel

__all__ = ["SignatureVerificationError", "bind_webhook", "verify_hmac_sha256"]

_logger = logging.getLogger(__name__)


class SignatureVerificationError(Exception):
    """Raised by a `verify` callable when a webhook's signature does not match."""


def verify_hmac_sha256(*, secret: str, raw_body: bytes, signature: str, prefix: str = "sha256=") -> None:
    """
    Default verifier — the common GitHub/Shopify-style scheme: HMAC-SHA256
    over the raw body, hex-encoded, optionally prefixed.

    Uses `hmac.compare_digest` — a plain `==` on digests is a timing-attack
    surface; this is the standard library's own constant-time comparison.

    Args:
        secret:    The shared webhook secret.
        raw_body:  The exact bytes the sender signed — not a re-serialized
                   parse of them.
        signature: The header value to check, e.g. `"sha256=abcd1234..."`.
        prefix:    Stripped from `signature` before comparing, if present.
                   Pass `""` for a provider that sends the bare hex digest
                   with no prefix.

    Raises:
        SignatureVerificationError: If the signature does not match.
    """
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    candidate = signature[len(prefix):] if prefix and signature.startswith(prefix) else signature
    if not hmac.compare_digest(expected, candidate):
        raise SignatureVerificationError("webhook signature mismatch")


def bind_webhook(
    router: APIRouter,
    path: str,
    *,
    handler_dep: Callable[..., Any],
    body_model: type[BaseModel],
    signature_header: str,
    secret: str,
    verify: Callable[..., None] = verify_hmac_sha256,
    status_code: int = 200,
    context_enricher: ContextEnricher | None = None,
    max_body_size: int | None = None,
    timestamp_header: str | None = None,
    replay_window_seconds: float = 300.0,
    is_replay: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """
    Register a POST route at *path* backed by a `CommandHandler`/`UseCase`,
    verifying *signature_header*'s value against the raw request body
    with *verify* before the handler ever runs.

    Args:
        router:           The `APIRouter` to register onto.
        path:             Route path, e.g. `"/webhooks/github"`.
        handler_dep:      A FastAPI dependency resolving to the handler
                          instance for this request — same convention as
                          `bind_command`'s own `handler_dep`.
        body_model:       The payload's Pydantic model. Parsed directly
                          from the same raw bytes the signature was
                          checked against (`model_validate_json`), not
                          FastAPI's own `Body(...)` extraction — there is
                          only one read of the body, used for both.
        signature_header: The request header carrying the signature, e.g.
                          `"X-Hub-Signature-256"`.
        secret:           The shared webhook secret passed to *verify*.
        verify:           `(secret, raw_body, signature) -> None`, raising
                          `SignatureVerificationError` on mismatch.
                          Defaults to `verify_hmac_sha256`; a provider
                          with a different scheme (e.g. Stripe's
                          timestamp+payload concatenation) supplies its
                          own.
        status_code:      Defaults to 200 — most webhook providers just
                          need an ack, not a particular response shape.
        max_body_size:    Rejects (413) a request whose body exceeds this
                          many bytes, checked against `Content-Length`
                          *before* the body is read, and again against the
                          actual bytes read (a sender can omit or lie about
                          `Content-Length`, or use chunked transfer, which
                          has none at all). `None` (the default) performs
                          no check — the previous behaviour, which read an
                          unbounded body into memory before anything else
                          ran.
        timestamp_header: If given, the request header carrying a Unix
                          timestamp (seconds) the sender signed alongside
                          the body — checked to be within
                          `replay_window_seconds` of now *before* signature
                          verification runs, rejecting (401) a request
                          whose timestamp is missing, unparseable, or too
                          old/far in the future. `None` (the default)
                          performs no check, since not every provider
                          sends one (GitHub/Shopify's own scheme doesn't;
                          Stripe's does, as `Stripe-Signature`'s own
                          `t=...` field).
        replay_window_seconds: How far from now `timestamp_header`'s value
                          may be, in either direction. Only meaningful
                          when `timestamp_header` is given.
        is_replay:        An optional `(signature) -> bool` hook (awaited)
                          checked *after* signature verification succeeds
                          — return `True` if this exact signature has
                          already been accepted, rejecting the request
                          (401) instead of invoking the handler again. A
                          timestamp window alone only bounds *how long* a
                          captured request stays replayable, not whether
                          it's replayed within that window; this is a
                          dedup hook, not a dedup *implementation* — this
                          stateless binder has nowhere of its own to keep
                          a "seen signatures" set, so the caller supplies
                          one (a cache, a database row, whatever already
                          exists for this service). `None` (the default)
                          performs no check, the previous behaviour.

    A missing or invalid signature returns 401 **before** `body_model`
    validation or the handler ever runs — a sender that can't prove who
    it is doesn't get to cause a validation error or a handler side
    effect either.
    """

    async def endpoint(request: Request, __handler__: Any = Depends(handler_dep)) -> Any:
        if max_body_size is not None:
            content_length = request.headers.get("content-length")
            if content_length is not None and content_length.isdigit() and int(content_length) > max_body_size:
                raise HTTPException(status_code=413, detail="webhook payload too large")

        raw_body = await request.body()
        if max_body_size is not None and len(raw_body) > max_body_size:
            # Content-Length was absent, non-numeric, or understated (e.g.
            # chunked transfer) — the check above couldn't catch this one.
            raise HTTPException(status_code=413, detail="webhook payload too large")

        signature = request.headers.get(signature_header)
        if signature is None:
            raise HTTPException(status_code=401, detail="missing webhook signature")

        if timestamp_header is not None:
            raw_timestamp = request.headers.get(timestamp_header)
            if raw_timestamp is None:
                raise HTTPException(status_code=401, detail="missing webhook timestamp")
            try:
                sent_at = float(raw_timestamp)
            except ValueError:
                raise HTTPException(status_code=401, detail="invalid webhook timestamp") from None
            if abs(time.time() - sent_at) > replay_window_seconds:
                raise HTTPException(status_code=401, detail="webhook timestamp outside replay window")

        try:
            verify(secret=secret, raw_body=raw_body, signature=signature)
        except SignatureVerificationError:
            raise HTTPException(status_code=401, detail="invalid webhook signature") from None

        if is_replay is not None and await is_replay(signature):
            raise HTTPException(status_code=401, detail="webhook already processed")

        body = body_model.model_validate_json(raw_body)
        context: RequestContext = build_request_context(dict(request.headers), context_enricher=context_enricher)
        return await __handler__.execute(body, context)

    router.add_api_route(path, endpoint, methods=["POST"], status_code=status_code)

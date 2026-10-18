"""
openframe.protocol.http
==========================
HTTP/ASGI binding, both directions (draft 7/8's redefinition):

- `bind_query`/`bind_command`/`install_error_handlers` (`server.py`,
  needs the `[server]` extra, i.e. FastAPI) — wire a `QueryHandler`/
  `CommandHandler`/`UseCase` to a route through the shared error mapper
  and `RequestContext` builder in `openframe.protocol.base`.
- `HttpRemoteQueryClient`/`HttpRemoteCommandClient` (`client.py`, needs
  the `[client]` extra, i.e. httpx) — call another service built on
  this platform, the outbound counterpart.

Lazily imported (PEP 562, `__getattr__` below) rather than eagerly
importing both submodules here — `server.py` needs FastAPI and
`client.py` needs httpx, and eagerly importing both from this file
would mean installing *either* extra alone still fails at
`import openframe.protocol.http` with the *other* extra's missing
dependency. Found and fixed before this ever shipped: both submodules
were originally imported unconditionally here, silently defeating the
`[server]`/`[client]` extras split this package's own `pyproject.toml`
already promised.

.. stability: experimental
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openframe.protocol.http.server import bind_command, bind_query, create_app, install_error_handlers
    from openframe.protocol.http.client import HttpRemoteCommandClient, HttpRemoteQueryClient, RequestSpec
    from openframe.protocol.http.sse import SseSession, bind_session
    from openframe.protocol.http.websocket import WebSocketSession, bind_websocket
    from openframe.protocol.http.webhook import SignatureVerificationError, bind_webhook, verify_hmac_sha256

__all__ = [
    "bind_query",
    "bind_command",
    "install_error_handlers",
    "create_app",
    "HttpRemoteQueryClient",
    "HttpRemoteCommandClient",
    "RequestSpec",
    "bind_session",
    "SseSession",
    "bind_websocket",
    "WebSocketSession",
    "bind_webhook",
    "verify_hmac_sha256",
    "SignatureVerificationError",
]

_FROM_SERVER = {"bind_query", "bind_command", "install_error_handlers", "create_app"}
_FROM_CLIENT = {"HttpRemoteQueryClient", "HttpRemoteCommandClient", "RequestSpec"}
_FROM_SSE = {"bind_session", "SseSession"}
_FROM_WEBSOCKET = {"bind_websocket", "WebSocketSession"}
_FROM_WEBHOOK = {"bind_webhook", "verify_hmac_sha256", "SignatureVerificationError"}


def __getattr__(name: str):
    if name in _FROM_SERVER:
        from openframe.protocol.http import server

        return getattr(server, name)
    if name in _FROM_CLIENT:
        from openframe.protocol.http import client

        return getattr(client, name)
    if name in _FROM_SSE:
        # Needs the [server] extra (FastAPI) — same as _FROM_SERVER, not
        # its own extra, since SSE's dependency footprint is identical.
        from openframe.protocol.http import sse

        return getattr(sse, name)
    if name in _FROM_WEBSOCKET:
        # Same [server] extra as SSE — Starlette's own WebSocket support
        # needs no package beyond FastAPI itself to build or test against.
        from openframe.protocol.http import websocket

        return getattr(websocket, name)
    if name in _FROM_WEBHOOK:
        # Same [server] extra — hmac/hashlib are stdlib, no new dependency.
        from openframe.protocol.http import webhook

        return getattr(webhook, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

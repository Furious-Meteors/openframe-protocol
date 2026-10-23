"""
openframe/protocol/http/quic.py
==================================
`create_quic_config` — builds the `hypercorn.Config` needed to serve an
`openframe.protocol.http.create_app()` app over HTTP/3 (QUIC), via
Hypercorn (the ASGI server family with native QUIC support — `uvicorn`
does not have one).

**No `bind_quic` here, by design, confirmed by a real spike, not
assumed**: QUIC is a transport-layer concern, not a new binding concern.
A real end-to-end round trip (a `bind_query`-bound route, served by
Hypercorn with this config, called over a real QUIC connection with a
real HTTP/3 client) confirmed `create_app`'s output — `bind_query`,
`bind_command`, `install_error_handlers`, `build_request_context`
(correlation id generation included) — needs **zero changes** to work
under HTTP/3. Unlike SSE/WebSocket/webhooks (each a genuinely new way an
inbound event reaches a handler), serving the exact same ASGI app over a
different transport is purely the host server's job — the same reason
`create_app` itself never calls `uvicorn.run()`.

This module owns exactly the one piece of real, repeatable boilerplate a
caller would otherwise get wrong by hand: `quic_bind` must mirror `bind`,
and `alpn_protocols` must include `"h3"` — both easy to forget, neither
interesting enough to leave as copy-pasted setup in every service.
Everything else (actually calling `hypercorn.asyncio.serve(app, config)`,
owning the process) stays the caller's own `main.py`, same boundary
`create_app` already draws for uvicorn.

Needs the `[quic]` extra (`hypercorn[h3]`) — not `[server]`, since most
services never need QUIC and `hypercorn`/`aioquic` are a meaningfully
heavier dependency than FastAPI alone.

Dependency order:
    protocol/http/quic → (no openframe.protocol.base dependency — this
    module only builds a Config object, it doesn't touch request
    handling at all)
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypercorn.config import Config

__all__ = ["create_quic_config"]


def create_quic_config(
    *,
    bind: str,
    certfile: str,
    keyfile: str,
    alpn_protocols: Sequence[str] = ("h3",),
) -> Config:
    """
    Build a `hypercorn.Config` for serving a `create_app()` app over
    HTTP/3 (QUIC).

    Args:
        bind:           Host:port to bind both the QUIC (UDP) and the
                        TCP fallback listener to, e.g. `"0.0.0.0:443"` —
                        HTTP/3 deployments conventionally serve HTTP/1.1
                        or HTTP/2 on the same port as a fallback for
                        clients that haven't yet discovered QUIC support
                        (via the `Alt-Svc` header Hypercorn sends
                        automatically once QUIC is configured).
        certfile:       Path to a TLS certificate — HTTP/3 mandates TLS;
                        there is no plaintext QUIC mode.
        keyfile:        Path to the certificate's private key.
        alpn_protocols: Defaults to `("h3",)`. Override only to also
                        advertise HTTP/1.1 (`"http/1.1"`) for the TCP
                        fallback listener's own ALPN negotiation — not
                        needed for QUIC itself, which always uses `h3`.

    Returns:
        A `hypercorn.Config` ready to pass to
        `hypercorn.asyncio.serve(app, config)` — this function builds
        the config only; the caller still owns calling `serve()` and
        running the process, the same boundary `create_app` already
        draws for uvicorn.

    Usage::

        from hypercorn.asyncio import serve
        from openframe.protocol.http import create_app
        from openframe.protocol.http.quic import create_quic_config

        app = create_app(title="my-service", on_startup=..., on_shutdown=..., routers=[router])
        config = create_quic_config(bind="0.0.0.0:443", certfile="cert.pem", keyfile="key.pem")
        await serve(app, config)
    """
    from hypercorn.config import Config

    config = Config()
    config.bind = [bind]
    config.quic_bind = [bind]
    config.certfile = certfile
    config.keyfile = keyfile
    config.alpn_protocols = list(alpn_protocols)
    return config

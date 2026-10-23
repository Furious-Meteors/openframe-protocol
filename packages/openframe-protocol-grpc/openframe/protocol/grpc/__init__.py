"""
openframe.protocol.grpc
==========================
gRPC binding, both directions, same two-file schema `-http` established:

- `bind_service` (`server.py`, needs the `[server]` extra) — wire a
  `QueryHandler`/`CommandHandler`/`UseCase` to a `grpc.aio.Server` through
  the shared error mapper and `RequestContext` builder in
  `openframe.protocol.base`.
- `GrpcRemoteQueryClient`/`GrpcRemoteCommandClient` (`client.py`, needs
  the `[client]` extra) — call another service built on this platform,
  the outbound counterpart.

Unlike `-http`, both extras pull in the same underlying dependency
(`grpcio`) — there is no separate client-only wire library the way httpx
is separate from FastAPI. The `[server]`/`[client]` split is kept anyway,
for the same reason `-http` has it: so a package that only ever calls out
(never serves) doesn't need to reason about why a server dependency is
present, even if today that dependency happens to be identical.

Lazily imported (PEP 562, `__getattr__` below), matching `-http`'s own
`__init__.py` exactly — not because the dependency differs here, but
because the *schema* is the point: every future `openframe-protocol-*`
package follows this same lazy-import shape regardless of whether a given
transport's own extras happen to overlap.

`generic_pb2`/`generic_pb2_grpc` (the compiled `.proto` stubs) are
re-exported too, but not lazily — they need `grpcio` either way, the same
as everything else in this package, so there is no extras boundary for
them to respect.

.. stability: experimental
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from openframe.protocol.grpc import generic_pb2, generic_pb2_grpc

if TYPE_CHECKING:
    from openframe.protocol.grpc.client import (
        GrpcRemoteCommandClient,
        GrpcRemoteQueryClient,
    )
    from openframe.protocol.grpc.server import (
        ROUTE_METADATA_KEY,
        GrpcSession,
        bind_service,
        bind_service_stream,
        create_server,
    )

__all__ = [
    "ROUTE_METADATA_KEY",
    "GrpcRemoteCommandClient",
    "GrpcRemoteQueryClient",
    "GrpcSession",
    "bind_service",
    "bind_service_stream",
    "create_server",
    "generic_pb2",
    "generic_pb2_grpc",
]

_FROM_SERVER = {"bind_service", "bind_service_stream", "GrpcSession", "ROUTE_METADATA_KEY", "create_server"}
_FROM_CLIENT = {"GrpcRemoteQueryClient", "GrpcRemoteCommandClient"}


def __getattr__(name: str):
    if name in _FROM_SERVER:
        from openframe.protocol.grpc import server

        return getattr(server, name)
    if name in _FROM_CLIENT:
        from openframe.protocol.grpc import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

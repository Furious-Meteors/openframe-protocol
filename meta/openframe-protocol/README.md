# openframe-protocol

Meta-package for the OpenFrame communication protocol family. Installs
`openframe-protocol-base` (shared mechanics) by default; install a
transport's extra for its binding package.

```
pip install "openframe-protocol[http]"
```

## Inventory

| Package | Extra | Status |
|---|---|---|
| `openframe-protocol-base` | (default) | 0.4.2 |
| `openframe-protocol-http` | `[http]` | 0.11.0 — `bind_query`/`bind_command`, SSE/WebSocket sessions, webhooks, HTTP/3 over QUIC |
| `openframe-protocol-grpc` | `[grpc]` | 0.4.2 — `bind_service` + `bind_service_stream` (gRPC streaming sessions) |

See the repo's `PLAN.md` and the design spec for what's built vs. still
only named (MCP, A2A, SMTP/FTP, media protocols — on demonstrated need).

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
| `openframe-protocol-base` | (default) | 0.1.0 |
| `openframe-protocol-http` | `[http]` | 0.1.0 — M1 of the project's own build plan |

See the repo's `PLAN.md` and the design spec for what's built vs. still
only named (gRPC, MCP, sessions, media protocols — all out of scope for
this first release).

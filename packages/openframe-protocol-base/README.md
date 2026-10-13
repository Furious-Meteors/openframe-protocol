# openframe-protocol-base

Shared, transport-neutral mechanics every `openframe-protocol-*` binding
package builds on. No transport dependencies of its own.

- `errors.py` — `categorize(exc) -> ErrorCategory` and `build_envelope(exc)`,
  the one place every transport maps `OpenFrameError` (and anything else)
  to a caller-safe error shape.
- `context.py` — `build_request_context(headers)`, building
  `openframe.core.inbound.RequestContext` consistently across transports
  (trace id, then a correlation header, then a generated uuid4).
- `testing.py` — `TransportConformanceTests` + `TransportDriver`, the
  suite every binding proves itself against.

## Wiring example

```python
from openframe.protocol.base import build_envelope, build_request_context

context = build_request_context(request.headers)
try:
    result = await handler.execute(command, context)
except Exception as exc:
    envelope = build_envelope(exc, correlation_id=context.correlation_id)
    # serialise envelope in whatever shape this transport uses
```

## Status

Experimental — incubating the inbound conformance suite here until two
transports (HTTP, gRPC) both pass it for real, per the project's own
rule-of-three discipline for promoting shared infrastructure into
`openframe-core`.

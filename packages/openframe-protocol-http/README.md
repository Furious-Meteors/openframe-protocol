# openframe-protocol-http

HTTP/ASGI server binding. Wires a `QueryHandler`/`CommandHandler`/`UseCase`
(`openframe.core.inbound`) to a FastAPI route through the shared error
mapper and `RequestContext` builder in `openframe-protocol-base`, instead
of each route hand-mapping `HTTPException`s itself.

Install the `[server]` extra for the FastAPI/ASGI dependencies —
`openframe-protocol-http[server]`.

## Wiring example

```python
from openframe.protocol.http import bind_query, bind_command, install_error_handlers

router = APIRouter(prefix="/items")

bind_query(
    router, "/{item_id}",
    handler_dep=get_get_item_handler,
    path_param=("item_id", str),
    response_model=Item,
    not_found_when=lambda item: item is None,
)

bind_command(
    router, "", method="POST",
    handler_dep=get_create_item_handler,
    body_model=Item,
    status_code=201,
    response_model=Item,
)

install_error_handlers(app)
```

## Status

Experimental, M1 of the project's own build plan — proves the inbound
handler contract survives a real transport before anything else
(HTTP client, gRPC, sessions) is attempted.

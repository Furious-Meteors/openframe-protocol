"""
openframe/protocol/media/ingest.py
======================================
RTMP ingest binding — wires a `SessionHandler` (`openframe.core.inbound`,
ADR-008) to a real RTMP publish connection via `pyrtmp` 0.3.1, the first
binary-payload transport in the `openframe-protocol-*` family.

Everything below was confirmed against a real spike — a real `pyrtmp`
server receiving a real publish from real `ffmpeg` — not assumed from the
RTMP spec or `pyrtmp`'s own README. Several of the real behaviours are
surprising enough that they shape this module's structure directly:

1. **`on_ns_publish` IS the success-response send.**
   `SimpleRTMPController.on_ns_publish` itself calls
   `session.write_chunk_to_stream(message.create_response())` then
   `await session.drain()` — the "accept" response is sent *by the base
   method*, not by some separate "accept this session" call. `_MediaController`
   therefore runs its own accept logic (`open_session`) first, and only
   calls `await super().on_ns_publish(session, message)` — committing the
   real `NetStream.Publish.Start` response to the wire — *after*
   `open_session` has actually succeeded. Calling it first, then rejecting,
   would be exactly the kind of commit-before-decide bug
   `openframe.protocol.base.session`'s own module docstring warns about
   for SSE/WebSocket; the same timing discipline applies here.

2. **A plain `raise` from `on_ns_publish` sends NOTHING to the client.**
   `pyrtmp`'s own `SimpleRTMPController.client_callback` wraps the whole
   per-chunk dispatch loop in `except Exception as ex: logger.exception(ex)`
   — an exception from any hook is logged and the loop simply ends; no
   RTMP-level response is ever written. Confirmed against real `ffmpeg`:
   raising from `on_ns_publish` produces a bare "Input/output error" on
   the publisher's side, no diagnostic at all. To send a real rejection,
   this module hand-builds an AMF0 `onStatus` message with `level="error"`
   itself (`_write_onstatus`) — there is no built-in reject/error-send
   helper in `pyrtmp`. Read directly from `pyrtmp.messages.command.NSPublish
   .create_response()` (the real source, not guessed) to get this
   construction right: `BitStream` comes from the third-party `bitstring`
   package (NOT `pyamf` — there is no `pyamf` dependency anywhere in
   `pyrtmp`), and `AMF0Serializer` lives at `pyrtmp.amf.serializers`, not
   `pyrtmp.amf` itself (`pyrtmp/amf/__init__.py` is empty — `from
   pyrtmp.amf import AMF0Serializer` would raise `ImportError`). `Chunk`
   itself is `pyrtmp.messages.Chunk` (re-exported by `pyrtmp.rtmp`, so
   importing it from either module works, but the canonical definition
   lives in `pyrtmp.messages`). Confirmed working against real `ffmpeg`:
   with this message sent, ffmpeg prints `[rtmp] Server error: <description>`
   and exits cleanly, instead of a bare I/O error.

3. **`self.transport` does not exist on the controller.** The real
   `asyncio.Transport` is reachable as `session.writer.transport`, where
   `session` is the `pyrtmp.session_manager.SessionManager` passed into
   every hook (it owns `.reader`/`.writer`, both real `asyncio.Stream*`
   objects). `session.writer.transport.pause_reading()`/`.resume_reading()`
   are confirmed to work correctly — zero frames delivered during a pause,
   full resumption on `resume_reading()`, with the accumulated backlog
   arriving as a tight burst, not paced. `MediaFlowController` (`flow.py`)
   is wired to exactly these two callables, never to anything named
   `self.transport`.

4. **`connection_lost(exc)` does NOT reliably distinguish a crashed/killed
   publisher from a graceful stop** — real SIGTERM and SIGKILL to a real
   `ffmpeg` both produced `exc=None` on the underlying protocol. **The
   only teardown hook `pyrtmp` calls on EVERY disconnect path is
   `cleanup(self, session)`** — it's in `SimpleRTMPController
   .client_callback`'s own `finally` block, unconditionally.
   `on_stream_closed` only fires for a clean EOF (a real
   `StreamClosedException`, raised by `BitStreamReader.read()` when the
   socket returns zero bytes) and is silently skipped for a genuine
   network-level reset (a raw `ConnectionResetError`/similar, caught by
   `client_callback`'s own bare `except Exception` branch, which does
   NOT call `on_stream_closed`). This module's session teardown
   therefore lives in `cleanup()`, never in `on_stream_closed`. Since a
   crashed vs. a cleanly-stopped publisher cannot be reliably
   distinguished at this layer, `cleanup()` always treats the session as
   ending with `CloseReason.COMPLETED` (via `flow.feed_eof()`, which
   drains whatever's buffered and lets `run_opened_session`'s own
   `async for` loop end normally) — this is a known, accepted limitation,
   not something engineered around. A transport that genuinely needs to
   tell a crash apart from a clean stop would need an application-level
   signal (e.g. an explicit "done" marker in the stream) that RTMP itself
   doesn't provide.

5. **`run_opened_session` is used here directly, unlike gRPC's
   `bind_service_stream`.** gRPC's own `server.py` docstring explains why
   it does NOT reuse `run_opened_session` wholesale: `grpc.aio` gives two
   independently-scheduled signals for the same disconnect (the inbound
   `request_iterator` ending, and `context.cancelled()`), which can race
   each other. RTMP ingest has no such race — `cleanup()` is the single,
   unambiguous "this connection is over" signal (see finding 4 above),
   and `MediaFlowController.feed_eof()` is exactly the kind of single
   `inbound_events` async-iterator `run_opened_session` was designed to
   consume. There is one inbound-dominant stream (media frames flowing in
   from the publisher) and effectively no meaningful outbound stream (see
   `RtmpSession.send`'s own docstring) — the simplest shape that fits,
   confirmed correct by this design rather than assumed.

6. **`Session.close()` must never eagerly close the TCP writer** — found
   while building the conformance test suite for this module, by
   reproducing a real `ConnectionResetError`, not by inspection.
   `openframe.protocol.base.session.open_session`'s own reject path calls
   `handler.on_close(session, CloseReason.ERROR)` *and*
   `session.close(CloseReason.ERROR)` itself, **before** re-raising the
   handler's exception back to the caller — i.e. before
   `_MediaController.on_ns_publish`'s own `except` branch ever gets a
   chance to hand-build and send the real `onStatus(level="error")`
   rejection (finding 2 above). An earlier version of `RtmpSession.close()`
   closed `session.writer` immediately and unconditionally; the
   subsequent error-send's own `await session.drain()` then raised
   `ConnectionResetError` against an already-closing socket, confirmed by
   reproducing it directly against a real connection, not assumed.
   `RtmpSession.close()` therefore only tracks the idempotent "closed"
   flag — actual TCP teardown is left to `pyrtmp`'s own
   `SimpleRTMPController.client_callback`, which unconditionally closes
   the writer right after `cleanup()` returns on every path (accept,
   reject, clean end, crash) regardless of what any hook does.

Frame parsing (`_parse_video_frame`/`_parse_audio_frame`) isolates all
FLV-tag-specific bit-twiddling in this module — `openframe.core.media
.MediaFrame` never sees the raw FLV envelope. Confirmed against real
H.264+AAC frames from real `ffmpeg`: `message.control` is the 1-byte FLV
tag header (video: high nibble is frame type, `1`=keyframe/IDR; audio:
high nibble is sound format, `10`=AAC); `message.data` is everything
after it — for video (AVC), byte 0 is `AVCPacketType` (`0`=sequence
header/AVCDecoderConfigurationRecord, `1`=NALU data) followed by a 3-byte
composition time and length-prefixed (4-byte big-endian, NOT Annex-B
start codes) NAL units; for audio (AAC), byte 0 is `AACPacketType`
(`0`=sequence header/`AudioSpecificConfig`, `1`=raw AAC frame). The
config-frame test (`data[0] == 0`) is identical in shape for both kinds.

Dependency order:
    protocol/media/ingest → protocol/media/flow, protocol/base (context,
    errors, session), openframe.core.inbound, openframe.core.media,
    openframe.core.telemetry, pyrtmp
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from bitstring import BitStream
from openframe.core.exceptions import AdapterConnectionError
from openframe.core.inbound import CloseReason, SessionHandler
from openframe.core.media import MediaFrame, MediaKind
from openframe.core.telemetry import (
    record_lifecycle_event,
    setup_telemetry,
    shutdown_telemetry,
)
from openframe.protocol.base.context import ContextEnricher, build_request_context
from openframe.protocol.base.errors import build_envelope
from openframe.protocol.base.session import open_session, run_opened_session
from openframe.protocol.media.flow import FlowConfig, MediaFlowController
from pydantic import ValidationError
from pyrtmp.amf.serializers import AMF0Serializer
from pyrtmp.messages import Chunk
from pyrtmp.messages.audio import AudioMessage
from pyrtmp.messages.command import NCConnect, NSPublish
from pyrtmp.messages.video import VideoMessage
from pyrtmp.rtmp import RTMPProtocol, SimpleRTMPController
from pyrtmp.session_manager import SessionManager

__all__ = ["RtmpRegistry", "RtmpSession", "bind_ingest", "create_server"]

_logger = logging.getLogger(__name__)

_ONSTATUS_CHUNK_ID = 3  # same chunk id NSPublish.create_response() itself uses


async def _write_onstatus(
    session: SessionManager, msg_stream_id: int, *, level: str, code: str, description: str
) -> None:
    """
    Hand-built AMF0 `onStatus` command message — replicates
    `pyrtmp.messages.command.NSPublish.create_response()`'s own real
    construction (read from the installed package source, not guessed),
    generalised to accept an arbitrary `level`/`code`/`description` so it
    can send either a success or an error status. There is no built-in
    helper for this in `pyrtmp` itself.
    """
    data = BitStream()
    AMF0Serializer.create_object(data, "onStatus")
    AMF0Serializer.create_object(data, 0)
    AMF0Serializer.create_object(data, None)
    AMF0Serializer.create_object(data, {"level": level, "code": code, "description": description})
    chunk = Chunk(
        chunk_type=0,
        chunk_id=_ONSTATUS_CHUNK_ID,
        timestamp=0,
        msg_length=len(data.bytes),
        msg_type_id=0x14,
        msg_stream_id=msg_stream_id,
        payload=data.bytes,
    )
    session.write_chunk_to_stream(chunk)
    await session.drain()


def _parse_video_frame(message: VideoMessage) -> MediaFrame:
    """FLV video tag parsing — see this module's own docstring for the confirmed byte layout."""
    control = message.control[0]
    frame_type = (control & 0xF0) >> 4
    codec_id = control & 0x0F
    codec = "h264" if codec_id == 7 else f"video-codec-{codec_id}"
    is_config = len(message.data) > 0 and message.data[0] == 0
    is_keyframe = frame_type == 1
    return MediaFrame(
        kind=MediaKind.VIDEO,
        payload=message.data,
        timestamp_ms=message.timestamp,
        is_keyframe=is_keyframe,
        is_config=is_config,
        codec=codec,
        received_at_ns=time.perf_counter_ns(),
    )


def _parse_audio_frame(message: AudioMessage) -> MediaFrame:
    """FLV audio tag parsing — see this module's own docstring for the confirmed byte layout."""
    control = message.control[0]
    sound_format = (control & 0xF0) >> 4
    codec = "aac" if sound_format == 10 else f"audio-codec-{sound_format}"
    is_config = len(message.data) > 0 and message.data[0] == 0
    return MediaFrame(
        kind=MediaKind.AUDIO,
        payload=message.data,
        timestamp_ms=message.timestamp,
        is_keyframe=False,
        is_config=is_config,
        codec=codec,
        received_at_ns=time.perf_counter_ns(),
    )


class RtmpSession:
    """
    `Session[MediaFrame | Mapping[str, Any]]` (structural — `openframe
    .core.inbound.Session` is a `Protocol`) backed by one `pyrtmp`
    publish connection.

    `TOut` is a union, not a single type, because RTMP's own outbound
    channel for a *publish* connection is fundamentally different from
    SSE/WebSocket/gRPC's: there is no generic "send arbitrary data back
    to the publisher" channel, only AMF0 command/status messages
    (`onStatus`). `send()` therefore only actually implements the one
    outbound shape that is meaningful here — `send_error_event`'s
    hardcoded `{"error": {...}}` mapping, translated into a hand-built
    `onStatus(level="error")` message (see `_write_onstatus`). A
    `MediaFrame` is accepted by the type signature (for a hypothetical
    future bidirectional use, e.g. relaying a transcoded frame back) but
    is NOT implemented in this v1 ingest-only release — see `send`'s own
    docstring.
    """

    def __init__(self, *, context: Any, manager: SessionManager, msg_stream_id: int) -> None:
        self.context = context
        self._manager = manager
        self._msg_stream_id = msg_stream_id
        self._closed = False

    async def send(self, event: MediaFrame | Mapping[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("cannot send on a closed session")
        if isinstance(event, Mapping) and "error" in event:
            error = event["error"]
            description = f"{error.get('code', 'error')}: {error.get('message', '')}"
            await _write_onstatus(
                self._manager, self._msg_stream_id,
                level="error", code="NetStream.Publish.Error", description=description,
            )
            return
        if isinstance(event, MediaFrame):
            # v1 is ingest-only — a publish-only RTMP connection has no
            # generic outbound media channel (RTMP's own outbound
            # messages here are AMF0 status commands, not elementary
            # stream data). Reserved for a future bidirectional/relay
            # use case, not implemented now; raising rather than
            # silently dropping so a caller that expects this to work
            # finds out immediately, not via a mysteriously silent no-op.
            raise NotImplementedError(
                "RtmpSession.send() does not support outbound MediaFrame delivery — "
                "this is an ingest-only (publish) RTMP binding"
            )
        raise TypeError(f"RtmpSession.send() does not know how to send {type(event)!r}")

    async def close(self, reason: CloseReason = CloseReason.COMPLETED) -> None:
        # Deliberately does NOT close the underlying TCP writer — confirmed
        # necessary by a real spike bug, not a style choice. open_session's
        # own reject path (openframe.protocol.base.session) calls
        # session.close(CloseReason.ERROR) itself BEFORE re-raising the
        # handler's exception — i.e. before _MediaController.on_ns_publish's
        # except branch ever gets a chance to hand-build and send the real
        # onStatus(level="error") rejection (spike finding 2). Closing the
        # writer here would race that: the error write's own drain() would
        # hit an already-closing socket and raise ConnectionResetError,
        # confirmed by reproducing it directly. The real TCP teardown for
        # every path (accept, reject, clean end, crash) already happens
        # unconditionally in pyrtmp's own SimpleRTMPController.client_callback
        # (`writer.close()` right after `cleanup()` returns, in every case) —
        # this method only needs to track the idempotent "closed" flag so a
        # second send() after close() is rejected.
        if self._closed:
            return
        self._closed = True


@dataclass(frozen=True)
class _Binding:
    handler_factory: Callable[[], SessionHandler]
    context_enricher: ContextEnricher | None
    flow: FlowConfig


class RtmpRegistry:
    """
    Maps an RTMP `app` name (the first path segment of
    `rtmp://host/app/stream-key`, carried in the `connect` command's
    `command_object["app"]`) to a registered ingest binding — the RTMP
    counterpart of `openframe.protocol.grpc.server`'s own
    `_RouteRegisteringServicer` route table.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, _Binding] = {}

    def register(self, app: str, binding: _Binding) -> None:
        self._bindings[app] = binding

    def get(self, app: str) -> _Binding | None:
        return self._bindings.get(app)


def bind_ingest(
    registry: RtmpRegistry,
    app: str,
    *,
    handler_factory: Callable[[], SessionHandler],
    context_enricher: ContextEnricher | None = None,
    flow: FlowConfig = FlowConfig(),
) -> None:
    """
    Register one RTMP `app` name's ingest binding on *registry*.

    Args:
        registry:         The `RtmpRegistry` to register onto.
        app:              RTMP application name a publisher connects to —
                          `rtmp://host/<app>/<stream-key>`.
        handler_factory:  Builds a fresh `SessionHandler` for each publish
                          connection to this `app`. Called once per
                          connection, no caching — same lifecycle as
                          `bind_service_stream`'s own `handler_factory`.
        context_enricher: Optional hook to attach `principal`/`tenant`
                          once real auth exists — passed straight through
                          to `build_request_context`.
        flow:             `FlowConfig` for this app's `MediaFlowController`.
                          Defaults to `FlowConfig()`'s own defaults.
    """
    registry.register(app, _Binding(handler_factory=handler_factory, context_enricher=context_enricher, flow=flow))


class _MediaController(SimpleRTMPController):
    """
    Per-server (not per-connection) `pyrtmp` controller — `pyrtmp` itself
    constructs one `RTMPProtocol` per connection but shares a single
    controller instance across all of them (see `pyrtmp.rtmp
    .SimpleRTMPServer.create`'s own pattern, which this module's
    `create_server` replicates directly since `SimpleRTMPServer` itself
    hardcodes `SimpleRTMPController()` with no way to inject a custom
    controller). All per-connection state therefore lives on
    `session.state` (a plain `dict` every `SessionManager` already owns),
    never as an attribute on `self`.
    """

    def __init__(self, registry: RtmpRegistry) -> None:
        self._registry = registry
        self.active_sessions: set[SessionManager] = set()

    async def on_nc_connect(self, session: SessionManager, message: NCConnect) -> None:
        session.state["app"] = (message.command_object or {}).get("app", "")
        await super().on_nc_connect(session, message)

    async def on_ns_publish(self, session: SessionManager, message: NSPublish) -> None:
        app = session.state.get("app", "")
        binding = self._registry.get(app)
        correlation_id = str(uuid.uuid4())
        headers = {
            "x-correlation-id": correlation_id,
            "x-stream-key": message.publishing_name,
            "x-rtmp-app": app,
        }

        if binding is None:
            await _write_onstatus(
                session, message.msg_stream_id,
                level="error", code="NetStream.Publish.BadName",
                description=f"no ingest binding registered for app {app!r}",
            )
            return

        context = build_request_context(headers, context_enricher=binding.context_enricher)
        handler = binding.handler_factory()
        rtmp_session = RtmpSession(context=context, manager=session, msg_stream_id=message.msg_stream_id)

        try:
            # Commit point (confirmed via spike finding 1): on_ns_publish
            # IS the success-response send, so open_session (the
            # accept/reject decision) MUST run before super().on_ns_publish()
            # commits anything to the wire.
            await open_session(handler, rtmp_session)
        except Exception as exc:
            # Confirmed via spike finding 2: a plain `raise` here sends
            # nothing to the client at all. The hand-built onStatus error
            # is the only way to actually reject with a real diagnostic.
            envelope = build_envelope(exc, correlation_id=context.correlation_id)
            await _write_onstatus(
                session, message.msg_stream_id,
                level="error", code="NetStream.Publish.BadName", description=envelope.message,
            )
            # A well-behaved publisher (confirmed against real ffmpeg)
            # disconnects on its own upon seeing this. Proactively closing
            # here too is defense in depth against one that doesn't, so a
            # rejected connection never lingers indefinitely waiting for
            # chunks that will never complete a graceful handshake.
            session.writer.close()
            return

        # Accept: now safe to let the base class send the real
        # NetStream.Publish.Start response.
        await super().on_ns_publish(session, message)

        flow = MediaFlowController(
            config=binding.flow,
            # Confirmed via spike finding 3: the real asyncio.Transport is
            # session.writer.transport, never self.transport (which does
            # not exist on the controller at all).
            pause=session.writer.transport.pause_reading,
            resume=session.writer.transport.resume_reading,
        )
        session.state["rtmp_session"] = rtmp_session
        session.state["handler"] = handler
        session.state["flow"] = flow
        session.state["dispatch_task"] = asyncio.create_task(run_opened_session(handler, rtmp_session, flow))
        self.active_sessions.add(session)

    async def on_video_message(self, session: SessionManager, message: VideoMessage) -> None:
        flow: MediaFlowController | None = session.state.get("flow")
        if flow is None:
            return
        flow.offer(_parse_video_frame(message))

    async def on_audio_message(self, session: SessionManager, message: AudioMessage) -> None:
        flow: MediaFlowController | None = session.state.get("flow")
        if flow is None:
            return
        flow.offer(_parse_audio_frame(message))

    async def cleanup(self, session: SessionManager) -> None:
        # Confirmed via spike finding 4: cleanup() is the ONLY teardown
        # hook pyrtmp calls on every disconnect path, clean or crashed —
        # on_stream_closed is skipped for a genuine network-level reset.
        # Crash vs. clean stop cannot be reliably distinguished here
        # (connection_lost(exc) was exc=None for both a real SIGTERM and
        # a real SIGKILL during the spike), so every cleanup() is treated
        # as CloseReason.COMPLETED via feed_eof() — a known, accepted
        # limitation, documented rather than engineered around.
        flow: MediaFlowController | None = session.state.get("flow")
        if flow is not None:
            flow.feed_eof()
        dispatch_task: asyncio.Task | None = session.state.get("dispatch_task")
        if dispatch_task is not None:
            with contextlib.suppress(Exception):
                await dispatch_task
        self.active_sessions.discard(session)


@asynccontextmanager
async def create_server(
    *,
    address: str = "0.0.0.0",
    port: int = 1935,
    configure: Callable[[RtmpRegistry], None],
    on_startup: Callable[[], Awaitable[None]],
    on_shutdown: Callable[[], Awaitable[None]],
    degraded_mode_exceptions: tuple[type[BaseException], ...] = (AdapterConnectionError, ValidationError),
    stop_grace: float | None = 5.0,
    manage_telemetry: bool = True,
) -> AsyncIterator[asyncio.Server]:
    """
    Async context manager yielding a started `asyncio.Server` accepting
    RTMP publish connections — the RTMP counterpart of
    `openframe.protocol.grpc.server.create_server`, same telemetry-setup /
    degraded-mode-startup / telemetry-shutdown shape, including the same
    leak fix (a failed `configure`/`on_startup` tears down telemetry
    before re-raising, instead of leaking the providers `create_server`
    just started).

    `pyrtmp.rtmp.SimpleRTMPServer` is NOT used here — it hardcodes
    `SimpleRTMPController()` in its own `create()` method, with no way to
    inject `_MediaController`. This replicates the same
    `loop.create_server(lambda: RTMPProtocol(controller=...), ...)`
    pattern `SimpleRTMPServer.create` itself uses, just with our own
    controller.

    Args:
        address:     Host to bind. Defaults to `"0.0.0.0"`.
        port:        Port to bind. Defaults to RTMP's conventional `1935`;
                     pass `0` for an OS-assigned ephemeral port (the
                     pattern this repo's other conformance tests use for
                     an isolated, parallel-safe test server) — the real
                     bound port is stashed on the returned server as
                     `_openframe_bound_port`, mirroring `bind_service`'s
                     own `_openframe_bound_port` stash for the same
                     `:0`-port test need.
        configure:   Called with a fresh `RtmpRegistry` before
                     `on_startup`/binding the socket — register
                     `bind_ingest(registry, app, ...)` calls here.
        on_startup:  Awaited after `configure`. Any exception in
                     `degraded_mode_exceptions` is caught and logged as a
                     warning rather than preventing the server from
                     starting.
        on_shutdown: Awaited after the `with` block exits, before the
                     server socket is closed.
        degraded_mode_exceptions: Same default pair `create_server`
                     (gRPC)/`create_app` (HTTP) use.
        stop_grace:  Seconds to wait for in-flight publish sessions'
                     dispatch tasks to drain (via `feed_eof()`'s own
                     clean-`StopAsyncIteration` path) before cancelling
                     them outright. `None` waits indefinitely.
        manage_telemetry: Same meaning as `create_server` (gRPC)/
                     `create_app` (HTTP)'s own `manage_telemetry`.

    Usage::

        async with create_server(
            port=1935,
            configure=lambda registry: bind_ingest(registry, "live", handler_factory=get_handler),
            on_startup=dependencies.initialise,
            on_shutdown=dependencies.shutdown,
        ) as server:
            await server.serve_forever()
    """
    if manage_telemetry:
        setup_telemetry()
        record_lifecycle_event("cold_start")

    registry = RtmpRegistry()
    controller = _MediaController(registry)
    server: asyncio.Server | None = None
    try:
        configure(registry)

        try:
            await on_startup()
        except degraded_mode_exceptions as exc:
            _logger.warning("dependency not ready at startup: %s — running in degraded mode", exc)

        loop = asyncio.get_event_loop()
        server = await loop.create_server(lambda: RTMPProtocol(controller=controller), host=address, port=port)
        bound_port = server.sockets[0].getsockname()[1] if server.sockets else port
        server._openframe_bound_port = bound_port  # type: ignore[attr-defined]
    except BaseException:
        if manage_telemetry:
            shutdown_telemetry()
        raise

    try:
        yield server
    finally:
        await on_shutdown()
        server.close()

        dispatch_tasks = [
            session.state["dispatch_task"]
            for session in controller.active_sessions
            if session.state.get("dispatch_task") is not None
        ]
        if dispatch_tasks:
            _done, pending = await asyncio.wait(dispatch_tasks, timeout=stop_grace)
            for task in pending:
                task.cancel()
        for session in list(controller.active_sessions):
            with contextlib.suppress(Exception):
                session.writer.close()

        await server.wait_closed()
        if manage_telemetry:
            shutdown_telemetry()

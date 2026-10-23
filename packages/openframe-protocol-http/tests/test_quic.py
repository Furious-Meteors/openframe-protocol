"""
Spikes create_quic_config for real — not assumed. Proves a real HTTP/3
request over a real QUIC connection reaches a create_app() app's
bind_query route unchanged — no bind_quic, no changes to server.py at
all, confirmed by an actual client/server round trip (a real Hypercorn
server + a real aioquic HTTP/3 client), not by reasoning about ASGI in
the abstract.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import APIRouter
from hypercorn.asyncio import serve as hypercorn_serve
from openframe.protocol.http import bind_query, create_app
from openframe.protocol.http.quic import create_quic_config


def _generate_self_signed_cert(tmp_path):
    """Generates a localhost/127.0.0.1 self-signed cert pair — pure Python, no openssl subprocess."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile = tmp_path / "cert.pem"
    keyfile = tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(certfile), str(keyfile)


class _EchoQueryHandler:
    async def execute(self, query, context=None):
        return {"echo": "hello-over-quic", "correlation_id": context.correlation_id if context else None}


async def test_real_http3_request_reaches_bind_query_route_unchanged(tmp_path) -> None:
    from aioquic.asyncio.client import connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3Connection
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration

    certfile, keyfile = _generate_self_signed_cert(tmp_path)

    router = APIRouter()
    bind_query(router, "/ping", handler_dep=lambda: _EchoQueryHandler(), build_input=lambda: "ping")

    async def noop():
        pass

    app = create_app(title="quic-test", on_startup=noop, on_shutdown=noop, routers=[router])
    config = create_quic_config(bind="127.0.0.1:0", certfile=certfile, keyfile=keyfile)
    # Bind to an ephemeral port ourselves (matching every other conformance
    # test's own pattern) — Hypercorn's own Config doesn't resolve "the
    # real port" the way grpc.aio's add_insecure_port does, so bind
    # directly to a fixed-but-unlikely-collision high port instead; this
    # test's own isolation is per-call (a fresh event loop/task), not
    # per-worker-parallel, so a fixed port is acceptable here.
    port = 44330 + (hash(certfile) % 1000)
    config.bind = [f"127.0.0.1:{port}"]
    config.quic_bind = [f"127.0.0.1:{port}"]

    shutdown_event = asyncio.Event()
    server_task = asyncio.create_task(
        hypercorn_serve(app, config, shutdown_trigger=shutdown_event.wait)
    )
    await asyncio.sleep(0.5)  # let the server actually start listening

    class _H3ClientProtocol(QuicConnectionProtocol):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.http = H3Connection(self._quic)
            self.answer: asyncio.Future = asyncio.get_event_loop().create_future()
            self._body = b""
            self._status = None

        def quic_event_received(self, event):
            for http_event in self.http.handle_event(event):
                if isinstance(http_event, HeadersReceived):
                    for k, v in http_event.headers:
                        if k == b":status":
                            self._status = v.decode()
                elif isinstance(http_event, DataReceived):
                    self._body += http_event.data
                    if http_event.stream_ended and not self.answer.done():
                        self.answer.set_result((self._status, self._body))

        async def get(self, path: str):
            stream_id = self._quic.get_next_available_stream_id()
            self.http.send_headers(
                stream_id=stream_id,
                headers=[
                    (b":method", b"GET"),
                    (b":scheme", b"https"),
                    (b":authority", b"127.0.0.1"),
                    (b":path", path.encode()),
                ],
                end_stream=True,
            )
            self.transmit()
            return await asyncio.wait_for(self.answer, timeout=5)

    try:
        client_config = QuicConfiguration(is_client=True, alpn_protocols=["h3"])
        client_config.verify_mode = ssl.CERT_NONE
        async with connect(
            "127.0.0.1", port, configuration=client_config, create_protocol=_H3ClientProtocol
        ) as protocol:
            status, body = await protocol.get("/ping")
    finally:
        shutdown_event.set()
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except TimeoutError:
            server_task.cancel()

    assert status == "200"
    import json

    parsed = json.loads(body)
    assert parsed["echo"] == "hello-over-quic"
    assert parsed["correlation_id"] is not None, "build_request_context must still run unchanged over HTTP/3"

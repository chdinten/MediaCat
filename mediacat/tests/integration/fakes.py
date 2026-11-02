"""Test doubles for the integration harness: object store, LLM, network guard.

Nothing in here talks to the network.  :class:`NetworkGuard` makes sure the
code under test doesn't either.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json as json_mod
import random
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from PIL import Image

from mediacat.llm.adapter import LlmResponse
from mediacat.storage.object_store import (
    ALLOWED_MIME_TYPES,
    ObjectStoreError,
    StoredObject,
    _mime_to_ext,
)


def png_bytes(width: int = 64, height: int = 48, colour: str = "red") -> bytes:
    """A small valid PNG (distinct colours give distinct content hashes)."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, "PNG")
    return buf.getvalue()


def noisy_jpeg(seed: int = 0, size: int = 64) -> bytes:
    """A JPEG of random pixels: several KB, unlike :func:`png_bytes` (some code skips < 1 KB)."""
    rnd = random.Random(seed)  # noqa: S311 - test data, not crypto
    img = Image.frombytes(
        "RGB", (size, size), bytes(rnd.randrange(256) for _ in range(size * size * 3))
    )
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class FakeObjectStore:
    """In-memory stand-in for :class:`mediacat.storage.object_store.ObjectStore`.

    Same public methods and the same content checks as the MinIO store
    (MIME allowlist, Pillow validation, SHA-256 keys), so upload handlers
    take their real code paths.  ``objects`` maps ``(bucket, key)`` to bytes.
    """

    def __init__(self, default_bucket: str = "media-originals") -> None:
        self.default_bucket = default_bucket
        self.objects: dict[tuple[str, str], bytes] = {}
        self.mime_types: dict[tuple[str, str], str] = {}
        self.buckets: set[str] = set()

    async def ensure_bucket(self, bucket: str | None = None) -> None:
        self.buckets.add(bucket or self.default_bucket)

    async def put_image(
        self,
        data: bytes,
        mime_type: str,
        *,
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        if mime_type not in ALLOWED_MIME_TYPES:
            msg = f"Rejected MIME type {mime_type!r}"
            raise ObjectStoreError(msg)
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()
            width, height = Image.open(io.BytesIO(data)).size
        except Exception as exc:
            msg = f"Image validation failed: {exc}"
            raise ObjectStoreError(msg) from exc
        content_hash = hashlib.sha256(data).hexdigest()
        bucket = bucket or self.default_bucket
        key = f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}{_mime_to_ext(mime_type)}"
        self.objects[(bucket, key)] = data
        self.mime_types[(bucket, key)] = mime_type
        return StoredObject(
            content_hash=content_hash,
            bucket=bucket,
            object_key=key,
            size_bytes=len(data),
            mime_type=mime_type,
            width_px=width,
            height_px=height,
        )

    async def put_object(
        self, object_key: str, data: bytes, mime_type: str, *, bucket: str | None = None
    ) -> None:
        bucket = bucket or self.default_bucket
        self.objects[(bucket, object_key)] = data
        self.mime_types[(bucket, object_key)] = mime_type

    async def get_object(
        self, object_key: str, bucket: str | None = None, *, max_bytes: int = 200_000_000
    ) -> bytes:
        try:
            data = self.objects[(bucket or self.default_bucket, object_key)]
        except KeyError:
            msg = f"NoSuchKey: {object_key}"
            raise ObjectStoreError(msg) from None
        if len(data) > max_bytes:
            msg = f"Object {object_key} exceeds limit {max_bytes}"
            raise ObjectStoreError(msg)
        return data

    async def exists(self, object_key: str, bucket: str | None = None) -> bool:
        return (bucket or self.default_bucket, object_key) in self.objects


@dataclass
class StubLlmBackend:
    """LLM backend with a canned reply; wrap it in the real ``HybridLlm``.

    ``calls`` records ``(system, user)`` for each completion.
    """

    reply: str = ""
    calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def provider_name(self) -> str:
        return "stub"

    async def complete(self, system: str, user: str, **_kwargs: Any) -> LlmResponse:
        self.calls.append((system, user))
        return LlmResponse(text=self.reply, provider="stub", model="stub-model")


Responder = httpx.Response | Callable[[httpx.Request], httpx.Response]


class FakeHttp:
    """Canned answers for outbound ``httpx`` requests (Discogs, MusicBrainz, CDNs).

    Replaces ``httpx.AsyncHTTPTransport.handle_async_request`` on top of
    :class:`NetworkGuard`, so code that builds its own ``httpx.AsyncClient``
    gets these answers.  A request matches on method and URL without the
    query string; one matching no route is reported to the guard as blocked,
    so the test still fails at teardown.  ``requests`` lists every request.
    """

    def __init__(self, guard: NetworkGuard) -> None:
        self.guard = guard
        self.routes: list[tuple[str, str, list[Responder]]] = []
        self.requests: list[httpx.Request] = []

    def add(self, method: str, url: str, *responses: Responder) -> None:
        """Answer *method* requests to *url* (scheme://host/path).

        Several responses are served in order; the last one repeats.  A
        callable gets the request and returns the response.
        """
        self.routes.append((method.upper(), url, list(responses)))

    def reply(
        self,
        url: str,
        *,
        json: Any = None,
        content: bytes = b"",
        status: int = 200,
        headers: dict[str, str] | None = None,
        method: str = "GET",
    ) -> None:
        """Shorthand for :meth:`add` with one response built from the arguments."""
        resp = (
            httpx.Response(status, json=json, headers=headers)
            if json is not None
            else httpx.Response(status, content=content, headers=headers)
        )
        self.add(method, url, resp)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
        fake = self

        async def _send(_transport: Any, request: httpx.Request) -> httpx.Response:
            await request.aread()
            fake.requests.append(request)
            key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
            for method, url, responses in fake.routes:
                if method == request.method and url == key:
                    responder = responses.pop(0) if len(responses) > 1 else responses[0]
                    return responder(request) if callable(responder) else responder
            fake.guard.blocked.append(str(request.url))
            msg = f"network blocked in tests (no FakeHttp route): {request.url}"
            raise httpx.ConnectError(msg, request=request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _send)
        return self

    def urls(self) -> list[str]:
        """URLs (without query) of all requests, in order."""
        return [str(r.url.copy_with(query=None)) for r in self.requests]

    def sent(self, method: str, url_prefix: str) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.method == method and str(r.url).startswith(url_prefix)
        ]


def json_body(request: httpx.Request) -> Any:
    return json_mod.loads(request.content)


_LOOPBACK_NAMES = {"localhost", "testserver"}


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return True  # unix socket path / bytes: local by definition
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class NetworkGuard:
    """Blocks every outbound connection except loopback and records the attempt.

    Installed for every integration test.  A blocked call raises inside the
    code under test (``httpx.ConnectError`` / ``OSError``), and the test then
    fails at teardown unless it called :meth:`allow_blocked` — so each test
    has to stub the external service it reaches, rather than silently going
    down the "service unavailable" branch.
    """

    def __init__(self) -> None:
        self.blocked: list[str] = []
        self._allowed = False

    def allow_blocked(self) -> None:
        """Mark blocked calls as expected (tests of the offline / error path)."""
        self._allowed = True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = self

        async def _async_send(_self: Any, request: httpx.Request) -> httpx.Response:
            guard.blocked.append(str(request.url))
            msg = f"network blocked in tests: {request.url}"
            raise httpx.ConnectError(msg, request=request)

        def _sync_send(_self: Any, request: httpx.Request) -> httpx.Response:
            guard.blocked.append(str(request.url))
            msg = f"network blocked in tests: {request.url}"
            raise httpx.ConnectError(msg, request=request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _async_send)
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _sync_send)

        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex

        def _getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            if host is not None and not _is_loopback(host):
                guard.blocked.append(f"dns:{host}")
                msg = f"network blocked in tests: {host}"
                raise socket.gaierror(msg)
            return real_getaddrinfo(host, *args, **kwargs)

        def _check(address: Any) -> None:
            if isinstance(address, tuple) and not _is_loopback(address[0]):
                guard.blocked.append(f"connect:{address[0]}")
                msg = f"network blocked in tests: {address[0]}"
                raise ConnectionRefusedError(msg)

        def _connect(self: socket.socket, address: Any) -> None:
            _check(address)
            real_connect(self, address)

        def _connect_ex(self: socket.socket, address: Any) -> int:
            _check(address)
            return real_connect_ex(self, address)

        monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
        monkeypatch.setattr(socket.socket, "connect", _connect)
        monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)

    def check(self) -> None:
        if self.blocked and not self._allowed:
            pytest.fail(
                "Unstubbed outbound network calls (stub the service, or call "
                f"network_guard.allow_blocked() for an offline-path test): {self.blocked}",
                pytrace=False,
            )

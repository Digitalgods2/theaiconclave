"""HTTP transport that connects only to the public IP it actually validated.

Host routing and TLS certificate validation continue to use the original hostname.
No environment proxies are used by callers of this transport.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx


def public_addresses(host: str, port: int) -> list[str]:
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = sorted({str(record[4][0]).split("%", 1)[0] for record in records})
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("evidence URL resolves to a non-public address")
    return addresses


class PublicAddressTransport(httpx.AsyncBaseTransport):
    def __init__(self, require_https: bool = True):
        self.require_https = require_https
        self.transport = httpx.AsyncHTTPTransport(retries=0, limits=httpx.Limits(max_keepalive_connections=0))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ({"https"} if self.require_https else {"http", "https"}):
            raise ValueError("unsupported evidence URL scheme")
        if url.username or url.password:
            raise ValueError("embedded evidence URL credentials are forbidden")
        addresses = await asyncio.to_thread(public_addresses, url.host, url.port or (443 if url.scheme == "https" else 80))
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        extensions = {**request.extensions, "sni_hostname": url.host}
        pinned = httpx.Request(request.method, url.copy_with(host=addresses[0]),
                               headers=headers, stream=request.stream, extensions=extensions)
        return await self.transport.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.transport.aclose()

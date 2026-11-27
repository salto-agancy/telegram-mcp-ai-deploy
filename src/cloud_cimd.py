"""Bounded CIMD retrieval with validated, pinned DNS answers."""

import asyncio
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit

import h11

GLOBAL_V6 = ipaddress.ip_network("2000::/3")


def public_ip(ip):
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved
            and (ip.version == 4 or (ip in GLOBAL_V6 and ip.sixtofour is None and ip.teredo is None)))


def redirect_allowed(value):
    if not isinstance(value, str) or any(ord(c) <= 32 or c in "\\#" for c in value):
        return False
    try:
        uri = urlsplit(value)
        if not uri.hostname or uri.username is not None or uri.port == 0:
            return False
        return uri.scheme == "https" or (uri.scheme == "http" and uri.hostname in {"localhost", "127.0.0.1"})
    except ValueError:
        return False


async def fetch_document(url):
    uri = urlsplit(url)
    if not redirect_allowed(url) or uri.scheme != "https":
        raise ValueError("Invalid metadata URL")
    host, port = uri.hostname.encode("idna").decode("ascii"), uri.port or 443
    async with asyncio.timeout(6):
        records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = {ipaddress.ip_address(record[4][0]) for record in records}
        if not ips or not all(map(public_ip, ips)):
            raise ValueError("Non-public metadata address")
        for ip in sorted(ips, key=lambda addr: addr.version):
            try:
                async with asyncio.timeout(2):
                    reader, writer = await asyncio.open_connection(str(ip), port, ssl=ssl.create_default_context(), server_hostname=host)
                break
            except OSError:
                continue
        else:
            raise ValueError("Metadata host unavailable")
        stream = h11.Connection(h11.CLIENT, max_incomplete_event_size=8192)
        try:
            target = (uri.path or "/") + ("?" + uri.query if uri.query else "")
            writer.write(stream.send(h11.Request(method="GET", target=target, headers=[("Host", uri.netloc), ("Accept", "application/json")]))
                         + stream.send(h11.EndOfMessage()))
            await writer.drain()
            body = bytearray()
            while True:
                event = stream.next_event()
                if event is h11.NEED_DATA:
                    stream.receive_data(await reader.read(8192))
                elif isinstance(event, h11.Response) and event.status_code == 200:
                    continue
                elif isinstance(event, h11.Data) and len(body) + len(event.data) <= 32768:
                    body.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    return json.loads(body)
                else:
                    raise ValueError("Unexpected metadata response")
        except h11.ProtocolError as exc:
            raise ValueError("Invalid metadata response") from exc
        finally:
            writer.close()


class MetadataClients:
    def __init__(self):
        self.cache = {}

    async def redirects(self, client):
        stamp, value = self.cache.get(client, (0, []))
        if stamp > time.monotonic():
            return value
        data = await fetch_document(client)
        if not isinstance(data, dict) or data.get("client_id") != client or data.get("token_endpoint_auth_method", "none") != "none":
            raise ValueError("Invalid client metadata")
        value = data.get("redirect_uris")
        if not isinstance(value, list) or not 1 <= len(value) <= 20 or not all(map(redirect_allowed, value)):
            raise ValueError("Invalid metadata redirects")
        if len(self.cache) >= 128:
            self.cache.clear()
        self.cache[client] = time.monotonic() + 300, value
        return value

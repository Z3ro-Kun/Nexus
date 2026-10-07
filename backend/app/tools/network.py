"""Outbound network policy for LLM-controlled URLs (SSRF protection).

A URL is fetched only if ALL of these hold:

1. scheme is http or https; no user:password@ part; a hostname is present;
2. port is the scheme default (80/443); other ports are refused;
3. the hostname is not a blocked name (`localhost`, `*.localhost`, `*.local`,
   `*.internal`, `*.localdomain`, `*.home.arpa`, `*.arpa`, and cloud-metadata names);
4. the hostname is resolved by this code, and EVERY resolved address is globally
   routable. Refused: loopback, private (RFC 1918, ULA fc00::/7), link-local
   (169.254.0.0/16, fe80::/10, which includes 169.254.169.254 metadata), CGNAT
   100.64.0.0/10 (includes 100.100.100.200), unspecified, multicast, reserved and
   documentation ranges, i.e. anything with `ipaddress.is_global == False`. IPv6 forms
   that embed an IPv4 address (IPv4-mapped, 6to4, Teredo, NAT64 64:ff9b::/96) are
   checked against the embedded IPv4 address as well.

The fetcher then connects to the validated IP itself (not the hostname), so a second DNS
answer cannot redirect the connection (DNS rebinding). Every redirect hop is checked
again from step 1.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from app.tools.errors import DestinationBlockedError, ToolNetworkError

ALLOWED_SCHEMES = {"http": 80, "https": 443}
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".arpa")
NAT64 = ipaddress.ip_network("64:ff9b::/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str  # lowercase hostname or IP literal (without brackets)
    port: int


def check_url(url: str) -> Destination:
    """Static checks (1-3, and 4 for IP literals). Raises DestinationBlockedError."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise DestinationBlockedError(f"invalid URL: {exc}") from None
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise DestinationBlockedError(f"scheme {scheme!r} is not allowed (http/https only)")
    if parts.username is not None or parts.password is not None:
        raise DestinationBlockedError("credentials in URLs are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise DestinationBlockedError("URL has no host")
    default_port = ALLOWED_SCHEMES[scheme]
    if port is not None and port != default_port:
        raise DestinationBlockedError(f"port {port} is not allowed (only {default_port} for {scheme})")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise DestinationBlockedError(f"host {host!r} is not allowed")
    literal = _ip_literal(host)
    if literal is not None:
        check_ip(literal)
    return Destination(scheme=scheme, host=host, port=default_port)


def check_ip(address: IPAddress) -> None:
    """Refuse any address that is not globally routable (see module docstring)."""
    reason = _blocked_reason(address)
    if reason is None and isinstance(address, ipaddress.IPv6Address):
        for embedded in _embedded_ipv4(address):
            reason = _blocked_reason(embedded)
            if reason is not None:
                reason = f"embeds {embedded} ({reason})"
                break
    if reason is not None:
        raise DestinationBlockedError(f"address {address} is not allowed: {reason}")


def _blocked_reason(address: IPAddress) -> str | None:
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_private:
        return "private"
    if address.is_unspecified:
        return "unspecified"
    if address.is_multicast:
        return "multicast"
    if address.is_reserved:
        return "reserved"
    if not address.is_global:
        return "not globally routable"
    return None


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    embedded = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        embedded.extend(address.teredo)
    if address in NAT64:
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return embedded


def _ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> list[str]: ...


class SystemResolver:
    async def resolve(self, host: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ToolNetworkError(f"could not resolve {host!r}: {exc}") from None
        return sorted({str(info[4][0]) for info in infos})


async def resolve_allowed(destination: Destination, resolver: Resolver) -> str:
    """Resolve the host and return an address to connect to. Every resolved address must
    pass `check_ip`; one bad record rejects the destination."""
    literal = _ip_literal(destination.host)
    if literal is not None:
        return str(literal)
    addresses = await resolver.resolve(destination.host, destination.port)
    if not addresses:
        raise ToolNetworkError(f"{destination.host!r} did not resolve to any address")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            raise DestinationBlockedError(f"{destination.host!r} resolved to invalid address {raw!r}") from None
        try:
            check_ip(address)
        except DestinationBlockedError as exc:
            raise DestinationBlockedError(f"{destination.host!r} resolves to a blocked address: {exc.message}") from None
    return addresses[0].split("%")[0]

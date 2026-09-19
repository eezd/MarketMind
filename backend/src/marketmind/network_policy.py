"""Network boundaries shared by HTTP clients, browser routes and proxy checks."""

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

from marketmind.collection_types import SourceError

# Exact hosts only: neither arbitrary subdomains nor third-party URLs from news payloads.
TARGET_HOSTS = {
    "wscn": frozenset({"wallstreetcn.com", "www.wallstreetcn.com", "api.wscn.net", "api-one-wscn.awtmt.com"}),
    "cls": frozenset({"cls.cn", "www.cls.cn", "wwwjs.cls.cn", "cdnjs.cls.cn", "img.cls.cn"}),
    "jin10": frozenset(
        {
            "jin10.com",
            "www.jin10.com",
            "xnews.jin10.com",
            "flash-api.jin10.com",
            "news-api.jin10.com",
            "uc.jin10.com",
            "uc-api.jin10.com",
            "passport.jin10.com",
            "login.jin10.com",
            "cdn.jin10.com",
            "img.jin10.com",
            "static.jin10.com",
            "cdn-fast.jin10.com",
        }
    ),
}
CHECK_TARGETS = {
    "wscn": "https://wallstreetcn.com/",
    "cls": "https://www.cls.cn/telegraph",
    "jin10": "https://www.jin10.com/",
}


def _addresses(host: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return {ipaddress.ip_address(host)}
    except ValueError:
        try:
            return {ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        except (OSError, ValueError):
            raise ValueError("地址解析失败") from None


def validate_proxy_address(host: str, port: int) -> str:
    if not host or any(char in host for char in "/@?#\\\r\n\t ") or not 1 <= port <= 65535:
        raise ValueError("代理地址无效")
    try:
        allowed = [
            ipaddress.ip_network(item.strip())
            for item in os.environ.get("MM_PROXY_ALLOWED_CIDRS", "").split(",")
            if item.strip()
        ]
    except ValueError:
        raise ValueError("代理网络白名单配置无效") from None
    addresses = _addresses(host, port)
    if not addresses or any(
        (not address.is_global and not any(address in network for network in allowed))
        or address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or str(address) in {"169.254.169.254", "100.100.100.200", "fd00:ec2::254"}
        for address in addresses
    ):
        raise ValueError("代理地址不在允许的公网或显式内网白名单中")
    return str(min(addresses, key=lambda address: (address.version, int(address))))


async def validate_target_url(url: str, source_code: str) -> str:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in TARGET_HOSTS.get(source_code, ())
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise ValueError("目标地址不在来源官方白名单中")
        addresses = await asyncio.wait_for(asyncio.to_thread(_addresses, parsed.hostname, 443), timeout=5)
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("目标地址解析到非公网网络")
        return str(min(addresses, key=lambda address: (address.version, int(address))))
    except (ValueError, TimeoutError):
        raise SourceError("unsafe_target", "请求地址或 DNS 不符合来源网络安全策略") from None

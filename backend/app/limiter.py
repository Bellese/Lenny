"""Shared rate-limiter instance for upload endpoints.

Caddy proxies all traffic, so request.client.host is the docker-network
gateway rather than the real user IP. The key function reads from
X-Forwarded-For instead.
"""

from fastapi import Request
from slowapi import Limiter


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
        if ip:
            return ip
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=_client_ip)

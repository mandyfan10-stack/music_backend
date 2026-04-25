from urllib.parse import urlparse
import socket
import ipaddress

def is_safe_public_url(url: str) -> bool:
    """Basic SSRF protection: allow only http(s) and public IP targets."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        if host.lower() in {"localhost"}:
            return False
        addr_info = socket.getaddrinfo(host, None)
        for info in addr_info:
            ip = ipaddress.ip_address(info[4][0])
            if any([
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
            ]):
                return False
        return True
    except Exception:
        return False

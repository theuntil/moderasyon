"""Cloudflare arkasında gerçek istemci IP'si.

Domain Cloudflare üzerinden (turuncu bulut) geçiyorsa, uygulamaya ulaşan bağlantının kaynağı bir
Cloudflare sunucusudur; gerçek ziyaretçi IP'si CF-Connecting-IP başlığındadır. Bu başlığa SADECE
bağlantı gerçekten Cloudflare'in resmi IP aralığından geliyorsa güvenilir; başka kaynaktan gelen sahte
başlık yok sayılır. Bu yapılmazsa IP banları, hız sınırları ve otomatik koruma Cloudflare sunucularına
uygulanır ve o sunucudan gelen tüm masum kullanıcılar etkilenir.

Kaynak: https://www.cloudflare.com/ips/ (15 IPv4 + 7 IPv6 aralığı)
"""
import ipaddress

CLOUDFLARE_RANGES = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
)]


def is_cloudflare(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in CLOUDFLARE_RANGES)


class CloudflareRealIPMiddleware:
    """En dış katman: bağlantı Cloudflare'den geliyorsa istemci adresini CF-Connecting-IP ile değiştirir."""

    def __init__(self, app, enabled: bool = True):
        self.app, self.enabled = app, enabled

    async def __call__(self, scope, receive, send):
        if self.enabled and scope["type"] == "http":
            client = scope.get("client")
            if client and is_cloudflare(client[0]):
                raw = dict(scope.get("headers") or []).get(b"cf-connecting-ip", b"").decode("latin-1").strip()
                try:
                    real = str(ipaddress.ip_address(raw)) if raw else ""
                except ValueError:
                    real = ""
                if real:
                    scope = dict(scope)
                    scope["client"] = (real, client[1])
        await self.app(scope, receive, send)

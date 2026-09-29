"""SSRF korumalı dış HTTP istekleri (medya indirme ve webhook gönderimi).

Koruma katmanları:
1. Sadece http/https, sadece 80/443/8080/8443 portları, URL'de kullanıcı:şifre yok.
2. Adres IP ise doğrudan kontrol edilir; alan adıysa DNS çözümlemesi SafeResolver'da yapılır ve
   dönen adreslerden BİRİ bile özel/iç ağ ise istek reddedilir. Bağlantı, kontrol edilen bu IP'lere
   kurulur; kontrol ile bağlantı arasında DNS değişemez (DNS rebinding koruması).
3. Yönlendirmeler elle takip edilir; her adım aynı kontrollerden geçer, en fazla N adım.
4. Boyut sınırı akış sırasında uygulanır; Content-Length'e güvenilmez.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from app.config import settings

ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
USER_AGENT = "ModerationPlatform/1.0 (+content-moderation)"


class UnsafeURL(Exception):
    """URL veya çözümlenen adres güvenli değil."""


class FetchError(Exception):
    """İndirme başarısız (HTTP hatası, zaman aşımı, boyut sınırı...)."""


def is_public_ip(value: str) -> bool:
    if settings.allow_private_network:
        return True
    try:
        ip = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global: özel, loopback, link-local (169.254 metadata dahil), CGNAT, reserved hariç
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified


def validate_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeURL("invalid_url")
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("scheme_not_allowed")
    if parts.username or parts.password:
        raise UnsafeURL("credentials_in_url")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURL("missing_host")
    if port not in ALLOWED_PORTS and not settings.allow_private_network:
        raise UnsafeURL("port_not_allowed")
    if not settings.allow_private_network and (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise UnsafeURL("blocked_address")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip and not is_public_ip(host):
        raise UnsafeURL("blocked_address")
    if not is_ip:
        # "2130706433", "0177.0.0.1", "0x7f000001" gibi eski IPv4 yazımları: işletim sistemi bunları
        # 127.0.0.1 olarak çözer. Gerçek IP'ye çevirip kontrol et.
        try:
            legacy = socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            legacy = None
        if legacy is not None and not is_public_ip(legacy):
            raise UnsafeURL("blocked_address")


class SafeResolver(AbstractResolver):
    def __init__(self) -> None:
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        infos = await self._inner.resolve(host, port, family)
        if not infos or not all(is_public_ip(i["host"]) for i in infos):
            raise UnsafeURL("blocked_address")
        return infos

    async def close(self) -> None:
        await self._inner.close()


def _session(timeout_s: float) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=SafeResolver(), use_dns_cache=False, limit=8)
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=timeout_s, sock_connect=5),
        headers={"User-Agent": USER_AGENT},
        auto_decompress=True,
    )


async def download(url: str, write, *, max_bytes: int, timeout_s: float, max_redirects: int = 3) -> int:
    """URL'yi indirir ve her parçayı write(bytes) ile verir (diske yazılmaz; ör. R2 akış yüklemesi).
    Yazılan bayt sayısını döner."""
    current = url
    async with _session(timeout_s) as session:
        for _ in range(max_redirects + 1):
            validate_url(current)
            try:
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise FetchError("redirect_without_location")
                        current = urljoin(current, location)
                        continue
                    if resp.status != 200:
                        raise FetchError(f"http_{resp.status}")
                    if resp.content_length is not None and resp.content_length > max_bytes:
                        raise FetchError("too_large")
                    written = 0
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise FetchError("too_large")
                        await write(chunk)
                    if written == 0:
                        raise FetchError("empty_body")
                    return written
            except UnsafeURL:
                raise
            except FetchError:
                raise
            except aiohttp.ClientConnectorError as exc:
                if isinstance(exc.__cause__, UnsafeURL) or "blocked_address" in str(exc):
                    raise UnsafeURL("blocked_address")
                raise FetchError("connect_failed")
            except TimeoutError:
                raise FetchError("timeout")
            except aiohttp.ClientError:
                raise FetchError("fetch_failed")
        raise FetchError("too_many_redirects")


async def post_json(url: str, body: bytes, headers: dict[str, str], *, timeout_s: float) -> int:
    """Webhook gönderimi: yönlendirme takip edilmez. HTTP durum kodunu döner."""
    validate_url(url)
    async with _session(timeout_s) as session:
        try:
            async with session.post(url, data=body, headers={"Content-Type": "application/json", **headers},
                                    allow_redirects=False) as resp:
                await resp.content.read(4096)
                return resp.status
        except aiohttp.ClientConnectorError as exc:
            if isinstance(exc.__cause__, UnsafeURL) or "blocked_address" in str(exc):
                raise UnsafeURL("blocked_address")
            raise FetchError("connect_failed")
        except TimeoutError:
            raise FetchError("timeout")
        except aiohttp.ClientError:
            raise FetchError("request_failed")

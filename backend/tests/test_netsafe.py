"""SSRF korumasının production davranışı (geliştirme bayrağından bağımsız)."""
import asyncio

import pytest

from app import netsafe
from app.config import settings


@pytest.fixture(autouse=True)
def production_mode(monkeypatch):
    monkeypatch.setattr(settings, "allow_private_network", False)


BLOCKED = [
    "http://127.0.0.1/", "http://localhost/", "http://127.1/", "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data/",          # bulut metadata servisi
    "http://10.0.0.5/", "http://172.17.0.1/", "http://192.168.1.1/", "http://100.64.1.1/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fd00::1]/",
    "http://2130706433/", "http://0177.0.0.1/", "http://0x7f000001/", "http://017700000001/",
    "http://postgres.internal/", "http://redis.local/",
    "file:///etc/passwd", "gopher://example.com/", "ftp://example.com/",
    "http://user:pass@example.com/", "http://example.com:6379/", "http://example.com:22/",
]
ALLOWED = ["https://example.com/a.jpg", "http://8.8.8.8/x.png", "https://cdn.example.com:8443/v.mp4"]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked(url):
    with pytest.raises(netsafe.UnsafeURL):
        netsafe.validate_url(url)


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed(url):
    netsafe.validate_url(url)


def test_resolver_rejects_private_answers(monkeypatch):
    """DNS rebinding: alan adı iç adrese çözülürse bağlantı kurulmaz."""
    async def fake_resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": "10.1.2.3", "port": port, "family": 2, "proto": 0, "flags": 0}]

    monkeypatch.setattr(netsafe.aiohttp.ThreadedResolver, "resolve", fake_resolve)
    async def run():
        return await netsafe.SafeResolver().resolve("evil.example.com", 80)

    with pytest.raises(netsafe.UnsafeURL):
        asyncio.run(run())


def test_is_public_ip():
    assert netsafe.is_public_ip("8.8.8.8")
    for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:10.0.0.1", "224.0.0.1", "0.0.0.0"):
        assert not netsafe.is_public_ip(ip), ip

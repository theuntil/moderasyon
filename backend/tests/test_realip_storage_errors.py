"""Cloudflare arkasında gerçek IP ve R2 hata sınıflandırması (servis gerektirmez)."""
import asyncio

from botocore.exceptions import ClientError, EndpointConnectionError

from app.realip import CloudflareRealIPMiddleware, is_cloudflare
from app.storage import error_code, is_permanent


def run_mw(client_ip: str, headers: dict) -> str:
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]

    mw = CloudflareRealIPMiddleware(app)
    scope = {"type": "http", "client": (client_ip, 1234),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(mw(scope, None, None))
    return seen["client"]


def test_cloudflare_ranges():
    assert is_cloudflare("104.23.168.52")            # testte görülen gerçek Cloudflare adresi
    assert is_cloudflare("2606:4700::1111")
    assert not is_cloudflare("8.8.8.8") and not is_cloudflare("88.247.12.40")


def test_real_ip_from_cloudflare_header():
    assert run_mw("104.23.168.52", {"CF-Connecting-IP": "88.247.12.40"}) == "88.247.12.40"


def test_spoofed_header_from_non_cloudflare_is_ignored():
    # Cloudflare'den gelmeyen bağlantı başlığı taklit ederse yok sayılır (IP banı atlatılamaz)
    assert run_mw("45.12.34.56", {"CF-Connecting-IP": "1.2.3.4"}) == "45.12.34.56"


def test_invalid_header_value_is_ignored():
    assert run_mw("104.23.168.52", {"CF-Connecting-IP": "bu-bir-ip-degil"}) == "104.23.168.52"
    assert run_mw("104.23.168.52", {}) == "104.23.168.52"


def err(code, status=400, op="PutObject"):
    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


def test_storage_error_classification():
    assert error_code(err("NoSuchBucket")) == "NoSuchBucket" and is_permanent(err("NoSuchBucket"))
    assert is_permanent(err("AccessDenied", 403)) and is_permanent(err("InvalidAccessKeyId", 403))
    assert error_code(ClientError({"Error": {}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadBucket")) == "404"
    assert not is_permanent(err("InternalError", 500))               # geçici: tekrar denenir
    assert not is_permanent(err("SlowDown", 503))
    assert error_code(EndpointConnectionError(endpoint_url="https://x")) == "EndpointConnectionError"

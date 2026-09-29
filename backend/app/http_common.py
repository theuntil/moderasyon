"""API ve admin API için ortak HTTP güvenlik katmanı.

- BodyLimitMiddleware: gövde boyutunu akış halinde sayar. Content-Length olmayan (chunked) istekler de
  sınırın üstüne çıkamaz; sadece başlığa bakmak yetmez.
- Request ID: her yanıtta X-Request-Id; loglarla eşleştirmek için.
- Hata yanıtları: doğrulama hataları gönderilen içeriği geri yansıtmaz, 500'ler iç detay sızdırmaz.
"""
import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("http")


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Saf ASGI middleware. limits: [(yol öneki, bayt)], ilk eşleşen kullanılır."""

    def __init__(self, app, default_limit: int, limits: list[tuple[str, int]] | None = None):
        self.app = app
        self.default_limit = default_limit
        self.limits = limits or []

    def _limit_for(self, path: str) -> int:
        for prefix, limit in self.limits:
            if path.startswith(prefix):
                return limit
        return self.default_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        limit = self._limit_for(scope.get("path", ""))
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            if not declared.isdigit() or int(declared) > limit:
                return await _send_json(send, 413, {"detail": {"error": "request_too_large"}})

        received = 0
        response_started = False
        rejected = False

        async def limited_receive():
            nonlocal received, rejected, response_started
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit and not rejected:
                    rejected = True
                    # Uygulama gövde hatasını 400'e çevirebilir; 413'ü burada kendimiz gönderip
                    # uygulamanın sonraki yanıtını yok sayıyoruz.
                    if not response_started:
                        response_started = True
                        await _send_json(send, 413, {"detail": {"error": "request_too_large"}})
                    raise _TooLarge()
            return message

        async def tracking_send(message):
            nonlocal response_started
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            pass


async def _send_json(send, status: int, body: dict) -> None:
    import json

    raw = json.dumps(body).encode()
    await send({
        "type": "http.response.start", "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
                    (b"connection", b"close")],
    })
    await send({"type": "http.response.body", "body": raw})


def install_common(app: FastAPI, *, cache_control: str = "no-store") -> None:
    @app.middleware("http")
    async def request_id_and_headers(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if 8 <= len(incoming) <= 64 and incoming.replace("-", "").isalnum() else secrets.token_hex(8)
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", cache_control)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic'in varsayılan cevabı gönderilen değeri ("input") geri basar; içerik yansıtılmasın
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path", "header")]
        message = str(first.get("msg", "Invalid request.")).removeprefix("Value error, ")
        return JSONResponse(
            status_code=422,
            content={"detail": {"error": "validation_error", "field": ".".join(loc), "message": message[:300]}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": _code_for(exc.status_code)}
        content = {"detail": detail}
        if detail.get("status") == "unprocessed":
            # "işlenmedi" sözleşmesi: alanlar üst seviyede de bulunur (istemci kolayca kaydedebilsin)
            content = {k: v for k, v in detail.items()} | {"detail": detail}
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.exception("unhandled error request_id=%s path=%s", rid, request.url.path)
        detail = {"error": "internal_error", "request_id": rid}
        content = {"detail": detail}
        if request.url.path.startswith("/v1/"):
            content = {"status": "unprocessed", "decision": None, "retryable": True, **detail, "detail": detail}
        return JSONResponse(status_code=500, content=content, headers={"X-Request-Id": rid})


def _code_for(status: int) -> str:
    return {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden"}.get(status, f"http_{status}")


class TimeoutMiddleware:
    """Hiçbir /v1 isteği süre sınırını aşmaz: aşarsa 503 + {"status": "unprocessed", "retryable": true}."""

    def __init__(self, app, timeout_s: float | None = None, prefix: str = "/v1/", exclude: tuple[str, ...] = ()):
        self.app, self._timeout_s, self.prefix, self.exclude = app, timeout_s, prefix, exclude

    @property
    def timeout_s(self) -> float:
        from app.config import settings
        return self._timeout_s or settings.request_timeout_s

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(self.prefix) or path.startswith(self.exclude):
            return await self.app(scope, receive, send)
        started = False

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        import asyncio
        try:
            await asyncio.wait_for(self.app(scope, receive, tracking_send), timeout=self.timeout_s)
        except TimeoutError:
            if not started:
                detail = {"error": "timeout", "message": "The request took too long. Retry later."}
                await _send_json(send, 503, {"status": "unprocessed", "decision": None, "retryable": True,
                                             **detail, "detail": detail})

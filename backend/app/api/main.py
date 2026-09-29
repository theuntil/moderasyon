from contextlib import asynccontextmanager

from arq import create_pool as create_arq_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import moderate
from app.config import settings
from app.logsetup import setup as _setup_logging

_setup_logging()
from app.db import create_pool
from app.autoban import AutoBanMiddleware
from app.realip import CloudflareRealIPMiddleware
from app.http_common import BodyLimitMiddleware, TimeoutMiddleware, install_common
from app.platform_settings import SettingsCache
from botocore.exceptions import BotoCoreError, ClientError

from app.storage import StorageError, storage


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await create_pool(min_size=2, max_size=settings.db_pool_size, statement_timeout_ms=5_000)
    app.state.settings_cache = SettingsCache()
    app.state.arq = await create_arq_pool(RedisSettings.from_dsn(settings.redis_url))
    await storage.start()
    yield
    await storage.close()
    await app.state.arq.aclose()
    await app.state.db.close()


docs = settings.api_docs_enabled
app = FastAPI(
    title="Moderation Platform API",
    version="0.3.0",
    lifespan=lifespan,
    docs_url="/docs" if docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if docs else None,
)
app.include_router(moderate.router)
install_common(app)

# Gövde sınırı akış halinde uygulanır (chunked istekler dahil). Yükleme ucu daha büyük dosya kabul eder.
app.add_middleware(
    BodyLimitMiddleware,
    default_limit=settings.max_body_bytes,
    limits=[("/v1/moderate/upload", settings.max_video_bytes + 1024 * 1024)],
)
# Yükleme hariç her /v1 isteği süre sınırına tabi (büyük video yüklemesi ağ hızına bağlı)
app.add_middleware(TimeoutMiddleware, prefix="/v1/",
                   exclude=("/v1/moderate/upload",))


# En dışta: banlı IP'ler hiçbir işlem yapılmadan reddedilir; tarama ve hacim burada sayılır
app.add_middleware(AutoBanMiddleware, get_state=lambda: (app.state.arq, app.state.db, app.state.settings_cache))


@app.exception_handler(StorageError)
@app.exception_handler(ClientError)
@app.exception_handler(BotoCoreError)
async def storage_unavailable(request: Request, exc: Exception):
    """R2'ye erişilemiyor: istemci "işlenmedi, tekrar dene" yanıtı alır (500 değil)."""
    import logging
    from app.storage import HINTS, error_code
    code = error_code(exc)
    logging.getLogger("api").error("R2 hatası: %s. %s", code, HINTS.get(code, ""))
    detail = {"error": "media_storage_error", "message": "Media storage is temporarily unavailable. Retry later."}
    return JSONResponse(status_code=503, headers={"Retry-After": "30"},
                        content={"status": "unprocessed", "decision": None, "retryable": True, **detail, "detail": detail})


@app.middleware("http")
async def rate_limit_headers(request: Request, call_next):
    response = await call_next(request)
    rl = getattr(request.state, "rate_limit", None)
    if rl is not None and rl.allowed:
        response.headers["X-RateLimit-Limit"] = str(rl.limit)
        response.headers["X-RateLimit-Remaining"] = str(rl.remaining)
    return response


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/ready", include_in_schema=False)
async def ready(request: Request):
    try:
        await request.app.state.db.fetchval("SELECT 1")
        await request.app.state.arq.ping()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {"status": "ready"}


# En dış katman (en son eklenen): Cloudflare arkasında gerçek istemci IP'si. IP banı, hız sınırı ve
# otomatik koruma bu adresi kullanır.
app.add_middleware(CloudflareRealIPMiddleware, enabled=settings.trust_cloudflare)

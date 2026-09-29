"""Layer 2 sağlayıcıları: sadece HTTP çağrısı yapar, skor ve token kullanımını döner.

Bütçe, limit, devre kesici ve maliyet kaydı bu modülde DEĞİL, app.moderation.ai_gateway'dedir.

Sağlayıcılar (panelden seçilir):
- openai_moderation: OpenAI Moderation API (omni-moderation-latest). Ücretsiz; metin ve görsel.
- chat: OpenAI uyumlu sohbet modeli (ör. gpt-5-nano) veya kendi sunucunuz (Ollama, vLLM). Token başına ücretli.

Gizlilik: yerel modelin çıplaklık/cinsel içerik bulduğu görseller hiçbir sağlayıcıya gönderilmez
(OpenAI, çocuk istismarı şüphesi olan materyalin API'ye gönderilmemesini ister).
"""
import asyncio
import base64
import io
import json
import time
from dataclasses import dataclass, field

import aiohttp
from PIL import Image

from app.config import settings
from app.moderation.text_pipeline import DetectionResult

CATEGORIES = ("harassment", "hate", "sexual", "nudity", "violence", "weapons", "drugs", "self_harm",
              "spam", "scam", "extremism", "illicit")

MODERATION_MAP = {
    "harassment": "harassment", "harassment/threatening": "harassment",
    "hate": "hate", "hate/threatening": "hate",
    "sexual": "sexual", "sexual/minors": "sexual_minors",
    "violence": "violence", "violence/graphic": "violence",
    "self-harm": "self_harm", "self-harm/intent": "self_harm", "self-harm/instructions": "self_harm",
    "illicit": "illicit", "illicit/violent": "illicit",
}
SENSITIVE_IMAGE_CATEGORIES = ("sexual", "nudity")
# OpenAI Moderation'ın gerçekten değerlendirdiği kategoriler: metinde tamamı, görselde sadece bunlar.
# Hakem modunda AI sadece değerlendirdiği kategoride yerel sonucun yerine geçer (bakmadığı konuda
# "sorun yok" demiş sayılmaz; ör. görseldeki yazıdaki hakaret).
MODERATION_TEXT_EVAL = frozenset({"harassment", "hate", "sexual", "sexual_minors", "violence", "self_harm", "illicit"})
MODERATION_IMAGE_EVAL = frozenset({"sexual", "violence", "self_harm"})

MAX_COMPLETION_TOKENS = 600          # akıl yürüten modellerde çıktı + akıl yürütme tokenları üst sınırı
IMAGE_TOKEN_ESTIMATE = 1500          # düşük detay görsel için temkinli tahmin (bütçe ayırma amaçlı)

SYSTEM_PROMPT = """You are a content moderation classifier for Turkish and English user content.
Score how likely the content violates each category from 0.0 (clearly not) to 1.0 (clearly violates).
Categories:
- harassment: insults, profanity aimed at someone, bullying (Turkish slang and obfuscated swearing count)
- hate: attacks on protected groups (ethnicity, religion, gender, sexual orientation, disability)
- sexual: explicit sexual content or sexual solicitation
- nudity: exposed intimate body parts without explicit sexual activity
- violence: gore, graphic injury, threats or glorification of violence
- weapons: weapons shown in a threatening context or offered for sale
- drugs: illegal drug use, sale or promotion
- self_harm: self-harm or suicide promotion or instructions
- spam: unsolicited advertising, repetitive or link spam
- scam: fraud, phishing, fake giveaways
- extremism: terrorist or violent extremist propaganda
- illicit: instructions or offers for other illegal activity
Context matters: news, education, jokes between friends and non-sexual nudity (art, medical) are not violations.
Reply with ONLY a JSON object: {"scores": {"<category>": <0-1>, ...}, "reason": "<max 12 words>"}"""


@dataclass
class CallResult:
    scores: dict[str, float] | None
    status: str                       # ok | error | timeout | rate_limited | auth_error | bad_response
    input_tokens: int = 0
    output_tokens: int = 0
    usage_reported: bool = False      # sağlayıcı token sayısını bildirdi mi (bildirmezse tahmin kullanılır)
    latency_ms: int = 0
    http_calls: int = 0
    error: str | None = None
    kinds: list[str] = field(default_factory=list)
    evaluated: frozenset = frozenset()   # sağlayıcının gerçekten değerlendirdiği kategoriler


def base_url() -> str:
    if settings.ai_base_url:
        return settings.ai_base_url.rstrip("/")
    return "https://api.openai.com/v1" if settings.ai_api_key else ""


def configured() -> bool:
    return bool(base_url())


def masked_key() -> str | None:
    key = settings.ai_api_key
    return f"{key[:3]}…{key[-4:]}" if len(key) >= 12 else ("tanımlı" if key else None)


def model_for(provider: str, model: str, vision_model: str, has_images: bool) -> str:
    if provider == "openai_moderation":
        return settings.ai_moderation_model
    return vision_model if has_images else model


def may_send_images(layer1_scores: dict[str, float], *, allow_sensitive: bool = False, block_threshold: float = 0.85) -> bool:
    """Çıplaklık/cinsel içerik şüphesi olan görseller varsayılan olarak dış sağlayıcıya gönderilmez.
    allow_sensitive (panel: "belirsiz çıplaklıkta AI'a danış"): yerel modelin EMİN OLAMADIĞI (engelleme eşiği
    altındaki) görseller sınıflandırma için gönderilir; kesin tespit edilenler zaten engellenir, gönderilmez."""
    worst = max((layer1_scores.get(c, 0.0) for c in SENSITIVE_IMAGE_CATEGORIES), default=0.0)
    if worst < 0.3:
        return True
    return allow_sensitive and worst < block_threshold


def is_reasoning_model(model: str) -> bool:
    return model.lower().startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def estimate_tokens(text: str | None, n_images: int) -> tuple[int, int]:
    """Bütçe ayırmak için temkinli (yüksek) tahmin: (girdi, çıktı)."""
    chars = len(SYSTEM_PROMPT) + len(text or "")
    return chars // 3 + 50 + n_images * IMAGE_TOKEN_ESTIMATE, MAX_COMPLETION_TOKENS


def _image_url(img: Image.Image) -> str:
    copy = img.copy()
    copy.thumbnail((768, 768))
    buf = io.BytesIO()
    copy.convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.ai_api_key}"} if settings.ai_api_key else {}


def _status_for(http_status: int) -> str:
    if http_status in (401, 403):
        return "auth_error"
    if http_status == 429:
        return "rate_limited"
    return "error"


def parse_scores(content: str) -> dict[str, float] | None:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    scores = data.get("scores", data) if isinstance(data, dict) else None
    if not isinstance(scores, dict):
        return None
    out = {}
    for name in CATEGORIES:
        try:
            value = float(scores.get(name, 0) or 0)
        except (TypeError, ValueError):
            continue
        out[name] = round(max(0.0, min(1.0, value)), 4)
    return out


# ---------------------------------------------------------------- OpenAI Moderation

async def _moderation_one(session, item: dict) -> tuple[dict[str, float] | None, str]:
    body = {"model": settings.ai_moderation_model, "input": [item]}
    async with session.post(base_url() + "/moderations", json=body, headers=_headers()) as resp:
        if resp.status != 200:
            return None, _status_for(resp.status)
        data = await resp.json(content_type=None)
    try:
        raw = data["results"][0]["category_scores"]
    except (KeyError, IndexError, TypeError):
        return None, "bad_response"
    out: dict[str, float] = {}
    for key, value in raw.items():
        name = MODERATION_MAP.get(key)
        if name is None:
            continue
        try:
            out[name] = max(out.get(name, 0.0), round(max(0.0, min(1.0, float(value))), 4))
        except (TypeError, ValueError):
            continue
    return out, "ok"


async def call_moderation(text: str | None, images: list[Image.Image]) -> CallResult:
    items = ([{"type": "text", "text": text[:8000]}] if text else []) + \
            [{"type": "image_url", "image_url": {"url": _image_url(i)}} for i in images[:4]]
    started = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=settings.ai_timeout_s)) as session:
            results = await asyncio.gather(*[_moderation_one(session, i) for i in items], return_exceptions=True)
    except Exception as exc:  # noqa: BLE001
        return CallResult(None, "error", latency_ms=_ms(started), http_calls=len(items), error=type(exc).__name__)
    latency = _ms(started)
    merged: dict[str, float] = {}
    for r in results:
        if isinstance(r, BaseException):
            status = "timeout" if isinstance(r, TimeoutError) else "error"
            return CallResult(None, status, latency_ms=latency, http_calls=len(items), error=type(r).__name__)
        scores, status = r
        if scores is None:   # biri bile başarısızsa kısmi sonuçla karar verilmez
            return CallResult(None, status, latency_ms=latency, http_calls=len(items), error=status)
        for k, v in scores.items():
            merged[k] = max(merged.get(k, 0.0), v)
    evaluated = (MODERATION_TEXT_EVAL if text else frozenset()) | (MODERATION_IMAGE_EVAL if images else frozenset())
    return CallResult(merged, "ok", latency_ms=latency, http_calls=len(items), usage_reported=True, evaluated=evaluated)


# ---------------------------------------------------------------- sohbet modeli

def visual_prompt(rules: list[dict]) -> str:
    items = "\n".join(f'- "{r["id"]}": {r["description"]}' for r in rules)
    return ("You check images (frames of one user post) for specific forbidden visual content.\n"
            "For each item below, estimate how likely the images CLEARLY show it (0.0 to 1.0). Only visible\n"
            "symbols, flags, emblems, logos, banners or written names count. Do not identify people by their face.\n"
            f"Items:\n{items}\n"
            'Reply with ONLY a JSON object: {"matches": {"<item id>": <0-1>, ...}}')


def parse_matches(content: str) -> dict[str, float] | None:
    text = content.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    matches = data.get("matches", data) if isinstance(data, dict) else None
    if not isinstance(matches, dict):
        return None
    out = {}
    for k, v in matches.items():
        try:
            out[str(k)] = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            continue
    return out


async def call_chat(model: str, text: str | None, images: list[Image.Image], *,
                    system_prompt: str = SYSTEM_PROMPT, parse=None) -> CallResult:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": f"Content to classify:\n<<<\n{text[:4000]}\n>>>"})
    elif images:
        content.append({"type": "text", "text": "Classify these images (frames of one post)."})
    for img in images[:4]:
        # detail=low: görsel başına sabit, düşük token maliyeti
        content.append({"type": "image_url", "image_url": {"url": _image_url(img), "detail": "low"}})
    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}],
    }
    if is_reasoning_model(model):
        # GPT-5 ailesi temperature/max_tokens kabul etmez; en düşük akıl yürütme = en ucuz ve hızlı
        body |= {"max_completion_tokens": MAX_COMPLETION_TOKENS, "reasoning_effort": "minimal"}
    else:
        body |= {"temperature": 0, "max_tokens": 300}

    started = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=settings.ai_timeout_s)) as session:
            async with session.post(base_url() + "/chat/completions", json=body, headers=_headers()) as resp:
                if resp.status != 200:
                    detail = (await resp.text())[:200]
                    return CallResult(None, _status_for(resp.status), latency_ms=_ms(started), http_calls=1,
                                      error=f"http_{resp.status}: {detail}")
                data = await resp.json(content_type=None)
    except TimeoutError:
        return CallResult(None, "timeout", latency_ms=_ms(started), http_calls=1, error="timeout")
    except Exception as exc:  # noqa: BLE001
        return CallResult(None, "error", latency_ms=_ms(started), http_calls=1, error=type(exc).__name__)

    usage = data.get("usage") or {}
    result = CallResult(None, "ok", latency_ms=_ms(started), http_calls=1, evaluated=frozenset(CATEGORIES))
    if "prompt_tokens" in usage:
        result.input_tokens = int(usage.get("prompt_tokens") or 0)
        result.output_tokens = int(usage.get("completion_tokens") or 0)
        result.usage_reported = True
    try:
        message = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        result.status, result.error = "bad_response", "no_choices"
        return result
    result.scores = (parse or parse_scores)(message)
    if result.scores is None:
        result.status, result.error = "bad_response", "unparseable_json"
    return result


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def merge(detection: DetectionResult, scores: dict[str, float], *, judge: bool, model: str,
          evaluated: frozenset | None = None) -> DetectionResult:
    """judge=True (akıllı mod): AI hakemdir, Layer 1 skorlarının yerini alır (engel listesi ve AI'ya
    gönderilmeyen hassas görsel skorları hariç). judge=False (her zaman): AI sadece ekler."""
    out = DetectionResult(providers=list(detection.providers) + [{"provider": "llm", "model": model, "version": "layer2"}])
    for c in detection.categories:
        ai_judged = evaluated is None or c.name in evaluated
        if judge and ai_judged and c.name != "blocklist_match" and c.name not in SENSITIVE_IMAGE_CATEGORIES:
            continue
        out.add(c.name, c.score)
    for name, score in scores.items():
        if score >= 0.05:
            out.add(name, score)
    return out

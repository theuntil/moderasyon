"""Görsel tespit modelleri (CPU, yerel, veri dışarı çıkmaz).

- NudeNet (ONNX, 320n): çıplaklık / cinsel içerik tespiti. Kutu bazlı etiketler kategori skorlarına çevrilir.
- Tesseract OCR (Türkçe + İngilizce): görseldeki yazı okunur ve metin filtresinden geçirilir
  (hakaret içeren "caps"ler, görsele gömülü spam linkleri).

Skor eşlemesi bilinçli olarak temkinli: açık cinsel içerik güçlü tespitte engellenir; göğüs/kalça
çıplaklığı incelemeye gider, güçlüyse engellenir; mayo/iç çamaşırı gibi "suggestive" içerik tek başına
karar tetiklemez (0.45 tavan), sadece kayda geçer. Eşikler panelden ayarlanır.
"""
import asyncio
import logging
import tempfile
import threading
from pathlib import Path

from PIL import Image

from app.media.files import to_bgr_array
from app.moderation.text_pipeline import run_text_pipeline

log = logging.getLogger("detectors")

NUDENET_VERSION = "nudenet-3.4"          # etkin model adı _get_detector() sonrası MODEL_NAME'de
MODEL_NAME = "320n"
TILE_MIN_SIDE = 700      # bundan büyük karelerde parçalı tarama
TILE_FRACTION = 0.6      # her parça karenin %60'ı; parçalar örtüşür, sınırdaki nesne kaçmaz
OCR_VERSION = "tesseract-5-tur+eng"

EXPLICIT = {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}
NUDITY = {"FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"}
SUGGESTIVE = {"FEMALE_BREAST_COVERED", "BUTTOCKS_COVERED", "FEMALE_GENITALIA_COVERED", "ANUS_COVERED", "BELLY_EXPOSED"}

_detector = None
_lock = threading.Lock()


def _get_detector():
    """640m modeli varsa (Docker build'de indirilir) onu, yoksa pakete gömülü 320n modelini yükler."""
    global _detector, MODEL_NAME
    if _detector is None:
        with _lock:
            if _detector is None:
                import os

                from nudenet import NudeDetector
                path = os.environ.get("NUDENET_MODEL_PATH", "/models/640m.onnx")
                if os.path.isfile(path) and os.path.getsize(path) > 10_000_000:
                    try:
                        _detector = NudeDetector(model_path=path, inference_resolution=640)
                        MODEL_NAME = "640m"
                    except Exception:  # noqa: BLE001 — bozuk dosya: gömülü modelle devam
                        log.exception("NudeNet 640m yüklenemedi, 320n kullanılacak")
                if _detector is None:
                    _detector = NudeDetector()
                    MODEL_NAME = "320n"
                log.warning("Çıplaklık modeli: NudeNet %s (parçalı tarama açık)", MODEL_NAME)
    return _detector


def warmup() -> None:
    _get_detector()


def _scale(score: float) -> float:
    # NudeNet güveni 0.25 altında gürültü, 0.70 üstü güçlü tespit
    return max(0.0, min(1.0, (score - 0.25) / 0.45))


def nudity_scores(detections: list[dict]) -> dict[str, float]:
    best: dict[str, float] = {}
    for d in detections:
        best[d["class"]] = max(best.get(d["class"], 0.0), float(d["score"]))
    out: dict[str, float] = {}
    sexual = max((_scale(best[k]) for k in EXPLICIT if k in best), default=0.0)
    nudity = max((_scale(best[k]) * 0.95 for k in NUDITY if k in best), default=0.0)
    suggestive = min(0.45, max((_scale(best[k]) * 0.6 for k in SUGGESTIVE if k in best), default=0.0))
    for name, value in (("sexual", sexual), ("nudity", nudity), ("suggestive", suggestive)):
        if value >= 0.05:
            out[name] = round(value, 4)
    return out


def tiles(img: Image.Image) -> list[Image.Image]:
    """Karenin tamamı + örtüşen 4 parçası. Model girişi sabit boyutlu olduğundan (320/640 px), karede küçük
    kalan bir beden parçada 2-3 kat büyük görünür ve yakalanır (ör. ekran kaydında pencere içindeki fotoğraf)."""
    w, h = img.size
    if max(w, h) < TILE_MIN_SIDE:
        return [img]
    tw, th = int(w * TILE_FRACTION), int(h * TILE_FRACTION)
    boxes = [(0, 0), (w - tw, 0), (0, h - th), (w - tw, h - th)]
    return [img] + [img.crop((x, y, x + tw, y + th)) for x, y in boxes]


def _detect_batch(images: list[Image.Image]) -> list[dict[str, float]]:
    detector = _get_detector()
    arrays, owners = [], []
    for i, img in enumerate(images):
        for t in tiles(img):
            arrays.append(to_bgr_array(t))
            owners.append(i)
    results = detector.detect_batch(arrays, batch_size=4) if len(arrays) > 1 else [detector.detect(arrays[0])]
    merged: list[list[dict]] = [[] for _ in images]
    for owner, dets in zip(owners, results):
        merged[owner].extend(dets)
    return [nudity_scores(d) for d in merged]


async def detect_nudity(images: list[Image.Image]) -> list[dict[str, float]]:
    if not images:
        return []
    # ONNX Runtime hesaplama sırasında GIL'i bırakır; event loop bloklanmasın diye thread'de çalışır
    return await asyncio.to_thread(_detect_batch, images)


MAX_TEXT_REGIONS = 6


def text_regions(img: Image.Image) -> list[tuple[int, int, int, int]]:
    """Görseldeki olası metin satırlarını bulur (OpenCV). Tesseract karışık/koyu zeminde bütün görselde
    metin bölgesini kaçırabiliyor; satırlar kırpılıp ayrı okunduğunda doğruluk çok artıyor."""
    import cv2
    import numpy as np

    gray = np.asarray(img.convert("L"))
    h, w = gray.shape
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, w // 60), 3))
    connected = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
    # RETR_LIST: çerçeve içindeki satırlar da gelsin (meme'lerde yazı genelde bir kutunun içinde)
    contours, _ = cv2.findContours(connected, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, bw_, bh = cv2.boundingRect(c)
        if bh < 10 or bh > h * 0.4 or bw_ < 30 or bw_ / max(bh, 1) < 1.8:
            continue
        roi = bw[y:y + bh, x:x + bw_]
        fill = float(cv2.countNonZero(roi)) / (bw_ * bh)
        if 0.15 < fill < 0.95:  # metin satırı: ne boş ne tamamen dolu
            boxes.append((x, y, bw_, bh))
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    chosen: list[tuple[int, int, int, int]] = []
    for b in boxes:
        # Büyük ölçüde başka bir seçili kutunun içindeyse atla
        if any(_overlap(b, c) > 0.7 for c in chosen):
            continue
        chosen.append(b)
        if len(chosen) >= MAX_TEXT_REGIONS:
            break
    return chosen


def _overlap(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return (ix * iy) / float(min(aw * ah, bw * bh) or 1)


async def _tesseract(img: Image.Image, psm: int, timeout: float) -> str:
    with tempfile.TemporaryDirectory(prefix="ocr_") as tmp:
        src = Path(tmp) / "in.png"
        img.save(src)
        proc = await asyncio.create_subprocess_exec(
            "tesseract", str(src), "stdout", "-l", "tur+eng", "--psm", str(psm),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ""
    return " ".join(out.decode("utf-8", "ignore").split())


async def ocr_text(img: Image.Image, timeout: float = 8.0) -> str:
    """Tüm görsel (seyrek metin modu) + bulunan her metin satırı ayrı ayrı okunur."""
    from PIL import ImageOps

    gray = img.convert("L")
    if max(gray.size) < 900:
        factor = 900 / max(gray.size)
        gray = gray.resize((int(gray.width * factor), int(gray.height * factor)))
    texts = [await _tesseract(gray, 11, timeout)]
    try:
        boxes = await asyncio.to_thread(text_regions, gray)
    except Exception:  # noqa: BLE001 — bölge bulma başarısızsa tam görsel sonucu yeter
        boxes = []
    for x, y, w, h in boxes:
        pad = max(6, h // 3)
        crop = gray.crop((max(0, x - pad), max(0, y - pad), min(gray.width, x + w + pad), min(gray.height, y + h + pad)))
        crop = ImageOps.expand(crop, border=20, fill=255)
        texts.append(await _tesseract(crop, 7, timeout))
    seen, out = set(), []
    for t in texts:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out)


def text_scores(text: str, profanity_level: str = "strict") -> dict[str, float]:
    if len(text) < 3:
        return {}
    result = run_text_pipeline(text, profanity_level)
    return {c.name: round(c.score, 4) for c in result.categories}

"""Görsel/video analiz hattı.

Sıra:
1. SHA-256 + perceptual hash → görsel engel listesi (moderatörün daha önce engellediği içerik,
   yeniden boyutlandırılmış/sıkıştırılmış kopyaları dahil) → eşleşirse doğrudan engel.
2. Aynı dosya daha önce aynı model sürümüyle analiz edildiyse sonuç önbellekten (viral paylaşımlar).
3. NudeNet (tüm kareler) + OCR → metin filtresi (görsel: tek kare, video: en fazla 4 kare).
4. Kategori skoru = karelerdeki en yüksek skor.
"""
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.media import detectors, video
from app.media.files import Frame, InvalidMedia, load_frame_file, load_image_frames, sha256_bytes
from app.media.sniff import Sniffed
from app.moderation.text_pipeline import DetectionResult

log = logging.getLogger("media")

PHASH_MAX_DISTANCE = 6          # 64 bitte en fazla 6 bit fark = aynı görsel sayılır
MAX_OCR_FRAMES = 12              # görsel olarak farklı karelerden en fazla bu kadarı okunur
OCR_DISTINCT_BITS = 10           # bundan az fark: aynı sahne, tekrar okumaya gerek yok
CACHE_TTL_S = 7 * 24 * 3600
PIPELINE_VERSION = f"{detectors.NUDENET_VERSION}|{detectors.OCR_VERSION}|v3-tiles"


@dataclass
class AnalyzedFrame:
    frame: Frame
    categories: dict[str, float] = field(default_factory=dict)

    @property
    def top(self) -> float:
        return max(self.categories.values(), default=0.0)


@dataclass
class MediaAnalysis:
    detection: DetectionResult
    frames: list[AnalyzedFrame]
    sha256: str
    width: int
    height: int
    duration_ms: int | None
    blocklist_id: str | None = None
    text: str = ""          # görsel/videodan OCR ile okunan metin (AI'ya bağlam olarak gider)


async def _blocklist_match(pool, project_id, sha256: str, phashes: list[int]) -> str | None:
    row = await pool.fetchrow(
        """
        SELECT b.id FROM hash_blocklist b
        WHERE (b.project_id IS NULL OR b.project_id = $1)
          AND (
            b.sha256 = $2
            OR EXISTS (
              SELECT 1 FROM unnest(b.phashes) bp, unnest($3::bigint[]) f
              WHERE bit_count((bp # f)::bit(64)) <= $4
            )
          )
        LIMIT 1
        """,
        project_id, sha256, phashes, PHASH_MAX_DISTANCE,
    )
    return str(row["id"]) if row else None


def _distinct_frames(frames: list["AnalyzedFrame"], limit: int) -> list["AnalyzedFrame"]:
    chosen: list[AnalyzedFrame] = []
    for a in frames:
        ph = a.frame.phash
        if ph is not None and any(
            c.frame.phash is not None and bin((ph ^ c.frame.phash) & 0xFFFFFFFFFFFFFFFF).count("1") < OCR_DISTINCT_BITS
            for c in chosen
        ):
            continue
        chosen.append(a)
    if len(chosen) > limit:
        # Eşit aralıklı örnekle ki videonun sonu da kapsansın
        step = len(chosen) / limit
        chosen = [chosen[int(i * step)] for i in range(limit)]
    return chosen


async def _load(key: str, sniffed: Sniffed, workdir: Path) -> tuple[str, list[Frame], int, int, int | None]:
    """(sha256, kareler, genişlik, yükseklik, süre) — medya diske yazılmadan R2'den okunur."""
    from app.storage import storage

    if sniffed.kind == "image":
        data = await storage.get_bytes(key, settings.max_image_bytes)
        frames, w, h = load_image_frames(data)
        return sha256_bytes(data), frames, w, h, None
    sha = await storage.sha256(key)
    url = await storage.presign_get(key, expires=900)
    info = await video.probe(url)
    files = await video.extract_frames(url, workdir / "frames", info)   # küçük kareler, RAM'deki /tmp
    frames = [load_frame_file(f, ts) for f, ts in files]
    return sha, frames, info.width, info.height, info.duration_ms


async def analyze_file(key: str, sniffed: Sniffed, *, pool, redis, project_id,
                       profanity_level: str = "strict") -> MediaAnalysis:
    with tempfile.TemporaryDirectory(prefix="media_") as tmp:
        sha, frames, width, height, duration_ms = await _load(key, sniffed, Path(tmp))
        if not frames:
            raise InvalidMedia("no_frames")
        analyzed = [AnalyzedFrame(f) for f in frames]
        detection = DetectionResult()

        # 1) Engel listesi
        phashes = [f.phash for f in frames if f.phash is not None]
        match = await _blocklist_match(pool, project_id, sha, phashes)
        detection.providers.append({"provider": "hash_blocklist", "model": "phash64", "version": "1"})
        if match:
            detection.add("blocklist_match", 1.0)
            for a in analyzed:
                a.categories["blocklist_match"] = 1.0
            return MediaAnalysis(detection, analyzed, sha, width, height, duration_ms, match)

        # 2) Önbellek (aynı dosya, aynı model sürümü)
        cache_key = f"media:scores:{sha}:{PIPELINE_VERSION}:{profanity_level}"
        cached = None
        try:
            raw = await redis.get(cache_key)
            cached = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 — önbellek yoksa analiz yap
            cached = None

        ocr_texts: list[str] = []
        if cached and len(cached) == len(analyzed):
            for a, scores in zip(analyzed, cached):
                a.categories.update(scores)
        else:
            # 3a) Çıplaklık
            nudity = await detectors.detect_nudity([a.frame.image for a in analyzed])
            for a, scores in zip(analyzed, nudity):
                a.categories.update(scores)

            # 3b) Görseldeki yazı. Sadece ilk kareye bakmak yetmez: yazı animasyonun/videonun ortasındaki
            #     tek bir kareye gizlenebilir. Görsel olarak farklı her kare okunur, tekrarlayanlar atlanır.
            if settings.ocr_enabled:
                for a in _distinct_frames(analyzed, MAX_OCR_FRAMES):
                    text = await detectors.ocr_text(a.frame.image)
                    if text:
                        ocr_texts.append(text)
                    for name, score in detectors.text_scores(text, profanity_level).items():
                        a.categories[name] = max(a.categories.get(name, 0.0), score)

            try:
                await redis.set(cache_key, json.dumps([a.categories for a in analyzed]), ex=CACHE_TTL_S)
            except Exception:  # noqa: BLE001
                pass

        detection.providers.append({"provider": "nudenet", "model": detectors.MODEL_NAME, "version": detectors.NUDENET_VERSION})
        if settings.ocr_enabled:
            detection.providers.append({"provider": "ocr", "model": "tesseract", "version": detectors.OCR_VERSION})

        # 4) Kareler üzerinden en yüksek skor
        for a in analyzed:
            for name, score in a.categories.items():
                detection.add(name, score)

        unique = list(dict.fromkeys(t for t in ocr_texts if len(t) >= 3))
        return MediaAnalysis(detection, analyzed, sha, width, height, duration_ms, text=" ".join(unique)[:4000])

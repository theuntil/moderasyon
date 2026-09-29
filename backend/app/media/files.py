"""Görsel çözme ve kanıt kareleri: tamamen bellekte (diske yazılmaz).

Güvenlik:
- Decompression bomb: piksel sınırı aşılırsa açılmaz (küçük dosya, devasa görsel).
- Animasyonlu GIF/WebP'nin sadece ilk karesine bakmak yetmez; kareler eşit aralıklarla örneklenir.
- Kanıt kareleri yeniden kodlanır: EXIF (GPS konumu dahil) ve gömülü veriler atılır.
"""
import hashlib
import io
import warnings
from dataclasses import dataclass
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image, ImageOps, ImageStat

from app.config import settings

Image.MAX_IMAGE_PIXELS = settings.max_image_pixels
ANALYSIS_MAX_SIDE = 1280
EVIDENCE_MAX_SIDE = 800
MAX_ANIMATION_FRAMES = 8


class InvalidMedia(Exception):
    """Dosya bozuk, desteklenmiyor veya sınırları aşıyor. Tekrar denemek anlamsız."""


@dataclass
class Frame:
    image: Image.Image          # RGB, analiz boyutuna küçültülmüş
    timestamp_ms: int | None
    phash: int | None           # None: bilgi taşımayan kare (düz renk vb.)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _to_signed64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def perceptual_hash(img: Image.Image) -> int | None:
    # Neredeyse tek renkli karelerin hash'i birbirine çok benzer; engel listesinde her boş kareyi
    # engellemesin diye düşük bilgili karelerde hash üretilmez.
    stat = ImageStat.Stat(img.convert("L").resize((64, 64)))
    if stat.stddev[0] < 10:
        return None
    return _to_signed64(int(str(imagehash.phash(img)), 16))


def _prepare(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
    img.thumbnail((ANALYSIS_MAX_SIDE, ANALYSIS_MAX_SIDE))
    return img


def load_image_frames(data: bytes) -> tuple[list[Frame], int, int]:
    """Görseli bellekten güvenli şekilde açar. (kareler, genişlik, yükseklik) döner."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
                if width * height > settings.max_image_pixels:
                    raise InvalidMedia("image_too_large")
                n = getattr(im, "n_frames", 1)
                if n > MAX_ANIMATION_FRAMES:
                    indices = sorted({round(i * (n - 1) / (MAX_ANIMATION_FRAMES - 1)) for i in range(MAX_ANIMATION_FRAMES)})
                else:
                    indices = list(range(n))
                starts: dict[int, int] = {}
                if n > 1:
                    elapsed = 0
                    for i in range(n):
                        starts[i] = elapsed
                        im.seek(i)
                        elapsed += int(im.info.get("duration", 100) or 100)
                frames: list[Frame] = []
                for i in indices:
                    im.seek(i)
                    frame = _prepare(im.copy())
                    frames.append(Frame(frame, starts.get(i) if n > 1 else None, perceptual_hash(frame)))
                return frames, width, height
        except InvalidMedia:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise InvalidMedia("image_too_large")
        except Exception:
            raise InvalidMedia("invalid_image")


def load_frame_file(path: Path, timestamp_ms: int | None) -> Frame:
    """ffmpeg'in RAM'deki (tmpfs) geçici klasöre yazdığı küçük video karesi."""
    with Image.open(path) as im:
        img = _prepare(im.copy())
    return Frame(img, timestamp_ms, perceptual_hash(img))


def encode_evidence(img: Image.Image) -> tuple[bytes, int, int]:
    """Küçültülmüş, EXIF'siz JPEG (bayt olarak; R2'ye yüklenir)."""
    copy = img.copy()
    copy.thumbnail((EVIDENCE_MAX_SIDE, EVIDENCE_MAX_SIDE))
    buf = io.BytesIO()
    copy.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue(), copy.size[0], copy.size[1]


def to_bgr_array(img: Image.Image) -> np.ndarray:
    return np.ascontiguousarray(np.asarray(img)[:, :, ::-1])

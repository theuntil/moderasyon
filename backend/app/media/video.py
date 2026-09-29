"""Video: ffprobe ile doğrulama, ffmpeg ile kare çıkarma.

Video diske indirilmez: ffmpeg, R2'deki nesneyi bizim ürettiğimiz kısa ömürlü imzalı adresten okur
(sadece gereken bölümleri, HTTP range istekleriyle).

Güvenlik: ffmpeg karmaşık bir çözücüdür ve kötü niyetli dosyalar onu hedef alabilir.
- Protokol izin listesi: yalnızca R2 okuması için gereken http(s)/tls/tcp. Video içine gömülü başka
  protokol/kaynak referansları (file, concat, data, rtmp...) açılamaz.
- Süre, çözünürlük, kare sayısı ve iş parçacığı sınırlı; zaman aşımında süreç öldürülür.
- Kareler küçük JPEG'lere dönüştürülür; analiz orijinal akış üzerinde değil bu kareler üzerinde yapılır.
"""
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.media.files import InvalidMedia

PROBE_TIMEOUT_S = 20
EXTRACT_TIMEOUT_S = 180
MAX_FPS = 2.0
FRAME_MAX_SIDE = 1280


@dataclass
class VideoInfo:
    duration_ms: int
    width: int
    height: int


async def _run(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise InvalidMedia("video_processing_timeout")
    return proc.returncode, out, err


def _protocols(source: str) -> str:
    if source.startswith("https://"):
        return "https,tls,tcp"
    if source.startswith("http://"):
        return "http,tcp"          # yalnızca yerel test S3 sunucusu
    return "file"


async def probe(source: str) -> VideoInfo:
    code, out, _ = await _run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", _protocols(source), "-rw_timeout", "30000000",
         "-print_format", "json", "-show_format", "-show_streams", source],
        PROBE_TIMEOUT_S,
    )
    if code != 0:
        raise InvalidMedia("invalid_video")
    try:
        data = json.loads(out)
    except ValueError:
        raise InvalidMedia("invalid_video")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise InvalidMedia("no_video_stream")
    try:
        duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise InvalidMedia("invalid_video")
    if duration > settings.max_video_seconds:
        raise InvalidMedia("video_too_long")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0 or width * height > 8192 * 8192:
        raise InvalidMedia("invalid_video")
    return VideoInfo(int(duration * 1000), width, height)


async def extract_frames(source: str, out_dir: Path, info: VideoInfo) -> list[tuple[Path, int]]:
    """Videoya eşit aralıklarla yayılmış kareleri çıkarır. [(dosya, zaman_ms)] döner."""
    out_dir.mkdir(parents=True, exist_ok=True)
    duration_s = info.duration_ms / 1000
    max_frames = settings.video_max_frames
    fps = min(MAX_FPS, max_frames / max(duration_s, 0.5))
    code, _, err = await _run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", _protocols(source),
         "-rw_timeout", "30000000", "-threads", "2", "-i", source, "-map", "0:v:0", "-an", "-sn", "-dn",
         # Uzun kenar en fazla 1280 px: 640 px'de Retina ekran kayıtlarındaki ve yüksek çözünürlüklü
         # videolardaki normal boyutlu yazılar OCR ile okunamayacak kadar küçülüyordu.
         "-vf", f"fps={fps:.5f},scale='if(gt(iw,ih),min({FRAME_MAX_SIDE},iw),-2)':'if(gt(iw,ih),-2,min({FRAME_MAX_SIDE},ih))'",
         "-frames:v", str(max_frames),
         "-q:v", "4", str(out_dir / "f_%03d.jpg")],
        EXTRACT_TIMEOUT_S,
    )
    frames = sorted(out_dir.glob("f_*.jpg"))
    if code != 0 and not frames:
        raise InvalidMedia("invalid_video")
    if not frames:
        raise InvalidMedia("no_frames")
    # fps filtresi kareleri aralığın ortasından seçer: i. kare ≈ (i + 0.5) / fps saniyede
    return [(f, int(((i + 0.5) / fps) * 1000)) for i, f in enumerate(frames)]

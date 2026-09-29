"""Dosya türünü ilk baytlardan (magic bytes) tespit eder.

İstemcinin gönderdiği Content-Type veya dosya uzantısına güvenilmez; sadece burada tanınan
formatlar işlenir. Bilinmeyen her şey reddedilir.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Sniffed:
    kind: str   # image | video
    mime: str


def sniff(head: bytes) -> Sniffed | None:
    if head.startswith(b"\xff\xd8\xff"):
        return Sniffed("image", "image/jpeg")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Sniffed("image", "image/png")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return Sniffed("image", "image/gif")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Sniffed("image", "image/webp")
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return Sniffed("video", "video/x-msvideo")
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return Sniffed("video", "video/webm")
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif"):
            return None  # HEIC/AVIF henüz desteklenmiyor
        if brand == b"qt  ":
            return Sniffed("video", "video/quicktime")
        return Sniffed("video", "video/mp4")
    return None

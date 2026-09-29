"""Faz 1 text moderasyon pipeline'ı.

Şimdilik iki basit sinyal var: kelime listesi ve spam sezgileri.
Faz 2-3'te buraya yerel sınıflandırıcı (ör. BERTurk/ONNX) ve AI katmanı
provider olarak eklenecek; policy.py değişmeden kalacak.
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.moderation.normalize import normalize_for_matching, tokenize

_WORDLIST_DIR = Path(__file__).parent / "wordlists"
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)


@dataclass
class CategoryScore:
    name: str
    score: float


@dataclass
class DetectionResult:
    categories: list[CategoryScore] = field(default_factory=list)
    providers: list[dict] = field(default_factory=list)
    labels: set = field(default_factory=set)       # uygulamaya ve panele dönen etiketler (ör. "küfür", "terör:pkk")
    critical: bool = False                          # kritik: yasal saklama + severity=critical

    def add(self, name: str, score: float) -> None:
        for c in self.categories:
            if c.name == name:
                c.score = max(c.score, score)
                return
        self.categories.append(CategoryScore(name, score))


@lru_cache
def _load_wordlist(filename: str) -> tuple[frozenset[str], tuple[str, ...]]:
    exact: set[str] = set()
    prefixes: list[str] = []
    for line in (_WORDLIST_DIR / filename).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        term = normalize_for_matching(line)
        if len(term) <= 3:
            exact.add(term)
        else:
            prefixes.append(term)
    return frozenset(exact), tuple(prefixes)


def _wordlist_hits(tokens: list[str], filename: str) -> int:
    exact, prefixes = _load_wordlist(filename)
    return sum(1 for t in tokens if t in exact or t.startswith(prefixes))


PROFANITY_LEVELS = ("strict", "moderate", "off")


def profanity_hits(text: str) -> dict[str, int]:
    """Metindeki küfür katmanı eşleşmeleri: vulgar (ağır), slang (sokak ağzı), insult (hafif hakaret)."""
    tokens = tokenize(normalize_for_matching(text))
    return {"vulgar": _wordlist_hits(tokens, "tr_vulgar.txt"), "slang": _wordlist_hits(tokens, "tr_slang.txt"),
            "insult": _wordlist_hits(tokens, "tr_insult.txt")}


def run_text_pipeline(text: str, profanity_level: str = "strict") -> DetectionResult:
    """profanity_level (proje ayarı):
    - strict:   ağır küfür + sokak ağzı (amk, mk, aq) engellenir; hafif hakaret belirsiz sayılır
    - moderate: sadece ağır küfür engellenir; sokak ağzı ve hafif hakaret serbest
    - off:      küfür filtresi kapalı (nefret, tehdit vb. AI katmanında)"""
    result = DetectionResult()
    tokens = tokenize(normalize_for_matching(text))

    # 1) Küfür katmanları (proje küfür seviyesine göre)
    tokens_all = tokens
    vulgar = _wordlist_hits(tokens_all, "tr_vulgar.txt")
    slang = _wordlist_hits(tokens_all, "tr_slang.txt")
    insult = _wordlist_hits(tokens_all, "tr_insult.txt")
    if profanity_level != "off":
        if vulgar:
            result.add("harassment", 0.95 if vulgar + slang >= 2 else 0.90)
            result.labels.add("küfür")
        elif slang and profanity_level == "strict":
            result.add("harassment", 0.90)
            result.labels.add("küfür")
        elif insult and profanity_level == "strict":
            result.add("harassment", 0.75 if insult >= 2 else 0.60)
            result.labels.add("hakaret")
    result.providers.append({"provider": "wordlist", "model": f"tr_3tier:{profanity_level}", "version": "0.3.0"})

    # 2) Spam sezgileri
    url_count = len(_URL.findall(text))
    if url_count >= 3:
        result.add("spam", 0.75)
    elif url_count == 2:
        result.add("spam", 0.40)
    if len(tokens) >= 8 and len(set(tokens)) / len(tokens) < 0.3:
        result.add("spam", 0.60)  # aynı kelimelerin tekrarı
    result.providers.append({"provider": "heuristics", "model": "spam_basic", "version": "0.1.0"})

    return result

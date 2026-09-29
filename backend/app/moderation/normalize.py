"""Türkçeye özel metin normalizasyonu.

Amaç: "S1KT1RRR", "ş.e.r.e.f.s.i.z", "ORUSPU" gibi varyasyonları
aynı forma indirip kelime listesi ve sınıflandırıcıların işini kolaylaştırmak.
"""
import re
import unicodedata

# Türkçe büyük/küçük harf: Python'un lower()'ı "I" → "i" yapar, Türkçede "ı" olmalı.
_TR_UPPER_TO_LOWER = str.maketrans({"I": "ı", "İ": "i"})

# Eşleştirme için Türkçe karakterleri ASCII'ye katla (insanlar ikisini de yazar)
_TR_FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})

# Leetspeak
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "€": "e"})

_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_REPEATS = re.compile(r"(.)\1+")
_SPACES = re.compile(r"\s+")
# "ş e r e f s i z" / "s.i.k.t.i.r" gibi tek harfleri ayırarak yazılmış diziler
_SPACED_LETTERS = re.compile(r"\b(?:[a-z]\s){2,}[a-z]\b")


def turkish_lower(text: str) -> str:
    return text.translate(_TR_UPPER_TO_LOWER).lower()


def normalize_for_matching(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = turkish_lower(text)
    text = text.translate(_TR_FOLD)
    text = text.translate(_LEET)
    text = _NON_WORD.sub(" ", text)          # noktalama → boşluk
    text = _SPACES.sub(" ", text).strip()
    # "s i k t i r" → "siktir"
    text = _SPACED_LETTERS.sub(lambda m: m.group(0).replace(" ", ""), text)
    text = _REPEATS.sub(r"\1", text)          # "siktirrrr" → "siktir"
    return text


def tokenize(normalized: str) -> list[str]:
    return normalized.split() if normalized else []

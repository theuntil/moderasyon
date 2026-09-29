"""Platform kuralları: yasaklı kelimeler, yasaklı görsel/semboller, etiketler ve kritik içerik.

- Yasaklı kelimeler panelden eklenir (tüm platform veya tek proje). Eşleşme normalize edilmiş metinde
  yapılır; "p.k.k", "P K K", "pkk'lı" gibi gizleme denemeleri de yakalanır. Metinde ve görsel/videodaki
  yazıda (OCR) aranır.
- Yasaklı görseller (semboller, bayraklar, amblemler) AI görsel kontrolüyle aranır (ai_gateway).
- Her karar etiket taşır (ör. "küfür", "müstehcen", "terör:pkk"); uygulamaya `labels` olarak döner.
- Kritik içerik (pornografi, çocuk istismarı, terör ve "kritik" işaretli kurallar): yanıt `severity:
  "critical"`; yasal saklama açıksa orijinal medya, IP ve kullanıcı bilgisi yönetici silene kadar saklanır.
"""
import time
from dataclasses import dataclass

from app.moderation.normalize import normalize_for_matching, tokenize

CATEGORY_LABELS = {
    "sexual": "müstehcen", "nudity": "çıplaklık", "sexual_minors": "çocuk_istismarı",
    "violence": "şiddet", "self_harm": "kendine_zarar", "hate": "nefret", "extremism": "terör",
    "illicit": "yasa_dışı", "weapons": "silah", "drugs": "uyuşturucu", "spam": "spam",
    "scam": "dolandırıcılık", "blocklist_match": "engel_listesi", "harassment": "hakaret",
}
# Engellendiğinde kritik sayılan kategoriler (pornografi, çocuk istismarı, terör)
CRITICAL_CATEGORIES = {"sexual", "sexual_minors", "extremism"}
CACHE_TTL_S = 15


def normalize_term(term: str) -> str:
    return " ".join(tokenize(normalize_for_matching(term)))


@dataclass(frozen=True)
class Term:
    project_id: str | None
    normalized: str
    mode: str          # word | contains
    label: str
    critical: bool


def match_terms(text: str, terms: list[Term], project_id) -> list[Term]:
    """Metinde geçen yasaklı kelimeler. word: tam kelime/ifade; contains: kelime içinde de (ör. pkk'lı)."""
    if not text or not terms:
        return []
    tokens = tokenize(normalize_for_matching(text))
    if not tokens:
        return []
    spaced = " " + " ".join(tokens) + " "
    compact = "".join(tokens)
    pid = str(project_id) if project_id else None
    hits = []
    for t in terms:
        if t.project_id is not None and t.project_id != pid:
            continue
        if t.mode == "contains":
            if t.normalized.replace(" ", "") in compact:
                hits.append(t)
        elif f" {t.normalized} " in spaced:
            hits.append(t)
    return hits


class RulesCache:
    """Kelime ve görsel kuralları süreç içinde kısa süre tutulur (panel değişikliği en geç 15 sn'de yansır)."""

    def __init__(self) -> None:
        self._loaded = 0.0
        self.terms: list[Term] = []
        self.visual: list[dict] = []

    async def get(self, pool) -> "RulesCache":
        if time.monotonic() - self._loaded > CACHE_TTL_S:
            rows = await pool.fetch("SELECT project_id, normalized, match_mode, label, severity FROM custom_terms")
            self.terms = [Term(str(r["project_id"]) if r["project_id"] else None, r["normalized"], r["match_mode"],
                               r["label"], r["severity"] == "critical") for r in rows]
            self.visual = [dict(r) for r in await pool.fetch(
                "SELECT id::text AS id, label, description, severity FROM visual_rules WHERE enabled ORDER BY created_at"
            )]
            self._loaded = time.monotonic()
        return self


def apply_terms(detection, text: str | None, rules: RulesCache, project_id) -> None:
    for t in match_terms(text or "", rules.terms, project_id):
        detection.add("custom_term", 1.0)
        detection.labels.add(t.label)
        if t.critical:
            detection.critical = True


def final_labels(detection, decision: str, flagged: list[str]) -> list[str]:
    """Kararın etiketleri: kural/kelime listesi etiketleri + işaretlenen kategoriler. İzin verilen içerik etiketsiz."""
    if decision == "allow":
        return []
    labels = set(detection.labels)
    for name in flagged:
        label = CATEGORY_LABELS.get(name)
        if label and not (name == "harassment" and labels & {"küfür", "hakaret"}):
            labels.add(label)
    return sorted(labels)


def is_critical(detection, decision: str, flagged: list[str]) -> bool:
    return decision == "block" and (detection.critical or bool(set(flagged) & CRITICAL_CATEGORIES))

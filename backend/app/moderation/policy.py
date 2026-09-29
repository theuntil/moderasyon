"""Policy: tespit skorlarını (detection) karara (allow / review / block) çevirir.

İki katman:
1. Platform eşikleri (panel > Platform ayarları). Her değişiklik yeni bir policy sürümü oluşturur.
2. Proje policy'si (panel > Proje > Policy). Kategori bazında aksiyon ve eşik:
   - default: eşiklere göre karar
   - allow:   bu kategori bu projede yok sayılır (ör. flört uygulamasında "suggestive")
   - review:  en fazla incelemeye gider, otomatik engellenmez
   - block:   inceleme eşiğini geçtiği anda engellenir (ör. çocuk platformunda her şey sıkı)
   Kategori bazında "review"/"block" eşikleri platform eşiklerini ezer.

Kararın sürümü "v<platform>.p<proje revizyonu>" olarak saklanır; her karar hangi kurallarla verildiği bilinir.
"blocklist_match" (moderatörün engellediği içerik) proje policy'sinden etkilenmez, her zaman engellenir.

Karar modu (proje ayarı):
- two_step (varsayılan): sadece izin ver / engelle. Belirsiz bölgedeki (inceleme eşiği ile engelleme
  eşiği arası) içerik AI'ya sorulur; AI yoksa veya yanıt veremezse "uncertain_action" uygulanır
  (varsayılan: engelle; asla sessizce onaylanmaz).
- three_step: izin ver / incele / engelle. Belirsiz içerik AI'dan sonra hâlâ belirsizse insana gider.
"""
from dataclasses import dataclass

from app.moderation.text_pipeline import DetectionResult
from app.platform_settings import PlatformSettings

ACTIONS = ("default", "allow", "review", "block")
_RANK = {"allow": 0, "review": 1, "block": 2}
# Proje policy'si bunları gevşetemez: engel listesi ve reşit olmayanları içeren cinsel içerik
ALWAYS_BLOCK = {"blocklist_match", "sexual_minors", "custom_term", "forbidden_visual"}


DECISION_MODES = ("two_step", "three_step")
AI_MODES = ("off", "smart", "always")


@dataclass(frozen=True)
class ProjectPolicy:
    categories: dict
    ai_mode: str = "smart"             # off | smart (risk ≥ ai_trigger olunca) | always
    revision: int = 0
    decision_mode: str = "two_step"    # two_step | three_step
    uncertain_action: str = "block"    # two_step'te AI karar veremezse: block | allow
    ai_trigger: float = 0.5            # smart modda AI'yı tetikleyen risk skoru
    fallback_decision: str | None = None   # işlenemeyen içerik için uygulamaya önerilen karar
    profanity_level: str = "strict"    # strict | moderate | off

    @staticmethod
    def from_row(policy: dict | None, revision: int = 0) -> "ProjectPolicy":
        policy = policy or {}
        mode = policy.get("decision_mode")
        ai_mode = policy.get("ai_mode")
        return ProjectPolicy(
            categories=policy.get("categories") or {},
            ai_mode=ai_mode if ai_mode in AI_MODES else "smart",
            revision=revision,
            decision_mode=mode if mode in DECISION_MODES else "two_step",
            uncertain_action="allow" if policy.get("uncertain_action") == "allow" else "block",
            ai_trigger=float(policy.get("ai_trigger") or 0.5),
            fallback_decision=policy.get("fallback_decision") if policy.get("fallback_decision") in ("allow", "review", "block") else None,
            profanity_level=policy.get("profanity_level") if policy.get("profanity_level") in ("strict", "moderate", "off") else "strict",
        )

    def wants_ai(self, layer1: "PolicyDecision") -> bool:
        """AI'ya sorulmalı mı? Kesin engellenmiş içerik gönderilmez (maliyet ve gizlilik)."""
        if self.ai_mode == "off" or layer1.decision == "block":
            return False
        if self.ai_mode == "always":
            return True
        return layer1.max_score >= self.ai_trigger


EMPTY_POLICY = ProjectPolicy({})


@dataclass(frozen=True)
class PolicyDecision:
    decision: str        # allow | review | block
    reason: str          # client'a dönen, internal detay içermeyen gerekçe
    max_score: float
    policy_version: str
    category: str | None = None   # review kararında belirsizliğe yol açan kategori
    flagged: tuple = ()           # izin verilmeyen (incele/engelle) kategoriler: etiketler bunlardan üretilir


def _category_decision(name: str, score: float, settings: PlatformSettings, policy: ProjectPolicy) -> str:
    if name in ALWAYS_BLOCK and score >= (0.3 if name == "sexual_minors" else 0.5):
        return "block"
    rule = policy.categories.get(name) or {}
    action = rule.get("action", "default")
    review_t = float(rule.get("review", settings.threshold_review))
    block_t = float(rule.get("block", settings.threshold_block))
    if action == "allow":
        return "allow"
    if score < review_t:
        return "allow"
    if action == "block":
        return "block"
    if action == "review":
        return "review"
    return "block" if score >= block_t else "review"


def evaluate(detection: DetectionResult, settings: PlatformSettings, policy: ProjectPolicy = EMPTY_POLICY) -> PolicyDecision:
    version = f"{settings.policy_version_label}.p{policy.revision}" if policy.revision else settings.policy_version_label
    worst, worst_cat, worst_score = "allow", None, 0.0
    top_score = max((c.score for c in detection.categories), default=0.0)
    flagged = []
    for c in detection.categories:
        d = _category_decision(c.name, c.score, settings, policy)
        if d != "allow":
            flagged.append(c.name)
        if _RANK[d] > _RANK[worst] or (_RANK[d] == _RANK[worst] and d != "allow" and c.score > worst_score):
            worst, worst_cat, worst_score = d, c.name, c.score

    if worst == "block":
        return PolicyDecision("block", f"{worst_cat}_detected", worst_score, version, flagged=tuple(flagged))
    if worst == "review":
        return PolicyDecision("review", "content_requires_review", worst_score, version, worst_cat, tuple(flagged))
    return PolicyDecision("allow", "no_violation_detected", top_score, version)


def resolve_mode(decision: PolicyDecision, policy: ProjectPolicy) -> PolicyDecision:
    """İki adımlı modda hâlâ belirsiz kalan kararı izin/engele çevirir."""
    if decision.decision != "review" or policy.decision_mode == "three_step":
        return decision
    if policy.uncertain_action == "allow":
        return PolicyDecision("allow", "no_violation_detected", decision.max_score, decision.policy_version)
    return PolicyDecision("block", f"{decision.category or 'content'}_detected", decision.max_score,
                          decision.policy_version, flagged=decision.flagged)

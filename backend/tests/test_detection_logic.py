"""Tespit mantığı: parçalı çıplaklık taraması, AI'ın yalnızca değerlendirdiği kategoride hakemliği,
kaba kısaltmalar (servis gerektirmez)."""
from PIL import Image

from app.media import detectors
from app.moderation import ai
from app.moderation.text_pipeline import DetectionResult, run_text_pipeline


def test_large_frames_are_scanned_in_overlapping_tiles():
    assert len(detectors.tiles(Image.new("RGB", (640, 400)))) == 1          # küçük kare: tek geçiş
    parts = detectors.tiles(Image.new("RGB", (1280, 800)))
    assert len(parts) == 5 and parts[1].size == (768, 480)                 # tamamı + 4 örtüşen parça


def test_small_region_found_only_in_a_tile_is_reported(monkeypatch):
    calls = {"n": 0}

    class FakeDetector:
        def detect_batch(self, arrays, batch_size=4):
            calls["n"] += len(arrays)
            # Sadece sağ alt parçada (5. giriş) tespit var: tam karede görünmeyecek kadar küçük nesne
            return [[] for _ in arrays[:4]] + [[{"class": "FEMALE_GENITALIA_EXPOSED", "score": 0.9, "box": [0, 0, 1, 1]}]]

    monkeypatch.setattr(detectors, "_detector", FakeDetector())
    scores = detectors._detect_batch([Image.new("RGB", (1280, 800))])
    assert calls["n"] == 5 and scores[0]["sexual"] >= 0.9


def test_ai_only_overrides_categories_it_evaluated():
    local = DetectionResult()
    local.add("harassment", 0.6)          # görseldeki yazıdan (OCR)
    local.add("violence", 0.55)
    # OpenAI görsel moderasyonu hakareti değerlendirmez; şiddet için "yok" der
    merged = ai.merge(local, {"violence": 0.01}, judge=True, model="omni", evaluated=ai.MODERATION_IMAGE_EVAL)
    names = {c.name: c.score for c in merged.categories}
    assert names.get("harassment") == 0.6                 # bakmadığı konuda "sorun yok" sayılmaz
    assert "violence" not in names                        # değerlendirdiği konuda hakem
    # Metin de gönderildiyse hakaret de değerlendirilmiş olur
    merged = ai.merge(local, {"harassment": 0.02}, judge=True, model="omni",
                      evaluated=ai.MODERATION_TEXT_EVAL | ai.MODERATION_IMAGE_EVAL)
    assert "harassment" not in {c.name for c in merged.categories}


def test_vulgar_abbreviations_are_severe():
    for text in ("kocam mk senin", "oç", "amk"):
        top = max((c.score for c in run_text_pipeline(text).categories), default=0)
        assert top >= 0.9, text
    assert not run_text_pipeline("Makedonya MKE ankara").categories


def test_sensitive_media_toggle():
    assert not ai.may_send_images({"nudity": 0.6})                                     # varsayılan kapalı
    assert ai.may_send_images({"nudity": 0.6}, allow_sensitive=True, block_threshold=0.85)   # belirsiz → AI
    assert not ai.may_send_images({"sexual": 0.95}, allow_sensitive=True, block_threshold=0.85)  # kesin: gönderilmez


def test_term_matching_catches_obfuscation():
    from app.moderation.rules import Term, match_terms, normalize_term
    t = Term(None, normalize_term("pkk"), "contains", "terör:pkk", True)
    for s in ("P.K.K", "p k k'lılar", "pkkya", "PKK!"):
        assert match_terms(s, [t], None), s
    w = Term(None, normalize_term("apo"), "word", "terör:apo", True)
    assert match_terms("yaşasın apo", [w], None) and not match_terms("apollo görevi", [w], None)

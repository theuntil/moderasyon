"""Uygulama logları: zaman damgalı, tek satır; Dokploy/Docker loglarında okunaklı."""
import logging


def setup() -> None:
    root = logging.getLogger()
    if not any(getattr(h, "_moderation", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler._moderation = True
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)

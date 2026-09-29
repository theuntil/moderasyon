# İki hedef:
#   base  → api, admin-api, worker (hafif: ffmpeg/OCR/model yok)
#   media → media-worker (ffmpeg, Tesseract Türkçe, NudeNet ONNX)

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv \
    # Sadece iç ağdaki proxy'ler (Traefik, panel nginx) X-Forwarded-For için güvenilir.
    # '*' kullanmayın: sahte başlıkla IP ban atlatılır.
    FORWARDED_ALLOW_IPS="127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --create-home --uid 10001 appuser

COPY app ./app
COPY migrations ./migrations
COPY start-api.sh ./start-api.sh
RUN chmod +x start-api.sh

USER appuser
CMD ["sh", "./start-api.sh"]


FROM base AS media
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-tur tesseract-ocr-eng libgomp1 \
 && rm -rf /var/lib/apt/lists/*
COPY requirements-media.txt .
# 320n modeli pakete gömülü. Daha hassas 640m modeli build sırasında indirilmeye çalışılır; inmezse veya
# bozuksa silinir ve 320n ile devam edilir (build asla bu yüzden bozulmaz). Çalışırken internete çıkılmaz.
ARG NUDENET_640M_URL=https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.onnx
ENV NUDENET_MODEL_PATH=/models/640m.onnx
COPY scripts/fetch_models.py ./scripts/fetch_models.py
RUN pip install --no-cache-dir -r requirements-media.txt \
 && python scripts/fetch_models.py
USER appuser
CMD ["arq", "app.worker.main.MediaWorkerSettings"]

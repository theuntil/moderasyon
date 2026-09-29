#!/bin/sh
set -e

# Migration'lar advisory lock ile korunur; birden fazla API instance güvenle başlar.
python -m app.migrate

# Gerçek istemci IP'si: X-Forwarded-For zincirinde sağdan ilk "güvenilmeyen" adres.
# Güvenilen ağlar FORWARDED_ALLOW_IPS ortam değişkeninden okunur (Dockerfile'da iç ağlar).
# '*' KULLANMAYIN: istemci sahte X-Forwarded-For göndererek IP banını atlatabilir.
exec uvicorn app.api.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --no-server-header \
  --timeout-keep-alive 95 \
  --workers "${API_WORKERS:-2}"

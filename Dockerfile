# Backend FastAPI para Google Cloud Run (São Paulo / southamerica-east1).
# NÃO commitar enquanto o Railway for o host de produção: o Railway, ao ver um
# Dockerfile, troca o build de Nixpacks/Procfile para Docker. Este arquivo é usado
# pelo Cloud Build via `gcloud run deploy --source .` (não precisa estar no git).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependências primeiro (camada cacheável).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código.
COPY . .

# Cloud Run injeta $PORT (default 8080). Sem --workers fixo: o Cloud Run escala por
# INSTÂNCIAS (não por workers); 1 worker async por instância é o idiomático aqui.
# A concorrência é controlada pela config de concurrency do serviço Cloud Run.
ENV PORT=8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips="*"

"""Validação de uploads públicos (C-NEW-2 / spec §13.4).

Endpoints de upload abertos (candidatura) são vetor de abuso. Este módulo:
- lê o arquivo em chunks com LIMITE DE TAMANHO (aborta antes de esgotar disco);
- valida os MAGIC BYTES reais (não confia no header content_type, falsificável);
- oferece um rate limiter por IP (reusa a infra in-memory do login).
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request, UploadFile

from app.services.login_rate_limiter import InMemoryLoginRateLimiter

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))  # 15 MB (gen. p/ fotos de celular)
_CHUNK_SIZE = 64 * 1024
_HEAD_SIZE = 16

# Assinaturas (magic bytes) das imagens aceitas para documentos.
_PREFIX_SIGNATURES = (
    b"\xff\xd8\xff",          # JPEG
    b"\x89PNG\r\n\x1a\n",    # PNG
    b"GIF87a",
    b"GIF89a",
)
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heif", b"heim", b"heis"}

# Rate limiter por IP: 20 uploads / 10 min por origem (beta, single replica).
upload_rate_limiter = InMemoryLoginRateLimiter(
    max_failures=int(os.getenv("UPLOAD_RATE_LIMIT", "20")),
    window_seconds=float(os.getenv("UPLOAD_RATE_WINDOW_SECONDS", "600")),
)


def _looks_like_image(head: bytes) -> bool:
    if any(head.startswith(sig) for sig in _PREFIX_SIGNATURES):
        return True
    # WEBP: "RIFF" .... "WEBP"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    # HEIC/HEIF: caixa "ftyp" no offset 4 com marca conhecida
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS:
        return True
    return False


def _looks_like_pdf(head: bytes) -> bool:
    # PDFs reais comecam com "%PDF-".
    return head.startswith(b"%PDF-")


def enforce_upload_rate_limit(request: Request) -> None:
    client_ip = (request.client.host if request and request.client else "") or "unknown"
    if upload_rate_limiter.is_blocked(client_ip):
        raise HTTPException(status_code=429, detail="Muitos envios. Tente novamente em alguns minutos.")
    upload_rate_limiter.record_failure(client_ip)


async def read_image_upload_safely(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Lê o upload em chunks aplicando limite de tamanho e valida que é imagem real."""
    chunks: list[bytes] = []
    total = 0
    head = b""
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Arquivo excede o limite de {max_bytes // (1024 * 1024)} MB.",
            )
        if len(head) < _HEAD_SIZE:
            head = (head + chunk)[:_HEAD_SIZE]
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    if not _looks_like_image(head):
        raise HTTPException(status_code=400, detail="O arquivo enviado nao e uma imagem valida.")
    return b"".join(chunks)


async def read_document_upload_safely(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Lê o upload aplicando limite de tamanho e aceita IMAGEM ou PDF (certidoes).

    Usado pelos uploads de certidao de antecedentes (Background Check Fase 0), que
    sao PDFs oficiais. Valida magic bytes (nao confia no content_type, falsificavel).
    """
    chunks: list[bytes] = []
    total = 0
    head = b""
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Arquivo excede o limite de {max_bytes // (1024 * 1024)} MB.",
            )
        if len(head) < _HEAD_SIZE:
            head = (head + chunk)[:_HEAD_SIZE]
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    if not (_looks_like_image(head) or _looks_like_pdf(head)):
        raise HTTPException(status_code=400, detail="Envie uma imagem ou PDF valido.")
    return b"".join(chunks)

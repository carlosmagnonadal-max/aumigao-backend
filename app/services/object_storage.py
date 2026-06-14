"""Camada de storage de uploads: Cloudflare R2 (S3) quando configurado, senão disco local.

Compatibilidade total: SEM as envs de R2 (Railway/dev/testes), usa o filesystem
exatamente como antes. COM R2 (Cloud Run), grava e serve do bucket — necessário
porque o Cloud Run não tem disco persistente (o sistema de arquivos é efêmero).

Envs (todas obrigatórias p/ ligar o R2):
  R2_BUCKET, R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
"""
from __future__ import annotations

import logging
import mimetypes
import os
import threading
from pathlib import Path

from app.services.signed_uploads import UPLOAD_ROOT, normalize_upload_path, upload_file_path

logger = logging.getLogger(__name__)


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


_client = None
_client_lock = threading.Lock()


def r2_enabled() -> bool:
    return bool(
        _env("R2_BUCKET")
        and _env("R2_ENDPOINT")
        and _env("R2_ACCESS_KEY_ID")
        and _env("R2_SECRET_ACCESS_KEY")
    )


def _bucket() -> str:
    return _env("R2_BUCKET")


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                from botocore.config import Config

                _client = boto3.client(
                    "s3",
                    endpoint_url=_env("R2_ENDPOINT"),
                    aws_access_key_id=_env("R2_ACCESS_KEY_ID"),
                    aws_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
                    region_name="auto",
                    config=Config(
                        signature_version="s3v4",
                        retries={"max_attempts": 3, "mode": "standard"},
                    ),
                )
    return _client


def _key_for(destination: Path | str) -> str | None:
    """Converte um destino (Path sob UPLOAD_ROOT ou caminho relativo) na key do objeto R2."""
    try:
        rel = Path(destination).resolve().relative_to(UPLOAD_ROOT.resolve())
        return rel.as_posix()
    except (ValueError, OSError):
        return normalize_upload_path(str(destination))


def save(destination: Path, data: bytes, content_type: str | None = None) -> None:
    """Grava o upload. Em R2 quando configurado; senão no disco local (comportamento antigo)."""
    if r2_enabled():
        key = _key_for(destination)
        if not key:
            raise ValueError(f"caminho de upload inválido para R2: {destination}")
        kwargs = {"Bucket": _bucket(), "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        _get_client().put_object(**kwargs)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def fetch(upload_path: str) -> tuple[bytes, str] | None:
    """Retorna (bytes, content_type) do objeto, ou None se não existir. Usado no serve do R2."""
    key = normalize_upload_path(upload_path)
    if not key:
        return None
    try:
        obj = _get_client().get_object(Bucket=_bucket(), Key=key)
    except Exception:
        return None
    body = obj["Body"].read()
    ctype = obj.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"
    return body, ctype


def local_exists(upload_path: str) -> bool:
    fp = upload_file_path(upload_path)
    return bool(fp and fp.is_file())

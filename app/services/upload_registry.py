"""Registro de uploads (spec §13). Cria um UploadFile com os metadados do arquivo.

NÃO commita — o caller commita junto com o resto da operação. Envolto em try/except
para nunca quebrar o upload em si caso o registro falhe.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.upload_file import UploadFile


def record_upload(
    db: Session,
    *,
    context: str,
    storage_path: str,
    owner_id: str | None = None,
    tenant_id: str | None = None,
    document_type: str | None = None,
    mime_type: str | None = None,
    size_bytes: int | None = None,
) -> UploadFile | None:
    try:
        record = UploadFile(
            context=context,
            owner_id=owner_id,
            tenant_id=tenant_id,
            document_type=document_type,
            storage_path=storage_path,
            mime_type=mime_type,
            size_bytes=size_bytes,
        )
        db.add(record)
        return record
    except Exception:
        return None

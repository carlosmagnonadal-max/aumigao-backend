"""tenant_branding.py — Rotas de branding do tenant (self-service).

Familia A (self-service — admin do proprio tenant):
  GET  /tenants/current/branding-runtime        — publico, sem auth
  GET  /tenants/{tenant_id}/branding-runtime    — publico, sem auth
  PATCH /api/admin/tenants/current/branding     — RBAC branding.update
  POST  /api/admin/tenants/current/branding/upload-image — RBAC branding.update

Upload de imagem de branding:
  Tipos: jpg, png, webp. Limite 5 MB. Valida magic bytes (nao confia no
  Content-Type). Rate limit por IP (mesmo infra do login). Prefixo de arquivo:
  `tenant_branding_{kind}-<uuid>.<ext>` — FORA dos prefixos sensiveis de walker.
  Resposta: {"url": "<url_publica>"}

Regra de ouro do repo: todo endpoint de ESCRITA admin chama get_admin_tenant_scope
no topo (injeta GUC RLS antes de INSERT/UPDATE).
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_admin
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope, is_super_admin
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant_branding import TenantBrandingRuntimeResponse
from app.schemas.tenant_branding_update import TenantBrandingUpdatePayload
from app.services import object_storage
from app.services.signed_uploads import UPLOAD_ROOT as UPLOADS_BASE
from app.services.tenant_branding_service import get_tenant_branding_runtime, update_tenant_branding_runtime
from app.services.tenant_context import resolve_current_tenant
from app.services.upload_registry import record_upload
from app.services.upload_validation import enforce_upload_rate_limit, read_image_upload_safely

# ---------------------------------------------------------------------------
# Upload de imagem de branding
# ---------------------------------------------------------------------------
_BRANDING_UPLOAD_ROOT = UPLOADS_BASE / "tenant-branding-images"
_BRANDING_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_BRANDING_UPLOAD_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_BRANDING_VALID_KINDS = {"logo", "icon", "splash"}


def _branding_upload_extension(filename: str | None, content_type: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    _allowed = {".jpg", ".jpeg", ".png", ".webp"}
    if suffix in _allowed:
        return suffix
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type in {"image/jpeg"}:
        return ".jpg"
    raise HTTPException(status_code=400, detail="Tipo de arquivo nao suportado. Use jpg, png ou webp.")


def _branding_image_url(request: Request, path: Path) -> str:
    relative = path.relative_to(UPLOADS_BASE).as_posix()
    public_base = (os.getenv("PUBLIC_BACKEND_URL") or str(request.base_url)).strip().rstrip("/")
    if "railway.app" in public_base and public_base.startswith("http://"):
        public_base = public_base.replace("http://", "https://", 1)
    return f"{public_base}/uploads/{relative}"


router = APIRouter(prefix="/tenants", tags=["tenant-branding"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-branding"])
admin_api_router = APIRouter(prefix="/api/admin/tenants", tags=["admin-tenant-branding"], dependencies=[Depends(require_permission("branding.read"))])


@router.get("/current/branding-runtime", response_model=TenantBrandingRuntimeResponse)
@api_router.get("/current/branding-runtime", response_model=TenantBrandingRuntimeResponse)
def get_current_branding_runtime(request: Request, db: Session = Depends(get_db)):
    return get_tenant_branding_runtime(db, getattr(request.state, "tenant_id", None))


@router.get("/{tenant_id}/branding-runtime", response_model=TenantBrandingRuntimeResponse)
@api_router.get("/{tenant_id}/branding-runtime", response_model=TenantBrandingRuntimeResponse)
def get_branding_runtime(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_branding_runtime(db, tenant_id)


@admin_api_router.patch("/current/branding", response_model=TenantBrandingRuntimeResponse)
def update_current_branding(
    payload: TenantBrandingUpdatePayload,
    request: Request,
    admin: User = Depends(require_permission("branding.update")),
    db: Session = Depends(get_db),
):
    # A5: admin de tenant deve editar o branding do SEU tenant, não o default.
    # super_admin mantém o comportamento original (resolve_current_tenant / act-as).
    # Injeta o escopo RLS (super_admin → '*'; admin → próprio tenant) ANTES de
    # ler/gravar tenant_branding — senão o UPDATE viola WITH CHECK (o GUC ficaria
    # no tenant default, pois o BFF do admin-web não injeta X-Tenant-Slug).
    get_admin_tenant_scope(admin, db)
    if not is_super_admin(admin):
        # admin de tenant: usa o tenant_id do usuário autenticado
        if not admin.tenant_id:
            raise HTTPException(status_code=400, detail="Admin sem tenant_id configurado")
        tenant = db.get(Tenant, admin.tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant do admin nao encontrado")
    else:
        tenant = resolve_current_tenant(db, request)
    return update_tenant_branding_runtime(db, tenant, payload, actor=admin)


@admin_api_router.post("/current/branding/upload-image", status_code=201)
async def upload_branding_image(
    request: Request,
    file: UploadFile = File(...),
    kind: str = "logo",
    admin: User = Depends(require_permission("branding.update")),
    db: Session = Depends(get_db),
):
    """Faz upload de uma imagem de branding (logo, icon ou splash) e devolve a URL publica.

    O admin usa a URL devolvida para preencher logo_url / icon_url / splash_image_url
    no PATCH de branding. Tipos aceitos: jpg, png, webp. Limite 5 MB.

    RBAC: branding.update (mesma permissao do PATCH).
    Rate limit: mesmo infra do login, 20 uploads/10min por IP.
    Magic bytes: validados internamente (nao confia no Content-Type).
    """
    # Valida kind antes de qualquer IO.
    kind_norm = (kind or "").strip().lower()
    if kind_norm not in _BRANDING_VALID_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"kind invalido: '{kind}'. Use: {sorted(_BRANDING_VALID_KINDS)}.",
        )

    enforce_upload_rate_limit(request)

    if not file.content_type or file.content_type not in _BRANDING_ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de arquivo nao suportado. Use jpg, png ou webp.")

    extension = _branding_upload_extension(file.filename, file.content_type)
    content = await read_image_upload_safely(file, max_bytes=_BRANDING_UPLOAD_MAX_BYTES)

    destination = _BRANDING_UPLOAD_ROOT / f"tenant_branding_{kind_norm}-{uuid4().hex}{extension}"

    object_storage.save(destination, content, file.content_type)

    # Injeta escopo RLS para o registro de upload (write cross-tenant seguro).
    get_admin_tenant_scope(admin, db)
    record_upload(
        db,
        context="tenant_branding",
        owner_id=admin.id,
        tenant_id=admin.tenant_id,
        document_type=f"branding_{kind_norm}",
        storage_path=str(destination),
        mime_type=file.content_type,
        size_bytes=len(content),
    )
    db.commit()

    return {"url": _branding_image_url(request, destination)}

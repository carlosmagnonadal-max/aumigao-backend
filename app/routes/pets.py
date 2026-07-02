import os
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.services.upload_validation import enforce_upload_rate_limit, read_image_upload_safely
from app.services.upload_registry import record_upload
from app.models.pet import Pet
from app.models.walk import Walk
from app.models.user import User
from app.schemas.pet import PetCreate, PetResponse, PetUpdate
from app.services.tenant_context import resolve_current_tenant_id
from app.services import object_storage
from app.services.signed_uploads import UPLOAD_ROOT as UPLOADS_BASE
from app.utils.url_utils import normalize_media_url as _normalize_media_url

router = APIRouter(prefix="/pets", tags=["pets"])

UPLOAD_ROOT = UPLOADS_BASE / "pet-photos"
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


def _safe_upload_extension(filename: str | None, content_type: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in ALLOWED_UPLOAD_EXTENSIONS:
        return suffix
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type in {"image/heic", "image/heif"}:
        return ".heic"
    if content_type == "image/jpeg":
        return ".jpg"
    # G3: extensão/tipo não reconhecido — rejeitar explicitamente.
    raise HTTPException(status_code=400, detail="Tipo de arquivo nao suportado.")


def _public_upload_url(request: Request, path: Path) -> str:
    relative = path.relative_to(UPLOADS_BASE).as_posix()
    public_base_url = (os.getenv("PUBLIC_BACKEND_URL") or str(request.base_url)).strip().rstrip("/")
    if "railway.app" in public_base_url and public_base_url.startswith("http://"):
        public_base_url = public_base_url.replace("http://", "https://", 1)
    return f"{public_base_url}/uploads/{relative}"


# Alias local: mantém o nome original; lógica centralizada em url_utils.
_normalize_pet_photo_url = _normalize_media_url


def _parse_walk_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _has_blocking_walk(pet_id: str, db: Session) -> bool:
    now = datetime.utcnow()
    limit = now + timedelta(hours=24)
    blocking_statuses = {
        "Agendado",
        "Confirmado",
        "Indo buscar o pet",
        "Passeando agora",
        "pending_walker_confirmation",
        "auto_rematching",
        "walker_accepted",
        "ride_scheduled",
        "walker_arriving",
        "ride_in_progress",
    }

    for walk in db.query(Walk).filter(Walk.pet_id == pet_id).all():
        status = walk.operational_status or walk.status
        if status not in blocking_statuses:
            continue
        scheduled_at = _parse_walk_start(walk.scheduled_date)
        if scheduled_at and now <= scheduled_at <= limit:
            return True

    return False


def _current_tenant_id(user: User, db: Session) -> str:
    tenant_id = user.tenant_id or resolve_current_tenant_id(db)
    if not user.tenant_id:
        user.tenant_id = tenant_id
    return tenant_id


def _pet_tenant_filter(tenant_id: str):
    return or_(Pet.tenant_id == tenant_id, Pet.tenant_id.is_(None))

@router.post("/upload-photo", status_code=201)
async def upload_pet_photo(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # U3/G2: rate limit em endpoint autenticado de upload de foto.
    enforce_upload_rate_limit(request)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Envie uma imagem valida.")

    destination_dir = UPLOAD_ROOT / user.id
    destination_dir.mkdir(parents=True, exist_ok=True)

    extension = _safe_upload_extension(file.filename, file.content_type)
    destination = destination_dir / f"pet-{uuid4().hex}{extension}"

    # G7: fotos de pet limitadas a 5 MB.
    content = await read_image_upload_safely(file, max_bytes=5 * 1024 * 1024)

    object_storage.save(destination, content, file.content_type)

    record_upload(
        db, context="pet", owner_id=user.id,
        document_type="pet_photo", storage_path=str(destination),
        mime_type=file.content_type, size_bytes=len(content),
    )
    db.commit()

    return {
        "photo_url": _public_upload_url(request, destination),
        "url": _public_upload_url(request, destination),
    }

@router.get("", response_model=list[PetResponse])
def list_pets(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = _current_tenant_id(user, db)
    pets = db.query(Pet).filter(Pet.tutor_id == user.id, _pet_tenant_filter(tenant_id)).order_by(Pet.created_at.desc()).all()

    for pet in pets:
        if pet.is_neutered is None:
            pet.is_neutered = False

    db.commit()
    return pets

@router.post("", response_model=PetResponse)
def create_pet(payload: PetCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = payload.model_dump()
    data["photo_url"] = _normalize_pet_photo_url(data.get("photo_url"))
    tenant_id = _current_tenant_id(user, db)
    # Plano free: máx N pets por tutor (default 2, env FREE_PLAN_PETS_PER_TUTOR).
    # Só bloqueia pet NOVO — excedentes de antes do downgrade permanecem.
    if tenant_id:
        from app.models.tenant import Tenant
        from app.services.tenant_free_plan_service import enforce_free_plan_pet_limit

        _tenant_pet_check = db.get(Tenant, tenant_id)
        if _tenant_pet_check is not None:
            enforce_free_plan_pet_limit(db, _tenant_pet_check, user.id)
    pet = Pet(id=str(uuid4()), tutor_id=user.id, tenant_id=tenant_id, **data)
    db.add(pet)
    db.commit()
    db.refresh(pet)
    return pet

@router.get("/{pet_id}", response_model=PetResponse)
def get_pet(pet_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = _current_tenant_id(user, db)
    pet = db.query(Pet).filter(Pet.id == pet_id, Pet.tutor_id == user.id, _pet_tenant_filter(tenant_id)).first()
    if not pet:
        raise HTTPException(status_code=404, detail="Pet nao encontrado")
    return pet

@router.put("/{pet_id}", response_model=PetResponse)
def update_pet(pet_id: str, payload: PetUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = get_pet(pet_id, user, db)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "photo_url":
            value = _normalize_pet_photo_url(value)
        setattr(pet, key, value)
    db.commit()
    db.refresh(pet)
    return pet

@router.delete("/{pet_id}")
def delete_pet(pet_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pet = get_pet(pet_id, user, db)
    if _has_blocking_walk(pet_id, db):
        raise HTTPException(status_code=409, detail="Não é possível excluir este pet com passeio agendado para as próximas 24 horas.")
    db.delete(pet)
    db.commit()
    return {"ok": True}

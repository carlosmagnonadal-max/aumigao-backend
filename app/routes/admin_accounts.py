"""CRUD de contas admin (Feature 1).

Gerencia usuários com role em ("admin", "super_admin") sem criar novo modelo —
usa User diretamente (tabela users).

Regras de permissão:
- GET  /admin/accounts          → lista. super_admin vê todos; admin vê só seu tenant.
- POST /admin/accounts          → cria conta admin. Só super_admin pode criar
                                  super_admin ou criar em outro tenant.
- PATCH /admin/accounts/{id}    → atualiza full_name, role, is_active. Não permite
                                  desativar a si mesmo. Só super_admin pode mudar
                                  role para/de super_admin ou mexer em outro tenant.

Permissão base: require_permission("admin.access") + checagens explícitas de role.
NÃO implementa hard-delete (só is_active=false).
"""
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_password_hash
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import apply_tenant_filter, get_admin_tenant_scope
from app.models.user import User

ADMIN_ROLES = {"admin", "super_admin"}

router = APIRouter(
    prefix="/admin",
    tags=["admin-accounts"],
    dependencies=[Depends(require_permission("admin.access"))],
)
api_router = APIRouter(
    prefix="/api/admin",
    tags=["admin-accounts"],
    dependencies=[Depends(require_permission("admin.access"))],
)


# ---------------------------------------------------------------------------
# Serialização
# ---------------------------------------------------------------------------

def _serialize_account(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name or "",
        "role": user.role,
        "tenant_id": user.tenant_id,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


# ---------------------------------------------------------------------------
# Schemas Pydantic
# ---------------------------------------------------------------------------

class AccountCreate(BaseModel):
    email: str = Field(..., max_length=254)
    full_name: str = Field("", max_length=200)
    role: str = Field(..., pattern="^(admin|super_admin)$")
    tenant_id: str | None = Field(None, max_length=100)
    password: str = Field(..., min_length=8, max_length=128)


class AccountUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=200)
    role: str | None = Field(None, pattern="^(admin|super_admin)$")
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Helpers de autorização
# ---------------------------------------------------------------------------

def _is_super_admin(user: User) -> bool:
    return getattr(user, "role", None) == "super_admin"


def _assert_can_manage_target(actor: User, target_tenant_id: str | None, target_role: str) -> None:
    """Levanta 403 se o actor não pode criar/editar uma conta com os atributos dados."""
    if _is_super_admin(actor):
        return  # super_admin pode tudo

    # admin de tenant NÃO pode criar/gerenciar super_admin
    if target_role == "super_admin":
        raise HTTPException(status_code=403, detail="Apenas super_admin pode gerenciar contas super_admin.")

    # admin de tenant NÃO pode criar/gerenciar em outro tenant
    actor_tenant = getattr(actor, "tenant_id", None)
    if target_tenant_id and target_tenant_id != actor_tenant:
        raise HTTPException(status_code=403, detail="Admin nao pode gerenciar contas de outro tenant.")


# ---------------------------------------------------------------------------
# Rotas — padrão dual-router (decorator empilhado)
# ---------------------------------------------------------------------------

@router.get("/accounts")
@api_router.get("/accounts")
def list_accounts(
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    """Lista usuários com role admin/super_admin, com tenant-scoping."""
    scope = get_admin_tenant_scope(admin, db)
    query = db.query(User).filter(User.role.in_(ADMIN_ROLES))
    query = apply_tenant_filter(query, User, scope)
    rows = query.order_by(User.created_at.desc()).all()
    return {"items": [_serialize_account(u) for u in rows], "total": len(rows)}


@router.post("/accounts", status_code=201)
@api_router.post("/accounts", status_code=201)
def create_account(
    payload: AccountCreate,
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    """Cria conta admin ou super_admin com senha cifrada."""
    # Normaliza e valida e-mail minimamente
    email = payload.email.strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(status_code=400, detail="E-mail invalido.")

    # Regras: role=admin exige tenant_id
    if payload.role == "admin" and not payload.tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id e obrigatorio para role 'admin'.")

    # Verifica permissão do actor
    _assert_can_manage_target(admin, payload.tenant_id, payload.role)

    # Unicidade de e-mail
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=409, detail="E-mail ja cadastrado.")

    user = User(
        id=str(uuid4()),
        email=email,
        full_name=(payload.full_name or "").strip(),
        role=payload.role,
        tenant_id=payload.tenant_id,
        password_hash=get_password_hash(payload.password),
        is_active=True,
        must_change_password=True,  # B2: troca obrigatoria no 1o login
        created_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _serialize_account(user)


@router.patch("/accounts/{user_id}")
@api_router.patch("/accounts/{user_id}")
def update_account(
    user_id: str,
    payload: AccountUpdate,
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    """Atualiza full_name, role e/ou is_active de uma conta admin."""
    target = db.get(User, user_id)
    if not target or target.role not in ADMIN_ROLES:
        raise HTTPException(status_code=404, detail="Conta admin nao encontrada.")

    # Verifica permissão sobre o target ANTES de qualquer mudança
    new_role = payload.role if payload.role is not None else target.role
    _assert_can_manage_target(admin, target.tenant_id, new_role)

    # Se o target já é super_admin, só outro super_admin pode editar
    if target.role == "super_admin" and not _is_super_admin(admin):
        raise HTTPException(status_code=403, detail="Apenas super_admin pode editar contas super_admin.")

    # Proteção: admin não pode desativar a si mesmo
    if payload.is_active is False and target.id == admin.id:
        raise HTTPException(status_code=400, detail="Admin nao pode desativar a propria conta.")

    if payload.full_name is not None:
        target.full_name = payload.full_name.strip()

    if payload.role is not None:
        target.role = payload.role

    if payload.is_active is not None:
        target.is_active = payload.is_active

    db.commit()
    db.refresh(target)
    return _serialize_account(target)

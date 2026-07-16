import logging
import os
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.core.security import get_password_hash, verify_password
from app.models.user import User
from app.services.tenant_seed_service import default_tenant_id


_logger = logging.getLogger("aumigao.admin_seed")

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _configured_admins() -> list[dict[str, str]]:
    pairs = [
        ("ADMIN_EMAIL", "ADMIN_PASSWORD", "admin", "Admin Aumigao"),
        ("SUPER_ADMIN_EMAIL", "SUPER_ADMIN_PASSWORD", "super_admin", "Super Admin Aumigao"),
    ]
    admins = []
    for email_key, password_key, role, fallback_name in pairs:
        email = (os.getenv(email_key) or "").strip().strip('"')
        password = (os.getenv(password_key) or "").strip().strip('"')
        if email and password:
            admins.append({"email": email, "password": password, "role": role, "full_name": fallback_name})
    return admins


def ensure_configured_admin_users(db: Session) -> list[User]:
    admins = []
    tenant_id = default_tenant_id(db)
    for config in _configured_admins():
        user = db.query(User).filter(User.email == config["email"]).first()
        if not user:
            user = User(
                id=str(uuid5(NAMESPACE_URL, f"aumigao-admin:{config['email']}")),
                email=config["email"],
                full_name=config["full_name"],
                role=config["role"],
                password_hash=get_password_hash(config["password"]),
                tenant_id=tenant_id,
                is_active=True,
            )
            db.add(user)
        else:
            # SEC: conta JÁ existe — NÃO sobrescrever password_hash/is_active/role.
            # Redefinir a senha do env aqui reverteria uma rotação de senha feita
            # pela UI (o env fica desatualizado e "reanimaria" a senha antiga a cada
            # boot). Só backfill NÃO-destrutivo (tenant_id/full_name se vazios) e um
            # aviso quando as credenciais do env não conferem mais (esperado após
            # rotação).
            user.tenant_id = user.tenant_id or tenant_id
            if not user.full_name:
                user.full_name = config["full_name"]
            if not verify_password(config["password"], user.password_hash or ""):
                _logger.warning(
                    "admin_seed: credenciais do env nao conferem para %s "
                    "(esperado apos rotacao de senha pela UI; nao sobrescrevendo).",
                    config["email"],
                )
        admins.append(user)
    if admins:
        db.commit()
    return admins

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.main  # noqa: E402,F401
from app.core.database import SessionLocal, get_database_diagnostics, mask_database_url  # noqa: E402
from app.core.security import verify_password  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.admin_seed_service import _configured_admins, ensure_configured_admin_users  # noqa: E402


ADMIN_ROLES = {"admin", "super_admin", "superadmin"}


def _admin_rows(db) -> list[User]:
    return (
        db.query(User)
        .filter(User.role.in_(ADMIN_ROLES))
        .order_by(User.email.asc())
        .all()
    )


def _print_database_diagnostics() -> None:
    diagnostics = get_database_diagnostics()
    print("Banco usado:")
    print(f"  DATABASE_URL: {mask_database_url(diagnostics['database_url'])}")
    print(f"  env: {diagnostics['env_path']}")
    if "sqlite_path" in diagnostics:
        print(f"  sqlite absoluto: {diagnostics['sqlite_path']}")


def _print_admins(label: str, admins: list[User]) -> None:
    print(label)
    if not admins:
        print("  nenhum admin encontrado")
        return
    for admin in admins:
        print(
            f"  email={admin.email} role={admin.role} ativo={bool(admin.is_active)} "
            f"id={admin.id}"
        )


def main() -> int:
    configured = _configured_admins()
    _print_database_diagnostics()
    print("Admins configurados no .env:")
    if not configured:
        print("  nenhum ADMIN_EMAIL/SUPER_ADMIN_EMAIL com senha configurado")
        return 1
    for item in configured:
        print(f"  email={item['email']} role={item['role']}")

    db = SessionLocal()
    try:
        _print_admins("Admins antes:", _admin_rows(db))
        ensured = ensure_configured_admin_users(db)
        db.expire_all()
        _print_admins("Admins depois:", _admin_rows(db))

        print("Validacao de senha .env:")
        ok = True
        for item in configured:
            user = db.query(User).filter(User.email == item["email"]).first()
            password_ok = bool(user and verify_password(item["password"], user.password_hash))
            role_ok = bool(user and user.role == item["role"])
            active_ok = bool(user and user.is_active)
            print(
                f"  {item['email']}: senha={'OK' if password_ok else 'FALHOU'} "
                f"role={'OK' if role_ok else 'FALHOU'} ativo={'OK' if active_ok else 'FALHOU'}"
            )
            ok = ok and password_ok and role_ok and active_ok

        print(f"Admins preservados/criados: {len(ensured)}")
        print("Dados operacionais criados: 0")
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

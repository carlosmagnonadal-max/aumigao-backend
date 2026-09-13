"""LGPD — direitos do titular (art. 18) sobre a PRÓPRIA conta.

GET  /me/data-export       → JSON estruturado com os dados pessoais do titular.
POST /me/account-deletion  → exclusão/anonimização da conta (reautenticação exigida).

Montadas também sob /api/v1 (main.py). Autenticação: get_current_user (token com
revogação por token_version). Sessão de dados: escopo GLOBAL de RLS porque a
identidade é global (Modelo B) — TODA query do serviço filtra pelo id do titular
autenticado (mesmo contrato de get_tutor_self_db/get_walker_self_db com flag ON).

Reautenticação na exclusão (padrão do app):
  - conta com senha  → `password` (mesmo verify_password do login/change-password);
  - conta social (Google/Apple, password_hash vazio) → `provider` + `token` recém-emitido
    pelo provedor, validado pelos MESMOS verificadores do /auth/social e /auth/link-apple
    (Apple: assinatura JWKS + audience, âncora no `sub`; Google: tokeninfo com audience).
Falhas de reautenticação são rate-limited por usuário (5 / 15 min).
"""
from __future__ import annotations

import logging

import anyio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_global_db
from app.core.security import verify_password
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.routes import auth as auth_routes
from app.services.audit_service import record_audit_log
from app.services.data_subject_rights_service import (
    DPO_CONTACT,
    anonymize_account,
    build_data_export,
    deletion_blockers,
    is_account_anonymized,
    purge_upload_files,
)
from app.services.login_rate_limiter import _make_rate_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["lgpd"])

# Exportação: 5 por hora por usuário (payload grande; evita raspagem com token vazado).
_export_rate_limiter = _make_rate_limiter(max_failures=5, window_seconds=3600, key_prefix="lgpd_export")
# Reautenticação da exclusão: 5 falhas por 15 min por usuário (mesmo padrão do change-password).
_deletion_reauth_limiter = _make_rate_limiter(max_failures=5, window_seconds=900, key_prefix="lgpd_delete")


class AccountDeletionRequest(BaseModel):
    # Confirmação explícita de intenção — a UI deve exigir ação deliberada do titular.
    confirm: bool = False
    password: str | None = Field(default=None, max_length=256)
    provider: str | None = Field(default=None, max_length=16)  # "google" | "apple"
    token: str | None = Field(default=None, max_length=4096)


def _load_titular(db: Session, user: User) -> User:
    titular = db.get(User, user.id)
    if titular is None:  # pragma: no cover - get_current_user já garantiu existência
        raise HTTPException(status_code=401, detail="Usuario invalido")
    return titular


@router.get("/data-export")
def export_my_data(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_global_db),
):
    if _export_rate_limiter.is_blocked(user.id):
        raise HTTPException(
            status_code=429,
            detail="Limite de exportações atingido. Tente novamente em 1 hora.",
            headers={"Retry-After": "3600"},
        )
    _export_rate_limiter.record_failure(user.id)

    titular = _load_titular(db, user)
    payload = build_data_export(db, titular)

    # Trilha de auditoria SEM PII (só o fato + id pseudonimizado do titular).
    record_audit_log(
        db,
        action="lgpd.data_export",
        entity_type="user",
        entity_id=titular.id,
        actor=titular,
        tenant_id=titular.tenant_id,
    )
    db.commit()

    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="aumigao-meus-dados.json"',
        },
    )


def _reauthenticate(titular: User, payload: AccountDeletionRequest) -> None:
    limiter_key = titular.id
    if _deletion_reauth_limiter.is_blocked(limiter_key):
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em 15 minutos.")

    def _fail() -> None:
        _deletion_reauth_limiter.record_failure(limiter_key)
        # 403 (não 401): o app trata 401 como sessão expirada e desloga.
        raise HTTPException(
            status_code=403,
            detail={"code": "reautenticacao_invalida", "message": "Não foi possível confirmar sua identidade."},
        )

    if payload.password:
        current_hash = titular.password_hash or ""
        if current_hash and verify_password(payload.password, current_hash):
            _deletion_reauth_limiter.clear(limiter_key)
            return
        _fail()

    provider = (payload.provider or "").strip().lower()
    if provider and payload.token:
        try:
            if provider == "apple":
                data = auth_routes._decode_apple_jwt_payload(payload.token)
                sub = data.get("sub")
                verified_email = (data.get("email") or "").strip().lower()
                if titular.apple_sub:
                    ok = bool(sub) and sub == titular.apple_sub
                else:
                    ok = bool(verified_email) and verified_email == (titular.email or "").strip().lower()
            elif provider == "google":
                info = anyio.from_thread.run(auth_routes._google_user_info, payload.token)
                email = (info.get("email") or "").strip().lower()
                ok = bool(email) and email == (titular.email or "").strip().lower()
            else:
                ok = False
        except HTTPException:
            ok = False
        if ok:
            _deletion_reauth_limiter.clear(limiter_key)
            return
        _fail()

    raise HTTPException(
        status_code=400,
        detail={
            "code": "reautenticacao_obrigatoria",
            "message": "Informe sua senha ou entre novamente com Google/Apple para confirmar a exclusão.",
            "method": "password" if (titular.password_hash or "") else "social",
        },
    )


@router.post("/account-deletion")
def delete_my_account(
    payload: AccountDeletionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_global_db),
):
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail={"code": "confirmacao_obrigatoria", "message": "Confirme a exclusão definitiva da conta."},
        )

    # Serializa pedidos concorrentes do MESMO titular (no-op em SQLite).
    titular = db.query(User).filter(User.id == user.id).with_for_update().first()
    if titular is None:  # pragma: no cover
        raise HTTPException(status_code=401, detail="Usuario invalido")

    # Idempotência: pedido repetido/concorrente após a anonimização já concluída.
    if is_account_anonymized(titular):
        return {"status": "already_deleted"}

    _reauthenticate(titular, payload)

    blockers = deletion_blockers(db, titular)
    if blockers:
        record_audit_log(
            db,
            action="lgpd.account_deletion_blocked",
            entity_type="user",
            entity_id=titular.id,
            actor=titular,
            tenant_id=titular.tenant_id,
            after={"reasons": [b["code"] for b in blockers]},
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail={
                "code": blockers[0]["code"],
                "message": blockers[0]["message"],
                "reasons": blockers,
            },
        )

    role = titular.role
    tenant_id = titular.tenant_id
    result = anonymize_account(db, titular)
    record_audit_log(
        db,
        action="lgpd.account_deletion",
        entity_type="user",
        entity_id=titular.id,
        actor=titular,
        tenant_id=tenant_id,
        after={"role": role, "counts": result["counts"]},
    )
    db.commit()

    removed_files = purge_upload_files(result["upload_paths"])
    logger.info(
        "lgpd_account_deleted role=%s files_listed=%d files_removed=%d",
        role, len(result["upload_paths"]), removed_files,
    )

    return {
        "status": "deleted",
        "message": (
            "Sua conta foi excluída e seus dados pessoais foram anonimizados. Registros "
            "financeiros, fiscais e de passeios são mantidos de forma anonimizada pelo prazo "
            "legal (LGPD, art. 16)."
        ),
        "retained_categories": [
            "pagamentos_e_repasses",
            "assinaturas_e_creditos",
            "passeios_sem_endereco",
            "avaliacoes_sem_comentario",
            "aceites_de_termos",
            "reclamacoes_e_auditoria",
        ],
        "dpo_contact": DPO_CONTACT,
    }

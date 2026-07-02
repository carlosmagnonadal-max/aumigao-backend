"""BG-6 — checagem automatica de sancoes (Portal da Transparencia, GRATIS).

Consulta as bases publicas CEIS (empresas inidoneas/suspensas) e CNEP (empresas
punidas — Lei Anticorrupcao) do Portal da Transparencia pelo CPF do passeador, no
momento em que ele envia uma certidao E quando o admin valida. E APOIO, nao gate.

Comportamento (dormente por default):
  - SEM TRANSPARENCIA_API_KEY configurada => NO-OP silencioso: retorna None e NAO
    toca no profile. A feature inteira fica desligada ate a chave existir.
  - COM chave => consulta CEIS+CNEP de forma SINCRONA, timeout curto (4s) e
    FAIL-OPEN: qualquer erro/timeout/HTTP!=200 NUNCA bloqueia o fluxo — loga, marca
    o veredito como "error" e segue.

Veredito (persistencia MINIMIZADA — LGPD): grava SO status + timestamp em
walker_profiles (sanctions_check_status / sanctions_checked_at). NUNCA guarda o
dossie retornado pela API.

Veredito "hit" NAO muda o status agregado das certidoes (nao vira flagged
automatico — a decisao e humana). Alem do campo, notifica os admins do tenant
(mesmo padrao do credit_refund_service._notify_admins_credit_refund).

NADA de sancoes e exposto ao tutor nem ao proprio walker.

Auth do Portal: header `chave-api-dados`. Doc: api.portaldatransparencia.gov.br.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models.walker_profile import WalkerProfile

logger = logging.getLogger("aumigao.sanctions_service")

# Endpoints publicos (v1). CEIS + CNEP aceitam consulta por CPF.
_BASE_URL = "https://api.portaldatransparencia.gov.br/api-de-dados"
_CEIS_URL = f"{_BASE_URL}/ceis"
_CNEP_URL = f"{_BASE_URL}/cnep"

# Timeout curto — a checagem e apoio sincrono; nao pode segurar o request do usuario.
_TIMEOUT_SECONDS = 4.0


def _api_key() -> str | None:
    """Chave do Portal da Transparencia. Ausente => feature inteira dormente."""
    key = (os.getenv("TRANSPARENCIA_API_KEY") or "").strip()
    return key or None


def _cpf_param_name() -> str:
    """Nome do parametro de consulta por CPF (parametrizavel por seguranca).

    O Portal usa `cpfSancionado` para os endpoints /ceis e /cnep. Deixamos
    configuravel via env caso a API mude o nome do parametro.
    """
    return (os.getenv("TRANSPARENCIA_CPF_PARAM") or "cpfSancionado").strip()


def _clean_cpf(cpf: str | None) -> str:
    return re.sub(r"\D", "", cpf or "")


def _query_base(url: str, key: str, cpf: str) -> bool:
    """Consulta UMA base (CEIS ou CNEP). Retorna True se ha registro (hit).

    Levanta em erro/timeout/HTTP!=200 — o caller decide o fail-open.
    """
    resp = httpx.get(
        url,
        headers={"chave-api-dados": key, "Accept": "application/json"},
        params={_cpf_param_name(): cpf, "pagina": 1},
        timeout=_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} em {url}")
    data = resp.json()
    # A API devolve uma LISTA — nao-vazia significa que ha sancao para o CPF.
    return isinstance(data, list) and len(data) > 0


def _notify_admins_sanctions_hit(db: "Session", profile: "WalkerProfile", tenant_id: str | None) -> None:
    """Alerta os admins do tenant sobre um POSSIVEL registro de sancao.

    Best-effort — nunca bloqueia o fluxo. NAO carrega detalhes da sancao (LGPD):
    so aponta o passeador para revisao manual. Segue o padrao de
    credit_refund_service._notify_admins_credit_refund.
    """
    try:
        from app.models.user import User
        from app.routes.notifications import NotificationCreate, _create_notification

        admins = (
            db.query(User)
            .filter(User.role.in_(["admin", "super_admin"]), User.tenant_id == tenant_id)
            .all()
            if tenant_id
            else db.query(User).filter(User.role.in_(["admin", "super_admin"])).all()
        )
        for admin in admins:
            _create_notification(db, NotificationCreate(
                user_id=admin.id,
                user_role=admin.role,
                tenant_id=tenant_id,
                title="⚠️ Passeador com possivel registro em base de sancoes",
                message=(
                    "A checagem automatica encontrou um possivel registro para este "
                    "passeador nas bases publicas de sancoes (CEIS/CNEP). Isso NAO "
                    "reprova o cadastro automaticamente — verifique manualmente antes "
                    "de decidir."
                ),
                type="sanctions_hit_review",
                related_entity_type="walker",
                related_entity_id=profile.user_id,
                # LGPD: sem dossie — so o ponteiro para o profile.
                metadata={"walker_profile_id": profile.id},
            ))
    except Exception:
        logger.exception(
            "sanctions: falha best-effort ao notificar admins walker_profile_id=%s",
            getattr(profile, "id", None),
        )


def run_sanctions_check(
    db: "Session",
    profile: "WalkerProfile",
    tenant_id: str | None,
) -> str | None:
    """Executa a checagem de sancoes para um passeador. Fail-open.

    Retorna o veredito ("clear"|"hit"|"error") ou None (no-op: sem chave ou sem CPF).
    Persiste SO veredito + timestamp no profile (nao commita — o caller commita).
    Em "hit", notifica os admins do tenant. NUNCA levanta excecao.
    """
    key = _api_key()
    if not key:
        # Feature dormente: sem chave, nao mexe em nada.
        return None

    cpf = _clean_cpf(getattr(profile, "cpf", None))
    if not cpf:
        # Sem CPF nao ha o que consultar.
        return None

    try:
        hit = _query_base(_CEIS_URL, key, cpf) or _query_base(_CNEP_URL, key, cpf)
        verdict = "hit" if hit else "clear"
    except Exception as exc:
        # FAIL-OPEN: erro/timeout/HTTP!=200 nunca bloqueia — loga e segue.
        logger.warning(
            "sanctions: checagem falhou (fail-open) walker_profile_id=%s: %s",
            getattr(profile, "id", None), exc,
        )
        verdict = "error"

    profile.sanctions_check_status = verdict
    profile.sanctions_checked_at = datetime.utcnow()

    if verdict == "hit":
        # hit NAO altera background_check_status (decisao humana) — so notifica.
        _notify_admins_sanctions_hit(db, profile, tenant_id)

    logger.info(
        "sanctions: veredito=%s walker_profile_id=%s tenant_id=%s",
        verdict, getattr(profile, "id", None), tenant_id,
    )
    return verdict

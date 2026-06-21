"""Servico de Confianca do passeador — selos, certificacoes e nivel (compute-only).

Substitui o `verified` booleano (placeholder). Selos/certificacoes/nivel sao GLOBAIS
(o passeador e da plataforma); a EXIBICAO e gated por tenant (flag `verified_walkers`),
o que e responsabilidade da camada de rota/front — este servico apenas CALCULA.

Tudo e derivado dos dados/servicos JA existentes (sem tabela nova / sem migracao):
- contagem de passeios concluidos e reputacao -> `reputation_service`
- risco e cancelamento -> `reputation_service.calculate_hybrid_reputation_score`
- documentos / has_vehicle / status / created_at -> `WalkerProfile`
- incidentes criticos (90 dias) -> `Complaint` (severity critica, alvo = passeador)

Spec: docs/CONFIANCA-PASSEADOR.md

NAO faz o rename global de niveis (Iniciante/Confiavel/... -> Bronze/...): isso e do
integrador (matching/painel/mobile). Aqui o nivel ja e computado nos rotulos novos.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.complaint import Complaint
from app.models.walker_profile import WalkerProfile
from app.services import reputation_service
from app.constants import (
    LEVEL_PRATA_MIN_WALKS,
    LEVEL_PRATA_MIN_RATING,
    LEVEL_OURO_MIN_WALKS,
    LEVEL_OURO_MIN_RATING,
    LEVEL_DIAMANTE_MIN_WALKS,
    LEVEL_DIAMANTE_MIN_RATING,
)

# Status considerados "aprovado ou superior" para o cadastro estar liberado.
APPROVED_STATUSES = {"approved", "active"}
# Status que representa a analise documental concluida (identidade verificada).
ACTIVE_STATUS = "active"

# Janela de incidentes criticos (spec: "0 incidentes criticos em 90 dias").
CRITICAL_INCIDENT_WINDOW_DAYS = 90
# Severidade de reclamacao considerada incidente critico (ver complaint_service).
CRITICAL_COMPLAINT_SEVERITIES = {"critica"}

# Criterios numericos (espelham a spec — Camadas 1, 2 e 3).
VERIFIED_MIN_WALKS = 20
VERIFIED_MIN_RATING = 4.7
VERIFIED_MIN_REVIEWS = 5
VERIFIED_MAX_CANCELLATION = 12.0

EXPERIENCE_MIN_WALKS = 50
EXPERIENCE_MIN_MONTHS = 3
PREMIUM_MIN_RATING = 4.9
PREMIUM_MIN_WALKS = 100

# Cortes de nível importados de app.constants (fonte única — B3).
# Reexportados aqui para que código que já importa de walker_trust_service
# não precise mudar o import.
__all__ = [
    "LEVEL_PRATA_MIN_WALKS", "LEVEL_PRATA_MIN_RATING",
    "LEVEL_OURO_MIN_WALKS", "LEVEL_OURO_MIN_RATING",
    "LEVEL_DIAMANTE_MIN_WALKS", "LEVEL_DIAMANTE_MIN_RATING",
    "compute_walker_level", "compute_walker_trust",
    "critical_incidents_count",
]


def _profile(walker_user_id: str, db: Session) -> WalkerProfile | None:
    return db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_user_id).first()


def _has_all_documents(profile: WalkerProfile | None) -> bool:
    if not profile:
        return False
    return bool(
        profile.document_url
        and profile.identity_document_back_url
        and profile.selfie_url
        and profile.proof_of_address_url
    )


def _months_since(created_at: datetime | None) -> float:
    if not created_at:
        return 0.0
    days = (datetime.utcnow() - created_at).days
    return days / 30.0


def critical_incidents_count(walker_user_id: str, db: Session, *, window_days: int = CRITICAL_INCIDENT_WINDOW_DAYS) -> int:
    """Reclamacoes criticas contra o passeador na janela (default 90 dias)."""
    since = datetime.utcnow() - timedelta(days=window_days)
    return (
        db.query(Complaint)
        .filter(
            Complaint.target_type == "walker",
            Complaint.target_user_id == walker_user_id,
            Complaint.severity.in_(CRITICAL_COMPLAINT_SEVERITIES),
            Complaint.created_at >= since,
        )
        .count()
    )


def _cadastro_verificado(profile: WalkerProfile | None, db: Session, walker_user_id: str) -> bool:
    # Cadastro completo + status >= aprovado. OTP de email/telefone NAO existe -> MVP = "validado":
    # exigimos os dados de cadastro preenchidos e um usuario (email) existente.
    if not profile:
        return False
    if profile.status not in APPROVED_STATUSES:
        return False
    user = reputation_service.get_walker_identity(walker_user_id, db)["user"]
    has_email = bool(user and getattr(user, "email", None))
    cadastro_completo = bool(profile.full_name and profile.cpf and profile.phone)
    return cadastro_completo and has_email


def _identidade_verificada(profile: WalkerProfile | None) -> bool:
    # Pos-analise manual: documentos aprovados -> status `active`. "Todo active ja tem."
    if not profile:
        return False
    return profile.status == ACTIVE_STATUS and _has_all_documents(profile)


def compute_walker_trust(db: Session, walker_user_id: str) -> dict:
    """Calcula selos + certificacoes automaticas + nivel de um passeador.

    COMPUTE-ONLY: nada e persistido. Retorna um dict serializavel por
    `app.schemas.walker_trust.WalkerTrustResponse`.
    """
    profile = _profile(walker_user_id, db)

    summary = reputation_service.reputation_summary(walker_user_id, db)
    hybrid = reputation_service.calculate_hybrid_reputation_score(walker_user_id, db)

    total_walks = summary["total_walks"]
    rating = summary["rating_average"]
    reviews_count = summary["reviews_count"]
    risk_level = hybrid["risk_level"]
    cancellation_rate = float(hybrid["behavior_details"].get("cancellation_rate", 0.0))
    months_active = _months_since(profile.created_at if profile else None)
    critical_incidents = critical_incidents_count(walker_user_id, db)
    is_active = bool(profile and profile.status == ACTIVE_STATUS)

    # ----- Camada 1: selos -----
    cadastro_verificado = _cadastro_verificado(profile, db, walker_user_id)
    identidade_verificada = _identidade_verificada(profile)
    passeador_verificado = (
        is_active
        and identidade_verificada
        and total_walks >= VERIFIED_MIN_WALKS
        and reviews_count >= VERIFIED_MIN_REVIEWS
        and rating >= VERIFIED_MIN_RATING
        and risk_level == "normal"
        and cancellation_rate < VERIFIED_MAX_CANCELLATION
        and critical_incidents == 0
    )
    # Background Check Fase 0 — selo de antecedentes verificados (PF + TJ validadas).
    # Dormente ate ligarem a flag de tenant `background_checks`: enquanto o passeador
    # nao enviar/validar certidoes, background_check_status fica "none" => selo False.
    antecedentes_verificados = bool(profile and getattr(profile, "background_check_status", "none") == "verified")
    seals = {
        "cadastro_verificado": cadastro_verificado,
        "identidade_verificada": identidade_verificada,
        "passeador_verificado": passeador_verificado,
        "antecedentes_verificados": antecedentes_verificados,
    }

    # ----- Camada 2: certificacoes automaticas -----
    documentacao_completa = _has_all_documents(profile)
    endereco_confirmado = bool(profile and profile.proof_of_address_url)
    possui_transporte = bool(profile and profile.has_vehicle)
    experiencia_comprovada = total_walks >= EXPERIENCE_MIN_WALKS and months_active >= EXPERIENCE_MIN_MONTHS
    atendimento_premium = rating >= PREMIUM_MIN_RATING and total_walks >= PREMIUM_MIN_WALKS

    certifications = [
        {
            "key": "documentacao_completa",
            "label": "Documentacao Completa",
            "icon": "document",
            "granted": documentacao_completa,
        },
        {
            "key": "endereco_confirmado",
            "label": "Endereco Confirmado",
            "icon": "home",
            "granted": endereco_confirmado,
        },
        {
            "key": "possui_transporte",
            "label": "Possui Transporte",
            "icon": "car",
            "granted": possui_transporte,
        },
        {
            "key": "experiencia_comprovada",
            "label": "Experiencia Comprovada",
            "icon": "dog",
            "granted": experiencia_comprovada,
        },
        {
            "key": "atendimento_premium",
            "label": "Atendimento Premium",
            "icon": "star",
            "granted": atendimento_premium,
        },
    ]

    # ----- Camada 3: nivel (Bronze/Prata/Ouro/Diamante) -----
    level = compute_walker_level(
        is_active=is_active,
        total_walks=total_walks,
        rating=rating,
        risk_level=risk_level,
        cancellation_rate=cancellation_rate,
        critical_incidents=critical_incidents,
        passeador_verificado=passeador_verificado,
    )

    return {
        "walker_user_id": walker_user_id,
        "seals": seals,
        "certifications": certifications,
        "level": level,
        "metrics": {
            "total_walks": total_walks,
            "rating_average": rating,
            "reviews_count": reviews_count,
            "risk_level": risk_level,
            "cancellation_rate": cancellation_rate,
            "months_active": round(months_active, 1),
            "critical_incidents_90d": critical_incidents,
            "is_active": is_active,
        },
    }


def compute_walker_level(
    *,
    is_active: bool,
    total_walks: int,
    rating: float,
    risk_level: str,
    cancellation_rate: float,
    critical_incidents: int,
    passeador_verificado: bool,
) -> str:
    """Nivel auto nos rotulos novos (Bronze->Diamante), conforme a spec (Camada 3).

    Sem o passeador estar `active` ele ainda nao tem nivel publico de confianca.
    """
    if not is_active:
        return "Bronze"

    # Diamante
    if (
        total_walks >= LEVEL_DIAMANTE_MIN_WALKS
        and rating >= LEVEL_DIAMANTE_MIN_RATING
        and risk_level == "normal"
        and cancellation_rate < 8.0
        and critical_incidents == 0
        and passeador_verificado
    ):
        return "Diamante"
    # Ouro
    if (
        total_walks >= LEVEL_OURO_MIN_WALKS
        and rating >= LEVEL_OURO_MIN_RATING
        and risk_level == "normal"
        and cancellation_rate < 12.0
    ):
        return "Ouro"
    # Prata
    if (
        total_walks >= LEVEL_PRATA_MIN_WALKS
        and rating >= LEVEL_PRATA_MIN_RATING
        and risk_level in {"normal", "attention"}
    ):
        return "Prata"
    # Bronze (active, <10 passeios ou nao atingiu os cortes acima)
    return "Bronze"

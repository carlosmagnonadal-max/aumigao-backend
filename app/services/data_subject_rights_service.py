"""Direitos do titular (LGPD, art. 18) — exportação dos próprios dados e exclusão/anonimização.

Promessa da política de privacidade publicada (site/app/privacidade, seções 8.1 e 10):
acesso/portabilidade em formato estruturado e eliminação/anonimização, ressalvadas as
hipóteses de guarda obrigatória (art. 16: obrigação legal/fiscal, exercício regular de
direitos, prevenção a fraudes).

ESCOPO DE DADOS (Modelo B — identidade GLOBAL): o titular é UMA linha em `users`, mas
seus dados podem estar em vários tenants (tutor que usa 2 estabelecimentos, passeador
da rede). Por isso as funções abaixo recebem uma sessão de escopo GLOBAL de RLS e
TODA query filtra explicitamente pelo id do titular autenticado — nunca por tenant da
request. Dado de terceiro só entra no mínimo necessário (ex.: primeiro nome do
passeador de um passeio do tutor; nome do estabelecimento).

Nada aqui commita: o caller (rota) commita junto com a trilha de auditoria.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.legal_acceptance import LegalAcceptance
from app.models.notification import Notification
from app.models.password_reset_code import PasswordResetCode
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.pet_health_record import PetHealthRecord
from app.models.pet_reminder import PetReminder
from app.models.pet_self_walk import PetSelfWalk
from app.models.pet_share_link import PetShareLink
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.protected_chat_message import ProtectedChatMessage
from app.models.push_token import PushToken
from app.models.recurring_plan import RecurringPlan, TutorSubscription
from app.models.support_ticket import SupportTicket
from app.models.contact_message import ContactMessage
from app.models.tenant import Tenant
from app.models.tutor_profile import TutorProfile
from app.models.upload_file import UploadFile
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_location_ping import WalkLocationPing
from app.models.walk_review import WalkReview
from app.models.walk_share_link import WalkShareLink
from app.models.walk_tip import WalkTip
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.models.walker_earning import WalkerEarning, WE_VOID
from app.models.walker_network_profile import WalkerNetworkProfile
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview

logger = logging.getLogger(__name__)

EXPORT_FORMAT_VERSION = "1.0"
CONTROLLER_NAME = "Aumigão Walk Tecnologia"
DPO_CONTACT = "contato@aumigaowalk.com.br"

# Marcador de conta anonimizada. O domínio .invalid (RFC 2606) nunca recebe e-mail e o
# local-part é aleatório — não deriva de nenhum dado do titular. Serve de marcador de
# idempotência (conta já excluída) sem precisar de coluna/migration nova.
ANONYMIZED_EMAIL_DOMAIN = "anonimizado.invalid"
ANONYMIZED_NAME = "Conta excluída"
ANONYMIZED_PET_NAME = "Pet removido"
REMOVED_TEXT = "[removido a pedido do titular]"

ADMIN_ROLES = {"admin", "super_admin", "interno"}
WALKER_ROLES = {"walker", "passeador"}

# Passeio "vivo" = qualquer estado que ainda pode gerar deslocamento do passeador,
# entrega do pet, cobrança ou repasse. Terminais (ride_completed, ride_cancelled,
# canceled_by_tutor, no_walker_found) NÃO bloqueiam.
BLOCKING_WALK_OPERATIONAL_STATUSES = frozenset({
    "awaiting_payment",
    "pending_walker_confirmation",
    "walker_accepted",
    "walker_declined",
    "auto_rematching",
    "priority_matching",
    "reschedule_requested",
    "awaiting_tutor_reconfirmation",
    "ride_scheduled",
    "walker_arriving",
    "pet_handover_confirmed",
    "ride_in_progress",
    "awaiting_completion_review",
    "completion_rejected",
})
# Linhas legadas podem ter operational_status parado no default ("ride_scheduled")
# com o status legado já terminal — o status legado terminal prevalece.
LEGACY_TERMINAL_WALK_STATUSES = frozenset({"Finalizado", "Cancelado"})

# Assinatura ainda cobrando no gateway (overdue continua gerando cobrança).
BLOCKING_SUBSCRIPTION_STATUSES = frozenset({"active", "overdue"})

# Saque ainda não liquidado nem rejeitado.
PENDING_WITHDRAWAL_STATUSES = frozenset({
    "pending",
    "processing",
    "em_processamento",
    "pagamento_sandbox_criado",
    "aguardando_pagamento",
    "AWAITING_RISK_ANALYSIS",
})

# Uploads do próprio titular cujo arquivo físico é removido. walk_completion (fotos de
# evidência do passeio) fica preservado: prova do serviço prestado (exercício de direitos).
PURGEABLE_UPLOAD_CONTEXTS = frozenset({"pet", "walker_kit", "partner_application"})


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de serialização
# ─────────────────────────────────────────────────────────────────────────────

def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _pick(obj: Any, fields: Iterable[str]) -> dict:
    """Serializa SOMENTE os campos da allowlist (nunca o objeto inteiro)."""
    return {f: _jsonable(getattr(obj, f, None)) for f in fields}


def _first_name(full_name: str | None) -> str | None:
    name = (full_name or "").strip()
    return name.split()[0] if name else None


def _tenant_names(db: Session, tenant_ids: Iterable[str | None]) -> dict[str, str]:
    ids = {t for t in tenant_ids if t}
    if not ids:
        return {}
    return {t.id: t.name for t in db.query(Tenant).filter(Tenant.id.in_(ids)).all()}


def is_account_anonymized(user: User) -> bool:
    return (not user.is_active) and (user.email or "").endswith("@" + ANONYMIZED_EMAIL_DOMAIN)


# ─────────────────────────────────────────────────────────────────────────────
# Exportação (acesso + portabilidade — art. 18, II e V)
# ─────────────────────────────────────────────────────────────────────────────

_TUTOR_PROFILE_FIELDS = (
    "full_name", "cpf", "phone", "photo_url", "cep", "street", "number", "complement",
    "neighborhood", "city", "state", "reference_point", "access_instructions",
    "pickup_notes", "preferred_method", "created_at",
)
_WALKER_PROFILE_FIELDS = (
    "full_name", "cpf", "rg", "phone", "birth_date", "city", "state", "experience", "bio",
    "profile_photo_url", "pet_photo_url", "status", "active_as_walker", "has_vehicle",
    "max_dog_size", "pix_key", "background_check_status", "background_verified_at",
    "sanctions_check_status", "sanctions_checked_at", "approved_at", "rejected_at",
    "rejection_reason", "suspension_reason", "created_at", "updated_at",
    "training_completed_version", "training_completed_at",
)
_PET_FIELDS = (
    "id", "name", "photo_url", "species", "sex", "breed", "size", "weight", "age",
    "behavior_notes", "is_social", "afraid_of_noise", "pulls_leash",
    "can_walk_with_other_pets", "is_neutered", "allergies", "medications", "restrictions",
    "health_notes", "birth_date", "chip_number", "vet_name", "vet_phone",
    "emergency_contact", "microchip", "diet_type", "diet_brand", "diet_line",
    "diet_grams_per_meal", "diet_meals_per_day", "diet_meal_times", "diet_notes",
    "supplements_json", "food_bag_weight_kg", "food_bag_opened_at", "vet_clinic",
    "insurance_provider", "insurance_policy", "behavior_with_dogs",
    "behavior_with_children", "behavior_with_cats", "fear_triggers_json", "created_at",
)
_WALK_TUTOR_FIELDS = (
    "id", "pet_id", "scheduled_date", "duration_minutes", "price", "status",
    "operational_status", "modality", "pickup_method", "address_snapshot", "destination",
    "meeting_point", "notes", "did_pee", "did_poop", "cancellation_reason_type",
    "cancellation_reason", "cancelled_at", "cancelled_by_role", "created_at",
)
# Visão do PASSEADOR: sem endereço/observações/pet do tutor (dados de terceiro).
_WALK_WALKER_FIELDS = (
    "id", "scheduled_date", "duration_minutes", "status", "operational_status",
    "modality", "did_pee", "did_poop", "cancelled_at", "cancelled_by_role", "created_at",
)


def build_data_export(db: Session, user: User) -> dict:
    """Monta o JSON de exportação do titular. Allowlist explícita por entidade:
    NUNCA inclui password_hash, token_version, apple_sub, blind index, tokens de push,
    tokens de links de compartilhamento, código de coleta, ids de gateway (Asaas),
    carteira Asaas, notas internas de admin nem ids de admins."""
    uid = user.id

    tutor_profile = db.query(TutorProfile).filter(TutorProfile.user_id == uid).first()
    walker_profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == uid).first()

    pets = db.query(Pet).filter(Pet.tutor_id == uid).order_by(Pet.created_at).all()
    pet_ids = [p.id for p in pets]

    walks_as_tutor = db.query(Walk).filter(Walk.tutor_id == uid).order_by(Walk.created_at).all()
    walks_as_walker = (
        db.query(Walk)
        .filter((Walk.walker_id == uid) | (Walk.assigned_walker_id == uid))
        .filter(Walk.tutor_id != uid)
        .order_by(Walk.created_at)
        .all()
    )

    payments = (
        db.query(Payment)
        .filter(Payment.tutor_id == uid)
        .order_by(Payment.id)
        .all()
    )
    subscriptions = db.query(TutorSubscription).filter(TutorSubscription.tutor_id == uid).all()
    tips_given = db.query(WalkTip).filter(WalkTip.tutor_id == uid).all()
    tips_received = db.query(WalkTip).filter(WalkTip.walker_id == uid).all()
    earnings = db.query(WalkerEarning).filter(WalkerEarning.walker_id == uid).all()

    tenant_ids: list[str | None] = [user.tenant_id, getattr(tutor_profile, "tenant_id", None)]
    tenant_ids += [w.tenant_id for w in walks_as_tutor + walks_as_walker]
    tenant_ids += [p.tenant_id for p in payments] + [s.tenant_id for s in subscriptions]
    tenant_ids += [e.tenant_id for e in earnings]
    acceptances = (
        db.query(LegalAcceptance)
        .filter(LegalAcceptance.user_id == uid)
        .order_by(LegalAcceptance.accepted_at)
        .all()
    )
    tenant_ids += [a.tenant_id for a in acceptances]
    tenant_names = _tenant_names(db, tenant_ids)

    def _tn(tid: str | None) -> str | None:
        return tenant_names.get(tid) if tid else None

    # ── Pets + dados do Perfil Vivo (todos do próprio titular) ───────────────
    health = self_walks = reminders = timeline = []
    if pet_ids:
        health = db.query(PetHealthRecord).filter(PetHealthRecord.pet_id.in_(pet_ids)).all()
        reminders = db.query(PetReminder).filter(PetReminder.pet_id.in_(pet_ids)).all()
        self_walks = db.query(PetSelfWalk).filter(
            PetSelfWalk.pet_id.in_(pet_ids), PetSelfWalk.tutor_id == uid
        ).all()
        timeline = db.query(PetTimelineEvent).filter(PetTimelineEvent.pet_id.in_(pet_ids)).all()

    pets_out = []
    pet_names = {}
    for pet in pets:
        pet_names[pet.id] = pet.name
        item = _pick(pet, _PET_FIELDS)
        item["health_records"] = [
            _pick(r, ("kind", "name", "applied_at", "valid_until", "notes", "created_by_role", "created_at"))
            for r in health if r.pet_id == pet.id
        ]
        item["reminders"] = [
            _pick(r, ("kind", "due_date", "active", "created_at")) for r in reminders if r.pet_id == pet.id
        ]
        item["self_walks"] = [
            _pick(r, (
                "started_at", "duration_seconds", "distance_km", "walk_type", "intensity",
                "had_gps", "need_pee", "need_poop", "need_water", "interacted_dogs",
                "interacted_people", "pulled_leash", "showed_fear", "showed_reactivity",
                "notes", "created_at",
            ))
            for r in self_walks if r.pet_id == pet.id
        ]
        item["timeline_events"] = [
            _pick(r, ("event_type", "title", "notes", "occurred_at", "source", "created_at"))
            for r in timeline if r.pet_id == pet.id
        ]
        pets_out.append(item)

    # ── Passeios ─────────────────────────────────────────────────────────────
    walker_ids = {w.walker_id or w.assigned_walker_id for w in walks_as_tutor} - {None}
    walker_first_names: dict[str, str | None] = {}
    if walker_ids:
        for wp in db.query(WalkerProfile).filter(WalkerProfile.user_id.in_(walker_ids)).all():
            walker_first_names[wp.user_id] = _first_name(wp.full_name)
        for u in db.query(User).filter(User.id.in_(walker_ids)).all():
            walker_first_names.setdefault(u.id, None)
            if not walker_first_names[u.id] and not is_account_anonymized(u):
                walker_first_names[u.id] = _first_name(u.full_name)

    walks_tutor_out = []
    for w in walks_as_tutor:
        item = _pick(w, _WALK_TUTOR_FIELDS)
        item["pet_name"] = pet_names.get(w.pet_id)
        item["establishment"] = _tn(w.tenant_id)
        wid = w.walker_id or w.assigned_walker_id
        # Mínimo necessário do terceiro: só o primeiro nome do passeador.
        item["walker_first_name"] = walker_first_names.get(wid) if wid else None
        walks_tutor_out.append(item)

    walks_walker_out = []
    for w in walks_as_walker:
        item = _pick(w, _WALK_WALKER_FIELDS)
        item["establishment"] = _tn(w.tenant_id)
        walks_walker_out.append(item)

    # ── Financeiro ───────────────────────────────────────────────────────────
    payments_out, withdrawals_out = [], []
    for p in payments:
        is_withdrawal = p.provider == "pix" and p.walk_id is None and float(p.amount or 0) < 0
        base = _pick(p, ("amount", "status", "provider", "refund_status", "refunded_amount"))
        base["establishment"] = _tn(p.tenant_id)
        if is_withdrawal:
            base["amount"] = abs(float(p.amount or 0))
            withdrawals_out.append(base)
        else:
            base["walk_id"] = p.walk_id
            payments_out.append(base)

    plan_names = {}
    plan_ids = {s.plan_id for s in subscriptions if s.plan_id}
    if plan_ids:
        plan_names = {pl.id: pl.name for pl in db.query(RecurringPlan).filter(RecurringPlan.id.in_(plan_ids)).all()}
    subscriptions_out = []
    for s in subscriptions:
        item = _pick(s, (
            "status", "price", "walks_per_cycle", "credits_remaining", "current_period_start",
            "current_period_end", "cancelled_at", "cancel_reason", "created_at",
        ))
        item["plan_name"] = plan_names.get(s.plan_id)
        item["establishment"] = _tn(s.tenant_id)
        subscriptions_out.append(item)

    tip_fields = ("walk_id", "amount", "status", "created_at", "paid_at")
    earnings_out = []
    for e in earnings:
        item = _pick(e, ("walk_id", "gross", "amount", "status", "accrued_at", "payable_at", "void_reason", "voided_at"))
        item["establishment"] = _tn(e.tenant_id)
        earnings_out.append(item)

    # ── Avaliações ───────────────────────────────────────────────────────────
    review_fields = ("walk_id", "rating", "comment", "created_at")
    reviews_given = (
        [dict(_pick(r, review_fields + ("tags_json",)), kind="walk_review")
         for r in db.query(WalkReview).filter(WalkReview.tutor_id == uid).all()]
        + [dict(_pick(r, review_fields + ("punctuality_rating", "care_rating", "communication_rating")), kind="walker_review")
           for r in db.query(WalkerReview).filter(WalkerReview.tutor_id == uid).all()]
    )
    # Recebidas (passeador): nota/comentário sem identificar o autor.
    reviews_received = (
        [dict(_pick(r, review_fields), kind="walk_review")
         for r in db.query(WalkReview).filter(WalkReview.walker_id == uid, WalkReview.tutor_id != uid).all()]
        + [dict(_pick(r, review_fields + ("punctuality_rating", "care_rating", "communication_rating")), kind="walker_review")
           for r in db.query(WalkerReview).filter(WalkerReview.walker_id == uid, WalkerReview.tutor_id != uid).all()]
    )

    # ── Mensagens próprias (só as ENVIADAS pelo titular) ─────────────────────
    messages_out = [
        _pick(m, ("walk_id", "sender_role", "body", "created_at"))
        for m in db.query(ProtectedChatMessage)
        .filter(ProtectedChatMessage.sender_user_id == uid)
        .order_by(ProtectedChatMessage.created_at)
        .all()
    ]

    # ── Consentimentos ───────────────────────────────────────────────────────
    consents = {
        "legal_acceptances": [
            dict(
                _pick(a, (
                    "user_role", "terms_version", "privacy_version", "cancellation_version",
                    "lgpd_version", "geolocation_version", "accepted_at",
                )),
                scope="establishment" if a.tenant_id else "platform",
                establishment=_tn(a.tenant_id),
            )
            for a in acceptances
        ],
        "background_check_consent": (
            _pick(walker_profile, ("background_consent_at", "background_consent_version"))
            if walker_profile else None
        ),
    }

    tickets_out = [
        _pick(t, ("subject", "description", "requester_name", "requester_email", "status", "reply", "replied_at", "created_at"))
        for t in db.query(SupportTicket).filter(SupportTicket.user_id == uid).all()
    ]
    devices_out = [
        _pick(t, ("platform", "updated_at"))
        for t in db.query(PushToken).filter(PushToken.user_id == uid).all()
    ]

    walker_out = None
    if walker_profile:
        walker_out = _pick(walker_profile, _WALKER_PROFILE_FIELDS)
        # Documentos de identidade: só a indicação de que estão em arquivo (o arquivo
        # em si é sensível e servido apenas por URL assinada nas telas próprias).
        walker_out["documents_on_file"] = {
            "identity_document_front": bool(walker_profile.document_url),
            "identity_document_back": bool(walker_profile.identity_document_back_url),
            "selfie": bool(walker_profile.selfie_url),
            "proof_of_address": bool(walker_profile.proof_of_address_url),
        }
        walker_out["background_certificates"] = [
            _pick(c, ("cert_type", "issuer_uf", "status", "validated_at", "expires_at", "created_at"))
            for c in db.query(WalkerBackgroundCertificate)
            .filter(WalkerBackgroundCertificate.walker_profile_id == walker_profile.id)
            .all()
        ]

    tutor_out = None
    if tutor_profile:
        tutor_out = _pick(tutor_profile, _TUTOR_PROFILE_FIELDS)
        tutor_out["establishment"] = _tn(tutor_profile.tenant_id)

    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "generated_at": _jsonable(datetime.now(timezone.utc)),
        "controller": {"name": CONTROLLER_NAME, "dpo_contact": DPO_CONTACT},
        "notice": (
            "Dados pessoais do titular tratados pela Plataforma (LGPD, art. 18, II e V). "
            "Dados de terceiros foram omitidos ou reduzidos ao mínimo necessário."
        ),
        "account": {
            "id": uid,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "is_active": user.is_active,
            "apple_linked": user.apple_sub is not None,
            "home_establishment": _tn(user.tenant_id),
            "created_at": _jsonable(user.created_at),
        },
        "tutor_profile": tutor_out,
        "walker_profile": walker_out,
        "pets": pets_out,
        "walks_as_tutor": walks_tutor_out,
        "walks_as_walker": walks_walker_out,
        "payments": payments_out,
        "withdrawals": withdrawals_out,
        "walker_earnings": earnings_out,
        "tips_given": [_pick(t, tip_fields) for t in tips_given],
        "tips_received": [_pick(t, tip_fields) for t in tips_received],
        "subscriptions": subscriptions_out,
        "reviews_given": reviews_given,
        "reviews_received": reviews_received,
        "chat_messages_sent": messages_out,
        "consents": consents,
        "support_tickets": tickets_out,
        "devices": devices_out,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Exclusão / anonimização (art. 18, IV e VI; guarda do art. 16)
# ─────────────────────────────────────────────────────────────────────────────

def deletion_blockers(db: Session, user: User) -> list[dict]:
    """Motivos que impedem a exclusão AGORA. Lista vazia = pode excluir."""
    uid = user.id
    reasons: list[dict] = []

    if (user.role or "") in ADMIN_ROLES:
        reasons.append({
            "code": "conta_administrativa",
            "message": (
                "Contas administrativas não podem ser excluídas pelo app. "
                f"Solicite o encerramento ao Encarregado de Dados ({DPO_CONTACT})."
            ),
        })

    live_walks = (
        db.query(Walk.id)
        .filter((Walk.tutor_id == uid) | (Walk.walker_id == uid) | (Walk.assigned_walker_id == uid))
        .filter(Walk.operational_status.in_(BLOCKING_WALK_OPERATIONAL_STATUSES))
        .filter(~Walk.status.in_(LEGACY_TERMINAL_WALK_STATUSES))
        .count()
    )
    if live_walks:
        reasons.append({
            "code": "passeio_ativo",
            "message": "Há passeio em andamento, agendado ou aguardando pagamento/revisão. Conclua ou cancele antes de excluir a conta.",
            "count": live_walks,
        })

    live_subs = (
        db.query(TutorSubscription.id)
        .filter(TutorSubscription.tutor_id == uid, TutorSubscription.status.in_(BLOCKING_SUBSCRIPTION_STATUSES))
        .count()
    )
    if live_subs:
        reasons.append({
            "code": "assinatura_ativa",
            "message": "Há assinatura de plano ativa ou em atraso. Cancele a assinatura antes de excluir a conta.",
            "count": live_subs,
        })

    pending_withdrawals = (
        db.query(Payment.id)
        .filter(
            Payment.tutor_id == uid,  # no saque, tutor_id guarda o passeador
            Payment.provider == "pix",
            Payment.walk_id.is_(None),
            Payment.amount < 0,
            Payment.status.in_(PENDING_WITHDRAWAL_STATUSES),
        )
        .count()
    )
    if pending_withdrawals:
        reasons.append({
            "code": "saque_pendente",
            "message": "Há saque pendente de processamento. Aguarde a conclusão antes de excluir a conta.",
            "count": pending_withdrawals,
        })

    is_walker = (user.role or "") in WALKER_ROLES or (
        db.query(WalkerProfile.id).filter(WalkerProfile.user_id == uid).first() is not None
    )
    if is_walker:
        # Ganhos da rede ainda a liberar (payable_at futuro).
        now = datetime.now(timezone.utc)
        to_release = 0.0
        for e in db.query(WalkerEarning).filter(WalkerEarning.walker_id == uid, WalkerEarning.status != WE_VOID).all():
            pa = e.payable_at
            if pa is not None and pa.tzinfo is None:
                pa = pa.replace(tzinfo=timezone.utc)
            if pa is None or pa > now:
                to_release += float(e.amount or 0)
        if to_release > 0.009:
            reasons.append({
                "code": "saldo_a_liberar",
                "message": "Há ganhos ainda a liberar na sua carteira. Aguarde a liberação e o saque antes de excluir a conta.",
            })
        # Saldo disponível: reusa o cálculo canônico da carteira do passeador.
        try:
            from app.routes.walker import _available_balance

            balance = float(_available_balance(user, db) or 0)
        except Exception:  # pragma: no cover - falha-fechada: sem cálculo, não exclui
            logger.exception("lgpd_deletion_balance_check_failed")
            balance = None
        if balance is None:
            reasons.append({
                "code": "saldo_indisponivel",
                "message": "Não foi possível verificar o saldo da carteira agora. Tente novamente mais tarde.",
            })
        elif balance > 0.009:
            reasons.append({
                "code": "saldo_disponivel",
                "message": "Há saldo disponível na sua carteira. Solicite o saque antes de excluir a conta.",
            })

    return reasons


def _upload_path_from_url(url: str | None) -> str | None:
    if not url:
        return None
    marker = "/uploads/"
    raw = url.split("?", 1)[0]
    if marker in raw:
        return raw.split(marker, 1)[1]
    if raw.startswith("uploads/"):
        return raw[len("uploads/"):]
    return None


def anonymize_account(db: Session, user: User) -> dict:
    """Anonimiza o titular e seus dados pessoais. NÃO commita.

    Retorna {"counts": {...}, "upload_paths": [...]} — counts sem PII (para auditoria);
    upload_paths para purga best-effort dos arquivos DEPOIS do commit.

    PRESERVADO (anonimizado pela desvinculação da identidade), por obrigação legal /
    exercício regular de direitos / prevenção a fraudes (art. 16):
      payments, walker_earnings, walk_tips, tutor_subscriptions, credit ledger,
      commission/fiscal/nfse, walks (sem endereço/observações), avaliações (nota, sem
      comentário), aceites legais (prova do consentimento), reclamações/ocorrências e
      trilha de auditoria.
    """
    uid = user.id
    counts: dict[str, int] = {}
    upload_paths: set[str] = set()
    original_email = (user.email or "").strip().lower()

    # ── Conta ────────────────────────────────────────────────────────────────
    user.email = f"excluido-{uuid4().hex}@{ANONYMIZED_EMAIL_DOMAIN}"
    user.full_name = ANONYMIZED_NAME
    user.password_hash = ""
    user.apple_sub = None
    user.must_change_password = False
    user.is_active = False
    # Revoga TODAS as sessões (access + refresh) — mesmo mecanismo do logout.
    user.token_version = (user.token_version or 0) + 1

    # ── Perfil de tutor ──────────────────────────────────────────────────────
    tp = db.query(TutorProfile).filter(TutorProfile.user_id == uid).first()
    if tp:
        upload_paths.add(_upload_path_from_url(tp.photo_url) or "")
        for f in ("full_name", "cpf", "phone", "cep", "street", "number", "complement",
                  "neighborhood", "city", "state", "reference_point", "access_instructions",
                  "pickup_notes"):
            setattr(tp, f, "")
        tp.cpf_bidx = None
        tp.photo_url = None
        counts["tutor_profile"] = 1

    # ── Perfil de passeador ──────────────────────────────────────────────────
    wp = db.query(WalkerProfile).filter(WalkerProfile.user_id == uid).first()
    if wp:
        for f in ("profile_photo_url", "document_url", "identity_document_back_url", "selfie_url",
                  "pet_photo_url", "proof_of_address_url"):
            upload_paths.add(_upload_path_from_url(getattr(wp, f)) or "")
            setattr(wp, f, None)
        for f in ("full_name", "cpf", "rg", "phone", "birth_date", "city", "state", "experience",
                  "bio", "internal_notes", "resubmission_requested_documents"):
            setattr(wp, f, "")
        wp.cpf_bidx = None
        wp.pix_key = None
        wp.asaas_wallet_id = None
        wp.rejection_reason = None
        wp.suspension_reason = None
        wp.status = "inactive"
        wp.active_as_walker = False
        wp.is_online = False
        wp.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        counts["walker_profile"] = 1
        certs = db.query(WalkerBackgroundCertificate).filter(
            WalkerBackgroundCertificate.walker_profile_id == wp.id
        ).all()
        for c in certs:
            upload_paths.add(_upload_path_from_url(c.document_url) or "")
            c.document_url = None
            c.cert_number = None
            c.notes = ""
        counts["background_certificates"] = len(certs)
        net = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == uid).first()
        if net:
            net.network_enabled = False
            net.network_status = "inactive"

    # ── Pets (linha preservada: passeios referenciam pet_id) ─────────────────
    pets = db.query(Pet).filter(Pet.tutor_id == uid).all()
    pet_ids = [p.id for p in pets]
    for pet in pets:
        upload_paths.add(_upload_path_from_url(pet.photo_url) or "")
        pet.name = ANONYMIZED_PET_NAME
        pet.photo_url = None
        for f in ("behavior_notes", "allergies", "medications", "restrictions", "health_notes"):
            setattr(pet, f, "")
        for f in ("birth_date", "chip_number", "vet_name", "vet_phone", "emergency_contact",
                  "microchip", "diet_type", "diet_brand", "diet_line", "diet_grams_per_meal",
                  "diet_meals_per_day", "diet_meal_times", "diet_notes", "supplements_json",
                  "food_bag_weight_kg", "food_bag_opened_at", "vet_clinic", "insurance_provider",
                  "insurance_policy", "behavior_with_dogs", "behavior_with_children",
                  "behavior_with_cats", "fear_triggers_json"):
            setattr(pet, f, None)
    counts["pets"] = len(pets)
    if pet_ids:
        counts["pet_health_records"] = db.query(PetHealthRecord).filter(
            PetHealthRecord.pet_id.in_(pet_ids)).delete(synchronize_session=False)
        counts["pet_reminders"] = db.query(PetReminder).filter(
            PetReminder.pet_id.in_(pet_ids)).delete(synchronize_session=False)
        counts["pet_timeline_events"] = db.query(PetTimelineEvent).filter(
            PetTimelineEvent.pet_id.in_(pet_ids)).delete(synchronize_session=False)
        counts["pet_share_links"] = db.query(PetShareLink).filter(
            PetShareLink.pet_id.in_(pet_ids)).delete(synchronize_session=False)
    counts["pet_self_walks"] = db.query(PetSelfWalk).filter(
        PetSelfWalk.tutor_id == uid).delete(synchronize_session=False)

    # ── Passeios como tutor: preserva registro, remove endereço/observações ──
    tutor_walks = db.query(Walk).filter(Walk.tutor_id == uid).all()
    tutor_walk_ids = [w.id for w in tutor_walks]
    for w in tutor_walks:
        w.address_snapshot = ""
        w.notes = ""
        w.destination = ""
        w.destination_lat = None
        w.destination_lng = None
        w.meeting_point = None
        w.meeting_lat = None
        w.meeting_lng = None
        w.cancellation_reason = None
        w.security_code = None
    counts["walks_anonymized"] = len(tutor_walks)

    # Trajeto GPS (política: expurgo em até 7 dias) — do titular passeador e dos
    # passeios do titular tutor (revela o endereço de busca).
    pings = db.query(WalkLocationPing).filter(WalkLocationPing.walker_id == uid)
    counts["walk_location_pings"] = pings.delete(synchronize_session=False)
    if tutor_walk_ids:
        counts["walk_location_pings"] += db.query(WalkLocationPing).filter(
            WalkLocationPing.walk_id.in_(tutor_walk_ids)).delete(synchronize_session=False)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    counts["walk_share_links_revoked"] = db.query(WalkShareLink).filter(
        WalkShareLink.created_by == uid, WalkShareLink.revoked_at.is_(None)
    ).update({WalkShareLink.revoked_at: now}, synchronize_session=False)

    # ── Conteúdo textual do titular ──────────────────────────────────────────
    counts["chat_messages"] = db.query(ProtectedChatMessage).filter(
        ProtectedChatMessage.sender_user_id == uid
    ).update({ProtectedChatMessage.body: REMOVED_TEXT}, synchronize_session=False)
    counts["walk_review_comments"] = db.query(WalkReview).filter(
        WalkReview.tutor_id == uid, WalkReview.comment.isnot(None)
    ).update({WalkReview.comment: None}, synchronize_session=False)
    counts["walker_review_comments"] = db.query(WalkerReview).filter(
        WalkerReview.tutor_id == uid, WalkerReview.comment.isnot(None)
    ).update({WalkerReview.comment: None}, synchronize_session=False)
    counts["support_tickets"] = db.query(SupportTicket).filter(SupportTicket.user_id == uid).update(
        {
            SupportTicket.subject: REMOVED_TEXT,
            SupportTicket.description: REMOVED_TEXT,
            SupportTicket.requester_name: None,
            SupportTicket.requester_email: None,
            SupportTicket.reply: None,
        },
        synchronize_session=False,
    )
    if original_email:
        counts["contact_messages"] = db.query(ContactMessage).filter(
            func.lower(ContactMessage.email) == original_email
        ).update(
            {
                ContactMessage.name: "",
                ContactMessage.company: "",
                ContactMessage.email: f"anonimizado@{ANONYMIZED_EMAIL_DOMAIN}",
                ContactMessage.phone: "",
                ContactMessage.city: "",
                ContactMessage.message: "",
            },
            synchronize_session=False,
        )

    # ── Canais e credenciais ─────────────────────────────────────────────────
    counts["push_tokens"] = db.query(PushToken).filter(PushToken.user_id == uid).delete(synchronize_session=False)
    counts["notifications"] = db.query(Notification).filter(Notification.user_id == uid).delete(synchronize_session=False)
    counts["password_reset_codes"] = db.query(PasswordResetCode).filter(
        PasswordResetCode.user_id == uid).delete(synchronize_session=False)

    # ── Registro de uploads do titular (arquivo físico purgado após commit) ──
    uploads = db.query(UploadFile).filter(
        UploadFile.owner_id == uid, UploadFile.context.in_(PURGEABLE_UPLOAD_CONTEXTS)
    ).all()
    for up in uploads:
        upload_paths.add(_upload_path_from_url(up.storage_path) or (up.storage_path or ""))
        db.delete(up)
    counts["upload_files"] = len(uploads)

    return {"counts": counts, "upload_paths": sorted(p for p in upload_paths if p)}


def purge_upload_files(paths: Iterable[str]) -> int:
    """Remove os arquivos físicos (R2 quando configurado; senão disco local). Best-effort:
    nunca levanta — a anonimização no banco já foi commitada. Retorna quantos removeu."""
    from app.services import object_storage
    from app.services.signed_uploads import normalize_upload_path, upload_file_path

    removed = 0
    for raw in paths:
        key = normalize_upload_path(raw or "")
        if not key:
            continue
        try:
            if object_storage.r2_enabled():
                object_storage._get_client().delete_object(Bucket=object_storage._bucket(), Key=key)
                removed += 1
            else:
                fp = upload_file_path(key)
                if fp and fp.is_file():
                    fp.unlink()
                    removed += 1
        except Exception as exc:
            logger.warning("lgpd_upload_purge_failed reason=%s", type(exc).__name__)
    return removed

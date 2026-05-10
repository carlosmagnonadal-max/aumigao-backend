from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import SessionLocal
from app.models.complaint import Complaint, ComplaintDecision, ComplaintEvidence, ComplaintStatusHistory, RiskScore
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.tip_integrity_flag import TipIntegrityFlag
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt, WalkOperationalLog
from app.models.walker_boost import WalkerBoost
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.models.walker_profile import WalkerProfile
from app.models.walker_recovery_plan import WalkerRecoveryPlan
from app.models.walker_referral import WalkerReferral
from app.models.walker_reputation_snapshot import WalkerReputationSnapshot
from app.models.walker_review import WalkerReview
from app.models.walker_weekly_mission import WalkerWeeklyMission


TEST_TOKENS = (
    "teste",
    "test",
    "demo",
    "mock",
    "auditoria",
    "fluxo real",
    "login",
    "docs",
    "seed",
    "sample",
    "fallback",
    "local",
    "ficticio",
    "fictício",
    "fake",
    "pet-demo",
    "walk-demo",
    "request-demo",
)
ADMIN_ROLES = {"admin", "super_admin"}
TUTOR_ROLES = {"tutor", "cliente", "client", "customer"}
WALKER_ROLES = {"walker", "passeador"}


def _read_env_file() -> dict[str, str]:
    env_path = ROOT / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _protected_emails() -> set[str]:
    env_values = _read_env_file()
    keys = ("ADMIN_EMAIL", "SUPER_ADMIN_EMAIL", "ROOT_ADMIN_EMAIL", "SUPPORT_ADMIN_EMAIL")
    emails = {env_values.get(key, "") for key in keys}
    emails.update(os.getenv(key, "") for key in keys)
    extra = env_values.get("PRESERVE_USER_EMAILS", "") or os.getenv("PRESERVE_USER_EMAILS", "")
    emails.update(item.strip() for item in extra.split(","))
    return {email.strip().lower() for email in emails if email.strip()}


def _text(*values: object) -> str:
    return " ".join(str(value or "").strip().lower() for value in values)


def _has_test_token(*values: object) -> bool:
    haystack = _text(*values)
    return any(token in haystack for token in TEST_TOKENS)


def _ids(rows: Iterable[object]) -> set[str]:
    return {str(getattr(row, "id", "") or "") for row in rows if getattr(row, "id", None)}


def _sample(values: Iterable[str], limit: int = 8) -> str:
    items = sorted(str(value) for value in values if value)
    if not items:
        return "-"
    suffix = "" if len(items) <= limit else f" ... +{len(items) - limit}"
    return ", ".join(items[:limit]) + suffix


def _delete(db, model, condition, dry_run: bool) -> int:
    query = db.query(model).filter(condition)
    count = query.count()
    if not dry_run and count:
        query.delete(synchronize_session=False)
    return count


def build_plan(db) -> dict[str, object]:
    protected_emails = _protected_emails()
    all_users = db.query(User).all()
    protected_user_ids = {
        user.id
        for user in all_users
        if (user.role or "").lower() in ADMIN_ROLES or (user.email or "").lower() in protected_emails
    }

    test_user_ids: set[str] = set()
    for user in all_users:
        role = (user.role or "").lower()
        if user.id in protected_user_ids or role in ADMIN_ROLES:
            continue
        if role in TUTOR_ROLES | WALKER_ROLES and _has_test_token(user.id, user.email, user.full_name, role):
            test_user_ids.add(user.id)

    test_walker_profiles = []
    for profile in db.query(WalkerProfile).all():
        user = db.get(User, profile.user_id) if profile.user_id else None
        if profile.user_id in protected_user_ids:
            continue
        if profile.user_id in test_user_ids or _has_test_token(
            profile.id,
            profile.user_id,
            profile.full_name,
            profile.cpf,
            profile.phone,
            profile.profile_photo_url,
            profile.document_url,
            profile.identity_document_back_url,
            profile.selfie_url,
            profile.proof_of_address_url,
            user.email if user else "",
            user.full_name if user else "",
        ):
            test_walker_profiles.append(profile)
            if profile.user_id:
                test_user_ids.add(profile.user_id)

    test_tutor_profiles = []
    for profile in db.query(TutorProfile).all():
        if profile.user_id in protected_user_ids:
            continue
        if profile.user_id in test_user_ids or _has_test_token(
            profile.id,
            profile.user_id,
            profile.full_name,
            profile.cpf,
            profile.phone,
            profile.photo_url,
            profile.street,
            profile.neighborhood,
            profile.city,
            profile.access_instructions,
            profile.pickup_notes,
        ):
            test_tutor_profiles.append(profile)
            test_user_ids.add(profile.user_id)

    test_pets = []
    for pet in db.query(Pet).all():
        if pet.tutor_id in test_user_ids or _has_test_token(pet.id, pet.tutor_id, pet.name, pet.photo_url, pet.behavior_notes, pet.health_notes):
            test_pets.append(pet)
            if pet.tutor_id not in protected_user_ids:
                test_user_ids.add(pet.tutor_id)

    test_pet_ids = _ids(test_pets)
    test_walks = []
    for walk in db.query(Walk).all():
        if (
            walk.tutor_id in test_user_ids
            or walk.walker_id in test_user_ids
            or walk.assigned_walker_id in test_user_ids
            or walk.pet_id in test_pet_ids
            or _has_test_token(walk.id, walk.tutor_id, walk.walker_id, walk.assigned_walker_id, walk.pet_id, walk.address_snapshot, walk.notes)
        ):
            test_walks.append(walk)

    test_walk_ids = _ids(test_walks)
    test_payment_ids = {
        payment.id
        for payment in db.query(Payment).all()
        if payment.tutor_id in test_user_ids or payment.walk_id in test_walk_ids or _has_test_token(payment.id, payment.tutor_id, payment.walk_id, payment.provider, payment.provider_payment_id)
    }
    test_review_ids = {
        review.id
        for review in db.query(WalkerReview).all()
        if review.walk_id in test_walk_ids or review.tutor_id in test_user_ids or review.walker_id in test_user_ids or _has_test_token(review.id, review.comment, review.admin_notes)
    }
    test_tip_ids = {
        tip.id
        for tip in db.query(TipIntegrityFlag).all()
        if tip.walk_id in test_walk_ids or tip.tutor_id in test_user_ids or tip.walker_id in test_user_ids or _has_test_token(tip.id, tip.notes, tip.flag_type)
    }
    test_attempt_ids = {
        attempt.id
        for attempt in db.query(WalkMatchingAttempt).all()
        if attempt.walk_id in test_walk_ids or attempt.walker_id in test_user_ids or _has_test_token(attempt.id, attempt.walk_id, attempt.walker_id, attempt.reason)
    }
    test_log_ids = {
        log.id
        for log in db.query(WalkOperationalLog).all()
        if log.walk_id in test_walk_ids or log.actor_id in test_user_ids or _has_test_token(log.id, log.walk_id, log.actor_id, log.metadata_json)
    }

    test_complaint_ids = {
        complaint.id
        for complaint in db.query(Complaint).all()
        if (
            complaint.author_id in test_user_ids
            or complaint.target_user_id in test_user_ids
            or complaint.target_pet_id in test_pet_ids
            or complaint.walk_id in test_walk_ids
            or _has_test_token(complaint.id, complaint.title, complaint.description, complaint.metadata_json)
        )
    }
    test_risk_ids = {
        risk.id
        for risk in db.query(RiskScore).all()
        if risk.subject_id in test_user_ids or risk.subject_id in test_pet_ids or risk.subject_id in test_walk_ids or _has_test_token(risk.id, risk.subject_id, risk.subject_type)
    }
    test_referral_ids = {
        referral.id
        for referral in db.query(WalkerReferral).all()
        if (
            referral.referrer_user_id in test_user_ids
            or referral.referred_user_id in test_user_ids
            or _has_test_token(referral.id, referral.referred_name, referral.referred_phone, referral.referral_code, referral.invite_link, referral.notes)
        )
    }

    document_count = sum(
        1
        for profile in test_walker_profiles
        for value in (profile.document_url, profile.identity_document_back_url, profile.selfie_url, profile.proof_of_address_url)
        if value
    )
    document_count += sum(1 for profile in test_tutor_profiles if profile.photo_url)

    walker_user_ids = {
        user_id
        for user_id in test_user_ids
        if (db.get(User, user_id) and (db.get(User, user_id).role or "").lower() in WALKER_ROLES)
    }

    return {
        "protected_emails": protected_emails,
        "protected_user_ids": protected_user_ids,
        "test_user_ids": test_user_ids,
        "test_tutor_ids": {user.id for user in all_users if user.id in test_user_ids and (user.role or "").lower() in TUTOR_ROLES},
        "test_walker_user_ids": walker_user_ids,
        "test_walker_profile_ids": _ids(test_walker_profiles),
        "test_tutor_profile_ids": _ids(test_tutor_profiles),
        "test_pet_ids": test_pet_ids,
        "test_walk_ids": test_walk_ids,
        "test_payment_ids": test_payment_ids,
        "test_review_ids": test_review_ids,
        "test_tip_ids": test_tip_ids,
        "test_attempt_ids": test_attempt_ids,
        "test_log_ids": test_log_ids,
        "test_complaint_ids": test_complaint_ids,
        "test_risk_ids": test_risk_ids,
        "test_referral_ids": test_referral_ids,
        "document_count": document_count,
    }


def print_plan(plan: dict[str, object], dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "CONFIRM"
    print(f"Modo: {mode}")
    print("Preservados:")
    print(f"  emails admin/env: {_sample(plan['protected_emails'])}")
    print(f"  usuarios admin/superadmin: {len(plan['protected_user_ids'])}")
    print("Candidatos a limpeza:")
    for label, key in (
        ("clientes/tutores", "test_tutor_ids"),
        ("passeadores usuarios", "test_walker_user_ids"),
        ("walker_profiles", "test_walker_profile_ids"),
        ("tutor_profiles", "test_tutor_profile_ids"),
        ("pets", "test_pet_ids"),
        ("walks", "test_walk_ids"),
        ("payments", "test_payment_ids"),
        ("reviews", "test_review_ids"),
        ("tips", "test_tip_ids"),
        ("matching_attempts", "test_attempt_ids"),
        ("operational_logs", "test_log_ids"),
        ("complaints", "test_complaint_ids"),
        ("risk_scores", "test_risk_ids"),
        ("walker_referrals", "test_referral_ids"),
    ):
        values = plan[key]
        print(f"  {label}: {len(values)} ({_sample(values)})")
    print(f"  documentos vinculados em perfis: {plan['document_count']}")


def execute_cleanup(db, plan: dict[str, object], dry_run: bool) -> dict[str, int]:
    user_ids = plan["test_user_ids"]
    walker_user_ids = plan["test_walker_user_ids"]
    walker_profile_ids = plan["test_walker_profile_ids"]
    tutor_profile_ids = plan["test_tutor_profile_ids"]
    pet_ids = plan["test_pet_ids"]
    walk_ids = plan["test_walk_ids"]
    payment_ids = plan["test_payment_ids"]
    review_ids = plan["test_review_ids"]
    tip_ids = plan["test_tip_ids"]
    attempt_ids = plan["test_attempt_ids"]
    log_ids = plan["test_log_ids"]
    complaint_ids = plan["test_complaint_ids"]
    risk_ids = plan["test_risk_ids"]
    referral_ids = plan["test_referral_ids"]

    counts: dict[str, int] = {}
    counts["complaint_evidences"] = _delete(db, ComplaintEvidence, ComplaintEvidence.complaint_id.in_(complaint_ids), dry_run) if complaint_ids else 0
    counts["complaint_decisions"] = _delete(db, ComplaintDecision, ComplaintDecision.complaint_id.in_(complaint_ids), dry_run) if complaint_ids else 0
    counts["complaint_status_history"] = _delete(db, ComplaintStatusHistory, ComplaintStatusHistory.complaint_id.in_(complaint_ids), dry_run) if complaint_ids else 0
    counts["risk_scores"] = _delete(db, RiskScore, RiskScore.id.in_(risk_ids), dry_run) if risk_ids else 0
    counts["complaints"] = _delete(db, Complaint, Complaint.id.in_(complaint_ids), dry_run) if complaint_ids else 0
    counts["operational_logs"] = _delete(db, WalkOperationalLog, WalkOperationalLog.id.in_(log_ids), dry_run) if log_ids else 0
    counts["matching_attempts"] = _delete(db, WalkMatchingAttempt, WalkMatchingAttempt.id.in_(attempt_ids), dry_run) if attempt_ids else 0
    counts["reviews"] = _delete(db, WalkerReview, WalkerReview.id.in_(review_ids), dry_run) if review_ids else 0
    counts["tips"] = _delete(db, TipIntegrityFlag, TipIntegrityFlag.id.in_(tip_ids), dry_run) if tip_ids else 0
    counts["payments"] = _delete(db, Payment, Payment.id.in_(payment_ids), dry_run) if payment_ids else 0
    counts["walker_weekly_missions"] = _delete(db, WalkerWeeklyMission, WalkerWeeklyMission.walker_id.in_(walker_user_ids), dry_run) if walker_user_ids else 0
    counts["walker_boosts"] = _delete(db, WalkerBoost, WalkerBoost.walker_id.in_(walker_user_ids), dry_run) if walker_user_ids else 0
    counts["walker_reputation_snapshots"] = _delete(db, WalkerReputationSnapshot, WalkerReputationSnapshot.walker_id.in_(walker_user_ids), dry_run) if walker_user_ids else 0
    counts["walker_incentives"] = _delete(db, WalkerIncentive, WalkerIncentive.walker_id.in_(walker_user_ids), dry_run) if walker_user_ids else 0
    counts["walker_monitoring_alerts"] = _delete(db, WalkerMonitoringAlert, WalkerMonitoringAlert.walker_id.in_(walker_user_ids), dry_run) if walker_user_ids else 0
    counts["walker_recovery_plans"] = _delete(db, WalkerRecoveryPlan, WalkerRecoveryPlan.walker_id.in_(walker_user_ids), dry_run) if walker_user_ids else 0
    counts["walker_referrals"] = _delete(db, WalkerReferral, WalkerReferral.id.in_(referral_ids), dry_run) if referral_ids else 0
    counts["walks"] = _delete(db, Walk, Walk.id.in_(walk_ids), dry_run) if walk_ids else 0
    counts["pets"] = _delete(db, Pet, Pet.id.in_(pet_ids), dry_run) if pet_ids else 0
    counts["walker_profiles"] = _delete(db, WalkerProfile, WalkerProfile.id.in_(walker_profile_ids), dry_run) if walker_profile_ids else 0
    counts["tutor_profiles"] = _delete(db, TutorProfile, TutorProfile.id.in_(tutor_profile_ids), dry_run) if tutor_profile_ids else 0
    counts["users"] = _delete(db, User, User.id.in_(user_ids), dry_run) if user_ids else 0

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return counts


def print_report(counts: dict[str, int], plan: dict[str, object], dry_run: bool) -> None:
    print("Relatorio final:")
    print(f"  clientes apagados: {len(plan['test_tutor_ids']) if dry_run else counts.get('users', 0)}")
    print(f"  passeadores apagados: {len(plan['test_walker_user_ids'])}")
    print(f"  pets: {counts.get('pets', 0)}")
    print(f"  passeios: {counts.get('walks', 0)}")
    print(f"  pagamentos: {counts.get('payments', 0)}")
    print(f"  reviews: {counts.get('reviews', 0)}")
    print(f"  tips: {counts.get('tips', 0)}")
    print(f"  matching_attempts: {counts.get('matching_attempts', 0)}")
    print(f"  operational_logs: {counts.get('operational_logs', 0)}")
    print(f"  documentos vinculados preservados/removidos manualmente: {plan['document_count']}")
    print(f"  registros admin preservados: {len(plan['protected_user_ids'])}")
    if dry_run:
        print("Nenhum dado foi apagado. Execute com --confirm para aplicar.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Limpeza controlada de usuarios e dados de teste.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Lista e calcula a limpeza sem apagar dados.")
    group.add_argument("--confirm", action="store_true", help="Apaga os dados de teste encontrados.")
    args = parser.parse_args()

    dry_run = not args.confirm
    db = SessionLocal()
    try:
        plan = build_plan(db)
        print_plan(plan, dry_run=dry_run)
        counts = execute_cleanup(db, plan, dry_run=dry_run)
        print_report(counts, plan, dry_run=dry_run)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import (  # noqa: E402
    SQLALCHEMY_DATABASE_URL,
    SessionLocal,
    engine,
    get_database_diagnostics,
    mask_database_url,
)
from app.models.complaint import Complaint, ComplaintDecision, ComplaintEvidence, ComplaintStatusHistory, RiskScore  # noqa: E402
from app.models.payment import Payment  # noqa: E402
from app.models.pet import Pet  # noqa: E402
from app.models.tip_integrity_flag import TipIntegrityFlag  # noqa: E402
from app.models.tutor_profile import TutorProfile  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.walk import Walk, WalkMatchingAttempt, WalkOperationalLog  # noqa: E402
from app.models.walker_boost import WalkerBoost  # noqa: E402
from app.models.walker_incentive import WalkerIncentive  # noqa: E402
from app.models.walker_monitoring_alert import WalkerMonitoringAlert  # noqa: E402
from app.models.walker_profile import WalkerProfile  # noqa: E402
from app.models.walker_recovery_plan import WalkerRecoveryPlan  # noqa: E402
from app.models.walker_referral import WalkerReferral  # noqa: E402
from app.models.walker_reputation_snapshot import WalkerReputationSnapshot  # noqa: E402
from app.models.walker_review import WalkerReview  # noqa: E402
from app.models.walker_weekly_mission import WalkerWeeklyMission  # noqa: E402


ADMIN_ROLES = {"admin", "super_admin", "superadmin"}
TUTOR_ROLES = {"tutor", "cliente", "client", "customer"}
WALKER_ROLES = {"walker", "passeador"}


def _ids(rows: Iterable[object]) -> set[str]:
    return {str(getattr(row, "id", "") or "") for row in rows if getattr(row, "id", None)}


def _sample(values: Iterable[str], limit: int = 8) -> str:
    items = sorted(str(value) for value in values if value)
    if not items:
        return "-"
    suffix = "" if len(items) <= limit else f" ... +{len(items) - limit}"
    return ", ".join(items[:limit]) + suffix


def _table_exists(model: type) -> bool:
    return inspect(engine).has_table(model.__tablename__)


def _all(db, model: type) -> list:
    if not _table_exists(model):
        return []
    return db.query(model).all()


def _rows(db, table_name: str, columns: tuple[str, ...]) -> list[dict[str, object]]:
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return []
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    selected = [column for column in columns if column in existing]
    if not selected:
        return []
    sql = text(f"SELECT {', '.join(selected)} FROM {table_name}")
    result = db.execute(sql).mappings().all()
    return [{column: row.get(column) for column in columns} for row in result]


def _count(db, model: type, condition=None) -> int:
    if not _table_exists(model):
        return 0
    query = db.query(model)
    if condition is not None:
        query = query.filter(condition)
    return query.count()


def _delete(db, model: type, condition, dry_run: bool) -> int:
    if not _table_exists(model):
        return 0
    query = db.query(model).filter(condition)
    count = query.count()
    if count and not dry_run:
        query.delete(synchronize_session=False)
    return count


def _delete_in(db, table_name: str, column_name: str, values: set[str], dry_run: bool) -> int:
    if not values:
        return 0
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return 0
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name not in existing:
        return 0
    params = {"values": list(values)}
    count_sql = text(f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} IN :values").bindparams(
        bindparam("values", expanding=True)
    )
    count = int(db.execute(count_sql, params).scalar() or 0)
    if count and not dry_run:
        delete_sql = text(f"DELETE FROM {table_name} WHERE {column_name} IN :values").bindparams(
            bindparam("values", expanding=True)
        )
        db.execute(delete_sql, params)
    return count


def _print_database_diagnostics() -> None:
    backend = get_database_diagnostics()
    cleanup_url = engine.url.render_as_string(hide_password=False)
    same_database = make_url(SQLALCHEMY_DATABASE_URL) == engine.url
    print("Diagnostico de banco:")
    print(f"  backend DATABASE_URL: {mask_database_url(backend['database_url'])}")
    print(f"  cleanup DATABASE_URL: {mask_database_url(cleanup_url)}")
    print(f"  mesmo banco: {'SIM' if same_database else 'NAO'}")
    print(f"  env backend: {backend['env_path']}")
    if "sqlite_path" in backend:
        print(f"  sqlite absoluto: {backend['sqlite_path']}")


def build_plan(db) -> dict[str, object]:
    users = _rows(db, "users", ("id", "email", "full_name", "role"))
    admin_user_ids = {
        str(user["id"])
        for user in users
        if (str(user.get("role") or "")).strip().lower() in ADMIN_ROLES
    }
    operational_user_ids = {
        str(user["id"])
        for user in users
        if (str(user.get("role") or "")).strip().lower() in (TUTOR_ROLES | WALKER_ROLES)
        and str(user["id"]) not in admin_user_ids
    }

    tutor_profiles = [
        profile
        for profile in _rows(db, "tutor_profiles", ("id", "user_id", "photo_url"))
        if str(profile.get("user_id") or "") not in admin_user_ids
    ]
    walker_profiles = [
        profile
        for profile in _rows(
            db,
            "walker_profiles",
            (
                "id",
                "user_id",
                "profile_photo_url",
                "document_url",
                "identity_document_back_url",
                "selfie_url",
                "proof_of_address_url",
            ),
        )
        if str(profile.get("user_id") or "") not in admin_user_ids
    ]
    operational_user_ids.update(str(profile["user_id"]) for profile in tutor_profiles if profile.get("user_id"))
    operational_user_ids.update(str(profile["user_id"]) for profile in walker_profiles if profile.get("user_id"))

    tutor_user_ids = {
        str(user["id"])
        for user in users
        if str(user["id"]) in operational_user_ids and (str(user.get("role") or "")).strip().lower() in TUTOR_ROLES
    }
    walker_user_ids = {
        str(user["id"])
        for user in users
        if str(user["id"]) in operational_user_ids and (str(user.get("role") or "")).strip().lower() in WALKER_ROLES
    }

    pets = [
        pet
        for pet in _rows(db, "pets", ("id", "tutor_id", "photo_url"))
        if str(pet.get("tutor_id") or "") in operational_user_ids
    ]
    pet_ids = {str(pet["id"]) for pet in pets if pet.get("id")}

    walks = [
        walk
        for walk in _rows(db, "walks", ("id", "tutor_id", "walker_id", "assigned_walker_id", "pet_id"))
        if str(walk.get("tutor_id") or "") in operational_user_ids
        or str(walk.get("walker_id") or "") in operational_user_ids
        or str(walk.get("assigned_walker_id") or "") in operational_user_ids
        or str(walk.get("pet_id") or "") in pet_ids
    ]
    walk_ids = {str(walk["id"]) for walk in walks if walk.get("id")}

    payments = [
        payment
        for payment in _rows(db, "payments", ("id", "tutor_id", "walk_id"))
        if str(payment.get("tutor_id") or "") in operational_user_ids or str(payment.get("walk_id") or "") in walk_ids
    ]
    reviews = [
        review
        for review in _rows(db, "walker_reviews", ("id", "walk_id", "tutor_id", "walker_id"))
        if str(review.get("walk_id") or "") in walk_ids
        or str(review.get("tutor_id") or "") in operational_user_ids
        or str(review.get("walker_id") or "") in operational_user_ids
    ]
    tips = [
        tip
        for tip in _rows(db, "tip_integrity_flags", ("id", "walk_id", "tutor_id", "walker_id"))
        if str(tip.get("walk_id") or "") in walk_ids
        or str(tip.get("tutor_id") or "") in operational_user_ids
        or str(tip.get("walker_id") or "") in operational_user_ids
    ]
    attempts = [
        attempt
        for attempt in _rows(db, "walk_matching_attempts", ("id", "walk_id", "walker_id"))
        if str(attempt.get("walk_id") or "") in walk_ids or str(attempt.get("walker_id") or "") in operational_user_ids
    ]
    logs = [
        log
        for log in _rows(db, "walk_operational_logs", ("id", "walk_id", "actor_id"))
        if str(log.get("walk_id") or "") in walk_ids or str(log.get("actor_id") or "") in operational_user_ids
    ]
    complaints = [
        complaint
        for complaint in _rows(
            db,
            "complaints",
            ("id", "author_id", "target_user_id", "target_pet_id", "walk_id", "resolved_by_admin_id"),
        )
        if str(complaint.get("author_id") or "") in operational_user_ids
        or str(complaint.get("target_user_id") or "") in operational_user_ids
        or str(complaint.get("target_pet_id") or "") in pet_ids
        or str(complaint.get("walk_id") or "") in walk_ids
        or str(complaint.get("resolved_by_admin_id") or "") in operational_user_ids
    ]
    complaint_ids = {str(complaint["id"]) for complaint in complaints if complaint.get("id")}
    risks = [
        risk
        for risk in _rows(db, "risk_scores", ("id", "subject_id"))
        if str(risk.get("subject_id") or "") in operational_user_ids
        or str(risk.get("subject_id") or "") in pet_ids
        or str(risk.get("subject_id") or "") in walk_ids
    ]
    referrals = [
        referral
        for referral in _rows(db, "walker_referrals", ("id", "referrer_user_id", "referred_user_id"))
        if str(referral.get("referrer_user_id") or "") in operational_user_ids
        or str(referral.get("referred_user_id") or "") in operational_user_ids
    ]

    documents = []
    for profile in walker_profiles:
        documents.extend(
            value
            for value in (
                profile.get("profile_photo_url"),
                profile.get("document_url"),
                profile.get("identity_document_back_url"),
                profile.get("selfie_url"),
                profile.get("proof_of_address_url"),
            )
            if value
        )
    documents.extend(profile.get("photo_url") for profile in tutor_profiles if profile.get("photo_url"))
    documents.extend(pet.get("photo_url") for pet in pets if pet.get("photo_url"))

    return {
        "admin_user_ids": admin_user_ids,
        "operational_user_ids": operational_user_ids,
        "tutor_user_ids": tutor_user_ids,
        "walker_user_ids": walker_user_ids,
        "tutor_profile_ids": {str(profile["id"]) for profile in tutor_profiles if profile.get("id")},
        "walker_profile_ids": {str(profile["id"]) for profile in walker_profiles if profile.get("id")},
        "pet_ids": pet_ids,
        "walk_ids": walk_ids,
        "payment_ids": {str(payment["id"]) for payment in payments if payment.get("id")},
        "review_ids": {str(review["id"]) for review in reviews if review.get("id")},
        "tip_ids": {str(tip["id"]) for tip in tips if tip.get("id")},
        "attempt_ids": {str(attempt["id"]) for attempt in attempts if attempt.get("id")},
        "log_ids": {str(log["id"]) for log in logs if log.get("id")},
        "complaint_ids": complaint_ids,
        "risk_ids": {str(risk["id"]) for risk in risks if risk.get("id")},
        "referral_ids": {str(referral["id"]) for referral in referrals if referral.get("id")},
        "document_refs": documents,
    }


def print_plan(plan: dict[str, object], dry_run: bool) -> None:
    print(f"Modo: {'DRY-RUN' if dry_run else 'CONFIRM'}")
    print("Preservados:")
    print(f"  admins/superadmins: {len(plan['admin_user_ids'])} ({_sample(plan['admin_user_ids'])})")
    print("Candidatos a apagar:")
    labels = (
        ("clientes", "tutor_user_ids"),
        ("passeadores", "walker_user_ids"),
        ("tutor_profiles", "tutor_profile_ids"),
        ("walker_profiles", "walker_profile_ids"),
        ("pets", "pet_ids"),
        ("walks", "walk_ids"),
        ("payments", "payment_ids"),
        ("tips", "tip_ids"),
        ("reviews", "review_ids"),
        ("matching_attempts", "attempt_ids"),
        ("operational_logs", "log_ids"),
        ("complaints", "complaint_ids"),
        ("risk_scores", "risk_ids"),
        ("walker_referrals", "referral_ids"),
        ("usuarios operacionais", "operational_user_ids"),
    )
    for label, key in labels:
        print(f"  {label}: {len(plan[key])} ({_sample(plan[key])})")
    print(f"  documentos vinculados em registros: {len(plan['document_refs'])}")


def execute_cleanup(db, plan: dict[str, object], dry_run: bool) -> dict[str, int]:
    operational_user_ids = plan["operational_user_ids"]
    walker_user_ids = plan["walker_user_ids"]
    pet_ids = plan["pet_ids"]
    walk_ids = plan["walk_ids"]
    complaint_ids = plan["complaint_ids"]

    counts: dict[str, int] = {}
    counts["complaint_evidences"] = _delete_in(db, "complaint_evidences", "complaint_id", complaint_ids, dry_run)
    counts["complaint_decisions"] = _delete_in(db, "complaint_decisions", "complaint_id", complaint_ids, dry_run)
    counts["complaint_status_history"] = _delete_in(db, "complaint_status_history", "complaint_id", complaint_ids, dry_run)
    counts["risk_scores"] = _delete_in(db, "risk_scores", "id", plan["risk_ids"], dry_run)
    counts["complaints"] = _delete_in(db, "complaints", "id", complaint_ids, dry_run)
    counts["operational_logs"] = _delete_in(db, "walk_operational_logs", "id", plan["log_ids"], dry_run)
    counts["matching_attempts"] = _delete_in(db, "walk_matching_attempts", "id", plan["attempt_ids"], dry_run)
    counts["reviews"] = _delete_in(db, "walker_reviews", "id", plan["review_ids"], dry_run)
    counts["tips"] = _delete_in(db, "tip_integrity_flags", "id", plan["tip_ids"], dry_run)
    counts["payments"] = _delete_in(db, "payments", "id", plan["payment_ids"], dry_run)
    counts["walker_weekly_missions"] = _delete_in(db, "walker_weekly_missions", "walker_id", walker_user_ids, dry_run)
    counts["walker_boosts"] = _delete_in(db, "walker_boosts", "walker_id", walker_user_ids, dry_run)
    counts["walker_reputation_snapshots"] = _delete_in(db, "walker_reputation_snapshots", "walker_id", walker_user_ids, dry_run)
    counts["walker_incentives"] = _delete_in(db, "walker_incentives", "walker_id", walker_user_ids, dry_run)
    counts["walker_monitoring_alerts"] = _delete_in(db, "walker_monitoring_alerts", "walker_id", walker_user_ids, dry_run)
    counts["walker_recovery_plans"] = _delete_in(db, "walker_recovery_plans", "walker_id", walker_user_ids, dry_run)
    counts["walker_referrals"] = _delete_in(db, "walker_referrals", "id", plan["referral_ids"], dry_run)
    counts["walks"] = _delete_in(db, "walks", "id", walk_ids, dry_run)
    counts["pets"] = _delete_in(db, "pets", "id", pet_ids, dry_run)
    counts["walker_profiles"] = _delete_in(db, "walker_profiles", "id", plan["walker_profile_ids"], dry_run)
    counts["tutor_profiles"] = _delete_in(db, "tutor_profiles", "id", plan["tutor_profile_ids"], dry_run)
    counts["users"] = _delete_in(db, "users", "id", operational_user_ids, dry_run)

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return counts


def final_counts(db) -> dict[str, int]:
    role_values = TUTOR_ROLES | WALKER_ROLES
    operational_user_ids = {
        str(user["id"])
        for user in _rows(db, "users", ("id", "role"))
        if (str(user.get("role") or "")).strip().lower() in role_values
    }
    return {
        "clientes": _count(db, User, User.role.in_(TUTOR_ROLES)),
        "passeadores": _count(db, User, User.role.in_(WALKER_ROLES)),
        "pets": _count(db, Pet),
        "passeios": _count(db, Walk),
        "pagamentos_operacionais": _count(db, Payment, Payment.tutor_id.in_(operational_user_ids)) if operational_user_ids else _count(db, Payment),
    }


def print_report(db, counts: dict[str, int], plan: dict[str, object], dry_run: bool) -> None:
    print("Relatorio:")
    for key, count in counts.items():
        print(f"  apagaria {key}: {count}" if dry_run else f"  apagou {key}: {count}")
    print(f"  documentos vinculados nos registros apagados: {len(plan['document_refs'])}")
    if dry_run:
        print("Nenhum dado foi apagado. Execute com --confirm para aplicar.")
        return

    remaining = final_counts(db)
    print("Contagem final no mesmo banco:")
    print(f"  clientes = {remaining['clientes']}")
    print(f"  passeadores = {remaining['passeadores']}")
    print(f"  pets = {remaining['pets']}")
    print(f"  passeios = {remaining['passeios']}")
    print(f"  pagamentos operacionais = {remaining['pagamentos_operacionais']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Limpeza operacional definitiva preservando admin/superadmin.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Mostra o plano sem apagar dados.")
    group.add_argument("--confirm", action="store_true", help="Apaga clientes, passeadores e dados operacionais.")
    args = parser.parse_args()

    dry_run = not args.confirm
    _print_database_diagnostics()
    db = SessionLocal()
    try:
        plan = build_plan(db)
        print_plan(plan, dry_run=dry_run)
        counts = execute_cleanup(db, plan, dry_run=dry_run)
        print_report(db, counts, plan, dry_run=dry_run)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

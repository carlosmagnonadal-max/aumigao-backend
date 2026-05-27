import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from app.core.database import Base, SessionLocal, engine, get_database_diagnostics, mask_database_url
from app.models import (
    AdminOperationalEvent,
    Complaint,
    ComplaintDecision,
    ComplaintEvidence,
    ComplaintStatusHistory,
    OperationalBetaLog,
    Payment,
    Pet,
    RiskScore,
    TipIntegrityFlag,
    TutorProfile,
    User,
    Walk,
    WalkMatchingAttempt,
    WalkOperationalLog,
    WalkerBoost,
    WalkerIncentive,
    WalkerKitSubmission,
    WalkerMonitoringAlert,
    WalkerProfile,
    WalkerRecoveryPlan,
    WalkerReferral,
    WalkerReputationSnapshot,
    WalkerReview,
    WalkerWeeklyMission,
    WalkCompletionReview,
    LegalAcceptance,
)
from app.routes import admin, auth, complaints, legal, matching, notifications, operational_walks, payments, pets, referrals, reviews, tutor, walker, walker_quality, walks, weekly_missions
from app.services.admin_seed_service import ensure_configured_admin_users
from app.services.operational_matching_service import ensure_operational_schema
from app.services.operational_scheduler_service import (
    mark_operational_scheduler_started,
    mark_operational_scheduler_stopped,
    run_operational_scheduler_cycle,
    scheduler_interval_seconds,
)

logger = logging.getLogger(__name__)


def ensure_legacy_id_compatibility():
    if engine.dialect.name != "postgresql":
        return
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                connection.execute(text(f"DROP TYPE IF EXISTS {table.name} CASCADE"))
    targets = {
        "users": ("id",),
        "tutor_profiles": ("id", "user_id"),
        "pets": ("id", "owner_id"),
    }
    with engine.begin() as connection:
        for table_name in targets:
            if table_name not in inspector.get_table_names():
                continue
            for foreign_key in inspector.get_foreign_keys(table_name):
                constraint_name = foreign_key.get("name")
                if constraint_name:
                    connection.execute(text(f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {constraint_name}"))
    for table_name, column_names in targets.items():
        if table_name not in inspector.get_table_names():
            continue
        columns = {column["name"]: str(column["type"]).lower() for column in inspector.get_columns(table_name)}
        with engine.begin() as connection:
            for column_name in column_names:
                column_type = columns.get(column_name, "")
                if column_type and "char" not in column_type and "text" not in column_type:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE VARCHAR USING {column_name}::VARCHAR")
                    )


ensure_legacy_id_compatibility()
Base.metadata.create_all(bind=engine)
ensure_operational_schema(engine)
_db_diagnostics = get_database_diagnostics()
print(f"[database] backend DATABASE_URL={mask_database_url(_db_diagnostics['database_url'])}")
if "sqlite_path" in _db_diagnostics:
    print(f"[database] backend SQLite path={_db_diagnostics['sqlite_path']}")

def _sql_type(kind: str) -> str:
    if kind == "datetime":
        return "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
    if kind == "boolean_true":
        return "BOOLEAN DEFAULT TRUE" if engine.dialect.name == "postgresql" else "BOOLEAN DEFAULT 1"
    if kind == "boolean_false":
        return "BOOLEAN DEFAULT FALSE" if engine.dialect.name == "postgresql" else "BOOLEAN DEFAULT 0"
    return kind


def _add_missing_columns(table_name: str, columns: dict[str, str]):
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    with engine.begin() as connection:
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {definition}"))


def ensure_user_schema():
    _add_missing_columns(
        "users",
        {
            "password_hash": "VARCHAR DEFAULT ''",
            "full_name": "VARCHAR DEFAULT ''",
            "role": "VARCHAR DEFAULT 'tutor'",
            "is_active": _sql_type("boolean_true"),
            "created_at": _sql_type("datetime"),
        },
    )
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "password" in existing and "password_hash" in existing:
        with engine.begin() as connection:
            connection.execute(text("UPDATE users SET password_hash = password WHERE COALESCE(password_hash, '') = ''"))


ensure_user_schema()

def ensure_walker_profile_schema():
    _add_missing_columns(
        "walker_profiles",
        {
            "cpf": "VARCHAR DEFAULT ''",
            "profile_photo_url": "VARCHAR",
            "identity_document_back_url": "VARCHAR",
            "internal_notes": "TEXT DEFAULT ''",
            "active_as_walker": _sql_type("boolean_false"),
            "approved_at": _sql_type("datetime"),
            "rejected_at": _sql_type("datetime"),
            "updated_at": _sql_type("datetime"),
            "reviewed_by_admin_id": "VARCHAR",
            "resubmission_requested_documents": "TEXT DEFAULT ''",
        },
    )

ensure_walker_profile_schema()

def ensure_tutor_profile_schema():
    _add_missing_columns(
        "tutor_profiles",
        {
            "cpf": "VARCHAR DEFAULT ''",
            "photo_url": "VARCHAR",
            "pickup_notes": "TEXT DEFAULT ''",
            "preferred_method": "VARCHAR DEFAULT 'Buscar em casa'",
            "created_at": _sql_type("datetime"),
        },
    )
    inspector = inspect(engine)
    if "tutor_profiles" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("tutor_profiles")}
    with engine.begin() as connection:
        if {"profile_photo_url", "photo_url"}.issubset(existing):
            connection.execute(text("UPDATE tutor_profiles SET photo_url = profile_photo_url WHERE photo_url IS NULL"))
        if {"pet_pickup_notes", "pickup_notes"}.issubset(existing):
            connection.execute(text("UPDATE tutor_profiles SET pickup_notes = pet_pickup_notes WHERE COALESCE(pickup_notes, '') = ''"))
        if {"preferred_pickup_method", "preferred_method"}.issubset(existing):
            connection.execute(text("UPDATE tutor_profiles SET preferred_method = preferred_pickup_method WHERE COALESCE(preferred_method, '') = ''"))

ensure_tutor_profile_schema()

def ensure_pet_schema():
    _add_missing_columns(
        "pets",
        {
            "tutor_id": "VARCHAR",
            "photo_url": "VARCHAR",
            "species": "VARCHAR DEFAULT 'Cachorro'",
            "sex": "VARCHAR DEFAULT ''",
            "weight": "FLOAT",
            "behavior_notes": "TEXT DEFAULT ''",
            "is_social": _sql_type("boolean_true"),
            "afraid_of_noise": _sql_type("boolean_false"),
            "pulls_leash": _sql_type("boolean_false"),
            "can_walk_with_other_pets": _sql_type("boolean_false"),
            "is_neutered": _sql_type("boolean_false"),
            "medications": "TEXT DEFAULT ''",
            "restrictions": "TEXT DEFAULT ''",
            "created_at": _sql_type("datetime"),
        },
    )
    inspector = inspect(engine)
    if "pets" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("pets")}
    with engine.begin() as connection:
        if {"owner_id", "tutor_id"}.issubset(existing):
            connection.execute(text("UPDATE pets SET tutor_id = owner_id WHERE tutor_id IS NULL"))
        if {"behavior", "behavior_notes"}.issubset(existing):
            connection.execute(text("UPDATE pets SET behavior_notes = behavior WHERE COALESCE(behavior_notes, '') = ''"))
        if {"castrated", "is_neutered"}.issubset(existing):
            connection.execute(
                text(
                    "UPDATE pets SET is_neutered = LOWER(CAST(castrated AS VARCHAR)) IN ('true', '1', 'sim', 'yes') "
                    "WHERE is_neutered = FALSE"
                )
            )


ensure_pet_schema()
with SessionLocal() as db:
    ensure_configured_admin_users(db)

app = FastAPI(title="Aumigao Walk API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Path("uploads").mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

app.include_router(auth.router)
app.include_router(tutor.router)
app.include_router(pets.router)
app.include_router(walks.router)
app.include_router(notifications.router)
app.include_router(notifications.api_router)
app.include_router(operational_walks.router)
app.include_router(operational_walks.api_router)
app.include_router(walker.router)
app.include_router(walker.api_public_router)
app.include_router(walker.partner_router)
app.include_router(payments.router)
app.include_router(admin.router)
app.include_router(admin.api_router)
app.include_router(operational_walks.admin_router)
app.include_router(operational_walks.api_admin_router)
app.include_router(referrals.router)
app.include_router(referrals.api_router)
app.include_router(referrals.admin_router)
app.include_router(referrals.api_admin_router)
app.include_router(reviews.walks_router)
app.include_router(reviews.api_walks_router)
app.include_router(reviews.walkers_router)
app.include_router(reviews.api_walkers_router)
app.include_router(reviews.walker_router)
app.include_router(reviews.api_walker_router)
app.include_router(reviews.admin_router)
app.include_router(reviews.api_admin_router)
app.include_router(weekly_missions.walker_router)
app.include_router(weekly_missions.api_walker_router)
app.include_router(weekly_missions.admin_router)
app.include_router(weekly_missions.api_admin_router)
app.include_router(matching.router)
app.include_router(matching.api_router)
app.include_router(matching.admin_router)
app.include_router(matching.api_admin_router)
app.include_router(walker_quality.walker_router)
app.include_router(walker_quality.api_walker_router)
app.include_router(walker_quality.admin_router)
app.include_router(walker_quality.api_admin_router)
app.include_router(complaints.router)
app.include_router(complaints.api_router)
app.include_router(complaints.admin_router)
app.include_router(complaints.api_admin_router)
app.include_router(complaints.legacy_admin_occurrences_router)
app.include_router(complaints.api_legacy_admin_occurrences_router)
app.include_router(legal.router)
app.include_router(legal.api_router)

_operational_scheduler_task: asyncio.Task | None = None


async def _operational_scheduler_loop():
    mark_operational_scheduler_started()
    try:
        while True:
            await asyncio.sleep(scheduler_interval_seconds())
            try:
                await run_operational_scheduler_cycle(SessionLocal)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                mark_operational_scheduler_stopped(str(exc))
                logger.exception("Erro no scheduler operacional do beta.")
    except asyncio.CancelledError:
        mark_operational_scheduler_stopped()
        raise


@app.on_event("startup")
async def start_operational_scheduler():
    global _operational_scheduler_task
    if _operational_scheduler_task and not _operational_scheduler_task.done():
        mark_operational_scheduler_started()
        return
    _operational_scheduler_task = asyncio.create_task(_operational_scheduler_loop())


@app.on_event("shutdown")
async def stop_operational_scheduler():
    global _operational_scheduler_task
    if not _operational_scheduler_task:
        mark_operational_scheduler_stopped()
        return
    if not _operational_scheduler_task.done():
        _operational_scheduler_task.cancel()
        try:
            await _operational_scheduler_task
        except asyncio.CancelledError:
            pass
    _operational_scheduler_task = None
    mark_operational_scheduler_stopped()


@app.get("/")
def root():
    return {"message": "Aumigao Walk API rodando"}

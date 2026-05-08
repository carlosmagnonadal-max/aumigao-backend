import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from app.core.database import Base, SessionLocal, engine
from app.models import (
    Complaint,
    ComplaintDecision,
    ComplaintEvidence,
    ComplaintStatusHistory,
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
    WalkerMonitoringAlert,
    WalkerProfile,
    WalkerRecoveryPlan,
    WalkerReferral,
    WalkerReputationSnapshot,
    WalkerReview,
    WalkerWeeklyMission,
)
from app.routes import admin, auth, complaints, matching, operational_walks, payments, pets, referrals, reviews, tutor, walker, walker_quality, walks, weekly_missions
from app.services.admin_seed_service import ensure_configured_admin_users
from app.services.operational_matching_service import ensure_operational_schema, process_expired_attempts

Base.metadata.create_all(bind=engine)
ensure_operational_schema(engine)

def ensure_walker_profile_schema():
    inspector = inspect(engine)
    if "walker_profiles" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("walker_profiles")}
    columns = {
        "cpf": "VARCHAR DEFAULT ''",
        "profile_photo_url": "VARCHAR",
        "internal_notes": "TEXT DEFAULT ''",
        "active_as_walker": "BOOLEAN DEFAULT 0",
        "approved_at": "DATETIME",
        "rejected_at": "DATETIME",
    }
    with engine.begin() as connection:
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE walker_profiles ADD COLUMN {name} {definition}"))

ensure_walker_profile_schema()

def ensure_tutor_profile_schema():
    inspector = inspect(engine)
    if "tutor_profiles" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("tutor_profiles")}
    columns = {
        "cpf": "VARCHAR DEFAULT ''",
    }
    with engine.begin() as connection:
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE tutor_profiles ADD COLUMN {name} {definition}"))

ensure_tutor_profile_schema()
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

try:
    app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
except RuntimeError:
    pass

app.include_router(auth.router)
app.include_router(tutor.router)
app.include_router(pets.router)
app.include_router(walks.router)
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


async def _operational_matching_scheduler():
    while True:
        await asyncio.sleep(30)
        db = SessionLocal()
        try:
            process_expired_attempts(db)
        finally:
            db.close()


@app.on_event("startup")
async def start_operational_matching_scheduler():
    asyncio.create_task(_operational_matching_scheduler())


@app.get("/")
def root():
    return {"message": "Aumigao Walk API rodando"}

import asyncio
import logging
import os
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import inspect, text

# O3 — logging estruturado: deve ser configurado o mais cedo possível, antes de
# qualquer logger ser usado.
from app.core.logging_config import configure_logging
configure_logging()

# O4 — Sentry opcional: import e init guardados para o app funcionar sem o pacote.
_SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk  # type: ignore
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            environment=os.getenv("ENVIRONMENT", "local"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
        )
    except Exception as _sentry_err:
        logging.getLogger(__name__).warning("Sentry init falhou: %s", _sentry_err)

from app.core.database import Base, SessionLocal, engine, get_database_diagnostics, mask_database_url
from app.core.request_context import request_id_var
from app.middleware.request_context import RequestContextMiddleware
from app.middleware.tenant_resolver import TenantResolverMiddleware
from app.models import (
    AdminOperationalEvent,
    Complaint,
    ComplaintDecision,
    ComplaintEvidence,
    ComplaintStatusHistory,
    OperationalBetaLog,
    Payment,
    Pet,
    ProtectedChatMessage,
    RiskScore,
    TipIntegrityFlag,
    Tenant,
    TenantBranding,
    TenantFeature,
    TenantSettings,
    TenantUnit,
    TenantWalkerAccess,
    TutorProfile,
    User,
    Walk,
    WalkMatchingAttempt,
    WalkOperationalLog,
    WalkerBoost,
    WalkerIncentive,
    WalkerKitSubmission,
    WalkerMonitoringAlert,
    WalkerNetworkProfile,
    WalkerProfile,
    WalkerRecoveryPlan,
    WalkerReferral,
    WalkerReputationSnapshot,
    WalkerReview,
    WalkerWeeklyMission,
    WalkCompletionReview,
    LegalAcceptance,
)
from app.models.support_ticket import SupportTicket  # noqa: F401 — garante tabela no metadata
from app.models.walk_location_ping import WalkLocationPing  # noqa: F401 — garante tabela no metadata
from app.routes import admin, admin_accounts, auth, client_errors, complaints, contact, coupons, fiscal, incentives, individual_walk_pricing, legal, live_share, matching, notifications, operational_walks, partner_application, payments, pet_health, pet_profile, pet_routine, pet_self_walk, pet_share, pet_tour, pets, protected_chat, recurring_plans, referrals, reviews, shared_walks, support_tickets, tenant_app_config, tenant_branding, tenant_commercial, tenant_dedicated_app_readiness, tenant_features_runtime, tenant_launch_readiness, tenant_units_runtime, tenants, tutor, tutor_gamification, tutor_referral_config, tutor_referrals, walker, walker_ecosystem, walker_network, walker_quality, walker_trust, walk_locations, walks, weekly_missions
from app.services.admin_seed_service import ensure_configured_admin_users
from app.services.tenant_seed_service import ensure_default_tenant_links, ensure_network_profiles
from app.services.operational_matching_service import ensure_operational_schema
from app.services.operational_scheduler_service import (
    mark_operational_scheduler_started,
    mark_operational_scheduler_stopped,
    run_operational_scheduler_cycle,
    scheduler_interval_seconds,
)
from app.services import object_storage
from app.services.signed_uploads import UPLOAD_ROOT, has_valid_upload_signature, is_sensitive_upload_path, upload_file_path

logger = logging.getLogger(__name__)

import re as _re

_SAFE_IDENT_RE = _re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def _safe_ident(name: str) -> str:
    """Validate a SQL identifier (table or column name) against a strict allowlist
    regex before interpolation into DDL text(). Raises ValueError if invalid.
    Only call this for values that are NOT already controlled hardcoded literals."""
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier rejected: {name!r}")
    return name


def get_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_production_environment() -> bool:
    environment = (os.getenv("ENVIRONMENT") or os.getenv("RAILWAY_ENVIRONMENT") or "").strip().lower()
    return environment in {"production", "prod"}


def ensure_legacy_id_compatibility():
    if engine.dialect.name != "postgresql":
        return
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                # nosec: table.name comes from SQLAlchemy model metadata (internal literal)
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
                    try:
                        safe_constraint = _safe_ident(constraint_name)
                    except ValueError:
                        logger.warning("ensure_legacy_id_compatibility: skipping unsafe constraint name %r", constraint_name)
                        continue
                    # nosec: table_name is a hardcoded key from targets dict above
                    connection.execute(text(f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {safe_constraint}"))
    for table_name, column_names in targets.items():
        if table_name not in inspector.get_table_names():
            continue
        columns = {column["name"]: str(column["type"]).lower() for column in inspector.get_columns(table_name)}
        with engine.begin() as connection:
            for column_name in column_names:
                column_type = columns.get(column_name, "")
                if column_type and "char" not in column_type and "text" not in column_type:
                    safe_col = _safe_ident(column_name)  # column_names are hardcoded literals
                    # nosec: table_name is a hardcoded key from targets dict above
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ALTER COLUMN {safe_col} TYPE VARCHAR USING {safe_col}::VARCHAR")
                    )

_db_diagnostics = get_database_diagnostics()
logger.info("[database] backend DATABASE_URL=%s", mask_database_url(_db_diagnostics['database_url']))
if "sqlite_path" in _db_diagnostics:
    logger.info("[database] backend SQLite path=%s", _db_diagnostics['sqlite_path'])

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
    # Validate table_name and column names as SQL identifiers before DDL interpolation.
    # All callers pass hardcoded literals, but we guard defensively.
    safe_table = _safe_ident(table_name)
    with engine.begin() as connection:
        for name, definition in columns.items():
            if name not in existing:
                safe_name = _safe_ident(name)
                # nosec: definition is a SQL type string from hardcoded caller dicts (not user input)
                connection.execute(text(f"ALTER TABLE {safe_table} ADD COLUMN {safe_name} {definition}"))


def ensure_user_schema():
    _add_missing_columns(
        "users",
        {
            "password_hash": "VARCHAR DEFAULT ''",
            "tenant_id": "VARCHAR",
            "full_name": "VARCHAR DEFAULT ''",
            "role": "VARCHAR DEFAULT 'tutor'",
            "is_active": _sql_type("boolean_true"),
            "created_at": _sql_type("datetime"),
        },
    )
    # Bloco de migração legado REMOVIDO (2026-06-22): copiava uma coluna `password`
    # (fóssil de schema antigo) para `password_hash`. Era footgun de segurança —
    # poderia mover senha em texto plano para o campo de hash. A migração one-time
    # já rodou em todo boot por meses; o ORM (user.py) usa apenas `password_hash`.


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

def ensure_tutor_profile_schema():
    _add_missing_columns(
        "tutor_profiles",
        {
            "cpf": "VARCHAR DEFAULT ''",
            "tenant_id": "VARCHAR",
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

def ensure_pet_schema():
    _add_missing_columns(
        "pets",
        {
            "tutor_id": "VARCHAR",
            "tenant_id": "VARCHAR",
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
            "birth_date": "DATE",
            "chip_number": "VARCHAR",
            "vet_name": "VARCHAR",
            "vet_phone": "VARCHAR",
            "emergency_contact": "VARCHAR",
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


def ensure_walk_schema():
    _add_missing_columns(
        "walks",
        {
            "tenant_id": "VARCHAR",
        },
    )


def ensure_notification_schema():
    _add_missing_columns(
        "notifications",
        {
            "tenant_id": "VARCHAR",
        },
    )


_production_environment = _is_production_environment()
_run_startup_schema_ensure = get_bool_env("RUN_STARTUP_SCHEMA_ENSURE", default=not _production_environment)
_run_startup_admin_seed = get_bool_env("RUN_STARTUP_ADMIN_SEED", default=not _production_environment)
# DDL DESTRUTIVO (DROP de constraints/types) — fail-SAFE: só roda com opt-in
# explícito (RUN_LEGACY_ID_COMPAT=true), nunca por omissão de env. Em SQLite é
# no-op (dialect != postgresql) de qualquer forma. Onda 0 / mt-MT6.
_run_legacy_id_compat = get_bool_env("RUN_LEGACY_ID_COMPAT", default=False)

if _run_startup_schema_ensure:
    logger.info("[startup] schema ensure enabled")
    if _run_legacy_id_compat:
        logger.info("[startup] legacy id compatibility ENABLED (DDL destrutivo) — RUN_LEGACY_ID_COMPAT=true")
        ensure_legacy_id_compatibility()
    else:
        logger.info("[startup] legacy id compatibility SKIPPED (defina RUN_LEGACY_ID_COMPAT=true para habilitar)")
    Base.metadata.create_all(bind=engine)
    ensure_operational_schema(engine)
    ensure_user_schema()
    ensure_walker_profile_schema()
    ensure_tutor_profile_schema()
    ensure_pet_schema()
    ensure_walk_schema()
    ensure_notification_schema()
else:
    logger.info("[startup] schema ensure skipped")

if _run_startup_admin_seed:
    logger.info("[startup] admin seed enabled")
    with SessionLocal() as db:
        # Fase 2c: seed é operação global de plataforma → acesso irrestrito.
        db.info["rls_tenant"] = "*"
        ensure_configured_admin_users(db)
        ensure_default_tenant_links(db)
        ensure_network_profiles(db)
else:
    logger.info("[startup] admin seed skipped")

# Sec: em produção desabilita /docs, /redoc e /openapi.json. Expor o schema
# completo da API publicamente facilita reconhecimento de atacante. Em dev/staging
# (ENVIRONMENT != production) os docs continuam disponíveis para desenvolvimento.
_is_production = os.getenv("ENVIRONMENT", "").strip().lower() == "production"
app = FastAPI(
    title="Aumigao Walk API",
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)


# O1 — Exception handler global: captura qualquer Exception não tratada
# (HTTPException continua seguindo o fluxo normal do FastAPI).
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request_id_var.get("-")
    logger.error(
        "unhandled_exception method=%s path=%s request_id=%s\n%s",
        request.method,
        request.url.path,
        request_id,
        traceback.format_exc(),
    )
    # Registra na tabela operacional sem deixar o registro derrubar o handler.
    try:
        from app.services.operational_observability_service import record_operational_exception
        with SessionLocal() as _db:
            # Fase 2c: exception handler é operação global de plataforma → acesso irrestrito.
            _db.info["rls_tenant"] = "*"
            record_operational_exception(
                _db,
                event_type="unhandled_exception",
                source="global_exception_handler",
                exc=exc,
                context={
                    "method": request.method,
                    "path": request.url.path,
                    "request_id": request_id,
                },
            )
            _db.commit()
    except Exception as _reg_err:
        logger.warning("Falha ao registrar exceção operacional: %s", _reg_err)

    return JSONResponse(
        status_code=500,
        content={"detail": "Erro interno do servidor"},
        headers={"X-Request-ID": request_id},
    )


# ---------------------------------------------------------------------------
# CORS: origens permitidas via env CORS_ALLOWED_ORIGINS (CSV).
# Fallback = domínios conhecidos em produção.
# Se a env NÃO estiver setada, usa "*" para não quebrar ambientes locais/dev.
# ---------------------------------------------------------------------------
_cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
if _cors_env:
    _cors_origins: list[str] = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    # Fallback para domínios conhecidos em produção (sem wildcard).
    # Para dev local, sete CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8081
    _cors_origins = [
        "https://aumigaowalk.com.br",
        "https://www.aumigaowalk.com.br",
        # Admin-web na Vercel
        "https://admin-aumigao.vercel.app",
    ]

# Middlewares — ordem de add_middleware é de fora para dentro (LIFO na execução).
# RequestContextMiddleware fica mais externo para que o request_id esteja
# disponível para todos os outros middlewares e rotas.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    # Sec-fix: explicit allowlist replaces allow_headers=["*"].
    # Headers confirmed by grep on site/ and admin-web/ browser fetch calls:
    #   Authorization      — Bearer token (mobile app / BFF proxy)
    #   Content-Type       — JSON bodies
    #   X-Tenant-Slug      — build-dedicated tenant resolution (TenantResolverMiddleware)
    #   X-Tenant-Id        — direct tenant id override (TenantResolverMiddleware)
    #   X-Act-As-Tenant    — super_admin act-as impersonation (admin-web lib/api.ts)
    #   X-Request-ID       — correlation id (RequestContextMiddleware)
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Tenant-Slug",
        "X-Tenant-Id",
        "X-Act-As-Tenant",
        "X-Request-ID",
    ],
)
app.add_middleware(TenantResolverMiddleware, session_factory=SessionLocal)
app.add_middleware(RequestContextMiddleware)


# Sec-P3: headers de segurança defensivos injetados em TODAS as respostas.
# Colocado como @app.middleware após add_middleware (executa na camada de rota,
# depois dos middlewares ASGI registrados acima) para não colidir com CORS.
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # CSP estrito SÓ em produção: API JSON não renderiza páginas; default-src 'none'
    # nega tudo (inofensivo p/ JSON, cobre respostas HTML acidentais como erros).
    # Fica de fora em dev p/ não quebrar o Swagger /docs (que só existe fora de prod).
    if _is_production:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
    return response

UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

# Guard de uploads: avisa se o volume persistente não estiver configurado ou
# não for gravável. NÃO aborta o startup em hipótese alguma — apenas loga.
def _check_upload_volume() -> None:
    uploads_dir_env = os.getenv("UPLOADS_DIR")
    try:
        # Testa se o diretório é gravável com um arquivo temporário.
        _probe = UPLOAD_ROOT / ".upload_write_probe"
        _probe.write_text("ok")
        _probe.unlink()
        writable = True
    except Exception:
        writable = False

    if not uploads_dir_env or not writable:
        reasons = []
        if not uploads_dir_env:
            reasons.append("env UPLOADS_DIR não está definida")
        if not writable:
            reasons.append(f"diretório '{UPLOAD_ROOT}' não é gravável")
        logger.warning(
            "UPLOADS: %s. Arquivos de upload podem ser PERDIDOS em redeploy "
            "(disco efêmero). Para persistência, monte um volume e defina "
            "UPLOADS_DIR=/caminho/do/volume.",
            " e ".join(reasons),
        )
    else:
        logger.info("UPLOADS: volume configurado e gravável em '%s'.", UPLOAD_ROOT)


try:
    _check_upload_volume()
except Exception:
    # A própria checagem não pode crashar o boot.
    logger.warning(
        "UPLOADS: falha ao verificar o diretório de uploads ('%s'). "
        "Arquivos podem ser perdidos em redeploy se UPLOADS_DIR não estiver "
        "apontando para um volume persistente.",
        UPLOAD_ROOT,
    )


def _is_document_upload_path(upload_path: str) -> bool:
    """G5: documentos sensíveis recebem Content-Disposition: attachment.

    Prefixos que são documentos (não devem ser renderizados inline no browser):
    walker-documents (RG/CPF/endereço/selfie) e walk-completions (fotos de
    finalização — contêm dados do passeio). Fotos de perfil/pet ficam inline.
    """
    normalized = (upload_path or "").replace("\\", "/").lstrip("/")
    if normalized.startswith("uploads/"):
        normalized = normalized[len("uploads/"):]
    return normalized.startswith(("walker-documents/", "walk-completions/"))


@app.api_route("/uploads/{upload_path:path}", methods=["GET", "HEAD"])
def serve_upload(upload_path: str, request: Request):
    # Com R2 ligado (Cloud Run), serve do bucket; senão, do disco local (Railway/dev).
    if object_storage.r2_enabled():
        fetched = object_storage.fetch(upload_path)
        if fetched is None:
            raise HTTPException(status_code=404, detail="Arquivo nao encontrado")
        if is_sensitive_upload_path(upload_path) and not has_valid_upload_signature(upload_path, request.url.query):
            raise HTTPException(status_code=403, detail="Assinatura invalida ou expirada")
        body, content_type = fetched
        # G5: documentos forçam download; fotos de perfil/pet ficam inline.
        headers = {}
        if _is_document_upload_path(upload_path):
            headers["Content-Disposition"] = "attachment"
        return Response(content=body, media_type=content_type, headers=headers)

    file_path = upload_file_path(upload_path)
    if not file_path or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")
    if is_sensitive_upload_path(upload_path) and not has_valid_upload_signature(upload_path, request.url.query):
        raise HTTPException(status_code=403, detail="Assinatura invalida ou expirada")
    # G5: documentos forçam download; fotos de perfil/pet ficam inline.
    if _is_document_upload_path(upload_path):
        return FileResponse(file_path, headers={"Content-Disposition": "attachment"})
    return FileResponse(file_path)


# O serving de /uploads e feito exclusivamente pela rota serve_upload acima, que
# exige assinatura valida para arquivos sensiveis. Um StaticFiles montado aqui
# contornaria essa protecao, entao nao deve ser usado.

app.include_router(auth.router)
# Colisão de rota corrigida: admin_router do operational_walks deve ser registrado
# ANTES de admin.router para que /admin/walks/operational-metrics não seja capturado
# pelo paramétrico /admin/walks/{walk_id} do admin.router.
app.include_router(operational_walks.admin_router)
app.include_router(operational_walks.api_admin_router)
app.include_router(tutor.router)
app.include_router(pets.router)
app.include_router(walks.router)
app.include_router(notifications.router)
app.include_router(notifications.api_router)
app.include_router(protected_chat.router)
app.include_router(protected_chat.api_router)
app.include_router(operational_walks.router)
app.include_router(operational_walks.api_router)
app.include_router(walker.router)
app.include_router(walker.api_public_router)
app.include_router(partner_application.router)
app.include_router(walker_network.router)
app.include_router(walker_network.api_router)
app.include_router(walker_network.walker_router)
app.include_router(payments.router)
app.include_router(admin.router)
app.include_router(admin.api_router)
app.include_router(admin_accounts.router)
app.include_router(admin_accounts.api_router)
app.include_router(support_tickets.router)
app.include_router(support_tickets.api_router)
app.include_router(support_tickets.user_router)
app.include_router(support_tickets.api_user_router)
app.include_router(tenant_branding.router)
app.include_router(tenant_branding.api_router)
app.include_router(tenant_branding.admin_api_router)
app.include_router(tenant_commercial.router)
app.include_router(tenant_commercial.api_router)
app.include_router(tenant_features_runtime.router)
app.include_router(tenant_features_runtime.api_router)
app.include_router(tenant_units_runtime.router)
app.include_router(tenant_units_runtime.api_router)
app.include_router(tenant_app_config.router)
app.include_router(tenant_app_config.api_router)
app.include_router(tenant_dedicated_app_readiness.router)
app.include_router(tenant_dedicated_app_readiness.api_router)
app.include_router(tenant_launch_readiness.router)
app.include_router(tenant_launch_readiness.api_router)
app.include_router(tenants.router)
app.include_router(tenants.api_router)
app.include_router(recurring_plans.router)
app.include_router(recurring_plans.api_router)
app.include_router(recurring_plans.admin_router)
app.include_router(recurring_plans.api_admin_router)
app.include_router(pet_tour.router)
app.include_router(pet_tour.api_router)
app.include_router(pet_tour.admin_router)
app.include_router(pet_tour.api_admin_router)
app.include_router(tutor_referral_config.admin_router)
app.include_router(tutor_referral_config.api_admin_router)
app.include_router(tutor_referral_config.metrics_admin_router)
app.include_router(tutor_referral_config.metrics_api_router)
app.include_router(shared_walks.router)
app.include_router(shared_walks.api_router)
app.include_router(shared_walks.admin_router)
app.include_router(shared_walks.api_admin_router)
app.include_router(individual_walk_pricing.router)
app.include_router(individual_walk_pricing.api_router)
app.include_router(individual_walk_pricing.admin_router)
app.include_router(individual_walk_pricing.api_admin_router)
app.include_router(coupons.router)
app.include_router(coupons.api_router)
app.include_router(coupons.admin_router)
app.include_router(coupons.api_admin_router)
app.include_router(contact.router)
# Observabilidade: ingestão de erros do app mobile → Cloud Logging / Error Reporting BR.
app.include_router(client_errors.router)
# Incentivos: registrado ANTES de walker_quality para vencer a colisao de rota
# (/walker/me/incentives e /admin/walkers/{id}/incentives) com a versao gated + regras do tenant.
app.include_router(incentives.walker_router)
app.include_router(incentives.api_walker_router)
app.include_router(incentives.admin_router)
app.include_router(incentives.api_admin_router)
app.include_router(tutor_gamification.router)
app.include_router(tutor_gamification.api_router)
app.include_router(pet_routine.router)
app.include_router(pet_routine.api_router)
app.include_router(walker_trust.router)
app.include_router(walker_trust.api_router)
app.include_router(referrals.router)
app.include_router(referrals.api_router)
app.include_router(referrals.admin_router)
app.include_router(referrals.api_admin_router)
app.include_router(tutor_referrals.router)
app.include_router(tutor_referrals.api_router)
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
app.include_router(walker_ecosystem.walker_router)
app.include_router(walker_ecosystem.api_walker_router)
app.include_router(complaints.router)
app.include_router(complaints.api_router)
app.include_router(complaints.admin_router)
app.include_router(complaints.api_admin_router)
app.include_router(complaints.legacy_admin_occurrences_router)
app.include_router(complaints.api_legacy_admin_occurrences_router)
app.include_router(legal.router)
app.include_router(legal.api_router)
app.include_router(walk_locations.router)
app.include_router(walk_locations.api_router)
app.include_router(live_share.router)
app.include_router(live_share.api_router)
app.include_router(pet_share.public_router)
app.include_router(pet_share.bare_router)
app.include_router(pet_share.api_router)
# Fase B/5 (diário do tutor + stats): anexa suas rotas aos routers de pet_profile
# ANTES do include_router (que congela o conjunto de rotas).
from app.routes import pet_diary_routes  # noqa: E402,F401
# Fase E (comportamento multi-fonte + convivência): anexa suas rotas aos routers
# de pet_profile ANTES do include_router (que congela o conjunto de rotas).
from app.routes import pet_behavior_routes  # noqa: E402,F401
app.include_router(pet_profile.router)
app.include_router(pet_profile.api_router)
app.include_router(pet_profile.admin_router)
app.include_router(pet_profile.api_admin_router)
app.include_router(pet_profile.walk_obs_router)
app.include_router(pet_profile.api_walk_obs_router)
app.include_router(pet_health.router)
app.include_router(pet_health.api_router)
app.include_router(pet_health.walk_router)
app.include_router(pet_health.api_walk_router)
app.include_router(pet_self_walk.router)
app.include_router(pet_self_walk.api_router)
app.include_router(fiscal.router)
app.include_router(fiscal.api_router)
app.include_router(fiscal.payments_router)
app.include_router(fiscal.api_payments_router)

# ── /api/v1 — versionamento de contrato (api-T5). ADITIVO: as rotas atuais (sem
# versão) continuam servindo os apps em uso; /api/v1/* é a superfície ESTÁVEL para
# apps/integrações futuras apontarem sem risco de quebra de contrato. Monta os
# routers de NEGÓCIO (consumidos pelos apps) sob o prefixo versionado; rotas de
# console/admin internas ficam fora por ora. Cada router de negócio tem prefixo
# próprio (/auth, /payments, ...), então não há colisão /api/v1/api.
_v1_router = APIRouter(prefix="/api/v1")
for _v1_child in (
    auth.router, tutor.router, pets.router, walks.router, walk_locations.router,
    walker.router, walker_network.walker_router, payments.router, notifications.router,
    matching.router, pet_tour.router, recurring_plans.router, shared_walks.router,
    individual_walk_pricing.router, protected_chat.router,
):
    _v1_router.include_router(_v1_child)
app.include_router(_v1_router)

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
    # No Cloud Run (CPU throttled fora de requests), o loop in-process não roda
    # confiável. Setando DISABLE_INPROCESS_SCHEDULER=true, o ciclo é disparado
    # externamente pelo Cloud Scheduler em POST /internal/scheduler/run-cycle.
    # No Railway (sem essa env), o loop in-process roda como antes.
    if get_bool_env("DISABLE_INPROCESS_SCHEDULER", default=False):
        logger.info("Scheduler in-process desativado; usando trigger externo (Cloud Scheduler).")
        return
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


@app.post("/internal/scheduler/run-cycle")
async def internal_run_scheduler_cycle(request: Request):
    """Dispara UM ciclo do scheduler operacional. Protegido por token (X-Scheduler-Token).
    Usado pelo Cloud Scheduler no Cloud Run (onde o loop in-process é desativado)."""
    import hmac

    expected = (os.getenv("SCHEDULER_TRIGGER_TOKEN") or "").strip()
    provided = (request.headers.get("X-Scheduler-Token") or "").strip()
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="forbidden")
    return await run_operational_scheduler_cycle(SessionLocal)


@app.get("/")
def root():
    return {"message": "Aumigao Walk API rodando"}


@app.get("/health")
def health():
    # Sec-P3: resposta mínima — o Cloud Run só precisa de HTTP 200.
    # Campos "environment" e "database" foram removidos do endpoint PÚBLICO
    # para não vazar informações de infraestrutura. Detalhes internos disponíveis
    # apenas via /internal/health (autenticado) se necessário no futuro.
    import logging as _logging
    _health_logger = _logging.getLogger("app.health")
    db_status = "ok"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as _exc:
        # Banco indisponível: ainda retorna 200 para o health check do Cloud Run
        # não derrubar a instância (o banco pode ser transitório); o alerta de
        # indisponibilidade fica no Sentry/logs, não exposto publicamente.
        _health_logger.error("health_check_db_failed reason=%s", type(_exc).__name__)
        db_status = "down"
    return {"status": "ok", "db": db_status}

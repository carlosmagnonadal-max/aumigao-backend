import os
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models.operational_beta_log import OperationalBetaLog
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.push_token import PushToken
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_operational_event import WalkOperationalEvent
from app.models.walk_review import WalkReview
from app.models.walk_tip import WalkTip


ReadinessStatus = str

OK: ReadinessStatus = "OK"
ATTENTION: ReadinessStatus = "Atenção"
CRITICAL: ReadinessStatus = "Crítico"

ACCEPTED_PAYMENT_MODES = {"asaas_sandbox", "asaas_live"}
TIP_PROVIDER = "internal_mock"

CRITICAL_TABLES = {
    "users": User,
    "pets": Pet,
    "walks": Walk,
    "payments": Payment,
    "walk_completion_reviews": WalkCompletionReview,
    "walk_operational_events": WalkOperationalEvent,
    "walk_reviews": WalkReview,
    "walk_tips": WalkTip,
    "operational_beta_logs": OperationalBetaLog,
    "push_tokens": PushToken,
}


@dataclass
class ReadinessItem:
    key: str
    label: str
    status: ReadinessStatus
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "message": self.message,
        }


def _table_exists(db: Session, table_name: str) -> bool:
    try:
        return inspect(db.get_bind()).has_table(table_name)
    except Exception:
        return False


def _safe_count(db: Session, model: Any) -> tuple[bool, int | None, str | None]:
    try:
        return True, db.query(model).count(), None
    except Exception as exc:
        return False, None, str(exc)


def _safe_item(key: str, label: str, builder: Callable[[], ReadinessItem]) -> ReadinessItem:
    try:
        return builder()
    except Exception as exc:
        return ReadinessItem(
            key=key,
            label=label,
            status=CRITICAL,
            message=f"Checagem indisponível: {exc}",
        )


def _status_counts(items: list[ReadinessItem]) -> dict[str, int]:
    return {
        "ok": len([item for item in items if item.status == OK]),
        "attention": len([item for item in items if item.status == ATTENTION]),
        "critical": len([item for item in items if item.status == CRITICAL]),
    }


def _overall_status(items: list[ReadinessItem]) -> str:
    if any(item.status == CRITICAL for item in items):
        return "Não recomendado"
    if any(item.status == ATTENTION for item in items):
        return "Pronto com ressalvas"
    return "Pronto para operação"


def _overall_summary(overall: str, counts: dict[str, int]) -> str:
    if overall == "Não recomendado":
        return "Há dependências críticas indisponíveis para operar com segurança."
    if overall == "Pronto com ressalvas":
        return "A operação pode prosseguir com acompanhamento ativo dos pontos em atenção."
    return "Checklist operacional completo."


def build_beta_readiness_checklist(
    db: Session,
    beta_operational_health: dict[str, Any] | None = None,
    operational_observability: dict[str, Any] | None = None,
    operational_scheduler: dict[str, Any] | None = None,
    recovery_statuses: set[str] | None = None,
) -> dict[str, Any]:
    beta_operational_health = beta_operational_health or {}
    operational_observability = operational_observability or {}
    operational_scheduler = operational_scheduler or {}
    recovery_statuses = recovery_statuses or set()

    table_state = {table: _table_exists(db, table) for table in CRITICAL_TABLES}

    items: list[ReadinessItem] = [
        _safe_item(
            "backend_responding",
            "Backend respondendo",
            lambda: ReadinessItem(
                key="backend_responding",
                label="Backend respondendo",
                status=OK,
                message="Dashboard admin respondeu e executou as checagens de prontidão.",
            ),
        ),
        _safe_item(
            "database_connected",
            "Banco conectado",
            lambda: _database_item(db),
        ),
        _safe_item(
            "scheduler_active",
            "Scheduler ativo",
            lambda: _scheduler_item(operational_scheduler),
        ),
        _safe_item(
            "observability_active",
            "Observabilidade ativa",
            lambda: _observability_item(table_state, operational_observability),
        ),
        _safe_item(
            "dashboard_complete",
            "Dashboard operacional com dados completos",
            lambda: _dashboard_completeness_item(beta_operational_health),
        ),
        _safe_item(
            "critical_tables",
            "Tabelas críticas existentes",
            lambda: _critical_tables_item(table_state),
        ),
        _safe_item(
            "pending_completion_queue",
            "Fila de finalizações pendentes acessível",
            lambda: _queryable_table_item(
                db,
                table_state,
                "walk_completion_reviews",
                WalkCompletionReview,
                "pending_completion_queue",
                "Fila de finalizações pendentes acessível",
                "fila de finalizações",
            ),
        ),
        _safe_item(
            "operational_alerts",
            "Alertas operacionais acessíveis",
            lambda: _operational_alerts_item(db, table_state, recovery_statuses),
        ),
        _safe_item(
            "push_configured",
            "Push configurado",
            lambda: _push_item(db, table_state),
        ),
        _safe_item(
            "payment_provider",
            "Payment provider em modo esperado",
            lambda: _payment_provider_item(),
        ),
        _safe_item(
            "auditable_completion",
            "Finalização auditável ativa",
            lambda: _queryable_table_item(
                db,
                table_state,
                "walk_completion_reviews",
                WalkCompletionReview,
                "auditable_completion",
                "Finalização auditável ativa",
                "estrutura de revisão de finalização",
            ),
        ),
        _safe_item(
            "reviews_available",
            "Reviews disponíveis",
            lambda: _queryable_table_item(
                db,
                table_state,
                "walk_reviews",
                WalkReview,
                "reviews_available",
                "Reviews disponíveis",
                "avaliações pós-passeio",
            ),
        ),
        _safe_item(
            "tips_available",
            "Gorjetas disponíveis",
            lambda: _queryable_table_item(
                db,
                table_state,
                "walk_tips",
                WalkTip,
                "tips_available",
                "Gorjetas disponíveis",
                "gorjetas WalkTip",
            ),
        ),
        _safe_item(
            "recovery_available",
            "Recovery disponível",
            lambda: _recovery_item(db, table_state, recovery_statuses),
        ),
    ]

    counts = _status_counts(items)
    overall = _overall_status(items)
    return {
        "overall_status": overall,
        "summary": _overall_summary(overall, counts),
        "counts": counts,
        "items": [item.as_dict() for item in items],
    }


def _database_item(db: Session) -> ReadinessItem:
    try:
        db.execute(text("SELECT 1"))
        return ReadinessItem(
            key="database_connected",
            label="Banco conectado",
            status=OK,
            message="Conexão ativa validada com consulta direta.",
        )
    except Exception as exc:
        return ReadinessItem(
            key="database_connected",
            label="Banco conectado",
            status=CRITICAL,
            message=f"Banco indisponível para leitura operacional: {exc}",
        )


def _scheduler_item(operational_scheduler: dict[str, Any]) -> ReadinessItem:
    if operational_scheduler.get("scheduler_running"):
        last_cycle = operational_scheduler.get("last_scheduler_cycle_at") or "aguardando primeiro ciclo"
        return ReadinessItem(
            key="scheduler_active",
            label="Scheduler ativo",
            status=OK,
            message=f"Scheduler em execução; último ciclo: {last_cycle}.",
        )
    return ReadinessItem(
        key="scheduler_active",
        label="Scheduler ativo",
        status=ATTENTION,
        message="Scheduler não está ativo no processo atual; manter acompanhamento operacional.",
    )


def _observability_item(table_state: dict[str, bool], operational_observability: dict[str, Any]) -> ReadinessItem:
    if not table_state.get("operational_beta_logs"):
        return ReadinessItem(
            key="observability_active",
            label="Observabilidade ativa",
            status=ATTENTION,
            message="Tabela de logs operacionais ainda não está disponível.",
        )
    if operational_observability.get("data_available") is False:
        return ReadinessItem(
            key="observability_active",
            label="Observabilidade ativa",
            status=ATTENTION,
            message="Observabilidade respondeu, mas ainda está em modo parcial.",
        )
    return ReadinessItem(
        key="observability_active",
        label="Observabilidade ativa",
        status=OK,
        message="Logs operacionais disponíveis para leitura no dashboard.",
    )


def _dashboard_completeness_item(beta_operational_health: dict[str, Any]) -> ReadinessItem:
    availability = beta_operational_health.get("data_availability") or {}
    missing = [key for key, available in availability.items() if available is False]
    if missing:
        return ReadinessItem(
            key="dashboard_complete",
            label="Dashboard operacional com dados completos",
            status=ATTENTION,
            message=f"Dashboard em modo parcial: faltam {', '.join(missing)}.",
        )
    if availability:
        return ReadinessItem(
            key="dashboard_complete",
            label="Dashboard operacional com dados completos",
            status=OK,
            message="Todas as fontes operacionais do dashboard estão disponíveis.",
        )
    return ReadinessItem(
        key="dashboard_complete",
        label="Dashboard operacional com dados completos",
        status=ATTENTION,
        message="Dashboard respondeu sem mapa de disponibilidade detalhado.",
    )


def _critical_tables_item(table_state: dict[str, bool]) -> ReadinessItem:
    missing = [table for table, exists in table_state.items() if not exists]
    if missing:
        status = CRITICAL if any(table in {"users", "pets", "walks", "payments"} for table in missing) else ATTENTION
        return ReadinessItem(
            key="critical_tables",
            label="Tabelas críticas existentes",
            status=status,
            message=f"Estruturas ausentes: {', '.join(missing)}.",
        )
    return ReadinessItem(
        key="critical_tables",
        label="Tabelas críticas existentes",
        status=OK,
        message="Tabelas críticas do beta foram localizadas no banco.",
    )


def _queryable_table_item(
    db: Session,
    table_state: dict[str, bool],
    table_name: str,
    model: Any,
    key: str,
    label: str,
    subject: str,
) -> ReadinessItem:
    if not table_state.get(table_name):
        return ReadinessItem(
            key=key,
            label=label,
            status=CRITICAL,
            message=f"Tabela de {subject} indisponível.",
        )
    ok, count, error = _safe_count(db, model)
    if not ok:
        return ReadinessItem(
            key=key,
            label=label,
            status=CRITICAL,
            message=f"Consulta de {subject} falhou: {error}",
        )
    return ReadinessItem(
        key=key,
        label=label,
        status=OK,
        message=f"Leitura de {subject} acessível; registros atuais: {count}.",
    )


def _operational_alerts_item(db: Session, table_state: dict[str, bool], recovery_statuses: set[str]) -> ReadinessItem:
    if not table_state.get("walks"):
        return ReadinessItem(
            key="operational_alerts",
            label="Alertas operacionais acessíveis",
            status=CRITICAL,
            message="Tabela de passeios indisponível para alertas operacionais.",
        )
    if not recovery_statuses:
        return ReadinessItem(
            key="operational_alerts",
            label="Alertas operacionais acessíveis",
            status=ATTENTION,
            message="Status de recovery não foram carregados para leitura assistida.",
        )
    try:
        total = (
            db.query(Walk)
            .filter(Walk.operational_status.in_(list(recovery_statuses)))
            .count()
        )
        return ReadinessItem(
            key="operational_alerts",
            label="Alertas operacionais acessíveis",
            status=OK,
            message=f"Alertas operacionais acessíveis; itens em recovery: {total}.",
        )
    except Exception as exc:
        return ReadinessItem(
            key="operational_alerts",
            label="Alertas operacionais acessíveis",
            status=CRITICAL,
            message=f"Consulta de alertas operacionais falhou: {exc}",
        )


def _push_item(db: Session, table_state: dict[str, bool]) -> ReadinessItem:
    if not table_state.get("push_tokens"):
        return ReadinessItem(
            key="push_configured",
            label="Push configurado",
            status=ATTENTION,
            message="Tabela de tokens push indisponível; push fica limitado a in-app.",
        )
    ok, count, error = _safe_count(db, PushToken)
    if not ok:
        return ReadinessItem(
            key="push_configured",
            label="Push configurado",
            status=ATTENTION,
            message=f"Não foi possível consultar tokens push: {error}",
        )
    status = OK if count and count > 0 else ATTENTION
    message = (
        f"Push operacional com {count} token(s) registrado(s)."
        if count and count > 0
        else "Infraestrutura push disponível; nenhum token registrado ainda."
    )
    return ReadinessItem(
        key="push_configured",
        label="Push configurado",
        status=status,
        message=message,
    )


def _payment_provider_item() -> ReadinessItem:
    payment_mode = os.getenv("PAYMENT_MODE", "")
    if payment_mode not in ACCEPTED_PAYMENT_MODES:
        return ReadinessItem(
            key="payment_provider",
            label="Payment provider em modo esperado",
            status=ATTENTION,
            message=(
                f"PAYMENT_MODE atual é '{payment_mode or '(não definido)'}'; "
                f"valores aceitos: {', '.join(sorted(ACCEPTED_PAYMENT_MODES))}."
            ),
        )
    if payment_mode == "asaas_live":
        live_key = os.getenv("ASAAS_LIVE_API_KEY", "")
        if not live_key:
            return ReadinessItem(
                key="payment_provider",
                label="Payment provider em modo esperado",
                status=ATTENTION,
                message="PAYMENT_MODE=asaas_live, mas ASAAS_LIVE_API_KEY não está definida.",
            )
    return ReadinessItem(
        key="payment_provider",
        label="Payment provider em modo esperado",
        status=OK,
        message=f"Pagamento principal em {payment_mode}; gorjetas em {TIP_PROVIDER}.",
    )


def _recovery_item(db: Session, table_state: dict[str, bool], recovery_statuses: set[str]) -> ReadinessItem:
    if not recovery_statuses:
        return ReadinessItem(
            key="recovery_available",
            label="Recovery disponível",
            status=ATTENTION,
            message="Lista de status de recovery não está disponível.",
        )
    if not table_state.get("walk_operational_events"):
        return ReadinessItem(
            key="recovery_available",
            label="Recovery disponível",
            status=ATTENTION,
            message="Recovery existe, mas eventos operacionais não estão disponíveis para histórico.",
        )
    ok, count, error = _safe_count(db, WalkOperationalEvent)
    if not ok:
        return ReadinessItem(
            key="recovery_available",
            label="Recovery disponível",
            status=ATTENTION,
            message=f"Recovery disponível com histórico limitado: {error}",
        )
    return ReadinessItem(
        key="recovery_available",
        label="Recovery disponível",
        status=OK,
        message=f"Recovery operacional disponível; eventos registrados: {count}.",
    )

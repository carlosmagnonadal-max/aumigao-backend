"""pet_wellness_service.py — Índice de Bem-estar do pet (Perfil Vivo 2.0, Fase B).

Score composto 0-100, 100% RUNTIME: nada é persistido, não há migration. Reune
três componentes com DADO REAL da operação:

  - Clínico (peso 40): carteira de saúde (Fase A) — status das vacinas/vermífugo/
    antipulgas via pet_health_service.record_status; vacina pesa mais.
  - Rotina (peso 35): frequência de passeios nos últimos 30 dias — passeios PAGOS
    concluídos (dado transacional — Walk.status in WALK_COMPLETED_STATUSES; proxy
    created_at, mesmo critério do stats da Fase 5) MAIS os self-walks do tutor
    (Fase D — engajamento). O detail discrimina a mistura (X com passeador, Y do
    tutor). Self-walks NÃO contam nas conquistas da Fase C (transacionais).
  - Comportamento (peso 25): observações do passeador (WalkObservation) dos
    últimos 90 dias — base 100 com penalidades proporcionais à frequência de
    incidente/reatividade/ansiedade. Sem observações = neutro (70).

Tendência 30d: recomputa o score com um corte temporal (`as_of` = hoje-30d) e
compara — up/down/stable (limiar ±5). Todas as funções aceitam `as_of` para
permitir esse recálculo determinístico.

O campo `detail` de cada componente SEMPRE explica o porquê (acionável).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.constants import WALK_COMPLETED_STATUSES
from app.models.pet_health_record import PetHealthRecord
from app.models.walk import Walk
from app.models.walk_observation import WalkObservation
from app.services.pet_health_service import list_health_records, record_status

# ---------------------------------------------------------------------------
# Constantes de pesos e escalas (nomeadas — fáceis de calibrar)
# ---------------------------------------------------------------------------

# Pesos dos componentes do score final (somam 100).
WEIGHT_CLINICAL = 40
WEIGHT_ROUTINE = 35
WEIGHT_BEHAVIOR = 25

# Clínico — pontos por status de um kind (0-100).
CLINICAL_STATUS_POINTS = {
    "em_dia": 100,
    "vencendo": 60,
    "atrasada": 10,
    "sem_validade": 60,   # tratamento sem vencimento: neutro-positivo, não pune
}
# Pontos de AUSÊNCIA de registro, por kind. Vacina sem registro é grave (baixo);
# vermífugo/antipulgas sem registro é neutro (muitos tutores não trilham) — não
# pune quem só mantém a vacina em dia, mas registrar melhora.
CLINICAL_MISSING_POINTS = {
    "vaccine": 20,
    "dewormer": 70,
    "flea_tick": 70,
}
# Peso relativo de cada kind no componente clínico (vacina pesa mais).
CLINICAL_KIND_WEIGHTS = {
    "vaccine": 3,
    "dewormer": 1,
    "flea_tick": 1,
    # treatment não entra no score (não tem semântica de "em dia" de saúde base).
}
CLINICAL_KIND_PT = {
    "vaccine": "vacinas",
    "dewormer": "vermífugo",
    "flea_tick": "antipulgas/carrapatos",
}

# Rotina — passeios concluídos em 30d → pontuação (buckets, do maior pro menor).
ROUTINE_WINDOW_DAYS = 30
ROUTINE_BUCKETS = [
    (12, 100),
    (8, 90),
    (4, 70),
    (1, 40),
    (0, 0),
]

# Comportamento — janela + penalidades proporcionais à frequência (por passeio).
BEHAVIOR_WINDOW_DAYS = 90
BEHAVIOR_BASE = 100
BEHAVIOR_NEUTRAL = 70            # sem observações suficientes
BEHAVIOR_PENALTY_INCIDENT = 70   # peso da fração de passeios com incidente
BEHAVIOR_PENALTY_REACTIVE = 40   # fração reativa (socialization=reactive)
BEHAVIOR_PENALTY_ANXIOUS = 25    # fração ansiosa/agitada (mood)

# Tendência — limiar de delta para considerar up/down (senão stable).
TREND_WINDOW_DAYS = 30
TREND_THRESHOLD = 5

_ANXIOUS_MOODS = {"anxious", "agitated"}


# ---------------------------------------------------------------------------
# Rótulo por faixa
# ---------------------------------------------------------------------------

def score_label(score: int) -> str:
    """Rótulo pt-BR por faixa do score final."""
    if score >= 80:
        return "Ótimo"
    if score >= 60:
        return "Bom"
    if score >= 40:
        return "Atenção"
    return "Alerta"


# ---------------------------------------------------------------------------
# Componente clínico (carteira de saúde da Fase A)
# ---------------------------------------------------------------------------

def _worst_status_by_kind(records: list[PetHealthRecord], *, today: date) -> dict[str, str]:
    """Pior status por kind (atrasada > vencendo > sem_validade > em_dia)."""
    severity = {"atrasada": 3, "vencendo": 2, "sem_validade": 1, "em_dia": 0}
    worst: dict[str, str] = {}
    for r in records:
        st = record_status(r.valid_until, today=today)
        cur = worst.get(r.kind)
        if cur is None or severity.get(st, 0) > severity.get(cur, 0):
            worst[r.kind] = st
    return worst


def compute_clinical(db: Session, pet_id: str, *, as_of: date | None = None) -> dict:
    """Componente clínico (0-100) a partir da carteira, avaliada em `as_of`."""
    today = as_of or date.today()
    records = list_health_records(db, pet_id)
    worst = _worst_status_by_kind(records, today=today)

    weighted_sum = 0.0
    weight_total = 0
    fragments: list[str] = []
    for kind, weight in CLINICAL_KIND_WEIGHTS.items():
        status = worst.get(kind)
        if status is None:
            status = "sem_registro"
            points = CLINICAL_MISSING_POINTS[kind]
        else:
            points = CLINICAL_STATUS_POINTS.get(status, CLINICAL_MISSING_POINTS[kind])
        weighted_sum += points * weight
        weight_total += weight
        fragments.append(f"{CLINICAL_KIND_PT[kind]} {_status_pt(status)}")

    score = round(weighted_sum / weight_total) if weight_total else 0
    return {
        "key": "clinico",
        "label": "Clínico",
        "score": score,
        "weight": WEIGHT_CLINICAL,
        "detail": "; ".join(fragments),
    }


def _status_pt(status: str) -> str:
    return {
        "em_dia": "em dia",
        "vencendo": "vencendo",
        "atrasada": "atrasada",
        "sem_validade": "sem validade",
        "sem_registro": "sem registro",
    }.get(status, status)


# ---------------------------------------------------------------------------
# Componente rotina (passeios concluídos 30d)
# ---------------------------------------------------------------------------

def _routine_score_for_count(count: int) -> int:
    for threshold, points in ROUTINE_BUCKETS:
        if count >= threshold:
            return points
    return 0


def compute_routine(db: Session, pet_id: str, *, as_of: datetime | None = None) -> dict:
    """Componente rotina (0-100): passeios na janela de 30d até `as_of`.

    Soma os passeios PAGOS concluídos (dado transacional — proxy created_at, mesmo
    critério do stats da Fase 5) COM os self-walks do tutor (Fase D — engajamento),
    na MESMA janela. O `detail` discrimina a mistura quando há self-walks.

    Self-walks contam aqui (rotina/frequência), mas NÃO nas conquistas da Fase C
    (que são transacionais). Ver pet_achievement_service.
    """
    reference = as_of or datetime.utcnow()
    start = reference - timedelta(days=ROUTINE_WINDOW_DAYS)
    paid = (
        db.query(Walk)
        .filter(
            Walk.pet_id == pet_id,
            Walk.status.in_(list(WALK_COMPLETED_STATUSES)),
            Walk.created_at >= start,
            Walk.created_at <= reference,
        )
        .count()
    )
    # Import tardio evita ciclo (self_walk_service não depende do wellness).
    from app.services.pet_self_walk_service import count_in_window

    self_count = count_in_window(db, pet_id, start=start, end=reference)
    count = paid + self_count
    score = _routine_score_for_count(count)

    plural = "passeio" if count == 1 else "passeios"
    detail = f"{count} {plural} nos últimos {ROUTINE_WINDOW_DAYS} dias"
    if self_count:
        # Discrimina a mistura (ex.: "8 passeios no mês (5 com passeador, 3 do tutor)").
        detail += f" ({paid} com passeador, {self_count} do tutor)"
    return {
        "key": "rotina",
        "label": "Rotina",
        "score": score,
        "weight": WEIGHT_ROUTINE,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Componente comportamento (observações do passeador 90d)
# ---------------------------------------------------------------------------

def compute_behavior(db: Session, pet_id: str, *, as_of: datetime | None = None) -> dict:
    """Componente comportamento (0-100) das observações na janela de 90d até `as_of`.

    Base 100, penalidades proporcionais à FRAÇÃO de passeios com incidente,
    reatividade e ansiedade. Sem observações → neutro (70, "sem dados").
    """
    reference = as_of or datetime.utcnow()
    start = reference - timedelta(days=BEHAVIOR_WINDOW_DAYS)
    rows = (
        db.query(WalkObservation)
        .filter(
            WalkObservation.pet_id == pet_id,
            WalkObservation.created_at >= start,
            WalkObservation.created_at <= reference,
        )
        .all()
    )
    total = len(rows)
    if total == 0:
        return {
            "key": "comportamento",
            "label": "Comportamento",
            "score": BEHAVIOR_NEUTRAL,
            "weight": WEIGHT_BEHAVIOR,
            "detail": "sem dados suficientes de passeios ainda",
        }

    incidents = sum(1 for o in rows if o.incident)
    reactive = sum(1 for o in rows if o.socialization == "reactive")
    anxious = sum(1 for o in rows if o.mood in _ANXIOUS_MOODS)

    frac_incident = incidents / total
    frac_reactive = reactive / total
    frac_anxious = anxious / total

    penalty = (
        frac_incident * BEHAVIOR_PENALTY_INCIDENT
        + frac_reactive * BEHAVIOR_PENALTY_REACTIVE
        + frac_anxious * BEHAVIOR_PENALTY_ANXIOUS
    )
    score = max(0, min(100, round(BEHAVIOR_BASE - penalty)))

    # Detalhe acionável: aponta o principal ofensor (ou tranquilidade).
    fragments = [f"{total} passeios avaliados"]
    if incidents:
        fragments.append(f"{round(frac_incident * 100)}% com incidente")
    if reactive:
        fragments.append(f"{round(frac_reactive * 100)}% reativo")
    if anxious:
        fragments.append(f"{round(frac_anxious * 100)}% ansioso/agitado")
    if not (incidents or reactive or anxious):
        fragments.append("sem incidentes nem reatividade")

    return {
        "key": "comportamento",
        "label": "Comportamento",
        "score": score,
        "weight": WEIGHT_BEHAVIOR,
        "detail": "; ".join(fragments),
    }


# ---------------------------------------------------------------------------
# Composição + tendência
# ---------------------------------------------------------------------------

def _compose_score(components: list[dict]) -> int:
    """Média ponderada dos componentes pelos seus pesos (arredondada)."""
    weighted = sum(c["score"] * c["weight"] for c in components)
    total_weight = sum(c["weight"] for c in components)
    return round(weighted / total_weight) if total_weight else 0


def _score_at(db: Session, pet_id: str, *, at: datetime) -> int:
    """Score final composto avaliado num instante `at` (usado na tendência)."""
    components = [
        compute_clinical(db, pet_id, as_of=at.date()),
        compute_routine(db, pet_id, as_of=at),
        compute_behavior(db, pet_id, as_of=at),
    ]
    return _compose_score(components)


def _trend(current: int, previous: int) -> dict:
    delta = current - previous
    if delta > TREND_THRESHOLD:
        direction = "up"
    elif delta < -TREND_THRESHOLD:
        direction = "down"
    else:
        direction = "stable"
    return {"direction": direction, "delta": delta, "window_days": TREND_WINDOW_DAYS}


def compute_wellness(db: Session, pet_id: str, *, now: datetime | None = None) -> dict:
    """Payload completo do GET /wellness (runtime puro, sem persistência)."""
    reference = now or datetime.utcnow()
    components = [
        compute_clinical(db, pet_id, as_of=reference.date()),
        compute_routine(db, pet_id, as_of=reference),
        compute_behavior(db, pet_id, as_of=reference),
    ]
    score = _compose_score(components)

    # Tendência: mesmo cálculo com corte 30d atrás.
    past = reference - timedelta(days=TREND_WINDOW_DAYS)
    previous = _score_at(db, pet_id, at=past)
    trend = _trend(score, previous)

    return {
        "pet_id": pet_id,
        "score": score,
        "label": score_label(score),
        "trend": trend,
        "components": components,
        "computed_at": reference.isoformat(),
    }

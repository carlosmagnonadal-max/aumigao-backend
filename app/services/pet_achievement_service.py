"""pet_achievement_service.py — Conquistas do pet (Perfil Vivo 2.0, Fase C).

Marcos (badges) TRANSACIONAIS calculados 100% em RUNTIME — nada persistido, sem
migration (mesma filosofia do pet_wellness_service). Cada badge nasce de dado REAL:
passeios concluídos, carteira de saúde (Fase A) e completude da ficha.

CATÁLOGO estático por CATEGORIA (constantes abaixo), extensível no futuro por
serviço do white-label (creche/hotel/banho) — basta adicionar entradas numa nova
categoria e o serializer/rota já as expõem, sem tocar na infra.

Contrato de cada badge (dict):
  - key, category, label, description (pt-BR)
  - achieved: bool
  - achieved_at: str|None (ISO date) — só quando DERIVÁVEL BARATO (ex.: data do
    Nº-ésimo passeio concluído); senão null.
  - progress: {current, target, unit}
  - offer_hint: str|None — o gancho de OFERTA do tenant quando NÃO conquistada
    (ex.: "Faltam 3 passeios — que tal uma sequência semanal?"). Conquistada = null.

Regras de leitura de dados (mesmos critérios do wellness):
  - Passeio concluído: Walk.status in WALK_COMPLETED_STATUSES, proxy created_at.
  - "1ª memória": 1º passeio concluído COM foto de finalização. A foto vive em
    WalkCompletionReview.photo_url (join por walk_id) — consultável barato.
  - Saúde: carteira da Fase A (pet_health_service.record_status por kind).
  - Ficha completa: campos ricos do Pet (dieta, vet, emergência, peso).
  - Bem-estar ótimo: reusa pet_wellness_service.compute_wellness (score >= 80).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from app.constants import WALK_COMPLETED_STATUSES
from app.models.pet import Pet
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.services import pet_wellness_service as wellness
from app.services.pet_health_service import list_health_records, record_status, worst_status_by_kind

# ---------------------------------------------------------------------------
# Constantes de configuração (calibráveis)
# ---------------------------------------------------------------------------

# Metas de passeios concluídos por badge cumulativo.
WALKS_FIRST = 1
WALKS_EXPLORER = 10
WALKS_ADVENTURER = 50

# "Rotina do bem": nº de SEMANAS ISO consecutivas (cada uma com >=1 passeio).
ROUTINE_WEEKS_TARGET = 4

# "Bem-estar ótimo": limiar do score de bem-estar (Fase B).
WELLNESS_GREAT_THRESHOLD = 80

# Categorias (rótulo pt-BR + ordem de exibição por categoria).
CATEGORY_LABELS = {
    "passeios": "Passeios",
    "saude": "Saúde",
    "perfil": "Perfil",
}
CATEGORY_ORDER = {"passeios": 0, "saude": 1, "perfil": 2}


# ---------------------------------------------------------------------------
# Coleta de dados brutos (uma consulta por fonte, reaproveitada entre badges)
# ---------------------------------------------------------------------------

def _completed_walks(db: Session, pet_id: str) -> list[Walk]:
    """Passeios CONCLUÍDOS do pet, do mais antigo pro mais novo (por created_at).

    Ordem crescente permite achar barato a DATA do N-ésimo passeio (achieved_at).
    Proxy created_at (mesmo critério do wellness/stats — não há coluna de conclusão).
    """
    return (
        db.query(Walk)
        .filter(
            Walk.pet_id == pet_id,
            Walk.status.in_(list(WALK_COMPLETED_STATUSES)),
        )
        .order_by(Walk.created_at.asc())
        .all()
    )


def _first_memory_date(db: Session, pet_id: str) -> date | None:
    """Data do 1º passeio concluído COM foto de finalização, ou None.

    A foto de finalização vive em WalkCompletionReview.photo_url. Join enxuto por
    walk_id, filtrando passeios concluídos do pet com photo_url preenchida. Retorna
    a MAIS ANTIGA (menor created_at do walk) — a "primeira memória".
    """
    row = (
        db.query(Walk.created_at)
        .join(WalkCompletionReview, WalkCompletionReview.walk_id == Walk.id)
        .filter(
            Walk.pet_id == pet_id,
            Walk.status.in_(list(WALK_COMPLETED_STATUSES)),
            WalkCompletionReview.photo_url.isnot(None),
            WalkCompletionReview.photo_url != "",
        )
        .order_by(Walk.created_at.asc())
        .first()
    )
    if not row or row[0] is None:
        return None
    created = row[0]
    return created.date() if isinstance(created, datetime) else created


def _consecutive_iso_weeks(walks: list[Walk]) -> int:
    """Maior sequência de SEMANAS ISO consecutivas com >=1 passeio concluído.

    Cada passeio é mapeado à sua (ano-ISO, semana-ISO). Semanas consecutivas =
    diferença de 1 na numeração linear (ano*53 + semana), o que trata a virada de
    ano corretamente na prática (semanas ISO 1..52/53).
    """
    weeks: set[int] = set()
    for w in walks:
        ref = w.created_at
        d = ref.date() if isinstance(ref, datetime) else ref
        if d is None:
            continue
        iso = d.isocalendar()  # (year, week, weekday)
        weeks.add(iso[0] * 53 + iso[1])
    if not weeks:
        return 0
    ordered = sorted(weeks)
    best = run = 1
    for prev, cur in zip(ordered, ordered[1:]):
        run = run + 1 if cur == prev + 1 else 1
        best = max(best, run)
    return best


def _worst_status_by_kind(records, *, today: date) -> dict[str, str]:
    """Delega para o helper canônico em pet_health_service (fonte única da verdade)."""
    return worst_status_by_kind(records, today=today)


def _profile_completeness(pet: Pet) -> bool:
    """Ficha rica preenchida: dieta (type) + vet_name + emergency_contact + peso."""
    return bool(
        pet.diet_type
        and pet.vet_name
        and pet.emergency_contact
        and pet.weight is not None
    )


# ---------------------------------------------------------------------------
# Montagem de um badge (contrato uniforme)
# ---------------------------------------------------------------------------

def _badge(
    key: str, category: str, label: str, description: str, *,
    current: int, target: int, unit: str,
    achieved: bool, achieved_at: date | None, offer_hint: str,
) -> dict:
    """Monta o dict de UM badge no contrato final.

    - progress.current é limitado a target (não passa da meta na barra).
    - offer_hint só aparece quando NÃO conquistada (é o gancho de oferta).
    - achieved_at vira ISO só quando conquistada E derivável (senão null).
    """
    return {
        "key": key,
        "category": category,
        "label": label,
        "description": description,
        "achieved": achieved,
        "achieved_at": achieved_at.isoformat() if (achieved and achieved_at) else None,
        "progress": {"current": min(current, target), "target": target, "unit": unit},
        "offer_hint": None if achieved else offer_hint,
    }


def _nth_walk_date(walks: list[Walk], n: int) -> date | None:
    """Data (created_at) do N-ésimo passeio concluído (1-based), ou None."""
    if n <= 0 or len(walks) < n:
        return None
    ref = walks[n - 1].created_at
    if ref is None:
        return None
    return ref.date() if isinstance(ref, datetime) else ref


# ---------------------------------------------------------------------------
# Cálculo do catálogo completo
# ---------------------------------------------------------------------------

def compute_achievements(db: Session, pet: Pet, *, now: datetime | None = None) -> dict:
    """Payload do GET /achievements: summary + lista ordenada de badges.

    100% runtime. Ordenação: conquistadas primeiro (por categoria), depois as em
    progresso por PROXIMIDADE da meta (mais perto primeiro).
    """
    reference = now or datetime.utcnow()
    today = reference.date()

    walks = _completed_walks(db, pet.id)
    walk_count = len(walks)
    records = list_health_records(db, pet.id)
    worst = _worst_status_by_kind(records, today=today)

    badges: list[dict] = []

    # ── Passeios ────────────────────────────────────────────────────────────
    badges.append(_badge(
        "primeiro_passeio", "passeios", "Primeiro passeio",
        "O primeiro passeio concluído do pet.",
        current=walk_count, target=WALKS_FIRST, unit="passeios",
        achieved=walk_count >= WALKS_FIRST,
        achieved_at=_nth_walk_date(walks, WALKS_FIRST),
        offer_hint="Agende o primeiro passeio e comece a jornada do pet.",
    ))
    badges.append(_badge(
        "explorador", "passeios", "Explorador",
        "10 passeios concluídos.",
        current=walk_count, target=WALKS_EXPLORER, unit="passeios",
        achieved=walk_count >= WALKS_EXPLORER,
        achieved_at=_nth_walk_date(walks, WALKS_EXPLORER),
        offer_hint=f"Faltam {max(0, WALKS_EXPLORER - walk_count)} passeios — "
                   "que tal uma sequência semanal?",
    ))
    badges.append(_badge(
        "aventureiro", "passeios", "Aventureiro",
        "50 passeios concluídos.",
        current=walk_count, target=WALKS_ADVENTURER, unit="passeios",
        achieved=walk_count >= WALKS_ADVENTURER,
        achieved_at=_nth_walk_date(walks, WALKS_ADVENTURER),
        offer_hint=f"Faltam {max(0, WALKS_ADVENTURER - walk_count)} passeios "
                   "para a conquista Aventureiro.",
    ))

    weeks_streak = _consecutive_iso_weeks(walks)
    badges.append(_badge(
        "rotina_do_bem", "passeios", "Rotina do bem",
        "Passeios em 4 semanas seguidas.",
        current=weeks_streak, target=ROUTINE_WEEKS_TARGET, unit="semanas",
        achieved=weeks_streak >= ROUTINE_WEEKS_TARGET,
        achieved_at=None,  # não derivável barato (streak não tem "data de conquista")
        offer_hint="Mantenha um passeio por semana para criar rotina "
                   f"({weeks_streak}/{ROUTINE_WEEKS_TARGET} semanas).",
    ))

    memory_date = _first_memory_date(db, pet.id)
    badges.append(_badge(
        "primeira_memoria", "passeios", "Primeira memória",
        "O primeiro passeio concluído com foto de finalização.",
        current=1 if memory_date else 0, target=1, unit="fotos",
        achieved=memory_date is not None,
        achieved_at=memory_date,
        offer_hint="Peça a foto de finalização no próximo passeio "
                   "para guardar a primeira memória.",
    ))

    # ── Saúde (carteira Fase A) ─────────────────────────────────────────────
    vaccine_ok = worst.get("vaccine") == "em_dia"
    badges.append(_badge(
        "vacinas_em_dia", "saude", "Vacinas em dia",
        "Vacinação com validade em dia.",
        current=1 if vaccine_ok else 0, target=1, unit="status",
        achieved=vaccine_ok, achieved_at=None,
        offer_hint="Vacinação pendente — registre ou agende um reforço.",
    ))

    protection = all(worst.get(k) == "em_dia" for k in ("vaccine", "dewormer", "flea_tick"))
    badges.append(_badge(
        "protecao_total", "saude", "Proteção total",
        "Vacina, vermífugo e antipulgas todos em dia.",
        current=sum(1 for k in ("vaccine", "dewormer", "flea_tick") if worst.get(k) == "em_dia"),
        target=3, unit="trilhas",
        achieved=protection, achieved_at=None,
        offer_hint="Complete vacina, vermífugo e antipulgas para a proteção total.",
    ))

    has_health_record = len(records) >= 1
    badges.append(_badge(
        "primeiro_registro_saude", "saude", "Primeiro registro de saúde",
        "Ao menos um registro na carteira de saúde.",
        current=1 if has_health_record else 0, target=1, unit="registros",
        achieved=has_health_record, achieved_at=None,
        offer_hint="Registre a primeira vacina ou tratamento na carteira de saúde.",
    ))

    # ── Perfil ──────────────────────────────────────────────────────────────
    profile_ok = _profile_completeness(pet)
    badges.append(_badge(
        "perfil_completo", "perfil", "Perfil completo",
        "Ficha rica preenchida: dieta, veterinário, emergência e peso.",
        current=sum(1 for v in (pet.diet_type, pet.vet_name, pet.emergency_contact,
                                pet.weight is not None) if v),
        target=4, unit="campos",
        achieved=profile_ok, achieved_at=None,
        offer_hint="Preencha dieta, veterinário, contato de emergência e peso "
                   "para completar o perfil.",
    ))

    wellness_score = wellness.compute_wellness(db, pet.id, now=reference)["score"]
    wellness_ok = wellness_score >= WELLNESS_GREAT_THRESHOLD
    badges.append(_badge(
        "bem_estar_otimo", "perfil", "Bem-estar ótimo",
        "Índice de bem-estar em nível ótimo (80+).",
        current=wellness_score, target=WELLNESS_GREAT_THRESHOLD, unit="pontos",
        achieved=wellness_ok, achieved_at=None,
        offer_hint="Melhore rotina de passeios e saúde para elevar o "
                   f"bem-estar ({wellness_score}/{WELLNESS_GREAT_THRESHOLD}).",
    ))

    ordered = _order_badges(badges)
    achieved_n = sum(1 for b in ordered if b["achieved"])
    return {
        "pet_id": pet.id,
        "summary": {"achieved": achieved_n, "total": len(ordered)},
        "achievements": ordered,
        "computed_at": reference.isoformat(),
    }


def _order_badges(badges: list[dict]) -> list[dict]:
    """Ordena: conquistadas primeiro (por categoria), depois em progresso por proximidade.

    - Conquistadas: agrupadas por categoria (ordem passeios→saúde→perfil), key estável.
    - Em progresso: mais PERTO da meta primeiro (maior fração current/target).
    """
    def _proximity(b: dict) -> float:
        p = b["progress"]
        target = p["target"] or 1
        return p["current"] / target

    achieved = [b for b in badges if b["achieved"]]
    pending = [b for b in badges if not b["achieved"]]
    achieved.sort(key=lambda b: (CATEGORY_ORDER.get(b["category"], 99), b["key"]))
    pending.sort(key=lambda b: (-_proximity(b), CATEGORY_ORDER.get(b["category"], 99), b["key"]))
    return achieved + pending


def achievements_summary(db: Session, pet: Pet, *, now: datetime | None = None) -> dict:
    """Resumo compacto {achieved, total} — reuso do cálculo completo.

    Barato o suficiente para um pet único (rota/serializer de detalhe). NÃO usar em
    loop de lista grande (N pets) — ver nota no serializer admin.
    """
    full = compute_achievements(db, pet, now=now)
    return full["summary"]

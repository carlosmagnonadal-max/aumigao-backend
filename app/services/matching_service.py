import logging
import os
import unicodedata
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.models.pet import Pet
from app.models.walk import Walk
from app.models.walker_availability_exception import WalkerAvailabilityException
from app.models.walker_profile import WalkerProfile
from app.schemas.matching import MatchingWalkerRequest
from app.services.badge_service import generate_badges, generate_display_reason
from app.services.behavior_score_service import get_behavior_score
from app.services.boost_service import boost_score_for_walker
from app.services.reputation_service import DEFAULT_WALKER_PHOTO, calculate_hybrid_reputation_score, get_walker_identity, reputation_summary
from app.services.walker_availability_service import _covers
from app.services.walker_trust_service import compute_walker_trust

NEARBY_NEIGHBORHOODS = {
    "pituba": {"itaigara", "caminho das arvores", "costa azul", "amaralina"},
    "costa azul": {"pituba", "armacao", "jardim de alah"},
    "armacao": {"costa azul", "boca do rio", "jardim de alah"},
}
DEMO_MODE = os.getenv("EXPO_PUBLIC_DEMO_MODE", os.getenv("DEMO_MODE", "false")).strip().lower() in {"1", "true", "yes", "on"}


def clamp(value: float, min_value: float = 0, max_value: float = 100) -> float:
    return max(min_value, min(max_value, value))


def normalize(value: str | None) -> str:
    return (value or "").strip().lower()


# Wave 5 — ordem de porte para compatibilidade pet ↔ passeador.
_SIZE_RANK = {"pequeno": 1, "medio": 2, "grande": 3}


def size_rank(value: str | None) -> int | None:
    """Rank de porte: normaliza (lower + remove acento) e mapeia para 1/2/3.

    Retorna None para valor desconhecido/"" (ex.: "", "Gigante") — usado em
    FAIL-OPEN no matching para não derrubar recall quando o dado é incerto.
    """
    if not value:
        return None
    text = unicodedata.normalize("NFKD", value.strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _SIZE_RANK.get(text)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def walk_interval_conflict(walk: Walk, scheduled_at: datetime, duration_minutes: int, buffer_minutes: int = 15) -> bool:
    existing_start = parse_datetime(walk.scheduled_date)
    if not existing_start:
        return False
    existing_end = existing_start + timedelta(minutes=int(walk.duration_minutes or 0))
    new_end = scheduled_at + timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=buffer_minutes)
    return scheduled_at < existing_end + buffer and new_end + buffer > existing_start


def has_schedule_conflict(walker_id: str, request: MatchingWalkerRequest, db: Session) -> bool:
    scheduled_at = parse_datetime(request.scheduled_at)
    if not scheduled_at:
        return False

    # NOVO: exceção 'block' cobrindo o horário = indisponível (conta como conflito).
    # Filtro de tenant: block global (NULL) conflita sempre; block de tenant só conflita
    # quando o request é do mesmo tenant.
    block_q = db.query(WalkerAvailabilityException).filter(
        WalkerAvailabilityException.walker_user_id == walker_id,
        WalkerAvailabilityException.exception_date == scheduled_at.date(),
        WalkerAvailabilityException.kind == "block",
    )
    req_tenant = getattr(request, "tenant_id", None)
    if req_tenant is not None:
        block_q = block_q.filter(
            (WalkerAvailabilityException.tenant_id.is_(None))
            | (WalkerAvailabilityException.tenant_id == req_tenant)
        )
    else:
        block_q = block_q.filter(WalkerAvailabilityException.tenant_id.is_(None))
    blocks = block_q.all()
    if any(_covers(b, scheduled_at.strftime("%H:%M")) for b in blocks):
        return True

    active = (
        db.query(Walk)
        .filter(Walk.walker_id == walker_id, Walk.status.in_(["Agendado", "Indo buscar o pet", "Passeando agora"]))
        .all()
    )
    return any(walk_interval_conflict(walk, scheduled_at, request.duration_minutes) for walk in active)


def calculate_proximity_score(profile: WalkerProfile, request: MatchingWalkerRequest) -> tuple[float, float | None]:
    request_city = normalize(request.city)
    walker_city = normalize(profile.city)
    request_neighborhood = normalize(request.neighborhood)
    walker_area = normalize(profile.state)

    if request_city and walker_city and request_city != walker_city:
        return 0.0, None
    if request_neighborhood and walker_area and request_neighborhood == walker_area:
        return 90.0, 1.6
    if request_neighborhood and walker_area and walker_area in NEARBY_NEIGHBORHOODS.get(request_neighborhood, set()):
        return 70.0, 3.8
    if request_city and walker_city and request_city == walker_city:
        return 50.0, 7.5
    return 65.0, 5.0


def calculate_rating_score(summary: dict) -> float:
    if summary["reviews_count"] == 0:
        return 75.0
    return clamp((summary["rating_average"] / 5) * 100)


def calculate_experience_score(total_walks: int) -> float:
    if total_walks >= 80:
        return 100.0
    if total_walks >= 30:
        return 85.0
    if total_walks >= 10:
        return 70.0
    if total_walks >= 5:
        return 55.0
    return 40.0


def _matching_requires_online() -> bool:
    """WK-10: gate de produto. Quando ligado, passeador offline não entra no pool.
    Default DESLIGADO para não mudar o matching em produção sem decisão explícita."""
    return os.getenv("MATCHING_REQUIRE_ONLINE", "false").strip().lower() in {"1", "true", "yes", "on"}


def passes_online_gate(profile: WalkerProfile) -> bool:
    """WK-10: True se o passeador pode entrar no pool dado o gate de presença."""
    if not _matching_requires_online():
        return True
    return bool(getattr(profile, "is_online", False))


def calculate_availability_score(profile: WalkerProfile, request: MatchingWalkerRequest, db: Session) -> float:
    if has_schedule_conflict(profile.user_id, request, db):
        return 0.0
    base = 100.0 if request.scheduled_at else 80.0
    # WK-10: presença real entra no score — deixa de ser constante. Offline pesa
    # metade (continua elegível salvo se o gate MATCHING_REQUIRE_ONLINE estiver ligado).
    if not getattr(profile, "is_online", False):
        base = round(base * 0.5, 2)
    return base


def calculate_base_matching_score(proximity_score: float, rating_score: float, experience_score: float, availability_score: float) -> float:
    return round(proximity_score * 0.40 + rating_score * 0.30 + experience_score * 0.20 + availability_score * 0.10, 2)


def calculate_final_matching_score(base_score: float, behavior_score: float, boost_score: float) -> float:
    return round(clamp(base_score * 0.60 + behavior_score * 0.40 + boost_score), 2)


def risk_visibility_adjustment(risk_level: str) -> float:
    if risk_level == "critical":
        return -15.0
    if risk_level == "risk":
        return -7.0
    if risk_level == "attention":
        return -2.0
    return 0.0


def get_eligible_walkers(request: MatchingWalkerRequest, db: Session, tenant_id: str | None = None) -> list[WalkerProfile]:
    query = db.query(WalkerProfile).filter(
        WalkerProfile.status == "active",
        WalkerProfile.active_as_walker.is_(True),
    )
    # Pet Tour exige passeador com carro.
    if getattr(request, "modality", "standard") == "pet_tour":
        query = query.filter(WalkerProfile.has_vehicle.is_(True))
    profiles = query.order_by(WalkerProfile.created_at.desc()).all()
    # C11/mt-MT3: quando há tenant, a PREVIEW respeita o MESMO pool da rede usado pela
    # alocação vinculante — a vitrine não vaza passeadores de fora da rede do tenant
    # (nem convidados pending/declined). Sem tenant_id mantém o comportamento legado.
    tenant_pool: set[str] | None = None
    if tenant_id:
        from app.services.walker_network_matching_service import get_matching_pool_for_tenant
        try:
            tenant_pool = set(get_matching_pool_for_tenant(db, tenant_id))
        except Exception as _pool_exc:
            logger.warning(
                "matching_pool_load_failed tenant_id=%s reason=%s — falling back to empty pool",
                tenant_id, type(_pool_exc).__name__,
            )
            tenant_pool = set()
    # Wave 5 — porte do pet (carregado uma vez, de forma segura). FAIL-OPEN:
    # se não há pet_id, o pet não existe, ou o porte é desconhecido, pet_size_rank
    # fica None e NÃO filtramos por porte (mantém recall).
    pet_size_rank: int | None = None
    pet_id = getattr(request, "pet_id", None)
    if pet_id:
        try:
            pet = db.get(Pet, pet_id)
        except Exception as _pet_exc:
            logger.warning(
                "matching_pet_load_failed pet_id=%s reason=%s — skipping size filter",
                pet_id, type(_pet_exc).__name__,
            )
            pet = None
        if pet is not None:
            pet_size_rank = size_rank(getattr(pet, "size", None))
    eligible = []
    seen_keys = set()
    for profile in profiles:
        dedupe_key = profile.cpf or profile.user_id or profile.id
        if not profile.user_id or not dedupe_key or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        if tenant_pool is not None and profile.user_id not in tenant_pool:
            continue
        # WK-10: gate de presença (ligável por flag) — offline fora do pool quando ligado.
        if not passes_online_gate(profile):
            continue
        # Wave 5 — compatibilidade de porte. Só filtra quando AMBOS os ranks são
        # conhecidos (FAIL-OPEN caso contrário). Exclui se o passeador aceita um
        # porte máximo menor que o do pet.
        if pet_size_rank is not None:
            walker_max_rank = size_rank(getattr(profile, "max_dog_size", None))
            if walker_max_rank is not None and walker_max_rank < pet_size_rank:
                continue
        proximity_score, _ = calculate_proximity_score(profile, request)
        if proximity_score <= 0:
            continue
        if calculate_availability_score(profile, request, db) <= 0:
            continue
        # (Removida guarda de risk_level == "suspended": inalcancavel aqui, pois a query
        # ja exige status == "active" e "suspended" so ocorre com status suspended/blocked.)
        eligible.append(profile)
    return eligible


def matched_walker_payload(profile: WalkerProfile, request: MatchingWalkerRequest, db: Session) -> dict:
    summary = reputation_summary(profile.user_id, db)
    proximity_score, distance_km = calculate_proximity_score(profile, request)
    rating_score = calculate_rating_score(summary)
    experience_score = calculate_experience_score(summary["total_walks"])
    availability_score = calculate_availability_score(profile, request, db)
    base_score = calculate_base_matching_score(proximity_score, rating_score, experience_score, availability_score)
    behavior_details = get_behavior_score(profile.user_id, db)
    hybrid_details = calculate_hybrid_reputation_score(profile.user_id, db)
    combined_behavior_score = round(behavior_details["behavior_score"] * 0.45 + hybrid_details["hybrid_reputation_score"] * 0.55, 2)
    boost_score = boost_score_for_walker(profile, profile.user_id, db)
    final_score = clamp(calculate_final_matching_score(base_score, combined_behavior_score, boost_score) + risk_visibility_adjustment(hybrid_details["risk_level"]))
    identity = get_walker_identity(profile.user_id, db)
    # Confianca (selos/certificacoes/nivel) do passeador, exposta ao tutor.
    # Gating de EXIBICAO e responsabilidade do front (decisao da spec), nao aqui.
    trust = compute_walker_trust(db, profile.user_id)

    return {
        "walker_id": profile.user_id,
        "name": identity["name"],
        "photo_url": identity["photo"] or "",
        "rating_average": summary["rating_average"],
        "reviews_count": summary["reviews_count"],
        "total_walks": summary["total_walks"],
        "level": summary["level"],
        "trust": trust,
        "distance_km": distance_km,
        "estimated_arrival_minutes": int((distance_km or 4) * 5) + 4,
        "can_select": True,
        "proximity_score": proximity_score,
        "rating_score": rating_score,
        "experience_score": experience_score,
        "availability_score": availability_score,
        "matching_score_base": base_score,
        "behavior_score": round(combined_behavior_score, 2),
        "behavior_details": {**behavior_details, "hybrid_reputation_score": hybrid_details["hybrid_reputation_score"]},
        "boost_score": boost_score,
        "final_matching_score": round(final_score, 2),
        "risk_level": hybrid_details["risk_level"],
        "eligibility_notes": ["approved", "agenda_compativel", "gorjeta_excluida_do_score"],
    }


def demo_matching_response(request: MatchingWalkerRequest, debug: bool = False) -> dict:
    items = [
        {
            "walker_id": "walker-1",
            "name": "Carlos Oliveira",
            "photo_url": DEFAULT_WALKER_PHOTO,
            "rating_average": 4.9,
            "reviews_count": 126,
            "total_walks": 38,
            "level": "Ouro",
            "distance_km": 1.8,
            "estimated_arrival_minutes": 12,
            "badges": ["Mais recomendado", "Perto de voce", "Destaque da regiao"],
            "display_reason": "Otima avaliacao e perto de voce",
            "can_select": True,
            "proximity_score": 90,
            "rating_score": 98,
            "experience_score": 85,
            "availability_score": 100,
            "matching_score_base": 92.4,
            "behavior_score": 86,
            "boost_score": 0,
            "final_matching_score": 89.44,
            "eligibility_notes": ["demo", "approved"],
        },
        {
            "walker_id": "walker-2",
            "name": "Ana Martins",
            "photo_url": DEFAULT_WALKER_PHOTO,
            "rating_average": 4.8,
            "reviews_count": 64,
            "total_walks": 82,
            "level": "Diamante",
            "distance_km": 3.6,
            "estimated_arrival_minutes": 18,
            "badges": ["Mais experiente", "Responde rapido"],
            "display_reason": "Passeador experiente na sua regiao",
            "can_select": True,
            "proximity_score": 70,
            "rating_score": 96,
            "experience_score": 100,
            "availability_score": 100,
            "matching_score_base": 86.8,
            "behavior_score": 88,
            "boost_score": 2,
            "final_matching_score": 89.28,
            "eligibility_notes": ["demo", "approved"],
        },
        {
            "walker_id": "walker-3",
            "name": "Bruno Costa",
            "photo_url": DEFAULT_WALKER_PHOTO,
            "rating_average": 4.6,
            "reviews_count": 18,
            "total_walks": 22,
            "level": "Prata",
            "distance_km": 5.2,
            "estimated_arrival_minutes": 28,
            "badges": ["Responde rapido"],
            "display_reason": "Boa disponibilidade para este horario",
            "can_select": True,
            "proximity_score": 50,
            "rating_score": 92,
            "experience_score": 70,
            "availability_score": 100,
            "matching_score_base": 71.6,
            "behavior_score": 82,
            "boost_score": 0,
            "final_matching_score": 75.76,
            "eligibility_notes": ["demo", "approved"],
        },
    ]
    context = {
        "city": request.city,
        "neighborhood": request.neighborhood,
        "scheduled_at": request.scheduled_at,
        "duration_minutes": request.duration_minutes,
    }
    if debug:
        return {"items": items, "total_found": len(items), "matching_context": context}
    public_items = [{k: v for k, v in item.items() if not k.endswith("_score") and k not in {"behavior_details", "matching_score_base", "final_matching_score", "eligibility_notes"}} for item in items]
    return {"top_recommended": public_items[:3], "other_options": public_items[3:], "total_found": len(items), "matching_context": context}


def rank_walkers(request: MatchingWalkerRequest, db: Session, debug: bool = False, tenant_id: str | None = None) -> dict:
    profiles = get_eligible_walkers(request, db, tenant_id=tenant_id)
    if not profiles:
        if DEMO_MODE:
            return demo_matching_response(request, debug=debug)
        context = {
            "city": request.city,
            "neighborhood": request.neighborhood,
            "scheduled_at": request.scheduled_at,
            "duration_minutes": request.duration_minutes,
        }
        if debug:
            return {"items": [], "total_found": 0, "matching_context": context}
        return {"top_recommended": [], "other_options": [], "total_found": 0, "matching_context": context}

    items = [matched_walker_payload(profile, request, db) for profile in profiles]
    best_rating = max((item["rating_average"] for item in items), default=0)
    most_walks = max((item["total_walks"] for item in items), default=0)
    items.sort(
        key=lambda item: (
            item["final_matching_score"],
            item["rating_average"],
            item["proximity_score"],
            item["total_walks"],
            item["availability_score"],
        ),
        reverse=True,
    )
    for index, item in enumerate(items, start=1):
        badges = generate_badges(item, index, {"best_rating": best_rating, "most_walks": most_walks})
        item["badges"] = badges
        item["display_reason"] = generate_display_reason(item, badges)

    context = {
        "city": request.city,
        "neighborhood": request.neighborhood,
        "scheduled_at": request.scheduled_at,
        "duration_minutes": request.duration_minutes,
    }
    if debug:
        return {"items": items, "total_found": len(items), "matching_context": context}

    public_items = [
        {
            "walker_id": item["walker_id"],
            "name": item["name"],
            "photo_url": item["photo_url"],
            "rating_average": item["rating_average"],
            "reviews_count": item["reviews_count"],
            "total_walks": item["total_walks"],
            "level": item["level"],
            "distance_km": item["distance_km"],
            "estimated_arrival_minutes": item["estimated_arrival_minutes"],
            "badges": item["badges"],
            "display_reason": item["display_reason"],
            "can_select": item["can_select"],
            "trust": item["trust"],
        }
        for item in items
    ]
    return {
        "top_recommended": public_items[:3],
        "other_options": public_items[3:],
        "total_found": len(public_items),
        "matching_context": context,
    }

"""local_rules_service — regras locais × ficha do pet × local do passeio (S3).

Spec: docs/superpowers/specs/2026-09-15-seguranca-do-passeio-design.md §3.
Plano: docs/superpowers/plans/2026-09-15-s3-regras-locais-limite-caes.md (DV1–DV7).

Local do passeio (DV1) — `walks` não tem cidade estruturada. Ordem:
  1. meeting_point com "Cidade - UF" / "Cidade/UF";
  2. TutorProfile.city/state do tutor;
  3. o mesmo padrão em address_snapshot (passeios antigos);
  4. cidade das unidades ATIVAS do tenant, se todas forem a mesma.
Nada resolvido → sem alerta (vale o padrão nacional do manual).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.models.local_rule import (
    CRITERIO_BREED_LIST,
    CRITERIO_REACTIVE,
    CRITERIO_WEIGHT_MIN_KG,
    PET_CRITERIA,
    STATUS_CONFLITO,
    STATUS_TRAMITACAO,
    STATUS_VIGENTE,
    TEMA_LIMITE_CAES,
    LocalRule,
)
from app.models.pet import Pet
from app.models.tenant import TenantUnit
from app.models.tutor_profile import TutorProfile
from app.models.walk import Walk

logger = logging.getLogger(__name__)

_UF_BY_NAME = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA", "ceara": "CE",
    "distrito federal": "DF", "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
    "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG", "para": "PA",
    "paraiba": "PB", "parana": "PR", "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
    "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO",
}
UFS = frozenset(_UF_BY_NAME.values())

# "<cidade> - UF" ou "<cidade>/UF"; cidade sem vírgula/ponto-e-vírgula/·/parênteses/dígitos.
_CITY_UF_RE = re.compile(r"([^,;·()\d]+?)\s*(?:/|\s-\s)\s*([A-Za-z]{2})(?![A-Za-z])")


def normalize_text(value) -> str:
    """Minúsculas, sem acento, espaços colapsados. None → ''."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_only.lower().split())


def normalize_uf(value) -> str | None:
    """Sigla válida ('BA') a partir de sigla ou nome por extenso; senão None."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.upper() in UFS:
        return raw.upper()
    return _UF_BY_NAME.get(normalize_text(raw))


def parse_city_uf(text) -> tuple[str | None, str | None]:
    """Extrai (cidade, UF) de um endereço em texto livre. Usa a ÚLTIMA ocorrência válida.

    Descarta candidatos cujo último "token" antes do separador tem 1 letra só
    (ex.: "Bloco B/AP", "Ed. Solar - Bloco B/AP 101") — referência de
    bloco/apartamento, não nome de cidade. Ajuste do implementador (Task 3):
    a regex do plano, sozinha, casava esse texto como cidade="Bloco B"/UF="AP"
    e falhava no próprio teste do plano (test_parse_city_uf).
    """
    if not text:
        return None, None
    found: tuple[str | None, str | None] = (None, None)
    for match in _CITY_UF_RE.finditer(str(text)):
        uf = normalize_uf(match.group(2))
        city = re.split(r"\s-\s", match.group(1))[-1].strip(" .—-")
        last_token = city.split()[-1] if city else ""
        if uf and city and len(last_token) > 1:
            found = (city, uf)
    return found


def _tutor_profile_location(db: Session, tutor_id: str | None) -> tuple[str | None, str | None]:
    if not tutor_id:
        return None, None
    profile = db.query(TutorProfile).filter(TutorProfile.user_id == tutor_id).first()
    if not profile:
        return None, None
    city = (profile.city or "").strip()
    uf = normalize_uf(profile.state)
    return (city, uf) if city and uf else (None, None)


def _tenant_units_location(db: Session, tenant_id: str | None) -> tuple[str | None, str | None]:
    if not tenant_id:
        return None, None
    units = (
        db.query(TenantUnit)
        .filter(TenantUnit.tenant_id == tenant_id, TenantUnit.status == "active")
        .all()
    )
    distinct: dict[tuple[str, str], tuple[str, str]] = {}
    for unit in units:
        city = (unit.city or "").strip()
        uf = normalize_uf(unit.state)
        if city and uf:
            distinct.setdefault((normalize_text(city), uf), (city, uf))
    if len(distinct) == 1:
        return next(iter(distinct.values()))
    return None, None


def resolve_tutor_location(db: Session, tutor_id: str | None, tenant_id: str | None) -> tuple[str | None, str | None]:
    """Local de referência de um tutor (passeio compartilhado): perfil → unidades do tenant."""
    city, uf = _tutor_profile_location(db, tutor_id)
    if city:
        return city, uf
    return _tenant_units_location(db, tenant_id)


def resolve_walk_location(db: Session, walk: Walk) -> tuple[str | None, str | None]:
    """(município, UF) do passeio — ordem documentada no topo do módulo (DV1)."""
    if (walk.meeting_point or "").strip():
        city, uf = parse_city_uf(walk.meeting_point)
        if city:
            return city, uf
    city, uf = _tutor_profile_location(db, walk.tutor_id)
    if city:
        return city, uf
    city, uf = parse_city_uf(walk.address_snapshot)
    if city:
        return city, uf
    return _tenant_units_location(db, walk.tenant_id)


_LARGE_SIZE_LABELS = frozenset({"grande", "gigante"})


@dataclass(frozen=True)
class SafetyAlert:
    rule_id: str
    uf: str
    municipio: str | None
    tema: str
    severity: str
    title: str
    message: str
    norma: str
    fonte_url: str | None
    status: str
    verificado_em: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _load_json(value: str | None, *, rule_id: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        logger.warning("[local_rules] JSON inválido na regra %s", rule_id)
        return {}
    return data if isinstance(data, dict) else {}


def rules_for_city(
    db: Session,
    municipio: str | None,
    uf: str | None,
    *,
    include_tramitacao: bool = False,
    cache: dict | None = None,
) -> list[LocalRule]:
    """Regras municipais da cidade + estaduais da UF (vigente e conflito; tramitação opcional).

    Usado pelo S2 (M10-B "Regras da sua cidade"). Municipais primeiro, depois
    estaduais; dentro de cada grupo por tema e id. `cache` evita repetir a
    consulta numa listagem (chave = cidade normalizada + UF + flag).
    """
    uf_norm = normalize_uf(uf)
    if not uf_norm:
        return []
    city_norm = normalize_text(municipio)
    key = (city_norm, uf_norm, include_tramitacao)
    if cache is not None and key in cache:
        return cache[key]
    statuses = [STATUS_VIGENTE, STATUS_CONFLITO]
    if include_tramitacao:
        statuses.append(STATUS_TRAMITACAO)
    rows = db.query(LocalRule).filter(LocalRule.uf == uf_norm, LocalRule.status.in_(statuses)).all()
    result = [
        rule for rule in rows
        if rule.municipio is None or (city_norm and normalize_text(rule.municipio) == city_norm)
    ]
    result.sort(key=lambda rule: (rule.municipio is None, rule.tema, rule.id))
    if cache is not None:
        cache[key] = result
    return result


def pet_is_reactive(pet: Pet) -> bool:
    """Reativo declarado (chip → is_reactive) ou ficha expandida behavior_with_dogs='reativo' (DV7)."""
    return bool(getattr(pet, "is_reactive", False)) or normalize_text(getattr(pet, "behavior_with_dogs", None)) == "reativo"


def _format_kg(weight: float) -> str:
    return f"{float(weight):g}".replace(".", ",")


def _join_pt(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " e " + items[-1]


def _match_reason(rule: LocalRule, pet: Pet, params: dict) -> str | None:
    if rule.criterio == CRITERIO_WEIGHT_MIN_KG:
        try:
            min_kg = float(params["min_kg"])
        except (KeyError, TypeError, ValueError):
            return None
        # inclusive=false → "acima de N kg" (Lei 9.108/2016 Salvador: > 24). Padrão: inclusivo.
        inclusive = params.get("inclusive", True) is not False
        if pet.weight is not None and float(pet.weight) > 0:
            weight = float(pet.weight)
            matches = weight >= min_kg if inclusive else weight > min_kg
            return f"cão de {_format_kg(weight)} kg" if matches else None
        fallback = {normalize_text(size) for size in params.get("size_fallback", []) if isinstance(size, str)}
        size = normalize_text(pet.size)
        if size and size in fallback and size in _LARGE_SIZE_LABELS:
            return f"cão de porte {size}"
        return None
    if rule.criterio == CRITERIO_REACTIVE:
        return "cão reativo" if pet_is_reactive(pet) else None
    if rule.criterio == CRITERIO_BREED_LIST:
        breeds = {normalize_text(b) for b in params.get("breeds", []) if isinstance(b, str)}
        breed = normalize_text(pet.breed)
        return f"raça {(pet.breed or '').strip()}" if breed and breed in breeds else None
    return None


def alerts_for(
    db: Session,
    pet: Pet | None,
    municipio: str | None,
    uf: str | None,
    *,
    cache: dict | None = None,
) -> list[SafetyAlert]:
    """Alertas ao passeador: regras VIGENTES com critério de pet que casam com a ficha.

    Regras de local (praia/afluxo/dejetos) e `conflito` nunca viram alerta
    categórico (DV6) — ficam em rules_for_city para o M10-B.
    """
    if pet is None:
        return []
    alerts: list[SafetyAlert] = []
    for rule in rules_for_city(db, municipio, uf, cache=cache):
        if rule.status != STATUS_VIGENTE or rule.criterio not in PET_CRITERIA:
            continue
        reason = _match_reason(rule, pet, _load_json(rule.params_json, rule_id=rule.id))
        if not reason:
            continue
        exigencia = _load_json(rule.exigencia_json, rule_id=rule.id)
        itens = [str(item).strip() for item in exigencia.get("itens", []) if str(item).strip()]
        onde = str(exigencia.get("onde") or "").strip()
        requirement = " ".join(part for part in (_join_pt(itens), onde) if part)
        local = rule.municipio or rule.uf
        message = f"{local}: {reason} — {requirement}." if requirement else f"{local}: {reason} — confira a regra local."
        alerts.append(SafetyAlert(
            rule_id=rule.id,
            uf=rule.uf,
            municipio=rule.municipio,
            tema=rule.tema,
            severity="warning",
            title=f"Regra local · {local}",
            message=message,
            norma=rule.norma,
            fonte_url=rule.fonte_url,
            status=rule.status,
            verificado_em=rule.verificado_em.isoformat() if rule.verificado_em else None,
        ))
    return alerts


def safety_alerts_for_walk(db: Session, walk: Walk, *, cache: dict | None = None) -> list[dict]:
    """`safety_alerts` do payload do passeador: pet do passeio × regras do local resolvido."""
    pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
    if pet is None:
        return []
    municipio, uf = resolve_walk_location(db, walk)
    if not uf:
        return []
    return [alert.to_dict() for alert in alerts_for(db, pet, municipio, uf, cache=cache)]


def national_max_dogs_per_walker(config) -> int:
    """Padrão nacional DERIVADO da regra atual do compartilhado (DV3): tutores × pets por tutor.

    Informativo (M10-B/S2). NÃO é aplicado como teto novo — sem override nada muda.
    """
    return int(config.max_tutors) * int(config.max_pets_same_tutor)


def local_max_dogs_override(db: Session, municipio: str | None, uf: str | None) -> int | None:
    """Menor `max_dogs` entre regras `limite_caes` VIGENTES da cidade/UF; None se não houver."""
    values: list[int] = []
    for rule in rules_for_city(db, municipio, uf):
        if rule.tema != TEMA_LIMITE_CAES or rule.status != STATUS_VIGENTE:
            continue
        raw = _load_json(rule.params_json, rule_id=rule.id).get("max_dogs")
        if isinstance(raw, bool):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("[local_rules] max_dogs inválido na regra %s", rule.id)
            continue
        if value >= 1:
            values.append(value)
    return min(values) if values else None


def max_dogs_per_walker(db: Session, municipio: str | None, uf: str | None, *, national_default: int) -> int:
    override = local_max_dogs_override(db, municipio, uf)
    return override if override is not None else national_default

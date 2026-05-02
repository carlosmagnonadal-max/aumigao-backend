import logging
import os
import uuid
import base64
import random
import re
import secrets
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, cast

import bcrypt
import jwt
import httpx
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim
from dotenv import load_dotenv, dotenv_values
from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, EmailStr, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

ROOT_DIR = Path(__file__).parent
UPLOADS_DIR = ROOT_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT_DIR / ".env")

mongo_url = os.environ["MONGO_URL"]
db_name = os.environ["DB_NAME"]
client = AsyncIOMotorClient(mongo_url)
db = client[db_name]
GEOCODER = Nominatim(user_agent="petpasso-geocoder/1.0")

app = FastAPI(title="PetPasso API")
api_router = APIRouter(prefix="/api")

STATUS_SCHEDULED = "Agendado"
STATUS_GOING_TO_PICKUP = "Indo buscar o pet"
STATUS_WALKING_NOW = "Passeando agora"
STATUS_FINISHED = "Finalizado"
STATUS_CANCELED = "Cancelado"
STATUS_NO_SHOW_CLIENT = "Não comparecimento do cliente"
STATUS_NO_SHOW_WALKER = "Não comparecimento do passeador"
STATUS_PENDING_REVIEW = "Pendente de análise"
LEGACY_STATUS_IN_PROGRESS = "Em andamento"
ACTIVE_WALK_STATUSES = {STATUS_SCHEDULED, STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW}
BLOCKING_WALK_STATUSES = {STATUS_SCHEDULED, STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW, STATUS_PENDING_REVIEW}
DECISION_TERMINAL_STATUSES = {STATUS_FINISHED, STATUS_CANCELED, STATUS_NO_SHOW_CLIENT, STATUS_NO_SHOW_WALKER}
MIN_BUFFER_BETWEEN_WALKS_MINUTES = 25
TOLERANCE_MINUTES = 10
OCC_PENDING_ANALYSIS = "pendente_analise"
OCC_PENDING_ANALYSIS_REOPENED = "pendente_analise_reaberta"
OCC_NO_SHOW_CLIENT = "nao_comparecimento_cliente"
OCC_NO_SHOW_WALKER = "nao_comparecimento_passeador"
OCC_LATE_LIGHT = "atraso_leve"
OCC_LATE_SEVERE = "atraso_grave"
OCC_DISPUTE_OPEN = "disputa_financeira_aberta"
OCC_DISPUTE_RESOLVED = "disputa_financeira_resolvida"
OCC_RESOLVED = "resolvido"
OCC_UNRESOLVED = "nao_resolvido"
OCC_SUSPECT_DISINTERMEDIATION = "suspeita_desintermediacao"
WALKER_OP_STATUS_ACTIVE = "ativo"
WALKER_OP_STATUS_OBSERVATION = "observacao"
WALKER_OP_STATUS_RESTRICTED = "restrito"
WALKER_OP_STATUS_SUSPENDED = "suspenso"
QUALITY_STATUS_PREMIUM = "ativo_premium"
QUALITY_STATUS_ACTIVE = "ativo"
QUALITY_STATUS_OBSERVATION = "em_observacao"
QUALITY_STATUS_RESTRICTED = "restrito"
QUALITY_STATUS_SUSPENDED = "suspenso"
RESTRICTED_DAILY_LIMIT = 2
WALK_TYPE_INDIVIDUAL = "Individual"
WALK_TYPE_SHARED = "Compartilhado"
SHARED_CONTEXT_SAME_HOUSEHOLD = "same_household"
SHARED_CONTEXT_OTHER_CLIENT = "other_client"
START_MODE_TUTOR_ADDRESS = "endereco_tutor"
START_MODE_MEETING_POINT = "ponto_encontro"
START_MODE_PREMIUM_RELOCATION = "deslocamento_premium"
PREMIUM_ANALYSIS_NA = "nao_aplicavel"
PREMIUM_ANALYSIS_APPROVED = "aprovado"
PREMIUM_ANALYSIS_WAITING = "aguardando_analise"
PREMIUM_ANALYSIS_REJECTED = "rejeitado"
KIT_BASIC_FIELDS = ("has_water", "has_bowl", "has_bags", "has_first_aid")
KIT_ESSENTIAL_FIELDS = ("has_towel",)
KIT_PREMIUM_FIELDS = ("has_premium_items",)
KIT_LEVEL2_BOOST_FACTOR = 0.03
KIT_LEVEL3_BOOST_FACTOR = 0.05
KIT_BOOST_MIN_SCORE_BASE = 55.0
KIT_OCCURRENCE_STATUS = "kit_item_ausente"
PREMIUM_VERIFIED_BADGE_NAME = "Passeador Premium Verificado"
PREMIUM_VERIFIED_BADGE_SUBTITLE = "Checklist de segurança cumprido"
DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET = 5
DEFAULT_PREMIUM_VERIFIED_BONUS_SCORE = 7.0
DEFAULT_PREMIUM_VERIFIED_PRIORITY_BONUS = 1.5
DEFAULT_PREMIUM_VERIFIED_CR_EFFICIENCY_MULTIPLIER = 1.2
VERIFICATION_LEVEL_NONE = "NONE"
VERIFICATION_LEVEL_VERIFIED = "VERIFIED"
VERIFICATION_LEVEL_PLUS = "PLUS"
VERIFICATION_LEVEL_PREMIUM = "PREMIUM"
VERIFICATION_RECENT_WINDOW_DAYS = 60
VERIFICATION_PLUS_CANCEL_RATE_MAX = 8.0
VERIFICATION_PLUS_BOOST_POINTS = 2.0
VERIFICATION_PREMIUM_BOOST_POINTS = 4.0
VERIFICATION_LOW_SCORE_GUARD = 55.0
CR_ACTION_COSTS = {
    "matching_boost": 5,
    "early_wave": 4,
    "visual_highlight": 3,
}
CR_DAILY_USES_LIMIT = 3
CR_ACTION_DURATIONS_HOURS = {
    "matching_boost": 24,
    "early_wave": 24,
    "visual_highlight": 12,
}
CR_MATCHING_BOOST_BASE_POINTS = 5.0
CR_EARLY_WAVE_BASE_PRIORITY = 1.0
CR_VISUAL_EXPOSURE_BASE_POINTS = 1.0
DEFAULT_PREMIUM_PAYOUT_PERCENT = 75.0
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 24
REFRESH_TOKEN_DAYS = 14
FAILED_LOGIN_LIMIT = 5
FAILED_LOGIN_LOCKOUT_MINUTES = 15
ADMIN_PERMISSION_KEYS = [
    "dashboard",
    "clientes",
    "passeios",
    #"pagamentos",
    "passeadores",
    "planos",
    "suporte",
    "configuracoes",
    "juridico",
    "administradores",
]
WEEKDAY_KEYS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
DEFAULT_AVAILABILITY_DAYS = ["seg", "ter", "qua", "qui", "sex"]
DEFAULT_AVAILABILITY_START_TIME = "08:00"
DEFAULT_AVAILABILITY_END_TIME = "18:00"
DEFAULT_AVAILABILITY_PERIODS = {
    "manha": {"start_time": "06:00", "end_time": "11:59"},
    "tarde": {"start_time": "12:00", "end_time": "17:59"},
    "noite": {"start_time": "18:00", "end_time": "22:00"},
}
DEFAULT_AVAILABILITY_CAPACITY_BY_PERIOD = {
    "manha": 3,
    "tarde": 3,
    "noite": 2,
}
WALK_DURATION_OPTIONS = [30, 45, 60]
COUPON_WALK_TYPES = [WALK_TYPE_INDIVIDUAL, WALK_TYPE_SHARED]
MIN_PUBLIC_RATING_COUNT = 5
MIN_CLIENT_RATING_DISPLAY_COUNT = 5
RECENT_RATING_WINDOW = 10
PREMIUM_NO_SHOW_LOOKBACK_WALKS = 10
RECENCY_SHORT_DAYS = 7
RECENCY_MEDIUM_DAYS = 30
REPUTATION_SCORE_HISTORY_LIMIT = 60
MATCH_MIN_SCORE = 60.0
MATCH_FALLBACK_MIN_SCORE = 50.0
MATCH_TOP_DEFAULT = 3
MATCH_TOP_EXPANDED = 5
MATCH_TOP_EXPANDED_ELIGIBLE_THRESHOLD = 8
MATCH_TOP_WAVE4_MAX = 10
MATCH_WAVE_TIMEOUT_SECONDS = 20
MATCH_PRIMARY_RADIUS_KM = 10.0
MATCH_TEMP_PENALTY_POINTS = 2.0
MATCH_TEMP_PENALTY_MINUTES = 30
MATCH_COOLDOWN_AFTER_REJECTIONS = 3
MATCH_COOLDOWN_MINUTES = 10
MATCH_NO_SHOW_CRITICAL_24H = 2
MATCH_LOAD_PENALTY_PER_WALK = 15.0
MATCH_LOAD_SCORE_FLOOR = 40.0
MATCH_BEHAVIORAL_LOW_SCORE_BLOCK = 55.0
MATCH_PREMIUM_BOOST_DEFAULT = 10.0
MATCH_PREMIUM_BOOST_REDUCED = 5.0
MATCH_PREMIUM_BOOST_HIGH_DEMAND = 12.0
MATCH_PREMIUM_BASE_ELIGIBILITY_MIN = 70.0
MATCH_PREMIUM_DISTANCE_MULTIPLIER_LIMIT = 1.8
MATCH_HIGH_DEMAND_HOURLY_THRESHOLD = 6
MARKETPLACE_MODE_AUTOMATIC = "automatico"
MARKETPLACE_MODE_ASSISTED = "manual_assistido"
MARKETPLACE_MODE_MANUAL = "manual_total"
MARKETPLACE_CONTEXT_CRITICAL = "critico"
MARKETPLACE_CONTEXT_BALANCED = "equilibrado"
MARKETPLACE_CONTEXT_SURPLUS = "sobra_oferta"
MARKETPLACE_MAX_CR_WEIGHT_PERCENT = 20.0
MARKETPLACE_DEFAULT_CR_WEIGHT_PERCENT = 20.0
MARKETPLACE_DEFAULT_CRITICAL_RATIO = 1.2
MARKETPLACE_DEFAULT_BALANCED_RATIO_FLOOR = 0.8
MARKETPLACE_DEFAULT_BALANCED_RATIO_CEIL = 1.2
MARKETPLACE_DEFAULT_POLLING_SECONDS = 30
MARKETPLACE_CONTEXT_WINDOW_HOURS = 2
MARKETPLACE_CRITICAL_ACCEPTANCE_SECONDS = 180.0
MARKETPLACE_CRITICAL_MATCH_RATE = 0.55
MARKETPLACE_SURPLUS_MATCH_RATE = 0.75
DYNAMIC_PRICING_MODE_OFF = "off"
DYNAMIC_PRICING_MODE_SHADOW = "shadow"
DYNAMIC_PRICING_MODE_ACTIVE = "active"
DYNAMIC_PRICING_MIN_SUPPLY_BOOST = 0.10
DYNAMIC_PRICING_MAX_SUPPLY_BOOST = 0.20
DYNAMIC_PRICING_MIN_DEMAND_BOOST = 0.05
DYNAMIC_PRICING_MAX_DEMAND_BOOST = 0.15
DYNAMIC_PRICING_CRITICAL_BOOST = 0.05
DYNAMIC_PRICING_MAX_TOTAL_BOOST = 0.20
DYNAMIC_PRICING_MAX_ARCHITECTURE_BOOST = 0.40
REFERRAL_STATUS_CREATED = "criada"
REFERRAL_STATUS_PENDING = "pendente_ativacao"
REFERRAL_STATUS_IN_PROGRESS = "em_progresso"
REFERRAL_STATUS_ELIGIBLE = "elegivel_recompensa"
REFERRAL_STATUS_REWARDED = "recompensa_liberada"
REFERRAL_STATUS_CANCELED = "cancelada"
REFERRAL_STATUS_FRAUD = "invalida_fraude"
TIP_SCORE_RECENT_WALKS_WINDOW = 20
TIP_SCORE_MAX_SHARE = 0.10
TIP_SCORE_MAX_POINTS = 10.0
TIP_SUSPICIOUS_HIGH_AMOUNT = 25.0
TIP_SUSPICIOUS_VERY_HIGH_AMOUNT = 35.0
TIP_SUSPICIOUS_CONCENTRATION_SHARE = 0.65
TIP_SUSPICIOUS_PLATFORM_MULTIPLIER = 2.5
TIP_SUSPICIOUS_REPEAT_THRESHOLD = 3
TIP_SUSPICIOUS_RECENT_WINDOW_DAYS = 30
WALKER_LEVEL_BRONZE = "bronze"
WALKER_LEVEL_SILVER = "silver"
WALKER_LEVEL_GOLD = "gold"
WALKER_LEVEL_PRATA = WALKER_LEVEL_SILVER
WALKER_LEVEL_OURO = WALKER_LEVEL_GOLD
WALKER_LEVEL_ELITE = WALKER_LEVEL_GOLD
WALKER_LEVEL_PRIORITY_BONUS = {
    WALKER_LEVEL_BRONZE: 0.02,
    WALKER_LEVEL_SILVER: 0.04,
    WALKER_LEVEL_GOLD: 0.06,
}
WALKER_LEVEL_RULES = {
    WALKER_LEVEL_SILVER: {"score": 0.78, "walks": 10, "rating": 4.5, "max_cancel_rate": 15.0, "checklist_streak": 5},
    WALKER_LEVEL_GOLD: {"score": 0.88, "walks": 25, "rating": 4.7, "max_cancel_rate": 8.0, "checklist_streak": 12, "max_infractions": 0},
}
DEFAULT_WALKER_LEVEL_SETTINGS = {
    "silver_min_walks": 10,
    "silver_min_rating": 4.5,
    "silver_max_cancel_rate": 15.0,
    "silver_min_checklist_streak": 5,
    "silver_min_score_ratio": 0.78,
    "gold_min_walks": 25,
    "gold_min_rating": 4.7,
    "gold_max_cancel_rate": 8.0,
    "gold_min_checklist_streak": 12,
    "gold_min_score_ratio": 0.88,
    "gold_max_infractions": 0,
    "bronze_boost_factor": 0.02,
    "silver_boost_factor": 0.04,
    "gold_boost_factor": 0.06,
}
WALKER_LEVEL_SETTINGS_CACHE: Dict[str, Any] = dict(DEFAULT_WALKER_LEVEL_SETTINGS)
WEEKLY_MISSION_SCORE_BONUS = 0.02
WEEKLY_TIP_GOAL_AMOUNT = 20.0
DISINTERMEDIATION_CONTACT_ATTEMPTS_THRESHOLD = 3
DISINTERMEDIATION_CONTACT_WINDOW_DAYS = 7
DISINTERMEDIATION_CONTACT_DEBOUNCE_MINUTES = 5
DISINTERMEDIATION_CANCEL_WINDOW_DAYS = 14
DISINTERMEDIATION_CANCEL_RATE_THRESHOLD = 0.40
DISINTERMEDIATION_FLAG_EXPIRY_DAYS = 7
DISINTERMEDIATION_MATCH_PENALTY_POINTS = 2.0
SYSTEM_ALERT_STATUS_PENDING = "pendente"
SYSTEM_ALERT_STATUS_EXECUTED = "executado"
SYSTEM_ALERT_STATUS_IGNORED = "ignorado"
SYSTEM_ALERT_STATUS_REVIEW_LATER = "revisar_depois"
SYSTEM_ALERT_AUTO_OBSERVATION_DAYS = 7
SYSTEM_ALERT_PERMITTED_AUTO_ACTIONS = {
    "set_observation_7d",
    "reduce_matching_priority",
    "mark_occurrence_pending",
    "apply_risk_flag",
    "block_suspicious_coupon",
    "suspend_auto_preselection",
}
SYSTEM_ALERT_PRIORITY_DEFAULT_WEIGHTS = {
    "impacto_financeiro": 30.0,
    "risco_operacional": 30.0,
    "reincidencia": 15.0,
    "proximidade_tempo": 15.0,
    "frequencia_evento": 10.0,
}
SYSTEM_ALERT_DEFAULT_GROUPING_WINDOWS_HOURS = {
    "operacional": 24,
    "financeiro": 24,
    "comportamental": 24,
    "sistemico": 12,
}
SYSTEM_ALERT_SYSTEMIC_REGION_FAILURE_THRESHOLD = 3
SYSTEM_ALERT_SYSTEMIC_OVERLOAD_THRESHOLD = 8
FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT = "engajamento_cliente"
FEATURE_FLAG_GROUP_MONETIZATION = "monetizacao_incentivos"
FEATURE_FLAG_GROUP_VISIBILITY = "visibilidade_ranking"
FEATURE_FLAG_GROUP_MARKETPLACE_INTELLIGENCE = "inteligencia_marketplace"
FEATURE_FLAGS_CATALOG: Dict[str, Dict[str, Any]] = {
    "referral_program": {
        "feature_name": "referral_program",
        "title": "Programa de indicação (geral)",
        "group": FEATURE_FLAG_GROUP_MONETIZATION,
        "is_active": False,
        "is_visible": False,
    },
    "client_referral_system": {
        "feature_name": "client_referral_system",
        "title": "Sistema de indicação (cliente → cliente)",
        "group": FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT,
        "is_active": False,
        "is_visible": False,
    },
    "recurring_usage_benefit": {
        "feature_name": "recurring_usage_benefit",
        "title": "Benefício por uso recorrente",
        "group": FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT,
        "is_active": True,
        "is_visible": True,
    },
    "pet_routine": {
        "feature_name": "pet_routine",
        "title": "Rotina do pet",
        "group": FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT,
        "is_active": True,
        "is_visible": True,
    },
    "pet_transport": {
        "feature_name": "pet_transport",
        "title": "Passeio com transporte",
        "group": FEATURE_FLAG_GROUP_MONETIZATION,
        "is_active": False,
        "is_visible": False,
    },
    "premium_verified_badge_enabled": {
        "feature_name": "premium_verified_badge_enabled",
        "title": "Selo Passeador Premium Verificado",
        "group": FEATURE_FLAG_GROUP_VISIBILITY,
        "is_active": True,
        "is_visible": True,
    },
    "premium_verified_bonus_enabled": {
        "feature_name": "premium_verified_bonus_enabled",
        "title": "Bônus do selo Premium Verificado",
        "group": FEATURE_FLAG_GROUP_MONETIZATION,
        "is_active": True,
        "is_visible": True,
    },
    "walker_verification_enabled": {
        "feature_name": "walker_verification_enabled",
        "title": "Selos de verificação de passeador",
        "group": FEATURE_FLAG_GROUP_VISIBILITY,
        "is_active": True,
        "is_visible": True,
    },
    "usage_streak": {
        "feature_name": "usage_streak",
        "title": "Streak / uso contínuo",
        "group": FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT,
        "is_active": True,
        "is_visible": True,
    },
    "habit_incentive": {
        "feature_name": "habit_incentive",
        "title": "Incentivo ao hábito",
        "group": FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT,
        "is_active": True,
        "is_visible": True,
    },
    "tips": {
        "feature_name": "tips",
        "title": "Gorjetas",
        "group": FEATURE_FLAG_GROUP_MONETIZATION,
        "is_active": True,
        "is_visible": True,
    },
    "walker_referral": {
        "feature_name": "walker_referral",
        "title": "Indicação de passeadores (passeador → passeador)",
        "group": FEATURE_FLAG_GROUP_MONETIZATION,
        "is_active": False,
        "is_visible": False,
    },
    "visible_badges": {
        "feature_name": "visible_badges",
        "title": "Badges / selos visíveis",
        "group": FEATURE_FLAG_GROUP_VISIBILITY,
        "is_active": True,
        "is_visible": True,
    },
    "weekly_highlights": {
        "feature_name": "weekly_highlights",
        "title": "Destaques semanais",
        "group": FEATURE_FLAG_GROUP_VISIBILITY,
        "is_active": True,
        "is_visible": True,
    },
    "motor_autonomo_enabled": {
        "feature_name": "motor_autonomo_enabled",
        "title": "Motor autônomo de decisão",
        "group": FEATURE_FLAG_GROUP_MARKETPLACE_INTELLIGENCE,
        "is_active": True,
        "is_visible": True,
    },
    "cr_system_enabled": {
        "feature_name": "cr_system_enabled",
        "title": "Sistema de Créditos de Reputação (CR)",
        "group": FEATURE_FLAG_GROUP_MARKETPLACE_INTELLIGENCE,
        "is_active": True,
        "is_visible": True,
    },
    "dynamic_adjustment_enabled": {
        "feature_name": "dynamic_adjustment_enabled",
        "title": "Ajustes dinâmicos por contexto",
        "group": FEATURE_FLAG_GROUP_MARKETPLACE_INTELLIGENCE,
        "is_active": True,
        "is_visible": True,
    },
    "kit_system_enabled": {
        "feature_name": "kit_system_enabled",
        "title": "Sistema de kit verificado",
        "group": FEATURE_FLAG_GROUP_MARKETPLACE_INTELLIGENCE,
        "is_active": True,
        "is_visible": True,
    },
    "premium_verified_enabled": {
        "feature_name": "premium_verified_enabled",
        "title": "Selo premium verificado",
        "group": FEATURE_FLAG_GROUP_MARKETPLACE_INTELLIGENCE,
        "is_active": True,
        "is_visible": True,
    },
    "level_system_enabled": {
        "feature_name": "level_system_enabled",
        "title": "Sistema de níveis do passeador",
        "group": FEATURE_FLAG_GROUP_MARKETPLACE_INTELLIGENCE,
        "is_active": True,
        "is_visible": True,
    },
}
RUNTIME_FEATURE_FLAGS: Dict[str, Dict[str, Any]] = {
    feature_name: {
        "feature_name": feature_name,
        "title": cfg["title"],
        "group": cfg["group"],
        "is_active": bool(cfg["is_active"]),
        "is_visible": bool(cfg["is_visible"]),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system",
    }
    for feature_name, cfg in FEATURE_FLAGS_CATALOG.items()
}
MONITORING_SEVERE_DELAY_RECURRENCE_THRESHOLD = 3
MAX_ACCOUNTS_PER_DEVICE = 3
MAX_REGISTRATIONS_PER_IP_PER_HOUR = 4
MAX_COUPON_USES_PER_IP_IN_15_MIN = 2
COUPON_IP_WINDOW_MINUTES = 15
TEMP_REGISTRATION_BLOCK_HOURS = 6
RAPID_REGISTRATION_ALERT_THRESHOLD = 3
COUPON_ERROR_INVALID = "Cupom inválido ou já utilizado"
COUPON_ERROR_LIMIT = "Limite de uso atingido"
DEFAULT_WALKER_SHARE_PERCENT = 80.0
DEFAULT_PLATFORM_SHARE_PERCENT = 20.0
DEFAULT_QUALITY_BONUS_PERCENT = 5.0
DEFAULT_QUALITY_BONUS_MIN_WEIGHTED = 4.7
DEFAULT_QUALITY_BONUS_MIN_WALKS = 10
DEFAULT_CONSISTENCY_BONUS_AMOUNT = 30.0
DEFAULT_CONSISTENCY_DAYS_REQUIRED = 7
DEFAULT_CRITICAL_HOUR_BONUS_AMOUNT = 5.0
DEFAULT_VOLUME_BONUS_TIERS = [
    {"target_walks": 20, "amount": 30.0},
    {"target_walks": 40, "amount": 70.0},
    {"target_walks": 60, "amount": 120.0},
]
DEFAULT_CRITICAL_WINDOWS = [
    {"start": "06:00", "end": "09:59"},
    {"start": "16:00", "end": "21:59"},
]

RUNTIME_WALKER_SHARE_PERCENT = DEFAULT_WALKER_SHARE_PERCENT
RUNTIME_PLATFORM_SHARE_PERCENT = DEFAULT_PLATFORM_SHARE_PERCENT


def _build_avatar_data_uri(background: str, foreground: str) -> str:
    svg = f"""
    <svg xmlns='http://www.w3.org/2000/svg' width='240' height='240' viewBox='0 0 240 240'>
      <rect width='240' height='240' rx='120' fill='{background}'/>
      <circle cx='120' cy='88' r='42' fill='{foreground}' opacity='0.95'/>
      <path d='M47 201c11-38 40-58 73-58s62 20 73 58' fill='{foreground}' opacity='0.95'/>
      <circle cx='92' cy='83' r='5' fill='white'/>
      <circle cx='148' cy='83' r='5' fill='white'/>
      <path d='M95 111c7 8 17 12 25 12 9 0 18-4 25-12' stroke='white' stroke-width='5' stroke-linecap='round'/>
    </svg>
    """
    encoded = base64.b64encode(svg.encode("utf-8")).decode("utf-8")
    return f"data:image/svg+xml;base64,{encoded}"


def _clock_to_minutes(clock_value: str) -> int:
    if not re.match(r"^\d{2}:\d{2}$", clock_value or ""):
        raise ValueError("Formato de horário inválido")
    hours, minutes = [int(part) for part in clock_value.split(":")]
    if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
        raise ValueError("Horário fora do intervalo permitido")
    return (hours * 60) + minutes


def _minutes_to_clock(minutes_total: int) -> str:
    hours = minutes_total // 60
    minutes = minutes_total % 60
    return f"{hours:02d}:{minutes:02d}"


def _normalize_clock(clock_value: Optional[str], fallback: str) -> str:
    raw = str(clock_value or "").strip()
    if not raw:
        return fallback
    try:
        return _minutes_to_clock(_clock_to_minutes(raw))
    except ValueError:
        return fallback


def _normalize_availability_days(raw_days: Any) -> List[str]:
    values = raw_days if isinstance(raw_days, list) else []
    normalized: List[str] = []
    for item in values:
        day = str(item).strip().lower()
        if day in WEEKDAY_KEYS and day not in normalized:
            normalized.append(day)
    return normalized or DEFAULT_AVAILABILITY_DAYS.copy()


def _build_slots_for_duration(start_time: str, end_time: str, duration_minutes: int) -> List[str]:
    start_minutes = _clock_to_minutes(start_time)
    end_minutes = _clock_to_minutes(end_time)
    if end_minutes - start_minutes < duration_minutes:
        return []

    slots: List[str] = []
    current = start_minutes
    while current + duration_minutes <= end_minutes:
        slots.append(_minutes_to_clock(current))
        current += 15
    return slots


def _build_horarios_disponiveis(days: List[str], start_time: str, end_time: str) -> Dict[str, Dict[str, List[str]]]:
    schedule: Dict[str, Dict[str, List[str]]] = {}
    normalized_days = _normalize_availability_days(days)
    normalized_start = _normalize_clock(start_time, DEFAULT_AVAILABILITY_START_TIME)
    normalized_end = _normalize_clock(end_time, DEFAULT_AVAILABILITY_END_TIME)

    start_minutes = _clock_to_minutes(normalized_start)
    end_minutes = _clock_to_minutes(normalized_end)
    if end_minutes - start_minutes < 60:
        normalized_start = DEFAULT_AVAILABILITY_START_TIME
        normalized_end = DEFAULT_AVAILABILITY_END_TIME

    for duration in WALK_DURATION_OPTIONS:
        day_map: Dict[str, List[str]] = {day: [] for day in WEEKDAY_KEYS}
        for day in normalized_days:
            day_map[day] = _build_slots_for_duration(normalized_start, normalized_end, duration)
        schedule[str(duration)] = day_map
    return schedule


def _normalize_availability_periods(raw: Any) -> Dict[str, Dict[str, str]]:
    defaults = DEFAULT_AVAILABILITY_PERIODS
    if not isinstance(raw, dict):
        return defaults

    normalized: Dict[str, Dict[str, str]] = {}
    for key in ["manha", "tarde", "noite"]:
        source_row = raw.get(key)
        if isinstance(source_row, dict):
            source = source_row
        elif hasattr(source_row, "model_dump"):
            source = source_row.model_dump()
        else:
            source = {}
        start_time = _normalize_clock(source.get("start_time"), defaults[key]["start_time"])
        end_time = _normalize_clock(source.get("end_time"), defaults[key]["end_time"])
        if _clock_to_minutes(end_time) <= _clock_to_minutes(start_time):
            start_time, end_time = defaults[key]["start_time"], defaults[key]["end_time"]
        normalized[key] = {"start_time": start_time, "end_time": end_time}
    return normalized


def _normalize_availability_capacity_by_period(raw: Any) -> Dict[str, int]:
    defaults = dict(DEFAULT_AVAILABILITY_CAPACITY_BY_PERIOD)
    if not isinstance(raw, dict):
        return defaults

    normalized = dict(defaults)
    for key in ["manha", "tarde", "noite"]:
        value = raw.get(key)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = defaults[key]
        normalized[key] = max(0, min(parsed, 24))
    return normalized


def _normalize_daily_capacity_overrides(raw: Any) -> Dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    normalized: Dict[str, int] = {}
    for key, value in raw.items():
        date_text = str(key or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
            continue
        parsed = int(_coerce_float(value, 0.0))
        if parsed <= 0:
            continue
        normalized[date_text] = max(1, min(parsed, 40))
    return normalized


def _period_key_for_clock(time_value: str) -> str:
    minutes = _clock_to_minutes(_normalize_clock(time_value, "00:00"))
    if minutes <= _clock_to_minutes("11:59"):
        return "manha"
    if minutes <= _clock_to_minutes("17:59"):
        return "tarde"
    return "noite"


def _build_horarios_disponiveis_from_periods(days: List[str], periods: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, List[str]]]:
    schedule: Dict[str, Dict[str, List[str]]] = {}
    normalized_days = _normalize_availability_days(days)
    normalized_periods = _normalize_availability_periods(periods)

    for duration in WALK_DURATION_OPTIONS:
        day_map: Dict[str, List[str]] = {day: [] for day in WEEKDAY_KEYS}
        for day in normalized_days:
            slots: List[str] = []
            for period_key in ["manha", "tarde", "noite"]:
                row = normalized_periods.get(period_key, {})
                slots.extend(_build_slots_for_duration(row.get("start_time", "08:00"), row.get("end_time", "18:00"), duration))
            day_map[day] = sorted(list(dict.fromkeys(slots)))
        schedule[str(duration)] = day_map

    return schedule


def _normalize_horarios_disponiveis(raw_schedule: Any) -> Dict[str, Dict[str, List[str]]]:
    if not isinstance(raw_schedule, dict):
        return {}

    normalized: Dict[str, Dict[str, List[str]]] = {}
    for duration in WALK_DURATION_OPTIONS:
        duration_key = str(duration)
        raw_by_day = raw_schedule.get(duration_key)
        if not isinstance(raw_by_day, dict):
            continue

        day_map: Dict[str, List[str]] = {day: [] for day in WEEKDAY_KEYS}
        for day in WEEKDAY_KEYS:
            candidate_slots = raw_by_day.get(day)
            if not isinstance(candidate_slots, list):
                continue

            valid_slots: List[str] = []
            for slot in candidate_slots:
                slot_text = str(slot).strip()
                if not slot_text:
                    continue
                try:
                    valid_slots.append(_minutes_to_clock(_clock_to_minutes(slot_text)))
                except ValueError:
                    continue
            day_map[day] = list(dict.fromkeys(valid_slots))
        normalized[duration_key] = day_map
    return normalized


def _normalize_availability_blocks(raw_blocks: Any) -> List[dict]:
    if not isinstance(raw_blocks, list):
        return []

    normalized: List[dict] = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue

        start_dt = _parse_iso_datetime(item.get("start_at"))
        end_dt = _parse_iso_datetime(item.get("end_at"))
        if not start_dt or not end_dt or end_dt <= start_dt:
            continue

        block_id = str(item.get("id") or uuid.uuid4())
        block_type = str(item.get("block_type") or "manual")
        if block_type not in {"manual", "quick_unavailable"}:
            block_type = "manual"

        normalized.append(
            {
                "id": block_id,
                "start_at": start_dt.isoformat(),
                "end_at": end_dt.isoformat(),
                "is_full_day": bool(item.get("is_full_day", False)),
                "reason": str(item.get("reason") or "").strip()[:120],
                "block_type": block_type,
                "created_at": str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
            }
        )

    normalized.sort(key=lambda block: block["start_at"])
    return normalized


def _slot_overlaps_block(candidate_start: datetime, duration_minutes: int, block: dict) -> bool:
    block_start = _parse_iso_datetime(block.get("start_at"))
    block_end = _parse_iso_datetime(block.get("end_at"))
    if not block_start or not block_end:
        return False

    candidate_end = candidate_start + timedelta(minutes=duration_minutes)
    return candidate_start < block_end and candidate_end > block_start


def _build_block_period(
    *,
    start_date: str,
    start_time: str,
    end_date: Optional[str],
    end_time: Optional[str],
    full_day: bool,
) -> tuple[datetime, datetime]:
    if full_day:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0, tzinfo=timezone.utc)
            end_anchor = datetime.strptime(end_date or start_date, "%Y-%m-%d")
            end_dt = end_anchor.replace(hour=23, minute=59, second=0, tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Data inválida para bloqueio") from exc
    else:
        start_value = _normalize_clock(start_time, DEFAULT_AVAILABILITY_START_TIME)
        end_value = _normalize_clock(end_time, DEFAULT_AVAILABILITY_END_TIME)
        try:
            start_dt = datetime.strptime(f"{start_date} {start_value}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            end_dt = datetime.strptime(f"{end_date or start_date} {end_value}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Data/horário inválidos para bloqueio") from exc

    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="O período de bloqueio deve terminar após o início")
    return start_dt, end_dt


def _walker_identity_query(user: dict) -> Dict[str, Any]:
    user_id = str(user.get("id") or "").strip()
    full_name = str(user.get("full_name") or "").strip()
    slug_name = full_name.lower().replace(" ", "-") if full_name else ""

    possible_ids = [user_id, f"partner-{user_id}"]
    if slug_name:
        possible_ids.append(f"partner-{slug_name}")

    clauses: List[Dict[str, Any]] = [{"walker_id": {"$in": list(dict.fromkeys([value for value in possible_ids if value]))}}]
    if user_id:
        clauses.append({"walker_user_id": user_id})
    if full_name:
        clauses.append({"walker_name": full_name})
    return {"$or": clauses}


async def _find_confirmed_walk_conflicts_for_period(user: dict, start_dt: datetime, end_dt: datetime) -> List[dict]:
    query: Dict[str, Any] = {
        "$and": [
            _walker_identity_query(user),
            {"status": {"$in": list(BLOCKING_WALK_STATUSES)}},
        ]
    }
    rows = await db.walks.find(query, {"_id": 0}).to_list(200)
    conflicts: List[dict] = []
    for row in rows:
        row_start, row_end = _walk_start_end(row)
        if not row_start or not row_end:
            continue
        if start_dt < row_end and end_dt > row_start:
            conflicts.append(row)
    return conflicts


def _ensure_walker_schedule(source: dict) -> dict:
    days = _normalize_availability_days(source.get("availability_days"))
    start_time = _normalize_clock(source.get("availability_start_time"), DEFAULT_AVAILABILITY_START_TIME)
    end_time = _normalize_clock(source.get("availability_end_time"), DEFAULT_AVAILABILITY_END_TIME)
    availability_periods = _normalize_availability_periods(source.get("availability_periods"))
    availability_capacity_by_period = _normalize_availability_capacity_by_period(source.get("availability_capacity_by_period"))
    daily_capacity_overrides = _normalize_daily_capacity_overrides(source.get("availability_daily_capacity_overrides"))

    start_minutes = _clock_to_minutes(start_time)
    end_minutes = _clock_to_minutes(end_time)
    if end_minutes <= start_minutes:
        end_time = DEFAULT_AVAILABILITY_END_TIME

    existing_schedule = _normalize_horarios_disponiveis(source.get("horarios_disponiveis"))
    if not existing_schedule:
        existing_schedule = _build_horarios_disponiveis_from_periods(days, availability_periods)

    payload = dict(source)
    payload["availability_days"] = days
    payload["availability_start_time"] = start_time
    payload["availability_end_time"] = end_time
    payload["availability_periods"] = availability_periods
    payload["availability_capacity_by_period"] = availability_capacity_by_period
    payload["availability_daily_capacity_overrides"] = daily_capacity_overrides
    payload["horarios_disponiveis"] = existing_schedule
    payload["availability_blocks"] = _normalize_availability_blocks(source.get("availability_blocks"))
    payload["unavailable_until"] = source.get("unavailable_until")
    return payload


def _weekday_key_from_date(date_value: str) -> str:
    try:
        parsed = datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Data inválida para consulta de disponibilidade") from exc
    return WEEKDAY_KEYS[parsed.weekday()]


def _walker_slots_for_date(walker: dict, walk_date: str, duration_minutes: int) -> List[str]:
    schedule_payload = _ensure_walker_schedule(walker)
    weekday_key = _weekday_key_from_date(walk_date)
    schedule = schedule_payload.get("horarios_disponiveis", {})
    day_slots = schedule.get(str(duration_minutes), {}).get(weekday_key, [])
    return [str(slot) for slot in day_slots]


WALKER_PROFILES = [
    {
        "id": "walker-1",
        "name": "Carla Menezes",
        "photo_url": _build_avatar_data_uri("#DFF5E8", "#2FBF71"),
        "possuiVeiculo": True,
        "aceitaDeslocamentoPremium": True,
        "raioMaximoPremiumKm": 5.0,
        "ativoParaTransportePremium": True,
        "has_water": True,
        "has_bowl": True,
        "has_bags": True,
        "has_first_aid": True,
        "has_towel": True,
        "has_extra_leash": True,
        "has_premium_items": False,
        "premium_verified_badge_active": False,
        "premium_verified_streak": 0,
        "premium_verified_last_reason": "",
        "is_verified": False,
        "verification_level": VERIFICATION_LEVEL_NONE,
        "verification_score_snapshot": 0,
        "reputation_credits": 0,
        "last_credit_update": None,
        "cr_matching_boost_until": None,
        "cr_early_wave_until": None,
        "cr_visual_highlight_until": None,
        "availability_days": ["seg", "ter", "qua", "qui", "sex"],
        "availability_start_time": "08:00",
        "availability_end_time": "18:00",
    },
    {
        "id": "walker-2",
        "name": "Rafael Souza",
        "photo_url": _build_avatar_data_uri("#E8F1FF", "#3B82F6"),
        "possuiVeiculo": True,
        "aceitaDeslocamentoPremium": True,
        "raioMaximoPremiumKm": 8.0,
        "ativoParaTransportePremium": True,
        "has_water": True,
        "has_bowl": True,
        "has_bags": True,
        "has_first_aid": True,
        "has_towel": True,
        "has_extra_leash": True,
        "has_premium_items": True,
        "premium_verified_badge_active": False,
        "premium_verified_streak": 0,
        "premium_verified_last_reason": "",
        "is_verified": False,
        "verification_level": VERIFICATION_LEVEL_NONE,
        "verification_score_snapshot": 0,
        "reputation_credits": 0,
        "last_credit_update": None,
        "cr_matching_boost_until": None,
        "cr_early_wave_until": None,
        "cr_visual_highlight_until": None,
        "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab"],
        "availability_start_time": "07:30",
        "availability_end_time": "19:00",
    },
    {
        "id": "walker-3",
        "name": "Amanda Lima",
        "photo_url": _build_avatar_data_uri("#FFF4DF", "#C47A00"),
        "possuiVeiculo": False,
        "aceitaDeslocamentoPremium": False,
        "raioMaximoPremiumKm": 0.0,
        "ativoParaTransportePremium": False,
        "has_water": True,
        "has_bowl": True,
        "has_bags": True,
        "has_first_aid": False,
        "has_towel": False,
        "has_extra_leash": False,
        "has_premium_items": False,
        "premium_verified_badge_active": False,
        "premium_verified_streak": 0,
        "premium_verified_last_reason": "",
        "is_verified": False,
        "verification_level": VERIFICATION_LEVEL_NONE,
        "verification_score_snapshot": 0,
        "reputation_credits": 0,
        "last_credit_update": None,
        "cr_matching_boost_until": None,
        "cr_early_wave_until": None,
        "cr_visual_highlight_until": None,
        "availability_days": ["seg", "ter", "qua", "qui", "sex"],
        "availability_start_time": "09:00",
        "availability_end_time": "17:00",
    },
]
WALKER_MAP = {walker["id"]: walker for walker in WALKER_PROFILES}


def _jwt_secret() -> str:
    return os.environ.get("JWT_SECRET", "petpasso-default-secret-key-2026-strong-value")


def _hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def _create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def _create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_DAYS),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def _extract_bearer_token(request: Request) -> Optional[str]:
    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token

    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header.replace("Bearer ", "", 1)
    return None


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str):
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
        max_age=ACCESS_TOKEN_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
        max_age=REFRESH_TOKEN_DAYS * 24 * 60 * 60,
    )


def _empty_permissions_map() -> Dict[str, bool]:
    return {key: False for key in ADMIN_PERMISSION_KEYS}


def _default_admin_permissions_map() -> Dict[str, bool]:
    defaults = _empty_permissions_map()
    for key in ["dashboard", "clientes", "passeios", "passeadores", "suporte"]:
        defaults[key] = True
    return defaults


def _full_permissions_map() -> Dict[str, bool]:
    return {key: True for key in ADMIN_PERMISSION_KEYS}


def _normalize_admin_permissions(raw_permissions) -> Dict[str, bool]:
    normalized = _empty_permissions_map()

    if isinstance(raw_permissions, dict):
        for key in ADMIN_PERMISSION_KEYS:
            normalized[key] = bool(raw_permissions.get(key, False))
        return normalized

    if isinstance(raw_permissions, list):
        for permission in raw_permissions:
            key = str(permission).strip()
            if key in normalized:
                normalized[key] = True
        return normalized

    return normalized


def _enabled_permissions(permissions: Dict[str, bool]) -> Set[str]:
    return {key for key, value in permissions.items() if value}


def _is_super_admin_user(user: dict) -> bool:
    return user.get("role") == "super_admin"


def _has_full_admin_access(permissions) -> bool:
    return _enabled_permissions(_normalize_admin_permissions(permissions)) == set(ADMIN_PERMISSION_KEYS)


def _has_admin_permission(user: dict, permission: str) -> bool:
    if not user.get("isAdmin", False):
        return False
    if user.get("isActive", True) is False:
        return False
    if _is_super_admin_user(user):
        return True
    return _normalize_admin_permissions(user.get("permissions", {})).get(permission, False)


def _permission_for_admin_path(path: str) -> Optional[str]:
    mapping = [
        ("/api/admin/dashboard", "dashboard"),
        ("/api/admin/clients", "clientes"),
        ("/api/admin/walks", "passeios"),
        #("/api/admin/payments", "pagamentos"),
        #("/api/admin/tips", "pagamentos"),
        ("/api/admin/partner-applications", "passeadores"),
        ("/api/admin/occurrences", "dashboard"),
        ("/api/admin/alerts", "dashboard"),
        ("/api/admin/feature-flags", "configuracoes"),
        ("/api/admin/walkers", "passeadores"),
        ("/api/admin/pending-actions", "dashboard"),
        ("/api/admin/messages", "suporte"),
        ("/api/admin/suporte", "suporte"),
        ("/api/admin/planos", "planos"),
        ("/api/admin/configuracoes", "configuracoes"),
        ("/api/admin/juridico", "juridico"),
        ("/api/admin/pets", "passeios"),
        #("/api/admin/coupons", "pagamentos"),
        ("/api/admin/administrators", "administradores"),
    ]
    for prefix, permission in mapping:
        if path.startswith(prefix):
            return permission
    return None


def _default_alert_priority_settings_dict() -> dict:
    return {
        "id": "default",
        "weights": dict(SYSTEM_ALERT_PRIORITY_DEFAULT_WEIGHTS),
        "grouping_windows_hours": dict(SYSTEM_ALERT_DEFAULT_GROUPING_WINDOWS_HOURS),
        "systemic_region_failure_threshold": SYSTEM_ALERT_SYSTEMIC_REGION_FAILURE_THRESHOLD,
        "systemic_overload_threshold": SYSTEM_ALERT_SYSTEMIC_OVERLOAD_THRESHOLD,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalize_alert_weights(payload: Optional[Dict[str, Any]]) -> Dict[str, float]:
    weights = dict(SYSTEM_ALERT_PRIORITY_DEFAULT_WEIGHTS)
    if isinstance(payload, dict):
        for key in weights:
            if key in payload:
                weights[key] = max(0.0, min(100.0, _coerce_float(payload.get(key), weights[key])))
    total = sum(weights.values())
    if total <= 0:
        return dict(SYSTEM_ALERT_PRIORITY_DEFAULT_WEIGHTS)
    scale = 100.0 / total
    return {key: round(value * scale, 4) for key, value in weights.items()}


def _normalize_grouping_windows(payload: Optional[Dict[str, Any]]) -> Dict[str, int]:
    windows = dict(SYSTEM_ALERT_DEFAULT_GROUPING_WINDOWS_HOURS)
    if isinstance(payload, dict):
        for key in windows:
            if key in payload:
                windows[key] = max(1, min(168, int(_coerce_float(payload.get(key), windows[key]))))
    return windows


def _normalize_alert_priority_settings(row: Optional[dict]) -> dict:
    base = _default_alert_priority_settings_dict()
    if isinstance(row, dict):
        base["weights"] = _normalize_alert_weights(row.get("weights"))
        base["grouping_windows_hours"] = _normalize_grouping_windows(row.get("grouping_windows_hours"))
        base["systemic_region_failure_threshold"] = max(
            2,
            min(20, int(_coerce_float(row.get("systemic_region_failure_threshold"), SYSTEM_ALERT_SYSTEMIC_REGION_FAILURE_THRESHOLD))),
        )
        base["systemic_overload_threshold"] = max(
            3,
            min(50, int(_coerce_float(row.get("systemic_overload_threshold"), SYSTEM_ALERT_SYSTEMIC_OVERLOAD_THRESHOLD))),
        )
        if row.get("updated_at"):
            base["updated_at"] = str(row.get("updated_at"))
    return base


def _normalize_praise_tags(tags: List[str]) -> List[str]:
    allowed = {"docil", "brincalhao", "tranquilo", "ativo", "sociavel"}
    normalized: List[str] = []
    for raw in tags:
        value = str(raw or "").strip().lower()
        if value in allowed and value not in normalized:
            normalized.append(value)
    return normalized[:3]


def _pet_engagement_badges(*, month_walks: int, praise_count: int, is_featured: bool) -> List[str]:
    badges: List[str] = []
    if month_walks >= 4:
        badges.append("Pet ativo")
    if month_walks >= 8:
        badges.append("Cliente frequente")
    if is_featured or praise_count >= 3:
        badges.append("Pet destaque")
    return badges[:3]


def _latest_praise_tags_for_pet(praise_rows: List[dict], pet_id: str) -> List[str]:
    for row in sorted(praise_rows, key=lambda item: str(item.get("created_at") or ""), reverse=True):
        if str(row.get("pet_id") or "") == pet_id:
            return [str(tag) for tag in (row.get("tags") or []) if tag]
    return []


async def _get_system_alert_priority_settings() -> dict:
    row = await db.system_alert_priority_settings.find_one({"id": "default"}, {"_id": 0})
    if not row:
        row = _default_alert_priority_settings_dict()
        await db.system_alert_priority_settings.insert_one(row)
    normalized = _normalize_alert_priority_settings(row)
    if normalized != row:
        await db.system_alert_priority_settings.update_one({"id": "default"}, {"$set": normalized}, upsert=True)
    return normalized


def _alert_category_from_tipo(tipo_alerta: str) -> Literal["operacional", "financeiro", "comportamental", "sistemico"]:
    tipo = str(tipo_alerta or "").strip().upper()
    if tipo.startswith("FINANCIAL_") or "TIP" in tipo or "REFUND" in tipo or "DISPUTE" in tipo:
        return "financeiro"
    if tipo.startswith("SYSTEM_") or tipo.startswith("SYSTEMIC_"):
        return "sistemico"
    if tipo.startswith("DISINTERMEDIATION_") or "CANCEL" in tipo or "INCONSISTENT" in tipo or "SUSPECT" in tipo:
        return "comportamental"
    return "operacional"


def _priority_level_from_score(score: float) -> int:
    if score >= 75:
        return 4
    if score >= 55:
        return 3
    if score >= 30:
        return 2
    return 1


def _normalize_factor_value(value: Any, fallback: float = 0.0) -> float:
    return max(0.0, min(100.0, _coerce_float(value, fallback)))


def _build_alert_priority(
    *,
    settings: dict,
    provided_level: int,
    occurrences: int,
    metadata: dict,
    created_at_iso: str,
) -> Dict[str, Any]:
    weights = _normalize_alert_weights(settings.get("weights"))
    created_dt = _parse_iso_datetime(created_at_iso) or datetime.now(timezone.utc)
    minutes_since_created = max(0.0, (datetime.now(timezone.utc) - created_dt).total_seconds() / 60.0)
    proximity_from_age = max(0.0, 100.0 - min(100.0, minutes_since_created / 15.0))

    impacto_financeiro = _normalize_factor_value(metadata.get("impacto_financeiro"), provided_level * 20.0)
    risco_operacional = _normalize_factor_value(metadata.get("risco_operacional"), provided_level * 22.0)
    reincidencia = _normalize_factor_value(metadata.get("reincidencia"), min(100.0, occurrences * 20.0))
    proximidade_tempo = _normalize_factor_value(metadata.get("proximidade_tempo"), max(0.0, proximity_from_age))
    frequencia_evento = _normalize_factor_value(metadata.get("frequencia_evento"), min(100.0, occurrences * 18.0))

    factors = {
        "impacto_financeiro": impacto_financeiro,
        "risco_operacional": risco_operacional,
        "reincidencia": reincidencia,
        "proximidade_tempo": proximidade_tempo,
        "frequencia_evento": frequencia_evento,
    }
    weighted_score = 0.0
    for key, value in factors.items():
        weighted_score += value * (weights.get(key, 0.0) / 100.0)

    base_score = max(weighted_score, provided_level * 20.0)
    score = round(min(100.0, max(0.0, base_score)), 2)
    return {
        "prioridade_score": score,
        "nivel_prioridade": max(provided_level, _priority_level_from_score(score)),
        "fatores": factors,
    }


def _grouping_window_for_category(settings: dict, category: str) -> timedelta:
    grouping_windows = _normalize_grouping_windows(settings.get("grouping_windows_hours"))
    hours = grouping_windows.get(category, SYSTEM_ALERT_DEFAULT_GROUPING_WINDOWS_HOURS.get(category, 24))
    return timedelta(hours=max(1, min(168, int(hours))))


def _feature_flag_default_doc(feature_name: str) -> dict:
    cfg = FEATURE_FLAGS_CATALOG.get(feature_name)
    if cfg:
        return {
            "feature_name": feature_name,
            "title": str(cfg.get("title") or feature_name),
            "group": str(cfg.get("group") or FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT),
            "is_active": bool(cfg.get("is_active", False)),
            "is_visible": bool(cfg.get("is_visible", False)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": "system",
        }
    return {
        "feature_name": feature_name,
        "title": feature_name,
        "group": FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT,
        "is_active": False,
        "is_visible": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system",
    }


def _refresh_runtime_feature_flags(rows: List[dict]) -> None:
    runtime: Dict[str, Dict[str, Any]] = {}
    for feature_name in FEATURE_FLAGS_CATALOG.keys():
        runtime[feature_name] = _feature_flag_default_doc(feature_name)

    for row in rows:
        feature_name = str(row.get("feature_name") or "").strip()
        if not feature_name:
            continue
        runtime[feature_name] = {
            "feature_name": feature_name,
            "title": str(row.get("title") or _feature_flag_default_doc(feature_name).get("title")),
            "group": str(row.get("group") or _feature_flag_default_doc(feature_name).get("group")),
            "is_active": bool(row.get("is_active", False)),
            "is_visible": bool(row.get("is_visible", False)),
            "updated_at": str(row.get("updated_at") or datetime.now(timezone.utc).isoformat()),
            "updated_by": str(row.get("updated_by") or "system"),
        }

    global RUNTIME_FEATURE_FLAGS
    RUNTIME_FEATURE_FLAGS = runtime


def _get_runtime_feature_flag(feature_name: str) -> dict:
    feature_key = str(feature_name or "").strip()
    if feature_key in RUNTIME_FEATURE_FLAGS:
        return RUNTIME_FEATURE_FLAGS[feature_key]
    return _feature_flag_default_doc(feature_key)


def _is_feature_active(feature_name: str) -> bool:
    return bool(_get_runtime_feature_flag(feature_name).get("is_active", False))


def _is_feature_visible(feature_name: str) -> bool:
    return bool(_get_runtime_feature_flag(feature_name).get("is_visible", False))


async def _ensure_feature_flags_seeded() -> None:
    rows = await db.feature_flags.find({}, {"_id": 0}).to_list(200)
    existing = {str(row.get("feature_name") or "").strip() for row in rows}
    now_iso = datetime.now(timezone.utc).isoformat()
    missing_docs = []
    for feature_name in FEATURE_FLAGS_CATALOG.keys():
        if feature_name in existing:
            continue
        doc = _feature_flag_default_doc(feature_name)
        doc["updated_at"] = now_iso
        missing_docs.append(doc)

    if missing_docs:
        await db.feature_flags.insert_many(missing_docs)
        rows.extend(missing_docs)

    pet_routine_row = next((row for row in rows if str(row.get("feature_name") or "") == "pet_routine"), None)
    if pet_routine_row and (not bool(pet_routine_row.get("is_active", False)) or not bool(pet_routine_row.get("is_visible", False))):
        await db.feature_flags.update_one(
            {"feature_name": "pet_routine"},
            {
                "$set": {
                    "is_active": True,
                    "is_visible": True,
                    "updated_at": now_iso,
                    "updated_by": "system_migration",
                }
            },
        )
        pet_routine_row["is_active"] = True
        pet_routine_row["is_visible"] = True
        pet_routine_row["updated_at"] = now_iso
        pet_routine_row["updated_by"] = "system_migration"

    _refresh_runtime_feature_flags(rows)


def _validate_permission_assignment(requester: dict, target_permissions: Dict[str, bool]):
    if _is_super_admin_user(requester):
        return

    requester_permissions = _normalize_admin_permissions(requester.get("permissions", {}))
    requester_enabled = _enabled_permissions(requester_permissions)
    target_enabled = _enabled_permissions(target_permissions)

    if not target_enabled.issubset(requester_enabled):
        raise HTTPException(status_code=403, detail="Não é permitido conceder permissões acima do seu nível")

    if _has_full_admin_access(target_permissions):
        raise HTTPException(status_code=403, detail="Apenas Super Admin pode conceder acesso total")


async def _log_admin_action(actor: dict, action: str, target_admin_id: str, changes: dict):
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": str(uuid.uuid4()),
        "actor_admin_id": actor.get("id", "system"),
        "actor_email": actor.get("email", "system"),
        "action": action,
        "target_admin_id": target_admin_id,
        "changes": changes,
        "created_at": now_iso,
    }
    await db.admin_action_logs.insert_one(payload)


def _user_to_auth_response(user: dict) -> "AuthUserResponse":
    schedule_payload = (
        _ensure_walker_schedule(user)
        if user.get("role") == "passeador"
        else {"horarios_disponiveis": {}, "availability_blocks": [], "unavailable_until": None}
    )
    kit_profile = _walker_kit_profile_from_user(user)
    missing_reports_count = int(user.get("kit_missing_reports_count", 0) or 0)
    kit_level = _kit_effective_level(kit_profile, missing_reports_count)
    kit_labels = _kit_labels_from_level(kit_level)
    verification_level = str(user.get("verification_level") or VERIFICATION_LEVEL_NONE)
    verification_score_snapshot = int(user.get("verification_score_snapshot", 0) or 0)
    premium_verified_streak = int(user.get("premium_verified_streak", 0) or 0)
    premium_verified_target = int(user.get("premium_verified_streak_target", DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET) or DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET)
    return AuthUserResponse(
        id=user["id"],
        full_name=user.get("full_name", ""),
        email=user.get("email", ""),
        role=user.get("role", "cliente"),
        isAdmin=user.get("isAdmin", False),
        permissions=_normalize_admin_permissions(user.get("permissions", {})),
        isActive=user.get("isActive", True),
        hasSeguro=user.get("possuiSeguro", False),
        region=user.get("region", ""),
        possuiVeiculo=bool(user.get("possuiVeiculo", False)),
        aceitaDeslocamentoPremium=bool(user.get("aceitaDeslocamentoPremium", False)),
        raioMaximoPremiumKm=float(user.get("raioMaximoPremiumKm", 0) or 0),
        ativoParaTransportePremium=bool(user.get("ativoParaTransportePremium", False)),
        has_vehicle=bool(user.get("possuiVeiculo", False)),
        accepts_pet_transport=bool(user.get("aceitaDeslocamentoPremium", False)),
        vehicle_type=str(user.get("tipoVeiculo", "") or ""),
        transport_enabled=bool(user.get("ativoParaTransportePremium", False)),
        is_transport_eligible=bool(user.get("possuiVeiculo", False))
        and bool(user.get("aceitaDeslocamentoPremium", False))
        and bool(user.get("ativoParaTransportePremium", False)),
        has_water=bool(kit_profile.get("has_water", False)),
        has_bowl=bool(kit_profile.get("has_bowl", False)),
        has_bags=bool(kit_profile.get("has_bags", False)),
        has_first_aid=bool(kit_profile.get("has_first_aid", False)),
        has_towel=bool(kit_profile.get("has_towel", False)),
        has_extra_leash=bool(kit_profile.get("has_extra_leash", False)),
        has_premium_items=bool(kit_profile.get("has_premium_items", False)),
        kit_complete=bool(kit_profile.get("kit_complete", False)),
        kit_basic_complete=bool(kit_profile.get("kit_basic_complete", False)),
        kit_essential_complete=bool(kit_profile.get("kit_essential_complete", False)),
        kit_premium=bool(kit_profile.get("kit_premium", False)),
        kit_level=kit_level,
        kit_labels=kit_labels,
        premium_verified_badge_active=bool(
            user.get("premium_verified_badge_active", False)
            and _is_feature_active("premium_verified_badge_enabled")
            and _is_feature_active("premium_verified_enabled")
        ),
        premium_verified_badge_name=PREMIUM_VERIFIED_BADGE_NAME,
        premium_verified_badge_subtitle=PREMIUM_VERIFIED_BADGE_SUBTITLE,
        premium_verified_reason=str(user.get("premium_verified_last_reason") or ""),
        premium_verified_streak=premium_verified_streak,
        premium_verified_streak_target=premium_verified_target,
        premium_verified_progress=f"{min(premium_verified_streak, premium_verified_target)}/{premium_verified_target}",
        premium_verified_bonus_score_applied=0.0,
        premium_verified_cr_efficiency_multiplier=1.0,
        is_verified=bool(user.get("is_verified", False)),
        verification_level=verification_level,
        verification_score_snapshot=verification_score_snapshot,
        reputation_credits=int(user.get("reputation_credits", 0) or 0),
        cr_matching_boost_active=_is_cr_effect_active_until(user.get("cr_matching_boost_until")),
        cr_early_wave_active=_is_cr_effect_active_until(user.get("cr_early_wave_until")),
        cr_visual_highlight_active=_is_cr_effect_active_until(user.get("cr_visual_highlight_until")),
        horarios_disponiveis=schedule_payload.get("horarios_disponiveis", {}),
        availability_blocks=schedule_payload.get("availability_blocks", []),
        availability_daily_capacity_overrides=schedule_payload.get("availability_daily_capacity_overrides", {}),
        unavailable_until=schedule_payload.get("unavailable_until"),
    )


async def _get_current_user(request: Request) -> dict:
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Não autenticado")

    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Token inválido")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessão expirada")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

    user = await db.users.find_one({"id": payload.get("sub")}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Usuário não encontrado")
    if user.get("isActive", True) is False:
        raise HTTPException(status_code=403, detail="Conta inativa")
    return user


async def _require_admin(request: Request) -> dict:
    user = await _get_current_user(request)
    if not user.get("isAdmin", False):
        raise HTTPException(status_code=403, detail="Acesso restrito ao administrador")
    if user.get("isActive", True) is False:
        raise HTTPException(status_code=403, detail="Conta administrativa inativa")
    return user


async def _require_admin_permission(request: Request, permission: str) -> dict:
    user = await _require_admin(request)
    if not _has_admin_permission(user, permission):
        raise HTTPException(status_code=403, detail="Sem permissão para este módulo")
    return user


async def _require_role(request: Request, roles: List[str]) -> dict:
    user = await _get_current_user(request)
    allowed_roles = set(roles)
    if "admin" in allowed_roles:
        allowed_roles.add("super_admin")
    if user.get("role") not in allowed_roles:
        raise HTTPException(status_code=403, detail="Acesso não permitido para este perfil")
    return user


def _owner_profile_id_for_user(user_id: str) -> str:
    return f"owner-{user_id}"


def _is_admin_user(user: dict) -> bool:
    return bool(user.get("isAdmin", False))


def _pet_belongs_to_user(pet: dict, user: dict) -> bool:
    if _is_admin_user(user):
        return True

    if pet.get("owner_user_id") == user.get("id"):
        return True

    pet_owner_name = str(pet.get("owner_name", "")).strip().lower()
    user_name = str(user.get("full_name", "")).strip().lower()
    return bool(pet_owner_name and user_name and pet_owner_name == user_name)


def _walk_participant_user_ids(walk: dict) -> List[str]:
    raw_values = walk.get("participant_user_ids")
    if not isinstance(raw_values, list):
        return []
    return [str(item) for item in raw_values if item]


def _walk_belongs_to_user(walk: dict, user: dict) -> bool:
    if _is_admin_user(user):
        return True

    role = user.get("role")
    user_id = user.get("id")
    user_name = str(user.get("full_name", "")).strip()

    if role == "passeador":
        return walk.get("walker_user_id") == user_id or walk.get("walker_name") == user_name

    participant_ids = _walk_participant_user_ids(walk)
    return (
        walk.get("client_user_id") == user_id
        or user_id in participant_ids
        or walk.get("client_name") == user_name
    )


async def _get_walk_for_user_or_403(walk_id: str, user: dict) -> dict:
    walk = await _get_walk_or_404(walk_id)
    if not _walk_belongs_to_user(walk, user):
        raise HTTPException(status_code=403, detail="Sem permissão para acessar este passeio")
    return walk


def _login_identifier(request: Request, email: str) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    ip = forwarded_for.split(",")[0].strip() if forwarded_for else (request.client.host if request.client else "unknown")
    return f"{ip}:{email.lower().strip()}"


async def _create_notification(user_id: str, role: str, title: str, message: str, category: str):
    row = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "role": role,
        "title": title,
        "message": message,
        "category": category,
        "read": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.notifications.insert_one(row)


async def _notify_admins(title: str, message: str, category: str):
    admins = await db.users.find({"isAdmin": True, "isActive": True}, {"_id": 0}).to_list(100)
    for admin in admins:
        await _create_notification(
            user_id=admin["id"],
            role=admin.get("role", "admin"),
            title=title,
            message=message,
            category=category,
        )


async def _get_operational_settings() -> dict:
    row = await db.operational_settings.find_one({"id": "pricing"}, {"_id": 0})
    if row:
        percent = _coerce_float(row.get("premiumRepassePercentual", DEFAULT_PREMIUM_PAYOUT_PERCENT), DEFAULT_PREMIUM_PAYOUT_PERCENT)
        row["premiumRepassePercentual"] = min(80.0, max(70.0, percent))
        return row
    return {"id": "pricing", "premiumRepassePercentual": DEFAULT_PREMIUM_PAYOUT_PERCENT}


async def _create_auth_session(user: dict) -> "AuthTokensResponse":
    access_token = _create_access_token(user["id"], user["email"], user["role"])
    refresh_token = _create_refresh_token(user["id"])
    return AuthTokensResponse(user=_user_to_auth_response(user), access_token=access_token, refresh_token=refresh_token)


class WalkerResponse(BaseModel):
    id: str
    name: str
    photo_url: str
    possuiVeiculo: bool = False
    aceitaDeslocamentoPremium: bool = False
    raioMaximoPremiumKm: float = 0.0
    ativoParaTransportePremium: bool = False
    has_vehicle: bool = False
    accepts_pet_transport: bool = False
    vehicle_type: str = ""
    transport_enabled: bool = False
    is_transport_eligible: bool = False
    has_water: bool = False
    has_bowl: bool = False
    has_bags: bool = False
    has_first_aid: bool = False
    has_towel: bool = False
    has_extra_leash: bool = False
    has_premium_items: bool = False
    kit_complete: bool = False
    kit_basic_complete: bool = False
    kit_essential_complete: bool = False
    kit_premium: bool = False
    kit_level: int = 0
    kit_labels: List[str] = Field(default_factory=list)
    kit_photo_urls: List[str] = Field(default_factory=list)
    premium_verified_badge_active: bool = False
    premium_verified_badge_name: str = PREMIUM_VERIFIED_BADGE_NAME
    premium_verified_badge_subtitle: str = PREMIUM_VERIFIED_BADGE_SUBTITLE
    premium_verified_reason: str = ""
    premium_verified_streak: int = 0
    premium_verified_streak_target: int = DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET
    premium_verified_progress: str = "0/3"
    premium_verified_bonus_score_applied: float = 0.0
    premium_verified_priority_bonus_applied: float = 0.0
    premium_verified_cr_efficiency_multiplier: float = 1.0
    is_verified: bool = False
    verification_level: Literal["NONE", "VERIFIED", "PLUS", "PREMIUM"] = VERIFICATION_LEVEL_NONE
    verification_score_snapshot: int = 0
    reputation_credits: int = 0
    cr_matching_boost_active: bool = False
    cr_early_wave_active: bool = False
    cr_visual_highlight_active: bool = False
    availability_days: List[Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]] = Field(default_factory=list)
    availability_start_time: str = ""
    availability_end_time: str = ""
    horarios_disponiveis: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)
    availability_capacity_by_period: Dict[Literal["manha", "tarde", "noite"], int] = Field(default_factory=dict)
    availability_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    unavailable_until: Optional[str] = None
    rating_avg: float = 0.0
    rating_count: int = 0
    public_rating_label: str = "Novo na plataforma"
    public_badge: str = ""
    quality_status: Literal["ativo_premium", "ativo", "em_observacao", "restrito", "suspenso"] = QUALITY_STATUS_ACTIVE
    score_final: float = 75.0
    score_base_component: float = 0.0
    score_distancia_component: float = 0.0
    score_confiabilidade_component: float = 0.0
    score_disponibilidade_component: float = 0.0
    premium_boost_points: float = 0.0
    proximity_boost_points: float = 0.0
    match_score: float = 75.0
    ranking_score_final: float = 75.0
    proximity_score: float = 60.0
    distance_proxy_km: float = 0.0
    availability_score: float = 60.0
    load_balance_score: float = 100.0
    walker_level: Literal["bronze", "silver", "gold", "prata", "ouro", "elite"] = WALKER_LEVEL_BRONZE
    level_priority_bonus: float = 0.0
    mission_bonus_points: float = 0.0
    mission_priority_points: float = 0.0
    weekly_tip_total: float = 0.0
    weekly_tip_goal_reached: bool = False
    gamification_badges: List[str] = Field(default_factory=list)
    completed_walks: int = 0
    severe_delay_rate: float = 0.0
    no_show_rate: float = 0.0
    availability_label: str = "Disponibilidade em análise"
    proximity_label: str = "Região próxima"
    high_demand_context: bool = False
    dynamic_price_multiplier: float = 1.0
    dynamic_price_reason: str = "Preço padrão"
    conversion_priority_score: float = 0.0
    reliability_priority_score: float = 0.0
    margin_priority_score: float = 0.0
    calendar_priority_score: float = 0.0
    business_priority_score: float = 0.0
    recommended_label: str = ""
    value_context_labels: List[str] = Field(default_factory=list)
    is_preferred_rebooking: bool = False
    region: str = ""
    selection_reason: str = ""
    available_slots: List[str] = Field(default_factory=list)
    selected_slot: str = ""
    is_available_exact_time: bool = False
    is_top_match: bool = False
    is_eligible_for_matching: bool = True
    within_primary_radius: bool = True
    wave_hint: int = 0
    highlight_label: str = ""
    is_premium_featured: bool = False


class WalkerAvailabilitySlotsResponse(BaseModel):
    walker_id: str
    date: str
    weekday: Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
    duration_minutes: Literal[30, 45, 60]
    available_slots: List[str] = Field(default_factory=list)


class WalkerAvailabilityPeriod(BaseModel):
    start_time: str = ""
    end_time: str = ""


class WalkerAvailabilityUpdatePayload(BaseModel):
    availability_days: List[Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]] = Field(min_length=1)
    availability_start_time: str = Field(min_length=5, max_length=5)
    availability_end_time: str = Field(min_length=5, max_length=5)
    availability_periods: Optional[Dict[Literal["manha", "tarde", "noite"], WalkerAvailabilityPeriod]] = None
    availability_capacity_by_period: Optional[Dict[Literal["manha", "tarde", "noite"], int]] = None
    availability_daily_capacity_overrides: Optional[Dict[str, int]] = None


class WalkerAvailabilityBlock(BaseModel):
    id: str
    start_at: str
    end_at: str
    is_full_day: bool = False
    reason: str = ""
    block_type: Literal["manual", "quick_unavailable"] = "manual"
    created_at: str


class WalkerAvailabilityBlockCreatePayload(BaseModel):
    start_date: str = Field(min_length=10, max_length=10)
    start_time: str = Field(default="00:00", min_length=5, max_length=5)
    end_date: Optional[str] = Field(default=None, min_length=10, max_length=10)
    end_time: Optional[str] = Field(default=None, min_length=5, max_length=5)
    full_day: bool = False
    reason: str = Field(default="", max_length=120)


class WalkerQuickUnavailablePayload(BaseModel):
    mode: Literal["today", "until_date", "custom_period"]
    until_date: Optional[str] = Field(default=None, min_length=10, max_length=10)
    start_date: Optional[str] = Field(default=None, min_length=10, max_length=10)
    start_time: Optional[str] = Field(default=None, min_length=5, max_length=5)
    end_date: Optional[str] = Field(default=None, min_length=10, max_length=10)
    end_time: Optional[str] = Field(default=None, min_length=5, max_length=5)
    reason: str = Field(default="Indisponibilidade temporária", max_length=120)


class WalkerAvailabilitySettingsResponse(BaseModel):
    availability_days: List[Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]] = Field(default_factory=list)
    availability_start_time: str = ""
    availability_end_time: str = ""
    availability_periods: Dict[Literal["manha", "tarde", "noite"], WalkerAvailabilityPeriod] = Field(default_factory=dict)
    availability_capacity_by_period: Dict[Literal["manha", "tarde", "noite"], int] = Field(default_factory=dict)
    availability_daily_capacity_overrides: Dict[str, int] = Field(default_factory=dict)
    blocks: List[WalkerAvailabilityBlock] = Field(default_factory=list)
    unavailable_until: Optional[str] = None
    is_temporarily_unavailable: bool = False


class WalkerRatingItem(BaseModel):
    walk_id: str
    rating: int
    comment: str = ""
    client_name: str = ""
    created_at: str = ""


class WalkerRatingSummaryResponse(BaseModel):
    rating_avg: float = 0.0
    rating_count: int = 0
    recent_reviews: List[WalkerRatingItem] = Field(default_factory=list)
    encouragement_message: str = ""


class WalkerQualityDashboardResponse(BaseModel):
    quality_status: Literal["ativo_premium", "ativo", "em_observacao", "restrito", "suspenso"]
    status_reason: str
    rating_avg: float
    rating_recent_avg: float
    rating_weighted_avg: float
    rating_count: int
    public_rating_label: str = "Novo na plataforma"
    public_badge: str = ""
    score_base: float = 0.0
    score_final: float = 0.0
    score_trend: float = 0.0
    recency_factor: float = 1.0
    consistency_factor: float = 1.0
    severe_penalty_factor: float = 1.0
    status_penalty_factor: float = 1.0
    accepted_walks: int = 0
    completed_walks: int
    severe_delay_rate: float
    no_show_rate: float
    cancel_rate: float
    recent_comments: List[str] = Field(default_factory=list)
    encouragement_message: str = ""
    monitor_target_walks: int = 0
    monitor_completed_walks: int = 0
    monitor_remaining_walks: int = 0
    monitor_severity: str = "padrao"
    monitor_reset_count: int = 0
    monitor_severe_delay_incidents: int = 0
    recovery_required: bool = False
    course_completed: bool = False
    quiz_passed: bool = False
    quiz_attempts: int = 0
    review_recommended: bool = False
    recent_history: List[str] = Field(default_factory=list)
    instructions: List[str] = Field(default_factory=list)


class WalkerQualityCourseCompletePayload(BaseModel):
    checklist_confirmed: bool = False


class WalkerQualityQuizSubmitPayload(BaseModel):
    answers: List[int] = Field(min_length=5, max_length=5)


class AuthRegisterPayload(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=6, max_length=120)
    role: Literal["cliente"] = "cliente"
    accepted_terms: bool = False
    accepted_privacy: bool = False
    accepted_lgpd: bool = False


class AuthLoginPayload(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=6, max_length=120)


class ForgotPasswordPayload(BaseModel):
    email: str = Field(min_length=5, max_length=120)


class ResetPasswordPayload(BaseModel):
    token: str = Field(min_length=10, max_length=400)
    new_password: str = Field(min_length=6, max_length=120)


class AuthUserResponse(BaseModel):
    id: str
    full_name: str
    email: str
    role: Literal["cliente", "passeador", "admin", "super_admin"]
    isAdmin: bool
    permissions: Dict[str, bool] = Field(default_factory=_empty_permissions_map)
    isActive: bool = True
    hasSeguro: bool = False
    region: str = ""
    possuiVeiculo: bool = False
    aceitaDeslocamentoPremium: bool = False
    raioMaximoPremiumKm: float = 0.0
    ativoParaTransportePremium: bool = False
    has_vehicle: bool = False
    accepts_pet_transport: bool = False
    vehicle_type: str = ""
    transport_enabled: bool = False
    is_transport_eligible: bool = False
    has_water: bool = False
    has_bowl: bool = False
    has_bags: bool = False
    has_first_aid: bool = False
    has_towel: bool = False
    has_extra_leash: bool = False
    has_premium_items: bool = False
    kit_complete: bool = False
    kit_basic_complete: bool = False
    kit_essential_complete: bool = False
    kit_premium: bool = False
    kit_level: int = 0
    kit_labels: List[str] = Field(default_factory=list)
    kit_photo_urls: List[str] = Field(default_factory=list)
    premium_verified_badge_active: bool = False
    premium_verified_badge_name: str = PREMIUM_VERIFIED_BADGE_NAME
    premium_verified_badge_subtitle: str = PREMIUM_VERIFIED_BADGE_SUBTITLE
    premium_verified_reason: str = ""
    premium_verified_streak: int = 0
    premium_verified_streak_target: int = DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET
    premium_verified_progress: str = "0/3"
    premium_verified_bonus_score_applied: float = 0.0
    premium_verified_cr_efficiency_multiplier: float = 1.0
    is_verified: bool = False
    verification_level: Literal["NONE", "VERIFIED", "PLUS", "PREMIUM"] = VERIFICATION_LEVEL_NONE
    verification_score_snapshot: int = 0
    reputation_credits: int = 0
    cr_matching_boost_active: bool = False
    cr_early_wave_active: bool = False
    cr_visual_highlight_active: bool = False
    horarios_disponiveis: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)
    availability_blocks: List[Dict[str, Any]] = Field(default_factory=list)
    availability_daily_capacity_overrides: Dict[str, int] = Field(default_factory=dict)
    unavailable_until: Optional[str] = None


class AuthTokensResponse(BaseModel):
    user: AuthUserResponse
    access_token: str
    refresh_token: str


class SupportTicketCreatePayload(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=3, max_length=2000)


class SupportTicketReplyPayload(BaseModel):
    message: str = Field(min_length=3, max_length=2000)
    status: Literal["aberto", "em_andamento", "resolvido"]


class SupportTicketResponse(BaseModel):
    id: str
    user_id: str
    user_name: str
    user_email: str
    subject: str
    message: str
    status: Literal["aberto", "em_andamento", "resolvido"]
    admin_reply: str = ""
    created_at: str
    updated_at: str


class NotificationResponse(BaseModel):
    id: str
    user_id: str
    role: Literal["cliente", "passeador", "admin", "super_admin"]
    title: str
    message: str
    category: str
    read: bool
    created_at: str


class WalkerRequestResponse(BaseModel):
    id: str
    pet_name: str
    neighborhood: str
    approx_location: str
    walk_date: str
    walk_time: str
    duration_minutes: Literal[30, 45, 60]
    walk_type: Literal["Individual", "Compartilhado"]
    base_price: float = 0.0
    total_price: float = 0.0
    status: Literal["pending", "accepted", "rejected", "ignored", "canceled", "expired"]
    matching_request_id: Optional[str] = None
    respond_until: str
    created_at: str


class WalkerRequestDecisionPayload(BaseModel):
    decision: Literal["accept", "reject"]


class WalkerAlertResponse(BaseModel):
    id: str
    title: str
    message: str
    tone: Literal["warning", "success", "info"]
    icon: str
    active: bool
    created_at: str


class AdminAccountCreatePayload(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=6, max_length=120)
    role: Literal["super_admin", "admin"] = "admin"
    isActive: bool = True
    permissions: Dict[str, bool] = Field(default_factory=_default_admin_permissions_map)


class AdminAccountUpdatePayload(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    role: Optional[Literal["super_admin", "admin"]] = None
    isActive: Optional[bool] = None
    permissions: Optional[Dict[str, bool]] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=120)


class AdminAccountResponse(BaseModel):
    id: str
    full_name: str
    email: str
    role: Literal["super_admin", "admin"]
    isActive: bool
    permissions: Dict[str, bool]
    created_by: Optional[str] = None
    created_at: str
    updated_at: str


class AdminActionLogResponse(BaseModel):
    id: str
    actor_admin_id: str
    actor_email: str
    action: str
    target_admin_id: str
    changes: dict
    created_at: str


class PendingActionResponse(BaseModel):
    id: str
    type: str
    description: str
    action_route: str
    created_at: str


class AdminMessageCreatePayload(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=3, max_length=2000)
    audience: Literal["todos_usuarios", "usuarios_inativos", "passeadores"]


class AdminMessageCampaignResponse(BaseModel):
    id: str
    title: str
    message: str
    audience: Literal["todos_usuarios", "usuarios_inativos", "passeadores"]
    sent_count: int
    created_by: str
    created_at: str


class CouponCreatePayload(BaseModel):
    code: str = Field(min_length=3, max_length=40)
    discount_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    discount_fixed: Optional[float] = Field(default=None, ge=0.0)
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    max_global_uses: Optional[int] = Field(default=None, ge=1)
    max_uses_per_user: int = Field(default=1, ge=1)
    applicable_walk_types: List[Literal["Individual", "Compartilhado"]] = Field(default_factory=lambda: COUPON_WALK_TYPES.copy())
    is_active: bool = True


class CouponUpdatePayload(BaseModel):
    discount_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    discount_fixed: Optional[float] = Field(default=None, ge=0.0)
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    max_global_uses: Optional[int] = Field(default=None, ge=1)
    max_uses_per_user: Optional[int] = Field(default=None, ge=1)
    applicable_walk_types: Optional[List[Literal["Individual", "Compartilhado"]]] = None
    is_active: Optional[bool] = None


class CouponValidatePayload(BaseModel):
    code: str = Field(min_length=3, max_length=40)
    walk_type: Literal["Individual", "Compartilhado"]
    subtotal: float = Field(ge=0.0)


class CouponResponse(BaseModel):
    id: str
    code: str
    discount_percent: float = 0.0
    discount_fixed: float = 0.0
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    max_global_uses: int = 0
    max_uses_per_user: int = 1
    used_count: int = 0
    applicable_walk_types: List[Literal["Individual", "Compartilhado"]] = Field(default_factory=list)
    is_active: bool = True
    legacy_mode: bool = False
    created_at: str
    updated_at: str


class CouponValidateResponse(BaseModel):
    code: str
    discount_percent: float = 0.0
    discount_fixed: float = 0.0
    discount_amount: float = 0.0
    subtotal: float = 0.0
    total_after_discount: float = 0.0
    max_uses_per_user: int = 1
    remaining_uses_for_user: Optional[int] = None
    valid_until: Optional[str] = None
    applicable_walk_types: List[Literal["Individual", "Compartilhado"]] = Field(default_factory=list)


class CouponFraudAlertResponse(BaseModel):
    id: str
    alert_type: str
    severity: Literal["low", "medium", "high"] = "medium"
    message: str
    coupon_id: Optional[str] = None
    coupon_code: Optional[str] = None
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    user_phone: Optional[str] = None
    device_id: Optional[str] = None
    ip_address: Optional[str] = None
    blocked: bool = False
    created_at: str


class CouponFraudAggregateResponse(BaseModel):
    key: str
    uses_count: int
    unique_users: int
    unique_coupons: int


class CouponAntiAbuseOverviewResponse(BaseModel):
    alerts: List[CouponFraudAlertResponse] = Field(default_factory=list)
    usage_by_user: List[CouponFraudAggregateResponse] = Field(default_factory=list)
    usage_by_device: List[CouponFraudAggregateResponse] = Field(default_factory=list)
    usage_by_ip: List[CouponFraudAggregateResponse] = Field(default_factory=list)


class WalkCancelPayload(BaseModel):
    tipoCancelamento: Literal["cliente", "passeador"]
    motivoCancelamento: str = Field(min_length=3, max_length=400)


class WalkCreate(BaseModel):
    pet_name: str = Field(min_length=1, max_length=80)
    pet_id: Optional[str] = None
    second_pet_id: Optional[str] = None
    client_name: str = Field(min_length=1, max_length=80)
    walk_date: str = Field(min_length=10, max_length=10)
    walk_time: str = Field(min_length=5, max_length=5)
    duration_minutes: Literal[30, 45, 60]
    walk_type: Literal["Individual", "Compartilhado"]
    tipo_passeio: Literal["padrao", "transporte"] = "padrao"
    modo_inicio_passeio: Literal["endereco_tutor", "ponto_encontro", "deslocamento_premium"] = START_MODE_TUTOR_ADDRESS
    usar_ponto_retirada_alternativo: bool = False
    base_latitude: Optional[float] = None
    base_longitude: Optional[float] = None
    ponto_retirada_alternativo_nome: str = ""
    ponto_retirada_alternativo_referencia: str = ""
    ponto_retirada_alternativo_latitude: Optional[float] = None
    ponto_retirada_alternativo_longitude: Optional[float] = None
    ponto_encontro_nome: str = ""
    ponto_encontro_referencia: str = ""
    ponto_encontro_latitude: Optional[float] = None
    ponto_encontro_longitude: Optional[float] = None
    local_destino_nome: str = ""
    local_destino_referencia: str = ""
    local_destino_latitude: Optional[float] = None
    local_destino_longitude: Optional[float] = None
    shared_context: Optional[Literal["same_household", "other_client"]] = None
    walker_id: str = Field(min_length=1, max_length=80)
    pickup_street: str = Field(min_length=2, max_length=120)
    pickup_number: str = Field(min_length=1, max_length=15)
    pickup_neighborhood: str = Field(min_length=2, max_length=80)
    pickup_complement: str = ""
    location_reference: str = ""
    coupon_code: str = Field(default="", max_length=40)
    pet_behavior_notes: str = ""
    notes: str = ""


class MatchingWalkCreatePayload(BaseModel):
    pet_name: str = Field(min_length=1, max_length=80)
    pet_id: Optional[str] = None
    second_pet_id: Optional[str] = None
    client_name: str = Field(min_length=1, max_length=80)
    walk_date: str = Field(min_length=10, max_length=10)
    walk_time: str = Field(min_length=5, max_length=5)
    duration_minutes: Literal[30, 45, 60]
    walk_type: Literal["Individual", "Compartilhado"]
    tipo_passeio: Literal["padrao", "transporte"] = "padrao"
    modo_inicio_passeio: Literal["endereco_tutor", "ponto_encontro", "deslocamento_premium"] = START_MODE_TUTOR_ADDRESS
    usar_ponto_retirada_alternativo: bool = False
    ponto_retirada_alternativo_nome: str = ""
    ponto_retirada_alternativo_referencia: str = ""
    ponto_encontro_nome: str = ""
    ponto_encontro_referencia: str = ""
    local_destino_nome: str = ""
    local_destino_referencia: str = ""
    pickup_street: str = Field(min_length=2, max_length=120)
    pickup_number: str = Field(min_length=1, max_length=15)
    pickup_neighborhood: str = Field(min_length=2, max_length=80)
    pickup_complement: str = ""
    location_reference: str = ""
    pet_behavior_notes: str = ""
    notes: str = ""


class MatchingWalkRequestResponse(BaseModel):
    id: str
    status: Literal["searching", "matched", "expired", "canceled"]
    current_wave: int = 1
    client_message: str = "Buscando melhor passeador próximo..."
    selected_walker_user_id: Optional[str] = None
    selected_walker_name: Optional[str] = None
    accepted_walk_id: Optional[str] = None
    confirmed_in_seconds: Optional[int] = None
    rejected_count: int = 0
    ignored_count: int = 0
    attempted_count: int = 0
    selected_position: Optional[int] = None
    min_score_threshold: float = MATCH_MIN_SCORE
    fallback_mode: bool = False
    marketplace_context: Literal["critico", "equilibrado", "sobra_oferta"] = MARKETPLACE_CONTEXT_BALANCED
    demand_active: int = 0
    supply_active: int = 0
    demand_supply_ratio: float = 0.0
    dynamic_price_multiplier: float = 1.0
    dynamic_price_reason: str = "Preço padrão"
    created_at: str
    updated_at: str


class ProtectedChatMessageCreatePayload(BaseModel):
    conversation_id: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=1, max_length=1200)


class ProtectedChatMessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender_user_id: str
    sender_role: str
    message: str
    blocked: bool = False
    block_reasons: List[str] = Field(default_factory=list)
    created_at: str


class ProtectedChatSendResponse(BaseModel):
    sent: bool
    blocked: bool
    warning_message: Optional[str] = None
    message: Optional[ProtectedChatMessageResponse] = None


class AdminDisintermediationUserResponse(BaseModel):
    user_id: str
    role: str
    name: str
    region: str
    flagged: bool
    flag_reason: Optional[str] = None
    flagged_at: Optional[str] = None
    expires_at: Optional[str] = None
    contact_attempts_7d: int = 0
    cancel_rate_14d: float = 0.0


class AdminDisintermediationOverviewResponse(BaseModel):
    total_flagged_users: int = 0
    total_contact_attempts_7d: int = 0
    users: List[AdminDisintermediationUserResponse] = Field(default_factory=list)


class AdminDisintermediationActionPayload(BaseModel):
    action: Literal["warn", "limit", "suspend", "clear_flag"]
    note: str = ""


class SystemAlertResponse(BaseModel):
    alert_id: str
    tipo_alerta: str
    categoria: Literal["operacional", "financeiro", "comportamental", "sistemico"] = "operacional"
    prioridade_score: float = 0.0
    nivel_gravidade: Literal[1, 2, 3, 4]
    status: Literal["pendente", "executado", "ignorado", "revisar_depois"]
    user_id: str
    user_role: str
    contexto: str = ""
    mensagem: str
    acao_sugerida: str
    acao_final: Optional[str] = None
    auto_executado: bool = False
    justificativa_admin: Optional[str] = None
    ocorrencias: int = 1
    criado_em: str
    atualizado_em: str


class SystemAlertDecisionPayload(BaseModel):
    decision: Literal["confirm", "review_later", "ignore"]
    justification: str = ""


class SystemAlertPrioritySettingsResponse(BaseModel):
    weights: Dict[str, float]
    grouping_windows_hours: Dict[str, int]
    systemic_region_failure_threshold: int
    systemic_overload_threshold: int
    updated_at: str


class SystemAlertPrioritySettingsUpdatePayload(BaseModel):
    weights: Optional[Dict[str, float]] = None
    grouping_windows_hours: Optional[Dict[str, int]] = None
    systemic_region_failure_threshold: Optional[int] = None
    systemic_overload_threshold: Optional[int] = None


class PlanSimulationPayload(BaseModel):
    frequencia_semanal: Literal[1, 2, 3, 4, 5]
    duracao_plano: Literal["mensal", "trimestral", "semestral"]
    duracao_passeio: Literal[30, 45, 60]


class PlanSimulationResponse(BaseModel):
    frequencia_semanal: int
    duracao_plano: Literal["mensal", "trimestral", "semestral"]
    duracao_passeio: Literal[30, 45, 60]
    valor_base_por_passeio: float
    total_passeios: int
    desconto_frequencia_percent: float
    desconto_plano_percent: float
    desconto_total_percent: float
    desconto_reduzido_por_margem: bool = False
    margem_minima_percent: float = 15.0
    margem_estimada_percent: float = 0.0
    valor_total_sem_desconto: float
    valor_total_com_desconto: float
    valor_por_passeio: float
    economia: float
    comparacao_avulso_total: float
    comparacao_avulso_por_passeio: float
    mensagem_economia: str
    ready_for_subscription: bool = True
    subscription_payload: Dict[str, Any] = Field(default_factory=dict)


class PlanSubscriptionIntentPayload(BaseModel):
    frequencia_semanal: Literal[1, 2, 3, 4, 5]
    duracao_plano: Literal["mensal", "trimestral", "semestral"]
    duracao_passeio: Literal[30, 45, 60]


class PlanSubscriptionIntentResponse(BaseModel):
    intent_id: str
    status: Literal["pending_provider_integration"]
    ready_for_subscription: bool = True
    summary: PlanSimulationResponse


class FeatureFlagResponse(BaseModel):
    feature_name: str
    title: str
    group: Literal[
        "engajamento_cliente",
        "monetizacao_incentivos",
        "visibilidade_ranking",
        "inteligencia_marketplace",
    ]
    is_active: bool
    is_visible: bool
    updated_at: str
    updated_by: str


class FeatureFlagUpdatePayload(BaseModel):
    is_active: Optional[bool] = None
    is_visible: Optional[bool] = None


class FeatureFlagsVisibilityResponse(BaseModel):
    flags: Dict[str, bool]


class MarketplaceRegionalRulePayload(BaseModel):
    city: str = ""
    neighborhood: str = ""
    priority_bonus_points: float = Field(default=0.0, ge=0.0, le=10.0)
    cr_weight_percent: Optional[float] = Field(default=None, ge=0.0, le=MARKETPLACE_MAX_CR_WEIGHT_PERCENT)
    context_sensitivity: Optional[float] = Field(default=None, ge=0.5, le=3.0)
    enabled: bool = True


class MarketplaceIntelligenceSettingsResponse(BaseModel):
    id: str
    mode: Literal["automatico", "manual_assistido", "manual_total"] = MARKETPLACE_MODE_AUTOMATIC
    polling_seconds: int = MARKETPLACE_DEFAULT_POLLING_SECONDS
    cr_weight_percent: float = MARKETPLACE_DEFAULT_CR_WEIGHT_PERCENT
    cr_boost_cap_points: float = 12.0
    context_sensitivity: float = 1.0
    critical_ratio_threshold: float = MARKETPLACE_DEFAULT_CRITICAL_RATIO
    balanced_ratio_floor: float = MARKETPLACE_DEFAULT_BALANCED_RATIO_FLOOR
    balanced_ratio_ceil: float = MARKETPLACE_DEFAULT_BALANCED_RATIO_CEIL
    low_supply_cr_multiplier: float = 1.2
    high_supply_cr_multiplier: float = 0.7
    low_supply_cr_gain_multiplier: float = 1.2
    high_supply_quality_floor: float = 60.0
    low_supply_min_score_relaxation: float = 5.0
    high_supply_min_score_hardening: float = 5.0
    low_supply_wave_extra_candidates: int = 2
    high_supply_wave_reduction: int = 1
    critical_acceptance_seconds: float = MARKETPLACE_CRITICAL_ACCEPTANCE_SECONDS
    critical_match_rate_threshold: float = MARKETPLACE_CRITICAL_MATCH_RATE
    surplus_match_rate_threshold: float = MARKETPLACE_SURPLUS_MATCH_RATE
    dynamicPricingEnabled: bool = False
    dynamicPricingMode: Literal["off", "shadow", "active"] = DYNAMIC_PRICING_MODE_OFF
    regional_rules: List[MarketplaceRegionalRulePayload] = Field(default_factory=list)
    updated_at: str
    updated_by: str


class MarketplaceIntelligenceSettingsUpdatePayload(BaseModel):
    mode: Optional[Literal["automatico", "manual_assistido", "manual_total"]] = None
    polling_seconds: Optional[int] = Field(default=None, ge=10, le=300)
    cr_weight_percent: Optional[float] = Field(default=None, ge=0.0, le=MARKETPLACE_MAX_CR_WEIGHT_PERCENT)
    cr_boost_cap_points: Optional[float] = Field(default=None, ge=1.0, le=30.0)
    context_sensitivity: Optional[float] = Field(default=None, ge=0.5, le=3.0)
    critical_ratio_threshold: Optional[float] = Field(default=None, ge=1.0, le=5.0)
    balanced_ratio_floor: Optional[float] = Field(default=None, ge=0.1, le=2.0)
    balanced_ratio_ceil: Optional[float] = Field(default=None, ge=0.2, le=4.0)
    low_supply_cr_multiplier: Optional[float] = Field(default=None, ge=1.0, le=3.0)
    high_supply_cr_multiplier: Optional[float] = Field(default=None, ge=0.1, le=1.0)
    low_supply_cr_gain_multiplier: Optional[float] = Field(default=None, ge=1.0, le=3.0)
    high_supply_quality_floor: Optional[float] = Field(default=None, ge=55.0, le=90.0)
    low_supply_min_score_relaxation: Optional[float] = Field(default=None, ge=0.0, le=15.0)
    high_supply_min_score_hardening: Optional[float] = Field(default=None, ge=0.0, le=15.0)
    low_supply_wave_extra_candidates: Optional[int] = Field(default=None, ge=0, le=5)
    high_supply_wave_reduction: Optional[int] = Field(default=None, ge=0, le=3)
    critical_acceptance_seconds: Optional[float] = Field(default=None, ge=30.0, le=900.0)
    critical_match_rate_threshold: Optional[float] = Field(default=None, ge=0.1, le=1.0)
    surplus_match_rate_threshold: Optional[float] = Field(default=None, ge=0.1, le=1.0)
    dynamicPricingEnabled: Optional[bool] = None
    dynamicPricingMode: Optional[Literal["off", "shadow", "active"]] = None
    regional_rules: Optional[List[MarketplaceRegionalRulePayload]] = None


class MarketplaceContextMetricsResponse(BaseModel):
    city: str = ""
    neighborhood: str = ""
    mode: Literal["automatico", "manual_assistido", "manual_total"] = MARKETPLACE_MODE_AUTOMATIC
    context_state: Literal["critico", "equilibrado", "sobra_oferta"] = MARKETPLACE_CONTEXT_BALANCED
    demand_active: int = 0
    supply_active: int = 0
    demand_supply_ratio: float = 0.0
    match_rate: float = 0.0
    average_acceptance_seconds: float = 0.0
    cancel_rate: float = 0.0
    cr_usage_24h: int = 0
    updated_at: str


class MarketplaceDecisionAuditResponse(BaseModel):
    id: str
    request_id: Optional[str] = None
    city: str = ""
    neighborhood: str = ""
    context_state: Literal["critico", "equilibrado", "sobra_oferta"] = MARKETPLACE_CONTEXT_BALANCED
    mode: Literal["automatico", "manual_assistido", "manual_total"] = MARKETPLACE_MODE_AUTOMATIC
    min_score_threshold: float = MATCH_MIN_SCORE
    top_limit: int = MATCH_TOP_WAVE4_MAX
    demand_active: int = 0
    supply_active: int = 0
    demand_supply_ratio: float = 0.0
    match_rate: float = 0.0
    average_acceptance_seconds: float = 0.0
    cancel_rate: float = 0.0
    cr_usage_24h: int = 0
    selected_candidates_preview: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: str


class DynamicPricingSettingsResponse(BaseModel):
    id: str = "default"
    dynamicPricingEnabled: bool = False
    dynamicPricingMode: Literal["off", "shadow", "active"] = DYNAMIC_PRICING_MODE_OFF
    low_supply_min_boost: float = DYNAMIC_PRICING_MIN_SUPPLY_BOOST
    low_supply_max_boost: float = DYNAMIC_PRICING_MAX_SUPPLY_BOOST
    high_demand_min_boost: float = DYNAMIC_PRICING_MIN_DEMAND_BOOST
    high_demand_max_boost: float = DYNAMIC_PRICING_MAX_DEMAND_BOOST
    critical_hour_boost: float = DYNAMIC_PRICING_CRITICAL_BOOST
    max_total_boost: float = DYNAMIC_PRICING_MAX_TOTAL_BOOST
    smoothing_limit: float = 0.10
    max_price_cap: float = 40.0
    auto_calibration_enabled: bool = False
    manual_lock: bool = False
    updated_at: str
    updated_by: str


class DynamicPricingSettingsUpdatePayload(BaseModel):
    dynamicPricingEnabled: Optional[bool] = None
    dynamicPricingMode: Optional[Literal["off", "shadow", "active"]] = None
    low_supply_min_boost: Optional[float] = Field(default=None, ge=0.0, le=0.4)
    low_supply_max_boost: Optional[float] = Field(default=None, ge=0.0, le=0.4)
    high_demand_min_boost: Optional[float] = Field(default=None, ge=0.0, le=0.4)
    high_demand_max_boost: Optional[float] = Field(default=None, ge=0.0, le=0.4)
    critical_hour_boost: Optional[float] = Field(default=None, ge=0.0, le=0.2)
    max_total_boost: Optional[float] = Field(default=None, ge=0.0, le=0.4)
    smoothing_limit: Optional[float] = Field(default=None, ge=0.0, le=0.2)
    max_price_cap: Optional[float] = Field(default=None, ge=25.0, le=100.0)
    auto_calibration_enabled: Optional[bool] = None
    manual_lock: Optional[bool] = None


class DynamicPricingCalibrationSnapshotResponse(BaseModel):
    id: str
    created_at: str
    reason: str
    previous_settings: Dict[str, Any] = Field(default_factory=dict)
    new_settings: Dict[str, Any] = Field(default_factory=dict)
    conversion_rate: float = 0.0
    avg_revenue: float = 0.0
    impact_note: str = ""


class DynamicPricingRollbackPayload(BaseModel):
    snapshot_id: str


class DynamicPricingHourMetric(BaseModel):
    hour: str
    attempts: int = 0
    conversion_rate: float = 0.0
    abandonment_rate: float = 0.0


class DynamicPricingMetricsResponse(BaseModel):
    avg_base_price: float = 0.0
    avg_dynamic_price: float = 0.0
    low_supply_slots_percent: float = 0.0
    highest_abandonment_slots: List[DynamicPricingHourMetric] = Field(default_factory=list)
    estimated_shadow_revenue_uplift: float = 0.0
    conversion_by_hour: List[DynamicPricingHourMetric] = Field(default_factory=list)
    total_attempts: int = 0
    mode: Literal["off", "shadow", "active"] = DYNAMIC_PRICING_MODE_OFF


class WalkerLevelSystemSettingsResponse(BaseModel):
    silver_min_walks: int = 10
    silver_min_rating: float = 4.5
    silver_max_cancel_rate: float = 15.0
    silver_min_checklist_streak: int = 5
    silver_min_score_ratio: float = 0.78
    gold_min_walks: int = 25
    gold_min_rating: float = 4.7
    gold_max_cancel_rate: float = 8.0
    gold_min_checklist_streak: int = 12
    gold_min_score_ratio: float = 0.88
    gold_max_infractions: int = 0
    bronze_boost_factor: float = 0.02
    silver_boost_factor: float = 0.04
    gold_boost_factor: float = 0.06
    updated_at: str
    updated_by: str


class WalkerLevelSystemSettingsUpdatePayload(BaseModel):
    silver_min_walks: Optional[int] = Field(default=None, ge=3, le=100)
    silver_min_rating: Optional[float] = Field(default=None, ge=3.0, le=5.0)
    silver_max_cancel_rate: Optional[float] = Field(default=None, ge=1.0, le=50.0)
    silver_min_checklist_streak: Optional[int] = Field(default=None, ge=1, le=50)
    silver_min_score_ratio: Optional[float] = Field(default=None, ge=0.5, le=1.0)
    gold_min_walks: Optional[int] = Field(default=None, ge=10, le=200)
    gold_min_rating: Optional[float] = Field(default=None, ge=3.5, le=5.0)
    gold_max_cancel_rate: Optional[float] = Field(default=None, ge=0.0, le=30.0)
    gold_min_checklist_streak: Optional[int] = Field(default=None, ge=3, le=100)
    gold_min_score_ratio: Optional[float] = Field(default=None, ge=0.6, le=1.0)
    gold_max_infractions: Optional[int] = Field(default=None, ge=0, le=5)
    bronze_boost_factor: Optional[float] = Field(default=None, ge=0.0, le=0.2)
    silver_boost_factor: Optional[float] = Field(default=None, ge=0.0, le=0.2)
    gold_boost_factor: Optional[float] = Field(default=None, ge=0.0, le=0.2)


class PremiumEstimatePayload(BaseModel):
    pickup_street: str = Field(min_length=2, max_length=120)
    pickup_number: str = Field(min_length=1, max_length=15)
    pickup_neighborhood: str = Field(min_length=2, max_length=80)
    pickup_complement: str = ""
    location_reference: str = ""
    local_destino_nome: str = Field(min_length=2, max_length=120)
    local_destino_referencia: str = ""
    duracao_passeio_minutos: Literal[30, 45, 60] = 45


class PremiumEstimateResponse(BaseModel):
    origem: str
    destino: str
    distanciaKm: float
    adicionalDeslocamento: float
    tipoPasseio: Literal["padrao", "transporte"] = "padrao"
    tempoDeslocamentoMinutos: int = 0
    tempoTotalMinutos: int = 0
    rastreamentoReforcado: bool = False
    trackingIntervalSegundos: int = 60
    precisaAnaliseManualDeslocamento: bool
    statusAnaliseDeslocamento: Literal["nao_aplicavel", "aprovado", "aguardando_analise", "rejeitado"]


class WalkStatusUpdate(BaseModel):
    status: Literal[
        "Agendado",
        "Indo buscar o pet",
        "Passeando agora",
        "Finalizado",
        "Cancelado",
        "Não comparecimento do cliente",
        "Não comparecimento do passeador",
        "Pendente de análise",
    ]
    checklist_confirm_water: Optional[bool] = None
    checklist_confirm_bowl: Optional[bool] = None
    checklist_confirm_bags: Optional[bool] = None


class AdminWalkStatusUpdate(BaseModel):
    status: Literal[
        "Agendado",
        "Indo buscar o pet",
        "Passeando agora",
        "Finalizado",
        "Cancelado",
        "Não comparecimento do cliente",
        "Não comparecimento do passeador",
        "Pendente de análise",
    ]


class AdminPremiumAnalysisUpdate(BaseModel):
    statusAnaliseDeslocamento: Literal["aprovado", "rejeitado"]
    adicionalDeslocamento: Optional[float] = None


class PremiumSettingsUpdate(BaseModel):
    premiumRepassePercentual: float = Field(ge=70.0, le=80.0)


class PremiumSettingsResponse(BaseModel):
    premiumRepassePercentual: float


class PetTransportSettingsUpdate(BaseModel):
    pricing_mode: Optional[Literal["fixed", "per_km"]] = None
    transport_fee_fixed: Optional[float] = Field(default=None, ge=0.0, le=200.0)
    transport_fee_per_km: Optional[float] = Field(default=None, ge=0.0, le=50.0)
    auto_approve_distance_km: Optional[float] = Field(default=None, ge=1.0, le=20.0)
    estimated_minutes_per_km: Optional[float] = Field(default=None, ge=1.0, le=10.0)
    tracking_interval_seconds: Optional[int] = Field(default=None, ge=10, le=30)
    pet_transport_enabled_for: Optional[List[str]] = None


class PetTransportSettingsResponse(BaseModel):
    pricing_mode: Literal["fixed", "per_km"]
    transport_fee_fixed: float
    transport_fee_per_km: float
    auto_approve_distance_km: float
    estimated_minutes_per_km: float
    tracking_interval_seconds: int
    pet_transport_enabled_for: List[str] = Field(default_factory=lambda: ["all"])
    updated_at: str
    updated_by: str = "system"


class WalkerTransportSettingsUpdate(BaseModel):
    has_vehicle: Optional[bool] = None
    accepts_pet_transport: Optional[bool] = None
    vehicle_type: Optional[str] = Field(default=None, max_length=80)
    transport_enabled: Optional[bool] = None


class WalkerTransportSettingsResponse(BaseModel):
    has_vehicle: bool
    accepts_pet_transport: bool
    vehicle_type: str = ""
    transport_enabled: bool
    is_transport_eligible: bool
    updated_at: str


class WalkerCertifiedKitUpdate(BaseModel):
    water_sealed: Optional[bool] = None
    water_bowl: Optional[bool] = None
    poop_bags: Optional[bool] = None
    first_aid_kit: Optional[bool] = None
    has_water: Optional[bool] = None
    has_bowl: Optional[bool] = None
    has_bags: Optional[bool] = None
    has_first_aid: Optional[bool] = None
    has_towel: Optional[bool] = None
    has_extra_leash: Optional[bool] = None
    has_premium_items: Optional[bool] = None
    kit_photos_base64: Optional[List[str]] = None
    kit_photo_urls: Optional[List[str]] = None


class WalkerCertifiedKitResponse(BaseModel):
    walker_kit: Dict[str, bool] = Field(default_factory=dict)
    water_sealed: bool = False
    water_bowl: bool = False
    poop_bags: bool = False
    first_aid_kit: bool = False
    kit_complete: bool = False
    has_water: bool
    has_bowl: bool
    has_bags: bool
    has_first_aid: bool
    has_towel: bool
    has_extra_leash: bool
    has_premium_items: bool
    kit_basic_complete: bool
    kit_essential_complete: bool
    kit_premium: bool
    kit_level: int
    kit_labels: List[str] = Field(default_factory=list)
    kit_photos_base64: List[str] = Field(default_factory=list)
    kit_photo_urls: List[str] = Field(default_factory=list)
    kit_missing_reports_count: int = 0
    kit_audit_status: Literal["pendente", "aprovado", "reprovado"] = "pendente"
    kit_audit_note: str = ""
    kit_audited_at: Optional[str] = None
    updated_at: str


class WalkerKitAuditUpdate(BaseModel):
    kit_audit_status: Literal["aprovado", "reprovado"]
    kit_audit_note: str = Field(default="", max_length=300)


class WalkKitChecklistConfirm(BaseModel):
    checklist_confirm_water: bool = False
    checklist_confirm_bowl: bool = False
    checklist_confirm_bags: bool = False
    checklist_confirm_first_aid: bool = False


class WalkKitIssueReportPayload(BaseModel):
    confirm_report: bool = False
    missing_items: List[
        Literal[
            "has_water",
            "has_bowl",
            "has_bags",
            "has_first_aid",
            "has_towel",
            "has_extra_leash",
            "has_premium_items",
            "water_sealed",
            "water_bowl",
            "poop_bags",
            "first_aid_kit",
        ]
    ] = Field(default_factory=list)
    note: str = Field(default="", max_length=400)


class PremiumVerifiedSettingsResponse(BaseModel):
    streak_minimo_para_selo: int = Field(default=DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET, ge=1, le=20)
    bonus_score_base: float = Field(default=DEFAULT_PREMIUM_VERIFIED_BONUS_SCORE, ge=0.0, le=20.0)
    priority_bonus: float = Field(default=DEFAULT_PREMIUM_VERIFIED_PRIORITY_BONUS, ge=0.0, le=10.0)
    cr_efficiency_multiplier: float = Field(default=DEFAULT_PREMIUM_VERIFIED_CR_EFFICIENCY_MULTIPLIER, ge=1.0, le=2.0)
    updated_at: str
    updated_by: str = "system"


class PremiumVerifiedSettingsUpdate(BaseModel):
    streak_minimo_para_selo: Optional[int] = Field(default=None, ge=1, le=20)
    bonus_score_base: Optional[float] = Field(default=None, ge=0.0, le=20.0)
    priority_bonus: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    cr_efficiency_multiplier: Optional[float] = Field(default=None, ge=1.0, le=2.0)


class WalkerPremiumVerifiedStatusResponse(BaseModel):
    badge_active: bool
    badge_name: str = PREMIUM_VERIFIED_BADGE_NAME
    badge_subtitle: str = PREMIUM_VERIFIED_BADGE_SUBTITLE
    reason: str = ""
    streak_atual: int = 0
    streak_minimo_para_selo: int = DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET
    progresso: str = "0/3"
    infracoes_consecutivas: int = 0
    penalty_level: Literal["none", "leve", "moderada", "grave"] = "none"
    bonus_score_base_aplicavel: float = 0.0
    cr_efficiency_multiplier: float = 1.0


class WalkerReputationCreditsUsePayload(BaseModel):
    action: Literal["matching_boost", "early_wave", "visual_highlight"]


class WalkerReputationCreditsResponse(BaseModel):
    reputation_credits: int
    last_credit_update: Optional[str] = None
    daily_uses_count: int = 0
    daily_uses_limit: int = 3
    daily_uses_remaining: int = 3
    verification_level: Literal["NONE", "VERIFIED", "PLUS", "PREMIUM"] = VERIFICATION_LEVEL_NONE
    gain_multiplier: float = 1.0
    premium_cost_multiplier: float = 1.0
    premium_effect_multiplier: float = 1.0
    matching_boost_until: Optional[str] = None
    early_wave_until: Optional[str] = None
    visual_highlight_until: Optional[str] = None
    is_matching_boost_active: bool = False
    is_early_wave_active: bool = False
    is_visual_highlight_active: bool = False
    recent_ledger: List[Dict[str, Any]] = Field(default_factory=list)


class SharedWalkApprovalUpdate(BaseModel):
    approved: bool
    second_pet_id: Optional[str] = None
    walker_id: Optional[str] = None


class WalkExperienceUpdate(BaseModel):
    did_pee: bool
    did_poop: bool


class WalkRatingUpdate(BaseModel):
    rating: Literal[1, 2, 3, 4, 5]
    comment: str = ""


class PetProfileUpdate(BaseModel):
    pet_name: str = Field(min_length=1, max_length=80)
    behavioral_notes: str = ""
    photo_url: str = ""


class PetCreateUpdate(BaseModel):
    pet_name: str = Field(min_length=1, max_length=80)
    behavioral_notes: str = ""
    photo_url: str = ""
    owner_name: str = ""
    gets_along_with_dogs: bool = True
    accepts_shared_walk: bool = True
    pet_size: Literal["Pequeno", "Médio", "Grande"] = "Médio"
    energy_level: Literal["Baixo", "Médio", "Alto"] = "Médio"
    pulls_leash: bool = False
    dog_behavior: Literal["Calmo", "Neutro", "Reativo"] = "Neutro"


class PetResponse(BaseModel):
    id: str
    owner_profile_id: str
    owner_name: str
    pet_name: str
    behavioral_notes: str
    photo_url: str = ""
    gets_along_with_dogs: bool
    accepts_shared_walk: bool
    pet_size: Literal["Pequeno", "Médio", "Grande"]
    energy_level: Literal["Baixo", "Médio", "Alto"]
    pulls_leash: bool
    dog_behavior: Literal["Calmo", "Neutro", "Reativo"]
    podeParticiparCompartilhado: bool = False
    aprovadoParaCompartilhado: bool = False
    created_at: str
    updated_at: str


class AdminPetSharedEligibilityUpdate(BaseModel):
    podeParticiparCompartilhado: bool
    aprovadoParaCompartilhado: bool


class AdminPetSummaryResponse(PetResponse):
    finished_walks_count: int = 0


PetPraiseTag = Literal["docil", "brincalhao", "tranquilo", "ativo", "sociavel"]


class PetPraiseCreatePayload(BaseModel):
    walk_id: Optional[str] = None
    tags: List[PetPraiseTag] = Field(default_factory=list)


class PetPraiseEntryResponse(BaseModel):
    id: str
    pet_id: str
    walk_id: Optional[str] = None
    walker_user_id: str
    walker_name: str
    tags: List[PetPraiseTag] = Field(default_factory=list)
    created_at: str


class PetHighlightItemResponse(BaseModel):
    pet_id: str
    pet_name: str
    photo_url: str = ""
    title: str
    reason: str
    badges: List[str] = Field(default_factory=list)
    praise_tags: List[PetPraiseTag] = Field(default_factory=list)


class PetHighlightsResponse(BaseModel):
    pet_da_semana: Optional[PetHighlightItemResponse] = None
    pet_do_mes: Optional[PetHighlightItemResponse] = None
    pets_em_destaque: List[PetHighlightItemResponse] = Field(default_factory=list)


class PetProfileResponse(BaseModel):
    id: str
    pet_name: str
    behavioral_notes: str
    photo_url: str = ""
    updated_at: str


class OwnerProfileUpdate(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=8, max_length=30)
    email: EmailStr
    street: str = Field(min_length=2, max_length=120)
    number: str = Field(min_length=1, max_length=15)
    neighborhood: str = Field(min_length=2, max_length=80)
    complement: str = ""


class OwnerProfileResponse(BaseModel):
    id: str
    full_name: str
    phone: str
    email: str
    street: str
    number: str
    neighborhood: str
    complement: str
    primary_address_full: str
    updated_at: str


class PartnerApplicationCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=8, max_length=30)
    email: EmailStr
    neighborhood_region: str = Field(min_length=2, max_length=80)
    has_pet_experience: bool
    has_third_party_experience: bool
    experience_description: str = Field(min_length=8, max_length=800)
    availability_days: List[Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]] = Field(min_length=1)
    availability_start_time: str = Field(min_length=5, max_length=5)
    availability_end_time: str = Field(min_length=5, max_length=5)
    profile_photo_url: str = Field(min_length=10, max_length=250000)
    accepted_declaration: bool
    possuiSeguro: bool = False


class PartnerApplicationStatusUpdate(BaseModel):
    status: Literal["Em análise", "Aprovado", "Reprovado"]


class PartnerApplicationAdminFieldsUpdate(BaseModel):
    internal_notes: Optional[str] = None
    active_as_walker: Optional[bool] = None


class PartnerApplicationResponse(BaseModel):
    id: str
    full_name: str
    phone: str
    email: str
    neighborhood_region: str
    has_pet_experience: bool
    has_third_party_experience: bool
    experience_description: str
    availability: str = ""
    availability_days: List[Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]] = Field(default_factory=list)
    availability_start_time: str = ""
    availability_end_time: str = ""
    horarios_disponiveis: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)
    profile_photo_url: str
    possuiSeguro: bool = False
    accepted_declaration: bool
    status: Literal["Em análise", "Aprovado", "Reprovado"]
    internal_notes: str = ""
    approved_at: Optional[str] = None
    active_as_walker: bool = False
    created_at: str
    updated_at: str


class PartnerApplicationPublicResponse(BaseModel):
    id: str
    full_name: str
    phone: str
    email: str
    neighborhood_region: str
    has_pet_experience: bool
    has_third_party_experience: bool
    experience_description: str
    availability: str = ""
    availability_days: List[Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]] = Field(default_factory=list)
    availability_start_time: str = ""
    availability_end_time: str = ""
    horarios_disponiveis: Dict[str, Dict[str, List[str]]] = Field(default_factory=dict)
    profile_photo_url: str
    possuiSeguro: bool = False
    accepted_declaration: bool
    status: Literal["Em análise", "Aprovado", "Reprovado"]
    created_at: str
    updated_at: str


class AdminDashboardResponse(BaseModel):
    total_clients: int
    total_active_walkers: int
    total_walks_finished: int
    total_walks_scheduled: int
    total_walks_in_progress: int
    estimated_revenue_paid: float
    pending_occurrences: int = 0
    open_disputes: int = 0
    walkers_at_risk: int = 0
    top_rated_walkers: int = 0
    disintermediation_alerts: int = 0
    weekly_tips_amount: float = 0.0
    no_show_rate: float = 0.0


class AdminClientSummaryResponse(BaseModel):
    id: str
    name: str
    phone: str
    neighborhood: str
    completed_walks_count: int


class AdminClientDetailResponse(BaseModel):
    id: str
    name: str
    phone: str
    email: str
    street: str
    number: str
    neighborhood: str
    complement: str
    pets: List[dict]
    walks: List[dict]


class AdminPaymentResponse(BaseModel):
    id: str
    walk_id: str
    client_name: str
    plan_type: str
    tipoPlano: str
    value: float
    payment_status: Literal["Pendente", "Pago", "Cancelado"]
    payment_method: str
    #tipoPagamento: str
    payment_date: Optional[str] = None
    notes: str = ""
    created_at: str
    updated_at: str


class AdminPaymentStatusUpdate(BaseModel):
    payment_status: Literal["Pendente", "Pago", "Cancelado"]
    payment_method: str = ""
    notes: str = ""



class TipCheckoutRequest(BaseModel):
    quick_amount: Optional[Literal[2, 5, 10]] = None
    custom_amount: Optional[float] = Field(default=None, ge=1.0)
    origin_url: Optional[str] = None


class TipCheckoutResponse(BaseModel):
    tip_id: str
    session_id: str
    amount: float
    max_allowed_amount: float
    deadline_at: str


class TipCheckoutStatusResponse(BaseModel):
    session_id: str
    status: str
    payment_status: str
    walk_id: str
    tip_amount: float = 0.0
    tip_status: str
    paid_at: Optional[str] = None
    suspicious_flag: bool = False


class WalkerTipEntryResponse(BaseModel):
    id: str
    walk_id: str
    amount: float
    paid_at: str
    client_name: str


class WalkerTipSummaryResponse(BaseModel):
    today_total: float = 0.0
    month_total: float = 0.0
    historical_total: float = 0.0
    recent_tips: List[WalkerTipEntryResponse] = Field(default_factory=list)


class IncentiveTier(BaseModel):
    target_walks: int
    amount: float


class IncentiveSettingsResponse(BaseModel):
    walker_share_percent: float
    platform_share_percent: float
    quality_bonus_percent: float
    quality_bonus_min_weighted: float
    quality_bonus_min_walks: int
    consistency_bonus_amount: float
    consistency_days_required: int
    critical_hour_bonus_amount: float
    critical_windows: List[Dict[str, str]] = Field(default_factory=list)
    volume_bonus_tiers: List[IncentiveTier] = Field(default_factory=list)
    enabled: bool = True
    updated_at: str


class IncentiveSettingsUpdatePayload(BaseModel):
    walker_share_percent: Optional[float] = None
    platform_share_percent: Optional[float] = None
    quality_bonus_percent: Optional[float] = None
    quality_bonus_min_weighted: Optional[float] = None
    quality_bonus_min_walks: Optional[int] = None
    consistency_bonus_amount: Optional[float] = None
    consistency_days_required: Optional[int] = None
    critical_hour_bonus_amount: Optional[float] = None
    critical_windows: Optional[List[Dict[str, str]]] = None
    volume_bonus_tiers: Optional[List[IncentiveTier]] = None
    enabled: Optional[bool] = None


class WalkerBonusEntryResponse(BaseModel):
    id: str
    bonus_type: str
    amount: float
    status: str
    created_at: str
    description: str


class WalkerIncentiveProgressResponse(BaseModel):
    key: str
    label: str
    current: float
    target: float
    percentage: float


class WalkerRankingEntryResponse(BaseModel):
    position: int
    walker_user_id: str
    name: str
    score: float
    completed_walks: int
    rating_avg: float
    no_show_rate: float
    walker_level: Literal["bronze", "silver", "gold", "prata", "ouro", "elite"] = WALKER_LEVEL_BRONZE


class WalkerIncentiveSummaryResponse(BaseModel):
    week_earnings: float
    month_earnings: float
    historical_earnings: float
    week_walks: int
    month_walks: int
    active_bonuses: List[str] = Field(default_factory=list)
    progress_items: List[WalkerIncentiveProgressResponse] = Field(default_factory=list)
    missions: List[WalkerIncentiveProgressResponse] = Field(default_factory=list)
    recent_bonus_history: List[WalkerBonusEntryResponse] = Field(default_factory=list)
    status_label: str
    walker_level: Literal["bronze", "silver", "gold", "prata", "ouro", "elite"] = WALKER_LEVEL_BRONZE
    next_level: Optional[Literal["silver", "gold", "prata", "ouro", "elite"]] = None
    level_progress_percent: float = 0.0
    level_priority_bonus: float = 0.0
    mission_bonus_active: bool = False
    mission_bonus_value: float = WEEKLY_MISSION_SCORE_BONUS
    weekly_tip_total: float = 0.0
    weekly_tip_goal: float = WEEKLY_TIP_GOAL_AMOUNT
    weekly_tip_goal_reached: bool = False
    gamification_badges: List[str] = Field(default_factory=list)
    incentive_messages: List[str] = Field(default_factory=list)
    rides_to_next_bonus: int = 0
    earnings_to_next_bonus: float = 0.0
    weekly_goal_target_amount: float = 0.0
    weekly_goal_remaining_amount: float = 0.0
    weekly_goal_progress_percent: float = 0.0
    critical_mission_bonus_active: bool = False
    mission_priority_points: float = 0.0
    high_demand_windows: List[Dict[str, str]] = Field(default_factory=list)
    ranking_week_position: int = 0
    ranking_month_position: int = 0
    ranking_week_top: List[WalkerRankingEntryResponse] = Field(default_factory=list)
    ranking_month_top: List[WalkerRankingEntryResponse] = Field(default_factory=list)


RoutineWeekday = Literal["seg", "ter", "qua", "qui", "sex", "sab", "dom"]


class PetRoutineSuggestionResponse(BaseModel):
    date: str
    weekday: RoutineWeekday
    time: str
    duration_minutes: Literal[30, 45, 60]
    label: str


class PetRoutineConfigCreatePayload(BaseModel):
    pet_id: str = Field(min_length=1, max_length=120)
    frequencia_semanal: int = Field(ge=1, le=5)
    dias_preferenciais: List[RoutineWeekday] = Field(min_length=1, max_length=7)
    horario_preferencial: str = Field(min_length=5, max_length=5)
    duracao_passeio: Literal[30, 45, 60]


class PetRoutineConfigUpdatePayload(BaseModel):
    frequencia_semanal: Optional[int] = Field(default=None, ge=1, le=5)
    dias_preferenciais: Optional[List[RoutineWeekday]] = Field(default=None, min_length=1, max_length=7)
    horario_preferencial: Optional[str] = Field(default=None, min_length=5, max_length=5)
    duracao_passeio: Optional[Literal[30, 45, 60]] = None
    is_active: Optional[bool] = None


class PetRoutineConfigResponse(BaseModel):
    id: str
    user_id: str
    pet_id: str
    pet_name: str
    frequencia_semanal: int
    dias_preferenciais: List[RoutineWeekday] = Field(default_factory=list)
    horario_preferencial: str
    duracao_passeio: Literal[30, 45, 60]
    is_active: bool = True
    created_at: str
    updated_at: str


class PetRoutineProgressResponse(BaseModel):
    id: str
    routine_id: Optional[str] = None
    pet_id: Optional[str] = None
    pet_name: str = ""
    user_id: str
    frequencia_semanal: int = 0
    dias_preferenciais: List[RoutineWeekday] = Field(default_factory=list)
    horario_preferencial: str = ""
    duracao_passeio: int = 30
    is_active: bool = False
    current_streak: int = 0
    best_streak: int = 0
    total_ciclos_cumpridos: int = 0
    total_ciclos_perdidos: int = 0
    total_passeios_realizados_no_periodo: int = 0
    taxa_cumprimento_rotina: float = 0.0
    ultimo_passeio_valido_em_rotina: Optional[str] = None
    proximo_passeio_esperado: Optional[str] = None
    planned_this_week: int = 0
    completed_this_week: int = 0
    week_progress_label: str = "0 de 0 passeios planejados"
    # Backward compatibility
    streak_days: int = 0
    best_streak_days: int = 0
    finished_walks_total: int = 0
    finished_walks_week: int = 0
    finished_walks_month: int = 0
    simple_badges: List[str] = Field(default_factory=list)
    encouragement_message: str = "Seu pet está criando uma rotina saudável"
    last_finished_walk_at: Optional[str] = None
    suggestions: List[PetRoutineSuggestionResponse] = Field(default_factory=list)
    updated_at: str


class PetRoutineRecalculatePayload(BaseModel):
    user_id: Optional[str] = None
    pet_id: Optional[str] = None


class PetRoutineRecalculateResponse(BaseModel):
    processed_users: int
    updated_profiles: int
    items: List[PetRoutineProgressResponse] = Field(default_factory=list)


class PetRoutineDashboardResponse(BaseModel):
    feature_active: bool
    feature_visible: bool
    routine: Optional[PetRoutineConfigResponse] = None
    progress: PetRoutineProgressResponse


class ClientReferralRules(BaseModel):
    indicated_discount_amount: float = 20.0
    referrer_coupon_credit_amount: float = 20.0
    min_paid_walks_for_referrer_bonus: int = 2
    referral_limit_per_user: int = 20
    benefit_validity_days: int = 45


class WalkerReferralRules(BaseModel):
    fixed_bonus_amount: float = 100.0
    min_completed_walks: int = 20
    min_rating_required: float = 4.7
    max_no_show_rate: float = 4.0
    eligibility_window_days: int = 60


class ReferralProgramSettingsResponse(BaseModel):
    program_enabled: bool = False
    client_referral_enabled: bool = False
    walker_referral_enabled: bool = False
    app_visible: bool = False
    client_rules: ClientReferralRules = Field(default_factory=ClientReferralRules)
    walker_rules: WalkerReferralRules = Field(default_factory=WalkerReferralRules)
    updated_at: str
    updated_by: str = "system"


class ReferralProgramSettingsUpdatePayload(BaseModel):
    program_enabled: Optional[bool] = None
    client_referral_enabled: Optional[bool] = None
    walker_referral_enabled: Optional[bool] = None
    app_visible: Optional[bool] = None
    client_rules: Optional[ClientReferralRules] = None
    walker_rules: Optional[WalkerReferralRules] = None


ReferralType = Literal["cliente_para_cliente", "passeador_para_passeador"]
ReferralStatus = Literal[
    "criada",
    "pendente_ativacao",
    "em_progresso",
    "elegivel_recompensa",
    "recompensa_liberada",
    "cancelada",
    "invalida_fraude",
]


class ReferralRecordResponse(BaseModel):
    id: str
    referral_code: str
    referral_type: ReferralType
    status: ReferralStatus
    referrer_user_id: str
    referred_user_id: Optional[str] = None
    referrer_role: Literal["cliente", "passeador"]
    referred_role: Literal["cliente", "passeador"]
    created_at: str
    activated_at: Optional[str] = None
    unlock_condition: Dict[str, Any] = Field(default_factory=dict)
    reward_amount: float = 0.0
    reward_released_at: Optional[str] = None
    benefit_released_at: Optional[str] = None
    condition_progress: Dict[str, Any] = Field(default_factory=dict)
    device_id: Optional[str] = None
    ip_address: Optional[str] = None
    fraud_flags: List[str] = Field(default_factory=list)
    audit_log: List[Dict[str, Any]] = Field(default_factory=list)


class ReferralApplyPayload(BaseModel):
    referral_code: str = Field(min_length=5, max_length=24)


class AdminReferralStatusUpdatePayload(BaseModel):
    status: ReferralStatus
    note: str = Field(default="", max_length=300)


class ReferralDashboardResponse(BaseModel):
    program_enabled: bool
    app_visible: bool
    role_enabled: bool
    role: Literal["cliente", "passeador"]
    referral_code: Optional[str] = None
    referral_type: Optional[ReferralType] = None
    invite_link: Optional[str] = None
    stats: Dict[str, Any] = Field(default_factory=dict)
    referrals: List[ReferralRecordResponse] = Field(default_factory=list)


class AdminReferralListResponse(BaseModel):
    items: List[ReferralRecordResponse] = Field(default_factory=list)
    total: int = 0


class AdminTipResponse(BaseModel):
    id: str
    walk_id: str
    walk_date: str
    walker_id: str
    walker_name: str
    client_id: str
    client_name: str
    amount: float
    paid_at: str
    suspicious_flag: bool = False


class OccurrenceLogEntry(BaseModel):
    id: str
    action: str
    performed_by_id: str
    performed_by_name: str
    timestamp: str
    note: str = ""
    before_values: Dict[str, Any] = Field(default_factory=dict)
    after_values: Dict[str, Any] = Field(default_factory=dict)


class AdminOccurrenceResponse(BaseModel):
    id: str
    occurrence_status: str
    walk_status: str
    resolved: bool = False
    client_name: str
    walker_name: str
    pet_name: str
    walk_date: str
    walk_time: str
    region: str = ""
    summary: str = ""
    scheduled_start_at: Optional[str] = None
    walker_check_in_at: Optional[str] = None
    client_confirmed_at: Optional[str] = None
    tolerance_expires_at: Optional[str] = None
    base_price: float = 0.0
    charged_amount: float = 0.0
    walker_payout_amount: float = 0.0
    platform_retained_amount: float = 0.0
    walker_share_percent: float = DEFAULT_WALKER_SHARE_PERCENT
    platform_fee_percent: float = DEFAULT_PLATFORM_SHARE_PERCENT
    client_refund_amount: float = 0.0
    tip_amount: float = 0.0
    financial_status: str = "pendente"
    payment_released_at: Optional[str] = None
    payment_paid_at: Optional[str] = None
    payment_method: str = ""
    payment_transaction_id: str = ""
    payment_failure_reason: str = ""
    payment_block_reason: str = ""
    suspected_disintermediation: bool = False
    internal_note: str = ""
    logs: List[OccurrenceLogEntry] = Field(default_factory=list)


class AdminOccurrenceActionPayload(BaseModel):
    action: Literal[
        "approve_auto_decision",
        "reverse_decision",
        "refund_client",
        "release_walker_payment",
        "open_financial_dispute",
        "resolve_financial_dispute",
        "add_internal_note",
        "mark_resolved",
        "mark_unresolved",
        "mark_disintermediation_suspect",
        "warn_walker",
        "mark_payment_paid",
        "mark_payment_failed",
        "block_payment_analysis",
    ]
    note: str = Field(default="", max_length=500)
    refund_amount: Optional[float] = None
    payout_amount: Optional[float] = None
    retained_amount: Optional[float] = None
    payment_method: str = ""
    transaction_id: str = ""
    block_reason: str = ""
    failure_reason: str = ""


class AdminWalkerPerformanceResponse(BaseModel):
    user_id: str
    name: str
    photo_url: str
    region: str
    operational_status: Literal["ativo", "observacao", "restrito", "suspenso"]
    rating_avg: float = 0.0
    rating_count: int = 0
    completed_walks: int = 0
    severe_delay_rate: float = 0.0
    no_show_rate: float = 0.0
    cancel_rate: float = 0.0
    score_final: float = 0.0
    tip_total_amount: float = 0.0
    tip_average_amount: float = 0.0
    tip_recent_window_total: float = 0.0
    tip_recent_window_count: int = 0
    tip_suspicious_flag: bool = False
    tip_suspicious_reasons: List[str] = Field(default_factory=list)
    tip_score_impact_points: float = 0.0
    tip_score_impact_mode: Literal["normal", "ignore_current", "ignore_recent_window", "blocked_until_review"] = "normal"
    tip_platform_avg_comparison: float = 0.0
    tip_origin_top_clients: List[Dict[str, Any]] = Field(default_factory=list)
    alerts_count: int = 0
    risk_flag: bool = False
    suspected_disintermediation: bool = False
    is_premium_featured: bool = False
    can_be_featured_by_rule: bool = False
    premium_override: bool = False


class AdminWalkerActionPayload(BaseModel):
    action: Literal[
        "warn",
        "set_observation",
        "restrict",
        "suspend",
        "reactivate",
        "feature_premium",
        "force_feature_premium",
        "remove_feature",
        "start_recovery",
        "tip_review_progressive",
        "tip_restore_impact",
    ]
    note: str = Field(default="", max_length=500)


class WalkResponse(BaseModel):
    id: str
    pet_name: str
    pet_ids: List[str] = Field(default_factory=list)
    shared_pet_names: List[str] = Field(default_factory=list)
    shared_client_names: List[str] = Field(default_factory=list)
    shared_owner_keys: List[str] = Field(default_factory=list)
    participant_user_ids: List[str] = Field(default_factory=list)
    client_user_id: Optional[str] = None
    client_name: str
    walk_type: Literal["Individual", "Compartilhado"] = WALK_TYPE_INDIVIDUAL
    tipoPasseio: Literal["padrao", "transporte"] = "padrao"
    modoInicioPasseio: Literal["endereco_tutor", "ponto_encontro", "deslocamento_premium"] = START_MODE_TUTOR_ADDRESS
    enderecoBaseTutor: str = ""
    pontoRetiradaAlternativo: Optional[dict] = None
    pontoEncontro: Optional[dict] = None
    localDestinoPasseio: Optional[dict] = None
    distanciaKm: float = 0.0
    adicionalDeslocamento: float = 0.0
    tempoDeslocamentoMinutos: int = 0
    tempoPasseioMinutos: int = 0
    tempoTotalMinutos: int = 0
    rastreamentoReforcado: bool = False
    trackingIntervalSegundos: int = 60
    statusTransporte: str = "nao_aplicavel"
    eventosTransporte: List[Dict[str, Any]] = Field(default_factory=list)
    precisaAnaliseManualDeslocamento: bool = False
    statusAnaliseDeslocamento: Literal["nao_aplicavel", "aprovado", "aguardando_analise", "rejeitado"] = PREMIUM_ANALYSIS_NA
    premiumRepassePercentual: float = DEFAULT_PREMIUM_PAYOUT_PERCENT
    valor_base_passeio: float = 0.0
    coupon_id: Optional[str] = None
    coupon_code: str = ""
    discount_percent_applied: float = 0.0
    discount_fixed_applied: float = 0.0
    discount_amount: float = 0.0
    price_before_discount: float = 0.0
    dynamic_pricing_mode: Literal["off", "shadow", "active"] = DYNAMIC_PRICING_MODE_OFF
    dynamic_price_multiplier: float = 1.0
    dynamic_price_reason: str = "Preço padrão"
    dynamic_price_calculated: float = 0.0
    dynamic_price_difference_percent: float = 0.0
    tip_id: Optional[str] = None
    tip_amount: float = 0.0
    tip_status: str = "none"
    tip_paid_at: Optional[str] = None
    tip_deadline_at: Optional[str] = None
    shared_context: Optional[Literal["same_household", "other_client"]] = None
    shared_approved: bool = False
    shared_group: Optional[dict] = None
    walk_date: str
    walk_time: str
    duration_minutes: Literal[30, 45, 60]
    walker_id: str = "walker-1"
    walker_user_id: Optional[str] = None
    walker_name: str = "Passeador não informado"
    walker_photo_url: Optional[str] = None
    walker_rating_avg: float = 0.0
    walker_rating_count: int = 0
    walker_highlight_label: str = ""
    pickup_street: str = ""
    pickup_number: str = ""
    pickup_neighborhood: str = ""
    pickup_complement: str = ""
    location_reference: str = "Local não informado"
    security_code: str = "0000"
    did_pee: bool = False
    did_poop: bool = False
    rating: Optional[int] = None
    rating_comment: str = ""
    summary_text: str = ""
    pet_behavior_notes: str = ""
    notes: str
    motivoCancelamento: str = ""
    tipoCancelamento: Optional[Literal["cliente", "passeador"]] = None
    penalidadePercentual: int = 0
    base_price: float = 0.0
    walker_payout: float = 0.0
    scheduled_start_at: Optional[str] = None
    walker_check_in_at: Optional[str] = None
    client_confirmed_at: Optional[str] = None
    tolerance_expires_at: Optional[str] = None
    tolerance_minutes: int = TOLERANCE_MINUTES
    attendance_message: str = ""
    charged_amount: float = 0.0
    walker_payout_amount: float = 0.0
    platform_retained_amount: float = 0.0
    walker_share_percent: float = DEFAULT_WALKER_SHARE_PERCENT
    platform_fee_percent: float = DEFAULT_PLATFORM_SHARE_PERCENT
    client_refund_amount: float = 0.0
    decision_resolved_at: Optional[str] = None
    decision_source: str = ""
    walker_penalty_registered: bool = False
    kit_checklist_check_in_confirmed: bool = False
    kit_checklist_start_confirmed: bool = False
    checklist_validado_chegada: bool = False
    checklist_confirmado_inicio: bool = False
    kit_checklist_check_in: Dict[str, Any] = Field(default_factory=dict)
    kit_checklist_start: Dict[str, Any] = Field(default_factory=dict)
    kit_issue_report: Dict[str, Any] = Field(default_factory=dict)
    premium_verified_validation: Dict[str, Any] = Field(default_factory=dict)
    occurrence_status: str = ""
    occurrence_resolved: bool = False
    internal_note: str = ""
    occurrence_logs: List[OccurrenceLogEntry] = Field(default_factory=list)
    status: Literal[
        "Agendado",
        "Indo buscar o pet",
        "Passeando agora",
        "Finalizado",
        "Cancelado",
        "Não comparecimento do cliente",
        "Não comparecimento do passeador",
        "Pendente de análise",
    ]
    photo_url: Optional[str] = None
    walk_datetime_iso: str
    created_at: str
    updated_at: str


def _validate_datetime_iso(walk_date: str, walk_time: str) -> str:
    try:
        combined = datetime.strptime(f"{walk_date} {walk_time}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Data ou horário inválido") from exc
    return combined.replace(tzinfo=timezone.utc).isoformat()


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _walk_start_end(walk: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    start_dt = _parse_iso_datetime(walk.get("walk_datetime_iso"))
    if not start_dt:
        try:
            start_dt = datetime.strptime(
                f"{walk.get('walk_date', '')} {walk.get('walk_time', '')}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return None, None

    duration_minutes = int(_coerce_float(walk.get("duration_minutes", 30), 30))
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    return start_dt, end_dt


def _base_amount_from_walk(walk: dict) -> float:
    if "base_price" in walk:
        return round(max(0.0, _coerce_float(walk.get("base_price"), 0.0)), 2)

    subtotal = _walk_subtotal_before_discount(walk)
    _, _, discount_amount = _coupon_discount_components(walk, subtotal)
    total = round(max(0.0, subtotal - discount_amount), 2)
    return total


def _compute_no_show_financials(walk: dict, status: str) -> Dict[str, float]:
    total = _base_amount_from_walk(walk)

    if status == STATUS_NO_SHOW_CLIENT:
        walker_amount = round(total * 0.5, 2)
        platform_amount = round(total - walker_amount, 2)
        return {
            "charged_amount": total,
            "walker_payout_amount": walker_amount,
            "platform_retained_amount": platform_amount,
            "client_refund_amount": 0.0,
        }

    if status == STATUS_NO_SHOW_WALKER:
        return {
            "charged_amount": total,
            "walker_payout_amount": 0.0,
            "platform_retained_amount": 0.0,
            "client_refund_amount": total,
        }

    return {
        "charged_amount": total,
        "walker_payout_amount": round(total * (RUNTIME_WALKER_SHARE_PERCENT / 100.0), 2),
        "platform_retained_amount": round(total - round(total * (RUNTIME_WALKER_SHARE_PERCENT / 100.0), 2), 2),
        "client_refund_amount": 0.0,
    }


def _is_user_assigned_walker(user: dict, walk: dict) -> bool:
    user_id = user.get("id")
    if not user_id:
        return False

    walker_id = str(walk.get("walker_id", ""))
    if walker_id in {user_id, f"partner-{user_id}"}:
        return True

    if walk.get("walker_user_id") == user_id:
        return True

    return str(walk.get("walker_name", "")).strip().lower() == str(user.get("full_name", "")).strip().lower()


def _is_slot_valid_against_existing_walks(
    candidate_start: datetime,
    duration_minutes: int,
    existing_walks: List[dict],
    *,
    exclude_walk_id: Optional[str] = None,
) -> bool:
    candidate_end = candidate_start + timedelta(minutes=duration_minutes)
    buffer_delta = timedelta(minutes=MIN_BUFFER_BETWEEN_WALKS_MINUTES)

    for existing in existing_walks:
        if exclude_walk_id and existing.get("id") == exclude_walk_id:
            continue
        if existing.get("status") not in BLOCKING_WALK_STATUSES:
            continue

        existing_start, existing_end = _walk_start_end(existing)
        if not existing_start or not existing_end:
            continue

        has_conflict = (
            candidate_start < (existing_end + buffer_delta)
            and (candidate_end + buffer_delta) > existing_start
        )
        if has_conflict:
            return False

    return True


async def _get_walker_existing_walks(walker_id: str, exclude_walk_id: Optional[str] = None) -> List[dict]:
    query: Dict[str, Any] = {
        "walker_id": walker_id,
        "status": {"$in": list(BLOCKING_WALK_STATUSES)},
    }
    if exclude_walk_id:
        query["id"] = {"$ne": exclude_walk_id}

    return await db.walks.find(query, {"_id": 0}).to_list(1000)


async def _get_available_slots_for_walker(
    walker: dict,
    walker_id: str,
    walk_date: str,
    duration_minutes: int,
    *,
    exclude_walk_id: Optional[str] = None,
) -> List[str]:
    normalized_walker = _ensure_walker_schedule(walker)
    schedule_slots = _walker_slots_for_date(normalized_walker, walk_date, duration_minutes)
    if not schedule_slots:
        return []

    unavailable_until_dt = _parse_iso_datetime(normalized_walker.get("unavailable_until"))
    if unavailable_until_dt:
        try:
            selected_day = datetime.strptime(walk_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if selected_day.date() <= unavailable_until_dt.date():
                return []
        except ValueError:
            return []

    existing_walks = await _get_walker_existing_walks(walker_id, exclude_walk_id=exclude_walk_id)
    availability_blocks = normalized_walker.get("availability_blocks", [])
    capacity_by_period = _normalize_availability_capacity_by_period(normalized_walker.get("availability_capacity_by_period"))
    period_load = {"manha": 0, "tarde": 0, "noite": 0}
    for walk in existing_walks:
        if str(walk.get("walk_date") or "") != walk_date:
            continue
        walk_time = str(walk.get("walk_time") or "").strip()
        if not walk_time:
            continue
        try:
            period_key = _period_key_for_clock(walk_time)
        except ValueError:
            continue
        period_load[period_key] = period_load.get(period_key, 0) + 1

    valid_slots: List[str] = []

    for slot in schedule_slots:
        try:
            candidate_start = datetime.strptime(f"{walk_date} {slot}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if unavailable_until_dt and candidate_start <= unavailable_until_dt:
            continue

        try:
            period_key = _period_key_for_clock(slot)
        except ValueError:
            continue
        if period_load.get(period_key, 0) >= capacity_by_period.get(period_key, 0):
            continue

        if any(_slot_overlaps_block(candidate_start, duration_minutes, block) for block in availability_blocks):
            continue

        if _is_slot_valid_against_existing_walks(candidate_start, duration_minutes, existing_walks, exclude_walk_id=exclude_walk_id):
            valid_slots.append(slot)

    return valid_slots


async def _apply_attendance_decision_if_needed(walk: dict, *, now: Optional[datetime] = None, trigger: str = "automation") -> dict:
    current_status = walk.get("status", STATUS_SCHEDULED)
    if current_status not in {STATUS_SCHEDULED, STATUS_GOING_TO_PICKUP}:
        return walk

    now_dt = now or datetime.now(timezone.utc)
    start_dt, _ = _walk_start_end(walk)
    if not start_dt:
        return walk

    check_in_dt = _parse_iso_datetime(walk.get("walker_check_in_at"))
    client_confirm_dt = _parse_iso_datetime(walk.get("client_confirmed_at"))
    scheduled_deadline = start_dt + timedelta(minutes=TOLERANCE_MINUTES)
    resolution_status: Optional[str] = None
    message = ""
    tolerance_expires_at = walk.get("tolerance_expires_at")

    if check_in_dt:
        tolerance_base = check_in_dt if check_in_dt > start_dt else start_dt
        client_deadline = tolerance_base + timedelta(minutes=TOLERANCE_MINUTES)
        tolerance_expires_at = client_deadline.isoformat()
        if not client_confirm_dt and now_dt >= client_deadline:
            resolution_status = STATUS_NO_SHOW_CLIENT
            message = "Cliente não compareceu dentro do prazo de tolerância."
    else:
        tolerance_expires_at = scheduled_deadline.isoformat()
        if now_dt >= scheduled_deadline:
            if client_confirm_dt:
                resolution_status = STATUS_NO_SHOW_WALKER
                message = "Passeador não compareceu dentro do prazo de tolerância."
            else:
                resolution_status = STATUS_PENDING_REVIEW
                message = "Sem registro das partes no prazo. Encaminhado para análise manual."

    updates: Dict[str, Any] = {}
    current_logs = _normalize_occurrence_logs(walk.get("occurrence_logs"))
    if tolerance_expires_at and walk.get("tolerance_expires_at") != tolerance_expires_at:
        updates["tolerance_expires_at"] = tolerance_expires_at

    if resolution_status and resolution_status != current_status:
        decision_time = now_dt.isoformat()
        payment_status = walk.get("payment_status", "Pendente")
        occurrence_status = OCC_UNRESOLVED
        if resolution_status == STATUS_NO_SHOW_WALKER:
            payment_status = "Cancelado"
            occurrence_status = OCC_NO_SHOW_WALKER
        elif resolution_status == STATUS_NO_SHOW_CLIENT:
            payment_status = "Pendente"
            occurrence_status = OCC_NO_SHOW_CLIENT
        elif resolution_status == STATUS_PENDING_REVIEW:
            occurrence_status = OCC_PENDING_ANALYSIS

        updates.update(
            {
                "status": resolution_status,
                "attendance_message": message,
                "decision_resolved_at": decision_time,
                "decision_source": trigger,
                "payment_status": payment_status,
                "occurrence_status": occurrence_status,
                "occurrence_resolved": False,
            }
        )
        updates.update(_compute_no_show_financials(walk, resolution_status))
        if resolution_status == STATUS_NO_SHOW_WALKER:
            updates["walker_penalty_registered"] = True

        current_logs.append(
            OccurrenceLogEntry(
                id=str(uuid.uuid4()),
                action="auto_decision",
                performed_by_id="system",
                performed_by_name="Sistema",
                timestamp=now_dt.isoformat(),
                note=message,
                before_values={"status": current_status},
                after_values={"status": resolution_status, "occurrence_status": updates.get("occurrence_status")},
            ).model_dump()
        )
        updates["occurrence_logs"] = current_logs

    if not updates:
        return walk

    updates["updated_at"] = now_dt.isoformat()
    await db.walks.update_one({"id": walk["id"]}, {"$set": updates})
    refreshed = await _get_walk_or_404(walk["id"])

    if resolution_status:
        client_user_id = refreshed.get("client_user_id")
        if client_user_id:
            await _create_notification(
                user_id=client_user_id,
                role="cliente",
                title="Atualização do passeio",
                message=message,
                category="status_passeio",
            )

        walker_user_id = refreshed.get("walker_user_id")
        if walker_user_id:
            await _create_notification(
                user_id=walker_user_id,
                role="passeador",
                title="Atualização do passeio",
                message=message,
                category="operacao",
            )

        await _recalculate_quality_from_walk(refreshed, "attendance_event")

        if client_user_id:
            client_user = await db.users.find_one({"id": client_user_id, "role": "cliente"}, {"_id": 0})
            if client_user:
                client_walks = await db.walks.find({"client_user_id": client_user_id}, {"_id": 0}).to_list(500)
                await _generate_client_alerts(client_user, client_walks)
                if _is_disintermediation_flag_active(client_user):
                    await _generate_disintermediation_alert_for_user(
                        client_user,
                        str(client_user.get("desintermediacao_flag_reason") or "FLAG_ACTIVE"),
                    )

    return refreshed


def _derive_occurrence_status(walk: dict) -> str:
    explicit_status = str(walk.get("occurrence_status") or "").strip()
    if explicit_status:
        return explicit_status

    if bool(walk.get("suspected_disintermediation", False)):
        return OCC_SUSPECT_DISINTERMEDIATION

    if walk.get("occurrence_resolved"):
        return OCC_RESOLVED

    walk_status = walk.get("status")
    if walk_status == STATUS_PENDING_REVIEW:
        return OCC_PENDING_ANALYSIS
    if walk_status == STATUS_NO_SHOW_CLIENT:
        return OCC_NO_SHOW_CLIENT
    if walk_status == STATUS_NO_SHOW_WALKER:
        return OCC_NO_SHOW_WALKER

    start_dt, _ = _walk_start_end(walk)
    check_in_dt = _parse_iso_datetime(walk.get("walker_check_in_at"))
    if start_dt and check_in_dt:
        if check_in_dt <= start_dt + timedelta(minutes=TOLERANCE_MINUTES):
            return OCC_LATE_LIGHT
        return OCC_LATE_SEVERE

    return OCC_UNRESOLVED


def _normalize_occurrence_logs(raw_logs: Any) -> List[dict]:
    if not isinstance(raw_logs, list):
        return []
    normalized: List[dict] = []
    for raw in raw_logs:
        if isinstance(raw, OccurrenceLogEntry):
            normalized.append(raw.model_dump())
            continue
        if not isinstance(raw, dict):
            continue
        try:
            normalized.append(OccurrenceLogEntry(**raw).model_dump())
        except Exception:
            continue
    return normalized


async def _append_occurrence_log(walk_id: str, entry: dict):
    walk = await _get_walk_or_404(walk_id)
    logs = _normalize_occurrence_logs(walk.get("occurrence_logs"))
    logs.append(OccurrenceLogEntry(**entry).model_dump())
    await db.walks.update_one({"id": walk_id}, {"$set": {"occurrence_logs": logs, "updated_at": datetime.now(timezone.utc).isoformat()}})


def _to_admin_occurrence_response(walk: dict) -> AdminOccurrenceResponse:
    walk_response = _to_walk_response(walk)
    logs = _normalize_occurrence_logs(walk.get("occurrence_logs"))
    return AdminOccurrenceResponse(
        id=walk_response.id,
        occurrence_status=walk_response.occurrence_status,
        walk_status=walk_response.status,
        resolved=walk_response.occurrence_resolved,
        client_name=walk_response.client_name,
        walker_name=walk_response.walker_name,
        pet_name=walk_response.pet_name,
        walk_date=walk_response.walk_date,
        walk_time=walk_response.walk_time,
        region=str(walk_response.pickup_neighborhood or ""),
        summary=str(walk.get("occurrence_summary") or ""),
        scheduled_start_at=walk_response.scheduled_start_at,
        walker_check_in_at=walk_response.walker_check_in_at,
        client_confirmed_at=walk_response.client_confirmed_at,
        tolerance_expires_at=walk_response.tolerance_expires_at,
        base_price=walk_response.base_price,
        charged_amount=walk_response.charged_amount,
        walker_payout_amount=walk_response.walker_payout_amount,
        platform_retained_amount=walk_response.platform_retained_amount,
        client_refund_amount=walk_response.client_refund_amount,
        tip_amount=_coerce_float(walk_response.tip_amount, 0.0),
        financial_status=str(walk.get("financial_status") or "pendente"),
        payment_released_at=walk.get("payment_released_at"),
        payment_paid_at=walk.get("payment_paid_at"),
        payment_method=str(walk.get("payment_method") or ""),
        payment_transaction_id=str(walk.get("payment_transaction_id") or ""),
        payment_failure_reason=str(walk.get("payment_failure_reason") or ""),
        payment_block_reason=str(walk.get("payment_block_reason") or ""),
        suspected_disintermediation=bool(walk.get("suspected_disintermediation", False)),
        internal_note=walk_response.internal_note,
        logs=[OccurrenceLogEntry(**log) for log in logs],
    )


async def _get_walk_or_404(walk_id: str) -> dict:
    walk = await db.walks.find_one({"id": walk_id}, {"_id": 0})
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio não encontrado")
    return walk


def _to_walk_response(walk: dict) -> WalkResponse:
    normalized = dict(walk)
    normalized.setdefault("notes", "")
    normalized.setdefault("motivoCancelamento", "")
    normalized.setdefault("tipoCancelamento", None)
    normalized.setdefault("penalidadePercentual", 0)
    normalized.setdefault("pet_ids", [])
    normalized.setdefault("shared_pet_names", [])
    normalized.setdefault("shared_client_names", [])
    normalized.setdefault("shared_owner_keys", [])
    normalized.setdefault("participant_user_ids", [])
    normalized.setdefault("client_user_id", None)
    normalized.setdefault("walk_type", WALK_TYPE_INDIVIDUAL)
    normalized.setdefault("tipoPasseio", "padrao")
    normalized.setdefault("modoInicioPasseio", START_MODE_TUTOR_ADDRESS)
    normalized.setdefault("enderecoBaseTutor", "")
    normalized.setdefault("pontoRetiradaAlternativo", None)
    normalized.setdefault("pontoEncontro", None)
    normalized.setdefault("localDestinoPasseio", None)
    normalized.setdefault("distanciaKm", 0.0)
    normalized.setdefault("adicionalDeslocamento", 0.0)
    normalized.setdefault("tempoDeslocamentoMinutos", 0)
    normalized.setdefault("tempoPasseioMinutos", int(normalized.get("duration_minutes", 0) or 0))
    normalized.setdefault("tempoTotalMinutos", int(normalized.get("duration_minutes", 0) or 0))
    normalized.setdefault("rastreamentoReforcado", False)
    normalized.setdefault("trackingIntervalSegundos", 60)
    normalized.setdefault("statusTransporte", "nao_aplicavel")
    normalized.setdefault("eventosTransporte", [])
    normalized.setdefault("precisaAnaliseManualDeslocamento", False)
    normalized.setdefault("statusAnaliseDeslocamento", PREMIUM_ANALYSIS_NA)
    normalized.setdefault("premiumRepassePercentual", DEFAULT_PREMIUM_PAYOUT_PERCENT)
    normalized.setdefault("valor_base_passeio", _base_walk_price(normalized))
    normalized.setdefault("coupon_id", None)
    normalized.setdefault("coupon_code", "")
    normalized.setdefault("discount_percent_applied", 0.0)
    normalized.setdefault("discount_fixed_applied", 0.0)
    normalized.setdefault("discount_amount", 0.0)
    normalized.setdefault("price_before_discount", _walk_subtotal_before_discount(normalized))
    normalized.setdefault("tip_id", None)
    normalized.setdefault("tip_amount", 0.0)
    normalized.setdefault("tip_status", "none")
    normalized.setdefault("tip_paid_at", None)
    normalized.setdefault("tip_deadline_at", None)
    normalized.setdefault("financial_status", "pendente")
    normalized.setdefault("payment_released_at", None)
    normalized.setdefault("payment_paid_at", None)
    normalized.setdefault("payment_method", "")
    normalized.setdefault("payment_transaction_id", "")
    normalized.setdefault("payment_failure_reason", "")
    normalized.setdefault("payment_block_reason", "")
    normalized.setdefault("suspected_disintermediation", False)
    normalized.setdefault("shared_context", None)
    normalized.setdefault("shared_approved", False)
    normalized.setdefault("shared_group", None)
    normalized.setdefault("walker_id", "walker-1")
    normalized.setdefault("walker_user_id", None)
    normalized.setdefault("pickup_street", "")
    normalized.setdefault("pickup_number", "")
    normalized.setdefault("pickup_neighborhood", "")
    normalized.setdefault("pickup_complement", "")
    normalized.setdefault("location_reference", "")
    normalized.setdefault("security_code", "")
    normalized.setdefault("did_pee", False)
    normalized.setdefault("did_poop", False)
    normalized.setdefault("rating", None)
    normalized.setdefault("rating_comment", "")
    normalized.setdefault("summary_text", "")
    normalized.setdefault("pet_behavior_notes", "")
    normalized.setdefault("walker_rating_avg", 0.0)
    normalized.setdefault("walker_rating_count", 0)
    normalized.setdefault("walker_highlight_label", "")
    normalized.setdefault("scheduled_start_at", normalized.get("walk_datetime_iso"))
    normalized.setdefault("walker_check_in_at", None)
    normalized.setdefault("client_confirmed_at", None)
    normalized.setdefault("tolerance_expires_at", None)
    normalized.setdefault("tolerance_minutes", TOLERANCE_MINUTES)
    normalized.setdefault("attendance_message", "")
    normalized.setdefault("walker_share_percent", RUNTIME_WALKER_SHARE_PERCENT)
    normalized.setdefault("platform_fee_percent", RUNTIME_PLATFORM_SHARE_PERCENT)
    normalized.setdefault("decision_resolved_at", None)
    normalized.setdefault("decision_source", "")
    normalized.setdefault("walker_penalty_registered", False)
    normalized.setdefault("kit_checklist_check_in_confirmed", False)
    normalized.setdefault("kit_checklist_start_confirmed", False)
    normalized.setdefault("checklist_validado_chegada", bool(normalized.get("kit_checklist_check_in_confirmed", False)))
    normalized.setdefault("checklist_confirmado_inicio", bool(normalized.get("kit_checklist_start_confirmed", False)))
    normalized.setdefault("kit_checklist_check_in", {})
    normalized.setdefault("kit_checklist_start", {})
    normalized.setdefault("kit_issue_report", {})
    normalized.setdefault("premium_verified_validation", {})
    normalized.setdefault("occurrence_status", "")
    normalized.setdefault("occurrence_resolved", False)
    normalized.setdefault("internal_note", "")
    normalized.setdefault("occurrence_logs", [])

    legacy_status_map = {
        LEGACY_STATUS_IN_PROGRESS: STATUS_WALKING_NOW,
        "Aceito": STATUS_SCHEDULED,
        "Aceita": STATUS_SCHEDULED,
        "Em andamento": STATUS_WALKING_NOW,
        "Concluido": STATUS_FINISHED,
        "Concluído": STATUS_FINISHED,
    }
    normalized["status"] = legacy_status_map.get(str(normalized.get("status") or ""), normalized.get("status"))

    if not normalized.get("security_code"):
        id_source = str(normalized.get("id", ""))
        numeric = "".join(ch for ch in id_source if ch.isdigit())
        normalized["security_code"] = (numeric[-4:] if len(numeric) >= 4 else "4826").zfill(4)

    if normalized.get("duration_minutes") == 50:
        normalized["duration_minutes"] = 60

    walker_profile = WALKER_MAP.get(normalized.get("walker_id", ""))
    if walker_profile:
        normalized["walker_name"] = normalized.get("walker_name") or walker_profile["name"]
        normalized["walker_photo_url"] = normalized.get("walker_photo_url") or walker_profile["photo_url"]

    normalized["walker_name"] = normalized.get("walker_name") or "Passeador não informado"
    normalized["walker_photo_url"] = normalized.get("walker_photo_url")

    if not normalized.get("location_reference"):
        neighborhood = str(normalized.get("pickup_neighborhood", "")).strip()
        street = str(normalized.get("pickup_street", "")).strip()
        number = str(normalized.get("pickup_number", "")).strip()
        fallback_location = neighborhood or f"{street}, {number}".strip(" ,")
        normalized["location_reference"] = fallback_location or "Local não informado"

    normalized["coupon_code"] = _normalize_coupon_code(normalized.get("coupon_code", ""))
    if not normalized.get("coupon_code"):
        normalized["coupon_id"] = None

    subtotal_before_discount = _walk_subtotal_before_discount(normalized)
    discount_percent, discount_fixed, discount_amount = _coupon_discount_components(normalized, subtotal_before_discount)
    normalized["discount_percent_applied"] = discount_percent
    normalized["discount_fixed_applied"] = discount_fixed
    normalized["discount_amount"] = discount_amount
    normalized["price_before_discount"] = subtotal_before_discount

    base_price, walker_payout = _calculate_walk_pricing(normalized)
    normalized["base_price"] = base_price
    normalized["walker_payout"] = walker_payout
    normalized.setdefault("charged_amount", _base_amount_from_walk(normalized))
    normalized.setdefault("walker_payout_amount", _coerce_float(normalized.get("walker_payout"), 0.0))
    normalized.setdefault(
        "platform_retained_amount",
        round(_base_amount_from_walk(normalized) - _coerce_float(normalized.get("walker_payout"), 0.0), 2),
    )
    normalized.setdefault("client_refund_amount", 0.0)

    if normalized.get("status") == STATUS_FINISHED and not normalized.get("tip_deadline_at"):
        reference_dt = (
            _parse_iso_datetime(normalized.get("decision_resolved_at"))
            or _parse_iso_datetime(normalized.get("updated_at"))
            or _parse_iso_datetime(normalized.get("walk_datetime_iso"))
            or datetime.now(timezone.utc)
        )
        normalized["tip_deadline_at"] = (reference_dt + timedelta(hours=24)).isoformat()

    if not normalized.get("attendance_message"):
        if normalized.get("status") == STATUS_NO_SHOW_CLIENT:
            normalized["attendance_message"] = "Cliente não compareceu dentro do prazo de tolerância."
        elif normalized.get("status") == STATUS_NO_SHOW_WALKER:
            normalized["attendance_message"] = "Passeador não compareceu dentro do prazo de tolerância."
        elif normalized.get("status") == STATUS_PENDING_REVIEW:
            normalized["attendance_message"] = "Sem registro das partes no prazo. Encaminhado para análise manual."

    if not normalized.get("occurrence_status"):
        normalized["occurrence_status"] = _derive_occurrence_status(normalized)

    if isinstance(normalized.get("occurrence_logs"), list):
        sanitized_logs: List[Dict[str, Any]] = []
        for log in normalized["occurrence_logs"]:
            if not isinstance(log, dict):
                continue
            try:
                sanitized_logs.append(OccurrenceLogEntry(**log).model_dump())
            except Exception:
                continue
        normalized["occurrence_logs"] = sanitized_logs

    if normalized.get("walk_type") == WALK_TYPE_SHARED and normalized.get("shared_pet_names"):
        normalized["pet_name"] = " + ".join(normalized.get("shared_pet_names", []))

    if not normalized.get("walk_datetime_iso"):
        walk_date = str(normalized.get("walk_date") or "").strip()
        walk_time = str(normalized.get("walk_time") or "").strip()
        if walk_date and walk_time:
            normalized["walk_datetime_iso"] = f"{walk_date}T{walk_time}:00Z"
        else:
            normalized["walk_datetime_iso"] = datetime.now(timezone.utc).isoformat()

    if normalized.get("rating") is not None:
        normalized["rating"] = int(max(0, min(5, round(_coerce_float(normalized.get("rating"), 0.0)))))

    return WalkResponse(**normalized)


def _to_payment_response(payment: dict) -> AdminPaymentResponse:
    normalized = dict(payment)
    normalized.setdefault("plan_type", "Avulso")
    normalized.setdefault("tipoPlano", "avulso")
    normalized.setdefault("payment_method", "")
    #normalized.setdefault("tipoPagamento", normalized.get("payment_method", ""))
    normalized.setdefault("notes", "")
    return AdminPaymentResponse(**normalized)


def _generate_security_code() -> str:
    return f"{random.randint(1000, 9999)}"


def _generate_walk_summary(walk: dict) -> str:
    pet_name = walk.get("pet_name", "Seu pet")
    walker_name = walk.get("walker_name", "profissional")
    duration = walk.get("duration_minutes", 30)
    did_pee = bool(walk.get("did_pee", False))
    did_poop = bool(walk.get("did_poop", False))

    if did_pee and did_poop:
        needs_text = "Fez xixi e cocô."
    elif did_pee:
        needs_text = "Fez apenas xixi."
    elif did_poop:
        needs_text = "Fez apenas cocô."
    else:
        needs_text = "Não houve registro de necessidades."

    notes = str(walk.get("notes", "")).strip()
    summary = f"{pet_name} passeou por {duration} minutos com {walker_name}. {needs_text}"
    if notes:
        summary = f"{summary} {notes}"
    return summary


def _client_id_from_name(name: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    return "-".join(filter(None, normalized.split("-"))) or "cliente"


def _individual_walk_value(duration_minutes: int) -> float:
    if duration_minutes == 30:
        return 29.90
    if duration_minutes == 45:
        return 34.90
    return 39.90


def _plan_total_weeks(duracao_plano: str) -> int:
    if duracao_plano == "mensal":
        return 4
    if duracao_plano == "trimestral":
        return 12
    return 24


def _plan_frequency_discount_percent(frequencia_semanal: int) -> float:
    if frequencia_semanal <= 1:
        return 0.0
    if frequencia_semanal == 2:
        return 3.0
    if frequencia_semanal == 3:
        return 5.0
    if frequencia_semanal == 4:
        return 5.0 + ((8.0 - 5.0) / 2.0)
    return 8.0


def _plan_duration_discount_percent(duracao_plano: str) -> float:
    if duracao_plano == "trimestral":
        return 3.0
    if duracao_plano == "semestral":
        return 6.0
    return 0.0


def _round_money(value: float) -> float:
    return round(float(value or 0.0), 2)


def _build_plan_simulation(
    *,
    frequencia_semanal: int,
    duracao_plano: Literal["mensal", "trimestral", "semestral"],
    duracao_passeio: Literal[30, 45, 60],
    margem_minima_percent: float = 15.0,
) -> dict:
    valor_base_por_passeio = _round_money(_individual_walk_value(int(duracao_passeio)))
    total_passeios = int(max(1, frequencia_semanal) * _plan_total_weeks(duracao_plano))

    desconto_frequencia_percent = _plan_frequency_discount_percent(frequencia_semanal)
    desconto_plano_percent = _plan_duration_discount_percent(duracao_plano)
    desconto_total_percent = min(15.0, desconto_frequencia_percent + desconto_plano_percent)

    platform_share_percent = max(0.0, min(100.0, _coerce_float(RUNTIME_PLATFORM_SHARE_PERCENT, DEFAULT_PLATFORM_SHARE_PERCENT)))
    margem_estimada_percent = max(0.0, platform_share_percent - desconto_total_percent)

    desconto_reduzido_por_margem = False
    margem_minima = max(0.0, _coerce_float(margem_minima_percent, 15.0))
    if margem_estimada_percent < margem_minima:
        desconto_ajustado = max(0.0, platform_share_percent - margem_minima)
        if desconto_ajustado < desconto_total_percent:
            desconto_total_percent = desconto_ajustado
            desconto_reduzido_por_margem = True
        margem_estimada_percent = max(0.0, platform_share_percent - desconto_total_percent)

    desconto_total_percent = min(15.0, max(0.0, desconto_total_percent))

    desconto_frequencia_aplicado = min(desconto_frequencia_percent, desconto_total_percent)
    desconto_plano_aplicado = max(0.0, desconto_total_percent - desconto_frequencia_aplicado)

    valor_total_sem_desconto = _round_money(total_passeios * valor_base_por_passeio)
    valor_total_com_desconto = _round_money(valor_total_sem_desconto * (1.0 - (desconto_total_percent / 100.0)))
    valor_por_passeio = _round_money(valor_total_com_desconto / max(1, total_passeios))
    economia = _round_money(max(0.0, valor_total_sem_desconto - valor_total_com_desconto))

    mensagem_economia = f"Você economiza R$ {economia:.2f} com este plano"
    subscription_payload = {
        "frequencia_semanal": frequencia_semanal,
        "duracao_plano": duracao_plano,
        "duracao_passeio": duracao_passeio,
        "total_passeios": total_passeios,
        "valor_base_por_passeio": valor_base_por_passeio,
        "valor_total_com_desconto": valor_total_com_desconto,
        "valor_por_passeio": valor_por_passeio,
        "desconto_total_percent": round(desconto_total_percent, 2),
        "ready_for_subscription": True,
    }

    return {
        "frequencia_semanal": frequencia_semanal,
        "duracao_plano": duracao_plano,
        "duracao_passeio": duracao_passeio,
        "valor_base_por_passeio": valor_base_por_passeio,
        "total_passeios": total_passeios,
        "desconto_frequencia_percent": round(desconto_frequencia_aplicado, 2),
        "desconto_plano_percent": round(desconto_plano_aplicado, 2),
        "desconto_total_percent": round(desconto_total_percent, 2),
        "desconto_reduzido_por_margem": desconto_reduzido_por_margem,
        "margem_minima_percent": round(margem_minima, 2),
        "margem_estimada_percent": round(margem_estimada_percent, 2),
        "valor_total_sem_desconto": valor_total_sem_desconto,
        "valor_total_com_desconto": valor_total_com_desconto,
        "valor_por_passeio": valor_por_passeio,
        "economia": economia,
        "comparacao_avulso_total": valor_total_sem_desconto,
        "comparacao_avulso_por_passeio": valor_base_por_passeio,
        "mensagem_economia": mensagem_economia,
        "ready_for_subscription": True,
        "subscription_payload": subscription_payload,
    }


def _owner_key_from_pet(pet: dict) -> str:
    owner_user_id = str(pet.get("owner_user_id", "")).strip()
    if owner_user_id:
        return f"user:{owner_user_id}"
    owner_profile_id = str(pet.get("owner_profile_id", "")).strip()
    if owner_profile_id:
        return f"profile:{owner_profile_id}"
    return ""


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return round(r * c, 2)


@lru_cache(maxsize=512)
def _geocode_location(query: str) -> Optional[dict]:
    cleaned = (query or "").strip()
    if not cleaned:
        return None
    try:
        location = GEOCODER.geocode(cleaned, exactly_one=True, country_codes="br", timeout=8)
    except (GeocoderTimedOut, GeocoderServiceError):
        return None
    except Exception:
        return None

    if not location:
        return None

    return {
        "address": location.address,
        "latitude": float(location.latitude),
        "longitude": float(location.longitude),
    }


def _calculate_premium_transport(distance_km: float, settings: Optional[dict] = None) -> tuple[float, bool, str, int]:
    transport_settings = settings or _default_pet_transport_settings_dict()
    manual_limit_km = max(1.0, _coerce_float(transport_settings.get("auto_approve_distance_km"), 5.0))
    pricing_mode = str(transport_settings.get("pricing_mode") or "fixed")
    fee_fixed = max(0.0, _coerce_float(transport_settings.get("transport_fee_fixed"), 12.0))
    fee_per_km = max(0.0, _coerce_float(transport_settings.get("transport_fee_per_km"), 2.5))
    minutes_per_km = max(1.0, _coerce_float(transport_settings.get("estimated_minutes_per_km"), 3.0))

    if distance_km <= 0:
        return 0.0, False, PREMIUM_ANALYSIS_APPROVED, 0

    additional_fee = fee_fixed if pricing_mode == "fixed" else round(distance_km * fee_per_km, 2)
    estimated_travel_minutes = max(5, int(round(distance_km * minutes_per_km)))

    if distance_km > manual_limit_km:
        return additional_fee, True, PREMIUM_ANALYSIS_WAITING, estimated_travel_minutes

    return additional_fee, False, PREMIUM_ANALYSIS_APPROVED, estimated_travel_minutes


async def _estimate_transport_route(
    *,
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    fallback_minutes_per_km: float,
) -> tuple[float, int]:
    fallback_distance = max(0.1, _haversine_km(origin_lat, origin_lng, destination_lat, destination_lng))
    fallback_minutes = max(5, int(round(fallback_distance * max(1.0, fallback_minutes_per_km))))

    route_url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{origin_lng},{origin_lat};{destination_lng},{destination_lat}?overview=false"
    )

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(route_url)
            if response.status_code >= 400:
                return fallback_distance, fallback_minutes
            payload = response.json() if response.content else {}
            routes = payload.get("routes") if isinstance(payload, dict) else None
            if not routes:
                return fallback_distance, fallback_minutes
            route = routes[0]
            distance_meters = _coerce_float(route.get("distance"), fallback_distance * 1000)
            duration_seconds = _coerce_float(route.get("duration"), fallback_minutes * 60)
            distance_km = max(0.1, distance_meters / 1000.0)
            duration_minutes = max(5, int(round(duration_seconds / 60.0)))
            return distance_km, duration_minutes
    except Exception:
        return fallback_distance, fallback_minutes


def _transport_events_for_status(status: str) -> List[tuple[str, str]]:
    if status == STATUS_GOING_TO_PICKUP:
        return [("inicio_deslocamento", "A caminho do local")]
    if status in {STATUS_WALKING_NOW, LEGACY_STATUS_IN_PROGRESS}:
        return [
            ("chegada_destino", "Chegou ao local"),
            ("inicio_passeio", "Passeando"),
        ]
    if status == STATUS_FINISHED:
        return [
            ("fim_passeio", "Fim do passeio"),
            ("retorno", "Retornando"),
        ]
    return []


def _normalize_coupon_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    cleaned = re.sub(r"[^A-Z0-9_-]", "", raw)
    return cleaned


def _normalize_coupon_walk_types(values: Any) -> List[str]:
    source = values if isinstance(values, list) else []
    normalized: List[str] = []
    for item in source:
        walk_type = str(item).strip()
        if walk_type in COUPON_WALK_TYPES and walk_type not in normalized:
            normalized.append(walk_type)
    return normalized or COUPON_WALK_TYPES.copy()


def _parse_coupon_datetime_input(value: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        base_dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            return base_dt.replace(hour=23, minute=59, second=59)
        return base_dt.replace(hour=0, minute=0, second=0)

    parsed = _parse_iso_datetime(text)
    if not parsed:
        raise HTTPException(status_code=422, detail="Data de cupom inválida")
    return parsed


def _walk_subtotal_before_discount(walk: dict) -> float:
    base_component = _base_walk_price(walk)
    additional_component = round(_coerce_float(walk.get("adicionalDeslocamento", 0.0), 0.0), 2)
    return round(base_component + additional_component, 2)


def _coupon_discount_components(walk: dict, subtotal: float) -> tuple[float, float, float]:
    discount_percent = min(100.0, max(0.0, _coerce_float(walk.get("discount_percent_applied", 0.0), 0.0)))
    discount_fixed = max(0.0, _coerce_float(walk.get("discount_fixed_applied", 0.0), 0.0))
    explicit_discount = max(0.0, _coerce_float(walk.get("discount_amount", 0.0), 0.0))

    if discount_percent > 0 or discount_fixed > 0:
        discount_amount = round((subtotal * (discount_percent / 100.0)) + discount_fixed, 2)
    else:
        discount_amount = round(explicit_discount, 2)

    discount_amount = min(round(max(0.0, discount_amount), 2), round(max(0.0, subtotal), 2))
    return discount_percent, discount_fixed, discount_amount


def _base_walk_price(walk: dict) -> float:
    walk_type = walk.get("walk_type", WALK_TYPE_INDIVIDUAL)
    if walk_type == WALK_TYPE_SHARED:
        pet_count = len(walk.get("pet_ids", [])) or len(walk.get("shared_pet_names", [])) or 1
        owner_keys = [key for key in walk.get("shared_owner_keys", []) if key]
        unique_owner_count = len(set(owner_keys)) if owner_keys else 1

        if pet_count >= 2 and unique_owner_count <= 1:
            base_price = 54.90
        elif pet_count >= 2 and unique_owner_count >= 2:
            base_price = round(29.90 * pet_count, 2)
        else:
            base_price = 29.90
    else:
        base_price = _individual_walk_value(int(walk.get("duration_minutes", 30)))

    return round(base_price, 2)


def _calculate_walk_pricing(walk: dict) -> tuple[float, float]:
    base_price = _base_walk_price(walk)
    adicional = round(_coerce_float(walk.get("adicionalDeslocamento", 0.0), 0.0), 2)
    dynamic_multiplier = max(1.0, min(1.0 + DYNAMIC_PRICING_MAX_TOTAL_BOOST, _coerce_float(walk.get("dynamic_price_multiplier", 1.0), 1.0)))
    subtotal = round((base_price + adicional) * dynamic_multiplier, 2)
    _, _, discount_amount = _coupon_discount_components(walk, subtotal)
    total_price = round(max(0.0, subtotal - discount_amount), 2)

    walker_share_percent = min(80.0, max(70.0, _coerce_float(RUNTIME_WALKER_SHARE_PERCENT, DEFAULT_WALKER_SHARE_PERCENT)))
    walker_payout = round(total_price * (walker_share_percent / 100.0), 2)
    walker_payout = min(walker_payout, total_price)
    return total_price, walker_payout


def _default_incentive_settings_dict() -> dict:
    return {
        "id": "default",
        "walker_share_percent": DEFAULT_WALKER_SHARE_PERCENT,
        "platform_share_percent": DEFAULT_PLATFORM_SHARE_PERCENT,
        "quality_bonus_percent": DEFAULT_QUALITY_BONUS_PERCENT,
        "quality_bonus_min_weighted": DEFAULT_QUALITY_BONUS_MIN_WEIGHTED,
        "quality_bonus_min_walks": DEFAULT_QUALITY_BONUS_MIN_WALKS,
        "consistency_bonus_amount": DEFAULT_CONSISTENCY_BONUS_AMOUNT,
        "consistency_days_required": DEFAULT_CONSISTENCY_DAYS_REQUIRED,
        "critical_hour_bonus_amount": DEFAULT_CRITICAL_HOUR_BONUS_AMOUNT,
        "critical_windows": DEFAULT_CRITICAL_WINDOWS.copy(),
        "volume_bonus_tiers": [dict(item) for item in DEFAULT_VOLUME_BONUS_TIERS],
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _refresh_runtime_incentive_split(settings: dict):
    global RUNTIME_WALKER_SHARE_PERCENT, RUNTIME_PLATFORM_SHARE_PERCENT
    walker_share = _coerce_float(settings.get("walker_share_percent"), DEFAULT_WALKER_SHARE_PERCENT)
    platform_share = _coerce_float(settings.get("platform_share_percent"), max(0.0, 100.0 - walker_share))
    total = walker_share + platform_share
    if total <= 0:
        walker_share = DEFAULT_WALKER_SHARE_PERCENT
        platform_share = DEFAULT_PLATFORM_SHARE_PERCENT
    else:
        walker_share = round((walker_share / total) * 100.0, 2)
        platform_share = round(100.0 - walker_share, 2)

    RUNTIME_WALKER_SHARE_PERCENT = min(80.0, max(70.0, walker_share))
    RUNTIME_PLATFORM_SHARE_PERCENT = round(100.0 - RUNTIME_WALKER_SHARE_PERCENT, 2)


async def _get_incentive_settings_dict() -> dict:
    settings = await db.incentive_settings.find_one({"id": "default"}, {"_id": 0})
    if not settings:
        settings = _default_incentive_settings_dict()
        await db.incentive_settings.insert_one(settings)
    _refresh_runtime_incentive_split(settings)
    return settings


def _default_referral_program_settings_dict() -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": "default",
        "program_enabled": False,
        "client_referral_enabled": False,
        "walker_referral_enabled": False,
        "app_visible": False,
        "client_rules": ClientReferralRules().model_dump(),
        "walker_rules": WalkerReferralRules().model_dump(),
        "created_at": now_iso,
        "updated_at": now_iso,
        "updated_by": "system",
    }


async def _get_referral_program_settings_dict() -> dict:
    settings = await db.referral_program_settings.find_one({"id": "default"}, {"_id": 0})
    if not settings:
        settings = _default_referral_program_settings_dict()
        await db.referral_program_settings.insert_one(settings)
    return settings


def _default_pet_transport_settings_dict() -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": "default",
        "pricing_mode": "fixed",
        "transport_fee_fixed": 12.0,
        "transport_fee_per_km": 2.5,
        "auto_approve_distance_km": 5.0,
        "estimated_minutes_per_km": 3.0,
        "tracking_interval_seconds": 15,
        "pet_transport_enabled_for": ["all"],
        "updated_at": now_iso,
        "updated_by": "system",
    }


def _sanitize_pet_transport_enabled_for(raw_values: Any) -> List[str]:
    if not isinstance(raw_values, list):
        return ["all"]
    normalized: List[str] = []
    seen: set[str] = set()
    for value in raw_values:
        item = str(value or "").strip().lower()
        if not item:
            continue
        if item in {"all", "test_users"} or item.startswith("user:") or item.startswith("email:"):
            if item not in seen:
                seen.add(item)
                normalized.append(item)
    if not normalized:
        return ["all"]
    return normalized


def _is_test_user_account(user: Optional[dict]) -> bool:
    if not user:
        return False
    if bool(user.get("is_test_user", False)):
        return True
    email = str(user.get("email") or "").strip().lower()
    return email.endswith("@petpasso.com")


def _walker_kit_profile_from_user(user: Optional[dict]) -> Dict[str, Any]:
    source = user or {}
    water_sealed = bool(source.get("water_sealed", source.get("has_water", False)))
    water_bowl = bool(source.get("water_bowl", source.get("has_bowl", False)))
    poop_bags = bool(source.get("poop_bags", source.get("has_bags", False)))
    first_aid_kit = bool(source.get("first_aid_kit", source.get("has_first_aid", False)))
    profile = {
        "water_sealed": water_sealed,
        "water_bowl": water_bowl,
        "poop_bags": poop_bags,
        "first_aid_kit": first_aid_kit,
        "has_water": water_sealed,
        "has_bowl": water_bowl,
        "has_bags": poop_bags,
        "has_first_aid": first_aid_kit,
        "has_towel": bool(source.get("has_towel", False)),
        "has_extra_leash": bool(source.get("has_extra_leash", False)),
        "has_premium_items": bool(source.get("has_premium_items", False)),
    }
    profile["kit_complete"] = bool(water_sealed and water_bowl and poop_bags and first_aid_kit)
    profile["kit_basic_complete"] = all(bool(profile[field]) for field in KIT_BASIC_FIELDS)
    profile["kit_essential_complete"] = all(bool(profile[field]) for field in KIT_ESSENTIAL_FIELDS)
    profile["kit_premium"] = bool(profile["has_premium_items"])
    return profile


def _kit_base_level(profile: Dict[str, Any]) -> int:
    if not profile.get("kit_basic_complete", False):
        return 0
    if profile.get("kit_essential_complete", False) and profile.get("kit_premium", False):
        return 3
    if profile.get("kit_essential_complete", False):
        return 2
    return 1


def _kit_effective_level(profile: Dict[str, Any], missing_reports_count: int) -> int:
    base_level = _kit_base_level(profile)
    if missing_reports_count >= 3:
        return max(0, base_level - 1)
    return base_level


def _kit_labels_from_level(level: int) -> List[str]:
    labels: List[str] = []
    if level >= 1:
        labels.append("Kit Básico Completo")
    if level >= 2:
        labels.append("Kit Essencial")
    if level >= 3:
        labels.append("Passeador Premium")
    return labels


def _kit_boost_factor(level: int) -> float:
    if level >= 3:
        return KIT_LEVEL3_BOOST_FACTOR
    if level >= 2:
        return KIT_LEVEL2_BOOST_FACTOR
    return 0.0


def _kit_reliability_penalty_points(missing_reports_count: int) -> float:
    if missing_reports_count <= 1:
        return 0.0
    if missing_reports_count == 2:
        return 2.5
    return min(12.0, 5.0 + (missing_reports_count - 3) * 2.0)


def _validate_kit_checklist_confirmation(payload: WalkKitChecklistConfirm) -> Dict[str, bool]:
    checklist = {
        "checklist_confirm_water": bool(payload.checklist_confirm_water),
        "checklist_confirm_bowl": bool(payload.checklist_confirm_bowl),
        "checklist_confirm_bags": bool(payload.checklist_confirm_bags),
        "checklist_confirm_first_aid": bool(payload.checklist_confirm_first_aid),
    }
    if not all(checklist.values()):
        raise HTTPException(status_code=400, detail="Checklist obrigatório: confirme água lacrada, vasilha, saquinhos e kit de primeiros socorros")
    return checklist


def _assert_walker_basic_kit_ready(kit_profile: Dict[str, Any]):
    if not kit_profile.get("kit_complete", False):
        raise HTTPException(
            status_code=400,
            detail="Kit obrigatório incompleto. Inclua água lacrada, recipiente, saquinhos e primeiros socorros",
        )


def _default_premium_verified_settings_dict() -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": "default",
        "streak_minimo_para_selo": DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET,
        "bonus_score_base": DEFAULT_PREMIUM_VERIFIED_BONUS_SCORE,
        "priority_bonus": DEFAULT_PREMIUM_VERIFIED_PRIORITY_BONUS,
        "cr_efficiency_multiplier": DEFAULT_PREMIUM_VERIFIED_CR_EFFICIENCY_MULTIPLIER,
        "updated_at": now_iso,
        "updated_by": "system",
    }


async def _get_premium_verified_settings_dict() -> dict:
    row = await db.premium_verified_settings.find_one({"id": "default"}, {"_id": 0})
    if not row:
        row = _default_premium_verified_settings_dict()
        await db.premium_verified_settings.insert_one(row)
    normalized = {
        **_default_premium_verified_settings_dict(),
        **row,
    }
    normalized["streak_minimo_para_selo"] = int(max(5, min(20, int(normalized.get("streak_minimo_para_selo") or DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET))))
    normalized["bonus_score_base"] = round(min(20.0, max(0.0, _coerce_float(normalized.get("bonus_score_base"), DEFAULT_PREMIUM_VERIFIED_BONUS_SCORE))), 2)
    normalized["priority_bonus"] = round(min(10.0, max(0.0, _coerce_float(normalized.get("priority_bonus"), DEFAULT_PREMIUM_VERIFIED_PRIORITY_BONUS))), 2)
    normalized["cr_efficiency_multiplier"] = round(min(2.0, max(1.0, _coerce_float(normalized.get("cr_efficiency_multiplier"), DEFAULT_PREMIUM_VERIFIED_CR_EFFICIENCY_MULTIPLIER))), 2)
    return normalized


def _normalize_marketplace_region(city: Optional[str], neighborhood: Optional[str]) -> tuple[str, str]:
    return (
        str(city or "").strip().lower(),
        str(neighborhood or "").strip().lower(),
    )


def _default_marketplace_intelligence_settings_dict() -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": "default",
        "mode": MARKETPLACE_MODE_AUTOMATIC,
        "polling_seconds": MARKETPLACE_DEFAULT_POLLING_SECONDS,
        "cr_weight_percent": MARKETPLACE_DEFAULT_CR_WEIGHT_PERCENT,
        "cr_boost_cap_points": 12.0,
        "context_sensitivity": 1.0,
        "critical_ratio_threshold": MARKETPLACE_DEFAULT_CRITICAL_RATIO,
        "balanced_ratio_floor": MARKETPLACE_DEFAULT_BALANCED_RATIO_FLOOR,
        "balanced_ratio_ceil": MARKETPLACE_DEFAULT_BALANCED_RATIO_CEIL,
        "low_supply_cr_multiplier": 1.2,
        "high_supply_cr_multiplier": 0.7,
        "low_supply_cr_gain_multiplier": 1.2,
        "high_supply_quality_floor": 60.0,
        "low_supply_min_score_relaxation": 5.0,
        "high_supply_min_score_hardening": 5.0,
        "low_supply_wave_extra_candidates": 2,
        "high_supply_wave_reduction": 1,
        "critical_acceptance_seconds": MARKETPLACE_CRITICAL_ACCEPTANCE_SECONDS,
        "critical_match_rate_threshold": MARKETPLACE_CRITICAL_MATCH_RATE,
        "surplus_match_rate_threshold": MARKETPLACE_SURPLUS_MATCH_RATE,
        "regional_rules": [],
        "updated_at": now_iso,
        "updated_by": "system",
    }


def _normalize_marketplace_regional_rules(raw_rules: Any) -> List[dict]:
    rules: List[dict] = []
    if not isinstance(raw_rules, list):
        return rules

    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        city, neighborhood = _normalize_marketplace_region(raw.get("city"), raw.get("neighborhood"))
        if not city and not neighborhood:
            continue
        rules.append(
            {
                "city": city,
                "neighborhood": neighborhood,
                "priority_bonus_points": round(min(10.0, max(0.0, _coerce_float(raw.get("priority_bonus_points"), 0.0))), 2),
                "cr_weight_percent": (
                    round(min(MARKETPLACE_MAX_CR_WEIGHT_PERCENT, max(0.0, _coerce_float(raw.get("cr_weight_percent"), 0.0))), 2)
                    if raw.get("cr_weight_percent") is not None
                    else None
                ),
                "context_sensitivity": (
                    round(min(3.0, max(0.5, _coerce_float(raw.get("context_sensitivity"), 1.0))), 2)
                    if raw.get("context_sensitivity") is not None
                    else None
                ),
                "enabled": bool(raw.get("enabled", True)),
            }
        )

    return rules[:200]


def _normalize_marketplace_intelligence_settings(settings: dict) -> dict:
    defaults = _default_marketplace_intelligence_settings_dict()
    merged = {**defaults, **(settings or {})}

    mode = str(merged.get("mode") or MARKETPLACE_MODE_AUTOMATIC).strip().lower()
    if mode not in {MARKETPLACE_MODE_AUTOMATIC, MARKETPLACE_MODE_ASSISTED, MARKETPLACE_MODE_MANUAL}:
        mode = MARKETPLACE_MODE_AUTOMATIC
    merged["mode"] = mode
    merged["polling_seconds"] = int(max(10, min(300, int(merged.get("polling_seconds") or MARKETPLACE_DEFAULT_POLLING_SECONDS))))
    merged["cr_weight_percent"] = round(
        min(MARKETPLACE_MAX_CR_WEIGHT_PERCENT, max(0.0, _coerce_float(merged.get("cr_weight_percent"), MARKETPLACE_DEFAULT_CR_WEIGHT_PERCENT))),
        2,
    )
    merged["cr_boost_cap_points"] = round(min(30.0, max(1.0, _coerce_float(merged.get("cr_boost_cap_points"), 12.0))), 2)
    merged["context_sensitivity"] = round(min(3.0, max(0.5, _coerce_float(merged.get("context_sensitivity"), 1.0))), 2)
    merged["critical_ratio_threshold"] = round(min(5.0, max(1.0, _coerce_float(merged.get("critical_ratio_threshold"), MARKETPLACE_DEFAULT_CRITICAL_RATIO))), 3)
    merged["balanced_ratio_floor"] = round(min(2.0, max(0.1, _coerce_float(merged.get("balanced_ratio_floor"), MARKETPLACE_DEFAULT_BALANCED_RATIO_FLOOR))), 3)
    merged["balanced_ratio_ceil"] = round(min(4.0, max(0.2, _coerce_float(merged.get("balanced_ratio_ceil"), MARKETPLACE_DEFAULT_BALANCED_RATIO_CEIL))), 3)
    if merged["balanced_ratio_ceil"] <= merged["balanced_ratio_floor"]:
        merged["balanced_ratio_ceil"] = round(merged["balanced_ratio_floor"] + 0.2, 3)
    merged["low_supply_cr_multiplier"] = round(min(3.0, max(1.0, _coerce_float(merged.get("low_supply_cr_multiplier"), 1.2))), 2)
    merged["high_supply_cr_multiplier"] = round(min(1.0, max(0.1, _coerce_float(merged.get("high_supply_cr_multiplier"), 0.7))), 2)
    merged["low_supply_cr_gain_multiplier"] = round(min(3.0, max(1.0, _coerce_float(merged.get("low_supply_cr_gain_multiplier"), 1.2))), 2)
    merged["high_supply_quality_floor"] = round(min(90.0, max(55.0, _coerce_float(merged.get("high_supply_quality_floor"), 60.0))), 2)
    merged["low_supply_min_score_relaxation"] = round(min(15.0, max(0.0, _coerce_float(merged.get("low_supply_min_score_relaxation"), 5.0))), 2)
    merged["high_supply_min_score_hardening"] = round(min(15.0, max(0.0, _coerce_float(merged.get("high_supply_min_score_hardening"), 5.0))), 2)
    merged["low_supply_wave_extra_candidates"] = int(max(0, min(5, int(merged.get("low_supply_wave_extra_candidates") or 2))))
    merged["high_supply_wave_reduction"] = int(max(0, min(3, int(merged.get("high_supply_wave_reduction") or 1))))
    merged["critical_acceptance_seconds"] = round(min(900.0, max(30.0, _coerce_float(merged.get("critical_acceptance_seconds"), MARKETPLACE_CRITICAL_ACCEPTANCE_SECONDS))), 2)
    merged["critical_match_rate_threshold"] = round(min(1.0, max(0.1, _coerce_float(merged.get("critical_match_rate_threshold"), MARKETPLACE_CRITICAL_MATCH_RATE))), 3)
    merged["surplus_match_rate_threshold"] = round(min(1.0, max(0.1, _coerce_float(merged.get("surplus_match_rate_threshold"), MARKETPLACE_SURPLUS_MATCH_RATE))), 3)
    merged["regional_rules"] = _normalize_marketplace_regional_rules(merged.get("regional_rules"))
    return merged


async def _get_marketplace_intelligence_settings_dict() -> dict:
    row = await db.marketplace_intelligence_settings.find_one({"id": "default"}, {"_id": 0})
    if not row:
        row = _default_marketplace_intelligence_settings_dict()
        await db.marketplace_intelligence_settings.insert_one(row)

    normalized = _normalize_marketplace_intelligence_settings(row)
    return normalized


def _resolve_marketplace_regional_rule(settings: dict, city: str, neighborhood: str) -> dict:
    rules = list(settings.get("regional_rules") or [])
    if not rules:
        return {}

    city_key, neighborhood_key = _normalize_marketplace_region(city, neighborhood)
    if not city_key and not neighborhood_key:
        return {}

    exact = next(
        (
            row
            for row in rules
            if bool(row.get("enabled", True))
            and str(row.get("city") or "") == city_key
            and str(row.get("neighborhood") or "") == neighborhood_key
        ),
        None,
    )
    if exact:
        return exact

    city_only = next(
        (
            row
            for row in rules
            if bool(row.get("enabled", True))
            and str(row.get("city") or "") == city_key
            and not str(row.get("neighborhood") or "")
        ),
        None,
    )
    return city_only or {}


def _marketplace_context_from_metrics(metrics: dict, settings: dict, regional_rule: dict) -> str:
    sensitivity = _coerce_float(regional_rule.get("context_sensitivity"), _coerce_float(settings.get("context_sensitivity"), 1.0))
    sensitivity = max(0.5, min(3.0, sensitivity))
    ratio = _coerce_float(metrics.get("demand_supply_ratio"), 0.0)
    match_rate = _coerce_float(metrics.get("match_rate"), 0.0)
    avg_acceptance = _coerce_float(metrics.get("average_acceptance_seconds"), 0.0)

    critical_ratio = _coerce_float(settings.get("critical_ratio_threshold"), MARKETPLACE_DEFAULT_CRITICAL_RATIO) / sensitivity
    balanced_floor = _coerce_float(settings.get("balanced_ratio_floor"), MARKETPLACE_DEFAULT_BALANCED_RATIO_FLOOR) * sensitivity
    balanced_ceil = _coerce_float(settings.get("balanced_ratio_ceil"), MARKETPLACE_DEFAULT_BALANCED_RATIO_CEIL) / sensitivity
    critical_match_rate = _coerce_float(settings.get("critical_match_rate_threshold"), MARKETPLACE_CRITICAL_MATCH_RATE)
    surplus_match_rate = _coerce_float(settings.get("surplus_match_rate_threshold"), MARKETPLACE_SURPLUS_MATCH_RATE)
    critical_acceptance = _coerce_float(settings.get("critical_acceptance_seconds"), MARKETPLACE_CRITICAL_ACCEPTANCE_SECONDS) / sensitivity

    if ratio > critical_ratio or match_rate < critical_match_rate or avg_acceptance > critical_acceptance:
        return MARKETPLACE_CONTEXT_CRITICAL

    if ratio < balanced_floor and match_rate >= surplus_match_rate:
        return MARKETPLACE_CONTEXT_SURPLUS

    if balanced_floor <= ratio <= balanced_ceil:
        return MARKETPLACE_CONTEXT_BALANCED

    return MARKETPLACE_CONTEXT_SURPLUS if ratio < balanced_floor else MARKETPLACE_CONTEXT_CRITICAL


def _default_dynamic_pricing_settings_dict() -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": "default",
        "dynamicPricingEnabled": False,
        "dynamicPricingMode": DYNAMIC_PRICING_MODE_OFF,
        "low_supply_min_boost": DYNAMIC_PRICING_MIN_SUPPLY_BOOST,
        "low_supply_max_boost": DYNAMIC_PRICING_MAX_SUPPLY_BOOST,
        "high_demand_min_boost": DYNAMIC_PRICING_MIN_DEMAND_BOOST,
        "high_demand_max_boost": DYNAMIC_PRICING_MAX_DEMAND_BOOST,
        "critical_hour_boost": DYNAMIC_PRICING_CRITICAL_BOOST,
        "max_total_boost": DYNAMIC_PRICING_MAX_TOTAL_BOOST,
        "smoothing_limit": 0.10,
        "max_price_cap": 40.0,
        "auto_calibration_enabled": False,
        "manual_lock": False,
        "calibration_window_days": 7,
        "calibration_min_events": 100,
        "max_step_adjustment": 0.02,
        "last_calibrated_at": "",
        "last_conversion_rate": 0.0,
        "last_avg_revenue": 0.0,
        "updated_at": now_iso,
        "updated_by": "system",
    }


def _normalize_dynamic_pricing_settings(settings: dict) -> dict:
    defaults = _default_dynamic_pricing_settings_dict()
    merged = {**defaults, **(settings or {})}
    mode = str(merged.get("dynamicPricingMode") or DYNAMIC_PRICING_MODE_OFF).strip().lower()
    if mode not in {DYNAMIC_PRICING_MODE_OFF, DYNAMIC_PRICING_MODE_SHADOW, DYNAMIC_PRICING_MODE_ACTIVE}:
        mode = DYNAMIC_PRICING_MODE_OFF
    merged["dynamicPricingEnabled"] = bool(merged.get("dynamicPricingEnabled", False))
    if not merged["dynamicPricingEnabled"]:
        mode = DYNAMIC_PRICING_MODE_OFF
    merged["dynamicPricingMode"] = mode

    merged["low_supply_min_boost"] = round(min(DYNAMIC_PRICING_MAX_ARCHITECTURE_BOOST, max(0.0, _coerce_float(merged.get("low_supply_min_boost"), DYNAMIC_PRICING_MIN_SUPPLY_BOOST))), 4)
    merged["low_supply_max_boost"] = round(min(DYNAMIC_PRICING_MAX_ARCHITECTURE_BOOST, max(merged["low_supply_min_boost"], _coerce_float(merged.get("low_supply_max_boost"), DYNAMIC_PRICING_MAX_SUPPLY_BOOST))), 4)
    merged["high_demand_min_boost"] = round(min(DYNAMIC_PRICING_MAX_ARCHITECTURE_BOOST, max(0.0, _coerce_float(merged.get("high_demand_min_boost"), DYNAMIC_PRICING_MIN_DEMAND_BOOST))), 4)
    merged["high_demand_max_boost"] = round(min(DYNAMIC_PRICING_MAX_ARCHITECTURE_BOOST, max(merged["high_demand_min_boost"], _coerce_float(merged.get("high_demand_max_boost"), DYNAMIC_PRICING_MAX_DEMAND_BOOST))), 4)
    merged["critical_hour_boost"] = round(min(0.2, max(0.0, _coerce_float(merged.get("critical_hour_boost"), DYNAMIC_PRICING_CRITICAL_BOOST))), 4)
    merged["max_total_boost"] = round(min(DYNAMIC_PRICING_MAX_TOTAL_BOOST, max(0.0, _coerce_float(merged.get("max_total_boost"), DYNAMIC_PRICING_MAX_TOTAL_BOOST))), 4)
    merged["smoothing_limit"] = round(min(0.2, max(0.0, _coerce_float(merged.get("smoothing_limit"), 0.10))), 4)
    merged["max_price_cap"] = round(min(40.0, max(25.0, _coerce_float(merged.get("max_price_cap"), 40.0))), 2)
    merged["auto_calibration_enabled"] = bool(merged.get("auto_calibration_enabled", False))
    merged["manual_lock"] = bool(merged.get("manual_lock", False))
    merged["calibration_window_days"] = int(max(1, min(30, int(_coerce_float(merged.get("calibration_window_days"), 7)))))
    merged["calibration_min_events"] = int(max(10, min(2000, int(_coerce_float(merged.get("calibration_min_events"), 100)))))
    merged["max_step_adjustment"] = round(min(0.05, max(0.0, _coerce_float(merged.get("max_step_adjustment"), 0.02))), 4)
    merged["last_calibrated_at"] = str(merged.get("last_calibrated_at") or "")
    merged["last_conversion_rate"] = round(max(0.0, _coerce_float(merged.get("last_conversion_rate"), 0.0)), 4)
    merged["last_avg_revenue"] = round(max(0.0, _coerce_float(merged.get("last_avg_revenue"), 0.0)), 2)
    merged["updated_at"] = str(merged.get("updated_at") or datetime.now(timezone.utc).isoformat())
    merged["updated_by"] = str(merged.get("updated_by") or "system")
    return merged


async def _load_dynamic_pricing_settings() -> dict:
    row = await db.dynamic_pricing_settings.find_one({"id": "default"}, {"_id": 0})
    if not row:
        defaults = _default_dynamic_pricing_settings_dict()
        await db.dynamic_pricing_settings.update_one({"id": "default"}, {"$set": defaults}, upsert=True)
        return defaults
    normalized = _normalize_dynamic_pricing_settings(row)
    if normalized != row:
        await db.dynamic_pricing_settings.update_one({"id": "default"}, {"$set": normalized}, upsert=True)
    calibrated = await _maybe_auto_calibrate_dynamic_pricing_settings(normalized)
    return calibrated


def _utc_now_to_brazil_time(now_dt: Optional[datetime] = None) -> datetime:
    base = now_dt or datetime.now(timezone.utc)
    return base + timedelta(hours=-3)


async def _maybe_auto_calibrate_dynamic_pricing_settings(current_settings: dict) -> dict:
    settings = dict(current_settings)
    if not settings.get("auto_calibration_enabled") or settings.get("manual_lock"):
        return settings

    now_brt = _utc_now_to_brazil_time()
    # Janela estrita: calibração automática apenas na hora local 03:xx BRT (UTC-3).
    if now_brt.hour != 3:
        return settings

    last_calibrated_raw = str(settings.get("last_calibrated_at") or "")
    last_calibrated_dt = _parse_iso_datetime(last_calibrated_raw) if last_calibrated_raw else None
    if last_calibrated_dt and _utc_now_to_brazil_time(last_calibrated_dt).date() == now_brt.date():
        return settings

    window_days = int(settings.get("calibration_window_days", 7) or 7)
    min_events = int(settings.get("calibration_min_events", 100) or 100)
    window_start = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    logs = await db.dynamic_pricing_logs.find({"created_at": {"$gte": window_start}}, {"_id": 0}).to_list(6000)
    if len(logs) < min_events:
        return settings

    attempts = len(logs)
    completed = len([item for item in logs if bool(item.get("completed"))])
    conversion_rate = completed / max(1, attempts)
    avg_revenue = sum(_coerce_float(item.get("final_price"), _coerce_float(item.get("base_price"), 0.0)) for item in logs) / max(1, attempts)
    low_supply_ratio = len([item for item in logs if int(item.get("supply_level", 0) or 0) <= 2]) / max(1, attempts)

    previous_conversion = _coerce_float(settings.get("last_conversion_rate"), 0.0)
    previous_revenue = _coerce_float(settings.get("last_avg_revenue"), 0.0)
    step = _coerce_float(settings.get("max_step_adjustment"), 0.02)

    updated = dict(settings)
    reason = "Sem ajuste automático"
    if previous_conversion > 0 and conversion_rate < previous_conversion * 0.9:
        updated["low_supply_max_boost"] = max(updated["low_supply_min_boost"], round(_coerce_float(updated.get("low_supply_max_boost"), 0.2) - step, 4))
        updated["high_demand_max_boost"] = max(updated["high_demand_min_boost"], round(_coerce_float(updated.get("high_demand_max_boost"), 0.15) - step, 4))
        updated["max_total_boost"] = max(0.0, round(_coerce_float(updated.get("max_total_boost"), DYNAMIC_PRICING_MAX_TOTAL_BOOST) - step, 4))
        reason = "Queda de conversão >10%: reduzindo fatores"
    elif previous_revenue > 0 and conversion_rate >= previous_conversion * 0.98 and avg_revenue > previous_revenue:
        updated["low_supply_max_boost"] = min(DYNAMIC_PRICING_MAX_ARCHITECTURE_BOOST, round(_coerce_float(updated.get("low_supply_max_boost"), 0.2) + step, 4))
        updated["high_demand_max_boost"] = min(DYNAMIC_PRICING_MAX_ARCHITECTURE_BOOST, round(_coerce_float(updated.get("high_demand_max_boost"), 0.15) + step, 4))
        updated["max_total_boost"] = min(DYNAMIC_PRICING_MAX_TOTAL_BOOST, round(_coerce_float(updated.get("max_total_boost"), DYNAMIC_PRICING_MAX_TOTAL_BOOST) + step, 4))
        reason = "Conversão estável + receita maior: ajuste progressivo"

    if low_supply_ratio >= 0.35:
        updated["low_supply_max_boost"] = min(DYNAMIC_PRICING_MAX_ARCHITECTURE_BOOST, round(_coerce_float(updated.get("low_supply_max_boost"), 0.2) + step, 4))
        reason = "Baixa oferta recorrente: reforçando peso de supply"

    updated["last_conversion_rate"] = round(conversion_rate, 4)
    updated["last_avg_revenue"] = round(avg_revenue, 2)
    updated["last_calibrated_at"] = datetime.now(timezone.utc).isoformat()
    updated["updated_at"] = updated["last_calibrated_at"]
    updated = _normalize_dynamic_pricing_settings(updated)

    changed = any(updated.get(key) != settings.get(key) for key in ["low_supply_max_boost", "high_demand_max_boost", "max_total_boost", "last_conversion_rate", "last_avg_revenue", "last_calibrated_at"])
    if not changed:
        return settings

    snapshot_id = str(uuid.uuid4())
    await db.dynamic_pricing_snapshots.insert_one(
        {
            "id": snapshot_id,
            "created_at": updated["last_calibrated_at"],
            "reason": reason,
            "previous_settings": settings,
            "new_settings": updated,
            "conversion_rate": round(conversion_rate * 100.0, 2),
            "avg_revenue": round(avg_revenue, 2),
            "impact_note": reason,
            "is_auto": True,
        }
    )
    updated["last_snapshot_id"] = snapshot_id
    await db.dynamic_pricing_settings.update_one({"id": "default"}, {"$set": updated}, upsert=True)
    return updated


def calculateDynamicPrice(input_payload: dict, settings: dict) -> dict:
    base_price = round(max(0.0, _coerce_float(input_payload.get("basePrice"), 0.0)), 2)
    demand_level = int(max(0, _coerce_float(input_payload.get("demandLevel"), 0.0)))
    supply_level = int(max(0, _coerce_float(input_payload.get("supplyLevel"), 0.0)))
    time_slot = str(input_payload.get("timeSlot") or "")
    previous_multiplier = max(1.0, _coerce_float(input_payload.get("previousMultiplier"), 1.0))
    max_total_boost_guardrail = min(
        DYNAMIC_PRICING_MAX_TOTAL_BOOST,
        max(0.0, _coerce_float(settings.get("max_total_boost"), DYNAMIC_PRICING_MAX_TOTAL_BOOST)),
    )
    absolute_multiplier_cap = round(1.0 + max_total_boost_guardrail, 4)
    max_price_cap = round(min(40.0, max(25.0, _coerce_float(settings.get("max_price_cap"), 40.0))), 2)

    low_supply_ratio = 0.0
    if supply_level <= 2:
        low_supply_ratio = settings["low_supply_max_boost"]
    elif supply_level <= 4:
        low_supply_ratio = max(settings["low_supply_min_boost"], (settings["low_supply_min_boost"] + settings["low_supply_max_boost"]) / 2)
    elif supply_level <= 6:
        low_supply_ratio = settings["low_supply_min_boost"]

    demand_ratio = 0.0
    hour = int(time_slot[:2] or 0) if time_slot else 0
    if (6 <= hour <= 8) or (17 <= hour <= 20):
        demand_ratio = settings["high_demand_max_boost"] if demand_level >= 6 else max(settings["high_demand_min_boost"], 0.10)
    elif 12 <= hour <= 14:
        demand_ratio = settings["high_demand_min_boost"]

    time_ratio = 0.0
    if _is_critical_hour(time_slot, DEFAULT_CRITICAL_WINDOWS):
        time_ratio = settings["critical_hour_boost"] if demand_level < 7 else min(0.10, settings["critical_hour_boost"] * 2)

    total_boost = min(max_total_boost_guardrail, low_supply_ratio + demand_ratio + time_ratio)
    raw_multiplier = 1.0 + total_boost
    smoothing_limit = _coerce_float(settings.get("smoothing_limit"), 0.10)
    min_allowed = max(1.0, previous_multiplier - smoothing_limit)
    max_allowed = previous_multiplier + smoothing_limit
    multiplier = round(min(absolute_multiplier_cap, min(max_allowed, max(min_allowed, raw_multiplier))), 4)
    dynamic_price = round(min(max_price_cap, max(0.0, base_price * multiplier)), 2)

    return {
        "base_price": base_price,
        "dynamic_price": dynamic_price,
        "multiplier": multiplier,
        "difference_percent": round(((dynamic_price - base_price) / max(base_price, 0.01)) * 100.0, 2),
        "applied_boost": round(total_boost, 4),
        "demand_component": round(demand_ratio, 4),
        "supply_component": round(low_supply_ratio, 4),
        "time_component": round(time_ratio, 4),
        "guardrail_multiplier_cap": absolute_multiplier_cap,
        "guardrail_price_cap": max_price_cap,
    }


async def _log_dynamic_pricing_attempt(*, user_id: str, walk_date: str, time_slot: str, day_of_week: str, base_price: float, dynamic_price: float, supply_level: int, demand_level: int, mode: str, final_price: float) -> None:
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    window_start_iso = (now_dt - timedelta(minutes=30)).isoformat()

    attempts_same_slot = await db.dynamic_pricing_logs.count_documents(
        {
            "user_id": user_id,
            "walk_date": walk_date,
            "time_slot": time_slot,
            "created_at": {"$gte": window_start_iso},
        }
    )

    await db.dynamic_pricing_logs.update_many(
        {
            "user_id": user_id,
            "created_at": {"$lt": window_start_iso},
            "completed": False,
            "abandoned": False,
        },
        {"$set": {"abandoned": True, "abandoned_at": now_iso}},
    )

    await db.dynamic_pricing_logs.insert_one(
        {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "created_at": now_iso,
            "walk_date": walk_date,
            "time_slot": time_slot,
            "day_of_week": day_of_week,
            "base_price": round(base_price, 2),
            "dynamic_price_calculated": round(dynamic_price, 2),
            "difference_percent": round(((dynamic_price - base_price) / max(base_price, 0.01)) * 100.0, 2),
            "supply_level": int(supply_level),
            "demand_level": int(demand_level),
            "attempts_same_slot_30m": int(attempts_same_slot + 1),
            "mode": mode,
            "final_price": round(final_price, 2),
            "completed": False,
            "abandoned": False,
            "walk_id": None,
        }
    )


async def _recent_hour_multiplier(walk_date: str, time_slot: str) -> float:
    try:
        hour = int(str(time_slot or "00")[:2])
    except ValueError:
        return 1.0
    previous_hour = max(0, hour - 1)
    previous_prefix = f"{previous_hour:02d}:"
    row = await db.dynamic_pricing_logs.find_one(
        {
            "walk_date": walk_date,
            "time_slot": {"$regex": f"^{previous_prefix}"},
        },
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    if not row:
        return 1.0
    base = _coerce_float(row.get("base_price"), 0.0)
    dynamic = _coerce_float(row.get("dynamic_price_calculated"), base)
    if base <= 0:
        return 1.0
    return max(1.0, min(1.0 + DYNAMIC_PRICING_MAX_TOTAL_BOOST, round(dynamic / base, 4)))


async def _get_latest_dynamic_pricing_preview(*, user_id: str, walk_date: str, walk_time: str) -> Optional[dict]:
    window_start_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    return await db.dynamic_pricing_logs.find_one(
        {
            "user_id": user_id,
            "walk_date": walk_date,
            "time_slot": walk_time,
            "created_at": {"$gte": window_start_iso},
        },
        {"_id": 0},
        sort=[("created_at", -1)],
    )


async def _mark_dynamic_pricing_conversion(
    *,
    user_id: str,
    walk_date: str,
    walk_time: str,
    walk_id: str,
    confirmed_price: Optional[float] = None,
    confirmed_multiplier: Optional[float] = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    latest = await db.dynamic_pricing_logs.find_one(
        {
            "user_id": user_id,
            "walk_date": walk_date,
            "time_slot": walk_time,
            "completed": False,
        },
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    if not latest:
        return

    updates: Dict[str, Any] = {
        "completed": True,
        "abandoned": False,
        "completed_at": now_iso,
        "walk_id": walk_id,
    }

    if confirmed_price is not None:
        updates["confirmed_final_price"] = round(max(0.0, _coerce_float(confirmed_price, 0.0)), 2)
    if confirmed_multiplier is not None:
        updates["confirmed_dynamic_multiplier"] = round(
            max(1.0, min(1.0 + DYNAMIC_PRICING_MAX_TOTAL_BOOST, _coerce_float(confirmed_multiplier, 1.0))),
            4,
        )
    if confirmed_price is not None:
        preview_final = round(max(0.0, _coerce_float(latest.get("final_price"), 0.0)), 2)
        updates["price_preview_vs_confirmed_consistent"] = abs(preview_final - updates["confirmed_final_price"]) <= 0.01

    await db.dynamic_pricing_logs.update_one({"id": str(latest.get("id") or "")}, {"$set": updates})


async def _compute_dynamic_pricing_metrics() -> DynamicPricingMetricsResponse:
    settings = await _load_dynamic_pricing_settings()
    mode = str(settings.get("dynamicPricingMode") or DYNAMIC_PRICING_MODE_OFF)
    if not bool(settings.get("dynamicPricingEnabled", False)):
        mode = DYNAMIC_PRICING_MODE_OFF

    logs = await db.dynamic_pricing_logs.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    if not logs:
        return DynamicPricingMetricsResponse(mode=mode)

    total_attempts = len(logs)
    avg_base = round(sum(_coerce_float(item.get("base_price"), 0.0) for item in logs) / max(1, total_attempts), 2)
    avg_dynamic = round(sum(_coerce_float(item.get("dynamic_price_calculated"), _coerce_float(item.get("base_price"), 0.0)) for item in logs) / max(1, total_attempts), 2)
    low_supply_count = len([item for item in logs if int(item.get("supply_level", 0) or 0) <= 2])
    low_supply_percent = round((low_supply_count / max(1, total_attempts)) * 100.0, 1)

    completed_logs = [item for item in logs if bool(item.get("completed", False))]
    estimated_uplift = round(
        sum(
            max(0.0, _coerce_float(item.get("dynamic_price_calculated"), 0.0) - _coerce_float(item.get("base_price"), 0.0))
            for item in completed_logs
        ),
        2,
    )

    by_hour: Dict[str, Dict[str, int]] = {}
    for item in logs:
        hour = str(item.get("time_slot") or "00:00")[:2]
        if hour not in by_hour:
            by_hour[hour] = {"attempts": 0, "completed": 0, "abandoned": 0}
        by_hour[hour]["attempts"] += 1
        if bool(item.get("completed", False)):
            by_hour[hour]["completed"] += 1
        if bool(item.get("abandoned", False)):
            by_hour[hour]["abandoned"] += 1

    hour_rows: List[DynamicPricingHourMetric] = []
    for hour, counters in by_hour.items():
        attempts = max(1, counters["attempts"])
        hour_rows.append(
            DynamicPricingHourMetric(
                hour=f"{hour}:00",
                attempts=counters["attempts"],
                conversion_rate=round((counters["completed"] / attempts) * 100.0, 1),
                abandonment_rate=round((counters["abandoned"] / attempts) * 100.0, 1),
            )
        )

    hour_rows.sort(key=lambda item: int(item.hour[:2]))
    highest_abandonment = sorted(hour_rows, key=lambda item: item.abandonment_rate, reverse=True)[:5]

    return DynamicPricingMetricsResponse(
        avg_base_price=avg_base,
        avg_dynamic_price=avg_dynamic,
        low_supply_slots_percent=low_supply_percent,
        highest_abandonment_slots=highest_abandonment,
        estimated_shadow_revenue_uplift=estimated_uplift,
        conversion_by_hour=hour_rows,
        total_attempts=total_attempts,
        mode=mode,
    )


async def _compute_marketplace_context_metrics(city: str, neighborhood: str) -> dict:
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    window_start_iso = (now_dt - timedelta(hours=MARKETPLACE_CONTEXT_WINDOW_HOURS)).isoformat()
    city_key, neighborhood_key = _normalize_marketplace_region(city, neighborhood)

    request_query: Dict[str, Any] = {"created_at": {"$gte": window_start_iso}}
    if city_key:
        request_query["pickup_city_normalized"] = city_key
    if neighborhood_key:
        request_query["pickup_neighborhood_normalized"] = neighborhood_key

    demand_active = await db.matching_requests.count_documents({**request_query, "status": "searching"})
    recent_requests = await db.matching_requests.find(
        {**request_query, "status": {"$in": ["matched", "expired", "canceled"]}},
        {"_id": 0, "status": 1, "confirmed_in_seconds": 1},
    ).to_list(400)
    total_recent = len(recent_requests)
    matched_rows = [row for row in recent_requests if str(row.get("status") or "") == "matched"]
    match_rate = round((len(matched_rows) / max(1, total_recent)) if total_recent > 0 else 1.0, 4)
    acceptance_values = [
        _coerce_float(row.get("confirmed_in_seconds"), 0.0)
        for row in matched_rows
        if _coerce_float(row.get("confirmed_in_seconds"), 0.0) > 0
    ]
    avg_acceptance = round((sum(acceptance_values) / len(acceptance_values)) if acceptance_values else 0.0, 2)

    supply_query: Dict[str, Any] = {
        "role": "passeador",
        "isActive": {"$ne": False},
        "quality_status": {"$in": [QUALITY_STATUS_ACTIVE, QUALITY_STATUS_PREMIUM]},
    }
    if city_key and neighborhood_key:
        supply_query["$or"] = [
            {"region": {"$regex": city_key, "$options": "i"}},
            {"region": {"$regex": neighborhood_key, "$options": "i"}},
            {"city": {"$regex": city_key, "$options": "i"}},
            {"neighborhood": {"$regex": neighborhood_key, "$options": "i"}},
        ]
    elif city_key:
        supply_query["$or"] = [
            {"region": {"$regex": city_key, "$options": "i"}},
            {"city": {"$regex": city_key, "$options": "i"}},
        ]

    supply_active = await db.users.count_documents(supply_query)
    ratio = round(demand_active / max(1, supply_active), 4)

    walk_query: Dict[str, Any] = {
        "updated_at": {"$gte": window_start_iso},
        "status": {"$in": [STATUS_FINISHED, STATUS_CANCELED, STATUS_NO_SHOW_CLIENT, STATUS_NO_SHOW_WALKER]},
    }
    if neighborhood_key:
        walk_query["pickup_neighborhood"] = {"$regex": neighborhood_key, "$options": "i"}
    walk_rows = await db.walks.find(walk_query, {"_id": 0, "status": 1}).to_list(400)
    cancel_events = sum(1 for row in walk_rows if str(row.get("status") or "") in {STATUS_CANCELED, STATUS_NO_SHOW_CLIENT, STATUS_NO_SHOW_WALKER})
    cancel_rate = round(cancel_events / max(1, len(walk_rows)), 4)

    cr_usage_24h = await db.reputation_credit_ledger.count_documents(
        {
            "reason": {"$regex": "^use_"},
            "created_at": {"$gte": (now_dt - timedelta(hours=24)).isoformat()},
        }
    )

    return {
        "city": city_key,
        "neighborhood": neighborhood_key,
        "demand_active": int(demand_active),
        "supply_active": int(supply_active),
        "demand_supply_ratio": ratio,
        "match_rate": match_rate,
        "average_acceptance_seconds": avg_acceptance,
        "cancel_rate": cancel_rate,
        "cr_usage_24h": int(cr_usage_24h),
        "updated_at": now_iso,
    }


async def _marketplace_runtime_context(city: str, neighborhood: str) -> dict:
    settings = await _get_marketplace_intelligence_settings_dict()
    metrics = await _compute_marketplace_context_metrics(city, neighborhood)
    regional_rule = _resolve_marketplace_regional_rule(settings, metrics.get("city", ""), metrics.get("neighborhood", ""))
    context_state = _marketplace_context_from_metrics(metrics, settings, regional_rule)

    snapshot = {
        "id": str(uuid.uuid4()),
        "city": metrics.get("city", ""),
        "neighborhood": metrics.get("neighborhood", ""),
        "context_state": context_state,
        "mode": str(settings.get("mode") or MARKETPLACE_MODE_AUTOMATIC),
        **metrics,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.marketplace_context_snapshots.insert_one(snapshot)
    return {
        "settings": settings,
        "metrics": metrics,
        "regional_rule": regional_rule,
        "context_state": context_state,
    }


def _marketplace_effective_cr_weight(settings: dict, regional_rule: dict) -> float:
    override = regional_rule.get("cr_weight_percent")
    base = _coerce_float(override, _coerce_float(settings.get("cr_weight_percent"), MARKETPLACE_DEFAULT_CR_WEIGHT_PERCENT))
    return min(MARKETPLACE_MAX_CR_WEIGHT_PERCENT, max(0.0, base))


def _marketplace_context_cr_multiplier(context_state: str, settings: dict) -> float:
    if context_state == MARKETPLACE_CONTEXT_CRITICAL:
        return max(1.0, _coerce_float(settings.get("low_supply_cr_multiplier"), 1.2))
    if context_state == MARKETPLACE_CONTEXT_SURPLUS:
        return max(0.1, min(1.0, _coerce_float(settings.get("high_supply_cr_multiplier"), 0.7)))
    return 1.0


def _marketplace_adjust_min_score_threshold(base_threshold: float, context_state: str, settings: dict) -> float:
    threshold = _coerce_float(base_threshold, MATCH_MIN_SCORE)
    if context_state == MARKETPLACE_CONTEXT_CRITICAL:
        threshold -= _coerce_float(settings.get("low_supply_min_score_relaxation"), 5.0)
    elif context_state == MARKETPLACE_CONTEXT_SURPLUS:
        threshold += _coerce_float(settings.get("high_supply_min_score_hardening"), 5.0)
    return round(max(MATCH_BEHAVIORAL_LOW_SCORE_BLOCK, min(95.0, threshold)), 2)


def _marketplace_adjust_top_limit(base_top_limit: int, total_candidates: int, context_state: str, settings: dict) -> int:
    top_limit = int(base_top_limit)
    if context_state == MARKETPLACE_CONTEXT_CRITICAL:
        top_limit += int(settings.get("low_supply_wave_extra_candidates") or 0)
    elif context_state == MARKETPLACE_CONTEXT_SURPLUS:
        top_limit -= int(settings.get("high_supply_wave_reduction") or 0)
    return max(1, min(MATCH_TOP_WAVE4_MAX, min(int(total_candidates), top_limit)))


def _marketplace_context_adjustment_points(context_state: str, candidate: dict, settings: dict, regional_rule: dict) -> float:
    sensitivity = _coerce_float(regional_rule.get("context_sensitivity"), _coerce_float(settings.get("context_sensitivity"), 1.0))
    sensitivity = max(0.5, min(3.0, sensitivity))
    priority_bonus = _coerce_float(regional_rule.get("priority_bonus_points"), 0.0)
    score_base = _coerce_float(candidate.get("score_base_component"), 0.0)
    availability = _coerce_float(candidate.get("availability_score"), 0.0)
    load_score = _coerce_float(candidate.get("load_balance_score"), 0.0)

    adjustment = 0.0
    if context_state == MARKETPLACE_CONTEXT_CRITICAL:
        if availability >= 75.0:
            adjustment += 1.2
        if load_score >= 70.0:
            adjustment += 0.8
    elif context_state == MARKETPLACE_CONTEXT_SURPLUS:
        quality_floor = _coerce_float(settings.get("high_supply_quality_floor"), 60.0)
        if score_base < quality_floor:
            adjustment -= 1.5

    adjustment = (adjustment + priority_bonus) * sensitivity
    return round(max(-6.0, min(6.0, adjustment)), 2)


async def _marketplace_cr_gain_multiplier_for_walk(walk: dict) -> float:
    if not _is_feature_active("motor_autonomo_enabled") or not _is_feature_active("dynamic_adjustment_enabled"):
        return 1.0

    city, neighborhood = _normalize_marketplace_region(walk.get("pickup_city"), walk.get("pickup_neighborhood"))
    if not city and not neighborhood:
        return 1.0

    snapshot = await db.marketplace_context_snapshots.find_one(
        {
            "city": city,
            "neighborhood": neighborhood,
            "created_at": {"$gte": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()},
        },
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    if not snapshot:
        return 1.0

    if str(snapshot.get("context_state") or "") != MARKETPLACE_CONTEXT_CRITICAL:
        return 1.0

    settings = await _get_marketplace_intelligence_settings_dict()
    return max(1.0, _coerce_float(settings.get("low_supply_cr_gain_multiplier"), 1.2))


async def _log_marketplace_decision_audit(entry: dict) -> None:
    await db.marketplace_decision_audit.insert_one({
        "id": str(uuid.uuid4()),
        **entry,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


async def _append_walker_event_log(walker_user_id: str, event_type: str, payload: dict) -> None:
    if not walker_user_id:
        return
    await db.walker_operational_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "walker_user_id": walker_user_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _normalize_walker_level_settings(raw: Optional[dict]) -> dict:
    base = dict(DEFAULT_WALKER_LEVEL_SETTINGS)
    if isinstance(raw, dict):
        base.update(raw)

    normalized = {
        "silver_min_walks": int(max(3, min(100, int(base.get("silver_min_walks") or 10)))),
        "silver_min_rating": round(min(5.0, max(3.0, _coerce_float(base.get("silver_min_rating"), 4.5))), 2),
        "silver_max_cancel_rate": round(min(50.0, max(1.0, _coerce_float(base.get("silver_max_cancel_rate"), 15.0))), 2),
        "silver_min_checklist_streak": int(max(1, min(50, int(base.get("silver_min_checklist_streak") or 5)))),
        "silver_min_score_ratio": round(min(1.0, max(0.5, _coerce_float(base.get("silver_min_score_ratio"), 0.78))), 3),
        "gold_min_walks": int(max(10, min(200, int(base.get("gold_min_walks") or 25)))),
        "gold_min_rating": round(min(5.0, max(3.5, _coerce_float(base.get("gold_min_rating"), 4.7))), 2),
        "gold_max_cancel_rate": round(min(30.0, max(0.0, _coerce_float(base.get("gold_max_cancel_rate"), 8.0))), 2),
        "gold_min_checklist_streak": int(max(3, min(100, int(base.get("gold_min_checklist_streak") or 12)))),
        "gold_min_score_ratio": round(min(1.0, max(0.6, _coerce_float(base.get("gold_min_score_ratio"), 0.88))), 3),
        "gold_max_infractions": int(max(0, min(5, int(base.get("gold_max_infractions") or 0)))),
        "bronze_boost_factor": round(min(0.2, max(0.0, _coerce_float(base.get("bronze_boost_factor"), 0.02))), 4),
        "silver_boost_factor": round(min(0.2, max(0.0, _coerce_float(base.get("silver_boost_factor"), 0.04))), 4),
        "gold_boost_factor": round(min(0.2, max(0.0, _coerce_float(base.get("gold_boost_factor"), 0.06))), 4),
    }
    return normalized


async def _get_walker_level_settings_dict() -> dict:
    row = await db.walker_level_settings.find_one({"id": "default"}, {"_id": 0})
    if not row:
        row = {
            "id": "default",
            **dict(DEFAULT_WALKER_LEVEL_SETTINGS),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": "system",
        }
        await db.walker_level_settings.insert_one(row)

    normalized = _normalize_walker_level_settings(row)
    WALKER_LEVEL_SETTINGS_CACHE.clear()
    WALKER_LEVEL_SETTINGS_CACHE.update(normalized)
    return {
        "id": "default",
        **normalized,
        "updated_at": str(row.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        "updated_by": str(row.get("updated_by") or "system"),
    }


def _premium_verified_blocking_occurrence(occurrence_status: str) -> bool:
    return occurrence_status in {
        KIT_OCCURRENCE_STATUS,
        OCC_NO_SHOW_CLIENT,
        OCC_NO_SHOW_WALKER,
        OCC_LATE_SEVERE,
        OCC_SUSPECT_DISINTERMEDIATION,
    }


def _premium_verified_penalty_level(infractions_consecutive: int, severe: bool) -> str:
    if severe or infractions_consecutive >= 3:
        return "grave"
    if infractions_consecutive >= 2:
        return "moderada"
    if infractions_consecutive >= 1:
        return "leve"
    return "none"


def _premium_verified_light_penalty(level: str) -> float:
    if level == "grave":
        return 6.0
    if level == "moderada":
        return 2.5
    if level == "leve":
        return 0.0
    return 0.0


async def _write_premium_verified_badge_audit(
    *,
    walker_user_id: str,
    walk_id: Optional[str],
    action: str,
    reason: str,
    before_active: bool,
    after_active: bool,
    score_bonus_applied: float,
):
    await db.premium_verified_badge_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "walker_user_id": walker_user_id,
            "walk_id": walk_id,
            "action": action,
            "reason": reason,
            "before_active": before_active,
            "after_active": after_active,
            "score_bonus_applied": round(score_bonus_applied, 2),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


async def _evaluate_premium_verified_badge_for_walk(
    *,
    walk: dict,
    trigger: str,
    severe_infraction: bool = False,
) -> None:
    walker_user_id = str(walk.get("walker_user_id") or "").strip()
    if not walker_user_id:
        return
    walker_user = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if not walker_user:
        return

    settings = await _get_premium_verified_settings_dict()
    streak_target = int(settings.get("streak_minimo_para_selo") or DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET)
    bonus_score_base = _coerce_float(settings.get("bonus_score_base"), DEFAULT_PREMIUM_VERIFIED_BONUS_SCORE)
    badge_feature_on = _is_feature_active("premium_verified_badge_enabled") and _is_feature_active("premium_verified_enabled")

    checklist_validado_chegada = bool(walk.get("kit_checklist_check_in_confirmed", False))
    checklist_confirmado_inicio = bool(walk.get("kit_checklist_start_confirmed", False))

    report_payload = walk.get("kit_issue_report") if isinstance(walk.get("kit_issue_report"), dict) else {}
    missing_items = report_payload.get("missing_items") if isinstance(report_payload.get("missing_items"), list) else []
    mandatory_missing = any(
        item in {"has_water", "has_bowl", "has_bags", "has_first_aid", "water_sealed", "water_bowl", "poop_bags", "first_aid_kit"}
        for item in missing_items
    )
    kit_profile = _walker_kit_profile_from_user(walker_user)
    kit_complete = bool(kit_profile.get("kit_complete", False))
    occurrence_status = str(walk.get("occurrence_status") or _derive_occurrence_status(walk) or "")
    has_blocking_infraction = _premium_verified_blocking_occurrence(occurrence_status)

    validation_ok = (
        checklist_validado_chegada
        and checklist_confirmado_inicio
        and kit_complete
        and not mandatory_missing
        and not has_blocking_infraction
    )

    before_active = bool(walker_user.get("premium_verified_badge_active", False))
    current_streak = int(walker_user.get("premium_verified_streak", 0) or 0)
    current_infractions = int(walker_user.get("premium_verified_infractions_consecutive", 0) or 0)
    level_downgrade = int(walker_user.get("premium_verified_level_downgrade", 0) or 0)

    now_iso = datetime.now(timezone.utc).isoformat()
    walker_updates: Dict[str, Any] = {"updated_at": now_iso}
    walk_updates: Dict[str, Any] = {
        "premium_verified_validation": {
            "trigger": trigger,
            "validated_at": now_iso,
            "checklist_validado_chegada": checklist_validado_chegada,
            "checklist_confirmado_inicio": checklist_confirmado_inicio,
            "kit_complete": kit_complete,
            "mandatory_missing": mandatory_missing,
            "has_blocking_infraction": has_blocking_infraction,
            "premium_verified_eligible": validation_ok,
        }
    }

    if validation_ok:
        next_streak = current_streak + 1
        after_active = bool(badge_feature_on and next_streak >= streak_target)
        reason = "Checklist de segurança cumprido"
        walker_updates.update(
            {
                "premium_verified_streak": next_streak,
                "premium_verified_infractions_consecutive": 0,
                "premium_verified_badge_active": after_active,
                "premium_verified_last_reason": reason,
                "premium_verified_penalty_level": "none",
                "premium_verified_penalty_points": 0.0,
            }
        )
        if after_active and not before_active:
            walker_updates["premium_verified_last_activated_at"] = now_iso
            await _write_premium_verified_badge_audit(
                walker_user_id=walker_user_id,
                walk_id=str(walk.get("id") or "") or None,
                action="activated",
                reason=reason,
                before_active=before_active,
                after_active=after_active,
                score_bonus_applied=bonus_score_base,
            )
    else:
        next_infractions = current_infractions + 1
        next_penalty_level = _premium_verified_penalty_level(next_infractions, severe_infraction)
        next_penalty_points = _premium_verified_light_penalty(next_penalty_level)

        after_active = before_active
        reason = "Primeira ocorrência: streak quebrada sem perda do selo"
        if next_infractions >= 2 or severe_infraction:
            after_active = False
            reason = "Ocorrência progressiva: selo removido por infração"
        if next_infractions >= 3 or severe_infraction:
            level_downgrade += 1
            reason = "Ocorrência grave/reincidente: selo removido com rebaixamento"

        walker_updates.update(
            {
                "premium_verified_streak": 0,
                "premium_verified_infractions_consecutive": next_infractions,
                "premium_verified_badge_active": after_active,
                "premium_verified_last_reason": reason,
                "premium_verified_penalty_level": next_penalty_level,
                "premium_verified_penalty_points": next_penalty_points,
                "premium_verified_level_downgrade": level_downgrade,
            }
        )

        if before_active and not after_active:
            walker_updates["premium_verified_last_removed_at"] = now_iso
            await _write_premium_verified_badge_audit(
                walker_user_id=walker_user_id,
                walk_id=str(walk.get("id") or "") or None,
                action="removed",
                reason=reason,
                before_active=before_active,
                after_active=after_active,
                score_bonus_applied=0.0,
            )

    await db.users.update_one({"id": walker_user_id}, {"$set": walker_updates})
    await db.walks.update_one({"id": str(walk.get("id") or "")}, {"$set": {**walk_updates, "updated_at": now_iso}})


def _verification_profile_complete(user_doc: dict) -> bool:
    required_fields = [
        str(user_doc.get("full_name") or "").strip(),
        str(user_doc.get("email") or "").strip(),
        str(user_doc.get("region") or "").strip(),
    ]
    has_schedule = bool(user_doc.get("availability_start_time") and user_doc.get("availability_end_time"))
    return all(required_fields) and has_schedule


async def _verification_recent_grave_occurrence_exists(walker_user_id: str) -> bool:
    window_start = (datetime.now(timezone.utc) - timedelta(days=VERIFICATION_RECENT_WINDOW_DAYS)).isoformat()
    severe_statuses = [
        OCC_NO_SHOW_WALKER,
        OCC_LATE_SEVERE,
        OCC_SUSPECT_DISINTERMEDIATION,
    ]
    count = await db.walks.count_documents(
        {
            "walker_user_id": walker_user_id,
            "updated_at": {"$gte": window_start},
            "$or": [
                {"status": STATUS_NO_SHOW_WALKER},
                {"occurrence_status": {"$in": severe_statuses}},
            ],
        }
    )
    return count > 0


async def _verification_recent_relevant_alert_exists(walker_user_id: str) -> bool:
    window_start = (datetime.now(timezone.utc) - timedelta(days=VERIFICATION_RECENT_WINDOW_DAYS)).isoformat()
    count = await db.walks.count_documents(
        {
            "walker_user_id": walker_user_id,
            "updated_at": {"$gte": window_start},
            "occurrence_status": {
                "$in": [
                    OCC_PENDING_ANALYSIS,
                    OCC_PENDING_ANALYSIS_REOPENED,
                    OCC_NO_SHOW_WALKER,
                    OCC_LATE_SEVERE,
                    KIT_OCCURRENCE_STATUS,
                ]
            },
            "occurrence_resolved": False,
        }
    )
    return count > 0


def _verification_badges_for_level(level: str) -> List[str]:
    if level == VERIFICATION_LEVEL_PREMIUM:
        return ["Verificado", "Alta confiabilidade", "Top desempenho"]
    if level == VERIFICATION_LEVEL_PLUS:
        return ["Verificado", "Alta confiabilidade"]
    if level == VERIFICATION_LEVEL_VERIFIED:
        return ["Verificado"]
    return []


def _verification_visual_meta_for_level(level: str) -> Dict[str, str]:
    if level == VERIFICATION_LEVEL_PREMIUM:
        return {"label": "Verificado Premium", "color": "gold"}
    if level == VERIFICATION_LEVEL_PLUS:
        return {"label": "Verificado Plus", "color": "blue"}
    if level == VERIFICATION_LEVEL_VERIFIED:
        return {"label": "Verificado", "color": "gray"}
    return {"label": "", "color": "none"}


def _derive_walker_verification_level(
    *,
    user_doc: dict,
    metrics: dict,
    kit_profile: Dict[str, Any],
    score_snapshot: float,
    has_recent_grave_occurrence: bool,
    has_recent_relevant_alert: bool,
) -> str:
    completion_percent = _coerce_float(metrics.get("completion_percent"), 0.0)
    rating_avg = _coerce_float(metrics.get("rating_avg"), 0.0)
    if rating_avg <= 0:
        rating_avg = _coerce_float(user_doc.get("rating_avg"), 0.0)
    cancel_rate = _coerce_float(metrics.get("cancel_rate"), 100.0)
    if cancel_rate >= 100.0:
        cancel_rate = _coerce_float(user_doc.get("cancel_rate"), 100.0)
    reliability_component = _coerce_float(metrics.get("reliability_component"), 0.0)
    if reliability_component <= 0:
        reliability_component = _coerce_float(user_doc.get("reliability_component"), 0.0)

    verified_ok = (
        bool(user_doc.get("perfil_id_verificado", False))
        and _verification_profile_complete(user_doc)
        and not has_recent_grave_occurrence
        and bool(kit_profile.get("kit_basic_complete", False))
        and completion_percent >= 85.0
    )
    if not verified_ok:
        return VERIFICATION_LEVEL_NONE

    plus_ok = (
        score_snapshot >= 70.0
        and rating_avg >= 4.5
        and cancel_rate <= VERIFICATION_PLUS_CANCEL_RATE_MAX
    )
    if not plus_ok:
        return VERIFICATION_LEVEL_VERIFIED

    premium_ok = (
        score_snapshot >= 85.0
        and bool(kit_profile.get("kit_essential_complete", False))
        and reliability_component >= 75.0
        and not has_recent_relevant_alert
    )
    if premium_ok:
        return VERIFICATION_LEVEL_PREMIUM
    return VERIFICATION_LEVEL_PLUS


async def _recalculate_walker_verification_for_user(
    *,
    walker_user_id: str,
    trigger: str,
    walk_id: Optional[str] = None,
) -> None:
    walker_user = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if not walker_user:
        return

    if not _is_feature_active("walker_verification_enabled"):
        await db.users.update_one(
            {"id": walker_user_id},
            {
                "$set": {
                    "is_verified": False,
                    "verification_level": VERIFICATION_LEVEL_NONE,
                    "verification_score_snapshot": 0,
                    "verification_badges": [],
                    "verification_label": "",
                    "verification_color": "none",
                    "verification_updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        return

    walker_walks = await db.walks.find(
        {
            "$or": [
                {"walker_user_id": walker_user_id},
                {"walker_id": f"partner-{walker_user_id}"},
            ]
        },
        {"_id": 0},
    ).to_list(length=500)
    quality_status = str(walker_user.get("quality_status") or QUALITY_STATUS_ACTIVE)
    metrics = _compute_reputation_metrics(
        walker_walks=walker_walks,
        quality_status=quality_status,
    )
    kit_profile = _walker_kit_profile_from_user(walker_user)
    has_recent_grave_occurrence = await _verification_recent_grave_occurrence_exists(walker_user_id)
    has_recent_relevant_alert = await _verification_recent_relevant_alert_exists(walker_user_id)

    score_snapshot = _coerce_float(
        walker_user.get("score_final"),
        _coerce_float(metrics.get("reliability_component"), 0.0),
    )
    before_level = str(walker_user.get("verification_level") or VERIFICATION_LEVEL_NONE)
    level = _derive_walker_verification_level(
        user_doc=walker_user,
        metrics=metrics,
        kit_profile=kit_profile,
        score_snapshot=score_snapshot,
        has_recent_grave_occurrence=has_recent_grave_occurrence,
        has_recent_relevant_alert=has_recent_relevant_alert,
    )
    visual_meta = _verification_visual_meta_for_level(level)
    badges = _verification_badges_for_level(level)
    now_iso = datetime.now(timezone.utc).isoformat()
    updates = {
        "is_verified": level != VERIFICATION_LEVEL_NONE,
        "verification_level": level,
        "verification_score_snapshot": int(round(score_snapshot)),
        "verification_badges": badges,
        "verification_label": visual_meta["label"],
        "verification_color": visual_meta["color"],
        "verification_updated_at": now_iso,
        "updated_at": now_iso,
    }
    await db.users.update_one({"id": walker_user_id}, {"$set": updates})

    if before_level != level:
        await db.walker_verification_audit.insert_one(
            {
                "id": str(uuid.uuid4()),
                "walker_user_id": walker_user_id,
                "walk_id": walk_id,
                "trigger": trigger,
                "before_level": before_level,
                "after_level": level,
                "verification_score_snapshot": int(round(score_snapshot)),
                "impact_note": (
                    f"boost={VERIFICATION_PREMIUM_BOOST_POINTS if level == VERIFICATION_LEVEL_PREMIUM else (VERIFICATION_PLUS_BOOST_POINTS if level == VERIFICATION_LEVEL_PLUS else 0)}"
                ),
                "created_at": now_iso,
            }
        )


def _cr_gain_multiplier(level: str) -> float:
    if level == VERIFICATION_LEVEL_PREMIUM:
        return 1.2
    if level == VERIFICATION_LEVEL_PLUS:
        return 1.1
    return 1.0


def _normalize_walker_level_value(level: Optional[str]) -> str:
    raw = str(level or "").strip().lower()
    if raw in {"prata", "silver"}:
        return WALKER_LEVEL_SILVER
    if raw in {"ouro", "gold", "elite"}:
        return WALKER_LEVEL_GOLD
    return WALKER_LEVEL_BRONZE


def _walker_level_boost_factor(level: Optional[str]) -> float:
    normalized = _normalize_walker_level_value(level)
    if normalized == WALKER_LEVEL_GOLD:
        return _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("gold_boost_factor"), 0.06)
    if normalized == WALKER_LEVEL_SILVER:
        return _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("silver_boost_factor"), 0.04)
    return _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("bronze_boost_factor"), 0.02)


def _cr_usage_multipliers(level: str, walker_level: Optional[str] = None) -> tuple[float, float]:
    cost_multiplier = 1.0
    effect_multiplier = 1.0

    if level == VERIFICATION_LEVEL_PREMIUM:
        cost_multiplier *= 0.8
        effect_multiplier *= 1.2

    normalized_level = _normalize_walker_level_value(walker_level)
    if normalized_level == WALKER_LEVEL_SILVER:
        cost_multiplier *= 0.9
    elif normalized_level == WALKER_LEVEL_GOLD:
        cost_multiplier *= 0.85
        effect_multiplier *= 1.15

    return (max(0.5, round(cost_multiplier, 3)), max(1.0, round(effect_multiplier, 3)))


def _is_cr_effect_active_until(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(raw)
    except Exception:
        return False


def _cr_today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _apply_reputation_credit_delta(
    *,
    walker_user_id: str,
    delta: int,
    reason: str,
    event_key: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    if delta == 0:
        user_doc = await db.users.find_one({"id": walker_user_id}, {"_id": 0, "reputation_credits": 1})
        return int((user_doc or {}).get("reputation_credits", 0) or 0)

    existing = await db.reputation_credit_ledger.find_one(
        {"walker_user_id": walker_user_id, "event_key": event_key},
        {"_id": 0, "id": 1},
    )
    if existing:
        user_doc = await db.users.find_one({"id": walker_user_id}, {"_id": 0, "reputation_credits": 1})
        return int((user_doc or {}).get("reputation_credits", 0) or 0)

    user_doc = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if not user_doc:
        return 0

    current_credits = int(user_doc.get("reputation_credits", 0) or 0)
    next_credits = max(0, current_credits + int(delta))
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"id": walker_user_id},
        {
            "$set": {
                "reputation_credits": next_credits,
                "last_credit_update": now_iso,
                "updated_at": now_iso,
            }
        },
    )
    await db.reputation_credit_ledger.insert_one(
        {
            "id": str(uuid.uuid4()),
            "walker_user_id": walker_user_id,
            "delta": int(delta),
            "reason": reason,
            "event_key": event_key,
            "balance_after": next_credits,
            "metadata": metadata or {},
            "created_at": now_iso,
        }
    )
    return next_credits


async def _award_reputation_credits(
    *,
    walker_user_id: str,
    base_amount: int,
    reason: str,
    event_key: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    if not _is_feature_active("cr_system_enabled"):
        user_doc = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0, "reputation_credits": 1})
        return int((user_doc or {}).get("reputation_credits", 0) or 0)

    user_doc = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if not user_doc:
        return 0
    level = str(user_doc.get("verification_level") or VERIFICATION_LEVEL_NONE)
    gain_multiplier = _cr_gain_multiplier(level)
    context_gain_multiplier = await _marketplace_cr_gain_multiplier_for_walk(metadata.get("walk") if isinstance(metadata, dict) else {})
    adjusted_multiplier = gain_multiplier * context_gain_multiplier
    adjusted = max(1, int(round(base_amount * adjusted_multiplier)))
    return await _apply_reputation_credit_delta(
        walker_user_id=walker_user_id,
        delta=adjusted,
        reason=reason,
        event_key=event_key,
        metadata={
            "base_amount": base_amount,
            "gain_multiplier": gain_multiplier,
            "context_gain_multiplier": context_gain_multiplier,
            "effective_gain_multiplier": adjusted_multiplier,
            **(metadata or {}),
        },
    )


async def _evaluate_reputation_credit_gains_for_walk(walk: dict, trigger: str) -> None:
    walker_user_id = str(walk.get("walker_user_id") or "").strip()
    if not walker_user_id:
        return

    walk_id = str(walk.get("id") or "").strip()
    occurrence_status = str(walk.get("occurrence_status") or "")
    status = str(walk.get("status") or "")
    rating = int(walk.get("rating", 0) or 0)
    context_walk = {
        "pickup_city": walk.get("pickup_city"),
        "pickup_neighborhood": walk.get("pickup_neighborhood"),
    }

    if status == STATUS_FINISHED and occurrence_status in {"", OCC_RESOLVED}:
        await _award_reputation_credits(
            walker_user_id=walker_user_id,
            base_amount=1,
            reason="walk_clean",
            event_key=f"walk_clean:{walk_id}",
            metadata={"trigger": trigger, "walk": context_walk},
        )

    if rating >= 4:
        await _award_reputation_credits(
            walker_user_id=walker_user_id,
            base_amount=1,
            reason="positive_rating",
            event_key=f"positive_rating:{walk_id}",
            metadata={"rating": rating, "trigger": trigger, "walk": context_walk},
        )

        recent_positive = await db.walks.count_documents(
            {
                "walker_user_id": walker_user_id,
                "status": STATUS_FINISHED,
                "rating": {"$gte": 4},
            }
        )
        if recent_positive > 0 and recent_positive % 5 == 0:
            await _award_reputation_credits(
                walker_user_id=walker_user_id,
                base_amount=3,
                reason="positive_rating_streak_5",
                event_key=f"positive_streak5:{walker_user_id}:{recent_positive}",
                metadata={"trigger": trigger, "positive_count": recent_positive, "walk": context_walk},
            )

    completed_walks = await db.walks.count_documents({"walker_user_id": walker_user_id, "status": STATUS_FINISHED})
    if completed_walks > 0 and completed_walks % 5 == 0:
        await _award_reputation_credits(
            walker_user_id=walker_user_id,
            base_amount=2,
            reason="consistency_streak",
            event_key=f"consistency:{walker_user_id}:{completed_walks}",
            metadata={"trigger": trigger, "completed_walks": completed_walks, "walk": context_walk},
        )

    no_alert_window_start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent_alerts = await db.walks.count_documents(
        {
            "walker_user_id": walker_user_id,
            "updated_at": {"$gte": no_alert_window_start},
            "occurrence_status": {
                "$in": [
                    OCC_PENDING_ANALYSIS,
                    OCC_PENDING_ANALYSIS_REOPENED,
                    OCC_NO_SHOW_WALKER,
                    OCC_LATE_SEVERE,
                    KIT_OCCURRENCE_STATUS,
                ]
            },
        }
    )
    if recent_alerts == 0:
        today_key = _cr_today_key()
        await _award_reputation_credits(
            walker_user_id=walker_user_id,
            base_amount=2,
            reason="no_alerts_7d",
            event_key=f"no_alerts7:{walker_user_id}:{today_key}",
            metadata={"trigger": trigger, "walk": context_walk},
        )


async def _build_reputation_credits_response_for_user(user_doc: dict) -> WalkerReputationCreditsResponse:
    walker_user_id = str(user_doc.get("id") or "")
    today_key = _cr_today_key()
    daily_uses_count = (
        int(user_doc.get("cr_daily_uses_count", 0) or 0)
        if str(user_doc.get("cr_daily_uses_date") or "") == today_key
        else 0
    )
    level = str(user_doc.get("verification_level") or VERIFICATION_LEVEL_NONE)
    cr_enabled = _is_feature_active("cr_system_enabled")
    gain_multiplier = _cr_gain_multiplier(level) if cr_enabled else 0.0
    usage_cost_multiplier, usage_effect_multiplier = _cr_usage_multipliers(level, str(user_doc.get("walker_level") or ""))
    recent_ledger = await db.reputation_credit_ledger.find(
        {"walker_user_id": walker_user_id},
        {"_id": 0},
    ).sort("created_at", -1).limit(20).to_list(20)

    return WalkerReputationCreditsResponse(
        reputation_credits=int(user_doc.get("reputation_credits", 0) or 0),
        last_credit_update=user_doc.get("last_credit_update"),
        daily_uses_count=daily_uses_count,
        daily_uses_limit=(CR_DAILY_USES_LIMIT if cr_enabled else 0),
        daily_uses_remaining=(max(0, CR_DAILY_USES_LIMIT - daily_uses_count) if cr_enabled else 0),
        verification_level=level,
        gain_multiplier=gain_multiplier,
        premium_cost_multiplier=(usage_cost_multiplier if cr_enabled else 0.0),
        premium_effect_multiplier=(usage_effect_multiplier if cr_enabled else 0.0),
        matching_boost_until=user_doc.get("cr_matching_boost_until"),
        early_wave_until=user_doc.get("cr_early_wave_until"),
        visual_highlight_until=user_doc.get("cr_visual_highlight_until"),
        is_matching_boost_active=(cr_enabled and _is_cr_effect_active_until(user_doc.get("cr_matching_boost_until"))),
        is_early_wave_active=(cr_enabled and _is_cr_effect_active_until(user_doc.get("cr_early_wave_until"))),
        is_visual_highlight_active=(cr_enabled and _is_cr_effect_active_until(user_doc.get("cr_visual_highlight_until"))),
        recent_ledger=recent_ledger,
    )


async def _get_pet_transport_settings_dict() -> dict:
    settings = await db.pet_transport_settings.find_one({"id": "default"}, {"_id": 0})
    if not settings:
        settings = _default_pet_transport_settings_dict()
        await db.pet_transport_settings.insert_one(settings)
    normalized_enabled_for = _sanitize_pet_transport_enabled_for(settings.get("pet_transport_enabled_for"))
    if settings.get("pet_transport_enabled_for") != normalized_enabled_for:
        settings["pet_transport_enabled_for"] = normalized_enabled_for
        await db.pet_transport_settings.update_one(
            {"id": "default"},
            {"$set": {"pet_transport_enabled_for": normalized_enabled_for}},
            upsert=True,
        )
    return settings


async def _is_pet_transport_available_for_user(user: Optional[dict]) -> bool:
    if not _is_feature_active("pet_transport"):
        return False
    settings = await _get_pet_transport_settings_dict()
    enabled_for = _sanitize_pet_transport_enabled_for(settings.get("pet_transport_enabled_for"))
    if "all" in enabled_for:
        return True
    if not user:
        return False

    user_id = str(user.get("id") or "").strip().lower()
    email = str(user.get("email") or "").strip().lower()
    if user_id and f"user:{user_id}" in enabled_for:
        return True
    if email and f"email:{email}" in enabled_for:
        return True
    if "test_users" in enabled_for and _is_test_user_account(user):
        return True
    return False


def _is_referral_program_enabled_for_role(settings: dict, role: str) -> bool:
    if not bool(settings.get("program_enabled", False)):
        return False
    if role == "cliente":
        return bool(settings.get("client_referral_enabled", False))
    if role == "passeador":
        return bool(settings.get("walker_referral_enabled", False))
    return False


def _extract_request_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = str(request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    return str(request.client.host if request.client else "unknown").strip()


def _extract_request_device_id(request: Request) -> str:
    return str(request.headers.get("x-device-id") or "").strip()[:120]


def _generate_referral_code(prefix: str) -> str:
    random_part = uuid.uuid4().hex[:4].upper()
    return f"{prefix}-{random_part}"


def _to_referral_record_response(row: dict) -> ReferralRecordResponse:
    normalized = dict(row)
    normalized.setdefault("status", REFERRAL_STATUS_CREATED)
    normalized.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    normalized.setdefault("unlock_condition", {})
    normalized.setdefault("condition_progress", {})
    normalized.setdefault("fraud_flags", [])
    normalized.setdefault("audit_log", [])
    normalized.setdefault("reward_amount", 0.0)
    return ReferralRecordResponse(**normalized)


def _append_referral_audit_event(referral_row: dict, event_type: str, note: str, actor_user_id: Optional[str] = None):
    events = list(referral_row.get("audit_log") or [])
    events.append(
        {
            "event_type": event_type,
            "note": note,
            "actor_user_id": actor_user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    referral_row["audit_log"] = events[-60:]


async def _get_or_create_referral_code_for_user(user: dict, settings: dict) -> dict:
    user_id = str(user.get("id") or "")
    role = str(user.get("role") or "")
    referral_type = "cliente_para_cliente" if role == "cliente" else "passeador_para_passeador"
    code_row = await db.referral_codes.find_one({"owner_user_id": user_id, "referral_type": referral_type}, {"_id": 0})
    if code_row:
        return code_row

    prefix = "PET" if role == "cliente" else "DOG"
    code_value = _generate_referral_code(prefix)
    while await db.referral_codes.find_one({"code": code_value}, {"_id": 0}):
        code_value = _generate_referral_code(prefix)

    code_row = {
        "id": str(uuid.uuid4()),
        "code": code_value,
        "owner_user_id": user_id,
        "owner_role": role,
        "referral_type": referral_type,
        "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.referral_codes.insert_one(code_row)
    return code_row


async def _create_private_referral_coupon(*, user_id: str, amount: float, validity_days: int, reason: str) -> dict:
    now_dt = datetime.now(timezone.utc)
    valid_until = now_dt + timedelta(days=max(1, validity_days))
    code = _generate_referral_code("BONUS")
    while await db.coupons.find_one({"code": code}, {"_id": 0}):
        code = _generate_referral_code("BONUS")

    coupon_doc = {
        "id": str(uuid.uuid4()),
        "code": code,
        "title": f"Benefício de indicação - {reason}",
        "description": reason,
        "discount_percent": 0.0,
        "discount_fixed": float(amount),
        "valid_from": now_dt.isoformat(),
        "valid_until": valid_until.isoformat(),
        "max_global_uses": 1,
        "max_uses_per_user": 1,
        "used_count": 0,
        "is_active": True,
        "legacy_mode": False,
        "created_at": now_dt.isoformat(),
        "updated_at": now_dt.isoformat(),
        "applicable_walk_types": COUPON_WALK_TYPES.copy(),
        "target_user_id": user_id,
    }
    await db.coupons.insert_one(coupon_doc)
    return coupon_doc


def _walk_involves_user(walk: dict, user_id: str) -> bool:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return False
    if str(walk.get("client_user_id") or "").strip() == normalized_user_id:
        return True
    participant_ids = _walk_participant_user_ids(walk)
    return normalized_user_id in participant_ids


async def _evaluate_referrals_for_user(user_id: str):
    settings = await _get_referral_program_settings_dict()
    if not bool(settings.get("program_enabled", False)):
        return

    referrals = await db.referrals.find(
        {
            "referred_user_id": user_id,
            "status": {
                "$in": [
                    REFERRAL_STATUS_PENDING,
                    REFERRAL_STATUS_IN_PROGRESS,
                    REFERRAL_STATUS_ELIGIBLE,
                ]
            },
        },
        {"_id": 0},
    ).to_list(500)
    if not referrals:
        return

    walks = await db.walks.find({}, {"_id": 0}).to_list(6000)

    for referral in referrals:
        referral_type = str(referral.get("referral_type") or "")
        updated = dict(referral)
        now_iso = datetime.now(timezone.utc).isoformat()

        if referral_type == "cliente_para_cliente":
            if not bool(settings.get("client_referral_enabled", False)):
                continue
            client_rules = ClientReferralRules(**dict(settings.get("client_rules") or {}))
            paid_walks = sum(
                1
                for walk in walks
                if _walk_involves_user(walk, user_id)
                and str(walk.get("payment_status") or "") == "Pago"
            )

            updated["condition_progress"] = {
                "paid_walks": paid_walks,
                "required_paid_walks": client_rules.min_paid_walks_for_referrer_bonus,
            }

            if paid_walks >= 1 and not updated.get("activated_at"):
                updated["activated_at"] = now_iso
                updated["status"] = REFERRAL_STATUS_IN_PROGRESS
                _append_referral_audit_event(updated, "activated", "Indicado realizou o primeiro passeio pago.")

            if paid_walks >= client_rules.min_paid_walks_for_referrer_bonus:
                updated["status"] = REFERRAL_STATUS_ELIGIBLE

            if updated.get("status") == REFERRAL_STATUS_ELIGIBLE and not updated.get("reward_released_at"):
                coupon = await _create_private_referral_coupon(
                    user_id=str(updated.get("referrer_user_id") or ""),
                    amount=client_rules.referrer_coupon_credit_amount,
                    validity_days=client_rules.benefit_validity_days,
                    reason="Bônus de indicação cliente",
                )
                updated["reward_released_at"] = now_iso
                updated["status"] = REFERRAL_STATUS_REWARDED
                updated["reward_amount"] = client_rules.referrer_coupon_credit_amount
                updated["reward_reference_id"] = coupon.get("id")
                _append_referral_audit_event(updated, "reward_released", "Cupom do indicador liberado.")

        if referral_type == "passeador_para_passeador":
            if not bool(settings.get("walker_referral_enabled", False)):
                continue
            walker_rules = WalkerReferralRules(**dict(settings.get("walker_rules") or {}))
            referred_user = await db.users.find_one({"id": user_id}, {"_id": 0}) or {}
            metrics = dict(referred_user.get("reputation_metrics") or {})
            completed_walks = int(metrics.get("completed_walks") or 0)
            rating_avg = _coerce_float(metrics.get("rating_avg"), 0.0)
            no_show_rate = _coerce_float(metrics.get("no_show_rate"), 0.0)

            updated["condition_progress"] = {
                "completed_walks": completed_walks,
                "rating_avg": rating_avg,
                "no_show_rate": no_show_rate,
                "required_completed_walks": walker_rules.min_completed_walks,
                "required_rating": walker_rules.min_rating_required,
                "max_no_show_rate": walker_rules.max_no_show_rate,
            }

            if completed_walks > 0 and not updated.get("activated_at"):
                updated["activated_at"] = now_iso
                updated["status"] = REFERRAL_STATUS_IN_PROGRESS

            created_at_dt = _parse_iso_datetime(updated.get("created_at")) or datetime.now(timezone.utc)
            still_in_window = datetime.now(timezone.utc) <= created_at_dt + timedelta(days=walker_rules.eligibility_window_days)
            if (
                still_in_window
                and completed_walks >= walker_rules.min_completed_walks
                and rating_avg >= walker_rules.min_rating_required
                and no_show_rate <= walker_rules.max_no_show_rate
            ):
                updated["status"] = REFERRAL_STATUS_ELIGIBLE

            if updated.get("status") == REFERRAL_STATUS_ELIGIBLE and not updated.get("reward_released_at"):
                bonus_key = f"referral-walker-{updated.get('id')}"
                await _upsert_bonus_payment(
                    bonus_key=bonus_key,
                    walker_user_id=str(updated.get("referrer_user_id") or ""),
                    bonus_type="referral_walker_bonus",
                    amount=walker_rules.fixed_bonus_amount,
                    status="ready",
                    description="Bônus por indicação de passeador validada",
                )
                updated["reward_released_at"] = now_iso
                updated["status"] = REFERRAL_STATUS_REWARDED
                updated["reward_amount"] = walker_rules.fixed_bonus_amount
                updated["reward_reference_id"] = bonus_key
                _append_referral_audit_event(updated, "reward_released", "Bônus financeiro liberado ao indicador.")

        updated["updated_at"] = now_iso
        await db.referrals.update_one({"id": updated.get("id")}, {"$set": updated})

def _week_bounds(reference: datetime) -> tuple[datetime, datetime]:
    start = reference.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=reference.weekday())
    end = start + timedelta(days=7)
    return start, end


def _walk_datetime_from_doc(walk: dict) -> Optional[datetime]:
    walk_dt = _parse_iso_datetime(walk.get("walk_datetime_iso"))
    if walk_dt:
        return walk_dt
    walk_date = str(walk.get("walk_date") or "").strip()
    walk_time = _normalize_clock(walk.get("walk_time"), "00:00")
    if not walk_date:
        return None
    try:
        return datetime.strptime(f"{walk_date} {walk_time}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_critical_hour(walk_time: str, windows: List[dict]) -> bool:
    minutes = _clock_to_minutes(_normalize_clock(walk_time, "00:00"))
    for window in windows:
        start_minutes = _clock_to_minutes(_normalize_clock(window.get("start"), "00:00"))
        end_minutes = _clock_to_minutes(_normalize_clock(window.get("end"), "23:59"))
        if start_minutes <= minutes <= end_minutes:
            return True
    return False


def _consistency_streak_days(walker_walks: List[dict], reference: datetime) -> int:
    daily: Dict[str, dict] = {}
    for walk in walker_walks:
        walk_dt = _walk_datetime_from_doc(walk)
        if not walk_dt:
            continue
        key = walk_dt.date().isoformat()
        status = str(walk.get("status") or "")
        occ = _derive_occurrence_status(walk)
        row = daily.setdefault(key, {"has_walk": False, "has_issue": False})
        if status in ACTIVE_WALK_STATUSES or status == STATUS_FINISHED:
            row["has_walk"] = True
        if status == STATUS_NO_SHOW_WALKER or occ == OCC_LATE_SEVERE:
            row["has_issue"] = True

    streak = 0
    cursor = reference.date()
    for _ in range(30):
        key = cursor.isoformat()
        day_row = daily.get(key)
        if not day_row or not day_row.get("has_walk") or day_row.get("has_issue"):
            break
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _walk_matches_client_user(walk: dict, client_user: dict) -> bool:
    client_user_id = str(client_user.get("id") or "").strip()
    walk_client_user_id = str(walk.get("client_user_id") or "").strip()
    if client_user_id and walk_client_user_id == client_user_id:
        return True

    participant_ids = [
        str(item).strip()
        for item in list(walk.get("participant_user_ids") or [])
        if str(item).strip()
    ]
    if client_user_id and client_user_id in participant_ids:
        return True

    client_name = str(client_user.get("full_name") or "").strip().lower()
    walk_client_name = str(walk.get("client_name") or "").strip().lower()
    return bool(client_name and walk_client_name and walk_client_name == client_name)


ROUTINE_WEEKDAY_TO_INDEX: Dict[str, int] = {
    "seg": 0,
    "ter": 1,
    "qua": 2,
    "qui": 3,
    "sex": 4,
    "sab": 5,
    "dom": 6,
}
ROUTINE_INDEX_TO_WEEKDAY: Dict[int, RoutineWeekday] = {index: day for day, index in ROUTINE_WEEKDAY_TO_INDEX.items()}


def _normalize_routine_days(days: List[str]) -> List[RoutineWeekday]:
    ordered: List[RoutineWeekday] = []
    for day in days:
        normalized = str(day or "").strip().lower()
        if normalized not in ROUTINE_WEEKDAY_TO_INDEX:
            continue
        typed_day = cast(RoutineWeekday, normalized)
        if typed_day not in ordered:
            ordered.append(typed_day)
    return ordered


def _validate_routine_config(*, frequencia_semanal: int, dias_preferenciais: List[str], horario_preferencial: str, duracao_passeio: int):
    if frequencia_semanal < 1 or frequencia_semanal > 5:
        raise HTTPException(status_code=400, detail="Frequência semanal deve ser entre 1 e 5")

    normalized_days = _normalize_routine_days(dias_preferenciais)
    if not normalized_days:
        raise HTTPException(status_code=400, detail="Informe ao menos um dia preferencial")

    if len(normalized_days) < frequencia_semanal:
        raise HTTPException(
            status_code=400,
            detail="A quantidade de dias preferenciais deve ser maior ou igual à frequência semanal",
        )

    if _normalize_clock(horario_preferencial, "") == "":
        raise HTTPException(status_code=400, detail="Horário preferencial inválido")

    if int(duracao_passeio) not in {30, 45, 60}:
        raise HTTPException(status_code=400, detail="Duração deve ser 30, 45 ou 60 minutos")


def _is_valid_routine_walk(walk: dict) -> bool:
    status = str(walk.get("status") or "")
    payment_status = str(walk.get("payment_status") or "")
    return status == STATUS_FINISHED or payment_status == "Pago"


def _walk_matches_routine(walk: dict, routine: dict) -> bool:
    if not _is_valid_routine_walk(walk):
        return False

    pet_id = str(routine.get("pet_id") or "").strip()
    routine_user_id = str(routine.get("user_id") or "").strip()
    if not pet_id or not routine_user_id:
        return False

    walk_pet_ids = [str(item).strip() for item in list(walk.get("pet_ids") or []) if str(item).strip()]
    walk_single_pet_id = str(walk.get("pet_id") or "").strip()
    if pet_id not in walk_pet_ids and walk_single_pet_id != pet_id:
        return False

    walk_client_user_id = str(walk.get("client_user_id") or "").strip()
    if walk_client_user_id and walk_client_user_id == routine_user_id:
        return True

    participant_ids = [str(item).strip() for item in list(walk.get("participant_user_ids") or []) if str(item).strip()]
    if routine_user_id in participant_ids:
        return True

    routine_user_name = str(routine.get("user_name") or "").strip().lower()
    walk_client_name = str(walk.get("client_name") or "").strip().lower()
    return bool(routine_user_name and walk_client_name and routine_user_name == walk_client_name)


def _routine_week_start(reference: datetime) -> datetime:
    start, _ = _week_bounds(reference)
    return start


def _match_expected_dates_with_tolerance(
    *,
    expected_dates: List[datetime],
    actual_dates: List[datetime],
    tolerance_days: int = 1,
) -> List[datetime]:
    if not expected_dates or not actual_dates:
        return []

    used_actual_indices: set[int] = set()
    matched: List[datetime] = []

    for expected_dt in expected_dates:
        best_index: Optional[int] = None
        best_distance: Optional[int] = None
        for index, actual_dt in enumerate(actual_dates):
            if index in used_actual_indices:
                continue
            distance = abs((actual_dt.date() - expected_dt.date()).days)
            if distance > tolerance_days:
                continue
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_index = index
        if best_index is not None:
            used_actual_indices.add(best_index)
            matched.append(actual_dates[best_index])

    return matched


def _pet_routine_badges(*, total_ciclos_cumpridos: int) -> List[str]:
    badges: List[str] = []
    if total_ciclos_cumpridos >= 1:
        badges.append("Início Ativo")
    if total_ciclos_cumpridos >= 3:
        badges.append("Pet em Movimento")
    if total_ciclos_cumpridos >= 5:
        badges.append("Rotina Saudável")
    if total_ciclos_cumpridos >= 10:
        badges.append("Alto Nível de Atividade")
    return badges[:4]


def _pet_routine_message(*, pet_name: str, current_streak: int, best_streak: int, planned_this_week: int, completed_this_week: int) -> str:
    display_name = pet_name.strip() or "seu pet"
    if current_streak >= max(2, best_streak):
        return f"Você está indo muito bem com a rotina do {display_name}."
    if current_streak > 0:
        return f"Seu pet está criando uma rotina saudável. Sequência atual: {current_streak} ciclos."
    if planned_this_week > 0 and completed_this_week < planned_this_week:
        return "Falta pouco para bater seu recorde. Continue assim para manter a sequência."
    return "Seu pet vai adorar criar uma rotina saudável com passeios frequentes."


def _routine_config_from_doc(routine: dict) -> dict:
    return {
        "frequencia_semanal": int(routine.get("frequencia_semanal") or 1),
        "dias_preferenciais": _normalize_routine_days(list(routine.get("dias_preferenciais") or [])),
        "horario_preferencial": _normalize_clock(routine.get("horario_preferencial"), "09:00"),
        "duracao_passeio": int(routine.get("duracao_passeio") or 30),
        "is_active": bool(routine.get("is_active", True)),
    }


def _current_routine_segment_for_week(routine: dict, week_start: datetime) -> Optional[dict]:
    history = list(routine.get("config_history") or [])
    if not history:
        return _routine_config_from_doc(routine)

    for segment in history:
        from_dt = _parse_iso_datetime(segment.get("effective_from"))
        to_dt = _parse_iso_datetime(segment.get("effective_to"))
        if not from_dt:
            continue
        if from_dt <= week_start and (not to_dt or week_start < to_dt):
            return {
                "frequencia_semanal": int(segment.get("frequencia_semanal") or 1),
                "dias_preferenciais": _normalize_routine_days(list(segment.get("dias_preferenciais") or [])),
                "horario_preferencial": _normalize_clock(segment.get("horario_preferencial"), "09:00"),
                "duracao_passeio": int(segment.get("duracao_passeio") or 30),
                "is_active": bool(segment.get("is_active", True)),
            }
    return None


def _build_pet_routine_suggestions(*, routine: dict, walk_datetimes: List[datetime], days_ahead: int = 7) -> List[dict]:
    if not bool(routine.get("is_active", True)):
        return []

    preferred_days = _normalize_routine_days(list(routine.get("dias_preferenciais") or []))
    if not preferred_days:
        return []

    preferred_time = _normalize_clock(routine.get("horario_preferencial"), "09:00")
    duration = int(routine.get("duracao_passeio") or 30)

    walk_dates = {walk_dt.date().isoformat() for walk_dt in walk_datetimes}
    now_dt = datetime.now(timezone.utc)
    suggestions: List[dict] = []

    for offset in range(max(1, days_ahead)):
        candidate_dt = now_dt + timedelta(days=offset)
        weekday_key = ROUTINE_INDEX_TO_WEEKDAY.get(candidate_dt.weekday())
        if not weekday_key or weekday_key not in preferred_days:
            continue
        candidate_date = candidate_dt.date().isoformat()
        if candidate_date in walk_dates:
            continue
        suggestions.append(
            {
                "date": candidate_date,
                "weekday": weekday_key,
                "time": preferred_time,
                "duration_minutes": cast(Literal[30, 45, 60], duration if duration in {30, 45, 60} else 30),
                "label": f"{weekday_key.upper()} às {preferred_time}",
            }
        )

    return suggestions[:7]


def _compute_pet_routine_progress_from_walks(routine: dict, walks: List[dict], pet_name: str) -> dict:
    now_dt = datetime.now(timezone.utc)
    week_start, week_end = _week_bounds(now_dt)
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    routine_created_at = _parse_iso_datetime(routine.get("created_at")) or now_dt
    first_cycle_start = _routine_week_start(routine_created_at)
    current_cycle_start = _routine_week_start(now_dt)

    valid_walk_datetimes = sorted(
        [
            walk_dt
            for walk in walks
            if _walk_matches_routine(walk, routine)
            if (walk_dt := _walk_datetime_from_doc(walk)) is not None
        ]
    )

    total_finished_walks = len(valid_walk_datetimes)
    finished_walks_week = sum(1 for walk_dt in valid_walk_datetimes if week_start <= walk_dt < week_end)
    finished_walks_month = sum(1 for walk_dt in valid_walk_datetimes if month_start <= walk_dt <= now_dt)

    cycle_results: List[bool] = []
    cycle_completed_total = 0
    cycle_missed_total = 0
    planned_this_week = 0
    completed_this_week = 0
    matched_walks_for_routine: List[datetime] = []

    cursor = first_cycle_start
    while cursor <= current_cycle_start:
        segment = _current_routine_segment_for_week(routine, cursor)
        if not segment:
            cursor += timedelta(days=7)
            continue

        if not bool(segment.get("is_active", True)):
            cursor += timedelta(days=7)
            continue

        normalized_days = _normalize_routine_days(list(segment.get("dias_preferenciais") or []))
        if not normalized_days:
            cursor += timedelta(days=7)
            continue

        frequency = int(segment.get("frequencia_semanal") or 1)
        expected_days = normalized_days[: max(1, min(frequency, len(normalized_days)))]
        expected_datetimes = [cursor + timedelta(days=ROUTINE_WEEKDAY_TO_INDEX[day]) for day in expected_days]

        cycle_end = cursor + timedelta(days=7)
        cycle_actual = [walk_dt for walk_dt in valid_walk_datetimes if cursor <= walk_dt < cycle_end]
        matched_walks = _match_expected_dates_with_tolerance(
            expected_dates=expected_datetimes,
            actual_dates=cycle_actual,
            tolerance_days=1,
        )

        matched_count = len(matched_walks)
        cycle_completed = matched_count >= len(expected_datetimes)
        cycle_results.append(cycle_completed)
        matched_walks_for_routine.extend(matched_walks)

        if cycle_completed:
            cycle_completed_total += 1
        else:
            cycle_missed_total += 1

        if cursor == current_cycle_start:
            planned_this_week = len(expected_datetimes)
            completed_this_week = matched_count

        cursor += timedelta(days=7)

    current_streak = 0
    for cycle_ok in reversed(cycle_results):
        if cycle_ok:
            current_streak += 1
        else:
            break

    best_streak = 0
    running = 0
    for cycle_ok in cycle_results:
        if cycle_ok:
            running += 1
            best_streak = max(best_streak, running)
        else:
            running = 0

    total_cycles = cycle_completed_total + cycle_missed_total
    completion_rate = round((cycle_completed_total / total_cycles) * 100.0, 2) if total_cycles else 0.0

    suggestions = _build_pet_routine_suggestions(routine=routine, walk_datetimes=valid_walk_datetimes, days_ahead=7)
    next_expected_iso: Optional[str] = None
    if suggestions:
        next_expected_iso = f"{suggestions[0]['date']}T{suggestions[0]['time']}:00+00:00"

    last_valid_walk = max(matched_walks_for_routine).isoformat() if matched_walks_for_routine else None
    encouragement_message = _pet_routine_message(
        pet_name=pet_name,
        current_streak=current_streak,
        best_streak=best_streak,
        planned_this_week=planned_this_week,
        completed_this_week=completed_this_week,
    )

    return {
        "id": f"pet-routine-progress-{routine.get('id')}",
        "routine_id": str(routine.get("id") or ""),
        "pet_id": str(routine.get("pet_id") or ""),
        "pet_name": pet_name,
        "user_id": str(routine.get("user_id") or ""),
        "frequencia_semanal": int(routine.get("frequencia_semanal") or 1),
        "dias_preferenciais": _normalize_routine_days(list(routine.get("dias_preferenciais") or [])),
        "horario_preferencial": _normalize_clock(routine.get("horario_preferencial"), "09:00"),
        "duracao_passeio": int(routine.get("duracao_passeio") or 30),
        "is_active": bool(routine.get("is_active", True)),
        "current_streak": int(current_streak),
        "best_streak": int(best_streak),
        "total_ciclos_cumpridos": int(cycle_completed_total),
        "total_ciclos_perdidos": int(cycle_missed_total),
        "total_passeios_realizados_no_periodo": int(total_finished_walks),
        "taxa_cumprimento_rotina": completion_rate,
        "ultimo_passeio_valido_em_rotina": last_valid_walk,
        "proximo_passeio_esperado": next_expected_iso,
        "planned_this_week": int(planned_this_week),
        "completed_this_week": int(min(planned_this_week, completed_this_week)),
        "week_progress_label": f"Você cumpriu {min(planned_this_week, completed_this_week)} de {planned_this_week} passeios planejados nesta semana",
        "streak_days": int(current_streak),
        "best_streak_days": int(best_streak),
        "finished_walks_total": int(total_finished_walks),
        "finished_walks_week": int(finished_walks_week),
        "finished_walks_month": int(finished_walks_month),
        "simple_badges": _pet_routine_badges(total_ciclos_cumpridos=cycle_completed_total),
        "encouragement_message": encouragement_message,
        "last_finished_walk_at": last_valid_walk,
        "suggestions": suggestions,
        "updated_at": now_dt.isoformat(),
    }


def _build_routine_config_segment(*, routine_config: dict, action: str, effective_from: str) -> dict:
    return {
        "effective_from": effective_from,
        "effective_to": None,
        "action": action,
        "frequencia_semanal": int(routine_config.get("frequencia_semanal") or 1),
        "dias_preferenciais": _normalize_routine_days(list(routine_config.get("dias_preferenciais") or [])),
        "horario_preferencial": _normalize_clock(routine_config.get("horario_preferencial"), "09:00"),
        "duracao_passeio": int(routine_config.get("duracao_passeio") or 30),
        "is_active": bool(routine_config.get("is_active", True)),
    }


def _serialize_routine_response(routine: dict, pet_name: str) -> dict:
    return {
        "id": str(routine.get("id") or ""),
        "user_id": str(routine.get("user_id") or ""),
        "pet_id": str(routine.get("pet_id") or ""),
        "pet_name": pet_name,
        "frequencia_semanal": int(routine.get("frequencia_semanal") or 1),
        "dias_preferenciais": _normalize_routine_days(list(routine.get("dias_preferenciais") or [])),
        "horario_preferencial": _normalize_clock(routine.get("horario_preferencial"), "09:00"),
        "duracao_passeio": int(routine.get("duracao_passeio") or 30),
        "is_active": bool(routine.get("is_active", True)),
        "created_at": str(routine.get("created_at") or datetime.now(timezone.utc).isoformat()),
        "updated_at": str(routine.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    }


async def _upsert_pet_routine_progress_for_user(client_user: dict, pet_id: Optional[str] = None) -> dict:
    if not _is_feature_active("pet_routine"):
        now_iso = datetime.now(timezone.utc).isoformat()
        return {
            "id": "pet-routine-disabled",
            "user_id": str(client_user.get("id") or ""),
            "updated_at": now_iso,
            "encouragement_message": "Rotina do Pet está desativada no momento.",
        }

    client_user_id = str(client_user.get("id") or "").strip()
    if not client_user_id:
        raise HTTPException(status_code=400, detail="Usuário inválido para rotina do pet")

    routines_query: dict = {"user_id": client_user_id}
    normalized_pet_id = str(pet_id or "").strip()
    if normalized_pet_id:
        routines_query["pet_id"] = normalized_pet_id

    routines = await db.pet_routines.find(routines_query, {"_id": 0}).sort("updated_at", -1).to_list(100)
    if not routines:
        now_iso = datetime.now(timezone.utc).isoformat()
        return {
            "id": f"pet-routine-progress-empty-{client_user_id}",
            "user_id": client_user_id,
            "updated_at": now_iso,
            "encouragement_message": "Configure a rotina do seu pet para começar sua sequência.",
            "simple_badges": [],
            "dias_preferenciais": [],
            "suggestions": [],
            "week_progress_label": "Você cumpriu 0 de 0 passeios planejados nesta semana",
        }

    walks = await db.walks.find({}, {"_id": 0}).to_list(6000)
    progress_rows: List[dict] = []

    for routine in routines:
        pet = await db.pets.find_one({"id": str(routine.get("pet_id") or "")}, {"_id": 0, "pet_name": 1})
        pet_name = str((pet or {}).get("pet_name") or "Pet")
        progress = _compute_pet_routine_progress_from_walks(routine, walks, pet_name)
        await db.pet_routine_progress.update_one(
            {"routine_id": str(routine.get("id") or "")},
            {"$set": progress},
            upsert=True,
        )
        progress_rows.append(progress)

    progress_rows.sort(
        key=lambda item: (
            0 if item.get("is_active", False) else 1,
            -int(item.get("current_streak") or 0),
            -_coerce_float(item.get("taxa_cumprimento_rotina"), 0.0),
        )
    )
    return progress_rows[0]


async def _refresh_pet_routine_progress_by_user_id(client_user_id: str, pet_id: Optional[str] = None) -> Optional[dict]:
    normalized_user_id = str(client_user_id or "").strip()
    if not normalized_user_id or not _is_feature_active("pet_routine"):
        return None
    client_user = await db.users.find_one({"id": normalized_user_id, "role": "cliente"}, {"_id": 0})
    if not client_user:
        return None
    return await _upsert_pet_routine_progress_for_user(client_user, pet_id=pet_id)


async def _refresh_pet_routine_progress_from_walk(walk: dict) -> Optional[dict]:
    if not _is_feature_active("pet_routine"):
        return None

    client_user_id = str(walk.get("client_user_id") or "").strip()
    pet_ids = [str(item).strip() for item in list(walk.get("pet_ids") or []) if str(item).strip()]
    if client_user_id:
        refreshed: Optional[dict] = None
        if pet_ids:
            for pet_id in pet_ids:
                refreshed = await _refresh_pet_routine_progress_by_user_id(client_user_id, pet_id=pet_id)
        else:
            refreshed = await _refresh_pet_routine_progress_by_user_id(client_user_id)
        return refreshed

    client_name = str(walk.get("client_name") or "").strip()
    if not client_name:
        return None
    client_user = await db.users.find_one({"full_name": client_name, "role": "cliente"}, {"_id": 0})
    if not client_user:
        return None
    return await _upsert_pet_routine_progress_for_user(client_user, pet_id=pet_ids[0] if pet_ids else None)


async def _get_pet_routine_or_404(routine_id: str, user_id: str) -> dict:
    routine = await db.pet_routines.find_one({"id": routine_id, "user_id": user_id}, {"_id": 0})
    if not routine:
        raise HTTPException(status_code=404, detail="Rotina do pet não encontrada")
    return routine


def _apply_routine_config_update(*, routine: dict, new_config: dict, action: str) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    history = list(routine.get("config_history") or [])
    if history:
        history[-1]["effective_to"] = now_iso

    history.append(_build_routine_config_segment(routine_config=new_config, action=action, effective_from=now_iso))
    history = history[-120:]

    routine.update(
        {
            "frequencia_semanal": int(new_config.get("frequencia_semanal") or 1),
            "dias_preferenciais": _normalize_routine_days(list(new_config.get("dias_preferenciais") or [])),
            "horario_preferencial": _normalize_clock(new_config.get("horario_preferencial"), "09:00"),
            "duracao_passeio": int(new_config.get("duracao_passeio") or 30),
            "is_active": bool(new_config.get("is_active", True)),
            "config_history": history,
            "updated_at": now_iso,
        }
    )
    return routine


async def _upsert_bonus_payment(
    *,
    walker_user_id: str,
    bonus_key: str,
    bonus_type: str,
    amount: float,
    description: str,
    walk_id: Optional[str] = None,
    week_key: Optional[str] = None,
):
    now_iso = datetime.now(timezone.utc).isoformat()
    existing = await db.walker_bonus_payments.find_one({"bonus_key": bonus_key}, {"_id": 0, "id": 1})
    if existing:
        return
    await db.walker_bonus_payments.insert_one(
        {
            "id": str(uuid.uuid4()),
            "bonus_key": bonus_key,
            "walker_user_id": walker_user_id,
            "bonus_type": bonus_type,
            "amount": round(max(0.0, amount), 2),
            "status": "active",
            "description": description,
            "walk_id": walk_id,
            "week_key": week_key,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )


async def _invalidate_walker_week_bonus_entries(walker_user_id: str, week_key: str, reason: str):
    await db.walker_bonus_payments.update_many(
        {"walker_user_id": walker_user_id, "week_key": week_key, "status": "active"},
        {
            "$set": {
                "status": "invalidated",
                "invalidated_reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )


async def _process_walker_incentive_events(
    *,
    walker_user: dict,
    walker_walks: List[dict],
    metrics: dict,
    current_walk: Optional[dict],
):
    walker_user_id = str(walker_user.get("id") or "")
    if not walker_user_id or not current_walk:
        return

    if not _is_feature_active("habit_incentive"):
        return

    settings = await _get_incentive_settings_dict()
    if not bool(settings.get("enabled", True)):
        return

    if bool(walker_user.get("is_suspected", False)) or bool(current_walk.get("suspected_disintermediation", False)):
        return

    walk_dt = _walk_datetime_from_doc(current_walk) or datetime.now(timezone.utc)
    week_start, week_end = _week_bounds(walk_dt)
    week_key = week_start.date().isoformat()

    walk_status = str(current_walk.get("status") or "")
    if walk_status == STATUS_NO_SHOW_WALKER:
        await _invalidate_walker_week_bonus_entries(walker_user_id, week_key, "No-show do passeador no período")
        return

    if walk_status != STATUS_FINISHED:
        return

    week_walks = [item for item in walker_walks if (dt := _walk_datetime_from_doc(item)) and week_start <= dt < week_end and item.get("status") == STATUS_FINISHED]
    week_completed = len(week_walks)

    weighted_score = _coerce_float(metrics.get("rating_weighted_avg"), 0.0)
    if weighted_score >= _coerce_float(settings.get("quality_bonus_min_weighted"), DEFAULT_QUALITY_BONUS_MIN_WEIGHTED) and week_completed >= int(settings.get("quality_bonus_min_walks") or DEFAULT_QUALITY_BONUS_MIN_WALKS):
        quality_bonus_amount = round(_coerce_float(current_walk.get("walker_payout_amount"), 0.0) * (_coerce_float(settings.get("quality_bonus_percent"), DEFAULT_QUALITY_BONUS_PERCENT) / 100.0), 2)
        await _upsert_bonus_payment(
            walker_user_id=walker_user_id,
            bonus_key=f"quality:{str(current_walk.get('id') or '')}",
            bonus_type="quality_performance",
            amount=quality_bonus_amount,
            description="Bônus de qualidade por nota ponderada no período",
            walk_id=str(current_walk.get("id") or ""),
            week_key=week_key,
        )

    if _is_critical_hour(str(current_walk.get("walk_time") or ""), settings.get("critical_windows") or DEFAULT_CRITICAL_WINDOWS):
        await _upsert_bonus_payment(
            walker_user_id=walker_user_id,
            bonus_key=f"critical:{str(current_walk.get('id') or '')}",
            bonus_type="critical_hour",
            amount=_coerce_float(settings.get("critical_hour_bonus_amount"), DEFAULT_CRITICAL_HOUR_BONUS_AMOUNT),
            description="Bônus por passeio em horário de alta demanda",
            walk_id=str(current_walk.get("id") or ""),
            week_key=week_key,
        )

    streak_days = _consistency_streak_days(walker_walks, walk_dt)
    if streak_days >= int(settings.get("consistency_days_required") or DEFAULT_CONSISTENCY_DAYS_REQUIRED):
        await _upsert_bonus_payment(
            walker_user_id=walker_user_id,
            bonus_key=f"consistency:{week_key}",
            bonus_type="consistency_week",
            amount=_coerce_float(settings.get("consistency_bonus_amount"), DEFAULT_CONSISTENCY_BONUS_AMOUNT),
            description="Bônus de consistência semanal",
            week_key=week_key,
        )

    tiers = sorted(settings.get("volume_bonus_tiers") or DEFAULT_VOLUME_BONUS_TIERS, key=lambda row: int(row.get("target_walks", 0)))
    reached_tier = None
    for tier in tiers:
        if week_completed >= int(tier.get("target_walks", 0)):
            reached_tier = tier
    if reached_tier:
        target = int(reached_tier.get("target_walks", 0))
        await _upsert_bonus_payment(
            walker_user_id=walker_user_id,
            bonus_key=f"volume:{week_key}:{target}",
            bonus_type="volume_week",
            amount=_coerce_float(reached_tier.get("amount"), 0.0),
            description=f"Bônus de volume semanal (meta {target} passeios)",
            week_key=week_key,
        )


def _to_coupon_response(coupon: dict) -> CouponResponse:
    normalized = dict(coupon)
    normalized["code"] = _normalize_coupon_code(normalized.get("code", ""))
    normalized["discount_percent"] = min(100.0, max(0.0, _coerce_float(normalized.get("discount_percent", 0.0), 0.0)))
    normalized["discount_fixed"] = max(0.0, _coerce_float(normalized.get("discount_fixed", 0.0), 0.0))
    normalized["max_global_uses"] = int(normalized.get("max_global_uses") or 0)
    normalized["max_uses_per_user"] = max(1, int(normalized.get("max_uses_per_user") or 1))
    normalized["used_count"] = max(0, int(normalized.get("used_count") or 0))
    normalized["applicable_walk_types"] = _normalize_coupon_walk_types(normalized.get("applicable_walk_types"))
    normalized["is_active"] = bool(normalized.get("is_active", True))
    normalized["legacy_mode"] = (
        normalized["max_global_uses"] <= 0
        or not str(normalized.get("valid_from") or "").strip()
        or not str(normalized.get("valid_until") or "").strip()
    )
    normalized.setdefault("valid_from", None)
    normalized.setdefault("valid_until", None)
    normalized.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    normalized.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    return CouponResponse(**normalized)


async def _evaluate_coupon_for_user(
    *,
    code: str,
    user: Optional[dict],
    request: Request,
    walk_type: str,
    subtotal: float,
) -> dict:
    def _extract_request_ip(req: Request) -> str:
        forwarded = str(req.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = str(req.headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip
        return str(req.client.host if req.client else "").strip()

    def _extract_device_id(req: Request) -> str:
        return str(req.headers.get("x-device-id") or "").strip()[:120]

    def _normalize_phone_identity(value: Any) -> str:
        return re.sub(r"\D", "", str(value or ""))[:20]

    def _coupon_identity_filters(identity_payload: dict) -> List[dict]:
        filters: List[dict] = []
        if identity_payload.get("user_id"):
            filters.append({"user_id": identity_payload["user_id"]})
        if identity_payload.get("user_email"):
            filters.append({"user_email": identity_payload["user_email"]})
        if identity_payload.get("user_phone"):
            filters.append({"user_phone": identity_payload["user_phone"]})
        return filters

    async def _mark_user_suspected(user_id_value: str, reason: str):
        if not user_id_value:
            return
        now_iso_local = datetime.now(timezone.utc).isoformat()
        await db.users.update_one(
            {"id": user_id_value},
            {
                "$set": {"is_suspected": True, "updated_at": now_iso_local},
                "$addToSet": {"suspicion_reasons": reason},
            },
        )

    async def _create_coupon_fraud_alert(*, alert_type: str, message: str, severity: str = "medium", blocked: bool = False, coupon_payload: Optional[CouponResponse] = None, identity_payload: Optional[dict] = None, device_id: str = "", ip_address: str = "", metadata: Optional[dict] = None):
        now_iso_local = datetime.now(timezone.utc).isoformat()
        identity_data = identity_payload or {}
        await db.coupon_fraud_alerts.insert_one(
            {
                "id": str(uuid.uuid4()),
                "alert_type": alert_type,
                "severity": severity if severity in {"low", "medium", "high"} else "medium",
                "message": message,
                "coupon_id": coupon_payload.id if coupon_payload else None,
                "coupon_code": coupon_payload.code if coupon_payload else None,
                "user_id": identity_data.get("user_id") or None,
                "user_email": identity_data.get("user_email") or None,
                "user_phone": identity_data.get("user_phone") or None,
                "device_id": device_id or None,
                "ip_address": ip_address or None,
                "blocked": bool(blocked),
                "metadata": metadata or {},
                "created_at": now_iso_local,
                "updated_at": now_iso_local,
            }
        )

    async def _set_temporary_block(kind: str, value: str, reason: str):
        if not value:
            return
        now_dt_local = datetime.now(timezone.utc)
        await db.anti_abuse_blocks.update_one(
            {"kind": kind, "value": value},
            {
                "$set": {
                    "kind": kind,
                    "value": value,
                    "reason": reason,
                    "blocked_until": (now_dt_local + timedelta(hours=TEMP_REGISTRATION_BLOCK_HOURS)).isoformat(),
                    "updated_at": now_dt_local.isoformat(),
                },
                "$setOnInsert": {"id": str(uuid.uuid4()), "created_at": now_dt_local.isoformat()},
            },
            upsert=True,
        )

    async def _is_temporarily_blocked(kind: str, value: str) -> bool:
        if not value:
            return False
        now_iso_local = datetime.now(timezone.utc).isoformat()
        blocked_row = await db.anti_abuse_blocks.find_one(
            {"kind": kind, "value": value, "blocked_until": {"$gt": now_iso_local}},
            {"_id": 0},
        )
        return bool(blocked_row)

    normalized_code = _normalize_coupon_code(code)
    if not normalized_code:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    coupon = await db.coupons.find_one({"code": normalized_code}, {"_id": 0})
    if not coupon:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    coupon_response = _to_coupon_response(coupon)
    if not coupon_response.is_active:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)
    if walk_type not in coupon_response.applicable_walk_types:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    if not user:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    user_id = str(user.get("id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    target_user_id = str(coupon.get("target_user_id") or "").strip()
    if target_user_id and target_user_id != user_id:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    if bool(user.get("is_coupon_blocked", False)):
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    user_email = str(user.get("email") or "").strip().lower()
    user_phone = _normalize_phone_identity(user.get("phone"))
    if not user_phone:
        owner_profile = await db.owner_profiles.find_one({"user_id": user_id}, {"_id": 0, "phone": 1})
        user_phone = _normalize_phone_identity((owner_profile or {}).get("phone"))

    identity = {
        "user_id": user_id,
        "user_email": user_email,
        "user_phone": user_phone,
    }
    identity_filters = _coupon_identity_filters(identity)

    ip_address = _extract_request_ip(request)
    device_id = _extract_device_id(request)
    if await _is_temporarily_blocked("ip", ip_address) or await _is_temporarily_blocked("device", device_id):
        await _create_coupon_fraud_alert(
            alert_type="coupon_temporary_block",
            message="Uso bloqueado por regra temporária de segurança.",
            severity="high",
            blocked=True,
            coupon_payload=coupon_response,
            identity_payload=identity,
            ip_address=ip_address,
            device_id=device_id,
        )
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    now_dt = datetime.now(timezone.utc)
    valid_from = _parse_iso_datetime(coupon_response.valid_from)
    valid_until = _parse_iso_datetime(coupon_response.valid_until)
    if valid_from and now_dt < valid_from:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)
    if valid_until and now_dt > valid_until:
        await db.coupons.update_one({"id": coupon_response.id}, {"$set": {"is_active": False, "updated_at": now_dt.isoformat()}})
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    if coupon_response.max_global_uses > 0 and coupon_response.used_count >= coupon_response.max_global_uses:
        await db.coupons.update_one({"id": coupon_response.id}, {"$set": {"is_active": False, "updated_at": now_dt.isoformat()}})
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    user_uses = 0
    if identity_filters:
        user_uses = await db.coupon_redemptions.count_documents({"coupon_id": coupon_response.id, "$or": identity_filters})
    if user_uses >= coupon_response.max_uses_per_user:
        await _mark_user_suspected(user_id, "Tentativa de reutilização acima do limite de cupom")
        await _create_coupon_fraud_alert(
            alert_type="coupon_limit_per_identity",
            message="Limite por usuário/email/telefone atingido.",
            severity="medium",
            blocked=True,
            coupon_payload=coupon_response,
            identity_payload=identity,
            ip_address=ip_address,
            device_id=device_id,
        )
        raise HTTPException(status_code=400, detail=COUPON_ERROR_LIMIT)

    if ip_address:
        window_start = (now_dt - timedelta(minutes=COUPON_IP_WINDOW_MINUTES)).isoformat()
        ip_recent_uses = await db.coupon_redemptions.count_documents(
            {
                "coupon_id": coupon_response.id,
                "ip_address": ip_address,
                "used_at": {"$gte": window_start},
            }
        )
        if ip_recent_uses >= 1:
            await _create_coupon_fraud_alert(
                alert_type="coupon_ip_watch",
                message="Padrão de repetição por IP monitorado.",
                severity="medium",
                blocked=False,
                coupon_payload=coupon_response,
                identity_payload=identity,
                ip_address=ip_address,
                device_id=device_id,
                metadata={"window_minutes": COUPON_IP_WINDOW_MINUTES, "recent_uses": ip_recent_uses},
            )
        if ip_recent_uses >= MAX_COUPON_USES_PER_IP_IN_15_MIN:
            await _set_temporary_block("ip", ip_address, "Uso de cupom acima do limite por IP")
            await _mark_user_suspected(user_id, "Uso repetitivo de cupom por IP em janela curta")
            await _create_coupon_fraud_alert(
                alert_type="coupon_ip_burst",
                message="Múltiplos usos do mesmo cupom no mesmo IP em curto período.",
                severity="high",
                blocked=True,
                coupon_payload=coupon_response,
                identity_payload=identity,
                ip_address=ip_address,
                device_id=device_id,
                metadata={"window_minutes": COUPON_IP_WINDOW_MINUTES, "recent_uses": ip_recent_uses},
            )
            raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    if device_id:
        distinct_device_users = await db.coupon_redemptions.distinct(
            "user_id",
            {
                "coupon_id": coupon_response.id,
                "device_id": device_id,
                "user_id": {"$ne": ""},
            },
        )
        known_users = {str(item) for item in distinct_device_users if str(item).strip()}
        if len(known_users) >= 2:
            await _create_coupon_fraud_alert(
                alert_type="coupon_device_watch",
                message="Uso de cupom em múltiplas contas no mesmo dispositivo em monitoramento.",
                severity="medium",
                blocked=False,
                coupon_payload=coupon_response,
                identity_payload=identity,
                ip_address=ip_address,
                device_id=device_id,
                metadata={"known_accounts": len(known_users)},
            )
        if user_id not in known_users and len(known_users) >= MAX_ACCOUNTS_PER_DEVICE:
            await _set_temporary_block("device", device_id, "Uso de cupom em múltiplas contas no mesmo dispositivo")
            await _mark_user_suspected(user_id, "Múltiplas contas no mesmo dispositivo para uso de cupom")
            await _create_coupon_fraud_alert(
                alert_type="coupon_device_multi_account",
                message="Dispositivo excedeu o limite de contas para uso de cupons.",
                severity="high",
                blocked=True,
                coupon_payload=coupon_response,
                identity_payload=identity,
                ip_address=ip_address,
                device_id=device_id,
                metadata={"known_accounts": len(known_users)},
            )
            raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    subtotal_value = round(max(0.0, _coerce_float(subtotal, 0.0)), 2)
    discount_amount = round(
        (subtotal_value * (coupon_response.discount_percent / 100.0)) + coupon_response.discount_fixed,
        2,
    )
    discount_amount = min(discount_amount, subtotal_value)
    total_after_discount = round(max(0.0, subtotal_value - discount_amount), 2)

    remaining_uses_for_user = max(0, coupon_response.max_uses_per_user - user_uses)
    return {
        "coupon": coupon_response,
        "discount_amount": discount_amount,
        "subtotal": subtotal_value,
        "total_after_discount": total_after_discount,
        "remaining_uses_for_user": remaining_uses_for_user,
        "identity": identity,
        "ip_address": ip_address,
        "device_id": device_id,
    }


async def _register_coupon_redemption(
    *,
    coupon: CouponResponse,
    identity: dict,
    ip_address: str,
    device_id: str,
    walk_id: str,
    discount_amount: float,
):
    user_id = str(identity.get("user_id") or "").strip()
    user_email = str(identity.get("user_email") or "").strip().lower()
    user_phone = re.sub(r"\D", "", str(identity.get("user_phone") or ""))

    identity_filters: List[dict] = []
    if user_id:
        identity_filters.append({"user_id": user_id})
    if user_email:
        identity_filters.append({"user_email": user_email})
    if user_phone:
        identity_filters.append({"user_phone": user_phone})

    if identity_filters:
        current_user_uses = await db.coupon_redemptions.count_documents({"coupon_id": coupon.id, "$or": identity_filters})
        if current_user_uses >= coupon.max_uses_per_user:
            raise HTTPException(status_code=400, detail=COUPON_ERROR_LIMIT)

    coupon_query: Dict[str, Any] = {"id": coupon.id, "is_active": True}
    if coupon.max_global_uses > 0:
        coupon_query["used_count"] = {"$lt": coupon.max_global_uses}

    update_result = await db.coupons.update_one(
        coupon_query,
        {
            "$inc": {"used_count": 1},
            "$set": {"updated_at": datetime.now(timezone.utc).isoformat()},
        },
    )
    if update_result.matched_count == 0:
        raise HTTPException(status_code=400, detail=COUPON_ERROR_INVALID)

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.coupon_redemptions.insert_one(
        {
            "id": str(uuid.uuid4()),
            "coupon_id": coupon.id,
            "coupon_code": coupon.code,
            "user_id": user_id or "",
            "user_email": user_email,
            "user_phone": user_phone,
            "device_id": device_id,
            "ip_address": ip_address,
            "walk_id": walk_id,
            "discount_amount": round(max(0.0, _coerce_float(discount_amount, 0.0)), 2),
            "used_at": now_iso,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )

    if coupon.max_global_uses > 0:
        refreshed_coupon = await db.coupons.find_one({"id": coupon.id}, {"_id": 0, "used_count": 1, "max_global_uses": 1})
        if refreshed_coupon and int(refreshed_coupon.get("used_count", 0) or 0) >= int(refreshed_coupon.get("max_global_uses", 0) or 0):
            await db.coupons.update_one(
                {"id": coupon.id},
                {"$set": {"is_active": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
            )

    if user_id:
        rapid_window_start = (datetime.now(timezone.utc) - timedelta(minutes=COUPON_IP_WINDOW_MINUTES)).isoformat()
        recent_user_redemptions = await db.coupon_redemptions.count_documents(
            {
                "user_id": user_id,
                "used_at": {"$gte": rapid_window_start},
            }
        )
        if recent_user_redemptions >= 3:
            await db.users.update_one(
                {"id": user_id},
                {
                    "$set": {"is_suspected": True, "updated_at": datetime.now(timezone.utc).isoformat()},
                    "$addToSet": {"suspicion_reasons": "Uso repetitivo de cupons em sequência curta"},
                },
            )
            await db.coupon_fraud_alerts.insert_one(
                {
                    "id": str(uuid.uuid4()),
                    "alert_type": "coupon_sequence_short_window",
                    "severity": "medium",
                    "message": "Uso repetitivo de cupons em sequência curta detectado.",
                    "coupon_id": coupon.id,
                    "coupon_code": coupon.code,
                    "user_id": user_id,
                    "user_email": user_email or None,
                    "user_phone": user_phone or None,
                    "device_id": device_id or None,
                    "ip_address": ip_address or None,
                    "blocked": False,
                    "metadata": {"recent_uses": recent_user_redemptions},
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
            )


async def _resolve_walker_profile(walker_id: str) -> Optional[dict]:
    selected_walker = WALKER_MAP.get(walker_id)
    if selected_walker:
        return _ensure_walker_schedule(selected_walker)

    if walker_id.startswith("partner-"):
        partner_id = walker_id.replace("partner-", "", 1)
        partner = await db.partner_applications.find_one(
            {"id": partner_id, "status": "Aprovado", "active_as_walker": True},
            {"_id": 0},
        )
        if partner:
            return _ensure_walker_schedule({
                "id": walker_id,
                "name": partner["full_name"],
                "photo_url": partner.get("profile_photo_url") or _build_avatar_data_uri("#DFF5E8", "#2FBF71"),
                "possuiVeiculo": bool(partner.get("possuiVeiculo", False)),
                "aceitaDeslocamentoPremium": bool(partner.get("aceitaDeslocamentoPremium", False)),
                "raioMaximoPremiumKm": float(partner.get("raioMaximoPremiumKm", 0) or 0),
                "ativoParaTransportePremium": bool(partner.get("ativoParaTransportePremium", False)),
                "has_water": bool(partner.get("has_water", False)),
                "has_bowl": bool(partner.get("has_bowl", False)),
                "has_bags": bool(partner.get("has_bags", False)),
                "has_first_aid": bool(partner.get("has_first_aid", False)),
                "has_towel": bool(partner.get("has_towel", False)),
                "has_extra_leash": bool(partner.get("has_extra_leash", False)),
                "has_premium_items": bool(partner.get("has_premium_items", False)),
                "kit_missing_reports_count": int(partner.get("kit_missing_reports_count", 0) or 0),
                "premium_verified_badge_active": bool(partner.get("premium_verified_badge_active", False)),
                "premium_verified_streak": int(partner.get("premium_verified_streak", 0) or 0),
                "premium_verified_last_reason": str(partner.get("premium_verified_last_reason") or ""),
                "is_verified": bool(partner.get("is_verified", False)),
                "verification_level": str(partner.get("verification_level") or VERIFICATION_LEVEL_NONE),
                "verification_score_snapshot": int(partner.get("verification_score_snapshot", 0) or 0),
                "reputation_credits": int(partner.get("reputation_credits", 0) or 0),
                "last_credit_update": partner.get("last_credit_update"),
                "cr_matching_boost_until": partner.get("cr_matching_boost_until"),
                "cr_early_wave_until": partner.get("cr_early_wave_until"),
                "cr_visual_highlight_until": partner.get("cr_visual_highlight_until"),
                "cr_matching_boost_points_active": _coerce_float(partner.get("cr_matching_boost_points_active"), CR_MATCHING_BOOST_BASE_POINTS),
                "cr_early_wave_priority_active": _coerce_float(partner.get("cr_early_wave_priority_active"), CR_EARLY_WAVE_BASE_PRIORITY),
                "cr_visual_exposure_points_active": _coerce_float(partner.get("cr_visual_exposure_points_active"), CR_VISUAL_EXPOSURE_BASE_POINTS),
                "availability_days": partner.get("availability_days", []),
                "availability_start_time": partner.get("availability_start_time", ""),
                "availability_end_time": partner.get("availability_end_time", ""),
                "availability_capacity_by_period": partner.get("availability_capacity_by_period", {}),
                "horarios_disponiveis": partner.get("horarios_disponiveis", {}),
                "availability_blocks": partner.get("availability_blocks", []),
                "unavailable_until": partner.get("unavailable_until"),
                "region": partner.get("neighborhood_region", partner.get("region", "")),
                "quality_status": partner.get("quality_status", QUALITY_STATUS_ACTIVE),
            })

        walker_user = await db.users.find_one({"id": partner_id, "role": "passeador", "isActive": True}, {"_id": 0})
        if walker_user:
            return _ensure_walker_schedule({
                "id": walker_id,
                "name": walker_user.get("full_name", "Passeador"),
                "photo_url": walker_user.get("profile_photo_url") or _build_avatar_data_uri("#EAF7EF", "#2FBF71"),
                "possuiVeiculo": bool(walker_user.get("possuiVeiculo", False)),
                "aceitaDeslocamentoPremium": bool(walker_user.get("aceitaDeslocamentoPremium", False)),
                "raioMaximoPremiumKm": float(walker_user.get("raioMaximoPremiumKm", 0) or 0),
                "ativoParaTransportePremium": bool(walker_user.get("ativoParaTransportePremium", False)),
                "has_water": bool(walker_user.get("has_water", False)),
                "has_bowl": bool(walker_user.get("has_bowl", False)),
                "has_bags": bool(walker_user.get("has_bags", False)),
                "has_first_aid": bool(walker_user.get("has_first_aid", False)),
                "has_towel": bool(walker_user.get("has_towel", False)),
                "has_extra_leash": bool(walker_user.get("has_extra_leash", False)),
                "has_premium_items": bool(walker_user.get("has_premium_items", False)),
                "kit_missing_reports_count": int(walker_user.get("kit_missing_reports_count", 0) or 0),
                "premium_verified_badge_active": bool(walker_user.get("premium_verified_badge_active", False)),
                "premium_verified_streak": int(walker_user.get("premium_verified_streak", 0) or 0),
                "premium_verified_last_reason": str(walker_user.get("premium_verified_last_reason") or ""),
                "is_verified": bool(walker_user.get("is_verified", False)),
                "verification_level": str(walker_user.get("verification_level") or VERIFICATION_LEVEL_NONE),
                "verification_score_snapshot": int(walker_user.get("verification_score_snapshot", 0) or 0),
                "reputation_credits": int(walker_user.get("reputation_credits", 0) or 0),
                "last_credit_update": walker_user.get("last_credit_update"),
                "cr_matching_boost_until": walker_user.get("cr_matching_boost_until"),
                "cr_early_wave_until": walker_user.get("cr_early_wave_until"),
                "cr_visual_highlight_until": walker_user.get("cr_visual_highlight_until"),
                "cr_matching_boost_points_active": _coerce_float(walker_user.get("cr_matching_boost_points_active"), CR_MATCHING_BOOST_BASE_POINTS),
                "cr_early_wave_priority_active": _coerce_float(walker_user.get("cr_early_wave_priority_active"), CR_EARLY_WAVE_BASE_PRIORITY),
                "cr_visual_exposure_points_active": _coerce_float(walker_user.get("cr_visual_exposure_points_active"), CR_VISUAL_EXPOSURE_BASE_POINTS),
                "availability_days": walker_user.get("availability_days", []),
                "availability_start_time": walker_user.get("availability_start_time", ""),
                "availability_end_time": walker_user.get("availability_end_time", ""),
                "availability_capacity_by_period": walker_user.get("availability_capacity_by_period", {}),
                "horarios_disponiveis": walker_user.get("horarios_disponiveis", {}),
                "availability_blocks": walker_user.get("availability_blocks", []),
                "unavailable_until": walker_user.get("unavailable_until"),
                "region": walker_user.get("region", ""),
                "quality_status": walker_user.get("quality_status", QUALITY_STATUS_ACTIVE),
                "quality_metrics": walker_user.get("quality_metrics", {}),
            })

    return None


async def _get_pet_or_404(pet_id: str) -> dict:
    pet = await db.pets.find_one({"id": pet_id}, {"_id": 0})
    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado")
    return pet


async def _finished_walks_count_for_pet(pet_id: str) -> int:
    return await db.walks.count_documents({"status": STATUS_FINISHED, "pet_ids": pet_id})


async def _validate_pet_for_shared_rules(pet: dict, require_admin_flags: bool = True):
    if pet.get("dog_behavior") == "Reativo":
        raise HTTPException(status_code=400, detail=f"{pet.get('pet_name')} é reativo e não pode participar de passeio compartilhado")

    if not pet.get("gets_along_with_dogs", False) or not pet.get("accepts_shared_walk", False):
        raise HTTPException(status_code=400, detail=f"{pet.get('pet_name')} não aceita passeio compartilhado")

    if require_admin_flags and (
        not pet.get("podeParticiparCompartilhado", False) or not pet.get("aprovadoParaCompartilhado", False)
    ):
        raise HTTPException(status_code=400, detail=f"{pet.get('pet_name')} ainda não foi aprovado para compartilhado")

    finished = await _finished_walks_count_for_pet(pet["id"])
    if finished < 1:
        raise HTTPException(status_code=400, detail=f"{pet.get('pet_name')} precisa de ao menos 1 passeio finalizado antes de compartilhado")


def _payment_rows_for_walk(walk: dict) -> List[dict]:
    now_iso = datetime.now(timezone.utc).isoformat()
    walk_id = walk["id"]
    total_price, _ = _calculate_walk_pricing(walk)
    base_component = _coerce_float(walk.get("valor_base_passeio", _base_walk_price(walk)), 0.0)
    additional_component = _coerce_float(walk.get("adicionalDeslocamento", 0.0), 0.0)
    subtotal_before_discount = round(base_component + additional_component, 2)
    _, _, discount_amount = _coupon_discount_components(walk, subtotal_before_discount)
    coupon_code = _normalize_coupon_code(walk.get("coupon_code", ""))
    walk_type = walk.get("walk_type", WALK_TYPE_INDIVIDUAL)
    context = walk.get("shared_context")
    shared_clients = walk.get("shared_client_names", [])

    coupon_note = ""
    if coupon_code and discount_amount > 0:
        coupon_note = f"Cupom {coupon_code} aplicado (-R$ {discount_amount:.2f})"

    if walk_type == WALK_TYPE_SHARED and context == SHARED_CONTEXT_OTHER_CLIENT and len(shared_clients) >= 2:
        split_value = round(total_price / 2, 2)
        second_split_value = round(total_price - split_value, 2)
        shared_notes = "Cobrança individual de passeio compartilhado" + (" + deslocamento premium" if additional_component else "")
        if coupon_note:
            shared_notes = f"{shared_notes}. {coupon_note}"

        return [
            {
                "id": str(uuid.uuid4()),
                "walk_id": walk_id,
                "client_name": shared_clients[0],
                "plan_type": "Compartilhado (45 min)",
                "tipoPlano": "avulso",
                "value": split_value,
                "payment_status": "Pendente",
                "payment_method": "",
                #"tipoPagamento": "",
                "payment_date": None,
                "notes": shared_notes,
                "created_at": now_iso,
                "updated_at": now_iso,
            },
            {
                "id": str(uuid.uuid4()),
                "walk_id": walk_id,
                "client_name": shared_clients[1],
                "plan_type": "Compartilhado (45 min)",
                "tipoPlano": "avulso",
                "value": second_split_value,
                "payment_status": "Pendente",
                "payment_method": "",
                #"tipoPagamento": "",
                "payment_date": None,
                "notes": shared_notes,
                "created_at": now_iso,
                "updated_at": now_iso,
            },
        ]

    plan_type = "Avulso"
    if walk_type == WALK_TYPE_SHARED:
        plan_type = "Compartilhado (mesma casa)" if context == SHARED_CONTEXT_SAME_HOUSEHOLD else "Compartilhado (análise)"

    notes = "Com adicional premium" if additional_component else ""
    if coupon_note:
        notes = f"{notes}. {coupon_note}".strip(". ")

    return [
        {
            "id": str(uuid.uuid4()),
            "walk_id": walk_id,
            "client_name": walk.get("client_name", "Cliente"),
            "plan_type": plan_type,
            "tipoPlano": "avulso",
            "value": total_price,
            "payment_status": "Pendente",
            "payment_method": "",
            #"tipoPagamento": "",
            "payment_date": None,
            "notes": notes,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    ]


def _sanitize_origin_url(origin_url: Optional[str], request: Request) -> str:
    incoming = str(origin_url or "").strip()
    if incoming.startswith("http://") or incoming.startswith("https://"):
        return incoming.rstrip("/")
    return str(request.base_url).rstrip("/")



async def _sync_tip_checkout_status(session_id: str, request: Request) -> TipCheckoutStatusResponse:
    transaction = await db.payment_transactions.find_one(
        {"session_id": session_id, "transaction_type": "tip"},
        {"_id": 0},
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Transação de gorjeta não encontrada")

    now_iso = datetime.now(timezone.utc).isoformat()
    payment_status = str(transaction.get("payment_status") or transaction.get("status") or "pending").lower()
    tip_id = str(transaction.get("tip_id") or "")
    tip_doc = await db.tips.find_one({"id": tip_id}, {"_id": 0}) if tip_id else None
    if not tip_doc:
        raise HTTPException(status_code=404, detail="Registro de gorjeta não encontrado")

    await db.payment_transactions.update_one(
        {"id": transaction["id"]},
        {"$set": {"payment_status": payment_status, "updated_at": now_iso}},
    )

    tip_status = str(tip_doc.get("status") or "pending")
    paid_at = tip_doc.get("paid_at")
    suspicious_flag = bool(tip_doc.get("suspicious_flag", False))

    if payment_status == "paid" and tip_status != "paid":
        paid_at = paid_at or now_iso
        tip_status = "paid"
        suspicious_flag = _tip_is_suspicious(
            amount=_coerce_float(tip_doc.get("amount"), 0.0),
            walk=await _get_walk_or_404(str(tip_doc.get("walk_id") or "")),
            client_user_id=str(tip_doc.get("client_user_id") or ""),
            walker_user_id=str(tip_doc.get("walker_user_id") or ""),
        )
        await db.tips.update_one(
            {"id": tip_doc["id"]},
            {"$set": {"status": tip_status, "paid_at": paid_at, "suspicious_flag": suspicious_flag, "updated_at": now_iso}},
        )
        await db.walks.update_one(
            {"id": str(tip_doc.get("walk_id") or "")},
            {"$set": {"tip_status": tip_status, "tip_amount": _coerce_float(tip_doc.get("amount"), 0.0), "tip_paid_at": paid_at, "updated_at": now_iso}},
        )

    return TipCheckoutStatusResponse(
        session_id=session_id,
        status=payment_status,
        payment_status=payment_status,
        walk_id=str(tip_doc.get("walk_id") or ""),
        tip_amount=_coerce_float(tip_doc.get("amount"), 0.0),
        tip_status=tip_status,
        paid_at=paid_at,
        suspicious_flag=suspicious_flag,
    )


async def _rebuild_payments_for_walk(walk: dict):
    await db.payments.delete_many({"walk_id": walk["id"]})
    payment_rows = _payment_rows_for_walk(walk)
    if payment_rows:
        await db.payments.insert_many(payment_rows)


async def _sync_payments_from_walks():
    walks = await db.walks.find({}, {"_id": 0}).to_list(1000)
    for walk in walks:
        if walk.get("id"):
            existing = await db.payments.find({"walk_id": walk["id"]}, {"_id": 0}).to_list(10)
            if existing:
                continue
            payment_rows = _payment_rows_for_walk(walk)
            if payment_rows:
                await db.payments.insert_many(payment_rows)


@api_router.get("/")
async def root():
    return {"message": "PetPasso API ativa"}


@api_router.get("/walkers", response_model=List[WalkerResponse])
async def list_walkers(
    request: Request,
    date: Optional[str] = None,
    duration_minutes: int = 30,
    preferred_time: Optional[str] = None,
    neighborhood: Optional[str] = None,
    tipo_passeio: Literal["padrao", "transporte"] = "padrao",
    selected_pets_count: int = Query(default=1, ge=1, le=2),
):
    if duration_minutes not in WALK_DURATION_OPTIONS:
        raise HTTPException(status_code=422, detail="A duração deve ser 30, 45 ou 60 minutos")

    selected_date = str(date or "").strip()
    if not selected_date:
        selected_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        _weekday_key_from_date(selected_date)

    normalized_preferred_time = str(preferred_time or "").strip()
    if normalized_preferred_time and not re.match(r"^\d{2}:\d{2}$", normalized_preferred_time):
        normalized_preferred_time = ""

    normalized_neighborhood = str(neighborhood or "").strip().lower()
    normalized_tipo_passeio = str(tipo_passeio or "padrao").strip().lower()
    is_transport_request = normalized_tipo_passeio == "transporte"
    dynamic_pricing_settings = await _load_dynamic_pricing_settings()
    dynamic_mode = str(dynamic_pricing_settings.get("dynamicPricingMode") or DYNAMIC_PRICING_MODE_OFF)
    if not bool(dynamic_pricing_settings.get("dynamicPricingEnabled", False)):
        dynamic_mode = DYNAMIC_PRICING_MODE_OFF
    preferred_hour = normalized_preferred_time[:2] if normalized_preferred_time else ""
    is_critical_hour_context = bool(normalized_preferred_time and _is_critical_hour(normalized_preferred_time, DEFAULT_CRITICAL_WINDOWS))

    viewer_role = "public"
    viewer_user = None
    try:
        viewer_user = await _get_current_user(request)
        viewer_role = str(viewer_user.get("role") or "public")
        if str(viewer_user.get("id") or ""):
            await _evaluate_and_apply_disintermediation_flag(str(viewer_user.get("id") or ""))
    except HTTPException:
        pass

    preferred_rebooking_walker_id = ""
    if viewer_role == "cliente" and viewer_user and str(viewer_user.get("id") or ""):
        latest_finished_walk = await db.walks.find_one(
            {
                "client_user_id": str(viewer_user.get("id") or ""),
                "status": STATUS_FINISHED,
            },
            {"_id": 0, "walker_id": 1, "walker_user_id": 1},
            sort=[("walk_datetime_iso", -1), ("updated_at", -1)],
        )
        if latest_finished_walk:
            preferred_rebooking_walker_id = str(latest_finished_walk.get("walker_id") or "").strip()
            if not preferred_rebooking_walker_id:
                walker_user_id = str(latest_finished_walk.get("walker_user_id") or "").strip()
                if walker_user_id:
                    preferred_rebooking_walker_id = f"partner-{walker_user_id}"

    if is_transport_request and not await _is_pet_transport_available_for_user(viewer_user):
        raise HTTPException(status_code=400, detail="Passeio com transporte está desativado para sua conta")

    mask_public_identity = viewer_role in {"public", "cliente"}

    active_partners = await db.partner_applications.find(
        {"status": "Aprovado", "active_as_walker": True},
        {"_id": 0},
    ).to_list(500)

    dynamic_walkers = [
        _ensure_walker_schedule({
            "id": f"partner-{candidate['id']}",
            "name": candidate["full_name"],
            "photo_url": candidate.get("profile_photo_url") or _build_avatar_data_uri("#DFF5E8", "#2FBF71"),
            "possuiVeiculo": bool(candidate.get("possuiVeiculo", False)),
            "aceitaDeslocamentoPremium": bool(candidate.get("aceitaDeslocamentoPremium", False)),
            "raioMaximoPremiumKm": float(candidate.get("raioMaximoPremiumKm", 0) or 0),
            "ativoParaTransportePremium": bool(candidate.get("ativoParaTransportePremium", False)),
            "has_water": bool(candidate.get("has_water", False)),
            "has_bowl": bool(candidate.get("has_bowl", False)),
            "has_bags": bool(candidate.get("has_bags", False)),
            "has_first_aid": bool(candidate.get("has_first_aid", False)),
            "has_towel": bool(candidate.get("has_towel", False)),
            "has_extra_leash": bool(candidate.get("has_extra_leash", False)),
            "has_premium_items": bool(candidate.get("has_premium_items", False)),
            "kit_missing_reports_count": int(candidate.get("kit_missing_reports_count", 0) or 0),
            "premium_verified_badge_active": bool(candidate.get("premium_verified_badge_active", False)),
            "premium_verified_streak": int(candidate.get("premium_verified_streak", 0) or 0),
            "premium_verified_last_reason": str(candidate.get("premium_verified_last_reason") or ""),
            "is_verified": bool(candidate.get("is_verified", False)),
            "verification_level": str(candidate.get("verification_level") or VERIFICATION_LEVEL_NONE),
            "verification_score_snapshot": int(candidate.get("verification_score_snapshot", 0) or 0),
            "reputation_credits": int(candidate.get("reputation_credits", 0) or 0),
            "last_credit_update": candidate.get("last_credit_update"),
            "cr_matching_boost_until": candidate.get("cr_matching_boost_until"),
            "cr_early_wave_until": candidate.get("cr_early_wave_until"),
            "cr_visual_highlight_until": candidate.get("cr_visual_highlight_until"),
            "cr_matching_boost_points_active": _coerce_float(candidate.get("cr_matching_boost_points_active"), CR_MATCHING_BOOST_BASE_POINTS),
            "cr_early_wave_priority_active": _coerce_float(candidate.get("cr_early_wave_priority_active"), CR_EARLY_WAVE_BASE_PRIORITY),
            "cr_visual_exposure_points_active": _coerce_float(candidate.get("cr_visual_exposure_points_active"), CR_VISUAL_EXPOSURE_BASE_POINTS),
            "availability_days": candidate.get("availability_days", []),
            "availability_start_time": candidate.get("availability_start_time", ""),
            "availability_end_time": candidate.get("availability_end_time", ""),
            "availability_capacity_by_period": candidate.get("availability_capacity_by_period", {}),
            "horarios_disponiveis": candidate.get("horarios_disponiveis", {}),
            "availability_blocks": candidate.get("availability_blocks", []),
            "unavailable_until": candidate.get("unavailable_until"),
            "region": candidate.get("neighborhood_region", candidate.get("region", "")),
            "quality_status": candidate.get("quality_status", QUALITY_STATUS_ACTIVE),
            "flag_suspeita_desintermediacao": bool(candidate.get("flag_suspeita_desintermediacao", False)),
            "desintermediacao_flag_expires_at": candidate.get("desintermediacao_flag_expires_at"),
            "disintermediation_limited_until": candidate.get("disintermediation_limited_until"),
        })
        for candidate in active_partners
    ]

    registered_walkers = await db.users.find(
        {"role": "passeador", "isActive": True},
        {"_id": 0},
    ).to_list(500)
    user_walkers = [
        _ensure_walker_schedule({
            "id": f"partner-{row['id']}",
            "name": row.get("full_name", "Passeador"),
            "photo_url": row.get("profile_photo_url") or _build_avatar_data_uri("#EAF7EF", "#2FBF71"),
            "possuiVeiculo": bool(row.get("possuiVeiculo", False)),
            "aceitaDeslocamentoPremium": bool(row.get("aceitaDeslocamentoPremium", False)),
            "raioMaximoPremiumKm": float(row.get("raioMaximoPremiumKm", 0) or 0),
            "ativoParaTransportePremium": bool(row.get("ativoParaTransportePremium", False)),
            "has_water": bool(row.get("has_water", False)),
            "has_bowl": bool(row.get("has_bowl", False)),
            "has_bags": bool(row.get("has_bags", False)),
            "has_first_aid": bool(row.get("has_first_aid", False)),
            "has_towel": bool(row.get("has_towel", False)),
            "has_extra_leash": bool(row.get("has_extra_leash", False)),
            "has_premium_items": bool(row.get("has_premium_items", False)),
            "kit_missing_reports_count": int(row.get("kit_missing_reports_count", 0) or 0),
            "premium_verified_badge_active": bool(row.get("premium_verified_badge_active", False)),
            "premium_verified_streak": int(row.get("premium_verified_streak", 0) or 0),
            "premium_verified_last_reason": str(row.get("premium_verified_last_reason") or ""),
            "is_verified": bool(row.get("is_verified", False)),
            "verification_level": str(row.get("verification_level") or VERIFICATION_LEVEL_NONE),
            "verification_score_snapshot": int(row.get("verification_score_snapshot", 0) or 0),
            "reputation_credits": int(row.get("reputation_credits", 0) or 0),
            "last_credit_update": row.get("last_credit_update"),
            "cr_matching_boost_until": row.get("cr_matching_boost_until"),
            "cr_early_wave_until": row.get("cr_early_wave_until"),
            "cr_visual_highlight_until": row.get("cr_visual_highlight_until"),
            "cr_matching_boost_points_active": _coerce_float(row.get("cr_matching_boost_points_active"), CR_MATCHING_BOOST_BASE_POINTS),
            "cr_early_wave_priority_active": _coerce_float(row.get("cr_early_wave_priority_active"), CR_EARLY_WAVE_BASE_PRIORITY),
            "cr_visual_exposure_points_active": _coerce_float(row.get("cr_visual_exposure_points_active"), CR_VISUAL_EXPOSURE_BASE_POINTS),
            "availability_days": row.get("availability_days", []),
            "availability_start_time": row.get("availability_start_time", ""),
            "availability_end_time": row.get("availability_end_time", ""),
            "availability_capacity_by_period": row.get("availability_capacity_by_period", {}),
            "horarios_disponiveis": row.get("horarios_disponiveis", {}),
            "availability_blocks": row.get("availability_blocks", []),
            "unavailable_until": row.get("unavailable_until"),
            "is_premium_featured": bool(row.get("is_premium_featured", False)),
            "quality_status": row.get("quality_status", QUALITY_STATUS_ACTIVE),
            "region": row.get("region", ""),
            "flag_suspeita_desintermediacao": bool(row.get("flag_suspeita_desintermediacao", False)),
            "desintermediacao_flag_expires_at": row.get("desintermediacao_flag_expires_at"),
            "disintermediation_limited_until": row.get("disintermediation_limited_until"),
            "behavior_risk_flag_active": bool(row.get("behavior_risk_flag_active", False)),
            "behavior_risk_flag_until": row.get("behavior_risk_flag_until"),
        })
        for row in registered_walkers
    ]

    all_walkers = [_ensure_walker_schedule(walker) for walker in WALKER_PROFILES] + dynamic_walkers + user_walkers
    deduped = {walker["id"]: walker for walker in all_walkers}

    all_walks = await db.walks.find({}, {"_id": 0}).to_list(5000)
    walk_index: Dict[str, List[dict]] = {}
    today_walk_count: Dict[str, int] = {}
    regional_hour_load: Dict[str, int] = {}
    selected_date_today = selected_date

    for walk in all_walks:
        walker_id = str(walk.get("walker_id") or "").strip()
        walker_name_key = str(walk.get("walker_name") or "").strip().lower()
        walker_user_id = str(walk.get("walker_user_id") or "").strip()
        keys = {walker_id, walker_name_key}
        if walker_user_id:
            keys.add(walker_user_id)
            keys.add(f"partner-{walker_user_id}")

        for key in [item for item in keys if item]:
            walk_index.setdefault(key, []).append(walk)

        if walker_id and str(walk.get("walk_date") or "") == selected_date_today and walk.get("status") in BLOCKING_WALK_STATUSES:
            today_walk_count[walker_id] = today_walk_count.get(walker_id, 0) + 1

        walk_date = str(walk.get("walk_date") or "")
        walk_time = str(walk.get("walk_time") or "")
        walk_region = str(walk.get("pickup_neighborhood") or walk.get("neighborhood") or "").strip().lower()
        if walk_date == selected_date_today and walk_time and walk_region and walk.get("status") in BLOCKING_WALK_STATUSES:
            hour = walk_time[:2]
            regional_hour_key = f"{walk_date}:{hour}:{walk_region}"
            regional_hour_load[regional_hour_key] = regional_hour_load.get(regional_hour_key, 0) + 1

    ranked_walkers: List[dict] = []
    incentive_settings = await _get_incentive_settings_dict()
    premium_verified_settings = await _get_premium_verified_settings_dict()
    premium_verified_badge_enabled = _is_feature_active("premium_verified_badge_enabled") and _is_feature_active("premium_verified_enabled")
    premium_verified_bonus_enabled = _is_feature_active("premium_verified_bonus_enabled")
    current_dt = datetime.now(timezone.utc)
    week_start, week_end = _week_bounds(current_dt)
    platform_tip_average = await _platform_tip_average_recent()
    tips_active = _is_feature_active("tips")
    incentives_active = _is_feature_active("habit_incentive")
    badges_visible = _is_feature_visible("visible_badges")
    highlights_visible = _is_feature_visible("weekly_highlights")
    for walker in deduped.values():
        walker_payload = dict(walker)
        walker_id = str(walker_payload.get("id") or "")
        if walker_id.startswith("partner-") and "flag_suspeita_desintermediacao" not in walker_payload:
            walker_user_id_lookup = walker_id.replace("partner-", "", 1)
            walker_flag_row = await db.users.find_one(
                {"id": walker_user_id_lookup},
                {
                    "_id": 0,
                    "flag_suspeita_desintermediacao": 1,
                    "desintermediacao_flag_expires_at": 1,
                    "disintermediation_limited_until": 1,
                    "behavior_risk_flag_active": 1,
                    "behavior_risk_flag_until": 1,
                },
            )
            if walker_flag_row:
                walker_payload["flag_suspeita_desintermediacao"] = bool(walker_flag_row.get("flag_suspeita_desintermediacao", False))
                walker_payload["desintermediacao_flag_expires_at"] = walker_flag_row.get("desintermediacao_flag_expires_at")
                walker_payload["disintermediation_limited_until"] = walker_flag_row.get("disintermediation_limited_until")
                walker_payload["behavior_risk_flag_active"] = bool(walker_flag_row.get("behavior_risk_flag_active", False))
                walker_payload["behavior_risk_flag_until"] = walker_flag_row.get("behavior_risk_flag_until")
        walker_name_key = str(walker_payload.get("name") or "").strip().lower()

        walker_walks = walk_index.get(walker_id) or walk_index.get(walker_name_key) or []
        walker_user_id = str(walker_payload.get("id") or "").replace("partner-", "")
        tip_rows = (
            await _list_paid_tips_for_walker(
                walker_user_id=walker_user_id,
                walker_id=walker_id,
                walker_name=str(walker_payload.get("name") or ""),
                limit=500,
            )
            if tips_active
            else []
        )
        tip_total_amount = round(sum(_coerce_float(item.get("amount"), 0.0) for item in tip_rows), 2)
        initial_status = str(walker_payload.get("quality_status") or QUALITY_STATUS_ACTIVE)
        inferred_status, _ = _quality_status_from_reputation(
            _compute_reputation_metrics(
                walker_walks,
                initial_status,
                tip_total_amount,
                tip_rows,
                walker_payload,
                platform_tip_average,
            )
        )
        quality_status = initial_status if initial_status in {
            QUALITY_STATUS_PREMIUM,
            QUALITY_STATUS_ACTIVE,
            QUALITY_STATUS_OBSERVATION,
            QUALITY_STATUS_RESTRICTED,
            QUALITY_STATUS_SUSPENDED,
        } else inferred_status

        limited_until = _parse_iso_datetime(walker_payload.get("disintermediation_limited_until"))
        if limited_until and limited_until > current_dt:
            continue

        metrics = _compute_reputation_metrics(
            walker_walks,
            quality_status,
            tip_total_amount,
            tip_rows,
            walker_payload,
            platform_tip_average,
        )
        score_final = _coerce_float(metrics.get("score_final"), 0.0)

        week_walks = [
            walk
            for walk in walker_walks
            if walk.get("status") == STATUS_FINISHED
            and (walk_dt := _walk_datetime_from_doc(walk))
            and week_start <= walk_dt < week_end
        ]
        weekly_completed_walks = len(week_walks)
        week_tip_rows = [
            tip
            for tip in tip_rows
            if (tip_dt := _parse_iso_datetime(tip.get("paid_at") or tip.get("updated_at") or tip.get("created_at")))
            and week_start <= tip_dt < week_end
        ]
        week_tip_total = round(sum(_coerce_float(item.get("amount"), 0.0) for item in week_tip_rows), 2)
        mission_info = _weekly_mission_progress(week_walks, week_tip_rows)
        mission_bonus_points = _coerce_float(mission_info.get("mission_bonus_points"), 0.0)
        mission_bonus_active = bool(mission_info.get("completed_all", False))
        mission_priority_points = 2.5 if mission_bonus_active else 0.0
        if not incentives_active:
            mission_bonus_points = 0.0
            mission_bonus_active = False
            mission_priority_points = 0.0

        no_show_rate = _coerce_float(metrics.get("no_show_rate"), 0.0)
        completed_walks_total = int(metrics.get("completed_walks", 0) or 0)
        checklist_streak = int(walker_payload.get("kit_checklist_streak", 0) or 0)
        infractions = int(walker_payload.get("kit_missing_reports_count", 0) or 0)
        rating_avg = _coerce_float(metrics.get("rating_weighted_avg"), _coerce_float(metrics.get("rating_avg"), 0.0))
        cancel_rate = _coerce_float(metrics.get("cancel_rate"), 0.0)
        walker_level = _determine_walker_level(
            score_final,
            completed_walks_total,
            no_show_rate,
            rating_avg=rating_avg,
            cancel_rate=cancel_rate,
            checklist_streak=checklist_streak,
            infractions=infractions,
        )
        level_priority_bonus = _walker_level_boost_factor(walker_level)
        level_priority_points = 0.0
        weekly_tip_goal_reached = week_tip_total >= WEEKLY_TIP_GOAL_AMOUNT
        if weekly_tip_goal_reached:
            level_priority_points += 0.5
        if not incentives_active:
            level_priority_points = 0.0
            level_priority_bonus = 0.0
            weekly_tip_goal_reached = False
        gamification_badges = _gamification_badges(metrics, week_tip_total)
        if mission_bonus_active:
            gamification_badges.append("Missão da semana")
        if not badges_visible:
            gamification_badges = []
        consistency_streak_days = _consistency_streak_days(walker_walks, current_dt)
        quality_bonus_active = (
            _coerce_float(metrics.get("rating_weighted_avg"), 0.0)
            >= _coerce_float(incentive_settings.get("quality_bonus_min_weighted"), DEFAULT_QUALITY_BONUS_MIN_WEIGHTED)
            and weekly_completed_walks >= int(incentive_settings.get("quality_bonus_min_walks") or DEFAULT_QUALITY_BONUS_MIN_WALKS)
        )
        bonus_active = quality_bonus_active or consistency_streak_days >= int(incentive_settings.get("consistency_days_required") or DEFAULT_CONSISTENCY_DAYS_REQUIRED)
        if not incentives_active:
            quality_bonus_active = False
            bonus_active = False

        if quality_status == QUALITY_STATUS_SUSPENDED:
            score_final = 0.0

        availability_score = 20.0
        availability_label = "Sem horário para a data escolhida"
        available_slots = await _get_available_slots_for_walker(walker_payload, walker_id, selected_date, duration_minutes)
        exact_time_available = bool(normalized_preferred_time and normalized_preferred_time in available_slots)
        selected_slot = ""
        if available_slots:
            availability_score = 80.0
            availability_label = "Disponível agora"
            selected_slot = available_slots[0]
            if normalized_preferred_time:
                if exact_time_available:
                    availability_score = 100.0
                    availability_label = f"Disponível às {normalized_preferred_time}"
                    selected_slot = normalized_preferred_time
                else:
                    preferred_minutes = _clock_to_minutes(normalized_preferred_time)
                    selected_slot = min(available_slots, key=lambda slot: abs(_clock_to_minutes(slot) - preferred_minutes))
                    diffs = [abs(_clock_to_minutes(slot) - preferred_minutes) for slot in available_slots]
                    nearest = min(diffs) if diffs else 9999
                    if nearest <= 30:
                        availability_score = 80.0
                        availability_label = "Horário próximo disponível"
                    elif nearest <= 60:
                        availability_score = 60.0
                        availability_label = "Disponível em até 1h"
                    else:
                        availability_score = 40.0
                        availability_label = "Disponível em outros horários"

        region = str(walker_payload.get("region") or "").strip()
        proximity_score = 60.0
        proximity_label = "Região atendida"
        if normalized_neighborhood:
            if normalized_neighborhood in region.lower():
                proximity_score = 100.0
                proximity_label = "Mesma região"
            elif region:
                proximity_score = 70.0
                proximity_label = "Região próxima"

        load_count = int(today_walk_count.get(walker_id, 0) or 0)
        balance_score = max(40.0, 100.0 - (load_count * 15.0))

        disintermediation_flag_active = False
        if bool(walker_payload.get("flag_suspeita_desintermediacao", False)):
            expires_at = _parse_iso_datetime(walker_payload.get("desintermediacao_flag_expires_at"))
            disintermediation_flag_active = not expires_at or expires_at > current_dt
        disintermediation_penalty_points = DISINTERMEDIATION_MATCH_PENALTY_POINTS if disintermediation_flag_active else 0.0
        behavior_risk_flag_until = _parse_iso_datetime(walker_payload.get("behavior_risk_flag_until"))
        behavior_risk_flag_active = bool(walker_payload.get("behavior_risk_flag_active", False)) and (
            not behavior_risk_flag_until or behavior_risk_flag_until > current_dt
        )
        behavior_risk_penalty_points = 3.0 if behavior_risk_flag_active else 0.0
        total_penalty_points = disintermediation_penalty_points + behavior_risk_penalty_points

        score_distancia_component = _bucket_proximity_score(_coerce_float(proximity_score, 60.0))
        score_disponibilidade_component = _bucket_availability_score(_coerce_float(availability_score, 60.0))
        score_confiabilidade_component = _matching_score_reliability_component(metrics)
        kit_profile = _walker_kit_profile_from_user(walker_payload)
        kit_missing_reports_count = int(walker_payload.get("kit_missing_reports_count", 0) or 0)
        kit_level = _kit_effective_level(kit_profile, kit_missing_reports_count)
        kit_labels = _kit_labels_from_level(kit_level)
        kit_reliability_penalty_points = _kit_reliability_penalty_points(kit_missing_reports_count)
        premium_verified_streak_target = int(
            premium_verified_settings.get("streak_minimo_para_selo") or DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET
        )
        premium_verified_streak = int(walker_payload.get("premium_verified_streak", 0) or 0)
        premium_verified_reason = str(walker_payload.get("premium_verified_last_reason") or "")
        premium_verified_badge_active = bool(
            premium_verified_badge_enabled and bool(walker_payload.get("premium_verified_badge_active", False))
        )
        premium_verified_progress = f"{min(premium_verified_streak, premium_verified_streak_target)}/{premium_verified_streak_target}"
        verification_enabled = _is_feature_active("walker_verification_enabled")
        verification_level_raw = str(walker_payload.get("verification_level") or VERIFICATION_LEVEL_NONE)
        verification_level = verification_level_raw if verification_enabled else VERIFICATION_LEVEL_NONE
        is_verified = bool(verification_enabled and bool(walker_payload.get("is_verified", False)) and verification_level != VERIFICATION_LEVEL_NONE)
        verification_score_snapshot = int(walker_payload.get("verification_score_snapshot", 0) or 0) if verification_enabled else 0
        rating_avg = _coerce_float(metrics.get("rating_avg"), 0.0)
        rating_recent = _coerce_float(metrics.get("rating_recent_avg"), rating_avg)
        severe_delay_rate = _coerce_float(metrics.get("severe_delay_rate"), 0.0)
        no_show_rate = _coerce_float(metrics.get("no_show_rate"), 0.0)

        recent_walks_for_behavior = sorted(walker_walks, key=lambda row: row.get("walk_datetime_iso", ""), reverse=True)[:12]
        recent_failure_count = sum(
            1
            for walk in recent_walks_for_behavior
            if walk.get("status") in {STATUS_NO_SHOW_WALKER, STATUS_NO_SHOW_CLIENT}
            or _derive_occurrence_status(walk) == OCC_LATE_SEVERE
        )
        if recent_failure_count > 0:
            score_confiabilidade_component = _clamp_score(score_confiabilidade_component - min(20.0, recent_failure_count * 4.0))

        if kit_reliability_penalty_points > 0:
            score_confiabilidade_component = _clamp_score(
                score_confiabilidade_component - min(18.0, kit_reliability_penalty_points * 2.0)
            )

        quality_component = _ranking_quality_component(
            metrics,
            quality_status=quality_status,
            badges_count=len(gamification_badges),
        )
        score_base_component = _ranking_score_base(
            quality_component=quality_component,
            reliability_component=score_confiabilidade_component,
            availability_component=score_disponibilidade_component,
        )
        distance_proxy_km = _matching_distance_proxy_km(score_distancia_component)
        proximity_boost_points = _proximity_boost_for_distance(distance_proxy_km)
        ranking_score_without_kit = _clamp_score(score_base_component + proximity_boost_points)
        kit_boost_factor = _kit_boost_factor(kit_level)
        kit_boost_points = _clamp_score(score_base_component * kit_boost_factor) if kit_boost_factor > 0 else 0.0
        ranking_score_final = _clamp_score(ranking_score_without_kit + kit_boost_points)
        within_primary_radius = distance_proxy_km <= MATCH_PRIMARY_RADIUS_KM

        demand_region = normalized_neighborhood or str(region).strip().lower()
        demand_hour_key = f"{selected_date}:{preferred_hour}:{demand_region}" if demand_region and preferred_hour else ""
        regional_hour_demand = regional_hour_load.get(demand_hour_key, 0) if demand_hour_key else 0
        high_demand_context = is_critical_hour_context or regional_hour_demand >= MATCH_HIGH_DEMAND_HOURLY_THRESHOLD
        if high_demand_context and mission_bonus_active:
            mission_priority_points = round(mission_priority_points + 0.5, 2)

        has_recent_negative_behavior = (
            severe_delay_rate >= 12.0
            or (rating_recent > 0 and rating_avg > 0 and rating_recent <= rating_avg - 0.4)
            or disintermediation_flag_active
        )
        level_system_enabled = _is_feature_active("level_system_enabled")

        premium_boost_points = _matching_premium_boost_points(
            quality_status=quality_status,
            score_base_component=score_base_component,
            has_recent_negative_behavior=has_recent_negative_behavior,
            high_demand_context=high_demand_context,
        )
        level_boost_points = _clamp_score(score_base_component * level_priority_bonus) if level_system_enabled else 0.0
        level_priority_points = round(level_boost_points, 2)

        premium_verified_bonus_points = 0.0
        premium_verified_priority_bonus_points = 0.0
        premium_verified_cr_efficiency_multiplier = 1.0
        if premium_verified_badge_active and premium_verified_bonus_enabled:
            premium_verified_cr_efficiency_multiplier = _coerce_float(
                premium_verified_settings.get("cr_efficiency_multiplier"),
                DEFAULT_PREMIUM_VERIFIED_CR_EFFICIENCY_MULTIPLIER,
            )
            if score_base_component >= KIT_BOOST_MIN_SCORE_BASE:
                premium_verified_bonus_points = _coerce_float(
                    premium_verified_settings.get("bonus_score_base"),
                    DEFAULT_PREMIUM_VERIFIED_BONUS_SCORE,
                )
                premium_verified_priority_bonus_points = _coerce_float(
                    premium_verified_settings.get("priority_bonus"),
                    DEFAULT_PREMIUM_VERIFIED_PRIORITY_BONUS,
                )

        premium_boost_points = min(12.0, premium_boost_points * premium_verified_cr_efficiency_multiplier)

        verification_boost_points = 0.0
        verification_priority_bonus_points = 0.0
        if verification_enabled and is_verified and score_base_component >= VERIFICATION_LOW_SCORE_GUARD:
            if verification_level == VERIFICATION_LEVEL_PLUS:
                verification_boost_points = VERIFICATION_PLUS_BOOST_POINTS
            elif verification_level == VERIFICATION_LEVEL_PREMIUM:
                verification_boost_points = VERIFICATION_PREMIUM_BOOST_POINTS
                verification_priority_bonus_points = 0.5

        cr_matching_boost_points = 0.0
        cr_wave_priority_points = 0.0
        cr_visual_exposure_points = 0.0
        cr_matching_boost_active = _is_cr_effect_active_until(walker_payload.get("cr_matching_boost_until"))
        cr_early_wave_active = _is_cr_effect_active_until(walker_payload.get("cr_early_wave_until"))
        cr_visual_highlight_active = _is_cr_effect_active_until(walker_payload.get("cr_visual_highlight_until"))
        if score_base_component >= VERIFICATION_LOW_SCORE_GUARD:
            if cr_matching_boost_active:
                cr_matching_boost_points = _coerce_float(
                    walker_payload.get("cr_matching_boost_points_active"),
                    CR_MATCHING_BOOST_BASE_POINTS,
                )
            if cr_early_wave_active:
                cr_wave_priority_points = _coerce_float(
                    walker_payload.get("cr_early_wave_priority_active"),
                    CR_EARLY_WAVE_BASE_PRIORITY,
                )
            if cr_visual_highlight_active:
                cr_visual_exposure_points = _coerce_float(
                    walker_payload.get("cr_visual_exposure_points_active"),
                    CR_VISUAL_EXPOSURE_BASE_POINTS,
                )

        ranking_score_final = _clamp_score(
            ranking_score_final
            + level_boost_points
            + premium_boost_points
            + premium_verified_bonus_points
            + premium_verified_priority_bonus_points
            + verification_boost_points
            + verification_priority_bonus_points
            + cr_matching_boost_points
            + cr_visual_exposure_points
            + mission_priority_points
        )

        match_score = ranking_score_final

        has_vehicle = bool(walker_payload.get("possuiVeiculo", False))
        accepts_transport = bool(walker_payload.get("aceitaDeslocamentoPremium", False))
        transport_enabled = bool(walker_payload.get("ativoParaTransportePremium", False))
        is_transport_eligible = has_vehicle and accepts_transport and transport_enabled

        status_eligible = quality_status in {QUALITY_STATUS_ACTIVE, QUALITY_STATUS_PREMIUM}
        if normalized_preferred_time:
            is_eligible = status_eligible and exact_time_available
        else:
            is_eligible = status_eligible and availability_score > 20

        if is_transport_request and not is_transport_eligible:
            is_eligible = False

        conversion_base_statuses = {
            STATUS_FINISHED,
            STATUS_SCHEDULED,
            STATUS_GOING_TO_PICKUP,
            STATUS_WALKING_NOW,
            LEGACY_STATUS_IN_PROGRESS,
            STATUS_CANCELED,
            STATUS_NO_SHOW_CLIENT,
            STATUS_NO_SHOW_WALKER,
        }
        conversion_attempts = sum(1 for walk in walker_walks if str(walk.get("status") or "") in conversion_base_statuses)
        conversion_completed = sum(1 for walk in walker_walks if str(walk.get("status") or "") == STATUS_FINISHED)
        conversion_priority_score = _clamp_score((conversion_completed / max(1, conversion_attempts)) * 100.0)

        margin_percent_rows: List[float] = []
        for walk in walker_walks:
            if str(walk.get("status") or "") != STATUS_FINISHED:
                continue
            charged_amount = _coerce_float(walk.get("charged_amount"), 0.0)
            walker_payout_amount = _coerce_float(
                walk.get("walker_payout_amount"),
                _coerce_float(walk.get("walker_payout"), 0.0),
            )
            platform_retained_amount = _coerce_float(
                walk.get("platform_retained_amount"),
                charged_amount - walker_payout_amount,
            )
            if charged_amount <= 0:
                continue
            margin_percent_rows.append(max(0.0, min(100.0, (platform_retained_amount / max(charged_amount, 0.01)) * 100.0)))
        margin_priority_score = _clamp_score(sum(margin_percent_rows) / len(margin_percent_rows)) if margin_percent_rows else 45.0

        capacity_by_period = _normalize_availability_capacity_by_period(walker_payload.get("availability_capacity_by_period"))
        total_capacity = int(sum(max(0, int(value or 0)) for value in capacity_by_period.values()))
        organized_days_count = len(walker_payload.get("availability_days") or [])
        availability_blocks_count = len(walker_payload.get("availability_blocks") or [])
        calendar_priority_score = _clamp_score(
            35.0
            + min(28.0, organized_days_count * 4.0)
            + min(22.0, total_capacity * 3.0)
            + (15.0 if exact_time_available else (10.0 if available_slots else 0.0))
            - min(20.0, availability_blocks_count * 1.8)
        )

        reliability_priority_score = _clamp_score(score_confiabilidade_component)
        ranking_priority_score = _clamp_score(ranking_score_final)
        business_priority_score = _clamp_score(
            (conversion_priority_score * 0.30)
            + (reliability_priority_score * 0.27)
            + (margin_priority_score * 0.18)
            + (calendar_priority_score * 0.15)
            + (ranking_priority_score * 0.10)
        )

        is_preferred_rebooking = bool(preferred_rebooking_walker_id and preferred_rebooking_walker_id == walker_id)
        if is_preferred_rebooking:
            business_priority_score = _clamp_score(business_priority_score + 6.0)

        value_context_labels: List[str] = []
        if is_verified or premium_verified_badge_active or bool(kit_profile.get("kit_complete", False)):
            value_context_labels.append("Passeador verificado")
        if conversion_priority_score >= 82.0 or completed_walks_total >= 20:
            value_context_labels.append("Alta taxa de conclusão")
        if cancel_rate <= 3.0:
            value_context_labels.append("Sem cancelamentos recentes")
        if exact_time_available and "Agenda organizada" not in value_context_labels:
            value_context_labels.append("Agenda organizada")
        value_context_labels = value_context_labels[:3]

        selection_reason = "Disponível para o horário solicitado"
        if quality_status == QUALITY_STATUS_OBSERVATION:
            selection_reason = "Em observação: não entra na elegibilidade principal"
        if quality_status in {QUALITY_STATUS_RESTRICTED, QUALITY_STATUS_SUSPENDED}:
            selection_reason = "Sem elegibilidade para novos passeios no momento"
        if not within_primary_radius:
            selection_reason = "Fora do raio principal (entra apenas no pool expandido da Onda 4)"
        if is_transport_request and not is_transport_eligible:
            selection_reason = "Sem elegibilidade para transporte (veículo/aceite/ativação)"
        elif premium_boost_points >= MATCH_PREMIUM_BOOST_HIGH_DEMAND and quality_status == QUALITY_STATUS_PREMIUM:
            selection_reason = "Premium com boost dinâmico por alta demanda"
        elif premium_boost_points == MATCH_PREMIUM_BOOST_REDUCED and quality_status == QUALITY_STATUS_PREMIUM:
            selection_reason = "Premium com boost reduzido por comportamento recente"

        normalized_level = _normalize_walker_level_value(walker_level)
        if normalized_level == WALKER_LEVEL_GOLD and "Sem elegibilidade" not in selection_reason:
            selection_reason = "Nível Gold com prioridade elevada"
        elif normalized_level == WALKER_LEVEL_SILVER and "Sem elegibilidade" not in selection_reason:
            selection_reason = "Nível Silver com priorização operacional"
        if kit_level >= 3 and "Sem elegibilidade" not in selection_reason:
            selection_reason = "Passeador Premium com kit certificado completo"
        if verification_enabled and is_verified and "Sem elegibilidade" not in selection_reason:
            selection_reason = "Passeador verificado por qualidade operacional"
        elif premium_verified_badge_active and "Sem elegibilidade" not in selection_reason:
            selection_reason = "Passeador Premium Verificado"
        if not highlights_visible and selection_reason.startswith("Nível"):
            selection_reason = "Disponível para o horário solicitado"

        badge = _score_badge_label(score_final)
        if not badge:
            badge = str(metrics.get("public_badge") or "")
        if normalized_level == WALKER_LEVEL_GOLD:
            badge = "🏅 Gold"
        elif normalized_level == WALKER_LEVEL_SILVER and not badge:
            badge = "🥈 Silver"
        if not badges_visible:
            badge = ""

        if badges_visible and kit_level >= 3:
            badge = "Passeador Premium"
        if badges_visible and verification_enabled and is_verified:
            verification_visual = _verification_visual_meta_for_level(verification_level)
            badge = verification_visual["label"]
        elif badges_visible and premium_verified_badge_active:
            badge = PREMIUM_VERIFIED_BADGE_NAME

        walker_payload.update(
            {
                "rating_avg": _coerce_float(metrics.get("rating_avg"), 0.0),
                "rating_recent_avg": _coerce_float(metrics.get("rating_recent_avg"), 0.0),
                "rating_weighted_avg": _coerce_float(metrics.get("rating_weighted_avg"), 0.0),
                "rating_count": int(metrics.get("rating_count", 0) or 0),
                "public_rating_label": str(metrics.get("public_rating_label") or "Novo na plataforma"),
                "public_badge": badge,
                "quality_status": quality_status,
                "score_final": score_final,
                "score_with_bonus": _clamp_score(score_final + mission_bonus_points),
                "score_base_component": score_base_component,
                "score_distancia_component": score_distancia_component,
                "score_confiabilidade_component": score_confiabilidade_component,
                "score_disponibilidade_component": score_disponibilidade_component,
                "premium_boost_points": premium_boost_points,
                "proximity_boost_points": proximity_boost_points,
                "kit_boost_points": kit_boost_points,
                "kit_reliability_penalty_points": kit_reliability_penalty_points,
                "verification_boost_points": verification_boost_points,
                "verification_priority_bonus_points": verification_priority_bonus_points,
                "cr_matching_boost_points": cr_matching_boost_points,
                "cr_wave_priority_points": cr_wave_priority_points,
                "cr_visual_exposure_points": cr_visual_exposure_points,
                "match_score": match_score,
                "ranking_score_without_kit": ranking_score_without_kit,
                "ranking_score_final": ranking_score_final,
                "completed_walks": int(metrics.get("completed_walks", 0) or 0),
                "severe_delay_rate": severe_delay_rate,
                "no_show_rate": no_show_rate,
                "load_balance_score": balance_score,
                "availability_score": availability_score,
                "proximity_score": proximity_score,
                "distance_proxy_km": distance_proxy_km,
                "within_primary_radius": within_primary_radius,
                "has_vehicle": has_vehicle,
                "accepts_pet_transport": accepts_transport,
                "vehicle_type": str(walker_payload.get("tipoVeiculo", "") or ""),
                "transport_enabled": transport_enabled,
                "is_transport_eligible": is_transport_eligible,
                "has_water": bool(kit_profile.get("has_water", False)),
                "has_bowl": bool(kit_profile.get("has_bowl", False)),
                "has_bags": bool(kit_profile.get("has_bags", False)),
                "has_first_aid": bool(kit_profile.get("has_first_aid", False)),
                "has_towel": bool(kit_profile.get("has_towel", False)),
                "has_extra_leash": bool(kit_profile.get("has_extra_leash", False)),
                "has_premium_items": bool(kit_profile.get("has_premium_items", False)),
                "kit_complete": bool(kit_profile.get("kit_complete", False)),
                "kit_basic_complete": bool(kit_profile.get("kit_basic_complete", False)),
                "kit_essential_complete": bool(kit_profile.get("kit_essential_complete", False)),
                "kit_premium": bool(kit_profile.get("kit_premium", False)),
                "kit_level": int(kit_level),
                "kit_labels": kit_labels,
                "kit_photo_urls": [
                    str(item)
                    for item in (walker_payload.get("kit_photo_urls") or [])
                    if isinstance(item, str) and item.startswith("/")
                ][:3],
                "premium_verified_badge_active": premium_verified_badge_active,
                "premium_verified_badge_name": PREMIUM_VERIFIED_BADGE_NAME,
                "premium_verified_badge_subtitle": PREMIUM_VERIFIED_BADGE_SUBTITLE,
                "premium_verified_reason": premium_verified_reason,
                "premium_verified_streak": premium_verified_streak,
                "premium_verified_streak_target": premium_verified_streak_target,
                "premium_verified_progress": premium_verified_progress,
                "premium_verified_bonus_score_applied": premium_verified_bonus_points,
                "premium_verified_priority_bonus_applied": premium_verified_priority_bonus_points,
                "premium_verified_cr_efficiency_multiplier": premium_verified_cr_efficiency_multiplier,
                "is_verified": is_verified,
                "verification_level": verification_level,
                "verification_score_snapshot": verification_score_snapshot,
                "verification_badges": _verification_badges_for_level(verification_level if is_verified else VERIFICATION_LEVEL_NONE),
                "verification_label": _verification_visual_meta_for_level(verification_level if is_verified else VERIFICATION_LEVEL_NONE)["label"],
                "verification_color": _verification_visual_meta_for_level(verification_level if is_verified else VERIFICATION_LEVEL_NONE)["color"],
                "reputation_credits": int(walker_payload.get("reputation_credits", 0) or 0),
                "last_credit_update": walker_payload.get("last_credit_update"),
                "cr_matching_boost_active": cr_matching_boost_active,
                "cr_early_wave_active": cr_early_wave_active,
                "cr_visual_highlight_active": cr_visual_highlight_active,
                "matching_penalty_points": total_penalty_points,
                "bonus_active": bonus_active,
                "walker_level": walker_level,
                "level_priority_bonus": level_priority_bonus,
                "mission_bonus_points": mission_bonus_points,
                "mission_priority_points": mission_priority_points,
                "weekly_tip_total": week_tip_total if tips_active else 0.0,
                "weekly_tip_goal_reached": weekly_tip_goal_reached,
                "gamification_badges": gamification_badges,
                "flag_suspeita_desintermediacao": disintermediation_flag_active,
                "behavior_risk_flag_active": behavior_risk_flag_active,
                "consistency_streak_days": consistency_streak_days,
                "availability_label": availability_label,
                "proximity_label": proximity_label,
                "available_slots": available_slots,
                "selected_slot": selected_slot,
                "is_available_exact_time": exact_time_available,
                "conversion_priority_score": round(conversion_priority_score, 2),
                "reliability_priority_score": round(reliability_priority_score, 2),
                "margin_priority_score": round(margin_priority_score, 2),
                "calendar_priority_score": round(calendar_priority_score, 2),
                "business_priority_score": round(business_priority_score, 2),
                "recommended_label": "",
                "value_context_labels": value_context_labels,
                "is_preferred_rebooking": is_preferred_rebooking,
                "selection_reason": selection_reason,
                "is_eligible_for_matching": is_eligible,
                "high_demand_context": high_demand_context,
                "is_top_match": False,
                "wave_hint": 0,
                "highlight_label": ("Passeador Premium" if highlights_visible and kit_level >= 3 else (badge if highlights_visible else "")),
                "is_premium_featured": highlights_visible
                and (
                    bool(walker_payload.get("premium_override", False))
                    or quality_status == QUALITY_STATUS_PREMIUM
                    or walker_level == WALKER_LEVEL_ELITE
                    or kit_level >= 3
                    or cr_visual_highlight_active
                ),
            }
        )
        ranked_walkers.append(walker_payload)

    best_non_premium_base = max(
        [
            _coerce_float(row.get("score_base_component"), 0.0)
            for row in ranked_walkers
            if str(row.get("quality_status") or "") == QUALITY_STATUS_ACTIVE and bool(row.get("is_eligible_for_matching"))
        ]
        or [0.0]
    )
    if best_non_premium_base > 0:
        for row in ranked_walkers:
            if str(row.get("quality_status") or "") != QUALITY_STATUS_PREMIUM:
                continue
            if not bool(row.get("is_eligible_for_matching")):
                continue
            premium_base = _coerce_float(row.get("score_base_component"), 0.0)
            base_gap = best_non_premium_base - premium_base
            if base_gap < 12.0:
                continue
            fairness_penalty = min(8.0, base_gap * 0.40)
            adjusted_final = _clamp_score(
                _coerce_float(row.get("ranking_score_final"), _coerce_float(row.get("match_score"), 0.0)) - fairness_penalty
            )
            row["ranking_score_final"] = adjusted_final
            row["match_score"] = adjusted_final

    ranked_walkers.sort(
        key=lambda row: (
            0 if row.get("is_eligible_for_matching") else 1,
            0 if row.get("within_primary_radius", False) else 1,
            -_coerce_float(row.get("business_priority_score"), 0.0),
            -_coerce_float(row.get("ranking_score_final"), _coerce_float(row.get("match_score"), 0.0)),
            -_coerce_float(row.get("verification_priority_bonus_points"), 0.0),
            -_coerce_float(row.get("cr_wave_priority_points"), 0.0),
            -_coerce_float(row.get("cr_visual_exposure_points"), 0.0),
            _coerce_float(row.get("distance_proxy_km"), 999.0),
            -_coerce_float(row.get("score_confiabilidade_component"), 0.0),
            -_coerce_float(row.get("score_base_component"), 0.0),
        ),
    )

    if ranked_walkers:
        top_candidate = ranked_walkers[0]
        top_kit_boost = _coerce_float(top_candidate.get("kit_boost_points"), 0.0)
        top_base_for_kit = _coerce_float(top_candidate.get("score_base_component"), 0.0)
        if bool(top_candidate.get("is_eligible_for_matching")) and top_base_for_kit < KIT_BOOST_MIN_SCORE_BASE and top_kit_boost > 0:
            top_candidate["kit_boost_points"] = 0.0
            adjusted_without_kit = _coerce_float(
                top_candidate.get("ranking_score_without_kit"),
                _coerce_float(top_candidate.get("ranking_score_final"), 0.0) - top_kit_boost,
            )
            top_candidate["ranking_score_final"] = adjusted_without_kit
            top_candidate["match_score"] = adjusted_without_kit
            ranked_walkers.sort(
                key=lambda row: (
                    0 if row.get("is_eligible_for_matching") else 1,
                    0 if row.get("within_primary_radius", False) else 1,
                    -_coerce_float(row.get("business_priority_score"), 0.0),
                    -_coerce_float(row.get("ranking_score_final"), _coerce_float(row.get("match_score"), 0.0)),
                    -_coerce_float(row.get("verification_priority_bonus_points"), 0.0),
                    -_coerce_float(row.get("cr_wave_priority_points"), 0.0),
                    -_coerce_float(row.get("cr_visual_exposure_points"), 0.0),
                    _coerce_float(row.get("distance_proxy_km"), 999.0),
                    -_coerce_float(row.get("score_confiabilidade_component"), 0.0),
                    -_coerce_float(row.get("score_base_component"), 0.0),
                ),
            )
            top_candidate = ranked_walkers[0]
        top_base = _coerce_float(top_candidate.get("score_base_component"), 0.0)
        if bool(top_candidate.get("is_eligible_for_matching")) and top_base < MATCH_BEHAVIORAL_LOW_SCORE_BLOCK:
            replacement_index: Optional[int] = None
            for idx, candidate in enumerate(ranked_walkers[1:], start=1):
                if not bool(candidate.get("is_eligible_for_matching")):
                    continue
                candidate_base = _coerce_float(candidate.get("score_base_component"), 0.0)
                if candidate_base >= MATCH_BEHAVIORAL_LOW_SCORE_BLOCK:
                    replacement_index = idx
                    break
            if replacement_index is not None:
                promoted = ranked_walkers.pop(replacement_index)
                ranked_walkers.insert(0, promoted)

    for index, row in enumerate(ranked_walkers):
        row["is_top_match"] = index == 0 and bool(row.get("is_eligible_for_matching"))

    visible_walkers = [
        walker
        for walker in ranked_walkers
        if walker.get("is_eligible_for_matching") and walker.get("quality_status") in {QUALITY_STATUS_ACTIVE, QUALITY_STATUS_PREMIUM}
    ]

    top_conversion_walker_id = ""
    top_reliability_walker_id = ""
    if visible_walkers:
        top_conversion_row = max(visible_walkers, key=lambda item: _coerce_float(item.get("conversion_priority_score"), 0.0))
        top_reliability_row = max(visible_walkers, key=lambda item: _coerce_float(item.get("reliability_priority_score"), 0.0))
        top_conversion_walker_id = str(top_conversion_row.get("id") or "")
        top_reliability_walker_id = str(top_reliability_row.get("id") or "")
        if top_reliability_walker_id == top_conversion_walker_id and len(visible_walkers) > 1:
            sorted_by_reliability = sorted(
                visible_walkers,
                key=lambda item: _coerce_float(item.get("reliability_priority_score"), 0.0),
                reverse=True,
            )
            for candidate in sorted_by_reliability:
                candidate_id = str(candidate.get("id") or "")
                if candidate_id and candidate_id != top_conversion_walker_id:
                    top_reliability_walker_id = candidate_id
                    break

    for index, row in enumerate(visible_walkers):
        row["is_top_match"] = index == 0 and bool(row.get("is_eligible_for_matching"))
        if index == 0:
            row["wave_hint"] = 1
        elif index <= 2:
            row["wave_hint"] = 2
        elif index <= 4:
            row["wave_hint"] = 3
        elif index <= 9:
            row["wave_hint"] = 4
        else:
            row["wave_hint"] = 0

        recommendation_labels: List[str] = []
        if bool(row.get("is_preferred_rebooking", False)):
            recommendation_labels.append("Seu passeador preferido")
        if str(row.get("id") or "") == top_conversion_walker_id:
            recommendation_labels.append("Mais escolhido")
        if str(row.get("id") or "") == top_reliability_walker_id:
            recommendation_labels.append("Mais confiável")

        if not recommendation_labels and index == 0:
            recommendation_labels.append("Recomendado para você")

        row["recommended_label"] = recommendation_labels[0] if recommendation_labels else ""

        if index < 3:
            if index == 0:
                row["selection_reason"] = "Opção recomendada para confirmar mais rápido"
            elif index == 1:
                row["selection_reason"] = "Alternativa confiável para comparar"
            else:
                row["selection_reason"] = "Outra opção com boa disponibilidade"
        else:
            row["selection_reason"] = ""

    for row in visible_walkers:
        if mask_public_identity:
            row["name"] = _mask_public_name(str(row.get("name") or "Passeador"))
        if row.get("quality_status") != QUALITY_STATUS_PREMIUM:
            row["quality_status"] = QUALITY_STATUS_ACTIVE

    critical_supply_count = len(visible_walkers)
    base_price_estimate = _base_walk_price(
        {
            "duration_minutes": duration_minutes,
            "walk_type": WALK_TYPE_SHARED if selected_pets_count > 1 else WALK_TYPE_INDIVIDUAL,
            "selected_pets_count": selected_pets_count,
        }
    )
    previous_multiplier = await _recent_hour_multiplier(selected_date, normalized_preferred_time)
    dynamic_payload = calculateDynamicPrice(
        {
            "basePrice": base_price_estimate,
            "demandLevel": regional_hour_demand,
            "supplyLevel": critical_supply_count,
            "timeSlot": normalized_preferred_time,
            "dayOfWeek": _weekday_key_from_date(selected_date),
            "previousMultiplier": previous_multiplier,
        },
        dynamic_pricing_settings,
    )
    dynamic_price_multiplier = round(
        max(1.0, min(1.0 + DYNAMIC_PRICING_MAX_TOTAL_BOOST, dynamic_payload["dynamic_price"] / max(dynamic_payload["base_price"], 0.01))),
        4,
    )
    dynamic_price_reason = "Horário de alta demanda" if dynamic_payload["dynamic_price"] > dynamic_payload["base_price"] else "Preço padrão"

    expose_dynamic = dynamic_mode == DYNAMIC_PRICING_MODE_ACTIVE
    for row in visible_walkers:
        row["dynamic_price_multiplier"] = dynamic_price_multiplier if expose_dynamic else 1.0
        row["dynamic_price_reason"] = dynamic_price_reason if expose_dynamic else "Preço padrão"

    if viewer_role == "cliente" and normalized_preferred_time and str(viewer_user.get("id") or ""):
        final_price_preview = dynamic_payload["base_price"]
        if dynamic_mode == DYNAMIC_PRICING_MODE_ACTIVE:
            final_price_preview = dynamic_payload["dynamic_price"]
        await _log_dynamic_pricing_attempt(
            user_id=str(viewer_user.get("id") or ""),
            walk_date=selected_date,
            time_slot=normalized_preferred_time,
            day_of_week=_weekday_key_from_date(selected_date),
            base_price=dynamic_payload["base_price"],
            dynamic_price=dynamic_payload["dynamic_price"],
            supply_level=critical_supply_count,
            demand_level=int(regional_hour_demand),
            mode=dynamic_mode,
            final_price=final_price_preview,
        )

    return [WalkerResponse(**walker) for walker in visible_walkers]


def _matching_score_base_component(metrics: dict) -> float:
    rating_avg = _coerce_float(metrics.get("rating_avg"), 0.0)
    rating_recent = _coerce_float(metrics.get("rating_recent_avg"), rating_avg)
    rating_std_dev = _coerce_float(metrics.get("rating_std_dev"), 0.0)

    avg_component = _clamp_score((rating_avg / 5.0) * 100.0)
    recent_component = _clamp_score((rating_recent / 5.0) * 100.0)
    consistency_component = _clamp_score(100.0 - min(100.0, rating_std_dev * 55.0))
    return _clamp_score((recent_component * 0.55) + (avg_component * 0.30) + (consistency_component * 0.15))


def _matching_score_reliability_component(metrics: dict) -> float:
    completion_percent_raw = _coerce_float(metrics.get("completion_percent", metrics.get("completion_rate", 0.0)), 0.0)
    completion_percent = completion_percent_raw if completion_percent_raw > 1 else completion_percent_raw * 100.0
    severe_delay_rate = _coerce_float(metrics.get("severe_delay_rate"), 0.0)
    no_show_rate = _coerce_float(metrics.get("no_show_rate"), 0.0)

    completion_component = _clamp_score(completion_percent)
    delay_component = _clamp_score(100.0 - min(100.0, severe_delay_rate * 2.5))
    no_show_component = _clamp_score(100.0 - min(100.0, no_show_rate * 8.0))
    return _clamp_score((completion_component * 0.50) + (delay_component * 0.25) + (no_show_component * 0.25))


def _matching_premium_boost_points(
    *,
    quality_status: str,
    score_base_component: float,
    has_recent_negative_behavior: bool,
    high_demand_context: bool,
) -> float:
    if quality_status != QUALITY_STATUS_PREMIUM:
        return 0.0
    if score_base_component < MATCH_PREMIUM_BASE_ELIGIBILITY_MIN:
        return 0.0
    if has_recent_negative_behavior:
        return MATCH_PREMIUM_BOOST_REDUCED
    if high_demand_context:
        return MATCH_PREMIUM_BOOST_HIGH_DEMAND
    return MATCH_PREMIUM_BOOST_DEFAULT


def _matching_distance_proxy_km(distance_score: float) -> float:
    return round(max(0.5, ((100.0 - _clamp_score(distance_score)) / 12.0) + 0.5), 2)


def _dynamic_price_multiplier_for_supply(*, is_critical_context: bool, supply_count: int) -> tuple[float, str]:
    if not is_critical_context:
        return 1.0, "Preço padrão"
    if supply_count <= 2:
        return 1.2, "Baixa oferta em horário crítico"
    if supply_count <= 4:
        return 1.1, "Oferta moderada em horário crítico"
    return 1.0, "Preço padrão"


def _matching_final_score(
    score_base_component: float,
    score_distancia_component: float,
    score_confiabilidade_component: float,
    score_disponibilidade_component: float,
    premium_boost_points: float,
    penalty_points: float = 0.0,
) -> float:
    score = (
        (score_base_component * 0.40)
        + (score_distancia_component * 0.25)
        + (score_confiabilidade_component * 0.20)
        + (score_disponibilidade_component * 0.15)
        + premium_boost_points
        - max(0.0, penalty_points)
    )
    return _clamp_score(score)


def _ranking_quality_component(metrics: dict, *, quality_status: str, badges_count: int) -> float:
    rating_avg = _coerce_float(metrics.get("rating_avg"), 0.0)
    rating_recent = _coerce_float(metrics.get("rating_recent_avg"), rating_avg)
    rating_count = max(0.0, _coerce_float(metrics.get("rating_count"), 0.0))

    rating_component = _clamp_score(((rating_recent / 5.0) * 100.0 * 0.6) + ((rating_avg / 5.0) * 100.0 * 0.4))
    reviews_component = _clamp_score(min(100.0, (rating_count / 40.0) * 100.0))
    badge_component = _clamp_score(min(100.0, badges_count * 18.0))
    premium_component = 100.0 if quality_status == QUALITY_STATUS_PREMIUM else 0.0

    return _clamp_score(
        (rating_component * 0.65)
        + (reviews_component * 0.15)
        + (badge_component * 0.10)
        + (premium_component * 0.10)
    )


def _ranking_score_base(
    *,
    quality_component: float,
    reliability_component: float,
    availability_component: float,
) -> float:
    return _clamp_score((quality_component * 0.60) + (reliability_component * 0.25) + (availability_component * 0.15))


def _proximity_boost_for_distance(distance_km: float) -> float:
    if distance_km <= 1.0:
        return 12.0
    if distance_km <= 2.0:
        return 10.0
    if distance_km <= 3.0:
        return 8.0
    if distance_km <= 5.0:
        return 5.0
    if distance_km <= 7.0:
        return 2.0
    return 0.0


def _bucket_proximity_score(raw_score: float) -> float:
    if raw_score >= 95:
        return 100.0
    if raw_score >= 75:
        return 80.0
    if raw_score >= 55:
        return 60.0
    if raw_score >= 35:
        return 40.0
    return 20.0


def _bucket_availability_score(raw_score: float) -> float:
    if raw_score >= 95:
        return 100.0
    if raw_score >= 75:
        return 80.0
    if raw_score >= 55:
        return 60.0
    return 40.0


def _walker_user_id_from_walker_id(walker_id: str) -> str:
    normalized = str(walker_id or "").strip()
    if normalized.startswith("partner-"):
        return normalized.replace("partner-", "", 1)
    return normalized


def _matching_client_message(row: dict) -> str:
    status = str(row.get("status") or "searching")
    if status == "matched":
        seconds = int(row.get("confirmed_in_seconds") or 0)
        return f"Passeador confirmado em {seconds} segundos" if seconds > 0 else "Passeador confirmado"
    if status == "expired":
        return "Não encontramos aceite no tempo esperado. Ajuste horário/região e tente novamente."
    if status == "canceled":
        return "Busca cancelada."
    return "Buscando melhor passeador próximo..."


def _to_matching_response(row: dict) -> MatchingWalkRequestResponse:
    payload = {
        "id": str(row.get("id") or ""),
        "status": str(row.get("status") or "searching"),
        "current_wave": int(row.get("current_wave", 1) or 1),
        "client_message": _matching_client_message(row),
        "selected_walker_user_id": row.get("selected_walker_user_id"),
        "selected_walker_name": row.get("selected_walker_name"),
        "accepted_walk_id": row.get("accepted_walk_id"),
        "confirmed_in_seconds": row.get("confirmed_in_seconds"),
        "rejected_count": int(row.get("rejected_count", 0) or 0),
        "ignored_count": int(row.get("ignored_count", 0) or 0),
        "attempted_count": int(row.get("attempted_count", 0) or 0),
        "selected_position": row.get("selected_position"),
        "min_score_threshold": _coerce_float(row.get("min_score_threshold"), MATCH_MIN_SCORE),
        "fallback_mode": bool(row.get("fallback_mode", False)),
        "marketplace_context": str(row.get("marketplace_context") or MARKETPLACE_CONTEXT_BALANCED),
        "demand_active": int(row.get("demand_active", 0) or 0),
        "supply_active": int(row.get("supply_active", 0) or 0),
        "demand_supply_ratio": _coerce_float(row.get("demand_supply_ratio"), 0.0),
        "dynamic_price_multiplier": _coerce_float(row.get("dynamic_price_multiplier"), 1.0),
        "dynamic_price_reason": str(row.get("dynamic_price_reason") or "Preço padrão"),
        "created_at": str(row.get("created_at") or datetime.now(timezone.utc).isoformat()),
        "updated_at": str(row.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    }
    return MatchingWalkRequestResponse(**payload)


async def _recent_no_show_24h_count(walker_user_id: str, walker_id: str) -> int:
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    query: Dict[str, Any] = {
        "status": STATUS_NO_SHOW_WALKER,
        "walk_datetime_iso": {"$gte": cutoff_iso},
        "$or": [
            {"walker_user_id": walker_user_id},
            {"walker_id": walker_id},
            {"walker_id": f"partner-{walker_user_id}"},
        ],
    }
    return await db.walks.count_documents(query)


async def _walker_active_walk_count(walker_user_id: str, walker_id: str) -> int:
    return await db.walks.count_documents(
        {
            "status": {"$in": list(ACTIVE_WALK_STATUSES)},
            "$or": [
                {"walker_user_id": walker_user_id},
                {"walker_id": walker_id},
                {"walker_id": f"partner-{walker_user_id}"},
            ],
        }
    )


async def _has_unresolved_active_delay(walker_user_id: str, walker_id: str) -> bool:
    count = await db.walks.count_documents(
        {
            "status": {"$in": [STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW, STATUS_PENDING_REVIEW]},
            "occurrence_status": OCC_LATE_SEVERE,
            "occurrence_resolved": False,
            "$or": [
                {"walker_user_id": walker_user_id},
                {"walker_id": walker_id},
                {"walker_id": f"partner-{walker_user_id}"},
            ],
        }
    )
    return count > 0


async def _pending_offer_count_for_walker(walker_user_id: str) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    return await db.walker_requests.count_documents(
        {
            "status": "pending",
            "target_walker_user_id": walker_user_id,
            "respond_until": {"$gte": now_iso},
        }
    )


async def _apply_match_penalty(walker_user_id: str, reason: str) -> None:
    user_row = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if not user_row:
        return

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    current_penalty_until = _parse_iso_datetime(user_row.get("match_penalty_until"))
    current_penalty_points = _coerce_float(user_row.get("match_penalty_points"), 0.0)
    if not current_penalty_until or current_penalty_until <= now_dt:
        current_penalty_points = 0.0

    rejection_streak = int(user_row.get("match_rejection_streak", 0) or 0) + 1
    new_penalty_points = min(10.0, current_penalty_points + MATCH_TEMP_PENALTY_POINTS)
    penalty_until_iso = (now_dt + timedelta(minutes=MATCH_TEMP_PENALTY_MINUTES)).isoformat()

    updates: Dict[str, Any] = {
        "match_penalty_points": new_penalty_points,
        "match_penalty_until": penalty_until_iso,
        "match_rejection_streak": rejection_streak,
        "match_last_penalty_reason": reason,
        "updated_at": now_iso,
    }

    if rejection_streak >= MATCH_COOLDOWN_AFTER_REJECTIONS:
        updates["match_cooldown_until"] = (now_dt + timedelta(minutes=MATCH_COOLDOWN_MINUTES)).isoformat()
        updates["match_rejection_streak"] = 0

    await db.users.update_one({"id": walker_user_id}, {"$set": updates})


async def _reset_match_penalty_on_accept(walker_user_id: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"id": walker_user_id},
        {
            "$set": {
                "match_penalty_points": 0.0,
                "match_penalty_until": None,
                "match_rejection_streak": 0,
                "updated_at": now_iso,
            }
        },
    )


async def _rank_matching_candidates(
    request: Request,
    payload: MatchingWalkCreatePayload,
    min_score_threshold: float,
    runtime_context: Optional[dict] = None,
) -> List[dict]:
    runtime = runtime_context or await _marketplace_runtime_context("", payload.pickup_neighborhood)
    settings = dict(runtime.get("settings") or {})
    metrics = dict(runtime.get("metrics") or {})
    regional_rule = dict(runtime.get("regional_rule") or {})
    context_state = str(runtime.get("context_state") or MARKETPLACE_CONTEXT_BALANCED)

    motor_enabled = _is_feature_active("motor_autonomo_enabled")
    cr_enabled = _is_feature_active("cr_system_enabled")
    dynamic_enabled = _is_feature_active("dynamic_adjustment_enabled")

    tipo_passeio = "transporte" if (
        payload.modo_inicio_passeio == START_MODE_PREMIUM_RELOCATION
        or str(payload.tipo_passeio or "padrao") == "transporte"
    ) else "padrao"
    ranked = await list_walkers(
        request,
        date=payload.walk_date,
        duration_minutes=payload.duration_minutes,
        preferred_time=payload.walk_time,
        neighborhood=payload.pickup_neighborhood,
        tipo_passeio=tipo_passeio,
    )

    now_dt = datetime.now(timezone.utc)
    candidates: List[dict] = []

    for walker in ranked:
        row = walker.model_dump()
        walker_id = str(row.get("id") or "")
        walker_user_id = _walker_user_id_from_walker_id(walker_id)
        if not walker_user_id:
            continue

        quality_status = str(row.get("quality_status") or QUALITY_STATUS_ACTIVE)
        if quality_status not in {QUALITY_STATUS_ACTIVE, QUALITY_STATUS_PREMIUM}:
            continue

        ranking_score_final = _coerce_float(
            row.get("ranking_score_final"),
            _coerce_float(row.get("match_score"), _coerce_float(row.get("score_final"), 0.0)),
        )
        if ranking_score_final < min_score_threshold:
            continue

        if not bool(row.get("is_eligible_for_matching", False)):
            continue

        user_row = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
        if not user_row or user_row.get("isActive", True) is False:
            continue

        await _clear_disintermediation_flag_if_expired(user_row)
        limited_until = _parse_iso_datetime(user_row.get("disintermediation_limited_until"))
        if limited_until and limited_until > now_dt:
            continue

        is_transport_matching = payload.modo_inicio_passeio == START_MODE_PREMIUM_RELOCATION or str(payload.tipo_passeio or "padrao") == "transporte"
        if is_transport_matching:
            if not (
                bool(user_row.get("possuiVeiculo", False))
                and bool(user_row.get("aceitaDeslocamentoPremium", False))
                and bool(user_row.get("ativoParaTransportePremium", False))
            ):
                continue

        cooldown_until = _parse_iso_datetime(user_row.get("match_cooldown_until"))
        if cooldown_until and cooldown_until > now_dt:
            continue

        auto_preselection_suspended_until = _parse_iso_datetime(user_row.get("auto_preselection_suspended_until"))
        if auto_preselection_suspended_until and auto_preselection_suspended_until > now_dt:
            continue

        pending_offer_count = await _pending_offer_count_for_walker(walker_user_id)
        if is_transport_matching:
            max_pending_offers_for_matching = 5
        elif motor_enabled and dynamic_enabled and str(settings.get("mode") or MARKETPLACE_MODE_AUTOMATIC) == MARKETPLACE_MODE_AUTOMATIC:
            if context_state == MARKETPLACE_CONTEXT_CRITICAL:
                max_pending_offers_for_matching = 3
            elif context_state == MARKETPLACE_CONTEXT_BALANCED:
                max_pending_offers_for_matching = 1
            else:
                max_pending_offers_for_matching = 0
        else:
            max_pending_offers_for_matching = 0
        if pending_offer_count > max_pending_offers_for_matching:
            continue

        active_walk_count = await _walker_active_walk_count(walker_user_id, walker_id)
        if is_transport_matching:
            max_active_walks_for_matching = 999
        elif motor_enabled and dynamic_enabled and str(settings.get("mode") or MARKETPLACE_MODE_AUTOMATIC) == MARKETPLACE_MODE_AUTOMATIC:
            if context_state == MARKETPLACE_CONTEXT_CRITICAL:
                max_active_walks_for_matching = 2
            elif context_state == MARKETPLACE_CONTEXT_BALANCED:
                max_active_walks_for_matching = 1
            else:
                max_active_walks_for_matching = 0
        else:
            max_active_walks_for_matching = 0
        if active_walk_count > max_active_walks_for_matching:
            continue

        if await _has_unresolved_active_delay(walker_user_id, walker_id):
            continue

        if await _recent_no_show_24h_count(walker_user_id, walker_id) >= MATCH_NO_SHOW_CRITICAL_24H:
            continue

        score_base_component = _clamp_score(
            _coerce_float(row.get("score_base_component"), _coerce_float(row.get("score_final"), 0.0))
        )
        score_distancia_component = _bucket_proximity_score(
            _coerce_float(row.get("score_distancia_component"), _coerce_float(row.get("proximity_score"), 60.0))
        )
        score_confiabilidade_component = _clamp_score(
            _coerce_float(row.get("score_confiabilidade_component"), _coerce_float(row.get("score_final"), 0.0))
        )
        score_disponibilidade_component = _bucket_availability_score(
            _coerce_float(row.get("score_disponibilidade_component"), _coerce_float(row.get("availability_score"), 60.0))
        )
        premium_boost_points = _coerce_float(row.get("premium_boost_points"), 0.0)
        level_priority_bonus = _coerce_float(row.get("level_priority_bonus"), 0.0)
        if not _is_feature_active("level_system_enabled"):
            level_priority_bonus = 0.0
        level_priority_bonus = max(0.0, min(0.06, level_priority_bonus))
        level_boost_points = _clamp_score(score_base_component * level_priority_bonus) if level_priority_bonus > 0 else 0.0
        proximity_boost_points = _coerce_float(
            row.get("proximity_boost_points"),
            _proximity_boost_for_distance(
                _coerce_float(row.get("distance_proxy_km"), _matching_distance_proxy_km(score_distancia_component))
            ),
        )
        distance_proxy_km = _coerce_float(
            row.get("distance_proxy_km"),
            _matching_distance_proxy_km(score_distancia_component),
        )
        within_primary_radius = bool(row.get("within_primary_radius", distance_proxy_km <= MATCH_PRIMARY_RADIUS_KM))

        daily_load = await db.walks.count_documents(
            {
                "walk_date": payload.walk_date,
                "status": {"$in": list(BLOCKING_WALK_STATUSES)},
                "$or": [
                    {"walker_user_id": walker_user_id},
                    {"walker_id": walker_id},
                    {"walker_id": f"partner-{walker_user_id}"},
                ],
            }
        )
        load_balance_score = max(MATCH_LOAD_SCORE_FLOOR, 100.0 - (daily_load * MATCH_LOAD_PENALTY_PER_WALK))
        base_priority = _clamp_score(score_base_component + proximity_boost_points + premium_boost_points + level_boost_points)

        critical_penalty_override = (
            score_confiabilidade_component < 55.0
            or bool(user_row.get("behavior_risk_flag_active", False))
            or bool(user_row.get("flag_suspeita_desintermediacao", False))
        )

        raw_cr_boost_points = 0.0
        cr_wave_priority_points = 0.0
        if motor_enabled and cr_enabled:
            if _is_cr_effect_active_until(user_row.get("cr_matching_boost_until")):
                raw_cr_boost_points += _coerce_float(user_row.get("cr_matching_boost_points_active"), 5.0)
            if _is_cr_effect_active_until(user_row.get("cr_visual_highlight_until")):
                raw_cr_boost_points += max(0.5, _coerce_float(user_row.get("cr_visual_exposure_points_active"), 2.0))
            if _is_cr_effect_active_until(user_row.get("cr_early_wave_until")):
                cr_wave_priority_points = _coerce_float(user_row.get("cr_early_wave_priority_active"), 1.0)

        cr_multiplier = 1.0
        if motor_enabled and dynamic_enabled and str(settings.get("mode") or MARKETPLACE_MODE_AUTOMATIC) == MARKETPLACE_MODE_AUTOMATIC:
            cr_multiplier = _marketplace_context_cr_multiplier(context_state, settings)

        effective_cr_weight = _marketplace_effective_cr_weight(settings, regional_rule) if motor_enabled else 0.0
        cr_weight_cap_points = base_priority * (effective_cr_weight / 100.0)
        cr_cap_points = min(
            _coerce_float(settings.get("cr_boost_cap_points"), 12.0),
            score_base_component,
            cr_weight_cap_points,
        )
        adjusted_cr_boost = min(raw_cr_boost_points * cr_multiplier, max(0.0, cr_cap_points))
        if critical_penalty_override:
            adjusted_cr_boost = 0.0

        context_adjustment = 0.0
        if motor_enabled and dynamic_enabled and str(settings.get("mode") or MARKETPLACE_MODE_AUTOMATIC) == MARKETPLACE_MODE_AUTOMATIC:
            context_adjustment = _marketplace_context_adjustment_points(
                context_state,
                {
                    "score_base_component": score_base_component,
                    "availability_score": score_disponibilidade_component,
                    "load_balance_score": load_balance_score,
                },
                settings,
                regional_rule,
            )

        final_ranking_score = _clamp_score(
            base_priority
            + (adjusted_cr_boost if (motor_enabled and cr_enabled) else 0.0)
            + (context_adjustment if motor_enabled else 0.0)
        )

        candidates.append(
            {
                "walker_id": walker_id,
                "walker_user_id": walker_user_id,
                "walker_name": str(row.get("name") or "Passeador"),
                "region": str(row.get("region") or ""),
                "score_final": ranking_score_final,
                "score_base_component": score_base_component,
                "score_distancia_component": score_distancia_component,
                "score_confiabilidade_component": score_confiabilidade_component,
                "score_disponibilidade_component": score_disponibilidade_component,
                "premium_boost_points": premium_boost_points,
                "proximity_boost_points": proximity_boost_points,
                "proximity_score": score_distancia_component,
                "availability_score": score_disponibilidade_component,
                "distance_proxy_km": distance_proxy_km,
                "within_primary_radius": within_primary_radius,
                "load_balance_score": load_balance_score,
                "match_score": final_ranking_score,
                "behavioral_match_score": final_ranking_score,
                "ranking_score_final": final_ranking_score,
                "priority_final": final_ranking_score,
                "level_boost_points": round(level_boost_points, 2),
                "mission_priority_points": round(_coerce_float(row.get("mission_priority_points"), 0.0), 2),
                "cr_boost_adjusted": round(adjusted_cr_boost, 2),
                "cr_boost_raw": round(raw_cr_boost_points, 2),
                "cr_wave_priority_points": round(cr_wave_priority_points, 2),
                "context_adjustment_points": round(context_adjustment, 2),
                "marketplace_context_state": context_state,
                "marketplace_mode": str(settings.get("mode") or MARKETPLACE_MODE_AUTOMATIC),
                "dynamic_price_multiplier": _coerce_float(row.get("dynamic_price_multiplier"), 1.0),
                "dynamic_price_reason": str(row.get("dynamic_price_reason") or "Preço padrão"),
                "demand_supply_ratio": _coerce_float(metrics.get("demand_supply_ratio"), 0.0),
                "daily_load": int(daily_load),
            }
        )

    candidates.sort(
        key=lambda item: (
            -_coerce_float(item.get("ranking_score_final"), _coerce_float(item.get("match_score"), 0.0)),
            -_coerce_float(item.get("cr_wave_priority_points"), 0.0),
            _coerce_float(item.get("distance_proxy_km"), 999.0),
            -_coerce_float(item.get("score_confiabilidade_component"), 0.0),
        )
    )

    if candidates:
        top_candidate = candidates[0]
        top_base = _coerce_float(top_candidate.get("score_base_component"), 0.0)
        if top_base < MATCH_BEHAVIORAL_LOW_SCORE_BLOCK:
            replacement_index: Optional[int] = None
            for idx, candidate in enumerate(candidates[1:], start=1):
                candidate_base = _coerce_float(candidate.get("score_base_component"), 0.0)
                if candidate_base >= MATCH_BEHAVIORAL_LOW_SCORE_BLOCK:
                    replacement_index = idx
                    break
            if replacement_index is not None:
                promoted = candidates.pop(replacement_index)
                candidates.insert(0, promoted)

    for idx, item in enumerate(candidates, start=1):
        item["rank_position"] = idx

    return candidates


async def _dispatch_matching_wave(matching_row: dict, wave: int) -> dict:
    if str(matching_row.get("status") or "") != "searching":
        return matching_row

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    respond_until_iso = (now_dt + timedelta(seconds=MATCH_WAVE_TIMEOUT_SECONDS)).isoformat()
    candidates: List[dict] = list(matching_row.get("candidates") or [])
    if not candidates:
        await db.matching_requests.update_one(
            {"id": matching_row["id"]},
            {"$set": {"status": "expired", "updated_at": now_iso}},
        )
        updated = await db.matching_requests.find_one({"id": matching_row["id"]}, {"_id": 0})
        return updated or matching_row

    top_limit = int(matching_row.get("top_limit", MATCH_TOP_WAVE4_MAX) or MATCH_TOP_WAVE4_MAX)
    top_limit = max(1, min(MATCH_TOP_WAVE4_MAX, top_limit))
    context_state = str(matching_row.get("marketplace_context") or MARKETPLACE_CONTEXT_BALANCED)
    wave_extra = int(matching_row.get("context_wave_extra_candidates", 0) or 0)
    wave_reduction = int(matching_row.get("context_wave_reduction", 0) or 0)
    primary_candidates = [
        candidate
        for candidate in candidates
        if bool(candidate.get("within_primary_radius", _coerce_float(candidate.get("distance_proxy_km"), 999.0) <= MATCH_PRIMARY_RADIUS_KM))
    ]
    fallback_candidates = [
        candidate
        for candidate in candidates
        if not bool(candidate.get("within_primary_radius", _coerce_float(candidate.get("distance_proxy_km"), 999.0) <= MATCH_PRIMARY_RADIUS_KM))
    ]

    if wave == 1:
        wave_candidates = primary_candidates[:1]
    elif wave == 2:
        base_start = 1
        base_end = 3 + (wave_extra if context_state == MARKETPLACE_CONTEXT_CRITICAL else 0)
        wave_candidates = primary_candidates[base_start:base_end]
    elif wave == 3:
        base_start = 3 + (wave_extra if context_state == MARKETPLACE_CONTEXT_CRITICAL else 0)
        base_end = 5 + (2 * wave_extra if context_state == MARKETPLACE_CONTEXT_CRITICAL else 0)
        wave_candidates = primary_candidates[base_start:base_end]
    else:
        start_index = 5 + (2 * wave_extra if context_state == MARKETPLACE_CONTEXT_CRITICAL else 0)
        if context_state == MARKETPLACE_CONTEXT_SURPLUS and wave_reduction > 0:
            top_limit = max(1, top_limit - wave_reduction)
        expanded_primary = primary_candidates[start_index:top_limit]
        wave_candidates = (expanded_primary + fallback_candidates)[:top_limit]

    is_transport_matching = (
        str(matching_row.get("tipo_passeio") or "padrao") == "transporte"
        or str(matching_row.get("modo_inicio_passeio") or "") == START_MODE_PREMIUM_RELOCATION
    )

    created_count = 0
    for candidate in wave_candidates:
        walker_user_id = str(candidate.get("walker_user_id") or "")
        if not walker_user_id:
            continue

        pending_offer_count = await _pending_offer_count_for_walker(walker_user_id)
        max_pending_offers_for_matching = 5 if is_transport_matching else 0
        if pending_offer_count > max_pending_offers_for_matching:
            continue

        existing = await db.walker_requests.find_one(
            {
                "matching_request_id": matching_row["id"],
                "target_walker_user_id": walker_user_id,
            },
            {"_id": 0},
        )
        if existing:
            continue

        request_doc = {
            "id": str(uuid.uuid4()),
            "matching_request_id": matching_row["id"],
            "wave": wave,
            "rank_position": int(candidate.get("rank_position", 0) or 0),
            "match_score": _coerce_float(candidate.get("match_score"), 0.0),
            "behavioral_match_score": _coerce_float(candidate.get("behavioral_match_score"), 0.0),
            "score_final": _coerce_float(candidate.get("score_final"), 0.0),
            "score_base_component": _coerce_float(candidate.get("score_base_component"), 0.0),
            "score_distancia_component": _coerce_float(candidate.get("score_distancia_component"), 0.0),
            "score_confiabilidade_component": _coerce_float(candidate.get("score_confiabilidade_component"), 0.0),
            "score_disponibilidade_component": _coerce_float(candidate.get("score_disponibilidade_component"), 0.0),
            "premium_boost_points": _coerce_float(candidate.get("premium_boost_points"), 0.0),
            "proximity_score": _coerce_float(candidate.get("proximity_score"), 0.0),
            "availability_score": _coerce_float(candidate.get("availability_score"), 0.0),
            "load_balance_score": _coerce_float(candidate.get("load_balance_score"), 0.0),
            "priority_final": _coerce_float(candidate.get("priority_final"), _coerce_float(candidate.get("match_score"), 0.0)),
            "cr_boost_adjusted": _coerce_float(candidate.get("cr_boost_adjusted"), 0.0),
            "cr_boost_raw": _coerce_float(candidate.get("cr_boost_raw"), 0.0),
            "context_adjustment_points": _coerce_float(candidate.get("context_adjustment_points"), 0.0),
            "marketplace_context_state": str(candidate.get("marketplace_context_state") or context_state),
            "pet_name": matching_row.get("pet_name", "Pet"),
            "client_name": matching_row.get("client_name", "Cliente"),
            "client_user_id": matching_row.get("client_user_id"),
            "neighborhood": matching_row.get("pickup_neighborhood", ""),
            "approx_location": matching_row.get("location_reference", ""),
            "walk_date": matching_row.get("walk_date"),
            "walk_time": matching_row.get("walk_time"),
            "duration_minutes": matching_row.get("duration_minutes", 45),
            "walk_type": matching_row.get("walk_type", WALK_TYPE_INDIVIDUAL),
            "base_price": _coerce_float(matching_row.get("base_price"), 0.0),
            "total_price": _coerce_float(matching_row.get("base_price"), 0.0),
            "region": str(candidate.get("region") or ""),
            "status": "pending",
            "target_walker_user_id": walker_user_id,
            "respond_until": respond_until_iso,
            "created_at": now_iso,
            "updated_at": now_iso,
            "pickup_street": matching_row.get("pickup_street", ""),
            "pickup_number": matching_row.get("pickup_number", ""),
            "pickup_neighborhood": matching_row.get("pickup_neighborhood", ""),
            "pickup_complement": matching_row.get("pickup_complement", ""),
            "location_reference": matching_row.get("location_reference", ""),
            "pet_behavior_notes": matching_row.get("pet_behavior_notes", ""),
            "notes": matching_row.get("notes", ""),
            "pet_id": matching_row.get("pet_id"),
            "second_pet_id": matching_row.get("second_pet_id"),
            "tipo_passeio": matching_row.get("tipo_passeio", "padrao"),
            "modo_inicio_passeio": matching_row.get("modo_inicio_passeio", START_MODE_TUTOR_ADDRESS),
            "usar_ponto_retirada_alternativo": bool(matching_row.get("usar_ponto_retirada_alternativo", False)),
            "ponto_retirada_alternativo_nome": matching_row.get("ponto_retirada_alternativo_nome", ""),
            "ponto_retirada_alternativo_referencia": matching_row.get("ponto_retirada_alternativo_referencia", ""),
            "ponto_encontro_nome": matching_row.get("ponto_encontro_nome", ""),
            "ponto_encontro_referencia": matching_row.get("ponto_encontro_referencia", ""),
            "local_destino_nome": matching_row.get("local_destino_nome", ""),
            "local_destino_referencia": matching_row.get("local_destino_referencia", ""),
        }
        await db.walker_requests.insert_one(request_doc)
        created_count += 1

    update_fields: Dict[str, Any] = {
        "current_wave": wave,
        "wave_expires_at": respond_until_iso,
        "updated_at": now_iso,
    }
    if created_count == 0 and wave >= 4:
        update_fields["status"] = "expired"

    await db.matching_requests.update_one(
        {"id": matching_row["id"]},
        {
            "$set": update_fields,
            "$inc": {"attempted_count": created_count},
        },
    )

    updated = await db.matching_requests.find_one({"id": matching_row["id"]}, {"_id": 0})
    return updated or matching_row


async def _expire_overdue_offers_and_penalize(matching_request_id: str) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    overdue = await db.walker_requests.find(
        {
            "matching_request_id": matching_request_id,
            "status": "pending",
            "respond_until": {"$lt": now_iso},
        },
        {"_id": 0},
    ).to_list(100)

    if not overdue:
        return 0

    for row in overdue:
        walker_user_id = str(row.get("target_walker_user_id") or "")
        if walker_user_id:
            await _apply_match_penalty(walker_user_id, "ignore")

    overdue_ids = [str(row.get("id") or "") for row in overdue if str(row.get("id") or "")]
    if overdue_ids:
        await db.walker_requests.update_many(
            {"id": {"$in": overdue_ids}, "status": "pending"},
            {"$set": {"status": "ignored", "updated_at": now_iso}},
        )
    return len(overdue)


async def _advance_matching_request_state(matching_row: dict) -> dict:
    status = str(matching_row.get("status") or "searching")
    if status != "searching":
        return matching_row

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    matching_id = str(matching_row.get("id") or "")
    if not matching_id:
        return matching_row

    ignored_now = await _expire_overdue_offers_and_penalize(matching_id)
    if ignored_now > 0:
        await db.matching_requests.update_one(
            {"id": matching_id},
            {"$inc": {"ignored_count": ignored_now}, "$set": {"updated_at": now_iso}},
        )

    refreshed = await db.matching_requests.find_one({"id": matching_id}, {"_id": 0}) or matching_row
    if str(refreshed.get("status") or "") != "searching":
        return refreshed

    pending_count = await db.walker_requests.count_documents(
        {"matching_request_id": matching_id, "status": "pending"}
    )
    wave = int(refreshed.get("current_wave", 0) or 0)
    wave_expires_at = _parse_iso_datetime(refreshed.get("wave_expires_at"))

    if pending_count == 0:
        if wave == 0:
            refreshed = await _dispatch_matching_wave(refreshed, 1)
        elif wave == 1:
            refreshed = await _dispatch_matching_wave(refreshed, 2)
        elif wave == 2:
            refreshed = await _dispatch_matching_wave(refreshed, 3)
        elif wave == 3:
            refreshed = await _dispatch_matching_wave(refreshed, 4)
        else:
            await db.matching_requests.update_one(
                {"id": matching_id},
                {"$set": {"status": "expired", "updated_at": now_iso}},
            )
            refreshed = await db.matching_requests.find_one({"id": matching_id}, {"_id": 0}) or refreshed
    elif wave_expires_at and wave_expires_at <= now_dt:
        if wave == 1:
            refreshed = await _dispatch_matching_wave(refreshed, 2)
        elif wave == 2:
            refreshed = await _dispatch_matching_wave(refreshed, 3)
        elif wave == 3:
            refreshed = await _dispatch_matching_wave(refreshed, 4)
        elif wave >= 4:
            await db.matching_requests.update_one(
                {"id": matching_id},
                {"$set": {"status": "expired", "updated_at": now_iso}},
            )
            refreshed = await db.matching_requests.find_one({"id": matching_id}, {"_id": 0}) or refreshed

    return refreshed


@api_router.post("/walks/matching-request", response_model=MatchingWalkRequestResponse, status_code=201)
async def create_matching_walk_request(payload: MatchingWalkCreatePayload, request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    _validate_datetime_iso(payload.walk_date, payload.walk_time)
    is_transport_walk = str(payload.tipo_passeio or "padrao") == "transporte" or payload.modo_inicio_passeio == START_MODE_PREMIUM_RELOCATION
    if is_transport_walk and not await _is_pet_transport_available_for_user(user):
        raise HTTPException(status_code=400, detail="Passeio com transporte está desativado para sua conta")
    if is_transport_walk and not payload.local_destino_nome.strip():
        raise HTTPException(status_code=400, detail="Informe o destino do passeio com transporte")
    if str(user.get("id") or ""):
        await _evaluate_and_apply_disintermediation_flag(str(user.get("id") or ""))

    request_city = str(user.get("city") or user.get("region") or "").strip()
    request_neighborhood = payload.pickup_neighborhood.strip()
    runtime_context = await _marketplace_runtime_context(request_city, request_neighborhood)
    settings = dict(runtime_context.get("settings") or {})
    metrics = dict(runtime_context.get("metrics") or {})
    context_state = str(runtime_context.get("context_state") or MARKETPLACE_CONTEXT_BALANCED)
    dynamic_pricing_settings = await _load_dynamic_pricing_settings()
    dynamic_mode = str(dynamic_pricing_settings.get("dynamicPricingMode") or DYNAMIC_PRICING_MODE_OFF)
    if not bool(dynamic_pricing_settings.get("dynamicPricingEnabled", False)):
        dynamic_mode = DYNAMIC_PRICING_MODE_OFF
    motor_enabled = _is_feature_active("motor_autonomo_enabled")
    dynamic_enabled = _is_feature_active("dynamic_adjustment_enabled")
    mode = str(settings.get("mode") or MARKETPLACE_MODE_AUTOMATIC)

    min_score_threshold = MATCH_MIN_SCORE
    if motor_enabled and dynamic_enabled and mode == MARKETPLACE_MODE_AUTOMATIC:
        min_score_threshold = _marketplace_adjust_min_score_threshold(MATCH_MIN_SCORE, context_state, settings)

    candidates = await _rank_matching_candidates(request, payload, min_score_threshold, runtime_context)
    fallback_mode = False

    if len(candidates) < 3:
        fallback_mode = True
        fallback_threshold = MATCH_FALLBACK_MIN_SCORE
        if motor_enabled and dynamic_enabled and mode == MARKETPLACE_MODE_AUTOMATIC:
            fallback_threshold = _marketplace_adjust_min_score_threshold(MATCH_FALLBACK_MIN_SCORE, context_state, settings)
        min_score_threshold = fallback_threshold
        candidates = await _rank_matching_candidates(request, payload, fallback_threshold, runtime_context)

    emergency_relaxed_mode = False
    if not candidates and motor_enabled and dynamic_enabled and mode == MARKETPLACE_MODE_AUTOMATIC:
        emergency_relaxed_mode = True
        fallback_mode = True
        context_state = MARKETPLACE_CONTEXT_CRITICAL
        min_score_threshold = MATCH_FALLBACK_MIN_SCORE
        emergency_runtime_context = {
            **runtime_context,
            "context_state": MARKETPLACE_CONTEXT_CRITICAL,
        }
        candidates = await _rank_matching_candidates(request, payload, MATCH_FALLBACK_MIN_SCORE, emergency_runtime_context)

    if not candidates:
        raise HTTPException(status_code=404, detail="Nenhum passeador elegível encontrado para este horário")

    owner_user_id = user.get("id") if not _is_admin_user(user) else None
    previous_multiplier = await _recent_hour_multiplier(payload.walk_date, payload.walk_time)
    dynamic_calc = calculateDynamicPrice(
        {
            "basePrice": _base_walk_price(
                {
                    "duration_minutes": payload.duration_minutes,
                    "walk_type": payload.walk_type,
                    "selected_pets_count": 1,
                }
            ),
            "demandLevel": int(max(_coerce_float(metrics.get("hour_demand"), 0.0), _coerce_float(metrics.get("demand_active"), 0.0))),
            "supplyLevel": len(candidates),
            "timeSlot": payload.walk_time,
            "dayOfWeek": _weekday_key_from_date(payload.walk_date),
            "previousMultiplier": previous_multiplier,
        },
        dynamic_pricing_settings,
    )

    if owner_user_id and dynamic_mode == DYNAMIC_PRICING_MODE_ACTIVE:
        latest_preview = await _get_latest_dynamic_pricing_preview(
            user_id=str(owner_user_id),
            walk_date=payload.walk_date,
            walk_time=payload.walk_time,
        )
        if latest_preview:
            preview_base = round(max(0.0, _coerce_float(latest_preview.get("base_price"), 0.0)), 2)
            preview_dynamic = round(
                max(preview_base, _coerce_float(latest_preview.get("dynamic_price_calculated"), preview_base)),
                2,
            )
            if preview_base > 0 and abs(preview_base - dynamic_calc["base_price"]) <= 0.05:
                guardrail_cap = round(
                    min(40.0, max(25.0, _coerce_float(dynamic_calc.get("guardrail_price_cap"), 40.0))),
                    2,
                )
                preview_dynamic = round(min(guardrail_cap, preview_dynamic), 2)
                dynamic_calc["dynamic_price"] = preview_dynamic
                dynamic_calc["multiplier"] = round(
                    max(
                        1.0,
                        min(
                            1.0 + DYNAMIC_PRICING_MAX_TOTAL_BOOST,
                            preview_dynamic / max(dynamic_calc["base_price"], 0.01),
                        ),
                    ),
                    4,
                )
                dynamic_calc["difference_percent"] = round(
                    ((dynamic_calc["dynamic_price"] - dynamic_calc["base_price"]) / max(dynamic_calc["base_price"], 0.01)) * 100.0,
                    2,
                )
    dynamic_price_multiplier = 1.0
    dynamic_price_reason = "Preço padrão"
    if dynamic_mode == DYNAMIC_PRICING_MODE_ACTIVE and dynamic_calc["dynamic_price"] > dynamic_calc["base_price"]:
        dynamic_price_multiplier = round(dynamic_calc["dynamic_price"] / max(dynamic_calc["base_price"], 0.01), 4)
        dynamic_price_reason = "Horário de alta demanda"

    base_top_limit = max(1, min(MATCH_TOP_WAVE4_MAX, len(candidates)))
    top_limit = (
        _marketplace_adjust_top_limit(base_top_limit, len(candidates), context_state, settings)
        if motor_enabled and dynamic_enabled and mode == MARKETPLACE_MODE_AUTOMATIC
        else base_top_limit
    )
    context_wave_extra = (
        int(settings.get("low_supply_wave_extra_candidates") or 0)
        if context_state == MARKETPLACE_CONTEXT_CRITICAL and motor_enabled and dynamic_enabled and mode == MARKETPLACE_MODE_AUTOMATIC
        else 0
    )
    context_wave_reduction = (
        int(settings.get("high_supply_wave_reduction") or 0)
        if context_state == MARKETPLACE_CONTEXT_SURPLUS and motor_enabled and dynamic_enabled and mode == MARKETPLACE_MODE_AUTOMATIC
        else 0
    )
    direct_send_all = False

    now_iso = datetime.now(timezone.utc).isoformat()

    matching_row = {
        "id": str(uuid.uuid4()),
        "status": "searching",
        "current_wave": 0,
        "wave_expires_at": None,
        "requested_by_user_id": user.get("id"),
        "client_user_id": owner_user_id,
        "client_name": payload.client_name.strip() or user.get("full_name", "Cliente"),
        "pet_name": payload.pet_name.strip(),
        "pet_id": payload.pet_id,
        "second_pet_id": payload.second_pet_id,
        "walk_type": payload.walk_type,
        "walk_date": payload.walk_date,
        "walk_time": payload.walk_time,
        "duration_minutes": payload.duration_minutes,
        "pickup_street": payload.pickup_street.strip(),
        "pickup_number": payload.pickup_number.strip(),
        "pickup_neighborhood": payload.pickup_neighborhood.strip(),
        "pickup_city": request_city,
        "pickup_city_normalized": _normalize_marketplace_region(request_city, request_neighborhood)[0],
        "pickup_neighborhood_normalized": _normalize_marketplace_region(request_city, request_neighborhood)[1],
        "pickup_complement": payload.pickup_complement.strip(),
        "location_reference": payload.location_reference.strip() or payload.pickup_neighborhood.strip(),
        "pet_behavior_notes": payload.pet_behavior_notes.strip(),
        "notes": payload.notes.strip(),
        "usar_ponto_retirada_alternativo": bool(payload.usar_ponto_retirada_alternativo),
        "ponto_retirada_alternativo_nome": payload.ponto_retirada_alternativo_nome.strip(),
        "ponto_retirada_alternativo_referencia": payload.ponto_retirada_alternativo_referencia.strip(),
        "ponto_encontro_nome": payload.ponto_encontro_nome.strip(),
        "ponto_encontro_referencia": payload.ponto_encontro_referencia.strip(),
        "local_destino_nome": payload.local_destino_nome.strip(),
        "local_destino_referencia": payload.local_destino_referencia.strip(),
        "modo_inicio_passeio": payload.modo_inicio_passeio,
        "tipo_passeio": "transporte" if is_transport_walk else "padrao",
        "min_score_threshold": min_score_threshold,
        "fallback_mode": fallback_mode,
        "top_limit": top_limit,
        "emergency_relaxed_mode": emergency_relaxed_mode,
        "marketplace_mode": mode,
        "marketplace_context": context_state,
        "demand_active": int(metrics.get("demand_active", 0) or 0),
        "supply_active": int(metrics.get("supply_active", 0) or 0),
        "demand_supply_ratio": _coerce_float(metrics.get("demand_supply_ratio"), 0.0),
        "match_rate": _coerce_float(metrics.get("match_rate"), 0.0),
        "average_acceptance_seconds": _coerce_float(metrics.get("average_acceptance_seconds"), 0.0),
        "cancel_rate": _coerce_float(metrics.get("cancel_rate"), 0.0),
        "cr_usage_24h": int(metrics.get("cr_usage_24h", 0) or 0),
        "context_wave_extra_candidates": context_wave_extra,
        "context_wave_reduction": context_wave_reduction,
        "direct_send_all": direct_send_all,
        "dynamic_pricing_mode": dynamic_mode,
        "dynamic_price_multiplier": dynamic_price_multiplier,
        "dynamic_price_reason": dynamic_price_reason,
        "dynamic_price_calculated": dynamic_calc["dynamic_price"],
        "dynamic_price_difference_percent": dynamic_calc["difference_percent"],
        "candidates": candidates[:top_limit],
        "attempted_count": 0,
        "rejected_count": 0,
        "ignored_count": 0,
        "selected_position": None,
        "selected_walker_user_id": None,
        "selected_walker_name": None,
        "accepted_walk_id": None,
        "confirmed_in_seconds": None,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    await db.matching_requests.insert_one(matching_row)
    if owner_user_id:
        final_price_preview = dynamic_calc["base_price"] if dynamic_mode != DYNAMIC_PRICING_MODE_ACTIVE else dynamic_calc["dynamic_price"]
        await _log_dynamic_pricing_attempt(
            user_id=str(owner_user_id),
            walk_date=payload.walk_date,
            time_slot=payload.walk_time,
            day_of_week=_weekday_key_from_date(payload.walk_date),
            base_price=dynamic_calc["base_price"],
            dynamic_price=dynamic_calc["dynamic_price"],
            supply_level=len(candidates),
            demand_level=int(max(_coerce_float(metrics.get("hour_demand"), 0.0), _coerce_float(metrics.get("demand_active"), 0.0))),
            mode=dynamic_mode,
            final_price=final_price_preview,
        )
    await _log_marketplace_decision_audit(
        {
            "request_id": matching_row["id"],
            "city": matching_row.get("pickup_city_normalized", ""),
            "neighborhood": matching_row.get("pickup_neighborhood_normalized", ""),
            "context_state": context_state,
            "mode": mode,
            "min_score_threshold": _coerce_float(min_score_threshold, MATCH_MIN_SCORE),
            "top_limit": int(top_limit),
            "demand_active": int(metrics.get("demand_active", 0) or 0),
            "supply_active": int(metrics.get("supply_active", 0) or 0),
            "demand_supply_ratio": _coerce_float(metrics.get("demand_supply_ratio"), 0.0),
            "match_rate": _coerce_float(metrics.get("match_rate"), 0.0),
            "average_acceptance_seconds": _coerce_float(metrics.get("average_acceptance_seconds"), 0.0),
            "cancel_rate": _coerce_float(metrics.get("cancel_rate"), 0.0),
            "cr_usage_24h": int(metrics.get("cr_usage_24h", 0) or 0),
            "selected_candidates_preview": [
                {
                    "walker_user_id": str(candidate.get("walker_user_id") or ""),
                    "rank_position": int(candidate.get("rank_position", 0) or 0),
                    "priority_final": _coerce_float(candidate.get("priority_final"), 0.0),
                    "score_base_component": _coerce_float(candidate.get("score_base_component"), 0.0),
                    "cr_boost_adjusted": _coerce_float(candidate.get("cr_boost_adjusted"), 0.0),
                    "context_adjustment_points": _coerce_float(candidate.get("context_adjustment_points"), 0.0),
                }
                for candidate in candidates[:5]
            ],
        }
    )
    dispatched = await _dispatch_matching_wave(matching_row, 1)
    return _to_matching_response(dispatched)


@api_router.get("/walks/matching-request/{matching_request_id}", response_model=MatchingWalkRequestResponse)
async def get_matching_walk_request_status(matching_request_id: str, request: Request):
    user = await _require_role(request, ["cliente", "admin", "super_admin"])
    query: Dict[str, Any] = {"id": matching_request_id}
    if not _is_admin_user(user):
        query["requested_by_user_id"] = user.get("id")

    matching_row = await db.matching_requests.find_one(query, {"_id": 0})
    if not matching_row:
        raise HTTPException(status_code=404, detail="Solicitação de matching não encontrada")

    refreshed = await _advance_matching_request_state(matching_row)
    return _to_matching_response(refreshed)


@api_router.post("/walks/matching-request/{matching_request_id}/cancel", response_model=MatchingWalkRequestResponse)
async def cancel_matching_walk_request(matching_request_id: str, request: Request):
    user = await _require_role(request, ["cliente", "admin", "super_admin"])
    query: Dict[str, Any] = {"id": matching_request_id}
    if not _is_admin_user(user):
        query["requested_by_user_id"] = user.get("id")

    matching_row = await db.matching_requests.find_one(query, {"_id": 0})
    if not matching_row:
        raise HTTPException(status_code=404, detail="Solicitação de matching não encontrada")

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.matching_requests.update_one(
        {"id": matching_request_id, "status": "searching"},
        {"$set": {"status": "canceled", "updated_at": now_iso}},
    )
    await db.walker_requests.update_many(
        {"matching_request_id": matching_request_id, "status": "pending"},
        {"$set": {"status": "canceled", "updated_at": now_iso}},
    )

    updated = await db.matching_requests.find_one({"id": matching_request_id}, {"_id": 0})
    if not updated:
        raise HTTPException(status_code=404, detail="Solicitação de matching não encontrada")
    return _to_matching_response(updated)


@api_router.post("/chat/protected/send", response_model=ProtectedChatSendResponse)
async def send_protected_chat_message(payload: ProtectedChatMessageCreatePayload, request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin", "super_admin"])
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    reasons = _detect_contact_attempt_patterns(payload.message)

    if reasons:
        debounce_cutoff_iso = (now_dt - timedelta(minutes=DISINTERMEDIATION_CONTACT_DEBOUNCE_MINUTES)).isoformat()
        recent_same_conversation = await db.anti_disintermediation_events.find_one(
            {
                "event_type": "CONTACT_ATTEMPT",
                "user_id": user.get("id"),
                "conversation_id": payload.conversation_id,
                "created_at": {"$gte": debounce_cutoff_iso},
            },
            {"_id": 0, "id": 1},
        )
        counted_for_threshold = recent_same_conversation is None
        await db.anti_disintermediation_events.insert_one(
            {
                "id": str(uuid.uuid4()),
                "event_type": "CONTACT_ATTEMPT",
                "user_id": user.get("id"),
                "role": user.get("role"),
                "conversation_id": payload.conversation_id,
                "message_excerpt": payload.message[:180],
                "block_reasons": reasons,
                "counted_for_threshold": counted_for_threshold,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
        await _evaluate_and_apply_disintermediation_flag(str(user.get("id") or ""))
        return ProtectedChatSendResponse(
            sent=False,
            blocked=True,
            warning_message="Para sua segurança, mantenha a comunicação dentro do app 💚",
            message=None,
        )

    message_id = str(uuid.uuid4())
    message_doc = {
        "id": message_id,
        "conversation_id": payload.conversation_id,
        "sender_user_id": str(user.get("id") or ""),
        "sender_role": str(user.get("role") or ""),
        "message": payload.message.strip(),
        "blocked": False,
        "block_reasons": [],
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await db.protected_chat_messages.insert_one(message_doc)
    return ProtectedChatSendResponse(sent=True, blocked=False, warning_message=None, message=ProtectedChatMessageResponse(**message_doc))


@api_router.get("/chat/protected/{conversation_id}", response_model=List[ProtectedChatMessageResponse])
async def list_protected_chat_messages(conversation_id: str, request: Request):
    await _require_role(request, ["cliente", "passeador", "admin", "super_admin"])
    rows = await db.protected_chat_messages.find(
        {"conversation_id": conversation_id},
        {"_id": 0},
    ).sort("created_at", 1).to_list(500)
    return [ProtectedChatMessageResponse(**row) for row in rows]


@api_router.get("/admin/disintermediation/overview", response_model=AdminDisintermediationOverviewResponse)
async def get_admin_disintermediation_overview(request: Request):
    await _require_role(request, ["admin", "super_admin"])

    now_dt = datetime.now(timezone.utc)
    attempts_cutoff_iso = (now_dt - timedelta(days=DISINTERMEDIATION_CONTACT_WINDOW_DAYS)).isoformat()
    recent_events = await db.anti_disintermediation_events.find(
        {
            "event_type": "CONTACT_ATTEMPT",
            "created_at": {"$gte": attempts_cutoff_iso},
        },
        {"_id": 0, "user_id": 1, "counted_for_threshold": 1},
    ).to_list(1000)

    recent_user_ids = {str(row.get("user_id") or "") for row in recent_events if str(row.get("user_id") or "")}
    flagged_users = await db.users.find({"flag_suspeita_desintermediacao": True}, {"_id": 0}).to_list(500)
    flagged_user_ids = {str(row.get("id") or "") for row in flagged_users if str(row.get("id") or "")}
    all_ids = list(recent_user_ids.union(flagged_user_ids))

    users = await db.users.find({"id": {"$in": all_ids}}, {"_id": 0}).to_list(1000) if all_ids else []
    user_map = {str(row.get("id") or ""): row for row in users}

    response_rows: List[AdminDisintermediationUserResponse] = []
    for user_id in all_ids:
        row = user_map.get(user_id)
        if not row:
            continue
        await _clear_disintermediation_flag_if_expired(row)
        contact_attempts = len(
            [
                event
                for event in recent_events
                if str(event.get("user_id") or "") == user_id and bool(event.get("counted_for_threshold", False))
            ]
        )
        cancel_rate = await _post_match_cancel_rate_for_user(user_id)
        response_rows.append(
            AdminDisintermediationUserResponse(
                user_id=user_id,
                role=str(row.get("role") or ""),
                name=str(row.get("full_name") or row.get("name") or "Usuário"),
                region=str(row.get("region") or ""),
                flagged=bool(row.get("flag_suspeita_desintermediacao", False)),
                flag_reason=str(row.get("desintermediacao_flag_reason") or "") or None,
                flagged_at=str(row.get("desintermediacao_flagged_at") or "") or None,
                expires_at=str(row.get("desintermediacao_flag_expires_at") or "") or None,
                contact_attempts_7d=contact_attempts,
                cancel_rate_14d=round(cancel_rate * 100.0, 2),
            )
        )

    response_rows.sort(key=lambda item: (0 if item.flagged else 1, -item.contact_attempts_7d, -item.cancel_rate_14d))
    return AdminDisintermediationOverviewResponse(
        total_flagged_users=len([row for row in response_rows if row.flagged]),
        total_contact_attempts_7d=len([row for row in recent_events if bool(row.get("counted_for_threshold", False))]),
        users=response_rows,
    )


@api_router.post("/admin/disintermediation/{target_user_id}/action")
async def admin_disintermediation_action(target_user_id: str, payload: AdminDisintermediationActionPayload, request: Request):
    admin_user = await _require_role(request, ["admin", "super_admin"])
    target_user = await db.users.find_one({"id": target_user_id}, {"_id": 0})
    if not target_user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    updates: Dict[str, Any] = {"updated_at": now_iso}

    if payload.action == "warn":
        await _create_notification(
            user_id=target_user_id,
            role=str(target_user.get("role") or "cliente"),
            title="Aviso de segurança",
            message="Para sua segurança, mantenha toda contratação e comunicação dentro do app 💚",
            category="seguranca",
        )
    elif payload.action == "limit":
        updates["disintermediation_limited_until"] = (now_dt + timedelta(days=7)).isoformat()
    elif payload.action == "suspend":
        updates["isActive"] = False
        if str(target_user.get("role") or "") == "passeador":
            updates["quality_status"] = QUALITY_STATUS_SUSPENDED
    elif payload.action == "clear_flag":
        updates["flag_suspeita_desintermediacao"] = False
        updates["desintermediacao_flag_reason"] = None
        updates["desintermediacao_flagged_at"] = None
        updates["desintermediacao_flag_expires_at"] = None

    await db.users.update_one({"id": target_user_id}, {"$set": updates})
    await db.admin_audit_logs.insert_one(
        {
            "id": str(uuid.uuid4()),
            "admin_user_id": str(admin_user.get("id") or ""),
            "target_user_id": target_user_id,
            "action": payload.action,
            "note": payload.note,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )

    return {"ok": True}


@api_router.get("/walkers/{walker_id}/availability-slots", response_model=WalkerAvailabilitySlotsResponse)
async def get_walker_availability_slots(
    walker_id: str,
    date: str,
    duration_minutes: int,
    request: Request,
):
    await _require_role(request, ["cliente", "passeador", "admin", "super_admin"])
    if duration_minutes not in WALK_DURATION_OPTIONS:
        raise HTTPException(status_code=422, detail="A duração deve ser 30, 45 ou 60 minutos")

    walker = await _resolve_walker_profile(walker_id)
    if not walker:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    weekday = _weekday_key_from_date(date)
    slots = await _get_available_slots_for_walker(walker, walker_id, date, duration_minutes)
    return WalkerAvailabilitySlotsResponse(
        walker_id=walker_id,
        date=date,
        weekday=weekday,
        duration_minutes=duration_minutes,
        available_slots=slots,
    )


@api_router.post("/auth/register", response_model=AuthTokensResponse, status_code=201)
async def register_auth(payload: AuthRegisterPayload, request: Request, response: Response):
    if not payload.accepted_terms or not payload.accepted_privacy or not payload.accepted_lgpd:
        raise HTTPException(status_code=400, detail="É necessário aceitar termos, privacidade e consentimento LGPD")

    email = str(payload.email).strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email é obrigatório")

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    request_ip = str((request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or request.headers.get("x-real-ip") or (request.client.host if request.client else "")).strip()
    request_device_id = str(request.headers.get("x-device-id") or "").strip()[:120]

    if request_ip:
        blocked_ip = await db.anti_abuse_blocks.find_one(
            {"kind": "ip", "value": request_ip, "blocked_until": {"$gt": now_iso}},
            {"_id": 0},
        )
        if blocked_ip:
            raise HTTPException(status_code=429, detail="Cadastro temporariamente indisponível. Tente novamente mais tarde")

    if request_device_id:
        blocked_device = await db.anti_abuse_blocks.find_one(
            {"kind": "device", "value": request_device_id, "blocked_until": {"$gt": now_iso}},
            {"_id": 0},
        )
        if blocked_device:
            raise HTTPException(status_code=429, detail="Cadastro temporariamente indisponível. Tente novamente mais tarde")

    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=409, detail="Email já cadastrado")

    if request_device_id:
        device_accounts = await db.users.count_documents({"registration_device_id": request_device_id})
        if device_accounts >= MAX_ACCOUNTS_PER_DEVICE:
            block_until = (now_dt + timedelta(hours=TEMP_REGISTRATION_BLOCK_HOURS)).isoformat()
            await db.anti_abuse_blocks.update_one(
                {"kind": "device", "value": request_device_id},
                {
                    "$set": {
                        "kind": "device",
                        "value": request_device_id,
                        "reason": "Excesso de contas no mesmo dispositivo",
                        "blocked_until": block_until,
                        "updated_at": now_iso,
                    },
                    "$setOnInsert": {"id": str(uuid.uuid4()), "created_at": now_iso},
                },
                upsert=True,
            )
            await db.coupon_fraud_alerts.insert_one(
                {
                    "id": str(uuid.uuid4()),
                    "alert_type": "registration_device_limit",
                    "severity": "high",
                    "message": "Limite de contas por dispositivo excedido.",
                    "coupon_id": None,
                    "coupon_code": None,
                    "user_id": None,
                    "user_email": email,
                    "user_phone": None,
                    "device_id": request_device_id,
                    "ip_address": request_ip or None,
                    "blocked": True,
                    "metadata": {"existing_accounts": device_accounts},
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
            )
            raise HTTPException(status_code=429, detail="Cadastro temporariamente indisponível. Tente novamente mais tarde")

    if request_ip:
        one_hour_ago = (now_dt - timedelta(hours=1)).isoformat()
        ip_registrations_last_hour = await db.users.count_documents(
            {"registration_ip": request_ip, "created_at": {"$gte": one_hour_ago}}
        )
        if ip_registrations_last_hour >= MAX_REGISTRATIONS_PER_IP_PER_HOUR:
            block_until = (now_dt + timedelta(hours=TEMP_REGISTRATION_BLOCK_HOURS)).isoformat()
            await db.anti_abuse_blocks.update_one(
                {"kind": "ip", "value": request_ip},
                {
                    "$set": {
                        "kind": "ip",
                        "value": request_ip,
                        "reason": "Excesso de cadastros no mesmo IP",
                        "blocked_until": block_until,
                        "updated_at": now_iso,
                    },
                    "$setOnInsert": {"id": str(uuid.uuid4()), "created_at": now_iso},
                },
                upsert=True,
            )
            await db.coupon_fraud_alerts.insert_one(
                {
                    "id": str(uuid.uuid4()),
                    "alert_type": "registration_ip_limit",
                    "severity": "high",
                    "message": "Excesso de cadastros no mesmo IP em 1 hora.",
                    "coupon_id": None,
                    "coupon_code": None,
                    "user_id": None,
                    "user_email": email,
                    "user_phone": None,
                    "device_id": request_device_id or None,
                    "ip_address": request_ip,
                    "blocked": True,
                    "metadata": {"registrations_last_hour": ip_registrations_last_hour},
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
            )
            raise HTTPException(status_code=429, detail="Cadastro temporariamente indisponível. Tente novamente mais tarde")

    role = "cliente"
    is_admin = False

    user = {
        "id": str(uuid.uuid4()),
        "full_name": payload.full_name.strip(),
        "email": email,
        "password_hash": _hash_password(payload.password),
        "role": role,
        "isAdmin": is_admin,
        "permissions": _empty_permissions_map(),
        "isActive": True,
        "possuiSeguro": False,
        "accepted_terms": payload.accepted_terms,
        "accepted_privacy": payload.accepted_privacy,
        "accepted_lgpd": payload.accepted_lgpd,
        "registration_ip": request_ip,
        "registration_device_id": request_device_id,
        "is_suspected": False,
        "is_coupon_blocked": False,
        "suspicion_reasons": [],
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_active_at": now_iso,
    }
    await db.users.insert_one(user)

    rapid_window_start = (now_dt - timedelta(minutes=5)).isoformat()
    if request_ip:
        rapid_ip_registrations = await db.users.count_documents(
            {"registration_ip": request_ip, "created_at": {"$gte": rapid_window_start}}
        )
        if rapid_ip_registrations >= RAPID_REGISTRATION_ALERT_THRESHOLD:
            await db.users.update_one(
                {"id": user["id"]},
                {
                    "$set": {"is_suspected": True, "updated_at": datetime.now(timezone.utc).isoformat()},
                    "$addToSet": {"suspicion_reasons": "Múltiplos cadastros em poucos minutos no mesmo IP"},
                },
            )
            await db.coupon_fraud_alerts.insert_one(
                {
                    "id": str(uuid.uuid4()),
                    "alert_type": "registration_rapid_ip",
                    "severity": "medium",
                    "message": "Padrão rápido de criação de contas detectado no IP.",
                    "coupon_id": None,
                    "coupon_code": None,
                    "user_id": user["id"],
                    "user_email": email,
                    "user_phone": None,
                    "device_id": request_device_id or None,
                    "ip_address": request_ip,
                    "blocked": False,
                    "metadata": {"registrations_last_5m": rapid_ip_registrations},
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
            )
    await _create_notification(
        user_id=user["id"],
        role=user["role"],
        title="Bem-vindo ao Aumigão",
        message="Sua conta foi criada com sucesso.",
        category="onboarding",
    )
    session = await _create_auth_session(user)
    _set_auth_cookies(response, session.access_token, session.refresh_token)
    return session


@api_router.post("/auth/login", response_model=AuthTokensResponse)
async def login_auth(payload: AuthLoginPayload, request: Request, response: Response):
    email = payload.email.strip().lower()
    identifier = _login_identifier(request, email)
    now = datetime.now(timezone.utc)

    attempt = await db.login_attempts.find_one({"identifier": identifier}, {"_id": 0})
    if attempt:
        locked_until = attempt.get("locked_until")
        if locked_until and datetime.fromisoformat(locked_until) > now:
            raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em alguns minutos")

    user = await db.users.find_one({"email": email}, {"_id": 0})
    if not user or not _verify_password(payload.password, user.get("password_hash", "")):
        failed_count = int((attempt or {}).get("failed_count", 0)) + 1
        lock_data = {}
        if failed_count >= FAILED_LOGIN_LIMIT:
            lock_data["locked_until"] = (now + timedelta(minutes=FAILED_LOGIN_LOCKOUT_MINUTES)).isoformat()

        await db.login_attempts.update_one(
            {"identifier": identifier},
            {
                "$set": {
                    "identifier": identifier,
                    "failed_count": failed_count,
                    "updated_at": now.isoformat(),
                    **lock_data,
                }
            },
            upsert=True,
        )
        raise HTTPException(status_code=401, detail="Credenciais inválidas")

    if user.get("isActive", True) is False:
        raise HTTPException(status_code=403, detail="Conta inativa")

    await db.login_attempts.delete_many({"identifier": identifier})
    await db.users.update_one({"id": user["id"]}, {"$set": {"last_active_at": now.isoformat()}})
    user["last_active_at"] = now.isoformat()
    session = await _create_auth_session(user)
    _set_auth_cookies(response, session.access_token, session.refresh_token)
    return session


@api_router.get("/auth/me", response_model=AuthUserResponse)
async def auth_me(request: Request):
    user = await _get_current_user(request)
    await db.users.update_one({"id": user["id"]}, {"$set": {"last_active_at": datetime.now(timezone.utc).isoformat()}})
    return _user_to_auth_response(user)


@api_router.post("/auth/refresh", response_model=AuthTokensResponse)
async def auth_refresh(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            token = header.replace("Bearer ", "", 1)

    if not token:
        raise HTTPException(status_code=401, detail="Refresh token ausente")

    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Token inválido")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Refresh inválido")

    user = await db.users.find_one({"id": payload.get("sub")}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Usuário não encontrado")

    session = await _create_auth_session(user)
    _set_auth_cookies(response, session.access_token, session.refresh_token)
    return session


@api_router.post("/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(key="access_token", path="/")
    response.delete_cookie(key="refresh_token", path="/")
    return {"message": "Logout realizado"}


@api_router.post("/auth/forgot-password")
async def auth_forgot_password(payload: ForgotPasswordPayload):
    email = payload.email.strip().lower()
    user = await db.users.find_one({"email": email}, {"_id": 0})
    if not user:
        return {"message": "Se o email existir, enviaremos instruções de redefinição"}

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    reset = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "token": token,
        "used": False,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }
    await db.password_reset_tokens.insert_one(reset)
    return {"message": "Token de recuperação gerado para MVP", "reset_token": token}


@api_router.post("/auth/reset-password")
async def auth_reset_password(payload: ResetPasswordPayload):
    reset = await db.password_reset_tokens.find_one({"token": payload.token}, {"_id": 0})
    if not reset:
        raise HTTPException(status_code=404, detail="Token inválido")
    if reset.get("used"):
        raise HTTPException(status_code=400, detail="Token já utilizado")

    if datetime.fromisoformat(reset["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Token expirado")

    await db.users.update_one(
        {"id": reset["user_id"]},
        {"$set": {"password_hash": _hash_password(payload.new_password), "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    await db.password_reset_tokens.update_one(
        {"token": payload.token},
        {"$set": {"used": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"message": "Senha redefinida com sucesso"}


@api_router.get("/faq")
async def get_faq_items():
    return [
        {"id": "faq-1", "title": "Como agendar passeio", "answer": "Acesse Agendar, escolha data, horário e confirme."},
        #{"id": "faq-2", "title": "Como funciona pagamento", "answer": "O pagamento é gerado automaticamente após o agendamento."},
        {
            "id": "faq-3",
            "title": "Cancelamento",
            "answer": ">24h: reembolso total, <24h: 50%, Não comparecimento sem aviso: cobrança integral (100%).",
        },
        {"id": "faq-4", "title": "Segurança", "answer": "Passeadores aprovados e código de segurança no passeio."},
        {"id": "faq-5", "title": "Passeios compartilhados", "answer": "Somente com análise de compatibilidade e aprovação manual."},
    ]


@api_router.get("/legal/terms")
async def get_terms_of_use():
    return {
        "title": "Termos de uso",
        "content": "Ao usar o Aumigão, você concorda com as regras de uso da plataforma e prestação de serviço sob demanda.",
    }


@api_router.get("/legal/privacy")
async def get_privacy_policy():
    return {
        "title": "Política de privacidade",
        "content": "Coletamos dados essenciais para operar passeios e suporte. Você pode solicitar revisão/exclusão de dados futuramente.",
    }


@api_router.get("/legal/walker-contract")
async def get_walker_contract_base():
    return {
        "title": "Base Contratual do Passeador",
        #"content": "Parceria autônoma, sem vínculo empregatício, pagamento por demanda e sem subordinação direta.",
    }


@api_router.get("/legal/protection")
async def get_protection_info():
    return {
        "title": "Proteção e Segurança",
        "content": "Aprovação de passeadores, validações operacionais e controles internos para reduzir riscos.",
    }


@api_router.post("/support/tickets", response_model=SupportTicketResponse, status_code=201)
async def create_support_ticket(payload: SupportTicketCreatePayload, request: Request):
    user = await _get_current_user(request)
    now_iso = datetime.now(timezone.utc).isoformat()
    ticket = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "user_name": user.get("full_name", ""),
        "user_email": user.get("email", ""),
        "subject": payload.subject.strip(),
        "message": payload.message.strip(),
        "status": "aberto",
        "admin_reply": "",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await db.support_tickets.insert_one(ticket)
    await _notify_admins(
        title="Ticket recebido",
        message=f"Novo ticket aberto: {ticket['subject']}",
        category="admin_pendencia",
    )
    await _create_notification(
        user_id=user["id"],
        role=user["role"],
        title="Ticket criado",
        message="Recebemos sua solicitação de suporte.",
        category="suporte",
    )
    return SupportTicketResponse(**ticket)


@api_router.get("/support/tickets", response_model=List[SupportTicketResponse])
async def list_support_tickets(request: Request):
    user = await _get_current_user(request)
    query = {} if user.get("isAdmin") else {"user_id": user["id"]}
    rows = await db.support_tickets.find(query, {"_id": 0}).sort("updated_at", -1).to_list(200)
    return [SupportTicketResponse(**row) for row in rows]


@api_router.patch("/support/tickets/{ticket_id}/reply", response_model=SupportTicketResponse)
async def reply_support_ticket(ticket_id: str, payload: SupportTicketReplyPayload, request: Request):
    await _require_admin(request)
    ticket = await db.support_tickets.find_one({"id": ticket_id}, {"_id": 0})
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket não encontrado")

    ticket["admin_reply"] = payload.message.strip()
    ticket["status"] = payload.status
    ticket["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.support_tickets.update_one({"id": ticket_id}, {"$set": ticket})

    await _create_notification(
        user_id=ticket["user_id"],
        role="cliente",
        title="Resposta do suporte",
        message=f"Seu ticket '{ticket['subject']}' foi atualizado.",
        category="suporte",
    )
    return SupportTicketResponse(**ticket)


@api_router.get("/notifications", response_model=List[NotificationResponse])
async def get_notifications(request: Request):
    user = await _get_current_user(request)
    rows = await db.notifications.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(300)
    return [NotificationResponse(**row) for row in rows]


@api_router.patch("/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_as_read(notification_id: str, request: Request):
    user = await _get_current_user(request)
    row = await db.notifications.find_one({"id": notification_id, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Notificação não encontrada")

    row["read"] = True
    await db.notifications.update_one({"id": notification_id}, {"$set": {"read": True}})
    return NotificationResponse(**row)


@api_router.get("/pet-routines", response_model=List[PetRoutineConfigResponse])
async def list_pet_routines(request: Request):
    user = await _require_role(request, ["cliente"])
    user_id = str(user.get("id") or "")
    routines = await db.pet_routines.find({"user_id": user_id}, {"_id": 0}).sort("updated_at", -1).to_list(200)
    responses: List[PetRoutineConfigResponse] = []
    for routine in routines:
        pet = await db.pets.find_one({"id": str(routine.get("pet_id") or "")}, {"_id": 0, "pet_name": 1})
        pet_name = str((pet or {}).get("pet_name") or "Pet")
        responses.append(PetRoutineConfigResponse(**_serialize_routine_response(routine, pet_name)))
    return responses


@api_router.post("/pet-routines", response_model=PetRoutineConfigResponse)
async def create_pet_routine(payload: PetRoutineConfigCreatePayload, request: Request):
    user = await _require_role(request, ["cliente"])
    user_id = str(user.get("id") or "")

    pet = await db.pets.find_one({"id": payload.pet_id, "owner_user_id": user_id}, {"_id": 0})
    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado para criação da rotina")

    _validate_routine_config(
        frequencia_semanal=payload.frequencia_semanal,
        dias_preferenciais=list(payload.dias_preferenciais),
        horario_preferencial=payload.horario_preferencial,
        duracao_passeio=int(payload.duracao_passeio),
    )

    existing = await db.pet_routines.find_one({"user_id": user_id, "pet_id": payload.pet_id}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=409, detail="Este pet já possui rotina configurada. Use editar rotina.")

    now_iso = datetime.now(timezone.utc).isoformat()
    routine = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "user_name": str(user.get("full_name") or "").strip(),
        "pet_id": payload.pet_id,
        "frequencia_semanal": int(payload.frequencia_semanal),
        "dias_preferenciais": _normalize_routine_days(list(payload.dias_preferenciais)),
        "horario_preferencial": _normalize_clock(payload.horario_preferencial, "09:00"),
        "duracao_passeio": int(payload.duracao_passeio),
        "is_active": True,
        "created_at": now_iso,
        "updated_at": now_iso,
        "config_history": [
            _build_routine_config_segment(
                routine_config={
                    "frequencia_semanal": int(payload.frequencia_semanal),
                    "dias_preferenciais": list(payload.dias_preferenciais),
                    "horario_preferencial": payload.horario_preferencial,
                    "duracao_passeio": int(payload.duracao_passeio),
                    "is_active": True,
                },
                action="created",
                effective_from=now_iso,
            )
        ],
    }
    await db.pet_routines.insert_one(routine)
    await _upsert_pet_routine_progress_for_user(user, pet_id=payload.pet_id)
    return PetRoutineConfigResponse(**_serialize_routine_response(routine, str(pet.get("pet_name") or "Pet")))


@api_router.patch("/pet-routines/{routine_id}", response_model=PetRoutineConfigResponse)
async def update_pet_routine(routine_id: str, payload: PetRoutineConfigUpdatePayload, request: Request):
    user = await _require_role(request, ["cliente"])
    user_id = str(user.get("id") or "")
    routine = await _get_pet_routine_or_404(routine_id, user_id)

    updated_config = {
        "frequencia_semanal": int(payload.frequencia_semanal or routine.get("frequencia_semanal") or 1),
        "dias_preferenciais": list(payload.dias_preferenciais or routine.get("dias_preferenciais") or []),
        "horario_preferencial": str(payload.horario_preferencial or routine.get("horario_preferencial") or "09:00"),
        "duracao_passeio": int(payload.duracao_passeio or routine.get("duracao_passeio") or 30),
        "is_active": bool(payload.is_active if payload.is_active is not None else routine.get("is_active", True)),
    }

    _validate_routine_config(
        frequencia_semanal=updated_config["frequencia_semanal"],
        dias_preferenciais=updated_config["dias_preferenciais"],
        horario_preferencial=updated_config["horario_preferencial"],
        duracao_passeio=updated_config["duracao_passeio"],
    )

    routine = _apply_routine_config_update(routine=routine, new_config=updated_config, action="updated")
    await db.pet_routines.update_one({"id": routine_id}, {"$set": routine})

    pet = await db.pets.find_one({"id": str(routine.get("pet_id") or "")}, {"_id": 0, "pet_name": 1})
    await _upsert_pet_routine_progress_for_user(user, pet_id=str(routine.get("pet_id") or ""))
    return PetRoutineConfigResponse(**_serialize_routine_response(routine, str((pet or {}).get("pet_name") or "Pet")))


@api_router.post("/pet-routines/{routine_id}/pause", response_model=PetRoutineConfigResponse)
async def pause_pet_routine(routine_id: str, request: Request):
    user = await _require_role(request, ["cliente"])
    user_id = str(user.get("id") or "")
    routine = await _get_pet_routine_or_404(routine_id, user_id)
    if not routine.get("is_active", True):
        return PetRoutineConfigResponse(**_serialize_routine_response(routine, str((await db.pets.find_one({"id": routine.get("pet_id")}, {"_id": 0, "pet_name": 1}) or {}).get("pet_name") or "Pet")))

    paused_config = {
        "frequencia_semanal": int(routine.get("frequencia_semanal") or 1),
        "dias_preferenciais": list(routine.get("dias_preferenciais") or []),
        "horario_preferencial": str(routine.get("horario_preferencial") or "09:00"),
        "duracao_passeio": int(routine.get("duracao_passeio") or 30),
        "is_active": False,
    }
    routine = _apply_routine_config_update(routine=routine, new_config=paused_config, action="paused")
    routine["paused_at"] = datetime.now(timezone.utc).isoformat()
    await db.pet_routines.update_one({"id": routine_id}, {"$set": routine})
    pet = await db.pets.find_one({"id": str(routine.get("pet_id") or "")}, {"_id": 0, "pet_name": 1})
    await _upsert_pet_routine_progress_for_user(user, pet_id=str(routine.get("pet_id") or ""))
    return PetRoutineConfigResponse(**_serialize_routine_response(routine, str((pet or {}).get("pet_name") or "Pet")))


@api_router.post("/pet-routines/{routine_id}/reactivate", response_model=PetRoutineConfigResponse)
async def reactivate_pet_routine(routine_id: str, request: Request):
    user = await _require_role(request, ["cliente"])
    user_id = str(user.get("id") or "")
    routine = await _get_pet_routine_or_404(routine_id, user_id)
    if routine.get("is_active", True):
        return PetRoutineConfigResponse(**_serialize_routine_response(routine, str((await db.pets.find_one({"id": routine.get("pet_id")}, {"_id": 0, "pet_name": 1}) or {}).get("pet_name") or "Pet")))

    active_config = {
        "frequencia_semanal": int(routine.get("frequencia_semanal") or 1),
        "dias_preferenciais": list(routine.get("dias_preferenciais") or []),
        "horario_preferencial": str(routine.get("horario_preferencial") or "09:00"),
        "duracao_passeio": int(routine.get("duracao_passeio") or 30),
        "is_active": True,
    }
    routine = _apply_routine_config_update(routine=routine, new_config=active_config, action="reactivated")
    routine["reactivated_at"] = datetime.now(timezone.utc).isoformat()
    await db.pet_routines.update_one({"id": routine_id}, {"$set": routine})
    pet = await db.pets.find_one({"id": str(routine.get("pet_id") or "")}, {"_id": 0, "pet_name": 1})
    await _upsert_pet_routine_progress_for_user(user, pet_id=str(routine.get("pet_id") or ""))
    return PetRoutineConfigResponse(**_serialize_routine_response(routine, str((pet or {}).get("pet_name") or "Pet")))


@api_router.get("/pet-routines/{routine_id}/suggestions", response_model=List[PetRoutineSuggestionResponse])
async def get_pet_routine_suggestions(routine_id: str, request: Request):
    user = await _require_role(request, ["cliente"])
    user_id = str(user.get("id") or "")
    routine = await _get_pet_routine_or_404(routine_id, user_id)
    walks = await db.walks.find({}, {"_id": 0}).to_list(6000)
    walk_datetimes = [
        walk_dt
        for walk in walks
        if _walk_matches_routine(walk, routine)
        if (walk_dt := _walk_datetime_from_doc(walk)) is not None
    ]
    suggestions = _build_pet_routine_suggestions(routine=routine, walk_datetimes=walk_datetimes, days_ahead=7)
    return [PetRoutineSuggestionResponse(**item) for item in suggestions]


@api_router.get("/pet-routine/dashboard", response_model=PetRoutineDashboardResponse)
async def get_pet_routine_dashboard(request: Request, pet_id: Optional[str] = None, user_id: Optional[str] = None):
    user = await _require_role(request, ["cliente", "admin", "super_admin"])
    is_admin_actor = _is_admin_user(user)
    target_user_id = str(user.get("id") or "").strip()
    if is_admin_actor:
        target_user_id = str(user_id or "").strip()
        if not target_user_id:
            raise HTTPException(status_code=400, detail="Para admin, informe user_id")

    client_user = await db.users.find_one({"id": target_user_id, "role": "cliente"}, {"_id": 0})
    if not client_user:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    routine_query: dict = {"user_id": target_user_id}
    if str(pet_id or "").strip():
        routine_query["pet_id"] = str(pet_id or "").strip()

    routines = await db.pet_routines.find(routine_query, {"_id": 0}).sort("updated_at", -1).to_list(100)
    selected_routine = next((item for item in routines if bool(item.get("is_active", False))), None) or (routines[0] if routines else None)

    if selected_routine:
        pet = await db.pets.find_one({"id": str(selected_routine.get("pet_id") or "")}, {"_id": 0, "pet_name": 1})
        pet_name = str((pet or {}).get("pet_name") or "Pet")
        routine_response = PetRoutineConfigResponse(**_serialize_routine_response(selected_routine, pet_name))
        if _is_feature_active("pet_routine"):
            progress_row = await _upsert_pet_routine_progress_for_user(client_user, pet_id=str(selected_routine.get("pet_id") or ""))
        else:
            progress_row = (
                await db.pet_routine_progress.find_one({"routine_id": str(selected_routine.get("id") or "")}, {"_id": 0})
                or {
                    "id": f"pet-routine-progress-{selected_routine.get('id')}",
                    "routine_id": str(selected_routine.get("id") or ""),
                    "pet_id": str(selected_routine.get("pet_id") or ""),
                    "pet_name": pet_name,
                    "user_id": target_user_id,
                    "encouragement_message": "Rotina do Pet está pausada no momento.",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "dias_preferenciais": _normalize_routine_days(list(selected_routine.get("dias_preferenciais") or [])),
                    "suggestions": [],
                    "week_progress_label": "Você cumpriu 0 de 0 passeios planejados nesta semana",
                }
            )
    else:
        routine_response = None
        progress_row = {
            "id": f"pet-routine-progress-empty-{target_user_id}",
            "user_id": target_user_id,
            "encouragement_message": "Configure a rotina do seu pet para começar sua sequência.",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "simple_badges": [],
            "dias_preferenciais": [],
            "suggestions": [],
            "week_progress_label": "Você cumpriu 0 de 0 passeios planejados nesta semana",
        }

    return PetRoutineDashboardResponse(
        feature_active=_is_feature_active("pet_routine"),
        feature_visible=_is_feature_visible("pet_routine"),
        routine=routine_response,
        progress=PetRoutineProgressResponse(**progress_row),
    )


@api_router.get("/pet-routine/progress", response_model=PetRoutineProgressResponse)
async def get_pet_routine_progress(request: Request, pet_id: Optional[str] = None, user_id: Optional[str] = None):
    dashboard = await get_pet_routine_dashboard(request=request, pet_id=pet_id, user_id=user_id)
    return dashboard.progress


@api_router.post("/admin/pet-routine/recalculate", response_model=PetRoutineRecalculateResponse)
async def recalculate_pet_routine_progress(payload: PetRoutineRecalculatePayload, request: Request):
    await _require_admin_permission(request, "clientes")
    if not _is_feature_active("pet_routine"):
        return PetRoutineRecalculateResponse(processed_users=0, updated_profiles=0, items=[])

    target_user_id = str(payload.user_id or "").strip()
    target_pet_id = str(payload.pet_id or "").strip()

    users_to_process: List[dict] = []
    if target_user_id:
        client_user = await db.users.find_one({"id": target_user_id, "role": "cliente"}, {"_id": 0})
        if not client_user:
            raise HTTPException(status_code=404, detail="Cliente não encontrado para recálculo")
        users_to_process = [client_user]
    else:
        users_to_process = await db.users.find({"role": "cliente"}, {"_id": 0}).to_list(3000)

    items: List[PetRoutineProgressResponse] = []
    for client_user in users_to_process:
        progress = await _upsert_pet_routine_progress_for_user(client_user, pet_id=target_pet_id or None)
        items.append(PetRoutineProgressResponse(**progress))

    return PetRoutineRecalculateResponse(
        processed_users=len(users_to_process),
        updated_profiles=len(items),
        items=items,
    )


@api_router.post("/automations/run")
async def run_automations(request: Request):
    await _require_admin(request)
    now = datetime.now(timezone.utc)
    users = await db.users.find({}, {"_id": 0}).to_list(500)
    created = 0
    attendance_updates = 0

    active_walks = await db.walks.find({"status": {"$in": [STATUS_SCHEDULED, STATUS_GOING_TO_PICKUP]}}, {"_id": 0}).to_list(1000)
    for walk in active_walks:
        previous_status = walk.get("status")
        refreshed = await _apply_attendance_decision_if_needed(walk, now=now, trigger="automation")
        if refreshed.get("status") != previous_status:
            attendance_updates += 1

    for user in users:
        user_walks = await db.walks.find({"client_name": user.get("full_name", "")}, {"_id": 0}).to_list(100)
        finished_count = sum(1 for walk in user_walks if walk.get("status") == STATUS_FINISHED)
        last_active = datetime.fromisoformat(user.get("last_active_at", user.get("created_at")))
        days_inactive = (now - last_active).days

        if days_inactive >= 14:
            await _create_notification(
                user_id=user["id"],
                role=user["role"],
                title="Sentimos sua falta",
                message="Já faz um tempo! Agende um novo passeio para manter a rotina do seu pet.",
                category="reengajamento",
            )
            created += 1

        if finished_count == 1:
            await _create_notification(
                user_id=user["id"],
                role=user["role"],
                title="Seu pet pode precisar de rotina",
                message="Agende novamente com 1 clique e mantenha a consistência.",
                category="remarketing",
            )
            created += 1

    return {"created_notifications": created, "attendance_updates": attendance_updates}


@api_router.patch("/walker/availability", response_model=AuthUserResponse)
@api_router.post("/walker/availability", response_model=AuthUserResponse)
async def update_walker_availability(payload: WalkerAvailabilityUpdatePayload, request: Request):
    user = await _require_role(request, ["passeador", "admin"])
    if user.get("role") == "admin":
        raise HTTPException(status_code=403, detail="Atualização de disponibilidade permitida apenas para passeadores")

    start_time = _normalize_clock(payload.availability_start_time, DEFAULT_AVAILABILITY_START_TIME)
    end_time = _normalize_clock(payload.availability_end_time, DEFAULT_AVAILABILITY_END_TIME)
    if _clock_to_minutes(end_time) - _clock_to_minutes(start_time) < 60:
        raise HTTPException(status_code=400, detail="A janela de disponibilidade deve ter ao menos 1 hora")

    availability_days = _normalize_availability_days(payload.availability_days)
    availability_periods = _normalize_availability_periods(payload.availability_periods)
    availability_capacity_by_period = _normalize_availability_capacity_by_period(payload.availability_capacity_by_period)
    availability_daily_capacity_overrides = _normalize_daily_capacity_overrides(payload.availability_daily_capacity_overrides)
    horarios_disponiveis = _build_horarios_disponiveis_from_periods(availability_days, availability_periods)
    now_iso = datetime.now(timezone.utc).isoformat()

    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {
                "availability_days": availability_days,
                "availability_start_time": start_time,
                "availability_end_time": end_time,
                "availability_periods": availability_periods,
                "availability_capacity_by_period": availability_capacity_by_period,
                "availability_daily_capacity_overrides": availability_daily_capacity_overrides,
                "horarios_disponiveis": horarios_disponiveis,
                "updated_at": now_iso,
            }
        },
    )

    updated_user = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not updated_user:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")
    return _user_to_auth_response(updated_user)


def _to_walker_availability_settings(user_doc: dict) -> WalkerAvailabilitySettingsResponse:
    schedule = _ensure_walker_schedule(user_doc)
    blocks = schedule.get("availability_blocks", [])
    now = datetime.now(timezone.utc)
    is_unavailable = any(
        (_parse_iso_datetime(block.get("start_at")) or now) <= now <= (_parse_iso_datetime(block.get("end_at")) or now)
        for block in blocks
    )
    return WalkerAvailabilitySettingsResponse(
        availability_days=schedule.get("availability_days", []),
        availability_start_time=schedule.get("availability_start_time", ""),
        availability_end_time=schedule.get("availability_end_time", ""),
        availability_periods=schedule.get("availability_periods", {}),
        availability_capacity_by_period=schedule.get("availability_capacity_by_period", DEFAULT_AVAILABILITY_CAPACITY_BY_PERIOD),
        availability_daily_capacity_overrides=schedule.get("availability_daily_capacity_overrides", {}),
        blocks=[WalkerAvailabilityBlock(**block) for block in blocks],
        unavailable_until=schedule.get("unavailable_until"),
        is_temporarily_unavailable=is_unavailable,
    )


@api_router.get("/walker/availability-settings", response_model=WalkerAvailabilitySettingsResponse)
async def get_walker_availability_settings(request: Request):
    user = await _require_role(request, ["passeador"])
    user_doc = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")
    return _to_walker_availability_settings(user_doc)


@api_router.post("/walker/availability-blocks", response_model=WalkerAvailabilitySettingsResponse)
async def create_walker_availability_block(payload: WalkerAvailabilityBlockCreatePayload, request: Request):
    user = await _require_role(request, ["passeador"])

    start_dt, end_dt = _build_block_period(
        start_date=payload.start_date,
        start_time=payload.start_time,
        end_date=payload.end_date,
        end_time=payload.end_time,
        full_day=payload.full_day,
    )

    conflicts = await _find_confirmed_walk_conflicts_for_period(user, start_dt, end_dt)
    if conflicts:
        first_conflict = conflicts[0]
        raise HTTPException(
            status_code=400,
            detail=(
                "Existe passeio já agendado no período selecionado "
                f"({first_conflict.get('walk_date')} às {first_conflict.get('walk_time')})."
            ),
        )

    user_doc = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    schedule = _ensure_walker_schedule(user_doc)
    blocks = schedule.get("availability_blocks", [])
    blocks.append(
        {
            "id": str(uuid.uuid4()),
            "start_at": start_dt.isoformat(),
            "end_at": end_dt.isoformat(),
            "is_full_day": payload.full_day,
            "reason": payload.reason.strip(),
            "block_type": "manual",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    normalized_blocks = _normalize_availability_blocks(blocks)

    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {
                "availability_blocks": normalized_blocks,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    updated = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return _to_walker_availability_settings(updated or schedule)


@api_router.delete("/walker/availability-blocks/{block_id}", response_model=WalkerAvailabilitySettingsResponse)
async def delete_walker_availability_block(block_id: str, request: Request):
    user = await _require_role(request, ["passeador"])
    user_doc = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    schedule = _ensure_walker_schedule(user_doc)
    current_blocks = schedule.get("availability_blocks", [])
    updated_blocks = [block for block in current_blocks if block.get("id") != block_id]

    if len(updated_blocks) == len(current_blocks):
        raise HTTPException(status_code=404, detail="Bloqueio não encontrado")

    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {
                "availability_blocks": updated_blocks,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    updated = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return _to_walker_availability_settings(updated or schedule)


@api_router.post("/walker/unavailable", response_model=WalkerAvailabilitySettingsResponse)
async def set_walker_unavailable(payload: WalkerQuickUnavailablePayload, request: Request):
    user = await _require_role(request, ["passeador"])
    now = datetime.now(timezone.utc)

    if payload.mode == "today":
        start_dt, end_dt = _build_block_period(
            start_date=now.strftime("%Y-%m-%d"),
            start_time="00:00",
            end_date=now.strftime("%Y-%m-%d"),
            end_time="23:59",
            full_day=True,
        )
    elif payload.mode == "until_date":
        if not payload.until_date:
            raise HTTPException(status_code=400, detail="Informe a data final da indisponibilidade")
        start_dt, end_dt = _build_block_period(
            start_date=now.strftime("%Y-%m-%d"),
            start_time="00:00",
            end_date=payload.until_date,
            end_time="23:59",
            full_day=True,
        )
    else:
        if not payload.start_date or not payload.end_date or not payload.start_time or not payload.end_time:
            raise HTTPException(status_code=400, detail="Informe período completo para indisponibilidade personalizada")
        start_dt, end_dt = _build_block_period(
            start_date=payload.start_date,
            start_time=payload.start_time,
            end_date=payload.end_date,
            end_time=payload.end_time,
            full_day=False,
        )

    conflicts = await _find_confirmed_walk_conflicts_for_period(user, start_dt, end_dt)
    if conflicts:
        first_conflict = conflicts[0]
        raise HTTPException(
            status_code=400,
            detail=(
                "Existe passeio já agendado no período selecionado "
                f"({first_conflict.get('walk_date')} às {first_conflict.get('walk_time')})."
            ),
        )

    user_doc = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    schedule = _ensure_walker_schedule(user_doc)
    blocks = [block for block in schedule.get("availability_blocks", []) if block.get("block_type") != "quick_unavailable"]
    blocks.append(
        {
            "id": str(uuid.uuid4()),
            "start_at": start_dt.isoformat(),
            "end_at": end_dt.isoformat(),
            "is_full_day": payload.mode != "custom_period",
            "reason": payload.reason.strip(),
            "block_type": "quick_unavailable",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    normalized_blocks = _normalize_availability_blocks(blocks)

    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {
                "availability_blocks": normalized_blocks,
                "unavailable_until": end_dt.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    updated = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return _to_walker_availability_settings(updated or schedule)


@api_router.delete("/walker/unavailable", response_model=WalkerAvailabilitySettingsResponse)
async def clear_walker_unavailable(request: Request):
    user = await _require_role(request, ["passeador"])
    user_doc = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    schedule = _ensure_walker_schedule(user_doc)
    blocks = [block for block in schedule.get("availability_blocks", []) if block.get("block_type") != "quick_unavailable"]

    await db.users.update_one(
        {"id": user["id"]},
        {
            "$set": {
                "availability_blocks": blocks,
                "unavailable_until": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    updated = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    return _to_walker_availability_settings(updated or schedule)


@api_router.get("/walker/rating-summary", response_model=WalkerRatingSummaryResponse)
async def get_walker_rating_summary(request: Request):
    user = await _require_role(request, ["passeador"])
    query = _walker_identity_query(user)
    walks = await db.walks.find(query, {"_id": 0}).sort("updated_at", -1).to_list(500)
    rated_walks = [walk for walk in walks if isinstance(walk.get("rating"), int)]

    rating_count = len(rated_walks)
    rating_avg = round(sum(int(walk.get("rating", 0)) for walk in rated_walks) / rating_count, 2) if rating_count else 0.0

    recent_reviews = [
        WalkerRatingItem(
            walk_id=walk["id"],
            rating=int(walk.get("rating", 0)),
            comment=str(walk.get("comment", "") or ""),
            client_name=str(walk.get("client_name", "") or ""),
            created_at=str(walk.get("updated_at", "") or ""),
        )
        for walk in rated_walks[:5]
    ]

    encouragement = "Continue evoluindo para ganhar mais confiança dos clientes."
    if rating_count >= 5 and rating_avg >= 4.7:
        encouragement = "Você está em excelente desempenho. Continue assim para receber mais confiança dos clientes."
    elif rating_count >= 5 and rating_avg >= 4.3:
        encouragement = "Ótimo trabalho! Pequenos ajustes podem elevar ainda mais sua nota."

    return WalkerRatingSummaryResponse(
        rating_avg=rating_avg,
        rating_count=rating_count,
        recent_reviews=recent_reviews,
        encouragement_message=encouragement,
    )


@api_router.get("/walker/quality", response_model=WalkerQualityDashboardResponse)
async def get_walker_quality_dashboard(request: Request):
    user = await _require_role(request, ["passeador"])
    await _recalculate_walker_quality(user["id"], trigger_event="dashboard_read")
    user_doc = await db.users.find_one({"id": user["id"], "role": "passeador"}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    quality_metrics = user_doc.get("quality_metrics", {}) if isinstance(user_doc.get("quality_metrics"), dict) else {}
    monitoring = _normalize_quality_monitoring(user_doc.get("quality_monitoring"))
    target = monitoring.get("target_walks", 0)
    completed = monitoring.get("completed_walks", 0)
    remaining = max(0, target - completed)
    monitor_severity = str(monitoring.get("severity", "padrao") or "padrao")

    status_reason = str(user_doc.get("quality_status_reason") or "Sem observações no momento")
    instructions: List[str] = []
    if user_doc.get("quality_status") in {QUALITY_STATUS_RESTRICTED, QUALITY_STATUS_SUSPENDED}:
        instructions.append("Conclua o mini-curso obrigatório com checklist de cenários práticos.")
        instructions.append("Cenário 1: atraso do cliente e comunicação preventiva em até 5 minutos.")
        instructions.append("Cenário 2: início do passeio com confirmação de segurança no app.")
        instructions.append("Cenário 3: intercorrência no trajeto e atualização imediata ao cliente.")
        instructions.append("Aprove no quiz (mínimo de 80%) para iniciar monitoramento válido.")
        instructions.append(f"Complete {remaining or target} passeios sem não comparecimento para recuperar status.")
        instructions.append("Se ocorrerem 3 atrasos graves no monitoramento, a contagem reinicia automaticamente.")
    elif user_doc.get("quality_status") == QUALITY_STATUS_OBSERVATION:
        instructions.append("Você está em observação por pontualidade.")
        instructions.append("Evite atrasos graves para recuperar prioridade de exibição.")
    else:
        instructions.append("Continue com boa pontualidade e atendimento para manter seu nível.")

    quality_history = user_doc.get("quality_history", []) if isinstance(user_doc.get("quality_history"), list) else []
    recent_history = [
        f"{str(item.get('timestamp', ''))}: {str(item.get('to', ''))} — {str(item.get('reason', ''))}"
        for item in quality_history[-5:]
        if isinstance(item, dict)
    ]
    encouragement_message = str(quality_metrics.get("encouragement_message") or _walker_incentive_message(user_doc.get("quality_status", QUALITY_STATUS_ACTIVE), quality_metrics))
    if user_doc.get("quality_status") == QUALITY_STATUS_OBSERVATION:
        encouragement_message = "Você está em observação por pontualidade"
    if monitoring.get("active") and target > 0:
        encouragement_message = f"Complete {remaining} passeios sem atraso grave para melhorar sua classificação"
    recent_comments = [
        str(comment)
        for comment in (quality_metrics.get("recent_comments") or [])
        if isinstance(comment, str) and str(comment).strip()
    ][:5]

    return WalkerQualityDashboardResponse(
        quality_status=user_doc.get("quality_status", QUALITY_STATUS_ACTIVE),
        status_reason=status_reason,
        rating_avg=_coerce_float(quality_metrics.get("rating_avg"), 0.0),
        rating_recent_avg=_coerce_float(quality_metrics.get("rating_recent_avg"), 0.0),
        rating_weighted_avg=_coerce_float(quality_metrics.get("rating_weighted_avg"), 0.0),
        rating_count=int(quality_metrics.get("rating_count", 0) or 0),
        public_rating_label=str(quality_metrics.get("public_rating_label") or "Novo na plataforma"),
        public_badge=str(quality_metrics.get("public_badge") or ""),
        score_base=_coerce_float(quality_metrics.get("score_base"), 0.0),
        score_final=_coerce_float(quality_metrics.get("score_final"), 0.0),
        score_trend=_coerce_float(quality_metrics.get("score_trend"), 0.0),
        recency_factor=_coerce_float(quality_metrics.get("recency_factor"), 1.0),
        consistency_factor=_coerce_float(quality_metrics.get("consistency_factor"), 1.0),
        severe_penalty_factor=_coerce_float(quality_metrics.get("severe_penalty_factor"), 1.0),
        status_penalty_factor=_coerce_float(quality_metrics.get("status_penalty_factor"), 1.0),
        accepted_walks=int(quality_metrics.get("accepted_walks", 0) or 0),
        completed_walks=int(quality_metrics.get("completed_walks", 0) or 0),
        severe_delay_rate=_coerce_float(quality_metrics.get("severe_delay_rate"), 0.0),
        no_show_rate=_coerce_float(quality_metrics.get("no_show_rate"), 0.0),
        cancel_rate=_coerce_float(quality_metrics.get("cancel_rate"), 0.0),
        recent_comments=recent_comments,
        encouragement_message=encouragement_message,
        monitor_target_walks=target,
        monitor_completed_walks=completed,
        monitor_remaining_walks=remaining,
        monitor_severity=monitor_severity,
        monitor_reset_count=int(monitoring.get("reset_count", 0) or 0),
        monitor_severe_delay_incidents=int(monitoring.get("severe_delay_incidents", 0) or 0),
        recovery_required=bool(monitoring.get("active", False)),
        course_completed=bool(monitoring.get("course_completed", False)),
        quiz_passed=bool(monitoring.get("quiz_passed", False)),
        quiz_attempts=int(monitoring.get("quiz_attempts", 0) or 0),
        review_recommended=bool(monitoring.get("review_recommended", False)),
        recent_history=recent_history,
        instructions=instructions,
    )


@api_router.post("/walker/quality/course-complete", response_model=WalkerQualityDashboardResponse)
async def complete_walker_quality_course(payload: WalkerQualityCourseCompletePayload, request: Request):
    user = await _require_role(request, ["passeador"])
    if not payload.checklist_confirmed:
        raise HTTPException(status_code=400, detail="Confirme o checklist para concluir o mini-curso")

    user_doc = await db.users.find_one({"id": user["id"], "role": "passeador"}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    monitoring = _normalize_quality_monitoring(user_doc.get("quality_monitoring"))
    monitoring["active"] = True
    if monitoring.get("target_walks", 0) <= 0:
        monitoring["target_walks"] = _quality_target_from_severity(monitoring.get("severity", "padrao"))
    monitoring["course_completed"] = True

    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"quality_monitoring": monitoring, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return await get_walker_quality_dashboard(request)


@api_router.post("/walker/quality/quiz-submit")
async def submit_walker_quality_quiz(payload: WalkerQualityQuizSubmitPayload, request: Request):
    user = await _require_role(request, ["passeador"])
    user_doc = await db.users.find_one({"id": user["id"], "role": "passeador"}, {"_id": 0})
    if not user_doc:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    monitoring = _normalize_quality_monitoring(user_doc.get("quality_monitoring"))
    monitoring["active"] = True
    if monitoring.get("target_walks", 0) <= 0:
        monitoring["target_walks"] = _quality_target_from_severity(monitoring.get("severity", "padrao"))

    expected = [1, 2, 3, 1, 2]
    correct = sum(1 for idx, value in enumerate(payload.answers) if value == expected[idx])
    score = (correct / 5) * 100

    monitoring["quiz_attempts"] += 1
    if score >= 80:
        monitoring["quiz_passed"] = True
        monitoring["consecutive_quiz_failures"] = 0
        monitoring["review_recommended"] = False
    else:
        monitoring["quiz_passed"] = False
        monitoring["consecutive_quiz_failures"] += 1
        if monitoring["consecutive_quiz_failures"] >= 3:
            monitoring["review_recommended"] = True

    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"quality_monitoring": monitoring, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )

    return {
        "score": score,
        "approved": score >= 80,
        "quiz_attempts": monitoring["quiz_attempts"],
        "review_recommended": monitoring["review_recommended"],
    }


@api_router.get("/walker/tasks", response_model=List[WalkResponse])
async def list_walker_tasks(request: Request):
    user = await _require_role(request, ["passeador", "admin"])
    query = (
        {}
        if user.get("isAdmin")
        else {
            "$or": [
                {"walker_user_id": user.get("id")},
                {"walker_name": user.get("full_name")},
            ]
        }
    )
    rows = await db.walks.find(query, {"_id": 0}).sort("walk_datetime_iso", 1).to_list(200)
    refreshed_rows: List[dict] = []
    for row in rows:
        refreshed_rows.append(await _apply_attendance_decision_if_needed(row, trigger="walker_read"))
    return [_to_walk_response(row) for row in refreshed_rows]


@api_router.get("/walker/requests", response_model=List[WalkerRequestResponse])
async def list_walker_requests(request: Request):
    user = await _require_role(request, ["passeador", "admin"])
    now_iso = datetime.now(timezone.utc).isoformat()

    if not user.get("isAdmin"):
        overdue_rows = await db.walker_requests.find(
            {
                "status": "pending",
                "target_walker_user_id": user.get("id"),
                "respond_until": {"$lt": now_iso},
            },
            {"_id": 0},
        ).to_list(100)
        if overdue_rows:
            overdue_ids = [str(row.get("id") or "") for row in overdue_rows if str(row.get("id") or "")]
            matching_count: Dict[str, int] = {}
            for row in overdue_rows:
                matching_id = str(row.get("matching_request_id") or "")
                if matching_id:
                    await _apply_match_penalty(str(user.get("id") or ""), "ignore")
                    matching_count[matching_id] = matching_count.get(matching_id, 0) + 1
            if overdue_ids:
                await db.walker_requests.update_many(
                    {"id": {"$in": overdue_ids}, "status": "pending"},
                    {"$set": {"status": "ignored", "updated_at": now_iso}},
                )
            for matching_id, count in matching_count.items():
                await db.matching_requests.update_one(
                    {"id": matching_id},
                    {"$inc": {"ignored_count": count}, "$set": {"updated_at": now_iso}},
                )

    query: Dict[str, Any] = {"status": "pending", "respond_until": {"$gte": now_iso}}
    if not user.get("isAdmin"):
        region = str(user.get("region", "")).strip()
        if region:
            query["region"] = region
        query["$or"] = [
            {"target_walker_user_id": user.get("id")},
            {"target_walker_user_id": None},
            {"target_walker_user_id": {"$exists": False}},
        ]

    rows = await db.walker_requests.find(query, {"_id": 0}).sort("respond_until", 1).to_list(100)
    return rows


@api_router.post("/walker/requests/{request_id}/decision", response_model=WalkerRequestResponse)
async def decide_walker_request(request_id: str, payload: WalkerRequestDecisionPayload, request: Request):
    user = await _require_role(request, ["passeador", "admin"])

    request_query: Dict[str, Any] = {"id": request_id, "status": "pending"}
    if not user.get("isAdmin"):
        region = str(user.get("region", "")).strip()
        if region:
            request_query["region"] = region
        request_query["$or"] = [
            {"target_walker_user_id": user.get("id")},
            {"target_walker_user_id": None},
            {"target_walker_user_id": {"$exists": False}},
        ]

    request_row = await db.walker_requests.find_one(request_query, {"_id": 0})
    if not request_row:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    respond_until = _parse_iso_datetime(request_row.get("respond_until"))
    if respond_until and respond_until < now_dt:
        await db.walker_requests.update_one(
            {"id": request_id, "status": "pending"},
            {"$set": {"status": "ignored", "updated_at": now_iso}},
        )
        if request_row.get("matching_request_id"):
            await _apply_match_penalty(str(user.get("id") or ""), "ignore")
        raise HTTPException(status_code=409, detail="Solicitação expirada")

    update_fields: Dict[str, Any] = {
        "status": "accepted" if payload.decision == "accept" else "rejected",
        "decision_by": user.get("id"),
        "decision_at": now_iso,
        "updated_at": now_iso,
    }

    matching_request_id = str(request_row.get("matching_request_id") or "").strip()

    if payload.decision == "accept":
        if matching_request_id:
            lock_result = await db.matching_requests.update_one(
                {"id": matching_request_id, "status": "searching"},
                {
                    "$set": {
                        "status": "matched",
                        "selected_walker_user_id": user.get("id"),
                        "selected_walker_name": user.get("full_name", "Passeador"),
                        "selected_position": int(request_row.get("rank_position", 0) or 0),
                        "updated_at": now_iso,
                        "accepted_at": now_iso,
                    },
                    "$inc": {"attempted_count": 0},
                },
            )
            if lock_result.modified_count == 0:
                current_match = await db.matching_requests.find_one({"id": matching_request_id}, {"_id": 0})
                if current_match and str(current_match.get("status") or "") == "matched":
                    raise HTTPException(status_code=409, detail="Este passeio já foi aceito por outro passeador")

        premium_settings = await _get_operational_settings()
        premium_percent = min(
            80.0,
            max(
                70.0,
                _coerce_float(
                    premium_settings.get("premiumRepassePercentual", DEFAULT_PREMIUM_PAYOUT_PERCENT),
                    DEFAULT_PREMIUM_PAYOUT_PERCENT,
                ),
            ),
        )

        walk_id = f"walker-request-{request_id}"
        duration_minutes = int(_coerce_float(request_row.get("duration_minutes"), 45))
        if duration_minutes not in WALK_DURATION_OPTIONS:
            duration_minutes = 45

        pickup_street = str(request_row.get("pickup_street") or "Rua principal").strip()
        pickup_number = str(request_row.get("pickup_number") or "S/N").strip()
        pickup_neighborhood = str(request_row.get("pickup_neighborhood") or request_row.get("neighborhood") or "Bairro").strip()
        pickup_complement = str(request_row.get("pickup_complement") or "").strip()
        location_reference = str(
            request_row.get("location_reference")
            or request_row.get("approx_location")
            or pickup_neighborhood
        ).strip()
        endereco_base_tutor = f"{pickup_street}, {pickup_number} - {pickup_neighborhood}"

        tipo_passeio = str(request_row.get("tipo_passeio") or "padrao").strip().lower()
        modo_inicio_passeio = str(request_row.get("modo_inicio_passeio") or START_MODE_TUTOR_ADDRESS).strip() or START_MODE_TUTOR_ADDRESS
        if tipo_passeio == "transporte" and modo_inicio_passeio != START_MODE_PREMIUM_RELOCATION:
            modo_inicio_passeio = START_MODE_PREMIUM_RELOCATION
        is_transport_walk = tipo_passeio == "transporte" or modo_inicio_passeio == START_MODE_PREMIUM_RELOCATION

        ponto_retirada_alternativo = None
        ponto_encontro = None
        local_destino_passeio = None
        distancia_km = 0.0
        adicional_deslocamento = 0.0
        tempo_deslocamento_minutos = 0
        tempo_total_minutos = duration_minutes
        precisa_analise_manual = False
        status_analise_deslocamento = PREMIUM_ANALYSIS_NA
        tracking_interval_seconds = 60
        status_transporte = "nao_aplicavel"
        eventos_transporte: List[Dict[str, Any]] = []

        if modo_inicio_passeio == START_MODE_TUTOR_ADDRESS:
            if bool(request_row.get("usar_ponto_retirada_alternativo", False)):
                alt_name = str(request_row.get("ponto_retirada_alternativo_nome") or "").strip()
                alt_reference = str(request_row.get("ponto_retirada_alternativo_referencia") or "").strip()
                if alt_name:
                    ponto_retirada_alternativo = {
                        "nome": alt_name,
                        "referencia": alt_reference,
                        "latitude": 0.0,
                        "longitude": 0.0,
                    }
        elif modo_inicio_passeio == START_MODE_MEETING_POINT:
            meeting_name = str(request_row.get("ponto_encontro_nome") or "").strip()
            meeting_reference = str(request_row.get("ponto_encontro_referencia") or "").strip()
            if not meeting_name:
                raise HTTPException(status_code=400, detail="Informe o ponto de encontro")
            ponto_encontro = {
                "nome": meeting_name,
                "referencia": meeting_reference,
                "latitude": 0.0,
                "longitude": 0.0,
            }
            location_reference = meeting_name
        elif modo_inicio_passeio == START_MODE_PREMIUM_RELOCATION:
            has_vehicle = bool(user.get("possuiVeiculo", False))
            accepts_transport = bool(user.get("aceitaDeslocamentoPremium", False))
            transport_enabled = bool(user.get("ativoParaTransportePremium", False))
            if not (has_vehicle and accepts_transport and transport_enabled):
                raise HTTPException(status_code=400, detail="Passeador selecionado não está apto para deslocamento premium")

            local_destino_nome = str(request_row.get("local_destino_nome") or "").strip()
            local_destino_referencia = str(request_row.get("local_destino_referencia") or "").strip()
            if not local_destino_nome:
                raise HTTPException(status_code=400, detail="Informe o destino do passeio com transporte")

            geocode_error = "Não conseguimos identificar o local. Tente ser mais específico."
            base_geocode = _geocode_location(f"{endereco_base_tutor}, Salvador, BA, Brasil")
            if not base_geocode:
                raise HTTPException(status_code=400, detail=geocode_error)

            destination_query = f"{local_destino_nome}, {local_destino_referencia}, Salvador, BA, Brasil"
            destination_geocode = _geocode_location(destination_query)
            if not destination_geocode:
                raise HTTPException(status_code=400, detail=geocode_error)

            base_latitude = _coerce_float(base_geocode.get("latitude"), -12.9714)
            base_longitude = _coerce_float(base_geocode.get("longitude"), -38.5014)
            destino_lat = _coerce_float(destination_geocode.get("latitude"), 0.0)
            destino_lng = _coerce_float(destination_geocode.get("longitude"), 0.0)
            transport_settings = await _get_pet_transport_settings_dict()
            distancia_km, tempo_rota_minutos = await _estimate_transport_route(
                origin_lat=base_latitude,
                origin_lng=base_longitude,
                destination_lat=destino_lat,
                destination_lng=destino_lng,
                fallback_minutes_per_km=_coerce_float(transport_settings.get("estimated_minutes_per_km"), 3.0),
            )
            adicional_deslocamento, precisa_analise_manual, status_analise_deslocamento, tempo_estimado_por_km = _calculate_premium_transport(
                distancia_km,
                transport_settings,
            )
            tempo_deslocamento_minutos = max(tempo_rota_minutos, tempo_estimado_por_km)
            tempo_total_minutos = duration_minutes + tempo_deslocamento_minutos
            tracking_interval_seconds = int(transport_settings.get("tracking_interval_seconds") or 15)
            status_transporte = "A caminho do local"
            eventos_transporte.append(
                {
                    "event": "inicio_deslocamento",
                    "timestamp": now_iso,
                    "status_label": status_transporte,
                }
            )
            local_destino_passeio = {
                "nome": local_destino_nome,
                "referencia": local_destino_referencia,
                "latitude": destino_lat,
                "longitude": destino_lng,
            }
            location_reference = local_destino_nome
        else:
            raise HTTPException(status_code=400, detail="Modo de início do passeio inválido")

        walk_datetime_iso = _validate_datetime_iso(request_row.get("walk_date"), request_row.get("walk_time"))
        walk_doc = {
            "id": walk_id,
            "pet_name": request_row.get("pet_name", "Pet"),
            "pet_ids": [request_row.get("pet_id")] if request_row.get("pet_id") else [],
            "shared_pet_names": [],
            "shared_client_names": [],
            "shared_owner_keys": [],
            "participant_user_ids": [request_row.get("client_user_id")] if request_row.get("client_user_id") else [],
            "client_user_id": request_row.get("client_user_id"),
            "client_name": request_row.get("client_name", "Cliente"),
            "walk_type": request_row.get("walk_type", WALK_TYPE_INDIVIDUAL),
            "tipoPasseio": "transporte" if is_transport_walk else "padrao",
            "modoInicioPasseio": modo_inicio_passeio,
            "enderecoBaseTutor": endereco_base_tutor,
            "pontoRetiradaAlternativo": ponto_retirada_alternativo,
            "pontoEncontro": ponto_encontro,
            "localDestinoPasseio": local_destino_passeio,
            "distanciaKm": round(distancia_km, 2),
            "adicionalDeslocamento": round(adicional_deslocamento, 2),
            "tempoDeslocamentoMinutos": int(tempo_deslocamento_minutos),
            "tempoPasseioMinutos": int(duration_minutes),
            "tempoTotalMinutos": int(tempo_total_minutos),
            "rastreamentoReforcado": bool(is_transport_walk),
            "trackingIntervalSegundos": int(tracking_interval_seconds),
            "statusTransporte": status_transporte,
            "eventosTransporte": eventos_transporte,
            "precisaAnaliseManualDeslocamento": bool(precisa_analise_manual),
            "statusAnaliseDeslocamento": status_analise_deslocamento,
            "premiumRepassePercentual": premium_percent,
            "dynamic_pricing_mode": str(request_row.get("dynamic_pricing_mode") or DYNAMIC_PRICING_MODE_OFF),
            "dynamic_price_multiplier": max(
                1.0,
                min(1.0 + DYNAMIC_PRICING_MAX_TOTAL_BOOST, _coerce_float(request_row.get("dynamic_price_multiplier"), 1.0)),
            ),
            "dynamic_price_reason": str(request_row.get("dynamic_price_reason") or "Preço padrão"),
            "dynamic_price_calculated": _coerce_float(request_row.get("dynamic_price_calculated"), 0.0),
            "dynamic_price_difference_percent": _coerce_float(request_row.get("dynamic_price_difference_percent"), 0.0),
            "shared_context": SHARED_CONTEXT_OTHER_CLIENT if request_row.get("walk_type") == WALK_TYPE_SHARED else None,
            "shared_approved": request_row.get("walk_type") != WALK_TYPE_SHARED,
            "shared_group": None,
            "walk_date": request_row.get("walk_date"),
            "walk_time": request_row.get("walk_time"),
            "duration_minutes": duration_minutes,
            "walker_id": f"partner-{user.get('id')}",
            "walker_user_id": user.get("id"),
            "walker_name": user.get("full_name", "Passeador"),
            "walker_photo_url": _build_avatar_data_uri("#E8F1FF", "#2FBF71"),
            "pickup_street": pickup_street,
            "pickup_number": pickup_number,
            "pickup_neighborhood": pickup_neighborhood,
            "pickup_complement": pickup_complement,
            "location_reference": location_reference,
            "security_code": _generate_security_code(),
            "did_pee": False,
            "did_poop": False,
            "rating": None,
            "rating_comment": "",
            "summary_text": "",
            "pet_behavior_notes": request_row.get("pet_behavior_notes", ""),
            "notes": request_row.get("notes", ""),
            "motivoCancelamento": "",
            "tipoCancelamento": None,
            "penalidadePercentual": 0,
            "status": STATUS_PENDING_REVIEW if precisa_analise_manual else STATUS_SCHEDULED,
            "scheduled_start_at": walk_datetime_iso,
            "walker_check_in_at": None,
            "client_confirmed_at": None,
            "tolerance_expires_at": (datetime.fromisoformat(walk_datetime_iso) + timedelta(minutes=TOLERANCE_MINUTES)).isoformat(),
            "tolerance_minutes": TOLERANCE_MINUTES,
            "attendance_message": "Há tolerância de até 10 minutos para início do passeio",
            "decision_resolved_at": None,
            "decision_source": "",
            "walker_penalty_registered": False,
            "occurrence_status": OCC_UNRESOLVED,
            "occurrence_resolved": False,
            "internal_note": "",
            "occurrence_logs": [],
            "photo_url": None,
            "walk_datetime_iso": walk_datetime_iso,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        walk_doc["price_before_discount"] = _walk_subtotal_before_discount(walk_doc)
        walk_doc["valor_base_passeio"] = _base_walk_price(walk_doc)
        base_price, walker_payout = _calculate_walk_pricing(walk_doc)
        walk_doc["base_price"] = base_price
        walk_doc["walker_payout"] = walker_payout
        walk_doc["charged_amount"] = _base_amount_from_walk(walk_doc)
        walk_doc["walker_payout_amount"] = walker_payout
        walk_doc["platform_retained_amount"] = round(walk_doc["charged_amount"] - walker_payout, 2)
        walk_doc["walker_share_percent"] = RUNTIME_WALKER_SHARE_PERCENT
        walk_doc["platform_fee_percent"] = RUNTIME_PLATFORM_SHARE_PERCENT
        walk_doc["client_refund_amount"] = 0.0

        await db.walks.update_one({"id": walk_doc["id"]}, {"$set": walk_doc}, upsert=True)
        await _rebuild_payments_for_walk(walk_doc)
        if str(walk_doc.get("client_user_id") or ""):
            await _mark_dynamic_pricing_conversion(
                user_id=str(walk_doc.get("client_user_id") or ""),
                walk_date=str(walk_doc.get("walk_date") or ""),
                walk_time=str(walk_doc.get("walk_time") or ""),
                walk_id=str(walk_doc.get("id") or ""),
                confirmed_price=_coerce_float(walk_doc.get("charged_amount"), 0.0),
                confirmed_multiplier=_coerce_float(walk_doc.get("dynamic_price_multiplier"), 1.0),
            )

        if status_analise_deslocamento == PREMIUM_ANALYSIS_WAITING:
            await _notify_admins(
                title="Análise de deslocamento pendente",
                message=f"Passeio premium {walk_id} requer análise manual de deslocamento.",
                category="admin_pendencia",
            )

        update_fields["accepted_walk_id"] = walk_id

        if matching_request_id:
            created_at = _parse_iso_datetime(request_row.get("created_at"))
            confirmed_in_seconds = 0
            if created_at:
                confirmed_in_seconds = max(1, int((now_dt - created_at).total_seconds()))
            await db.matching_requests.update_one(
                {"id": matching_request_id},
                {
                    "$set": {
                        "accepted_walk_id": walk_id,
                        "confirmed_in_seconds": confirmed_in_seconds,
                        "updated_at": now_iso,
                    }
                },
            )
            await db.walker_requests.update_many(
                {
                    "matching_request_id": matching_request_id,
                    "id": {"$ne": request_id},
                    "status": "pending",
                },
                {
                    "$set": {
                        "status": "canceled",
                        "updated_at": now_iso,
                        "decision_source": "accepted_by_another_walker",
                    }
                },
            )
            await _reset_match_penalty_on_accept(str(user.get("id") or ""))

        await _create_notification(
            user_id=user["id"],
            role=user.get("role", "passeador"),
            title="Nova solicitação aceita",
            message=f"Passeio de {request_row.get('pet_name', 'pet')} adicionado à sua agenda.",
            category="walker_request",
        )
    else:
        if matching_request_id:
            await db.matching_requests.update_one(
                {"id": matching_request_id},
                {"$inc": {"rejected_count": 1}, "$set": {"updated_at": now_iso}},
            )
            await _apply_match_penalty(str(user.get("id") or ""), "reject")
        await _create_notification(
            user_id=user["id"],
            role=user.get("role", "passeador"),
            title="Solicitação recusada",
            message=f"Solicitação de {request_row.get('pet_name', 'pet')} removida da fila.",
            category="walker_request",
        )

    await db.walker_requests.update_one({"id": request_id}, {"$set": update_fields})
    updated = await db.walker_requests.find_one({"id": request_id}, {"_id": 0})
    if not updated:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    return updated


@api_router.get("/walker/alerts", response_model=List[WalkerAlertResponse])
async def list_walker_alerts(request: Request):
    user = await _require_role(request, ["passeador", "admin"])
    query: Dict[str, Any] = {"active": True}
    if not user.get("isAdmin"):
        region = str(user.get("region", "")).strip()
        if region:
            query["region"] = region
        query["$or"] = [
            {"target_walker_user_id": user.get("id")},
            {"target_walker_user_id": None},
            {"target_walker_user_id": {"$exists": False}},
        ]

    rows = await db.walker_alerts.find(query, {"_id": 0}).sort("created_at", -1).to_list(50)
    return rows


def _pet_from_payload(
    payload: PetCreateUpdate,
    owner_name_fallback: str,
    owner_profile_id: str,
    owner_user_id: str,
    existing: Optional[dict] = None,
) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    owner_name = payload.owner_name.strip() or owner_name_fallback
    if not owner_name:
        owner_name = "Tutor"

    base = {
        "owner_profile_id": existing.get("owner_profile_id", owner_profile_id) if existing else owner_profile_id,
        "owner_user_id": existing.get("owner_user_id", owner_user_id) if existing else owner_user_id,
        "owner_name": owner_name,
        "pet_name": payload.pet_name.strip(),
        "behavioral_notes": payload.behavioral_notes.strip(),
        "photo_url": payload.photo_url.strip(),
        "gets_along_with_dogs": payload.gets_along_with_dogs,
        "accepts_shared_walk": payload.accepts_shared_walk,
        "pet_size": payload.pet_size,
        "energy_level": payload.energy_level,
        "pulls_leash": payload.pulls_leash,
        "dog_behavior": payload.dog_behavior,
        "updated_at": now_iso,
    }

    if existing:
        base["id"] = existing["id"]
        base["created_at"] = existing["created_at"]
        base["podeParticiparCompartilhado"] = existing.get("podeParticiparCompartilhado", False)
        base["aprovadoParaCompartilhado"] = existing.get("aprovadoParaCompartilhado", False)
    else:
        base["id"] = str(uuid.uuid4())
        base["created_at"] = now_iso
        base["podeParticiparCompartilhado"] = False
        base["aprovadoParaCompartilhado"] = False

    return base


@api_router.get("/pets", response_model=List[PetResponse])
async def list_pets(request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    if _is_admin_user(user):
        query = {}
    else:
        query = {
            "$or": [
                {"owner_user_id": user["id"]},
                {"owner_profile_id": "default", "owner_name": user.get("full_name", "")},
            ]
        }

    pets = await db.pets.find(query, {"_id": 0}).sort("created_at", 1).to_list(200)

    if not _is_admin_user(user):
        owner_profile_id = _owner_profile_id_for_user(user["id"])
        legacy_pet_ids = [pet["id"] for pet in pets if not pet.get("owner_user_id")]
        if legacy_pet_ids:
            await db.pets.update_many(
                {"id": {"$in": legacy_pet_ids}},
                {"$set": {"owner_user_id": user["id"], "owner_profile_id": owner_profile_id}},
            )

    return [PetResponse(**pet) for pet in pets]


@api_router.post("/pets/{pet_id}/praise-tags", response_model=PetPraiseEntryResponse, status_code=201)
async def create_pet_praise_tags(pet_id: str, payload: PetPraiseCreatePayload, request: Request):
    user = await _require_role(request, ["passeador", "admin"])
    pet = await db.pets.find_one({"id": pet_id}, {"_id": 0})
    if not pet:
        raise HTTPException(status_code=404, detail="Pet não encontrado")

    tags = _normalize_praise_tags(payload.tags)
    if not tags:
        raise HTTPException(status_code=400, detail="Selecione ao menos um elogio positivo")

    if payload.walk_id:
        walk = await db.walks.find_one({"id": payload.walk_id}, {"_id": 0})
        if not walk:
            raise HTTPException(status_code=404, detail="Passeio não encontrado")
        if pet_id not in (walk.get("pet_ids") or []):
            raise HTTPException(status_code=400, detail="Este passeio não está vinculado ao pet selecionado")

    now_iso = datetime.now(timezone.utc).isoformat()
    entry = {
        "id": str(uuid.uuid4()),
        "pet_id": pet_id,
        "walk_id": payload.walk_id,
        "walker_user_id": user["id"],
        "walker_name": user.get("full_name") or "Passeador",
        "tags": tags,
        "created_at": now_iso,
    }
    await db.pet_praise_feedback.insert_one(entry)
    return PetPraiseEntryResponse(**entry)


@api_router.get("/pets/highlights", response_model=PetHighlightsResponse)
async def get_pet_highlights(request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    if _is_admin_user(user):
        pet_query = {}
    else:
        pet_query = {
            "$or": [
                {"owner_user_id": user["id"]},
                {"owner_profile_id": "default", "owner_name": user.get("full_name", "")},
            ]
        }

    pets = await db.pets.find(pet_query, {"_id": 0}).to_list(200)
    if not pets:
        return PetHighlightsResponse()

    pet_ids = [str(p.get("id") or "") for p in pets if p.get("id")]
    walks = await db.walks.find(
        {"status": STATUS_FINISHED, "pet_ids": {"$in": pet_ids}},
        {"_id": 0, "pet_ids": 1, "walk_datetime_iso": 1, "walk_date": 1},
    ).to_list(3000)
    praise_rows = await db.pet_praise_feedback.find({"pet_id": {"$in": pet_ids}}, {"_id": 0}).to_list(2000)

    now_dt = datetime.now(timezone.utc)
    week_start = now_dt - timedelta(days=7)
    month_start = now_dt - timedelta(days=30)

    week_counts = {pet_id: 0 for pet_id in pet_ids}
    month_counts = {pet_id: 0 for pet_id in pet_ids}
    praise_counts = {pet_id: 0 for pet_id in pet_ids}

    for row in praise_rows:
        pet_ref = str(row.get("pet_id") or "")
        if pet_ref in praise_counts:
            praise_counts[pet_ref] += 1

    for walk in walks:
        walk_dt = _walk_datetime_from_doc(walk)
        if not walk_dt:
            continue
        for pet_ref in (walk.get("pet_ids") or []):
            pet_id_value = str(pet_ref or "")
            if pet_id_value not in month_counts:
                continue
            if walk_dt >= month_start:
                month_counts[pet_id_value] += 1
            if walk_dt >= week_start:
                week_counts[pet_id_value] += 1

    pet_index = {str(p.get("id") or ""): p for p in pets}

    def _pick_best(counts: dict) -> Optional[str]:
        ranked = sorted(
            pet_ids,
            key=lambda pid: (
                int(counts.get(pid, 0)),
                int(praise_counts.get(pid, 0)),
            ),
            reverse=True,
        )
        if not ranked:
            return None
        if int(counts.get(ranked[0], 0)) <= 0 and int(praise_counts.get(ranked[0], 0)) <= 0:
            return None
        return ranked[0]

    pet_week_id = _pick_best(week_counts)
    pet_month_id = _pick_best(month_counts)

    featured_rank = sorted(
        pet_ids,
        key=lambda pid: (
            int(month_counts.get(pid, 0)) * 2 + int(week_counts.get(pid, 0)) + int(praise_counts.get(pid, 0)) * 2
        ),
        reverse=True,
    )
    featured_ids = [pid for pid in featured_rank if int(month_counts.get(pid, 0)) > 0 or int(praise_counts.get(pid, 0)) > 0][:3]

    def _build_item(pet_id_value: str, *, title: str, reason: str, is_featured: bool) -> Optional[PetHighlightItemResponse]:
        pet = pet_index.get(pet_id_value)
        if not pet:
            return None
        return PetHighlightItemResponse(
            pet_id=pet_id_value,
            pet_name=str(pet.get("pet_name") or "Pet"),
            photo_url=str(pet.get("photo_url") or ""),
            title=title,
            reason=reason,
            badges=_pet_engagement_badges(
                month_walks=int(month_counts.get(pet_id_value, 0)),
                praise_count=int(praise_counts.get(pet_id_value, 0)),
                is_featured=is_featured,
            ),
            praise_tags=_latest_praise_tags_for_pet(praise_rows, pet_id_value),
        )

    featured_items = [
        item
        for item in [
            _build_item(
                pet_id_value,
                title="Pet em destaque",
                reason="Consistência de uso e elogios positivos recentes.",
                is_featured=True,
            )
            for pet_id_value in featured_ids
        ]
        if item
    ]

    pet_week_item = (
        _build_item(
            pet_week_id,
            title="Pet da semana",
            reason="Maior engajamento positivo nos últimos dias.",
            is_featured=True,
        )
        if pet_week_id
        else None
    )
    pet_month_item = (
        _build_item(
            pet_month_id,
            title="Pet do mês",
            reason="Uso frequente e comportamento positivo no período.",
            is_featured=True,
        )
        if pet_month_id
        else None
    )

    return PetHighlightsResponse(
        pet_da_semana=pet_week_item,
        pet_do_mes=pet_month_item,
        pets_em_destaque=featured_items,
    )


@api_router.post("/pets", response_model=PetResponse, status_code=201)
async def create_pet(payload: PetCreateUpdate, request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    owner_profile_id = _owner_profile_id_for_user(user["id"])
    owner_profile = await db.owner_profiles.find_one({"id": owner_profile_id}, {"_id": 0})
    owner_name_fallback = owner_profile.get("full_name", "") if owner_profile else user.get("full_name", "")
    pet = _pet_from_payload(payload, owner_name_fallback, owner_profile_id, user["id"])
    await db.pets.insert_one(pet)
    return PetResponse(**pet)


@api_router.put("/pets/{pet_id}", response_model=PetResponse)
async def update_pet(pet_id: str, payload: PetCreateUpdate, request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    existing = await db.pets.find_one({"id": pet_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Pet não encontrado")
    if not _pet_belongs_to_user(existing, user):
        raise HTTPException(status_code=403, detail="Sem permissão para editar este pet")

    owner_profile_id = existing.get("owner_profile_id") or _owner_profile_id_for_user(user["id"])
    owner_profile = await db.owner_profiles.find_one({"id": owner_profile_id}, {"_id": 0})
    owner_name_fallback = owner_profile.get("full_name", "") if owner_profile else user.get("full_name", "")
    pet = _pet_from_payload(payload, owner_name_fallback, owner_profile_id, user["id"], existing)
    await db.pets.update_one({"id": pet_id}, {"$set": pet})
    return PetResponse(**pet)


@api_router.delete("/pets/{pet_id}", status_code=204)
async def delete_pet(pet_id: str, request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    existing = await db.pets.find_one({"id": pet_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Pet não encontrado")
    if not _pet_belongs_to_user(existing, user):
        raise HTTPException(status_code=403, detail="Sem permissão para remover este pet")

    await db.pets.delete_one({"id": pet_id})
    return None


@api_router.get("/admin/pets", response_model=List[AdminPetSummaryResponse])
async def list_admin_pets():
    pets = await db.pets.find({}, {"_id": 0}).sort("created_at", 1).to_list(500)
    rows = []
    for pet in pets:
        finished = await _finished_walks_count_for_pet(pet["id"])
        rows.append(AdminPetSummaryResponse(**pet, finished_walks_count=finished))
    return rows


@api_router.patch("/admin/pets/{pet_id}/shared-eligibility", response_model=PetResponse)
async def update_admin_pet_shared_eligibility(pet_id: str, payload: AdminPetSharedEligibilityUpdate):
    existing = await db.pets.find_one({"id": pet_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Pet não encontrado")

    existing["podeParticiparCompartilhado"] = payload.podeParticiparCompartilhado
    existing["aprovadoParaCompartilhado"] = payload.aprovadoParaCompartilhado
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.pets.update_one({"id": pet_id}, {"$set": existing})
    return PetResponse(**existing)


@api_router.get("/pet-profile", response_model=Optional[PetProfileResponse])
async def get_pet_profile(request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    profile = await db.pets.find_one({"owner_user_id": user["id"]}, {"_id": 0}, sort=[("created_at", 1)])

    if not profile:
        profile = await db.pets.find_one(
            {"owner_profile_id": "default", "owner_name": user.get("full_name", "")},
            {"_id": 0},
            sort=[("created_at", 1)],
        )

    if profile and not profile.get("owner_user_id"):
        await db.pets.update_one(
            {"id": profile["id"]},
            {
                "$set": {
                    "owner_user_id": user["id"],
                    "owner_profile_id": _owner_profile_id_for_user(user["id"]),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )

    if not profile:
        return None
    return PetProfileResponse(
        id=profile["id"],
        pet_name=profile.get("pet_name", ""),
        behavioral_notes=profile.get("behavioral_notes", ""),
        photo_url=profile.get("photo_url", ""),
        updated_at=profile.get("updated_at", datetime.now(timezone.utc).isoformat()),
    )


@api_router.put("/pet-profile", response_model=PetProfileResponse)
async def upsert_pet_profile(payload: PetProfileUpdate, request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    now_iso = datetime.now(timezone.utc).isoformat()
    owner_profile_id = _owner_profile_id_for_user(user["id"])
    existing = await db.pets.find_one({"owner_user_id": user["id"]}, {"_id": 0}, sort=[("created_at", 1)])
    if not existing:
        existing = await db.pets.find_one(
            {"owner_profile_id": "default", "owner_name": user.get("full_name", "")},
            {"_id": 0},
            sort=[("created_at", 1)],
        )

    pet_payload = PetCreateUpdate(
        pet_name=payload.pet_name,
        behavioral_notes=payload.behavioral_notes,
        photo_url=payload.photo_url,
        owner_name="",
        gets_along_with_dogs=True,
        accepts_shared_walk=True,
        pet_size="Médio",
        energy_level="Médio",
        pulls_leash=False,
        dog_behavior="Neutro",
    )

    owner_profile = await db.owner_profiles.find_one({"id": owner_profile_id}, {"_id": 0})
    owner_name_fallback = owner_profile.get("full_name", "") if owner_profile else user.get("full_name", "")

    if existing:
        pet = _pet_from_payload(pet_payload, owner_name_fallback, owner_profile_id, user["id"], existing)
        await db.pets.update_one({"id": existing["id"]}, {"$set": pet})
    else:
        pet = _pet_from_payload(pet_payload, owner_name_fallback, owner_profile_id, user["id"])
        pet["updated_at"] = now_iso
        await db.pets.insert_one(pet)

    return PetProfileResponse(
        id=pet["id"],
        pet_name=pet.get("pet_name", ""),
        behavioral_notes=pet.get("behavioral_notes", ""),
        photo_url=pet.get("photo_url", ""),
        updated_at=pet.get("updated_at", now_iso),
    )


@api_router.get("/owner-profile", response_model=Optional[OwnerProfileResponse])
async def get_owner_profile(request: Request):
    user = await _require_role(request, ["cliente", "admin", "passeador"])
    profile_id = _owner_profile_id_for_user(user["id"])
    profile = await db.owner_profiles.find_one({"id": profile_id}, {"_id": 0})

    if not profile:
        profile = await db.owner_profiles.find_one(
            {"id": "default", "full_name": user.get("full_name", "")},
            {"_id": 0},
        )
        if profile:
            profile["id"] = profile_id
            profile["user_id"] = user["id"]
            profile["updated_at"] = datetime.now(timezone.utc).isoformat()
            await db.owner_profiles.update_one({"id": "default"}, {"$set": profile}, upsert=True)

    if not profile:
        return None
    return OwnerProfileResponse(**profile)


@api_router.put("/owner-profile", response_model=OwnerProfileResponse)
async def upsert_owner_profile(payload: OwnerProfileUpdate, request: Request):
    user = await _require_role(request, ["cliente", "admin", "passeador"])
    now_iso = datetime.now(timezone.utc).isoformat()
    profile_id = _owner_profile_id_for_user(user["id"])
    primary_address_full = (
        f"{payload.street.strip()}, {payload.number.strip()} - {payload.neighborhood.strip()}"
        f"{f' ({payload.complement.strip()})' if payload.complement.strip() else ''}"
    )
    normalized_email = str(payload.email).strip().lower()
    if not normalized_email:
        raise HTTPException(status_code=400, detail="Email é obrigatório")

    profile = {
        "id": profile_id,
        "user_id": user["id"],
        "full_name": payload.full_name.strip(),
        "phone": payload.phone.strip(),
        "email": normalized_email,
        "street": payload.street.strip(),
        "number": payload.number.strip(),
        "neighborhood": payload.neighborhood.strip(),
        "complement": payload.complement.strip(),
        "primary_address_full": primary_address_full,
        "updated_at": now_iso,
    }
    await db.owner_profiles.update_one({"id": profile_id}, {"$set": profile}, upsert=True)
    return OwnerProfileResponse(**profile)


@api_router.post("/partner-applications", response_model=PartnerApplicationPublicResponse, status_code=201)
async def create_partner_application(payload: PartnerApplicationCreate):
    if not payload.accepted_declaration:
        raise HTTPException(status_code=400, detail="Declaração de responsabilidade é obrigatória")

    availability_start_time = _normalize_clock(payload.availability_start_time, DEFAULT_AVAILABILITY_START_TIME)
    availability_end_time = _normalize_clock(payload.availability_end_time, DEFAULT_AVAILABILITY_END_TIME)
    if _clock_to_minutes(availability_end_time) - _clock_to_minutes(availability_start_time) < 60:
        raise HTTPException(status_code=400, detail="A disponibilidade deve ter no mínimo 1 hora")

    availability_days = _normalize_availability_days(payload.availability_days)
    horarios_disponiveis = _build_horarios_disponiveis(availability_days, availability_start_time, availability_end_time)
    availability_summary = (
        f"{', '.join(day.capitalize() for day in availability_days)} • "
        f"{availability_start_time} às {availability_end_time}"
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    application = PartnerApplicationResponse(
        id=str(uuid.uuid4()),
        full_name=payload.full_name.strip(),
        phone=payload.phone.strip(),
        email=str(payload.email).strip().lower(),
        neighborhood_region=payload.neighborhood_region.strip(),
        has_pet_experience=payload.has_pet_experience,
        has_third_party_experience=payload.has_third_party_experience,
        experience_description=payload.experience_description.strip(),
        availability=availability_summary,
        availability_days=availability_days,
        availability_start_time=availability_start_time,
        availability_end_time=availability_end_time,
        horarios_disponiveis=horarios_disponiveis,
        profile_photo_url=payload.profile_photo_url.strip(),
        possuiSeguro=payload.possuiSeguro,
        accepted_declaration=True,
        status="Em análise",
        internal_notes="",
        approved_at=None,
        active_as_walker=False,
        created_at=now_iso,
        updated_at=now_iso,
    )
    await db.partner_applications.insert_one(application.model_dump())
    await _notify_admins(
        title="Passeador aguardando aprovação",
        message=f"Novo candidato: {application.full_name}",
        category="admin_pendencia",
    )
    return PartnerApplicationPublicResponse(**application.model_dump())


@api_router.get("/partner-applications", response_model=List[PartnerApplicationPublicResponse])
async def list_partner_applications():
    applications = (
        await db.partner_applications.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    )
    return [PartnerApplicationPublicResponse(**app) for app in applications]


@api_router.get("/partner-applications/{application_id}", response_model=PartnerApplicationPublicResponse)
async def get_partner_application(application_id: str):
    application = await db.partner_applications.find_one({"id": application_id}, {"_id": 0})
    if not application:
        raise HTTPException(status_code=404, detail="Candidatura não encontrada")
    return PartnerApplicationPublicResponse(**application)


@api_router.get("/admin/partner-applications", response_model=List[PartnerApplicationResponse])
async def list_partner_applications_admin():
    applications = (
        await db.partner_applications.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    )
    return [PartnerApplicationResponse(**app) for app in applications]


@api_router.get("/admin/partner-applications/{application_id}", response_model=PartnerApplicationResponse)
async def get_partner_application_admin(application_id: str):
    application = await db.partner_applications.find_one({"id": application_id}, {"_id": 0})
    if not application:
        raise HTTPException(status_code=404, detail="Candidatura não encontrada")
    return PartnerApplicationResponse(**application)


@api_router.patch("/partner-applications/{application_id}/status", response_model=PartnerApplicationResponse)
async def update_partner_application_status(
    application_id: str,
    payload: PartnerApplicationStatusUpdate,
    request: Request,
):
    await _require_admin_permission(request, "passeadores")
    update_data = {
        "status": payload.status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if payload.status == "Aprovado":
        update_data["approved_at"] = datetime.now(timezone.utc).isoformat()
    else:
        update_data["active_as_walker"] = False
        if payload.status == "Em análise":
            update_data["approved_at"] = None

    result = await db.partner_applications.update_one(
        {"id": application_id},
        {"$set": update_data},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Candidatura não encontrada")

    updated = await db.partner_applications.find_one({"id": application_id}, {"_id": 0})
    return PartnerApplicationResponse(**updated)


@api_router.patch("/partner-applications/{application_id}/admin-fields", response_model=PartnerApplicationResponse)
async def update_partner_application_admin_fields(
    application_id: str,
    payload: PartnerApplicationAdminFieldsUpdate,
    request: Request,
):
    await _require_admin_permission(request, "passeadores")
    current = await db.partner_applications.find_one({"id": application_id}, {"_id": 0})
    if not current:
        raise HTTPException(status_code=404, detail="Candidatura não encontrada")

    update_data: dict = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if payload.internal_notes is not None:
        update_data["internal_notes"] = payload.internal_notes.strip()

    if payload.active_as_walker is not None:
        if payload.active_as_walker and current.get("status") != "Aprovado":
            raise HTTPException(status_code=400, detail="Apenas aprovados podem ser ativos")
        update_data["active_as_walker"] = payload.active_as_walker

    await db.partner_applications.update_one({"id": application_id}, {"$set": update_data})
    updated = await db.partner_applications.find_one({"id": application_id}, {"_id": 0})
    return PartnerApplicationResponse(**updated)


def _to_admin_account_response(row: dict) -> AdminAccountResponse:
    role = row.get("role", "admin")
    if role not in {"admin", "super_admin"}:
        role = "admin"
    return AdminAccountResponse(
        id=row["id"],
        full_name=row.get("full_name", ""),
        email=row.get("email", ""),
        role=role,
        isActive=row.get("isActive", True),
        permissions=_normalize_admin_permissions(row.get("permissions", {})),
        created_by=row.get("created_by"),
        created_at=row.get("created_at", datetime.now(timezone.utc).isoformat()),
        updated_at=row.get("updated_at", datetime.now(timezone.utc).isoformat()),
    )


@api_router.get("/admin/administrators", response_model=List[AdminAccountResponse])
async def list_admin_accounts(request: Request):
    await _require_admin_permission(request, "administradores")
    rows = await db.users.find({"isAdmin": True}, {"_id": 0}).sort("created_at", 1).to_list(500)
    return [_to_admin_account_response(row) for row in rows]


@api_router.post("/admin/administrators", response_model=AdminAccountResponse, status_code=201)
async def create_admin_account(payload: AdminAccountCreatePayload, request: Request):
    requester = await _require_admin_permission(request, "administradores")
    role = payload.role

    if role == "super_admin" and not _is_super_admin_user(requester):
        raise HTTPException(status_code=403, detail="Apenas Super Admin pode criar outro Super Admin")

    email = payload.email.strip().lower()
    existing = await db.users.find_one({"email": email}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=409, detail="Email já cadastrado")

    permissions = _normalize_admin_permissions(payload.permissions)
    if role == "super_admin":
        permissions = _full_permissions_map()
    elif not _enabled_permissions(permissions):
        permissions = _default_admin_permissions_map()

    _validate_permission_assignment(requester, permissions)

    now_iso = datetime.now(timezone.utc).isoformat()
    new_admin = {
        "id": str(uuid.uuid4()),
        "full_name": payload.full_name.strip(),
        "email": email,
        "password_hash": _hash_password(payload.password),
        "role": role,
        "isAdmin": True,
        "permissions": permissions,
        "isActive": payload.isActive,
        "created_by": requester.get("id"),
        "possuiSeguro": False,
        "accepted_terms": True,
        "accepted_privacy": True,
        "accepted_lgpd": True,
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_active_at": now_iso,
    }
    await db.users.insert_one(new_admin)
    await _log_admin_action(
        requester,
        "create_admin",
        new_admin["id"],
        {
            "role": role,
            "isActive": payload.isActive,
            "permissions": permissions,
        },
    )
    return _to_admin_account_response(new_admin)


@api_router.patch("/admin/administrators/{admin_id}", response_model=AdminAccountResponse)
async def update_admin_account(admin_id: str, payload: AdminAccountUpdatePayload, request: Request):
    requester = await _require_admin_permission(request, "administradores")
    current = await db.users.find_one({"id": admin_id, "isAdmin": True}, {"_id": 0})
    if not current:
        raise HTTPException(status_code=404, detail="Administrador não encontrado")

    if current.get("role") == "super_admin" and not _is_super_admin_user(requester):
        raise HTTPException(status_code=403, detail="Apenas Super Admin pode editar este usuário")

    target_role = payload.role or current.get("role", "admin")
    if target_role == "super_admin" and not _is_super_admin_user(requester):
        raise HTTPException(status_code=403, detail="Apenas Super Admin pode conceder acesso total")

    if payload.permissions is None:
        target_permissions = _normalize_admin_permissions(current.get("permissions", {}))
    else:
        target_permissions = _normalize_admin_permissions(payload.permissions)

    if target_role == "super_admin":
        target_permissions = _full_permissions_map()
    elif not _enabled_permissions(target_permissions):
        target_permissions = _default_admin_permissions_map()

    _validate_permission_assignment(requester, target_permissions)

    target_active = current.get("isActive", True) if payload.isActive is None else payload.isActive
    if current.get("role") == "super_admin" and target_active is False:
        active_super_admins = await db.users.count_documents({"role": "super_admin", "isAdmin": True, "isActive": True})
        if active_super_admins <= 1:
            raise HTTPException(status_code=400, detail="Não é possível desativar o único Super Admin ativo")

    updates = {
        "full_name": payload.full_name.strip() if payload.full_name else current.get("full_name", ""),
        "role": target_role,
        "isAdmin": True,
        "permissions": target_permissions,
        "isActive": target_active,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload.password:
        updates["password_hash"] = _hash_password(payload.password)

    await db.users.update_one({"id": admin_id}, {"$set": updates})
    updated = {**current, **updates}
    await _log_admin_action(
        requester,
        "update_admin",
        admin_id,
        {
            "role": target_role,
            "isActive": target_active,
            "permissions": target_permissions,
            "password_reset": bool(payload.password),
        },
    )
    return _to_admin_account_response(updated)


@api_router.get("/admin/administrators/logs", response_model=List[AdminActionLogResponse])
async def list_admin_action_logs(request: Request):
    await _require_admin_permission(request, "administradores")
    rows = await db.admin_action_logs.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return [AdminActionLogResponse(**row) for row in rows]


@api_router.get("/admin/pending-actions", response_model=List[PendingActionResponse])
async def list_admin_pending_actions(request: Request):
    await _require_admin_permission(request, "dashboard")

    actions: List[PendingActionResponse] = []

    partner_rows = await db.partner_applications.find({"status": "Em análise"}, {"_id": 0}).to_list(100)
    for row in partner_rows:
        actions.append(
            PendingActionResponse(
                id=f"partner-{row['id']}",
                type="passeador_aprovacao",
                description=f"Passeador {row.get('full_name', 'Sem nome')} aguardando aprovação",
                action_route=f"/admin/candidato/{row['id']}",
                created_at=row.get("updated_at", row.get("created_at", datetime.now(timezone.utc).isoformat())),
            )
        )

    shared_rows = await db.walks.find(
        {"walk_type": WALK_TYPE_SHARED, "shared_approved": False, "status": {"$ne": STATUS_CANCELED}},
        {"_id": 0},
    ).to_list(150)
    for row in shared_rows:
        actions.append(
            PendingActionResponse(
                id=f"shared-{row['id']}",
                type="compartilhado_aprovacao",
                description=f"Passeio compartilhado de {row.get('pet_name', 'pet')} aguardando aprovação",
                action_route=f"/admin/passeio/{row['id']}",
                created_at=row.get("updated_at", row.get("created_at", datetime.now(timezone.utc).isoformat())),
            )
        )

    premium_analysis_rows = await db.walks.find(
        {"statusAnaliseDeslocamento": PREMIUM_ANALYSIS_WAITING, "status": {"$ne": STATUS_CANCELED}},
        {"_id": 0},
    ).to_list(150)
    for row in premium_analysis_rows:
        actions.append(
            PendingActionResponse(
                id=f"premium-{row['id']}",
                type="deslocamento_premium",
                description=f"Deslocamento premium de {row.get('pet_name', 'pet')} aguardando análise manual",
                action_route=f"/admin/passeio/{row['id']}",
                created_at=row.get("updated_at", row.get("created_at", datetime.now(timezone.utc).isoformat())),
            )
        )

    payment_rows = await db.payments.find({"payment_status": "Pendente"}, {"_id": 0}).to_list(150)
    for row in payment_rows:
        actions.append(
            PendingActionResponse(
                id=f"payment-{row['id']}",
                #type="pagamento_pendente",
                #description=f"Pagamento pendente de {row.get('client_name', 'cliente')} (R$ {float(row.get('value', 0)):.2f})",
                #action_route=f"/admin/pagamento/{row['id']}",
                created_at=row.get("updated_at", row.get("created_at", datetime.now(timezone.utc).isoformat())),
            )
        )

    ticket_rows = await db.support_tickets.find({"status": {"$in": ["aberto", "em_andamento"]}}, {"_id": 0}).to_list(150)
    for row in ticket_rows:
        actions.append(
            PendingActionResponse(
                id=f"ticket-{row['id']}",
                type="ticket_suporte",
                description=f"Ticket '{row.get('subject', 'Sem assunto')}' aguardando retorno",
                action_route="/admin/suporte",
                created_at=row.get("updated_at", row.get("created_at", datetime.now(timezone.utc).isoformat())),
            )
        )

    actions.sort(key=lambda item: item.created_at, reverse=True)
    return actions


@api_router.get("/admin/messages", response_model=List[AdminMessageCampaignResponse])
async def list_admin_messages(request: Request):
    await _require_admin_permission(request, "suporte")
    rows = await db.admin_message_campaigns.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return [AdminMessageCampaignResponse(**row) for row in rows]


@api_router.post("/admin/messages", response_model=AdminMessageCampaignResponse, status_code=201)
async def create_admin_message(payload: AdminMessageCreatePayload, request: Request):
    admin_user = await _require_admin_permission(request, "suporte")
    now = datetime.now(timezone.utc)
    active_filter = {"$or": [{"isActive": True}, {"isActive": {"$exists": False}}]}

    if payload.audience == "todos_usuarios":
        target_query = {"role": {"$in": ["cliente", "passeador"]}, **active_filter}
    elif payload.audience == "usuarios_inativos":
        cutoff = (now - timedelta(days=14)).isoformat()
        target_query = {
            "role": {"$in": ["cliente", "passeador"]},
            **active_filter,
            "last_active_at": {"$lte": cutoff},
        }
    else:
        target_query = {"role": "passeador", **active_filter}

    targets = await db.users.find(target_query, {"_id": 0}).to_list(500)
    for target in targets:
        await _create_notification(
            user_id=target["id"],
            role=target.get("role", "cliente"),
            title=payload.title.strip(),
            message=payload.message.strip(),
            category="admin_mensagem",
        )

    campaign = {
        "id": str(uuid.uuid4()),
        "title": payload.title.strip(),
        "message": payload.message.strip(),
        "audience": payload.audience,
        "sent_count": len(targets),
        "created_by": admin_user.get("id", ""),
        "created_at": now.isoformat(),
    }
    await db.admin_message_campaigns.insert_one(campaign)

    await _notify_admins(
        title="Campanha enviada",
        message=f"Mensagem '{campaign['title']}' enviada para {campaign['sent_count']} usuários.",
        category="admin_operacional",
    )

    return AdminMessageCampaignResponse(**campaign)


@api_router.get("/admin/premium-config", response_model=PremiumSettingsResponse)
async def get_admin_premium_config(request: Request):
    await _require_admin_permission(request, "configuracoes")
    settings = await _get_operational_settings()
    return PremiumSettingsResponse(premiumRepassePercentual=settings.get("premiumRepassePercentual", DEFAULT_PREMIUM_PAYOUT_PERCENT))


@api_router.patch("/admin/premium-config", response_model=PremiumSettingsResponse)
async def update_admin_premium_config(payload: PremiumSettingsUpdate, request: Request):
    await _require_admin_permission(request, "configuracoes")
    percent = min(80.0, max(70.0, _coerce_float(payload.premiumRepassePercentual, DEFAULT_PREMIUM_PAYOUT_PERCENT)))
    await db.operational_settings.update_one(
        {"id": "pricing"},
        {"$set": {"premiumRepassePercentual": percent, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return PremiumSettingsResponse(premiumRepassePercentual=percent)


@api_router.get("/admin/premium-verified/settings", response_model=PremiumVerifiedSettingsResponse)
async def get_admin_premium_verified_settings(request: Request):
    await _require_admin_permission(request, "configuracoes")
    settings = await _get_premium_verified_settings_dict()
    return PremiumVerifiedSettingsResponse(**settings)


@api_router.patch("/admin/premium-verified/settings", response_model=PremiumVerifiedSettingsResponse)
async def update_admin_premium_verified_settings(payload: PremiumVerifiedSettingsUpdate, request: Request):
    admin_user = await _require_admin_permission(request, "configuracoes")
    current = await _get_premium_verified_settings_dict()
    updates = payload.model_dump(exclude_none=True)
    merged = {**current, **updates}
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    merged["updated_by"] = str(admin_user.get("id") or "admin")

    await db.premium_verified_settings.update_one({"id": "default"}, {"$set": merged}, upsert=True)
    return PremiumVerifiedSettingsResponse(**merged)


@api_router.get("/walker/premium-verified-status", response_model=WalkerPremiumVerifiedStatusResponse)
async def get_walker_premium_verified_status(request: Request):
    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    settings = await _get_premium_verified_settings_dict()
    streak_target = int(settings.get("streak_minimo_para_selo") or DEFAULT_PREMIUM_VERIFIED_STREAK_TARGET)
    streak = int(user.get("premium_verified_streak", 0) or 0)
    active = bool(
        user.get("premium_verified_badge_active", False)
        and _is_feature_active("premium_verified_badge_enabled")
        and _is_feature_active("premium_verified_enabled")
    )
    reason = str(user.get("premium_verified_last_reason") or "")
    if not reason:
        reason = "Checklist de segurança pendente para ativar selo" if not active else "Checklist de segurança cumprido"
    return WalkerPremiumVerifiedStatusResponse(
        badge_active=active,
        reason=reason,
        streak_atual=streak,
        streak_minimo_para_selo=streak_target,
        progresso=f"{min(streak, streak_target)}/{streak_target}",
        infracoes_consecutivas=int(user.get("premium_verified_infractions_consecutive", 0) or 0),
        penalty_level=str(user.get("premium_verified_penalty_level") or "none"),
        bonus_score_base_aplicavel=_coerce_float(settings.get("bonus_score_base"), DEFAULT_PREMIUM_VERIFIED_BONUS_SCORE),
        cr_efficiency_multiplier=(
            _coerce_float(settings.get("cr_efficiency_multiplier"), DEFAULT_PREMIUM_VERIFIED_CR_EFFICIENCY_MULTIPLIER)
            if active and _is_feature_active("premium_verified_bonus_enabled")
            else 1.0
        ),
    )


@api_router.get("/admin/coupons", response_model=List[CouponResponse])
async def list_admin_coupons(request: Request):
    #await _require_admin_permission(request, "pagamentos")
    coupons = await db.coupons.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return [_to_coupon_response(coupon) for coupon in coupons]


@api_router.post("/admin/coupons", response_model=CouponResponse, status_code=201)
async def create_admin_coupon(payload: CouponCreatePayload, request: Request):
    #await _require_admin_permission(request, "pagamentos")

    code = _normalize_coupon_code(payload.code)
    if len(code) < 3:
        raise HTTPException(status_code=422, detail="Código do cupom inválido")

    if payload.max_global_uses is None:
        raise HTTPException(status_code=400, detail="Limite global obrigatório para novos cupons")
    if not str(payload.valid_from or "").strip() or not str(payload.valid_until or "").strip():
        raise HTTPException(status_code=400, detail="Validade inicial e final são obrigatórias para novos cupons")

    discount_percent = min(100.0, max(0.0, _coerce_float(payload.discount_percent, 0.0)))
    discount_fixed = max(0.0, _coerce_float(payload.discount_fixed, 0.0))
    if discount_percent <= 0 and discount_fixed <= 0:
        raise HTTPException(status_code=400, detail="Informe desconto percentual e/ou valor fixo")

    valid_from_dt = _parse_coupon_datetime_input(payload.valid_from, end_of_day=False)
    valid_until_dt = _parse_coupon_datetime_input(payload.valid_until, end_of_day=True)
    if valid_from_dt and valid_until_dt and valid_until_dt < valid_from_dt:
        raise HTTPException(status_code=400, detail="Validade final deve ser posterior ao início")

    existing = await db.coupons.find_one({"code": code}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=409, detail="Já existe um cupom com este código")

    now_iso = datetime.now(timezone.utc).isoformat()
    coupon_row = {
        "id": str(uuid.uuid4()),
        "code": code,
        "discount_percent": round(discount_percent, 2),
        "discount_fixed": round(discount_fixed, 2),
        "valid_from": valid_from_dt.isoformat() if valid_from_dt else None,
        "valid_until": valid_until_dt.isoformat() if valid_until_dt else None,
        "max_global_uses": int(payload.max_global_uses),
        "max_uses_per_user": max(1, int(payload.max_uses_per_user or 1)),
        "used_count": 0,
        "applicable_walk_types": _normalize_coupon_walk_types(payload.applicable_walk_types),
        "is_active": bool(payload.is_active),
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await db.coupons.insert_one(coupon_row)
    return _to_coupon_response(coupon_row)


@api_router.patch("/admin/coupons/{coupon_id}", response_model=CouponResponse)
async def update_admin_coupon(coupon_id: str, payload: CouponUpdatePayload, request: Request):
    #await _require_admin_permission(request, "pagamentos")
    current = await db.coupons.find_one({"id": coupon_id}, {"_id": 0})
    if not current:
        raise HTTPException(status_code=404, detail="Cupom não encontrado")

    updates: Dict[str, Any] = {}
    if payload.discount_percent is not None:
        updates["discount_percent"] = round(min(100.0, max(0.0, _coerce_float(payload.discount_percent, 0.0))), 2)
    if payload.discount_fixed is not None:
        updates["discount_fixed"] = round(max(0.0, _coerce_float(payload.discount_fixed, 0.0)), 2)
    if payload.valid_from is not None:
        valid_from_dt = _parse_coupon_datetime_input(payload.valid_from, end_of_day=False)
        updates["valid_from"] = valid_from_dt.isoformat() if valid_from_dt else None
    if payload.valid_until is not None:
        valid_until_dt = _parse_coupon_datetime_input(payload.valid_until, end_of_day=True)
        updates["valid_until"] = valid_until_dt.isoformat() if valid_until_dt else None
    if payload.max_global_uses is not None:
        updates["max_global_uses"] = int(payload.max_global_uses)
    if payload.max_uses_per_user is not None:
        updates["max_uses_per_user"] = max(1, int(payload.max_uses_per_user))
    if payload.applicable_walk_types is not None:
        updates["applicable_walk_types"] = _normalize_coupon_walk_types(payload.applicable_walk_types)
    if payload.is_active is not None:
        updates["is_active"] = bool(payload.is_active)

    merged_discount_percent = _coerce_float(updates.get("discount_percent", current.get("discount_percent", 0.0)), 0.0)
    merged_discount_fixed = _coerce_float(updates.get("discount_fixed", current.get("discount_fixed", 0.0)), 0.0)
    if merged_discount_percent <= 0 and merged_discount_fixed <= 0:
        raise HTTPException(status_code=400, detail="O cupom precisa manter algum tipo de desconto")

    merged_valid_from = _parse_iso_datetime(updates.get("valid_from", current.get("valid_from")))
    merged_valid_until = _parse_iso_datetime(updates.get("valid_until", current.get("valid_until")))
    if merged_valid_from and merged_valid_until and merged_valid_until < merged_valid_from:
        raise HTTPException(status_code=400, detail="Validade final deve ser posterior ao início")

    merged_max_global_uses = int(updates.get("max_global_uses", current.get("max_global_uses", 0)) or 0)
    if merged_max_global_uses <= 0:
        raise HTTPException(status_code=400, detail="Limite global obrigatório para edições de cupom")
    if not merged_valid_from or not merged_valid_until:
        raise HTTPException(status_code=400, detail="Validade inicial e final são obrigatórias em edições de cupom")

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db.coupons.update_one({"id": coupon_id}, {"$set": updates})
    updated = await db.coupons.find_one({"id": coupon_id}, {"_id": 0})
    return _to_coupon_response(updated)


@api_router.get("/admin/coupons/anti-abuse/overview", response_model=CouponAntiAbuseOverviewResponse)
async def get_admin_coupon_anti_abuse_overview(request: Request):
    #await _require_admin_permission(request, "pagamentos")
    alerts = await db.coupon_fraud_alerts.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    redemptions = await db.coupon_redemptions.find({}, {"_id": 0}).sort("used_at", -1).to_list(3000)

    usage_by_user_map: Dict[str, dict] = {}
    usage_by_device_map: Dict[str, dict] = {}
    usage_by_ip_map: Dict[str, dict] = {}

    for row in redemptions:
        coupon_code = str(row.get("coupon_code") or "")
        user_key = str(row.get("user_id") or row.get("user_email") or row.get("user_phone") or "anônimo")
        device_key = str(row.get("device_id") or "sem-device")
        ip_key = str(row.get("ip_address") or "sem-ip")

        if user_key not in usage_by_user_map:
            usage_by_user_map[user_key] = {"key": user_key, "uses_count": 0, "users": set(), "coupons": set()}
        usage_by_user_map[user_key]["uses_count"] += 1
        if row.get("user_id"):
            usage_by_user_map[user_key]["users"].add(str(row.get("user_id")))
        usage_by_user_map[user_key]["coupons"].add(coupon_code)

        if device_key not in usage_by_device_map:
            usage_by_device_map[device_key] = {"key": device_key, "uses_count": 0, "users": set(), "coupons": set()}
        usage_by_device_map[device_key]["uses_count"] += 1
        if row.get("user_id"):
            usage_by_device_map[device_key]["users"].add(str(row.get("user_id")))
        usage_by_device_map[device_key]["coupons"].add(coupon_code)

        if ip_key not in usage_by_ip_map:
            usage_by_ip_map[ip_key] = {"key": ip_key, "uses_count": 0, "users": set(), "coupons": set()}
        usage_by_ip_map[ip_key]["uses_count"] += 1
        if row.get("user_id"):
            usage_by_ip_map[ip_key]["users"].add(str(row.get("user_id")))
        usage_by_ip_map[ip_key]["coupons"].add(coupon_code)

    def _to_aggregate_rows(source_map: Dict[str, dict]) -> List[CouponFraudAggregateResponse]:
        sorted_items = sorted(source_map.values(), key=lambda item: item.get("uses_count", 0), reverse=True)[:30]
        return [
            CouponFraudAggregateResponse(
                key=item["key"],
                uses_count=int(item.get("uses_count", 0)),
                unique_users=len(item.get("users", set())),
                unique_coupons=len(item.get("coupons", set())),
            )
            for item in sorted_items
        ]

    alert_rows = [
        CouponFraudAlertResponse(
            id=str(alert.get("id") or ""),
            alert_type=str(alert.get("alert_type") or ""),
            severity=str(alert.get("severity") or "medium"),
            message=str(alert.get("message") or "Alerta de uso suspeito"),
            coupon_id=alert.get("coupon_id"),
            coupon_code=alert.get("coupon_code"),
            user_id=alert.get("user_id"),
            user_email=alert.get("user_email"),
            user_phone=alert.get("user_phone"),
            device_id=alert.get("device_id"),
            ip_address=alert.get("ip_address"),
            blocked=bool(alert.get("blocked", False)),
            created_at=str(alert.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )
        for alert in alerts
    ]

    return CouponAntiAbuseOverviewResponse(
        alerts=alert_rows,
        usage_by_user=_to_aggregate_rows(usage_by_user_map),
        usage_by_device=_to_aggregate_rows(usage_by_device_map),
        usage_by_ip=_to_aggregate_rows(usage_by_ip_map),
    )


@api_router.patch("/admin/coupons/{coupon_id}/invalidate", response_model=CouponResponse)
async def invalidate_admin_coupon(coupon_id: str, request: Request):
    #await _require_admin_permission(request, "pagamentos")
    coupon = await db.coupons.find_one({"id": coupon_id}, {"_id": 0})
    if not coupon:
        raise HTTPException(status_code=404, detail="Cupom não encontrado")

    await db.coupons.update_one(
        {"id": coupon_id},
        {
            "$set": {
                "is_active": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    updated = await db.coupons.find_one({"id": coupon_id}, {"_id": 0})
    return _to_coupon_response(updated)


@api_router.patch("/admin/coupons/anti-abuse/users/{user_id}/block")
async def block_coupon_user_manually(user_id: str, request: Request):
    #await _require_admin_permission(request, "pagamentos")
    user_row = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user_row:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"id": user_id},
        {
            "$set": {
                "is_coupon_blocked": True,
                "is_suspected": True,
                "updated_at": now_iso,
            },
            "$addToSet": {"suspicion_reasons": "Bloqueio manual de cupom pela administração"},
        },
    )
    await db.coupon_fraud_alerts.insert_one(
        {
            "id": str(uuid.uuid4()),
            "alert_type": "manual_user_coupon_block",
            "severity": "high",
            "message": "Usuário bloqueado manualmente para uso de cupom.",
            "coupon_id": None,
            "coupon_code": None,
            "user_id": user_id,
            "user_email": user_row.get("email"),
            "user_phone": user_row.get("phone"),
            "device_id": user_row.get("registration_device_id"),
            "ip_address": user_row.get("registration_ip"),
            "blocked": True,
            "metadata": {},
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )
    return {"message": "Usuário bloqueado para uso de cupons"}


@api_router.get("/admin/incentives/settings", response_model=IncentiveSettingsResponse)
async def get_admin_incentive_settings(request: Request):
    #await _require_admin_permission(request, "pagamentos")
    settings = await _get_incentive_settings_dict()
    return IncentiveSettingsResponse(**settings)


@api_router.patch("/admin/incentives/settings", response_model=IncentiveSettingsResponse)
async def update_admin_incentive_settings(payload: IncentiveSettingsUpdatePayload, request: Request):
    #admin_user = await _require_admin_permission(request, "pagamentos")
    current = await _get_incentive_settings_dict()
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return IncentiveSettingsResponse(**current)

    merged = dict(current)
    merged.update(updates)

    walker_share = _coerce_float(merged.get("walker_share_percent"), DEFAULT_WALKER_SHARE_PERCENT)
    platform_share = _coerce_float(merged.get("platform_share_percent"), DEFAULT_PLATFORM_SHARE_PERCENT)
    total_share = walker_share + platform_share
    if total_share <= 0:
        raise HTTPException(status_code=400, detail="Percentuais inválidos para divisão de ganhos")
    walker_share = round((walker_share / total_share) * 100.0, 2)
    platform_share = round(100.0 - walker_share, 2)
    if walker_share < 70 or walker_share > 80:
        raise HTTPException(status_code=400, detail="Participação do passeador deve ficar entre 70% e 80%")

    volume_tiers = merged.get("volume_bonus_tiers") or []
    normalized_tiers = sorted(
        [
            {
                "target_walks": max(1, int(_coerce_float(item.get("target_walks"), 0))),
                "amount": round(max(0.0, _coerce_float(item.get("amount"), 0.0)), 2),
            }
            for item in volume_tiers
            if _coerce_float(item.get("target_walks"), 0) > 0
        ],
        key=lambda row: row["target_walks"],
    )
    if not normalized_tiers:
        normalized_tiers = [dict(item) for item in DEFAULT_VOLUME_BONUS_TIERS]

    merged.update(
        {
            "walker_share_percent": walker_share,
            "platform_share_percent": platform_share,
            "quality_bonus_percent": round(max(0.0, _coerce_float(merged.get("quality_bonus_percent"), DEFAULT_QUALITY_BONUS_PERCENT)), 2),
            "quality_bonus_min_weighted": round(max(0.0, _coerce_float(merged.get("quality_bonus_min_weighted"), DEFAULT_QUALITY_BONUS_MIN_WEIGHTED)), 2),
            "quality_bonus_min_walks": max(1, int(_coerce_float(merged.get("quality_bonus_min_walks"), DEFAULT_QUALITY_BONUS_MIN_WALKS))),
            "consistency_bonus_amount": round(max(0.0, _coerce_float(merged.get("consistency_bonus_amount"), DEFAULT_CONSISTENCY_BONUS_AMOUNT)), 2),
            "consistency_days_required": max(1, int(_coerce_float(merged.get("consistency_days_required"), DEFAULT_CONSISTENCY_DAYS_REQUIRED))),
            "critical_hour_bonus_amount": round(max(0.0, _coerce_float(merged.get("critical_hour_bonus_amount"), DEFAULT_CRITICAL_HOUR_BONUS_AMOUNT)), 2),
            "critical_windows": merged.get("critical_windows") or DEFAULT_CRITICAL_WINDOWS.copy(),
            "volume_bonus_tiers": normalized_tiers,
            "enabled": bool(merged.get("enabled", True)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    await db.incentive_settings.update_one({"id": "default"}, {"$set": merged}, upsert=True)
    await db.incentive_settings_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "admin_user_id": admin_user.get("id"),
            "admin_email": admin_user.get("email"),
            "changes": updates,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _refresh_runtime_incentive_split(merged)
    return IncentiveSettingsResponse(**merged)


@api_router.get("/admin/referral-program/settings", response_model=ReferralProgramSettingsResponse)
async def get_admin_referral_program_settings(request: Request):
    await _require_admin_permission(request, "configuracoes")
    settings = await _get_referral_program_settings_dict()
    return ReferralProgramSettingsResponse(**settings)


@api_router.patch("/admin/referral-program/settings", response_model=ReferralProgramSettingsResponse)
async def update_admin_referral_program_settings(payload: ReferralProgramSettingsUpdatePayload, request: Request):
    admin_user = await _require_admin_permission(request, "configuracoes")
    current = await _get_referral_program_settings_dict()
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return ReferralProgramSettingsResponse(**current)

    merged = dict(current)
    merged.update({k: v for k, v in updates.items() if k not in {"client_rules", "walker_rules"}})
    if payload.client_rules is not None:
        merged["client_rules"] = payload.client_rules.model_dump()
    if payload.walker_rules is not None:
        merged["walker_rules"] = payload.walker_rules.model_dump()

    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    merged["updated_by"] = str(admin_user.get("id") or "admin")

    await db.referral_program_settings.update_one({"id": "default"}, {"$set": merged}, upsert=True)
    await db.referral_program_settings_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "admin_user_id": admin_user.get("id"),
            "admin_email": admin_user.get("email"),
            "changes": updates,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    flag_updates = {
        "referral_program": {
            "is_active": bool(merged.get("program_enabled", False)),
            "is_visible": bool(merged.get("app_visible", False)),
        },
        "client_referral_system": {
            "is_active": bool(merged.get("program_enabled", False) and merged.get("client_referral_enabled", False)),
            "is_visible": bool(merged.get("app_visible", False) and merged.get("client_referral_enabled", False)),
        },
        "walker_referral": {
            "is_active": bool(merged.get("program_enabled", False) and merged.get("walker_referral_enabled", False)),
            "is_visible": bool(merged.get("app_visible", False) and merged.get("walker_referral_enabled", False)),
        },
    }

    for feature_name, values in flag_updates.items():
        await db.feature_flags.update_one(
            {"feature_name": feature_name},
            {
                "$set": {
                    "is_active": values["is_active"],
                    "is_visible": values["is_visible"],
                    "updated_at": merged["updated_at"],
                    "updated_by": merged["updated_by"],
                }
            },
            upsert=True,
        )

    rows = await db.feature_flags.find({}, {"_id": 0}).to_list(300)
    _refresh_runtime_feature_flags(rows)

    return ReferralProgramSettingsResponse(**merged)


@api_router.get("/admin/referrals", response_model=AdminReferralListResponse)
async def list_admin_referrals(
    request: Request,
    status: Optional[ReferralStatus] = None,
    referral_type: Optional[ReferralType] = None,
    limit: int = 100,
):
    await _require_admin_permission(request, "configuracoes")
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    if referral_type:
        query["referral_type"] = referral_type

    rows = await db.referrals.find(query, {"_id": 0}).sort("created_at", -1).to_list(max(1, min(500, limit)))
    items = [_to_referral_record_response(row) for row in rows]
    return AdminReferralListResponse(items=items, total=len(items))


@api_router.patch("/admin/referrals/{referral_id}/status", response_model=ReferralRecordResponse)
async def update_admin_referral_status(referral_id: str, payload: AdminReferralStatusUpdatePayload, request: Request):
    admin_user = await _require_admin_permission(request, "configuracoes")
    referral = await db.referrals.find_one({"id": referral_id}, {"_id": 0})
    if not referral:
        raise HTTPException(status_code=404, detail="Indicação não encontrada")

    updated = dict(referral)
    updated["status"] = payload.status
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    _append_referral_audit_event(
        updated,
        "admin_status_update",
        payload.note or f"Status alterado para {payload.status}",
        actor_user_id=str(admin_user.get("id") or ""),
    )
    await db.referrals.update_one({"id": referral_id}, {"$set": updated})
    return _to_referral_record_response(updated)


@api_router.get("/admin/pet-transport/settings", response_model=PetTransportSettingsResponse)
async def get_admin_pet_transport_settings(request: Request):
    await _require_admin_permission(request, "configuracoes")
    settings = await _get_pet_transport_settings_dict()
    return PetTransportSettingsResponse(**settings)


@api_router.patch("/admin/pet-transport/settings", response_model=PetTransportSettingsResponse)
async def update_admin_pet_transport_settings(payload: PetTransportSettingsUpdate, request: Request):
    admin_user = await _require_admin_permission(request, "configuracoes")
    current = await _get_pet_transport_settings_dict()
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return PetTransportSettingsResponse(**current)

    merged = dict(current)
    if "pet_transport_enabled_for" in updates:
        updates["pet_transport_enabled_for"] = _sanitize_pet_transport_enabled_for(updates.get("pet_transport_enabled_for"))
    merged.update(updates)
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    merged["updated_by"] = str(admin_user.get("id") or "admin")

    await db.pet_transport_settings.update_one({"id": "default"}, {"$set": merged}, upsert=True)
    return PetTransportSettingsResponse(**merged)


@api_router.get("/walker/transport-settings", response_model=WalkerTransportSettingsResponse)
async def get_walker_transport_settings(request: Request):
    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    has_vehicle = bool(user.get("possuiVeiculo", False))
    accepts_transport = bool(user.get("aceitaDeslocamentoPremium", False))
    transport_enabled = bool(user.get("ativoParaTransportePremium", False))
    return WalkerTransportSettingsResponse(
        has_vehicle=has_vehicle,
        accepts_pet_transport=accepts_transport,
        vehicle_type=str(user.get("tipoVeiculo", "") or ""),
        transport_enabled=transport_enabled,
        is_transport_eligible=has_vehicle and accepts_transport and transport_enabled,
        updated_at=str(user.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    )


def _walker_kit_response_from_user(user_doc: dict) -> WalkerCertifiedKitResponse:
    kit_profile = _walker_kit_profile_from_user(user_doc)
    missing_reports_count = int(user_doc.get("kit_missing_reports_count", 0) or 0)
    kit_level = _kit_effective_level(kit_profile, missing_reports_count)
    kit_labels = _kit_labels_from_level(kit_level)
    raw_photos = user_doc.get("kit_photos_base64", [])
    photos = [str(item) for item in raw_photos if isinstance(item, str) and item.startswith("data:image/")][:5]
    raw_photo_urls = user_doc.get("kit_photo_urls", [])
    photo_urls = [str(item) for item in raw_photo_urls if isinstance(item, str) and item.startswith("/")][:3]

    return WalkerCertifiedKitResponse(
        walker_kit={
            "water_sealed": bool(kit_profile.get("water_sealed", False)),
            "water_bowl": bool(kit_profile.get("water_bowl", False)),
            "poop_bags": bool(kit_profile.get("poop_bags", False)),
            "first_aid_kit": bool(kit_profile.get("first_aid_kit", False)),
            "kit_complete": bool(kit_profile.get("kit_complete", False)),
        },
        water_sealed=bool(kit_profile.get("water_sealed", False)),
        water_bowl=bool(kit_profile.get("water_bowl", False)),
        poop_bags=bool(kit_profile.get("poop_bags", False)),
        first_aid_kit=bool(kit_profile.get("first_aid_kit", False)),
        kit_complete=bool(kit_profile.get("kit_complete", False)),
        has_water=bool(kit_profile.get("has_water", False)),
        has_bowl=bool(kit_profile.get("has_bowl", False)),
        has_bags=bool(kit_profile.get("has_bags", False)),
        has_first_aid=bool(kit_profile.get("has_first_aid", False)),
        has_towel=bool(kit_profile.get("has_towel", False)),
        has_extra_leash=bool(kit_profile.get("has_extra_leash", False)),
        has_premium_items=bool(kit_profile.get("has_premium_items", False)),
        kit_basic_complete=bool(kit_profile.get("kit_basic_complete", False)),
        kit_essential_complete=bool(kit_profile.get("kit_essential_complete", False)),
        kit_premium=bool(kit_profile.get("kit_premium", False)),
        kit_level=kit_level,
        kit_labels=kit_labels,
        kit_photos_base64=photos,
        kit_photo_urls=photo_urls,
        kit_missing_reports_count=missing_reports_count,
        kit_audit_status=str(user_doc.get("kit_audit_status") or "pendente"),
        kit_audit_note=str(user_doc.get("kit_audit_note") or ""),
        kit_audited_at=user_doc.get("kit_audited_at"),
        updated_at=str(user_doc.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    )


@api_router.get("/walker/certified-kit", response_model=WalkerCertifiedKitResponse)
async def get_walker_certified_kit(request: Request):
    if not _is_feature_active("kit_system_enabled"):
        raise HTTPException(status_code=400, detail="Sistema de kit está desativado no momento")

    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    return _walker_kit_response_from_user(user)


@api_router.patch("/walker/certified-kit", response_model=WalkerCertifiedKitResponse)
async def update_walker_certified_kit(payload: WalkerCertifiedKitUpdate, request: Request):
    if not _is_feature_active("kit_system_enabled"):
        raise HTTPException(status_code=400, detail="Sistema de kit está desativado no momento")

    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    user_id = str(user.get("id") or "")
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return _walker_kit_response_from_user(user)

    set_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}

    field_aliases = {
        "water_sealed": "has_water",
        "water_bowl": "has_bowl",
        "poop_bags": "has_bags",
        "first_aid_kit": "has_first_aid",
    }
    for source_field, target_field in field_aliases.items():
        if source_field in updates:
            value = bool(updates.get(source_field))
            set_fields[source_field] = value
            set_fields[target_field] = value

    for field in [
        "has_water",
        "has_bowl",
        "has_bags",
        "has_first_aid",
        "has_towel",
        "has_extra_leash",
        "has_premium_items",
    ]:
        if field in updates:
            set_fields[field] = bool(updates.get(field))

    if "kit_photos_base64" in updates:
        raw_photos = updates.get("kit_photos_base64") or []
        photos = [str(item) for item in raw_photos if isinstance(item, str) and item.startswith("data:image/")][:5]
        set_fields["kit_photos_base64"] = photos

    if "kit_photo_urls" in updates:
        urls = [str(item) for item in (updates.get("kit_photo_urls") or []) if isinstance(item, str) and item.startswith("/")][:3]
        set_fields["kit_photo_urls"] = urls

    await db.users.update_one({"id": user_id}, {"$set": set_fields})
    await _recalculate_walker_verification_for_user(
        walker_user_id=user_id,
        trigger="kit_update",
        walk_id=None,
    )
    updated_user = await db.users.find_one({"id": user_id}, {"_id": 0}) or {**user, **set_fields}
    return _walker_kit_response_from_user(updated_user)


@api_router.post("/walker/certified-kit/upload-photo", response_model=WalkerCertifiedKitResponse)
async def upload_walker_kit_photo(request: Request, file: UploadFile = File(...)):
    if not _is_feature_active("kit_system_enabled"):
        raise HTTPException(status_code=400, detail="Sistema de kit está desativado no momento")

    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    user_id = str(user.get("id") or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="Passeador inválido")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=400, detail="Formato inválido. Use JPG, PNG ou WEBP")

    fresh_user = await db.users.find_one({"id": user_id}, {"_id": 0}) or user
    current_urls = [
        str(item)
        for item in ((fresh_user.get("kit_photo_urls") or []) if isinstance(fresh_user.get("kit_photo_urls"), list) else [])
        if isinstance(item, str) and item.startswith("/")
    ]
    if len(current_urls) >= 3:
        raise HTTPException(status_code=400, detail="Limite de 3 fotos do kit atingido")

    file_name = f"kit-{user_id}-{uuid.uuid4().hex}{ext}"
    target = UPLOADS_DIR / file_name
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Arquivo vazio")
    target.write_bytes(content)

    photo_url = f"/uploads/{file_name}"
    current_urls.append(photo_url)
    updates = {
        "kit_photo_urls": current_urls[:3],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.update_one({"id": user_id}, {"$set": updates})

    refreshed = await db.users.find_one({"id": user_id}, {"_id": 0}) or {**fresh_user, **updates}
    return _walker_kit_response_from_user(refreshed)


@api_router.patch("/admin/walker-kit/{walker_user_id}/audit", response_model=WalkerCertifiedKitResponse)
async def audit_walker_certified_kit(walker_user_id: str, payload: WalkerKitAuditUpdate, request: Request):
    admin_user = await _require_admin_permission(request, "passeadores")
    walker_user = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if not walker_user:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    updates = {
        "kit_audit_status": payload.kit_audit_status,
        "kit_audit_note": payload.kit_audit_note.strip(),
        "kit_audited_at": datetime.now(timezone.utc).isoformat(),
        "kit_audited_by": str(admin_user.get("id") or "admin"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.update_one({"id": walker_user_id}, {"$set": updates})
    refreshed = await db.users.find_one({"id": walker_user_id}, {"_id": 0}) or {**walker_user, **updates}
    return _walker_kit_response_from_user(refreshed)


@api_router.get("/walker/reputation-credits", response_model=WalkerReputationCreditsResponse)
async def get_walker_reputation_credits(request: Request):
    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    return await _build_reputation_credits_response_for_user(user)


@api_router.post("/walker/reputation-credits/use", response_model=WalkerReputationCreditsResponse)
async def use_walker_reputation_credits(payload: WalkerReputationCreditsUsePayload, request: Request):
    if not _is_feature_active("cr_system_enabled"):
        raise HTTPException(status_code=400, detail="Sistema de CR está desativado no momento")

    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    walker_user_id = str(user.get("id") or "")
    if not walker_user_id:
        raise HTTPException(status_code=400, detail="Passeador inválido")

    action = payload.action
    base_cost = int(CR_ACTION_COSTS.get(action, 0))
    if base_cost <= 0:
        raise HTTPException(status_code=400, detail="Ação de CR inválida")

    level = str(user.get("verification_level") or VERIFICATION_LEVEL_NONE)
    usage_cost_multiplier, usage_effect_multiplier = _cr_usage_multipliers(level, str(user.get("walker_level") or ""))
    final_cost = max(1, int(round(base_cost * usage_cost_multiplier)))

    today_key = _cr_today_key()
    daily_uses_count = (
        int(user.get("cr_daily_uses_count", 0) or 0)
        if str(user.get("cr_daily_uses_date") or "") == today_key
        else 0
    )
    if daily_uses_count >= CR_DAILY_USES_LIMIT:
        raise HTTPException(status_code=400, detail="Limite diário de uso de CR atingido")

    current_credits = int(user.get("reputation_credits", 0) or 0)
    if current_credits < final_cost:
        raise HTTPException(status_code=400, detail="CR insuficiente para esta ação")

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    effect_hours = int(CR_ACTION_DURATIONS_HOURS.get(action, 24))
    effect_until = (now_dt + timedelta(hours=effect_hours)).isoformat()

    active_field_by_action = {
        "matching_boost": "cr_matching_boost_until",
        "early_wave": "cr_early_wave_until",
        "visual_highlight": "cr_visual_highlight_until",
    }
    target_until_field = active_field_by_action[action]
    if _is_cr_effect_active_until(user.get(target_until_field)):
        raise HTTPException(status_code=400, detail="Este efeito de CR já está ativo")

    matching_points = _coerce_float(user.get("cr_matching_boost_points_active"), CR_MATCHING_BOOST_BASE_POINTS)
    early_wave_priority = _coerce_float(user.get("cr_early_wave_priority_active"), CR_EARLY_WAVE_BASE_PRIORITY)
    visual_exposure = _coerce_float(user.get("cr_visual_exposure_points_active"), CR_VISUAL_EXPOSURE_BASE_POINTS)

    updates: Dict[str, Any] = {
        "reputation_credits": max(0, current_credits - final_cost),
        "last_credit_update": now_iso,
        "cr_daily_uses_date": today_key,
        "cr_daily_uses_count": daily_uses_count + 1,
        "updated_at": now_iso,
    }

    if action == "matching_boost":
        updates["cr_matching_boost_until"] = effect_until
        updates["cr_matching_boost_points_active"] = round(CR_MATCHING_BOOST_BASE_POINTS * usage_effect_multiplier, 2)
    elif action == "early_wave":
        updates["cr_early_wave_until"] = effect_until
        updates["cr_early_wave_priority_active"] = round(CR_EARLY_WAVE_BASE_PRIORITY * usage_effect_multiplier, 2)
    elif action == "visual_highlight":
        updates["cr_visual_highlight_until"] = effect_until
        updates["cr_visual_exposure_points_active"] = round(CR_VISUAL_EXPOSURE_BASE_POINTS * usage_effect_multiplier, 2)

    await db.users.update_one({"id": walker_user_id}, {"$set": updates})
    await db.reputation_credit_ledger.insert_one(
        {
            "id": str(uuid.uuid4()),
            "walker_user_id": walker_user_id,
            "delta": -final_cost,
            "reason": f"use_{action}",
            "event_key": f"use:{action}:{now_iso}",
            "balance_after": max(0, current_credits - final_cost),
            "metadata": {
                "base_cost": base_cost,
                "final_cost": final_cost,
                "usage_cost_multiplier": usage_cost_multiplier,
                "usage_effect_multiplier": usage_effect_multiplier,
                "effect_until": effect_until,
                "matching_points_before": matching_points,
                "early_wave_priority_before": early_wave_priority,
                "visual_exposure_before": visual_exposure,
            },
            "created_at": now_iso,
        }
    )

    refreshed_user = await db.users.find_one({"id": walker_user_id}, {"_id": 0}) or {**user, **updates}
    return await _build_reputation_credits_response_for_user(refreshed_user)


@api_router.patch("/walker/transport-settings", response_model=WalkerTransportSettingsResponse)
async def update_walker_transport_settings(payload: WalkerTransportSettingsUpdate, request: Request):
    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    user_id = str(user.get("id") or "")
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return await get_walker_transport_settings(request)

    set_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if payload.has_vehicle is not None:
        set_fields["possuiVeiculo"] = bool(payload.has_vehicle)
    if payload.accepts_pet_transport is not None:
        set_fields["aceitaDeslocamentoPremium"] = bool(payload.accepts_pet_transport)
    if payload.vehicle_type is not None:
        set_fields["tipoVeiculo"] = str(payload.vehicle_type or "").strip()
    if payload.transport_enabled is not None:
        set_fields["ativoParaTransportePremium"] = bool(payload.transport_enabled)

    await db.users.update_one({"id": user_id}, {"$set": set_fields})
    updated_user = await db.users.find_one({"id": user_id}, {"_id": 0}) or {**user, **set_fields}
    has_vehicle = bool(updated_user.get("possuiVeiculo", False))
    accepts_transport = bool(updated_user.get("aceitaDeslocamentoPremium", False))
    transport_enabled = bool(updated_user.get("ativoParaTransportePremium", False))
    return WalkerTransportSettingsResponse(
        has_vehicle=has_vehicle,
        accepts_pet_transport=accepts_transport,
        vehicle_type=str(updated_user.get("tipoVeiculo", "") or ""),
        transport_enabled=transport_enabled,
        is_transport_eligible=has_vehicle and accepts_transport and transport_enabled,
        updated_at=str(updated_user.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    )


@api_router.post("/coupons/validate", response_model=CouponValidateResponse)
async def validate_coupon(payload: CouponValidatePayload, request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin", "super_admin"])
    evaluation = await _evaluate_coupon_for_user(
        code=payload.code,
        user=user,
        request=request,
        walk_type=payload.walk_type,
        subtotal=payload.subtotal,
    )
    coupon = evaluation["coupon"]
    return CouponValidateResponse(
        code=coupon.code,
        discount_percent=coupon.discount_percent,
        discount_fixed=coupon.discount_fixed,
        discount_amount=evaluation["discount_amount"],
        subtotal=evaluation["subtotal"],
        total_after_discount=evaluation["total_after_discount"],
        max_uses_per_user=coupon.max_uses_per_user,
        remaining_uses_for_user=evaluation.get("remaining_uses_for_user"),
        valid_until=coupon.valid_until,
        applicable_walk_types=coupon.applicable_walk_types,
    )


@api_router.post("/walks/premium-estimate", response_model=PremiumEstimateResponse)
async def estimate_walk_premium(payload: PremiumEstimatePayload, request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin", "super_admin"])
    if not await _is_pet_transport_available_for_user(user):
        raise HTTPException(status_code=400, detail="Passeio com transporte está desativado para sua conta")

    geocode_error = "Não conseguimos identificar o local. Tente ser mais específico."
    endereco_base_tutor = f"{payload.pickup_street.strip()}, {payload.pickup_number.strip()} - {payload.pickup_neighborhood.strip()}"
    origem_partes = [endereco_base_tutor]
    if payload.pickup_complement.strip():
        origem_partes.append(payload.pickup_complement.strip())
    if payload.location_reference.strip():
        origem_partes.append(payload.location_reference.strip())
    origem_texto = " • ".join([parte for parte in origem_partes if parte])

    base_geocode = _geocode_location(f"{endereco_base_tutor}, Salvador, BA, Brasil")
    if not base_geocode:
        raise HTTPException(status_code=400, detail=geocode_error)

    destino_nome = payload.local_destino_nome.strip()
    destino_referencia = payload.local_destino_referencia.strip()
    destination_query = f"{destino_nome}, {destino_referencia}, Salvador, BA, Brasil"
    destination_geocode = _geocode_location(destination_query)
    if not destination_geocode:
        raise HTTPException(status_code=400, detail=geocode_error)

    base_latitude = _coerce_float(base_geocode.get("latitude"), -12.9714)
    base_longitude = _coerce_float(base_geocode.get("longitude"), -38.5014)
    destino_lat = _coerce_float(destination_geocode.get("latitude"), 0.0)
    destino_lng = _coerce_float(destination_geocode.get("longitude"), 0.0)
    transport_settings = await _get_pet_transport_settings_dict()
    distancia_km, tempo_deslocamento_minutos = await _estimate_transport_route(
        origin_lat=base_latitude,
        origin_lng=base_longitude,
        destination_lat=destino_lat,
        destination_lng=destino_lng,
        fallback_minutes_per_km=_coerce_float(transport_settings.get("estimated_minutes_per_km"), 3.0),
    )
    adicional_deslocamento, precisa_analise_manual, status_analise, tempo_estimado_por_km = _calculate_premium_transport(
        distancia_km,
        transport_settings,
    )
    tempo_deslocamento = max(tempo_deslocamento_minutos, tempo_estimado_por_km)
    tempo_total = int(payload.duracao_passeio_minutos) + tempo_deslocamento
    tracking_interval_seconds = int(transport_settings.get("tracking_interval_seconds") or 15)

    destino_texto = destino_nome
    if destino_referencia:
        destino_texto = f"{destino_nome} ({destino_referencia})"

    return PremiumEstimateResponse(
        origem=origem_texto,
        destino=destino_texto,
        distanciaKm=round(distancia_km, 2),
        adicionalDeslocamento=round(adicional_deslocamento, 2),
        tipoPasseio="transporte",
        tempoDeslocamentoMinutos=tempo_deslocamento,
        tempoTotalMinutos=tempo_total,
        rastreamentoReforcado=True,
        trackingIntervalSegundos=tracking_interval_seconds,
        precisaAnaliseManualDeslocamento=precisa_analise_manual,
        statusAnaliseDeslocamento=status_analise,
    )


@api_router.post("/plans/simulate", response_model=PlanSimulationResponse)
async def simulate_plan(payload: PlanSimulationPayload, request: Request):
    await _require_role(request, ["cliente", "admin", "super_admin"])
    simulation = _build_plan_simulation(
        frequencia_semanal=int(payload.frequencia_semanal),
        duracao_plano=payload.duracao_plano,
        duracao_passeio=payload.duracao_passeio,
        margem_minima_percent=15.0,
    )
    return PlanSimulationResponse(**simulation)


@api_router.post("/plans/subscription-intent", response_model=PlanSubscriptionIntentResponse)
async def create_plan_subscription_intent(payload: PlanSubscriptionIntentPayload, request: Request):
    user = await _require_role(request, ["cliente", "admin", "super_admin"])
    summary = _build_plan_simulation(
        frequencia_semanal=int(payload.frequencia_semanal),
        duracao_plano=payload.duracao_plano,
        duracao_passeio=payload.duracao_passeio,
        margem_minima_percent=15.0,
    )

    intent_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.plan_subscription_intents.insert_one(
        {
            "id": intent_id,
            "user_id": str(user.get("id") or ""),
            "email": str(user.get("email") or ""),
            "status": "pending_provider_integration",
            "ready_for_subscription": True,
            "payload": summary.get("subscription_payload", {}),
            "summary": summary,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )

    return PlanSubscriptionIntentResponse(
        intent_id=intent_id,
        status="pending_provider_integration",
        ready_for_subscription=True,
        summary=PlanSimulationResponse(**summary),
    )


@api_router.post("/walks", response_model=WalkResponse, status_code=201)
async def create_walk(payload: WalkCreate, request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    selected_walker = await _resolve_walker_profile(payload.walker_id)
    if not selected_walker:
        raise HTTPException(status_code=400, detail="Passeador inválido")

    walker_user = await db.users.find_one(
        {"full_name": selected_walker["name"], "role": "passeador"},
        {"_id": 0},
    )
    walker_user_id = walker_user.get("id") if walker_user else None

    premium_settings = await _get_operational_settings()
    premium_percent = min(
        80.0,
        max(70.0, _coerce_float(premium_settings.get("premiumRepassePercentual", DEFAULT_PREMIUM_PAYOUT_PERCENT), DEFAULT_PREMIUM_PAYOUT_PERCENT)),
    )

    primary_pet = None
    secondary_pet = None
    client_user_id: Optional[str] = user.get("id") if not _is_admin_user(user) else None
    pet_ids: List[str] = []
    shared_pet_names: List[str] = []
    shared_client_names: List[str] = []
    shared_owner_keys: List[str] = []
    participant_user_ids: List[str] = []

    if payload.pet_id:
        primary_pet = await _get_pet_or_404(payload.pet_id)
        if not _pet_belongs_to_user(primary_pet, user):
            raise HTTPException(status_code=403, detail="Sem permissão para usar este pet")

        pet_ids.append(primary_pet["id"])
        shared_pet_names.append(primary_pet["pet_name"])
        shared_client_names.append(primary_pet.get("owner_name") or payload.client_name.strip())
        primary_owner_user_id = primary_pet.get("owner_user_id")
        if primary_owner_user_id:
            participant_user_ids.append(primary_owner_user_id)
            client_user_id = primary_owner_user_id
        primary_owner_key = _owner_key_from_pet(primary_pet)
        if primary_owner_key:
            shared_owner_keys.append(primary_owner_key)

    derived_shared_context: Optional[str] = None
    if payload.walk_type == WALK_TYPE_INDIVIDUAL and payload.second_pet_id:
        raise HTTPException(status_code=400, detail="Passeio individual permite apenas 1 pet")

    if payload.walk_type == WALK_TYPE_SHARED:
        if payload.duration_minutes != 45:
            raise HTTPException(status_code=400, detail="Passeio compartilhado no MVP deve ter 45 minutos")

        if not primary_pet:
            raise HTTPException(status_code=400, detail="Selecione o pet principal para passeio compartilhado")

        await _validate_pet_for_shared_rules(primary_pet, require_admin_flags=False)

        if payload.second_pet_id:
            secondary_pet = await _get_pet_or_404(payload.second_pet_id)
            if not _pet_belongs_to_user(secondary_pet, user):
                raise HTTPException(status_code=403, detail="Sem permissão para usar este pet")
            if secondary_pet["id"] == primary_pet["id"]:
                raise HTTPException(status_code=400, detail="Escolha dois pets diferentes")
            await _validate_pet_for_shared_rules(secondary_pet, require_admin_flags=False)

            pet_ids.append(secondary_pet["id"])
            shared_pet_names.append(secondary_pet["pet_name"])
            shared_client_names.append(secondary_pet.get("owner_name") or payload.client_name.strip())
            secondary_owner_user_id = secondary_pet.get("owner_user_id")
            if secondary_owner_user_id:
                participant_user_ids.append(secondary_owner_user_id)
            secondary_owner_key = _owner_key_from_pet(secondary_pet)
            if secondary_owner_key:
                shared_owner_keys.append(secondary_owner_key)

            primary_key = _owner_key_from_pet(primary_pet)
            if primary_key and secondary_owner_key and primary_key == secondary_owner_key:
                derived_shared_context = SHARED_CONTEXT_SAME_HOUSEHOLD
            else:
                derived_shared_context = SHARED_CONTEXT_OTHER_CLIENT
        else:
            derived_shared_context = SHARED_CONTEXT_OTHER_CLIENT

    if len(pet_ids) > 2:
        raise HTTPException(status_code=400, detail="No MVP o compartilhado permite no máximo 2 pets")

    walker_quality_user = None
    candidate_walker_id = str(selected_walker.get("id") or "")
    if candidate_walker_id.startswith("partner-"):
        maybe_user_id = candidate_walker_id.replace("partner-", "", 1)
        walker_quality_user = await db.users.find_one({"id": maybe_user_id, "role": "passeador"}, {"_id": 0})
    if not walker_quality_user:
        walker_quality_user = await db.users.find_one({"full_name": selected_walker.get("name", ""), "role": "passeador"}, {"_id": 0})

    if walker_quality_user:
        quality_status = walker_quality_user.get("quality_status", QUALITY_STATUS_ACTIVE)
        quality_metrics = walker_quality_user.get("quality_metrics", {}) if isinstance(walker_quality_user.get("quality_metrics"), dict) else {}

        selected_walker["rating_avg"] = _coerce_float(quality_metrics.get("rating_avg"), _coerce_float(selected_walker.get("rating_avg"), 0.0))
        selected_walker["rating_count"] = int(quality_metrics.get("rating_count", selected_walker.get("rating_count", 0)) or 0)
        selected_walker["highlight_label"] = str(
            quality_metrics.get("public_badge")
            or quality_metrics.get("score_badge")
            or selected_walker.get("highlight_label", "")
        )

        if quality_status == QUALITY_STATUS_SUSPENDED:
            raise HTTPException(status_code=400, detail="Este passeador está temporariamente indisponível para novos passeios")
        if quality_status == QUALITY_STATUS_RESTRICTED:
            existing_today = await db.walks.count_documents(
                {
                    "walker_id": selected_walker["id"],
                    "walk_date": payload.walk_date,
                    "status": {"$in": list(BLOCKING_WALK_STATUSES)},
                }
            )
            if existing_today >= RESTRICTED_DAILY_LIMIT:
                raise HTTPException(
                    status_code=400,
                    detail="Passeador em modo restrito já atingiu o limite diário de novos passeios",
                )

    requested_duration = 45 if payload.walk_type == WALK_TYPE_SHARED else payload.duration_minutes
    available_slots = await _get_available_slots_for_walker(
        selected_walker,
        selected_walker["id"],
        payload.walk_date,
        requested_duration,
    )
    if not available_slots:
        raise HTTPException(status_code=400, detail="O passeador selecionado não possui horários disponíveis nesta data")
    if payload.walk_time not in available_slots:
        options = ", ".join(available_slots[:6])
        raise HTTPException(
            status_code=400,
            detail=f"Horário indisponível para este passeador. Escolha um horário disponível: {options}",
        )

    dynamic_pricing_settings = await _load_dynamic_pricing_settings()
    dynamic_mode = str(dynamic_pricing_settings.get("dynamicPricingMode") or DYNAMIC_PRICING_MODE_OFF)
    if not bool(dynamic_pricing_settings.get("dynamicPricingEnabled", False)):
        dynamic_mode = DYNAMIC_PRICING_MODE_OFF

    selected_pets_count = max(1, len(pet_ids))
    normalized_neighborhood = payload.pickup_neighborhood.strip().lower()
    runtime_context = await _marketplace_runtime_context("São Paulo", normalized_neighborhood)
    market_context = runtime_context.get("metrics") if isinstance(runtime_context, dict) else {}
    market_metrics = market_context if isinstance(market_context, dict) else {}
    previous_multiplier = await _recent_hour_multiplier(payload.walk_date, payload.walk_time)
    dynamic_calc = calculateDynamicPrice(
        {
            "basePrice": _base_walk_price(
                {
                    "duration_minutes": requested_duration,
                    "walk_type": payload.walk_type,
                    "selected_pets_count": selected_pets_count,
                }
            ),
            "demandLevel": int(max(_coerce_float(market_metrics.get("hour_demand"), 0.0), _coerce_float(market_metrics.get("demand_active"), 0.0))),
            "supplyLevel": int(max(0, _coerce_float(market_metrics.get("active_walkers"), 0.0))),
            "timeSlot": payload.walk_time,
            "dayOfWeek": _weekday_key_from_date(payload.walk_date),
            "previousMultiplier": previous_multiplier,
        },
        dynamic_pricing_settings,
    )

    if client_user_id and dynamic_mode == DYNAMIC_PRICING_MODE_ACTIVE:
        latest_preview = await _get_latest_dynamic_pricing_preview(
            user_id=str(client_user_id),
            walk_date=payload.walk_date,
            walk_time=payload.walk_time,
        )
        if latest_preview:
            preview_base = round(max(0.0, _coerce_float(latest_preview.get("base_price"), 0.0)), 2)
            preview_dynamic = round(
                max(preview_base, _coerce_float(latest_preview.get("dynamic_price_calculated"), preview_base)),
                2,
            )
            if preview_base > 0 and abs(preview_base - dynamic_calc["base_price"]) <= 0.05:
                guardrail_cap = round(
                    min(40.0, max(25.0, _coerce_float(dynamic_calc.get("guardrail_price_cap"), 40.0))),
                    2,
                )
                preview_dynamic = round(min(guardrail_cap, preview_dynamic), 2)
                dynamic_calc["dynamic_price"] = preview_dynamic
                dynamic_calc["multiplier"] = round(
                    max(
                        1.0,
                        min(
                            1.0 + DYNAMIC_PRICING_MAX_TOTAL_BOOST,
                            preview_dynamic / max(dynamic_calc["base_price"], 0.01),
                        ),
                    ),
                    4,
                )
                dynamic_calc["difference_percent"] = round(
                    ((dynamic_calc["dynamic_price"] - dynamic_calc["base_price"]) / max(dynamic_calc["base_price"], 0.01)) * 100.0,
                    2,
                )

    dynamic_price_multiplier = 1.0
    dynamic_price_reason = "Preço padrão"
    if dynamic_mode == DYNAMIC_PRICING_MODE_ACTIVE and dynamic_calc["dynamic_price"] > dynamic_calc["base_price"]:
        dynamic_price_multiplier = round(dynamic_calc["dynamic_price"] / max(dynamic_calc["base_price"], 0.01), 4)
        dynamic_price_reason = "Horário de alta demanda"

    if client_user_id:
        participant_user_ids.append(client_user_id)
    participant_user_ids = list(dict.fromkeys(participant_user_ids))

    now_iso = datetime.now(timezone.utc).isoformat()
    walk_datetime_iso = _validate_datetime_iso(payload.walk_date, payload.walk_time)
    location_reference = payload.location_reference.strip() or f"{payload.pickup_street.strip()}, {payload.pickup_number.strip()}"
    if payload.pickup_neighborhood.strip():
        location_reference = payload.location_reference.strip() or payload.pickup_neighborhood.strip()

    tipo_passeio = str(payload.tipo_passeio or "padrao").strip().lower()
    modo_inicio = payload.modo_inicio_passeio
    if tipo_passeio == "transporte" and modo_inicio != START_MODE_PREMIUM_RELOCATION:
        modo_inicio = START_MODE_PREMIUM_RELOCATION
    is_transport_walk = tipo_passeio == "transporte" or modo_inicio == START_MODE_PREMIUM_RELOCATION
    if is_transport_walk and not await _is_pet_transport_available_for_user(user):
        raise HTTPException(status_code=400, detail="Passeio com transporte está desativado para sua conta")

    transport_settings = await _get_pet_transport_settings_dict()
    endereco_base_tutor = f"{payload.pickup_street.strip()}, {payload.pickup_number.strip()} - {payload.pickup_neighborhood.strip()}"
    geocode_error = "Não conseguimos identificar o local. Tente ser mais específico."
    base_geocode = None

    ponto_retirada_alternativo = None
    ponto_encontro = None
    local_destino_passeio = None
    distancia_km = 0.0
    adicional_deslocamento = 0.0
    tempo_deslocamento_minutos = 0
    tempo_total_minutos = int(payload.duration_minutes)
    precisa_analise_manual = False
    status_analise_deslocamento = PREMIUM_ANALYSIS_NA
    tracking_interval_seconds = 60
    status_transporte = "nao_aplicavel"
    eventos_transporte: List[Dict[str, Any]] = []

    if modo_inicio == START_MODE_TUTOR_ADDRESS:
        if payload.usar_ponto_retirada_alternativo:
            if not payload.ponto_retirada_alternativo_nome.strip():
                raise HTTPException(status_code=400, detail="Informe o nome do ponto alternativo")

            base_geocode = _geocode_location(f"{endereco_base_tutor}, Salvador, BA, Brasil")
            if not base_geocode:
                raise HTTPException(status_code=400, detail=geocode_error)

            alt_query = f"{payload.ponto_retirada_alternativo_nome.strip()}, {payload.ponto_retirada_alternativo_referencia.strip()}, Salvador, BA, Brasil"
            alt_geocode = _geocode_location(alt_query)
            if not alt_geocode:
                raise HTTPException(status_code=400, detail=geocode_error)

            alt_lat = _coerce_float(alt_geocode.get("latitude"), 0.0)
            alt_lng = _coerce_float(alt_geocode.get("longitude"), 0.0)
            base_latitude = _coerce_float(base_geocode.get("latitude"), -12.9714)
            base_longitude = _coerce_float(base_geocode.get("longitude"), -38.5014)
            alt_distance = _haversine_km(base_latitude, base_longitude, alt_lat, alt_lng)
            if alt_distance > 1.0:
                raise HTTPException(status_code=400, detail="Ponto alternativo deve estar em raio máximo de 1 km")

            ponto_retirada_alternativo = {
                "nome": payload.ponto_retirada_alternativo_nome.strip(),
                "referencia": payload.ponto_retirada_alternativo_referencia.strip(),
                "latitude": alt_lat,
                "longitude": alt_lng,
            }

    elif modo_inicio == START_MODE_MEETING_POINT:
        if not payload.ponto_encontro_nome.strip():
            raise HTTPException(status_code=400, detail="Informe o ponto de encontro")

        meeting_query = f"{payload.ponto_encontro_nome.strip()}, {payload.ponto_encontro_referencia.strip()}, Salvador, BA, Brasil"
        meeting_geocode = _geocode_location(meeting_query)
        if not meeting_geocode:
            raise HTTPException(status_code=400, detail=geocode_error)

        ponto_encontro = {
            "nome": payload.ponto_encontro_nome.strip(),
            "referencia": payload.ponto_encontro_referencia.strip(),
            "latitude": _coerce_float(meeting_geocode.get("latitude"), 0.0),
            "longitude": _coerce_float(meeting_geocode.get("longitude"), 0.0),
        }

    elif modo_inicio == START_MODE_PREMIUM_RELOCATION:
        possui_veiculo = bool((walker_user or {}).get("possuiVeiculo", selected_walker.get("possuiVeiculo", False)))
        aceita_premium = bool((walker_user or {}).get("aceitaDeslocamentoPremium", selected_walker.get("aceitaDeslocamentoPremium", False)))
        ativo_premium = bool((walker_user or {}).get("ativoParaTransportePremium", selected_walker.get("ativoParaTransportePremium", False)))
        if not (possui_veiculo and aceita_premium and ativo_premium):
            raise HTTPException(status_code=400, detail="Passeador selecionado não está apto para deslocamento premium")

        if not payload.local_destino_nome.strip():
            raise HTTPException(status_code=400, detail="Informe o destino do passeio com transporte")

        base_geocode = _geocode_location(f"{endereco_base_tutor}, Salvador, BA, Brasil")
        if not base_geocode:
            raise HTTPException(status_code=400, detail=geocode_error)

        destination_query = f"{payload.local_destino_nome.strip()}, {payload.local_destino_referencia.strip()}, Salvador, BA, Brasil"
        destination_geocode = _geocode_location(destination_query)
        if not destination_geocode:
            raise HTTPException(status_code=400, detail=geocode_error)

        base_latitude = _coerce_float(base_geocode.get("latitude"), -12.9714)
        base_longitude = _coerce_float(base_geocode.get("longitude"), -38.5014)
        destino_lat = _coerce_float(destination_geocode.get("latitude"), 0.0)
        destino_lng = _coerce_float(destination_geocode.get("longitude"), 0.0)
        distancia_km, tempo_rota_minutos = await _estimate_transport_route(
            origin_lat=base_latitude,
            origin_lng=base_longitude,
            destination_lat=destino_lat,
            destination_lng=destino_lng,
            fallback_minutes_per_km=_coerce_float(transport_settings.get("estimated_minutes_per_km"), 3.0),
        )
        adicional_deslocamento, precisa_analise_manual, status_analise_deslocamento, tempo_estimado_por_km = _calculate_premium_transport(
            distancia_km,
            transport_settings,
        )
        tempo_deslocamento_minutos = max(tempo_rota_minutos, tempo_estimado_por_km)
        tempo_total_minutos = int(payload.duration_minutes) + tempo_deslocamento_minutos
        tracking_interval_seconds = int(transport_settings.get("tracking_interval_seconds") or 15)
        status_transporte = "A caminho do local"
        eventos_transporte.append(
            {
                "event": "inicio_deslocamento",
                "timestamp": now_iso,
                "status_label": status_transporte,
            }
        )

        local_destino_passeio = {
            "nome": payload.local_destino_nome.strip(),
            "referencia": payload.local_destino_referencia.strip(),
            "latitude": destino_lat,
            "longitude": destino_lng,
        }

    else:
        raise HTTPException(status_code=400, detail="Modo de início do passeio inválido")

    if ponto_encontro:
        location_reference = ponto_encontro.get("nome") or location_reference
    if local_destino_passeio:
        location_reference = local_destino_passeio.get("nome") or location_reference

    walk_type = payload.walk_type
    pet_name = payload.pet_name.strip()
    client_name = user.get("full_name", "") if not _is_admin_user(user) else payload.client_name.strip()
    pet_behavior_notes = payload.pet_behavior_notes.strip()

    if primary_pet:
        pet_name = primary_pet["pet_name"]
        client_name = primary_pet.get("owner_name") or client_name
        pet_behavior_notes = primary_pet.get("behavioral_notes", "")

    if walk_type == WALK_TYPE_SHARED and shared_pet_names:
        pet_name = " + ".join(shared_pet_names)
        if derived_shared_context == SHARED_CONTEXT_OTHER_CLIENT and len(shared_client_names) >= 2:
            client_name = "Compartilhado (2 tutores)"

    walk_payload = {
        "id": str(uuid.uuid4()),
        "pet_name": pet_name,
        "pet_ids": pet_ids,
        "shared_pet_names": shared_pet_names,
        "shared_client_names": shared_client_names,
        "shared_owner_keys": shared_owner_keys,
        "participant_user_ids": participant_user_ids,
        "client_user_id": client_user_id,
        "client_name": client_name,
        "walk_type": walk_type,
        "tipoPasseio": "transporte" if is_transport_walk else "padrao",
        "modoInicioPasseio": modo_inicio,
        "enderecoBaseTutor": endereco_base_tutor,
        "pontoRetiradaAlternativo": ponto_retirada_alternativo,
        "pontoEncontro": ponto_encontro,
        "localDestinoPasseio": local_destino_passeio,
        "distanciaKm": distancia_km,
        "adicionalDeslocamento": round(adicional_deslocamento, 2),
        "tempoDeslocamentoMinutos": int(tempo_deslocamento_minutos),
        "tempoPasseioMinutos": int(payload.duration_minutes),
        "tempoTotalMinutos": int(tempo_total_minutos),
        "rastreamentoReforcado": bool(is_transport_walk),
        "trackingIntervalSegundos": int(tracking_interval_seconds),
        "statusTransporte": status_transporte,
        "eventosTransporte": eventos_transporte,
        "precisaAnaliseManualDeslocamento": precisa_analise_manual,
        "statusAnaliseDeslocamento": status_analise_deslocamento,
        "premiumRepassePercentual": premium_percent,
        "coupon_id": None,
        "coupon_code": "",
        "discount_percent_applied": 0.0,
        "discount_fixed_applied": 0.0,
        "discount_amount": 0.0,
        "price_before_discount": 0.0,
        "tip_id": None,
        "tip_amount": 0.0,
        "tip_status": "none",
        "tip_paid_at": None,
        "tip_deadline_at": None,
        "financial_status": "pendente",
        "payment_released_at": None,
        "payment_paid_at": None,
        "payment_method": "",
        "payment_transaction_id": "",
        "payment_failure_reason": "",
        "payment_block_reason": "",
        "suspected_disintermediation": False,
        "shared_context": derived_shared_context if walk_type == WALK_TYPE_SHARED else None,
        "shared_approved": False if walk_type == WALK_TYPE_SHARED else True,
        "shared_group": None,
        "walk_date": payload.walk_date,
        "walk_time": payload.walk_time,
        "duration_minutes": payload.duration_minutes,
        "walker_id": selected_walker["id"],
        "walker_user_id": walker_user_id,
        "walker_name": selected_walker["name"],
        "walker_photo_url": selected_walker["photo_url"],
        "walker_rating_avg": _coerce_float(selected_walker.get("rating_avg"), 0.0),
        "walker_rating_count": int(selected_walker.get("rating_count", 0) or 0),
        "walker_highlight_label": str(selected_walker.get("highlight_label", "")),
        "pickup_street": payload.pickup_street.strip(),
        "pickup_number": payload.pickup_number.strip(),
        "pickup_neighborhood": payload.pickup_neighborhood.strip(),
        "pickup_complement": payload.pickup_complement.strip(),
        "location_reference": location_reference,
        "security_code": _generate_security_code(),
        "did_pee": False,
        "did_poop": False,
        "rating": None,
        "rating_comment": "",
        "summary_text": "",
        "pet_behavior_notes": pet_behavior_notes,
        "notes": payload.notes.strip(),
        "status": STATUS_PENDING_REVIEW if precisa_analise_manual else STATUS_SCHEDULED,
        "scheduled_start_at": walk_datetime_iso,
        "walker_check_in_at": None,
        "client_confirmed_at": None,
        "tolerance_expires_at": (datetime.fromisoformat(walk_datetime_iso) + timedelta(minutes=TOLERANCE_MINUTES)).isoformat(),
        "tolerance_minutes": TOLERANCE_MINUTES,
        "attendance_message": "Há tolerância de até 10 minutos para início do passeio",
        "decision_resolved_at": None,
        "decision_source": "",
        "walker_penalty_registered": False,
        "occurrence_status": OCC_UNRESOLVED,
        "occurrence_resolved": False,
        "internal_note": "",
        "occurrence_logs": [],
        "dynamic_pricing_mode": dynamic_mode,
        "dynamic_price_multiplier": dynamic_price_multiplier,
        "dynamic_price_reason": dynamic_price_reason,
        "dynamic_price_calculated": dynamic_calc["dynamic_price"],
        "dynamic_price_difference_percent": dynamic_calc["difference_percent"],
        "photo_url": None,
        "walk_datetime_iso": walk_datetime_iso,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    walk_payload["price_before_discount"] = _walk_subtotal_before_discount(walk_payload)

    coupon_evaluation = None
    normalized_coupon_code = _normalize_coupon_code(payload.coupon_code)
    if normalized_coupon_code:
        coupon_owner_user_id = client_user_id or user.get("id")
        coupon_owner_user = await db.users.find_one({"id": coupon_owner_user_id}, {"_id": 0}) if coupon_owner_user_id else user
        coupon_subtotal = _walk_subtotal_before_discount(walk_payload)
        coupon_evaluation = await _evaluate_coupon_for_user(
            code=normalized_coupon_code,
            user=coupon_owner_user or user,
            request=request,
            walk_type=walk_type,
            subtotal=coupon_subtotal,
        )
        selected_coupon: CouponResponse = coupon_evaluation["coupon"]
        walk_payload["coupon_id"] = selected_coupon.id
        walk_payload["coupon_code"] = selected_coupon.code
        walk_payload["discount_percent_applied"] = selected_coupon.discount_percent
        walk_payload["discount_fixed_applied"] = selected_coupon.discount_fixed
        walk_payload["discount_amount"] = coupon_evaluation["discount_amount"]
        walk_payload["price_before_discount"] = coupon_evaluation["subtotal"]

    walk_payload["valor_base_passeio"] = _base_walk_price(walk_payload)
    base_price, walker_payout = _calculate_walk_pricing(walk_payload)
    walk_payload["base_price"] = base_price
    walk_payload["walker_payout"] = walker_payout
    walk_payload["charged_amount"] = _base_amount_from_walk(walk_payload)
    walk_payload["walker_payout_amount"] = walker_payout
    walk_payload["platform_retained_amount"] = round(walk_payload["charged_amount"] - walker_payout, 2)
    walk_payload["walker_share_percent"] = RUNTIME_WALKER_SHARE_PERCENT
    walk_payload["platform_fee_percent"] = RUNTIME_PLATFORM_SHARE_PERCENT
    walk_payload["client_refund_amount"] = 0.0

    walk = WalkResponse(**walk_payload)
    await db.walks.insert_one(walk.model_dump())
    await _rebuild_payments_for_walk(walk.model_dump())
    if client_user_id:
        await _mark_dynamic_pricing_conversion(
            user_id=client_user_id,
            walk_date=walk.walk_date,
            walk_time=walk.walk_time,
            walk_id=walk.id,
            confirmed_price=_coerce_float(walk.charged_amount, 0.0),
            confirmed_multiplier=_coerce_float(walk.dynamic_price_multiplier, 1.0),
        )

    if coupon_evaluation:
        try:
            await _register_coupon_redemption(
                coupon=coupon_evaluation["coupon"],
                identity=coupon_evaluation.get("identity") or {},
                ip_address=str(coupon_evaluation.get("ip_address") or ""),
                device_id=str(coupon_evaluation.get("device_id") or ""),
                walk_id=walk.id,
                discount_amount=coupon_evaluation["discount_amount"],
            )
        except HTTPException:
            await db.payments.delete_many({"walk_id": walk.id})
            await db.walks.delete_one({"id": walk.id})
            raise

    await _notify_admins(
        title="Novo passeio criado",
        message=f"{walk.pet_name} em {walk.walk_date} às {walk.walk_time}.",
        category="admin_operacional",
    )
    await _notify_admins(
        title="Pendência criada",
        #message=f"Pagamento pendente gerado para o passeio de {walk.pet_name}.",
        category="admin_pendencia",
    )

    client_user = None
    if client_user_id:
        client_user = await db.users.find_one({"id": client_user_id, "role": "cliente"}, {"_id": 0})
    if not client_user:
        client_user = await db.users.find_one({"full_name": walk.client_name, "role": "cliente"}, {"_id": 0})

    if client_user:
        client_message = f"Passeio de {walk.pet_name} agendado para {walk.walk_date} às {walk.walk_time}."
        if walk.modoInicioPasseio == START_MODE_MEETING_POINT:
            client_message = (
                f"Passeio em ponto de encontro confirmado para {walk.walk_date} às {walk.walk_time}. "
                "Você será responsável por levar e buscar o pet no local selecionado."
            )
        elif walk.modoInicioPasseio == START_MODE_PREMIUM_RELOCATION:
            if walk.precisaAnaliseManualDeslocamento:
                client_message = "Solicitação enviada para análise da equipe devido ao deslocamento acima de 5 km."
            else:
                client_message = (
                    f"Serviço premium confirmado com adicional de R$ {walk.adicionalDeslocamento:.2f}."
                )

        await _create_notification(
            user_id=client_user["id"],
            role="cliente",
            title="Agendamento confirmado",
            message=client_message,
            category="agendamento",
        )

    if not walker_user and walker_user_id:
        walker_user = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if walker_user:
        walker_message = f"Você recebeu um passeio de {walk.pet_name} em {walk.walk_date}."
        if walk.modoInicioPasseio == START_MODE_MEETING_POINT:
            walker_message = f"Você recebeu um passeio com ponto de encontro definido pelo tutor ({walk.location_reference})."
        elif walk.modoInicioPasseio == START_MODE_PREMIUM_RELOCATION:
            walker_message = "Você recebeu um passeio premium com deslocamento."

        await _create_notification(
            user_id=walker_user["id"],
            role="passeador",
            title="Novo passeio atribuído",
            message=walker_message,
            category="operacao",
        )

    if walk.walk_type == WALK_TYPE_SHARED and not walk.shared_approved:
        await _notify_admins(
            title="Compartilhado pendente",
            message=f"Novo compartilhado aguardando aprovação: {walk.pet_name}",
            category="admin_pendencia",
        )

    if walk.statusAnaliseDeslocamento == PREMIUM_ANALYSIS_WAITING:
        await _notify_admins(
            title="Análise de deslocamento pendente",
            message=f"Passeio premium {walk.id} requer análise manual de deslocamento.",
            category="admin_pendencia",
        )
    return walk


@api_router.get("/walks", response_model=List[WalkResponse])
async def list_walks(request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin"])

    if _is_admin_user(user):
        query = {}
    elif user.get("role") == "passeador":
        query = {
            "$or": [
                {"walker_user_id": user.get("id")},
                {"walker_name": user.get("full_name")},
            ]
        }
    else:
        query = {
            "$or": [
                {"client_user_id": user.get("id")},
                {"participant_user_ids": user.get("id")},
                {"client_name": user.get("full_name")},
            ]
        }

    walks = await db.walks.find(query, {"_id": 0}).sort("walk_datetime_iso", 1).to_list(500)
    refreshed_walks: List[dict] = []
    for walk in walks:
        refreshed_walks.append(await _apply_attendance_decision_if_needed(walk, trigger="read"))
    return [_to_walk_response(walk) for walk in refreshed_walks]


@api_router.get("/walks/{walk_id}", response_model=WalkResponse)
async def get_walk(walk_id: str, request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)
    walk = await _apply_attendance_decision_if_needed(walk, trigger="read")
    return _to_walk_response(walk)


@api_router.get("/admin/dashboard", response_model=AdminDashboardResponse)
async def get_admin_dashboard(request: Request):
    await _require_role(request, ["admin"])
    await _sync_payments_from_walks()

    walks = await db.walks.find({}, {"_id": 0}).to_list(3000)
    clients = len({walk.get("client_name", "") for walk in walks if walk.get("client_name")})
    active_walkers = await db.partner_applications.count_documents({"status": "Aprovado", "active_as_walker": True})

    payments_paid = await db.payments.find({"payment_status": "Pago"}, {"_id": 0, "value": 1}).to_list(2000)
    revenue = round(sum(float(item.get("value", 0)) for item in payments_paid), 2)

    in_progress = sum(1 for walk in walks if walk.get("status") in {STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW})
    finished = sum(1 for walk in walks if walk.get("status") == STATUS_FINISHED)
    scheduled = sum(1 for walk in walks if walk.get("status") == STATUS_SCHEDULED)

    pending_occurrences = sum(
        1
        for walk in walks
        if _derive_occurrence_status(walk) in {OCC_PENDING_ANALYSIS, OCC_PENDING_ANALYSIS_REOPENED, OCC_UNRESOLVED}
    )
    open_disputes = sum(1 for walk in walks if _derive_occurrence_status(walk) == OCC_DISPUTE_OPEN)
    disintermediation_alerts = sum(1 for walk in walks if bool(walk.get("suspected_disintermediation", False)))

    users = await db.users.find({"role": "passeador"}, {"_id": 0, "walker_operational_status": 1, "quality_metrics": 1}).to_list(1000)
    walkers_at_risk = sum(
        1
        for user in users
        if user.get("walker_operational_status")
        in {WALKER_OP_STATUS_OBSERVATION, WALKER_OP_STATUS_RESTRICTED, WALKER_OP_STATUS_SUSPENDED}
    )
    top_rated_walkers = sum(
        1
        for user in users
        if _coerce_float((user.get("quality_metrics") or {}).get("rating_avg"), 0.0) >= 4.7
        and int((user.get("quality_metrics") or {}).get("rating_count", 0) or 0) >= 10
    )

    tips_paid = await db.tips.find({"status": "paid"}, {"_id": 0, "amount": 1, "paid_at": 1}).to_list(3000)
    week_start = datetime.now(timezone.utc) - timedelta(days=7)
    weekly_tips_amount = round(
        sum(
            _coerce_float(tip.get("amount"), 0.0)
            for tip in tips_paid
            if (_parse_iso_datetime(tip.get("paid_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= week_start
        ),
        2,
    )

    accepted_walks = sum(
        1
        for walk in walks
        if walk.get("status") in {STATUS_FINISHED, STATUS_NO_SHOW_CLIENT, STATUS_NO_SHOW_WALKER, STATUS_CANCELED}
    )
    no_show_total = sum(1 for walk in walks if walk.get("status") in {STATUS_NO_SHOW_CLIENT, STATUS_NO_SHOW_WALKER})
    no_show_rate = _to_percentage(no_show_total, accepted_walks)

    return AdminDashboardResponse(
        total_clients=clients,
        total_active_walkers=active_walkers,
        total_walks_finished=finished,
        total_walks_scheduled=scheduled,
        total_walks_in_progress=in_progress,
        estimated_revenue_paid=revenue,
        pending_occurrences=pending_occurrences,
        open_disputes=open_disputes,
        walkers_at_risk=walkers_at_risk,
        top_rated_walkers=top_rated_walkers,
        disintermediation_alerts=disintermediation_alerts,
        weekly_tips_amount=weekly_tips_amount,
        no_show_rate=no_show_rate,
    )


@api_router.get("/admin/alerts", response_model=List[SystemAlertResponse])
async def list_admin_system_alerts(
    request: Request,
    status: Optional[Literal["pendente", "executado", "ignorado", "revisar_depois"]] = None,
    nivel_gravidade: Optional[int] = Query(default=None, ge=1, le=4),
    tipo_alerta: Optional[str] = None,
    categoria: Optional[Literal["operacional", "financeiro", "comportamental", "sistemico"]] = None,
    limit: int = 120,
):
    await _require_admin_permission(request, "dashboard")
    await _run_system_alert_engine_snapshot()

    capped_limit = max(10, min(500, int(limit or 120)))
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    if nivel_gravidade:
        query["nivel_gravidade"] = int(nivel_gravidade)
    if tipo_alerta:
        query["tipo_alerta"] = str(tipo_alerta).strip().upper()
    if categoria:
        query["categoria"] = categoria

    rows = await db.system_alerts.find(query, {"_id": 0}).sort([("prioridade_score", -1), ("criado_em", -1)]).to_list(capped_limit)
    return [SystemAlertResponse(**row) for row in rows]


@api_router.get("/admin/alerts/priority-settings", response_model=SystemAlertPrioritySettingsResponse)
async def get_admin_system_alert_priority_settings(request: Request):
    await _require_admin_permission(request, "dashboard")
    settings = await _get_system_alert_priority_settings()
    return SystemAlertPrioritySettingsResponse(**settings)


@api_router.patch("/admin/alerts/priority-settings", response_model=SystemAlertPrioritySettingsResponse)
async def update_admin_system_alert_priority_settings(payload: SystemAlertPrioritySettingsUpdatePayload, request: Request):
    admin_user = await _require_admin_permission(request, "dashboard")
    current = await _get_system_alert_priority_settings()

    merged = {
        **current,
        "weights": _normalize_alert_weights(payload.weights if payload.weights is not None else current.get("weights")),
        "grouping_windows_hours": _normalize_grouping_windows(
            payload.grouping_windows_hours if payload.grouping_windows_hours is not None else current.get("grouping_windows_hours")
        ),
        "systemic_region_failure_threshold": max(
            2,
            min(
                20,
                int(
                    _coerce_float(
                        payload.systemic_region_failure_threshold,
                        current.get("systemic_region_failure_threshold", SYSTEM_ALERT_SYSTEMIC_REGION_FAILURE_THRESHOLD),
                    )
                ),
            ),
        ),
        "systemic_overload_threshold": max(
            3,
            min(
                50,
                int(
                    _coerce_float(
                        payload.systemic_overload_threshold,
                        current.get("systemic_overload_threshold", SYSTEM_ALERT_SYSTEMIC_OVERLOAD_THRESHOLD),
                    )
                ),
            ),
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    await db.system_alert_priority_settings.update_one({"id": "default"}, {"$set": merged}, upsert=True)
    await db.system_alert_priority_settings_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "actor_admin_id": str(admin_user.get("id") or ""),
            "changes": merged,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return SystemAlertPrioritySettingsResponse(**merged)


@api_router.get("/admin/marketplace-intelligence/settings", response_model=MarketplaceIntelligenceSettingsResponse)
async def get_admin_marketplace_intelligence_settings(request: Request):
    await _require_admin_permission(request, "configuracoes")
    settings = await _get_marketplace_intelligence_settings_dict()
    dynamic_settings = await _load_dynamic_pricing_settings()
    merged = {
        **settings,
        "dynamicPricingEnabled": bool(dynamic_settings.get("dynamicPricingEnabled", False)),
        "dynamicPricingMode": str(dynamic_settings.get("dynamicPricingMode") or DYNAMIC_PRICING_MODE_OFF),
    }
    return MarketplaceIntelligenceSettingsResponse(**merged)


@api_router.patch("/admin/marketplace-intelligence/settings", response_model=MarketplaceIntelligenceSettingsResponse)
async def update_admin_marketplace_intelligence_settings(
    payload: MarketplaceIntelligenceSettingsUpdatePayload,
    request: Request,
):
    admin_user = await _require_admin_permission(request, "configuracoes")
    current = await _get_marketplace_intelligence_settings_dict()
    updates = payload.model_dump(exclude_unset=True)
    dynamic_updates = {
        key: updates.pop(key)
        for key in ["dynamicPricingEnabled", "dynamicPricingMode"]
        if key in updates
    }

    merged = _normalize_marketplace_intelligence_settings({
        **current,
        **updates,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": str(admin_user.get("id") or "admin"),
    })

    await db.marketplace_intelligence_settings.update_one({"id": "default"}, {"$set": merged}, upsert=True)
    await db.marketplace_intelligence_settings_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "actor_admin_id": str(admin_user.get("id") or ""),
            "changes": updates,
            "result": merged,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    if dynamic_updates:
        current_dynamic = await _load_dynamic_pricing_settings()
        dynamic_merged = _normalize_dynamic_pricing_settings(
            {
                **current_dynamic,
                **dynamic_updates,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "updated_by": str(admin_user.get("id") or "admin"),
            }
        )
        await db.dynamic_pricing_settings.update_one({"id": "default"}, {"$set": dynamic_merged}, upsert=True)

    dynamic_settings = await _load_dynamic_pricing_settings()
    return MarketplaceIntelligenceSettingsResponse(
        **{
            **merged,
            "dynamicPricingEnabled": bool(dynamic_settings.get("dynamicPricingEnabled", False)),
            "dynamicPricingMode": str(dynamic_settings.get("dynamicPricingMode") or DYNAMIC_PRICING_MODE_OFF),
        }
    )


@api_router.get("/admin/marketplace-intelligence/metrics", response_model=MarketplaceContextMetricsResponse)
async def get_admin_marketplace_intelligence_metrics(
    request: Request,
    city: str = "",
    neighborhood: str = "",
):
    await _require_admin_permission(request, "dashboard")
    runtime = await _marketplace_runtime_context(city, neighborhood)
    settings = dict(runtime.get("settings") or {})
    metrics = dict(runtime.get("metrics") or {})
    return MarketplaceContextMetricsResponse(
        city=str(metrics.get("city") or ""),
        neighborhood=str(metrics.get("neighborhood") or ""),
        mode=str(settings.get("mode") or MARKETPLACE_MODE_AUTOMATIC),
        context_state=str(runtime.get("context_state") or MARKETPLACE_CONTEXT_BALANCED),
        demand_active=int(metrics.get("demand_active", 0) or 0),
        supply_active=int(metrics.get("supply_active", 0) or 0),
        demand_supply_ratio=_coerce_float(metrics.get("demand_supply_ratio"), 0.0),
        match_rate=_coerce_float(metrics.get("match_rate"), 0.0),
        average_acceptance_seconds=_coerce_float(metrics.get("average_acceptance_seconds"), 0.0),
        cancel_rate=_coerce_float(metrics.get("cancel_rate"), 0.0),
        cr_usage_24h=int(metrics.get("cr_usage_24h", 0) or 0),
        updated_at=str(metrics.get("updated_at") or datetime.now(timezone.utc).isoformat()),
    )


@api_router.get("/admin/marketplace-intelligence/audit", response_model=List[MarketplaceDecisionAuditResponse])
async def get_admin_marketplace_intelligence_audit(request: Request, limit: int = 50):
    await _require_admin_permission(request, "dashboard")
    capped_limit = max(10, min(300, int(limit or 50)))
    rows = await db.marketplace_decision_audit.find({}, {"_id": 0}).sort("created_at", -1).to_list(capped_limit)
    return [MarketplaceDecisionAuditResponse(**row) for row in rows]


@api_router.get("/admin/dynamic-pricing/settings", response_model=DynamicPricingSettingsResponse)
async def get_admin_dynamic_pricing_settings(request: Request):
    await _require_admin_permission(request, "configuracoes")
    settings = await _load_dynamic_pricing_settings()
    return DynamicPricingSettingsResponse(**settings)


@api_router.patch("/admin/dynamic-pricing/settings", response_model=DynamicPricingSettingsResponse)
async def update_admin_dynamic_pricing_settings(payload: DynamicPricingSettingsUpdatePayload, request: Request):
    admin_user = await _require_admin_permission(request, "configuracoes")
    current = await _load_dynamic_pricing_settings()
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return DynamicPricingSettingsResponse(**current)
    merged = _normalize_dynamic_pricing_settings(
        {
            **current,
            **updates,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": str(admin_user.get("id") or "admin"),
        }
    )
    if merged != current:
        await db.dynamic_pricing_snapshots.insert_one(
            {
                "id": str(uuid.uuid4()),
                "created_at": merged["updated_at"],
                "reason": "Ajuste manual no cockpit",
                "previous_settings": current,
                "new_settings": merged,
                "conversion_rate": _coerce_float(current.get("last_conversion_rate"), 0.0) * 100.0,
                "avg_revenue": _coerce_float(current.get("last_avg_revenue"), 0.0),
                "impact_note": "Alteração manual aplicada",
                "is_auto": False,
            }
        )
    await db.dynamic_pricing_settings.update_one({"id": "default"}, {"$set": merged}, upsert=True)
    return DynamicPricingSettingsResponse(**merged)


@api_router.get("/admin/dynamic-pricing/metrics", response_model=DynamicPricingMetricsResponse)
async def get_admin_dynamic_pricing_metrics(request: Request):
    await _require_admin_permission(request, "dashboard")
    return await _compute_dynamic_pricing_metrics()


@api_router.get("/admin/dynamic-pricing/snapshots", response_model=List[DynamicPricingCalibrationSnapshotResponse])
async def get_admin_dynamic_pricing_snapshots(request: Request, limit: int = 30):
    await _require_admin_permission(request, "configuracoes")
    capped = max(1, min(limit, 100))
    rows = await db.dynamic_pricing_snapshots.find({}, {"_id": 0}).sort("created_at", -1).to_list(capped)
    return [DynamicPricingCalibrationSnapshotResponse(**row) for row in rows]


@api_router.post("/admin/dynamic-pricing/rollback", response_model=DynamicPricingSettingsResponse)
async def rollback_admin_dynamic_pricing_settings(payload: DynamicPricingRollbackPayload, request: Request):
    admin_user = await _require_admin_permission(request, "configuracoes")
    snapshot = await db.dynamic_pricing_snapshots.find_one({"id": payload.snapshot_id}, {"_id": 0})
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot não encontrado")

    previous_settings = _normalize_dynamic_pricing_settings(snapshot.get("previous_settings") or {})
    previous_settings["updated_at"] = datetime.now(timezone.utc).isoformat()
    previous_settings["updated_by"] = str(admin_user.get("id") or "admin")
    await db.dynamic_pricing_settings.update_one({"id": "default"}, {"$set": previous_settings}, upsert=True)

    await db.dynamic_pricing_snapshots.insert_one(
        {
            "id": str(uuid.uuid4()),
            "created_at": previous_settings["updated_at"],
            "reason": f"Rollback manual do snapshot {payload.snapshot_id}",
            "previous_settings": snapshot.get("new_settings") or {},
            "new_settings": previous_settings,
            "conversion_rate": _coerce_float(snapshot.get("conversion_rate"), 0.0),
            "avg_revenue": _coerce_float(snapshot.get("avg_revenue"), 0.0),
            "impact_note": "Rollback manual aplicado",
            "is_auto": False,
        }
    )
    return DynamicPricingSettingsResponse(**previous_settings)


@api_router.get("/admin/walker-level/settings", response_model=WalkerLevelSystemSettingsResponse)
async def get_admin_walker_level_settings(request: Request):
    await _require_admin_permission(request, "configuracoes")
    settings = await _get_walker_level_settings_dict()
    return WalkerLevelSystemSettingsResponse(**settings)


@api_router.patch("/admin/walker-level/settings", response_model=WalkerLevelSystemSettingsResponse)
async def update_admin_walker_level_settings(payload: WalkerLevelSystemSettingsUpdatePayload, request: Request):
    admin_user = await _require_admin_permission(request, "configuracoes")
    current = await _get_walker_level_settings_dict()
    updates = payload.model_dump(exclude_unset=True)
    normalized = _normalize_walker_level_settings({**current, **updates})

    document = {
        "id": "default",
        **normalized,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": str(admin_user.get("id") or ""),
    }
    await db.walker_level_settings.update_one({"id": "default"}, {"$set": document}, upsert=True)

    WALKER_LEVEL_SETTINGS_CACHE.clear()
    WALKER_LEVEL_SETTINGS_CACHE.update(normalized)

    await db.walker_level_settings_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "admin_user_id": str(admin_user.get("id") or ""),
            "changes": updates,
            "result": document,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return WalkerLevelSystemSettingsResponse(**document)


@api_router.get("/admin/feature-flags", response_model=List[FeatureFlagResponse])
async def list_admin_feature_flags(request: Request):
    await _require_admin_permission(request, "configuracoes")
    await _ensure_feature_flags_seeded()
    rows = await db.feature_flags.find({}, {"_id": 0}).to_list(300)
    normalized_rows: List[FeatureFlagResponse] = []
    for row in rows:
        feature_name = str(row.get("feature_name") or "").strip()
        normalized = {
            **_feature_flag_default_doc(feature_name),
            **row,
            "feature_name": feature_name,
        }
        normalized_rows.append(FeatureFlagResponse(**normalized))

    normalized_rows.sort(key=lambda item: (item.group, item.title))
    return normalized_rows


@api_router.patch("/admin/feature-flags/{feature_name}", response_model=FeatureFlagResponse)
async def update_admin_feature_flag(feature_name: str, payload: FeatureFlagUpdatePayload, request: Request):
    admin_user = await _require_admin_permission(request, "configuracoes")
    await _ensure_feature_flags_seeded()

    feature_key = str(feature_name or "").strip()
    if feature_key not in FEATURE_FLAGS_CATALOG:
        raise HTTPException(status_code=404, detail="Funcionalidade não mapeada")

    current = await db.feature_flags.find_one({"feature_name": feature_key}, {"_id": 0})
    base_row = {**_feature_flag_default_doc(feature_key), **(current or {})}

    is_active = bool(base_row.get("is_active", False) if payload.is_active is None else payload.is_active)
    is_visible = bool(base_row.get("is_visible", False) if payload.is_visible is None else payload.is_visible)

    if not is_active and is_visible:
        raise HTTPException(status_code=400, detail="Estado inconsistente: funcionalidade inativa não pode permanecer visível")

    updated_at = datetime.now(timezone.utc).isoformat()
    updated_by = str(admin_user.get("id") or "")
    merged = {
        "feature_name": feature_key,
        "title": str(FEATURE_FLAGS_CATALOG[feature_key].get("title") or feature_key),
        "group": str(FEATURE_FLAGS_CATALOG[feature_key].get("group") or FEATURE_FLAG_GROUP_CLIENT_ENGAGEMENT),
        "is_active": is_active,
        "is_visible": is_visible,
        "updated_at": updated_at,
        "updated_by": updated_by,
    }

    await db.feature_flags.update_one({"feature_name": feature_key}, {"$set": merged}, upsert=True)
    await db.feature_flag_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "feature_name": feature_key,
            "is_active": is_active,
            "is_visible": is_visible,
            "updated_at": updated_at,
            "updated_by": updated_by,
        }
    )

    rows = await db.feature_flags.find({}, {"_id": 0}).to_list(300)
    _refresh_runtime_feature_flags(rows)
    return FeatureFlagResponse(**merged)


@api_router.get("/feature-flags/visibility", response_model=FeatureFlagsVisibilityResponse)
async def get_feature_flags_visibility(request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin", "super_admin"])
    await _ensure_feature_flags_seeded()
    flags = {feature_name: bool(_get_runtime_feature_flag(feature_name).get("is_visible", False)) for feature_name in FEATURE_FLAGS_CATALOG.keys()}
    if "pet_transport" in flags:
        flags["pet_transport"] = bool(flags["pet_transport"]) and await _is_pet_transport_available_for_user(user)
    return FeatureFlagsVisibilityResponse(flags=flags)


@api_router.get("/referrals/my-program", response_model=ReferralDashboardResponse)
async def get_my_referral_dashboard(request: Request):
    user = await _require_role(request, ["cliente", "passeador"])
    role = str(user.get("role") or "cliente")
    settings = await _get_referral_program_settings_dict()
    role_enabled = _is_referral_program_enabled_for_role(settings, role)
    program_enabled = bool(settings.get("program_enabled", False))
    app_visible = bool(settings.get("app_visible", False))

    referral_type = "cliente_para_cliente" if role == "cliente" else "passeador_para_passeador"
    referral_code_row = await db.referral_codes.find_one(
        {
            "owner_user_id": str(user.get("id") or ""),
            "referral_type": referral_type,
        },
        {"_id": 0},
    )

    referrals = await db.referrals.find(
        {
            "referrer_user_id": str(user.get("id") or ""),
            "referral_type": referral_type,
        },
        {"_id": 0},
    ).sort("created_at", -1).to_list(120)

    stats = {
        "total": len(referrals),
        "criada": sum(1 for row in referrals if row.get("status") == REFERRAL_STATUS_CREATED),
        "pendente_ativacao": sum(1 for row in referrals if row.get("status") == REFERRAL_STATUS_PENDING),
        "em_progresso": sum(1 for row in referrals if row.get("status") == REFERRAL_STATUS_IN_PROGRESS),
        "elegivel_recompensa": sum(1 for row in referrals if row.get("status") == REFERRAL_STATUS_ELIGIBLE),
        "recompensa_liberada": sum(1 for row in referrals if row.get("status") == REFERRAL_STATUS_REWARDED),
        "fraude": sum(1 for row in referrals if row.get("status") == REFERRAL_STATUS_FRAUD),
    }

    invite_link: Optional[str] = None
    referral_code = str((referral_code_row or {}).get("code") or "").strip() or None
    if referral_code:
        invite_link = f"petpasso://invite?code={referral_code}"

    return ReferralDashboardResponse(
        program_enabled=program_enabled,
        app_visible=app_visible,
        role_enabled=role_enabled,
        role=cast(Literal["cliente", "passeador"], role if role in {"cliente", "passeador"} else "cliente"),
        referral_code=referral_code,
        referral_type=cast(Optional[ReferralType], referral_type),
        invite_link=invite_link,
        stats=stats,
        referrals=[_to_referral_record_response(row) for row in referrals],
    )


@api_router.post("/referrals/my-code/generate", response_model=ReferralDashboardResponse)
async def generate_my_referral_code(request: Request):
    user = await _require_role(request, ["cliente", "passeador"])
    role = str(user.get("role") or "cliente")
    settings = await _get_referral_program_settings_dict()

    if not _is_referral_program_enabled_for_role(settings, role):
        raise HTTPException(status_code=403, detail="Programa de indicação desativado para seu perfil")
    if not bool(settings.get("app_visible", False)):
        raise HTTPException(status_code=403, detail="Programa de indicação oculto no app")

    await _get_or_create_referral_code_for_user(user, settings)
    return await get_my_referral_dashboard(request)


@api_router.post("/referrals/apply", response_model=ReferralRecordResponse, status_code=201)
async def apply_referral_code(payload: ReferralApplyPayload, request: Request):
    user = await _require_role(request, ["cliente", "passeador"])
    role = str(user.get("role") or "cliente")
    settings = await _get_referral_program_settings_dict()

    if not _is_referral_program_enabled_for_role(settings, role):
        raise HTTPException(status_code=403, detail="Programa de indicação desativado para seu perfil")

    referral_type = "cliente_para_cliente" if role == "cliente" else "passeador_para_passeador"
    normalized_code = _normalize_coupon_code(payload.referral_code)
    code_row = await db.referral_codes.find_one(
        {"code": normalized_code, "referral_type": referral_type, "is_active": True},
        {"_id": 0},
    )
    if not code_row:
        raise HTTPException(status_code=404, detail="Código de indicação inválido")

    referrer_user_id = str(code_row.get("owner_user_id") or "")
    referred_user_id = str(user.get("id") or "")
    if not referrer_user_id or referrer_user_id == referred_user_id:
        raise HTTPException(status_code=400, detail="Código inválido para este usuário")

    existing = await db.referrals.find_one(
        {
            "referred_user_id": referred_user_id,
            "referral_type": referral_type,
            "status": {"$nin": [REFERRAL_STATUS_CANCELED, REFERRAL_STATUS_FRAUD]},
        },
        {"_id": 0},
    )
    if existing:
        return _to_referral_record_response(existing)

    ip_address = _extract_request_ip(request)
    device_id = _extract_request_device_id(request)

    suspicious_flags: List[str] = []
    if device_id:
        repeated_device_count = await db.referrals.count_documents(
            {
                "device_id": device_id,
                "created_at": {"$gte": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
            }
        )
        if repeated_device_count >= 3:
            suspicious_flags.append("device_limit")

    if ip_address:
        repeated_ip_count = await db.referrals.count_documents(
            {
                "ip_address": ip_address,
                "created_at": {"$gte": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
            }
        )
        if repeated_ip_count >= 6:
            suspicious_flags.append("ip_limit")

    client_rules = ClientReferralRules(**dict(settings.get("client_rules") or {}))
    max_referrals_for_referrer = client_rules.referral_limit_per_user if role == "cliente" else max(20, client_rules.referral_limit_per_user)
    referrer_count = await db.referrals.count_documents(
        {
            "referrer_user_id": referrer_user_id,
            "referral_type": referral_type,
            "status": {"$nin": [REFERRAL_STATUS_CANCELED, REFERRAL_STATUS_FRAUD]},
        }
    )
    if referrer_count >= max_referrals_for_referrer:
        raise HTTPException(status_code=400, detail="Limite de indicações do indicador atingido")

    now_iso = datetime.now(timezone.utc).isoformat()
    referral_row = {
        "id": str(uuid.uuid4()),
        "referral_code": normalized_code,
        "referral_type": referral_type,
        "status": REFERRAL_STATUS_PENDING,
        "referrer_user_id": referrer_user_id,
        "referred_user_id": referred_user_id,
        "referrer_role": "cliente" if referral_type == "cliente_para_cliente" else "passeador",
        "referred_role": "cliente" if referral_type == "cliente_para_cliente" else "passeador",
        "created_at": now_iso,
        "updated_at": now_iso,
        "activated_at": None,
        "unlock_condition": {},
        "condition_progress": {},
        "reward_amount": 0.0,
        "reward_released_at": None,
        "benefit_released_at": None,
        "device_id": device_id or None,
        "ip_address": ip_address or None,
        "fraud_flags": suspicious_flags,
        "audit_log": [],
    }
    _append_referral_audit_event(referral_row, "created", "Indicação criada a partir de código.")

    if suspicious_flags:
        referral_row["status"] = REFERRAL_STATUS_FRAUD
        _append_referral_audit_event(referral_row, "fraud_flag", "Indicação marcada como suspeita automaticamente.")

    if referral_type == "cliente_para_cliente" and referral_row["status"] != REFERRAL_STATUS_FRAUD:
        indicated_coupon = await _create_private_referral_coupon(
            user_id=referred_user_id,
            amount=client_rules.indicated_discount_amount,
            validity_days=client_rules.benefit_validity_days,
            reason="Benefício do indicado",
        )
        referral_row["benefit_released_at"] = now_iso
        referral_row["referred_benefit_reference_id"] = indicated_coupon.get("id")
        referral_row["unlock_condition"] = {
            "required_paid_walks": client_rules.min_paid_walks_for_referrer_bonus,
            "referrer_reward_coupon_amount": client_rules.referrer_coupon_credit_amount,
        }

    if referral_type == "passeador_para_passeador":
        walker_rules = WalkerReferralRules(**dict(settings.get("walker_rules") or {}))
        referral_row["unlock_condition"] = {
            "required_completed_walks": walker_rules.min_completed_walks,
            "required_rating": walker_rules.min_rating_required,
            "max_no_show_rate": walker_rules.max_no_show_rate,
            "eligibility_window_days": walker_rules.eligibility_window_days,
            "fixed_bonus_amount": walker_rules.fixed_bonus_amount,
        }

    await db.referrals.insert_one(referral_row)
    return _to_referral_record_response(referral_row)


@api_router.post("/admin/alerts/{alert_id}/action", response_model=SystemAlertResponse)
async def decide_admin_system_alert(alert_id: str, payload: SystemAlertDecisionPayload, request: Request):
    admin_user = await _require_admin_permission(request, "dashboard")
    alert_row = await db.system_alerts.find_one({"alert_id": alert_id}, {"_id": 0})
    if not alert_row:
        raise HTTPException(status_code=404, detail="Alerta não encontrado")

    now_iso = datetime.now(timezone.utc).isoformat()
    decision = payload.decision
    justification = payload.justification.strip()
    severity = int(alert_row.get("nivel_gravidade") or 1)

    update_fields: Dict[str, Any] = {
        "atualizado_em": now_iso,
        "admin_decision_by": str(admin_user.get("id") or ""),
        "admin_decision_at": now_iso,
    }

    if decision == "ignore":
        if severity in {2, 4} and not justification:
            raise HTTPException(status_code=400, detail="Justificativa obrigatória para ignorar alertas de nível 2 e 4")
        update_fields["status"] = SYSTEM_ALERT_STATUS_IGNORED
        update_fields["acao_final"] = "Ignorado pelo administrador"
        update_fields["justificativa_admin"] = justification or alert_row.get("justificativa_admin")
    elif decision == "review_later":
        update_fields["status"] = SYSTEM_ALERT_STATUS_REVIEW_LATER
        update_fields["acao_final"] = str(alert_row.get("acao_final") or "Marcado para revisão posterior")
        if justification:
            update_fields["justificativa_admin"] = justification
    else:
        suggested_action = str(alert_row.get("acao_sugerida") or "")
        metadata = alert_row.get("metadata") if isinstance(alert_row.get("metadata"), dict) else {}

        action_result = str(alert_row.get("acao_final") or "Confirmado pelo administrador")
        auto_executado = bool(alert_row.get("auto_executado", False))
        if suggested_action in SYSTEM_ALERT_PERMITTED_AUTO_ACTIONS and not auto_executado:
            action_result = await _execute_reversible_alert_action(suggested_action, str(alert_row.get("user_id") or ""), metadata)
            auto_executado = True
        elif suggested_action not in SYSTEM_ALERT_PERMITTED_AUTO_ACTIONS:
            action_result = "Confirmado pelo administrador sem auto-ação"

        update_fields["status"] = SYSTEM_ALERT_STATUS_EXECUTED
        update_fields["acao_final"] = action_result
        update_fields["auto_executado"] = auto_executado
        if justification:
            update_fields["justificativa_admin"] = justification

    await db.system_alerts.update_one({"alert_id": alert_id}, {"$set": update_fields})
    updated_alert = await db.system_alerts.find_one({"alert_id": alert_id}, {"_id": 0})
    if not updated_alert:
        raise HTTPException(status_code=404, detail="Alerta não encontrado")

    await _record_system_alert_audit(
        alert_row=alert_row,
        decision=decision,
        actor_admin_id=str(admin_user.get("id") or ""),
        justification=justification,
        resulting_status=str(updated_alert.get("status") or ""),
        resulting_action=str(updated_alert.get("acao_final") or ""),
    )
    return SystemAlertResponse(**updated_alert)


@api_router.get("/admin/clients", response_model=List[AdminClientSummaryResponse])
async def list_admin_clients():
    walks = await db.walks.find({}, {"_id": 0}).to_list(2000)
    owner_profiles = await db.owner_profiles.find({}, {"_id": 0}).to_list(2000)
    owner_by_name = {profile.get("full_name", ""): profile for profile in owner_profiles if profile.get("full_name")}

    grouped: dict = {}
    for walk in walks:
        name = walk.get("client_name", "Cliente")
        client_id = _client_id_from_name(name)
        if client_id not in grouped:
            owner = owner_by_name.get(name)
            grouped[client_id] = {
                "id": client_id,
                "name": name,
                "phone": owner.get("phone", "") if owner else "",
                "neighborhood": walk.get("pickup_neighborhood", ""),
                "completed_walks_count": 0,
            }
        if walk.get("status") == STATUS_FINISHED:
            grouped[client_id]["completed_walks_count"] += 1

    return [AdminClientSummaryResponse(**item) for item in grouped.values()]


@api_router.get("/admin/clients/{client_id}", response_model=AdminClientDetailResponse)
async def get_admin_client_detail(client_id: str):
    walks = await db.walks.find({}, {"_id": 0}).to_list(2000)
    owner_profiles = await db.owner_profiles.find({}, {"_id": 0}).to_list(2000)

    client_walks = [walk for walk in walks if _client_id_from_name(walk.get("client_name", "")) == client_id]
    if not client_walks:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    first_walk = client_walks[0]
    name = first_walk.get("client_name", "Cliente")
    owner = next((profile for profile in owner_profiles if profile.get("full_name") == name), None)

    pets = []
    seen = set()
    for walk in client_walks:
        pet_name = walk.get("pet_name", "")
        if pet_name and pet_name not in seen:
            seen.add(pet_name)
            pets.append({"name": pet_name, "behavior_notes": walk.get("pet_behavior_notes", "")})

    walk_history = [
        {
            "id": walk.get("id"),
            "date": walk.get("walk_date"),
            "status": walk.get("status"),
            "duration_minutes": walk.get("duration_minutes"),
        }
        for walk in sorted(client_walks, key=lambda item: item.get("walk_datetime_iso", ""), reverse=True)
    ]

    return AdminClientDetailResponse(
        id=client_id,
        name=name,
        phone=owner.get("phone", "") if owner else "",
        email=owner.get("email", "") if owner else "",
        street=owner.get("street", "") if owner else first_walk.get("pickup_street", ""),
        number=owner.get("number", "") if owner else first_walk.get("pickup_number", ""),
        neighborhood=owner.get("neighborhood", "") if owner else first_walk.get("pickup_neighborhood", ""),
        complement=owner.get("complement", "") if owner else first_walk.get("pickup_complement", ""),
        pets=pets,
        walks=walk_history,
    )


@api_router.get("/admin/walks", response_model=List[WalkResponse])
async def list_admin_walks(
    status: Optional[str] = None,
    date: Optional[str] = None,
    walker_name: Optional[str] = None,
):
    query = {}
    if status:
        query["status"] = status
    if date:
        query["walk_date"] = date
    if walker_name:
        query["walker_name"] = walker_name

    walks = await db.walks.find(query, {"_id": 0}).sort("walk_datetime_iso", 1).to_list(500)
    refreshed_walks: List[dict] = []
    for walk in walks:
        refreshed_walks.append(await _apply_attendance_decision_if_needed(walk, trigger="admin_read"))
    return [_to_walk_response(walk) for walk in refreshed_walks]


def _walk_matches_walker_user(walk: dict, walker_user: dict) -> bool:
    walker_user_id = str(walker_user.get("id") or "")
    full_name = str(walker_user.get("full_name") or "")
    slug_name = full_name.lower().replace(" ", "-") if full_name else ""
    candidate_ids = {walker_user_id, f"partner-{walker_user_id}"}
    if slug_name:
        candidate_ids.add(f"partner-{slug_name}")

    return (
        walk.get("walker_user_id") == walker_user_id
        or str(walk.get("walker_id") or "") in candidate_ids
        or str(walk.get("walker_name") or "") == full_name
    )


def _to_percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def _safe_ratio(numerator: int, denominator: int, fallback: float = 1.0) -> float:
    if denominator <= 0:
        return fallback
    return max(0.0, min(1.0, numerator / denominator))


def _clamp_score(score: float) -> float:
    return round(max(0.0, min(100.0, score)), 2)


def _rating_std_deviation(values: List[int]) -> float:
    if len(values) < 2:
        return 0.0
    avg = sum(values) / len(values)
    variance = sum((value - avg) ** 2 for value in values) / len(values)
    return round(variance ** 0.5, 4)


def _walk_datetime_or_none(walk: dict) -> Optional[datetime]:
    walk_iso = _parse_iso_datetime(walk.get("walk_datetime_iso"))
    if walk_iso:
        return walk_iso

    walk_date = str(walk.get("walk_date") or "").strip()
    walk_time = str(walk.get("walk_time") or "").strip()
    if walk_date and walk_time:
        try:
            return datetime.fromisoformat(f"{walk_date}T{walk_time}:00+00:00")
        except ValueError:
            return None
    return None


def _fallback_base_score(completion_rate: float, punctuality_rate: float) -> float:
    _ = completion_rate
    _ = punctuality_rate
    return 75.0


def _public_rating_label(rating_avg: float, rating_count: int) -> str:
    if rating_count < MIN_CLIENT_RATING_DISPLAY_COUNT:
        return "Novo na plataforma"
    return f"{str(round(rating_avg, 1)).replace('.', ',')} ★ ({rating_count} avaliações)"


def _public_badge_label(rating_avg: float, rating_count: int) -> str:
    if rating_count < MIN_CLIENT_RATING_DISPLAY_COUNT:
        return "Novo na plataforma"
    if rating_count >= 10 and rating_avg >= 4.8:
        return "Top avaliado"
    if 4.5 <= rating_avg < 4.8:
        return "Muito bem avaliado"
    return ""


def _score_badge_label(score_final: float) -> str:
    if score_final >= 90:
        return "⭐ Destaque"
    if score_final >= 80:
        return "👍 Excelente"
    if score_final >= 70:
        return "✔ Confiável"
    return ""


def _determine_walker_level(
    score_final: float,
    completed_walks: int,
    no_show_rate: float,
    rating_avg: float = 0.0,
    cancel_rate: float = 0.0,
    checklist_streak: int = 0,
    infractions: int = 0,
) -> str:
    if not _is_feature_active("level_system_enabled"):
        return WALKER_LEVEL_BRONZE

    score_ratio = max(0.0, min(1.0, score_final / 100.0))

    silver_rule = {
        "score": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("silver_min_score_ratio"), 0.78),
        "walks": int(WALKER_LEVEL_SETTINGS_CACHE.get("silver_min_walks") or 10),
        "rating": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("silver_min_rating"), 4.5),
        "max_cancel_rate": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("silver_max_cancel_rate"), 15.0),
        "checklist_streak": int(WALKER_LEVEL_SETTINGS_CACHE.get("silver_min_checklist_streak") or 5),
    }
    gold_rule = {
        "score": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("gold_min_score_ratio"), 0.88),
        "walks": int(WALKER_LEVEL_SETTINGS_CACHE.get("gold_min_walks") or 25),
        "rating": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("gold_min_rating"), 4.7),
        "max_cancel_rate": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("gold_max_cancel_rate"), 8.0),
        "checklist_streak": int(WALKER_LEVEL_SETTINGS_CACHE.get("gold_min_checklist_streak") or 12),
        "max_infractions": int(WALKER_LEVEL_SETTINGS_CACHE.get("gold_max_infractions") or 0),
    }

    level = WALKER_LEVEL_BRONZE
    if (
        score_ratio >= _coerce_float(gold_rule.get("score"), 0.88)
        and completed_walks >= int(gold_rule.get("walks", 25))
        and rating_avg >= _coerce_float(gold_rule.get("rating"), 4.7)
        and cancel_rate <= _coerce_float(gold_rule.get("max_cancel_rate"), 8.0)
        and checklist_streak >= int(gold_rule.get("checklist_streak", 12))
        and infractions <= int(gold_rule.get("max_infractions", 0))
        and no_show_rate <= 6.0
    ):
        level = WALKER_LEVEL_GOLD
    elif (
        score_ratio >= _coerce_float(silver_rule.get("score"), 0.78)
        and completed_walks >= int(silver_rule.get("walks", 10))
        and rating_avg >= _coerce_float(silver_rule.get("rating"), 4.5)
        and cancel_rate <= _coerce_float(silver_rule.get("max_cancel_rate"), 15.0)
        and checklist_streak >= int(silver_rule.get("checklist_streak", 5))
        and no_show_rate <= 10.0
    ):
        level = WALKER_LEVEL_SILVER

    if infractions >= 3:
        return WALKER_LEVEL_BRONZE
    if infractions >= 2 and level == WALKER_LEVEL_GOLD:
        return WALKER_LEVEL_SILVER
    if infractions >= 2 and level == WALKER_LEVEL_SILVER:
        return WALKER_LEVEL_BRONZE
    return level


def _next_walker_level(current_level: str) -> Optional[str]:
    normalized = _normalize_walker_level_value(current_level)
    if normalized == WALKER_LEVEL_BRONZE:
        return WALKER_LEVEL_SILVER
    if normalized == WALKER_LEVEL_SILVER:
        return WALKER_LEVEL_GOLD
    return None


def _walker_level_progress_percent(
    current_level: str,
    score_final: float,
    completed_walks: int,
    no_show_rate: float,
    rating_avg: float = 0.0,
    cancel_rate: float = 0.0,
    checklist_streak: int = 0,
) -> float:
    if not _is_feature_active("level_system_enabled"):
        return 0.0

    next_level = _next_walker_level(current_level)
    if not next_level:
        return 100.0

    if next_level == WALKER_LEVEL_SILVER:
        rule = {
            "score": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("silver_min_score_ratio"), 0.78),
            "walks": int(WALKER_LEVEL_SETTINGS_CACHE.get("silver_min_walks") or 10),
            "rating": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("silver_min_rating"), 4.5),
            "max_cancel_rate": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("silver_max_cancel_rate"), 15.0),
            "checklist_streak": int(WALKER_LEVEL_SETTINGS_CACHE.get("silver_min_checklist_streak") or 5),
        }
    else:
        rule = {
            "score": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("gold_min_score_ratio"), 0.88),
            "walks": int(WALKER_LEVEL_SETTINGS_CACHE.get("gold_min_walks") or 25),
            "rating": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("gold_min_rating"), 4.7),
            "max_cancel_rate": _coerce_float(WALKER_LEVEL_SETTINGS_CACHE.get("gold_max_cancel_rate"), 8.0),
            "checklist_streak": int(WALKER_LEVEL_SETTINGS_CACHE.get("gold_min_checklist_streak") or 12),
        }
    target_score = _coerce_float(rule.get("score"), 1.0)
    target_walks = max(1, int(rule.get("walks", 1)))
    score_ratio = max(0.0, min(1.0, (score_final / 100.0) / max(0.01, target_score)))
    walks_ratio = max(0.0, min(1.0, completed_walks / target_walks))
    rating_target = _coerce_float(rule.get("rating"), 4.5)
    rating_ratio = max(0.0, min(1.0, rating_avg / max(0.1, rating_target)))
    cancel_max = max(1.0, _coerce_float(rule.get("max_cancel_rate"), 15.0))
    cancel_ratio = 1.0 if cancel_rate <= cancel_max else max(0.0, min(1.0, cancel_max / max(cancel_rate, 0.1)))
    checklist_target = max(1, int(rule.get("checklist_streak", 5)))
    checklist_ratio = max(0.0, min(1.0, checklist_streak / checklist_target))
    no_show_ratio = 1.0 if no_show_rate <= 10.0 else max(0.0, min(1.0, 10.0 / max(no_show_rate, 0.1)))
    progress = min(score_ratio, walks_ratio, rating_ratio, cancel_ratio, checklist_ratio, no_show_ratio)

    return round(progress * 100.0, 1)


def _weekly_mission_progress(week_walks: List[dict], week_tip_rows: List[dict]) -> dict:
    completed_walks = len([walk for walk in week_walks if walk.get("status") == STATUS_FINISHED])
    five_star_count = len([walk for walk in week_walks if walk.get("status") == STATUS_FINISHED and int(walk.get("rating", 0) or 0) == 5])
    tip_count = len([tip for tip in week_tip_rows if str(tip.get("status") or "") == "paid"])
    critical_acceptance_count = len(
        [
            walk
            for walk in week_walks
            if walk.get("status") == STATUS_FINISHED and _is_critical_hour(str(walk.get("walk_time") or ""), DEFAULT_CRITICAL_WINDOWS)
        ]
    )

    missions = [
        {
            "key": "mission_walks_5",
            "label": "Concluir 5 passeios",
            "current": float(completed_walks),
            "target": 5.0,
        },
        {
            "key": "mission_rating_5star_2",
            "label": "Receber 2 avaliações 5 estrelas",
            "current": float(five_star_count),
            "target": 2.0,
        },
        {
            "key": "mission_tip_1",
            "label": "Receber 1 gorjeta",
            "current": float(tip_count),
            "target": 1.0,
        },
        {
            "key": "mission_critical_hour_2",
            "label": "Aceitar 2 horários críticos",
            "current": float(critical_acceptance_count),
            "target": 2.0,
        },
    ]

    for mission in missions:
        target = max(1.0, _coerce_float(mission.get("target"), 1.0))
        current = _coerce_float(mission.get("current"), 0.0)
        mission["percentage"] = round(min(100.0, (current / target) * 100.0), 1)

    completed_all = all(_coerce_float(item.get("current"), 0.0) >= _coerce_float(item.get("target"), 1.0) for item in missions)

    return {
        "missions": missions,
        "completed_all": completed_all,
        "mission_bonus_points": 2.0 if completed_all else 0.0,
        "critical_acceptance_count": critical_acceptance_count,
    }


def _build_walker_leaderboard(
    *,
    period_start: datetime,
    period_end: datetime,
    walks: List[dict],
    walker_users: List[dict],
    limit: int = 10,
) -> List[dict]:
    profile_by_user_id = {str(user.get("id") or ""): user for user in walker_users}
    grouped: Dict[str, List[dict]] = {}
    for walk in walks:
        if walk.get("status") != STATUS_FINISHED:
            continue
        walk_dt = _walk_datetime_from_doc(walk)
        if not walk_dt or not (period_start <= walk_dt < period_end):
            continue
        walker_user_id = str(walk.get("walker_user_id") or "").strip()
        if not walker_user_id:
            walker_id = str(walk.get("walker_id") or "")
            walker_user_id = _walker_user_id_from_walker_id(walker_id)
        if not walker_user_id:
            continue
        grouped.setdefault(walker_user_id, []).append(walk)

    rows: List[dict] = []
    for walker_user_id, walker_walks in grouped.items():
        user = profile_by_user_id.get(walker_user_id, {})
        metrics = user.get("quality_metrics", {}) if isinstance(user.get("quality_metrics"), dict) else {}
        completed = len(walker_walks)
        rating_avg = _coerce_float(metrics.get("rating_weighted_avg"), _coerce_float(metrics.get("rating_avg"), 0.0))
        no_show_rate = _coerce_float(metrics.get("no_show_rate"), 0.0)
        reliability = max(0.0, 100.0 - min(100.0, no_show_rate * 8.0))
        ranking_score = round((completed * 6.0) + (rating_avg * 14.0) + (reliability * 0.35), 2)
        walker_level = _determine_walker_level(
            _coerce_float(metrics.get("score_final"), 0.0),
            int(metrics.get("completed_walks", completed) or completed),
            no_show_rate,
            rating_avg=rating_avg,
            cancel_rate=_coerce_float(metrics.get("cancel_rate"), 0.0),
            checklist_streak=int(user.get("kit_checklist_streak", 0) or 0),
            infractions=int(user.get("kit_missing_reports_count", 0) or 0),
        )
        rows.append(
            {
                "walker_user_id": walker_user_id,
                "name": str(user.get("full_name") or "Passeador"),
                "score": ranking_score,
                "completed_walks": completed,
                "rating_avg": round(rating_avg, 2),
                "no_show_rate": round(no_show_rate, 2),
                "walker_level": walker_level,
            }
        )

    rows.sort(
        key=lambda item: (
            -_coerce_float(item.get("score"), 0.0),
            -int(item.get("completed_walks", 0) or 0),
            -_coerce_float(item.get("rating_avg"), 0.0),
            _coerce_float(item.get("no_show_rate"), 100.0),
        )
    )

    output: List[dict] = []
    for index, item in enumerate(rows[: max(1, limit)], start=1):
        output.append(
            {
                "position": index,
                **item,
            }
        )
    return output


def _gamification_badges(metrics: dict, week_tip_total: float) -> List[str]:
    badges: List[str] = []
    if week_tip_total >= WEEKLY_TIP_GOAL_AMOUNT or _coerce_float(metrics.get("tip_recent_window_total"), 0.0) >= 40.0:
        badges.append("Pet favorito")
    if _coerce_float(metrics.get("rating_weighted_avg"), 0.0) >= 4.8 and int(metrics.get("rating_count", 0) or 0) >= 10:
        badges.append("5 estrelas")
    if _coerce_float(metrics.get("punctuality_rate"), 0.0) >= 0.95 and _coerce_float(metrics.get("severe_delay_rate"), 0.0) <= 5.0:
        badges.append("Pontual")
    if _coerce_float(metrics.get("recency_factor"), 1.0) > 1.02 or _coerce_float(metrics.get("score_last_7"), 0.0) >= (
        _coerce_float(metrics.get("score_reference"), 0.0) + 3.0
    ):
        badges.append("Em alta")
    return badges


def _mask_public_name(full_name: str) -> str:
    normalized = str(full_name or "").strip()
    if not normalized:
        return "Passeador"
    parts = [part for part in normalized.split() if part]
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def _detect_contact_attempt_patterns(text: str) -> List[str]:
    content = str(text or "").strip()
    if not content:
        return []

    reasons: List[str] = []
    digits_only = re.sub(r"\D", "", content)
    if len(digits_only) >= 8:
        reasons.append("PHONE")
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", content):
        reasons.append("EMAIL")
    if "@" in content:
        reasons.append("SOCIAL")
    if re.search(r"(https?://|www\.|t\.me/|wa\.me/)", content.lower()):
        reasons.append("URL")
    return list(dict.fromkeys(reasons))


def _is_disintermediation_flag_active(user_row: dict, reference_dt: Optional[datetime] = None) -> bool:
    now_dt = reference_dt or datetime.now(timezone.utc)
    if not bool(user_row.get("flag_suspeita_desintermediacao", False)):
        return False
    expires_at = _parse_iso_datetime(user_row.get("desintermediacao_flag_expires_at"))
    if not expires_at:
        return True
    return expires_at > now_dt


async def _clear_disintermediation_flag_if_expired(user_row: dict) -> None:
    if not bool(user_row.get("flag_suspeita_desintermediacao", False)):
        return
    expires_at = _parse_iso_datetime(user_row.get("desintermediacao_flag_expires_at"))
    now_dt = datetime.now(timezone.utc)
    if expires_at and expires_at <= now_dt:
        await db.users.update_one(
            {"id": user_row.get("id")},
            {
                "$set": {
                    "flag_suspeita_desintermediacao": False,
                    "desintermediacao_flag_reason": None,
                    "desintermediacao_flagged_at": None,
                    "desintermediacao_flag_expires_at": None,
                    "updated_at": now_dt.isoformat(),
                }
            },
        )


async def _post_match_cancel_rate_for_user(user_id: str) -> float:
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=DISINTERMEDIATION_CANCEL_WINDOW_DAYS)).isoformat()
    accepted_rows = await db.walks.find(
        {
            "client_user_id": user_id,
            "walker_user_id": {"$exists": True, "$ne": ""},
            "created_at": {"$gte": cutoff_iso},
            "status": {"$in": [STATUS_SCHEDULED, STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW, STATUS_FINISHED, STATUS_CANCELED, STATUS_NO_SHOW_CLIENT]},
        },
        {"_id": 0, "status": 1, "cancellation_justified_by_system": 1, "cancellation_justified_by_admin": 1},
    ).to_list(500)
    if not accepted_rows:
        return 0.0
    unjustified_canceled = [
        row
        for row in accepted_rows
        if row.get("status") == STATUS_CANCELED
        and not bool(row.get("cancellation_justified_by_system", False))
        and not bool(row.get("cancellation_justified_by_admin", False))
    ]
    return len(unjustified_canceled) / max(1, len(accepted_rows))


async def _evaluate_and_apply_disintermediation_flag(user_id: str) -> None:
    user_row = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not user_row:
        return

    await _clear_disintermediation_flag_if_expired(user_row)

    now_dt = datetime.now(timezone.utc)
    attempts_cutoff_iso = (now_dt - timedelta(days=DISINTERMEDIATION_CONTACT_WINDOW_DAYS)).isoformat()
    contact_attempts = await db.anti_disintermediation_events.count_documents(
        {
            "user_id": user_id,
            "event_type": "CONTACT_ATTEMPT",
            "counted_for_threshold": True,
            "created_at": {"$gte": attempts_cutoff_iso},
        }
    )
    cancel_rate = await _post_match_cancel_rate_for_user(user_id)

    trigger_reason: Optional[str] = None
    if contact_attempts >= DISINTERMEDIATION_CONTACT_ATTEMPTS_THRESHOLD:
        trigger_reason = "CONTACT_ATTEMPT"
    elif cancel_rate >= DISINTERMEDIATION_CANCEL_RATE_THRESHOLD:
        trigger_reason = "HIGH_CANCEL_RATE"

    if trigger_reason:
        flagged_at = now_dt.isoformat()
        expires_at = (now_dt + timedelta(days=DISINTERMEDIATION_FLAG_EXPIRY_DAYS)).isoformat()
        await db.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "flag_suspeita_desintermediacao": True,
                    "desintermediacao_flag_reason": trigger_reason,
                    "desintermediacao_flagged_at": flagged_at,
                    "desintermediacao_flag_expires_at": expires_at,
                    "updated_at": flagged_at,
                }
            },
        )
        alert_user = {
            **user_row,
            "flag_suspeita_desintermediacao": True,
            "desintermediacao_flag_reason": trigger_reason,
            "desintermediacao_flagged_at": flagged_at,
            "desintermediacao_flag_expires_at": expires_at,
        }
        await _generate_disintermediation_alert_for_user(alert_user, trigger_reason)


async def _execute_reversible_alert_action(action_key: str, user_id: str, metadata: Optional[dict] = None) -> str:
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    metadata = metadata or {}

    if action_key == "set_observation_7d":
        await db.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "quality_status": QUALITY_STATUS_OBSERVATION,
                    "observation_until": (now_dt + timedelta(days=SYSTEM_ALERT_AUTO_OBSERVATION_DAYS)).isoformat(),
                    "updated_at": now_iso,
                }
            },
        )
        return "Passeador colocado em observação por 7 dias"

    if action_key == "reduce_matching_priority":
        await db.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "match_penalty_until": (now_dt + timedelta(days=SYSTEM_ALERT_AUTO_OBSERVATION_DAYS)).isoformat(),
                    "match_penalty_points": max(DISINTERMEDIATION_MATCH_PENALTY_POINTS, _coerce_float(metadata.get("penalty_points"), DISINTERMEDIATION_MATCH_PENALTY_POINTS)),
                    "updated_at": now_iso,
                }
            },
        )
        return "Prioridade no matching reduzida temporariamente"

    if action_key == "mark_occurrence_pending":
        await db.operational_occurrences.insert_one(
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "status": "pending_analysis",
                "category": str(metadata.get("category") or "system_alert"),
                "description": str(metadata.get("description") or "Ocorrência marcada automaticamente para análise"),
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
        return "Ocorrência marcada como pendente de análise"

    if action_key == "apply_risk_flag":
        await db.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "behavior_risk_flag_active": True,
                    "behavior_risk_flag_reason": str(metadata.get("reason") or "SYSTEM_ALERT"),
                    "behavior_risk_flag_until": (now_dt + timedelta(days=SYSTEM_ALERT_AUTO_OBSERVATION_DAYS)).isoformat(),
                    "updated_at": now_iso,
                }
            },
        )
        return "Flag de risco comportamental aplicada temporariamente"

    if action_key == "suspend_auto_preselection":
        await db.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "auto_preselection_suspended_until": (now_dt + timedelta(days=SYSTEM_ALERT_AUTO_OBSERVATION_DAYS)).isoformat(),
                    "updated_at": now_iso,
                }
            },
        )
        return "Pré-seleção automática suspensa temporariamente"

    if action_key == "block_suspicious_coupon":
        coupon_id = str(metadata.get("coupon_id") or "").strip()
        if coupon_id:
            await db.coupons.update_one(
                {"id": coupon_id},
                {
                    "$set": {
                        "status": "blocked",
                        "blocked_by_system": True,
                        "blocked_reason": "system_alert_suspicious_coupon",
                        "updated_at": now_iso,
                    }
                },
            )
            return "Cupom suspeito bloqueado automaticamente"
        return "Ação de bloqueio de cupom solicitada sem cupom associado"

    return "Sem ação automática executada"


async def _upsert_system_alert(
    *,
    tipo_alerta: str,
    nivel_gravidade: int,
    user_id: str,
    user_role: str,
    mensagem: str,
    acao_sugerida: str,
    metadata: Optional[dict] = None,
    categoria: Optional[Literal["operacional", "financeiro", "comportamental", "sistemico"]] = None,
    contexto: str = "",
) -> dict:
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    settings = await _get_system_alert_priority_settings()
    resolved_category = categoria or _alert_category_from_tipo(tipo_alerta)
    grouping_window = _grouping_window_for_category(settings, resolved_category)
    signature = f"{tipo_alerta}:{user_id}:{acao_sugerida}"
    cutoff_iso = (now_dt - grouping_window).isoformat()
    metadata_payload = metadata.copy() if isinstance(metadata, dict) else {}

    existing = await db.system_alerts.find_one(
        {
            "alert_signature": signature,
            "criado_em": {"$gte": cutoff_iso},
            "status": {"$in": [SYSTEM_ALERT_STATUS_PENDING, SYSTEM_ALERT_STATUS_EXECUTED, SYSTEM_ALERT_STATUS_REVIEW_LATER]},
        },
        {"_id": 0},
    )

    if existing:
        existing_occurrences = int(existing.get("occurrences") or 1)
        updated_occurrences = existing_occurrences + 1
        priority_data = _build_alert_priority(
            settings=settings,
            provided_level=int(max(1, min(4, nivel_gravidade))),
            occurrences=updated_occurrences,
            metadata=metadata_payload,
            created_at_iso=str(existing.get("criado_em") or now_iso),
        )
        resolved_level = int(max(1, min(4, priority_data.get("nivel_prioridade", nivel_gravidade))))

        await db.system_alerts.update_one(
            {"alert_id": existing.get("alert_id")},
            {
                "$set": {
                    "mensagem": mensagem,
                    "nivel_gravidade": resolved_level,
                    "atualizado_em": now_iso,
                    "metadata": metadata_payload,
                    "categoria": resolved_category,
                    "prioridade_score": priority_data.get("prioridade_score", 0.0),
                    "prioridade_fatores": priority_data.get("fatores", {}),
                    "contexto": contexto,
                },
                "$inc": {"occurrences": 1},
            },
        )
        refreshed = await db.system_alerts.find_one({"alert_id": existing.get("alert_id")}, {"_id": 0})
        return refreshed or existing

    priority_data = _build_alert_priority(
        settings=settings,
        provided_level=int(max(1, min(4, nivel_gravidade))),
        occurrences=1,
        metadata=metadata_payload,
        created_at_iso=now_iso,
    )
    resolved_level = int(max(1, min(4, priority_data.get("nivel_prioridade", nivel_gravidade))))

    alert_doc = {
        "alert_id": str(uuid.uuid4()),
        "alert_signature": signature,
        "tipo_alerta": tipo_alerta,
        "categoria": resolved_category,
        "prioridade_score": priority_data.get("prioridade_score", 0.0),
        "prioridade_fatores": priority_data.get("fatores", {}),
        "nivel_gravidade": resolved_level,
        "status": SYSTEM_ALERT_STATUS_PENDING,
        "user_id": user_id,
        "user_role": user_role,
        "contexto": contexto,
        "mensagem": mensagem,
        "acao_sugerida": acao_sugerida,
        "acao_final": None,
        "auto_executado": False,
        "justificativa_admin": None,
        "occurrences": 1,
        "metadata": metadata_payload,
        "criado_em": now_iso,
        "atualizado_em": now_iso,
    }
    await db.system_alerts.insert_one(alert_doc)

    if int(alert_doc["nivel_gravidade"]) == 3 and acao_sugerida in SYSTEM_ALERT_PERMITTED_AUTO_ACTIONS:
        action_result = await _execute_reversible_alert_action(acao_sugerida, user_id, metadata_payload)
        await db.system_alerts.update_one(
            {"alert_id": alert_doc["alert_id"]},
            {
                "$set": {
                    "status": SYSTEM_ALERT_STATUS_EXECUTED,
                    "auto_executado": True,
                    "acao_final": action_result,
                    "atualizado_em": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        alert_doc["status"] = SYSTEM_ALERT_STATUS_EXECUTED
        alert_doc["auto_executado"] = True
        alert_doc["acao_final"] = action_result

    return alert_doc


async def _record_system_alert_audit(
    *,
    alert_row: dict,
    decision: str,
    actor_admin_id: str,
    justification: str,
    resulting_status: str,
    resulting_action: str,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.system_alert_audit.insert_one(
        {
            "id": str(uuid.uuid4()),
            "alert_id": str(alert_row.get("alert_id") or ""),
            "tipo_alerta": str(alert_row.get("tipo_alerta") or ""),
            "categoria": str(alert_row.get("categoria") or _alert_category_from_tipo(str(alert_row.get("tipo_alerta") or ""))),
            "prioridade_score": _coerce_float(alert_row.get("prioridade_score"), 0.0),
            "nivel_gravidade": int(alert_row.get("nivel_gravidade") or 1),
            "user_id": str(alert_row.get("user_id") or ""),
            "user_role": str(alert_row.get("user_role") or ""),
            "acao_sugerida": str(alert_row.get("acao_sugerida") or ""),
            "acao_executada": resulting_action,
            "decisao_admin": decision,
            "justificativa_admin": justification,
            "status_resultante": resulting_status,
            "actor_admin_id": actor_admin_id,
            "created_at": now_iso,
        }
    )


async def _generate_walker_alerts(walker: dict, metrics: dict, walker_walks: List[dict]) -> None:
    user_id = str(walker.get("id") or "")
    if not user_id:
        return

    rating_recent = _coerce_float(metrics.get("rating_recent_avg"), 0.0)
    rating_avg = _coerce_float(metrics.get("rating_avg"), 0.0)
    severe_delay_rate = _coerce_float(metrics.get("severe_delay_rate"), 0.0)
    no_show_rate = _coerce_float(metrics.get("no_show_rate"), 0.0)
    completion_rate_raw = _coerce_float(
        metrics.get("completion_rate", metrics.get("completion_percent", 100.0)),
        1.0,
    )
    completion_rate = completion_rate_raw / 100.0 if completion_rate_raw > 1 else completion_rate_raw
    rating_std = _coerce_float(metrics.get("rating_std_dev"), 0.0)
    full_name = str(walker.get("full_name") or "Passeador")

    sorted_walks = sorted(walker_walks, key=lambda row: row.get("walk_datetime_iso", ""), reverse=True)
    recent_finished = [walk for walk in sorted_walks if walk.get("status") == STATUS_FINISHED][:8]
    no_tip_streak = len(recent_finished) >= 5 and all(_coerce_float(walk.get("tip_amount"), 0.0) <= 0 for walk in recent_finished)
    high_tip_count = sum(1 for walk in recent_finished if _coerce_float(walk.get("tip_amount"), 0.0) >= 35.0)
    suspicious_tip_pattern = len(recent_finished) >= 5 and high_tip_count >= 3

    common_metadata = {
        "impacto_financeiro": 25.0,
        "risco_operacional": 30.0,
        "reincidencia": min(100.0, len(recent_finished) * 10.0),
        "proximidade_tempo": 70.0,
        "frequencia_evento": min(100.0, len(sorted_walks) * 5.0),
    }

    if rating_recent > 0 and rating_avg > 0 and rating_recent <= max(3.8, rating_avg - 0.6):
        await _upsert_system_alert(
            tipo_alerta="WALKER_RATING_DROP",
            nivel_gravidade=2,
            user_id=user_id,
            user_role="passeador",
            mensagem=f"{full_name} apresentou queda de nota recente ({rating_recent:.1f}).",
            contexto="Queda progressiva de qualidade detectada no histórico recente.",
            acao_sugerida="reduce_matching_priority",
            categoria="operacional",
            metadata={
                **common_metadata,
                "impacto_financeiro": 30.0,
                "risco_operacional": 60.0,
                "reincidencia": 55.0,
                "frequencia_evento": 50.0,
            },
        )

    if severe_delay_rate >= 15.0:
        await _upsert_system_alert(
            tipo_alerta="WALKER_SEVERE_DELAY_SPIKE",
            nivel_gravidade=3,
            user_id=user_id,
            user_role="passeador",
            mensagem=f"{full_name} com atraso grave elevado ({severe_delay_rate:.1f}%).",
            contexto="Atrasos graves recorrentes nas últimas corridas.",
            acao_sugerida="set_observation_7d",
            categoria="operacional",
            metadata={
                **common_metadata,
                "impacto_financeiro": 45.0,
                "risco_operacional": 80.0,
                "reincidencia": min(100.0, severe_delay_rate * 4.0),
                "frequencia_evento": min(100.0, severe_delay_rate * 3.0),
                "description": "Atrasos graves acima do limite.",
            },
        )

    if no_show_rate >= 5.0:
        await _upsert_system_alert(
            tipo_alerta="WALKER_NO_SHOW_SPIKE",
            nivel_gravidade=3,
            user_id=user_id,
            user_role="passeador",
            mensagem=f"{full_name} com no-show acima do padrão ({no_show_rate:.1f}%).",
            contexto="Impacta diretamente confiança do cliente e operação.",
            acao_sugerida="reduce_matching_priority",
            categoria="operacional",
            metadata={
                **common_metadata,
                "impacto_financeiro": 55.0,
                "risco_operacional": 85.0,
                "reincidencia": min(100.0, no_show_rate * 5.0),
                "frequencia_evento": min(100.0, no_show_rate * 4.0),
                "penalty_points": DISINTERMEDIATION_MATCH_PENALTY_POINTS,
            },
        )

    if completion_rate < 0.85:
        await _upsert_system_alert(
            tipo_alerta="WALKER_LOW_COMPLETION",
            nivel_gravidade=2,
            user_id=user_id,
            user_role="passeador",
            mensagem=f"Taxa de conclusão de {full_name} está baixa ({completion_rate * 100:.1f}%).",
            contexto="Queda de performance com risco operacional moderado.",
            acao_sugerida="set_observation_7d",
            categoria="operacional",
            metadata={
                **common_metadata,
                "impacto_financeiro": 35.0,
                "risco_operacional": 65.0,
                "reincidencia": 50.0,
                "frequencia_evento": 45.0,
            },
        )

    if no_tip_streak:
        await _upsert_system_alert(
            tipo_alerta="WALKER_NO_TIP_STREAK",
            nivel_gravidade=1,
            user_id=user_id,
            user_role="passeador",
            mensagem=f"{full_name} com sequência recente sem gorjetas.",
            contexto="Alerta informativo para acompanhamento de experiência do cliente.",
            acao_sugerida="revisar_desempenho",
            categoria="financeiro",
            metadata={
                **common_metadata,
                "impacto_financeiro": 25.0,
                "risco_operacional": 25.0,
                "reincidencia": 35.0,
                "frequencia_evento": 40.0,
            },
        )

    if rating_std >= 1.2:
        await _upsert_system_alert(
            tipo_alerta="WALKER_INCONSISTENT_PATTERN",
            nivel_gravidade=2,
            user_id=user_id,
            user_role="passeador",
            mensagem=f"Padrão comportamental inconsistente detectado para {full_name}.",
            contexto="Oscilação de avaliação e entrega acima do padrão esperado.",
            acao_sugerida="set_observation_7d",
            categoria="comportamental",
            metadata={
                **common_metadata,
                "impacto_financeiro": 30.0,
                "risco_operacional": 58.0,
                "reincidencia": 60.0,
                "frequencia_evento": 55.0,
            },
        )

    if suspicious_tip_pattern:
        await _upsert_system_alert(
            tipo_alerta="FINANCIAL_TIP_ANOMALY",
            nivel_gravidade=3,
            user_id=user_id,
            user_role="passeador",
            mensagem=f"Padrão de gorjetas anômalo detectado para {full_name}.",
            contexto="Frequência e valor de gorjetas fora do padrão recente.",
            acao_sugerida="apply_risk_flag",
            categoria="financeiro",
            metadata={
                **common_metadata,
                "impacto_financeiro": 75.0,
                "risco_operacional": 65.0,
                "reincidencia": min(100.0, high_tip_count * 20.0),
                "frequencia_evento": 70.0,
                "reason": "FINANCIAL_TIP_ANOMALY",
            },
        )

    if rating_recent >= 4.8 and completion_rate >= 0.96 and severe_delay_rate <= 2.0 and no_show_rate <= 1.0:
        await _upsert_system_alert(
            tipo_alerta="WALKER_CONSISTENT_HIGH_PERFORMANCE",
            nivel_gravidade=1,
            user_id=user_id,
            user_role="passeador",
            mensagem=f"{full_name} mantém alta performance consistente.",
            contexto="Sugerido aumentar exposição no ranking de matching.",
            acao_sugerida="increase_ranking_exposure",
            categoria="operacional",
            metadata={
                **common_metadata,
                "impacto_financeiro": 18.0,
                "risco_operacional": 12.0,
                "reincidencia": 45.0,
                "frequencia_evento": 50.0,
            },
        )


async def _generate_disintermediation_alert_for_user(user_row: dict, trigger_reason: str) -> None:
    user_id = str(user_row.get("id") or "").strip()
    if not user_id:
        return

    user_role = str(user_row.get("role") or "cliente")
    reason = str(trigger_reason or "FLAG_ACTIVE").strip().upper()
    nivel = 3
    acao_sugerida = "mark_occurrence_pending"
    mensagem = "Comportamento suspeito de desintermediação detectado."
    contexto = "Risco comportamental identificado pelo motor de decisão."
    metadata = {
        "category": "desintermediacao",
        "description": "Sinal de risco para análise da equipe.",
        "impacto_financeiro": 70.0,
        "risco_operacional": 78.0,
        "reincidencia": 60.0,
        "proximidade_tempo": 85.0,
        "frequencia_evento": 70.0,
    }

    if reason == "CONTACT_ATTEMPT":
        nivel = 3
        acao_sugerida = "reduce_matching_priority" if user_role == "passeador" else "mark_occurrence_pending"
        mensagem = "Tentativas repetidas de troca de contato foram detectadas."
        contexto = "Sugerida contenção imediata para reduzir risco de desintermediação."
        metadata["description"] = "Tentativas de contato externo acima do limite em 7 dias."
    elif reason == "HIGH_CANCEL_RATE":
        nivel = 4
        acao_sugerida = "mark_occurrence_pending"
        mensagem = "Taxa elevada de cancelamentos pós-match detectada."
        contexto = "Revisão humana obrigatória para possível restrição/suspensão."
        metadata["description"] = "Requer revisão humana obrigatória por risco de desintermediação."
        metadata["impacto_financeiro"] = 85.0
        metadata["risco_operacional"] = 88.0

    await _upsert_system_alert(
        tipo_alerta=f"DISINTERMEDIATION_{reason}",
        nivel_gravidade=nivel,
        user_id=user_id,
        user_role=user_role,
        mensagem=mensagem,
        contexto=contexto,
        acao_sugerida=acao_sugerida,
        categoria="comportamental",
        metadata=metadata,
    )


async def _generate_client_alerts(client_user: dict, client_walks: List[dict]) -> None:
    client_user_id = str(client_user.get("id") or "").strip()
    if not client_user_id or not client_walks:
        return

    client_name = str(client_user.get("full_name") or "Cliente")

    now_dt = datetime.now(timezone.utc)
    cutoff_14d = now_dt - timedelta(days=14)
    cutoff_30d = now_dt - timedelta(days=30)

    def _walk_event_time(walk: dict) -> Optional[datetime]:
        return _walk_datetime_from_doc(walk) or _parse_iso_datetime(walk.get("updated_at")) or _parse_iso_datetime(walk.get("created_at"))

    recent_14_walks = [walk for walk in client_walks if (event_dt := _walk_event_time(walk)) and event_dt >= cutoff_14d]
    recent_30_walks = [walk for walk in client_walks if (event_dt := _walk_event_time(walk)) and event_dt >= cutoff_30d]

    no_show_client_count = sum(1 for walk in recent_30_walks if walk.get("status") == STATUS_NO_SHOW_CLIENT)
    pending_analysis_count = sum(1 for walk in recent_14_walks if walk.get("status") == STATUS_PENDING_REVIEW)

    accepted_14 = [
        walk
        for walk in recent_14_walks
        if walk.get("status") in {STATUS_SCHEDULED, STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW, STATUS_FINISHED, STATUS_CANCELED, STATUS_NO_SHOW_CLIENT}
    ]
    canceled_by_client_14 = [
        walk
        for walk in recent_14_walks
        if walk.get("status") == STATUS_CANCELED
        and walk.get("tipoCancelamento") == "cliente"
        and not bool(walk.get("cancellation_justified_by_system", False))
        and not bool(walk.get("cancellation_justified_by_admin", False))
    ]
    cancel_rate = len(canceled_by_client_14) / max(1, len(accepted_14)) if accepted_14 else 0.0

    if no_show_client_count >= 2:
        await _upsert_system_alert(
            tipo_alerta="CLIENT_NO_SHOW_REINCIDENCE",
            nivel_gravidade=3,
            user_id=client_user_id,
            user_role="cliente",
            mensagem=f"{client_name} com reincidência de no-show ({no_show_client_count} em 30 dias).",
            contexto="Impacto direto na previsibilidade operacional.",
            acao_sugerida="mark_occurrence_pending",
            categoria="operacional",
            metadata={
                "category": "cliente",
                "description": "Ocorrência criada automaticamente para revisão do histórico de presença.",
                "impacto_financeiro": 55.0,
                "risco_operacional": 80.0,
                "reincidencia": min(100.0, no_show_client_count * 25.0),
                "proximidade_tempo": 70.0,
                "frequencia_evento": min(100.0, no_show_client_count * 20.0),
            },
        )

    if len(accepted_14) >= 3 and cancel_rate >= DISINTERMEDIATION_CANCEL_RATE_THRESHOLD:
        await _upsert_system_alert(
            tipo_alerta="CLIENT_HIGH_CANCEL_RATE",
            nivel_gravidade=2,
            user_id=client_user_id,
            user_role="cliente",
            mensagem=f"{client_name} com taxa de cancelamento elevada ({cancel_rate * 100:.1f}% em 14 dias).",
            contexto="Sugestão de revisão preventiva para reduzir churn operacional.",
            acao_sugerida="mark_occurrence_pending",
            categoria="comportamental",
            metadata={
                "category": "cliente",
                "description": "Sugerida análise de padrão de cancelamento para evitar risco operacional.",
                "impacto_financeiro": 48.0,
                "risco_operacional": 62.0,
                "reincidencia": min(100.0, cancel_rate * 100.0),
                "proximidade_tempo": 65.0,
                "frequencia_evento": min(100.0, len(canceled_by_client_14) * 20.0),
            },
        )

    if pending_analysis_count >= 2:
        await _upsert_system_alert(
            tipo_alerta="CLIENT_PENDING_ANALYSIS_REINCIDENCE",
            nivel_gravidade=4,
            user_id=client_user_id,
            user_role="cliente",
            mensagem=f"{client_name} com múltiplas ocorrências pendentes de análise recente.",
            contexto="Revisão humana obrigatória para possível restrição/suspensão.",
            acao_sugerida="mark_occurrence_pending",
            categoria="comportamental",
            metadata={
                "category": "cliente",
                "description": "Revisão humana obrigatória para sequência de ocorrências críticas.",
                "impacto_financeiro": 75.0,
                "risco_operacional": 85.0,
                "reincidencia": min(100.0, pending_analysis_count * 30.0),
                "proximidade_tempo": 80.0,
                "frequencia_evento": min(100.0, pending_analysis_count * 25.0),
            },
        )


async def _generate_financial_alerts(all_walks: List[dict]) -> None:
    now_dt = datetime.now(timezone.utc)
    cutoff_14d = now_dt - timedelta(days=14)
    refund_counter: Dict[str, int] = {}
    dispute_counter: Dict[str, int] = {}

    for walk in all_walks:
        event_dt = _walk_datetime_from_doc(walk) or _parse_iso_datetime(walk.get("updated_at")) or _parse_iso_datetime(walk.get("created_at"))
        if not event_dt or event_dt < cutoff_14d:
            continue

        client_id = str(walk.get("client_user_id") or "").strip()
        if not client_id:
            continue

        refund_amount = _coerce_float(walk.get("refund_amount"), 0.0)
        if refund_amount >= 25.0:
            refund_counter[client_id] = refund_counter.get(client_id, 0) + 1

        dispute_status = str(walk.get("dispute_status") or "").strip().lower()
        if dispute_status in {"open", "pending", "escalated", "recurring"}:
            dispute_counter[client_id] = dispute_counter.get(client_id, 0) + 1

    for client_id, total_refunds in refund_counter.items():
        if total_refunds < 2:
            continue
        await _upsert_system_alert(
            tipo_alerta="FINANCIAL_REFUND_OUTLIER",
            nivel_gravidade=3,
            user_id=client_id,
            user_role="cliente",
            mensagem=f"Cliente com reembolsos fora do padrão ({total_refunds} em 14 dias).",
            contexto="Possível risco financeiro recorrente.",
            acao_sugerida="mark_occurrence_pending",
            categoria="financeiro",
            metadata={
                "impacto_financeiro": min(100.0, total_refunds * 30.0),
                "risco_operacional": 60.0,
                "reincidencia": min(100.0, total_refunds * 25.0),
                "proximidade_tempo": 70.0,
                "frequencia_evento": min(100.0, total_refunds * 20.0),
            },
        )

    for client_id, disputes in dispute_counter.items():
        if disputes < 2:
            continue
        await _upsert_system_alert(
            tipo_alerta="FINANCIAL_RECURRING_DISPUTE",
            nivel_gravidade=4,
            user_id=client_id,
            user_role="cliente",
            mensagem=f"Cliente com disputas financeiras recorrentes ({disputes} em 14 dias).",
            contexto="Revisão humana obrigatória para tratar disputa sensível.",
            acao_sugerida="review_financial_dispute",
            categoria="financeiro",
            metadata={
                "impacto_financeiro": min(100.0, disputes * 35.0),
                "risco_operacional": 85.0,
                "reincidencia": min(100.0, disputes * 30.0),
                "proximidade_tempo": 80.0,
                "frequencia_evento": min(100.0, disputes * 25.0),
            },
        )


async def _generate_systemic_alerts(all_walks: List[dict]) -> None:
    settings = await _get_system_alert_priority_settings()
    region_failure_threshold = int(settings.get("systemic_region_failure_threshold") or SYSTEM_ALERT_SYSTEMIC_REGION_FAILURE_THRESHOLD)
    overload_threshold = int(settings.get("systemic_overload_threshold") or SYSTEM_ALERT_SYSTEMIC_OVERLOAD_THRESHOLD)

    now_dt = datetime.now(timezone.utc)
    cutoff_24h = now_dt - timedelta(hours=24)
    region_failure_counter: Dict[str, int] = {}
    region_hour_counter: Dict[str, int] = {}

    for walk in all_walks:
        event_dt = _walk_datetime_from_doc(walk) or _parse_iso_datetime(walk.get("updated_at")) or _parse_iso_datetime(walk.get("created_at"))
        if not event_dt or event_dt < cutoff_24h:
            continue

        region = str(walk.get("neighborhood") or walk.get("city") or "regiao_indefinida").strip().lower()
        if not region:
            region = "regiao_indefinida"

        if walk.get("status") in {STATUS_NO_SHOW_WALKER, STATUS_NO_SHOW_CLIENT, STATUS_PENDING_REVIEW}:
            region_failure_counter[region] = region_failure_counter.get(region, 0) + 1

        hour_key = f"{region}:{event_dt.strftime('%Y-%m-%d-%H')}"
        region_hour_counter[hour_key] = region_hour_counter.get(hour_key, 0) + 1

    for region, failures in region_failure_counter.items():
        if failures < region_failure_threshold:
            continue
        await _upsert_system_alert(
            tipo_alerta="SYSTEM_REGION_FAILURE",
            nivel_gravidade=4,
            user_id=f"region:{region}",
            user_role="sistema",
            mensagem=f"Falhas operacionais concentradas na região {region} ({failures} em 24h).",
            contexto="Ação imediata necessária para conter impacto em escala.",
            acao_sugerida="suspend_auto_preselection",
            categoria="sistemico",
            metadata={
                "impacto_financeiro": min(100.0, failures * 20.0),
                "risco_operacional": 95.0,
                "reincidencia": min(100.0, failures * 20.0),
                "proximidade_tempo": 90.0,
                "frequencia_evento": min(100.0, failures * 18.0),
            },
        )

    for hour_key, load in region_hour_counter.items():
        if load < overload_threshold:
            continue
        region = hour_key.split(":", 1)[0]
        await _upsert_system_alert(
            tipo_alerta="SYSTEM_SCHEDULE_OVERLOAD",
            nivel_gravidade=3,
            user_id=f"region:{region}",
            user_role="sistema",
            mensagem=f"Sobrecarga de horários detectada em {region} ({load} eventos/hora).",
            contexto="Sugerido rebalanceamento operacional e revisão de alocação.",
            acao_sugerida="rebalance_capacity",
            categoria="sistemico",
            metadata={
                "impacto_financeiro": min(100.0, load * 10.0),
                "risco_operacional": 75.0,
                "reincidencia": min(100.0, load * 12.0),
                "proximidade_tempo": 85.0,
                "frequencia_evento": min(100.0, load * 10.0),
            },
        )


async def _run_system_alert_engine_snapshot() -> None:
    all_walks = await db.walks.find({}, {"_id": 0}).to_list(5000)

    await _generate_financial_alerts(all_walks)
    await _generate_systemic_alerts(all_walks)

    walkers = await db.users.find({"role": "passeador"}, {"_id": 0}).to_list(1000)
    walker_walks_by_user: Dict[str, List[dict]] = {}
    for walk in all_walks:
        walker_user_id = str(walk.get("walker_user_id") or "").strip()
        if walker_user_id:
            walker_walks_by_user.setdefault(walker_user_id, []).append(walk)

    for walker in walkers:
        walker_id = str(walker.get("id") or "").strip()
        if not walker_id:
            continue
        walker_walks = walker_walks_by_user.get(walker_id, [])
        metrics = walker.get("quality_metrics") if isinstance(walker.get("quality_metrics"), dict) else {}
        await _generate_walker_alerts(walker, metrics, walker_walks)
        if _is_disintermediation_flag_active(walker):
            await _generate_disintermediation_alert_for_user(
                walker,
                str(walker.get("desintermediacao_flag_reason") or "FLAG_ACTIVE"),
            )

    clients = await db.users.find({"role": "cliente"}, {"_id": 0}).to_list(1500)
    client_walks_by_user: Dict[str, List[dict]] = {}
    for walk in all_walks:
        client_user_id = str(walk.get("client_user_id") or "").strip()
        if client_user_id:
            client_walks_by_user.setdefault(client_user_id, []).append(walk)

    for client_row in clients:
        client_id = str(client_row.get("id") or "").strip()
        if not client_id:
            continue
        await _generate_client_alerts(client_row, client_walks_by_user.get(client_id, []))
        if _is_disintermediation_flag_active(client_row):
            await _generate_disintermediation_alert_for_user(
                client_row,
                str(client_row.get("desintermediacao_flag_reason") or "FLAG_ACTIVE"),
            )


def _status_penalty_factor(quality_status: str, rating_recent_avg: float, severe_delay_rate: float) -> float:
    if quality_status == QUALITY_STATUS_PREMIUM:
        return 1.0
    if quality_status == QUALITY_STATUS_ACTIVE:
        return 1.0
    if quality_status == QUALITY_STATUS_OBSERVATION:
        if rating_recent_avg < 4.0 or severe_delay_rate > 15:
            return 0.8
        return 0.9
    if quality_status == QUALITY_STATUS_RESTRICTED:
        return 0.5
    if quality_status == QUALITY_STATUS_SUSPENDED:
        return 0.0
    return 1.0


def _base_score_from_components(
    rating_normalized: float,
    completion_rate: float,
    punctuality_rate: float,
    no_show_reliability: float,
) -> float:
    raw = (
        (rating_normalized * 0.40)
        + (completion_rate * 0.25)
        + (punctuality_rate * 0.20)
        + (no_show_reliability * 0.15)
    ) * 100
    return _clamp_score(raw)


def _compute_reputation_metrics(
    walker_walks: List[dict],
    quality_status: str,
    tip_total_amount: float = 0.0,
    tip_rows: Optional[List[dict]] = None,
    walker_controls: Optional[dict] = None,
    platform_tip_average: float = 0.0,
) -> dict:
    now_dt = datetime.now(timezone.utc)
    if not _is_feature_active("tips"):
        tip_total_amount = 0.0
        tip_rows = []

    sorted_walks = sorted(
        walker_walks,
        key=lambda row: row.get("walk_datetime_iso", ""),
        reverse=True,
    )

    total_walks = len(sorted_walks)
    completed_walks = [walk for walk in sorted_walks if walk.get("status") == STATUS_FINISHED]
    completed_count = len(completed_walks)
    severe_delays = [walk for walk in sorted_walks if _derive_occurrence_status(walk) == OCC_LATE_SEVERE]
    severe_delay_count = len(severe_delays)
    no_shows = [walk for walk in sorted_walks if walk.get("status") == STATUS_NO_SHOW_WALKER]
    no_show_count = len(no_shows)
    canceled_walks = [walk for walk in sorted_walks if walk.get("status") == STATUS_CANCELED]
    cancel_count = len(canceled_walks)

    rated_walks = [walk for walk in sorted_walks if isinstance(walk.get("rating"), int) and int(walk.get("rating", 0)) > 0]
    rating_values = [int(walk.get("rating", 0)) for walk in rated_walks]
    rating_count = len(rating_values)

    client_finished_count: Dict[str, int] = {}
    for walk in completed_walks:
        client_key = str(walk.get("client_user_id") or "").strip() or str(walk.get("client_name") or "").strip().lower()
        if client_key:
            client_finished_count[client_key] = client_finished_count.get(client_key, 0) + 1

    def _client_weight(row: dict) -> float:
        client_key = str(row.get("client_user_id") or "").strip() or str(row.get("client_name") or "").strip().lower()
        if not client_key:
            return 1.0
        return 1.2 if client_finished_count.get(client_key, 0) >= 3 else 1.0

    def _weighted_avg(rows: List[dict], fallback: float) -> float:
        weighted_sum = 0.0
        weight_total = 0.0
        for row in rows:
            rating_value = row.get("rating")
            if not isinstance(rating_value, int) or rating_value <= 0:
                continue
            weight = _client_weight(row)
            weighted_sum += float(rating_value) * weight
            weight_total += weight
        if weight_total <= 0:
            return fallback
        return round(weighted_sum / weight_total, 2)

    rating_avg = _weighted_avg(rated_walks, 0.0)
    recent_rated_walks = rated_walks[:RECENT_RATING_WINDOW]
    recent_rated_values = [int(walk.get("rating", 0)) for walk in recent_rated_walks]
    rating_recent_avg = _weighted_avg(recent_rated_walks, rating_avg)
    if rating_count:
        rating_weighted_avg = round((rating_recent_avg * 0.7) + (rating_avg * 0.3), 1)
    else:
        rating_weighted_avg = 0.0

    completion_rate = _safe_ratio(completed_count, total_walks, fallback=1.0)
    punctuality_rate = max(0.0, 1 - _safe_ratio(severe_delay_count, total_walks, fallback=0.0))
    no_show_reliability = max(0.0, 1 - _safe_ratio(no_show_count, total_walks, fallback=0.0))

    severe_delay_rate = _to_percentage(severe_delay_count, total_walks)
    no_show_rate = _to_percentage(no_show_count, total_walks)
    cancel_rate = _to_percentage(cancel_count, total_walks)
    recent_10_walks = sorted_walks[:PREMIUM_NO_SHOW_LOOKBACK_WALKS]
    recent_no_show_10 = len([walk for walk in recent_10_walks if walk.get("status") == STATUS_NO_SHOW_WALKER])
    recent_severe_delay_10 = len([walk for walk in recent_10_walks if _derive_occurrence_status(walk) == OCC_LATE_SEVERE])
    recent_cancel_10 = len([walk for walk in recent_10_walks if walk.get("status") == STATUS_CANCELED])

    rating_normalized = max(0.0, min(1.0, rating_weighted_avg / 5.0))
    tip_score = round((tip_total_amount / completed_count), 2) if completed_count > 0 else 0.0
    tip_score_normalized = max(0.0, min(1.0, tip_score / 5.0))

    if rating_count < MIN_PUBLIC_RATING_COUNT:
        score_base = 75.0
    else:
        score_base = _base_score_from_components(
            rating_normalized,
            completion_rate,
            punctuality_rate,
            no_show_reliability,
        )

    recent_7_cutoff = now_dt - timedelta(days=RECENCY_SHORT_DAYS)
    recent_30_cutoff = now_dt - timedelta(days=RECENCY_MEDIUM_DAYS)
    walks_7 = [walk for walk in sorted_walks if (_walk_datetime_or_none(walk) or datetime.min.replace(tzinfo=timezone.utc)) >= recent_7_cutoff]
    walks_30 = [walk for walk in sorted_walks if (_walk_datetime_or_none(walk) or datetime.min.replace(tzinfo=timezone.utc)) >= recent_30_cutoff]

    def subset_score(rows: List[dict], default_value: float) -> float:
        if not rows:
            return default_value

        subset_completed = len([row for row in rows if row.get("status") == STATUS_FINISHED])
        subset_severe_delay = len([row for row in rows if _derive_occurrence_status(row) == OCC_LATE_SEVERE])
        subset_no_show = len([row for row in rows if row.get("status") == STATUS_NO_SHOW_WALKER])
        subset_rated_rows = [row for row in rows if isinstance(row.get("rating"), int) and int(row.get("rating", 0)) > 0]
        subset_rating_count = len(subset_rated_rows)
        subset_rating_avg = _weighted_avg(subset_rated_rows, rating_weighted_avg)

        subset_completion = _safe_ratio(subset_completed, len(rows), fallback=completion_rate)
        subset_punctuality = max(0.0, 1 - _safe_ratio(subset_severe_delay, len(rows), fallback=1.0))
        subset_no_show_rel = max(0.0, 1 - _safe_ratio(subset_no_show, len(rows), fallback=1.0))
        subset_rating_norm = max(0.0, min(1.0, subset_rating_avg / 5.0))

        if subset_rating_count < MIN_PUBLIC_RATING_COUNT:
            return 75.0
        return _base_score_from_components(subset_rating_norm, subset_completion, subset_punctuality, subset_no_show_rel)

    score_last_7 = subset_score(walks_7, score_base)
    score_last_30 = subset_score(walks_30, score_base)
    score_reference = score_base
    recency_blended = (score_last_7 * 0.5) + (score_last_30 * 0.3) + (score_reference * 0.2)

    recency_factor = 1.0
    if rating_count >= MIN_PUBLIC_RATING_COUNT and score_reference > 0:
        recency_factor = recency_blended / score_reference
    recency_factor = round(max(0.75, min(1.25, recency_factor)), 4)

    std_dev = _rating_std_deviation(recent_rated_values or rating_values)
    if rating_count == 0:
        consistency_factor = 1.0
    else:
        consistency_factor = round(max(0.85, min(1.08, 1.08 - (std_dev * 0.12))), 4)

    no_show_recent_7 = len([walk for walk in walks_7 if walk.get("status") == STATUS_NO_SHOW_WALKER])
    no_show_recent_30 = len([walk for walk in walks_30 if walk.get("status") == STATUS_NO_SHOW_WALKER])
    severe_delay_recent_7 = len([walk for walk in walks_7 if _derive_occurrence_status(walk) == OCC_LATE_SEVERE])
    severe_delay_recent_30 = len([walk for walk in walks_30 if _derive_occurrence_status(walk) == OCC_LATE_SEVERE])

    penalty_points = 0.0
    penalty_points += no_show_recent_7 * 0.12
    penalty_points += max(0, no_show_recent_30 - no_show_recent_7) * 0.05
    penalty_points += severe_delay_recent_7 * 0.04
    penalty_points += max(0, severe_delay_recent_30 - severe_delay_recent_7) * 0.02
    penalty_points += max(0.0, 0.85 - completion_rate) * 0.5
    if severe_delay_rate > 20:
        penalty_points += 0.05
    severe_penalty_factor = round(max(0.35, 1.0 - min(0.65, penalty_points)), 4)

    recent_low_ratings = len([value for value in recent_rated_values if value <= 2])
    recent_bad_ratings = len([value for value in recent_rated_values if value <= 3])
    recent_complaint_ratings = len([value for value in recent_rated_values if value <= 2])
    recent_fraud_count = len([walk for walk in recent_10_walks if bool(walk.get("suspected_disintermediation", False))])
    status_penalty_factor = _status_penalty_factor(quality_status, rating_weighted_avg, severe_delay_rate)

    tip_metrics = _compute_tip_signal_metrics(
        walker_walks=sorted_walks,
        tip_rows=tip_rows or [],
        rating_weighted_avg=rating_weighted_avg,
        severe_delay_rate=severe_delay_rate,
        no_show_recent_7=no_show_recent_7,
        status_penalty_factor=status_penalty_factor,
        walker_controls=walker_controls,
        platform_tip_average=platform_tip_average,
    )

    if rating_count < MIN_PUBLIC_RATING_COUNT:
        score_operational_final = 75.0
        score_final = 75.0
        recency_factor = 1.0
        consistency_factor = 1.0
        severe_penalty_factor = 1.0
        status_penalty_factor = 1.0
        tip_impact_points = 0.0
        tip_impact_cap_points = 0.0
        tip_impact_enabled = False
    else:
        score_operational_final = score_base * recency_factor * consistency_factor * severe_penalty_factor * status_penalty_factor
        score_operational_final = _clamp_score(score_operational_final)

        tip_impact_cap_points = min(TIP_SCORE_MAX_POINTS, score_operational_final * TIP_SCORE_MAX_SHARE)
        tip_impact_candidate = min(tip_impact_cap_points, _coerce_float(tip_metrics.get("tip_boost_candidate"), 0.0))
        tip_impact_enabled = (
            not bool(tip_metrics.get("tip_suspicious_flag", False))
            and str(tip_metrics.get("tip_score_impact_mode") or "normal") != "blocked_until_review"
            and no_show_recent_7 == 0
            and severe_delay_recent_7 == 0
            and severe_penalty_factor >= 0.9
            and status_penalty_factor >= 1.0
        )
        tip_impact_points = round(tip_impact_candidate, 4) if tip_impact_enabled else 0.0
        score_final = _clamp_score(score_operational_final + tip_impact_points)

    recent_comments = [
        str(walk.get("comment", "")).strip()
        for walk in rated_walks[:RECENT_RATING_WINDOW]
        if str(walk.get("comment", "")).strip()
    ]

    return {
        "total_walks": total_walks,
        "accepted_walks": total_walks,
        "completed_walks": completed_count,
        "rating_avg": rating_avg,
        "rating_count": rating_count,
        "rating_recent_avg": rating_recent_avg,
        "rating_weighted_avg": rating_weighted_avg,
        "rating_loyalty_weight_enabled": True,
        "completion_rate": round(completion_rate, 4),
        "completion_percent": round(completion_rate * 100, 2),
        "punctuality_rate": round(punctuality_rate, 4),
        "punctuality_percent": round(punctuality_rate * 100, 2),
        "rating_normalized": round(rating_normalized, 4),
        "no_show_reliability": round(no_show_reliability, 4),
        "severe_delay_rate": severe_delay_rate,
        "no_show_rate": no_show_rate,
        "cancel_rate": cancel_rate,
        "score_base": score_base,
        "score_last_7": score_last_7,
        "score_last_30": score_last_30,
        "score_reference": score_reference,
        "recency_factor": recency_factor,
        "consistency_factor": consistency_factor,
        "severe_penalty_factor": severe_penalty_factor,
        "status_penalty_factor": status_penalty_factor,
        "score_operational_final": score_operational_final,
        "score_final": score_final,
        "score_badge": _score_badge_label(score_final),
        "tip_total_amount": round(max(0.0, tip_total_amount), 2),
        "tip_score": tip_score,
        "tip_score_normalized": round(tip_score_normalized, 4),
        "tip_recent_window_total": _coerce_float(tip_metrics.get("tip_recent_window_total"), 0.0),
        "tip_recent_window_count": int(tip_metrics.get("tip_recent_window_count", 0) or 0),
        "tip_weighted_ratio": _coerce_float(tip_metrics.get("tip_weighted_ratio"), 0.0),
        "tip_platform_avg_comparison": _coerce_float(tip_metrics.get("tip_platform_avg_comparison"), 0.0),
        "tip_suspicious_flag": bool(tip_metrics.get("tip_suspicious_flag", False)),
        "tip_suspicious_reasons": list(tip_metrics.get("tip_suspicious_reasons") or []),
        "tip_origin_top_clients": list(tip_metrics.get("tip_origin_top_clients") or []),
        "tip_score_impact_mode": str(tip_metrics.get("tip_score_impact_mode") or "normal"),
        "tip_score_impact_cap_points": round(tip_impact_cap_points, 4),
        "tip_score_impact_points": round(tip_impact_points, 4),
        "tip_score_impact_enabled": bool(tip_impact_enabled),
        "tip_suspicious_tip_ids": list(tip_metrics.get("tip_suspicious_tip_ids") or []),
        "public_rating_label": _public_rating_label(rating_avg, rating_count),
        "public_badge": _public_badge_label(rating_avg, rating_count),
        "recent_comments": recent_comments[:5],
        "recent_low_ratings": recent_low_ratings,
        "recent_no_show_7d": no_show_recent_7,
        "recent_no_show_30d": no_show_recent_30,
        "recent_severe_delay_7d": severe_delay_recent_7,
        "recent_severe_delay_30d": severe_delay_recent_30,
        "recent_no_show_10": recent_no_show_10,
        "recent_severe_delay_10": recent_severe_delay_10,
        "recent_cancel_10": recent_cancel_10,
        "recent_severe_delay_count": len([walk for walk in walks_30 if _derive_occurrence_status(walk) == OCC_LATE_SEVERE]),
        "recent_bad_ratings": recent_bad_ratings,
        "recent_complaint_ratings": recent_complaint_ratings,
        "recent_fraud_count": recent_fraud_count,
        "rating_std_dev": std_dev,
        "public_rating_visible": rating_count >= MIN_CLIENT_RATING_DISPLAY_COUNT,
    }


def _quality_status_from_reputation(metrics: dict) -> tuple[str, str]:
    rating_avg = _coerce_float(metrics.get("rating_avg"), 0.0)
    rating_recent = _coerce_float(metrics.get("rating_recent_avg"), rating_avg)
    rating_weighted = _coerce_float(metrics.get("rating_weighted_avg"), rating_recent)
    severe_delay_rate = _coerce_float(metrics.get("severe_delay_rate"), 0.0)
    no_show_rate = _coerce_float(metrics.get("no_show_rate"), 0.0)
    recent_no_show_10 = int(metrics.get("recent_no_show_10", 0) or 0)
    recent_severe_delay_10 = int(metrics.get("recent_severe_delay_10", 0) or 0)
    recent_severe_delay_count = int(metrics.get("recent_severe_delay_count", 0) or 0)
    recent_bad_ratings = int(metrics.get("recent_bad_ratings", 0) or 0)
    recent_complaint_ratings = int(metrics.get("recent_complaint_ratings", 0) or 0)
    recent_fraud_count = int(metrics.get("recent_fraud_count", 0) or 0)
    rating_count = int(metrics.get("rating_count", 0) or 0)
    has_rating_basis = rating_count >= MIN_PUBLIC_RATING_COUNT

    if recent_fraud_count >= 1:
        return QUALITY_STATUS_SUSPENDED, "Evento grave de fraude/comportamento suspeito detectado"

    if recent_severe_delay_count >= MONITORING_SEVERE_DELAY_RECURRENCE_THRESHOLD:
        return QUALITY_STATUS_SUSPENDED, "Reincidência grave de atrasos recentes"

    if recent_no_show_10 >= 2:
        return QUALITY_STATUS_SUSPENDED, "Reincidência grave de não comparecimento"

    if has_rating_basis and rating_weighted < 3.5:
        return QUALITY_STATUS_SUSPENDED, "Nota operacional crítica abaixo de 3,5"

    if recent_no_show_10 >= 1 or no_show_rate > 0:
        return QUALITY_STATUS_RESTRICTED, "Não comparecimento recente relevante"

    if not has_rating_basis:
        if recent_fraud_count >= 1:
            return QUALITY_STATUS_SUSPENDED, "Evento grave detectado durante período inicial"
        if rating_weighted < 4.2 or recent_bad_ratings >= 2 or recent_severe_delay_10 >= 1 or severe_delay_rate >= 20:
            return QUALITY_STATUS_OBSERVATION, "Observação leve até consolidar 5 avaliações"
        return QUALITY_STATUS_ACTIVE, "Status ativo em fase inicial de avaliações"

    if rating_weighted < 3.8 or recent_complaint_ratings >= 2:
        return QUALITY_STATUS_RESTRICTED, "Queda de qualidade operacional exige recuperação"

    if 3.8 <= rating_weighted <= 4.19 or recent_bad_ratings >= 2 or recent_severe_delay_10 >= 1 or severe_delay_rate >= 20:
        return QUALITY_STATUS_OBSERVATION, "Você está em observação por pontualidade"

    if rating_weighted >= 4.7 and rating_avg >= 4.7 and rating_recent >= 4.6 and recent_no_show_10 == 0 and recent_severe_delay_10 == 0:
        return QUALITY_STATUS_PREMIUM, "Alta qualidade recente e histórica"

    if rating_weighted >= 4.3 and rating_avg >= 4.3:
        return QUALITY_STATUS_ACTIVE, "Desempenho estável e confiável"

    return QUALITY_STATUS_ACTIVE, "Status ativo com monitoramento contínuo"


def _walker_incentive_message(quality_status: str, metrics: dict) -> str:
    score_final = _coerce_float(metrics.get("score_operational_final", metrics.get("score_final", 0.0)), 0.0)
    rating_recent = _coerce_float(metrics.get("rating_recent_avg"), 0.0)
    severe_delay_rate = _coerce_float(metrics.get("severe_delay_rate"), 0.0)

    if quality_status == QUALITY_STATUS_PREMIUM:
        return f"Sua nota atual é {rating_recent:.1f}. Continue assim para receber mais confiança dos clientes."
    if quality_status == QUALITY_STATUS_OBSERVATION:
        return "Você está em observação por pontualidade."
    if quality_status in {QUALITY_STATUS_RESTRICTED, QUALITY_STATUS_SUSPENDED}:
        return "Complete o plano de recuperação para voltar a receber prioridade em novos passeios."
    if score_final >= 80:
        return "Excelente ritmo! Mantenha constância para entrar no destaque máximo."
    if severe_delay_rate > 10:
        return "Reduzir atrasos graves eleva sua visibilidade para novos clientes."
    return "Mantenha qualidade e pontualidade para subir no ranking de confiança."


def _monitoring_severity_from_metrics(quality_status: str, metrics: dict) -> str:
    recent_no_show_10 = int(metrics.get("recent_no_show_10", 0) or 0)
    recent_severe_delay_count = int(metrics.get("recent_severe_delay_count", 0) or 0)
    severe_delay_rate = _coerce_float(metrics.get("severe_delay_rate"), 0.0)

    if quality_status == QUALITY_STATUS_SUSPENDED or recent_no_show_10 >= 1 or recent_severe_delay_count >= MONITORING_SEVERE_DELAY_RECURRENCE_THRESHOLD:
        return "grave"
    if quality_status == QUALITY_STATUS_RESTRICTED and recent_no_show_10 == 0 and recent_severe_delay_count == 0 and severe_delay_rate < 15:
        return "leve"
    return "padrao"


def _tip_repetition_weight(position: int) -> float:
    if position <= 1:
        return 1.0
    if position == 2:
        return 0.7
    return 0.4


async def _list_paid_tips_for_walker(
    *,
    walker_user_id: Optional[str],
    walker_id: str,
    walker_name: str = "",
    limit: int = 2000,
) -> List[dict]:
    clauses: List[dict] = []
    if walker_user_id:
        clauses.append({"walker_user_id": walker_user_id})
    if walker_id:
        clauses.append({"walker_id": walker_id})
    if walker_name:
        clauses.append({"walker_name": walker_name})

    if not clauses:
        return []

    query: Dict[str, Any] = {"status": "paid", "$or": clauses}
    tips = await db.tips.find(query, {"_id": 0}).sort("paid_at", -1).to_list(max(10, limit))
    return tips


async def _sum_paid_tips_for_walker(*, walker_user_id: Optional[str], walker_id: str, walker_name: str = "") -> float:
    tips = await _list_paid_tips_for_walker(
        walker_user_id=walker_user_id,
        walker_id=walker_id,
        walker_name=walker_name,
        limit=2000,
    )
    return round(sum(_coerce_float(item.get("amount"), 0.0) for item in tips), 2)


async def _platform_tip_average_recent(limit: int = 1000) -> float:
    rows = await db.tips.find({"status": "paid"}, {"_id": 0, "amount": 1}).sort("paid_at", -1).to_list(max(10, limit))
    if not rows:
        return 0.0
    total = sum(_coerce_float(row.get("amount"), 0.0) for row in rows)
    return round(total / max(1, len(rows)), 2)


def _compute_tip_signal_metrics(
    *,
    walker_walks: List[dict],
    tip_rows: List[dict],
    rating_weighted_avg: float,
    severe_delay_rate: float,
    no_show_recent_7: int,
    status_penalty_factor: float,
    walker_controls: Optional[dict],
    platform_tip_average: float,
) -> dict:
    completed_recent = [walk for walk in walker_walks if walk.get("status") == STATUS_FINISHED]
    completed_recent = sorted(completed_recent, key=lambda row: row.get("walk_datetime_iso", ""), reverse=True)[:TIP_SCORE_RECENT_WALKS_WINDOW]
    walk_map = {str(walk.get("id") or ""): walk for walk in completed_recent if str(walk.get("id") or "")}
    if not walk_map:
        return {
            "tip_recent_window_total": 0.0,
            "tip_recent_window_count": 0,
            "tip_weighted_ratio": 0.0,
            "tip_boost_candidate": 0.0,
            "tip_suspicious_flag": False,
            "tip_suspicious_reasons": [],
            "tip_origin_top_clients": [],
            "tip_platform_avg_comparison": 0.0,
            "tip_score_impact_mode": str((walker_controls or {}).get("tip_score_impact_mode") or "normal"),
            "tip_suspicious_tip_ids": [],
        }

    tips_by_walk = [
        tip for tip in tip_rows
        if str(tip.get("status") or "") == "paid" and str(tip.get("walk_id") or "") in walk_map
    ]
    tips_by_walk = sorted(
        tips_by_walk,
        key=lambda row: row.get("paid_at") or row.get("updated_at") or row.get("created_at") or "",
        reverse=True,
    )

    if not tips_by_walk:
        return {
            "tip_recent_window_total": 0.0,
            "tip_recent_window_count": 0,
            "tip_weighted_ratio": 0.0,
            "tip_boost_candidate": 0.0,
            "tip_suspicious_flag": False,
            "tip_suspicious_reasons": [],
            "tip_origin_top_clients": [],
            "tip_platform_avg_comparison": 0.0,
            "tip_score_impact_mode": str((walker_controls or {}).get("tip_score_impact_mode") or "normal"),
            "tip_suspicious_tip_ids": [],
        }

    client_seen_counts: Dict[str, int] = {}
    client_tip_stats: Dict[str, Dict[str, Any]] = {}
    weighted_ratio_sum = 0.0
    weighted_factor_sum = 0.0
    tip_total = 0.0
    tip_count = 0

    for tip in tips_by_walk:
        walk_id = str(tip.get("walk_id") or "")
        walk_row = walk_map.get(walk_id)
        if not walk_row:
            continue

        client_id = str(tip.get("client_user_id") or "")
        client_name = str(tip.get("client_name") or "Cliente")
        amount = _coerce_float(tip.get("amount"), 0.0)
        if amount < 1.0:
            continue

        tip_count += 1
        tip_total += amount

        prior = client_seen_counts.get(client_id, 0)
        position = prior + 1
        repetition_weight = _tip_repetition_weight(position)
        client_seen_counts[client_id] = position

        max_tip_for_walk = max(1.0, _tip_max_allowed_for_walk(walk_row))
        ratio = max(0.0, min(1.0, amount / max_tip_for_walk))
        weighted_ratio_sum += ratio * repetition_weight
        weighted_factor_sum += repetition_weight

        stat = client_tip_stats.get(client_id) or {
            "client_id": client_id,
            "client_name": client_name,
            "count": 0,
            "total": 0.0,
            "high_tip_count": 0,
            "walk_count": 0,
        }
        stat["count"] += 1
        stat["total"] = _coerce_float(stat.get("total"), 0.0) + amount
        if amount >= TIP_SUSPICIOUS_HIGH_AMOUNT:
            stat["high_tip_count"] = int(stat.get("high_tip_count", 0) or 0) + 1
        stat["walk_count"] += 1
        client_tip_stats[client_id] = stat

    tip_avg = round((tip_total / tip_count), 2) if tip_count else 0.0
    weighted_ratio = max(0.0, min(1.0, weighted_ratio_sum / weighted_factor_sum)) if weighted_factor_sum > 0 else 0.0
    tip_boost_candidate = round(weighted_ratio * TIP_SCORE_MAX_POINTS, 4)

    suspicious_reasons: List[str] = []
    suspicious_tip_ids: List[str] = []
    for client_stat in client_tip_stats.values():
        client_count = int(client_stat.get("count", 0) or 0)
        client_total = _coerce_float(client_stat.get("total"), 0.0)
        client_avg = round(client_total / max(1, client_count), 2)
        if client_count >= TIP_SUSPICIOUS_REPEAT_THRESHOLD and client_avg >= TIP_SUSPICIOUS_HIGH_AMOUNT:
            suspicious_reasons.append("Gorjetas altas repetidas do mesmo cliente")
            suspicious_tip_ids.extend(
                [
                    str(tip.get("id") or "")
                    for tip in tips_by_walk
                    if str(tip.get("client_user_id") or "") == str(client_stat.get("client_id") or "")
                    and _coerce_float(tip.get("amount"), 0.0) >= TIP_SUSPICIOUS_HIGH_AMOUNT
                ]
            )
        if client_stat.get("walk_count", 0) <= 3 and int(client_stat.get("high_tip_count", 0) or 0) >= 2:
            suspicious_reasons.append("Cliente com poucas corridas e muitas gorjetas altas")

    platform_ratio = round((tip_avg / platform_tip_average), 2) if platform_tip_average > 0 else 0.0
    if tip_count >= 4 and platform_tip_average > 0 and tip_avg >= (platform_tip_average * TIP_SUSPICIOUS_PLATFORM_MULTIPLIER):
        suspicious_reasons.append("Média de gorjetas acima da média da plataforma")

    top_clients = sorted(
        [
            {
                "client_id": str(stat.get("client_id") or ""),
                "client_name": str(stat.get("client_name") or "Cliente"),
                "count": int(stat.get("count", 0) or 0),
                "total": round(_coerce_float(stat.get("total"), 0.0), 2),
                "average": round(_coerce_float(stat.get("total"), 0.0) / max(1, int(stat.get("count", 0) or 0)), 2),
            }
            for stat in client_tip_stats.values()
        ],
        key=lambda row: (_coerce_float(row.get("total"), 0.0), int(row.get("count", 0) or 0)),
        reverse=True,
    )

    if tip_total > 0 and top_clients:
        concentration_share = _coerce_float(top_clients[0].get("total"), 0.0) / tip_total
        if tip_count >= 4 and concentration_share >= TIP_SUSPICIOUS_CONCENTRATION_SHARE:
            suspicious_reasons.append("Padrão concentrado de gorjetas em poucos clientes")

    if tip_count >= 3 and tip_avg >= TIP_SUSPICIOUS_HIGH_AMOUNT and (
        rating_weighted_avg < 4.2 or severe_delay_rate >= 15 or no_show_recent_7 > 0 or status_penalty_factor < 1.0
    ):
        suspicious_reasons.append("Gorjetas altas com qualidade operacional inconsistente")

    suspicious_reasons = list(dict.fromkeys(suspicious_reasons))
    tip_suspicious_flag = len(suspicious_reasons) > 0

    mode = str((walker_controls or {}).get("tip_score_impact_mode") or "normal")
    if mode not in {"normal", "ignore_current", "ignore_recent_window", "blocked_until_review"}:
        mode = "normal"

    excluded_tip_ids: set[str] = set()
    if mode == "ignore_current" and tips_by_walk:
        excluded_tip_ids.add(str(tips_by_walk[0].get("id") or ""))
    elif mode == "ignore_recent_window":
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=TIP_SUSPICIOUS_RECENT_WINDOW_DAYS)
        for tip in tips_by_walk:
            tip_dt = _parse_iso_datetime(tip.get("paid_at") or tip.get("updated_at") or tip.get("created_at"))
            if tip_dt and tip_dt >= cutoff_dt and (
                bool(tip.get("suspicious_flag", False)) or _coerce_float(tip.get("amount"), 0.0) >= TIP_SUSPICIOUS_HIGH_AMOUNT
            ):
                excluded_tip_ids.add(str(tip.get("id") or ""))

    if tip_suspicious_flag:
        excluded_tip_ids.update(str(tip_id) for tip_id in suspicious_tip_ids if str(tip_id))

    if mode == "blocked_until_review":
        weighted_ratio = 0.0
        tip_boost_candidate = 0.0
    elif excluded_tip_ids:
        adjusted_ratio_sum = 0.0
        adjusted_weight_sum = 0.0
        local_seen_counts: Dict[str, int] = {}
        for tip in tips_by_walk:
            tip_id = str(tip.get("id") or "")
            if tip_id in excluded_tip_ids:
                continue
            walk_row = walk_map.get(str(tip.get("walk_id") or ""))
            if not walk_row:
                continue
            amount = _coerce_float(tip.get("amount"), 0.0)
            if amount < 1.0:
                continue
            client_id = str(tip.get("client_user_id") or "")
            next_position = local_seen_counts.get(client_id, 0) + 1
            local_seen_counts[client_id] = next_position
            weight = _tip_repetition_weight(next_position)
            ratio = max(0.0, min(1.0, amount / max(1.0, _tip_max_allowed_for_walk(walk_row))))
            adjusted_ratio_sum += ratio * weight
            adjusted_weight_sum += weight
        weighted_ratio = max(0.0, min(1.0, adjusted_ratio_sum / adjusted_weight_sum)) if adjusted_weight_sum > 0 else 0.0
        tip_boost_candidate = round(weighted_ratio * TIP_SCORE_MAX_POINTS, 4)

    return {
        "tip_recent_window_total": round(tip_total, 2),
        "tip_recent_window_count": tip_count,
        "tip_weighted_ratio": round(weighted_ratio, 4),
        "tip_boost_candidate": round(tip_boost_candidate, 4),
        "tip_suspicious_flag": tip_suspicious_flag,
        "tip_suspicious_reasons": suspicious_reasons,
        "tip_origin_top_clients": top_clients[:5],
        "tip_platform_avg_comparison": platform_ratio,
        "tip_score_impact_mode": mode,
        "tip_suspicious_tip_ids": list(dict.fromkeys([str(x) for x in suspicious_tip_ids if str(x)])),
    }


def _tip_deadline_for_walk(walk: dict) -> datetime:
    reference_dt = (
        _parse_iso_datetime(walk.get("decision_resolved_at"))
        or _parse_iso_datetime(walk.get("updated_at"))
        or _parse_iso_datetime(walk.get("walk_datetime_iso"))
        or datetime.now(timezone.utc)
    )
    return reference_dt + timedelta(hours=24)


def _tip_max_allowed_for_walk(walk: dict) -> float:
    walk_amount = _base_amount_from_walk(walk)
    max_allowed = min(50.0, walk_amount * 1.5)
    return round(max(1.0, max_allowed), 2)



def _resolve_tip_amount(payload: TipCheckoutRequest, max_allowed: float) -> float:
    amount: Optional[float] = None
    if payload.quick_amount is not None:
        amount = float(payload.quick_amount)
    elif payload.custom_amount is not None:
        amount = float(payload.custom_amount)

    if amount is None:
        raise HTTPException(status_code=422, detail="Informe um valor de gorjeta")
    if amount < 1:
        raise HTTPException(status_code=400, detail="A gorjeta mínima é R$ 1,00")
    if amount > max_allowed:
        raise HTTPException(status_code=400, detail=f"Valor máximo permitido para este passeio é R$ {max_allowed:.2f}")
    return round(amount, 2)



def _build_stripe_checkout_client(request: Request):
    raise HTTPException(status_code=501, detail="Checkout externo temporariamente desativado no ambiente local")


def _can_be_featured(
    *,
    rating_avg: float,
    rating_recent_avg: float,
    rating_count: int,
    recent_severe_delay_10: int,
    no_show_in_last_10: bool,
) -> bool:
    return (
        rating_avg >= 4.7
        and rating_recent_avg >= 4.6
        and rating_count >= 10
        and recent_severe_delay_10 == 0
        and not no_show_in_last_10
    )


def _normalize_quality_monitoring(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "active": bool(raw.get("active", False)),
        "severity": str(raw.get("severity", "padrao") or "padrao"),
        "target_walks": int(raw.get("target_walks", 0) or 0),
        "completed_walks": int(raw.get("completed_walks", 0) or 0),
        "reset_count": int(raw.get("reset_count", 0) or 0),
        "severe_delay_incidents": int(raw.get("severe_delay_incidents", 0) or 0),
        "course_completed": bool(raw.get("course_completed", False)),
        "quiz_passed": bool(raw.get("quiz_passed", False)),
        "quiz_attempts": int(raw.get("quiz_attempts", 0) or 0),
        "consecutive_quiz_failures": int(raw.get("consecutive_quiz_failures", 0) or 0),
        "review_recommended": bool(raw.get("review_recommended", False)),
    }


def _quality_target_from_severity(severity: str) -> int:
    if severity == "leve":
        return 5
    if severity == "grave":
        return 10
    return 7


async def _recalculate_walker_quality(
    walker_user_id: str,
    *,
    trigger_event: str,
    current_walk: Optional[dict] = None,
):
    walker = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if not walker:
        return

    all_walks = await db.walks.find({}, {"_id": 0}).to_list(5000)
    walker_walks = [walk for walk in all_walks if _walk_matches_walker_user(walk, walker)]
    tip_rows = await _list_paid_tips_for_walker(
        walker_user_id=str(walker.get("id") or ""),
        walker_id=f"partner-{str(walker.get('id') or '').strip()}" if walker.get("id") else "",
        walker_name=str(walker.get("full_name") or ""),
        limit=1000,
    )
    tip_total_amount = round(sum(_coerce_float(row.get("amount"), 0.0) for row in tip_rows), 2)
    platform_tip_average = await _platform_tip_average_recent()

    baseline_metrics = _compute_reputation_metrics(
        walker_walks,
        walker.get("quality_status", QUALITY_STATUS_ACTIVE),
        tip_total_amount,
        tip_rows,
        walker,
        platform_tip_average,
    )
    quality_status, reason = _quality_status_from_reputation(baseline_metrics)
    metrics = _compute_reputation_metrics(
        walker_walks,
        quality_status,
        tip_total_amount,
        tip_rows,
        walker,
        platform_tip_average,
    )

    now_dt = datetime.now(timezone.utc)
    week_start, week_end = _week_bounds(now_dt)

    monitoring = _normalize_quality_monitoring(walker.get("quality_monitoring"))
    recovery_required = quality_status in {QUALITY_STATUS_RESTRICTED, QUALITY_STATUS_SUSPENDED}
    if not recovery_required and monitoring.get("active"):
        monitoring.update(
            {
                "active": False,
                "severity": "padrao",
                "target_walks": 0,
                "completed_walks": 0,
                "reset_count": 0,
                "severe_delay_incidents": 0,
                "course_completed": False,
                "quiz_passed": False,
                "quiz_attempts": 0,
                "consecutive_quiz_failures": 0,
                "review_recommended": False,
            }
        )

    if recovery_required and not monitoring["active"]:
        severity = _monitoring_severity_from_metrics(quality_status, metrics)
        monitoring.update(
            {
                "active": True,
                "severity": severity,
                "target_walks": _quality_target_from_severity(severity),
                "completed_walks": 0,
                "reset_count": 0,
                "severe_delay_incidents": 0,
                "course_completed": False,
                "quiz_passed": False,
                "quiz_attempts": 0,
                "consecutive_quiz_failures": 0,
                "review_recommended": False,
            }
        )

    if monitoring["active"] and trigger_event in {"walk_finished", "attendance_event"} and current_walk:
        current_status = current_walk.get("status")
        current_occ = _derive_occurrence_status(current_walk)
        reset_triggered = False
        if (
            current_status == STATUS_FINISHED
            and current_occ != OCC_LATE_SEVERE
            and monitoring.get("course_completed")
            and monitoring.get("quiz_passed")
        ):
            monitoring["completed_walks"] += 1

        if current_status == STATUS_NO_SHOW_WALKER:
            monitoring["completed_walks"] = 0
            monitoring["reset_count"] += 1
            monitoring["severe_delay_incidents"] = 0
            reset_triggered = True
        elif current_occ == OCC_LATE_SEVERE:
            monitoring["severe_delay_incidents"] += 1
            if monitoring["severe_delay_incidents"] >= MONITORING_SEVERE_DELAY_RECURRENCE_THRESHOLD:
                monitoring["completed_walks"] = 0
                monitoring["reset_count"] += 1
                monitoring["severe_delay_incidents"] = 0
                reset_triggered = True

        if (
            not reset_triggered
            and int(metrics.get("rating_count", 0) or 0) >= MIN_PUBLIC_RATING_COUNT
            and _coerce_float(metrics.get("rating_weighted_avg"), 0.0) < 4.0
        ):
            monitoring["completed_walks"] = 0
            monitoring["reset_count"] += 1

        if monitoring["completed_walks"] >= monitoring["target_walks"] and monitoring.get("course_completed") and monitoring.get("quiz_passed"):
            recalculated_metrics = _compute_reputation_metrics(
                walker_walks,
                QUALITY_STATUS_ACTIVE,
                tip_total_amount,
                tip_rows,
                walker,
                platform_tip_average,
            )
            recalculated_status, recalculated_reason = _quality_status_from_reputation(recalculated_metrics)
            quality_status = recalculated_status
            if quality_status in {QUALITY_STATUS_RESTRICTED, QUALITY_STATUS_SUSPENDED}:
                severity = _monitoring_severity_from_metrics(quality_status, recalculated_metrics)
                monitoring["active"] = True
                monitoring["severity"] = severity
                monitoring["target_walks"] = _quality_target_from_severity(severity)
                monitoring["completed_walks"] = 0
                monitoring["severe_delay_incidents"] = 0
                reason = f"Recuperação ainda insuficiente: {recalculated_reason}"
            else:
                reason = f"Recuperação concluída: {recalculated_reason}"
                monitoring["active"] = False
                monitoring["severe_delay_incidents"] = 0

    await _process_walker_incentive_events(
        walker_user=walker,
        walker_walks=walker_walks,
        metrics=metrics,
        current_walk=current_walk,
    )

    metrics = _compute_reputation_metrics(
        walker_walks,
        quality_status,
        tip_total_amount,
        tip_rows,
        walker,
        platform_tip_average,
    )
    incentive_message = _walker_incentive_message(quality_status, metrics)

    week_walks = [
        walk
        for walk in walker_walks
        if walk.get("status") == STATUS_FINISHED
        and (walk_dt := _walk_datetime_from_doc(walk))
        and week_start <= walk_dt < week_end
    ]
    week_tip_rows = [
        tip
        for tip in tip_rows
        if (tip_dt := _parse_iso_datetime(tip.get("paid_at") or tip.get("updated_at") or tip.get("created_at")))
        and week_start <= tip_dt < week_end
    ]
    week_tip_total = round(sum(_coerce_float(row.get("amount"), 0.0) for row in week_tip_rows), 2)
    mission_info = _weekly_mission_progress(week_walks, week_tip_rows)
    mission_bonus_points = _coerce_float(mission_info.get("mission_bonus_points"), 0.0)
    mission_bonus_active = bool(mission_info.get("completed_all", False))
    checklist_streak = int(walker.get("kit_checklist_streak", 0) or 0)
    infractions = int(walker.get("kit_missing_reports_count", 0) or 0)
    rating_avg = _coerce_float(metrics.get("rating_weighted_avg"), _coerce_float(metrics.get("rating_avg"), 0.0))
    cancel_rate = _coerce_float(metrics.get("cancel_rate"), 0.0)
    walker_level = _determine_walker_level(
        _coerce_float(metrics.get("score_final"), 0.0),
        int(metrics.get("completed_walks", 0) or 0),
        _coerce_float(metrics.get("no_show_rate"), 0.0),
        rating_avg=rating_avg,
        cancel_rate=cancel_rate,
        checklist_streak=checklist_streak,
        infractions=infractions,
    )
    next_level = _next_walker_level(walker_level)
    level_progress_percent = _walker_level_progress_percent(
        walker_level,
        _coerce_float(metrics.get("score_final"), 0.0),
        int(metrics.get("completed_walks", 0) or 0),
        _coerce_float(metrics.get("no_show_rate"), 0.0),
        rating_avg=rating_avg,
        cancel_rate=cancel_rate,
        checklist_streak=checklist_streak,
    )
    level_priority_bonus = _walker_level_boost_factor(walker_level)
    previous_walker_level = _normalize_walker_level_value(walker.get("walker_level"))
    weekly_tip_goal_reached = week_tip_total >= WEEKLY_TIP_GOAL_AMOUNT
    gamification_badges = _gamification_badges(metrics, week_tip_total)
    if mission_bonus_active:
        gamification_badges.append("Missão da semana")

    op_status_map = {
        QUALITY_STATUS_PREMIUM: WALKER_OP_STATUS_ACTIVE,
        QUALITY_STATUS_ACTIVE: WALKER_OP_STATUS_ACTIVE,
        QUALITY_STATUS_OBSERVATION: WALKER_OP_STATUS_OBSERVATION,
        QUALITY_STATUS_RESTRICTED: WALKER_OP_STATUS_RESTRICTED,
        QUALITY_STATUS_SUSPENDED: WALKER_OP_STATUS_SUSPENDED,
    }

    old_quality_status = walker.get("quality_status", QUALITY_STATUS_ACTIVE)
    quality_history = walker.get("quality_history", []) if isinstance(walker.get("quality_history"), list) else []
    if old_quality_status != quality_status:
        quality_history.append(
            {
                "id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "from": old_quality_status,
                "to": quality_status,
                "reason": reason,
                "event": trigger_event,
            }
        )

        await _create_notification(
            user_id=walker_user_id,
            role="passeador",
            title="Atualização de qualidade",
            message=f"Seu status agora é {quality_status.replace('_', ' ')}. Motivo: {reason}.",
            category="qualidade",
        )

    quality_score_history = walker.get("quality_score_history", []) if isinstance(walker.get("quality_score_history"), list) else []
    quality_score_history.append(
        {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "score_final": metrics.get("score_final", 0.0),
            "quality_status": quality_status,
            "event": trigger_event,
        }
    )

    has_premium_override = bool(walker.get("premium_override", False))

    updates = {
        "quality_status": quality_status,
        "quality_status_reason": reason,
        "quality_metrics": {
            "rating_avg": metrics.get("rating_avg", 0.0),
            "rating_recent_avg": metrics.get("rating_recent_avg", 0.0),
            "rating_weighted_avg": metrics.get("rating_weighted_avg", 0.0),
            "rating_count": metrics.get("rating_count", 0),
            "accepted_walks": metrics.get("accepted_walks", 0),
            "completed_walks": metrics.get("completed_walks", 0),
            "completion_rate": metrics.get("completion_percent", 0.0),
            "punctuality_rate": metrics.get("punctuality_percent", 0.0),
            "severe_delay_rate": metrics.get("severe_delay_rate", 0.0),
            "no_show_rate": metrics.get("no_show_rate", 0.0),
            "cancel_rate": metrics.get("cancel_rate", 0.0),
            "score_base": metrics.get("score_base", 0.0),
            "score_operational_final": metrics.get("score_operational_final", metrics.get("score_final", 0.0)),
            "score_final": metrics.get("score_final", 0.0),
            "score_trend": round(metrics.get("score_last_7", 0.0) - metrics.get("score_reference", 0.0), 2),
            "recency_factor": metrics.get("recency_factor", 1.0),
            "consistency_factor": metrics.get("consistency_factor", 1.0),
            "severe_penalty_factor": metrics.get("severe_penalty_factor", 1.0),
            "status_penalty_factor": metrics.get("status_penalty_factor", 1.0),
            "tip_total_amount": metrics.get("tip_total_amount", 0.0),
            "tip_score": metrics.get("tip_score", 0.0),
            "tip_score_normalized": metrics.get("tip_score_normalized", 0.0),
            "tip_recent_window_total": metrics.get("tip_recent_window_total", 0.0),
            "tip_recent_window_count": metrics.get("tip_recent_window_count", 0),
            "tip_weighted_ratio": metrics.get("tip_weighted_ratio", 0.0),
            "tip_platform_avg_comparison": metrics.get("tip_platform_avg_comparison", 0.0),
            "tip_suspicious_flag": metrics.get("tip_suspicious_flag", False),
            "tip_suspicious_reasons": metrics.get("tip_suspicious_reasons", []),
            "tip_origin_top_clients": metrics.get("tip_origin_top_clients", []),
            "tip_score_impact_mode": metrics.get("tip_score_impact_mode", "normal"),
            "tip_score_impact_cap_points": metrics.get("tip_score_impact_cap_points", 0.0),
            "tip_score_impact_points": metrics.get("tip_score_impact_points", 0.0),
            "tip_score_impact_enabled": metrics.get("tip_score_impact_enabled", False),
            "walker_level": walker_level,
            "level_score": _coerce_float(metrics.get("score_final"), 0.0),
            "checklist_streak": checklist_streak,
            "performance_metrics": {
                "acceptance_rate": _coerce_float(metrics.get("acceptance_percent"), 0.0),
                "cancel_rate": cancel_rate,
                "rating": rating_avg,
                "infractions": infractions,
            },
            "next_level": next_level,
            "level_progress_percent": level_progress_percent,
            "level_priority_bonus": level_priority_bonus,
            "mission_bonus_active": mission_bonus_active,
            "mission_bonus_points": mission_bonus_points,
            "weekly_tip_total": week_tip_total,
            "weekly_tip_goal": WEEKLY_TIP_GOAL_AMOUNT,
            "weekly_tip_goal_reached": weekly_tip_goal_reached,
            "gamification_badges": gamification_badges,
            "public_rating_label": metrics.get("public_rating_label", "Novo na plataforma"),
            "public_badge": metrics.get("public_badge", ""),
            "recent_comments": metrics.get("recent_comments", []),
            "encouragement_message": incentive_message,
        },
        "flag_suspeita_gorjeta": bool(metrics.get("tip_suspicious_flag", False)),
        "tip_suspicion_reasons": list(metrics.get("tip_suspicious_reasons") or []),
        "tip_suspicious_tip_ids": list(metrics.get("tip_suspicious_tip_ids") or []),
        "tip_score_impact_mode": str(metrics.get("tip_score_impact_mode") or walker.get("tip_score_impact_mode") or "normal"),
        "walker_level": walker_level,
        "level_score": _coerce_float(metrics.get("score_final"), 0.0),
        "checklist_streak": checklist_streak,
        "performance_metrics": {
            "acceptance_rate": _coerce_float(metrics.get("acceptance_percent"), 0.0),
            "cancel_rate": cancel_rate,
            "rating": rating_avg,
            "infractions": infractions,
        },
        "next_walker_level": next_level,
        "level_progress_percent": level_progress_percent,
        "level_priority_bonus": level_priority_bonus,
        "weekly_tip_total": week_tip_total,
        "weekly_tip_goal_reached": weekly_tip_goal_reached,
        "mission_bonus_active": mission_bonus_active,
        "mission_bonus_points": mission_bonus_points,
        "gamification_badges": gamification_badges,
        "quality_monitoring": monitoring,
        "quality_history": quality_history[-30:],
        "quality_score_history": quality_score_history[-REPUTATION_SCORE_HISTORY_LIMIT:],
        "walker_operational_status": op_status_map[quality_status],
        "is_premium_featured": has_premium_override or quality_status == QUALITY_STATUS_PREMIUM,
        "premium_override": has_premium_override,
        "isActive": quality_status != QUALITY_STATUS_SUSPENDED,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.update_one({"id": walker_user_id}, {"$set": updates})

    if previous_walker_level != walker_level:
        await _append_walker_event_log(
            walker_user_id,
            event_type="walker_level_changed",
            payload={
                "from": previous_walker_level,
                "to": walker_level,
                "trigger": trigger_event,
                "walk_id": str((current_walk or {}).get("id") or ""),
            },
        )
        await _create_notification(
            user_id=walker_user_id,
            role="passeador",
            title="Nível operacional atualizado",
            message=f"Seu nível mudou de {previous_walker_level.upper()} para {walker_level.upper()}.",
            category="qualidade",
        )

    alert_walker_snapshot = {
        **walker,
        "id": walker_user_id,
        "quality_metrics": updates.get("quality_metrics", {}),
        "quality_status": updates.get("quality_status", walker.get("quality_status")),
    }
    await _generate_walker_alerts(alert_walker_snapshot, metrics, walker_walks)
    if _is_disintermediation_flag_active(alert_walker_snapshot):
        await _generate_disintermediation_alert_for_user(
            alert_walker_snapshot,
            str(alert_walker_snapshot.get("desintermediacao_flag_reason") or "FLAG_ACTIVE"),
        )


async def _recalculate_quality_from_walk(walk: dict, trigger_event: str):
    walker_user_id = str(walk.get("walker_user_id") or "").strip()
    if not walker_user_id:
        walker_id = str(walk.get("walker_id") or "")
        if walker_id.startswith("partner-"):
            candidate_id = walker_id.replace("partner-", "", 1)
            candidate = await db.users.find_one({"id": candidate_id, "role": "passeador"}, {"_id": 0})
            if candidate:
                walker_user_id = candidate_id
    if not walker_user_id:
        walker_name = str(walk.get("walker_name") or "")
        if walker_name:
            candidate = await db.users.find_one({"full_name": walker_name, "role": "passeador"}, {"_id": 0})
            if candidate:
                walker_user_id = candidate.get("id", "")

    if walker_user_id:
        await _recalculate_walker_quality(walker_user_id, trigger_event=trigger_event, current_walk=walk)
        await _recalculate_walker_verification_for_user(
            walker_user_id=walker_user_id,
            trigger=f"score_update:{trigger_event}",
            walk_id=str(walk.get("id") or "") or None,
        )


@api_router.get("/admin/occurrences", response_model=List[AdminOccurrenceResponse])
async def list_admin_occurrences(
    request: Request,
    status: Optional[str] = None,
    query: Optional[str] = None,
    date: Optional[str] = None,
    region: Optional[str] = None,
    walker_id: Optional[str] = None,
):
    await _require_admin(request)
    rows = await db.walks.find({}, {"_id": 0}).sort("walk_datetime_iso", -1).to_list(1000)
    results: List[AdminOccurrenceResponse] = []
    normalized_query = str(query or "").strip().lower()
    normalized_region = str(region or "").strip().lower()
    normalized_walker_id = str(walker_id or "").strip()
    normalized_date = str(date or "").strip()

    for row in rows:
        refreshed = await _apply_attendance_decision_if_needed(row, trigger="admin_occurrence_read")
        occurrence_status = _derive_occurrence_status(refreshed)
        if status and occurrence_status != status:
            continue

        if normalized_date and str(refreshed.get("walk_date") or "") != normalized_date:
            continue

        if normalized_region and normalized_region not in str(refreshed.get("pickup_neighborhood") or "").lower():
            continue

        if normalized_walker_id:
            walker_user_id = str(refreshed.get("walker_user_id") or "")
            walker_identifier = str(refreshed.get("walker_id") or "")
            if normalized_walker_id not in {walker_user_id, walker_identifier}:
                continue

        if normalized_query:
            searchable = " ".join(
                [
                    str(refreshed.get("client_name") or ""),
                    str(refreshed.get("walker_name") or ""),
                    str(refreshed.get("pet_name") or ""),
                ]
            ).lower()
            if normalized_query not in searchable:
                continue

        refreshed["occurrence_status"] = occurrence_status
        results.append(_to_admin_occurrence_response(refreshed))

    return results


@api_router.post("/admin/occurrences/{walk_id}/action", response_model=AdminOccurrenceResponse)
async def execute_admin_occurrence_action(walk_id: str, payload: AdminOccurrenceActionPayload, request: Request):
    admin_user = await _require_admin(request)
    walk = await _get_walk_or_404(walk_id)

    before_values = {
        "occurrence_status": walk.get("occurrence_status") or _derive_occurrence_status(walk),
        "occurrence_resolved": bool(walk.get("occurrence_resolved", False)),
        "charged_amount": _coerce_float(walk.get("charged_amount"), 0.0),
        "walker_payout_amount": _coerce_float(walk.get("walker_payout_amount"), 0.0),
        "platform_retained_amount": _coerce_float(walk.get("platform_retained_amount"), 0.0),
        "client_refund_amount": _coerce_float(walk.get("client_refund_amount"), 0.0),
        "financial_status": str(walk.get("financial_status") or "pendente"),
        "suspected_disintermediation": bool(walk.get("suspected_disintermediation", False)),
    }

    updates: Dict[str, Any] = {}
    total_amount = _base_amount_from_walk(walk)
    current_occurrence_status = before_values["occurrence_status"]
    note_text = payload.note.strip()
    now_iso = datetime.now(timezone.utc).isoformat()

    if payload.action == "approve_auto_decision":
        updates["occurrence_status"] = OCC_RESOLVED
        updates["occurrence_resolved"] = True
    elif payload.action == "reverse_decision":
        updates["occurrence_status"] = OCC_PENDING_ANALYSIS_REOPENED
        updates["occurrence_resolved"] = False
        updates["status"] = STATUS_PENDING_REVIEW
    elif payload.action == "refund_client":
        refund_amount = round(float(payload.refund_amount if payload.refund_amount is not None else total_amount), 2)
        payout_amount = _coerce_float(walk.get("walker_payout_amount"), 0.0)
        retained_amount = max(0.0, round(total_amount - payout_amount - refund_amount, 2))
        updates.update(
            {
                "client_refund_amount": refund_amount,
                "platform_retained_amount": retained_amount,
                "occurrence_resolved": False,
                "occurrence_status": current_occurrence_status,
            }
        )
    elif payload.action == "release_walker_payment":
        payout_amount = round(float(payload.payout_amount if payload.payout_amount is not None else total_amount), 2)
        refund_amount = _coerce_float(walk.get("client_refund_amount"), 0.0)
        retained_amount = max(0.0, round(total_amount - payout_amount - refund_amount, 2))
        updates.update(
            {
                "walker_payout_amount": payout_amount,
                "platform_retained_amount": retained_amount,
                "occurrence_resolved": False,
                "occurrence_status": current_occurrence_status,
                "financial_status": "liberado_aguardando_repasse",
                "payment_released_at": now_iso,
                "payment_method": payload.payment_method.strip(),
            }
        )
    elif payload.action == "mark_payment_paid":
        updates.update(
            {
                "financial_status": "pago",
                "payment_paid_at": now_iso,
                "payment_method": payload.payment_method.strip() or str(walk.get("payment_method") or ""),
                "payment_transaction_id": payload.transaction_id.strip(),
            }
        )
    elif payload.action == "mark_payment_failed":
        updates.update(
            {
                "financial_status": "falha_no_repasse",
                "payment_failure_reason": payload.failure_reason.strip() or note_text,
            }
        )
    elif payload.action == "block_payment_analysis":
        updates.update(
            {
                "financial_status": "bloqueado_para_analise",
                "payment_block_reason": payload.block_reason.strip() or note_text,
            }
        )
    elif payload.action == "open_financial_dispute":
        updates["occurrence_status"] = OCC_DISPUTE_OPEN
        updates["occurrence_resolved"] = False
        updates["financial_status"] = "bloqueado_para_analise"
    elif payload.action == "resolve_financial_dispute":
        payout_amount = round(float(payload.payout_amount if payload.payout_amount is not None else _coerce_float(walk.get("walker_payout_amount"), 0.0)), 2)
        refund_amount = round(float(payload.refund_amount if payload.refund_amount is not None else _coerce_float(walk.get("client_refund_amount"), 0.0)), 2)
        retained_amount = round(float(payload.retained_amount if payload.retained_amount is not None else max(0.0, total_amount - payout_amount - refund_amount)), 2)
        updates.update(
            {
                "walker_payout_amount": payout_amount,
                "client_refund_amount": refund_amount,
                "platform_retained_amount": retained_amount,
                "occurrence_status": OCC_DISPUTE_RESOLVED,
                "occurrence_resolved": True,
                "financial_status": "liberado_aguardando_repasse",
            }
        )
    elif payload.action == "mark_disintermediation_suspect":
        updates["suspected_disintermediation"] = True
        updates["occurrence_status"] = OCC_SUSPECT_DISINTERMEDIATION
        updates["occurrence_resolved"] = False
    elif payload.action == "warn_walker":
        walker_user_id = str(walk.get("walker_user_id") or "")
        if walker_user_id:
            await _create_notification(
                user_id=walker_user_id,
                role="passeador",
                title="Advertência operacional",
                message=note_text or "Uma advertência foi registrada no seu histórico operacional.",
                category="qualidade",
            )
        updates["occurrence_status"] = current_occurrence_status
        updates["occurrence_resolved"] = False
    elif payload.action == "add_internal_note":
        current_note = str(walk.get("internal_note") or "").strip()
        appended_note = note_text
        updates["internal_note"] = f"{current_note}\n{appended_note}".strip() if current_note else appended_note
    elif payload.action == "mark_resolved":
        if len(note_text) < 15:
            raise HTTPException(status_code=400, detail="Observação interna obrigatória (mín. 15 caracteres)")
        updates["occurrence_status"] = OCC_RESOLVED
        updates["occurrence_resolved"] = True
        updates["internal_note"] = note_text
    elif payload.action == "mark_unresolved":
        updates["occurrence_status"] = OCC_UNRESOLVED
        updates["occurrence_resolved"] = False

    updates["updated_at"] = now_iso
    await db.walks.update_one({"id": walk_id}, {"$set": updates})

    updated_walk = await _get_walk_or_404(walk_id)
    log_entry = OccurrenceLogEntry(
        id=str(uuid.uuid4()),
        action=payload.action,
        performed_by_id=admin_user.get("id", ""),
        performed_by_name=admin_user.get("full_name", "Admin"),
        timestamp=now_iso,
        note=note_text,
        before_values=before_values,
        after_values={
            "occurrence_status": updated_walk.get("occurrence_status") or _derive_occurrence_status(updated_walk),
            "occurrence_resolved": bool(updated_walk.get("occurrence_resolved", False)),
            "charged_amount": _coerce_float(updated_walk.get("charged_amount"), 0.0),
            "walker_payout_amount": _coerce_float(updated_walk.get("walker_payout_amount"), 0.0),
            "platform_retained_amount": _coerce_float(updated_walk.get("platform_retained_amount"), 0.0),
            "client_refund_amount": _coerce_float(updated_walk.get("client_refund_amount"), 0.0),
            "financial_status": str(updated_walk.get("financial_status") or "pendente"),
            "suspected_disintermediation": bool(updated_walk.get("suspected_disintermediation", False)),
        },
    )
    await _append_occurrence_log(walk_id, log_entry.model_dump())
    final_walk = await _get_walk_or_404(walk_id)
    walker_user_id = str(final_walk.get("walker_user_id") or "").strip()
    if walker_user_id:
        await _recalculate_walker_verification_for_user(
            walker_user_id=walker_user_id,
            trigger=f"admin_occurrence:{payload.action}",
            walk_id=walk_id,
        )
        final_walk = await _get_walk_or_404(walk_id)
    return _to_admin_occurrence_response(final_walk)


@api_router.get("/admin/walkers/performance", response_model=List[AdminWalkerPerformanceResponse])
async def list_admin_walker_performance(request: Request):
    await _require_admin(request)
    walkers = await db.users.find({"role": "passeador"}, {"_id": 0}).to_list(500)
    all_walks = await db.walks.find({}, {"_id": 0}).to_list(5000)
    paid_tips = await db.tips.find({"status": "paid"}, {"_id": 0}).to_list(5000)

    response: List[AdminWalkerPerformanceResponse] = []
    for walker in walkers:
        await _recalculate_walker_quality(walker["id"], trigger_event="admin_scan")
        refreshed_walker = await db.users.find_one({"id": walker["id"], "role": "passeador"}, {"_id": 0})
        if refreshed_walker:
            walker = refreshed_walker

        walker_walks = [walk for walk in all_walks if _walk_matches_walker_user(walk, walker)]
        total = len(walker_walks)
        completed = [walk for walk in walker_walks if walk.get("status") == STATUS_FINISHED]
        severe_delays = [walk for walk in walker_walks if (_derive_occurrence_status(walk) == OCC_LATE_SEVERE)]
        no_shows = [walk for walk in walker_walks if walk.get("status") == STATUS_NO_SHOW_WALKER]
        canceled = [walk for walk in walker_walks if walk.get("status") == STATUS_CANCELED]
        rated = [walk for walk in walker_walks if isinstance(walk.get("rating"), int)]
        rating_count = len(rated)
        rating_avg = round(sum(int(walk.get("rating", 0)) for walk in rated) / rating_count, 2) if rating_count else 0.0

        latest_walks = sorted(
            walker_walks,
            key=lambda item: item.get("walk_datetime_iso", ""),
            reverse=True,
        )[:10]
        latest_ratings = [int(walk.get("rating", 0)) for walk in latest_walks if isinstance(walk.get("rating"), int) and int(walk.get("rating", 0)) > 0]
        rating_recent_avg = round(sum(latest_ratings) / len(latest_ratings), 2) if latest_ratings else rating_avg
        recent_severe_delay_10 = len([walk for walk in latest_walks if _derive_occurrence_status(walk) == OCC_LATE_SEVERE])
        no_show_in_last_10 = any(walk.get("status") == STATUS_NO_SHOW_WALKER for walk in latest_walks)
        severe_delay_rate = _to_percentage(len(severe_delays), total)

        can_feature = _can_be_featured(
            rating_avg=rating_avg,
            rating_recent_avg=rating_recent_avg,
            rating_count=rating_count,
            recent_severe_delay_10=recent_severe_delay_10,
            no_show_in_last_10=no_show_in_last_10,
        )
        is_featured = bool(walker.get("is_premium_featured", False))
        premium_override = bool(walker.get("premium_override", False))
        if is_featured and not premium_override and not can_feature:
            is_featured = False
            await db.users.update_one(
                {"id": walker.get("id")},
                {
                    "$set": {
                        "is_premium_featured": False,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )

        walker_tips = [
            tip
            for tip in paid_tips
            if str(tip.get("walker_user_id") or "") == walker.get("id")
            or str(tip.get("walker_id") or "") == f"partner-{walker.get('id')}"
        ]
        tip_total = round(sum(_coerce_float(item.get("amount"), 0.0) for item in walker_tips), 2)
        tip_average = round((tip_total / len(walker_tips)), 2) if walker_tips else 0.0
        tip_origin_map: Dict[str, Dict[str, Any]] = {}
        for tip in walker_tips:
            client_id = str(tip.get("client_user_id") or "")
            client_name = str(tip.get("client_name") or "Cliente")
            row = tip_origin_map.get(client_id) or {
                "client_id": client_id,
                "client_name": client_name,
                "count": 0,
                "total": 0.0,
                "average": 0.0,
            }
            row["count"] = int(row.get("count", 0) or 0) + 1
            row["total"] = _coerce_float(row.get("total"), 0.0) + _coerce_float(tip.get("amount"), 0.0)
            row["average"] = round(_coerce_float(row.get("total"), 0.0) / max(1, int(row.get("count", 0) or 0)), 2)
            tip_origin_map[client_id] = row
        tip_origin_top_clients = sorted(
            list(tip_origin_map.values()),
            key=lambda item: (_coerce_float(item.get("total"), 0.0), int(item.get("count", 0) or 0)),
            reverse=True,
        )[:5]

        quality_metrics = walker.get("quality_metrics") if isinstance(walker.get("quality_metrics"), dict) else {}
        score_final = _coerce_float(quality_metrics.get("score_final"), 0.0)
        alerts_count = len([alert for alert in (walker.get("walker_alerts") or []) if isinstance(alert, dict)])
        suspected_disintermediation = any(
            bool(walk.get("suspected_disintermediation", False)) for walk in walker_walks[-20:]
        )
        operational_status = walker.get("walker_operational_status", WALKER_OP_STATUS_ACTIVE)
        risk_flag = operational_status in {
            WALKER_OP_STATUS_OBSERVATION,
            WALKER_OP_STATUS_RESTRICTED,
            WALKER_OP_STATUS_SUSPENDED,
        } or severe_delay_rate >= 20 or _to_percentage(len(no_shows), total) >= 10 or suspected_disintermediation

        response.append(
            AdminWalkerPerformanceResponse(
                user_id=walker["id"],
                name=walker.get("full_name", "Passeador"),
                photo_url=walker.get("profile_photo_url") or _build_avatar_data_uri("#EAF7EF", "#2FBF71"),
                region=walker.get("region", "Não informado"),
                operational_status=operational_status,
                rating_avg=rating_avg,
                rating_count=rating_count,
                completed_walks=len(completed),
                severe_delay_rate=severe_delay_rate,
                no_show_rate=_to_percentage(len(no_shows), total),
                cancel_rate=_to_percentage(len(canceled), total),
                score_final=score_final,
                tip_total_amount=tip_total,
                tip_average_amount=tip_average,
                tip_recent_window_total=_coerce_float(quality_metrics.get("tip_recent_window_total"), 0.0),
                tip_recent_window_count=int(quality_metrics.get("tip_recent_window_count", 0) or 0),
                tip_suspicious_flag=bool(walker.get("flag_suspeita_gorjeta", False) or quality_metrics.get("tip_suspicious_flag", False)),
                tip_suspicious_reasons=list(
                    quality_metrics.get("tip_suspicious_reasons")
                    or walker.get("tip_suspicion_reasons")
                    or []
                ),
                tip_score_impact_points=_coerce_float(quality_metrics.get("tip_score_impact_points"), 0.0),
                tip_score_impact_mode=str(
                    walker.get("tip_score_impact_mode")
                    or quality_metrics.get("tip_score_impact_mode")
                    or "normal"
                ),
                tip_platform_avg_comparison=_coerce_float(quality_metrics.get("tip_platform_avg_comparison"), 0.0),
                tip_origin_top_clients=tip_origin_top_clients,
                alerts_count=alerts_count,
                risk_flag=risk_flag,
                suspected_disintermediation=suspected_disintermediation,
                is_premium_featured=is_featured,
                can_be_featured_by_rule=can_feature,
                premium_override=premium_override,
            )
        )

    response.sort(
        key=lambda item: (
            item.is_premium_featured,
            item.score_final,
            item.rating_avg,
            item.tip_total_amount,
            item.completed_walks,
            -item.alerts_count,
        ),
        reverse=True,
    )
    return response


@api_router.post("/admin/walkers/{walker_user_id}/action", response_model=AdminWalkerPerformanceResponse)
async def execute_admin_walker_action(walker_user_id: str, payload: AdminWalkerActionPayload, request: Request):
    admin_user = await _require_admin(request)
    walker = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
    if not walker:
        raise HTTPException(status_code=404, detail="Passeador não encontrado")

    updates: Dict[str, Any] = {}
    note_text = payload.note.strip()
    if payload.action == "warn":
        updates["walker_operational_status"] = WALKER_OP_STATUS_OBSERVATION
        updates["quality_status"] = QUALITY_STATUS_OBSERVATION
    elif payload.action == "set_observation":
        updates["walker_operational_status"] = WALKER_OP_STATUS_OBSERVATION
        updates["quality_status"] = QUALITY_STATUS_OBSERVATION
    elif payload.action == "restrict":
        updates["walker_operational_status"] = WALKER_OP_STATUS_RESTRICTED
        updates["quality_status"] = QUALITY_STATUS_RESTRICTED
    elif payload.action == "suspend":
        updates["walker_operational_status"] = WALKER_OP_STATUS_SUSPENDED
        updates["quality_status"] = QUALITY_STATUS_SUSPENDED
        updates["isActive"] = False
    elif payload.action == "reactivate":
        updates["walker_operational_status"] = WALKER_OP_STATUS_ACTIVE
        updates["quality_status"] = QUALITY_STATUS_ACTIVE
        updates["isActive"] = True
    elif payload.action == "start_recovery":
        all_walks = await db.walks.find({}, {"_id": 0}).to_list(5000)
        walker_walks = [row for row in all_walks if _walk_matches_walker_user(row, walker)]
        tip_rows = await _list_paid_tips_for_walker(
            walker_user_id=str(walker.get("id") or ""),
            walker_id=f"partner-{str(walker.get('id') or '').strip()}" if walker.get("id") else "",
            walker_name=str(walker.get("full_name") or ""),
            limit=1000,
        )
        tip_total_amount = round(sum(_coerce_float(row.get("amount"), 0.0) for row in tip_rows), 2)
        platform_tip_average = await _platform_tip_average_recent()
        metrics_seed = _compute_reputation_metrics(
            walker_walks,
            str(walker.get("quality_status") or QUALITY_STATUS_ACTIVE),
            tip_total_amount,
            tip_rows,
            walker,
            platform_tip_average,
        )
        severity = _monitoring_severity_from_metrics(
            str(walker.get("quality_status") or QUALITY_STATUS_ACTIVE),
            metrics_seed,
        )
        updates["quality_monitoring"] = {
            "active": True,
            "severity": severity,
            "target_walks": _quality_target_from_severity(severity),
            "completed_walks": 0,
            "reset_count": 0,
            "severe_delay_incidents": 0,
            "course_completed": False,
            "quiz_passed": False,
            "quiz_attempts": 0,
            "consecutive_quiz_failures": 0,
            "review_recommended": False,
        }
    elif payload.action == "feature_premium":
        metrics = await list_admin_walker_performance(request)
        target = next((item for item in metrics if item.user_id == walker_user_id), None)
        if not target or not target.can_be_featured_by_rule:
            raise HTTPException(status_code=400, detail="Passeador não atende critérios para destaque premium")
        updates["is_premium_featured"] = True
        updates["premium_override"] = False
    elif payload.action == "force_feature_premium":
        if len(note_text) < 30:
            raise HTTPException(status_code=400, detail="Justificativa obrigatória (mín. 30 caracteres) para override premium")
        updates["is_premium_featured"] = True
        updates["premium_override"] = True
    elif payload.action == "remove_feature":
        updates["is_premium_featured"] = False
        updates["premium_override"] = False
    elif payload.action == "tip_review_progressive":
        review_level = int(walker.get("tip_suspicion_review_level", 0) or 0) + 1
        if review_level <= 1:
            mode = "ignore_current"
        elif review_level == 2:
            mode = "ignore_recent_window"
        else:
            mode = "blocked_until_review"
        updates["tip_suspicion_review_level"] = review_level
        updates["tip_score_impact_mode"] = mode
        updates["tip_manual_review_required"] = True
        updates["flag_suspeita_gorjeta"] = True
    elif payload.action == "tip_restore_impact":
        updates["tip_suspicion_review_level"] = 0
        updates["tip_score_impact_mode"] = "normal"
        updates["tip_manual_review_required"] = False
        updates["flag_suspeita_gorjeta"] = False
        updates["tip_suspicion_reasons"] = []

    now_iso = datetime.now(timezone.utc).isoformat()
    action_logs = walker.get("admin_action_logs", []) if isinstance(walker.get("admin_action_logs"), list) else []
    action_logs.append(
        {
            "id": str(uuid.uuid4()),
            "action": payload.action,
            "note": note_text,
            "performed_by_id": admin_user.get("id", ""),
            "performed_by_name": admin_user.get("full_name", "Admin"),
            "timestamp": now_iso,
        }
    )
    updates["admin_action_logs"] = action_logs
    updates["updated_at"] = now_iso

    await db.users.update_one({"id": walker_user_id}, {"$set": updates})

    await _create_notification(
        user_id=walker_user_id,
        role="passeador",
        title="Atualização administrativa",
        message=note_text or f"Ação administrativa aplicada: {payload.action}",
        category="qualidade",
    )

    metrics_after = await list_admin_walker_performance(request)
    updated_metric = next((item for item in metrics_after if item.user_id == walker_user_id), None)
    if not updated_metric:
        raise HTTPException(status_code=404, detail="Passeador não encontrado após atualização")
    return updated_metric


@api_router.patch("/admin/walks/{walk_id}/status", response_model=WalkResponse)
async def update_admin_walk_status(walk_id: str, payload: AdminWalkStatusUpdate):
    current = await _get_walk_or_404(walk_id)
    if (
        current.get("walk_type") == WALK_TYPE_SHARED
        and not current.get("shared_approved", False)
        and payload.status in {STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW, STATUS_FINISHED}
    ):
        raise HTTPException(status_code=400, detail="Passeio compartilhado precisa de aprovação manual antes de iniciar")

    update_data = {"status": payload.status, "updated_at": datetime.now(timezone.utc).isoformat()}
    if payload.status == STATUS_FINISHED:
        update_data["summary_text"] = _generate_walk_summary({**current, **update_data})

    result = await db.walks.update_one({"id": walk_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Passeio não encontrado")

    updated = await _get_walk_or_404(walk_id)
    if str(updated.get("tipoPasseio") or "padrao") == "transporte":
        transport_events = list(updated.get("eventosTransporte") or [])
        status_label = str(updated.get("statusTransporte") or "A caminho do local")
        for event_key, event_label in _transport_events_for_status(payload.status):
            transport_events.append(
                {
                    "event": event_key,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status_label": event_label,
                }
            )
            status_label = event_label

        if transport_events:
            await db.walks.update_one(
                {"id": walk_id},
                {
                    "$set": {
                        "eventosTransporte": transport_events[-40:],
                        "statusTransporte": status_label,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
            updated = await _get_walk_or_404(walk_id)

    if payload.status == STATUS_FINISHED:
        await _recalculate_quality_from_walk(updated, "walk_finished")
        await _refresh_pet_routine_progress_from_walk(updated)
        client_user_id = str(updated.get("client_user_id") or "").strip()
        if client_user_id:
            await _evaluate_referrals_for_user(client_user_id)
        walker_user_id = str(updated.get("walker_user_id") or "").strip()
        if walker_user_id:
            await _evaluate_referrals_for_user(walker_user_id)
    elif payload.status == STATUS_NO_SHOW_WALKER:
        await _recalculate_quality_from_walk(updated, "attendance_event")
    elif payload.status == STATUS_CANCELED:
        await _recalculate_quality_from_walk(updated, "walk_canceled")
    return _to_walk_response(updated)


@api_router.patch("/admin/walks/{walk_id}/premium-analysis", response_model=WalkResponse)
async def update_admin_premium_analysis(walk_id: str, payload: AdminPremiumAnalysisUpdate, request: Request):
    await _require_admin_permission(request, "passeios")
    walk = await _get_walk_or_404(walk_id)
    if walk.get("modoInicioPasseio") != START_MODE_PREMIUM_RELOCATION:
        raise HTTPException(status_code=400, detail="Análise premium disponível apenas para passeios premium")

    updates: Dict[str, Any] = {
        "statusAnaliseDeslocamento": payload.statusAnaliseDeslocamento,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if payload.statusAnaliseDeslocamento == PREMIUM_ANALYSIS_APPROVED:
        updates["precisaAnaliseManualDeslocamento"] = False
        if payload.adicionalDeslocamento is not None:
            updates["adicionalDeslocamento"] = max(0.0, round(_coerce_float(payload.adicionalDeslocamento, 0.0), 2))
    elif payload.statusAnaliseDeslocamento == PREMIUM_ANALYSIS_REJECTED:
        updates["precisaAnaliseManualDeslocamento"] = False
        updates["adicionalDeslocamento"] = 0.0
        updates["status"] = STATUS_CANCELED
        updates["motivoCancelamento"] = "Deslocamento premium reprovado pela equipe"
        updates["tipoCancelamento"] = "passeador"

    walk.update(updates)
    walk["valor_base_passeio"] = _base_walk_price(walk)
    total_price, walker_payout = _calculate_walk_pricing(walk)
    walk["base_price"] = total_price
    walk["walker_payout"] = walker_payout

    await db.walks.update_one({"id": walk_id}, {"$set": walk})
    await _rebuild_payments_for_walk(walk)
    return _to_walk_response(walk)


@api_router.patch("/admin/walks/{walk_id}/shared-approval", response_model=WalkResponse)
async def update_shared_walk_approval(walk_id: str, payload: SharedWalkApprovalUpdate):
    walk = await _get_walk_or_404(walk_id)
    if walk.get("walk_type") != WALK_TYPE_SHARED:
        raise HTTPException(status_code=400, detail="Apenas passeios compartilhados podem ser aprovados")

    pet_ids = list(walk.get("pet_ids", []))
    if not pet_ids:
        raise HTTPException(status_code=400, detail="Passeio sem pet principal")

    pet1 = await _get_pet_or_404(pet_ids[0])
    pet2 = None

    if payload.approved:
        await _validate_pet_for_shared_rules(pet1)

        if walk.get("shared_context") == SHARED_CONTEXT_SAME_HOUSEHOLD:
            if len(pet_ids) < 2:
                raise HTTPException(status_code=400, detail="Faltando segundo pet no compartilhado de mesma casa")
            pet2 = await _get_pet_or_404(pet_ids[1])
        else:
            target_pet2_id = payload.second_pet_id or (pet_ids[1] if len(pet_ids) > 1 else None)
            if not target_pet2_id:
                raise HTTPException(status_code=400, detail="Selecione o segundo pet para aprovar o compartilhado")
            pet2 = await _get_pet_or_404(target_pet2_id)

        if pet2["id"] == pet1["id"]:
            raise HTTPException(status_code=400, detail="Os pets do compartilhado devem ser diferentes")

        await _validate_pet_for_shared_rules(pet2)

        selected_walker_id = payload.walker_id or walk.get("walker_id")
        selected_walker = await _resolve_walker_profile(selected_walker_id)
        if not selected_walker:
            raise HTTPException(status_code=400, detail="Passeador inválido para o grupo compartilhado")
        selected_walker_user = await db.users.find_one(
            {"full_name": selected_walker["name"], "role": "passeador"},
            {"_id": 0},
        )

        current_group = walk.get("shared_group") or {}
        group_payload = {
            "id": current_group.get("id") or str(uuid.uuid4()),
            "walk_id": walk_id,
            "pet1": pet1["id"],
            "pet2": pet2["id"],
            "passeador": selected_walker["id"],
            "aprovado": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if not current_group.get("id"):
            group_payload["created_at"] = datetime.now(timezone.utc).isoformat()

        await db.shared_walk_groups.update_one(
            {"walk_id": walk_id},
            {"$set": group_payload},
            upsert=True,
        )

        walk["pet_ids"] = [pet1["id"], pet2["id"]]
        walk["shared_pet_names"] = [pet1["pet_name"], pet2["pet_name"]]
        walk["shared_client_names"] = [pet1.get("owner_name", "Tutor 1"), pet2.get("owner_name", "Tutor 2")]
        owner_keys = [_owner_key_from_pet(pet1), _owner_key_from_pet(pet2)]
        walk["shared_owner_keys"] = [key for key in owner_keys if key]
        if len(set(walk["shared_owner_keys"])) <= 1:
            walk["shared_context"] = SHARED_CONTEXT_SAME_HOUSEHOLD
        else:
            walk["shared_context"] = SHARED_CONTEXT_OTHER_CLIENT
        walk["pet_name"] = " + ".join(walk["shared_pet_names"])
        walk["walker_id"] = selected_walker["id"]
        walk["walker_user_id"] = selected_walker_user.get("id") if selected_walker_user else None
        walk["walker_name"] = selected_walker["name"]
        walk["walker_photo_url"] = selected_walker["photo_url"]
        if not walk.get("client_user_id"):
            walk["client_user_id"] = pet1.get("owner_user_id")
        participant_user_ids = [
            pet1.get("owner_user_id"),
            pet2.get("owner_user_id"),
            walk.get("client_user_id"),
        ]
        walk["participant_user_ids"] = list(dict.fromkeys([item for item in participant_user_ids if item]))
        walk["shared_approved"] = True
        walk["shared_group"] = {
            "id": group_payload["id"],
            "pet1": group_payload["pet1"],
            "pet2": group_payload["pet2"],
            "passeador": group_payload["passeador"],
            "aprovado": True,
        }
    else:
        walk["shared_approved"] = False
        if walk.get("shared_group"):
            walk["shared_group"]["aprovado"] = False
        await db.shared_walk_groups.update_one(
            {"walk_id": walk_id},
            {"$set": {"aprovado": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
        )

    walk["updated_at"] = datetime.now(timezone.utc).isoformat()
    base_price, walker_payout = _calculate_walk_pricing(walk)
    walk["base_price"] = base_price
    walk["walker_payout"] = walker_payout

    await db.walks.update_one({"id": walk_id}, {"$set": walk})
    await _rebuild_payments_for_walk(walk)

    if payload.approved:
        client_users = await db.users.find({"full_name": {"$in": walk.get("shared_client_names", [])}}, {"_id": 0}).to_list(20)
        for client_user in client_users:
            await _create_notification(
                user_id=client_user["id"],
                role="cliente",
                title="Compartilhado aprovado",
                message="Seu passeio compartilhado foi aprovado pela equipe.",
                category="compartilhado",
            )

        walker_user = await db.users.find_one({"full_name": walk.get("walker_name"), "role": "passeador"}, {"_id": 0})
        if walker_user:
            await _create_notification(
                user_id=walker_user["id"],
                role="passeador",
                title="Compartilhado aprovado",
                message=f"Você foi confirmado no compartilhado de {walk.get('pet_name')}",
                category="operacao",
            )

    updated = await _get_walk_or_404(walk_id)
    return _to_walk_response(updated)


@api_router.get("/admin/payments", response_model=List[AdminPaymentResponse])
async def list_admin_payments():
    await _sync_payments_from_walks()
    payments = await db.payments.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return [_to_payment_response(payment) for payment in payments]


@api_router.get("/admin/payments/{payment_id}", response_model=AdminPaymentResponse)
async def get_admin_payment(payment_id: str):
    payment = await db.payments.find_one({"id": payment_id}, {"_id": 0})
    if not payment:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado")
    return _to_payment_response(payment)


@api_router.patch("/admin/payments/{payment_id}/status", response_model=AdminPaymentResponse)
async def update_admin_payment_status(payment_id: str, payload: AdminPaymentStatusUpdate):
    update_data = {
        "payment_status": payload.payment_status,
        "payment_method": payload.payment_method.strip(),
        #"tipoPagamento": payload.payment_method.strip(),
        "notes": payload.notes.strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload.payment_status == "Pago":
        update_data["payment_date"] = datetime.now(timezone.utc).isoformat()
    elif payload.payment_status == "Pendente":
        update_data["payment_date"] = None

    result = await db.payments.update_one({"id": payment_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Pagamento não encontrado")

    updated = await db.payments.find_one({"id": payment_id}, {"_id": 0})
    walk_id = str((updated or {}).get("walk_id") or "").strip()
    if walk_id:
        await db.walks.update_one(
            {"id": walk_id},
            {
                "$set": {
                    "payment_status": payload.payment_status,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        walk_row = await db.walks.find_one({"id": walk_id}, {"_id": 0})
        if walk_row and payload.payment_status == "Pago":
            await _refresh_pet_routine_progress_from_walk(walk_row)
            client_user_id = str(walk_row.get("client_user_id") or "").strip()
            if client_user_id:
                await _evaluate_referrals_for_user(client_user_id)
            walker_user_id = str(walk_row.get("walker_user_id") or "").strip()
            if walker_user_id:
                await _evaluate_referrals_for_user(walker_user_id)

    return _to_payment_response(updated)


@api_router.patch("/walks/{walk_id}/status", response_model=WalkResponse)
async def update_walk_status(walk_id: str, payload: WalkStatusUpdate, request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)
    if (
        walk.get("walk_type") == WALK_TYPE_SHARED
        and not walk.get("shared_approved", False)
        and payload.status in {STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW, STATUS_FINISHED}
    ):
        raise HTTPException(status_code=400, detail="Passeio compartilhado precisa de aprovação manual antes de iniciar")

    current_status = walk.get("status", STATUS_SCHEDULED)
    if current_status == LEGACY_STATUS_IN_PROGRESS:
        current_status = STATUS_WALKING_NOW

    if payload.status not in {STATUS_SCHEDULED, STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW, STATUS_FINISHED}:
        raise HTTPException(status_code=400, detail="Status inválido")

    allowed_transitions = {
        STATUS_SCHEDULED: {STATUS_GOING_TO_PICKUP},
        STATUS_GOING_TO_PICKUP: {STATUS_WALKING_NOW},
        STATUS_WALKING_NOW: {STATUS_FINISHED},
        STATUS_FINISHED: set(),
    }

    if payload.status != current_status and payload.status not in allowed_transitions.get(current_status, set()):
        raise HTTPException(status_code=400, detail="Transição de status inválida")

    update_data = {
        "status": payload.status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if payload.status == STATUS_FINISHED:
        walk_with_latest = {**walk, **update_data}
        update_data["summary_text"] = _generate_walk_summary(walk_with_latest)

    update_result = await db.walks.update_one(
        {"id": walk_id},
        {"$set": update_data},
    )
    if update_result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Passeio não encontrado")

    updated_walk = await _get_walk_or_404(walk_id)
    if str(updated_walk.get("tipoPasseio") or "padrao") == "transporte":
        transport_events = list(updated_walk.get("eventosTransporte") or [])
        status_label = str(updated_walk.get("statusTransporte") or "A caminho do local")
        for event_key, event_label in _transport_events_for_status(payload.status):
            transport_events.append(
                {
                    "event": event_key,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status_label": event_label,
                }
            )
            status_label = event_label
        if transport_events:
            await db.walks.update_one(
                {"id": walk_id},
                {
                    "$set": {
                        "eventosTransporte": transport_events[-40:],
                        "statusTransporte": status_label,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
            )
            updated_walk = await _get_walk_or_404(walk_id)

    if payload.status == STATUS_FINISHED:
        await _refresh_pet_routine_progress_from_walk(updated_walk)
        client_user_id = str(updated_walk.get("client_user_id") or "").strip()
        if client_user_id:
            await _evaluate_referrals_for_user(client_user_id)
        walker_user_id = str(updated_walk.get("walker_user_id") or "").strip()
        if walker_user_id:
            await _evaluate_referrals_for_user(walker_user_id)
            await _evaluate_reputation_credit_gains_for_walk(
                updated_walk,
                trigger="walk_finished",
            )
        await _evaluate_premium_verified_badge_for_walk(
            walk=updated_walk,
            trigger="walk_finished",
            severe_infraction=False,
        )
        updated_walk = await _get_walk_or_404(walk_id)

    user = await db.users.find_one({"full_name": updated_walk.get("client_name"), "role": "cliente"}, {"_id": 0})
    if user and payload.status in {STATUS_GOING_TO_PICKUP, STATUS_WALKING_NOW, STATUS_FINISHED}:
        status_message_map = {
            STATUS_GOING_TO_PICKUP: "O passeador está indo buscar seu pet.",
            STATUS_WALKING_NOW: "Passeio em andamento agora.",
            STATUS_FINISHED: "Passeio finalizado com sucesso.",
        }
        await _create_notification(
            user_id=user["id"],
            role="cliente",
            title=f"Status atualizado: {payload.status}",
            message=status_message_map[payload.status],
            category="status_passeio",
        )

    recalculated_walker_user_id = str(updated_walk.get("walker_user_id") or "").strip()
    if recalculated_walker_user_id:
        await _recalculate_walker_verification_for_user(
            walker_user_id=recalculated_walker_user_id,
            trigger=f"status_update:{payload.status}",
            walk_id=walk_id,
        )
        updated_walk = await _get_walk_or_404(walk_id)

    return _to_walk_response(updated_walk)


@api_router.post("/walks/{walk_id}/check-in", response_model=WalkResponse)
async def walker_check_in_at_location(walk_id: str, payload: WalkKitChecklistConfirm, request: Request):
    if not _is_feature_active("kit_system_enabled"):
        raise HTTPException(status_code=400, detail="Sistema de kit está desativado no momento")

    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)

    if user.get("role") == "passeador" and not _is_user_assigned_walker(user, walk):
        raise HTTPException(status_code=403, detail="Apenas o passeador atribuído pode registrar check-in")

    if walk.get("status") in DECISION_TERMINAL_STATUSES or walk.get("status") == STATUS_PENDING_REVIEW:
        return _to_walk_response(walk)

    if walk.get("walker_check_in_at"):
        return _to_walk_response(walk)

    walker_user_doc = user
    if user.get("role") != "passeador":
        assigned_walker_user_id = str(walk.get("walker_user_id") or "").strip()
        if assigned_walker_user_id:
            walker_user_doc = await db.users.find_one({"id": assigned_walker_user_id, "role": "passeador"}, {"_id": 0}) or {}

    kit_profile = _walker_kit_profile_from_user(walker_user_doc)
    _assert_walker_basic_kit_ready(kit_profile)
    checklist_data = _validate_kit_checklist_confirmation(payload)

    now_dt = datetime.now(timezone.utc)
    start_dt, _ = _walk_start_end(walk)
    if not start_dt:
        raise HTTPException(status_code=400, detail="Passeio com horário inválido")

    tolerance_base = now_dt if now_dt > start_dt else start_dt
    tolerance_expires_at = tolerance_base + timedelta(minutes=TOLERANCE_MINUTES)
    delay_status = OCC_LATE_SEVERE if now_dt > (start_dt + timedelta(minutes=TOLERANCE_MINUTES)) else OCC_LATE_LIGHT

    updates = {
        "walker_check_in_at": now_dt.isoformat(),
        "status": STATUS_GOING_TO_PICKUP,
        "tolerance_expires_at": tolerance_expires_at.isoformat(),
        "attendance_message": "Há tolerância de até 10 minutos para início do passeio",
        "occurrence_status": delay_status,
        "occurrence_resolved": False,
        "kit_checklist_check_in_confirmed": False,
        "checklist_validado_chegada": False,
        "kit_checklist_check_in": {
            "confirmed": False,
            "validated_by": "walker",
            "awaiting_client_validation": True,
            "confirmed_at": now_dt.isoformat(),
            **checklist_data,
        },
        "updated_at": now_dt.isoformat(),
    }
    await db.walks.update_one({"id": walk_id}, {"$set": updates})

    refreshed = await _get_walk_or_404(walk_id)
    refreshed = await _apply_attendance_decision_if_needed(refreshed, now=now_dt, trigger="walker_check_in")
    await _recalculate_quality_from_walk(refreshed, "attendance_event")

    client_user_id = refreshed.get("client_user_id")
    if client_user_id:
        await _create_notification(
            user_id=client_user_id,
            role="cliente",
            title="Passeador chegou ao local",
            message="O passeador registrou chegada. Valide o checklist do kit e confirme a entrega em até 10 minutos.",
            category="status_passeio",
        )

    walker_user_id = str(refreshed.get("walker_user_id") or "").strip()
    if walker_user_id:
        await _recalculate_walker_verification_for_user(
            walker_user_id=walker_user_id,
            trigger="check_in",
            walk_id=walk_id,
        )
        refreshed = await _get_walk_or_404(walk_id)

    return _to_walk_response(refreshed)


@api_router.post("/walks/{walk_id}/kit-checklist/check-in-validate", response_model=WalkResponse)
async def validate_walk_arrival_checklist(walk_id: str, payload: WalkKitChecklistConfirm, request: Request):
    if not _is_feature_active("kit_system_enabled"):
        raise HTTPException(status_code=400, detail="Sistema de kit está desativado no momento")

    user = await _require_role(request, ["cliente", "admin", "super_admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)

    if user.get("role") == "cliente" and walk.get("client_user_id") and walk.get("client_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Somente o cliente do passeio pode validar checklist de chegada")

    if walk.get("status") in DECISION_TERMINAL_STATUSES or walk.get("status") == STATUS_PENDING_REVIEW:
        return _to_walk_response(walk)

    checklist_data = _validate_kit_checklist_confirmation(payload)
    now_iso = datetime.now(timezone.utc).isoformat()
    updates = {
        "kit_checklist_check_in_confirmed": True,
        "checklist_validado_chegada": True,
        "kit_checklist_check_in": {
            "confirmed": True,
            "validated_by": str(user.get("role") or "cliente"),
            "validated_by_user_id": str(user.get("id") or ""),
            "validated_at": now_iso,
            **checklist_data,
        },
        "updated_at": now_iso,
    }
    await db.walks.update_one({"id": walk_id}, {"$set": updates})
    refreshed = await _get_walk_or_404(walk_id)

    walker_user_id = str(refreshed.get("walker_user_id") or "")
    if walker_user_id:
        await _append_walker_event_log(
            walker_user_id,
            event_type="kit_checklist_arrival_validated",
            payload={"walk_id": walk_id, "validated_by_user_id": str(user.get("id") or ""), "checklist": checklist_data},
        )

    return _to_walk_response(refreshed)


@api_router.post("/walks/{walk_id}/confirm-handover", response_model=WalkResponse)
async def client_confirm_pet_handover(walk_id: str, request: Request):
    user = await _require_role(request, ["cliente", "admin", "super_admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)
    walk = await _apply_attendance_decision_if_needed(walk, trigger="client_confirmation")

    if user.get("role") == "cliente" and walk.get("client_user_id") and walk.get("client_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Somente o cliente do passeio pode confirmar a entrega")

    if walk.get("status") in DECISION_TERMINAL_STATUSES or walk.get("status") == STATUS_PENDING_REVIEW:
        return _to_walk_response(walk)

    if walk.get("client_confirmed_at"):
        return _to_walk_response(walk)

    now_dt = datetime.now(timezone.utc)
    updates: Dict[str, Any] = {
        "client_confirmed_at": now_dt.isoformat(),
        "updated_at": now_dt.isoformat(),
    }

    if walk.get("walker_check_in_at"):
        if not bool(walk.get("kit_checklist_check_in_confirmed", False)):
            raise HTTPException(
                status_code=400,
                detail="Checklist de chegada pendente. Cliente deve validar itens antes do início.",
            )
        if not bool(walk.get("kit_checklist_start_confirmed", False)):
            raise HTTPException(
                status_code=400,
                detail="Checklist obrigatório do passeador pendente. Confirme água, vasilha, saquinhos e primeiros socorros antes de iniciar.",
            )
        updates["status"] = STATUS_WALKING_NOW
        updates["attendance_message"] = "Entrega confirmada. Passeio iniciado com segurança."
        updates["tolerance_expires_at"] = now_dt.isoformat()
    else:
        updates["attendance_message"] = "Cliente confirmou entrega. Aguardando check-in do passeador."

    await db.walks.update_one({"id": walk_id}, {"$set": updates})

    refreshed = await _get_walk_or_404(walk_id)
    refreshed = await _apply_attendance_decision_if_needed(refreshed, now=now_dt, trigger="client_confirmation")
    await _recalculate_quality_from_walk(refreshed, "attendance_event")

    walker_user_id = refreshed.get("walker_user_id")
    if walker_user_id:
        await _create_notification(
            user_id=walker_user_id,
            role="passeador",
            title="Cliente confirmou entrega",
            message="O cliente registrou a entrega do pet no app.",
            category="operacao",
        )
        await _recalculate_walker_verification_for_user(
            walker_user_id=str(walker_user_id),
            trigger="client_handover",
            walk_id=walk_id,
        )
        refreshed = await _get_walk_or_404(walk_id)

    return _to_walk_response(refreshed)


@api_router.post("/walks/{walk_id}/kit-checklist/start", response_model=WalkResponse)
async def confirm_walk_start_kit_checklist(walk_id: str, payload: WalkKitChecklistConfirm, request: Request):
    if not _is_feature_active("kit_system_enabled"):
        raise HTTPException(status_code=400, detail="Sistema de kit está desativado no momento")

    user = await _require_role(request, ["passeador", "admin", "super_admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)

    if user.get("role") == "passeador" and not _is_user_assigned_walker(user, walk):
        raise HTTPException(status_code=403, detail="Apenas o passeador atribuído pode confirmar checklist de início")

    if walk.get("status") in DECISION_TERMINAL_STATUSES or walk.get("status") == STATUS_PENDING_REVIEW:
        return _to_walk_response(walk)

    walker_user_doc = user
    if user.get("role") != "passeador":
        assigned_walker_user_id = str(walk.get("walker_user_id") or "").strip()
        if assigned_walker_user_id:
            walker_user_doc = await db.users.find_one({"id": assigned_walker_user_id, "role": "passeador"}, {"_id": 0}) or {}

    kit_profile = _walker_kit_profile_from_user(walker_user_doc)
    _assert_walker_basic_kit_ready(kit_profile)
    if not bool(walk.get("kit_checklist_check_in_confirmed", False)):
        raise HTTPException(status_code=400, detail="Checklist de chegada precisa ser validado pelo cliente antes do início")

    checklist_data = _validate_kit_checklist_confirmation(payload)

    now_iso = datetime.now(timezone.utc).isoformat()
    was_confirmed_before = bool(walk.get("kit_checklist_start_confirmed", False))
    updates = {
        "kit_checklist_start_confirmed": True,
        "checklist_confirmado_inicio": True,
        "kit_checklist_start": {
            "confirmed": True,
            "confirmed_at": now_iso,
            **checklist_data,
        },
        "updated_at": now_iso,
    }
    await db.walks.update_one({"id": walk_id}, {"$set": updates})
    if not was_confirmed_before:
        await db.users.update_one(
            {"id": str(walk.get("walker_user_id") or "")},
            {"$inc": {"kit_checklist_streak": 1}, "$set": {"updated_at": now_iso}},
        )
    refreshed = await _get_walk_or_404(walk_id)
    await _evaluate_premium_verified_badge_for_walk(
        walk=refreshed,
        trigger="checklist_start_confirmed",
        severe_infraction=False,
    )
    walker_user_id = str(refreshed.get("walker_user_id") or "").strip()
    if walker_user_id:
        await _recalculate_walker_verification_for_user(
            walker_user_id=walker_user_id,
            trigger="checklist_start_confirmed",
            walk_id=walk_id,
        )
    refreshed = await _get_walk_or_404(walk_id)
    return _to_walk_response(refreshed)


@api_router.post("/walks/{walk_id}/kit-issue-report", response_model=WalkResponse)
async def report_walk_kit_issue(walk_id: str, payload: WalkKitIssueReportPayload, request: Request):
    if not _is_feature_active("kit_system_enabled"):
        raise HTTPException(status_code=400, detail="Sistema de kit está desativado no momento")

    user = await _require_role(request, ["cliente", "admin", "super_admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)

    if user.get("role") == "cliente" and walk.get("client_user_id") and walk.get("client_user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="Somente o cliente do passeio pode registrar ocorrência de kit")

    if walk.get("status") == STATUS_CANCELED:
        raise HTTPException(status_code=400, detail="Passeio cancelado não permite denúncia de kit")

    if not bool(payload.confirm_report):
        raise HTTPException(status_code=400, detail="Confirme o relato para registrar ocorrência")

    alias_map = {
        "water_sealed": "has_water",
        "water_bowl": "has_bowl",
        "poop_bags": "has_bags",
        "first_aid_kit": "has_first_aid",
    }
    missing_items = [alias_map.get(str(item).strip(), str(item).strip()) for item in payload.missing_items if str(item).strip()]
    if not missing_items:
        raise HTTPException(status_code=400, detail="Selecione ao menos um item ausente")

    existing_report = walk.get("kit_issue_report") or {}
    if isinstance(existing_report, dict) and existing_report.get("confirmed"):
        raise HTTPException(status_code=400, detail="Esta ocorrência de kit já foi registrada neste passeio")

    now_iso = datetime.now(timezone.utc).isoformat()
    reporter_id = str(user.get("id") or "")
    report_payload = {
        "confirmed": True,
        "missing_items": missing_items,
        "note": payload.note.strip(),
        "reported_by_user_id": reporter_id,
        "reported_by_role": str(user.get("role") or "cliente"),
        "reported_at": now_iso,
    }

    occurrence_logs = list(walk.get("occurrence_logs") or [])
    occurrence_logs.append(
        {
            "id": str(uuid.uuid4()),
            "action": "kit_issue_reported",
            "timestamp": now_iso,
            "performed_by_id": reporter_id,
            "performed_by_name": str(user.get("full_name") or "Cliente"),
            "note": payload.note.strip(),
            "before_values": {},
            "after_values": {"missing_items": missing_items},
        }
    )

    walk_updates = {
        "kit_issue_report": report_payload,
        "occurrence_status": KIT_OCCURRENCE_STATUS,
        "occurrence_resolved": False,
        "occurrence_logs": occurrence_logs[-40:],
        "updated_at": now_iso,
    }
    await db.walks.update_one({"id": walk_id}, {"$set": walk_updates})

    walker_user_id = str(walk.get("walker_user_id") or "").strip()
    if walker_user_id:
        walker_user = await db.users.find_one({"id": walker_user_id, "role": "passeador"}, {"_id": 0})
        if walker_user:
            current_reports = int(walker_user.get("kit_missing_reports_count", 0) or 0)
            updated_reports = current_reports + 1
            reliability_penalty = _kit_reliability_penalty_points(updated_reports)
            walker_updates = {
                "kit_missing_reports_count": updated_reports,
                "kit_reliability_penalty_points": reliability_penalty,
                "kit_last_report_at": now_iso,
                "kit_checklist_streak": 0,
                "updated_at": now_iso,
            }
            await db.users.update_one({"id": walker_user_id}, {"$set": walker_updates})

            if updated_reports == 1:
                title = "Aviso leve sobre kit"
                message = "Um cliente reportou ausência de itens do kit. Não houve punição forte nesta primeira ocorrência."
            elif updated_reports == 2:
                title = "Reincidência de ocorrência de kit"
                message = "Nova ocorrência registrada. Penalidade completa aplicada e risco de rebaixamento de nível."
            else:
                title = "Múltiplas ocorrências de kit"
                message = "Foram registradas múltiplas ocorrências. Houve impacto de confiabilidade e rebaixamento de nível de kit."

            await _create_notification(
                user_id=walker_user_id,
                role="passeador",
                title=title,
                message=message,
                category="qualidade",
            )

            await _notify_admins(
                title="Ocorrência de kit reportada",
                message=f"Passeio {walk_id} recebeu denúncia de kit ausente.",
                category="admin_alerta",
            )

    severe_flag = "has_first_aid" in missing_items or len(missing_items) >= 2
    refreshed = await _get_walk_or_404(walk_id)
    await _evaluate_premium_verified_badge_for_walk(
        walk=refreshed,
        trigger="kit_issue_report",
        severe_infraction=severe_flag,
    )
    walker_user_id = str(refreshed.get("walker_user_id") or "").strip()
    if walker_user_id:
        await _recalculate_walker_verification_for_user(
            walker_user_id=walker_user_id,
            trigger="kit_issue_report",
            walk_id=walk_id,
        )
    refreshed = await _get_walk_or_404(walk_id)
    return _to_walk_response(refreshed)


@api_router.patch("/walks/{walk_id}/cancel", response_model=WalkResponse)
async def cancel_walk(walk_id: str, payload: WalkCancelPayload, request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)
    if walk.get("status") == STATUS_FINISHED:
        raise HTTPException(status_code=400, detail="Passeio finalizado não pode ser cancelado")

    now = datetime.now(timezone.utc)
    walk_time = datetime.fromisoformat(walk.get("walk_datetime_iso"))
    hours_until_walk = (walk_time - now).total_seconds() / 3600

    penalty_percent = 0
    if payload.tipoCancelamento == "cliente":
        if now > walk_time:
            penalty_percent = 100
        elif hours_until_walk >= 24:
            penalty_percent = 0
        else:
            penalty_percent = 50

    update_data = {
        "status": STATUS_CANCELED,
        "tipoCancelamento": payload.tipoCancelamento,
        "motivoCancelamento": payload.motivoCancelamento.strip(),
        "penalidadePercentual": penalty_percent,
        "updated_at": now.isoformat(),
    }
    await db.walks.update_one({"id": walk_id}, {"$set": update_data})

    payments = await db.payments.find({"walk_id": walk_id}, {"_id": 0}).to_list(20)
    for payment in payments:
        original_value = float(payment.get("original_value") or payment.get("value") or 0)
        charged_value = round(original_value * (penalty_percent / 100), 2)
        await db.payments.update_one(
            {"id": payment["id"]},
            {
                "$set": {
                    "original_value": original_value,
                    "value": charged_value,
                    "payment_status": "Cancelado" if penalty_percent == 0 else "Pendente",
                    "notes": f"Cancelamento {payload.tipoCancelamento}: penalidade {penalty_percent}%",
                    "updated_at": now.isoformat(),
                }
            },
        )

    updated_walk = await _get_walk_or_404(walk_id)
    await _recalculate_quality_from_walk(updated_walk, "walk_canceled")

    if payload.tipoCancelamento == "cliente":
        client_user_id = str(updated_walk.get("client_user_id") or "").strip()
        if client_user_id:
            client_user = await db.users.find_one({"id": client_user_id, "role": "cliente"}, {"_id": 0})
            if client_user:
                client_walks = await db.walks.find({"client_user_id": client_user_id}, {"_id": 0}).to_list(500)
                await _generate_client_alerts(client_user, client_walks)
                if _is_disintermediation_flag_active(client_user):
                    await _generate_disintermediation_alert_for_user(
                        client_user,
                        str(client_user.get("desintermediacao_flag_reason") or "FLAG_ACTIVE"),
                    )

    return _to_walk_response(updated_walk)


@api_router.patch("/walks/{walk_id}/experience", response_model=WalkResponse)
async def update_walk_experience(walk_id: str, payload: WalkExperienceUpdate, request: Request):
    user = await _require_role(request, ["cliente", "passeador", "admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)
    update_data = {
        "did_pee": payload.did_pee,
        "did_poop": payload.did_poop,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    status_now = walk.get("status")
    if status_now in {STATUS_FINISHED}:
        summary_source = {**walk, **update_data}
        update_data["summary_text"] = _generate_walk_summary(summary_source)

    await db.walks.update_one(
        {"id": walk_id},
        {"$set": update_data},
    )
    updated_walk = await _get_walk_or_404(walk_id)
    await _recalculate_quality_from_walk(updated_walk, "rating_update")
    await _evaluate_reputation_credit_gains_for_walk(updated_walk, trigger="rating_update")
    return _to_walk_response(updated_walk)


@api_router.patch("/walks/{walk_id}/rating", response_model=WalkResponse)
async def update_walk_rating(walk_id: str, payload: WalkRatingUpdate, request: Request):
    user = await _require_role(request, ["cliente", "admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)
    if walk.get("status") != STATUS_FINISHED:
        raise HTTPException(status_code=400, detail="Avaliação disponível após finalização")
    if user.get("role") == "cliente" and isinstance(walk.get("rating"), int):
        raise HTTPException(status_code=400, detail="Este passeio já foi avaliado")

    await db.walks.update_one(
        {"id": walk_id},
        {
            "$set": {
                "rating": payload.rating,
                "rating_comment": payload.comment.strip(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    updated_walk = await _get_walk_or_404(walk_id)
    await _recalculate_quality_from_walk(updated_walk, "rating_update")
    return _to_walk_response(updated_walk)



@api_router.post("/walks/{walk_id}/tips/checkout", response_model=TipCheckoutResponse)
async def create_tip_checkout_session(walk_id: str, payload: TipCheckoutRequest, request: Request):
    if not _is_feature_active("tips"):
        raise HTTPException(status_code=403, detail="Funcionalidade de gorjetas está desativada")

    user = await _require_role(request, ["cliente", "admin"])
    walk = await _get_walk_for_user_or_403(walk_id, user)

    if walk.get("status") != STATUS_FINISHED:
        raise HTTPException(status_code=400, detail="Gorjeta disponível apenas após passeio finalizado")

    tip_deadline = _tip_deadline_for_walk(walk)
    if datetime.now(timezone.utc) > tip_deadline:
        raise HTTPException(status_code=400, detail="Prazo de 24h para gorjeta expirado")

    if _coerce_float(walk.get("tip_amount"), 0.0) > 0 or walk.get("tip_status") == "paid":
        raise HTTPException(status_code=400, detail="Este passeio já recebeu gorjeta")

    existing_paid_tip = await db.tips.find_one({"walk_id": walk_id, "status": "paid"}, {"_id": 0, "id": 1})
    if existing_paid_tip:
        raise HTTPException(status_code=400, detail="Este passeio já recebeu gorjeta")

    existing_pending_tx = await db.payment_transactions.find_one(
        {
            "walk_id": walk_id,
            "transaction_type": "tip",
            "processed": False,
            "payment_status": {"$in": ["pending", "unpaid", "requires_payment_method"]},
        },
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    if existing_pending_tx and existing_pending_tx.get("session_id"):
        return TipCheckoutResponse(
            tip_id=str(existing_pending_tx.get("tip_id") or ""),
            session_id=str(existing_pending_tx.get("session_id") or ""),
            amount=_coerce_float(existing_pending_tx.get("amount"), 0.0),
            max_allowed_amount=_tip_max_allowed_for_walk(walk),
            deadline_at=tip_deadline.isoformat(),
        )

    max_allowed = _tip_max_allowed_for_walk(walk)
    tip_amount = _resolve_tip_amount(payload, max_allowed)
    tip_id = str(uuid.uuid4())
    session_id = f"manual-{tip_id}"
    now_iso = datetime.now(timezone.utc).isoformat()

    tip_doc = {
        "id": tip_id,
        "walk_id": walk_id,
        "client_user_id": str(user.get("id") or ""),
        "client_name": str(walk.get("client_name") or "Cliente"),
        "walker_user_id": str(walk.get("walker_user_id") or ""),
        "walker_id": str(walk.get("walker_id") or ""),
        "walker_name": str(walk.get("walker_name") or "Passeador"),
        "amount": tip_amount,
        "currency": "brl",
        "status": "pending",
        "payment_status": "pending",
        "tip_deadline_at": tip_deadline.isoformat(),
        "paid_at": None,
        "suspicious_flag": False,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await db.tips.insert_one(tip_doc)

    payment_transaction = {
        "id": str(uuid.uuid4()),
        "transaction_type": "tip",
        "tip_id": tip_id,
        "walk_id": walk_id,
        "client_user_id": str(user.get("id") or ""),
        "client_email": str(user.get("email") or ""),
        "walker_user_id": str(walk.get("walker_user_id") or ""),
        "amount": tip_amount,
        "currency": "brl",
        "session_id": session_id,
        "status": "initiated",
        "payment_status": "pending",
        "processed": False,
        "metadata": {
            "transaction_type": "tip",
            "tip_id": tip_id,
            "walk_id": walk_id,
        },
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await db.payment_transactions.insert_one(payment_transaction)

    await db.walks.update_one(
        {"id": walk_id},
        {
            "$set": {
                "tip_id": tip_id,
                "tip_amount": 0.0,
                "tip_status": "pending",
                "tip_paid_at": None,
                "tip_deadline_at": tip_deadline.isoformat(),
                "updated_at": now_iso,
            }
        },
    )

    return TipCheckoutResponse(
        tip_id=tip_id,
        session_id=session_id,
        amount=tip_amount,
        max_allowed_amount=max_allowed,
        deadline_at=tip_deadline.isoformat(),
    )



@api_router.get("/tips/checkout/status/{session_id}", response_model=TipCheckoutStatusResponse)
async def get_tip_checkout_status(session_id: str, request: Request):
    if not _is_feature_active("tips"):
        raise HTTPException(status_code=403, detail="Funcionalidade de gorjetas está desativada")

    await _require_role(request, ["cliente", "passeador", "admin", "super_admin"])
    return await _sync_tip_checkout_status(session_id, request)


@api_router.post("/webhook/stripe")
async def stripe_webhook_handler(request: Request):
    return {"ok": True, "ignored": True, "message": "Webhook Stripe desativado no ambiente local"}


@api_router.get("/walker/tips/summary", response_model=WalkerTipSummaryResponse)
async def get_walker_tips_summary(request: Request):
    if not _is_feature_active("tips"):
        return WalkerTipSummaryResponse(today_total=0.0, month_total=0.0, historical_total=0.0, recent_tips=[])

    user = await _require_role(request, ["passeador"])
    walker_user_id = str(user.get("id") or "")

    tips = await db.tips.find(
        {"status": "paid", "walker_user_id": walker_user_id},
        {"_id": 0},
    ).sort("paid_at", -1).to_list(2000)

    now_dt = datetime.now(timezone.utc)
    day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    today_total = 0.0
    month_total = 0.0
    historical_total = 0.0
    recent_rows: List[WalkerTipEntryResponse] = []

    for tip in tips:
        amount = _coerce_float(tip.get("amount"), 0.0)
        paid_at_dt = _parse_iso_datetime(tip.get("paid_at"))
        historical_total += amount
        if paid_at_dt and paid_at_dt >= month_start:
            month_total += amount
        if paid_at_dt and paid_at_dt >= day_start:
            today_total += amount

        if paid_at_dt and len(recent_rows) < 10:
            recent_rows.append(
                WalkerTipEntryResponse(
                    id=str(tip.get("id")),
                    walk_id=str(tip.get("walk_id")),
                    amount=round(amount, 2),
                    paid_at=paid_at_dt.isoformat(),
                    client_name=str(tip.get("client_name") or "Cliente"),
                )
            )

    return WalkerTipSummaryResponse(
        today_total=round(today_total, 2),
        month_total=round(month_total, 2),
        historical_total=round(historical_total, 2),
        recent_tips=recent_rows,
    )


@api_router.get("/walker/incentives/summary", response_model=WalkerIncentiveSummaryResponse)
async def get_walker_incentives_summary(request: Request):
    user = await _require_role(request, ["passeador"])

    if not _is_feature_active("habit_incentive"):
        checklist_streak = int(user.get("kit_checklist_streak", 0) or 0)
        infractions = int(user.get("kit_missing_reports_count", 0) or 0)
        walker_level = _determine_walker_level(
            _coerce_float(user.get("score_final"), 0.0),
            int(user.get("completed_walks") or 0),
            _coerce_float(user.get("no_show_rate"), 0.0),
            rating_avg=_coerce_float(user.get("rating_weighted_avg"), _coerce_float(user.get("rating_avg"), 0.0)),
            cancel_rate=_coerce_float(user.get("cancel_rate"), 0.0),
            checklist_streak=checklist_streak,
            infractions=infractions,
        )
        return WalkerIncentiveSummaryResponse(
            week_earnings=0.0,
            month_earnings=0.0,
            historical_earnings=0.0,
            week_walks=0,
            month_walks=0,
            active_bonuses=[],
            progress_items=[],
            missions=[],
            recent_bonus_history=[],
            status_label=str(user.get("quality_status") or QUALITY_STATUS_ACTIVE),
            walker_level=walker_level,
            next_level=_next_walker_level(walker_level),
            level_progress_percent=0.0,
            level_priority_bonus=0.0,
            mission_bonus_active=False,
            mission_bonus_value=0.0,
            weekly_tip_total=0.0,
            weekly_tip_goal=WEEKLY_TIP_GOAL_AMOUNT,
            weekly_tip_goal_reached=False,
            gamification_badges=[],
            incentive_messages=["Funcionalidade de incentivos está desativada pelo administrador."],
            rides_to_next_bonus=0,
            earnings_to_next_bonus=0.0,
            weekly_goal_target_amount=0.0,
            weekly_goal_remaining_amount=0.0,
            weekly_goal_progress_percent=0.0,
            critical_mission_bonus_active=False,
            mission_priority_points=0.0,
            high_demand_windows=[],
            ranking_week_position=0,
            ranking_month_position=0,
            ranking_week_top=[],
            ranking_month_top=[],
        )

    walker_user_id = str(user.get("id") or "")
    settings = await _get_incentive_settings_dict()

    all_walks = await db.walks.find({}, {"_id": 0}).to_list(5000)
    walker_walks = [walk for walk in all_walks if _walk_matches_walker_user(walk, user)]

    now_dt = datetime.now(timezone.utc)
    week_start, week_end = _week_bounds(now_dt)
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_key = week_start.date().isoformat()

    completed_walks = [walk for walk in walker_walks if walk.get("status") == STATUS_FINISHED]
    week_walks = [walk for walk in completed_walks if (dt := _walk_datetime_from_doc(walk)) and week_start <= dt < week_end]
    month_walks = [walk for walk in completed_walks if (dt := _walk_datetime_from_doc(walk)) and month_start <= dt <= now_dt]

    tip_rows = await _list_paid_tips_for_walker(
        walker_user_id=str(user.get("id") or ""),
        walker_id=f"partner-{str(user.get('id') or '').strip()}",
        walker_name=str(user.get("full_name") or ""),
        limit=1000,
    )
    tip_total_amount = round(sum(_coerce_float(row.get("amount"), 0.0) for row in tip_rows), 2)
    platform_tip_average = await _platform_tip_average_recent()
    quality_metrics = _compute_reputation_metrics(
        walker_walks,
        str(user.get("quality_status") or QUALITY_STATUS_ACTIVE),
        tip_total_amount,
        tip_rows,
        user,
        platform_tip_average,
    )
    weighted_score = _coerce_float(quality_metrics.get("rating_weighted_avg"), 0.0)
    checklist_streak = int(user.get("kit_checklist_streak", 0) or 0)
    infractions = int(user.get("kit_missing_reports_count", 0) or 0)
    cancel_rate = _coerce_float(quality_metrics.get("cancel_rate"), 0.0)
    week_tip_rows = [
        tip
        for tip in tip_rows
        if (tip_dt := _parse_iso_datetime(tip.get("paid_at") or tip.get("updated_at") or tip.get("created_at")))
        and week_start <= tip_dt < week_end
    ]
    week_tip_total = round(sum(_coerce_float(row.get("amount"), 0.0) for row in week_tip_rows), 2)
    mission_info = _weekly_mission_progress(week_walks, week_tip_rows)
    mission_bonus_active = bool(mission_info.get("completed_all", False))
    critical_mission_bonus_active = int(mission_info.get("critical_acceptance_count", 0) or 0) >= 2

    walker_level = _determine_walker_level(
        _coerce_float(quality_metrics.get("score_final"), 0.0),
        int(quality_metrics.get("completed_walks", len(completed_walks)) or len(completed_walks)),
        _coerce_float(quality_metrics.get("no_show_rate"), 0.0),
        rating_avg=weighted_score,
        cancel_rate=cancel_rate,
        checklist_streak=checklist_streak,
        infractions=infractions,
    )
    next_level = _next_walker_level(walker_level)
    level_progress_percent = _walker_level_progress_percent(
        walker_level,
        _coerce_float(quality_metrics.get("score_final"), 0.0),
        int(quality_metrics.get("completed_walks", len(completed_walks)) or len(completed_walks)),
        _coerce_float(quality_metrics.get("no_show_rate"), 0.0),
        rating_avg=weighted_score,
        cancel_rate=cancel_rate,
        checklist_streak=checklist_streak,
    )
    level_priority_bonus = _walker_level_boost_factor(walker_level)
    weekly_tip_goal_reached = week_tip_total >= WEEKLY_TIP_GOAL_AMOUNT
    gamification_badges = _gamification_badges(quality_metrics, week_tip_total)
    if mission_bonus_active:
        gamification_badges.append("Missão da semana")

    week_completed = len(week_walks)
    streak_days = _consistency_streak_days(walker_walks, now_dt)

    quality_target_walks = int(settings.get("quality_bonus_min_walks") or DEFAULT_QUALITY_BONUS_MIN_WALKS)
    quality_bonus_active = weighted_score >= _coerce_float(settings.get("quality_bonus_min_weighted"), DEFAULT_QUALITY_BONUS_MIN_WEIGHTED) and week_completed >= quality_target_walks
    consistency_target = int(settings.get("consistency_days_required") or DEFAULT_CONSISTENCY_DAYS_REQUIRED)
    consistency_active = streak_days >= consistency_target

    tiers = sorted(settings.get("volume_bonus_tiers") or DEFAULT_VOLUME_BONUS_TIERS, key=lambda row: int(row.get("target_walks", 0)))
    reached_tier = None
    next_tier = None
    for tier in tiers:
        target = int(tier.get("target_walks", 0))
        if week_completed >= target:
            reached_tier = tier
        elif not next_tier:
            next_tier = tier
    if not next_tier and tiers:
        next_tier = tiers[-1]

    if consistency_active:
        await _upsert_bonus_payment(
            walker_user_id=walker_user_id,
            bonus_key=f"consistency:{week_key}",
            bonus_type="consistency_week",
            amount=_coerce_float(settings.get("consistency_bonus_amount"), DEFAULT_CONSISTENCY_BONUS_AMOUNT),
            description="Bônus de consistência semanal",
            week_key=week_key,
        )

    if reached_tier:
        target = int(reached_tier.get("target_walks", 0))
        await _upsert_bonus_payment(
            walker_user_id=walker_user_id,
            bonus_key=f"volume:{week_key}:{target}",
            bonus_type="volume_week",
            amount=_coerce_float(reached_tier.get("amount"), 0.0),
            description=f"Bônus de volume semanal (meta {target} passeios)",
            week_key=week_key,
        )

    bonus_rows = await db.walker_bonus_payments.find({"walker_user_id": walker_user_id, "status": "active"}, {"_id": 0}).sort("created_at", -1).to_list(300)

    def _sum_walks(rows: List[dict]) -> float:
        return round(sum(_coerce_float(row.get("walker_payout_amount"), 0.0) for row in rows), 2)

    week_bonus_total = round(sum(_coerce_float(row.get("amount"), 0.0) for row in bonus_rows if row.get("week_key") == week_key), 2)
    month_bonus_total = round(
        sum(_coerce_float(row.get("amount"), 0.0) for row in bonus_rows if (dt := _parse_iso_datetime(row.get("created_at"))) and month_start <= dt <= now_dt),
        2,
    )
    historical_bonus_total = round(sum(_coerce_float(row.get("amount"), 0.0) for row in bonus_rows), 2)
    mission_cash_bonus = 15.0 if mission_bonus_active else (5.0 if critical_mission_bonus_active else 0.0)
    week_bonus_total = round(week_bonus_total + mission_cash_bonus, 2)
    month_bonus_total = round(month_bonus_total + mission_cash_bonus, 2)
    historical_bonus_total = round(historical_bonus_total + mission_cash_bonus, 2)

    active_bonuses: List[str] = []
    if quality_bonus_active:
        active_bonuses.append("Bônus de qualidade")
    if consistency_active:
        active_bonuses.append("Bônus de consistência")
    if reached_tier:
        active_bonuses.append("Bônus de volume")
    if mission_bonus_active:
        active_bonuses.append("Missão semanal (R$ 15,00)")
    elif critical_mission_bonus_active:
        active_bonuses.append("Missão de horário crítico (R$ 5,00)")
    if weekly_tip_goal_reached:
        active_bonuses.append("Destaque por gorjetas")

    next_target = int((next_tier or {}).get("target_walks", week_completed or 0))
    remaining_for_next = max(0, next_target - week_completed)
    next_bonus_amount = round(_coerce_float((next_tier or {}).get("amount"), 0.0), 2)

    progress_items = [
        WalkerIncentiveProgressResponse(key="quality", label="Meta de qualidade", current=round(weighted_score, 1), target=round(_coerce_float(settings.get("quality_bonus_min_weighted"), DEFAULT_QUALITY_BONUS_MIN_WEIGHTED), 1), percentage=round(min(100.0, (weighted_score / max(0.1, _coerce_float(settings.get("quality_bonus_min_weighted"), DEFAULT_QUALITY_BONUS_MIN_WEIGHTED))) * 100.0), 1)),
        WalkerIncentiveProgressResponse(key="consistency", label="Consistência operacional (dias)", current=float(streak_days), target=float(consistency_target), percentage=round(min(100.0, (streak_days / max(1, consistency_target)) * 100.0), 1)),
        WalkerIncentiveProgressResponse(key="volume", label="Meta semanal", current=float(week_completed), target=float(max(1, next_target)), percentage=round(min(100.0, (week_completed / max(1, next_target)) * 100.0), 1)),
    ]
    mission_items = [
        WalkerIncentiveProgressResponse(
            key=str(item.get("key") or "mission"),
            label=str(item.get("label") or "Missão"),
            current=float(_coerce_float(item.get("current"), 0.0)),
            target=float(_coerce_float(item.get("target"), 1.0)),
            percentage=float(_coerce_float(item.get("percentage"), 0.0)),
        )
        for item in list(mission_info.get("missions") or [])
    ]

    week_walk_earnings = _sum_walks(week_walks)
    ticket_medio_semana = round(week_walk_earnings / max(1, week_completed), 2) if week_completed > 0 else 24.0
    earnings_to_next_bonus = round((remaining_for_next * ticket_medio_semana) + next_bonus_amount, 2)
    weekly_goal_target_amount = round(week_walk_earnings + earnings_to_next_bonus, 2)
    weekly_goal_remaining_amount = round(max(0.0, weekly_goal_target_amount - week_walk_earnings), 2)
    weekly_goal_progress_percent = round(
        min(100.0, (week_walk_earnings / max(0.1, weekly_goal_target_amount)) * 100.0),
        1,
    ) if weekly_goal_target_amount > 0 else 0.0

    all_walker_users = await db.users.find(
        {"role": "passeador", "isActive": {"$ne": False}},
        {
            "_id": 0,
            "id": 1,
            "full_name": 1,
            "quality_metrics": 1,
            "kit_checklist_streak": 1,
            "kit_missing_reports_count": 1,
        },
    ).to_list(1000)
    week_ranking_rows = _build_walker_leaderboard(
        period_start=week_start,
        period_end=week_end,
        walks=all_walks,
        walker_users=all_walker_users,
        limit=10,
    )
    month_ranking_rows = _build_walker_leaderboard(
        period_start=month_start,
        period_end=now_dt + timedelta(seconds=1),
        walks=all_walks,
        walker_users=all_walker_users,
        limit=10,
    )
    ranking_week_position = next(
        (int(row.get("position", 0) or 0) for row in week_ranking_rows if str(row.get("walker_user_id") or "") == walker_user_id),
        0,
    )
    ranking_month_position = next(
        (int(row.get("position", 0) or 0) for row in month_ranking_rows if str(row.get("walker_user_id") or "") == walker_user_id),
        0,
    )
    mission_priority_points = 2.0 if mission_bonus_active else (1.0 if critical_mission_bonus_active else 0.0)

    recent_bonus_history = [
        WalkerBonusEntryResponse(
            id=str(row.get("id") or ""),
            bonus_type=str(row.get("bonus_type") or ""),
            amount=round(_coerce_float(row.get("amount"), 0.0), 2),
            status=str(row.get("status") or "active"),
            created_at=str(row.get("created_at") or now_dt.isoformat()),
            description=str(row.get("description") or "Bônus registrado"),
        )
        for row in bonus_rows[:20]
    ]

    return WalkerIncentiveSummaryResponse(
        week_earnings=round(week_walk_earnings + week_bonus_total, 2),
        month_earnings=round(_sum_walks(month_walks) + month_bonus_total, 2),
        historical_earnings=round(_sum_walks(completed_walks) + historical_bonus_total, 2),
        week_walks=week_completed,
        month_walks=len(month_walks),
        active_bonuses=active_bonuses,
        progress_items=progress_items,
        missions=mission_items,
        recent_bonus_history=recent_bonus_history,
        status_label=str(user.get("quality_status") or QUALITY_STATUS_ACTIVE),
        walker_level=walker_level,
        next_level=next_level,
        level_progress_percent=level_progress_percent,
        level_priority_bonus=level_priority_bonus,
        mission_bonus_active=mission_bonus_active,
        mission_bonus_value=WEEKLY_MISSION_SCORE_BONUS,
        weekly_tip_total=week_tip_total,
        weekly_tip_goal=WEEKLY_TIP_GOAL_AMOUNT,
        weekly_tip_goal_reached=weekly_tip_goal_reached,
        gamification_badges=gamification_badges,
        incentive_messages=[
            f"Faltam {remaining_for_next} passeios para desbloquear bônus" if remaining_for_next > 0 else "Meta de volume atual desbloqueada",
            "Você está entre os melhores da região" if str(user.get("quality_status") or "") == QUALITY_STATUS_PREMIUM else "Continue para subir no ranking",
            f"Você recebeu R$ {week_tip_total:.2f} em gorjetas esta semana",
            "Evite atrasos para manter prioridade",
        ],
        rides_to_next_bonus=remaining_for_next,
        earnings_to_next_bonus=earnings_to_next_bonus,
        weekly_goal_target_amount=weekly_goal_target_amount,
        weekly_goal_remaining_amount=weekly_goal_remaining_amount,
        weekly_goal_progress_percent=weekly_goal_progress_percent,
        critical_mission_bonus_active=critical_mission_bonus_active,
        mission_priority_points=mission_priority_points,
        high_demand_windows=[
            {
                "start": str(item.get("start") or ""),
                "end": str(item.get("end") or ""),
            }
            for item in list(settings.get("critical_windows") or DEFAULT_CRITICAL_WINDOWS)
        ],
        ranking_week_position=ranking_week_position,
        ranking_month_position=ranking_month_position,
        ranking_week_top=[WalkerRankingEntryResponse(**row) for row in week_ranking_rows[:5]],
        ranking_month_top=[WalkerRankingEntryResponse(**row) for row in month_ranking_rows[:5]],
    )


@api_router.get("/admin/tips", response_model=List[AdminTipResponse])
async def get_admin_tips(
    request: Request,
    walker_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    #await _require_admin_permission(request, "pagamentos")

    if not _is_feature_active("tips"):
        return []

    query: Dict[str, Any] = {"status": "paid"}
    if walker_id:
        query["$or"] = [{"walker_user_id": walker_id}, {"walker_id": walker_id}]

    tips = await db.tips.find(query, {"_id": 0}).sort("paid_at", -1).to_list(5000)

    start_dt = _parse_coupon_datetime_input(start_date) if start_date else None
    end_dt = _parse_coupon_datetime_input(end_date, end_of_day=True) if end_date else None

    rows: List[AdminTipResponse] = []
    for tip in tips:
        paid_at = _parse_iso_datetime(tip.get("paid_at"))
        if not paid_at:
            continue
        if start_dt and paid_at < start_dt:
            continue
        if end_dt and paid_at > end_dt:
            continue

        walk_doc = await db.walks.find_one({"id": tip.get("walk_id")}, {"_id": 0, "walk_date": 1})
        rows.append(
            AdminTipResponse(
                id=str(tip.get("id")),
                walk_id=str(tip.get("walk_id")),
                walk_date=str((walk_doc or {}).get("walk_date") or ""),
                walker_id=str(tip.get("walker_user_id") or tip.get("walker_id") or ""),
                walker_name=str(tip.get("walker_name") or "Passeador"),
                client_id=str(tip.get("client_user_id") or ""),
                client_name=str(tip.get("client_name") or "Cliente"),
                amount=round(_coerce_float(tip.get("amount"), 0.0), 2),
                paid_at=paid_at.isoformat(),
                suspicious_flag=bool(tip.get("suspicious_flag", False)),
            )
        )

    return rows


@api_router.post("/walks/{walk_id}/photo", response_model=WalkResponse)
async def upload_walk_photo(walk_id: str, request: Request, file: UploadFile = File(...)):
    user = await _require_role(request, ["cliente", "passeador", "admin"])
    await _get_walk_for_user_or_403(walk_id, user)

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Arquivo deve ser imagem")

    extension = Path(file.filename or "photo.jpg").suffix.lower() or ".jpg"
    if extension not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
        extension = ".jpg"

    file_name = f"{walk_id}-{uuid.uuid4().hex}{extension}"
    destination = UPLOADS_DIR / file_name
    content = await file.read()
    destination.write_bytes(content)

    photo_url = f"/api/walks/{walk_id}/photo-file"
    await db.walks.update_one(
        {"id": walk_id},
        {
            "$set": {
                "photo_url": photo_url,
                "photo_file_name": file_name,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )

    updated_walk = await _get_walk_or_404(walk_id)
    return _to_walk_response(updated_walk)


@api_router.get("/walks/{walk_id}/photo-file")
async def get_walk_photo_file(walk_id: str):
    walk = await _get_walk_or_404(walk_id)
    file_name = walk.get("photo_file_name")

    if not file_name and isinstance(walk.get("photo_url"), str) and walk["photo_url"].startswith("/uploads/"):
        file_name = walk["photo_url"].split("/")[-1]

    if not file_name:
        raise HTTPException(status_code=404, detail="Foto não encontrada")

    file_path = UPLOADS_DIR / file_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo de foto não encontrado")

    return FileResponse(path=file_path)


@api_router.get("/uploads/{file_name}")
async def get_upload_file_legacy(file_name: str):
    file_path = UPLOADS_DIR / file_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(path=file_path)


configured_frontend_origin = os.environ.get("FRONTEND_ORIGIN", "").strip().rstrip("/")
allowed_cors_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8001",
    "http://127.0.0.1:8001",
]
frontend_env_values = dotenv_values(ROOT_DIR.parent / "frontend" / ".env")
frontend_public_origin = str(
    frontend_env_values.get("EXPO_BACKEND_URL")
    or frontend_env_values.get("EXPO_PUBLIC_BACKEND_URL")
    or ""
).strip().rstrip("/")
if frontend_public_origin:
    allowed_cors_origins.append(frontend_public_origin)
if configured_frontend_origin:
    allowed_cors_origins.append(configured_frontend_origin)

CORS_PREVIEW_REGEX = r"https://[a-zA-Z0-9-]+\.preview\.emergentagent\.com"


def _is_cors_origin_allowed(origin: str) -> bool:
    if origin in allowed_cors_origins:
        return True
    return bool(re.fullmatch(CORS_PREVIEW_REGEX, origin))


@app.options("/api/{full_path:path}", include_in_schema=False)
async def handle_preflight_options(full_path: str, request: Request):
    origin = request.headers.get("origin", "").strip().rstrip("/")
    requested_headers = request.headers.get("access-control-request-headers", "*")
    request_host = request.headers.get("host", "").strip().rstrip("/")
    request_origin = f"{request.url.scheme}://{request_host}" if request_host else ""

    if origin and (_is_cors_origin_allowed(origin) or (request_origin and origin == request_origin)):
        allow_origin = origin
    elif configured_frontend_origin:
        allow_origin = configured_frontend_origin
    else:
        allow_origin = allowed_cors_origins[0]

    headers = {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, HEAD, PATCH",
        "Access-Control-Allow-Headers": requested_headers or "*",
        "Vary": "Origin",
    }
    return Response(status_code=204, headers=headers)


app.include_router(api_router)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=allowed_cors_origins,
    allow_origin_regex=CORS_PREVIEW_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def enforce_credentialed_cors_headers(request: Request, call_next):
    response = await call_next(request)
    origin = request.headers.get("origin", "").strip().rstrip("/")
    if not origin:
        return response

    request_host = request.headers.get("host", "").strip().rstrip("/")
    request_origin = f"{request.url.scheme}://{request_host}" if request_host else ""
    if _is_cors_origin_allowed(origin) or (request_origin and origin == request_origin):
        response.headers.setdefault("Access-Control-Allow-Origin", origin)
        response.headers["Access-Control-Allow-Credentials"] = "true"
        vary_header = str(response.headers.get("Vary") or "")
        if "Origin" not in vary_header:
            response.headers["Vary"] = f"{vary_header}, Origin".strip(", ") if vary_header else "Origin"

    return response


@app.middleware("http")
async def admin_route_guard(request: Request, call_next):
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return await call_next(request)

    if request.url.path.startswith("/api/admin"):
        token = _extract_bearer_token(request)
        if not token:
            return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
        try:
            payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
            if payload.get("type") != "access":
                return JSONResponse(status_code=401, content={"detail": "Token inválido"})
            user_id = payload.get("sub")
        except jwt.InvalidTokenError:
            return JSONResponse(status_code=401, content={"detail": "Token inválido"})

        user = await db.users.find_one({"id": user_id}, {"_id": 0})
        if not user or not user.get("isAdmin", False):
            return JSONResponse(status_code=403, content={"detail": "Acesso restrito ao administrador"})
        if user.get("isActive", True) is False:
            return JSONResponse(status_code=403, content={"detail": "Conta administrativa inativa"})

        required_permission = _permission_for_admin_path(request.url.path)
        if required_permission and not _has_admin_permission(user, required_permission):
            return JSONResponse(status_code=403, content={"detail": "Sem permissão para este módulo"})

    return await call_next(request)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def seed_auth_and_indexes():
    await db.users.create_index("email", unique=True)
    await db.login_attempts.create_index("identifier", unique=True)
    await db.password_reset_tokens.create_index("token", unique=True)
    await db.notifications.create_index([("user_id", 1), ("created_at", -1)])
    await db.support_tickets.create_index([("updated_at", -1)])
    await db.admin_action_logs.create_index([("created_at", -1)])
    await db.admin_message_campaigns.create_index([("created_at", -1)])
    await db.walker_requests.create_index("id", unique=True)
    await db.walker_requests.create_index([("status", 1), ("region", 1), ("respond_until", 1)])
    await db.walker_requests.create_index([("matching_request_id", 1), ("status", 1), ("respond_until", 1)])
    await db.matching_requests.create_index("id", unique=True)
    await db.matching_requests.create_index([("status", 1), ("updated_at", -1)])
    await db.matching_requests.create_index([("requested_by_user_id", 1), ("created_at", -1)])
    await db.matching_requests.create_index([("pickup_city_normalized", 1), ("pickup_neighborhood_normalized", 1), ("created_at", -1)])
    await db.matching_requests.create_index([("pickup_city_normalized", 1), ("pickup_neighborhood_normalized", 1), ("status", 1)])
    await db.protected_chat_messages.create_index([("conversation_id", 1), ("created_at", 1)])
    await db.anti_disintermediation_events.create_index([("user_id", 1), ("event_type", 1), ("created_at", -1)])
    await db.walker_alerts.create_index("id", unique=True)
    await db.walker_alerts.create_index([("active", 1), ("region", 1), ("created_at", -1)])
    await db.system_alerts.create_index("alert_id", unique=True)
    await db.system_alerts.create_index("alert_signature")
    await db.system_alerts.create_index([("status", 1), ("nivel_gravidade", 1), ("criado_em", -1)])
    await db.system_alerts.create_index([("user_id", 1), ("criado_em", -1)])
    await db.system_alerts.create_index([("prioridade_score", -1), ("criado_em", -1)])
    await db.system_alert_priority_settings.create_index("id", unique=True)
    await db.system_alert_priority_settings_audit.create_index([("created_at", -1)])
    await db.system_alert_audit.create_index([("alert_id", 1), ("created_at", -1)])
    await db.plan_subscription_intents.create_index("id", unique=True)
    await db.plan_subscription_intents.create_index([("user_id", 1), ("created_at", -1)])
    await db.feature_flags.create_index("feature_name", unique=True)
    await db.feature_flag_audit.create_index([("feature_name", 1), ("updated_at", -1)])
    await db.marketplace_intelligence_settings.create_index("id", unique=True)
    await db.marketplace_intelligence_settings_audit.create_index([("created_at", -1)])
    await db.marketplace_context_snapshots.create_index([("city", 1), ("neighborhood", 1), ("created_at", -1)])
    await db.marketplace_decision_audit.create_index([("request_id", 1), ("created_at", -1)])
    await db.marketplace_decision_audit.create_index([("created_at", -1)])
    await db.walker_level_settings.create_index("id", unique=True)
    await db.walker_level_settings_audit.create_index([("created_at", -1)])
    await db.walker_verification_audit.create_index([("walker_user_id", 1), ("created_at", -1)])
    await db.walker_verification_audit.create_index([("walk_id", 1), ("created_at", -1)])
    await db.reputation_credit_ledger.create_index([("walker_user_id", 1), ("created_at", -1)])
    await db.reputation_credit_ledger.create_index([("walker_user_id", 1), ("event_key", 1)], unique=True)
    await db.operational_settings.create_index("id", unique=True)
    await db.coupons.create_index("id", unique=True)
    await db.coupons.create_index("code", unique=True)
    await db.coupon_redemptions.create_index("id", unique=True)
    await db.coupon_redemptions.create_index([("coupon_id", 1), ("user_id", 1)])
    await db.coupon_redemptions.create_index([("walk_id", 1)])
    await db.coupon_redemptions.create_index([("coupon_id", 1), ("ip_address", 1), ("used_at", -1)])
    await db.coupon_redemptions.create_index([("coupon_id", 1), ("device_id", 1), ("used_at", -1)])
    await db.coupon_fraud_alerts.create_index("id", unique=True)
    await db.coupon_fraud_alerts.create_index([("created_at", -1)])
    await db.anti_abuse_blocks.create_index([("kind", 1), ("value", 1)], unique=True)
    await db.users.create_index("registration_device_id")
    await db.users.create_index([("registration_ip", 1), ("created_at", -1)])
    await db.tips.create_index("id", unique=True)
    await db.tips.create_index([("walk_id", 1), ("status", 1)])
    await db.tips.create_index([("walker_user_id", 1), ("paid_at", -1)])
    await db.tips.create_index([("client_user_id", 1), ("walker_user_id", 1), ("paid_at", -1)])
    await db.payment_transactions.create_index("id", unique=True)
    await db.payment_transactions.create_index("session_id", unique=True)
    await db.payment_transactions.create_index([("transaction_type", 1), ("walk_id", 1), ("created_at", -1)])
    await db.incentive_settings.create_index("id", unique=True)
    await db.incentive_settings_audit.create_index([("created_at", -1)])
    await db.walker_bonus_payments.create_index("id", unique=True)
    await db.walker_bonus_payments.create_index("bonus_key", unique=True)
    await db.walker_bonus_payments.create_index([("walker_user_id", 1), ("created_at", -1)])
    try:
        await db.pet_routine_progress.drop_index("user_id_1")
    except Exception:
        pass
    try:
        await db.pet_routine_progress.drop_index("routine_id_1")
    except Exception:
        pass
    await db.pet_routines.create_index("id", unique=True)
    await db.pet_routines.create_index([("user_id", 1), ("pet_id", 1)], unique=True)
    await db.pet_routines.create_index([("updated_at", -1)])
    await db.pet_routine_progress.create_index("id", unique=True)
    await db.pet_routine_progress.create_index(
        "routine_id",
        unique=True,
        partialFilterExpression={"routine_id": {"$type": "string"}},
    )
    await db.pet_routine_progress.create_index("user_id")
    await db.pet_routine_progress.create_index([("user_id", 1), ("pet_id", 1)])
    await db.pet_routine_progress.create_index([("updated_at", -1)])
    await db.referral_program_settings.create_index("id", unique=True)
    await db.referral_program_settings_audit.create_index([("created_at", -1)])
    await db.referral_codes.create_index("id", unique=True)
    await db.referral_codes.create_index("code", unique=True)
    await db.referral_codes.create_index([("owner_user_id", 1), ("referral_type", 1)], unique=True)
    await db.referrals.create_index("id", unique=True)
    await db.referrals.create_index([("referrer_user_id", 1), ("referral_type", 1), ("created_at", -1)])
    await db.referrals.create_index([("referred_user_id", 1), ("referral_type", 1), ("created_at", -1)])
    await db.referrals.create_index([("status", 1), ("created_at", -1)])

    await db.operational_settings.update_one(
        {"id": "pricing"},
        {
            "$setOnInsert": {
                "id": "pricing",
                "premiumRepassePercentual": DEFAULT_PREMIUM_PAYOUT_PERCENT,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    default_coupons = [
        {
            "id": "coupon-aumigao10",
            "code": "AUMIGAO10",
            "discount_percent": 10.0,
            "discount_fixed": 0.0,
            "valid_from": None,
            "valid_until": None,
            "max_global_uses": 5000,
            "max_uses_per_user": 3,
            "used_count": 0,
            "applicable_walk_types": COUPON_WALK_TYPES.copy(),
            "is_active": True,
            "created_at": now_iso,
            "updated_at": now_iso,
        },
        {
            "id": "coupon-petfixo15",
            "code": "PETFIXO15",
            "discount_percent": 0.0,
            "discount_fixed": 15.0,
            "valid_from": None,
            "valid_until": None,
            "max_global_uses": 5000,
            "max_uses_per_user": 2,
            "used_count": 0,
            "applicable_walk_types": COUPON_WALK_TYPES.copy(),
            "is_active": True,
            "created_at": now_iso,
            "updated_at": now_iso,
        },
    ]
    for coupon_row in default_coupons:
        await db.coupons.update_one(
            {"code": coupon_row["code"]},
            {"$setOnInsert": coupon_row},
            upsert=True,
        )

    await _get_incentive_settings_dict()
    await _get_marketplace_intelligence_settings_dict()
    await _get_walker_level_settings_dict()
    await _get_referral_program_settings_dict()
    await _get_system_alert_priority_settings()
    await _ensure_feature_flags_seeded()

    super_admin_email = os.environ.get("SUPER_ADMIN_EMAIL", "superadmin@petpasso.com").strip().lower()
    super_admin_password = os.environ.get("SUPER_ADMIN_PASSWORD", "SuperAdmin@123").strip()
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@petpasso.com").strip().lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "Admin@123").strip()

    defaults = [
        {
            "full_name": "Super Admin PetPasso",
            "email": super_admin_email,
            "password": super_admin_password,
            "role": "super_admin",
            "permissions": _full_permissions_map(),
            "isActive": True,
        },
        {
            "full_name": "Admin PetPasso",
            "email": admin_email,
            "password": admin_password,
            "role": "admin",
            "permissions": _default_admin_permissions_map(),
            "isActive": True,
        },
        {"full_name": "Cliente Demo", "email": "cliente@petpasso.com", "password": "Cliente@123", "role": "cliente"},
        {
            "full_name": "Passeador Demo",
            "email": "passeador@petpasso.com",
            "password": "Passeador@123",
            "role": "passeador",
            "possuiVeiculo": True,
            "aceitaDeslocamentoPremium": True,
            "raioMaximoPremiumKm": 6.0,
            "ativoParaTransportePremium": True,
            "has_water": True,
            "has_bowl": True,
            "has_bags": True,
            "has_first_aid": True,
            "has_towel": True,
            "has_extra_leash": True,
            "has_premium_items": False,
            "premium_verified_badge_active": False,
            "premium_verified_streak": 0,
            "premium_verified_infractions_consecutive": 0,
            "premium_verified_last_reason": "",
            "is_verified": False,
            "verification_level": VERIFICATION_LEVEL_NONE,
            "verification_score_snapshot": 0,
            "reputation_credits": 0,
            "last_credit_update": None,
            "cr_matching_boost_until": None,
            "cr_early_wave_until": None,
            "cr_visual_highlight_until": None,
            "region": "Salvador/BA",
            "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab"],
            "availability_start_time": "07:30",
            "availability_end_time": "19:00",
            "availability_blocks": [],
            "unavailable_until": None,
            "quality_status": QUALITY_STATUS_ACTIVE,
            "quality_status_reason": "Sem observações",
            "quality_metrics": {
                "rating_avg": 0.0,
                "rating_recent_avg": 0.0,
                "rating_weighted_avg": 0.0,
                "rating_count": 0,
                "accepted_walks": 0,
                "completed_walks": 0,
                "completion_rate": 0.0,
                "punctuality_rate": 0.0,
                "severe_delay_rate": 0.0,
                "no_show_rate": 0.0,
                "cancel_rate": 0.0,
                "score_base": 75.0,
                "score_final": 75.0,
                "score_trend": 0.0,
                "recency_factor": 1.0,
                "consistency_factor": 1.0,
                "severe_penalty_factor": 1.0,
                "status_penalty_factor": 1.0,
                "public_rating_label": "Novo na plataforma",
                "public_badge": "Novo na plataforma",
                "recent_comments": [],
                "encouragement_message": "Mantenha qualidade e pontualidade para subir no ranking de confiança.",
            },
            "quality_monitoring": {
                "active": False,
                "severity": "padrao",
                "target_walks": 7,
                "completed_walks": 0,
                "reset_count": 0,
                "course_completed": False,
                "quiz_passed": False,
                "quiz_attempts": 0,
                "consecutive_quiz_failures": 0,
                "review_recommended": False,
            },
            "quality_history": [],
        },
        {
            "full_name": "Carlos Oliveira",
            "email": "walker@petpasso.com",
            "password": "Walker@123",
            "role": "passeador",
            "isActive": True,
            "status": "Aprovado",
            "approved": True,
            "active_as_walker": True,
            "region": "Salvador/BA",
            "available_for_walks": True,
            "profile_completed": True,
            "phone": "(71) 99999-1234",
            "possuiVeiculo": True,
            "aceitaDeslocamentoPremium": True,
            "raioMaximoPremiumKm": 8.0,
            "ativoParaTransportePremium": True,
            "has_water": True,
            "has_bowl": True,
            "has_bags": True,
            "has_first_aid": True,
            "has_towel": True,
            "has_extra_leash": True,
            "has_premium_items": True,
            "premium_verified_badge_active": False,
            "premium_verified_streak": 0,
            "premium_verified_infractions_consecutive": 0,
            "premium_verified_last_reason": "",
            "is_verified": False,
            "verification_level": VERIFICATION_LEVEL_NONE,
            "verification_score_snapshot": 0,
            "reputation_credits": 0,
            "last_credit_update": None,
            "cr_matching_boost_until": None,
            "cr_early_wave_until": None,
            "cr_visual_highlight_until": None,
            "availability_days": ["seg", "ter", "qua", "qui", "sex", "sab"],
            "availability_start_time": "07:00",
            "availability_end_time": "20:00",
            "availability_blocks": [],
            "unavailable_until": None,
            "quality_status": QUALITY_STATUS_ACTIVE,
            "quality_status_reason": "Sem observações",
            "quality_metrics": {
                "rating_avg": 0.0,
                "rating_recent_avg": 0.0,
                "rating_weighted_avg": 0.0,
                "rating_count": 0,
                "accepted_walks": 0,
                "completed_walks": 0,
                "completion_rate": 0.0,
                "punctuality_rate": 0.0,
                "severe_delay_rate": 0.0,
                "no_show_rate": 0.0,
                "cancel_rate": 0.0,
                "score_base": 75.0,
                "score_final": 75.0,
                "score_trend": 0.0,
                "recency_factor": 1.0,
                "consistency_factor": 1.0,
                "severe_penalty_factor": 1.0,
                "status_penalty_factor": 1.0,
                "public_rating_label": "Novo na plataforma",
                "public_badge": "Novo na plataforma",
                "recent_comments": [],
                "encouragement_message": "Mantenha qualidade e pontualidade para subir no ranking de confiança.",
            },
            "quality_monitoring": {
                "active": False,
                "severity": "padrao",
                "target_walks": 7,
                "completed_walks": 0,
                "reset_count": 0,
                "course_completed": False,
                "quiz_passed": False,
                "quiz_attempts": 0,
                "consecutive_quiz_failures": 0,
                "review_recommended": False,
            },
            "quality_history": [],
        },
    ]

    for item in defaults:
        existing = await db.users.find_one({"email": item["email"]}, {"_id": 0})
        if existing:
            updates = {}
            expected_is_admin = item["role"] in {"admin", "super_admin"}
            if existing.get("full_name") != item["full_name"]:
                updates["full_name"] = item["full_name"]
            if existing.get("role") != item["role"]:
                updates["role"] = item["role"]
            if bool(existing.get("isAdmin", False)) != expected_is_admin:
                updates["isAdmin"] = expected_is_admin
            if expected_is_admin:
                expected_permissions = _normalize_admin_permissions(item.get("permissions", {}))
                if _normalize_admin_permissions(existing.get("permissions", {})) != expected_permissions:
                    updates["permissions"] = expected_permissions
                if existing.get("isActive", True) != item.get("isActive", True):
                    updates["isActive"] = item.get("isActive", True)
                if not existing.get("created_by"):
                    updates["created_by"] = "system"
            else:
                if existing.get("permissions"):
                    updates["permissions"] = _empty_permissions_map()
                if existing.get("isActive", True) is False:
                    updates["isActive"] = True
                for optional_key in [
                    "status",
                    "approved",
                    "active_as_walker",
                    "region",
                    "available_for_walks",
                    "profile_completed",
                    "phone",
                    "possuiVeiculo",
                    "aceitaDeslocamentoPremium",
                    "raioMaximoPremiumKm",
                    "ativoParaTransportePremium",
                    "has_water",
                    "has_bowl",
                    "has_bags",
                    "has_first_aid",
                    "has_towel",
                    "has_extra_leash",
                    "has_premium_items",
                    "premium_verified_badge_active",
                    "premium_verified_streak",
                    "premium_verified_infractions_consecutive",
                    "premium_verified_last_reason",
                    "is_verified",
                    "verification_level",
                    "verification_score_snapshot",
                    "reputation_credits",
                    "last_credit_update",
                    "cr_matching_boost_until",
                    "cr_early_wave_until",
                    "cr_visual_highlight_until",
                    "availability_days",
                    "availability_start_time",
                    "availability_end_time",
                ]:
                    if optional_key in item and existing.get(optional_key) != item.get(optional_key):
                        updates[optional_key] = item.get(optional_key)

                if item["role"] == "passeador":
                    availability_days = _normalize_availability_days(item.get("availability_days", []))
                    availability_start = _normalize_clock(item.get("availability_start_time"), DEFAULT_AVAILABILITY_START_TIME)
                    availability_end = _normalize_clock(item.get("availability_end_time"), DEFAULT_AVAILABILITY_END_TIME)
                    if _clock_to_minutes(availability_end) - _clock_to_minutes(availability_start) < 60:
                        availability_start = DEFAULT_AVAILABILITY_START_TIME
                        availability_end = DEFAULT_AVAILABILITY_END_TIME
                    horarios_disponiveis = _build_horarios_disponiveis(availability_days, availability_start, availability_end)

                    if existing.get("availability_days") != availability_days:
                        updates["availability_days"] = availability_days
                    if existing.get("availability_start_time") != availability_start:
                        updates["availability_start_time"] = availability_start
                    if existing.get("availability_end_time") != availability_end:
                        updates["availability_end_time"] = availability_end
                    if _normalize_horarios_disponiveis(existing.get("horarios_disponiveis")) != horarios_disponiveis:
                        updates["horarios_disponiveis"] = horarios_disponiveis

            existing_hash = str(existing.get("password_hash", ""))
            if not _verify_password(item["password"], existing_hash):
                updates["password_hash"] = _hash_password(item["password"])

            if updates:
                updates["updated_at"] = now_iso
                await db.users.update_one({"id": existing["id"]}, {"$set": updates})
            continue

        user = {
            "id": str(uuid.uuid4()),
            "full_name": item["full_name"],
            "email": item["email"],
            "password_hash": _hash_password(item["password"]),
            "role": item["role"],
            "isAdmin": item["role"] in {"admin", "super_admin"},
            "permissions": _normalize_admin_permissions(item.get("permissions", {})),
            "isActive": item.get("isActive", True),
            "created_by": "system",
            "possuiSeguro": False,
            "accepted_terms": True,
            "accepted_privacy": True,
            "accepted_lgpd": True,
            "created_at": now_iso,
            "updated_at": now_iso,
            "last_active_at": now_iso,
        }
        for optional_key in [
            "status",
            "approved",
            "active_as_walker",
            "region",
            "available_for_walks",
            "profile_completed",
            "phone",
            "possuiVeiculo",
            "aceitaDeslocamentoPremium",
            "raioMaximoPremiumKm",
            "ativoParaTransportePremium",
            "has_water",
            "has_bowl",
            "has_bags",
            "has_first_aid",
            "has_towel",
            "has_extra_leash",
            "has_premium_items",
            "premium_verified_badge_active",
            "premium_verified_streak",
            "premium_verified_infractions_consecutive",
            "premium_verified_last_reason",
            "is_verified",
            "verification_level",
            "verification_score_snapshot",
            "reputation_credits",
            "last_credit_update",
            "cr_matching_boost_until",
            "cr_early_wave_until",
            "cr_visual_highlight_until",
            "availability_days",
            "availability_start_time",
            "availability_end_time",
            "availability_blocks",
            "unavailable_until",
            "quality_status",
            "quality_status_reason",
            "quality_metrics",
            "quality_monitoring",
            "quality_history",
        ]:
            if optional_key in item:
                user[optional_key] = item[optional_key]

        if item["role"] == "passeador":
            availability_days = _normalize_availability_days(item.get("availability_days", []))
            availability_start = _normalize_clock(item.get("availability_start_time"), DEFAULT_AVAILABILITY_START_TIME)
            availability_end = _normalize_clock(item.get("availability_end_time"), DEFAULT_AVAILABILITY_END_TIME)
            if _clock_to_minutes(availability_end) - _clock_to_minutes(availability_start) < 60:
                availability_start = DEFAULT_AVAILABILITY_START_TIME
                availability_end = DEFAULT_AVAILABILITY_END_TIME
            user["availability_days"] = availability_days
            user["availability_start_time"] = availability_start
            user["availability_end_time"] = availability_end
            user["horarios_disponiveis"] = _build_horarios_disponiveis(availability_days, availability_start, availability_end)
            user["availability_blocks"] = _normalize_availability_blocks(item.get("availability_blocks", []))
            user["unavailable_until"] = item.get("unavailable_until")

        await db.users.insert_one(user)

    walker_user = await db.users.find_one({"email": "walker@petpasso.com"}, {"_id": 0})
    client_user = await db.users.find_one({"email": "cliente@petpasso.com"}, {"_id": 0})
    if walker_user and client_user:
        now = datetime.now(timezone.utc)
        walker_photo = _build_avatar_data_uri("#E8F1FF", "#2FBF71")

        scheduled_dt = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        in_progress_dt = now.replace(minute=0, second=0, microsecond=0)
        finished_dt = (now - timedelta(days=1)).replace(hour=17, minute=30, second=0, microsecond=0)

        seed_walks = [
            {
                "id": "walker-seed-agendado",
                "pet_name": "Thor",
                "client_name": client_user.get("full_name", "Cliente Demo"),
                "walk_date": scheduled_dt.strftime("%Y-%m-%d"),
                "walk_time": scheduled_dt.strftime("%H:%M"),
                "duration_minutes": 45,
                "status": STATUS_SCHEDULED,
                "notes": "Levar água para caminhada curta.",
            },
            {
                "id": "walker-seed-andamento",
                "pet_name": "Luna",
                "client_name": client_user.get("full_name", "Cliente Demo"),
                "walk_date": in_progress_dt.strftime("%Y-%m-%d"),
                "walk_time": in_progress_dt.strftime("%H:%M"),
                "duration_minutes": 45,
                "status": STATUS_WALKING_NOW,
                "notes": "Pet com energia alta, manter ritmo constante.",
            },
            {
                "id": "walker-seed-finalizado",
                "pet_name": "Nina",
                "client_name": client_user.get("full_name", "Cliente Demo"),
                "walk_date": finished_dt.strftime("%Y-%m-%d"),
                "walk_time": finished_dt.strftime("%H:%M"),
                "duration_minutes": 30,
                "status": STATUS_FINISHED,
                "did_pee": True,
                "did_poop": False,
                "summary_text": "Nina passeou com tranquilidade e retornou bem.",
                "notes": "Passeio concluído sem intercorrências.",
            },
        ]

        for item in seed_walks:
            walk_doc = {
                "id": item["id"],
                "pet_name": item["pet_name"],
                "pet_ids": [],
                "shared_pet_names": [],
                "shared_client_names": [],
                "shared_owner_keys": [],
                "participant_user_ids": [client_user.get("id", "")],
                "client_user_id": client_user.get("id"),
                "client_name": item["client_name"],
                "walk_type": WALK_TYPE_INDIVIDUAL,
                "shared_context": None,
                "shared_approved": False,
                "shared_group": None,
                "walk_date": item["walk_date"],
                "walk_time": item["walk_time"],
                "duration_minutes": item["duration_minutes"],
                "walker_id": "partner-carlos-oliveira",
                "walker_user_id": walker_user.get("id"),
                "walker_name": walker_user.get("full_name", "Carlos Oliveira"),
                "walker_photo_url": walker_photo,
                "pickup_street": "Rua das Acácias",
                "pickup_number": "120",
                "pickup_neighborhood": "Pituba",
                "pickup_complement": "Apto 302",
                "location_reference": "Próximo à praça central",
                "security_code": _generate_security_code(),
                "did_pee": item.get("did_pee", False),
                "did_poop": item.get("did_poop", False),
                "rating": 5 if item["status"] == STATUS_FINISHED else None,
                "rating_comment": "Ótimo passeio" if item["status"] == STATUS_FINISHED else "",
                "summary_text": item.get("summary_text", ""),
                "pet_behavior_notes": "Pet sociável e acostumado com passeios.",
                "notes": item.get("notes", ""),
                "motivoCancelamento": "",
                "tipoCancelamento": None,
                "penalidadePercentual": 0,
                "status": item["status"],
                "photo_url": None,
                "walk_datetime_iso": _validate_datetime_iso(item["walk_date"], item["walk_time"]),
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            base_price, walker_payout = _calculate_walk_pricing(walk_doc)
            walk_doc["base_price"] = base_price
            walk_doc["walker_payout"] = walker_payout
            await db.walks.update_one({"id": walk_doc["id"]}, {"$set": walk_doc}, upsert=True)

        request_now = datetime.now(timezone.utc)
        seed_requests = [
            {
                "id": "walker-request-1",
                "pet_name": "Max",
                "client_name": client_user.get("full_name", "Cliente Demo"),
                "client_user_id": client_user.get("id"),
                "neighborhood": "Pituba",
                "approx_location": "Praça Ana Lúcia Magalhães",
                "walk_date": (request_now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "walk_time": "11:00",
                "duration_minutes": 45,
                "walk_type": WALK_TYPE_INDIVIDUAL,
                "region": walker_user.get("region", "Salvador/BA"),
                "status": "pending",
                "target_walker_user_id": walker_user.get("id"),
                "respond_until": (request_now + timedelta(minutes=10)).isoformat(),
                "created_at": now_iso,
                "updated_at": now_iso,
                "notes": "Pet dócil e acostumado ao trajeto.",
            },
            {
                "id": "walker-request-2",
                "pet_name": "Bidu",
                "client_name": client_user.get("full_name", "Cliente Demo"),
                "client_user_id": client_user.get("id"),
                "neighborhood": "Rio Vermelho",
                "approx_location": "Largo da Mariquita",
                "walk_date": (request_now + timedelta(days=1)).strftime("%Y-%m-%d"),
                "walk_time": "15:30",
                "duration_minutes": 30,
                "walk_type": WALK_TYPE_SHARED,
                "region": walker_user.get("region", "Salvador/BA"),
                "status": "pending",
                "target_walker_user_id": walker_user.get("id"),
                "respond_until": (request_now + timedelta(minutes=10)).isoformat(),
                "created_at": now_iso,
                "updated_at": now_iso,
                "notes": "Possível compartilhamento com pet compatível.",
            },
        ]
        for request_row in seed_requests:
            await db.walker_requests.update_one({"id": request_row["id"]}, {"$set": request_row}, upsert=True)

        alert_row = {
            "id": "walker-alert-1",
            "title": "Você tem um passeio em 30 minutos",
            "message": "Prepare os itens essenciais e confirme o código de segurança.",
            "tone": "warning",
            "icon": "time-outline",
            "active": True,
            "region": walker_user.get("region", "Salvador/BA"),
            "target_walker_user_id": walker_user.get("id"),
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        await db.walker_alerts.update_one({"id": alert_row["id"]}, {"$set": alert_row}, upsert=True)

        seed_notifications = [
            {
                "id": "walker-notification-1",
                "user_id": walker_user.get("id"),
                "role": "passeador",
                "title": "Novo passeio disponível próximo a você",
                "message": "Solicitação em Pituba aguardando resposta.",
                "category": "walker_request",
                "read": False,
                "created_at": now_iso,
            },
            {
                "id": "walker-notification-2",
                "user_id": walker_user.get("id"),
                "role": "passeador",
                "title": "Atualize o status do passeio em andamento",
                "message": "Mantenha o tutor informado durante todo o trajeto.",
                "category": "operational",
                "read": False,
                "created_at": now_iso,
            },
        ]
        for notification in seed_notifications:
            await db.notifications.update_one({"id": notification["id"]}, {"$set": notification}, upsert=True)

    walker_rows = await db.users.find({"role": "passeador"}, {"_id": 0, "id": 1}).to_list(length=500)
    for walker_row in walker_rows:
        walker_id = str(walker_row.get("id") or "").strip()
        if not walker_id:
            continue
        await _recalculate_walker_verification_for_user(
            walker_user_id=walker_id,
            trigger="startup_recalculation",
            walk_id=None,
        )


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

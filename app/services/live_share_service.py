"""Helpers puros do compartilhamento de passeio ao vivo.

Sem I/O de rede/DB — só lógica testável: derivação de expiração, ofuscação de
origem (privacidade ~200m) e extração do 1º nome do pet.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from typing import Optional


def is_live_share_enabled() -> bool:
    """Flag global de rollout. Default OFF (mesmo padrão de PRICING_V2_ENABLED)."""
    return os.getenv("LIVE_SHARE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def pet_first_name(full_name: Optional[str]) -> str:
    """Primeiro token do nome do pet (privacidade: não expõe nome completo)."""
    if not full_name:
        return ""
    parts = full_name.strip().split()
    return parts[0] if parts else ""


def compute_share_expiry(
    walk, *, grace_minutes: int = 120, now: Optional[datetime] = None, tz_name: Optional[str] = None
) -> datetime:
    """Fim previsto do passeio + folga, em UTC naive (comparável a utcnow).

    O modelo Walk não persiste started_at/ended_at — deriva de scheduled_date
    (hora LOCAL do tenant, convertida via app.lib.walk_time; sem a conversão o
    link expirava ~3h mais cedo). Se scheduled_date não parsear, usa `now`
    (ou utcnow) como base.
    """
    from app.lib.walk_time import walk_start_utc

    duration = int(getattr(walk, "duration_minutes", 0) or 0)
    base = walk_start_utc(getattr(walk, "scheduled_date", None), tz_name)
    if base is None:
        base = now or datetime.utcnow()
    return base + timedelta(minutes=duration + grace_minutes)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em metros entre dois pontos (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def obfuscate_origin(pings: list[dict], *, radius_m: float = 200.0) -> list[dict]:
    """Remove pings a menos de `radius_m` do ponto de partida (1º ping).

    Protege o endereço de retirada: o trajeto público só 'começa' a ~200m de casa.
    `pings` deve vir ordenado por recorded_at asc; cada item tem latitude/longitude.
    """
    if not pings:
        return []
    origin = pings[0]
    olat, olon = float(origin["latitude"]), float(origin["longitude"])
    return [
        p for p in pings
        if _haversine_m(olat, olon, float(p["latitude"]), float(p["longitude"])) > radius_m
    ]

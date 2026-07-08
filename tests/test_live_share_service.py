from datetime import datetime

from app.services.live_share_service import (
    compute_share_expiry,
    obfuscate_origin,
    pet_first_name,
    is_live_share_enabled,
)


class _Walk:
    def __init__(self, scheduled_date, duration_minutes):
        self.scheduled_date = scheduled_date
        self.duration_minutes = duration_minutes


def test_pet_first_name_takes_first_token():
    assert pet_first_name("Rex do Carmo") == "Rex"
    assert pet_first_name("  Bolt ") == "Bolt"
    assert pet_first_name("") == ""
    assert pet_first_name(None) == ""


def test_compute_share_expiry_adds_duration_and_grace():
    walk = _Walk("2026-06-12T14:00:00", 45)
    exp = compute_share_expiry(walk, grace_minutes=120)
    # scheduled_date é hora LOCAL (America/Bahia, UTC-3): 14:00 local = 17:00 UTC.
    # 17:00 + 45min + 120min = 19:45 UTC (fix fuso 08/07).
    assert exp == datetime(2026, 6, 12, 19, 45, 0)


def test_compute_share_expiry_fallback_when_unparseable():
    walk = _Walk("nao-e-data", 30)
    exp = compute_share_expiry(walk, grace_minutes=120, now=datetime(2026, 1, 1, 10, 0, 0))
    # fallback: now + duration + grace = 10:00 + 30 + 120 = 12:30
    assert exp == datetime(2026, 1, 1, 12, 30, 0)


def test_obfuscate_origin_drops_pings_within_radius():
    # origem em (0,0); ~111km por grau de latitude.
    pings = [
        {"latitude": 0.0, "longitude": 0.0, "recorded_at": "t0"},       # 0 m -> dropado
        {"latitude": 0.001, "longitude": 0.0, "recorded_at": "t1"},     # ~111 m -> dropado
        {"latitude": 0.003, "longitude": 0.0, "recorded_at": "t2"},     # ~333 m -> mantido
    ]
    out = obfuscate_origin(pings, radius_m=200.0)
    assert len(out) == 1
    assert out[0]["recorded_at"] == "t2"


def test_obfuscate_origin_empty_is_safe():
    assert obfuscate_origin([], radius_m=200.0) == []


def test_is_live_share_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("LIVE_SHARE_ENABLED", "true")
    assert is_live_share_enabled() is True
    monkeypatch.setenv("LIVE_SHARE_ENABLED", "false")
    assert is_live_share_enabled() is False
    monkeypatch.delenv("LIVE_SHARE_ENABLED", raising=False)
    assert is_live_share_enabled() is False  # default OFF

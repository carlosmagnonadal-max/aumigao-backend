"""Pricing v2: 2 planos canônicos (Pro/Enterprise) + take-rate de REDE.

Cobre:
- canonical_plan_v2: mapeamento legado (starter/business→pro, enterprise→enterprise)
- commission_default_for_plan: take-rate próprio via v2 (Pro 10% / Enterprise 5%)
- network_commission_default_for_plan: take-rate de REDE (Pro 18% / Enterprise 10%)
- resolve_network_take_rate: expõe taxa de rede via serviço
- is_network_walk: detecta passeio de rede pelo TenantWalkerAccess
- get_commission_percent_for_walk: integração completa (rede vs próprio, override por par)
- Passeio próprio NÃO usa taxa de rede e vice-versa
"""
import os
import importlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra tabelas no Base.metadata
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _tenant(db, tid, plan):
    db.add(Tenant(id=tid, name=tid, slug=tid, status="active", plan=plan))
    db.commit()


def _user(db, uid, role="walker"):
    db.add(User(id=uid, full_name="u", email=f"{uid}@x.com", role=role, password_hash="x"))
    db.commit()


def _twa(db, tenant_id, walker_id, access_type="shared_network", status="active", commission_percent=None):
    from decimal import Decimal
    twa = TenantWalkerAccess(
        tenant_id=tenant_id,
        walker_user_id=walker_id,
        access_type=access_type,
        status=status,
    )
    if commission_percent is not None:
        twa.commission_percent = Decimal(str(commission_percent))
    db.add(twa)
    db.commit()


# ---------------------------------------------------------------------------
# 1. canonical_plan_v2 — mapeamento legado
# ---------------------------------------------------------------------------

def test_canonical_plan_starter_maps_to_pro():
    from app.models.tenant_payment_config import canonical_plan_v2, TENANT_PLAN_PRO
    assert canonical_plan_v2("starter") == TENANT_PLAN_PRO


def test_canonical_plan_business_maps_to_pro():
    from app.models.tenant_payment_config import canonical_plan_v2, TENANT_PLAN_PRO
    assert canonical_plan_v2("business") == TENANT_PLAN_PRO


def test_canonical_plan_enterprise_maps_to_enterprise():
    from app.models.tenant_payment_config import canonical_plan_v2, TENANT_PLAN_ENTERPRISE_V2
    assert canonical_plan_v2("enterprise") == TENANT_PLAN_ENTERPRISE_V2


def test_canonical_plan_pro_maps_to_pro():
    from app.models.tenant_payment_config import canonical_plan_v2, TENANT_PLAN_PRO
    assert canonical_plan_v2("pro") == TENANT_PLAN_PRO


def test_canonical_plan_none_maps_to_pro():
    from app.models.tenant_payment_config import canonical_plan_v2, TENANT_PLAN_PRO
    assert canonical_plan_v2(None) == TENANT_PLAN_PRO


def test_canonical_plan_unknown_maps_to_pro():
    from app.models.tenant_payment_config import canonical_plan_v2, TENANT_PLAN_PRO
    assert canonical_plan_v2("gold") == TENANT_PLAN_PRO


def test_canonical_plan_case_insensitive():
    from app.models.tenant_payment_config import canonical_plan_v2, TENANT_PLAN_PRO
    assert canonical_plan_v2("STARTER") == TENANT_PLAN_PRO
    assert canonical_plan_v2("Business") == TENANT_PLAN_PRO


# ---------------------------------------------------------------------------
# 2. network_commission_default_for_plan — take-rate de REDE (sempre v2)
# ---------------------------------------------------------------------------

def test_network_rate_pro_is_18():
    from app.models.tenant_payment_config import network_commission_default_for_plan
    assert network_commission_default_for_plan("pro") == 18.0


def test_network_rate_enterprise_is_10():
    from app.models.tenant_payment_config import network_commission_default_for_plan
    assert network_commission_default_for_plan("enterprise") == 10.0


def test_network_rate_legacy_starter_maps_to_pro_18():
    from app.models.tenant_payment_config import network_commission_default_for_plan
    assert network_commission_default_for_plan("starter") == 18.0


def test_network_rate_legacy_business_maps_to_pro_18():
    from app.models.tenant_payment_config import network_commission_default_for_plan
    assert network_commission_default_for_plan("business") == 18.0


def test_network_rate_legacy_enterprise_is_10():
    from app.models.tenant_payment_config import network_commission_default_for_plan
    assert network_commission_default_for_plan("enterprise") == 10.0


def test_network_rate_none_plan_falls_back_to_18():
    from app.models.tenant_payment_config import network_commission_default_for_plan
    assert network_commission_default_for_plan(None) == 18.0


# ---------------------------------------------------------------------------
# 3. resolve_network_take_rate — via serviço
# ---------------------------------------------------------------------------

def test_resolve_network_take_rate_pro():
    from app.services.payment_split_service import resolve_network_take_rate
    assert resolve_network_take_rate("pro") == 18.0


def test_resolve_network_take_rate_enterprise():
    from app.services.payment_split_service import resolve_network_take_rate
    assert resolve_network_take_rate("enterprise") == 10.0


def test_resolve_network_take_rate_starter_legacy():
    from app.services.payment_split_service import resolve_network_take_rate
    assert resolve_network_take_rate("starter") == 18.0


def test_resolve_network_take_rate_business_legacy():
    from app.services.payment_split_service import resolve_network_take_rate
    assert resolve_network_take_rate("business") == 18.0


# ---------------------------------------------------------------------------
# 4. is_network_walk
# ---------------------------------------------------------------------------

def test_is_network_walk_true_for_shared_network():
    from app.services.payment_split_service import is_network_walk
    db = _db()
    _tenant(db, "t1", "pro")
    _user(db, "w1")
    _twa(db, "t1", "w1", access_type="shared_network", status="active")
    assert is_network_walk(db, "t1", "w1") is True


def test_is_network_walk_true_for_tenant_exclusive():
    from app.services.payment_split_service import is_network_walk
    db = _db()
    _tenant(db, "t2", "pro")
    _user(db, "w2")
    _twa(db, "t2", "w2", access_type="tenant_exclusive", status="active")
    assert is_network_walk(db, "t2", "w2") is True


def test_is_network_walk_false_when_no_twa():
    from app.services.payment_split_service import is_network_walk
    db = _db()
    _tenant(db, "t3", "pro")
    _user(db, "w3")
    # Sem TenantWalkerAccess → walker é próprio do tenant
    assert is_network_walk(db, "t3", "w3") is False


def test_is_network_walk_false_when_status_not_active():
    from app.services.payment_split_service import is_network_walk
    db = _db()
    _tenant(db, "t4", "pro")
    _user(db, "w4")
    _twa(db, "t4", "w4", access_type="shared_network", status="pending")
    assert is_network_walk(db, "t4", "w4") is False


def test_is_network_walk_false_when_tenant_id_none():
    from app.services.payment_split_service import is_network_walk
    db = _db()
    assert is_network_walk(db, None, "w5") is False


def test_is_network_walk_false_when_walker_id_none():
    from app.services.payment_split_service import is_network_walk
    db = _db()
    _tenant(db, "t6", "pro")
    assert is_network_walk(db, "t6", None) is False


# ---------------------------------------------------------------------------
# 5. Passeio próprio NÃO usa taxa de rede; passeio de rede USA taxa de rede.
# ---------------------------------------------------------------------------

def test_own_walk_uses_own_rate_not_network_rate():
    """Walker sem TenantWalkerAccess → taxa própria (10% para pro em v2 habilitado,
    mas com PRICING_V2_ENABLED=False legado → 10% fallback para plano desconhecido 'pro'
    ou o default do plano).
    Aqui testamos is_network_walk=False e que get_commission_percent_for_walk
    NÃO retorna a taxa de rede (18% pro pro)."""
    from app.services.payment_split_service import get_commission_percent_for_walk, is_network_walk
    db = _db()
    _tenant(db, "t-own", "pro")
    _user(db, "w-own")
    # Sem vínculo de rede → is_network_walk deve ser False
    assert is_network_walk(db, "t-own", "w-own") is False
    # Taxa resultante NÃO deve ser 18% (taxa de rede do Pro)
    rate = get_commission_percent_for_walk(db, "t-own", walker_id="w-own")
    assert rate != 18.0, f"Passeio próprio não deve usar taxa de rede 18%, obteve {rate}"


def test_network_walk_uses_network_rate_not_own_rate():
    """Walker com TenantWalkerAccess shared_network → taxa de REDE."""
    from app.services.payment_split_service import get_commission_percent_for_walk
    db = _db()
    _tenant(db, "t-net", "pro")
    _user(db, "w-net")
    _twa(db, "t-net", "w-net", access_type="shared_network", status="active")
    rate = get_commission_percent_for_walk(db, "t-net", walker_id="w-net")
    # Taxa de rede para pro = 18%
    assert rate == 18.0, f"Passeio de rede (pro) deve usar 18%, obteve {rate}"


def test_network_walk_enterprise_uses_10_percent():
    """Walker de rede com tenant enterprise → taxa de rede 10%."""
    from app.services.payment_split_service import get_commission_percent_for_walk
    db = _db()
    _tenant(db, "t-ent-net", "enterprise")
    _user(db, "w-ent-net")
    _twa(db, "t-ent-net", "w-ent-net", access_type="shared_network", status="active")
    rate = get_commission_percent_for_walk(db, "t-ent-net", walker_id="w-ent-net")
    assert rate == 10.0, f"Passeio de rede (enterprise) deve usar 10%, obteve {rate}"


def test_network_walk_legacy_starter_tenant_uses_18_percent():
    """Walker de rede com tenant starter (legado) → mapeado para Pro → taxa de rede 18%."""
    from app.services.payment_split_service import get_commission_percent_for_walk
    db = _db()
    _tenant(db, "t-st-net", "starter")
    _user(db, "w-st-net")
    _twa(db, "t-st-net", "w-st-net", access_type="shared_network", status="active")
    rate = get_commission_percent_for_walk(db, "t-st-net", walker_id="w-st-net")
    assert rate == 18.0, f"Passeio de rede (starter→pro) deve usar 18%, obteve {rate}"


def test_network_walk_legacy_business_tenant_uses_18_percent():
    """Walker de rede com tenant business (legado) → mapeado para Pro → taxa de rede 18%."""
    from app.services.payment_split_service import get_commission_percent_for_walk
    db = _db()
    _tenant(db, "t-bz-net", "business")
    _user(db, "w-bz-net")
    _twa(db, "t-bz-net", "w-bz-net", access_type="shared_network", status="active")
    rate = get_commission_percent_for_walk(db, "t-bz-net", walker_id="w-bz-net")
    assert rate == 18.0, f"Passeio de rede (business→pro) deve usar 18%, obteve {rate}"


# ---------------------------------------------------------------------------
# 6. Override por par tem prioridade sobre taxa de rede
# ---------------------------------------------------------------------------

def test_pair_override_beats_network_rate():
    """TenantWalkerAccess.commission_percent negociado prevalece sobre taxa de rede."""
    from app.services.payment_split_service import get_commission_percent_for_walk
    db = _db()
    _tenant(db, "t-ov", "enterprise")
    _user(db, "w-ov")
    # Walker de rede mas com override negociado de 3%
    _twa(db, "t-ov", "w-ov", access_type="shared_network", status="active", commission_percent=3.0)
    rate = get_commission_percent_for_walk(db, "t-ov", walker_id="w-ov")
    assert rate == 3.0, f"Override por par deve prevalecer, obteve {rate}"


# ---------------------------------------------------------------------------
# 7. Retrocompatibilidade: get_commission_percent sem walker_id = sem regressão
# ---------------------------------------------------------------------------

def test_get_commission_percent_without_walker_id_unchanged():
    """get_commission_percent sem walker_id não deve mudar comportamento legado."""
    from app.services.payment_split_service import get_commission_percent
    db = _db()
    _tenant(db, "t-leg", "business")
    # PRICING_V2_ENABLED=false → legado → business = 8%
    rate = get_commission_percent(db, "t-leg")
    assert rate == 8.0


def test_get_commission_percent_for_walk_no_walker_id_no_regression():
    """get_commission_percent_for_walk sem walker_id cai na taxa própria legada."""
    from app.services.payment_split_service import get_commission_percent_for_walk
    db = _db()
    _tenant(db, "t-leg2", "starter")
    rate = get_commission_percent_for_walk(db, "t-leg2")
    assert rate == 12.0

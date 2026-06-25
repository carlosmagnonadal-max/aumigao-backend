"""Pricing v2: 2 planos canônicos (Pro/Enterprise) + take-rate de REDE.

Cobre:
- canonical_plan_v2: mapeamento legado (starter/business→pro, enterprise→enterprise)
- commission_default_for_plan: take-rate próprio via v2 (Pro 10% / Enterprise 5%)
- network_commission_default_for_plan: take-rate de REDE (Pro 18% / Enterprise 10%)
- resolve_network_take_rate: expõe taxa de rede via serviço
- is_network_walk: detecta passeio de rede pelo TenantWalkerAccess
- get_commission_percent_for_walk: gated por PRICING_V2_ENABLED
  - OFF (default): ≡ get_commission_percent — walks de rede usam taxa legada
  - ON:  ramo de rede ativo (18/10%), override por par tem prioridade
- build_payment_split: delegado a get_commission_percent_for_walk (wire-up)
- Capabilities v2 (2 planos) atrás de PRICING_V2_ENABLED
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
# 5. PRICING_V2_ENABLED=False (default): get_commission_percent_for_walk ≡
#    get_commission_percent — ramo de rede NÃO ativado.
# ---------------------------------------------------------------------------

def test_own_walk_uses_own_rate_not_network_rate_flag_off():
    """Walker sem TenantWalkerAccess → is_network_walk=False → taxa própria legada.
    Com flag OFF, o ramo de rede está desativado — confirm is_network_walk=False."""
    from app.services.payment_split_service import get_commission_percent_for_walk, is_network_walk
    db = _db()
    _tenant(db, "t-own", "pro")
    _user(db, "w-own")
    # Sem vínculo de rede → is_network_walk deve ser False
    assert is_network_walk(db, "t-own", "w-own") is False
    # Taxa resultante NÃO deve ser 18% (taxa de rede do Pro)
    rate = get_commission_percent_for_walk(db, "t-own", walker_id="w-own")
    assert rate != 18.0, f"Passeio próprio não deve usar taxa de rede 18%, obteve {rate}"


def test_network_walk_flag_off_uses_legacy_commission():
    """FLAG OFF + walker de rede → comportamento LEGADO (get_commission_percent).

    Com PRICING_V2_ENABLED=False, is_network_walk é True mas o ramo de rede é
    ignorado → taxa legada do plano (12% para starter)."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import get_commission_percent_for_walk
    # Garantir flag OFF para este teste
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = False
    try:
        db = _db()
        _tenant(db, "t-net-off", "starter")
        _user(db, "w-net-off")
        _twa(db, "t-net-off", "w-net-off", access_type="shared_network", status="active")
        rate = get_commission_percent_for_walk(db, "t-net-off", walker_id="w-net-off")
        # Legado: starter = 12% (PLAN_COMMISSION_DEFAULTS), NÃO 18% (rede v2)
        assert rate == 12.0, f"Flag OFF + rede deve usar taxa legada 12%, obteve {rate}"
    finally:
        svc_mod._PRICING_V2_ENABLED = original


def test_network_walk_enterprise_flag_off_uses_legacy_commission():
    """FLAG OFF + walker de rede enterprise → taxa legada 5% (enterprise legado)."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import get_commission_percent_for_walk
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = False
    try:
        db = _db()
        _tenant(db, "t-ent-off", "enterprise")
        _user(db, "w-ent-off")
        _twa(db, "t-ent-off", "w-ent-off", access_type="shared_network", status="active")
        rate = get_commission_percent_for_walk(db, "t-ent-off", walker_id="w-ent-off")
        # Legado enterprise = 5%, NÃO 10% (rede v2)
        assert rate == 5.0, f"Flag OFF + rede enterprise deve usar taxa legada 5%, obteve {rate}"
    finally:
        svc_mod._PRICING_V2_ENABLED = original


def test_network_walk_business_flag_off_uses_legacy_commission():
    """FLAG OFF + walker de rede business → taxa legada 8% (business legado)."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import get_commission_percent_for_walk
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = False
    try:
        db = _db()
        _tenant(db, "t-bz-off", "business")
        _user(db, "w-bz-off")
        _twa(db, "t-bz-off", "w-bz-off", access_type="shared_network", status="active")
        rate = get_commission_percent_for_walk(db, "t-bz-off", walker_id="w-bz-off")
        # Legado business = 8%, NÃO 18% (rede v2)
        assert rate == 8.0, f"Flag OFF + rede business deve usar taxa legada 8%, obteve {rate}"
    finally:
        svc_mod._PRICING_V2_ENABLED = original


# ---------------------------------------------------------------------------
# 6. PRICING_V2_ENABLED=True: ramo de rede ativo (18/10%), override por par
#    tem prioridade.
# ---------------------------------------------------------------------------

def test_network_walk_flag_on_uses_network_rate_pro():
    """FLAG ON + walker de rede pro → taxa de REDE 18%."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import get_commission_percent_for_walk
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = True
    try:
        db = _db()
        _tenant(db, "t-net-on", "pro")
        _user(db, "w-net-on")
        _twa(db, "t-net-on", "w-net-on", access_type="shared_network", status="active")
        rate = get_commission_percent_for_walk(db, "t-net-on", walker_id="w-net-on")
        assert rate == 18.0, f"Flag ON + rede pro deve usar 18%, obteve {rate}"
    finally:
        svc_mod._PRICING_V2_ENABLED = original


def test_network_walk_flag_on_enterprise_uses_10_percent():
    """FLAG ON + walker de rede enterprise → taxa de rede 10%."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import get_commission_percent_for_walk
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = True
    try:
        db = _db()
        _tenant(db, "t-ent-net-on", "enterprise")
        _user(db, "w-ent-net-on")
        _twa(db, "t-ent-net-on", "w-ent-net-on", access_type="shared_network", status="active")
        rate = get_commission_percent_for_walk(db, "t-ent-net-on", walker_id="w-ent-net-on")
        assert rate == 10.0, f"Flag ON + rede enterprise deve usar 10%, obteve {rate}"
    finally:
        svc_mod._PRICING_V2_ENABLED = original


def test_network_walk_flag_on_legacy_starter_maps_to_18():
    """FLAG ON + starter legado + rede → mapeado para Pro → taxa de rede 18%."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import get_commission_percent_for_walk
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = True
    try:
        db = _db()
        _tenant(db, "t-st-net-on", "starter")
        _user(db, "w-st-net-on")
        _twa(db, "t-st-net-on", "w-st-net-on", access_type="shared_network", status="active")
        rate = get_commission_percent_for_walk(db, "t-st-net-on", walker_id="w-st-net-on")
        assert rate == 18.0, f"Flag ON + rede (starter→pro) deve usar 18%, obteve {rate}"
    finally:
        svc_mod._PRICING_V2_ENABLED = original


def test_network_walk_flag_on_legacy_business_maps_to_18():
    """FLAG ON + business legado + rede → mapeado para Pro → taxa de rede 18%."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import get_commission_percent_for_walk
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = True
    try:
        db = _db()
        _tenant(db, "t-bz-net-on", "business")
        _user(db, "w-bz-net-on")
        _twa(db, "t-bz-net-on", "w-bz-net-on", access_type="shared_network", status="active")
        rate = get_commission_percent_for_walk(db, "t-bz-net-on", walker_id="w-bz-net-on")
        assert rate == 18.0, f"Flag ON + rede (business→pro) deve usar 18%, obteve {rate}"
    finally:
        svc_mod._PRICING_V2_ENABLED = original


def test_pair_override_beats_network_rate_flag_on():
    """FLAG ON: TenantWalkerAccess.commission_percent negociado prevalece sobre taxa de rede."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import get_commission_percent_for_walk
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = True
    try:
        db = _db()
        _tenant(db, "t-ov-on", "enterprise")
        _user(db, "w-ov-on")
        # Walker de rede mas com override negociado de 3%
        _twa(db, "t-ov-on", "w-ov-on", access_type="shared_network", status="active", commission_percent=3.0)
        rate = get_commission_percent_for_walk(db, "t-ov-on", walker_id="w-ov-on")
        assert rate == 3.0, f"Override por par deve prevalecer, obteve {rate}"
    finally:
        svc_mod._PRICING_V2_ENABLED = original


# ---------------------------------------------------------------------------
# 7. build_payment_split wire-up: com flag OFF → zero-regressão em call sites
# ---------------------------------------------------------------------------

def test_build_split_flag_off_network_walk_uses_legacy():
    """build_payment_split + flag OFF + walk de rede → comissão legada (não v2)."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import build_payment_split
    from app.models.tenant_payment_config import TenantPaymentConfig
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = False
    try:
        db = _db()
        _tenant(db, "t-bs-off", "starter")
        _user(db, "w-bs-off")
        _twa(db, "t-bs-off", "w-bs-off", access_type="shared_network", status="active")
        # Sem TenantPaymentConfig → fallback do plano legado (starter=12%)
        split = build_payment_split(db, "t-bs-off", 100.0, walker_id="w-bs-off")
        assert split["commission_percent"] == 12.0, (
            f"Flag OFF + rede deve usar taxa legada 12%, obteve {split['commission_percent']}"
        )
        assert split["walker_amount"] == 88.0
    finally:
        svc_mod._PRICING_V2_ENABLED = original


def test_build_split_flag_off_no_walker_id_unchanged():
    """build_payment_split sem walker_id (call site shared_walk / admin) → zero-regressão."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import build_payment_split
    from app.models.tenant_payment_config import TenantPaymentConfig
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = False
    try:
        db = _db()
        _tenant(db, "t-bs-nw", "business")
        # Configura TenantPaymentConfig com 20%
        db.add(TenantPaymentConfig(tenant_id="t-bs-nw", commission_percent=20.0, active=True))
        db.commit()
        split = build_payment_split(db, "t-bs-nw", 100.0)
        assert split["commission_percent"] == 20.0
        assert split["walker_amount"] == 80.0
    finally:
        svc_mod._PRICING_V2_ENABLED = original


def test_build_split_flag_on_network_walk_uses_v2_rate():
    """build_payment_split + flag ON + walk de rede → taxa de rede v2 (18%)."""
    import app.services.payment_split_service as svc_mod
    from app.services.payment_split_service import build_payment_split
    original = svc_mod._PRICING_V2_ENABLED
    svc_mod._PRICING_V2_ENABLED = True
    try:
        db = _db()
        _tenant(db, "t-bs-on", "starter")
        _user(db, "w-bs-on")
        _twa(db, "t-bs-on", "w-bs-on", access_type="shared_network", status="active")
        split = build_payment_split(db, "t-bs-on", 100.0, walker_id="w-bs-on")
        # Flag ON + starter (→ pro v2) + rede → 18%
        assert split["commission_percent"] == 18.0, (
            f"Flag ON + rede deve usar 18%, obteve {split['commission_percent']}"
        )
        assert split["walker_amount"] == 82.0
    finally:
        svc_mod._PRICING_V2_ENABLED = original


# ---------------------------------------------------------------------------
# 8. Retrocompatibilidade: get_commission_percent sem walker_id = sem regressão
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


# ---------------------------------------------------------------------------
# 9. Capabilities v2 (get_plan_capabilities) atrás de PRICING_V2_ENABLED
# ---------------------------------------------------------------------------

def test_capabilities_flag_off_starter_unchanged():
    """Flag OFF: get_plan_capabilities('starter') = legado (max_units=1, dedicated_app_allowed=False)."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = False
    try:
        caps = plan_svc.get_plan_capabilities("starter")
        assert caps["max_units"] == 1
        assert caps["dedicated_app_allowed"] is False
        assert caps["network_access_available"] is False
    finally:
        plan_svc._PRICING_V2_ENABLED = original


def test_capabilities_flag_off_business_unchanged():
    """Flag OFF: get_plan_capabilities('business') = legado (max_units=2, dedicated_app_allowed=True)."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = False
    try:
        caps = plan_svc.get_plan_capabilities("business")
        assert caps["max_units"] == 2
        assert caps["dedicated_app_allowed"] is True
        assert caps["max_units_with_addon"] == 3
    finally:
        plan_svc._PRICING_V2_ENABLED = original


def test_capabilities_flag_off_enterprise_unchanged():
    """Flag OFF: get_plan_capabilities('enterprise') = legado (max_units=None, dedicated_app_required=True)."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = False
    try:
        caps = plan_svc.get_plan_capabilities("enterprise")
        assert caps["max_units"] is None
        assert caps["dedicated_app_required"] is True
        assert caps["custom_projects_allowed"] is True
    finally:
        plan_svc._PRICING_V2_ENABLED = original


def test_capabilities_flag_on_pro_v2():
    """Flag ON: get_plan_capabilities('pro') = v2 (max_units=2, dedicated_app_allowed=True, required=False)."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = True
    try:
        caps = plan_svc.get_plan_capabilities("pro")
        assert caps["max_units"] == 2
        assert caps["dedicated_app_allowed"] is True
        assert caps["dedicated_app_required"] is False  # add-on, não automático
        assert caps["network_access_available"] is True
    finally:
        plan_svc._PRICING_V2_ENABLED = original


def test_capabilities_flag_on_enterprise_v2():
    """Flag ON: get_plan_capabilities('enterprise') = v2 (max_units=4, dedicated_app_required=True)."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = True
    try:
        caps = plan_svc.get_plan_capabilities("enterprise")
        assert caps["max_units"] == 4
        assert caps["dedicated_app_allowed"] is True
        assert caps["dedicated_app_required"] is True   # incluído no plano
        assert caps["custom_projects_allowed"] is True
    finally:
        plan_svc._PRICING_V2_ENABLED = original


def test_capabilities_flag_on_legacy_starter_maps_to_pro():
    """Flag ON: chave legada 'starter' → mapeada para Pro v2."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = True
    try:
        caps_starter = plan_svc.get_plan_capabilities("starter")
        caps_pro = plan_svc.get_plan_capabilities("pro")
        assert caps_starter == caps_pro
    finally:
        plan_svc._PRICING_V2_ENABLED = original


def test_capabilities_flag_on_legacy_business_maps_to_pro():
    """Flag ON: chave legada 'business' → mapeada para Pro v2."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = True
    try:
        caps_business = plan_svc.get_plan_capabilities("business")
        caps_pro = plan_svc.get_plan_capabilities("pro")
        assert caps_business == caps_pro
    finally:
        plan_svc._PRICING_V2_ENABLED = original


def test_capabilities_flag_on_dedicated_app_not_included_in_pro():
    """Flag ON + Pro: dedicated_app é add-on (allowed=True, required=False) — NÃO automático."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = True
    try:
        caps = plan_svc.get_plan_capabilities("pro")
        # dedicated_app_allowed=True significa que PODE contratar como add-on
        assert caps["dedicated_app_allowed"] is True
        # dedicated_app_required=False significa que NÃO vem automaticamente no plano
        assert caps["dedicated_app_required"] is False
    finally:
        plan_svc._PRICING_V2_ENABLED = original


def test_capabilities_flag_on_dedicated_app_included_in_enterprise():
    """Flag ON + Enterprise: dedicated_app incluído (allowed=True, required=True)."""
    import app.services.tenant_plan_service as plan_svc
    original = plan_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = True
    try:
        caps = plan_svc.get_plan_capabilities("enterprise")
        assert caps["dedicated_app_allowed"] is True
        assert caps["dedicated_app_required"] is True
    finally:
        plan_svc._PRICING_V2_ENABLED = original


# ---------------------------------------------------------------------------
# 10. Capabilities v2: commercial_service (normalize, catalog, runtime)
# ---------------------------------------------------------------------------

def test_commercial_normalize_flag_on_maps_legacy_to_pro():
    """Flag ON: normalize_commercial_plan('starter') e 'business' → 'pro'."""
    import app.services.tenant_commercial_service as comm_svc
    import app.services.tenant_plan_service as plan_svc
    orig_plan = plan_svc._PRICING_V2_ENABLED
    orig_comm = comm_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = True
    comm_svc._PRICING_V2_ENABLED = True
    try:
        assert comm_svc.normalize_commercial_plan("starter") == "pro"
        assert comm_svc.normalize_commercial_plan("business") == "pro"
        assert comm_svc.normalize_commercial_plan("enterprise") == "enterprise"
    finally:
        plan_svc._PRICING_V2_ENABLED = orig_plan
        comm_svc._PRICING_V2_ENABLED = orig_comm


def test_commercial_normalize_flag_off_unchanged():
    """Flag OFF: normalize_commercial_plan usa catálogo v1."""
    import app.services.tenant_commercial_service as comm_svc
    import app.services.tenant_plan_service as plan_svc
    orig_plan = plan_svc._PRICING_V2_ENABLED
    orig_comm = comm_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = False
    comm_svc._PRICING_V2_ENABLED = False
    try:
        assert comm_svc.normalize_commercial_plan("starter") == "starter"
        assert comm_svc.normalize_commercial_plan("business") == "business"
        assert comm_svc.normalize_commercial_plan("enterprise") == "enterprise"
        assert comm_svc.normalize_commercial_plan(None) == "starter"
    finally:
        plan_svc._PRICING_V2_ENABLED = orig_plan
        comm_svc._PRICING_V2_ENABLED = orig_comm


def test_commercial_catalog_flag_on_returns_two_plans():
    """Flag ON: get_commercial_plans() retorna pro e enterprise (2 planos)."""
    import app.services.tenant_commercial_service as comm_svc
    import app.services.tenant_plan_service as plan_svc
    orig_plan = plan_svc._PRICING_V2_ENABLED
    orig_comm = comm_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = True
    comm_svc._PRICING_V2_ENABLED = True
    try:
        result = comm_svc.get_commercial_plans()
        keys = [p["key"] for p in result["plans"]]
        assert keys == ["pro", "enterprise"]
        # Pro: dedicated_app=False (add-on, não incluído no plano base)
        pro = next(p for p in result["plans"] if p["key"] == "pro")
        assert pro["capabilities"]["dedicated_app"] is False
        # Enterprise: dedicated_app=True (incluído)
        ent = next(p for p in result["plans"] if p["key"] == "enterprise")
        assert ent["capabilities"]["dedicated_app"] is True
    finally:
        plan_svc._PRICING_V2_ENABLED = orig_plan
        comm_svc._PRICING_V2_ENABLED = orig_comm


def test_commercial_catalog_flag_off_returns_three_plans():
    """Flag OFF: get_commercial_plans() retorna starter/business/enterprise (3 planos) — zero-regressão."""
    import app.services.tenant_commercial_service as comm_svc
    import app.services.tenant_plan_service as plan_svc
    orig_plan = plan_svc._PRICING_V2_ENABLED
    orig_comm = comm_svc._PRICING_V2_ENABLED
    plan_svc._PRICING_V2_ENABLED = False
    comm_svc._PRICING_V2_ENABLED = False
    try:
        result = comm_svc.get_commercial_plans()
        keys = [p["key"] for p in result["plans"]]
        assert keys == ["starter", "business", "enterprise"]
    finally:
        plan_svc._PRICING_V2_ENABLED = orig_plan
        comm_svc._PRICING_V2_ENABLED = orig_comm

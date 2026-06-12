from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tenant import (
    Tenant,
    TenantBranding,
    TenantFeature,
    TenantSettings,
    TenantUnit,
)
from app.models.tenant_onboarding import TenantOnboarding
from app.services import tenant_feature_runtime_service as svc
from app.services.tenant_feature_runtime_service import PRODUCT_RUNTIME_FEATURE_KEYS, RUNTIME_FEATURE_KEYS
from app.services.tenant_plan_service import DEFAULT_ON_FEATURE_KEYS

ALL_RUNTIME_KEYS = (*RUNTIME_FEATURE_KEYS, *PRODUCT_RUNTIME_FEATURE_KEYS)

# Mapa de defaults esperados para as chaves de produto (Fase 3 T1).
_DEFAULT_PRODUCT_RUNTIME = {
    key: (key in DEFAULT_ON_FEATURE_KEYS)
    for key in PRODUCT_RUNTIME_FEATURE_KEYS
}
# Chaves comerciais (RUNTIME_FEATURE_KEYS) sempre iniciam False.
EXPECTED_DEFAULT_RUNTIME = {key: False for key in RUNTIME_FEATURE_KEYS}
EXPECTED_DEFAULT_RUNTIME.update(_DEFAULT_PRODUCT_RUNTIME)


def _db():
    engine = create_engine("sqlite:///:memory:")
    # Tenant + TenantFeature cover the runtime logic; the rest are required only
    # by the get_default_tenant -> ensure_default_tenant fallback path.
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantFeature.__table__,
            TenantBranding.__table__,
            TenantSettings.__table__,
            TenantOnboarding.__table__,
            TenantUnit.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _tenant(db, *, plan="starter", tenant_id="t1", slug="aumigao") -> Tenant:
    tenant = Tenant(id=tenant_id, name="Aumigao", slug=slug, status="active", plan=plan)
    db.add(tenant)
    db.commit()
    return tenant


def _feature(db, tenant_id: str, feature_key: str, *, enabled: bool, limit_value=None):
    db.add(
        TenantFeature(
            tenant_id=tenant_id,
            feature_key=feature_key,
            enabled=enabled,
            limit_value=limit_value,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# get_default_feature_runtime
# ---------------------------------------------------------------------------


def test_get_default_feature_runtime_all_false():
    # Fase 3 T1: chaves default-on agora iniciam True; chaves comerciais e verified_walkers False.
    runtime = svc.get_default_feature_runtime()
    assert set(runtime.keys()) == set(ALL_RUNTIME_KEYS)
    assert runtime == EXPECTED_DEFAULT_RUNTIME


def test_get_default_feature_runtime_returns_fresh_dict():
    a = svc.get_default_feature_runtime()
    a["network_access"] = True
    b = svc.get_default_feature_runtime()
    assert b["network_access"] is False


# ---------------------------------------------------------------------------
# get_tenant_feature_runtime - base (plan) gating
# ---------------------------------------------------------------------------


def test_runtime_starter_plan_all_disabled():
    """starter plan allows none of the commercial capabilities at the base level.
    Fase 3 T1: product default-on keys (tips, protected_chat, etc.) ainda retornam True
    pois sao gated por TenantFeature (default-on), nao por plano.
    """
    db = _db()
    tenant = _tenant(db, plan="starter")
    runtime = svc.get_tenant_feature_runtime(db, tenant=tenant)
    assert runtime["tenant_id"] == tenant.id
    features = runtime["features"]
    # Chaves comerciais continuam False para starter.
    for key in RUNTIME_FEATURE_KEYS:
        assert features[key] is False, f"Commercial key {key!r} should be False for starter"
    # Chaves default-on retornam True (sem linha na tabela → default-on).
    for key in DEFAULT_ON_FEATURE_KEYS:
        assert features[key] is True, f"Default-on product key {key!r} should be True"
    # verified_walkers continua False (default-off).
    assert features["verified_walkers"] is False


def test_runtime_enterprise_plan_all_enabled_without_overrides():
    """enterprise plan allows all base caps AND tenant caps default to plan caps.
    Fase 3 T1: inclui chaves novas default-on.
    """
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    runtime = svc.get_tenant_feature_runtime(db, tenant=tenant)
    features = runtime["features"]
    # Chaves comerciais: enterprise → True
    assert features["network_access"] is True
    assert features["dedicated_app"] is True
    assert features["custom_products"] is True
    assert features["custom_projects"] is True
    # verified_walkers: default-off
    assert features["verified_walkers"] is False
    # Chaves default-on: True
    for key in DEFAULT_ON_FEATURE_KEYS:
        assert features[key] is True, f"{key!r} should be True (default-on) for enterprise"


def test_runtime_business_plan_partial():
    """business plan: network/dedicated/custom_products allowed, custom_projects not.
    Fase 3 T1: inclui chaves novas default-on.
    """
    db = _db()
    tenant = _tenant(db, plan="business")
    runtime = svc.get_tenant_feature_runtime(db, tenant=tenant)
    features = runtime["features"]
    assert features["network_access"] is True
    assert features["dedicated_app"] is True
    assert features["custom_products"] is True
    assert features["custom_projects"] is False
    assert features["verified_walkers"] is False
    for key in DEFAULT_ON_FEATURE_KEYS:
        assert features[key] is True, f"{key!r} should be True (default-on) for business"


def test_runtime_unknown_plan_falls_back_to_starter():
    """Plano desconhecido: comportamento de starter para chaves comerciais.
    Fase 3 T1: chaves default-on ainda retornam True (gated por TenantFeature, nao por plano).
    """
    db = _db()
    tenant = _tenant(db, plan="totally_unknown_plan")
    runtime = svc.get_tenant_feature_runtime(db, tenant=tenant)
    features = runtime["features"]
    # Chaves comerciais: False (plano desconhecido → starter)
    for key in RUNTIME_FEATURE_KEYS:
        assert features[key] is False
    # Chaves default-on: True
    for key in DEFAULT_ON_FEATURE_KEYS:
        assert features[key] is True


# ---------------------------------------------------------------------------
# get_tenant_feature_runtime - tenant override interaction (base AND tenant)
# ---------------------------------------------------------------------------


def test_runtime_tenant_override_disables_within_allowing_plan():
    """enterprise allows network_access at base, but a disabled TenantFeature turns it off."""
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    _feature(db, tenant.id, "network_access", enabled=False)
    runtime = svc.get_tenant_feature_runtime(db, tenant=tenant)
    assert runtime["features"]["network_access"] is False
    # untouched features stay enabled
    assert runtime["features"]["dedicated_app"] is True


def test_runtime_tenant_override_cannot_enable_beyond_plan():
    """starter base forbids network_access; enabling the TenantFeature does NOT grant it
    because the result is base AND tenant."""
    db = _db()
    tenant = _tenant(db, plan="starter")
    _feature(db, tenant.id, "network_access", enabled=True)
    runtime = svc.get_tenant_feature_runtime(db, tenant=tenant)
    assert runtime["features"]["network_access"] is False


def test_runtime_enabled_when_base_and_tenant_both_allow():
    db = _db()
    tenant = _tenant(db, plan="business")
    _feature(db, tenant.id, "network_access", enabled=True)
    runtime = svc.get_tenant_feature_runtime(db, tenant=tenant)
    assert runtime["features"]["network_access"] is True


# ---------------------------------------------------------------------------
# _resolve_tenant via get_tenant_feature_runtime
# ---------------------------------------------------------------------------


def test_runtime_resolves_by_tenant_id():
    db = _db()
    tenant = _tenant(db, plan="enterprise", tenant_id="abc", slug="loja-x")
    runtime = svc.get_tenant_feature_runtime(db, tenant_id="abc")
    assert runtime["tenant_id"] == "abc"
    assert runtime["features"]["dedicated_app"] is True


def test_runtime_resolves_by_slug():
    db = _db()
    tenant = _tenant(db, plan="business", tenant_id="abc", slug="loja-y")
    runtime = svc.get_tenant_feature_runtime(db, tenant_id="loja-y")
    assert runtime["tenant_id"] == tenant.id
    assert runtime["features"]["custom_products"] is True


def test_runtime_explicit_tenant_takes_precedence_over_id():
    db = _db()
    t_business = _tenant(db, plan="business", tenant_id="b1", slug="b-slug")
    t_enterprise = _tenant(db, plan="enterprise", tenant_id="e1", slug="e-slug")
    # tenant arg wins even though tenant_id points elsewhere
    runtime = svc.get_tenant_feature_runtime(db, tenant_id="b1", tenant=t_enterprise)
    assert runtime["tenant_id"] == "e1"
    assert runtime["features"]["custom_projects"] is True


def test_runtime_resolves_default_tenant_when_not_found():
    """When tenant_id is unknown, falls back to get_default_tenant which ensures the
    seeded 'aumigao' enterprise tenant (features default disabled at the TenantFeature
    level, but base AND tenant => still False for commercial runtime keys).
    Fase 3 T1: chaves default-on retornam True (gated por TenantFeature, nao por plano).
    """
    db = _db()
    # No tenant created; service must seed default 'aumigao'
    runtime = svc.get_tenant_feature_runtime(db, tenant_id="does-not-exist")
    seeded = db.query(Tenant).filter(Tenant.slug == "aumigao").first()
    assert seeded is not None
    assert runtime["tenant_id"] == seeded.id
    features = runtime["features"]
    # Chaves comerciais: False (seeded com TenantFeature enabled=False)
    for key in RUNTIME_FEATURE_KEYS:
        assert features[key] is False, f"Commercial key {key!r} should be False for seeded tenant"
    # Chaves default-on: True (sem linha na tabela para o tenant semeado → default-on)
    for key in DEFAULT_ON_FEATURE_KEYS:
        assert features[key] is True, f"Default-on key {key!r} should be True for seeded tenant"


def test_runtime_tenant_id_current_falls_back_to_default():
    db = _db()
    runtime = svc.get_tenant_feature_runtime(db, tenant_id="current")
    seeded = db.query(Tenant).filter(Tenant.slug == "aumigao").first()
    assert seeded is not None
    assert runtime["tenant_id"] == seeded.id


# ---------------------------------------------------------------------------
# is_tenant_feature_enabled
# ---------------------------------------------------------------------------


def test_is_feature_enabled_true():
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    assert svc.is_tenant_feature_enabled(db, "dedicated_app", tenant=tenant) is True


def test_is_feature_enabled_false_when_base_blocks():
    db = _db()
    tenant = _tenant(db, plan="starter")
    assert svc.is_tenant_feature_enabled(db, "dedicated_app", tenant=tenant) is False


def test_is_feature_enabled_unknown_key_returns_false():
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    assert svc.is_tenant_feature_enabled(db, "not_a_real_feature", tenant=tenant) is False


def test_is_feature_enabled_empty_key_returns_false():
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    assert svc.is_tenant_feature_enabled(db, "", tenant=tenant) is False
    assert svc.is_tenant_feature_enabled(db, None, tenant=tenant) is False


def test_is_feature_enabled_strips_whitespace():
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    assert svc.is_tenant_feature_enabled(db, "  dedicated_app  ", tenant=tenant) is True


def test_is_feature_enabled_respects_tenant_override():
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    _feature(db, tenant.id, "custom_projects", enabled=False)
    assert svc.is_tenant_feature_enabled(db, "custom_projects", tenant=tenant) is False

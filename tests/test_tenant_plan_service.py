import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tenant import Tenant, TenantFeature, TenantUnit
from app.services import tenant_plan_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantFeature.__table__,
            TenantUnit.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _tenant(db, *, plan="starter", tid="t1", slug="aumigao") -> Tenant:
    tenant = Tenant(id=tid, name="Aumigao", slug=slug, status="active", plan=plan)
    db.add(tenant)
    db.commit()
    return tenant


def _feature(db, tenant_id, key, *, enabled=False, limit_value=None):
    f = TenantFeature(
        tenant_id=tenant_id, feature_key=key, enabled=enabled, limit_value=limit_value
    )
    db.add(f)
    db.commit()
    return f


def _unit(db, tenant_id, name="Unidade"):
    u = TenantUnit(tenant_id=tenant_id, name=name, status="active")
    db.add(u)
    db.commit()
    return u


# --------------------------------------------------------------------------
# get_plan_capabilities
# --------------------------------------------------------------------------


def test_get_plan_capabilities_starter():
    caps = svc.get_plan_capabilities("starter")
    assert caps["max_units"] == 1
    assert caps["max_units_with_addon"] == 1
    assert caps["dedicated_app_allowed"] is False
    assert caps["network_access_available"] is False
    assert caps["custom_products_allowed"] is False
    assert caps["powered_by_required"] is True
    assert caps["onboarding_mode"] == "self_service"


def test_get_plan_capabilities_business():
    caps = svc.get_plan_capabilities("business")
    assert caps["max_units"] == 2
    assert caps["max_units_with_addon"] == 3
    assert caps["dedicated_app_allowed"] is True
    assert caps["network_access_available"] is True
    assert caps["custom_products_allowed"] is True
    assert caps["custom_projects_allowed"] is False
    assert caps["onboarding_mode"] == "assisted"


def test_get_plan_capabilities_enterprise():
    caps = svc.get_plan_capabilities("enterprise")
    assert caps["max_units"] is None
    assert caps["max_units_with_addon"] is None
    assert caps["dedicated_app_required"] is True
    assert caps["custom_projects_allowed"] is True
    assert caps["onboarding_mode"] == "consultative"


def test_get_plan_capabilities_unknown_plan_falls_back_to_starter():
    caps = svc.get_plan_capabilities("nonexistent")
    assert caps == svc.get_plan_capabilities("starter")


def test_get_plan_capabilities_none_plan_falls_back_to_starter():
    caps = svc.get_plan_capabilities(None)
    assert caps == svc.get_plan_capabilities("starter")


def test_get_plan_capabilities_returns_deepcopy_isolated():
    caps = svc.get_plan_capabilities("starter")
    caps["max_units"] = 999
    fresh = svc.get_plan_capabilities("starter")
    assert fresh["max_units"] == 1


# --------------------------------------------------------------------------
# tenant_has_feature
# --------------------------------------------------------------------------


def test_tenant_has_feature_from_plan_baseline():
    db = _db()
    tenant = _tenant(db, plan="business")
    # business has network_access_available True by plan baseline
    assert svc.tenant_has_feature(tenant, db, "network_access") is True
    # starter-only False feature
    assert svc.tenant_has_feature(tenant, db, "custom_projects") is False


def test_tenant_has_feature_starter_dedicated_app_false():
    db = _db()
    tenant = _tenant(db, plan="starter")
    assert svc.tenant_has_feature(tenant, db, "dedicated_app") is False


def test_tenant_has_feature_enabled_by_tenant_feature_flag():
    db = _db()
    tenant = _tenant(db, plan="starter")
    # starter does not have dedicated_app; enabling the feature flips capability
    _feature(db, tenant.id, "dedicated_app", enabled=True)
    assert svc.tenant_has_feature(tenant, db, "dedicated_app") is True


def test_tenant_has_feature_disabled_by_tenant_feature_flag():
    db = _db()
    tenant = _tenant(db, plan="business")
    # business has network_access by plan, but explicit disabled flag overrides
    _feature(db, tenant.id, "network_access", enabled=False)
    assert svc.tenant_has_feature(tenant, db, "network_access") is False


def test_tenant_has_feature_generic_product_feature():
    db = _db()
    tenant = _tenant(db, plan="starter")
    # non-commercial product feature, enabled with no limit -> True
    _feature(db, tenant.id, "recurring_plans", enabled=True)
    assert svc.tenant_has_feature(tenant, db, "recurring_plans") is True


def test_tenant_has_feature_generic_product_feature_disabled():
    db = _db()
    tenant = _tenant(db, plan="starter")
    _feature(db, tenant.id, "recurring_plans", enabled=False)
    assert svc.tenant_has_feature(tenant, db, "recurring_plans") is False


def test_tenant_has_feature_unknown_returns_false():
    db = _db()
    tenant = _tenant(db, plan="starter")
    assert svc.tenant_has_feature(tenant, db, "totally_unknown") is False


# --------------------------------------------------------------------------
# enforce_tenant_feature_allowed (commercial features gated by plan)
# --------------------------------------------------------------------------


def test_enforce_feature_allowed_passes_when_plan_grants():
    db = _db()
    tenant = _tenant(db, plan="business")
    # business allows dedicated_app and custom_products
    assert svc.enforce_tenant_feature_allowed(tenant, db, "dedicated_app") is None
    assert svc.enforce_tenant_feature_allowed(tenant, db, "custom_products") is None


def test_enforce_feature_allowed_raises_403_when_plan_denies():
    db = _db()
    tenant = _tenant(db, plan="starter")
    with pytest.raises(HTTPException) as exc:
        svc.enforce_tenant_feature_allowed(tenant, db, "dedicated_app")
    assert exc.value.status_code == 403
    assert "dedicated_app" in exc.value.detail


def test_enforce_feature_allowed_custom_projects_denied_for_business():
    db = _db()
    tenant = _tenant(db, plan="business")
    with pytest.raises(HTTPException) as exc:
        svc.enforce_tenant_feature_allowed(tenant, db, "custom_projects")
    assert exc.value.status_code == 403


def test_enforce_feature_allowed_enterprise_allows_custom_projects():
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    assert svc.enforce_tenant_feature_allowed(tenant, db, "custom_projects") is None


def test_enforce_feature_allowed_ignores_non_commercial_feature():
    db = _db()
    tenant = _tenant(db, plan="starter")
    # not in ENFORCED_COMMERCIAL_FEATURES -> short-circuits, no raise
    assert svc.enforce_tenant_feature_allowed(tenant, db, "recurring_plans") is None


def test_enforce_feature_allowed_ignores_empty_key():
    db = _db()
    tenant = _tenant(db, plan="starter")
    assert svc.enforce_tenant_feature_allowed(tenant, db, "") is None
    assert svc.enforce_tenant_feature_allowed(tenant, db, None) is None


def test_enforce_feature_allowed_uses_plan_baseline_not_tenant_flag():
    db = _db()
    tenant = _tenant(db, plan="starter")
    # Even with a tenant feature flag enabling dedicated_app, the commercial
    # gate checks ONLY the plan baseline (get_plan_capabilities), so it still 403s.
    _feature(db, tenant.id, "dedicated_app", enabled=True)
    with pytest.raises(HTTPException) as exc:
        svc.enforce_tenant_feature_allowed(tenant, db, "dedicated_app")
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# enforce_tenant_product_feature (non-commercial, gated by tenant flag)
# --------------------------------------------------------------------------


def test_enforce_product_feature_passes_when_enabled():
    db = _db()
    tenant = _tenant(db, plan="starter")
    _feature(db, tenant.id, "recurring_plans", enabled=True)
    assert (
        svc.enforce_tenant_product_feature(tenant, db, "recurring_plans", "Planos recorrentes")
        is None
    )


def test_enforce_product_feature_raises_403_when_disabled():
    db = _db()
    tenant = _tenant(db, plan="starter")
    with pytest.raises(HTTPException) as exc:
        svc.enforce_tenant_product_feature(tenant, db, "recurring_plans", "Planos recorrentes")
    assert exc.value.status_code == 403
    assert "Planos recorrentes" in exc.value.detail


def test_enforce_product_feature_explicit_disabled_flag():
    db = _db()
    tenant = _tenant(db, plan="starter")
    _feature(db, tenant.id, "recurring_plans", enabled=False)
    with pytest.raises(HTTPException) as exc:
        svc.enforce_tenant_product_feature(tenant, db, "recurring_plans", "Planos")
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# can_add_tenant_unit
# --------------------------------------------------------------------------


def test_can_add_unit_enterprise_unlimited():
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    # enterprise max_units_with_addon is None -> always True
    for _ in range(5):
        _unit(db, tenant.id)
    assert svc.can_add_tenant_unit(tenant, db) is True


def test_can_add_unit_starter_limit_one():
    db = _db()
    tenant = _tenant(db, plan="starter")
    # limit 1, zero units -> can add
    assert svc.can_add_tenant_unit(tenant, db) is True
    _unit(db, tenant.id)
    # now at limit -> cannot add
    assert svc.can_add_tenant_unit(tenant, db) is False


def test_can_add_unit_business_limit_three_with_addon():
    db = _db()
    tenant = _tenant(db, plan="business")
    _unit(db, tenant.id)
    _unit(db, tenant.id)
    # 2 < 3 -> can still add
    assert svc.can_add_tenant_unit(tenant, db) is True
    _unit(db, tenant.id)
    # 3 == 3 -> cannot add
    assert svc.can_add_tenant_unit(tenant, db) is False


def test_can_add_unit_respects_tenant_feature_limit_override():
    db = _db()
    tenant = _tenant(db, plan="starter")
    # override limit via tenant feature to 2 (limit_value sets capability via _coerce_limit)
    _feature(db, tenant.id, "max_units_with_addon", enabled=True, limit_value="2")
    _unit(db, tenant.id)
    assert svc.can_add_tenant_unit(tenant, db) is True
    _unit(db, tenant.id)
    assert svc.can_add_tenant_unit(tenant, db) is False


def test_can_add_unit_unlimited_via_feature_limit_value():
    db = _db()
    tenant = _tenant(db, plan="starter")
    # "unlimited" coerces to None -> always allowed
    _feature(db, tenant.id, "max_units_with_addon", enabled=True, limit_value="unlimited")
    _unit(db, tenant.id)
    _unit(db, tenant.id)
    assert svc.can_add_tenant_unit(tenant, db) is True


def test_can_add_unit_non_int_limit_returns_true():
    db = _db()
    tenant = _tenant(db, plan="starter")
    # non-numeric limit_value coerces to the raw string -> not int -> True
    _feature(db, tenant.id, "max_units_with_addon", enabled=True, limit_value="abc")
    _unit(db, tenant.id)
    _unit(db, tenant.id)
    assert svc.can_add_tenant_unit(tenant, db) is True


def test_can_add_unit_isolated_per_tenant():
    db = _db()
    t1 = _tenant(db, plan="starter", tid="t1", slug="a")
    t2 = _tenant(db, plan="starter", tid="t2", slug="b")
    _unit(db, t1.id)
    # t1 is full, t2 has none
    assert svc.can_add_tenant_unit(t1, db) is False
    assert svc.can_add_tenant_unit(t2, db) is True


# --------------------------------------------------------------------------
# plan_allows_product_feature / enforce_plan_allows_product_feature (Business+)
# --------------------------------------------------------------------------

PLAN_GATED_KEYS = ("recurring_plans", "shared_walks", "pet_tour")


def test_plan_gated_product_feature_blocked_on_starter():
    db = _db()
    starter = _tenant(db, plan="starter")
    for key in PLAN_GATED_KEYS:
        assert svc.plan_allows_product_feature(starter, key) is False
        with pytest.raises(HTTPException) as exc:
            svc.enforce_plan_allows_product_feature(starter, key, "Módulo")
        assert exc.value.status_code == 403
        assert "Business" in exc.value.detail


def test_plan_gated_product_feature_allowed_on_business_and_enterprise():
    db = _db()
    business = _tenant(db, plan="business", tid="tb", slug="b")
    enterprise = _tenant(db, plan="enterprise", tid="te", slug="e")
    for key in PLAN_GATED_KEYS:
        assert svc.plan_allows_product_feature(business, key) is True
        assert svc.plan_allows_product_feature(enterprise, key) is True
        assert svc.enforce_plan_allows_product_feature(business, key) is None
        assert svc.enforce_plan_allows_product_feature(enterprise, key) is None


def test_non_gated_product_feature_allowed_on_all_plans():
    db = _db()
    starter = _tenant(db, plan="starter")
    # coupons NÃO está em PLAN_GATED_PRODUCT_FEATURES -> liberado em todos os planos.
    assert svc.plan_allows_product_feature(starter, "coupons") is True
    assert svc.enforce_plan_allows_product_feature(starter, "coupons") is None


# --------------------------------------------------------------------------
# enforce_can_add_tenant_unit
# --------------------------------------------------------------------------


def test_enforce_can_add_unit_raises_when_full():
    db = _db()
    tenant = _tenant(db, plan="starter")
    _unit(db, tenant.id)
    with pytest.raises(HTTPException) as exc:
        svc.enforce_can_add_tenant_unit(tenant, db)
    assert exc.value.status_code == 403


def test_enforce_can_add_unit_passes_when_room():
    db = _db()
    tenant = _tenant(db, plan="business")
    assert svc.enforce_can_add_tenant_unit(tenant, db) is None


# --------------------------------------------------------------------------
# enforce_network_access_allowed
# --------------------------------------------------------------------------


def test_enforce_network_access_passes_for_business():
    # Fase 1 Passo 1 (decisão 5 PRD): business agora exige network_access_addon=True.
    # Tenant business sem addon → 403; COM addon → passa.
    db = _db()
    tenant = _tenant(db, plan="business")
    tenant.network_access_addon = True
    db.commit()
    assert svc.enforce_network_access_allowed(tenant, db) is None


def test_enforce_network_access_raises_for_starter():
    db = _db()
    tenant = _tenant(db, plan="starter")
    with pytest.raises(HTTPException) as exc:
        svc.enforce_network_access_allowed(tenant, db)
    assert exc.value.status_code == 403

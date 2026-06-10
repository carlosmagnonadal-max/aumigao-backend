from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tenant import Tenant, TenantFeature, TenantUnit
from app.services import tenant_commercial_service as svc
from app.services.tenant_plan_service import (
    TENANT_PLAN_BUSINESS,
    TENANT_PLAN_ENTERPRISE,
    TENANT_PLAN_STARTER,
)


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


def _tenant(db, *, plan="business", tenant_id="t1", slug="aumigao-co", features=None):
    tenant = Tenant(id=tenant_id, name="Co", slug=slug, status="active", plan=plan)
    db.add(tenant)
    for feature_key, enabled in (features or {}).items():
        db.add(TenantFeature(tenant_id=tenant.id, feature_key=feature_key, enabled=enabled))
    db.commit()
    return tenant


# --------------------------------------------------------------------------
# normalize_commercial_plan
# --------------------------------------------------------------------------

def test_normalize_known_plans_passthrough():
    assert svc.normalize_commercial_plan("starter") == TENANT_PLAN_STARTER
    assert svc.normalize_commercial_plan("business") == TENANT_PLAN_BUSINESS
    assert svc.normalize_commercial_plan("enterprise") == TENANT_PLAN_ENTERPRISE


def test_normalize_trims_and_lowercases():
    assert svc.normalize_commercial_plan("  BUSINESS  ") == TENANT_PLAN_BUSINESS
    assert svc.normalize_commercial_plan("Enterprise") == TENANT_PLAN_ENTERPRISE


def test_normalize_unknown_or_none_falls_back_to_starter():
    assert svc.normalize_commercial_plan(None) == TENANT_PLAN_STARTER
    assert svc.normalize_commercial_plan("") == TENANT_PLAN_STARTER
    assert svc.normalize_commercial_plan("premium") == TENANT_PLAN_STARTER


# --------------------------------------------------------------------------
# default & per-plan commercial features
# --------------------------------------------------------------------------

def test_default_commercial_features_are_all_false():
    defaults = svc.get_default_commercial_features()
    assert defaults == {
        "network_access": False,
        "dedicated_app": False,
        "custom_products": False,
        "custom_projects": False,
    }


def test_default_features_return_a_copy():
    a = svc.get_default_commercial_features()
    a["network_access"] = True
    b = svc.get_default_commercial_features()
    assert b["network_access"] is False


def test_plan_features_starter():
    feats = svc.get_commercial_plan_features("starter")
    assert all(value is False for value in feats.values())


def test_plan_features_business_no_custom_projects():
    feats = svc.get_commercial_plan_features("business")
    assert feats["network_access"] is True
    assert feats["dedicated_app"] is True
    assert feats["custom_products"] is True
    assert feats["custom_projects"] is False


def test_plan_features_enterprise_all_true():
    feats = svc.get_commercial_plan_features("enterprise")
    assert all(value is True for value in feats.values())


def test_plan_features_unknown_plan_uses_starter():
    assert svc.get_commercial_plan_features("nope") == svc.get_commercial_plan_features("starter")


def test_plan_features_return_a_copy():
    feats = svc.get_commercial_plan_features("enterprise")
    feats["network_access"] = "mutated"
    fresh = svc.get_commercial_plan_features("enterprise")
    assert fresh["network_access"] is True


# --------------------------------------------------------------------------
# get_commercial_plans (catalog)
# --------------------------------------------------------------------------

def test_get_commercial_plans_lists_three_plans_in_order():
    result = svc.get_commercial_plans()
    keys = [plan["key"] for plan in result["plans"]]
    assert keys == [TENANT_PLAN_STARTER, TENANT_PLAN_BUSINESS, TENANT_PLAN_ENTERPRISE]


def test_get_commercial_plans_entries_have_expected_shape():
    result = svc.get_commercial_plans()
    for plan in result["plans"]:
        assert set(plan.keys()) == {"key", "label", "description", "capabilities", "recommended_for"}
        assert isinstance(plan["capabilities"], dict)
        assert isinstance(plan["recommended_for"], list)

    business = next(p for p in result["plans"] if p["key"] == TENANT_PLAN_BUSINESS)
    assert business["label"] == "Business"
    assert business["capabilities"] == svc.get_commercial_plan_features(TENANT_PLAN_BUSINESS)


# --------------------------------------------------------------------------
# get_tenant_commercial_runtime - merge of plan + runtime feature flags
# --------------------------------------------------------------------------

def test_runtime_resolves_explicit_tenant_object():
    db = _db()
    tenant = _tenant(db, plan="business")
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)

    assert result["tenant_id"] == tenant.id
    assert result["plan"] == TENANT_PLAN_BUSINESS
    assert result["plan_label"] == "Business"
    assert result["billing_enabled"] is False
    assert result["billing_status"] == "not_configured"


def test_runtime_upgrade_available_and_next_plan():
    db = _db()
    starter = _tenant(db, plan="starter", tenant_id="ts", slug="s")
    business = _tenant(db, plan="business", tenant_id="tb", slug="b")
    enterprise = _tenant(db, plan="enterprise", tenant_id="te", slug="e")

    r_starter = svc.get_tenant_commercial_runtime(db, tenant=starter)
    r_business = svc.get_tenant_commercial_runtime(db, tenant=business)
    r_enterprise = svc.get_tenant_commercial_runtime(db, tenant=enterprise)

    assert r_starter["next_recommended_plan"] == TENANT_PLAN_BUSINESS
    assert r_starter["upgrade_available"] is True
    assert r_business["next_recommended_plan"] == TENANT_PLAN_ENTERPRISE
    assert r_business["upgrade_available"] is True
    assert r_enterprise["next_recommended_plan"] is None
    assert r_enterprise["upgrade_available"] is False


def test_runtime_features_default_to_plan_allowance_when_no_flags():
    # Effective features are AND(base_plan_capability, tenant_capability).
    # With NO TenantFeature rows, get_tenant_capabilities returns the base plan
    # capabilities unchanged, so tenant_allows == base_allows. The effective
    # features therefore mirror the plan defaults (business: all True except
    # custom_projects), NOT all-False.
    db = _db()
    tenant = _tenant(db, plan="business")
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)

    assert result["features"] == {
        "network_access": True,
        "dedicated_app": True,
        "custom_products": True,
        "custom_projects": False,
    }


def test_runtime_starter_features_all_false_with_no_flags():
    db = _db()
    tenant = _tenant(db, plan="starter")
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)
    assert result["features"] == {
        "network_access": False,
        "dedicated_app": False,
        "custom_products": False,
        "custom_projects": False,
    }


def test_runtime_features_enabled_when_plan_and_flag_align():
    db = _db()
    tenant = _tenant(
        db,
        plan="business",
        features={"network_access": True, "dedicated_app": True, "custom_products": True},
    )
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)

    assert result["features"]["network_access"] is True
    assert result["features"]["dedicated_app"] is True
    assert result["features"]["custom_products"] is True
    # custom_projects not allowed for business plan, so AND keeps it False
    # even if a flag were set.
    assert result["features"]["custom_projects"] is False


def test_runtime_disabling_flag_turns_off_plan_allowed_feature():
    # A TenantFeature with enabled=False overrides the plan default via the AND.
    db = _db()
    tenant = _tenant(db, plan="business", features={"network_access": False})
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)
    assert result["features"]["network_access"] is False
    # other plan-allowed features remain on
    assert result["features"]["dedicated_app"] is True


def test_runtime_flag_cannot_unlock_feature_beyond_plan():
    # custom_projects is not allowed for business plan; enabling the tenant flag
    # must not unlock it (plan gate wins via AND).
    db = _db()
    tenant = _tenant(db, plan="business", features={"custom_projects": True})
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)
    assert result["features"]["custom_projects"] is False


def test_runtime_enterprise_with_all_flags_unlocks_everything():
    db = _db()
    tenant = _tenant(
        db,
        plan="enterprise",
        features={
            "network_access": True,
            "dedicated_app": True,
            "custom_products": True,
            "custom_projects": True,
        },
    )
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)
    assert all(value is True for value in result["features"].values())


def test_runtime_capabilities_reflect_plan():
    db = _db()
    tenant = _tenant(db, plan="enterprise")
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)
    caps = result["capabilities"]
    # enterprise capabilities from tenant_plan_service
    assert caps["custom_projects_allowed"] is True
    assert caps["onboarding_mode"] == "consultative"


def test_runtime_normalizes_unknown_plan_to_starter():
    db = _db()
    tenant = _tenant(db, plan="legacy-vip")
    result = svc.get_tenant_commercial_runtime(db, tenant=tenant)
    assert result["plan"] == TENANT_PLAN_STARTER
    assert result["plan_label"] == "Starter"
    assert result["next_recommended_plan"] == TENANT_PLAN_BUSINESS


def test_runtime_resolves_tenant_by_id():
    db = _db()
    tenant = _tenant(db, plan="business", tenant_id="abc123", slug="byid")
    result = svc.get_tenant_commercial_runtime(db, tenant_id="abc123")
    assert result["tenant_id"] == "abc123"
    assert result["plan"] == TENANT_PLAN_BUSINESS


def test_runtime_resolves_tenant_by_slug():
    db = _db()
    _tenant(db, plan="enterprise", tenant_id="xid", slug="my-slug")
    result = svc.get_tenant_commercial_runtime(db, tenant_id="my-slug")
    assert result["plan"] == TENANT_PLAN_ENTERPRISE
    assert result["tenant_id"] == "xid"

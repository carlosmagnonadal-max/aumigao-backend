import os
from datetime import datetime, timedelta
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.core.database import Base, get_db, get_global_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.payment import Payment
from app.models.tenant_saas_subscription import (
    TenantSaasSubscription, SAAS_ACTIVE, SAAS_OVERDUE, SAAS_CANCELLED,
)

TENANT_ID = "t-saas"

def _make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Cliente X", slug="cliente-x", status="active", plan="pro",
                  legal_name="Cliente X LTDA", document_number="11222333000181", contact_email="fin@clientex.com"))
    db.commit()
    return db

def _sessionmaker_for(db):
    return sessionmaker(bind=db.bind)

def _acoro(v):
    async def _f(*a, **k):
        return v
    return _f


# ------------------------------------------------------------------ pricing ---
from app.services.tenant_saas_pricing import resolve_saas_price

def test_pro_price_is_fixed():
    assert float(resolve_saas_price("pro", None)) == 129.90

def test_enterprise_default_floor():
    assert float(resolve_saas_price("enterprise", None)) == 1199.90

def test_enterprise_custom_overrides():
    assert float(resolve_saas_price("enterprise", 1500.0)) == 1500.0

def test_pro_ignores_custom():
    assert float(resolve_saas_price("pro", 50.0)) == 129.90

def test_enterprise_zero_price_raises():
    with pytest.raises(ValueError):
        resolve_saas_price("enterprise", 0.0)

def test_enterprise_negative_raises():
    with pytest.raises(ValueError):
        resolve_saas_price("enterprise", -10.0)

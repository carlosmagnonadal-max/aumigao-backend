# Provisão Fiscal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Camada contábil que registra, por pagamento confirmado, a divisão (passeador/Aumigão) e o imposto provisionado de cada lado, com config fiscal editável por tenant, visível no admin.

**Architecture:** 2 tabelas additivas — `tenant_fiscal_config` (alíquotas por tenant, default 0) e `payment_provision` (snapshot imutável por pagamento). Cálculo na confirmação do pagamento (hook best-effort no `asaas_webhook`, idempotente). Rotas admin para config e summary. Sem flag, sem mover dinheiro.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, pytest. Windows: `./.venv/Scripts/python.exe -m pytest ...`.

**Convenções do repo:**
- Money/Numeric: `from app.models.types import Money`.
- Rotas admin: `require_permission("finance.read"|"finance.manage")`, `get_admin_tenant_scope(admin, db)` no topo de escritas, `record_audit_log(...)`, `is_super_admin`.
- Migrations no estilo 0058/0059 (additivas, sem RLS).
- Baseline de testes: ~2586 passed / 15 failed / 99 errors pré-existentes (ambiente). Zero regressão NOVA.

---

### Task 1: Models + migration (tenant_fiscal_config, payment_provision)

**Files:**
- Create: `app/models/fiscal.py`
- Create: `alembic/versions/0061_fiscal_provisioning.py`
- Modify: `app/models/__init__.py` (importar os 2 models)
- Test: `tests/test_fiscal_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fiscal_models.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig, PaymentProvision

def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__, PaymentProvision.__table__])
    return sessionmaker(bind=e)()

def test_fiscal_config_persists_percent_fields():
    db = _db()
    cfg = TenantFiscalConfig(tenant_id="t1", commission_tax_percent=5, subscription_tax_percent=2, walker_tax_percent=1.5)
    db.add(cfg); db.commit(); db.refresh(cfg)
    assert cfg.id and cfg.active is True
    assert float(cfg.commission_tax_percent) == 5

def test_payment_provision_persists_breakdown():
    db = _db()
    p = PaymentProvision(tenant_id="t1", payment_id="p1", revenue_type="walk_commission",
                         walker_gross=80, walker_tax=0, walker_net=80,
                         platform_gross=20, platform_tax=1, platform_net=19,
                         walker_tax_percent_applied=0, platform_tax_percent_applied=5)
    db.add(p); db.commit(); db.refresh(p)
    assert p.id and float(p.platform_net) == 19
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_fiscal_models.py -q`
Expected: FAIL (`ModuleNotFoundError: app.models.fiscal`).

- [ ] **Step 3: Create the models**

```python
# app/models/fiscal.py
from datetime import datetime
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.models.types import Money

def _uuid() -> str:
    return str(uuid4())

# revenue_type values
REVENUE_WALK_COMMISSION = "walk_commission"
REVENUE_SAAS_SUBSCRIPTION = "saas_subscription"
REVENUE_TIP = "tip"

class TenantFiscalConfig(Base):
    __tablename__ = "tenant_fiscal_config"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    # Provisão (percentuais; default 0 -> provisão zero até configurar)
    commission_tax_percent: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    subscription_tax_percent: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    walker_tax_percent: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    # Estruturais NFS-e (usados quando a emissão ligar)
    iss_percent: Mapped[float | None] = mapped_column(Money, nullable=True)
    municipal_service_code: Mapped[str | None] = mapped_column(String, nullable=True)
    simples_nacional: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    cnae: Mapped[str | None] = mapped_column(String, nullable=True)
    service_description: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PaymentProvision(Base):
    __tablename__ = "payment_provision"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    payment_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    revenue_type: Mapped[str] = mapped_column(String, nullable=False)
    walker_gross: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    walker_tax: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    walker_net: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    platform_gross: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    platform_tax: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    platform_net: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    walker_tax_percent_applied: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    platform_tax_percent_applied: Mapped[float] = mapped_column(Money, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

Add to `app/models/__init__.py`:
```python
from app.models.fiscal import TenantFiscalConfig, PaymentProvision  # noqa: F401
```

- [ ] **Step 4: Create the migration**

```python
# alembic/versions/0061_fiscal_provisioning.py
"""tenant_fiscal_config + payment_provision

Revision ID: 0061_fiscal_provisioning
Revises: 0060_nfse
"""
import sqlalchemy as sa
from alembic import op

revision = "0061_fiscal_provisioning"
down_revision = "0060_nfse"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "tenant_fiscal_config",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("commission_tax_percent", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("subscription_tax_percent", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("walker_tax_percent", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("iss_percent", sa.Numeric(10, 2), nullable=True),
        sa.Column("municipal_service_code", sa.String(), nullable=True),
        sa.Column("simples_nacional", sa.Boolean(), nullable=True),
        sa.Column("cnae", sa.String(), nullable=True),
        sa.Column("service_description", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("uq_tenant_fiscal_config_tenant", "tenant_fiscal_config", ["tenant_id"], unique=True)
    op.create_table(
        "payment_provision",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("payment_id", sa.String(), nullable=False),
        sa.Column("revenue_type", sa.String(), nullable=False),
        sa.Column("walker_gross", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("walker_tax", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("walker_net", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("platform_gross", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("platform_tax", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("platform_net", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("walker_tax_percent_applied", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("platform_tax_percent_applied", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_payment_provision_tenant_id", "payment_provision", ["tenant_id"])
    op.create_index("uq_payment_provision_payment", "payment_provision", ["payment_id"], unique=True)

def downgrade() -> None:
    op.drop_table("payment_provision")
    op.drop_index("uq_tenant_fiscal_config_tenant", table_name="tenant_fiscal_config")
    op.drop_table("tenant_fiscal_config")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_fiscal_models.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add app/models/fiscal.py app/models/__init__.py alembic/versions/0061_fiscal_provisioning.py tests/test_fiscal_models.py
git commit -m "feat(fiscal): models tenant_fiscal_config + payment_provision + migration 0061"
```

---

### Task 2: fiscal_config_service (resolução com defaults zero + upsert)

**Files:**
- Create: `app/services/fiscal_config_service.py`
- Test: `tests/test_fiscal_config_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fiscal_config_service.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig
from app.services import fiscal_config_service as svc

def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__])
    return sessionmaker(bind=e)()

def test_resolve_returns_zero_defaults_when_absent():
    db = _db()
    cfg = svc.resolve_fiscal_config(db, "t-x")
    assert float(cfg.commission_tax_percent) == 0
    assert float(cfg.walker_tax_percent) == 0

def test_upsert_creates_then_updates():
    db = _db()
    a = svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 5})
    assert float(a.commission_tax_percent) == 5
    b = svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 3, "walker_tax_percent": 1})
    assert a.id == b.id and float(b.commission_tax_percent) == 3 and float(b.walker_tax_percent) == 1
    assert db.query(TenantFiscalConfig).count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_fiscal_config_service.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the service**

```python
# app/services/fiscal_config_service.py
from sqlalchemy.orm import Session
from app.models.fiscal import TenantFiscalConfig

_EDITABLE = {
    "commission_tax_percent", "subscription_tax_percent", "walker_tax_percent",
    "iss_percent", "municipal_service_code", "simples_nacional", "cnae",
    "service_description", "active",
}

def get_fiscal_config(db: Session, tenant_id: str) -> TenantFiscalConfig | None:
    return db.query(TenantFiscalConfig).filter(TenantFiscalConfig.tenant_id == tenant_id).first()

def resolve_fiscal_config(db: Session, tenant_id: str) -> TenantFiscalConfig:
    """Retorna a config do tenant ou uma instância transitória com defaults zero
    (NÃO persistida) — para o cálculo de provisão nunca falhar por ausência."""
    cfg = get_fiscal_config(db, tenant_id)
    if cfg is not None:
        return cfg
    return TenantFiscalConfig(
        tenant_id=tenant_id, commission_tax_percent=0,
        subscription_tax_percent=0, walker_tax_percent=0,
    )

def upsert_fiscal_config(db: Session, tenant_id: str, values: dict) -> TenantFiscalConfig:
    cfg = get_fiscal_config(db, tenant_id)
    if cfg is None:
        cfg = TenantFiscalConfig(tenant_id=tenant_id)
        db.add(cfg)
    for k, v in values.items():
        if k in _EDITABLE and v is not None:
            setattr(cfg, k, v)
    db.commit()
    db.refresh(cfg)
    return cfg
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_fiscal_config_service.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/fiscal_config_service.py tests/test_fiscal_config_service.py
git commit -m "feat(fiscal): fiscal_config_service (resolve com defaults zero + upsert)"
```

---

### Task 3: provision_service.compute_and_store_provision (cálculo, idempotência, imutabilidade)

**Files:**
- Create: `app/services/provision_service.py`
- Test: `tests/test_provision_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provision_service.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig, PaymentProvision
from app.services import fiscal_config_service as cfg_svc
from app.services import provision_service as svc

class FakePayment:
    def __init__(self, id, amount, platform_amount=None, walker_amount=None):
        self.id = id; self.amount = amount
        self.platform_amount = platform_amount; self.walker_amount = walker_amount

def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__, PaymentProvision.__table__])
    return sessionmaker(bind=e)()

def test_walk_commission_uses_commission_rate():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 5, "walker_tax_percent": 1})
    pay = FakePayment("p1", 100, platform_amount=20, walker_amount=80)
    prov = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    assert float(prov.platform_tax) == 1.0 and float(prov.platform_net) == 19.0
    assert float(prov.walker_tax) == 0.8 and float(prov.walker_net) == 79.2

def test_zero_config_yields_zero_tax():
    db = _db()
    pay = FakePayment("p2", 100, platform_amount=20, walker_amount=80)
    prov = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    assert float(prov.platform_tax) == 0 and float(prov.walker_tax) == 0

def test_idempotent_same_payment():
    db = _db()
    pay = FakePayment("p3", 100, platform_amount=20, walker_amount=80)
    a = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    b = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    assert a.id == b.id and db.query(PaymentProvision).count() == 1

def test_immutable_after_rate_change():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 5})
    pay = FakePayment("p4", 100, platform_amount=20, walker_amount=80)
    svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    cfg_svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 50})
    again = svc.compute_and_store_provision(db, "t1", pay, "walk_commission")
    assert float(again.platform_tax) == 1.0  # congelado na 1ª alíquota

def test_saas_subscription_taxes_full_amount_on_platform():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"subscription_tax_percent": 10})
    pay = FakePayment("p5", 129.90)
    prov = svc.compute_and_store_provision(db, "t1", pay, "saas_subscription")
    assert float(prov.platform_gross) == 129.90 and float(prov.walker_gross) == 0
    assert round(float(prov.platform_tax), 2) == 12.99

def test_tip_taxes_walker_side():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"walker_tax_percent": 10})
    pay = FakePayment("p6", 50)
    prov = svc.compute_and_store_provision(db, "t1", pay, "tip")
    assert float(prov.walker_gross) == 50 and float(prov.platform_gross) == 0
    assert float(prov.walker_tax) == 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_service.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the service**

```python
# app/services/provision_service.py
import logging
from sqlalchemy.orm import Session
from app.models.fiscal import (
    PaymentProvision, REVENUE_WALK_COMMISSION, REVENUE_SAAS_SUBSCRIPTION, REVENUE_TIP,
)
from app.services.fiscal_config_service import resolve_fiscal_config

logger = logging.getLogger("aumigao.provision_service")

def _f(v) -> float:
    return float(v) if v is not None else 0.0

def _bases(payment, revenue_type, cfg):
    """Retorna (walker_gross, walker_pct, platform_gross, platform_pct)."""
    if revenue_type == REVENUE_SAAS_SUBSCRIPTION:
        return 0.0, 0.0, _f(payment.amount), _f(cfg.subscription_tax_percent)
    if revenue_type == REVENUE_TIP:
        return _f(payment.amount), _f(cfg.walker_tax_percent), 0.0, 0.0
    # default: walk_commission
    return (
        _f(getattr(payment, "walker_amount", None)), _f(cfg.walker_tax_percent),
        _f(getattr(payment, "platform_amount", None)), _f(cfg.commission_tax_percent),
    )

def get_provision(db: Session, payment_id: str) -> PaymentProvision | None:
    return db.query(PaymentProvision).filter(PaymentProvision.payment_id == payment_id).first()

def compute_and_store_provision(db: Session, tenant_id: str, payment, revenue_type: str) -> PaymentProvision:
    existing = get_provision(db, payment.id)
    if existing is not None:
        return existing  # idempotente + imutável
    cfg = resolve_fiscal_config(db, tenant_id)
    wg, wpct, pg, ppct = _bases(payment, revenue_type, cfg)
    wtax = round(wg * wpct / 100.0, 2); ptax = round(pg * ppct / 100.0, 2)
    prov = PaymentProvision(
        tenant_id=tenant_id, payment_id=payment.id, revenue_type=revenue_type,
        walker_gross=wg, walker_tax=wtax, walker_net=round(wg - wtax, 2),
        platform_gross=pg, platform_tax=ptax, platform_net=round(pg - ptax, 2),
        walker_tax_percent_applied=wpct, platform_tax_percent_applied=ppct,
    )
    db.add(prov); db.commit(); db.refresh(prov)
    return prov
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_service.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/provision_service.py tests/test_provision_service.py
git commit -m "feat(fiscal): provision_service.compute_and_store_provision (idempotente, imutavel)"
```

---

### Task 4: provision_service.financial_summary (agregação)

**Files:**
- Modify: `app/services/provision_service.py`
- Test: `tests/test_provision_summary.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provision_summary.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.database import Base
from app.models.fiscal import TenantFiscalConfig, PaymentProvision
from app.services import fiscal_config_service as cfg_svc
from app.services import provision_service as svc

class FakePayment:
    def __init__(self, id, amount, platform_amount=None, walker_amount=None):
        self.id = id; self.amount = amount
        self.platform_amount = platform_amount; self.walker_amount = walker_amount

def _db():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e, tables=[TenantFiscalConfig.__table__, PaymentProvision.__table__])
    return sessionmaker(bind=e)()

def test_summary_aggregates_reserved_and_net():
    db = _db()
    cfg_svc.upsert_fiscal_config(db, "t1", {"commission_tax_percent": 10, "walker_tax_percent": 5})
    svc.compute_and_store_provision(db, "t1", FakePayment("a", 100, 20, 80), "walk_commission")
    svc.compute_and_store_provision(db, "t1", FakePayment("b", 100, 20, 80), "walk_commission")
    s = svc.financial_summary(db, "t1")
    assert s["count"] == 2
    assert round(s["platform_tax_reserved"], 2) == 4.0   # 2 x (20*10%)
    assert round(s["platform_net"], 2) == 36.0
    assert round(s["walker_tax_reserved"], 2) == 8.0     # 2 x (80*5%)
    assert round(s["walker_net"], 2) == 152.0

def test_summary_other_tenant_isolated():
    db = _db()
    svc.compute_and_store_provision(db, "t1", FakePayment("a", 100, 20, 80), "walk_commission")
    s = svc.financial_summary(db, "t2")
    assert s["count"] == 0 and s["platform_net"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_summary.py -q`
Expected: FAIL (`AttributeError: financial_summary`).

- [ ] **Step 3: Implement financial_summary**

```python
# adicionar em app/services/provision_service.py
from datetime import datetime

def financial_summary(db: Session, tenant_id: str, *, date_from: datetime | None = None, date_to: datetime | None = None) -> dict:
    q = db.query(PaymentProvision).filter(PaymentProvision.tenant_id == tenant_id)
    if date_from is not None:
        q = q.filter(PaymentProvision.created_at >= date_from)
    if date_to is not None:
        q = q.filter(PaymentProvision.created_at <= date_to)
    rows = q.all()
    agg = {
        "count": len(rows),
        "gross_total": 0.0,
        "platform_gross": 0.0, "platform_tax_reserved": 0.0, "platform_net": 0.0,
        "walker_gross": 0.0, "walker_tax_reserved": 0.0, "walker_net": 0.0,
    }
    for r in rows:
        agg["platform_gross"] += _f(r.platform_gross)
        agg["platform_tax_reserved"] += _f(r.platform_tax)
        agg["platform_net"] += _f(r.platform_net)
        agg["walker_gross"] += _f(r.walker_gross)
        agg["walker_tax_reserved"] += _f(r.walker_tax)
        agg["walker_net"] += _f(r.walker_net)
    agg["gross_total"] = round(agg["platform_gross"] + agg["walker_gross"], 2)
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in agg.items()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_summary.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/services/provision_service.py tests/test_provision_summary.py
git commit -m "feat(fiscal): provision_service.financial_summary (agregacao por tenant/periodo)"
```

---

### Task 5: Hook de provisão no asaas_webhook (3 pontos de confirmação)

**Files:**
- Modify: `app/routes/payments.py` (ramos: passeio regular ~1140; SaaS `_handle_tenant_saas_subscription_webhook`; gorjeta `_handle_tip_webhook`)
- Test: `tests/test_provision_webhook.py`

Padrão: helper best-effort `_provision_safe(db, tenant_id, payment_like, revenue_type)` que envolve `compute_and_store_provision` em try/except (loga, nunca relança). Chamado no ponto em que `new_status == _PAYMENT_CONFIRMED_STATUS` de cada ramo. Para gorjeta, montar um objeto leve com `id` e `amount` (a partir de `payment_data`/`tip`). Para SaaS, `revenue_type="saas_subscription"`, payment-like com `id`=provider_payment_id e `amount`=valor; tenant_id = sub.tenant_id. Para passeio regular, `revenue_type="walk_commission"`, usando o `Payment` local (tenant_id = payment.tenant_id).

- [ ] **Step 1: Write the failing test** — verifica que, após processar um webhook de pagamento de passeio confirmado, existe um `payment_provision` para aquele pagamento. (Montar via TestClient + dependency_overrides de get_global_db como nos testes de webhook existentes — seguir `tests/test_tenant_saas_billing.py`/`tests/test_payments*` para o scaffolding. Se o overhead for alto, testar o helper `_provision_safe` isoladamente garantindo idempotência e que exceção não propaga.)

```python
# tests/test_provision_webhook.py (forma mínima — helper isolado)
from app.routes.payments import _provision_safe

class P:  # payment-like
    id = "pp1"; amount = 100; platform_amount = 20; walker_amount = 80; tenant_id = "t1"

def test_provision_safe_never_raises(monkeypatch):
    # força erro interno e garante que não propaga
    import app.routes.payments as mod
    def boom(*a, **k): raise RuntimeError("x")
    monkeypatch.setattr(mod, "compute_and_store_provision", boom, raising=False)
    _provision_safe(db=None, tenant_id="t1", payment_like=P(), revenue_type="walk_commission")  # não levanta
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_webhook.py -q`
Expected: FAIL (`ImportError: _provision_safe`).

- [ ] **Step 3: Implement the helper + wiring**

```python
# app/routes/payments.py — adicionar helper perto do fim do arquivo
def _provision_safe(db, tenant_id, payment_like, revenue_type) -> None:
    """Best-effort: registra a provisão fiscal do pagamento confirmado.
    NUNCA propaga exceção — provisão jamais pode quebrar o webhook de pagamento."""
    try:
        from app.services.provision_service import compute_and_store_provision
        compute_and_store_provision(db, tenant_id, payment_like, revenue_type)
    except Exception:
        logger.exception("provision: falha best-effort tenant_id=%s revenue_type=%s", tenant_id, revenue_type)
        try:
            db.rollback()
        except Exception:
            pass
```

Wiring (chamar logo após o commit de confirmação de cada ramo):
- Passeio regular (após `db.commit()` ~1148, quando `new_status == _PAYMENT_CONFIRMED_STATUS`):
  `_provision_safe(db, payment.tenant_id, payment, REVENUE_WALK_COMMISSION)`
- SaaS (`_handle_tenant_saas_subscription_webhook`, junto do trigger de NFS-e já existente):
  montar `payment_like` com `.id = pid` e `.amount = float(sub.price)`; `_provision_safe(db, sub.tenant_id, payment_like, REVENUE_SAAS_SUBSCRIPTION)`
- Gorjeta (`_handle_tip_webhook`, quando `tip.status == "paid"`): `payment_like` com `.id = provider_payment_id` e `.amount = float(tip.amount)`; tenant_id = tip.tenant_id; `_provision_safe(db, tip.tenant_id, payment_like, REVENUE_TIP)`

Importar os REVENUE_* de `app.models.fiscal` no topo de payments.py. Para o `payment_like` de SaaS/gorjeta, usar um `types.SimpleNamespace(id=..., amount=..., platform_amount=None, walker_amount=None)`.

- [ ] **Step 4: Run test + suíte de webhook**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_provision_webhook.py tests/test_tenant_saas_billing.py tests/test_nfse.py -q`
Expected: PASS, zero regressão.

- [ ] **Step 5: Commit**

```bash
git add app/routes/payments.py tests/test_provision_webhook.py
git commit -m "feat(fiscal): hook best-effort de provisao na confirmacao de pagamento (3 ramos)"
```

---

### Task 6: Rotas admin GET/PUT fiscal-config

**Files:**
- Create: `app/routes/fiscal.py`
- Modify: `app/main.py` (incluir o router)
- Create: `app/schemas/fiscal.py`
- Test: `tests/test_routes_fiscal_config.py`

- [ ] **Step 1: Write the failing test** — super_admin faz PUT da config de um tenant e GET retorna os valores; admin de outro tenant recebe 403/404 ao acessar tenant alheio. Seguir o scaffolding de `tests/test_routes_admin_tenants.py` (montagem de client + usuário super_admin/tenant-admin).

```python
# tests/test_routes_fiscal_config.py (esqueleto — completar o build() como em test_routes_admin_tenants.py)
def test_put_then_get_fiscal_config_super_admin(client_super, tenant_id):
    r = client_super.put(f"/admin/tenants/{tenant_id}/fiscal-config", json={"commission_tax_percent": 5})
    assert r.status_code == 200 and r.json()["commission_tax_percent"] == 5
    g = client_super.get(f"/admin/tenants/{tenant_id}/fiscal-config")
    assert g.status_code == 200 and g.json()["commission_tax_percent"] == 5

def test_get_defaults_zero_when_absent(client_super, other_tenant_id):
    g = client_super.get(f"/admin/tenants/{other_tenant_id}/fiscal-config")
    assert g.status_code == 200 and g.json()["commission_tax_percent"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_routes_fiscal_config.py -q`
Expected: FAIL (404 — rota inexistente).

- [ ] **Step 3: Implement schema + router**

```python
# app/schemas/fiscal.py
from pydantic import BaseModel, Field
from app.schemas.common import ORMModel

class FiscalConfigResponse(ORMModel):
    tenant_id: str
    commission_tax_percent: float
    subscription_tax_percent: float
    walker_tax_percent: float
    iss_percent: float | None = None
    municipal_service_code: str | None = None
    simples_nacional: bool | None = None
    cnae: str | None = None
    service_description: str | None = None
    active: bool

class FiscalConfigUpdate(BaseModel):
    commission_tax_percent: float | None = Field(default=None, ge=0, le=100)
    subscription_tax_percent: float | None = Field(default=None, ge=0, le=100)
    walker_tax_percent: float | None = Field(default=None, ge=0, le=100)
    iss_percent: float | None = Field(default=None, ge=0, le=100)
    municipal_service_code: str | None = None
    simples_nacional: bool | None = None
    cnae: str | None = None
    service_description: str | None = None
    active: bool | None = None
```

```python
# app/routes/fiscal.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.fiscal import FiscalConfigResponse, FiscalConfigUpdate
from app.services import fiscal_config_service as svc
from app.services.audit_service import record_audit_log

router = APIRouter(prefix="/admin/tenants", tags=["fiscal"])
api_router = APIRouter(prefix="/api/admin/tenants", tags=["fiscal"])

def _ensure_scope(admin: User, tenant_id: str, db: Session):
    scope = get_admin_tenant_scope(admin, db)
    if scope.tenant_id is not None and scope.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Tenant não encontrado.")
    if db.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=404, detail="Tenant não encontrado.")

def _serialize(tenant_id, cfg) -> dict:
    return {
        "tenant_id": tenant_id,
        "commission_tax_percent": float(cfg.commission_tax_percent or 0),
        "subscription_tax_percent": float(cfg.subscription_tax_percent or 0),
        "walker_tax_percent": float(cfg.walker_tax_percent or 0),
        "iss_percent": float(cfg.iss_percent) if cfg.iss_percent is not None else None,
        "municipal_service_code": cfg.municipal_service_code,
        "simples_nacional": cfg.simples_nacional,
        "cnae": cfg.cnae,
        "service_description": cfg.service_description,
        "active": bool(cfg.active) if cfg.active is not None else True,
    }

@router.get("/{tenant_id}/fiscal-config")
@api_router.get("/{tenant_id}/fiscal-config")
def get_fiscal_config(tenant_id: str, admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    _ensure_scope(admin, tenant_id, db)
    return _serialize(tenant_id, svc.resolve_fiscal_config(db, tenant_id))

@router.put("/{tenant_id}/fiscal-config")
@api_router.put("/{tenant_id}/fiscal-config")
def put_fiscal_config(tenant_id: str, payload: FiscalConfigUpdate, admin: User = Depends(require_permission("finance.manage")), db: Session = Depends(get_db)):
    get_admin_tenant_scope(admin, db)  # injeta escopo RLS no topo da escrita
    _ensure_scope(admin, tenant_id, db)
    values = payload.model_dump(exclude_unset=True)
    cfg = svc.upsert_fiscal_config(db, tenant_id, values)
    record_audit_log(db, action="fiscal_config.updated", entity_type="tenant_fiscal_config",
                     entity_id=cfg.id, actor=admin, after=values, tenant_id=tenant_id)
    db.commit()
    return _serialize(tenant_id, cfg)
```

Em `app/main.py`, adicionar:
```python
from app.routes import fiscal as fiscal_routes
app.include_router(fiscal_routes.router)
app.include_router(fiscal_routes.api_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_routes_fiscal_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/fiscal.py app/schemas/fiscal.py app/main.py tests/test_routes_fiscal_config.py
git commit -m "feat(fiscal): rotas admin GET/PUT fiscal-config (gate finance + escopo + audit)"
```

---

### Task 7: Rotas admin financial-summary + provision por pagamento

**Files:**
- Modify: `app/routes/fiscal.py`
- Test: `tests/test_routes_financial_summary.py`

- [ ] **Step 1: Write the failing test** — após criar provisões (via provision_service direto no db do teste), GET `/admin/tenants/{id}/financial-summary` retorna os agregados; super_admin e tenant-admin do próprio tenant ok; tenant alheio 404.

```python
def test_financial_summary_returns_aggregates(client_super, tenant_id, seed_provisions):
    r = client_super.get(f"/admin/tenants/{tenant_id}/financial-summary")
    assert r.status_code == 200
    body = r.json()
    assert "platform_tax_reserved" in body and "walker_net" in body and body["count"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_routes_financial_summary.py -q`
Expected: FAIL (404).

- [ ] **Step 3: Implement the routes**

```python
# adicionar em app/routes/fiscal.py
from datetime import datetime
from app.services import provision_service as prov_svc

@router.get("/{tenant_id}/financial-summary")
@api_router.get("/{tenant_id}/financial-summary")
def financial_summary(tenant_id: str, date_from: str | None = None, date_to: str | None = None,
                      admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    _ensure_scope(admin, tenant_id, db)
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to) if date_to else None
    return prov_svc.financial_summary(db, tenant_id, date_from=df, date_to=dt)
```

```python
# rota de extrato por pagamento — em app/routes/fiscal.py (prefixo próprio)
payments_router = APIRouter(prefix="/admin/payments", tags=["fiscal"])
api_payments_router = APIRouter(prefix="/api/admin/payments", tags=["fiscal"])

@payments_router.get("/{payment_id}/provision")
@api_payments_router.get("/{payment_id}/provision")
def get_payment_provision(payment_id: str, admin: User = Depends(require_permission("finance.read")), db: Session = Depends(get_db)):
    prov = prov_svc.get_provision(db, payment_id)
    if prov is None:
        raise HTTPException(status_code=404, detail="Provisão não encontrada.")
    _ensure_scope(admin, prov.tenant_id, db)
    return {
        "payment_id": prov.payment_id, "tenant_id": prov.tenant_id, "revenue_type": prov.revenue_type,
        "walker_gross": float(prov.walker_gross), "walker_tax": float(prov.walker_tax), "walker_net": float(prov.walker_net),
        "platform_gross": float(prov.platform_gross), "platform_tax": float(prov.platform_tax), "platform_net": float(prov.platform_net),
    }
```

Incluir os novos routers em `app/main.py`:
```python
app.include_router(fiscal_routes.payments_router)
app.include_router(fiscal_routes.api_payments_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_routes_financial_summary.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routes/fiscal.py app/main.py tests/test_routes_financial_summary.py
git commit -m "feat(fiscal): rotas admin financial-summary + provisao por pagamento"
```

---

### Task 8: Script de backfill de provisões

**Files:**
- Create: `scripts/backfill_provisions.py`
- Test: `tests/test_backfill_provisions.py`

- [ ] **Step 1: Write the failing test** — dado N `Payment` confirmados sem provisão, a função `backfill(db)` cria N provisões; rodar 2x não duplica.

```python
# tests/test_backfill_provisions.py — testar a função pura backfill(db, payment_iter, tenant_resolver, type_resolver)
from scripts.backfill_provisions import backfill_provisions
# montar Payments fake + db in-memory com as tabelas fiscais; asserts de contagem e idempotência
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_backfill_provisions.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the script**

```python
# scripts/backfill_provisions.py
"""Backfill de payment_provision para pagamentos confirmados sem provisão.

Uso (prod, via owner): DATABASE_URL=<dono> ./.venv/Scripts/python.exe scripts/backfill_provisions.py
Idempotente: compute_and_store_provision pula pagamentos já provisionados.
"""
import logging
from sqlalchemy.orm import Session
from app.models.payment import Payment
from app.services.provision_service import compute_and_store_provision

logger = logging.getLogger("aumigao.backfill_provisions")
_CONFIRMED = "pagamento_confirmado_sandbox"

def _revenue_type_for(payment: Payment) -> str:
    # heurística: walk_id presente -> comissão de passeio; senão saas/avulso.
    return "walk_commission" if getattr(payment, "walk_id", None) else "saas_subscription"

def backfill_provisions(db: Session) -> int:
    n = 0
    rows = db.query(Payment).filter(Payment.status == _CONFIRMED).all()
    for p in rows:
        tenant_id = getattr(p, "tenant_id", None)
        if not tenant_id:
            continue
        compute_and_store_provision(db, tenant_id, p, _revenue_type_for(p))
        n += 1
    return n

if __name__ == "__main__":
    from app.core.database import SessionLocal
    db = SessionLocal()
    created = backfill_provisions(db)
    db.close()
    print(f"backfill: processados {created} pagamentos confirmados")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_backfill_provisions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_provisions.py tests/test_backfill_provisions.py
git commit -m "feat(fiscal): script idempotente de backfill de provisoes"
```

---

### Task 9: Suíte completa + verificação final

- [ ] **Step 1:** `./.venv/Scripts/python.exe -m pytest -q` — confirmar zero regressão NOVA vs baseline (~2586 passed / 15 failed / 99 errors pré-existentes; o delta deve ser só os testes novos passando).
- [ ] **Step 2:** Revisar diff completo (Opus revisa). Conferir: nada fora de gate, webhook intacto, escopo RLS nas escritas admin, imutabilidade do snapshot.
- [ ] **Step 3 (deploy — com OK do Carlos):** aplicar migrations 0060 (NFS-e, pendente) + 0061 no Neon via owner; deploy Cloud Run; rodar `scripts/backfill_provisions.py` em prod; validar `financial-summary` (tudo zero de imposto até alíquotas serem setadas).

---

## Notas de execução
- Os esqueletos de teste de rota (Tasks 6-7) devem reusar o `build()`/fixtures de `tests/test_routes_admin_tenants.py` (cliente super_admin e tenant-admin). Se a montagem do client for custosa, priorizar cobertura dos services (Tasks 2-4) e um smoke das rotas.
- `payment.tenant_id`: confirmar que o model `Payment` tem `tenant_id` (tem, é multi-tenant). Para gorjeta/saas, o tenant vem do `tip`/`sub`.
- Não introduzir flag: alíquota 0% default já garante zero efeito perceptível.

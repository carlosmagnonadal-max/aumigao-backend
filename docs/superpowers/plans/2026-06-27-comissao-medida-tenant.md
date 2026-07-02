# Comissão Medida do Tenant (Fase 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Faturar o tenant pelos **10% (variável) por passeio realizado pelo passeador PRÓPRIO dele**, medindo o passeio no app e cobrando no fim do mês — sem o Aumigão custodiar o dinheiro do passeio.

**Architecture:** Na finalização do passeio (`_ensure_internal_walk_payment`, já existente), além do Payment interno que já calcula o split, criamos uma entrada imutável num novo ledger `commission_entries` (snapshot da taxa resolvida + valor), **apenas para passeios não-rede**. Um job mensal soma as entradas `accrued` do período por tenant, emite **uma cobrança Asaas avulsa** ("tenant_comm:") e marca as entradas como `billed`; o webhook de pagamento marca como `paid`. Reusa o motor de comissão variável (`build_payment_split`, 3 níveis + pricing v2) e a infra de cobrança do Projeto B.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Asaas (HTTP), pytest (SQLite in-memory).

**Princípio inegociável:** o valor cobrado vem da **MEDIÇÃO** do passeio (`Walk.price` × taxa resolvida), nunca de custodiar o pagamento do tutor. Passeio de REDE **não** entra aqui (a margem da rede é capturada no preço do crédito — Fase 2).

---

## Pré-requisitos de leitura (o engenheiro tem zero contexto)

- `backend/app/services/payment_split_service.py`
  - `build_payment_split(db, tenant_id, amount, *, walker_id=None) -> dict` (linha 298) devolve `{commission_percent, tenant_margin_percent, platform_amount, tenant_amount, walker_amount}`.
  - `is_network_walk(db, tenant_id, walker_id) -> bool` (linha 132) diz se o passeio é da Rede (`access_type ∈ {shared_network, tenant_exclusive}`).
- `backend/app/routes/admin.py:268` — `_ensure_internal_walk_payment(walk, db)` é chamado ao **aprovar a finalização** (`walk.status = "Finalizado"`, linha 1845). É idempotente (não duplica Payment pago do mesmo walk).
- `backend/app/services/tenant_saas_billing_service.py` — `ensure_tenant_asaas_customer(...)` (cria/recupera customer Asaas do tenant) e o padrão de cobrança do Projeto B. `sweep_overdue_tenants` mostra o padrão de job interno.
- `backend/app/routes/payments.py:838` — `_handle_tenant_saas_subscription_webhook` mostra como o webhook roteia por `externalReference` com prefixo (`tenant_sub:`). O endpoint interno `POST /payments/internal/saas-billing/sweep` (token `INTERNAL_SWEEP_TOKEN`) mostra o padrão de gatilho.
- Convenção de teste: `backend/tests/test_*.py`, SQLite in-memory via `Base.metadata.create_all(engine)`; ver `backend/tests/test_tenant_saas_billing.py` (`_make_db`, `_acoro`).

---

## File Structure

- **Create** `backend/app/models/commission_entry.py` — model `CommissionEntry` (ledger imutável de comissão por passeio).
- **Create** `backend/alembic/versions/0063_commission_entries.py` — tabela `commission_entries`.
- **Create** `backend/app/services/commission_billing_service.py` — acúmulo + faturamento mensal.
- **Create** `backend/tests/test_commission_billing.py` — testes do serviço.
- **Modify** `backend/app/services/shared_walk_service.py:192` — corrigir bug do `walker_id` ausente.
- **Modify** `backend/app/routes/admin.py:268-295` — acumular `CommissionEntry` na finalização.
- **Modify** `backend/app/routes/payments.py` — endpoint interno de billing + roteamento de webhook `tenant_comm:`.

> Verifique antes de criar a migration: rode `Get-ChildItem backend/alembic/versions` e confirme que `0062_tax_regime` é a última. Se houver número maior, use o próximo sequencial e ajuste `down_revision`.

---

### Task 1: Bug fix — passeio compartilhado não recebe taxa de rede

**Files:**
- Modify: `backend/app/services/shared_walk_service.py:192`
- Test: `backend/tests/test_shared_walk_split_walker_id.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_shared_walk_split_walker_id.py
import inspect
from app.services import shared_walk_service

def test_shared_walk_passes_walker_id_to_build_split():
    """O call site de passeio compartilhado deve passar walker_id para build_payment_split,
    senão a taxa de rede (18/10%) nunca é aplicada mesmo com PRICING_V2_ENABLED=True."""
    src = inspect.getsource(shared_walk_service)
    # localiza a chamada a build_payment_split e garante que walker_id é passado
    assert "build_payment_split(" in src
    call = src[src.index("build_payment_split("):]
    call = call[: call.index(")") + 1]
    assert "walker_id" in call, f"walker_id ausente na chamada: {call}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; python -m pytest tests/test_shared_walk_split_walker_id.py -v`
Expected: FAIL — `walker_id ausente na chamada`.

- [ ] **Step 3: Fix the call site**

Abra `backend/app/services/shared_walk_service.py` na linha ~192. A chamada atual é parecida com:

```python
split = build_payment_split(db, tenant.id, amount)
```

Identifique a variável do passeador no escopo (o walk em questão). Troque por:

```python
split = build_payment_split(
    db, tenant.id, amount, walker_id=(walk.walker_id or walk.assigned_walker_id)
)
```

> Se a variável do walk tiver outro nome no escopo (ex.: `shared_walk`, `w`), use o nome correto. Confirme que existe um objeto Walk acessível ali; se não houver, suba a resolução do walker do registro `TenantWalkerAccess`/parâmetro que originou o split. O objetivo: passar o `walker_id` do passeador que realizou o passeio.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend; python -m pytest tests/test_shared_walk_split_walker_id.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing split suite (zero regressão)**

Run: `cd backend; python -m pytest tests/test_payment_split.py tests/test_payment_split_service.py tests/test_commission_by_plan.py -v`
Expected: PASS (todos).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/shared_walk_service.py backend/tests/test_shared_walk_split_walker_id.py
git commit -m "fix(split): passar walker_id no split do passeio compartilhado (habilita taxa de rede)"
```

---

### Task 2: Model `CommissionEntry` + migration

**Files:**
- Create: `backend/app/models/commission_entry.py`
- Create: `backend/alembic/versions/0063_commission_entries.py`
- Test: `backend/tests/test_commission_entry_model.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_commission_entry_model.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401  (registra todos os models no Base)
from app.core.database import Base
from app.models.commission_entry import CommissionEntry, COMM_ACCRUED

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def test_commission_entry_persists_snapshot():
    db = _db()
    e = CommissionEntry(
        id="ce-1", tenant_id="t1", walk_id="w1", period="2026-06",
        walk_price=30.0, commission_percent=10.0, amount=3.0,
        is_network=False, status=COMM_ACCRUED,
    )
    db.add(e); db.commit()
    got = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    assert got.amount == 3.0
    assert got.commission_percent == 10.0
    assert got.status == COMM_ACCRUED
    assert got.is_network is False

def test_walk_id_is_unique():
    import pytest
    from sqlalchemy.exc import IntegrityError
    db = _db()
    db.add(CommissionEntry(id="a", tenant_id="t1", walk_id="dup", period="2026-06",
                           walk_price=10, commission_percent=10, amount=1, is_network=False, status=COMM_ACCRUED))
    db.commit()
    db.add(CommissionEntry(id="b", tenant_id="t1", walk_id="dup", period="2026-06",
                           walk_price=10, commission_percent=10, amount=1, is_network=False, status=COMM_ACCRUED))
    with pytest.raises(IntegrityError):
        db.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; python -m pytest tests/test_commission_entry_model.py -v`
Expected: FAIL — `ModuleNotFoundError: app.models.commission_entry`.

- [ ] **Step 3: Create the model**

```python
# backend/app/models/commission_entry.py
"""Ledger imutável de comissão medida do tenant (Fase 1).

Uma entrada por passeio finalizado de passeador PRÓPRIO do tenant. Snapshot da
taxa resolvida no momento da finalização (variável: par/tenant/plano). Passeio de
REDE não gera entrada aqui (margem capturada no preço do crédito — Fase 2).
"""
from sqlalchemy import Boolean, Column, Float, String, DateTime, Index
from sqlalchemy.sql import func

from app.core.database import Base

# status do ciclo de cobrança
COMM_ACCRUED = "accrued"   # passeio medido, ainda não faturado
COMM_BILLED = "billed"     # incluído numa cobrança Asaas emitida
COMM_PAID = "paid"         # cobrança paga pelo tenant
COMM_VOID = "void"         # estornado/cancelado (ajuste manual)


class CommissionEntry(Base):
    __tablename__ = "commission_entries"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    walk_id = Column(String, nullable=False, unique=True)  # idempotência: 1 entrada por passeio
    period = Column(String, nullable=False, index=True)     # "YYYY-MM" da finalização

    walk_price = Column(Float, nullable=False)              # base medida (catálogo)
    commission_percent = Column(Float, nullable=False)      # taxa RESOLVIDA (snapshot)
    amount = Column(Float, nullable=False)                  # walk_price * commission_percent/100
    is_network = Column(Boolean, nullable=False, default=False)

    status = Column(String, nullable=False, default=COMM_ACCRUED, index=True)
    asaas_payment_id = Column(String, nullable=True)        # cobrança que faturou esta entrada
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    billed_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)


Index("ix_commission_entries_tenant_period_status",
      CommissionEntry.tenant_id, CommissionEntry.period, CommissionEntry.status)
```

- [ ] **Step 4: Register the model**

Abra `backend/app/models/__init__.py` e adicione, junto dos outros imports de model:

```python
from app.models.commission_entry import CommissionEntry  # noqa: F401
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend; python -m pytest tests/test_commission_entry_model.py -v`
Expected: PASS (ambos os testes).

- [ ] **Step 6: Create the migration**

```python
# backend/alembic/versions/0063_commission_entries.py
"""commission_entries: ledger de comissão medida do tenant (Fase 1)

Revision ID: 0063_commission_entries
Revises: 0062_tax_regime
"""
import sqlalchemy as sa
from alembic import op

revision = "0063_commission_entries"
down_revision = "0062_tax_regime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commission_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("walk_id", sa.String(), nullable=False),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("walk_price", sa.Float(), nullable=False),
        sa.Column("commission_percent", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("is_network", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(), nullable=False, server_default="accrued"),
        sa.Column("asaas_payment_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("billed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_commission_entries_walk_id", "commission_entries", ["walk_id"])
    op.create_index("ix_commission_entries_tenant_id", "commission_entries", ["tenant_id"])
    op.create_index("ix_commission_entries_period", "commission_entries", ["period"])
    op.create_index("ix_commission_entries_status", "commission_entries", ["status"])
    op.create_index(
        "ix_commission_entries_tenant_period_status",
        "commission_entries", ["tenant_id", "period", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_commission_entries_tenant_period_status", table_name="commission_entries")
    op.drop_index("ix_commission_entries_status", table_name="commission_entries")
    op.drop_index("ix_commission_entries_period", table_name="commission_entries")
    op.drop_index("ix_commission_entries_tenant_id", table_name="commission_entries")
    op.drop_constraint("uq_commission_entries_walk_id", "commission_entries", type_="unique")
    op.drop_table("commission_entries")
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/commission_entry.py backend/app/models/__init__.py backend/alembic/versions/0063_commission_entries.py backend/tests/test_commission_entry_model.py
git commit -m "feat(commission): model + migration do ledger de comissao medida"
```

---

### Task 3: Serviço de acúmulo (`accrue_commission_for_walk`)

**Files:**
- Create: `backend/app/services/commission_billing_service.py`
- Test: `backend/tests/test_commission_billing.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_commission_billing.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.commission_entry import CommissionEntry, COMM_ACCRUED

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

class _Walk:
    def __init__(self, id, tenant_id, walker_id, price, status="Finalizado"):
        self.id = id; self.tenant_id = tenant_id; self.walker_id = walker_id
        self.assigned_walker_id = None; self.price = price; self.status = status

def test_accrue_creates_entry_for_own_walker():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w1", "t1", "k1", 30.0)
    split = {"commission_percent": 10.0, "platform_amount": 3.0, "walker_amount": 27.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06")
    db.commit()
    e = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    assert e.amount == 3.0 and e.commission_percent == 10.0
    assert e.status == COMM_ACCRUED and e.is_network is False

def test_accrue_is_idempotent():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w1", "t1", "k1", 30.0)
    split = {"commission_percent": 10.0, "platform_amount": 3.0, "walker_amount": 27.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w1").count() == 1

def test_accrue_skips_network_walk():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w2", "t1", "k1", 30.0)
    split = {"commission_percent": 18.0, "platform_amount": 5.4, "walker_amount": 24.6}
    accrue_commission_for_walk(db, walk, split, is_network=True, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w2").count() == 0

def test_accrue_skips_zero_price():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w3", "t1", "k1", 0.0)
    split = {"commission_percent": 10.0, "platform_amount": 0.0, "walker_amount": 0.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w3").count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; python -m pytest tests/test_commission_billing.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.commission_billing_service`.

- [ ] **Step 3: Implement the accrual function**

```python
# backend/app/services/commission_billing_service.py
"""Acúmulo e faturamento da comissão medida do tenant (Fase 1).

Princípio: MEDIÇÃO ≠ CUSTÓDIA. O valor vem de Walk.price × taxa resolvida; o
Aumigão nunca toca no pagamento do tutor. Passeio de REDE não acumula aqui.
"""
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.commission_entry import (
    CommissionEntry, COMM_ACCRUED, COMM_BILLED, COMM_PAID,
)


def accrue_commission_for_walk(
    db: Session, walk, split: dict, *, is_network: bool, period: str
) -> CommissionEntry | None:
    """Cria (idempotente) a entrada de comissão para um passeio finalizado.

    - Só acumula passeio de passeador PRÓPRIO (is_network=False).
    - Não acumula preço zero.
    - Idempotente por walk_id (uq constraint + checagem prévia).
    Não faz commit — o caller comita junto da finalização.
    """
    if is_network:
        return None
    price = float(getattr(walk, "price", 0) or 0)
    if price <= 0:
        return None
    existing = db.query(CommissionEntry).filter(CommissionEntry.walk_id == walk.id).first()
    if existing:
        return existing
    amount = round(float(split.get("platform_amount", 0.0)), 2)
    entry = CommissionEntry(
        id=str(uuid4()),
        tenant_id=walk.tenant_id,
        walk_id=walk.id,
        period=period,
        walk_price=price,
        commission_percent=float(split.get("commission_percent", 0.0)),
        amount=amount,
        is_network=False,
        status=COMM_ACCRUED,
    )
    db.add(entry)
    return entry
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend; python -m pytest tests/test_commission_billing.py -v`
Expected: PASS (4 testes).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/commission_billing_service.py backend/tests/test_commission_billing.py
git commit -m "feat(commission): acumulo idempotente de comissao por passeio proprio"
```

---

### Task 4: Wire do acúmulo na finalização

**Files:**
- Modify: `backend/app/routes/admin.py:268-295` (`_ensure_internal_walk_payment`)
- Test: `backend/tests/test_commission_accrual_on_finalize.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_commission_accrual_on_finalize.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.commission_entry import CommissionEntry
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.routes.admin import _ensure_internal_walk_payment

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro"))
    db.commit()
    return db

def test_finalize_accrues_commission_entry():
    db = _db()
    walk = Walk(id="w1", tenant_id="t1", tutor_id="tut1", walker_id="k1",
                price=40.0, status="Finalizado")
    db.add(walk); db.commit()
    _ensure_internal_walk_payment(walk, db)
    db.commit()
    e = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    # Pro own-walker fallback = 10% (PRICING_V2 off ou on → 10% para 'pro')
    assert e.amount == 4.0
    assert e.commission_percent == 10.0
    assert e.is_network is False
```

> **Nota:** ajuste os kwargs do construtor `Walk(...)` aos campos reais do model (`backend/app/models/walk.py`). Os essenciais para o teste: `id, tenant_id, tutor_id, walker_id, price, status`. Se houver colunas NOT NULL adicionais, preencha com valores mínimos válidos.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; python -m pytest tests/test_commission_accrual_on_finalize.py -v`
Expected: FAIL — nenhuma `CommissionEntry` criada (`NoResultFound`).

- [ ] **Step 3: Wire accrual into `_ensure_internal_walk_payment`**

Em `backend/app/routes/admin.py`, no topo do arquivo, adicione aos imports:

```python
from app.services.commission_billing_service import accrue_commission_for_walk
from app.services.payment_split_service import is_network_walk
```

Em `_ensure_internal_walk_payment` (linha ~268), **após** `db.add(payment)` e **antes** do `return payment`, insira:

```python
    # Fase 1: acumula a comissão medida do tenant (só passeador PRÓPRIO).
    # Reusa a taxa já resolvida em `split`; period = mês da finalização.
    _walker_id = walk.walker_id or walk.assigned_walker_id
    _is_network = is_network_walk(db, walk.tenant_id, _walker_id)
    _period = (walk.date or walk.created_at or _now_utc()).strftime("%Y-%m") \
        if getattr(walk, "date", None) or getattr(walk, "created_at", None) else _fallback_period()
    accrue_commission_for_walk(db, walk, split, is_network=_is_network, period=_period)
```

> **Period:** use a data de realização do passeio. Verifique o nome real do campo no model `Walk` (`date`, `scheduled_at`, `created_at`...). Se existir helper de "agora" no arquivo, use-o; senão, defina no topo do módulo:
> ```python
> from datetime import datetime, timezone
> def _now_utc():
>     return datetime.now(timezone.utc)
> def _fallback_period():
>     return _now_utc().strftime("%Y-%m")
> ```
> Não use `Date.now()` mágico — use o campo do passeio para o period ser estável em reprocessamento.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend; python -m pytest tests/test_commission_accrual_on_finalize.py -v`
Expected: PASS.

- [ ] **Step 5: Run the finalization/admin suite (zero regressão)**

Run: `cd backend; python -m pytest tests/test_walk_payment_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/admin.py backend/tests/test_commission_accrual_on_finalize.py
git commit -m "feat(commission): acumular comissao medida na finalizacao do passeio"
```

---

### Task 5: Faturamento mensal (`bill_tenant_commission` + `run_monthly_commission_billing`)

**Files:**
- Modify: `backend/app/services/commission_billing_service.py`
- Test: `backend/tests/test_commission_billing.py` (append)

- [ ] **Step 1: Write the failing test (append ao arquivo existente)**

```python
# backend/tests/test_commission_billing.py  (adicionar)
from datetime import datetime, timezone
from app.models.tenant import Tenant
from app.models.commission_entry import COMM_BILLED, COMM_PAID

def _db_with_tenant():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro",
                  document_number="11222333000181", contact_email="fin@x.com"))
    db.commit()
    return db

def _seed_entry(db, walk_id, amount, period="2026-06", status="accrued", tenant_id="t1"):
    from app.models.commission_entry import CommissionEntry
    db.add(CommissionEntry(id="ce-" + walk_id, tenant_id=tenant_id, walk_id=walk_id,
                           period=period, walk_price=amount * 10, commission_percent=10.0,
                           amount=amount, is_network=False, status=status))
    db.commit()

def test_bill_aggregates_accrued_and_marks_billed():
    from app.services.commission_billing_service import bill_tenant_commission
    db = _db_with_tenant()
    _seed_entry(db, "w1", 3.0); _seed_entry(db, "w2", 4.5)
    captured = {}
    def fake_charge(db_, tenant, total, period, description):
        captured.update(total=total, period=period, tenant=tenant.id)
        return "asaas-charge-1"
    charge = bill_tenant_commission(db, "t1", "2026-06", charge_fn=fake_charge)
    db.commit()
    assert captured["total"] == 7.5
    assert charge == "asaas-charge-1"
    from app.models.commission_entry import CommissionEntry
    rows = db.query(CommissionEntry).filter_by(tenant_id="t1", period="2026-06").all()
    assert all(r.status == COMM_BILLED and r.asaas_payment_id == "asaas-charge-1" for r in rows)

def test_bill_noop_when_nothing_accrued():
    from app.services.commission_billing_service import bill_tenant_commission
    db = _db_with_tenant()
    called = {"n": 0}
    def fake_charge(*a, **k):
        called["n"] += 1; return "x"
    assert bill_tenant_commission(db, "t1", "2026-06", charge_fn=fake_charge) is None
    assert called["n"] == 0

def test_bill_ignores_already_billed():
    from app.services.commission_billing_service import bill_tenant_commission
    db = _db_with_tenant()
    _seed_entry(db, "w1", 3.0, status="billed")
    def fake_charge(*a, **k):
        raise AssertionError("não deveria cobrar — já faturado")
    assert bill_tenant_commission(db, "t1", "2026-06", charge_fn=fake_charge) is None

def test_run_monthly_bills_each_tenant_with_accrued():
    from app.services.commission_billing_service import run_monthly_commission_billing
    db = _db_with_tenant()
    db.add(Tenant(id="t2", name="Y", slug="y", status="active", plan="enterprise",
                  document_number="99888777000166", contact_email="fin@y.com")); db.commit()
    _seed_entry(db, "w1", 3.0, tenant_id="t1")
    _seed_entry(db, "w2", 5.0, tenant_id="t2")
    billed = []
    def fake_charge(db_, tenant, total, period, description):
        billed.append((tenant.id, total)); return "c-" + tenant.id
    run_monthly_commission_billing(db, "2026-06", charge_fn=fake_charge)
    db.commit()
    assert sorted(billed) == [("t1", 3.0), ("t2", 5.0)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend; python -m pytest tests/test_commission_billing.py -k "bill or run_monthly" -v`
Expected: FAIL — `bill_tenant_commission` / `run_monthly_commission_billing` não existem.

- [ ] **Step 3: Implement billing (append ao serviço)**

```python
# backend/app/services/commission_billing_service.py  (adicionar)
from datetime import datetime, timezone

from sqlalchemy import func


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def bill_tenant_commission(db: Session, tenant_id: str, period: str, *, charge_fn) -> str | None:
    """Soma as entradas `accrued` do tenant no período, emite UMA cobrança via
    `charge_fn` e marca as entradas como `billed`. Retorna o id da cobrança ou None.

    `charge_fn(db, tenant, total, period, description) -> asaas_payment_id` é injetável
    (testes passam fake; produção passa o adaptador Asaas — ver Task 6).
    Não faz commit.
    """
    from app.models.tenant import Tenant

    rows = (
        db.query(CommissionEntry)
        .filter(
            CommissionEntry.tenant_id == tenant_id,
            CommissionEntry.period == period,
            CommissionEntry.status == COMM_ACCRUED,
        )
        .all()
    )
    if not rows:
        return None
    total = round(sum(float(r.amount) for r in rows), 2)
    if total <= 0:
        return None
    tenant = db.get(Tenant, tenant_id)
    description = f"Comissão de uso Aumigão — {period} ({len(rows)} passeios)"
    asaas_payment_id = charge_fn(db, tenant, total, period, description)
    now = _now_utc()
    for r in rows:
        r.status = COMM_BILLED
        r.asaas_payment_id = asaas_payment_id
        r.billed_at = now
    return asaas_payment_id


def run_monthly_commission_billing(db: Session, period: str, *, charge_fn) -> list[str]:
    """Fatura todos os tenants com comissão `accrued` no período. Retorna ids das cobranças."""
    tenant_ids = [
        row[0]
        for row in db.query(CommissionEntry.tenant_id)
        .filter(CommissionEntry.period == period, CommissionEntry.status == COMM_ACCRUED)
        .group_by(CommissionEntry.tenant_id)
        .all()
    ]
    out: list[str] = []
    for tid in tenant_ids:
        cid = bill_tenant_commission(db, tid, period, charge_fn=charge_fn)
        if cid:
            out.append(cid)
    return out


def mark_commission_paid(db: Session, asaas_payment_id: str) -> int:
    """Webhook: marca como `paid` todas as entradas faturadas por esta cobrança.
    Retorna quantas linhas mudaram. Idempotente. Não faz commit."""
    rows = (
        db.query(CommissionEntry)
        .filter(
            CommissionEntry.asaas_payment_id == asaas_payment_id,
            CommissionEntry.status == COMM_BILLED,
        )
        .all()
    )
    now = _now_utc()
    for r in rows:
        r.status = COMM_PAID
        r.paid_at = now
    return len(rows)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend; python -m pytest tests/test_commission_billing.py -v`
Expected: PASS (todos).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/commission_billing_service.py backend/tests/test_commission_billing.py
git commit -m "feat(commission): faturamento mensal agregado + marcacao billed/paid"
```

---

### Task 6: Adaptador Asaas + endpoint interno + webhook

**Files:**
- Modify: `backend/app/services/commission_billing_service.py` (adaptador de cobrança real)
- Modify: `backend/app/routes/payments.py` (endpoint interno + roteamento de webhook `tenant_comm:`)
- Test: `backend/tests/test_commission_billing_endpoint.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_commission_billing_endpoint.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.models.commission_entry import CommissionEntry, COMM_PAID

def _client_and_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    from app.routes import payments as payments_routes
    app = FastAPI()
    app.include_router(payments_routes.router)
    app.dependency_overrides[get_db] = lambda: Session()
    return TestClient(app), db

def test_webhook_marks_commission_paid():
    os.environ["INTERNAL_SWEEP_TOKEN"] = "secret"
    client, db = _client_and_db()
    db.add(CommissionEntry(id="ce1", tenant_id="t1", walk_id="w1", period="2026-06",
                           walk_price=30, commission_percent=10, amount=3.0,
                           is_network=False, status="billed", asaas_payment_id="pay-9"))
    db.commit()
    # Evento Asaas de pagamento confirmado com externalReference tenant_comm:
    payload = {"event": "PAYMENT_RECEIVED",
               "payment": {"id": "pay-9", "externalReference": "tenant_comm:t1:2026-06"}}
    r = client.post("/payments/webhook", json=payload)
    assert r.status_code in (200, 204)
    db.expire_all()
    assert db.query(CommissionEntry).filter_by(id="ce1").one().status == COMM_PAID
```

> **Nota:** confirme a rota real do webhook Asaas em `payments.py` (pode ser `/payments/webhook` ou similar) e ajuste o path. Confirme também o formato do payload que o handler já parseia (reuse o mesmo shape de `_handle_tenant_saas_subscription_webhook`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; python -m pytest tests/test_commission_billing_endpoint.py -v`
Expected: FAIL — webhook não roteia `tenant_comm:` (entrada continua `billed`).

- [ ] **Step 3: Implement the Asaas charge adapter**

No `commission_billing_service.py`, adicione o adaptador de produção (cobrança avulsa Asaas), reusando o cliente/infra do Projeto B:

```python
# backend/app/services/commission_billing_service.py  (adicionar)
def make_asaas_charge_fn():
    """Retorna um charge_fn que cria uma cobrança avulsa no Asaas para a comissão.

    Reusa ensure_tenant_asaas_customer (Projeto B) e o cliente HTTP Asaas existente.
    externalReference = 'tenant_comm:<tenant>:<period>' para o webhook reconhecer.
    """
    from app.services.tenant_saas_billing_service import ensure_tenant_asaas_customer
    from app.services.asaas_client import create_payment  # ver nota abaixo

    def charge_fn(db, tenant, total, period, description):
        customer_id = ensure_tenant_asaas_customer(db, tenant)
        resp = create_payment(
            customer=customer_id,
            billing_type="PIX",
            value=total,
            description=description,
            external_reference=f"tenant_comm:{tenant.id}:{period}",
        )
        return resp["id"]

    return charge_fn
```

> **Adaptador Asaas:** o nome exato do helper de criação de cobrança avulsa varia. Procure em `tenant_saas_billing_service.py` / `payments.py` a função que faz `POST /payments` no Asaas (a mesma usada para criar a cobrança da assinatura) e reuse-a. O contrato necessário: criar uma cobrança PIX para `customer_id`, com `value`, `description` e `external_reference`. Se o cliente Asaas estiver inline em `payments.py`, extraia uma função fina `create_payment(...)` para `asaas_client.py` (ou reuse a existente) e importe aqui. NÃO duplicar a integração.

- [ ] **Step 4: Add the internal billing endpoint**

Em `backend/app/routes/payments.py`, ao lado do endpoint de sweep do Projeto B, adicione:

```python
@router.post("/payments/internal/commission-billing/run")
def run_commission_billing_endpoint(period: str, x_internal_token: str = Header(default="")):
    """Dispara o faturamento mensal da comissão medida. Protegido por INTERNAL_SWEEP_TOKEN.
    `period` = 'YYYY-MM' (geralmente o mês anterior). Idempotente: só fatura entradas `accrued`."""
    import os
    expected = os.getenv("INTERNAL_SWEEP_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    from app.core.database import SessionLocal
    from app.services.commission_billing_service import (
        run_monthly_commission_billing, make_asaas_charge_fn,
    )
    db = SessionLocal()
    try:
        ids = run_monthly_commission_billing(db, period, charge_fn=make_asaas_charge_fn())
        db.commit()
        return {"period": period, "charges_created": len(ids)}
    finally:
        db.close()
```

> Reuse o mesmo padrão de autenticação/sessão do endpoint `POST /payments/internal/saas-billing/sweep` já existente (mesmo nome de header e helper de sessão). Ajuste imports (`Header`, `HTTPException`, `SessionLocal`) ao que o arquivo já usa.

- [ ] **Step 5: Route the webhook**

No handler de webhook Asaas de `payments.py`, onde já existe o roteamento por prefixo de `externalReference` (`tenant_sub:`), adicione um ramo para `tenant_comm:`:

```python
    external_ref = (payment_obj or {}).get("externalReference") or ""
    if external_ref.startswith("tenant_comm:") and _event_is_paid(event):
        from app.services.commission_billing_service import mark_commission_paid
        mark_commission_paid(db, payment_obj["id"])
        db.commit()
        return {"ok": True}
```

> `_event_is_paid(event)`: reuse a lógica já existente que considera `PAYMENT_RECEIVED`/`PAYMENT_CONFIRMED` como pago (ver `STATUS_BY_WEBHOOK_EVENT` em `payments.py`). Se não houver helper, teste `event in ("PAYMENT_RECEIVED", "PAYMENT_CONFIRMED")`. Posicione este ramo junto aos outros despachos por prefixo, antes do fallback genérico.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend; python -m pytest tests/test_commission_billing_endpoint.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/commission_billing_service.py backend/app/routes/payments.py backend/tests/test_commission_billing_endpoint.py
git commit -m "feat(commission): adaptador Asaas + endpoint interno + webhook tenant_comm"
```

---

### Task 7: Teste de integração ponta-a-ponta

**Files:**
- Test: `backend/tests/test_commission_e2e.py` (Create)

- [ ] **Step 1: Write the end-to-end test**

```python
# backend/tests/test_commission_e2e.py
"""Fluxo completo: finaliza 2 passeios próprios + 1 de rede → acumula só os 2 próprios
→ fatura mensal agrega num único charge → webhook marca pago."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.models.commission_entry import CommissionEntry, COMM_PAID
from app.routes.admin import _ensure_internal_walk_payment
from app.services.commission_billing_service import (
    bill_tenant_commission, mark_commission_paid,
)

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro",
                  document_number="11222333000181", contact_email="fin@x.com"))
    db.commit()
    return db

def test_full_flow_two_own_walks_billed_then_paid():
    db = _db()
    for wid in ("w1", "w2"):
        w = Walk(id=wid, tenant_id="t1", tutor_id="tut", walker_id="k1",
                 price=50.0, status="Finalizado")
        db.add(w); db.commit()
        _ensure_internal_walk_payment(w, db); db.commit()
    # period é derivado do passeio; descubra-o a partir da entrada criada
    period = db.query(CommissionEntry).first().period
    cid = bill_tenant_commission(db, "t1", period, charge_fn=lambda *a, **k: "charge-1")
    db.commit()
    assert cid == "charge-1"
    assert mark_commission_paid(db, "charge-1") == 2
    db.commit()
    rows = db.query(CommissionEntry).filter_by(tenant_id="t1").all()
    assert len(rows) == 2
    assert all(r.status == COMM_PAID for r in rows)
    assert round(sum(r.amount for r in rows), 2) == 10.0  # 2 × (50 × 10%)
```

- [ ] **Step 2: Run the test**

Run: `cd backend; python -m pytest tests/test_commission_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Run the full commission + split suite**

Run: `cd backend; python -m pytest tests/ -k "commission or split or saas" -v`
Expected: PASS (sem regressão nas suites existentes).

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_commission_e2e.py
git commit -m "test(commission): fluxo e2e medicao->faturamento->pago"
```

---

## Operação (pós-implementação)

- **Agendamento mensal:** chamar `POST /payments/internal/commission-billing/run?period=YYYY-MM` no início de cada mês (para o mês anterior), com header `X-Internal-Token: $INTERNAL_SWEEP_TOKEN`. Reusar o mesmo agendador do sweep do Projeto B (Cloud Scheduler).
- **Flag de rede:** a comissão de rede (18/10%) só vale com `PRICING_V2_ENABLED=true`. Para a Fase 1 (só tenant), não é necessária; ligar quando a Fase 2 (rede) entrar.
- **Gate jurídico:** antes de ~R$100k/mês, validar com tributarista o reconhecimento de receita da comissão (ver doc de design).

## Out of scope (Fase 2+)

- Ledger-fornecedor do passeador da rede (`WalkerEarning`) e dissolução do furo D+32.
- PIX automático ao passeador.
- Colunas extras de auditoria no `payments` (`tenant_amount`, `tenant_margin_percent`, `is_network_walk`) — só necessárias quando a Fase 2 precisar do snapshot completo no Payment.
- Tela admin de visualização da comissão acumulada/faturada por período.

---

## Self-Review (feito)

- **Cobertura:** bug do walker_id (T1), ledger+migration (T2), acúmulo idempotente só-próprio só-preço>0 (T3), wire na finalização (T4), faturamento agregado + paid (T5), Asaas+endpoint+webhook (T6), e2e (T7). ✅
- **Sem placeholder de lógica:** todo passo com código real; pontos que dependem de nomes reais do projeto (campo de data do `Walk`, nome do helper Asaas, path do webhook) estão marcados como **Nota** com instrução de verificação — não como "TODO".
- **Consistência de tipos:** `accrue_commission_for_walk(db, walk, split, *, is_network, period)`, `bill_tenant_commission(db, tenant_id, period, *, charge_fn)`, `mark_commission_paid(db, asaas_payment_id)`, `charge_fn(db, tenant, total, period, description)->id` — assinaturas idênticas em todas as tasks/testes. Status: `accrued/billed/paid/void`. ✅

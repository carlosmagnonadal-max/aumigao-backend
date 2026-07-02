# Ledger-Fornecedor do Passeador da Rede (Fase 2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) ou superpowers:executing-plans para executar task-por-task. Passos usam checkbox (`- [ ]`).

**Goal:** Pagar o passeador da **REDE** como FORNECEDOR via um ledger próprio (`WalkerEarning`), com liberação em **cadência semanal** (não atrelada ao status do pagamento do tutor) — dissolvendo o furo D+32 — e corrigir um bug de double-spend no saldo do passeador.

**Architecture:** Na finalização de um passeio de REDE, em vez de creditar `Payment.walker_amount` (que entra direto no "disponível"), cria-se uma entrada imutável `WalkerEarning` com `payable_at` = quarta-feira da semana seguinte à conclusão. As funções de saldo passam a somar o ledger: ganhos com `payable_at <= agora` contam como "disponível"; o resto como "a receber". O passeio do passeador PRÓPRIO segue inalterado (pago pelo pet shop, fora do Aumigão). De quebra, a dedução de saques é corrigida para `{pending, approved, paid}` (exclui `rejected`).

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, pytest (SQLite in-memory).

**Base/branch:** parte de `feature/comissao-medida-tenant` (Fase 1, que já alterou `_ensure_internal_walk_payment`). Branch nova: `feature/walker-earning-fase2`. Migration = `0064` (down_revision `0063_commission_entries`).

**Princípio:** rede = Aumigão recebeu (crédito pré-pago) → Aumigão DEVE ao passeador → ledger. Próprio = pet shop paga → Aumigão fora. **MEDIÇÃO ≠ CUSTÓDIA** continua valendo.

---

## Pré-requisitos de leitura

- `app/routes/walker.py:657` `_balance_by_tenant` e `:740` `_available_balance` — as duas funções de saldo. Leia inteiras antes de editar.
  - `_balance_by_tenant`: soma `Payment.walker_amount` (via JOIN Walk) por tenant; débito = saques (`provider="pix"`, `walk_id IS NULL`, `amount<0`) **sem filtro de status** (BUG: desconta rejeitado).
  - `_available_balance`: soma `Payment.walker_amount` pagos (+ tips + fallback walk.price); débito = saques só em `{pending, approved}` (BUG: não desconta `paid` → double-spend).
  - Constantes no topo de walker.py: `_PAID_PAYMENT_STATUSES_CONST`, `_PENDING_PAYMENT_STATUSES`, `_PROCESSING_PAYMENT_STATUSES`.
- `app/routes/admin.py:268` `_ensure_internal_walk_payment` — já cria `Payment(walker_amount=split["walker_amount"])` e (Fase 1) chama `accrue_commission_for_walk`. Tem `_is_network = is_network_walk(db, walk.tenant_id, _walker_id)` no escopo (Fase 1).
- `app/services/payment_split_service.py:132` `is_network_walk(db, tenant_id, walker_id) -> bool`.
- Convenção de teste: SQLite in-memory, `Base.metadata.create_all`. Ver `tests/test_walker_earnings_by_tenant.py`.
- Testes a NÃO quebrar: `test_walker_earnings_by_tenant.py`, `test_routes_walker_core.py`, `test_routes_admin_finance.py`, `test_multitenant_isolation.py`, `test_recurring_plan_credits.py`, `test_commission_*`.

---

## File Structure

- **Create** `app/models/walker_earning.py` — model `WalkerEarning`.
- **Create** `app/services/walker_earning_service.py` — helper de cadência (`compute_payable_at`) + acúmulo (`accrue_walker_earning`) + soma de saldo (`network_balance_by_tenant`).
- **Create** `alembic/versions/0064_walker_earnings.py`.
- **Modify** `app/routes/admin.py` (`_ensure_internal_walk_payment`) — rede → ledger + zera walker_amount.
- **Modify** `app/routes/walker.py` (`_balance_by_tenant`, `_available_balance`) — somar ledger + corrigir dedução de saque.
- **Create** tests correspondentes.

> Antes da migration, confirme a última na branch: `cd backend && git log --oneline -1 -- alembic/versions/ ; ls alembic/versions/ | sort | tail -3`. Deve ser `0063_commission_entries`. Use `down_revision="0063_commission_entries"`.

---

### Task 1: Model `WalkerEarning` + cadência + migration

**Files:**
- Create: `app/models/walker_earning.py`
- Create: `app/services/walker_earning_service.py` (só o helper de cadência nesta task)
- Create: `alembic/versions/0064_walker_earnings.py`
- Test: `tests/test_walker_earning_model.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_walker_earning_model.py
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.walker_earning import WalkerEarning, WE_ACCRUED
from app.services.walker_earning_service import compute_payable_at

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def test_persists():
    db = _db()
    db.add(WalkerEarning(id="we1", walker_id="k1", tenant_id="t1", walk_id="w1",
                         gross=30.0, platform_amount=5.4, amount=24.6,
                         status=WE_ACCRUED,
                         accrued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                         payable_at=datetime(2026, 6, 10, tzinfo=timezone.utc)))
    db.commit()
    assert db.query(WalkerEarning).filter_by(walk_id="w1").one().amount == 24.6

def test_walk_id_unique():
    import pytest
    from sqlalchemy.exc import IntegrityError
    db = _db()
    for i in (1, 2):
        db.add(WalkerEarning(id=f"a{i}", walker_id="k1", tenant_id="t1", walk_id="dup",
                             gross=10, platform_amount=1, amount=9, status=WE_ACCRUED,
                             accrued_at=datetime(2026,6,1,tzinfo=timezone.utc),
                             payable_at=datetime(2026,6,10,tzinfo=timezone.utc)))
    with pytest.raises(IntegrityError):
        db.commit()

def test_payable_at_is_wednesday_of_next_week():
    # quarta 2026-06-10 (qualquer dia da semana de 08..14 jun -> quarta da semana seguinte = 17 jun)
    # semana de seg 2026-06-08 a dom 2026-06-14; quarta da semana seguinte = 2026-06-17
    got = compute_payable_at(datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc))
    assert got.year == 2026 and got.month == 6 and got.day == 17
    assert got.weekday() == 2  # quarta
    # domingo 2026-06-14 ainda é da mesma semana -> mesma quarta seguinte 2026-06-17
    got2 = compute_payable_at(datetime(2026, 6, 14, 23, 59, tzinfo=timezone.utc))
    assert got2.day == 17
    # segunda 2026-06-15 já é semana nova -> quarta seguinte = 2026-06-24
    got3 = compute_payable_at(datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc))
    assert got3.day == 24
```

- [ ] **Step 2: Run, verify fail**

Run: `cd backend && python -m pytest tests/test_walker_earning_model.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create the model**

```python
# backend/app/models/walker_earning.py
"""Ledger-fornecedor do passeador da REDE (Fase 2).

Uma entrada por passeio de REDE finalizado. O Aumigão DEVE este valor ao passeador
(que prestou serviço); liberação em cadência semanal via `payable_at` — desacoplada
do status do pagamento do tutor (dissolve o furo D+32). Passeio PRÓPRIO do tenant
NÃO entra aqui (é pago pelo pet shop).
"""
from sqlalchemy import Column, Float, String, DateTime
from sqlalchemy.sql import func

from app.core.database import Base

WE_ACCRUED = "accrued"   # registrado; disponibilidade definida por payable_at
WE_VOID = "void"         # estornado (reembolso/disputa) — Fase futura


class WalkerEarning(Base):
    __tablename__ = "walker_earnings"

    id = Column(String, primary_key=True)
    walker_id = Column(String, nullable=False, index=True)
    tenant_id = Column(String, nullable=True, index=True)
    walk_id = Column(String, nullable=False, unique=True)  # idempotência: 1 por passeio

    gross = Column(Float, nullable=False)            # preço do passeio (medido)
    platform_amount = Column(Float, nullable=False)  # margem do Aumigão (rede)
    amount = Column(Float, nullable=False)           # fatia do passeador (o que devemos)

    status = Column(String, nullable=False, default=WE_ACCRUED, index=True)
    accrued_at = Column(DateTime(timezone=True), server_default=func.now())
    payable_at = Column(DateTime(timezone=True), nullable=False)  # quando vira "disponível"
```

- [ ] **Step 4: Create the cadence helper**

```python
# backend/app/services/walker_earning_service.py
"""Serviço do ledger-fornecedor do passeador da rede (Fase 2)."""
from datetime import datetime, timedelta, timezone


def compute_payable_at(completion_dt: datetime) -> datetime:
    """Cadência SEMANAL: ganhos de passeios concluídos numa semana (seg–dom)
    ficam disponíveis na QUARTA-FEIRA da semana SEGUINTE.

    Determinístico (não usa 'now'): depende só da data de conclusão.
    Retorna datetime tz-aware (UTC) à meia-noite da quarta-feira alvo.
    """
    d = completion_dt.date()
    monday_this_week = d - timedelta(days=d.weekday())  # weekday(): seg=0
    wednesday_next_week = monday_this_week + timedelta(days=7 + 2)
    return datetime(
        wednesday_next_week.year, wednesday_next_week.month, wednesday_next_week.day,
        tzinfo=timezone.utc,
    )
```

- [ ] **Step 5: Register model**

Em `app/models/__init__.py`, junto dos outros imports:
```python
from app.models.walker_earning import WalkerEarning  # noqa: F401
```

- [ ] **Step 6: Run, verify pass**

Run: `cd backend && python -m pytest tests/test_walker_earning_model.py -v`
Expected: PASS (3 testes).

- [ ] **Step 7: Migration**

```python
# backend/alembic/versions/0064_walker_earnings.py
"""walker_earnings: ledger-fornecedor do passeador da rede (Fase 2)

Revision ID: 0064_walker_earnings
Revises: 0063_commission_entries
"""
import sqlalchemy as sa
from alembic import op

revision = "0064_walker_earnings"
down_revision = "0063_commission_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "walker_earnings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("walker_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("walk_id", sa.String(), nullable=False),
        sa.Column("gross", sa.Float(), nullable=False),
        sa.Column("platform_amount", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="accrued"),
        sa.Column("accrued_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("payable_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_walker_earnings_walk_id", "walker_earnings", ["walk_id"])
    op.create_index("ix_walker_earnings_walker_id", "walker_earnings", ["walker_id"])
    op.create_index("ix_walker_earnings_tenant_id", "walker_earnings", ["tenant_id"])
    op.create_index("ix_walker_earnings_status", "walker_earnings", ["status"])


def downgrade() -> None:
    op.drop_index("ix_walker_earnings_status", table_name="walker_earnings")
    op.drop_index("ix_walker_earnings_tenant_id", table_name="walker_earnings")
    op.drop_index("ix_walker_earnings_walker_id", table_name="walker_earnings")
    op.drop_constraint("uq_walker_earnings_walk_id", "walker_earnings", type_="unique")
    op.drop_table("walker_earnings")
```

- [ ] **Step 8: Commit**

```bash
cd backend && git add app/models/walker_earning.py app/models/__init__.py app/services/walker_earning_service.py alembic/versions/0064_walker_earnings.py tests/test_walker_earning_model.py && git commit -m "feat(walker-earning): model + cadencia semanal + migration 0064"
```

---

### Task 2: Acúmulo do ledger na finalização (rede → ledger, zera walker_amount)

**Files:**
- Modify: `app/services/walker_earning_service.py` (add `accrue_walker_earning`)
- Modify: `app/routes/admin.py` (`_ensure_internal_walk_payment`)
- Test: `tests/test_walker_earning_accrual.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_walker_earning_accrual.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.models.payment import Payment
from app.models.walker_earning import WalkerEarning
from app.models.tenant_walker_access import TenantWalkerAccess
from app.routes.admin import _ensure_internal_walk_payment

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro"))
    db.commit()
    return db

def _mk_walk(db, wid, walker_id, price=30.0):
    w = Walk(id=wid, tenant_id="t1", tutor_id="tut", walker_id=walker_id,
             price=price, status="Finalizado", scheduled_date="2026-06-10T10:00")
    db.add(w); db.commit()
    return w

def test_network_walk_creates_earning_and_zeroes_payment_walker_amount():
    db = _db()
    db.add(TenantWalkerAccess(id="twa1", tenant_id="t1", walker_user_id="k1",
                              access_type="shared_network", status="active"))
    db.commit()
    w = _mk_walk(db, "w1", "k1")
    _ensure_internal_walk_payment(w, db); db.commit()
    earning = db.query(WalkerEarning).filter_by(walk_id="w1").one()
    assert earning.amount > 0 and earning.walker_id == "k1"
    pay = db.query(Payment).filter_by(walk_id="w1").one()
    assert (pay.walker_amount or 0) == 0  # rede NÃO credita walker_amount (evita dupla contagem)

def test_own_walk_unchanged_no_earning():
    db = _db()
    w = _mk_walk(db, "w2", "k2")  # sem TenantWalkerAccess => não-rede
    _ensure_internal_walk_payment(w, db); db.commit()
    assert db.query(WalkerEarning).filter_by(walk_id="w2").count() == 0
    pay = db.query(Payment).filter_by(walk_id="w2").one()
    assert (pay.walker_amount or 0) > 0  # próprio: comportamento atual mantido

def test_accrual_idempotent():
    db = _db()
    db.add(TenantWalkerAccess(id="twa2", tenant_id="t1", walker_user_id="k1",
                              access_type="shared_network", status="active"))
    db.commit()
    w = _mk_walk(db, "w3", "k1")
    _ensure_internal_walk_payment(w, db); db.commit()
    _ensure_internal_walk_payment(w, db); db.commit()
    assert db.query(WalkerEarning).filter_by(walk_id="w3").count() == 1
```

- [ ] **Step 2: Run, verify fail**

Run: `cd backend && python -m pytest tests/test_walker_earning_accrual.py -v`
Expected: FAIL (sem WalkerEarning criado / walker_amount não zerado).

- [ ] **Step 3: Add `accrue_walker_earning` ao serviço**

```python
# backend/app/services/walker_earning_service.py  (adicionar)
from uuid import uuid4
from sqlalchemy.orm import Session
from app.models.walker_earning import WalkerEarning, WE_ACCRUED


def _completion_dt_from_walk(walk):
    """Deriva a data de conclusão do passeio (scheduled_date 'YYYY-MM-DD[THH:MM]' ou created_at, fallback now)."""
    sd = getattr(walk, "scheduled_date", None)
    if sd and isinstance(sd, str) and len(sd) >= 10:
        try:
            return datetime.fromisoformat(sd[:16]) if "T" in sd else datetime.fromisoformat(sd[:10])
        except ValueError:
            pass
    created = getattr(walk, "created_at", None)
    if isinstance(created, datetime):
        return created
    return datetime.now(timezone.utc)


def accrue_walker_earning(db: Session, walk, split: dict) -> WalkerEarning | None:
    """Cria (idempotente) a entrada de ganho do passeador da REDE.

    amount = fatia do passeador (split['walker_amount']); platform_amount = margem.
    payable_at = cadência semanal. Não faz commit (caller comita).
    Só deve ser chamado para passeio de REDE (o caller decide via is_network_walk).
    """
    price = float(getattr(walk, "price", 0) or 0)
    if price <= 0:
        return None
    existing = db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk.id).first()
    if existing:
        return existing
    completion = _completion_dt_from_walk(walk)
    comp = completion if completion.tzinfo else completion.replace(tzinfo=timezone.utc)
    earning = WalkerEarning(
        id=str(uuid4()),
        walker_id=walk.walker_id or getattr(walk, "assigned_walker_id", None),
        tenant_id=walk.tenant_id,
        walk_id=walk.id,
        gross=price,
        platform_amount=round(float(split.get("platform_amount", 0.0)), 2),
        amount=round(float(split.get("walker_amount", 0.0)), 2),
        status=WE_ACCRUED,
        payable_at=compute_payable_at(comp),
    )
    db.add(earning)
    return earning
```

- [ ] **Step 4: Wire em `_ensure_internal_walk_payment` (admin.py)**

No topo do admin.py, adicione ao import:
```python
from app.services.walker_earning_service import accrue_walker_earning
```

Em `_ensure_internal_walk_payment`, a Fase 1 já computa `_is_network = is_network_walk(...)`. Reordene para que `_is_network` seja calculado ANTES de montar o `Payment`, e use-o para zerar o `walker_amount` quando rede, criando o ledger:

```python
    amount = float(walk.price or 0)
    split = build_payment_split(
        db, walk.tenant_id, amount, walker_id=(walk.walker_id or walk.assigned_walker_id)
    )
    _walker_id = walk.walker_id or walk.assigned_walker_id
    _is_network = is_network_walk(db, walk.tenant_id, _walker_id)
    _provider = "subscription_walk" if getattr(walk, "subscription_id", None) else "internal"
    payment = Payment(
        id=str(uuid4()),
        tenant_id=walk.tenant_id,
        tutor_id=walk.tutor_id,
        walk_id=walk.id,
        amount=amount,
        status="paid",
        provider=_provider,
        commission_percent=split["commission_percent"],
        platform_amount=split["platform_amount"],
        # REDE: o ganho do passeador vai pro ledger WalkerEarning (não pro saldo via Payment).
        walker_amount=(0.0 if _is_network else split["walker_amount"]),
    )
    db.add(payment)
    if _is_network:
        accrue_walker_earning(db, walk, split)
    # Fase 1: comissão medida só para passeio PRÓPRIO (is_network=False) — inalterado abaixo.
```

Mantenha o bloco de `accrue_commission_for_walk` da Fase 1 logo após (ele já recebe `is_network=_is_network` e pula rede). Garanta que `_is_network`/`_walker_id`/`_period` continuam definidos exatamente uma vez (não duplicar). **Leia o código atual e integre sem duplicar variáveis.**

- [ ] **Step 5: Run, verify pass + regressão**

Run: `cd backend && python -m pytest tests/test_walker_earning_accrual.py tests/test_commission_accrual_on_finalize.py tests/test_commission_e2e.py tests/test_recurring_plan_credits.py -v`
Expected: PASS (novos + Fase 1 + créditos intactos).

- [ ] **Step 6: Commit**

```bash
cd backend && git add app/services/walker_earning_service.py app/routes/admin.py tests/test_walker_earning_accrual.py && git commit -m "feat(walker-earning): rede paga via ledger na finalizacao (zera walker_amount do Payment)"
```

---

### Task 3: Somar o ledger no saldo (disponível vs a receber), sem dupla contagem

**Files:**
- Modify: `app/services/walker_earning_service.py` (add `network_earnings_by_tenant`)
- Modify: `app/routes/walker.py` (`_balance_by_tenant`, `_available_balance`)
- Test: `tests/test_walker_earning_balance.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_walker_earning_balance.py
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.user import User
from app.models.walker_earning import WalkerEarning, WE_ACCRUED
from app.routes.walker import _balance_by_tenant, _available_balance

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def _user(db, uid="k1"):
    u = User(id=uid, email=f"{uid}@x.com", name="K", role="walker", password_hash="x")
    db.add(u); db.commit()
    return u

def _earn(db, wid, amount, payable_at, walker_id="k1", tenant_id="t1"):
    db.add(WalkerEarning(id="we-"+wid, walker_id=walker_id, tenant_id=tenant_id, walk_id=wid,
                         gross=amount*2, platform_amount=amount, amount=amount, status=WE_ACCRUED,
                         accrued_at=datetime(2026,6,1,tzinfo=timezone.utc), payable_at=payable_at))
    db.commit()

def test_payable_earning_counts_available_future_counts_areceber():
    db = _db(); u = _user(db)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=3)
    _earn(db, "w1", 24.0, past)
    _earn(db, "w2", 10.0, future)
    by = _balance_by_tenant(u, db)
    assert round(by["t1"]["available"], 2) == 24.0
    assert round(by["t1"]["pending"], 2) == 10.0   # 'a receber' (ainda não payable)
    # legado global: só o payable conta como disponível
    assert round(_available_balance(u, db), 2) == 24.0

def test_no_double_count_when_payment_walker_amount_zero():
    # rede grava Payment.walker_amount=0 + WalkerEarning; saldo deve refletir só o ledger uma vez
    db = _db(); u = _user(db)
    _earn(db, "w1", 24.0, datetime.now(timezone.utc) - timedelta(days=1))
    # nenhum Payment com walker_amount>0 criado => total = só ledger
    assert round(_available_balance(u, db), 2) == 24.0
    assert round(_balance_by_tenant(u, db)["t1"]["available"], 2) == 24.0
```

> **Nota:** ajuste kwargs de `User(...)` aos campos reais do model (`backend/app/models/user.py`); preencha NOT NULLs mínimos.

- [ ] **Step 2: Run, verify fail**

Run: `cd backend && python -m pytest tests/test_walker_earning_balance.py -v`
Expected: FAIL (saldo não inclui o ledger ainda).

- [ ] **Step 3: Add helper de agregação do ledger**

```python
# backend/app/services/walker_earning_service.py  (adicionar)
from app.models.walker_earning import WE_VOID

def network_earnings_by_tenant(db: Session, walker_id: str, now: datetime | None = None) -> dict:
    """Agrega o ledger do passeador por tenant_id.
    Retorna { tenant_id: {"available": x, "areceber": y} }.
    available = earnings com payable_at <= now; areceber = payable_at > now.
    Exclui status void.
    """
    now = now or datetime.now(timezone.utc)
    rows = (
        db.query(WalkerEarning)
        .filter(WalkerEarning.walker_id == walker_id, WalkerEarning.status != WE_VOID)
        .all()
    )
    out: dict = {}
    for r in rows:
        b = out.setdefault(r.tenant_id, {"available": 0.0, "areceber": 0.0})
        pa = r.payable_at
        if pa is not None and pa.tzinfo is None:
            pa = pa.replace(tzinfo=timezone.utc)
        if pa is not None and pa <= now:
            b["available"] += float(r.amount or 0)
        else:
            b["areceber"] += float(r.amount or 0)
    return out
```

- [ ] **Step 4: Integrar em `_balance_by_tenant`**

Em `app/routes/walker.py`, dentro de `_balance_by_tenant`, ANTES do bloco "Arredondamento e cálculo do total", adicione a soma do ledger (a parte `available` no bucket available, a `areceber` no `pending`):

```python
    # Fase 2: ganhos da REDE vêm do ledger WalkerEarning (não de Payment.walker_amount).
    from app.services.walker_earning_service import network_earnings_by_tenant
    for tid, vals in network_earnings_by_tenant(db, user.id).items():
        b = _bucket(tid)
        b["available"] += vals["available"]
        b["pending"] += vals["areceber"]   # 'a receber' = liberação futura (cadência)
```

- [ ] **Step 5: Integrar em `_available_balance`**

Em `_available_balance`, ANTES do `return round(gross, 2)`, adicione só a parte liberada:

```python
    # Fase 2: somar ganhos da REDE já liberados (payable_at <= now) do ledger.
    from app.services.walker_earning_service import network_earnings_by_tenant
    _net = network_earnings_by_tenant(db, user.id)
    gross += sum(v["available"] for v in _net.values())
```

- [ ] **Step 6: Run, verify pass + regressão**

Run: `cd backend && python -m pytest tests/test_walker_earning_balance.py tests/test_walker_earnings_by_tenant.py tests/test_routes_walker_core.py tests/test_multitenant_isolation.py -v`
Expected: PASS (novos + saldo existente intacto).

- [ ] **Step 7: Commit**

```bash
cd backend && git add app/services/walker_earning_service.py app/routes/walker.py tests/test_walker_earning_balance.py && git commit -m "feat(walker-earning): saldo da rede via ledger (disponivel vs a receber)"
```

---

### Task 4: Corrigir dedução de saque (bug double-spend) nas duas funções

**Files:**
- Modify: `app/routes/walker.py` (`_balance_by_tenant`, `_available_balance`)
- Test: `tests/test_withdrawal_deduction_fix.py`

Regra correta: saques em `{pending, approved, paid}` reduzem o saldo; `rejected` NÃO reduz.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_withdrawal_deduction_fix.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.user import User
from app.models.walk import Walk
from app.models.payment import Payment
from app.routes.walker import _balance_by_tenant, _available_balance

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def _user(db, uid="k1"):
    u = User(id=uid, email=f"{uid}@x.com", name="K", role="walker", password_hash="x")
    db.add(u); db.commit(); return u

def _credit(db, wid, amount, walker_id="k1", tenant_id="t1"):
    db.add(Walk(id=wid, tenant_id=tenant_id, tutor_id="tut", walker_id=walker_id,
                price=amount, status="Finalizado", scheduled_date="2026-06-10T10:00"))
    db.add(Payment(id="p-"+wid, tenant_id=tenant_id, tutor_id="tut", walk_id=wid,
                   amount=amount, status="paid", provider="internal", walker_amount=amount))
    db.commit()

def _withdrawal(db, pid, amount, status, walker_id="k1", tenant_id="t1"):
    db.add(Payment(id=pid, tenant_id=tenant_id, tutor_id=walker_id, walk_id=None,
                   amount=-amount, status=status, provider="pix"))
    db.commit()

def test_paid_withdrawal_still_deducts():
    db = _db(); u = _user(db); _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 40.0, "paid")
    assert round(_available_balance(u, db), 2) == 60.0          # antes (bug): 100
    assert round(_balance_by_tenant(u, db)["t1"]["available"], 2) == 60.0

def test_rejected_withdrawal_does_not_deduct():
    db = _db(); u = _user(db); _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 40.0, "rejected")
    assert round(_available_balance(u, db), 2) == 100.0
    assert round(_balance_by_tenant(u, db)["t1"]["available"], 2) == 100.0  # antes (bug): 60

def test_pending_and_approved_deduct():
    db = _db(); u = _user(db); _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 10.0, "pending")
    _withdrawal(db, "wd2", 15.0, "approved")
    assert round(_available_balance(u, db), 2) == 75.0
    assert round(_balance_by_tenant(u, db)["t1"]["available"], 2) == 75.0
```

> **Nota:** confirme o nome real do status "approved" e dos status pendentes nas constantes do walker.py. Use os mesmos identificadores que o fluxo de saque grava (admin approve grava `"paid"`; o saque nasce `"pending"`).

- [ ] **Step 2: Run, verify fail**

Run: `cd backend && python -m pytest tests/test_withdrawal_deduction_fix.py -v`
Expected: FAIL em `test_paid...` (available_balance não desconta paid) e `test_rejected...` (balance_by_tenant desconta rejeitado).

- [ ] **Step 3: Definir o conjunto correto e aplicar**

No topo de `walker.py` (perto das outras constantes de status), defina:
```python
_WITHDRAWAL_DEDUCT_STATUSES = _PENDING_PAYMENT_STATUSES | frozenset({"approved", "paid"})
```

Em `_balance_by_tenant`, no loop de débitos, filtre por status:
```python
    debit_payments = (
        db.query(Payment)
        .filter(
            Payment.tutor_id == user.id,
            Payment.provider == "pix",
            Payment.walk_id.is_(None),
            Payment.amount < 0,
            Payment.status.in_(_WITHDRAWAL_DEDUCT_STATUSES),  # exclui 'rejected'
        )
        .all()
    )
```

Em `_available_balance`, troque `_pending_withdrawal_statuses` por `_WITHDRAWAL_DEDUCT_STATUSES` (passa a descontar 'paid' também):
```python
    pending_withdrawals = (
        db.query(Payment)
        .filter(
            Payment.tutor_id == user.id,
            Payment.provider == "pix",
            Payment.walk_id.is_(None),
            Payment.amount < 0,
            Payment.status.in_(_WITHDRAWAL_DEDUCT_STATUSES),
        )
        .all()
    )
```

- [ ] **Step 4: Run, verify pass + regressão completa de saldo**

Run: `cd backend && python -m pytest tests/test_withdrawal_deduction_fix.py tests/test_walker_earnings_by_tenant.py tests/test_walker_earning_balance.py tests/test_routes_admin_finance.py -v`
Expected: PASS. Se algum teste antigo assumia o comportamento bugado, avalie: se ele codificava o bug, ajuste-o para a regra correta (documente no commit); se não, mantenha.

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/routes/walker.py tests/test_withdrawal_deduction_fix.py && git commit -m "fix(walker-balance): saque deduz {pending,approved,paid} e nao rejected (anti double-spend)"
```

---

### Task 5: Teste e2e + revisão

**Files:**
- Test: `tests/test_walker_earning_e2e.py`

- [ ] **Step 1: Write the e2e test**

```python
# backend/tests/test_walker_earning_e2e.py
"""Rede: finaliza passeio -> ledger 'a receber' -> após payable -> 'disponível' -> saque."""
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.payment import Payment
from app.models.walker_earning import WalkerEarning
from app.models.tenant_walker_access import TenantWalkerAccess
from app.routes.admin import _ensure_internal_walk_payment
from app.routes.walker import _balance_by_tenant

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro"))
    db.add(User(id="k1", email="k1@x.com", name="K", role="walker", password_hash="x"))
    db.add(TenantWalkerAccess(id="twa1", tenant_id="t1", walker_user_id="k1",
                              access_type="shared_network", status="active"))
    db.commit()
    return db

def test_network_walk_flows_areceber_then_available():
    db = _db()
    w = Walk(id="w1", tenant_id="t1", tutor_id="tut", walker_id="k1",
             price=50.0, status="Finalizado", scheduled_date="2026-06-10T10:00")
    db.add(w); db.commit()
    _ensure_internal_walk_payment(w, db); db.commit()

    e = db.query(WalkerEarning).filter_by(walk_id="w1").one()
    # payable_at no futuro (relativo a quando o teste roda? scheduled 2026-06-10 -> payable 2026-06-17)
    # Para tornar determinístico, force payable no passado e no futuro:
    e.payable_at = datetime.now(timezone.utc) + timedelta(days=3); db.commit()
    by = _balance_by_tenant(db.query(User).get("k1"), db)
    assert round(by["t1"]["pending"], 2) == round(e.amount, 2)  # a receber
    assert by["t1"]["available"] == 0.0

    e.payable_at = datetime.now(timezone.utc) - timedelta(days=1); db.commit()
    by2 = _balance_by_tenant(db.query(User).get("k1"), db)
    assert round(by2["t1"]["available"], 2) == round(e.amount, 2)  # disponível

    # Payment do walk de rede tem walker_amount 0 (sem dupla contagem)
    pay = db.query(Payment).filter_by(walk_id="w1").one()
    assert (pay.walker_amount or 0) == 0
```

- [ ] **Step 2: Run + suite ampla**

Run: `cd backend && python -m pytest tests/test_walker_earning_e2e.py -v`
Then: `cd backend && python -m pytest tests/ -k "walker or earning or commission or split or withdrawal or balance" -q`
Expected: novos PASS; falhas pré-existentes não relacionadas (ex.: `api_client` fixture, digest bcrypt) podem permanecer — reporte mas não conserte.

- [ ] **Step 3: Commit**

```bash
cd backend && git add tests/test_walker_earning_e2e.py && git commit -m "test(walker-earning): e2e rede a-receber->disponivel"
```

---

## Operação / pós-implementação

- **Sem job de flip de status:** a disponibilidade é derivada de `payable_at <= now` na leitura — não precisa de scheduler para "liberar".
- **Pagamento ao passeador:** o saque continua manual (admin aprova → marca `paid`). PIX automático = Fase 3 (opcional).
- **Furo D+32:** dissolvido — a liberação do passeador é a cadência semanal, independente do D+32 do cartão do tutor.

## Out of scope (Fase 3+)

- PIX automático (Asaas Transfer) ao aprovar saque.
- Estorno/void de `WalkerEarning` em reembolso/disputa (status `void` já reservado).
- Limpeza conceitual: passeador PRÓPRIO do tenant não deveria ter saldo sacável no Aumigão (hoje tem; mantido inalterado nesta fase).
- Ligar `PRICING_V2_ENABLED` (taxa de rede 18/10) — necessário para o split de rede usar a taxa correta no `platform_amount`/`amount` do ledger.

## Self-Review (feito)

- **Cobertura:** model+cadência+migration (T1), acúmulo rede→ledger+zera walker_amount (T2), saldo via ledger sem dupla contagem (T3), fix double-spend de saque (T4), e2e (T5). ✅
- **Sem placeholder de lógica:** código real em todos os passos; pontos dependentes de nomes reais (campos de `User`/`Walk`, constantes de status) marcados como **Nota** com instrução de verificação.
- **Consistência:** `compute_payable_at(dt)->dt`, `accrue_walker_earning(db, walk, split)->WalkerEarning|None`, `network_earnings_by_tenant(db, walker_id, now=None)->{tenant:{available,areceber}}`, `_WITHDRAWAL_DEDUCT_STATUSES`. Mesmos nomes em tasks e testes. Status do ledger: `accrued/void`. ✅
- **Anti-dupla-contagem:** rede grava `Payment.walker_amount=0` (T2) e o saldo soma o ledger (T3) — testado em `test_no_double_count...`. ✅

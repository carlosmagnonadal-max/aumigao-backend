# PIX Automático (gated) + Estorno de WalkerEarning (Fase 3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Passos usam checkbox (`- [ ]`).

**Goal:** (1) **Estorno** — anular (`void`) o ganho do passeador quando o passeio é estornado/chargeback (automático via webhook) ou por decisão do admin (manual); (2) **PIX automático** — na aprovação do saque, transferir via Asaas Transfer API para a chave PIX do passeador, **atrás de flag DESLIGADA por padrão** (manual continua o default).

**Architecture:** Sobre o ledger `WalkerEarning` da Fase 2. Estorno: `void_walker_earning(walk_id)` marca `status=void` (sai do saldo), exposto por endpoint admin e disparado pelo webhook em eventos de refund/chargeback de um Payment com `walk_id`. PIX auto: `transfer_to_walker(payment)` chama `POST /transfers` do Asaas (gated por `WALKER_AUTO_PIX_ENABLED`, idempotente via `Payment.provider_payment_id`), acionado em `approve_withdrawal`; webhook `TRANSFER_FAILED` reverte o saque para `pending`.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Asaas HTTP, pytest (SQLite in-memory).

**Base/branch:** parte de `feature/walker-earning-fase2`. Branch nova: `feature/pix-auto-estorno-fase3`. Migration `0065` (down_revision `0064_walker_earnings`).

**Princípio de segurança:** PIX auto move dinheiro real → **OFF por padrão** (`WALKER_AUTO_PIX_ENABLED=false`), idempotente, falha-fechada. Estorno nunca paga ninguém — só remove do saldo.

---

## Pré-requisitos de leitura

- `app/models/walker_earning.py` — `WalkerEarning` (Fase 2): tem `status` com `WE_ACCRUED`/`WE_VOID`. `network_earnings_by_tenant` em `app/services/walker_earning_service.py` já EXCLUI `void` do saldo (`status != WE_VOID`).
- `app/routes/admin.py:2455` `approve_withdrawal` (seta `payment.status="paid"`; guard `provider=="pix"`; `ensure_tenant_access`; `record_admin_operational_event`). `:2481` `reject_withdrawal`.
- `app/routes/payments.py:675` `resolve_payment_webhook_status` (PAYMENT_REFUNDED→`pagamento_estornado`); `:1070` `asaas_webhook` (aplica `new_status` ao Payment em `:1217`, achado por `provider_payment_id`). `_PAYMENT_CONFIRMED_EVENTS` (:662). Eventos de chargeback mapeados em `STATUS_BY_WEBHOOK_EVENT` (:104-107: `PAYMENT_REFUNDED`, `PAYMENT_CHARGEBACK_REQUESTED`, `PAYMENT_CHARGEBACK_DISPUTE`, `PAYMENT_AWAITING_CHARGEBACK_REVERSAL`).
- `make_asaas_charge_fn` em `app/services/commission_billing_service.py` (Fase 1) — PADRÃO de chamada HTTP ao Asaas (reusa `_get_asaas_config()` de payments.py + httpx). Use o MESMO padrão para `/transfers`.
- `WalkerProfile.pix_key` (`app/models/walker_profile.py`) — chave PIX do passeador (self-service). Saque é `Payment(tutor_id=<walker user id>, provider="pix", walk_id=None, amount<0)`.
- Convenção de teste: SQLite in-memory. Ver `tests/test_routes_admin_finance.py`, `tests/test_commission_billing_endpoint.py` (webhook).

---

## File Structure

- **Create** `app/services/walker_payout_service.py` — `void_walker_earning` + `transfer_to_walker` + flag.
- **Create** `alembic/versions/0065_walker_earning_void.py` — colunas `void_reason`, `voided_at` em `walker_earnings`.
- **Modify** `app/models/walker_earning.py` — colunas `void_reason`, `voided_at`.
- **Modify** `app/routes/admin.py` — endpoint manual de void + auto-PIX no `approve_withdrawal`.
- **Modify** `app/routes/payments.py` — auto-void no webhook (refund/chargeback) + webhook `TRANSFER_FAILED`.
- **Create** tests.

> Confirme a última migration: `cd backend && ls alembic/versions/ | sort | tail -3` → deve ser `0064_walker_earnings`. Use `down_revision="0064_walker_earnings"`.

---

### Task 1: Estorno — colunas de void + serviço + endpoint admin manual

**Files:**
- Modify: `app/models/walker_earning.py`
- Create: `alembic/versions/0065_walker_earning_void.py`
- Create: `app/services/walker_payout_service.py`
- Modify: `app/routes/admin.py`
- Test: `tests/test_walker_earning_void.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_walker_earning_void.py
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.walker_earning import WalkerEarning, WE_ACCRUED, WE_VOID

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def _earn(db, wid="w1", walker_id="k1"):
    db.add(WalkerEarning(id="we-"+wid, walker_id=walker_id, tenant_id="t1", walk_id=wid,
                         gross=30, platform_amount=5.4, amount=24.6, status=WE_ACCRUED,
                         accrued_at=datetime(2026,6,1,tzinfo=timezone.utc),
                         payable_at=datetime(2026,6,10,tzinfo=timezone.utc)))
    db.commit()

def test_void_marks_status_and_reason():
    from app.services.walker_payout_service import void_walker_earning
    db = _db(); _earn(db)
    out = void_walker_earning(db, "w1", reason="chargeback", source="test")
    db.commit()
    assert out is not None
    e = db.query(WalkerEarning).filter_by(walk_id="w1").one()
    assert e.status == WE_VOID and e.void_reason == "chargeback" and e.voided_at is not None

def test_void_idempotent():
    from app.services.walker_payout_service import void_walker_earning
    db = _db(); _earn(db)
    void_walker_earning(db, "w1", reason="a", source="t"); db.commit()
    first_voided_at = db.query(WalkerEarning).filter_by(walk_id="w1").one().voided_at
    out2 = void_walker_earning(db, "w1", reason="b", source="t"); db.commit()
    e = db.query(WalkerEarning).filter_by(walk_id="w1").one()
    assert e.status == WE_VOID and e.void_reason == "a"  # não sobrescreve
    assert e.voided_at == first_voided_at

def test_void_missing_earning_returns_none():
    from app.services.walker_payout_service import void_walker_earning
    db = _db()
    assert void_walker_earning(db, "nope", reason="x", source="t") is None
```

- [ ] **Step 2: Run, verify fail**

Run: `cd backend && python -m pytest tests/test_walker_earning_void.py -v`
Expected: FAIL (sem colunas / sem serviço).

- [ ] **Step 3: Add columns to the model**

Em `app/models/walker_earning.py`, adicione duas colunas à classe `WalkerEarning`:
```python
    void_reason = Column(String, nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Migration 0065**

```python
# backend/alembic/versions/0065_walker_earning_void.py
"""walker_earnings: colunas de estorno (void_reason, voided_at) — Fase 3

Revision ID: 0065_walker_earning_void
Revises: 0064_walker_earnings
"""
import sqlalchemy as sa
from alembic import op

revision = "0065_walker_earning_void"
down_revision = "0064_walker_earnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("walker_earnings", sa.Column("void_reason", sa.String(), nullable=True))
    op.add_column("walker_earnings", sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("walker_earnings", "voided_at")
    op.drop_column("walker_earnings", "void_reason")
```

- [ ] **Step 5: Create the void service**

```python
# backend/app/services/walker_payout_service.py
"""Estorno (void) e PIX automático do ganho do passeador (Fase 3).

void = remove o ganho do saldo (não paga ninguém). transfer = move dinheiro real
(gated por WALKER_AUTO_PIX_ENABLED). Princípio: falha-fechada, idempotente.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.walker_earning import WalkerEarning, WE_ACCRUED, WE_VOID


def void_walker_earning(db: Session, walk_id: str, *, reason: str, source: str) -> WalkerEarning | None:
    """Anula (idempotente) o ganho do passeador de um passeio. Retorna a entrada ou None.

    Só age sobre entradas ainda não anuladas. Não faz commit (caller comita).
    Observação: se o ganho já tiver sido sacado, o saldo pode ficar negativo
    (clawback legítimo — o passeador deve o valor de um passeio revertido).
    """
    earning = db.query(WalkerEarning).filter(WalkerEarning.walk_id == walk_id).first()
    if earning is None or earning.status == WE_VOID:
        return None
    earning.status = WE_VOID
    earning.void_reason = reason
    earning.voided_at = datetime.now(timezone.utc)
    return earning
```

- [ ] **Step 6: Endpoint admin manual**

Em `app/routes/admin.py`, adicione (perto dos outros endpoints de finance; reuse imports `require_permission`, `ensure_tenant_access`, `get_admin_tenant_scope`, `record_admin_operational_event`, `record_audit_log`, `BaseModel`):

```python
class VoidWalkerEarningRequest(BaseModel):
    walk_id: str
    reason: str

@router.post("/walker-earnings/void")
def void_walker_earning_endpoint(
    payload: VoidWalkerEarningRequest,
    admin: User = Depends(require_permission("finance.manage")),
    db: Session = Depends(get_db),
):
    from app.services.walker_payout_service import void_walker_earning
    from app.models.walker_earning import WalkerEarning
    earning = db.query(WalkerEarning).filter(WalkerEarning.walk_id == payload.walk_id).first()
    if earning is None:
        raise HTTPException(status_code=404, detail="Ganho do passeador nao encontrado para este passeio.")
    ensure_tenant_access(earning.tenant_id, get_admin_tenant_scope(admin, db))
    out = void_walker_earning(db, payload.walk_id, reason=payload.reason, source="admin")
    if out is None:
        return {"ok": True, "already_void": True}
    record_admin_operational_event(
        db, event_type="walker_earning_voided", entity_type="walker_earning",
        entity_id=out.id, severity="warning", title="Ganho do passeador anulado",
        description=f"Ganho anulado (motivo: {payload.reason}).", actor=admin,
        source="admin.walker_earning.void",
        metadata={"walk_id": payload.walk_id, "amount": out.amount},
    )
    db.commit()
    return {"ok": True}
```

- [ ] **Step 7: Run, verify pass**

Run: `cd backend && python -m pytest tests/test_walker_earning_void.py -v`
Expected: PASS (3).

- [ ] **Step 8: Commit**

```bash
cd backend && git add app/models/walker_earning.py alembic/versions/0065_walker_earning_void.py app/services/walker_payout_service.py app/routes/admin.py tests/test_walker_earning_void.py && git commit -m "feat(walker-earning): estorno (void) + endpoint admin manual + migration 0065"
```

---

### Task 2: Estorno automático no webhook (refund/chargeback)

**Files:**
- Modify: `app/routes/payments.py` (`asaas_webhook`)
- Test: `tests/test_walker_earning_autovoid_webhook.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_walker_earning_autovoid_webhook.py
import os
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
import app.models  # noqa: F401
from app.core.database import Base, get_global_db
from app.models.payment import Payment
from app.models.walker_earning import WalkerEarning, WE_ACCRUED, WE_VOID

def _client_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    from app.routes import payments as pr
    app = FastAPI(); app.include_router(pr.router)
    app.dependency_overrides[get_global_db] = lambda: Session()
    return TestClient(app), db

def _seed(db, walk_id="w1", pid="pay-1"):
    db.add(Payment(id="p1", tenant_id="t1", tutor_id="tut", walk_id=walk_id,
                   amount=30, status="pagamento_confirmado_sandbox", provider="asaas_sandbox",
                   provider_payment_id=pid, walker_amount=24.6))
    db.add(WalkerEarning(id="we1", walker_id="k1", tenant_id="t1", walk_id=walk_id,
                         gross=30, platform_amount=5.4, amount=24.6, status=WE_ACCRUED,
                         accrued_at=datetime(2026,6,1,tzinfo=timezone.utc),
                         payable_at=datetime(2026,6,10,tzinfo=timezone.utc)))
    db.commit()

def test_refund_event_voids_earning():
    client, db = _client_db(); _seed(db)
    r = client.post("/payments/webhooks/asaas",
                    json={"event": "PAYMENT_REFUNDED", "payment": {"id": "pay-1"}})
    assert r.status_code in (200, 204)
    db.expire_all()
    assert db.query(WalkerEarning).filter_by(walk_id="w1").one().status == WE_VOID

def test_confirmed_event_does_not_void():
    client, db = _client_db(); _seed(db)
    client.post("/payments/webhooks/asaas",
                json={"event": "PAYMENT_RECEIVED", "payment": {"id": "pay-1"}})
    db.expire_all()
    assert db.query(WalkerEarning).filter_by(walk_id="w1").one().status == WE_ACCRUED
```

> **Nota:** confirme o path do webhook (`/payments/webhooks/asaas`) e o shape do payload (`{"event":..., "payment": {"id":...}}`) com o handler real. Ajuste se preciso.

- [ ] **Step 2: Run, verify fail**

Run: `cd backend && python -m pytest tests/test_walker_earning_autovoid_webhook.py -v`
Expected: FAIL (refund não anula o earning).

- [ ] **Step 3: Add the void-events set + hook no webhook**

Em `payments.py`, perto de `_PAYMENT_REFUND_EVENTS`, adicione:
```python
# Fase 3: eventos que ANULAM (void) o ganho do passeador do walk associado.
_WALKER_EARNING_VOID_EVENTS = {
    "PAYMENT_REFUNDED", "PAYMENT_CHARGEBACK_REQUESTED",
    "PAYMENT_CHARGEBACK_DISPUTE", "PAYMENT_REVERSED",
}
```

No handler `asaas_webhook`, no ponto em que o Payment foi localizado por `provider_payment_id` e o `new_status` foi aplicado (~linha 1208-1217), adicione APÓS aplicar o status:
```python
                # Fase 3: estorno/chargeback de um pagamento com walk_id anula o ganho do passeador.
                if event in _WALKER_EARNING_VOID_EVENTS and payment.walk_id:
                    from app.services.walker_payout_service import void_walker_earning
                    void_walker_earning(db, payment.walk_id, reason=f"asaas:{event}", source="webhook")
```
Garanta que o `db.commit()` existente cubra essa mudança (o handler já comita ao fim do ramo). **Leia o fluxo real e posicione a chamada DENTRO do bloco onde `payment` existe e antes do commit.**

> **Nota sobre REDE:** o auto-void cobre passeios com Payment de gateway vinculado por `walk_id` (avulso). Passeio de REDE pago por crédito tem o refund no Payment da COMPRA do crédito (sem o walk_id do passeio) — esse caso é coberto pelo void MANUAL (Task 1). Documente isso num comentário.

- [ ] **Step 4: Run, verify pass + regressão do webhook**

Run: `cd backend && python -m pytest tests/test_walker_earning_autovoid_webhook.py tests/test_commission_billing_endpoint.py tests/test_routes_payments.py -v`
Expected: PASS (novos + webhook existente intacto).

- [ ] **Step 5: Commit**

```bash
cd backend && git add app/routes/payments.py tests/test_walker_earning_autovoid_webhook.py && git commit -m "feat(walker-earning): auto-void no webhook de refund/chargeback"
```

---

### Task 3: PIX automático (gated/OFF) — serviço de transferência + wire no approve

**Files:**
- Modify: `app/services/walker_payout_service.py`
- Modify: `app/routes/admin.py` (`approve_withdrawal`)
- Test: `tests/test_walker_auto_pix.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_walker_auto_pix.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.payment import Payment
from app.models.walker_profile import WalkerProfile
from app.models.user import User

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def _seed_withdrawal(db, pid="wd1", walker_id="k1", amount=50.0):
    db.add(User(id=walker_id, email="k@x.com", full_name="K", role="walker", password_hash="x"))
    db.add(WalkerProfile(user_id=walker_id, pix_key="k@pix.com"))
    db.add(Payment(id=pid, tenant_id="t1", tutor_id=walker_id, walk_id=None,
                   amount=-amount, status="pending", provider="pix"))
    db.commit()

def test_flag_off_does_not_transfer(monkeypatch):
    from app.services import walker_payout_service as svc
    monkeypatch.setenv("WALKER_AUTO_PIX_ENABLED", "false")
    db = _db(); _seed_withdrawal(db)
    called = {"n": 0}
    monkeypatch.setattr(svc, "_asaas_transfer_post", lambda *a, **k: called.__setitem__("n", called["n"]+1) or "tr1")
    out = svc.transfer_to_walker(db, db.get(Payment, "wd1"))
    assert out is None and called["n"] == 0   # flag off => no-op

def test_flag_on_transfers_once_and_is_idempotent(monkeypatch):
    from app.services import walker_payout_service as svc
    monkeypatch.setenv("WALKER_AUTO_PIX_ENABLED", "true")
    db = _db(); _seed_withdrawal(db)
    calls = {"n": 0}
    monkeypatch.setattr(svc, "_asaas_transfer_post", lambda value, pix_key: calls.__setitem__("n", calls["n"]+1) or "tr-123")
    p = db.get(Payment, "wd1")
    tid = svc.transfer_to_walker(db, p); db.commit()
    assert tid == "tr-123" and p.provider_payment_id == "tr-123" and calls["n"] == 1
    # idempotente: já transferido => não chama de novo
    tid2 = svc.transfer_to_walker(db, db.get(Payment, "wd1"))
    assert tid2 == "tr-123" and calls["n"] == 1

def test_flag_on_missing_pix_key_raises(monkeypatch):
    from app.services import walker_payout_service as svc
    from fastapi import HTTPException
    import pytest
    monkeypatch.setenv("WALKER_AUTO_PIX_ENABLED", "true")
    db = _db()
    db.add(User(id="k2", email="k2@x.com", full_name="K2", role="walker", password_hash="x"))
    db.add(WalkerProfile(user_id="k2", pix_key=None))
    db.add(Payment(id="wd2", tenant_id="t1", tutor_id="k2", walk_id=None, amount=-50, status="pending", provider="pix"))
    db.commit()
    with pytest.raises(HTTPException):
        svc.transfer_to_walker(db, db.get(Payment, "wd2"))
```

> **Nota:** ajuste kwargs de `User`/`WalkerProfile` aos campos reais (use `full_name`; confirme nome do campo de chave PIX em `walker_profile.py` — é `pix_key`).

- [ ] **Step 2: Run, verify fail**

Run: `cd backend && python -m pytest tests/test_walker_auto_pix.py -v`
Expected: FAIL (sem `transfer_to_walker`/`_asaas_transfer_post`).

- [ ] **Step 3: Implement the transfer service (gated, idempotente)**

```python
# backend/app/services/walker_payout_service.py  (adicionar)
import os
from fastapi import HTTPException
from app.models.payment import Payment
from app.models.walker_profile import WalkerProfile


def _auto_pix_enabled() -> bool:
    return os.getenv("WALKER_AUTO_PIX_ENABLED", "false").lower() in {"1", "true", "yes"}


def _asaas_transfer_post(value: float, pix_key: str) -> str:
    """Cria uma transferência PIX no Asaas e retorna o id. Reusa _get_asaas_config().
    Mockado nos testes; chamado de verdade só com a flag ligada em produção."""
    import httpx
    from app.routes.payments import _get_asaas_config
    cfg = _get_asaas_config()
    payload = {"value": round(float(value), 2), "pixAddressKey": pix_key, "operationType": "PIX"}
    with httpx.Client(base_url=cfg["base_url"],
                      headers={"access_token": cfg["api_key"], "Content-Type": "application/json"},
                      timeout=20) as client:
        resp = client.post("/transfers", json=payload)
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail="Falha na transferencia PIX ao passeador.")
        return resp.json()["id"]


def transfer_to_walker(db: Session, payment: Payment) -> str | None:
    """Transfere o valor do saque para a chave PIX do passeador (se a flag estiver ON).
    Idempotente: se o saque já tem provider_payment_id (transferência feita), retorna-o.
    Retorna None se a flag estiver OFF (mantém o fluxo manual). Não comita.
    """
    if not _auto_pix_enabled():
        return None
    if payment.provider_payment_id:  # já transferido
        return payment.provider_payment_id
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == payment.tutor_id).first()
    pix_key = getattr(profile, "pix_key", None) if profile else None
    if not pix_key:
        raise HTTPException(status_code=400, detail="Passeador sem chave PIX cadastrada.")
    value = abs(float(payment.amount or 0))
    transfer_id = _asaas_transfer_post(value, pix_key)
    payment.provider_payment_id = transfer_id
    return transfer_id
```
> **Nota Asaas:** confirme os campos do `POST /transfers` na doc Asaas (PIX): tipicamente `value` + `pixAddressKey` (+ talvez `pixAddressKeyType`/`operationType`). Como a feature é gated/OFF, isso pode ser validado antes de ligar; mas use os campos corretos da doc. Se a config tiver `is_live=False` (sandbox), tudo bem — em sandbox a chamada é inócua.

- [ ] **Step 4: Wire no `approve_withdrawal` (admin.py)**

Em `approve_withdrawal` (admin.py:2455), APÓS `payment.status = "paid"` e ANTES do `db.commit()`, adicione:
```python
        # Fase 3: PIX automático (gated/OFF por padrão). Se ligado, transfere de fato.
        from app.services.walker_payout_service import transfer_to_walker
        transfer_to_walker(db, payment)  # no-op se flag OFF; idempotente; levanta 502 se falhar
```
Se `transfer_to_walker` levantar `HTTPException`, o request falha e o `db.commit()` não roda → o saque NÃO fica `paid` (falha-fechada). Mantenha o `record_admin_operational_event` antes do commit.

- [ ] **Step 5: Run, verify pass + regressão do approve**

Run: `cd backend && python -m pytest tests/test_walker_auto_pix.py tests/test_routes_admin_finance.py -v`
Expected: PASS (novos + approve/reject existentes intactos — com flag OFF por padrão, comportamento atual preservado).

- [ ] **Step 6: Commit**

```bash
cd backend && git add app/services/walker_payout_service.py app/routes/admin.py tests/test_walker_auto_pix.py && git commit -m "feat(walker-payout): PIX automatico gated/OFF na aprovacao do saque"
```

---

### Task 4: Webhook TRANSFER_FAILED + e2e

**Files:**
- Modify: `app/routes/payments.py` (`asaas_webhook`)
- Test: `tests/test_walker_payout_e2e.py`

- [ ] **Step 1: Write the test (transfer failed reverte saque)**

```python
# backend/tests/test_walker_payout_e2e.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
import app.models  # noqa: F401
from app.core.database import Base, get_global_db
from app.models.payment import Payment

def _client_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    from app.routes import payments as pr
    app = FastAPI(); app.include_router(pr.router)
    app.dependency_overrides[get_global_db] = lambda: Session()
    return TestClient(app), db

def test_transfer_failed_reverts_withdrawal_to_pending():
    client, db = _client_db()
    db.add(Payment(id="wd1", tenant_id="t1", tutor_id="k1", walk_id=None, amount=-50,
                   status="paid", provider="pix", provider_payment_id="tr-9"))
    db.commit()
    r = client.post("/payments/webhooks/asaas",
                    json={"event": "TRANSFER_FAILED", "transfer": {"id": "tr-9"}})
    assert r.status_code in (200, 204)
    db.expire_all()
    assert db.get(Payment, "wd1").status == "pending"  # revertido p/ nova tentativa
```

> **Nota:** confirme o shape do evento de transfer do Asaas (`{"event":"TRANSFER_FAILED","transfer":{"id":...}}`) — o campo pode ser `transfer` em vez de `payment`. Ajuste o handler para ler o id de transfer corretamente.

- [ ] **Step 2: Run, verify fail**

Run: `cd backend && python -m pytest tests/test_walker_payout_e2e.py -v`
Expected: FAIL.

- [ ] **Step 3: Add the TRANSFER webhook branch**

No início do `asaas_webhook`, junto do roteamento por tipo de evento, adicione um ramo para eventos de transferência (que usam `transfer` no payload, não `payment`):
```python
    if (payload.get("event") or "").startswith("TRANSFER_"):
        event = payload.get("event")
        transfer = payload.get("transfer") or {}
        tr_id = transfer.get("id")
        if event == "TRANSFER_FAILED" and tr_id:
            wd = db.query(Payment).filter(
                Payment.provider_payment_id == tr_id, Payment.provider == "pix",
            ).first()
            if wd:
                wd.status = "pending"   # reverte p/ o admin tentar de novo
                db.commit()
        return {"ok": True, "received": event}
```
**Posicione este ramo no começo do handler**, antes da lógica que assume `payload["payment"]` — eventos TRANSFER_* não têm `payment`. Leia o início real do `asaas_webhook` e insira sem quebrar o parse existente.

- [ ] **Step 4: Run, verify pass + suite de webhook/payout**

Run: `cd backend && python -m pytest tests/test_walker_payout_e2e.py tests/test_walker_auto_pix.py tests/test_walker_earning_autovoid_webhook.py tests/test_routes_payments.py -v`
Expected: PASS.

- [ ] **Step 5: Run a suíte ampla**

Run: `cd backend && python -m pytest tests/ -k "walker or earning or payout or payment or webhook or withdrawal" -q`
Expected: novos PASS; falhas pré-existentes não relacionadas (`api_client`, digest bcrypt, authz business) podem permanecer — reporte.

- [ ] **Step 6: Commit**

```bash
cd backend && git add app/routes/payments.py tests/test_walker_payout_e2e.py && git commit -m "feat(walker-payout): webhook TRANSFER_FAILED reverte saque + e2e"
```

---

## Operação / pós-implementação

- **PIX automático fica DESLIGADO** (`WALKER_AUTO_PIX_ENABLED` ausente/false). Ligar só quando decidir, setando a env var. Com OFF, o fluxo é exatamente o de hoje (admin paga manual).
- **Estorno automático** cobre passeio com Payment de gateway vinculado (avulso). Rede/crédito → void manual pelo admin.
- **Clawback:** anular um ganho já sacado pode deixar o saldo do passeador negativo (dívida legítima). Política de cobrança desse saldo negativo = futuro.

## Out of scope (futuro)

- UI admin para o void/transfer (este plano expõe só os endpoints/webhooks).
- Reconciliação automática de transferências (TRANSFER_DONE) e relatório.
- Vínculo refund-do-crédito → void do ganho de rede (linkagem indireta).

## Self-Review (feito)

- **Cobertura:** void service+endpoint+migration (T1), auto-void webhook (T2), PIX auto gated+wire (T3), TRANSFER_FAILED+e2e (T4). ✅
- **Segurança de dinheiro:** PIX auto gated/OFF + idempotente (provider_payment_id) + falha-fechada (502 não comita) + sem chave PIX = 400. ✅
- **Consistência:** `void_walker_earning(db, walk_id, *, reason, source)`, `transfer_to_walker(db, payment)->str|None`, `_asaas_transfer_post(value, pix_key)->str`, `_auto_pix_enabled()`, `_WALKER_EARNING_VOID_EVENTS`. Status: `accrued/void`. ✅

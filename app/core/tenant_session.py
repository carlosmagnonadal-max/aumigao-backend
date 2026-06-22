"""Helper de sessão com escopo global (RLS irrestrito).

Uso canônico: operações de plataforma que precisam ver dados de TODOS os tenants
sem estar vinculadas a uma requisição HTTP — webhooks, scheduler, seed, handlers
de exceção global, etc.

    from app.core.tenant_session import global_scope_session

    with global_scope_session() as db:
        payment = db.query(Payment).filter(...).first()
        ...

O context manager garante que a sessão seja sempre fechada, mesmo em caso de
exceção, e define info["rls_tenant"] = "*" antes de qualquer query — espelhando
o padrão já usado em app/main.py e no scheduler.

NOTA PARA FUTURAS INTEGRAÇÕES:
  O webhook do Efí (substituto do Asaas) DEVE usar este mesmo helper, pois
  webhooks de gateway processam pagamentos de QUALQUER tenant e não têm
  tenant_id resolvido na requisição HTTP.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy.orm import Session

from app.core.database import SessionLocal


@contextmanager
def global_scope_session() -> Generator[Session, None, None]:
    """Yield uma SessionLocal com RLS irrestrito (rls_tenant = "*").

    Destinado a callers internos / operações de plataforma que precisam acessar
    dados de todos os tenants: webhooks confiáveis, scheduler, seed, handlers
    de exceção global, etc.

    Garante fechamento da sessão mesmo em caso de exceção — padrão idêntico ao
    used em app/main.py (with SessionLocal() as db: db.info["rls_tenant"] = "*").
    """
    db: Session = SessionLocal()
    # Webhooks confiáveis processam pagamentos de QUALQUER tenant → escopo global
    # após validar a assinatura. O futuro webhook do Efí DEVE usar este mesmo helper.
    db.info["rls_tenant"] = "*"
    try:
        yield db
    finally:
        db.close()

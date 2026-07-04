"""Regressão BUG 3 — Slug de unidade nova "não encontrado".

Cenário: o admin cria uma unidade (TenantUnit) com slug "unidade-norte".
O app envia X-Tenant-Slug: unidade-norte. O TenantResolverMiddleware chama
resolve_tenant_from_headers, que antes só procurava Tenant.slug.
Como "unidade-norte" é slug de TenantUnit (não de Tenant), a busca falhava →
o resolver caía no default (aumigao) ou retornava None → "tenant não encontrado".

Fix: resolve_tenant_from_headers agora também procura TenantUnit.slug como
fallback e retorna o tenant PAI da unidade ativa.
"""
from __future__ import annotations

import app.models  # noqa: F401 — registra todos os modelos em Base.metadata

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tenant import Tenant, TenantUnit
from app.services.tenant_resolver_service import resolve_tenant_from_headers


# ── helpers ──────────────────────────────────────────────────────────────────

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make_request(*, x_tenant_slug: str | None = None, x_tenant_id: str | None = None) -> MagicMock:
    headers: dict[str, str] = {}
    if x_tenant_slug is not None:
        headers["X-Tenant-Slug"] = x_tenant_slug
    if x_tenant_id is not None:
        headers["X-Tenant-Id"] = x_tenant_id

    req = MagicMock()
    req.headers = headers
    return req


# ── testes de regressão BUG 3 ─────────────────────────────────────────────────

def test_resolve_by_tenant_slug_still_works():
    """Caminho primário não-regressão: slug do tenant raiz continua resolvido."""
    db = _db()
    db.add(Tenant(id="t1", name="T1", slug="aumigao", status="active", plan="pro"))
    db.commit()

    req = _make_request(x_tenant_slug="aumigao")
    resolved = resolve_tenant_from_headers(req, db)
    assert resolved is not None
    assert resolved.id == "t1"


def test_resolve_by_tenant_id_still_works():
    """Caminho primário não-regressão: X-Tenant-Id continua resolvido."""
    db = _db()
    db.add(Tenant(id="t1", name="T1", slug="aumigao", status="active", plan="pro"))
    db.commit()

    req = _make_request(x_tenant_id="t1")
    resolved = resolve_tenant_from_headers(req, db)
    assert resolved is not None
    assert resolved.id == "t1"


def test_resolve_unit_slug_returns_parent_tenant():
    """BUG 3 — regressão: slug da unidade recém-criada deve resolver o tenant PAI.

    Antes do fix, resolve_tenant_from_headers procurava apenas Tenant.slug.
    Como "unidade-norte" é slug de TenantUnit, não de Tenant, a busca falhava
    e retornava None → 400 TENANT_REQUIRED ou fallback errado.
    """
    db = _db()
    db.add(Tenant(id="t1", name="Aumigao", slug="aumigao", status="active", plan="enterprise"))
    db.add(TenantUnit(id="u1", tenant_id="t1", name="Unidade Norte", slug="unidade-norte", status="active"))
    db.commit()

    req = _make_request(x_tenant_slug="unidade-norte")
    resolved = resolve_tenant_from_headers(req, db)

    assert resolved is not None, (
        "BUG 3 detectado: slug da unidade 'unidade-norte' retornou None — "
        "resolver não buscou em TenantUnit.slug como fallback"
    )
    assert resolved.id == "t1", (
        f"Esperava tenant pai 't1', mas resolveu para '{resolved.id if resolved else None}'"
    )


def test_inactive_unit_slug_does_not_resolve():
    """Unidade INATIVA não deve ser usada como fallback — status != 'active'."""
    db = _db()
    db.add(Tenant(id="t1", name="T1", slug="aumigao", status="active", plan="enterprise"))
    db.add(TenantUnit(id="u1", tenant_id="t1", name="Filial Fechada", slug="filial-fechada", status="inactive"))
    db.commit()

    req = _make_request(x_tenant_slug="filial-fechada")
    resolved = resolve_tenant_from_headers(req, db)
    # Unidade inativa não deve servir como fallback de resolução
    assert resolved is None


def test_unknown_slug_returns_none():
    """Slug completamente desconhecido (nem tenant nem unidade) → None."""
    db = _db()
    db.add(Tenant(id="t1", name="T1", slug="aumigao", status="active", plan="pro"))
    db.commit()

    req = _make_request(x_tenant_slug="slug-que-nao-existe")
    resolved = resolve_tenant_from_headers(req, db)
    assert resolved is None


def test_unit_slug_without_active_tenant_returns_none():
    """Unidade ativa mas tenant pai inexistente (dados inconsistentes) → None seguro."""
    db = _db()
    # Não insere o tenant pai (simula inconsistência de dados)
    db.add(TenantUnit(id="u1", tenant_id="t-fantasma", name="Fantasma", slug="fantasma", status="active"))
    db.commit()

    req = _make_request(x_tenant_slug="fantasma")
    resolved = resolve_tenant_from_headers(req, db)
    assert resolved is None


def test_unit_slug_tenant_id_priority():
    """X-Tenant-Id tem prioridade sobre X-Tenant-Slug (mesmo que o slug seja de unidade)."""
    db = _db()
    db.add(Tenant(id="t1", name="T1", slug="aumigao", status="active", plan="enterprise"))
    db.add(Tenant(id="t2", name="T2", slug="outro", status="active", plan="enterprise"))
    db.add(TenantUnit(id="u1", tenant_id="t2", name="Norte", slug="norte", status="active"))
    db.commit()

    # X-Tenant-Id aponta para t1; X-Tenant-Slug aponta para unidade de t2
    req = _make_request(x_tenant_id="t1", x_tenant_slug="norte")
    resolved = resolve_tenant_from_headers(req, db)
    # X-Tenant-Id tem prioridade (t1)
    assert resolved is not None
    assert resolved.id == "t1"

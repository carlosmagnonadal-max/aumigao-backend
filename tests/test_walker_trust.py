"""Testes do servico de Confianca do passeador (walker_trust_service).

Cobre cada selo, cada certificacao automatica e cada nivel com cenarios variados
(sem dados, dados parciais, dados completos), usando SQLite em memoria + apenas as
tabelas necessarias. NAO importa app.main e NAO toca banco real.

Spec: docs/CONFIANCA-PASSEADOR.md
"""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.complaint import Complaint
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.services import walker_trust_service

WALKER_ID = "walker-1"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# --------------------------------------------------------------------------- helpers
def make_walker(
    db,
    *,
    status: str = "active",
    full_name: str = "Passeador Teste",
    cpf: str = "12345678900",
    phone: str = "+5511999999999",
    has_vehicle: bool = False,
    docs: bool = True,
    proof_of_address: bool | None = None,
    created_months_ago: float = 6,
    walker_id: str = WALKER_ID,
    email: str = "walker@test.com",
):
    db.add(User(id=walker_id, email=email, password_hash="x", role="walker", full_name=full_name))
    created_at = datetime.utcnow() - timedelta(days=int(created_months_ago * 30))
    if proof_of_address is None:
        proof_of_address = docs
    db.add(
        WalkerProfile(
            id=str(uuid4()),
            user_id=walker_id,
            full_name=full_name,
            cpf=cpf,
            phone=phone,
            status=status,
            has_vehicle=has_vehicle,
            document_url="u" if docs else None,
            identity_document_back_url="u" if docs else None,
            selfie_url="u" if docs else None,
            proof_of_address_url="u" if proof_of_address else None,
            created_at=created_at,
        )
    )
    db.commit()


def add_walks(db, count: int, *, status: str = "Finalizado", walker_id: str = WALKER_ID):
    for _ in range(count):
        db.add(
            Walk(
                id=str(uuid4()),
                tutor_id="tutor-x",
                walker_id=walker_id,
                pet_id="pet-x",
                scheduled_date="2026-01-01",
                duration_minutes=30,
                price=50.0,
                status=status,
            )
        )
    db.commit()


def add_reviews(db, count: int, rating: int, *, walker_id: str = WALKER_ID):
    for _ in range(count):
        db.add(
            WalkerReview(
                id=str(uuid4()),
                walk_id=str(uuid4()),
                tutor_id="tutor-x",
                walker_id=walker_id,
                rating=rating,
            )
        )
    db.commit()


def add_critical_complaint(db, *, days_ago: int = 1, walker_id: str = WALKER_ID):
    db.add(
        Complaint(
            id=str(uuid4()),
            source="tutor",
            author_id="tutor-x",
            author_role="tutor",
            target_type="walker",
            target_user_id=walker_id,
            category="falta_cuidado",
            severity="critica",
            status="em_analise",
            created_at=datetime.utcnow() - timedelta(days=days_ago),
        )
    )
    db.commit()


# ----------------------------------------------------------------- selo: cadastro
def test_cadastro_verificado_sem_perfil(db):
    db.add(User(id=WALKER_ID, email="w@test.com", password_hash="x", role="walker"))
    db.commit()
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["cadastro_verificado"] is False
    assert trust["level"] == "Bronze"


def test_cadastro_verificado_pendente_nao_concede(db):
    make_walker(db, status="pending")
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["cadastro_verificado"] is False


def test_cadastro_verificado_aprovado_concede(db):
    make_walker(db, status="approved")
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["cadastro_verificado"] is True


def test_cadastro_verificado_incompleto_sem_cpf(db):
    make_walker(db, status="active", cpf="")
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["cadastro_verificado"] is False


# --------------------------------------------------------------- selo: identidade
def test_identidade_verificada_so_com_active_e_docs(db):
    make_walker(db, status="active", docs=True)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["identidade_verificada"] is True


def test_identidade_verificada_falsa_se_aprovado_sem_active(db):
    make_walker(db, status="approved", docs=True)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["identidade_verificada"] is False


def test_identidade_verificada_falsa_sem_documentos(db):
    make_walker(db, status="active", docs=False)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["identidade_verificada"] is False


# --------------------------------------------------------- selo: passeador verificado
def test_passeador_verificado_completo(db):
    make_walker(db, status="active", docs=True)
    add_walks(db, 25)
    add_reviews(db, 6, 5)  # nota 5.0, 6 avaliacoes
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["passeador_verificado"] is True


def test_passeador_verificado_falha_poucos_passeios(db):
    make_walker(db, status="active", docs=True)
    add_walks(db, 10)
    add_reviews(db, 6, 5)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["passeador_verificado"] is False


def test_passeador_verificado_falha_nota_baixa(db):
    make_walker(db, status="active", docs=True)
    add_walks(db, 25)
    add_reviews(db, 6, 4)  # nota 4.0 < 4.7
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["passeador_verificado"] is False


def test_passeador_verificado_falha_incidente_critico(db):
    make_walker(db, status="active", docs=True)
    add_walks(db, 25)
    add_reviews(db, 6, 5)
    add_critical_complaint(db, days_ago=5)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["passeador_verificado"] is False


def test_passeador_verificado_ok_incidente_critico_antigo(db):
    make_walker(db, status="active", docs=True)
    add_walks(db, 25)
    add_reviews(db, 6, 5)
    add_critical_complaint(db, days_ago=120)  # fora da janela de 90 dias
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["passeador_verificado"] is True


# --------------------------------------------------------------- certificacoes
def test_certificacao_documentacao_completa(db):
    make_walker(db, status="active", docs=True)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["documentacao_completa"] is True
    assert cert["endereco_confirmado"] is True


def test_certificacao_documentacao_incompleta(db):
    make_walker(db, status="active", docs=False)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["documentacao_completa"] is False


def test_certificacao_endereco_confirmado_isolado(db):
    make_walker(db, status="active", docs=False, proof_of_address=True)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["endereco_confirmado"] is True
    assert cert["documentacao_completa"] is False


def test_certificacao_possui_transporte(db):
    make_walker(db, status="active", has_vehicle=True)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["possui_transporte"] is True


def test_certificacao_sem_transporte(db):
    make_walker(db, status="active", has_vehicle=False)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["possui_transporte"] is False


def test_certificacao_experiencia_comprovada(db):
    make_walker(db, status="active", created_months_ago=6)
    add_walks(db, 55)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["experiencia_comprovada"] is True


def test_certificacao_experiencia_falha_tempo_curto(db):
    make_walker(db, status="active", created_months_ago=1)  # < 3 meses
    add_walks(db, 55)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["experiencia_comprovada"] is False


def test_certificacao_experiencia_falha_poucos_passeios(db):
    make_walker(db, status="active", created_months_ago=6)
    add_walks(db, 40)  # < 50
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["experiencia_comprovada"] is False


def test_certificacao_atendimento_premium(db):
    make_walker(db, status="active")
    add_walks(db, 110)
    add_reviews(db, 10, 5)  # nota 5.0 >= 4.9
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["atendimento_premium"] is True


def test_certificacao_atendimento_premium_falha_volume(db):
    make_walker(db, status="active")
    add_walks(db, 50)  # < 100
    add_reviews(db, 10, 5)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    cert = {c["key"]: c["granted"] for c in trust["certifications"]}
    assert cert["atendimento_premium"] is False


# ----------------------------------------------------------------------- niveis
def test_nivel_bronze_inativo(db):
    make_walker(db, status="pending")
    add_walks(db, 200)
    add_reviews(db, 50, 5)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["level"] == "Bronze"


def test_nivel_bronze_active_poucos_passeios(db):
    make_walker(db, status="active")
    add_walks(db, 5)
    add_reviews(db, 2, 5)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["level"] == "Bronze"


def test_nivel_prata(db):
    make_walker(db, status="active")
    add_walks(db, 12)
    add_reviews(db, 5, 5)  # nota 5.0 >= 4.5
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["level"] == "Prata"


def test_nivel_ouro(db):
    make_walker(db, status="active")
    add_walks(db, 60)
    add_reviews(db, 8, 5)  # nota 5.0 >= 4.7, risco normal, sem cancelamentos
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["level"] == "Ouro"


def test_nivel_diamante(db):
    make_walker(db, status="active", docs=True)
    add_walks(db, 160)
    add_reviews(db, 20, 5)  # nota 5.0 >= 4.9
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["passeador_verificado"] is True
    assert trust["level"] == "Diamante"


def test_nivel_diamante_rebaixa_sem_verificado(db):
    # 160 passeios, mas SEM documentos -> nao e passeador_verificado -> nao chega a Diamante,
    # mas atende Ouro (>=50, nota >=4.7, risco normal).
    make_walker(db, status="active", docs=False)
    add_walks(db, 160)
    add_reviews(db, 20, 5)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    assert trust["seals"]["passeador_verificado"] is False
    assert trust["level"] == "Ouro"


def test_metrics_expostas(db):
    make_walker(db, status="active")
    add_walks(db, 12)
    add_reviews(db, 5, 5)
    trust = walker_trust_service.compute_walker_trust(db, WALKER_ID)
    m = trust["metrics"]
    assert m["total_walks"] == 12
    assert m["rating_average"] == 5.0
    assert m["is_active"] is True
    assert m["critical_incidents_90d"] == 0

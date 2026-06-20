"""
TDD — testes de integração: CPF/RG cifrado no ORM (SQLite em memória).

Cobertura:
- TutorProfile: cpf é cifrado no banco, decifrado pelo ORM, cpf_bidx preenchido
- WalkerProfile: cpf + rg cifrados, cpf_bidx preenchido, rg sem bidx
- Valor RAW na coluna (via text()) começa com "gAAAA" (Fernet)
- cpf_bidx atualizado no update
- Usuário sem CPF (cpf="") não rompe o fluxo
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.pii_crypto import blind_index, _fernet
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walker_profile import WalkerProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_key(monkeypatch):
    monkeypatch.setenv("PII_ENCRYPTION_KEY", "sI9VJYXwVrM29Mykh649L9MzxjbneiYu3dI9X6k29ws=")
    _fernet.cache_clear()
    yield
    _fernet.cache_clear()


@pytest.fixture()
def session():
    """Sessão SQLite em memória com todas as tabelas necessárias."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    # Importar todos os modelos para garantir que as tabelas são criadas
    from app.models import tutor_profile, walker_profile  # noqa: F401
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            TutorProfile.__table__,
            WalkerProfile.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session()
    yield db
    db.close()
    engine.dispose()


def _make_user(db, user_id: str, email: str) -> User:
    u = User(id=user_id, email=email, password_hash="x")
    db.add(u)
    db.flush()
    return u


# ---------------------------------------------------------------------------
# TutorProfile — CPF cifrado
# ---------------------------------------------------------------------------


def test_tutor_cpf_roundtrip(session):
    """ORM retorna CPF em texto puro; banco armazena cifrado."""
    _make_user(session, "u1", "tutor@test.com")
    t = TutorProfile(id="tp1", user_id="u1", cpf="12345678900")
    session.add(t)
    session.commit()

    # Leitura via ORM → texto puro
    fetched = session.query(TutorProfile).filter_by(id="tp1").one()
    assert fetched.cpf == "12345678900"


def test_tutor_cpf_raw_is_encrypted(session):
    """Valor RAW na coluna `cpf` do banco começa com 'gAAAA' (Fernet)."""
    _make_user(session, "u1", "tutor@test.com")
    session.add(TutorProfile(id="tp1", user_id="u1", cpf="12345678900"))
    session.commit()

    row = session.execute(
        text("SELECT cpf FROM tutor_profiles WHERE id = 'tp1'")
    ).fetchone()
    raw_cpf = row[0]
    assert raw_cpf.startswith("gAAAA"), f"Esperado token Fernet, obteve: {raw_cpf!r}"


def test_tutor_cpf_bidx_set_on_insert(session):
    """cpf_bidx é preenchido automaticamente no insert."""
    _make_user(session, "u1", "tutor@test.com")
    session.add(TutorProfile(id="tp1", user_id="u1", cpf="12345678900"))
    session.commit()

    fetched = session.query(TutorProfile).filter_by(id="tp1").one()
    expected_bidx = blind_index("12345678900")
    assert fetched.cpf_bidx == expected_bidx


def test_tutor_cpf_bidx_updated_on_update(session):
    """cpf_bidx é atualizado quando o CPF muda."""
    _make_user(session, "u1", "tutor@test.com")
    t = TutorProfile(id="tp1", user_id="u1", cpf="11111111111")
    session.add(t)
    session.commit()

    # Atualizar CPF
    t.cpf = "99999999999"
    session.commit()

    fetched = session.query(TutorProfile).filter_by(id="tp1").one()
    assert fetched.cpf == "99999999999"
    assert fetched.cpf_bidx == blind_index("99999999999")


def test_tutor_empty_cpf_no_crash(session):
    """cpf='' não deve levantar exceção; cpf_bidx fica NULL."""
    _make_user(session, "u1", "tutor@test.com")
    t = TutorProfile(id="tp1", user_id="u1", cpf="")
    session.add(t)
    session.commit()

    fetched = session.query(TutorProfile).filter_by(id="tp1").one()
    assert fetched.cpf == ""
    assert fetched.cpf_bidx is None


def test_tutor_cpf_formatted_roundtrip(session):
    """CPF com pontuação é armazenado cifrado e retornado com pontuação original."""
    _make_user(session, "u1", "tutor@test.com")
    t = TutorProfile(id="tp1", user_id="u1", cpf="123.456.789-00")
    session.add(t)
    session.commit()

    fetched = session.query(TutorProfile).filter_by(id="tp1").one()
    assert fetched.cpf == "123.456.789-00"
    # blind_index deve normalizar — mesma entrada com/sem pontuação
    assert fetched.cpf_bidx == blind_index("123.456.789-00")
    assert fetched.cpf_bidx == blind_index("12345678900")


# ---------------------------------------------------------------------------
# WalkerProfile — CPF + RG cifrados
# ---------------------------------------------------------------------------


def test_walker_cpf_roundtrip(session):
    _make_user(session, "u2", "walker@test.com")
    w = WalkerProfile(id="wp1", user_id="u2", cpf="98765432100", rg="MG1234567")
    session.add(w)
    session.commit()

    fetched = session.query(WalkerProfile).filter_by(id="wp1").one()
    assert fetched.cpf == "98765432100"


def test_walker_rg_roundtrip(session):
    _make_user(session, "u2", "walker@test.com")
    w = WalkerProfile(id="wp1", user_id="u2", cpf="98765432100", rg="MG1234567")
    session.add(w)
    session.commit()

    fetched = session.query(WalkerProfile).filter_by(id="wp1").one()
    assert fetched.rg == "MG1234567"


def test_walker_cpf_raw_is_encrypted(session):
    _make_user(session, "u2", "walker@test.com")
    session.add(WalkerProfile(id="wp1", user_id="u2", cpf="98765432100", rg="MG1234567"))
    session.commit()

    row = session.execute(
        text("SELECT cpf FROM walker_profiles WHERE id = 'wp1'")
    ).fetchone()
    assert row[0].startswith("gAAAA"), f"CPF não cifrado: {row[0]!r}"


def test_walker_rg_raw_is_encrypted(session):
    _make_user(session, "u2", "walker@test.com")
    session.add(WalkerProfile(id="wp1", user_id="u2", cpf="98765432100", rg="MG1234567"))
    session.commit()

    row = session.execute(
        text("SELECT rg FROM walker_profiles WHERE id = 'wp1'")
    ).fetchone()
    assert row[0].startswith("gAAAA"), f"RG não cifrado: {row[0]!r}"


def test_walker_cpf_bidx_set_on_insert(session):
    _make_user(session, "u2", "walker@test.com")
    session.add(WalkerProfile(id="wp1", user_id="u2", cpf="98765432100", rg="MG1234567"))
    session.commit()

    fetched = session.query(WalkerProfile).filter_by(id="wp1").one()
    assert fetched.cpf_bidx == blind_index("98765432100")


def test_walker_rg_has_no_bidx_column(session):
    """RG não tem coluna cpf_bidx (não é usado em uniqueness queries)."""
    # Confirmar que WalkerProfile não tem atributo rg_bidx
    assert not hasattr(WalkerProfile, "rg_bidx")


def test_walker_empty_cpf_no_crash(session):
    _make_user(session, "u2", "walker@test.com")
    w = WalkerProfile(id="wp1", user_id="u2", cpf="", rg="")
    session.add(w)
    session.commit()

    fetched = session.query(WalkerProfile).filter_by(id="wp1").one()
    assert fetched.cpf == ""
    assert fetched.rg == ""
    assert fetched.cpf_bidx is None


def test_walker_cpf_bidx_updated_on_update(session):
    _make_user(session, "u2", "walker@test.com")
    w = WalkerProfile(id="wp1", user_id="u2", cpf="11111111111", rg="RG111")
    session.add(w)
    session.commit()

    w.cpf = "22222222222"
    session.commit()

    fetched = session.query(WalkerProfile).filter_by(id="wp1").one()
    assert fetched.cpf == "22222222222"
    assert fetched.cpf_bidx == blind_index("22222222222")

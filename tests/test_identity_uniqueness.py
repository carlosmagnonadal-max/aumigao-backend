import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.services.identity_uniqueness import ensure_unique_identity


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            TutorProfile.__table__,
            WalkerProfile.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _user(db, *, id="u1", email="a@a.com") -> User:
    u = User(id=id, email=email, password_hash="x")
    db.add(u)
    db.commit()
    return u


def _tutor(db, *, id="tp1", user_id="u1", cpf="", phone="") -> TutorProfile:
    t = TutorProfile(id=id, user_id=user_id, cpf=cpf, phone=phone)
    db.add(t)
    db.commit()
    return t


def _walker(db, *, id="wp1", user_id="u2", cpf="", phone="") -> WalkerProfile:
    w = WalkerProfile(id=id, user_id=user_id, cpf=cpf, phone=phone)
    db.add(w)
    db.commit()
    return w


# ---------- Caminho feliz: nada cadastrado, sem conflito ----------

def test_no_args_does_nothing():
    db = _db()
    # Nenhum parametro -> nao deve levantar nada
    ensure_unique_identity(db)


def test_unique_values_pass():
    db = _db()
    _user(db, id="u1", email="taken@a.com")
    _tutor(db, id="tp1", user_id="u1", cpf="111", phone="999")
    # Valores diferentes dos cadastrados -> ok
    ensure_unique_identity(db, email="new@a.com", cpf="222", phone="888")


# ---------- Email: unicidade contra User ----------

def test_email_conflict_raises_409():
    db = _db()
    _user(db, id="u1", email="taken@a.com")
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, email="taken@a.com")
    assert exc.value.status_code == 409
    assert "e-mail" in exc.value.detail.lower()


def test_email_excludes_current_user():
    db = _db()
    _user(db, id="u1", email="taken@a.com")
    # O proprio usuario mantendo o mesmo email -> nao conflita
    ensure_unique_identity(db, email="taken@a.com", current_user_id="u1")


def test_email_conflict_with_different_current_user():
    db = _db()
    _user(db, id="u1", email="taken@a.com")
    # Outro usuario tentando usar o email de u1
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, email="taken@a.com", current_user_id="u2")
    assert exc.value.status_code == 409


# ---------- CPF: unicidade contra Tutor e Walker ----------

def test_cpf_conflict_in_tutor():
    db = _db()
    _tutor(db, id="tp1", user_id="u1", cpf="123456")
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, cpf="123456")
    assert exc.value.status_code == 409
    assert "cpf" in exc.value.detail.lower()


def test_cpf_conflict_in_walker():
    db = _db()
    _walker(db, id="wp1", user_id="u2", cpf="123456")
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, cpf="123456")
    assert exc.value.status_code == 409


def test_cpf_unique_passes():
    db = _db()
    _tutor(db, id="tp1", user_id="u1", cpf="111")
    _walker(db, id="wp1", user_id="u2", cpf="222")
    ensure_unique_identity(db, cpf="333")


def test_cpf_excludes_current_user_tutor():
    db = _db()
    _tutor(db, id="tp1", user_id="u1", cpf="123456")
    # O proprio tutor (mesmo user_id) -> nao conflita
    ensure_unique_identity(db, cpf="123456", current_user_id="u1")


def test_cpf_excludes_current_user_walker():
    db = _db()
    _walker(db, id="wp1", user_id="u2", cpf="123456")
    ensure_unique_identity(db, cpf="123456", current_user_id="u2")


def test_cpf_conflict_when_current_user_is_someone_else():
    db = _db()
    _tutor(db, id="tp1", user_id="u1", cpf="123456")
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, cpf="123456", current_user_id="u9")
    assert exc.value.status_code == 409


# ---------- Phone: unicidade contra Tutor e Walker ----------

def test_phone_conflict_in_tutor():
    db = _db()
    _tutor(db, id="tp1", user_id="u1", phone="99999")
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, phone="99999")
    assert exc.value.status_code == 409
    assert "telefone" in exc.value.detail.lower()


def test_phone_conflict_in_walker():
    db = _db()
    _walker(db, id="wp1", user_id="u2", phone="99999")
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, phone="99999")
    assert exc.value.status_code == 409


def test_phone_excludes_current_user():
    db = _db()
    _tutor(db, id="tp1", user_id="u1", phone="99999")
    ensure_unique_identity(db, phone="99999", current_user_id="u1")


def test_phone_unique_passes():
    db = _db()
    _walker(db, id="wp1", user_id="u2", phone="11111")
    ensure_unique_identity(db, phone="22222")


# ---------- Bordas ----------

def test_empty_string_email_skips_check():
    db = _db()
    _user(db, id="u1", email="")
    # email="" e falsy -> bloco nao executa, mesmo havendo user com email vazio
    ensure_unique_identity(db, email="")


def test_email_checked_first_takes_priority():
    db = _db()
    _user(db, id="u1", email="taken@a.com")
    _tutor(db, id="tp1", user_id="u1", cpf="123")
    # Conflito de email tem prioridade (checado primeiro)
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, email="taken@a.com", cpf="123")
    assert "e-mail" in exc.value.detail.lower()


# ---------- CPF normalizado: mascarado vs sem máscara = duplicata ----------


def test_cpf_masked_vs_plain_is_duplicate_in_tutor():
    """CPF '123.456.789-00' e '12345678900' são o mesmo — deve barrar como duplicata."""
    db = _db()
    # Cadastra CPF sem máscara
    _tutor(db, id="tp1", user_id="u1", cpf="12345678900")
    # Tenta cadastrar CPF com máscara — deve colidir
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, cpf="123.456.789-00")
    assert exc.value.status_code == 409
    assert "cpf" in exc.value.detail.lower()


def test_cpf_masked_vs_plain_is_duplicate_in_walker():
    """CPF mascarado bate com CPF sem máscara no walker."""
    db = _db()
    _walker(db, id="wp1", user_id="u2", cpf="123.456.789-00")
    with pytest.raises(HTTPException) as exc:
        ensure_unique_identity(db, cpf="12345678900")
    assert exc.value.status_code == 409

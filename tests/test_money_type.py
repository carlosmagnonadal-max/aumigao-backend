"""Governança de dinheiro — tipo Money (Numeric(12,2)) com arredondamento a centavos.

Substitui Float nos campos monetários: armazena decimal exato (Postgres) e arredonda
para 2 casas na escrita, em QUALQUER banco (o round roda em Python no TypeDecorator),
mantendo float na leitura para não quebrar a aritmética existente.
"""
from sqlalchemy import Column, Integer, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.models.types import Money

Base = declarative_base()


class _MoneyRow(Base):
    __tablename__ = "_money_test"
    id = Column(Integer, primary_key=True)
    val = Column(Money)


def _db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_money_rounds_to_two_decimals_on_write():
    db = _db()
    db.add(_MoneyRow(id=1, val=33.336))
    db.commit()
    db.expire_all()
    assert db.get(_MoneyRow, 1).val == 33.34


def test_money_preserves_clean_value():
    db = _db()
    db.add(_MoneyRow(id=1, val=49.90))
    db.commit()
    db.expire_all()
    assert db.get(_MoneyRow, 1).val == 49.9


def test_money_returns_float_not_decimal():
    db = _db()
    db.add(_MoneyRow(id=1, val=10))
    db.commit()
    db.expire_all()
    assert isinstance(db.get(_MoneyRow, 1).val, float)


def test_money_handles_none():
    db = _db()
    db.add(_MoneyRow(id=1, val=None))
    db.commit()
    db.expire_all()
    assert db.get(_MoneyRow, 1).val is None


def test_money_accepts_string_numeric():
    db = _db()
    db.add(_MoneyRow(id=1, val="20.5"))
    db.commit()
    db.expire_all()
    assert db.get(_MoneyRow, 1).val == 20.5

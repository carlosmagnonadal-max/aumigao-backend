"""S2 — matching exclui passeador sem Capacitação SÓ com trava efetiva (on + vet_approved + carência)."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.schemas.matching import MatchingWalkerRequest
from app.services import matching_service
from app.services.operational_matching_service import _candidate_for_selected_walker
from app.services.walker_network_matching_service import get_matching_pool_for_tenant
from tests.training_helpers import configure_training, make_bundle, write_bundle

TRAINED = "walker-trained"
UNTRAINED = "walker-untrained"


def _subset_db():
    """Só as tabelas que get_eligible_walkers usa hoje (garante que o filtro não exige tabela nova)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[WalkerProfile.__table__, Walk.__table__,
                                             WalkerReview.__table__, Pet.__table__])
    return sessionmaker(bind=engine)()


def _full_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _profiles(db, *, with_users=False, tenant_id=None):
    for n, (user_id, version) in enumerate(((TRAINED, "9.0"), (UNTRAINED, None)), start=1):
        if with_users:
            db.add(User(id=user_id, email=f"{user_id}@t.com", password_hash="x", role="walker", is_active=True))
        db.add(WalkerProfile(
            id=f"wp-{n}", user_id=user_id, full_name=f"P{n}", city="salvador", state="pituba",
            status="active", active_as_walker=True, training_completed_version=version,
            created_at=datetime(2024, 1, 1, 12, 0, 0),
        ))
        if tenant_id:
            db.add(TenantWalkerAccess(id=f"twa-{n}", tenant_id=tenant_id, walker_user_id=user_id,
                                      access_type="shared_network", status="active"))
    db.commit()


def _request():
    return MatchingWalkerRequest(city="salvador", neighborhood="pituba")


@pytest.mark.parametrize(
    ("mode", "status", "expected"),
    [
        ("on", "vet_approved", {TRAINED}),
        ("on", "draft", {TRAINED, UNTRAINED}),
        ("warn", "vet_approved", {TRAINED, UNTRAINED}),
        ("off", "vet_approved", {TRAINED, UNTRAINED}),
    ],
)
def test_get_eligible_walkers_filters_only_when_blocking(tmp_path, monkeypatch, mode, status, expected):
    write_bundle(tmp_path, make_bundle(status=status))
    configure_training(monkeypatch, tmp_path, mode=mode)
    db = _subset_db()
    _profiles(db)
    result = {p.user_id for p in matching_service.get_eligible_walkers(_request(), db)}
    assert result == expected


def test_tenant_pool_filters_only_when_blocking(tmp_path, monkeypatch):
    write_bundle(tmp_path, make_bundle(status="vet_approved"))
    db = _full_db()
    db.add(Tenant(id="t-pool", name="Pool", slug="t-pool", status="active", plan="enterprise"))
    _profiles(db, with_users=True, tenant_id="t-pool")

    configure_training(monkeypatch, tmp_path, mode="warn")
    assert set(get_matching_pool_for_tenant(db, "t-pool")) == {TRAINED, UNTRAINED}
    configure_training(monkeypatch, tmp_path, mode="on")
    assert get_matching_pool_for_tenant(db, "t-pool") == [TRAINED]


def test_selected_walker_candidate_respects_gate(tmp_path, monkeypatch):
    write_bundle(tmp_path, make_bundle(status="vet_approved"))
    db = _full_db()
    _profiles(db, with_users=True)
    db.add(User(id="walker-sem-perfil", email="sp@t.com", password_hash="x", role="walker", is_active=True))
    # pet_id é NOT NULL no modelo (divergência com o plano, que usava None); SQLite não
    # aplica FK por padrão nesta suíte, então um id fictício basta para o teste do gate.
    walk = Walk(id="walk-sel", tutor_id="tutor-x", pet_id="pet-sel", scheduled_date="2099-07-10T10:00:00",
                duration_minutes=45, price=49.9, status="Agendado", address_snapshot="Rua Teste, 1",
                walker_selection_mode="manual", max_attempts=3)
    db.add(walk)
    db.commit()

    configure_training(monkeypatch, tmp_path, mode="on")
    assert _candidate_for_selected_walker(walk, UNTRAINED, db) is None
    assert _candidate_for_selected_walker(walk, "walker-sem-perfil", db) is None
    assert _candidate_for_selected_walker(walk, TRAINED, db)["walker_id"] == TRAINED

    configure_training(monkeypatch, tmp_path, mode="off")
    assert _candidate_for_selected_walker(walk, UNTRAINED, db)["walker_id"] == UNTRAINED

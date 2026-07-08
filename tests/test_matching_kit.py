"""Testes do kit aprovado exposto no payload do matching (T2 — visivel ao tutor).

Foco: matched_walker_payload (e o payload publico de rank_walkers) incluem a
chave "kit" no contrato {approved: bool, items: [{key, label, photo_url}]}.
So expoe itens quando existe WalkerKitSubmission com audit_status="approved" e
apenas os itens marcados available=True, com a primeira foto http(s) valida.

Padrao do repo: SQLite em memoria, so as tabelas usadas (sem app.main, sem
alembic, sem banco real). Espelha tests/test_matching_trust.py.
"""
import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.complaint import Complaint
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.schemas.matching import MatchingWalkerRequest
from app.services import matching_service as svc
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            TenantFeature.__table__,
            WalkerProfile.__table__,
            Walk.__table__,
            WalkerReview.__table__,
            Complaint.__table__,
            User.__table__,
            WalkerKitSubmission.__table__,
        ],
    )
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t-kit", name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.commit()
    return db


def _seed_walker(db, *, user_id="walker-1"):
    user = User(
        id=user_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        full_name="Passeador Kit",
        role="walker",
    )
    db.add(user)
    profile = WalkerProfile(
        id=f"wp-{user_id}",
        user_id=user_id,
        full_name="Passeador Kit",
        cpf=f"1234567890{user_id[-1]}",
        phone="71999999999",
        city="salvador",
        state="pituba",
        status="active",
        active_as_walker=True,
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _add_kit_submission(db, *, walker_user_id, audit_status="approved", items=None):
    row = WalkerKitSubmission(
        walker_user_id=walker_user_id,
        items_json=json.dumps(items or {}),
        audit_status=audit_status,
    )
    db.add(row)
    db.commit()
    return row


def _request(**kwargs) -> MatchingWalkerRequest:
    base = {"city": "salvador", "neighborhood": "pituba"}
    base.update(kwargs)
    return MatchingWalkerRequest(**base)


# --------------------------------------------------------------------------- #
# matched_walker_payload
# --------------------------------------------------------------------------- #
def test_matched_walker_payload_kit_approved_with_available_items():
    db = _db()
    profile = _seed_walker(db, user_id="walker-1")
    _add_kit_submission(
        db,
        walker_user_id="walker-1",
        items={
            "water": {"available": True, "photo_urls": ["https://cdn.test/water.jpg"]},
            "bowl": {"available": False, "photo_urls": []},
            "bags": {"available": True, "photo_urls": []},
        },
    )

    payload = svc.matched_walker_payload(profile, _request(), db)

    assert "kit" in payload
    kit = payload["kit"]
    assert kit["approved"] is True
    # so o item available=True entra; "bowl" (available=False) fica de fora.
    keys = {item["key"] for item in kit["items"]}
    assert keys == {"water", "bags"}
    water = next(item for item in kit["items"] if item["key"] == "water")
    assert water["photo_url"] == "https://cdn.test/water.jpg"
    assert water["label"] == "Agua"
    # "bags" nao tem foto -> photo_url None (nao quebra o contrato).
    bags = next(item for item in kit["items"] if item["key"] == "bags")
    assert bags["photo_url"] is None


def test_matched_walker_payload_kit_rejects_local_file_uri():
    """Foto file://... nao deveria existir persistida (WK-05 bloqueia no PUT /kit),
    mas o matching tambem filtra defensivamente — nunca devolve URI local ao tutor."""
    db = _db()
    profile = _seed_walker(db, user_id="walker-1")
    _add_kit_submission(
        db,
        walker_user_id="walker-1",
        items={"water": {"available": True, "photo_urls": ["file:///local/water.jpg"]}},
    )

    payload = svc.matched_walker_payload(profile, _request(), db)
    water = next(item for item in payload["kit"]["items"] if item["key"] == "water")
    assert water["photo_url"] is None


def test_matched_walker_payload_kit_not_approved_yields_empty():
    db = _db()
    profile = _seed_walker(db, user_id="walker-2")
    # submission existe mas ainda pending_review -> tutor nao ve nada.
    _add_kit_submission(
        db,
        walker_user_id="walker-2",
        audit_status="pending_review",
        items={"water": {"available": True, "photo_urls": ["https://cdn.test/water.jpg"]}},
    )

    payload = svc.matched_walker_payload(profile, _request(), db)
    assert payload["kit"] == {"approved": False, "items": []}


def test_matched_walker_payload_kit_no_submission_yields_empty():
    db = _db()
    profile = _seed_walker(db, user_id="walker-3")

    payload = svc.matched_walker_payload(profile, _request(), db)
    assert payload["kit"] == {"approved": False, "items": []}


# --------------------------------------------------------------------------- #
# rank_walkers expoe kit no payload PUBLICO (o que o tutor recebe)
# --------------------------------------------------------------------------- #
def test_rank_walkers_public_payload_includes_kit():
    db = _db()
    _seed_walker(db, user_id="walker-1")
    _add_kit_submission(
        db,
        walker_user_id="walker-1",
        items={"water": {"available": True, "photo_urls": ["https://cdn.test/water.jpg"]}},
    )
    _seed_walker(db, user_id="walker-2")  # sem kit

    result = svc.rank_walkers(_request(), db, debug=False)
    walkers = {item["walker_id"]: item for item in result["top_recommended"] + result["other_options"]}
    assert walkers["walker-1"]["kit"]["approved"] is True
    assert walkers["walker-2"]["kit"] == {"approved": False, "items": []}

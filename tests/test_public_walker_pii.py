import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import *  # noqa: F403
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes.walker import api_public_walkers, public_walkers


PUBLIC_WALKER_FIELDS = {
    "id",
    "partner_id",
    "name",
    "full_name",
    "role",
    "photo_url",
    "profile_photo_url",
    "status",
    "raw_status",
    "active_as_walker",
    "rating",
    "average_rating",
    "rating_average",
    "rating_avg",
    "rating_count",
    "reviews_count",
    "total_walks",
    "level",
    "reputation_score",
    "acceptance_rate",
    "cancellation_rate",
    "top_review_tags",
    "recent_review_comments",
    "recent_reviews",
    "city",
    "neighborhood",
    "bio",
    "walk_price",
    "verified",
    "walker_kit",
}

FORBIDDEN_PUBLIC_KEYS = {
    "cpf",
    "phone",
    "email",
    "rg",
    "birth_date",
    "document_url",
    "identity_front",
    "identity_back",
    "identity_document_front_url",
    "identity_document_back_url",
    "address_proof",
    "proof_of_address_url",
    "selfie_url",
    "internal_notes",
    "bank",
    "pix",
}


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()


def _forbidden_keys(payload):
    found = set()
    if isinstance(payload, dict):
        found.update(key for key in payload if key in FORBIDDEN_PUBLIC_KEYS)
        for value in payload.values():
            found.update(_forbidden_keys(value))
    elif isinstance(payload, list):
        for item in payload:
            found.update(_forbidden_keys(item))
    return found


def _seed_sensitive_walker(db):
    user = User(
        id="walker-public-safe",
        email="walker-public-safe@example.com",
        password_hash="x",
        full_name="Marina Costa",
        role="walker",
        is_active=True,
    )
    profile = WalkerProfile(
        id="profile-public-safe",
        user_id=user.id,
        full_name="Marina Costa",
        cpf="12345678909",
        phone="11999999999",
        birth_date="1990-01-01",
        rg="1234567",
        city="Salvador",
        state="Pituba",
        experience="Passeios urbanos",
        bio="Passeadora verificada com experiencia publica segura.",
        profile_photo_url="",
        document_url="/uploads/walker-documents/marina/identity_front-secret.jpg",
        identity_document_back_url="/uploads/walker-documents/marina/identity_back-secret.jpg",
        selfie_url="/uploads/walker-documents/marina/selfie-secret.jpg",
        proof_of_address_url="/uploads/walker-documents/marina/address_proof-secret.jpg",
        internal_notes="Nao deve aparecer em resposta publica.",
        status="active",
        active_as_walker=True,
    )
    db.add_all([user, profile])
    db.commit()


def _assert_public_walker_payload_is_safe(rows):
    assert rows
    for row in rows:
        assert set(row) <= PUBLIC_WALKER_FIELDS
        assert _forbidden_keys(row) == set()
        assert "cpf" not in row
        assert "phone" not in row
        assert "email" not in row
        assert "selfie_url" not in row
        assert "identity_front" not in row
        assert "identity_back" not in row
        assert "address_proof" not in row
        assert row["photo_url"] == ""
        assert row["profile_photo_url"] == ""


def test_walker_public_route_uses_safe_public_allowlist(db):
    _seed_sensitive_walker(db)

    payload = public_walkers(db=db)

    assert set(payload) == {"walkers"}
    _assert_public_walker_payload_is_safe(payload["walkers"])


def test_api_walkers_route_uses_safe_public_allowlist(db):
    _seed_sensitive_walker(db)

    rows = api_public_walkers(db=db)

    _assert_public_walker_payload_is_safe(rows)
